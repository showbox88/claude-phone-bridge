"""SQLite-backed session + message store for Phone Bridge."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

_DB_PATH: Path | None = None


def init(path: Path) -> None:
    global _DB_PATH
    _DB_PATH = path
    path.parent.mkdir(parents=True, exist_ok=True)
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            cwd TEXT NOT NULL,
            sdk_session_id TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            seq INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            created_at REAL NOT NULL,
            model TEXT,
            mode TEXT,
            duration_ms INTEGER DEFAULT 0,
            num_turns INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            cache_create_tokens INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_messages_session_seq
            ON messages(session_id, seq);
        CREATE INDEX IF NOT EXISTS idx_sessions_updated
            ON sessions(updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
        CREATE INDEX IF NOT EXISTS idx_turns_created ON turns(created_at DESC);
        """)
        # backwards-compat: add columns if upgrading from earlier schema
        for col, ddl in [
            ("mode",  "ALTER TABLE sessions ADD COLUMN mode TEXT DEFAULT 'code'"),
            ("model", "ALTER TABLE sessions ADD COLUMN model TEXT DEFAULT ''"),
        ]:
            try:
                c.execute(ddl)
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise


def _conn() -> sqlite3.Connection:
    assert _DB_PATH is not None, "db.init() not called"
    c = sqlite3.connect(_DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def create_session(cwd: str, title: str = "", mode: str = "code", model: str = "") -> str:
    sid = uuid.uuid4().hex
    now = time.time()
    with _conn() as c:
        c.execute(
            "INSERT INTO sessions(id, title, cwd, sdk_session_id, mode, model, created_at, updated_at) "
            "VALUES (?, ?, ?, NULL, ?, ?, ?, ?)",
            (sid, title, cwd, mode, model, now, now),
        )
    return sid


def list_sessions() -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute("""
            SELECT s.id, s.title, s.cwd, s.mode, s.model, s.created_at, s.updated_at,
                   (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS msg_count
            FROM sessions s
            ORDER BY s.updated_at DESC
        """).fetchall()
    return [dict(r) for r in rows]


def get_session(sid: str) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute(
            "SELECT id, title, cwd, sdk_session_id, mode, model, created_at, updated_at "
            "FROM sessions WHERE id = ?",
            (sid,),
        ).fetchone()
        if not row:
            return None
        msgs = c.execute(
            "SELECT seq, role, content, created_at FROM messages "
            "WHERE session_id = ? ORDER BY seq ASC",
            (sid,),
        ).fetchall()
    out = dict(row)
    out["messages"] = [
        {**dict(m), "content": json.loads(m["content"])} for m in msgs
    ]
    return out


def append_message(session_id: str, role: str, content: dict) -> int:
    now = time.time()
    with _conn() as c:
        seq_row = c.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 AS next FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        seq = seq_row["next"]
        c.execute(
            "INSERT INTO messages(session_id, seq, role, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, seq, role, json.dumps(content, ensure_ascii=False), now),
        )
        c.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
    return seq


def update_session(
    sid: str,
    *,
    title: str | None = None,
    sdk_session_id: str | None = None,
    cwd: str | None = None,
    mode: str | None = None,
    model: str | None = None,
) -> None:
    sets: list[str] = []
    args: list[Any] = []
    if title is not None:
        sets.append("title = ?"); args.append(title)
    if sdk_session_id is not None:
        sets.append("sdk_session_id = ?"); args.append(sdk_session_id)
    if cwd is not None:
        sets.append("cwd = ?"); args.append(cwd)
    if mode is not None:
        sets.append("mode = ?"); args.append(mode)
    if model is not None:
        sets.append("model = ?"); args.append(model)
    if not sets:
        return
    sets.append("updated_at = ?"); args.append(time.time())
    args.append(sid)
    with _conn() as c:
        c.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", args)


def append_turn(
    session_id: str,
    *,
    model: str | None,
    mode: str | None,
    duration_ms: int,
    num_turns: int,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_create_tokens: int,
    cost_usd: float,
) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO turns(session_id, created_at, model, mode, duration_ms, num_turns, "
            "input_tokens, output_tokens, cache_read_tokens, cache_create_tokens, cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, time.time(), model or "", mode or "", duration_ms, num_turns,
             input_tokens, output_tokens, cache_read_tokens, cache_create_tokens, cost_usd),
        )


def usage_summary() -> dict[str, Any]:
    """Aggregate stats: total, today, this month, by model, by day (last 30)."""
    now = time.time()
    today_start = now - (now % 86400)  # rough UTC day; OK for personal use
    month_start = now - 30 * 86400
    with _conn() as c:
        total = c.execute("""
            SELECT COUNT(*) AS turns,
                   COALESCE(SUM(input_tokens),0) AS in_tok,
                   COALESCE(SUM(output_tokens),0) AS out_tok,
                   COALESCE(SUM(cache_read_tokens),0) AS cache_read,
                   COALESCE(SUM(cache_create_tokens),0) AS cache_create,
                   COALESCE(SUM(cost_usd),0) AS cost
            FROM turns
        """).fetchone()
        today = c.execute(
            "SELECT COALESCE(SUM(cost_usd),0) AS cost, COUNT(*) AS turns "
            "FROM turns WHERE created_at >= ?", (today_start,)
        ).fetchone()
        month = c.execute(
            "SELECT COALESCE(SUM(cost_usd),0) AS cost, COUNT(*) AS turns "
            "FROM turns WHERE created_at >= ?", (month_start,)
        ).fetchone()
        by_model = c.execute("""
            SELECT model, COUNT(*) AS turns,
                   COALESCE(SUM(input_tokens),0) AS in_tok,
                   COALESCE(SUM(output_tokens),0) AS out_tok,
                   COALESCE(SUM(cost_usd),0) AS cost
            FROM turns
            GROUP BY model
            ORDER BY cost DESC
        """).fetchall()
        # daily for last 30 days
        by_day = c.execute("""
            SELECT CAST(created_at / 86400 AS INTEGER) AS day,
                   COALESCE(SUM(cost_usd),0) AS cost,
                   COUNT(*) AS turns
            FROM turns
            WHERE created_at >= ?
            GROUP BY day
            ORDER BY day ASC
        """, (month_start,)).fetchall()
    return {
        "total":   dict(total),
        "today":   dict(today),
        "month":   dict(month),
        "by_model": [dict(r) for r in by_model],
        "by_day":   [dict(r) for r in by_day],
    }


def delete_session(sid: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM sessions WHERE id = ?", (sid,))


def latest_session_id(mode: str | None = None) -> str | None:
    with _conn() as c:
        if mode is None:
            row = c.execute(
                "SELECT id FROM sessions ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        else:
            row = c.execute(
                "SELECT id FROM sessions WHERE COALESCE(mode,'code') = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (mode,),
            ).fetchone()
    return row["id"] if row else None
