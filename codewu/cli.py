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
    load_session,
    new_session_id,
    save_session,
)
from .slash import handle_slash, print_exit_hint
from .tools import tool_run_cmd


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

        user_msg_idx = len(messages)
        messages.append({"role": "user", "content": expanded})
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
