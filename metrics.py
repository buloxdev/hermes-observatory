"""Metrics collection for Hermes Observatory.

Stores:
  - tool_calls: every tool invocation (name, success, latency_ms, timestamp)
  - session_rollups: per-session aggregates (token_count, tool_count, errors, duration)
Runtime: ~10 MB/day footprint — very light.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

DB_PATH = Path(get_hermes_home()) / "plugins" / "hermes-observatory" / "metrics.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_name TEXT NOT NULL,
    task_id TEXT,
    session_id TEXT,
    success INTEGER NOT NULL,        -- 0/1
    latency_ms INTEGER,
    error_type TEXT,                -- e.g. "timeout", "network", "auth"
    captured_at TIMESTAMP DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS session_rollups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT UNIQUE NOT NULL,
    ended_at TIMESTAMP DEFAULT (datetime('now')),
    token_count INTEGER DEFAULT 0,
    tool_count INTEGER DEFAULT 0,
    error_count INTEGER DEFAULT 0,
    duration_seconds REAL,
    composite_score REAL            -- from agent-reliability, if available
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_tool ON tool_calls(tool_name);
CREATE INDEX IF NOT EXISTS idx_tool_calls_session ON tool_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_session_ended ON session_rollups(ended_at);
"""


@contextmanager
def get_conn():
    """Yield a connection with Row factory; ensure directory exists."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Create tables if not present — idempotent."""
    with get_conn() as conn:
        conn.executescript(SCHEMA)


# ---------------------------------------------------------------------------
# Hook handlers
# ---------------------------------------------------------------------------

def record_tool_call(**kwargs):
    """post_tool_call hook — record a tool invocation."""
    init_db()
    tool_name = kwargs.get("tool_name", "unknown")
    task_id = kwargs.get("task_id", "")
    session_id = kwargs.get("session_id", "")
    success_val = kwargs.get("success", False)
    success = 1 if success_val else 0
    started = kwargs.get("started_at")
    completed = kwargs.get("completed_at")
    error_msg = kwargs.get("error") or ""
    result = kwargs.get("result")

    latency_ms = None
    if started is not None and completed is not None:
        latency_ms = int((completed - started) * 1000)

    error_type = None
    if not success:
        elower = error_msg.lower()
        if "timeout" in elower:
            error_type = "timeout"
        elif "network" in elower or "connect" in elower:
            error_type = "network"
        elif "auth" in elower or "key" in elower or "unauthorized" in elower:
            error_type = "auth"
        else:
            error_type = "other"

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO tool_calls (tool_name, task_id, session_id, success, latency_ms, error_type) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (tool_name, task_id, session_id, success, latency_ms, error_type),
        )


def rollup_session(session_id: str = "", duration_seconds: float = 0.0, **kwargs):
    """on_session_end hook — aggregate session-level stats."""
    init_db()
    
    # Sum tools + errors from this session
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*), SUM(latency_ms), AVG(latency_ms) FROM tool_calls WHERE session_id=?",
            (session_id,)
        )
        tool_count, total_latency, avg_latency = cur.fetchone()
        tool_count = tool_count or 0
        
        cur.execute(
            "SELECT COUNT(*) FROM tool_calls WHERE session_id=? AND success=0",
            (session_id,)
        )
        error_count = cur.fetchone()[0] or 0
        
        # Try to get reliability composite score if available
        composite_score = None
        scores_path = Path(get_hermes_home()) / "skills" / "agent-reliability" / "data" / "scores.db"
        if scores_path.exists():
            try:
                score_conn = sqlite3.connect(str(scores_path))
                score_conn.row_factory = sqlite3.Row
                scur = score_conn.cursor()
                scur.execute(
                    "SELECT composite FROM scores WHERE session_id=? ORDER BY id DESC LIMIT 1",
                    (session_id,)
                )
                row = scur.fetchone()
                if row:
                    composite_score = row["composite"]
                score_conn.close()
            except Exception:
                pass
        
        conn.execute(
            "INSERT OR REPLACE INTO session_rollups "
            "(session_id, ended_at, token_count, tool_count, error_count, duration_seconds, composite_score) "
            "VALUES (?, datetime('now'), ?, ?, ?, ?, ?)",
            (session_id, tool_count, error_count, duration_seconds, composite_score),
        )
    
    logger.info(f"Observatory: rolled up session {session_id[:12]} — "
                f"{tool_count} tools, {error_count} errors, score={composite_score}")


# ---------------------------------------------------------------------------
# Helpers for TUI to query metrics
# ---------------------------------------------------------------------------

def get_recent_token_burn(limit: int = 30) -> list[int]:
    """Return token counts (as proxy for burn) from last N sessions."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT token_count FROM session_rollups ORDER BY ended_at DESC LIMIT ?",
            (limit,)
        )
        rows = cur.fetchall()
        return [r[0] for r in reversed(rows)]  # oldest first


def get_recent_tool_counts(limit: int = 30) -> list[int]:
    """Return tool call counts from last N sessions."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT tool_count FROM session_rollups ORDER BY ended_at DESC LIMIT ?",
            (limit,)
        )
        rows = cur.fetchall()
        return [r[0] for r in reversed(rows)]


# Initialize DB on module import
init_db()
