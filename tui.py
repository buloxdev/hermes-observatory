"""
Hermes Observatory TUI — visually polished, hackathon-ready dashboard.

Run: hermes observatory

Dependencies (install in Hermes venv):
  pip install textual simpleaudio
"""

import os
import subprocess
import sqlite3
import time
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from functools import partial

# Third-party
try:
    from textual import events, on
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
    from textual.widgets import Footer, Header, Static, Label
    from textual.message import Message
    from rich.columns import Columns
    from rich.console import Group
    from rich.text import Text
    from rich.table import Table
    from rich.panel import Panel
    import simpleaudio as sa
except ImportError as e:
    print(f"ERROR: Missing dependencies. Install with:\n  pip install textual simpleaudio\n\nImport error: {e}")
    raise

# ---------------------------------------------------------------------------
# Hermes paths
# ---------------------------------------------------------------------------

def get_hermes_home() -> Path:
    home = os.environ.get("HERMES_HOME")
    if home:
        return Path(home)
    return Path.home() / ".hermes"

HERMES_HOME = get_hermes_home()
GATEWAY_LOG = HERMES_HOME / "logs" / "gateway.log"
STATE_DB = HERMES_HOME / "state.db"
PLUGIN_DB = HERMES_HOME / "plugins" / "hermes-observatory" / "metrics.db"
SCORES_DB = HERMES_HOME / "skills" / "agent-reliability" / "data" / "scores.db"
CRON_JOBS_FILE = HERMES_HOME / "cron" / "jobs.json"

# ---------------------------------------------------------------------------
# Color Palette (Dark, Modern)
# ---------------------------------------------------------------------------

C = {
    "bg": "#0a0e17",
    "panel": "rgba(30, 41, 59, 0.85)",
    "panel_hover": "rgba(51, 65, 85, 0.9)",
    "accent": "#00d4aa",      # teal (success/live)
    "accent_glow": "#00ffcc",
    "alert": "#ff6b9d",       # pink
    "text": "#e2e8f0",
    "text_dim": "#64748b",
    "success": "#4ade80",
    "warning": "#fbbf24",
    "error": "#f87171",
    "border": "rgba(148, 163, 184, 0.25)",
}

# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def _parse_timestamp(value) -> Optional[datetime]:
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

def _relative_age(value) -> str:
    dt = _parse_timestamp(value)
    if dt is None:
        return "?"
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

# ---------------------------------------------------------------------------
# Data collectors — gateway, profiles, cron, scores, log tail
# ---------------------------------------------------------------------------

def get_gateway_status() -> Dict:
    running = False
    pid = None
    last_error: Optional[str] = None

    try:
        proc = subprocess.run(
            ["pgrep", "-fl", "hermes_cli.main.*gateway run"],
            capture_output=True, text=True, timeout=1,
        )
        matches = [line for line in proc.stdout.splitlines()
                   if "pgrep" not in line and "gateway run" in line]
        if matches:
            running = True
            try:
                pid = int(matches[0].split()[0])
            except Exception:
                pid = None
    except Exception:
        pass

    if GATEWAY_LOG.exists():
        mtime = GATEWAY_LOG.stat().st_mtime
        if time.time() - mtime < 60:
            running = True
        try:
            with open(GATEWAY_LOG) as f:
                all_lines = f.readlines()
            for line in reversed(all_lines[-200:]):
                if " ERROR " in line or " WARNING " in line:
                    last_error = line.strip()[-120:]
                    break
        except Exception:
            pass
    else:
        last_error = "gateway.log not found"

    return {"running": running, "pid": pid, "last_error": last_error}


def get_profile_statuses() -> List[Dict]:
    profiles: List[Dict] = []
    if not STATE_DB.exists():
        return profiles

    try:
        conn = sqlite3.connect(str(STATE_DB))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT source, user_id, MAX(started_at) AS last_started,
                   SUM(tool_call_count) AS tools,
                   SUM(input_tokens+output_tokens+reasoning_tokens) AS tokens
            FROM sessions
            GROUP BY source, user_id
            ORDER BY last_started DESC
            LIMIT 8
        """)
        rows = cur.fetchall()
        for row in rows:
            source = row["source"] or "cli"
            user = row["user_id"] or "local"
            last_ts = row["last_started"]
            last_dt = _parse_timestamp(last_ts)
            if last_dt:
                age_sec = (datetime.now(last_dt.tzinfo) - last_dt).total_seconds()
                status = "online" if age_sec < 300 else "idle"
            else:
                status = "unknown"
            profiles.append({
                "id": f"{source}/{user}",
                "status": status,
                "last_seen": _relative_age(last_ts),
                "tools": row["tools"] or 0,
                "tokens": row["tokens"] or 0,
            })
        conn.close()
    except Exception as e:
        print(f"[WARN] profiles error: {e}")

    return profiles


def _pid_alive(pid) -> bool:
    try:
        if not pid:
            return False
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def get_active_gateway_profiles() -> List[Dict]:
    """Profiles with a running gateway process and at least one connected platform."""
    candidates = [("default", HERMES_HOME / "gateway_state.json")]
    profiles_root = HERMES_HOME / "profiles"
    if profiles_root.exists():
        for state_file in sorted(profiles_root.glob("*/gateway_state.json")):
            candidates.append((state_file.parent.name, state_file))

    active: List[Dict] = []
    try:
        import json
        for name, state_file in candidates:
            if not state_file.exists():
                continue
            try:
                state = json.loads(state_file.read_text())
            except Exception:
                continue

            pid = state.get("pid")
            platforms = state.get("platforms") or {}
            connected = [
                platform
                for platform, detail in platforms.items()
                if isinstance(detail, dict) and detail.get("state") == "connected"
            ]
            is_running = state.get("gateway_state") == "running" and _pid_alive(pid)
            if not is_running and not connected:
                continue

            updated_at = state.get("updated_at")
            active.append({
                "id": name,
                "status": "online" if is_running else "stale",
                "last_seen": _relative_age(updated_at),
                "tools": len(connected),
                "tokens": 0,
                "detail": ", ".join(connected) if connected else "no connected platforms",
            })
    except Exception as e:
        print(f"[WARN] active gateway profiles error: {e}")

    return active


def get_cron_statuses() -> List[Dict]:
    jobs: List[Dict] = []
    if not CRON_JOBS_FILE.exists():
        return jobs

    try:
        import json
        from croniter import croniter
        data = json.loads(CRON_JOBS_FILE.read_text())
        for job in data.get("jobs", []):
            job_id = job.get("id", "?")
            name = job.get("name") or job_id
            schedule = job.get("schedule", "?")
            try:
                base = datetime.now()
                itr = croniter(schedule, base)
                next_run = itr.get_next(datetime)
                next_str = next_run.strftime("%m/%d %H:%")
            except Exception:
                next_str = "?"
            out_dir = HERMES_HOME / "cron" / "output" / job_id
            status = "ok"
            if out_dir.exists():
                latest = sorted(out_dir.glob("*.md"), key=lambda p: p.stat().st_mtime)
                if latest:
                    content = latest[-1].read_text()
                    if "ERROR" in content or "failed" in content.lower():
                        status = "fail"
            jobs.append({"name": name[:18], "schedule": schedule, "next": next_str, "status": status})
    except Exception as e:
        print(f"[WARN] cron error: {e}")

    return jobs


def get_reliability_scores() -> Dict:
    scores = {"avg": None, "count": 0, "issues": []}
    if not SCORES_DB.exists():
        return scores

    try:
        conn = sqlite3.connect(str(SCORES_DB))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*), AVG(composite) FROM scores")
        cnt, avg = cur.fetchone()
        scores["count"] = cnt or 0
        scores["avg"] = round(avg, 1) if avg else None

        cur.execute("PRAGMA table_info(scores)")
        columns = {row[1] for row in cur.fetchall()}
        detail_col = "highlights" if "highlights" in columns else "details"

        cur.execute(f"""
            SELECT session_id, composite, {detail_col}
            FROM scores
            ORDER BY composite ASC
            LIMIT 6
        """)
        issues = []
        for sid, comp, hl in cur.fetchall():
            summary = hl or ""
            if isinstance(summary, str) and summary.strip().startswith("{"):
                try:
                    import json
                    payload = json.loads(summary)
                    highlights = payload.get("highlights")
                    if isinstance(highlights, list) and highlights:
                        summary = " / ".join(str(item) for item in highlights[:2])
                    elif isinstance(payload.get("metrics"), dict):
                        metrics = payload["metrics"]
                        summary = (
                            f"{metrics.get('error_count', 0)} errors, "
                            f"{metrics.get('tool_call_failures', 0)} tool failures, "
                            f"{metrics.get('restart_count', 0)} restarts"
                        )
                except Exception:
                    pass
            issues.append({"id": sid[:12], "score": comp, "issues": summary})
        scores["issues"] = issues
        conn.close()
    except Exception as e:
        print(f"[WARN] scores error: {e}")

    return scores


def tail_gateway_log(lines: int = 50) -> List[str]:
    if not GATEWAY_LOG.exists():
        return ["[dim]gateway.log not found[/dim]"]
    try:
        with open(GATEWAY_LOG) as f:
            all_lines = f.readlines()
        return [l.rstrip("\n") for l in all_lines[-lines:]]
    except Exception:
        return ["[red]error reading gateway.log[/red]"]


def get_metrics_sparklines() -> tuple[List[int], List[int]]:
    tokens: List[int] = []
    tools: List[int] = []
    if PLUGIN_DB.exists():
        try:
            conn = sqlite3.connect(str(PLUGIN_DB))
            cur = conn.cursor()
            cur.execute("SELECT token_count FROM session_rollups ORDER BY ended_at DESC LIMIT 30")
            tokens = [r[0] for r in cur.fetchall()[::-1]]
            cur.execute("SELECT tool_count FROM session_rollups ORDER BY ended_at DESC LIMIT 30")
            tools = [r[0] for r in cur.fetchall()[::-1]]
            conn.close()
        except Exception:
            pass
    if (not tokens or not tools) and STATE_DB.exists():
        try:
            conn = sqlite3.connect(str(STATE_DB))
            cur = conn.cursor()
            cur.execute("""
                SELECT input_tokens + output_tokens + reasoning_tokens, tool_call_count
                FROM sessions
                WHERE (input_tokens + output_tokens + reasoning_tokens) > 0
                   OR tool_call_count > 0
                ORDER BY started_at DESC
                LIMIT 30
            """)
            rows = cur.fetchall()[::-1]
            tokens = [r[0] or 0 for r in rows]
            tools = [r[1] or 0 for r in rows]
            conn.close()
        except Exception:
            pass
    return tokens, tools


def get_recent_sessions(limit: int = 30) -> List[Dict]:
    """Return recent Hermes sessions for the Sessions tab."""
    if not STATE_DB.exists():
        return []
    try:
        conn = sqlite3.connect(str(STATE_DB))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, source, user_id, model, started_at, ended_at, end_reason,
                   title, tool_call_count, input_tokens, output_tokens,
                   reasoning_tokens, estimated_cost_usd
            FROM sessions
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = []
        for row in cur.fetchall():
            tokens = (
                (row["input_tokens"] or 0)
                + (row["output_tokens"] or 0)
                + (row["reasoning_tokens"] or 0)
            )
            rows.append({
                "id": (row["id"] or "")[:10],
                "source": row["source"] or "cli",
                "user": row["user_id"] or "local",
                "model": row["model"] or "-",
                "seen": _relative_age(row["started_at"]),
                "tools": row["tool_call_count"] or 0,
                "tokens": tokens,
                "cost": row["estimated_cost_usd"],
                "status": "OPEN" if row["ended_at"] is None else (row["end_reason"] or "DONE"),
                "title": row["title"] or "-",
            })
        conn.close()
        return rows
    except Exception as e:
        print(f"[WARN] sessions error: {e}")
        return []


def get_skill_rows(limit: int = 40) -> List[Dict]:
    """Return installed skill names from common Hermes/Codex skill folders."""
    roots = [
        HERMES_HOME / "skills",
        Path.home() / ".codex" / "skills",
        Path.home() / ".claude" / "skills",
    ]
    rows: List[Dict] = []
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("SKILL.md")):
            if len(rows) >= limit:
                break
            name = path.parent.name
            desc = ""
            try:
                for line in path.read_text(errors="replace").splitlines():
                    clean = line.strip("# ").strip()
                    if clean and clean.lower() != name.lower():
                        desc = clean[:92]
                        break
            except Exception:
                pass
            rows.append({
                "name": name,
                "status": "READY",
                "source": root.name,
                "description": desc or "Skill installed and discoverable",
            })
    return rows[:limit]


def get_env_rows(limit: int = 45) -> List[Dict]:
    """Return redacted environment/config keys for the Env tab."""
    rows: List[Dict] = []
    env_file = HERMES_HOME / ".env"
    if env_file.exists():
        try:
            for line in env_file.read_text(errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                rows.append({
                    "name": key.strip(),
                    "value": "SET" if value.strip() else "-",
                    "description": "Hermes environment file",
                })
                if len(rows) >= limit:
                    return rows
        except Exception:
            pass
    for key in sorted(os.environ):
        if key.startswith(("HERMES_", "OPENAI_", "ANTHROPIC_", "GEMINI_", "MINIMAX_", "STEPFUN_")):
            rows.append({"name": key, "value": "SET", "description": "process environment"})
            if len(rows) >= limit:
                break
    return rows


def _redact_config_value(key: str, value) -> str:
    """Return a compact, safe display value for config entries."""
    key_lower = key.lower()
    if any(part in key_lower for part in ("key", "token", "secret", "password", "credential")):
        return "SET" if value not in (None, "", False) else "-"
    if isinstance(value, bool):
        return "ON" if value else "OFF"
    if value is None:
        return "-"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return f"{len(value)} items"
    if isinstance(value, dict):
        return f"{len(value)} keys"
    text = str(value).strip()
    if not text:
        return "-"
    return text[:42] + ("..." if len(text) > 42 else "")


def _flatten_config(prefix: str, value, rows: List[Dict], limit: int) -> None:
    if len(rows) >= limit:
        return
    if isinstance(value, dict):
        if prefix:
            rows.append({
                "section": prefix.rsplit(".", 1)[0] if "." in prefix else "root",
                "key": prefix,
                "value": f"{len(value)} keys",
                "type": "section",
            })
        for key, child in value.items():
            child_key = f"{prefix}.{key}" if prefix else str(key)
            _flatten_config(child_key, child, rows, limit)
            if len(rows) >= limit:
                return
    else:
        rows.append({
            "section": prefix.rsplit(".", 1)[0] if "." in prefix else "root",
            "key": prefix,
            "value": _redact_config_value(prefix, value),
            "type": type(value).__name__,
        })


def get_config_rows(limit: int = 80) -> List[Dict]:
    """Return redacted config rows from the active Hermes config."""
    config_file = HERMES_HOME / "config.yaml"
    if not config_file.exists():
        return []
    try:
        import yaml
        data = yaml.safe_load(config_file.read_text(errors="replace")) or {}
    except Exception:
        return []
    rows: List[Dict] = []
    _flatten_config("", data, rows, limit)
    return rows[:limit]


# ---------------------------------------------------------------------------
# Sound effects (optional)
# ---------------------------------------------------------------------------

def play_sound(event: str) -> None:
    """Play a subtle sound effect if assets exist."""
    sound_map = {
        "refresh": "click.wav",
        "gateway_up": "chime.wav",
        "gateway_down": "buzz.wav",
        "alert": "pop.wav",
    }
    fname = sound_map.get(event)
    if not fname:
        return
    path = HERMES_HOME / "plugins" / "hermes-observatory" / "assets" / "sounds" / fname
    if not path.exists():
        return
    try:
        wave = sa.WaveObject.from_wave_file(str(path))
        wave.play()
    except Exception:
        pass  # Silently ignore sound errors


# ---------------------------------------------------------------------------
# Custom Widgets
# ---------------------------------------------------------------------------

class StatusBanner(Static):
    """Top gradient banner with compact metric chips."""

    def compose(self) -> ComposeResult:
        yield Static(id="banner-layout")

    def update_data(
        self,
        gw,
        reliability,
        profiles,
        cron_count,
        refreshed_at: str = "",
        paused: bool = False,
        demo: bool = False,
    ):
        banner = Text()
        banner.append(" HERMES OBSERVATORY ", style="bold #0f172a on #5eead4")
        banner.append("  real-time agent telemetry  ", style="#94a3b8")
        gw_color = C["success"] if gw["running"] else C["error"]
        banner.append("  GATEWAY ", style="#64748b")
        banner.append("ONLINE" if gw["running"] else "OFFLINE", style=f"bold {gw_color}")
        if gw.get("pid"):
            banner.append(f"  pid {gw['pid']}", style=C["text_dim"])
        score = reliability.get("avg")
        if score is not None:
            score_color = C["success"] if score >= 70 else C["warning"] if score >= 50 else C["error"]
            banner.append("   SCORE ", style="#64748b")
            banner.append(f"{score:>4}/100", style=f"bold {score_color}")
            banner.append(f"  {reliability['count']} sessions", style=C["text_dim"])
        banner.append("   ACTIVE STREAMS ", style="#64748b")
        banner.append(str(len(profiles)), style="bold #bfdbfe")
        banner.append("   SCHEDULED ", style="#64748b")
        banner.append(str(cron_count), style="bold #fde68a")
        if paused:
            banner.append("   PAUSED", style=f"bold {C['warning']}")
        if demo:
            banner.append("   DEMO", style="bold #c084fc")
        if refreshed_at:
            banner.append("   REFRESHED ", style="#64748b")
            banner.append(refreshed_at, style="bold #e2e8f0")

        self.query_one("#banner-layout", Static).update(banner)


class FleetGrid(Static):
    """Operator view: recent channels plus scheduled jobs."""

    def compose(self) -> ComposeResult:
        yield Static(id="fleet-content")

    def update_data(self, profiles: List[Dict], crons: List[Dict]):
        profile_table = Table(
            show_header=True,
            header_style="bold #5eead4",
            box=None,
            expand=True,
            padding=(0, 1),
        )
        profile_table.add_column("stream", no_wrap=True, ratio=2)
        profile_table.add_column("state", no_wrap=True, ratio=1)
        profile_table.add_column("seen", no_wrap=True, justify="right")
        profile_table.add_column("tools", no_wrap=True, justify="right")
        for p in profiles[:7]:
            state_color = C["success"] if p["status"] == "online" else C["warning"]
            profile_table.add_row(
                p["id"][:28],
                f"[{state_color}]{p['status'].upper()}[/{state_color}]",
                p["last_seen"],
                f"{p['tools']:,}",
            )
        if not profiles:
            profile_table.add_row("no recent streams", "-", "-", "-")

        cron_table = Table(
            show_header=True,
            header_style="bold #fde68a",
            box=None,
            expand=True,
            padding=(0, 1),
        )
        cron_table.add_column("job", no_wrap=True, ratio=2)
        cron_table.add_column("next", no_wrap=True, justify="right")
        for j in crons[:7]:
            status = "OK" if j["status"] == "ok" else "FAIL"
            color = C["success"] if j["status"] == "ok" else C["error"]
            cron_table.add_row(j["name"][:26], f"[{color}]{status} {j['next']}[/{color}]")
        if not crons:
            cron_table.add_row("no scheduled jobs", "-")

        self.query_one("#fleet-content", Static).update(
            Columns(
                [
                    Panel(profile_table, title="LIVE STREAMS", border_style="#2dd4bf", padding=(1, 1)),
                    Panel(cron_table, title="SCHEDULE", border_style="#f59e0b", padding=(1, 1)),
                ],
                equal=True,
                expand=True,
            )
        )


class AlertsPanel(ScrollableContainer):
    """Scrollable list of low-score sessions with color-coded severity."""

    def compose(self) -> ComposeResult:
        yield Static(id="alerts-content")

    def update_issues(self, issues: List[Dict]):
        content = []
        if not issues:
            content.append(Text("All tracked sessions are inside the healthy band.", style=C["text"]))
        else:
            content.append(Text("LOW-SCORE SESSIONS\n", style="bold #fb7185"))
            for iss in issues:
                sid = iss["id"]
                score = iss["score"]
                raw = (iss.get("issues") or "").replace("\n", " ")
                snippet = raw[:92] + ("..." if len(raw) > 92 else "")
                sev_color = C["error"] if score < 40 else C["warning"]
                content.append(Text(f"{sid:<14}", style="bold #e2e8f0"))
                content.append(Text(f"{score:>5.1f}", style=f"bold {sev_color}"))
                content.append(Text("  " + snippet + "\n", style=C["text_dim"]))

        self.query_one("#alerts-content", Static).update(Text("\n").join(content))


class SparklineWidget(Static):
    """Animated horizontal bar chart with gradient fill."""

    def __init__(self, title: str = "", color: str = C["accent"]):
        super().__init__()
        self.title = title
        self.color = color
        self._history: List[int] = []

    def compose(self) -> ComposeResult:
        yield Static(id="spark-content")

    def update_values(self, values: List[int]):
        self._history = values[-36:]

        if not self._history:
            self.query_one("#spark-content", Static).update(f"{self.title}: no data yet")
            return

        mn, mx = min(self._history), max(self._history)
        blocks = "▁▂▃▄▅▆▇█"
        if mn == mx:
            bars = "▁" * len(self._history)
        else:
            norm = [(v - mn) / (mx - mn + 1e-9) for v in self._history]
            bars = "".join(blocks[int(n * (len(blocks) - 1))] for n in norm)

        content = Text()
        content.append(f"{self.title.upper():<12}", style="bold #e2e8f0")
        content.append(bars, style=self.color)
        content.append(f"  latest {self._history[-1]:,}", style=C["text_dim"])
        self.query_one("#spark-content", Static).update(content)


class LogTail(ScrollableContainer):
    """Gateway log tail with syntax highlighting per line."""

    def compose(self) -> ComposeResult:
        yield Static(id="log-content")

    def update_lines(self, lines: List[str]):
        rendered = []
        for line in lines[-200:]:
            clean = line[-180:]
            text = Text(clean)
            if " ERROR " in line:
                text.stylize(C["error"])
            elif " WARNING " in line:
                text.stylize(C["warning"])
            elif " INFO " in line:
                text.stylize("#60a5fa")
            elif " DEBUG " in line:
                text.stylize(C["text_dim"])
            rendered.append(text)

        container = self.query_one("#log-content", Static)
        container.update(Text("\n").join(rendered))
        container.scroll_end(animate=False)


class TabBar(Static):
    """Top navigation inspired by HUDs, but with a cinematic Observatory skin."""

    TABS = ["Overview", "Sessions", "Skills", "Config", "Cron", "Gateway", "Env"]

    def compose(self) -> ComposeResult:
        yield Static(id="tab-content")

    def update_tabs(self, active: str):
        text = Text()
        for index, name in enumerate(self.TABS, 1):
            key = str(index)
            slug = name.lower()
            if slug == active:
                text.append(f" {key} {name} ", style="bold #05070d on #67e8f9")
            else:
                text.append(f" {key} {name} ", style="#64748b")
            text.append(" ")
        text.append("   / search   r refresh   p pause   d demo", style="#475569")
        self.query_one("#tab-content", Static).update(text)


class CommandDeck(Static):
    """Single viewport that renders the selected Observatory tab."""

    def compose(self) -> ComposeResult:
        yield Static(id="deck-content")

    def update_view(self, active: str, data: Dict):
        renderer = {
            "overview": self._overview,
            "sessions": self._sessions,
            "skills": self._skills,
            "config": self._config,
            "cron": self._cron,
            "gateway": self._gateway,
            "logs": self._gateway,
            "env": self._env,
        }.get(active, self._overview)
        self.query_one("#deck-content", Static).update(renderer(data))

    def _metric_panel(self, label: str, value: str, detail: str, color: str) -> Panel:
        body = Text()
        body.append(f"{value}\n", style=f"bold {color}")
        body.append(detail, style="#64748b")
        return Panel(body, title=label.upper(), border_style=color, padding=(1, 2))

    def _overview(self, data: Dict):
        gw = data["gateway"]
        reliability = data["reliability"]
        profiles = data["profiles"]
        crons = data["crons"]
        token_spark, tool_spark = data["sparks"]
        score = reliability.get("avg")
        score_value = f"{score:.1f}" if score is not None else "N/A"
        latest_tokens = f"{token_spark[-1]:,}" if token_spark else "0"
        latest_tools = f"{tool_spark[-1]:,}" if tool_spark else "0"

        metrics = Columns(
            [
                self._metric_panel(
                    "Gateway",
                    "ONLINE" if gw["running"] else "OFFLINE",
                    f"pid {gw.get('pid') or '-'}",
                    C["success"] if gw["running"] else C["error"],
                ),
                self._metric_panel("Reliability", f"{score_value}/100", f"{reliability.get('count', 0)} scored sessions", "#fb7185"),
                self._metric_panel("Token Burn", latest_tokens, "latest session window", "#2dd4bf"),
                self._metric_panel("Tool Calls", latest_tools, "latest session window", "#f59e0b"),
            ],
            equal=True,
            expand=True,
        )

        streams = self._profiles_table(profiles[:6])
        active_profiles = self._profiles_table(
            data.get("active_profiles", [])[:6],
            empty_label="no active profiles",
            detail_column=True,
        )
        issues = self._issues_table(data["reliability"].get("issues", [])[:5])
        schedule = self._cron_table(crons[:5])
        next_moves = self._next_moves_table(data)
        return Group(
            metrics,
            Columns(
                [
                    Panel(active_profiles, title="ACTIVE PROFILES", border_style="#4ade80", padding=(1, 1)),
                    Panel(issues, title="ANOMALY RADAR", border_style="#fb7185", padding=(1, 1)),
                    Panel(schedule, title="AUTOMATIONS", border_style="#f59e0b", padding=(1, 1)),
                ],
                equal=True,
                expand=True,
            ),
            Panel(streams, title="PROFILE ACTIVITY", border_style="#2dd4bf", padding=(1, 1)),
            Panel(next_moves, title="NEXT MOVES", border_style="#a78bfa", padding=(1, 1)),
            Panel(
                Group(
                    self._spark_text("TOKENS", token_spark, "#2dd4bf"),
                    self._spark_text("TOOLS ", tool_spark, "#fb7185"),
                ),
                title="SIGNAL TRACE",
                border_style="#475569",
                padding=(1, 2),
            ),
        )

    def _profiles_table(
        self,
        profiles: List[Dict],
        empty_label: str = "no streams",
        detail_column: bool = False,
    ) -> Table:
        table = Table(show_header=True, header_style="bold #67e8f9", box=None, expand=True, padding=(0, 1))
        table.add_column("profile", no_wrap=True)
        table.add_column("state", no_wrap=True)
        table.add_column("seen", justify="right", no_wrap=True)
        if detail_column:
            table.add_column("platforms", no_wrap=True)
        else:
            table.add_column("tools", justify="right", no_wrap=True)
            table.add_column("tokens", justify="right", no_wrap=True)
        for p in profiles:
            color = C["success"] if p["status"] == "online" else C["warning"]
            cells = [p["id"][:24], f"[{color}]{p['status'].upper()}[/{color}]", p["last_seen"]]
            if detail_column:
                cells.append(p.get("detail") or "-")
            else:
                cells.extend([f"{p['tools']:,}", f"{p['tokens']:,}"])
            table.add_row(*cells)
        if not profiles:
            table.add_row(empty_label, "-", "-", "-" if detail_column else "-", *([] if detail_column else ["-"]))
        return table

    def _issues_table(self, issues: List[Dict]) -> Table:
        table = Table(show_header=True, header_style="bold #fb7185", box=None, expand=True, padding=(0, 1))
        table.add_column("session", no_wrap=True)
        table.add_column("score", justify="right", no_wrap=True)
        table.add_column("signal")
        for issue in issues:
            color = C["error"] if issue["score"] < 40 else C["warning"]
            signal = (issue.get("issues") or "")[:64]
            table.add_row(issue["id"], f"[{color}]{issue['score']:.1f}[/{color}]", signal)
        if not issues:
            table.add_row("none", "-", "healthy")
        return table

    def _cron_table(self, crons: List[Dict]) -> Table:
        table = Table(show_header=True, header_style="bold #fbbf24", box=None, expand=True, padding=(0, 1))
        table.add_column("job", no_wrap=True)
        table.add_column("next", justify="right", no_wrap=True)
        for job in crons:
            color = C["success"] if job["status"] == "ok" else C["error"]
            table.add_row(job["name"][:24], f"[{color}]{job['next']}[/{color}]")
        if not crons:
            table.add_row("no jobs", "-")
        return table

    def _next_moves_table(self, data: Dict) -> Table:
        table = Table(show_header=True, header_style="bold #c084fc", box=None, expand=True, padding=(0, 1))
        table.add_column("priority", no_wrap=True)
        table.add_column("next move")
        table.add_column("why")

        moves: List[tuple[str, str, str, str]] = []
        gw = data["gateway"]
        reliability = data["reliability"]
        active_profiles = data.get("active_profiles", [])
        crons = data["crons"]
        token_spark, tool_spark = data["sparks"]
        config_rows = data.get("config", [])
        sessions = data.get("sessions", [])

        if not gw.get("running"):
            moves.append(("P0", C["error"], "Restart the gateway", "Messaging and cron entrypoints are offline."))

        if not active_profiles:
            moves.append(("P1", C["warning"], "Reconnect a profile", "No live gateway profile is currently connected."))
        elif len(active_profiles) >= 2:
            names = ", ".join(p["id"] for p in active_profiles[:3])
            moves.append(("P2", C["success"], "Demo profile switching", f"{names} are online."))

        low_scores = [issue for issue in reliability.get("issues", []) if issue.get("score", 100) < 50]
        if low_scores:
            moves.append(("P1", C["warning"], "Inspect the lowest-score session", f"{low_scores[0]['id']} is at {low_scores[0]['score']:.1f}/100."))

        failed_crons = [job for job in crons if job.get("status") != "ok"]
        if failed_crons:
            moves.append(("P1", C["warning"], "Fix failing automation", f"{failed_crons[0]['name']} reported a failed run."))

        if token_spark and len(token_spark) > 3:
            recent_avg = sum(token_spark[:-1]) / max(1, len(token_spark) - 1)
            if token_spark[-1] > 100_000 or token_spark[-1] > recent_avg * 2:
                moves.append(("P2", "#2dd4bf", "Review token burn", f"Latest session used {token_spark[-1]:,} tokens."))

        if tool_spark and tool_spark[-1] > 50:
            moves.append(("P2", "#f59e0b", "Audit tool-call loop risk", f"Latest session made {tool_spark[-1]:,} tool calls."))

        if not config_rows:
            moves.append(("P2", "#38bdf8", "Open Config", "No config rows are visible to Observatory."))

        if sessions:
            latest = sessions[0]
            title = latest.get("title") or latest.get("source") or latest.get("id")
            moves.append(("IDEA", "#67e8f9", "Turn latest session into a showcase", str(title)[:70]))

        moves.extend([
            ("IDEA", "#a78bfa", "Add a judge-mode demo script", "One guided flow: gateway, profiles, sessions, reliability."),
            ("IDEA", "#a78bfa", "Export an Observatory report", "Generate a one-page markdown or HTML health brief."),
            ("IDEA", "#a78bfa", "Add profile routing controls", "Start, stop, or restart a profile directly from the TUI."),
        ])

        for priority, color, move, reason in moves[:6]:
            table.add_row(f"[{color}]{priority}[/{color}]", move, reason)

        return table

    def _sessions(self, data: Dict):
        table = Table(show_header=True, header_style="bold #67e8f9", box=None, expand=True, padding=(0, 1))
        for name, kwargs in [
            ("id", {"no_wrap": True}),
            ("model", {"no_wrap": True}),
            ("src", {"no_wrap": True}),
            ("tok", {"justify": "right", "no_wrap": True}),
            ("tools", {"justify": "right", "no_wrap": True}),
            ("cost", {"justify": "right", "no_wrap": True}),
            ("state", {"no_wrap": True}),
            ("seen", {"justify": "right", "no_wrap": True}),
            ("title", {}),
        ]:
            table.add_column(name, **kwargs)
        for row in data["sessions"]:
            cost = "-" if row["cost"] in (None, 0) else f"${row['cost']:.4f}"
            table.add_row(row["id"], row["model"][:26], row["source"], f"{row['tokens']:,}", f"{row['tools']:,}", cost, row["status"][:8], row["seen"], row["title"][:42])
        return Panel(table, title=f"SESSIONS  total shown {len(data['sessions'])}", border_style="#67e8f9", padding=(1, 1))

    def _skills(self, data: Dict):
        table = Table(show_header=True, header_style="bold #c084fc", box=None, expand=True, padding=(0, 1))
        table.add_column("status", no_wrap=True)
        table.add_column("name", no_wrap=True)
        table.add_column("source", no_wrap=True)
        table.add_column("description")
        for row in data["skills"]:
            table.add_row(f"[{C['success']}]{row['status']}[/{C['success']}]", row["name"][:32], row["source"], row["description"])
        return Panel(table, title=f"SKILL INVENTORY  {len(data['skills'])} loaded", border_style="#a78bfa", padding=(1, 1))

    def _cron(self, data: Dict):
        return Panel(self._cron_table(data["crons"]), title=f"AUTOMATION QUEUE  {len(data['crons'])} jobs", border_style="#f59e0b", padding=(1, 1))

    def _config(self, data: Dict):
        table = Table(show_header=True, header_style="bold #38bdf8", box=None, expand=True, padding=(0, 1))
        table.add_column("section", no_wrap=True)
        table.add_column("key", no_wrap=True)
        table.add_column("value", no_wrap=True)
        table.add_column("type", no_wrap=True)
        for row in data["config"]:
            type_color = "#94a3b8" if row["type"] == "section" else "#67e8f9"
            table.add_row(
                row["section"][:24],
                row["key"][:42],
                str(row["value"])[:44],
                f"[{type_color}]{row['type']}[/{type_color}]",
            )
        if not data["config"]:
            table.add_row("root", "config.yaml", "not found", "-")
        return Panel(table, title=f"CONFIG  {HERMES_HOME / 'config.yaml'}", border_style="#38bdf8", padding=(1, 1))

    def _gateway(self, data: Dict):
        gw = data["gateway"]
        status = Table(show_header=False, box=None, expand=True, padding=(0, 2))
        status.add_column("metric", no_wrap=True, style="#64748b")
        status.add_column("value", no_wrap=True)
        status.add_row(
            "state",
            f"[{C['success']}]ONLINE[/{C['success']}]" if gw["running"] else f"[{C['error']}]OFFLINE[/{C['error']}]",
        )
        status.add_row("pid", str(gw.get("pid") or "-"))
        status.add_row("log", str(GATEWAY_LOG))
        status.add_row("last signal", gw.get("last_error") or "no warnings in recent window")

        events = Table(show_header=True, header_style="bold #60a5fa", box=None, expand=True, padding=(0, 1))
        events.add_column("kind", no_wrap=True)
        events.add_column("event")
        for kind, line in self._gateway_events(data["logs"]):
            color = {"ERROR": C["error"], "WARN": C["warning"], "START": "#67e8f9", "INFO": "#94a3b8"}.get(kind, "#94a3b8")
            events.add_row(f"[{color}]{kind}[/{color}]", line)
        if events.row_count == 0:
            events.add_row(f"[{C['success']}]OK[/{C['success']}]", "gateway log is quiet")

        return Group(
            Columns(
                [
                    Panel(status, title="GATEWAY HEALTH", border_style="#4ade80" if gw["running"] else "#f87171", padding=(1, 1)),
                    Panel(
                        self._profiles_table(
                            data.get("active_profiles", []) or data["profiles"][:8],
                            detail_column=bool(data.get("active_profiles")),
                        ),
                        title="CONNECTED PROFILES",
                        border_style="#2dd4bf",
                        padding=(1, 1),
                    ),
                ],
                equal=True,
                expand=True,
            ),
            Panel(events, title="RECENT GATEWAY EVENTS", border_style="#60a5fa", padding=(1, 1)),
        )

    def _gateway_events(self, raw_lines: List[str]) -> List[tuple[str, str]]:
        events: List[tuple[str, str]] = []
        seen = set()
        skip_fragments = (
            "Press Ctrl+C to stop",
            "Messaging platforms + cron scheduler",
            "─",
            "━",
            "│",
            "┊",
        )
        for raw in raw_lines[-120:]:
            line = raw.strip()
            if not line or any(fragment in line for fragment in skip_fragments):
                continue
            line = line.replace("⚠️", "").replace("✅", "").strip()
            if "No response from provider" in line or "WARNING" in line or "Reconnecting" in line:
                kind = "WARN"
            elif "ERROR" in line or "Traceback" in line:
                kind = "ERROR"
            elif "Gateway Starting" in line or "gateway run" in line:
                kind = "START"
            elif "created" in line or "started" in line or "listening" in line:
                kind = "INFO"
            else:
                continue
            line = line[-150:]
            key = (kind, line)
            if key in seen:
                continue
            seen.add(key)
            events.append((kind, line))
        return events[-18:]

    def _env(self, data: Dict):
        table = Table(show_header=True, header_style="bold #34d399", box=None, expand=True, padding=(0, 1))
        table.add_column("key", no_wrap=True)
        table.add_column("value", no_wrap=True)
        table.add_column("description")
        for row in data["env"]:
            table.add_row(row["name"][:36], f"[{C['success']}]{row['value']}[/{C['success']}]" if row["value"] == "SET" else "-", row["description"])
        return Panel(table, title="ENVIRONMENT  redacted", border_style="#34d399", padding=(1, 1))

    def _spark_text(self, label: str, values: List[int], color: str) -> Text:
        blocks = "▁▂▃▄▅▆▇█"
        text = Text(f"{label:<7} ", style="#94a3b8")
        if not values:
            text.append("no data", style="#475569")
            return text
        mn, mx = min(values), max(values)
        if mn == mx:
            bars = "▁" * min(len(values), 44)
        else:
            bars = "".join(blocks[int(((v - mn) / (mx - mn)) * (len(blocks) - 1))] for v in values[-44:])
        text.append(bars, style=color)
        text.append(f"  {values[-1]:,}", style="#64748b")
        return text


# ---------------------------------------------------------------------------
# Main App
# ---------------------------------------------------------------------------

class ObservatoryApp(App):
    """Hermes Observatory — polished real-time monitoring dashboard."""

    CSS = """
    Screen {
        background: #05070d;
        color: #e2e8f0;
    }

    StatusBanner {
        height: 3;
        border: none;
        margin: 0;
        padding: 1 2 0 2;
        background: #05070d;
    }
    #banner-layout {
        width: 100%;
        text-align: left;
    }

    TabBar {
        height: 3;
        padding: 0 2;
        background: #05070d;
    }

    CommandDeck {
        height: 1fr;
        padding: 0 1 1 1;
        background: #05070d;
    }

    StatusBanner, TabBar, CommandDeck {
        background: #0b1220;
        border: tall #26344d;
        border-title-color: #cbd5e1;
        border-title-background: #0b1220;
        border-subtitle-background: #0b1220;
        padding: 1;
    }

    StatusBanner:hover, TabBar:hover, CommandDeck:hover {
        background: #111827;
    }

    Footer {
        background: #05070d;
        color: #94a3b8;
        text-style: italic;
        dock: bottom;
        height: 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("c", "clear_errors", "Clear Errors"),
        Binding("s", "toggle_sound", "Sound"),
        Binding("d", "demo_mode", "Demo"),
        Binding("p", "pause", "Pause"),
        Binding("1", "tab_overview", "Overview"),
        Binding("2", "tab_sessions", "Sessions"),
        Binding("3", "tab_skills", "Skills"),
        Binding("4", "tab_config", "Config"),
        Binding("5", "tab_cron", "Cron"),
        Binding("6", "tab_gateway", "Gateway"),
        Binding("7", "tab_env", "Env"),
    ]

    TITLE = "Hermes Observatory"
    SUB_TITLE = "Real-time Agent Monitoring"

    def __init__(self):
        super().__init__()
        self._last_log_size = 0
        self._was_gateway_up = False
        self._known_critical: set = set()
        self._sound_enabled = True
        self._paused = False
        self._demo_index = 0
        self._demo_mode = False
        self._active_tab = "overview"
        self._last_refresh_label = ""

    def compose(self) -> ComposeResult:
        """Build UI with staggered startup animations."""
        yield Header()
        yield StatusBanner(id="status-banner").add_class("panel")
        yield TabBar(id="tab-bar").add_class("panel")
        yield CommandDeck(id="command-deck").add_class("panel")
        yield Footer()

    def on_mount(self) -> None:
        # Interval refresh
        self.set_interval(2.0, self.refresh_data)

        # Staggered fade-in for panels
        panels = [
            self.query_one("#status-banner", StatusBanner),
            self.query_one("#tab-bar", TabBar),
            self.query_one("#command-deck", CommandDeck),
        ]
        for i, panel in enumerate(panels):
            panel.styles.opacity = 0.0
            self.set_timer((i + 1) * 0.12, partial(
                panel.styles.animate, "opacity", 1.0, duration=0.7,
                easing="in_out_cubic"
            ))

        self.refresh_data()

    def refresh_data(self, force: bool = False) -> None:
        """Fetch all live data and update each widget."""
        if self._paused and not force:
            return

        if self._demo_mode:
            data = self._demo_data()
        else:
            gw = get_gateway_status()
            reliability = get_reliability_scores()
            profiles = get_profile_statuses()
            active_profiles = get_active_gateway_profiles()
            crons = get_cron_statuses()
            log_lines = tail_gateway_log(50)
            token_spark, tool_spark = get_metrics_sparklines()
            data = {
                "gateway": gw,
                "reliability": reliability,
                "profiles": profiles,
                "active_profiles": active_profiles,
                "crons": crons,
                "logs": log_lines,
                "sparks": (token_spark, tool_spark),
                "sessions": get_recent_sessions(),
                "skills": get_skill_rows(),
                "config": get_config_rows(),
                "env": get_env_rows(),
            }
        gw = data["gateway"]
        reliability = data["reliability"]
        profiles = data["profiles"]
        crons = data["crons"]
        self._last_refresh_label = datetime.now().strftime("%H:%M:%S")

        # ── Sound events ─────────────────────────────────────────────
        if self._sound_enabled:
            if gw["running"] and not self._was_gateway_up:
                play_sound("gateway_up")
            if not gw["running"] and self._was_gateway_up:
                play_sound("gateway_down")
            play_sound("refresh")
        self._was_gateway_up = gw["running"]

        # ── Critical alerts → sound ─────────────────────────────────
        if self._sound_enabled:
            for alert in reliability.get("issues", []):
                aid = f"{alert['id']}_{alert['score']}"
                if alert["score"] < 40 and aid not in self._known_critical:
                    play_sound("alert")
                    self._known_critical.add(aid)

        # ── Update widgets ───────────────────────────────────────────
        self.query_one("#status-banner", StatusBanner).update_data(
            gw,
            reliability,
            profiles,
            len(crons),
            refreshed_at=self._last_refresh_label,
            paused=self._paused,
            demo=self._demo_mode,
        )

        self.query_one("#tab-bar", TabBar).update_tabs(self._active_tab)
        self.query_one("#command-deck", CommandDeck).update_view(self._active_tab, data)

    def action_refresh(self) -> None:
        self.refresh_data(force=True)
        self.notify(f"Refreshed at {self._last_refresh_label}", severity="information")

    def action_clear_errors(self) -> None:
        self.notify("Log view refreshed", severity="info")

    def action_toggle_sound(self) -> None:
        self._sound_enabled = not self._sound_enabled
        self.notify(f"Sound: {'ON' if self._sound_enabled else 'OFF'}")

    def action_pause(self) -> None:
        self._paused = not self._paused
        self.notify(f"Auto-refresh: {'PAUSED' if self._paused else 'RESUMED'}")
        self.refresh_data(force=True)

    def _switch_tab(self, tab: str) -> None:
        self._active_tab = tab
        self.refresh_data(force=True)

    def action_tab_overview(self) -> None:
        self._switch_tab("overview")

    def action_tab_sessions(self) -> None:
        self._switch_tab("sessions")

    def action_tab_skills(self) -> None:
        self._switch_tab("skills")

    def action_tab_config(self) -> None:
        self._switch_tab("config")

    def action_tab_cron(self) -> None:
        self._switch_tab("cron")

    def action_tab_gateway(self) -> None:
        self._switch_tab("gateway")

    def action_tab_logs(self) -> None:
        self._switch_tab("gateway")

    def action_tab_env(self) -> None:
        self._switch_tab("env")

    def action_demo_mode(self) -> None:
        self._demo_mode = not self._demo_mode
        self.notify(f"Demo mode: {'ON' if self._demo_mode else 'OFF'}")
        self.refresh_data(force=True)

    def _demo_data(self) -> Dict:
        token_spark = [8200, 9400, 12100, 11800, 16300, 15200, 20100, 18700, 24000, 22100, 28600, 31200]
        tool_spark = [5, 8, 11, 7, 15, 13, 21, 18, 26, 24, 31, 29]
        profiles = [
            {"id": "telegram/founder", "status": "online", "last_seen": "12s", "tools": 84, "tokens": 412000},
            {"id": "cli/demo-stage", "status": "online", "last_seen": "41s", "tools": 117, "tokens": 686000},
            {"id": "cron/nightly-qa", "status": "idle", "last_seen": "18m", "tools": 52, "tokens": 198400},
            {"id": "slack/ops-room", "status": "idle", "last_seen": "42m", "tools": 33, "tokens": 143900},
        ]
        active_profiles = [
            {"id": "default", "status": "online", "last_seen": "12s", "tools": 1, "tokens": 0, "detail": "telegram"},
            {"id": "sales-assistant", "status": "online", "last_seen": "41s", "tools": 1, "tokens": 0, "detail": "telegram"},
        ]
        crons = [
            {"name": "release canary", "next": "19:20", "schedule": "*/20 * * * *", "status": "ok"},
            {"name": "lead scan", "next": "20:00", "schedule": "0 * * * *", "status": "ok"},
            {"name": "reliability grade", "next": "23:30", "schedule": "30 23 * * *", "status": "ok"},
        ]
        issues = [
            {"id": "sess-a17f9c", "score": 92.4, "issues": "Fast recovery after provider timeout; answer grounded in tool output."},
            {"id": "sess-b42e11", "score": 74.8, "issues": "Minor retry burst detected; no user-visible failure."},
            {"id": "sess-c90d02", "score": 38.2, "issues": "Context window pressure and repeated provider fallback."},
        ]
        sessions = [
            {"id": "a17f9c0aa", "model": "gpt-5.4", "source": "telegram", "tokens": 41288, "tools": 18, "cost": 0.0912, "status": "DONE", "seen": "12s", "title": "Investor diligence packet"},
            {"id": "b42e11df", "model": "stepfun/step-3.5", "source": "cli", "tokens": 68320, "tools": 31, "cost": 0.0441, "status": "DONE", "seen": "4m", "title": "Observatory demo polish"},
            {"id": "c90d02ef", "model": "minimax/m2.5", "source": "cron", "tokens": 21890, "tools": 9, "cost": 0.0127, "status": "WARN", "seen": "18m", "title": "Nightly QA sweep"},
        ]
        skills = [
            {"name": "qa", "status": "READY", "source": "skills", "description": "Systematically QA test a web application and report issues."},
            {"name": "design-review", "status": "READY", "source": "skills", "description": "Designer-eye QA for visual hierarchy, spacing, and polish."},
            {"name": "ship", "status": "READY", "source": "skills", "description": "Run tests, prepare a landing diff, and ship safely."},
        ]
        config = [
            {"section": "model", "key": "model.default", "value": "gpt-5.4", "type": "str"},
            {"section": "gateway", "key": "gateway.enabled_platforms", "value": "telegram, slack", "type": "list"},
            {"section": "plugins", "key": "plugins.enabled", "value": "hermes-observatory", "type": "list"},
            {"section": "tools", "key": "tools.code_execution", "value": "ON", "type": "bool"},
            {"section": "terminal", "key": "terminal.env_type", "value": "local", "type": "str"},
        ]
        env = [
            {"name": "HERMES_HOME", "value": "SET", "description": "active profile home"},
            {"name": "OPENAI_API_KEY", "value": "SET", "description": "redacted provider key"},
            {"name": "TELEGRAM_BOT_TOKEN", "value": "SET", "description": "redacted gateway token"},
        ]
        logs = [
            "INFO gateway.run: demo stream connected platform=telegram",
            "INFO tools.dispatch: qa sweep completed in 8.2s",
            "WARNING provider.fallback: primary model slow, routed to backup",
            "INFO reliability.score: composite=92.4 session=a17f9c0aa",
        ]
        return {
            "gateway": {"running": True, "pid": 42424, "last_error": None},
            "reliability": {"avg": 88.7, "count": 496, "issues": issues},
            "profiles": profiles,
            "active_profiles": active_profiles,
            "crons": crons,
            "logs": logs,
            "sparks": (token_spark, tool_spark),
            "sessions": sessions,
            "skills": skills,
            "config": config,
            "env": env,
        }

    def on_key(self, event: events.Key) -> None:
        """Log keypresses for debugging (remove in prod)."""
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app = ObservatoryApp()
    app.run()


if __name__ == "__main__":
    main()
