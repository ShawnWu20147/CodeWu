"""Session persistence + history rendering.

Sessions live in ~/.codewu/sessions/ globally. Each turn is persisted atomically;
latest.json is overwritten as a pointer for `--resume` with no explicit id.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime
from typing import Any

from . import ui
from .config import CWD, HISTORY_TRUNCATE, MODEL, SESSION_DIR


def new_session_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def save_session(session_id: str, messages: list[dict[str, Any]]) -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": session_id,
        "model": MODEL,
        "cwd": str(CWD),
        "messages": messages,
    }
    path = SESSION_DIR / f"{session_id}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    latest = SESSION_DIR / "latest.json"
    latest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_session(session_id: str | None) -> tuple[str, list[dict[str, Any]]]:
    if session_id is None:
        path = SESSION_DIR / "latest.json"
    else:
        path = SESSION_DIR / f"{session_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"no such session: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["session_id"], payload["messages"]


def list_sessions() -> list[tuple[str, str, str]]:
    """Returns list of (session_id, cwd, first_user_msg_preview)."""
    if not SESSION_DIR.exists():
        return []
    rows: list[tuple[str, str, str]] = []
    for p in sorted(SESSION_DIR.glob("*.json")):
        if p.name == "latest.json":
            continue
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
            sid = payload.get("session_id", p.stem)
            cwd = payload.get("cwd", "?")
            first_user = next(
                (m.get("content", "") for m in payload.get("messages", []) if m.get("role") == "user"),
                "",
            )
            first_user = (first_user or "").replace("\n", " ")[:60]
            rows.append((sid, cwd, first_user))
        except Exception:
            rows.append((p.stem, "?", "(unreadable)"))
    return rows


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
