"""End-to-end transport tests: spawn the real agent locally (no SSH).

These tests exercise the JSON-RPC framing, error propagation, reconnection,
and lifecycle behaviour against the actual agent script, with ``run_zfs``
inside the spawned agent monkeypatched away by a tiny replacement script
when we need to avoid touching the host's real ZFS state.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from zsnoop_mcp.transport import (
    AgentConnection,
    AgentRpcError,
    TransportError,
)

# Path to the real agent, computed relative to this test file.
AGENT_PATH = Path(__file__).resolve().parents[1] / "agent" / "zfs_snoop_agent.py"


def _agent_argv() -> list[str]:
    """Argv to spawn the agent using the same Python that runs the tests."""
    return [sys.executable, str(AGENT_PATH)]


# ---- agent_info round-trip ---------------------------------------------------


async def test_agent_info_round_trip() -> None:
    async with AgentConnection("local", _agent_argv()) as conn:
        result = await conn.call("agent_info")
    assert result["agent_version"] == "0.1.0"
    assert "list_snapshots" in result["methods"]
    assert result["limits"]["max_read_bytes"] > 0


async def test_large_response_exceeds_asyncio_default_line_buffer(tmp_path: Path) -> None:
    """A single JSON-RPC response larger than asyncio's default 64 KiB line
    buffer must round-trip. Regresses GH #8.

    Uses a fabricated agent that synthesises a large payload, so the test
    doesn't depend on the real ZFS dataset shape.
    """
    fake_agent = tmp_path / "fat_agent.py"
    fake_agent.write_text(
        textwrap.dedent("""
        import json, sys
        # 256 KiB of payload — well above asyncio's 64 KiB default, well
        # below transport's MAX_LINE_BYTES.
        big = "x" * (256 * 1024)
        for line in sys.stdin:
            try:
                req = json.loads(line)
            except Exception:
                continue
            resp = {"jsonrpc": "2.0", "id": req.get("id"), "result": {"payload": big}}
            sys.stdout.write(json.dumps(resp) + "\\n")
            sys.stdout.flush()
    """)
    )
    async with AgentConnection("local", [sys.executable, str(fake_agent)]) as conn:
        result = await conn.call("agent_info")
    assert len(result["payload"]) == 256 * 1024


async def test_multiple_sequential_calls_share_one_subprocess() -> None:
    async with AgentConnection("local", _agent_argv()) as conn:
        first = await conn.call("agent_info")
        second = await conn.call("agent_info")
    assert first == second


async def test_unknown_method_raises_agent_rpc_error() -> None:
    async with AgentConnection("local", _agent_argv()) as conn:
        with pytest.raises(AgentRpcError) as exc_info:
            await conn.call("totally_made_up_method")
    assert exc_info.value.code == -32601  # METHOD_NOT_FOUND


async def test_invalid_params_raise_agent_rpc_error() -> None:
    async with AgentConnection("local", _agent_argv()) as conn:
        with pytest.raises(AgentRpcError) as exc_info:
            # diff_snapshots requires snap_a and snap_b
            await conn.call("diff_snapshots", {})
    assert exc_info.value.code == -32602  # INVALID_PARAMS


# ---- reconnect after subprocess death ---------------------------------------


def _exit_after_first_call_agent(tmp_path: Path) -> Path:
    """A minimal agent that responds once then exits, to test reconnect."""
    script = tmp_path / "one_shot_agent.py"
    script.write_text(
        textwrap.dedent("""\
            import json, sys
            line = sys.stdin.readline()
            req = json.loads(line)
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": req["id"],
                "result": {"served_by": "one_shot"},
            }) + "\\n")
            sys.stdout.flush()
            sys.exit(0)
            """),
    )
    return script


async def test_reconnect_after_subprocess_dies(tmp_path: Path) -> None:
    script = _exit_after_first_call_agent(tmp_path)
    argv = [sys.executable, str(script)]
    async with AgentConnection("oneshot", argv) as conn:
        first = await conn.call("agent_info")
        assert first == {"served_by": "one_shot"}
        # The script exits after one response; the next call should respawn
        # transparently and succeed (the new instance also serves one then dies).
        second = await conn.call("agent_info")
        assert second == {"served_by": "one_shot"}


def _always_dies_agent(tmp_path: Path) -> Path:
    """Exits immediately without responding."""
    script = tmp_path / "dead_agent.py"
    script.write_text("import sys; sys.exit(1)\n")
    return script


async def test_reconnect_fails_after_exhausting_retries(tmp_path: Path) -> None:
    script = _always_dies_agent(tmp_path)
    argv = [sys.executable, str(script)]
    async with AgentConnection("dead", argv, max_reconnects=1) as conn:
        with pytest.raises(TransportError, match="unreachable"):
            await conn.call("agent_info")


# ---- garbage on stdout ------------------------------------------------------


def _garbage_agent(tmp_path: Path) -> Path:
    script = tmp_path / "garbage_agent.py"
    script.write_text(
        textwrap.dedent("""\
            import sys
            sys.stdin.readline()
            sys.stdout.write("this is not json\\n")
            sys.stdout.flush()
            """),
    )
    return script


async def test_garbage_response_raises_transport_error(tmp_path: Path) -> None:
    script = _garbage_agent(tmp_path)
    argv = [sys.executable, str(script)]
    async with AgentConnection("garbage", argv, max_reconnects=0) as conn:
        with pytest.raises(TransportError, match="non-JSON"):
            await conn.call("agent_info")


def _id_mismatch_agent(tmp_path: Path) -> Path:
    script = tmp_path / "mismatch_agent.py"
    script.write_text(
        textwrap.dedent("""\
            import json, sys
            sys.stdin.readline()
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": 999, "result": {}
            }) + "\\n")
            sys.stdout.flush()
            """),
    )
    return script


async def test_id_mismatch_raises_transport_error(tmp_path: Path) -> None:
    script = _id_mismatch_agent(tmp_path)
    argv = [sys.executable, str(script)]
    async with AgentConnection("mismatch", argv, max_reconnects=0) as conn:
        with pytest.raises(TransportError, match="id mismatch"):
            await conn.call("agent_info")


# ---- lifecycle --------------------------------------------------------------


async def test_close_terminates_subprocess() -> None:
    conn = AgentConnection("local", _agent_argv())
    await conn.call("agent_info")
    proc = conn._proc  # white-box: verify the subprocess actually exits
    assert proc is not None
    await conn.close()
    assert proc.returncode is not None


async def test_call_after_close_respawns() -> None:
    conn = AgentConnection("local", _agent_argv())
    await conn.call("agent_info")
    await conn.close()
    result = await conn.call("agent_info")
    assert result["agent_version"] == "0.1.0"
    await conn.close()


# ---- request payload sanity (no SSH, asserting wire shape) ------------------


async def test_request_includes_jsonrpc_and_id(tmp_path: Path) -> None:
    """Capture the agent's stdin to confirm we send a well-formed request."""
    script = tmp_path / "echo_agent.py"
    script.write_text(
        textwrap.dedent("""\
            import json, sys
            line = sys.stdin.readline()
            req = json.loads(line)
            # Echo a result containing what we received.
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": req["id"],
                "result": {"received": req},
            }) + "\\n")
            sys.stdout.flush()
            """),
    )
    argv = [sys.executable, str(script)]
    async with AgentConnection("echo", argv) as conn:
        result = await conn.call("list_datasets", {"foo": "bar"})
    received = result["received"]
    assert received["jsonrpc"] == "2.0"
    assert received["method"] == "list_datasets"
    assert received["params"] == {"foo": "bar"}
    assert isinstance(received["id"], int)
