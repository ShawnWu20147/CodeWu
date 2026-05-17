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

from . import config
from . import ui
from .loop import run_turn
from .session import (
    load_session,
    new_session_id,
    save_session,
)
from .slash import handle_slash, print_exit_hint
from .tools import tool_run_cmd


def handle_bang(line: str, messages: list[dict[str, Any]]) -> None:
    """Run a shell command directly and append its output to the conversation.

    The whole bang interaction is rendered with a dim-blue side-bar so it's
    visually distinct from agent / tool messages.
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
    # Prefix every output line with a dim-blue side-bar so bang output is
    # visually distinct from agent text or tool previews.
    bar = ui.style("│ ", ui.BLUE)
    for ln in output.splitlines() or [""]:
        print(f"{bar}{ln}")
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

    config.ALLOW_ALL = args.allow_all

    if args.resume is None:
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
    prompt_label = ui.style(">", ui.BOLD, ui.GREEN)
    while True:
        if turn_count > 0:
            print(f"\n{ui.separator()}")
        try:
            line = input(f"\n{prompt_label} ").strip()
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

        user_msg_idx = len(messages)
        messages.append({"role": "user", "content": line})
        try:
            run_turn(client, messages)
        except KeyboardInterrupt:
            del messages[user_msg_idx:]
            print(ui.style("\n[!] turn interrupted — rolled back to before this user message", ui.BOLD, ui.YELLOW))
            continue
        except Exception as e:
            print(ui.style(f"[!] turn failed: {type(e).__name__}: {e}", ui.BOLD, ui.RED))
            del messages[user_msg_idx:]
            continue
        save_session(session_id, messages)

    return 0
