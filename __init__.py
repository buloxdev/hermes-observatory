"""Hermes Observatory — real-time TUI dashboard (24hr hackathon entry).

Launch:  hermes observatory
Data sources: gateway.log, state.db, agent-reliability scores DB, our metrics DB.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Expose submodules for direct launcher (non-plugin mode)
from . import metrics, tui  # noqa: F401


# ---------------------------------------------------------------------------
# Hook handlers — record tool & session stats to metrics DB
# ---------------------------------------------------------------------------

def _on_tool_call(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    result: Any = None,
    task_id: str = "",
    session_id: str = "",
    started_at: Optional[float] = None,
    completed_at: Optional[float] = None,
    success: bool = False,
    error: Optional[str] = None,
    **_kwargs,
) -> None:
    """post_tool_call hook — delegate to metrics module."""
    from . import metrics
    metrics.record_tool_call(
        tool_name=tool_name,
        args=args,
        result=result,
        task_id=task_id,
        session_id=session_id,
        started_at=started_at,
        completed_at=completed_at,
        success=success,
        error=error,
    )


def _on_session_end(
    session_id: str = "",
    completed: bool = True,
    interrupted: bool = False,
    duration_seconds: float = 0.0,
    **_kwargs,
) -> None:
    """on_session_end hook — roll up session aggregates."""
    from . import metrics
    metrics.rollup_session(session_id=session_id, duration_seconds=duration_seconds)


# ---------------------------------------------------------------------------
# CLI command: `hermes observatory`
# ---------------------------------------------------------------------------

def _setup_observatory_subparser(subparsers):
    """Add `hermes observatory` arguments to the CLI."""
    # No arguments currently — extensible for future flags (--filter, --refresh)
    return None


def _handle_observatory(args) -> str:
    """CLI handler for `hermes observatory` — runs the TUI."""
    from . import tui
    # Run the Textual app; this blocks until the user quits (q)
    tui.main()
    return " Observatory session ended."


# ---------------------------------------------------------------------------
# Slash command: `/observatory` (gateway/chat)
# ---------------------------------------------------------------------------

def _handle_observatory_slash(raw_args: str) -> str:
    """Slash command response — instructs user how to launch the TUI."""
    return (
        "📊 To open the Observatory dashboard, run this from your terminal:\n\n"
        "    hermes observatory\n\n"
        "(Requires the hermes-observatory plugin to be enabled in config.yaml)"
    )


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def register(ctx):
    """Hermes calls this once per plugin load (both CLI and gateway modes)."""
    # Hooks — always safe to register
    ctx.register_hook("post_tool_call", _on_tool_call)
    ctx.register_hook("on_session_end", _on_session_end)

    # CLI subcommand — only takes effect when Hermes CLI loads plugins
    try:
        ctx.register_cli_command(
            name="observatory",
            help="Real-time agent health TUI dashboard",
            setup_fn=_setup_observatory_subparser,
            handler_fn=_handle_observatory,
            description="Interactive mission-control dashboard for Hermes agents",
        )
        logger.debug("Observatory: registered CLI command 'hermes observatory'")
    except Exception as e:
        # CLI commands not available in gateway-only mode — that's fine
        logger.debug(f"Observatory: CLI command registration skipped ({e})")

    # Slash command for chat contexts (Telegram, Discord, CLI chat)
    try:
        ctx.register_command(
            "observatory",
            handler=_handle_observatory_slash,
            description="Shows how to launch the Observatory TUI",
        )
        logger.debug("Observatory: registered slash command '/observatory'")
    except Exception as e:
        logger.debug(f"Observatory: slash command registration skipped ({e})")

    logger.info("Hermes Observatory plugin loaded — run 'hermes observatory'")
