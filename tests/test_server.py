"""Tests for the FastMCP server layer.

We use a tiny ``FakePool`` instead of the real transport so the test suite
stays hermetic. The goal is to assert that the server:
- registers exactly the expected tools,
- forwards parameters correctly (including stripping defaults / Nones),
- validates host names against the config,
- parses time phrases into ISO 8601 before forwarding,
- maps AgentRpcError to ValueError and TransportError to RuntimeError.

We don't spin up FastMCP's stdio loop here; we exercise the registered
tool callables directly via FastMCP's introspection helpers.
"""

from __future__ import annotations

from typing import Any

import pytest

from zsnoop_mcp.config import Config, HostConfig
from zsnoop_mcp.server import create_server
from zsnoop_mcp.transport import AgentRpcError, TransportError


class FakePool:
    """Stand-in for :class:`ConnectionPool` that records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.next_result: dict[str, Any] = {"ok": True}
        self.raise_: BaseException | None = None

    async def call(
        self,
        host: str,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append((host, method, params))
        if self.raise_:
            raise self.raise_
        return self.next_result


@pytest.fixture
def cfg() -> Config:
    return Config(
        hosts={
            "r2d2": HostConfig(name="r2d2", ssh_target="r2d2.lan"),
            "c3po": HostConfig(name="c3po", ssh_target="c3po.lan", sudo=True),
        },
    )


@pytest.fixture
def fake_pool() -> FakePool:
    return FakePool()


async def _tool_call(server: Any, name: str, **kwargs: Any) -> Any:
    """Invoke a registered FastMCP tool by name, returning its raw dict."""
    tool = server._tool_manager.get_tool(name)
    if tool is None:
        raise LookupError(f"tool not registered: {name}")
    return await tool.fn(**kwargs)


def _registered_tool_names(server: Any) -> set[str]:
    return set(server._tool_manager._tools.keys())


# ---- tool registration ------------------------------------------------------


async def test_server_registers_expected_tools(cfg: Config, fake_pool: FakePool) -> None:
    server = create_server(fake_pool, cfg)  # type: ignore[arg-type]
    names = _registered_tool_names(server)
    assert names == {
        "list_hosts",
        "agent_info",
        "list_datasets",
        "list_snapshots",
        "diff_snapshots",
        "list_dir",
        "read_file",
        "find_files",
        "content_grep",
        "file_history",
        "snapshots_containing",
        "first_appearance",
        "size_delta",
    }


# ---- list_hosts -------------------------------------------------------------


async def test_list_hosts_returns_configured_hosts(cfg: Config, fake_pool: FakePool) -> None:
    server = create_server(fake_pool, cfg)  # type: ignore[arg-type]
    result = await _tool_call(server, "list_hosts")
    names = {h["name"] for h in result["hosts"]}
    assert names == {"r2d2", "c3po"}
    c3po = next(h for h in result["hosts"] if h["name"] == "c3po")
    assert c3po["sudo"] is True
    assert fake_pool.calls == []  # list_hosts never calls the pool


# ---- straightforward forwarding --------------------------------------------


async def test_list_datasets_forwards_call(cfg: Config, fake_pool: FakePool) -> None:
    server = create_server(fake_pool, cfg)  # type: ignore[arg-type]
    await _tool_call(server, "list_datasets", host="r2d2")
    assert fake_pool.calls == [("r2d2", "list_datasets", None)]


async def test_list_snapshots_omits_dataset_when_none(cfg: Config, fake_pool: FakePool) -> None:
    server = create_server(fake_pool, cfg)  # type: ignore[arg-type]
    await _tool_call(server, "list_snapshots", host="r2d2")
    assert fake_pool.calls == [("r2d2", "list_snapshots", None)]


async def test_list_snapshots_includes_dataset_when_given(cfg: Config, fake_pool: FakePool) -> None:
    server = create_server(fake_pool, cfg)  # type: ignore[arg-type]
    await _tool_call(server, "list_snapshots", host="r2d2", dataset="rpool/home")
    assert fake_pool.calls == [("r2d2", "list_snapshots", {"dataset": "rpool/home"})]


async def test_read_file_omits_max_bytes_when_none(cfg: Config, fake_pool: FakePool) -> None:
    server = create_server(fake_pool, cfg)  # type: ignore[arg-type]
    await _tool_call(server, "read_file", host="r2d2", snapshot="rpool@a", path="foo")
    assert fake_pool.calls == [
        ("r2d2", "read_file", {"snapshot": "rpool@a", "path": "foo"}),
    ]


async def test_read_file_passes_max_bytes(cfg: Config, fake_pool: FakePool) -> None:
    server = create_server(fake_pool, cfg)  # type: ignore[arg-type]
    await _tool_call(
        server,
        "read_file",
        host="r2d2",
        snapshot="rpool@a",
        path="foo",
        max_bytes=4096,
    )
    assert fake_pool.calls == [
        ("r2d2", "read_file", {"snapshot": "rpool@a", "path": "foo", "max_bytes": 4096}),
    ]


# ---- time-phrase translation -----------------------------------------------


async def test_snapshots_containing_translates_phrases(cfg: Config, fake_pool: FakePool) -> None:
    server = create_server(fake_pool, cfg)  # type: ignore[arg-type]
    await _tool_call(
        server,
        "snapshots_containing",
        host="r2d2",
        dataset="rpool/home",
        path="foo",
        after="yesterday",
    )
    assert len(fake_pool.calls) == 1
    _host, method, params = fake_pool.calls[0]
    assert method == "snapshots_containing"
    assert params is not None
    # The phrase 'yesterday' becomes a fully-qualified ISO 8601 string.
    assert params["after"].endswith("+00:00")
    assert params["after"].count("T") == 1
    assert params["before"] is None


async def test_snapshots_containing_rejects_bad_phrase(cfg: Config, fake_pool: FakePool) -> None:
    server = create_server(fake_pool, cfg)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="could not parse time phrase"):
        await _tool_call(
            server,
            "snapshots_containing",
            host="r2d2",
            dataset="rpool/home",
            path="foo",
            after="when the dog barked",
        )


# ---- host validation --------------------------------------------------------


async def test_unknown_host_raises_value_error(cfg: Config, fake_pool: FakePool) -> None:
    server = create_server(fake_pool, cfg)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown host"):
        await _tool_call(server, "list_datasets", host="not-configured")


# ---- error propagation ------------------------------------------------------


async def test_agent_rpc_error_becomes_value_error(cfg: Config, fake_pool: FakePool) -> None:
    fake_pool.raise_ = AgentRpcError(-32602, "bad params")
    server = create_server(fake_pool, cfg)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="agent error"):
        await _tool_call(server, "list_datasets", host="r2d2")


async def test_transport_error_becomes_runtime_error(cfg: Config, fake_pool: FakePool) -> None:
    fake_pool.raise_ = TransportError("agent unreachable")
    server = create_server(fake_pool, cfg)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="transport error"):
        await _tool_call(server, "list_datasets", host="r2d2")
