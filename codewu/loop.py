"""LLM call wrapper (streaming), the tool-use inner loop, auto-continue safety net.

`run_turn` keeps looping: every iteration calls the LLM (streaming the response
live to the terminal), then either dispatches the tool_calls it requested OR
(if no tool_calls but the text looks like a verbal promise) injects a
continue-nudge and loops again. Bounded by MAX_AUTO_CONTINUE.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)

from . import config
from . import ui
from .config import MODEL
from .tools import TOOLS_SCHEMA, dispatch_tool


# Exception types that indicate a transient network / proxy hiccup and are
# worth retrying with backoff. Anything else (bad request, auth, etc.) we let
# propagate immediately so the caller can roll back.
_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.WriteError,
    APIConnectionError,
    APITimeoutError,
    RateLimitError,
    InternalServerError,
    ConnectionError,
)


MAX_AUTO_CONTINUE = 3

PROMISE_TAIL_CHARS = (":", "：", "...", "。。。", "—")

# Action verbs that, paired with "I'll / let me / I will", indicate a real action
# (avoids matching "let me know" / "let me see").
_ACTION_VERBS = (
    r"fix|update|add|write|create|run|check|implement|change|modify|do|continue|"
    r"proceed|build|make|start|finish|edit|delete|remove|rename|move|install|"
    r"refactor|rewrite|test|verify|patch|apply|push|commit"
)

PROMISE_PATTERNS = [
    # English: I'll / I will / I'm going to + action verb
    re.compile(rf"\bi[\'’]ll\s+(?:{_ACTION_VERBS}|now)\b", re.IGNORECASE),
    re.compile(rf"\bi will\s+(?:{_ACTION_VERBS}|now)\b", re.IGNORECASE),
    re.compile(r"\bi[\'’]m going to\s+\w+", re.IGNORECASE),
    re.compile(r"\bi am going to\s+\w+", re.IGNORECASE),
    re.compile(rf"\blet me\s+(?:{_ACTION_VERBS})\b", re.IGNORECASE),
    # English: leading temporal adverb + future
    re.compile(r"\bnow,?\s+i[\'’]?(?:ll| will)\b", re.IGNORECASE),
    re.compile(r"\bnext,?\s+i[\'’]?(?:ll| will)\b", re.IGNORECASE),
    # Chinese: explicit first-person future promises
    re.compile(r"(?:我来|我现在|我去|我马上|我先去|我接下来|让我来|让我去)"),
    # Chinese: temporal adverb (+ optional "我"/"我们") + future-tense particle.
    # Catches "现在将其转换...", "接下来我会写入...", "下一步要改...", etc.
    re.compile(r"现在(?:我|我们)?(?:将|就|要|来|去|开始|准备|马上|会)"),
    re.compile(r"接下来(?:我|我们)?(?:将|要|会|来|准备|开始|是)"),
    re.compile(r"下一步(?:我|我们)?(?:将|要|会|是|准备|开始)"),
    re.compile(r"马上(?:我|我们)?(?:将|就|要|会|来|去|开始)"),
]


def looks_like_promise(text: str) -> bool:
    """Heuristic: does this final-text response look like a verbal promise without follow-through?"""
    if not text:
        return False
    s = text.strip()
    if s.endswith(PROMISE_TAIL_CHARS):
        return True
    if len(s) > 250:
        return False
    for pat in PROMISE_PATTERNS:
        if pat.search(s):
            return True
    return False


_THINKING_CLEAR = "\r" + " " * 30 + "\r"


def _stream_once(client: OpenAI, messages: list[dict[str, Any]]) -> dict[str, Any]:
    """One attempt at streaming the LLM response.

    Raises on any error — including transient network errors. The retrying
    wrapper `call_llm_stream` decides whether to retry. UX:
      1. Print "[~] thinking..." immediately so the user sees activity.
      2. On the first chunk with content, erase that line, print "[CodeWu] "
         and stream subsequent tokens inline.
      3. On the first chunk with a tool_call (with a name), erase "thinking"
         and print "[~] calling tool: <name>". Arguments stream silently into
         the accumulator and are dispatched whole at the end.
      4. After the stream finishes, print a single "[~] N→M tokens, Xs" stats line.
    """
    print(ui.style("[~] thinking...", ui.DIM), end="", flush=True)
    t0 = time.monotonic()

    content_buf: list[str] = []
    tool_calls_by_idx: dict[int, dict[str, Any]] = {}
    usage = None
    label_state = "thinking"  # "thinking" → "content" → "tool"

    def clear_thinking() -> None:
        print(_THINKING_CLEAR, end="", flush=True)

    stream = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        tools=TOOLS_SCHEMA,
        tool_choice="auto",
        stream=True,
    )
    for chunk in stream:
        if getattr(chunk, "usage", None) is not None:
            usage = chunk.usage
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta

        if delta.content:
            if label_state == "thinking":
                clear_thinking()
                print(ui.style("[CodeWu]", ui.BOLD, ui.CYAN), end=" ", flush=True)
                label_state = "content"
            print(delta.content, end="", flush=True)
            content_buf.append(delta.content)

        if delta.tool_calls:
            for tc in delta.tool_calls:
                idx = tc.index
                if idx not in tool_calls_by_idx:
                    tool_calls_by_idx[idx] = {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    }
                rec = tool_calls_by_idx[idx]
                if getattr(tc, "id", None):
                    rec["id"] = tc.id
                if getattr(tc, "type", None):
                    rec["type"] = tc.type
                if tc.function is not None:
                    if getattr(tc.function, "name", None):
                        rec["function"]["name"] = tc.function.name
                        if label_state == "thinking":
                            clear_thinking()
                        elif label_state == "content":
                            print()
                        label_state = "tool"
                        meta = (
                            ui.style("[~] calling tool: ", ui.DIM)
                            + ui.style(tc.function.name, ui.YELLOW)
                        )
                        print(meta)
                    if getattr(tc.function, "arguments", None):
                        rec["function"]["arguments"] += tc.function.arguments

    elapsed = time.monotonic() - t0
    if label_state == "content":
        print()
    if label_state == "thinking" and not content_buf and not tool_calls_by_idx:
        clear_thinking()
        print(ui.style("[CodeWu]", ui.BOLD, ui.CYAN) + " (empty)")
    if usage is not None:
        stats = f"{usage.prompt_tokens}→{usage.completion_tokens} tokens, {elapsed:.1f}s"
    else:
        stats = f"{elapsed:.1f}s"
    print(ui.style(f"[~] {stats}", ui.DIM))

    assistant_msg: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(content_buf) if content_buf else None,
    }
    if tool_calls_by_idx:
        assistant_msg["tool_calls"] = [
            tool_calls_by_idx[i] for i in sorted(tool_calls_by_idx.keys())
        ]
    return assistant_msg


def _call_once_nonstream(client: OpenAI, messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Single non-streaming attempt. Used as the FINAL fallback after the
    streaming retries are exhausted.

    Why: some proxies hand back 200 OK and then break the chunked encoding
    mid-body (we've seen `peer closed connection without sending complete
    message body` even when the proxy's own log shows 200 every time).
    Non-streaming gets the response as one atomic blob, which sometimes
    succeeds where streaming repeatedly fails — especially for responses
    that consist mostly of tool_call deltas.
    """
    print(ui.style("[~] thinking... (non-stream fallback)", ui.DIM), end="", flush=True)
    t0 = time.monotonic()

    resp = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        tools=TOOLS_SCHEMA,
        tool_choice="auto",
        # stream defaults to False here
    )

    elapsed = time.monotonic() - t0
    msg = resp.choices[0].message
    usage = getattr(resp, "usage", None)

    # Erase the "thinking..." line.
    print(_THINKING_CLEAR, end="", flush=True)

    content = msg.content or ""
    has_content = bool(content)
    has_tool_calls = bool(msg.tool_calls)

    if has_content:
        print(ui.style("[CodeWu]", ui.BOLD, ui.CYAN) + f" {content}")
    elif not has_tool_calls:
        print(ui.style("[CodeWu]", ui.BOLD, ui.CYAN) + " (empty)")

    if has_tool_calls:
        for tc in msg.tool_calls:
            meta = (
                ui.style("[~] calling tool: ", ui.DIM)
                + ui.style(tc.function.name, ui.YELLOW)
            )
            print(meta)

    if usage is not None:
        stats = f"{usage.prompt_tokens}→{usage.completion_tokens} tokens, {elapsed:.1f}s (non-stream)"
    else:
        stats = f"{elapsed:.1f}s (non-stream)"
    print(ui.style(f"[~] {stats}", ui.DIM))

    assistant_msg: dict[str, Any] = {
        "role": "assistant",
        "content": content if has_content else None,
    }
    if msg.tool_calls:
        assistant_msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in msg.tool_calls
        ]
    return assistant_msg


_FAILED_REQUESTS_DIR = Path.home() / ".codewu" / "failed-requests"


def _dump_failed_request(messages: list[dict[str, Any]], error_type: str, error_msg: str = "") -> Path | None:
    """Dump the failing request payload to ~/.codewu/failed-requests/<ts>-<err>.json.

    Lets the user share an exact reproducer so the failure can be replayed
    offline against the same proxy. Best-effort: any IO error is swallowed.
    """
    try:
        _FAILED_REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_err = re.sub(r"[^A-Za-z0-9]+", "-", error_type).strip("-") or "error"
        path = _FAILED_REQUESTS_DIR / f"{ts}-{safe_err}.json"
        payload = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "error_type": error_type,
            "error_message": error_msg,
            "model": MODEL,
            "tools_count": len(TOOLS_SCHEMA),
            "messages_count": len(messages),
            "messages": messages,
            "tools": TOOLS_SCHEMA,
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path
    except Exception:
        return None


def call_llm_stream(client: OpenAI, messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Smart retry/fallback wrapper around _stream_once / _call_once_nonstream.

    Strategy:
      Phase A — first stream attempt.
      Phase B — on RemoteProtocolError: skip backoff, immediately try
                non-stream (RPE is deterministic for the same input, proxy
                idle-timeout cuts long generations; retrying stream is
                futile and wastes the user's time).
      Phase C — on any other retryable error, or if Phase B also failed:
                standard backoff retry loop, ending with one final
                non-stream fallback.
    On give-up, re-raise the last exception with an actionable diagnostic.
    """
    max_retries = max(0, config.LLM_MAX_RETRIES)

    if max_retries == 0:
        return _stream_once(client, messages)

    last_exc: BaseException | None = None

    # ---- Phase A: first stream attempt ------------------------------------
    try:
        return _stream_once(client, messages)
    except httpx.RemoteProtocolError as e:
        print()
        print(ui.style(
            "[!] stream error: RemoteProtocolError (proxy cut the chunked body mid-stream)",
            ui.BOLD, ui.YELLOW,
        ))
        dumped = _dump_failed_request(messages, "RemoteProtocolError", str(e))
        if dumped is not None:
            print(ui.style(f"    dumped failing request → {dumped}", ui.DIM))
        print(ui.style(
            "    skipping backoff — RPE means the proxy SSE timeout fired on a slow "
            "generation; retrying stream gives the same result. Trying non-stream now.",
            ui.DIM,
        ))
        # ---- Phase B: immediate non-stream --------------------------------
        try:
            return _call_once_nonstream(client, messages)
        except _RETRYABLE_EXCEPTIONS as e2:
            print()
            print(ui.style(
                f"[!] non-stream also failed: {type(e2).__name__}: {str(e2)[:120]}",
                ui.BOLD, ui.RED,
            ))
            last_exc = e2
    except _RETRYABLE_EXCEPTIONS as e:
        print()
        print(ui.style(
            f"[!] stream error: {type(e).__name__}: {str(e)[:120]}",
            ui.BOLD, ui.RED,
        ))
        last_exc = e

    # ---- Phase C: standard retry loop with backoff ------------------------
    for attempt in range(max_retries):
        backoff = 2 ** attempt
        is_final = (attempt == max_retries - 1)
        next_mode = "non-stream" if is_final else "stream"
        print(ui.style(
            f"    retrying in {backoff}s as {next_mode} ({attempt + 1}/{max_retries})",
            ui.DIM,
        ))
        time.sleep(backoff)
        try:
            if is_final:
                return _call_once_nonstream(client, messages)
            return _stream_once(client, messages)
        except _RETRYABLE_EXCEPTIONS as e:
            last_exc = e
            print()
            print(ui.style(
                f"[!] {next_mode} error: {type(e).__name__}: {str(e)[:120]}",
                ui.BOLD, ui.RED,
            ))

    # ---- All attempts exhausted -------------------------------------------
    print(ui.style(
        f"    giving up after {max_retries + 2} attempts (1 stream + 1 nonstream + {max_retries} retries)",
        ui.DIM,
    ))
    print(ui.style(
        "    Probable cause: proxy SSE idle timeout on a slow generation. "
        "Try /new to start fresh, shorten the message, or set CODEWU_LLM_MAX_RETRIES=0.",
        ui.DIM,
    ))
    assert last_exc is not None
    raise last_exc


def run_turn(client: OpenAI, messages: list[dict[str, Any]]) -> None:
    """Drive the tool-use loop until the assistant produces a final text reply."""
    auto_continues = 0
    while True:
        assistant_msg = call_llm_stream(client, messages)
        messages.append(assistant_msg)

        tool_calls = assistant_msg.get("tool_calls")

        if not tool_calls:
            text = assistant_msg.get("content") or ""
            if auto_continues < MAX_AUTO_CONTINUE and looks_like_promise(text):
                # The text already streamed live; just print the auto-continue notice
                # and inject the nudge user message.
                auto_continues += 1
                warn = ui.style(
                    f"[~] auto-continue: model paused on a promise ({auto_continues}/{MAX_AUTO_CONTINUE})",
                    ui.BOLD, ui.YELLOW,
                )
                print(warn)
                messages.append({
                    "role": "user",
                    "content": "You stopped after a verbal promise without calling any tool. Call the tool now to perform the action you just announced. Do not stop until the work is done.",
                })
                continue
            # Final response already on screen via streaming; nothing more to print.
            return

        # Side-effect tool calls go through approve_or_skip inside dispatch_tool.
        # The "[~] calling tool: <name>" label was already printed during streaming.
        for tc in tool_calls:
            name = tc["function"]["name"]
            result = dispatch_tool(name, tc["function"]["arguments"])
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": json.dumps(result, ensure_ascii=False),
            })
