"""End-to-end transport tests: spawn the real agent locally (no SSH).

These tests exercise the JSON-RPC framing, error propagation, reconnection,
and lifecycle behaviour against the actual agent script, with ``run_zfs``
inside the spawned agent monkeypatched away by a tiny replacement script
when we need to avoid touching the host's real ZFS state.
"""

from __future__ import annotations

import asyncio
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

from zsnoop_mcp.transport import (
    MAX_LINE_BYTES,
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
    assert result["agent_version"] == "0.2.0"
    assert "list_snapshots" in result["methods"]
    assert result["limits"]["max_read_bytes"] > 0


def _sized_response_agent(tmp_path: Path, *, payload_bytes: int) -> Path:
    """Build a one-shot agent that responds with a `payload_bytes`-sized blob."""
    script = tmp_path / f"sized_{payload_bytes}_agent.py"
    script.write_text(
        textwrap.dedent(f"""\
            import json, sys
            line = sys.stdin.readline()
            req = json.loads(line)
            payload = "x" * {payload_bytes}
            sys.stdout.write(json.dumps({{
                "jsonrpc": "2.0",
                "id": req["id"],
                "result": {{"payload": payload}},
            }}) + "\\n")
            sys.stdout.flush()
        """),
    )
    return script


async def test_large_response_exceeds_asyncio_default_line_buffer(tmp_path: Path) -> None:
    """A single JSON-RPC response larger than asyncio's default 64 KiB line
    buffer must round-trip. Regresses GH #8.

    Uses a fabricated agent that synthesises a large payload, so the test
    doesn't depend on the real ZFS dataset shape.
    """
    # 1 MiB — well above asyncio's 64 KiB default, comfortably below
    # transport's MAX_LINE_BYTES.
    script = _sized_response_agent(tmp_path, payload_bytes=1024 * 1024)
    async with AgentConnection("local", [sys.executable, str(script)]) as conn:
        result = await conn.call("agent_info")
    assert len(result["payload"]) == 1024 * 1024


async def test_response_larger_than_transport_limit_fails_cleanly(tmp_path: Path) -> None:
    """A response exceeding ``MAX_LINE_BYTES`` must raise ``TransportError``,
    not a raw asyncio ``ValueError``. Regresses the over-budget failure mode
    surfaced in GH #8.
    """
    script = _sized_response_agent(tmp_path, payload_bytes=MAX_LINE_BYTES + 1024)
    async with AgentConnection(
        "too-large", [sys.executable, str(script)], max_reconnects=0
    ) as conn:
        with pytest.raises(TransportError, match="line larger than"):
            await conn.call("agent_info")


async def test_protocol_corruption_tears_down_so_next_call_respawns(tmp_path: Path) -> None:
    """After a protocol-level corruption (oversize, garbage, id mismatch),
    the connection must drop the subprocess so the next call respawns clean.

    Without this, subsequent calls inherit a desynced pipe and surface as
    ``id mismatch`` errors on unrelated requests. This pins the recovery
    behaviour added to `_recv` / `_call_once` in PR #11.

    The fabricated agent uses a marker file to behave differently across
    invocations: the *first* subprocess sends garbage; subsequent
    subprocesses (proving the respawn happened) respond cleanly.
    """
    marker = tmp_path / "already_ran.flag"
    script = tmp_path / "garbage_then_ok.py"
    script.write_text(
        textwrap.dedent(f"""\
            import json, os, sys
            line = sys.stdin.readline()
            req = json.loads(line)
            marker = {str(marker)!r}
            if not os.path.exists(marker):
                open(marker, "w").close()
                sys.stdout.write("not json at all\\n")
            else:
                sys.stdout.write(json.dumps({{
                    "jsonrpc": "2.0", "id": req["id"],
                    "result": {{"recovered": True}},
                }}) + "\\n")
            sys.stdout.flush()
        """),
    )
    async with AgentConnection(
        "respawns",
        [sys.executable, str(script)],
        max_reconnects=0,
    ) as conn:
        with pytest.raises(TransportError, match="non-JSON"):
            await conn.call("agent_info")
        # White-box check: the corruption handler must have dropped the
        # subprocess so the next call respawns.
        assert conn._proc is None
        # And the next call must succeed, against the fresh subprocess.
        result = await conn.call("agent_info")
    assert result == {"recovered": True}


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


def _first_run_noisy_agent(tmp_path: Path) -> Path:
    """Agent that prints a stderr marker only on its FIRST invocation
    (uses a marker file). Responds once, then exits. The second invocation
    (after respawn) prints nothing to stderr — so any marker left in the
    transport's _stderr_tail must have leaked from the dead process.
    """
    marker = tmp_path / "first_run.flag"
    script = tmp_path / "first_run_noisy.py"
    script.write_text(
        textwrap.dedent(f"""\
            import json, os, sys
            marker = {str(marker)!r}
            if not os.path.exists(marker):
                open(marker, "w").close()
                sys.stderr.write("MARKER_FROM_FIRST_PROCESS\\n")
                sys.stderr.flush()
            line = sys.stdin.readline()
            req = json.loads(line)
            sys.stdout.write(json.dumps({{
                "jsonrpc": "2.0", "id": req["id"],
                "result": {{"ok": True}},
            }}) + "\\n")
            sys.stdout.flush()
            sys.exit(0)
        """),
    )
    return script


async def _wait_for(predicate: Callable[[], bool], *, deadline_secs: float = 2.0) -> bool:
    """Poll *predicate* until true or *deadline_secs* elapses. Returns the result.

    Used in place of fixed sleeps so the test doesn't burn 200 ms on slow
    CI and doesn't risk false negatives on truly slow hosts.
    """
    deadline = asyncio.get_running_loop().time() + deadline_secs
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


async def test_respawn_resets_stderr_tail_after_natural_death(tmp_path: Path) -> None:
    """When a subprocess dies naturally (returncode set, no _close_proc),
    the respawn path must call _close_proc to reset _stderr_tail —
    otherwise stale stderr lines from the dead process bleed into
    subsequent error reports.
    """
    script = _first_run_noisy_agent(tmp_path)
    argv = [sys.executable, str(script)]
    async with AgentConnection("noisy", argv) as conn:
        await conn.call("agent_info")
        # Wait for the drainer to actually capture the stderr marker
        # before the first process exits. Polling is more robust than a
        # fixed sleep on busy CI runners.
        captured = await _wait_for(
            lambda: any("MARKER_FROM_FIRST_PROCESS" in line for line in conn._stderr_tail),
        )
        assert captured, "first-process stderr marker never reached the drainer"
        # The next call triggers _ensure_alive's "returncode is not None"
        # branch, which must call _close_proc (resetting _stderr_tail)
        # before _spawn. The second-spawned agent writes nothing to stderr
        # (marker file already exists), so any marker still present in the
        # tail leaked from the dead first process.
        await conn.call("agent_info")
        # The cleanup is synchronous within _close_proc → _stderr_tail = []
        # happens before _spawn returns, so the assertion holds without
        # waiting. We still let one event-loop turn pass in case anything
        # else was queued.
        await asyncio.sleep(0)
        assert not any("MARKER_FROM_FIRST_PROCESS" in line for line in conn._stderr_tail)


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


async def test_agent_survives_non_serialisable_handler_result(tmp_path: Path) -> None:
    """If a handler returns something json.dumps can't encode, the agent
    must NOT crash and leave the LLM hanging waiting for a response. It
    should emit an INTERNAL_ERROR response so the wire stays synchronised
    and the next call works against the same subprocess.

    Stages a tiny standalone agent that registers a method returning bytes.
    """
    script = tmp_path / "bad_method_agent.py"
    script.write_text(
        textwrap.dedent(f"""\
            import sys
            sys.path.insert(0, {str(AGENT_PATH.parent)!r})
            import zfs_snoop_agent as agent
            agent.METHODS["bad"] = lambda _p: {{"x": b"not json-encodable"}}
            sys.exit(agent.main())
        """),
    )
    async with AgentConnection(
        "bad-handler",
        [sys.executable, str(script)],
        max_reconnects=0,
    ) as conn:
        # The bad method surfaces as an AgentRpcError (INTERNAL_ERROR), not
        # a transport failure — proving the agent caught the serialise
        # error and stayed alive.
        with pytest.raises(AgentRpcError, match="non-serialisable"):
            await conn.call("bad")
        # Subsequent calls still work against the same subprocess.
        result = await conn.call("agent_info")
        assert result["agent_version"] == "0.2.0"


async def test_recv_timeout_tears_down_subprocess(tmp_path: Path) -> None:
    """A recv timeout means the agent is in an unknown state from our
    perspective — any late response would land in the pipe and surface as
    an `id mismatch` on the next call. The transport must close the
    subprocess on timeout so the next call respawns clean.

    Uses a hanging agent that reads the request but never replies, paired
    with a sub-second recv_timeout to keep the test fast.
    """
    script = tmp_path / "hangs.py"
    script.write_text(
        textwrap.dedent("""\
            import sys, time
            sys.stdin.readline()
            # Never write a response. Sleep so the process is alive when
            # the transport times out.
            time.sleep(30)
        """),
    )
    async with AgentConnection(
        "hangs",
        [sys.executable, str(script)],
        max_reconnects=0,
        recv_timeout=0.2,
    ) as conn:
        with pytest.raises(TransportError, match="did not respond"):
            await conn.call("agent_info")
        # White-box: the timeout handler must have torn down the
        # subprocess so the next call would respawn against a fresh one.
        assert conn._proc is None


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
    assert result["agent_version"] == "0.2.0"
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
