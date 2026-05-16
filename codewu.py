"""CodeWu — a minimal coding agent prototype.

Chat Completions API + 4 local tools (read_file / write_file / list_dir / run_cmd)
with per-call y/n approval for any side-effect tool. Session messages are
persisted to .codewu/sessions/ for resume.

See SPEC.md for the design contract.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import secrets
import subprocess
import sys
import re
import textwrap
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from openai import OpenAI


# Force UTF-8 on Windows consoles so emoji / box-drawing chars don't blow up.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("CODEWU_BASE_URL", "http://localhost:4141/v1")
MODEL = os.environ.get("CODEWU_MODEL", "claude-opus-4.6-1m")
API_KEY = os.environ.get("CODEWU_API_KEY", "placeholder-not-used-by-proxy")

CWD = Path(os.getcwd()).resolve()
SESSION_DIR = Path.home() / ".codewu" / "sessions"  # global, shared across cwds
MAX_OUTPUT_BYTES = 8 * 1024  # truncate tool outputs to 8 KB
TODAY = date.today().isoformat()

IS_WINDOWS = platform.system() == "Windows"
SHELL_HINT = "PowerShell" if IS_WINDOWS else "POSIX sh"

# Mutated by main() based on --allow-all CLI flag.
ALLOW_ALL = False


SYSTEM_PROMPT = f"""You are CodeWu, a coding agent that builds JS and Python programs.

ENVIRONMENT
- Working directory: {CWD}
- Today's date: {TODAY}
- Host OS: {platform.system()} {platform.release()}
- run_cmd executes via: {SHELL_HINT}

RULES
- You can develop in JavaScript and Python only.
- All file paths you pass to tools are resolved relative to the working directory above. Stay inside it.
- Do NOT ask the user to approve tool calls in your text — the CLI intercepts and asks for approval automatically. Just call the tool.
- Before reporting a task as done, run a verification step (execute the program, run tests, or inspect the resulting file).
- Before non-trivial changes, explore the current state with list_dir / read_file.
- Keep messages short. Prefer doing over describing.

TURN COMPLETION
- A turn ends ONLY when (a) the work is fully done and verified, or (b) you have a specific question the user must answer before you can proceed.
- Verbal-only responses such as "I'll fix it", "let me update X", "我来修正一下", "我来加上" — without a tool call in the SAME response — are a bug. Do not produce them.
- If you say you will do something, the next action in this same turn MUST be the tool call that does it. Do not stop and wait for the user to say "continue".
- "Continuing" is your job: once you have decided on the next step, just do it.
- NEVER end a response with `:`, `：`, `...`, or `。。。`. These signal "to be continued", but a response with no tool call ends the turn. Either continue with a tool call in the same response, or finish your sentence properly.
"""


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _ok(result: str) -> dict[str, Any]:
    if len(result) > MAX_OUTPUT_BYTES:
        result = result[:MAX_OUTPUT_BYTES] + f"\n... [truncated, {len(result)} bytes total]"
    return {"ok": True, "result": result, "error": None}


def _err(error: str) -> dict[str, Any]:
    return {"ok": False, "result": "", "error": error}


def _resolve(path: str) -> Path:
    p = (CWD / path).resolve() if not os.path.isabs(path) else Path(path).resolve()
    return p


def tool_read_file(path: str) -> dict[str, Any]:
    try:
        p = _resolve(path)
        if not p.exists():
            return _err(f"file not found: {p}")
        if not p.is_file():
            return _err(f"not a file: {p}")
        return _ok(p.read_text(encoding="utf-8", errors="replace"))
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


def tool_write_file(path: str, content: str) -> dict[str, Any]:
    try:
        p = _resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return _ok(f"wrote {len(content)} chars to {p}")
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


def tool_list_dir(path: str) -> dict[str, Any]:
    try:
        p = _resolve(path)
        if not p.exists():
            return _err(f"path not found: {p}")
        if not p.is_dir():
            return _err(f"not a directory: {p}")
        lines = []
        for child in sorted(p.iterdir()):
            kind = "DIR " if child.is_dir() else "FILE"
            size = "" if child.is_dir() else f"  {child.stat().st_size}B"
            lines.append(f"{kind}  {child.name}{size}")
        return _ok("\n".join(lines) if lines else "(empty)")
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


def tool_run_cmd(command: str) -> dict[str, Any]:
    try:
        if IS_WINDOWS:
            argv = ["powershell", "-NoProfile", "-Command", command]
        else:
            argv = ["sh", "-c", command]
        proc = subprocess.run(
            argv,
            cwd=str(CWD),
            capture_output=True,
            text=True,
            timeout=120,
        )
        out = (
            f"exit_code: {proc.returncode}\n"
            f"--- stdout ---\n{proc.stdout}"
            f"--- stderr ---\n{proc.stderr}"
        )
        return _ok(out)
    except subprocess.TimeoutExpired:
        return _err("command timed out after 120s")
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Tool registry (OpenAI tool calling JSON Schema)
# ---------------------------------------------------------------------------

TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the full text content of a file. Path is relative to the working directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path, relative to the working directory."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write (or overwrite) a text file. Parent directories are created if missing. Requires user approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path, relative to the working directory."},
                    "content": {"type": "string", "description": "Full file content to write."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List one level of entries (files and directories) at the given path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path, relative to the working directory. Use '.' for cwd."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_cmd",
            "description": (
                f"Run a shell command via {SHELL_HINT} in the working directory. "
                "Captures stdout, stderr and exit code. Requires user approval."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": f"The {SHELL_HINT} command to execute."},
                },
                "required": ["command"],
            },
        },
    },
]


TOOLS_READONLY = {"read_file", "list_dir"}  # auto-approved
TOOLS_SIDE_EFFECT = {"write_file", "run_cmd"}  # require y/n


# ---------------------------------------------------------------------------
# Approval flow + dispatcher
# ---------------------------------------------------------------------------


def _preview_write(path: str, content: str) -> str:
    lines = content.splitlines()
    n_lines = len(lines)
    n_bytes = len(content.encode("utf-8"))

    target = _resolve(path)
    if target.exists() and target.is_file():
        try:
            old_bytes = target.stat().st_size
        except OSError:
            old_bytes = 0
        tag = f"OVERWRITE  {old_bytes}B → {n_bytes}B"
        head_n = 5
    else:
        tag = "NEW"
        head_n = 10

    head = "\n".join(f"| {ln}" for ln in lines[:head_n])
    tail = f"\n+ {n_lines - head_n} more lines" if n_lines > head_n else ""
    return (
        f"[Tool] write_file  path={path}  size={n_bytes}B  lines={n_lines}  ({tag})\n"
        f"{head}{tail}"
    )


def approve_or_skip(name: str, args: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Returns (approved, possibly_modified_args). ALLOW_ALL bypasses the prompt but keeps preview."""
    if name == "write_file":
        print(_preview_write(args.get("path", ""), args.get("content", "")))
        if ALLOW_ALL:
            print("Approve write? [auto-approved]")
            return True, args
        while True:
            choice = input("Approve write? [y/n]: ").strip().lower()
            if choice in {"y", "yes"}:
                return True, args
            if choice in {"n", "no", ""}:
                return False, args
    elif name == "run_cmd":
        if ALLOW_ALL:
            print(f"[Cmd] {args.get('command', '')}")
            print("Approve cmd? [auto-approved]")
            return True, args
        while True:
            cmd = args.get("command", "")
            print(f"[Cmd] {cmd}")
            choice = input("Approve cmd? [y/n/edit]: ").strip().lower()
            if choice in {"y", "yes"}:
                return True, args
            if choice in {"n", "no", ""}:
                return False, args
            if choice in {"e", "edit"}:
                new_cmd = input("Edit cmd: ").strip()
                if new_cmd:
                    args = dict(args, command=new_cmd)
                continue
    return True, args  # readonly tools: auto-approve


def dispatch_tool(name: str, raw_args: str) -> dict[str, Any]:
    try:
        args = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError as e:
        return _err(f"invalid JSON arguments: {e}")

    if name in TOOLS_SIDE_EFFECT:
        approved, args = approve_or_skip(name, args)
        if not approved:
            return _err("user denied this tool call")

    if name == "read_file":
        return tool_read_file(**args)
    if name == "write_file":
        return tool_write_file(**args)
    if name == "list_dir":
        return tool_list_dir(**args)
    if name == "run_cmd":
        return tool_run_cmd(**args)
    return _err(f"unknown tool: {name}")


# ---------------------------------------------------------------------------
# Session persistence
# ---------------------------------------------------------------------------


def new_session_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def save_session(session_id: str, messages: list[dict[str, Any]]) -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": session_id,
        "model": MODEL,
        "cwd": str(CWD),
        "messages": messages,
    }
    path = SESSION_DIR / f"{session_id}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    latest = SESSION_DIR / "latest.json"
    latest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_session(session_id: str | None) -> tuple[str, list[dict[str, Any]]]:
    if session_id is None:
        path = SESSION_DIR / "latest.json"
    else:
        path = SESSION_DIR / f"{session_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"no such session: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["session_id"], payload["messages"]


def list_sessions() -> list[tuple[str, str, str]]:
    """Returns list of (session_id, cwd, first_user_msg_preview)."""
    if not SESSION_DIR.exists():
        return []
    rows: list[tuple[str, str, str]] = []
    for p in sorted(SESSION_DIR.glob("*.json")):
        if p.name == "latest.json":
            continue
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
            sid = payload.get("session_id", p.stem)
            cwd = payload.get("cwd", "?")
            first_user = next(
                (m.get("content", "") for m in payload.get("messages", []) if m.get("role") == "user"),
                "",
            )
            first_user = (first_user or "").replace("\n", " ")[:60]
            rows.append((sid, cwd, first_user))
        except Exception:
            rows.append((p.stem, "?", "(unreadable)"))
    return rows


# ---------------------------------------------------------------------------
# Conversation loop
# ---------------------------------------------------------------------------


MAX_AUTO_CONTINUE = 3
PROMISE_TAIL_CHARS = (":", "：", "...", "。。。", "—")

# Action verbs that, when paired with "I'll / let me / I will", indicate a real action promise
# (vs. weak phrases like "let me know" or "let me see").
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
    print("[~] thinking...", end="", flush=True)
    t0 = time.monotonic()
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS_SCHEMA,
            tool_choice="auto",
        )
    except Exception:
        print("\r[~] (error)         ", flush=True)
        raise
    elapsed = time.monotonic() - t0
    usage = getattr(resp, "usage", None)
    if usage is not None:
        stats = f"{usage.prompt_tokens}→{usage.completion_tokens} tokens, {elapsed:.1f}s"
    else:
        stats = f"{elapsed:.1f}s"
    # \r overwrites "thinking..." on the same line, padding to clear trailing chars
    print(f"\r[~] {stats}".ljust(48), flush=True)
    return resp


def run_turn(client: OpenAI, messages: list[dict[str, Any]]) -> None:
    """Run inner tool-use loop until the assistant produces a final text reply."""
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
                print(f"\n[CodeWu] {text}")
                auto_continues += 1
                print(f"[~] auto-continue: model paused on a promise ({auto_continues}/{MAX_AUTO_CONTINUE})")
                messages.append({
                    "role": "user",
                    "content": "You stopped after a verbal promise without calling any tool. Call the tool now to perform the action you just announced. Do not stop until the work is done.",
                })
                continue
            print(f"\n[CodeWu] {text or '(empty)'}\n")
            return

        for tc in msg.tool_calls:
            name = tc.function.name
            print(f"\n[~] calling tool: {name}")
            result = dispatch_tool(name, tc.function.arguments)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, ensure_ascii=False),
            })


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------


def _print_exit_hint(session_id: str, messages: list[dict[str, Any]]) -> None:
    has_content = any(m.get("role") in {"user", "assistant"} for m in messages)
    if not has_content:
        return  # empty session — nothing to persist or resume
    try:
        save_session(session_id, messages)
    except Exception as e:
        print(f"\n[!] failed to persist session: {type(e).__name__}: {e}")
        return
    print(f"\nSession saved: {session_id}")
    print(f"Resume:        codewu --resume {session_id}")


def _cmd_exit(arg, sid, msgs):
    _print_exit_hint(sid, msgs)
    return sid, msgs, True


def _cmd_sessions(arg, sid, msgs):
    rows = list_sessions()
    if not rows:
        print("(no saved sessions)")
        return sid, msgs, False
    sid_w = max(len(r[0]) for r in rows)
    cwd_w = min(50, max(len(r[1]) for r in rows))
    print(f"  {'session_id':<{sid_w}}  {'cwd':<{cwd_w}}  first message")
    print(f"  {'-' * sid_w}  {'-' * cwd_w}  {'-' * 12}")
    for s, cwd, hint in rows:
        cwd_disp = cwd if len(cwd) <= cwd_w else "..." + cwd[-(cwd_w - 3):]
        print(f"  {s:<{sid_w}}  {cwd_disp:<{cwd_w}}  {hint}")
    return sid, msgs, False


def _cmd_resume(arg, sid, msgs):
    try:
        new_sid, new_msgs = load_session(arg or None)
    except FileNotFoundError as e:
        print(f"[!] {e}")
        return sid, msgs, False
    print(f"[*] resumed session {new_sid} ({len(new_msgs)} messages)")
    print_history(new_msgs)
    return new_sid, new_msgs, False


def _cmd_new(arg, sid, msgs):
    new_sid = new_session_id()
    print(f"[*] started new session {new_sid}")
    return new_sid, [{"role": "system", "content": SYSTEM_PROMPT}], False


def _cmd_dump(arg, sid, msgs):
    print(f"messages count: {len(msgs)}")
    for m in msgs[-3:]:
        role = m.get("role", "?")
        content = m.get("content")
        preview = (str(content) if content is not None else "<no content>")[:200]
        print(f"  [{role}] {preview}")
    return sid, msgs, False


def _cmd_help(arg, sid, msgs):
    width = max(len(name) + (len(meta["arg_hint"]) + 1 if meta["arg_hint"] else 0)
                for name, meta in SLASH_COMMANDS.items())
    print("Available commands:")
    for name, meta in SLASH_COMMANDS.items():
        label = name + ((" " + meta["arg_hint"]) if meta["arg_hint"] else "")
        print(f"  {label:<{width}}  {meta['desc']}")
    return sid, msgs, False


SLASH_COMMANDS: dict[str, dict[str, Any]] = {
    "/help":     {"arg_hint": "",     "desc": "Show this help",                                  "handler": _cmd_help},
    "/resume":   {"arg_hint": "[id]", "desc": "Resume a saved session (latest if id omitted)",   "handler": _cmd_resume},
    "/sessions": {"arg_hint": "",     "desc": "List saved sessions in .codewu/sessions/",        "handler": _cmd_sessions},
    "/new":      {"arg_hint": "",     "desc": "Discard current session and start fresh",         "handler": _cmd_new},
    "/dump":     {"arg_hint": "",     "desc": "Print last 3 messages (debug)",                   "handler": _cmd_dump},
    "/exit":     {"arg_hint": "",     "desc": "Quit CodeWu (session is auto-saved)",             "handler": _cmd_exit},
    "/quit":     {"arg_hint": "",     "desc": "Alias of /exit",                                  "handler": _cmd_exit},
}


def handle_slash(line: str, session_id: str, messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]], bool]:
    parts = line.strip().split(maxsplit=1)
    cmd = parts[0]
    arg = parts[1] if len(parts) > 1 else ""

    if cmd in SLASH_COMMANDS:
        return SLASH_COMMANDS[cmd]["handler"](arg, session_id, messages)

    if cmd != "/":
        print(f"[!] unknown command: {cmd}")
    return _cmd_help(arg, session_id, messages)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Bang shortcut: run a shell command directly, append output to context.
# ---------------------------------------------------------------------------


def handle_bang(line: str, messages: list[dict[str, Any]]) -> None:
    cmd = line[1:].strip()
    if not cmd:
        print("[!] usage: !<command>   e.g. !git status   (runs in shell, no approval, output appended to context)")
        return
    result = tool_run_cmd(cmd)
    output = result.get("result") or result.get("error") or ""
    print(output)
    messages.append({
        "role": "user",
        "content": f"[I ran: {cmd}]\n{output}",
    })


HISTORY_TRUNCATE = 500


def _truncate(s: str, n: int = HISTORY_TRUNCATE) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + f" ...[+{len(s) - n} chars]"


def _summarize_tool_call(tc: dict[str, Any]) -> str:
    name = tc.get("function", {}).get("name", "?")
    raw = tc.get("function", {}).get("arguments", "")
    try:
        args = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        args = {"_raw": raw}
    parts = []
    for k, v in args.items():
        v_str = str(v).replace("\n", " ")
        if len(v_str) > 80:
            v_str = v_str[:80] + "..."
        parts.append(f"{k}={v_str!r}" if isinstance(v, str) else f"{k}={v_str}")
    return f"{name}({', '.join(parts)})"


def print_history(messages: list[dict[str, Any]]) -> None:
    visible = [m for m in messages if m.get("role") != "system"]
    print(f"\n--- session history ({len(visible)} messages, excluding system) ---")
    for m in visible:
        role = m.get("role")
        if role == "user":
            print(f"\n> {_truncate(m.get('content', ''))}")
        elif role == "assistant":
            content = m.get("content")
            if content:
                print(f"\n[CodeWu] {_truncate(content)}")
            for tc in m.get("tool_calls", []) or []:
                print(f"[~] called {_summarize_tool_call(tc)}")
        elif role == "tool":
            # results are implied by the next assistant message; skip
            continue
    print("--- end history ---\n")


def banner(session_id: str, resumed: bool) -> None:
    mode = "resumed" if resumed else "new"
    print(textwrap.dedent(f"""
        ╭───────────────────────────────────────────────╮
        │  CodeWu — minimal coding agent (prototype)    │
        ╰───────────────────────────────────────────────╯
          model:   {MODEL}
          base:    {BASE_URL}
          cwd:     {CWD}
          shell:   {SHELL_HINT}
          session: {session_id}  ({mode})
        Type your request, or /help for commands.
    """).strip())
    if ALLOW_ALL:
        print("  ⚠  --allow-all is set: tool approval prompts are bypassed.")


def main() -> int:
    ap = argparse.ArgumentParser(description="CodeWu — minimal coding agent")
    ap.add_argument(
        "--resume",
        nargs="?",
        const="__latest__",
        default=None,
        help="Resume a session: --resume to load latest, --resume <id> to load specific.",
    )
    ap.add_argument(
        "--allow-all",
        action="store_true",
        help="Skip y/n approval for all side-effect tools. Previews still print. Use with care.",
    )
    args = ap.parse_args()

    global ALLOW_ALL
    ALLOW_ALL = args.allow_all

    if args.resume is None:
        session_id = new_session_id()
        messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        resumed = False
    else:
        sid_arg = None if args.resume == "__latest__" else args.resume
        try:
            session_id, messages = load_session(sid_arg)
            resumed = True
        except FileNotFoundError as e:
            print(f"[!] {e}", file=sys.stderr)
            return 1

    client = OpenAI(base_url=BASE_URL, api_key=API_KEY)

    banner(session_id, resumed)
    if resumed:
        print_history(messages)

    while True:
        try:
            line = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            _print_exit_hint(session_id, messages)
            break
        if not line:
            continue

        if line.startswith("/"):
            session_id, messages, should_exit = handle_slash(line, session_id, messages)
            if should_exit:
                break
            continue

        if line.startswith("!"):
            handle_bang(line, messages)
            save_session(session_id, messages)
            continue

        user_msg_idx = len(messages)
        messages.append({"role": "user", "content": line})
        try:
            run_turn(client, messages)
        except KeyboardInterrupt:
            del messages[user_msg_idx:]
            print("\n[!] turn interrupted — rolled back to before this user message")
            continue
        except Exception as e:
            print(f"[!] turn failed: {type(e).__name__}: {e}")
            del messages[user_msg_idx:]
            continue
        save_session(session_id, messages)

    return 0


if __name__ == "__main__":
    sys.exit(main())
