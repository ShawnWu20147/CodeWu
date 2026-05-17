"""LLM call wrapper, the tool-use inner loop, and the auto-continue safety net.

`run_turn` keeps looping: every iteration calls the LLM, then either dispatches
the tool_calls it requested OR (if no tool_calls but the text looks like a
verbal promise) injects a continue-nudge and loops again. Bounded by
MAX_AUTO_CONTINUE.
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
    re.compile(rf"\bi[\'’]ll\s+(?:{_ACTION_VERBS}|now)\b", re.IGNORECASE),
    re.compile(rf"\bi will\s+(?:{_ACTION_VERBS}|now)\b", re.IGNORECASE),
    re.compile(r"\bi[\'’]m going to\s+\w+", re.IGNORECASE),
    re.compile(r"\bi am going to\s+\w+", re.IGNORECASE),
    re.compile(rf"\blet me\s+(?:{_ACTION_VERBS})\b", re.IGNORECASE),
    re.compile(rf"\bnow i[\'’]ll\s+(?:{_ACTION_VERBS})\b", re.IGNORECASE),
    re.compile(r"(?:我来|我现在|我去|我马上|我先去|我接下来|让我来|让我去)"),
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


def call_llm(client: OpenAI, messages: list[dict[str, Any]]):
    """Wrap chat.completions.create with a thinking indicator + usage stats."""
    print(ui.style("[~] thinking...", ui.DIM), end="", flush=True)
    t0 = time.monotonic()
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS_SCHEMA,
            tool_choice="auto",
        )
    except Exception:
        print("\r" + ui.style("[~] (error)         ", ui.RED), flush=True)
        raise
    elapsed = time.monotonic() - t0
    usage = getattr(resp, "usage", None)
    if usage is not None:
        stats = f"{usage.prompt_tokens}→{usage.completion_tokens} tokens, {elapsed:.1f}s"
    else:
        stats = f"{elapsed:.1f}s"
    # \r overwrites "thinking..." on the same line, padding to clear trailing chars
    print("\r" + ui.style(f"[~] {stats}".ljust(48), ui.DIM), flush=True)
    return resp


def run_turn(client: OpenAI, messages: list[dict[str, Any]]) -> None:
    """Drive the tool-use loop until the assistant produces a final text reply."""
    auto_continues = 0
    while True:
        resp = call_llm(client, messages)
        msg = resp.choices[0].message

        assistant_msg: dict[str, Any] = {"role": "assistant", "content": msg.content}
        if msg.tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ]
        messages.append(assistant_msg)

        if not msg.tool_calls:
            text = msg.content or ""
            if auto_continues < MAX_AUTO_CONTINUE and looks_like_promise(text):
                label = ui.style("[CodeWu]", ui.BOLD, ui.CYAN)
                print(f"\n{label} {text}")
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
            label = ui.style("[CodeWu]", ui.BOLD, ui.CYAN)
            print(f"\n{label} {text or '(empty)'}\n")
            return

        for tc in msg.tool_calls:
            name = tc.function.name
            meta = ui.style(f"[~] calling tool: ", ui.DIM) + ui.style(name, ui.YELLOW)
            print(f"\n{meta}")
            result = dispatch_tool(name, tc.function.arguments)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, ensure_ascii=False),
            })
