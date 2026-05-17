"""ANSI color constants + small UI helpers.

Colors are auto-disabled when:
- stdout is not a TTY (piped/redirected output)
- environment variable NO_COLOR or CODEWU_NO_COLOR is set

On Windows we explicitly enable virtual terminal processing so escape codes
render in cmd.exe and older consoles. On modern Windows Terminal this is a
no-op.
"""

from __future__ import annotations

import os
import platform
import sys


def _enable_windows_ansi() -> None:
    if platform.system() != "Windows":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # GetStdHandle(-11) = STD_OUTPUT_HANDLE
        # ENABLE_PROCESSED_OUTPUT (1) | ENABLE_WRAP_AT_EOL_OUTPUT (2) | ENABLE_VIRTUAL_TERMINAL_PROCESSING (4)
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass


_enable_windows_ansi()


_NO_COLOR = bool(
    os.environ.get("NO_COLOR")
    or os.environ.get("CODEWU_NO_COLOR")
    or not sys.stdout.isatty()
)


if _NO_COLOR:
    RESET = BOLD = DIM = RED = GREEN = YELLOW = BLUE = MAGENTA = CYAN = GRAY = ""
else:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    GRAY = "\033[90m"


SEPARATOR_WIDTH = 60


def separator() -> str:
    """A dim horizontal line used to delimit conversation turns."""
    return f"{DIM}{'─' * SEPARATOR_WIDTH}{RESET}"


def style(text: str, *codes: str) -> str:
    """Wrap text in the given ANSI codes; safe to call when colors are disabled."""
    if not codes or _NO_COLOR:
        return text
    return "".join(codes) + text + RESET
