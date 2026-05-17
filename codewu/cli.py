"""CLI entry point: argparse, banner, REPL loop.

The REPL routes input lines into one of four paths:
  /something — slash command (see slash.py)
  !something — bang shortcut: run the rest in the shell, append output to context
  empty      — re-prompt
  anything else — append as user message, run a turn (run_turn from loop.py)

A dim separator line is printed before every prompt except the first one,
to visually delimit conversation rounds.
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from typing import Any

from openai import OpenAI

from . import bg
from . import config
from . import ui
from .loop import run_turn
from .repl import (
    attached_summary,
    expand_at_files,
    fuzzy_suggest,
    prompt_input,
)
from .session import (
    format_age,
    list_sessions_for_project,
    load_session,
    new_session_id,
    save_session,
)
from .slash import handle_slash, print_exit_hint
from .tools import tool_run_cmd


def _interactive_pick() -> tuple[str | None, list[dict[str, Any]] | None]:
    """Show a numbered menu of saved sessions for the current cwd and let the
    user resume one, start a new session, or quit.

    Returns:
      (session_id, messages) — resume that one
      (None, None)           — start a fresh session
    sys.exit on quit / Ctrl+C / EOF.
    """
    rows = list_sessions_for_project()
    if not rows:
        print(ui.style(f"No saved sessions for {config.CWD}", ui.DIM))
        print(ui.style("(starting a new session)", ui.DIM))
        return None, None

    print()
    print(ui.style(f"Sessions for {config.CWD} ({len(rows)} found):", ui.BOLD))
    for i, row in enumerate(rows, start=1):
        sid = row["session_id"]
        age = format_age(sid)
        nmsg = row.get("n_messages", 0)
        first = row.get("first_user_msg", "")
        if len(first) > 100:
            first = first[:100] + "…"
        idx = ui.style(f"  [{i}]", ui.BOLD, ui.CYAN)
        meta = ui.style(f"{sid}  ({nmsg} msgs, {age})", ui.DIM)
        print()
        print(f"{idx}  {meta}")
        if first:
            print(ui.style(f"       {first!r}", ui.DIM))
    print()

    prompt = ui.style("Pick number to resume, [n]ew, or [q]uit: ", ui.BOLD, ui.YELLOW)
    while True:
        try:
            choice = input(prompt).strip().lower()
        except (KeyboardInterrupt, EOFError):
            print()
            sys.exit(0)
        if choice in ("n", "new", ""):
            return None, None
        if choice in ("q", "quit", "exit"):
            sys.exit(0)
        try:
            idx = int(choice)
            if 1 <= idx <= len(rows):
                chosen_sid = rows[idx - 1]["session_id"]
                sid, msgs = load_session(chosen_sid)
                return sid, msgs
        except ValueError:
            pass
        print(ui.style(f"  invalid choice: {choice!r}", ui.RED))


def _clean_orphan_tool_calls(messages: list[dict[str, Any]]) -> bool:
    """If the trailing assistant message announced tool_calls but not all of
    them got a matching tool result (e.g. Ctrl+C or EOFError fired mid-dispatch),
    remove that assistant + any partial tool messages so the message list is a
    valid input to the next chat.completions.create call.

    Returns True if anything was cleaned, False otherwise.
    """
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            expected = {tc.get("id", "") for tc in m["tool_calls"]}
            actual = {
                t.get("tool_call_id", "")
                for t in messages[i + 1:]
                if t.get("role") == "tool"
            }
            if expected - actual:
                del messages[i:]
                return True
            return False
        if role not in ("tool", "assistant"):
            # Hit a user or system message — earlier state is stable.
            return False
    return False


def handle_bang(line: str, messages: list[dict[str, Any]]) -> None:
    """Run a shell command directly and append its output to the conversation.

    tool_run_cmd now streams stdout/stderr live with a dim-blue side-bar, so
    we just print the command header and let the streaming do the rest. The
    captured output is appended to the message history for the LLM to see on
    the next turn.
    """
    cmd = line[1:].strip()
    if not cmd:
        print(ui.style(
            "[!] usage: !<command>   e.g. !git status   (runs in shell, no approval, output appended to context)",
            ui.DIM,
        ))
        return
    label = ui.style("[!]", ui.BOLD, ui.BLUE) + " " + ui.style(cmd, ui.BLUE)
    print(label)
    result = tool_run_cmd(cmd)
    output = result.get("result") or result.get("error") or ""
    messages.append({
        "role": "user",
        "content": f"[I ran: {cmd}]\n{output}",
    })


def banner(session_id: str, resumed: bool) -> None:
    mode = "resumed" if resumed else "new"
    title = ui.style("CodeWu", ui.BOLD, ui.CYAN)
    print(ui.style("╭───────────────────────────────────────────────╮", ui.DIM))
    print(f"│  {title} — minimal coding agent (prototype)    │")
    print(ui.style("╰───────────────────────────────────────────────╯", ui.DIM))
    print(textwrap.dedent(f"""\
      model:   {ui.style(config.MODEL, ui.CYAN)}
      base:    {config.BASE_URL}
      cwd:     {ui.style(str(config.CWD), ui.CYAN)}
      shell:   {config.SHELL_HINT}
      session: {ui.style(session_id, ui.CYAN)}  ({ui.style(mode, ui.GREEN)})
    Type your request, or {ui.style('/help', ui.CYAN)} for commands."""))
    if config.ALLOW_ALL:
        print(ui.style("  ⚠  --allow-all is set: tool approval prompts are bypassed.", ui.BOLD, ui.RED))
    bg_alive = bg.list_alive()
    if bg_alive:
        n = len(bg_alive)
        plural = "es" if n != 1 else ""
        print(ui.style(
            f"  bg:      {n} background process{plural} still running — use {ui.style('/bg', ui.CYAN)}{ui.YELLOW} to list / stop",
            ui.YELLOW,
        ))


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
    ap.add_argument(
        "--pick", "-p",
        action="store_true",
        help="Show saved sessions for the current cwd and let you pick one to resume (or start fresh).",
    )
    args = ap.parse_args()

    config.ALLOW_ALL = args.allow_all

    if args.pick:
        chosen = _interactive_pick()
        if chosen[0] is None:
            session_id = new_session_id()
            messages: list[dict[str, Any]] = [{"role": "system", "content": config.SYSTEM_PROMPT}]
            resumed = False
        else:
            session_id, messages = chosen
            resumed = True
    elif args.resume is None:
        session_id = new_session_id()
        messages: list[dict[str, Any]] = [{"role": "system", "content": config.SYSTEM_PROMPT}]
        resumed = False
    else:
        sid_arg = None if args.resume == "__latest__" else args.resume
        try:
            session_id, messages = load_session(sid_arg)
            resumed = True
        except FileNotFoundError as e:
            print(ui.style(f"[!] {e}", ui.BOLD, ui.RED), file=sys.stderr)
            return 1

    client = OpenAI(base_url=config.BASE_URL, api_key=config.API_KEY)

    banner(session_id, resumed)
    if resumed:
        from .session import print_history
        print_history(messages)

    turn_count = 0
    prompt_label_plain = "> "  # prompt_toolkit handles its own coloring of the input area
    while True:
        if turn_count > 0:
            print(f"\n{ui.separator()}")
        try:
            line = prompt_input(prompt_label_plain).strip()
        except (EOFError, KeyboardInterrupt):
            print_exit_hint(session_id, messages)
            break
        turn_count += 1
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

        # Expand @<path> tokens into <file> blocks. Abort the turn if any
        # token does not resolve to a readable file — let the user fix typos
        # rather than silently sending a literal "@foo.py" to the model.
        expanded, attached, missing = expand_at_files(line)
        if missing:
            print(ui.style(
                f"[!] file(s) not found: {', '.join('@' + m for m in missing)}",
                ui.BOLD, ui.RED,
            ))
            for tok in missing:
                hits = fuzzy_suggest(tok)
                if hits:
                    print(ui.style(
                        f"     did you mean: {', '.join('@' + h for h in hits)}?",
                        ui.DIM,
                    ))
            continue
        if attached:
            print(ui.style(f"[~] attached: {attached_summary(attached)}", ui.DIM))

        messages.append({"role": "user", "content": expanded})
        try:
            run_turn(client, messages)
        except KeyboardInterrupt:
            # Ctrl+C may have fired mid-dispatch (between assistant tool_calls
            # and their tool_results). Clean up so the saved state stays valid.
            # The user's message is preserved so /resume shows what they tried.
            cleaned = _clean_orphan_tool_calls(messages)
            extra = " (cleaned up an incomplete tool call)" if cleaned else ""
            print(ui.style(f"\n[!] turn interrupted{extra}", ui.BOLD, ui.YELLOW))
            save_session(session_id, messages)
            continue
        except Exception as e:
            # call_llm_stream raises BEFORE appending its assistant_msg, so the
            # message list is normally clean here — but EOFError from an
            # approval input() etc. can also leak through, so we run the same
            # cleanup defensively before saving.
            cleaned = _clean_orphan_tool_calls(messages)
            extra = " (cleaned up an incomplete tool call)" if cleaned else ""
            print(ui.style(f"[!] turn failed: {type(e).__name__}: {e}{extra}", ui.BOLD, ui.RED))
            save_session(session_id, messages)
            continue
        save_session(session_id, messages)

    return 0
