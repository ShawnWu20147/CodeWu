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
from typing import Any

from openai import OpenAI

from . import ui
from .config import MODEL
from .tools import TOOLS_SCHEMA, dispatch_tool


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


def call_llm_stream(client: OpenAI, messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Stream the LLM response, printing content live and accumulating tool_calls.

    Returns the assembled assistant message dict (role, content, optional tool_calls).
    UX:
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
    # "thinking" until first chunk; "content" once we start streaming text;
    # "tool" once we have printed at least one [~] calling tool: <name> label.
    label_state = "thinking"

    def clear_thinking() -> None:
        print(_THINKING_CLEAR, end="", flush=True)

    try:
        stream = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS_SCHEMA,
            tool_choice="auto",
            stream=True,
        )
        for chunk in stream:
            # Usage may ride on a final chunk; some proxies put it on a choice-less chunk.
            if getattr(chunk, "usage", None) is not None:
                usage = chunk.usage
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            # --- content tokens ---
            if delta.content:
                if label_state == "thinking":
                    clear_thinking()
                    print(ui.style("[CodeWu]", ui.BOLD, ui.CYAN), end=" ", flush=True)
                    label_state = "content"
                print(delta.content, end="", flush=True)
                content_buf.append(delta.content)

            # --- tool_calls deltas (accumulate by index) ---
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
                            # First time we see a name for this tool_call → print its label.
                            if label_state == "thinking":
                                clear_thinking()
                            elif label_state == "content":
                                print()  # newline to separate from streamed text
                            label_state = "tool"
                            meta = (
                                ui.style("[~] calling tool: ", ui.DIM)
                                + ui.style(tc.function.name, ui.YELLOW)
                            )
                            print(meta)
                        if getattr(tc.function, "arguments", None):
                            rec["function"]["arguments"] += tc.function.arguments
    except Exception:
        print("\r" + ui.style("[~] (stream error)         ", ui.BOLD, ui.RED), flush=True)
        raise

    elapsed = time.monotonic() - t0

    # If we streamed content, terminate that line with a newline.
    if label_state == "content":
        print()
    # If the stream produced absolutely nothing visible, clear "thinking" + note empty.
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
