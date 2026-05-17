"""Tool implementations, JSON schemas for the LLM, and the dispatcher.

Side-effect tools (write_file, run_cmd) are routed through approval.approve_or_skip
before execution. Read-only tools (read_file, list_dir) run unconditionally.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

from . import bg
from . import config
from . import ui
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


def tool_edit_file(path: str, old_string: str, new_string: str) -> dict[str, Any]:
    """Replace exactly one occurrence of old_string with new_string in `path`.

    Constraints (enforced for safety + determinism):
      - File must exist and be UTF-8 text.
      - old_string must be non-empty.
      - old_string must occur in the file *exactly once*. If it doesn't,
        we return a clear error so the model can add more surrounding context.
      - old_string == new_string is rejected as a no-op.
    """
    try:
        p = resolve_path(path)
        if not p.exists():
            return _err(f"file not found: {p}")
        if not p.is_file():
            return _err(f"not a file: {p}")
        if not old_string:
            return _err("old_string must not be empty")
        if old_string == new_string:
            return _err("old_string and new_string are identical (no-op)")
        try:
            content = p.read_text(encoding="utf-8", errors="strict")
        except UnicodeDecodeError:
            return _err(f"file is not valid UTF-8 text: {p}")
        n = content.count(old_string)
        if n == 0:
            return _err(f"old_string not found in {p}")
        if n > 1:
            return _err(
                f"old_string appears {n} times in {p}; "
                "add more surrounding context to make it unique"
            )
        new_content = content.replace(old_string, new_string, 1)
        p.write_text(new_content, encoding="utf-8")
        return _ok(
            f"edited {p}: replaced 1 occurrence "
            f"({len(old_string)} → {len(new_string)} chars; "
            f"file {len(content)}B → {len(new_content)}B)"
        )
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


def _kill_process_tree(proc: "subprocess.Popen") -> None:
    """Kill the process and (on Windows) all of its descendants.

    Popen.kill() on Windows only terminates the immediate child (e.g. the
    PowerShell we spawned), leaving grandchildren (npm.cmd → node.exe → workers)
    as orphans that keep running. `taskkill /F /T /PID <pid>` walks the whole
    tree and force-kills it, no admin rights needed for processes we own.
    On POSIX, plain Popen.kill() handles our use cases.
    """
    if IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=5,
            )
            return
        except Exception:
            pass  # fall through to plain kill below
    try:
        proc.kill()
    except Exception:
        pass


def _run_bg(command: str) -> dict[str, Any]:
    """Spawn `command` detached. Returns immediately with pid + log path.
    Output goes to ~/.codewu/bg/<id>.log; the process survives codewu exit.
    """
    if IS_WINDOWS:
        argv = ["powershell", "-NoProfile", "-Command", command]
    else:
        argv = ["sh", "-c", command]

    # Slug the log filename from the first token of the command so the
    # file name carries a hint about what's inside.
    first_token = command.strip().split(None, 1)[0] if command.strip() else "bg"
    log_path = bg.new_log_path(first_token)

    try:
        log_fh = open(log_path, "w", encoding="utf-8", errors="replace")
    except Exception as e:
        return _err(f"failed to open bg log file {log_path}: {type(e).__name__}: {e}")

    try:
        if IS_WINDOWS:
            # CREATE_NO_WINDOW (0x08000000): give the child its own *hidden*
            # console so console-mode binaries (powershell.exe is one!) can
            # still initialize properly. DETACHED_PROCESS (no console at all)
            # makes PowerShell exit immediately during startup, even with all
            # stdio redirected — empirically confirmed on Windows 10 / PS 5.1.
            # CREATE_NEW_PROCESS_GROUP (0x00000200): independent signal group
            # so the child outlives the parent.
            creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            proc = subprocess.Popen(
                argv,
                cwd=str(CWD),
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
                close_fds=True,
            )
        else:
            proc = subprocess.Popen(
                argv,
                cwd=str(CWD),
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
    except Exception as e:
        try:
            log_fh.close()
        except Exception:
            pass
        return _err(f"failed to spawn bg process: {type(e).__name__}: {e}")
    finally:
        # The child has its own duplicated handle; we can safely close ours.
        try:
            log_fh.close()
        except Exception:
            pass

    bg.register(pid=proc.pid, command=command, cwd=str(CWD), log_file=str(log_path))

    print(ui.style(f"[~] started in background  pid={proc.pid}", ui.BOLD, ui.GREEN))
    print(ui.style(f"    log: {log_path}", ui.DIM))
    print(ui.style(f"    stop: /bg stop {proc.pid}", ui.DIM))

    return _ok(
        f"started background process pid={proc.pid}\n"
        f"command: {command}\n"
        f"log file: {log_path}\n"
        f"To stop the process: /bg stop {proc.pid}\n"
        f"To tail its log:     /bg log {proc.pid}\n"
        f"The process survives across CodeWu sessions until stopped."
    )


def tool_run_cmd(command: str, timeout_sec: int | None = None, background: bool = False) -> dict[str, Any]:
    """Run a shell command in cwd, streaming stdout/stderr live to the terminal.

    Implementation notes:
      - We use Popen + two reader threads so the user sees output as it happens
        (instead of capture_output blocking until the process exits). Output
        lines are mirrored to the terminal with a dim-blue "│ " sidebar.
      - Effective timeout = explicit `timeout_sec` arg if provided, else
        config.DEFAULT_CMD_TIMEOUT_SEC.
      - On timeout we kill the process and return whatever stdout/stderr we
        managed to collect so the LLM has context to choose a longer retry.
    """
    if background:
        return _run_bg(command)

    if timeout_sec is None or timeout_sec <= 0:
        timeout_sec = config.DEFAULT_CMD_TIMEOUT_SEC

    try:
        if IS_WINDOWS:
            argv = ["powershell", "-NoProfile", "-Command", command]
        else:
            argv = ["sh", "-c", command]

        print(ui.style(f"[~] running (timeout {timeout_sec}s)...", ui.DIM))

        proc = subprocess.Popen(
            argv,
            cwd=str(CWD),
            # Detach stdin from the user's terminal: when run interactively,
            # the child (PowerShell → npm → node …) inherits the user's TTY
            # and may sit waiting on phantom input even with non-interactive
            # flags like `npm init -y`. Giving the child a closed stdin makes
            # it behave the same way it does when run from a non-interactive
            # shell. We don't currently surface child stdin to the user
            # anyway, so this matches the tool's actual semantics.
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,  # line-buffered
        )

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        bar = ui.style("│ ", ui.BLUE)
        stderr_marker = ui.style("[err] ", ui.RED, ui.DIM)

        def _reader(stream, buf: list[str], stderr: bool) -> None:
            try:
                for line in stream:
                    buf.append(line)
                    prefix = stderr_marker if stderr else ""
                    # `end=""` because line keeps its trailing \n
                    print(f"{bar}{prefix}{line}", end="", flush=True)
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        t_out = threading.Thread(target=_reader, args=(proc.stdout, stdout_lines, False), daemon=True)
        t_err = threading.Thread(target=_reader, args=(proc.stderr, stderr_lines, True), daemon=True)
        t_out.start()
        t_err.start()

        timed_out = False
        try:
            returncode = proc.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc)
            try:
                returncode = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                returncode = -9
            timed_out = True

        # Make sure the reader threads finish flushing their pipes.
        t_out.join(timeout=2)
        t_err.join(timeout=2)

        stdout_text = "".join(stdout_lines)
        stderr_text = "".join(stderr_lines)

        if timed_out:
            print(ui.style(f"[!] timed out after {timeout_sec}s — process killed", ui.BOLD, ui.RED))
            out = (
                f"TIMED OUT after {timeout_sec}s (process killed; partial output below)\n"
                f"--- stdout (partial) ---\n{stdout_text}"
                f"--- stderr (partial) ---\n{stderr_text}"
            )
            return _err(out)

        out = (
            f"exit_code: {returncode}\n"
            f"--- stdout ---\n{stdout_text}"
            f"--- stderr ---\n{stderr_text}"
        )
        return _ok(out)
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
            "description": (
                "Create a new file or fully replace an existing file's contents. "
                "Prefer edit_file for changing existing files — write_file should "
                "only be used for new files or full rewrites. Requires user approval."
            ),
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
            "name": "edit_file",
            "description": (
                "Edit an existing file by replacing EXACTLY ONE occurrence of "
                "old_string with new_string. This is the preferred way to change "
                "an existing file; do not use write_file to rewrite a whole file "
                "just to change a few lines.\n\n"
                "Constraints:\n"
                "  - old_string must match the file exactly, including whitespace and indentation.\n"
                "  - old_string must appear in the file exactly ONCE. If a short "
                "snippet isn't unique, include surrounding context lines until it is.\n"
                "  - For multiple changes to the same file, make multiple edit_file calls."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path, relative to the working directory."},
                    "old_string": {"type": "string", "description": "Exact text to find (must appear once)."},
                    "new_string": {"type": "string", "description": "Replacement text."},
                },
                "required": ["path", "old_string", "new_string"],
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
                "Streams stdout / stderr live to the user and returns exit code "
                f"+ captured output. Default timeout is {config.DEFAULT_CMD_TIMEOUT_SEC}s; "
                "pass a longer timeout_sec for operations that legitimately take longer "
                "(npm install ~300, pip install ~180, docker build ~600, large test "
                "suites ~300). For commands that don't terminate on their own "
                "(dev servers like `npm start` / `vite dev`, file watchers, web servers, "
                "long-running daemons), pass background=true instead — DO NOT crank up "
                "timeout_sec. Requires user approval."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": f"The {SHELL_HINT} command to execute.",
                    },
                    "timeout_sec": {
                        "type": "integer",
                        "description": (
                            "Maximum seconds to allow before the process is killed. "
                            "Omit (or 0) to use the configured default. Set explicitly "
                            "for slow operations like installs, builds, or large test runs. "
                            "Ignored when background=true."
                        ),
                        "minimum": 1,
                    },
                    "background": {
                        "type": "boolean",
                        "description": (
                            "If true, spawn the command detached and return immediately. "
                            "Use for `npm start`, `vite dev`, `python -m http.server`, "
                            "watchers, daemons — anything that legitimately runs forever. "
                            "stdout/stderr are written to a log file the user can tail; "
                            "the process keeps running across CodeWu sessions until the "
                            "user runs `/bg stop <pid>`. When background is true, "
                            "timeout_sec is ignored. DO NOT use `Start-Process` / nohup / & "
                            "hacks — use this flag so the process is tracked."
                        ),
                    },
                },
                "required": ["command"],
            },
        },
    },
]


TOOLS_READONLY = frozenset({"read_file", "list_dir"})
TOOLS_SIDE_EFFECT = frozenset({"write_file", "edit_file", "run_cmd"})


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
    if name == "edit_file":
        return tool_edit_file(**args)
    if name == "list_dir":
        return tool_list_dir(**args)
    if name == "run_cmd":
        return tool_run_cmd(**args)
    return _err(f"unknown tool: {name}")
