"""Verify that transport failures surface agent stderr in the error message."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from zsnoop_mcp.transport import AgentConnection, TransportError


def _crash_with_stderr(tmp_path: Path) -> Path:
    """An agent that prints a diagnostic to stderr then exits without responding."""
    script = tmp_path / "noisy_dead_agent.py"
    script.write_text(
        textwrap.dedent("""\
            import sys
            print("CRITICAL: i could not reach the database", file=sys.stderr)
            print("CRITICAL: aborting", file=sys.stderr)
            sys.exit(2)
            """),
    )
    return script


async def test_transport_error_includes_agent_stderr(tmp_path: Path) -> None:
    script = _crash_with_stderr(tmp_path)
    argv = [sys.executable, str(script)]
    async with AgentConnection("noisy", argv, max_reconnects=0) as conn:
        with pytest.raises(TransportError) as exc_info:
            await conn.call("agent_info")
    msg = str(exc_info.value)
    assert "agent stderr" in msg
    assert "i could not reach the database" in msg
    assert "aborting" in msg
