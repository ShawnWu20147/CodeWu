"""Session persistence + history rendering.

Sessions are organized **per project (per cwd)** under
  ~/.codewu/sessions/<cwd-slug>/<session-id>.json
plus a per-project `latest.json` pointer for `codewu --resume`.

Pre-v1.20 sessions used a flat layout (all JSONs directly in
~/.codewu/sessions/). On import we run a one-shot migration that reads each
top-level session's `cwd` field, computes the slug, and moves the file into
the right subfolder. Unparseable files are left in place — we never lose data.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from . import ui
from .config import CWD, HISTORY_TRUNCATE, MODEL, SESSION_DIR


# ---------------------------------------------------------------------------
# cwd → filesystem slug
# ---------------------------------------------------------------------------


def cwd_to_slug(cwd: str | Path) -> str:
    """Encode a cwd path into a filesystem-safe slug, à la Claude Code.

        D:\\git-nonwork\\yongshen → D--git-nonwork--yongshen
        /home/user/project       → home--user--project
        C:/Users/Foo/proj        → C--Users--Foo--proj
    """
    s = str(cwd).strip()
    # Drop drive-letter colons.
    s = s.replace(":", "")
    # Normalize separators.
    s = s.replace("\\", "/")
    # Collapse path components, drop empties.
    parts = [p for p in s.split("/") if p]
    slug = "--".join(parts) if parts else "_root_"
    # Sanitize any remaining illegal filename chars on Windows.
    slug = re.sub(r'[<>:"|?*]', "_", slug)
    return slug


def project_dir(cwd: str | Path | None = None) -> Path:
    """The per-project session subfolder for the given (or current) cwd."""
    target = cwd if cwd is not None else CWD
    return SESSION_DIR / cwd_to_slug(target)


# ---------------------------------------------------------------------------
# Legacy migration (one-shot at import time)
# ---------------------------------------------------------------------------


def _maybe_migrate_legacy_sessions() -> None:
    """Move any pre-v1.20 top-level session JSONs into per-project subfolders.

    Idempotent: re-running is a no-op once everything has been moved. Reads
    each session's `cwd` field to determine the destination slug; sessions
    that don't have one go to `_unknown_/`. Unparseable files are left in
    place so we never silently lose data.
    """
    if not SESSION_DIR.exists():
        return
    for p in list(SESSION_DIR.iterdir()):
        if not p.is_file() or p.suffix != ".json":
            continue
        # Old global latest.json: each project will mint its own.
        if p.name == "latest.json":
            try:
                p.unlink()
            except Exception:
                pass
            continue
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
            cwd = payload.get("cwd", "") if isinstance(payload, dict) else ""
            slug = cwd_to_slug(cwd) if cwd else "_unknown_"
            target_dir = SESSION_DIR / slug
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / p.name
            if not target.exists():
                p.rename(target)
        except Exception:
            # Bad/unreadable file — leave it; user can investigate manually.
            continue


_maybe_migrate_legacy_sessions()


# ---------------------------------------------------------------------------
# Session id + persistence
# ---------------------------------------------------------------------------


def new_session_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def save_session(session_id: str, messages: list[dict[str, Any]]) -> None:
    pdir = project_dir()
    pdir.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": session_id,
        "model": MODEL,
        "cwd": str(CWD),
        "messages": messages,
    }
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    path = pdir / f"{session_id}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(encoded, encoding="utf-8")
    tmp.replace(path)
    latest = pdir / "latest.json"
    latest.write_text(encoded, encoding="utf-8")


def load_session(session_id: str | None) -> tuple[str, list[dict[str, Any]]]:
    """Load a session by id (None = current project's latest).

    Search order for an explicit id:
      1. Current project's subfolder.
      2. Any other project's subfolder (so `codewu --resume <id>` still works
         from a different cwd than where the session was created).
    """
    if session_id is None:
        path = project_dir() / "latest.json"
        if not path.exists():
            raise FileNotFoundError(
                f"no saved session for this project ({CWD}); "
                f"use --pick to choose one or pass an explicit id"
            )
    else:
        path = project_dir() / f"{session_id}.json"
        if not path.exists():
            # Cross-project fallback so users can paste an id from anywhere.
            path = _find_session_anywhere(session_id)
            if path is None:
                raise FileNotFoundError(f"no such session: {session_id}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["session_id"], payload["messages"]


def _find_session_anywhere(session_id: str) -> Path | None:
    if not SESSION_DIR.exists():
        return None
    for sub in SESSION_DIR.iterdir():
        if not sub.is_dir():
            continue
        cand = sub / f"{session_id}.json"
        if cand.exists():
            return cand
    return None


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def _row_from_file(p: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    sid = payload.get("session_id", p.stem)
    msgs = payload.get("messages", []) or []
    first_user = next(
        (m.get("content", "") for m in msgs if m.get("role") == "user"),
        "",
    )
    first_user = (first_user or "").replace("\n", " ")
    return {
        "session_id": sid,
        "cwd": payload.get("cwd", "?"),
        "first_user_msg": first_user,
        "n_messages": len(msgs),
    }


def list_sessions_for_project(cwd: str | Path | None = None) -> list[dict[str, Any]]:
    """Sessions in the given (or current) project's subfolder, newest first.
    Session ids sort lexically = chronologically because they start with
    YYYYMMDD-HHMMSS, so reverse() is enough.
    """
    pdir = project_dir(cwd)
    if not pdir.exists():
        return []
    rows: list[dict[str, Any]] = []
    for p in sorted(pdir.glob("*.json"), reverse=True):
        if p.name == "latest.json":
            continue
        row = _row_from_file(p)
        if row is not None:
            rows.append(row)
    return rows


def list_all_sessions() -> list[dict[str, Any]]:
    """All sessions across every project subfolder, newest first."""
    if not SESSION_DIR.exists():
        return []
    rows: list[dict[str, Any]] = []
    for sub in SESSION_DIR.iterdir():
        if not sub.is_dir():
            continue
        for p in sub.glob("*.json"):
            if p.name == "latest.json":
                continue
            row = _row_from_file(p)
            if row is not None:
                rows.append(row)
    rows.sort(key=lambda r: r["session_id"], reverse=True)
    return rows


# ---------------------------------------------------------------------------
# Age formatting (used by /sessions and --pick)
# ---------------------------------------------------------------------------


def format_age(session_id_or_iso: str) -> str:
    """Turn a session id (`YYYYMMDD-HHMMSS-hex`) or an ISO timestamp into a
    short relative-age string."""
    dt = None
    # ISO first
    try:
        dt = datetime.fromisoformat(session_id_or_iso)
    except (ValueError, TypeError):
        pass
    if dt is None:
        # Session-id format
        try:
            parts = session_id_or_iso.split("-")
            if len(parts) >= 2:
                dt = datetime.strptime(parts[0] + parts[1], "%Y%m%d%H%M%S")
        except Exception:
            return "?"
    if dt is None:
        return "?"
    delta = (datetime.now() - dt).total_seconds()
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta / 60)} min ago"
    if delta < 86400:
        return f"{int(delta / 3600)} h ago"
    if delta < 7 * 86400:
        return f"{int(delta / 86400)} days ago"
    return dt.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# History rendering (used by --resume / /resume to replay past messages)
# ---------------------------------------------------------------------------


def _truncate(s: str, n: int = HISTORY_TRUNCATE) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + f" ...[+{len(s) - n} chars]"


def _summarize_tool_call(tc: dict[str, Any]) -> str:
    name = tc.get("function", {}).get("name", "?")
    raw = tc.get("function", {}).get("arguments", "")
    try:
        args = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        args = {"_raw": raw}
    parts = []
    for k, v in args.items():
        v_str = str(v).replace("\n", " ")
        if len(v_str) > 80:
            v_str = v_str[:80] + "..."
        parts.append(f"{k}={v_str!r}" if isinstance(v, str) else f"{k}={v_str}")
    return f"{name}({', '.join(parts)})"


def print_history(messages: list[dict[str, Any]]) -> None:
    visible = [m for m in messages if m.get("role") != "system"]
    print(ui.style(f"\n--- session history ({len(visible)} messages, excluding system) ---", ui.BOLD, ui.DIM))
    for m in visible:
        role = m.get("role")
        if role == "user":
            prefix = ui.style(">", ui.BOLD, ui.GREEN)
            print(f"\n{prefix} {_truncate(m.get('content', ''))}")
        elif role == "assistant":
            content = m.get("content")
            if content:
                label = ui.style("[CodeWu]", ui.BOLD, ui.CYAN)
                print(f"\n{label} {_truncate(content)}")
            for tc in m.get("tool_calls", []) or []:
                meta = ui.style(f"[~] called {_summarize_tool_call(tc)}", ui.DIM)
                print(meta)
        elif role == "tool":
            # tool results are implied by the next assistant message; skip
            continue
    print(ui.style("--- end history ---\n", ui.BOLD, ui.DIM))
