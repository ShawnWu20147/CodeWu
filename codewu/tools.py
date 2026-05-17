"""Tool implementations, JSON schemas for the LLM, and the dispatcher.

Side-effect tools (write_file, run_cmd) are routed through approval.approve_or_skip
before execution. Read-only tools (read_file, list_dir) run unconditionally.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .config import CWD, IS_WINDOWS, MAX_OUTPUT_BYTES, SHELL_HINT


def _ok(result: str) -> dict[str, Any]:
    if len(result) > MAX_OUTPUT_BYTES:
        result = result[:MAX_OUTPUT_BYTES] + f"\n... [truncated, {len(result)} bytes total]"
    return {"ok": True, "result": result, "error": None}


def _err(error: str) -> dict[str, Any]:
    return {"ok": False, "result": "", "error": error}


def resolve_path(path: str) -> Path:
    """Resolve user-supplied path against CWD; absolute paths pass through."""
    if os.path.isabs(path):
        return Path(path).resolve()
    return (CWD / path).resolve()


def tool_read_file(path: str) -> dict[str, Any]:
    try:
        p = resolve_path(path)
        if not p.exists():
            return _err(f"file not found: {p}")
        if not p.is_file():
            return _err(f"not a file: {p}")
        return _ok(p.read_text(encoding="utf-8", errors="replace"))
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


def tool_write_file(path: str, content: str) -> dict[str, Any]:
    try:
        p = resolve_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return _ok(f"wrote {len(content)} chars to {p}")
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


def tool_list_dir(path: str) -> dict[str, Any]:
    try:
        p = resolve_path(path)
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


TOOLS_READONLY = frozenset({"read_file", "list_dir"})
TOOLS_SIDE_EFFECT = frozenset({"write_file", "run_cmd"})


def dispatch_tool(name: str, raw_args: str) -> dict[str, Any]:
    """Parse args, run approval flow for side-effect tools, then execute."""
    try:
        args = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError as e:
        return _err(f"invalid JSON arguments: {e}")

    if name in TOOLS_SIDE_EFFECT:
        # Late import: approval imports tools, which would be a circular import at module load.
        from .approval import approve_or_skip

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
