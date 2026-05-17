"""Background process state + lifecycle.

`tool_run_cmd(..., background=True)` spawns a detached process, redirects its
stdout/stderr to a log file under ~/.codewu/bg/, registers it here, and returns
immediately. Background processes outlive the CodeWu session that started them.

The state file (~/.codewu/bg/processes.json) is a flat JSON list of
  {pid, command, cwd, log_file, started_at}
entries. Each call to `list_alive` checks liveness and prunes dead PIDs so the
file doesn't grow unbounded.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import IS_WINDOWS


BG_DIR = Path.home() / ".codewu" / "bg"
STATE_FILE = BG_DIR / "processes.json"


def _ensure_dir() -> None:
    BG_DIR.mkdir(parents=True, exist_ok=True)


def _load_raw() -> list[dict[str, Any]]:
    if not STATE_FILE.exists():
        return []
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_raw(processes: list[dict[str, Any]]) -> None:
    _ensure_dir()
    STATE_FILE.write_text(
        json.dumps(processes, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def is_alive(pid: int) -> bool:
    """Cheap liveness check by PID."""
    if not pid:
        return False
    if IS_WINDOWS:
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=5,
            )
            return str(pid) in result.stdout
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except Exception:
        return False


def new_log_path(slug: str = "bg") -> Path:
    """Build a fresh, sortable log path under BG_DIR."""
    _ensure_dir()
    safe = "".join(c if c.isalnum() else "-" for c in slug)[:24].strip("-") or "bg"
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return BG_DIR / f"{ts}-{safe}-{secrets.token_hex(3)}.log"


def register(pid: int, command: str, cwd: str, log_file: str) -> None:
    """Add a new bg process and prune any dead entries already on file."""
    procs = [p for p in _load_raw() if is_alive(p.get("pid", 0))]
    procs.append({
        "pid": pid,
        "command": command,
        "cwd": cwd,
        "log_file": log_file,
        "started_at": datetime.now().isoformat(timespec="seconds"),
    })
    _save_raw(procs)


def list_alive() -> list[dict[str, Any]]:
    """Return only live entries; rewrite the state file if any died since last call."""
    raw = _load_raw()
    alive = [p for p in raw if is_alive(p.get("pid", 0))]
    if len(alive) != len(raw):
        _save_raw(alive)
    return alive


def stop(pid: int) -> bool:
    """Kill the bg process (whole tree on Windows via taskkill /T).

    Returns True if we actually killed a live process; False if it was already
    dead (in which case we just clean state).
    """
    was_alive = is_alive(pid)
    if was_alive:
        if IS_WINDOWS:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=5,
                )
            except Exception:
                pass
        else:
            try:
                os.kill(pid, 15)  # SIGTERM
                time.sleep(0.5)
                if is_alive(pid):
                    os.kill(pid, 9)  # SIGKILL
            except Exception:
                pass
    # Always clean state, whether or not we killed something
    procs = [p for p in _load_raw() if p.get("pid") != pid]
    _save_raw(procs)
    return was_alive


def tail_log(pid: int, n_lines: int = 50) -> tuple[str, list[str]]:
    """Return (log_file_path, last_n_lines_with_trailing_newlines).
    Returns ("", []) if no record for this pid or log file missing.
    """
    proc = next((p for p in _load_raw() if p.get("pid") == pid), None)
    if not proc:
        return "", []
    log_path = proc.get("log_file", "")
    if not log_path or not Path(log_path).exists():
        return log_path, []
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return log_path, lines[-n_lines:]
    except Exception:
        return log_path, []
