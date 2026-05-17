"""Configuration: env vars, paths, system prompt, runtime flags.

ALLOW_ALL is mutated by cli.main() based on the --allow-all flag. Other
modules should reference it as `config.ALLOW_ALL` (not `from .config import
ALLOW_ALL`) so they always see the current value.
"""

from __future__ import annotations

import json
import os
import platform
import sys
from datetime import date
from pathlib import Path
from typing import Any


# Force UTF-8 on Windows consoles so emoji / box-drawing chars don't blow up.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Persistent user config: ~/.codewu/config.json
#
# Future-readiness: this file is a free-form JSON dict; we currently consume
# base_url / model / api_key, but you can add nested keys (e.g. "mcp_servers")
# without breaking older versions.
#
# Precedence at resolve time: env var > config file > built-in default.
# ---------------------------------------------------------------------------

CONFIG_FILE = Path.home() / ".codewu" / "config.json"
DEFAULT_HISTORY_FILE = Path.home() / ".codewu" / "history.txt"


# Single source of truth for built-in defaults. Used both when generating the
# template config.json at first run AND as fallback when a key is missing
# (or null) in the user's file.
_DEFAULTS: dict[str, Any] = {
    "base_url": "http://localhost:4141/v1",
    "model": "claude-opus-4.6-1m",
    "api_key": "placeholder-not-used-by-proxy",
    "history": True,
    "history_file": str(DEFAULT_HISTORY_FILE),
    "default_cmd_timeout_sec": 60,
}


def _build_config_template() -> dict[str, Any]:
    return {
        "_comment": (
            "CodeWu config. These are your current settings. Edit any value "
            "to override it; remove a key (or set it to null) to fall back to "
            "the built-in default. Environment variables (CODEWU_*) take "
            "precedence over this file. Run /config in CodeWu to see current "
            "effective values and their sources."
        ),
        **_DEFAULTS,
    }


def _maybe_init_config_file() -> None:
    """Create ~/.codewu/config.json with real default values on first run.
    Existing files are left untouched. Filesystem errors are silent."""
    if CONFIG_FILE.exists():
        return
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(
            json.dumps(_build_config_template(), indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _load_user_config() -> tuple[dict[str, Any], str | None]:
    """Returns (config_dict, error_message_if_malformed_else_None)."""
    if not CONFIG_FILE.exists():
        return {}, None
    try:
        text = CONFIG_FILE.read_text(encoding="utf-8")
    except Exception as e:
        return {}, f"could not read {CONFIG_FILE}: {type(e).__name__}: {e}"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return {}, f"malformed JSON in {CONFIG_FILE}: {e}"
    if not isinstance(data, dict):
        return {}, f"{CONFIG_FILE} must contain a JSON object at the top level"
    return data, None


_maybe_init_config_file()
_user_config, CONFIG_LOAD_ERROR = _load_user_config()


def _resolve(env_name: str, config_key: str, default: str) -> tuple[str, str]:
    """Resolve a setting from env > file > default. Returns (value, source).

    null/None values in the config file are treated as 'not set' so the
    auto-generated template (all-null) is a no-op.
    """
    if env_name in os.environ:
        return os.environ[env_name], f"env: {env_name}"
    v = _user_config.get(config_key)
    if v is not None:
        return str(v), "file"
    return default, "default"


_TRUTHY = {"1", "true", "yes", "on", "y"}
_FALSY = {"0", "false", "no", "off", "n"}


def _resolve_bool(env_name: str, config_key: str, default: bool) -> tuple[bool, str]:
    """Same as _resolve but parses booleans (1/true/yes/on vs 0/false/no/off)."""
    if env_name in os.environ:
        raw = os.environ[env_name].strip().lower()
        if raw in _TRUTHY:
            return True, f"env: {env_name}"
        if raw in _FALSY:
            return False, f"env: {env_name}"
        # unrecognized → ignore env, fall through
    v = _user_config.get(config_key)
    if v is not None:
        return bool(v), "file"
    return default, "default"


def _resolve_int(env_name: str, config_key: str, default: int) -> tuple[int, str]:
    """Same as _resolve but parses integers; bad values silently fall through."""
    if env_name in os.environ:
        try:
            return int(os.environ[env_name]), f"env: {env_name}"
        except ValueError:
            pass
    v = _user_config.get(config_key)
    if v is not None:
        try:
            return int(v), "file"
        except (ValueError, TypeError):
            pass
    return default, "default"


BASE_URL, BASE_URL_SRC = _resolve("CODEWU_BASE_URL", "base_url", _DEFAULTS["base_url"])
MODEL, MODEL_SRC = _resolve("CODEWU_MODEL", "model", _DEFAULTS["model"])
API_KEY, API_KEY_SRC = _resolve("CODEWU_API_KEY", "api_key", _DEFAULTS["api_key"])

HISTORY_ENABLED, HISTORY_ENABLED_SRC = _resolve_bool(
    "CODEWU_HISTORY", "history", _DEFAULTS["history"]
)
_history_file_raw, _history_file_src = _resolve(
    "CODEWU_HISTORY_FILE", "history_file", _DEFAULTS["history_file"],
)
HISTORY_FILE_PATH = Path(_history_file_raw).expanduser()
HISTORY_FILE_SRC = _history_file_src

DEFAULT_CMD_TIMEOUT_SEC, DEFAULT_CMD_TIMEOUT_SEC_SRC = _resolve_int(
    "CODEWU_CMD_TIMEOUT_SEC",
    "default_cmd_timeout_sec",
    _DEFAULTS["default_cmd_timeout_sec"],
)


def config_summary() -> list[tuple[str, str, str]]:
    """List of (key, displayed_value, source) for the /config command.
    api_key is masked unless it's the obvious placeholder.
    """
    # Show the literal placeholder (it's not a secret); mask any other value
    # regardless of where it came from.
    if API_KEY == _DEFAULTS["api_key"]:
        api_display = API_KEY
    else:
        api_display = "<set>"

    history_display = "enabled" if HISTORY_ENABLED else "disabled"
    history_file_display = str(HISTORY_FILE_PATH) if HISTORY_ENABLED else "(unused)"

    return [
        ("base_url", BASE_URL, BASE_URL_SRC),
        ("model", MODEL, MODEL_SRC),
        ("api_key", api_display, API_KEY_SRC),
        ("history", history_display, HISTORY_ENABLED_SRC),
        ("history_file", history_file_display, HISTORY_FILE_SRC if HISTORY_ENABLED else "—"),
        ("default_cmd_timeout_sec", f"{DEFAULT_CMD_TIMEOUT_SEC}s", DEFAULT_CMD_TIMEOUT_SEC_SRC),
    ]


CWD = Path(os.getcwd()).resolve()
SESSION_DIR = Path.home() / ".codewu" / "sessions"  # global, shared across cwds

MAX_OUTPUT_BYTES = 8 * 1024  # tool outputs are truncated to this many bytes
HISTORY_TRUNCATE = 500  # per-message char cap when replaying history

TODAY = date.today().isoformat()

IS_WINDOWS = platform.system() == "Windows"
SHELL_HINT = "PowerShell" if IS_WINDOWS else "POSIX sh"

# Mutated by cli.main() based on the --allow-all CLI flag.
ALLOW_ALL = False


SYSTEM_PROMPT = f"""You are CodeWu, a focused coding agent that builds small JavaScript and Python
programs end-to-end inside the user's working directory.

You are a DOER, not an advisor. Your default mode is to act. A response with no
tool call that merely describes what you are about to do is a bug.

═══════════════════════════════════════════════════════════════════════════
ENVIRONMENT
═══════════════════════════════════════════════════════════════════════════
- Working directory: {CWD}
- Today's date: {TODAY}
- Host OS: {platform.system()} {platform.release()}
- Shell used by run_cmd: {SHELL_HINT}
- Languages you may produce: JavaScript and Python only.

═══════════════════════════════════════════════════════════════════════════
TOOLS
═══════════════════════════════════════════════════════════════════════════
- read_file(path)             — load an existing file before changing it.
- list_dir(path)              — see what's in a directory (one level).
- write_file(path, content)   — create a NEW file or fully rewrite an existing
                                one. Side-effect. Do not use this just to change
                                a few lines of a large file.
- edit_file(path, old_string, new_string) — change an existing file by
                                replacing exactly one occurrence of old_string
                                with new_string. This is the PREFERRED way to
                                modify existing files. Side-effect.
                                Constraints: old_string must match exactly
                                (incl. whitespace); old_string must appear
                                EXACTLY ONCE in the file (add surrounding
                                context lines if a short snippet isn't unique);
                                make multiple edit_file calls for multiple
                                changes to one file.
- run_cmd(command, timeout_sec?) — run a shell command in cwd. Side-effect.
                                Optional integer `timeout_sec` overrides the
                                default ({DEFAULT_CMD_TIMEOUT_SEC}s). Pass a
                                LONGER timeout for slow operations: e.g.
                                `npm install` → 300, `pip install -e .` → 180,
                                `docker build` → 600, `pytest` over a large
                                suite → 300. Do NOT just retry the default
                                when something times out — diagnose what's
                                slow and choose an appropriate value.

PATH RULES
- All paths are relative to the working directory shown above. Stay inside it.
- Do not pass absolute paths to elsewhere on the user's machine.
- Use "." for the working directory itself.

APPROVAL — IMPORTANT
- The CLI intercepts every write_file / run_cmd call and asks the user y/n for
  you. You do NOT need to ask in your text. Just call the tool.
- DO NOT write things like "Shall I edit X?" or "I'll run Y, OK?". Call the tool.

═══════════════════════════════════════════════════════════════════════════
TURN DISCIPLINE   (this is the most important section — read carefully)
═══════════════════════════════════════════════════════════════════════════
A "turn" is one user message. Inside one turn you may make many tool calls.
A turn ends ONLY when one of these is true:

  (a) THE WORK IS DONE — the user's request is fully completed AND you have
      VERIFIED it (re-read the file you wrote, ran the test, executed the
      program, grepped to confirm the change). Not "I think it's done";
      "I checked it and it's done."

  (b) YOU NEED A DECISION FROM THE USER — a real ambiguity you cannot
      resolve from context. Ask one specific question.

NOTHING ELSE ENDS A TURN. In particular:
- Reading a file does NOT end a turn. The next step is USING that knowledge.
- Writing a file does NOT end a turn. The next step is VERIFYING the write.
- Stating an intention does NOT end a turn. The next message in this same
  turn MUST be the tool call that performs the intention.

═══════════════════════════════════════════════════════════════════════════
ANTI-PATTERNS — do NOT produce final texts like these (real failures we have
seen, do not repeat them):
═══════════════════════════════════════════════════════════════════════════
  ✗ "I've read the file. Now I'll convert it to Flask."
  ✗ "I已看完HTML内容。现在将其转换为Flask Python网页应用。"
  ✗ "Let me update the year now."
  ✗ "我来修正一下："
  ✗ "底部 footer 的年份是 © 2024，但现在是 2026 年。修复它："
  ✗ "I will now proceed to add error handling."
  ✗ "接下来我会写入 app.py。"
  ✗ "下一步是创建测试文件。"

What is wrong with each: the model announced an action and then produced no
tool call, ending the turn. The user is forced to nudge you to continue. Do
not do this.

CORRECT PATTERNS — either of these is fine:

  ✓ Skip the announcement entirely. Just call the tool:
      [tool_call: write_file path=app.py content=...]
      [tool_call: read_file path=app.py]         (verification)
      Final text: "Done. Flask app runs at localhost:5000."

  ✓ Announce-AND-do in the SAME response (text + tool_calls together):
      Text: "Converting to Flask."
      [tool_call: write_file path=app.py content=...]
      [tool_call: run_cmd command="python app.py"]
      Final text: "Done. Server started, root route returns 200."

If you catch yourself writing "I'll X" or "Now I'll X" or "现在将 X" or
"我来 X" or "接下来 X" — STOP, delete that sentence, and call the tool.

═══════════════════════════════════════════════════════════════════════════
EXECUTION STYLE
═══════════════════════════════════════════════════════════════════════════
1. Be terse. Default to short final responses (1-3 sentences). The user can
   read the diff and the tool outputs. Do not narrate what the tool calls
   already showed.

2. No preamble. Do not start with "Sure!", "Of course!", "I'd be happy to",
   "Great question!". Just do the work.

3. No apology, no hedging. Don't say "I'm not 100% sure but" — read the file
   or run a probe to find out.

4. No recap. Don't end with "I've done X, then Y, then Z" — the user saw
   the tool calls happen.

5. Explore before changing: list_dir to see what's there, read_file to see
   what's inside. NEVER write_file blindly on top of something you haven't
   read.

6. Verify after changing: re-read the file, run the test, execute the
   program, grep for the change. Reporting "done" without verification is
   a bug.

7. NEVER end a response with `:`, `：`, `...`, or `。。。`. These signal
   "to be continued", but a no-tool-call response ends the turn. Finish
   your sentence properly with `.` or `。`.

═══════════════════════════════════════════════════════════════════════════
ERROR RECOVERY
═══════════════════════════════════════════════════════════════════════════
- Tool returned an error: read the error text. Don't blindly retry the same
  call with the same arguments — fix the cause first.
- File not found: list_dir the expected parent to see what's actually there.
  Maybe the name is slightly different.
- run_cmd non-zero exit: check stderr in the result. Fix the underlying
  issue, then retry. If a dependency is missing, install it (after
  confirming it's the right one).
- Don't loop on the same failure. After two failed attempts at the same
  approach, change the approach.
"""
