"""Hermes Observatory dashboard plugin API.

Mounted at /api/plugins/hermes-observatory/ by the dashboard plugin system.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter


router = APIRouter()


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


HOME = hermes_home()
STATE_DB = HOME / "state.db"
SCORES_DB = HOME / "skills" / "agent-reliability" / "data" / "scores.db"
CRON_JOBS_FILE = HOME / "cron" / "jobs.json"
GATEWAY_LOG = HOME / "logs" / "gateway.log"


def parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromtimestamp(float(text))
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def age(value: Any) -> str:
    dt = parse_ts(value)
    if dt is None:
        return "unknown"
    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
    seconds = max(0, int((now - dt).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return dt.strftime("%m/%d")


def pid_alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def gateway_status() -> dict[str, Any]:
    running = False
    pid = None
    try:
        proc = subprocess.run(
            ["pgrep", "-fl", "hermes_cli.main.*gateway run"],
            capture_output=True,
            text=True,
            timeout=1,
        )
        matches = [line for line in proc.stdout.splitlines() if "gateway run" in line and "pgrep" not in line]
        if matches:
            running = True
            pid = int(matches[0].split()[0])
    except Exception:
        pass

    last_signal = None
    if GATEWAY_LOG.exists():
        if time.time() - GATEWAY_LOG.stat().st_mtime < 90:
            running = True
        try:
            for line in reversed(GATEWAY_LOG.read_text(errors="ignore").splitlines()[-240:]):
                if " ERROR " in line or " WARNING " in line or "No response from provider" in line:
                    last_signal = line.strip()[-160:]
                    break
        except Exception:
            pass

    return {"running": running, "pid": pid, "last_signal": last_signal}


def active_profiles() -> list[dict[str, Any]]:
    candidates = [("default", HOME / "gateway_state.json")]
    root = HOME / "profiles"
    if root.exists():
        candidates.extend((p.parent.name, p) for p in sorted(root.glob("*/gateway_state.json")))

    rows: list[dict[str, Any]] = []
    for name, state_file in candidates:
        if not state_file.exists():
            continue
        try:
            state = json.loads(state_file.read_text())
        except Exception:
            continue
        platforms = state.get("platforms") or {}
        connected = [
            platform
            for platform, detail in platforms.items()
            if isinstance(detail, dict) and detail.get("state") == "connected"
        ]
        running = state.get("gateway_state") == "running" and pid_alive(state.get("pid"))
        if not running and not connected:
            continue
        rows.append({
            "name": name,
            "state": "online" if running else "stale",
            "platforms": connected,
            "updated": age(state.get("updated_at")),
            "pid": state.get("pid"),
        })
    return rows


def sessions(limit: int = 12) -> list[dict[str, Any]]:
    if not STATE_DB.exists():
        return []
    try:
        conn = sqlite3.connect(str(STATE_DB))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, source, user_id, model, started_at, title,
                   tool_call_count,
                   input_tokens + output_tokens + reasoning_tokens AS tokens,
                   estimated_cost_usd
            FROM sessions
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = [
            {
                "id": row["id"][:10],
                "source": row["source"] or "cli",
                "user": row["user_id"] or "local",
                "model": row["model"] or "-",
                "seen": age(row["started_at"]),
                "title": row["title"] or "-",
                "tools": row["tool_call_count"] or 0,
                "tokens": row["tokens"] or 0,
                "cost": row["estimated_cost_usd"] or 0,
            }
            for row in cur.fetchall()
        ]
        conn.close()
        return rows
    except Exception:
        return []


def reliability() -> dict[str, Any]:
    result: dict[str, Any] = {"avg": None, "count": 0, "issues": []}
    if not SCORES_DB.exists():
        return result
    try:
        conn = sqlite3.connect(str(SCORES_DB))
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*), AVG(composite) FROM scores")
        count, avg = cur.fetchone()
        result["count"] = count or 0
        result["avg"] = round(avg, 1) if avg else None
        cur.execute("PRAGMA table_info(scores)")
        columns = {row[1] for row in cur.fetchall()}
        detail_col = "highlights" if "highlights" in columns else "details"
        cur.execute(f"SELECT session_id, composite, {detail_col} FROM scores ORDER BY composite ASC LIMIT 5")
        issues = []
        for sid, score, detail in cur.fetchall():
            summary = str(detail or "")
            if summary.startswith("{"):
                try:
                    payload = json.loads(summary)
                    highlights = payload.get("highlights")
                    if isinstance(highlights, list) and highlights:
                        summary = " / ".join(str(item) for item in highlights[:2])
                    elif isinstance(payload.get("metrics"), dict):
                        metrics = payload["metrics"]
                        summary = f"{metrics.get('error_count', 0)} errors, {metrics.get('tool_call_failures', 0)} tool failures"
                except Exception:
                    pass
            issues.append({"id": sid[:10], "score": round(score or 0, 1), "summary": summary[:140]})
        result["issues"] = issues
        conn.close()
    except Exception:
        pass
    return result


def token_trace(limit: int = 30) -> dict[str, list[int]]:
    if not STATE_DB.exists():
        return {"tokens": [], "tools": []}
    try:
        conn = sqlite3.connect(str(STATE_DB))
        cur = conn.cursor()
        cur.execute(
            """
            SELECT input_tokens + output_tokens + reasoning_tokens, tool_call_count
            FROM sessions
            WHERE (input_tokens + output_tokens + reasoning_tokens) > 0
               OR tool_call_count > 0
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = cur.fetchall()[::-1]
        conn.close()
        return {"tokens": [r[0] or 0 for r in rows], "tools": [r[1] or 0 for r in rows]}
    except Exception:
        return {"tokens": [], "tools": []}


def cron_jobs() -> list[dict[str, Any]]:
    if not CRON_JOBS_FILE.exists():
        return []
    try:
        data = json.loads(CRON_JOBS_FILE.read_text())
        rows = []
        for job in data.get("jobs", [])[:8]:
            rows.append({
                "name": job.get("name") or job.get("id") or "job",
                "schedule": job.get("schedule") or "-",
                "platform": job.get("platform") or job.get("deliver") or "-",
                "status": "ok",
            })
        return rows
    except Exception:
        return []


def next_moves(snapshot: dict[str, Any]) -> list[dict[str, str]]:
    moves: list[dict[str, str]] = []
    trace = snapshot["trace"]
    latest_tokens = trace["tokens"][-1] if trace["tokens"] else 0
    latest_tools = trace["tools"][-1] if trace["tools"] else 0
    low = [i for i in snapshot["reliability"]["issues"] if i["score"] < 50]

    if not snapshot["gateway"]["running"]:
        moves.append({"priority": "P0", "title": "Restart the gateway", "why": "Messaging and cron entrypoints are offline."})
    if not snapshot["active_profiles"]:
        moves.append({"priority": "P1", "title": "Reconnect a profile", "why": "No live gateway profile is connected."})
    if len(snapshot["active_profiles"]) >= 2:
        names = ", ".join(p["name"] for p in snapshot["active_profiles"][:3])
        moves.append({"priority": "P2", "title": "Demo profile switching", "why": f"{names} are online."})
    if low:
        moves.append({"priority": "P1", "title": "Inspect lowest-score session", "why": f"{low[0]['id']} is at {low[0]['score']}/100."})
    if latest_tokens > 100_000:
        moves.append({"priority": "P2", "title": "Review token burn", "why": f"Latest session used {latest_tokens:,} tokens."})
    if latest_tools > 50:
        moves.append({"priority": "P2", "title": "Audit tool-call loop risk", "why": f"Latest session made {latest_tools:,} tool calls."})
    moves.extend([
        {"priority": "IDEA", "title": "Export an Observatory report", "why": "Create a shareable health brief for a team or judge."},
        {"priority": "IDEA", "title": "Add profile routing controls", "why": "Start, stop, or restart profiles from the dashboard."},
    ])
    return moves[:6]


@router.get("/snapshot")
async def snapshot() -> dict[str, Any]:
    data = {
        "gateway": gateway_status(),
        "active_profiles": active_profiles(),
        "sessions": sessions(),
        "reliability": reliability(),
        "trace": token_trace(),
        "cron": cron_jobs(),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    data["next_moves"] = next_moves(data)
    return data
