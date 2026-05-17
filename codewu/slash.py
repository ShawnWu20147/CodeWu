"""Slash commands: registry + handlers.

Each handler has signature (arg, session_id, messages) -> (session_id, messages, should_exit).
To add a new command, define a handler function and register it in SLASH_COMMANDS.
"""

from __future__ import annotations

from typing import Any

from . import config
from . import ui
from .config import SYSTEM_PROMPT
from .session import (
    list_sessions,
    load_session,
    new_session_id,
    print_history,
    save_session,
)


def print_exit_hint(session_id: str, messages: list[dict[str, Any]]) -> None:
    """Print the resume hint at quit time, after one final save.

    Empty sessions (no user/assistant content) exit silently — there is nothing
    to resume, and printing "Session saved" would be misleading.
    """
    has_content = any(m.get("role") in {"user", "assistant"} for m in messages)
    if not has_content:
        return
    try:
        save_session(session_id, messages)
    except Exception as e:
        err = ui.style(f"\n[!] failed to persist session: {type(e).__name__}: {e}", ui.BOLD, ui.RED)
        print(err)
        return
    print()
    print("Session saved: " + ui.style(session_id, ui.CYAN))
    print("Resume:        " + ui.style(f"codewu --resume {session_id}", ui.CYAN))


def _cmd_exit(arg, sid, msgs):
    print_exit_hint(sid, msgs)
    return sid, msgs, True


def _cmd_sessions(arg, sid, msgs):
    rows = list_sessions()
    if not rows:
        print(ui.style("(no saved sessions)", ui.DIM))
        return sid, msgs, False
    sid_w = max(len(r[0]) for r in rows)
    cwd_w = min(50, max(len(r[1]) for r in rows))
    header = ui.style(
        f"  {'session_id':<{sid_w}}  {'cwd':<{cwd_w}}  first message",
        ui.BOLD,
    )
    rule = ui.style(f"  {'-' * sid_w}  {'-' * cwd_w}  {'-' * 12}", ui.DIM)
    print(header)
    print(rule)
    for s, cwd, hint in rows:
        cwd_disp = cwd if len(cwd) <= cwd_w else "..." + cwd[-(cwd_w - 3):]
        # Pad first (based on visible width), then style — otherwise ANSI codes break alignment.
        sid_styled = ui.style(f"{s:<{sid_w}}", ui.CYAN)
        print(f"  {sid_styled}  {cwd_disp:<{cwd_w}}  {hint}")
    return sid, msgs, False


def _cmd_resume(arg, sid, msgs):
    try:
        new_sid, new_msgs = load_session(arg or None)
    except FileNotFoundError as e:
        print(ui.style(f"[!] {e}", ui.BOLD, ui.RED))
        return sid, msgs, False
    print(ui.style(f"[*] resumed session {new_sid} ({len(new_msgs)} messages)", ui.BOLD, ui.GREEN))
    print_history(new_msgs)
    return new_sid, new_msgs, False


def _cmd_new(arg, sid, msgs):
    new_sid = new_session_id()
    print(ui.style(f"[*] started new session {new_sid}", ui.BOLD, ui.GREEN))
    return new_sid, [{"role": "system", "content": SYSTEM_PROMPT}], False


def _cmd_dump(arg, sid, msgs):
    print(ui.style(f"messages count: {len(msgs)}", ui.BOLD))
    for m in msgs[-3:]:
        role = m.get("role", "?")
        content = m.get("content")
        preview = (str(content) if content is not None else "<no content>")[:200]
        role_styled = ui.style(f"[{role}]", ui.DIM)
        print(f"  {role_styled} {preview}")
    return sid, msgs, False


def _cmd_config(arg, sid, msgs):
    rows = config.config_summary()
    if config.CONFIG_LOAD_ERROR:
        print(ui.style(f"[!] config file load error: {config.CONFIG_LOAD_ERROR}", ui.BOLD, ui.RED))
    print(ui.style("Effective configuration:", ui.BOLD))
    key_w = max(len(k) for k, _, _ in rows)
    val_w = max(len(v) for _, v, _ in rows)
    for key, value, src in rows:
        val_styled = ui.style(f"{value:<{val_w}}", ui.CYAN)
        src_styled = ui.style(f"({src})", ui.DIM)
        print(f"  {key:<{key_w}}  {val_styled}  {src_styled}")
    print()
    path_str = ui.style(str(config.CONFIG_FILE), ui.CYAN)
    if config.CONFIG_FILE.exists():
        status = ui.style("(exists — edit to override defaults)", ui.DIM)
    else:
        status = ui.style("(not present — create this file to override defaults)", ui.DIM)
    print(f"Config file: {path_str}  {status}")
    print(ui.style(
        "  Precedence: environment variable > config file > built-in default.",
        ui.DIM,
    ))
    return sid, msgs, False


def _cmd_help(arg, sid, msgs):
    width = max(
        len(name) + (len(meta["arg_hint"]) + 1 if meta["arg_hint"] else 0)
        for name, meta in SLASH_COMMANDS.items()
    )
    print(ui.style("Available commands:", ui.BOLD))
    for name, meta in SLASH_COMMANDS.items():
        label_text = name + ((" " + meta["arg_hint"]) if meta["arg_hint"] else "")
        label = ui.style(f"{label_text:<{width}}", ui.CYAN)
        print(f"  {label}  {meta['desc']}")
    return sid, msgs, False


SLASH_COMMANDS: dict[str, dict[str, Any]] = {
    "/help":     {"arg_hint": "",     "desc": "Show this help",                                  "handler": _cmd_help},
    "/config":   {"arg_hint": "",     "desc": "Show effective config (env > ~/.codewu/config.json > default)", "handler": _cmd_config},
    "/resume":   {"arg_hint": "[id]", "desc": "Resume a saved session (latest if id omitted)",   "handler": _cmd_resume},
    "/sessions": {"arg_hint": "",     "desc": "List saved sessions in ~/.codewu/sessions/",      "handler": _cmd_sessions},
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
        print(ui.style(f"[!] unknown command: {cmd}", ui.BOLD, ui.RED))
    return _cmd_help(arg, session_id, messages)
