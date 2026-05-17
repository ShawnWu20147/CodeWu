"""Slash commands: registry + handlers.

Each handler has signature (arg, session_id, messages) -> (session_id, messages, should_exit).
To add a new command, define a handler function and register it in SLASH_COMMANDS.
"""

from __future__ import annotations

from typing import Any

from . import bg
from . import config
from . import ui
from .config import SYSTEM_PROMPT
from .session import (
    format_age,
    list_all_sessions,
    list_sessions_for_project,
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
    """Default: sessions for the current cwd. `/sessions all` to span projects."""
    scope = arg.strip().lower()
    if scope in ("all", "*"):
        rows = list_all_sessions()
        scope_label = "(all projects)"
        show_cwd = True
    else:
        rows = list_sessions_for_project()
        scope_label = f"(cwd: {config.CWD})"
        show_cwd = False

    if not rows:
        print(ui.style(f"(no saved sessions {scope_label})", ui.DIM))
        print(ui.style("    Tip: /sessions all  to see sessions from other projects", ui.DIM))
        return sid, msgs, False

    print(ui.style(f"Sessions {scope_label}: {len(rows)} total", ui.BOLD))
    print()

    sid_w = max(len(r["session_id"]) for r in rows)
    age_w = max(len(format_age(r["session_id"])) for r in rows)
    cwd_w = min(50, max(len(r.get("cwd", "")) for r in rows)) if show_cwd else 0

    for row in rows:
        s = row["session_id"]
        age = format_age(s)
        nmsg = row.get("n_messages", 0)
        first = row.get("first_user_msg", "")
        if len(first) > 80:
            first = first[:80] + "…"
        s_styled = ui.style(f"{s:<{sid_w}}", ui.CYAN)
        age_styled = ui.style(f"{age:<{age_w}}", ui.DIM)
        meta = ui.style(f"{nmsg} msgs", ui.DIM)
        if show_cwd:
            cwd_str = row.get("cwd", "?")
            cwd_disp = cwd_str if len(cwd_str) <= cwd_w else "..." + cwd_str[-(cwd_w - 3):]
            cwd_styled = ui.style(f"{cwd_disp:<{cwd_w}}", ui.DIM)
            print(f"  {s_styled}  {age_styled}  {meta}  {cwd_styled}")
        else:
            print(f"  {s_styled}  {age_styled}  {meta}")
        if first:
            print(ui.style(f"      {first!r}", ui.DIM))
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


def _cmd_bg(arg, sid, msgs):
    parts = arg.strip().split(maxsplit=1)
    sub = parts[0].lower() if parts else "list"
    rest = parts[1] if len(parts) > 1 else ""

    if sub in ("", "list", "ls"):
        procs = bg.list_alive()
        if not procs:
            print(ui.style("(no background processes running)", ui.DIM))
            return sid, msgs, False
        print(ui.style(f"Background processes ({len(procs)} running):", ui.BOLD))
        for p in procs:
            pid_str = ui.style(f"pid={p['pid']}", ui.CYAN)
            print(f"  {pid_str}  started={p.get('started_at', '?')}")
            print(ui.style(f"    cmd: {p.get('command', '')[:120]}", ui.DIM))
            print(ui.style(f"    cwd: {p.get('cwd', '')}", ui.DIM))
            print(ui.style(f"    log: {p.get('log_file', '')}", ui.DIM))
        return sid, msgs, False

    if sub == "stop":
        if not rest.strip():
            print(ui.style("[!] usage: /bg stop <pid>", ui.BOLD, ui.RED))
            return sid, msgs, False
        try:
            pid = int(rest.strip())
        except ValueError:
            print(ui.style(f"[!] pid must be a number, got: {rest!r}", ui.BOLD, ui.RED))
            return sid, msgs, False
        killed = bg.stop(pid)
        if killed:
            print(ui.style(f"[*] killed bg process pid={pid}", ui.BOLD, ui.GREEN))
        else:
            print(ui.style(f"(pid={pid} was not alive; state cleaned)", ui.DIM))
        return sid, msgs, False

    if sub == "log":
        if not rest.strip():
            print(ui.style("[!] usage: /bg log <pid>", ui.BOLD, ui.RED))
            return sid, msgs, False
        try:
            pid = int(rest.strip())
        except ValueError:
            print(ui.style(f"[!] pid must be a number, got: {rest!r}", ui.BOLD, ui.RED))
            return sid, msgs, False
        log_path, lines = bg.tail_log(pid, n_lines=50)
        if not log_path:
            print(ui.style(f"[!] no bg process record for pid={pid}", ui.BOLD, ui.RED))
            return sid, msgs, False
        if not lines:
            print(ui.style(f"(log file empty or missing: {log_path})", ui.DIM))
            return sid, msgs, False
        print(ui.style(f"--- tail -n 50  {log_path} ---", ui.DIM))
        bar = ui.style("│ ", ui.BLUE)
        for line in lines:
            print(f"{bar}{line}", end="" if line.endswith("\n") else "\n")
        return sid, msgs, False

    print(ui.style(f"[!] unknown /bg subcommand: {sub!r}", ui.BOLD, ui.RED))
    print(ui.style("    usage: /bg [list|stop <pid>|log <pid>]", ui.DIM))
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
    "/bg":       {"arg_hint": "[list|stop <pid>|log <pid>]", "desc": "Manage background processes started with run_cmd(background=true)", "handler": _cmd_bg},
    "/resume":   {"arg_hint": "[id]", "desc": "Resume a saved session (latest if id omitted)",   "handler": _cmd_resume},
    "/sessions": {"arg_hint": "[all]", "desc": "List saved sessions for current cwd (or `all` for every project)", "handler": _cmd_sessions},
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
