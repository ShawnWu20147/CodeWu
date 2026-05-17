"""Per-call approval flow for side-effect tools (write_file, run_cmd).

Read-only tools (read_file, list_dir) never come through here — they execute
unconditionally. write_file/run_cmd are routed through approve_or_skip from
tools.dispatch_tool. ALLOW_ALL bypasses the prompt but the preview still prints.
"""

from __future__ import annotations

from typing import Any

from . import config
from . import ui
from .tools import resolve_path


def _preview_write(path: str, content: str) -> str:
    lines = content.splitlines()
    n_lines = len(lines)
    n_bytes = len(content.encode("utf-8"))

    target = resolve_path(path)
    if target.exists() and target.is_file():
        try:
            old_bytes = target.stat().st_size
        except OSError:
            old_bytes = 0
        tag = f"OVERWRITE  {old_bytes}B → {n_bytes}B"
        tag_styled = ui.style(f"({tag})", ui.YELLOW)
        head_n = 5
    else:
        tag = "NEW"
        tag_styled = ui.style("(NEW)", ui.GREEN)
        head_n = 10

    head = "\n".join(ui.style(f"| {ln}", ui.DIM) for ln in lines[:head_n])
    tail = ui.style(f"\n+ {n_lines - head_n} more lines", ui.DIM) if n_lines > head_n else ""
    header = ui.style(f"[Tool] write_file", ui.BOLD, ui.YELLOW)
    meta = ui.style(f"path={path}  size={n_bytes}B  lines={n_lines}", ui.YELLOW)
    return f"{header}  {meta}  {tag_styled}\n{head}{tail}"


def _prompt_label(text: str) -> str:
    return ui.style(text, ui.BOLD, ui.YELLOW)


def approve_or_skip(name: str, args: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Returns (approved, possibly_modified_args).

    ALLOW_ALL bypasses the prompt but keeps the preview so the user can still
    see what was executed.
    """
    if name == "write_file":
        print(_preview_write(args.get("path", ""), args.get("content", "")))
        if config.ALLOW_ALL:
            print(_prompt_label("Approve write?") + " " + ui.style("[auto-approved]", ui.DIM))
            return True, args
        while True:
            choice = input(_prompt_label("Approve write? [y/n]: ")).strip().lower()
            if choice in {"y", "yes"}:
                return True, args
            if choice in {"n", "no", ""}:
                return False, args

    if name == "run_cmd":
        if config.ALLOW_ALL:
            print(ui.style(f"[Cmd]", ui.BOLD, ui.YELLOW) + f" {args.get('command', '')}")
            print(_prompt_label("Approve cmd?") + " " + ui.style("[auto-approved]", ui.DIM))
            return True, args
        while True:
            cmd = args.get("command", "")
            print(ui.style(f"[Cmd]", ui.BOLD, ui.YELLOW) + f" {cmd}")
            choice = input(_prompt_label("Approve cmd? [y/n/edit]: ")).strip().lower()
            if choice in {"y", "yes"}:
                return True, args
            if choice in {"n", "no", ""}:
                return False, args
            if choice in {"e", "edit"}:
                new_cmd = input(_prompt_label("Edit cmd: ")).strip()
                if new_cmd:
                    args = dict(args, command=new_cmd)
                continue

    return True, args  # readonly tools should never reach here, but be safe
