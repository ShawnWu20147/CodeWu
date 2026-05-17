"""Per-call approval flow for side-effect tools (write_file, edit_file, run_cmd).

Read-only tools (read_file, list_dir) execute unconditionally. Side-effect
tools come through approve_or_skip from tools.dispatch_tool. ALLOW_ALL bypasses
the prompt but the preview still prints.

Preview rendering uses difflib.unified_diff (red `-` / green `+` / yellow
hunk-headers / dim context) so the user can see exactly what's changing:

  - write_file NEW       → first 10 lines + total line/byte count (no diff baseline)
  - write_file OVERWRITE → unified diff vs current file content
  - edit_file            → unified diff (computed in-process, after validating
                           old_string is uniquely present)
"""

from __future__ import annotations

import difflib
from typing import Any

from . import config
from . import ui
from .tools import resolve_path


# ---------------------------------------------------------------------------
# Diff rendering
# ---------------------------------------------------------------------------


def _color_diff_line(line: str) -> str:
    """Colorize one line of unified-diff output."""
    if line.startswith("---") or line.startswith("+++"):
        return ui.style(line, ui.BOLD, ui.DIM)
    if line.startswith("@@"):
        return ui.style(line, ui.YELLOW)
    if line.startswith("-"):
        return ui.style(line, ui.RED)
    if line.startswith("+"):
        return ui.style(line, ui.GREEN)
    return ui.style(line, ui.DIM)  # context


def _format_diff(old_text: str, new_text: str, label: str, context: int = 3) -> str:
    """Return a colorized unified diff string. Handles missing trailing newlines."""
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines, new_lines,
        fromfile=label, tofile=label, n=context,
    )
    rendered = []
    for ln in diff:
        if ln.endswith("\n"):
            ln = ln[:-1]
        rendered.append(_color_diff_line(ln))
    if not rendered:
        return ui.style("  (no textual diff — identical content)", ui.DIM)
    return "\n".join(rendered)


# ---------------------------------------------------------------------------
# Per-tool preview formatters
# ---------------------------------------------------------------------------


def _preview_write(path: str, content: str) -> str:
    lines = content.splitlines()
    n_lines = len(lines)
    n_bytes = len(content.encode("utf-8"))

    target = resolve_path(path)
    is_overwrite = target.exists() and target.is_file()

    header = ui.style("[Tool] write_file", ui.BOLD, ui.YELLOW)

    if is_overwrite:
        try:
            old_content = target.read_text(encoding="utf-8", errors="replace")
            old_bytes = len(old_content.encode("utf-8"))
        except OSError:
            old_content, old_bytes = "", 0
        tag = ui.style(f"(OVERWRITE  {old_bytes}B → {n_bytes}B)", ui.YELLOW)
        meta = ui.style(f"path={path}  lines={n_lines}", ui.YELLOW)
        diff_block = _format_diff(old_content, content, label=path)
        return f"{header}  {meta}  {tag}\n{diff_block}"

    # NEW — no diff baseline, fall back to head preview
    head_n = 10
    head = "\n".join(ui.style(f"| {ln}", ui.DIM) for ln in lines[:head_n])
    tail = ui.style(f"\n+ {n_lines - head_n} more lines", ui.DIM) if n_lines > head_n else ""
    tag = ui.style("(NEW)", ui.GREEN)
    meta = ui.style(f"path={path}  size={n_bytes}B  lines={n_lines}", ui.YELLOW)
    return f"{header}  {meta}  {tag}\n{head}{tail}"


def _preview_edit(path: str, old_string: str, new_string: str) -> str:
    header = ui.style("[Tool] edit_file", ui.BOLD, ui.YELLOW)
    meta = ui.style(f"path={path}", ui.YELLOW)
    target = resolve_path(path)

    if not target.exists() or not target.is_file():
        warn = ui.style("  ⚠ target file does not exist — edit will fail", ui.BOLD, ui.RED)
        return f"{header}  {meta}\n{warn}"

    try:
        content = target.read_text(encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        warn = ui.style("  ⚠ target is not valid UTF-8 — edit will fail", ui.BOLD, ui.RED)
        return f"{header}  {meta}\n{warn}"
    except OSError as e:
        warn = ui.style(f"  ⚠ could not read target: {e}", ui.BOLD, ui.RED)
        return f"{header}  {meta}\n{warn}"

    n = content.count(old_string)
    if n == 0:
        warn = ui.style("  ⚠ old_string not found in file — edit will fail", ui.BOLD, ui.RED)
        return f"{header}  {meta}\n{warn}"
    if n > 1:
        warn = ui.style(
            f"  ⚠ old_string appears {n} times — edit will fail "
            "(add more surrounding context to make it unique)",
            ui.BOLD, ui.RED,
        )
        return f"{header}  {meta}\n{warn}"

    new_content = content.replace(old_string, new_string, 1)
    diff_block = _format_diff(content, new_content, label=path)
    return f"{header}  {meta}\n{diff_block}"


# ---------------------------------------------------------------------------
# Approval loop
# ---------------------------------------------------------------------------


def _prompt_label(text: str) -> str:
    return ui.style(text, ui.BOLD, ui.YELLOW)


def approve_or_skip(name: str, args: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Returns (approved, possibly_modified_args).

    ALLOW_ALL bypasses the y/n prompt but the preview still prints — so the
    user can see what got executed even in unattended mode.
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

    if name == "edit_file":
        print(_preview_edit(
            args.get("path", ""),
            args.get("old_string", ""),
            args.get("new_string", ""),
        ))
        if config.ALLOW_ALL:
            print(_prompt_label("Approve edit?") + " " + ui.style("[auto-approved]", ui.DIM))
            return True, args
        while True:
            choice = input(_prompt_label("Approve edit? [y/n]: ")).strip().lower()
            if choice in {"y", "yes"}:
                return True, args
            if choice in {"n", "no", ""}:
                return False, args

    if name == "run_cmd":
        def _render_cmd_header(args_):
            cmd = args_.get("command", "")
            background = bool(args_.get("background", False))
            print(ui.style("[Cmd]", ui.BOLD, ui.YELLOW) + f" {cmd}")
            if background:
                print(ui.style(
                    "      (background — no timeout; runs detached, /bg stop <pid> to kill)",
                    ui.GREEN,
                ))
            else:
                timeout = args_.get("timeout_sec") or config.DEFAULT_CMD_TIMEOUT_SEC
                print(ui.style(f"      timeout: {timeout}s", ui.DIM))

        if config.ALLOW_ALL:
            _render_cmd_header(args)
            print(_prompt_label("Approve cmd?") + " " + ui.style("[auto-approved]", ui.DIM))
            return True, args
        while True:
            _render_cmd_header(args)
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

    return True, args  # read-only tools should never reach here
