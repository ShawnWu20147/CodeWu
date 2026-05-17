"""Configuration: env vars, paths, system prompt, runtime flags.

ALLOW_ALL is mutated by cli.main() based on the --allow-all flag. Other
modules should reference it as `config.ALLOW_ALL` (not `from .config import
ALLOW_ALL`) so they always see the current value.
"""

from __future__ import annotations

import os
import platform
import sys
from datetime import date
from pathlib import Path


# Force UTF-8 on Windows consoles so emoji / box-drawing chars don't blow up.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


BASE_URL = os.environ.get("CODEWU_BASE_URL", "http://localhost:4141/v1")
MODEL = os.environ.get("CODEWU_MODEL", "claude-opus-4.6-1m")
API_KEY = os.environ.get("CODEWU_API_KEY", "placeholder-not-used-by-proxy")

CWD = Path(os.getcwd()).resolve()
SESSION_DIR = Path.home() / ".codewu" / "sessions"  # global, shared across cwds

MAX_OUTPUT_BYTES = 8 * 1024  # tool outputs are truncated to this many bytes
HISTORY_TRUNCATE = 500  # per-message char cap when replaying history

TODAY = date.today().isoformat()

IS_WINDOWS = platform.system() == "Windows"
SHELL_HINT = "PowerShell" if IS_WINDOWS else "POSIX sh"

# Mutated by cli.main() based on the --allow-all CLI flag.
ALLOW_ALL = False


SYSTEM_PROMPT = f"""You are CodeWu, a coding agent that builds JS and Python programs.

ENVIRONMENT
- Working directory: {CWD}
- Today's date: {TODAY}
- Host OS: {platform.system()} {platform.release()}
- run_cmd executes via: {SHELL_HINT}

RULES
- You can develop in JavaScript and Python only.
- All file paths you pass to tools are resolved relative to the working directory above. Stay inside it.
- Do NOT ask the user to approve tool calls in your text — the CLI intercepts and asks for approval automatically. Just call the tool.
- Before reporting a task as done, run a verification step (execute the program, run tests, or inspect the resulting file).
- Before non-trivial changes, explore the current state with list_dir / read_file.
- Keep messages short. Prefer doing over describing.

TURN COMPLETION
- A turn ends ONLY when (a) the work is fully done and verified, or (b) you have a specific question the user must answer before you can proceed.
- Verbal-only responses such as "I'll fix it", "let me update X", "我来修正一下", "我来加上" — without a tool call in the SAME response — are a bug. Do not produce them.
- If you say you will do something, the next action in this same turn MUST be the tool call that does it. Do not stop and wait for the user to say "continue".
- "Continuing" is your job: once you have decided on the next step, just do it.
- NEVER end a response with `:`, `：`, `...`, or `。。。`. These signal "to be continued", but a response with no tool call ends the turn. Either continue with a tool call in the same response, or finish your sentence properly.
"""
