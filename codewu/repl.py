"""Interactive line input via prompt_toolkit.

Replaces the plain `input()` used by the REPL with a prompt_toolkit session
that gives us, live as the user types:
  - syntactic coloring (`/cmd` cyan, `!cmd` blue, `@path` magenta)
  - completion menus (slash command names; cwd files with startswith filter,
    including one level of subdirectory navigation via `@dir/`)
  - command history persisted to ~/.codewu/history.txt
  - falls back to plain input() when stdin is not a TTY (so piped tests work)

The library is loaded lazily so that import-time failures (e.g. on a stripped
environment) do not break the rest of the package; in that case we degrade to
plain input().

`expand_at_files` parses `@<path>` tokens at submit time and injects the file
content as <file path="..."> blocks into the user's message.
"""

from __future__ import annotations

import difflib
import os
import re
import sys
from pathlib import Path
from typing import Iterable

from . import ui


# ---------------------------------------------------------------------------
# Lazy prompt_toolkit import. If unavailable, we silently fall back to input().
# ---------------------------------------------------------------------------

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.history import FileHistory, InMemoryHistory
    from prompt_toolkit.lexers import Lexer
    from prompt_toolkit.styles import Style
    _HAVE_PROMPT_TOOLKIT = True
except Exception:
    _HAVE_PROMPT_TOOLKIT = False
    PromptSession = None  # type: ignore[assignment]
    Completer = object  # type: ignore[assignment]
    Lexer = object  # type: ignore[assignment]

from . import config


# A token is `@` immediately followed by a path-like sequence. We only treat it
# as a file ref when it sits at start-of-line or after whitespace, so things
# like email addresses (foo@bar.com) are left alone.
AT_FILE_PATTERN = re.compile(r"(?:^|(?<=\s))@([\w./\\-]+)")


# ---------------------------------------------------------------------------
# Lexer — colorize the input line per character as the user types.
# ---------------------------------------------------------------------------


def _tokenize_with_at(line: str) -> list[tuple[str, str]]:
    """Split a normal line into ('', text) and ('class:at-file', '@path') segments."""
    tokens: list[tuple[str, str]] = []
    pos = 0
    for m in AT_FILE_PATTERN.finditer(line):
        start, end = m.span()
        if start > pos:
            tokens.append(("", line[pos:start]))
        tokens.append(("class:at-file", line[start:end]))
        pos = end
    if pos < len(line):
        tokens.append(("", line[pos:]))
    return tokens or [("", line)]


if _HAVE_PROMPT_TOOLKIT:

    class CodewuLexer(Lexer):  # type: ignore[misc]
        def lex_document(self, document):
            def get_line_tokens(lineno):
                line = document.lines[lineno]
                if not line:
                    return []
                if line.startswith("!"):
                    return [
                        ("class:bang.sigil", "!"),
                        ("class:bang.cmd", line[1:]),
                    ]
                if line.startswith("/"):
                    parts = line.split(maxsplit=1)
                    out = [("class:slash", parts[0])]
                    if len(parts) > 1:
                        out.append(("class:slash-arg", " " + parts[1]))
                    return out
                return _tokenize_with_at(line)
            return get_line_tokens

    # ---------------------------------------------------------------------
    # Completer — slash commands and @-file paths.
    # ---------------------------------------------------------------------

    def _at_word_before_cursor(text_before_cursor: str) -> str:
        """Return the @<partial> token immediately under the cursor, or empty."""
        for i in range(len(text_before_cursor) - 1, -1, -1):
            c = text_before_cursor[i]
            if c == "@":
                if i == 0 or text_before_cursor[i - 1] in " \t\n([{<,\"'":
                    return text_before_cursor[i:]
                return ""
            if c in " \t\n":
                return ""
        return ""

    class CodewuCompleter(Completer):  # type: ignore[misc]
        def get_completions(self, document, complete_event):
            text_before = document.text_before_cursor

            # /<partial> at line start with no space yet → slash command names
            if text_before.startswith("/") and " " not in text_before:
                from .slash import SLASH_COMMANDS  # late import — avoids cycles
                for cmd_name, meta in SLASH_COMMANDS.items():
                    if cmd_name.startswith(text_before):
                        yield Completion(
                            cmd_name,
                            start_position=-len(text_before),
                            display=cmd_name,
                            display_meta=meta["desc"],
                        )
                return

            # @<partial> token under cursor → file/dir completion
            word = _at_word_before_cursor(text_before)
            if word and word.startswith("@"):
                yield from self._at_completions(word)

        def _at_completions(self, word: str) -> Iterable:
            prefix = word[1:]  # strip @
            # Split into directory part + filename prefix to allow @dir/sub
            parts = re.split(r"[/\\]", prefix)
            base = Path.cwd()
            for p in parts[:-1]:
                base = base / p
            file_prefix = parts[-1]

            try:
                base = base.resolve()
                entries = sorted(
                    base.iterdir(),
                    key=lambda p: (not p.is_dir(), p.name.lower()),
                )
            except OSError:
                return

            sub_dir = "/".join(parts[:-1])
            sub_dir_with_sep = (sub_dir + "/") if sub_dir else ""

            for entry in entries:
                name = entry.name
                if not name.startswith(file_prefix):
                    continue
                is_dir = entry.is_dir()
                completion_path = sub_dir_with_sep + name + ("/" if is_dir else "")
                if is_dir:
                    meta = "dir"
                else:
                    try:
                        meta = f"{entry.stat().st_size} B"
                    except OSError:
                        meta = "file"
                yield Completion(
                    "@" + completion_path,
                    start_position=-len(word),
                    display=name + ("/" if is_dir else ""),
                    display_meta=meta,
                )

    # ---------------------------------------------------------------------
    # Style — ANSI palette for tokens and the completion menu.
    # ---------------------------------------------------------------------

    _STYLE = Style.from_dict({
        "bang.sigil": "fg:ansiblue bold",
        "bang.cmd": "fg:ansiblue",
        "slash": "fg:ansicyan bold",
        "slash-arg": "fg:ansicyan",
        "at-file": "fg:ansimagenta",
        "completion-menu.completion": "bg:#3a3a3a fg:#cccccc",
        "completion-menu.completion.current": "bg:#5f5f5f fg:#ffffff bold",
        "completion-menu.meta.completion": "bg:#3a3a3a fg:#888888",
        "completion-menu.meta.completion.current": "bg:#5f5f5f fg:#cccccc",
    })


# ---------------------------------------------------------------------------
# Session singleton + prompt entry point.
# ---------------------------------------------------------------------------

_session = None


def _build_history():
    """Pick a prompt_toolkit History backend based on user config."""
    if not config.HISTORY_ENABLED:
        return InMemoryHistory()
    path = config.HISTORY_FILE_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return FileHistory(str(path))
    except Exception:
        # If the configured path is unwritable, fall back to in-memory so the
        # session still works rather than crashing on prompt construction.
        return InMemoryHistory()


def _get_session():
    global _session
    if _session is not None:
        return _session
    if not _HAVE_PROMPT_TOOLKIT:
        return None
    _session = PromptSession(
        lexer=CodewuLexer(),
        completer=CodewuCompleter(),
        complete_while_typing=True,
        history=_build_history(),
        style=_STYLE,
    )
    return _session


def prompt_input(message: str) -> str:
    """Read a line from the user. Uses prompt_toolkit when stdin is a TTY,
    otherwise falls back to plain input() for piped/redirected stdin so the
    automated tests still work.
    """
    if not sys.stdin.isatty() or not _HAVE_PROMPT_TOOLKIT:
        sys.stdout.write(message)
        sys.stdout.flush()
        return input()
    session = _get_session()
    if session is None:
        sys.stdout.write(message)
        sys.stdout.flush()
        return input()
    return session.prompt(message)


# ---------------------------------------------------------------------------
# @file expansion (used by cli at submit time).
# ---------------------------------------------------------------------------


def expand_at_files(line: str) -> tuple[str, list[tuple[str, int]], list[str]]:
    """Find @<path> tokens in `line` and inline their content.

    Returns (expanded_text, attached, missing) where attached is a list of
    (path, content_bytes) and missing is a list of tokens that did not match
    a readable file.
    """
    attached: list[tuple[str, int]] = []
    missing: list[str] = []

    def replacer(m):
        rel = m.group(1)
        if os.path.isabs(rel):
            p = Path(rel)
        else:
            p = (Path.cwd() / rel)
        try:
            p = p.resolve()
        except OSError:
            missing.append(rel)
            return m.group(0)
        if not p.exists() or not p.is_file():
            missing.append(rel)
            return m.group(0)
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            missing.append(rel)
            return m.group(0)
        attached.append((rel, len(content.encode("utf-8"))))
        return f"\n\n<file path={rel!r}>\n{content}\n</file>\n\n"

    expanded = AT_FILE_PATTERN.sub(replacer, line)
    return expanded, attached, missing


def fuzzy_suggest(missing_token: str, max_n: int = 3) -> list[str]:
    """For a missing @<path> token, suggest existing cwd files by fuzzy match."""
    try:
        candidates = [p.name for p in Path.cwd().iterdir() if p.is_file()]
    except OSError:
        return []
    return difflib.get_close_matches(missing_token, candidates, n=max_n, cutoff=0.4)


def attached_summary(attached: list[tuple[str, int]]) -> str:
    return ", ".join(f"{p} ({size} B)" for p, size in attached)
