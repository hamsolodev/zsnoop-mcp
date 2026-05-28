"""Tests for the FastMCP server layer.

We use a tiny ``FakePool`` instead of the real transport so the test suite
stays hermetic. The goal is to assert that the server:
- registers exactly the expected tools,
- forwards parameters correctly (including stripping defaults / Nones),
- validates host names against the config,
- parses time phrases into ISO 8601 before forwarding,
- maps AgentRpcError to ValueError and TransportError to RuntimeError,
- injects queried_at into every _call() result,
- fetch_file / fetch_dir build the correct sftp batch command.

We don't spin up FastMCP's stdio loop here; we exercise the registered
tool callables directly via FastMCP's introspection helpers.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

import zsnoop_mcp.server as srv_mod
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
        "list_pools",
        "pool_status",
        "list_datasets",
        "dataset_properties",
        "list_snapshots",
        "snapshot_cadence",
        "diff_snapshots",
        "list_dir",
        "size_breakdown",
        "top_consumers",
        "read_file",
        "find_files",
        "content_grep",
        "file_history",
        "versions_of",
        "file_diff",
        "snapshots_containing",
        "first_appearance",
        "last_appearance",
        "find_deleted",
        "bisect_change",
        "stale_snapshots",
        "size_delta",
        "checksum_file",
        "fetch_file",
        "fetch_dir",
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


async def test_list_snapshots_translates_after_phrase(cfg: Config, fake_pool: FakePool) -> None:
    server = create_server(fake_pool, cfg)  # type: ignore[arg-type]
    await _tool_call(server, "list_snapshots", host="r2d2", after="yesterday")
    assert len(fake_pool.calls) == 1
    _h, method, params = fake_pool.calls[0]
    assert method == "list_snapshots"
    assert params is not None
    assert "after" in params
    assert params["after"].endswith("+00:00")
    assert "before" not in params  # before was None — must not be forwarded
    assert "dataset" not in params


async def test_list_snapshots_forwards_max_results(cfg: Config, fake_pool: FakePool) -> None:
    server = create_server(fake_pool, cfg)  # type: ignore[arg-type]
    await _tool_call(
        server,
        "list_snapshots",
        host="r2d2",
        dataset="rpool/home",
        max_results=500,
    )
    assert fake_pool.calls == [
        ("r2d2", "list_snapshots", {"dataset": "rpool/home", "max_results": 500}),
    ]


async def test_list_snapshots_rejects_bad_time_phrase(cfg: Config, fake_pool: FakePool) -> None:
    server = create_server(fake_pool, cfg)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="could not parse time phrase"):
        await _tool_call(server, "list_snapshots", host="r2d2", after="never")


async def test_list_snapshots_treats_empty_dataset_as_unscoped(
    cfg: Config,
    fake_pool: FakePool,
) -> None:
    """Empty-string ``dataset`` is treated as "no filter" — matches the legacy
    ``{dataset: dataset} if dataset else None`` convention used by sibling
    tools like ``snapshot_cadence``. Without this, an empty string would be
    forwarded and rejected by the agent's dataset validation."""
    server = create_server(fake_pool, cfg)  # type: ignore[arg-type]
    await _tool_call(server, "list_snapshots", host="r2d2", dataset="")
    assert fake_pool.calls == [("r2d2", "list_snapshots", None)]


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


async def test_size_breakdown_omits_max_entries_when_none(
    cfg: Config,
    fake_pool: FakePool,
) -> None:
    server = create_server(fake_pool, cfg)  # type: ignore[arg-type]
    await _tool_call(server, "size_breakdown", host="r2d2", snapshot="rpool@a", path="d")
    assert fake_pool.calls == [
        ("r2d2", "size_breakdown", {"snapshot": "rpool@a", "path": "d"}),
    ]


async def test_size_breakdown_passes_max_entries(cfg: Config, fake_pool: FakePool) -> None:
    server = create_server(fake_pool, cfg)  # type: ignore[arg-type]
    await _tool_call(
        server,
        "size_breakdown",
        host="r2d2",
        snapshot="rpool@a",
        path="d",
        max_entries=500,
    )
    assert fake_pool.calls == [
        ("r2d2", "size_breakdown", {"snapshot": "rpool@a", "path": "d", "max_entries": 500}),
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


# ---- queried_at injection --------------------------------------------------


async def test_call_injects_queried_at(cfg: Config, fake_pool: FakePool) -> None:
    fake_pool.next_result = {"pools": []}
    server = create_server(fake_pool, cfg)  # type: ignore[arg-type]
    result = await _tool_call(server, "list_pools", host="r2d2")
    assert "queried_at" in result
    # Should be a valid ISO 8601 UTC timestamp.
    dt = datetime.fromisoformat(result["queried_at"])
    assert dt.tzname() in ("+00:00", "UTC")


# ---- checksum_file ----------------------------------------------------------


async def test_checksum_file_forwards_params(cfg: Config, fake_pool: FakePool) -> None:
    server = create_server(fake_pool, cfg)  # type: ignore[arg-type]
    await _tool_call(server, "checksum_file", host="r2d2", snapshot="rpool@s", path="etc/foo")
    assert fake_pool.calls == [
        ("r2d2", "checksum_file", {"snapshot": "rpool@s", "path": "etc/foo"}),
    ]


# ---- fetch_file / fetch_dir -------------------------------------------------


def _make_fetch_pool(mountpoint: str) -> FakePool:
    """A FakePool that returns a realistic dataset_properties response."""
    pool = FakePool()
    pool.next_result = {
        "dataset": "rpool/data",
        "properties": [{"name": "mountpoint", "value": mountpoint, "source": "local"}],
    }
    return pool


async def test_fetch_file_rejects_snapshot_name_with_shell_metas(
    cfg: Config,
    tmp_path: Path,
) -> None:
    """Server boundary refuses snapshot names that don't match ZFS naming.
    With the sftp batch transport there's no remote shell to inject into,
    so this is defence-in-depth + a fast, clear error on malformed input
    rather than an injection guard."""
    pool = _make_fetch_pool("/data")
    server = create_server(pool, cfg)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="invalid snapshot name"):
        await _tool_call(
            server,
            "fetch_file",
            host="r2d2",
            snapshot="rpool/data@daily-1; touch /tmp/pwned",
            path="etc/app.conf",
            local_path=str(tmp_path / "out.conf"),
        )


def test_sftp_quote_escapes_backslash_and_doublequote() -> None:
    """_sftp_quote wraps in double quotes and escapes only ``\\`` and ``"``;
    everything else (spaces, $, ;, glob chars, single quotes) is literal
    inside the quotes, which is exactly what sftp's lexer wants."""
    assert srv_mod._sftp_quote("plain.txt") == '"plain.txt"'
    assert srv_mod._sftp_quote("with space.txt") == '"with space.txt"'
    assert srv_mod._sftp_quote("a$(b);*?[c]'q") == '"a$(b);*?[c]\'q"'
    # Backslash and double-quote are the two chars that must be escaped.
    assert srv_mod._sftp_quote('a"b') == '"a\\"b"'
    assert srv_mod._sftp_quote("a\\b") == '"a\\\\b"'


async def test_fetch_file_sftp_quotes_path_with_metacharacters(
    cfg: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A snapshot file whose name contains spaces or shell/glob
    metacharacters must reach sftp as a single double-quoted batch
    argument. sftp's lexer takes it literally and never invokes a remote
    shell, so the characters can't be word-split or interpreted — this is
    both injection-safe and correct for unusual filenames. Regresses the
    v0.3.0 ``shlex.quote`` approach, which broke any path with a space
    under scp's modern SFTP backend."""
    pool = _make_fetch_pool("/data")
    server = create_server(pool, cfg)  # type: ignore[arg-type]

    dest = tmp_path / "out.conf"
    captured: list[tuple[list[str], str | None]] = []

    async def fake_run_fetch(cmd: list[str], stdin_data: str | None = None) -> None:
        captured.append((cmd, stdin_data))
        await asyncio.to_thread(dest.write_bytes, b"x")

    monkeypatch.setattr(srv_mod, "_run_fetch", fake_run_fetch)

    await _tool_call(
        server,
        "fetch_file",
        host="r2d2",
        snapshot="rpool/data@daily-1",
        # Legitimate filename containing shell + glob metacharacters.
        path="etc/$(whoami)' file;*.conf",
        local_path=str(dest),
    )

    cmd, batch = captured[0]
    assert cmd[0] == "sftp"
    assert batch is not None
    # The remote path is wrapped in double quotes with the metacharacters
    # intact (literal). No backslash-escaping needed for these chars, and
    # crucially no single-quote/shell-quote form.
    assert batch == (f'get "/data/.zfs/snapshot/daily-1/etc/$(whoami)\' file;*.conf" "{dest}"\n')


async def test_fetch_file_builds_sftp_command(
    cfg: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _make_fetch_pool("/data")
    server = create_server(pool, cfg)  # type: ignore[arg-type]

    dest = tmp_path / "recovered.conf"
    captured: list[tuple[list[str], str | None]] = []

    async def fake_run_fetch(cmd: list[str], stdin_data: str | None = None) -> None:
        captured.append((cmd, stdin_data))
        # Write via thread to satisfy the no-sync-IO-in-async-context rule.
        await asyncio.to_thread(dest.write_bytes, b"fake content")

    monkeypatch.setattr(srv_mod, "_run_fetch", fake_run_fetch)

    result = await _tool_call(
        server,
        "fetch_file",
        host="r2d2",
        snapshot="rpool/data@daily-1",
        path="etc/app.conf",
        local_path=str(dest),
    )

    assert len(captured) == 1
    cmd, batch = captured[0]
    assert cmd[0] == "sftp"
    assert "-b" in cmd
    assert cmd[cmd.index("-b") + 1] == "-"  # batch read from stdin
    assert cmd[-1] == "r2d2.lan"  # host is the final argv element
    assert batch == f'get "/data/.zfs/snapshot/daily-1/etc/app.conf" "{dest}"\n'
    assert result["local_path"] == str(dest)
    assert result["size_bytes"] == len(b"fake content")
    assert "queried_at" in result


async def test_fetch_file_rejects_existing_dest_without_overwrite(
    cfg: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _make_fetch_pool("/data")
    server = create_server(pool, cfg)  # type: ignore[arg-type]

    existing = tmp_path / "existing.conf"
    existing.write_text("already here")

    with pytest.raises(ValueError, match="already exists"):
        await _tool_call(
            server,
            "fetch_file",
            host="r2d2",
            snapshot="rpool/data@daily-1",
            path="etc/app.conf",
            local_path=str(existing),
        )


async def test_fetch_file_rejects_dotdot_path(
    cfg: Config,
    tmp_path: Path,
) -> None:
    pool = _make_fetch_pool("/data")
    server = create_server(pool, cfg)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="parent-directory"):
        await _tool_call(
            server,
            "fetch_file",
            host="r2d2",
            snapshot="rpool/data@daily-1",
            path="../etc/passwd",
            local_path=str(tmp_path / "out"),
        )


async def test_fetch_file_rejects_missing_parent(
    cfg: Config,
    tmp_path: Path,
) -> None:
    pool = _make_fetch_pool("/data")
    server = create_server(pool, cfg)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="directory does not exist"):
        await _tool_call(
            server,
            "fetch_file",
            host="r2d2",
            snapshot="rpool/data@daily-1",
            path="etc/app.conf",
            local_path=str(tmp_path / "nonexistent" / "out.conf"),
        )


async def test_fetch_file_rejects_parent_that_is_a_file(
    cfg: Config,
    tmp_path: Path,
) -> None:
    pool = _make_fetch_pool("/data")
    server = create_server(pool, cfg)  # type: ignore[arg-type]

    parent_as_file = tmp_path / "not_a_dir"
    parent_as_file.write_text("oops")

    with pytest.raises(ValueError, match="is not a directory"):
        await _tool_call(
            server,
            "fetch_file",
            host="r2d2",
            snapshot="rpool/data@daily-1",
            path="etc/app.conf",
            local_path=str(parent_as_file / "out.conf"),
        )


async def test_fetch_file_rejects_relative_local_path(
    cfg: Config,
) -> None:
    pool = _make_fetch_pool("/data")
    server = create_server(pool, cfg)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="must be absolute"):
        await _tool_call(
            server,
            "fetch_file",
            host="r2d2",
            snapshot="rpool/data@daily-1",
            path="etc/app.conf",
            local_path="relative/out.conf",
        )


async def test_fetch_file_rejects_directory_destination(
    cfg: Config,
    tmp_path: Path,
) -> None:
    pool = _make_fetch_pool("/data")
    server = create_server(pool, cfg)  # type: ignore[arg-type]

    dest_dir = tmp_path / "existing_dir"
    dest_dir.mkdir()

    # Even with overwrite=True, a directory destination is refused — sftp/cp
    # would copy *into* the directory, breaking the returned local_path.
    with pytest.raises(ValueError, match="destination is a directory"):
        await _tool_call(
            server,
            "fetch_file",
            host="r2d2",
            snapshot="rpool/data@daily-1",
            path="etc/app.conf",
            local_path=str(dest_dir),
            overwrite=True,
        )


async def test_fetch_dir_builds_sftp_recursive_command(
    cfg: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _make_fetch_pool("/data")
    server = create_server(pool, cfg)  # type: ignore[arg-type]

    captured: list[tuple[list[str], str | None]] = []

    async def fake_run_fetch(cmd: list[str], stdin_data: str | None = None) -> None:
        captured.append((cmd, stdin_data))

    monkeypatch.setattr(srv_mod, "_run_fetch", fake_run_fetch)

    dest = tmp_path / "restored_dir"
    result = await _tool_call(
        server,
        "fetch_dir",
        host="r2d2",
        snapshot="rpool/data@daily-1",
        path="home/alice",
        local_path=str(dest),
    )

    assert len(captured) == 1
    cmd, batch = captured[0]
    assert cmd[0] == "sftp"
    assert cmd[-1] == "r2d2.lan"
    # Recursive get (`get -r`) with both paths double-quoted for sftp.
    assert batch == f'get -r "/data/.zfs/snapshot/daily-1/home/alice" "{dest}"\n'
    assert result["local_path"] == str(dest)
    assert "queried_at" in result


async def test_fetch_dir_rejects_existing_destination(
    cfg: Config,
    tmp_path: Path,
) -> None:
    pool = _make_fetch_pool("/data")
    server = create_server(pool, cfg)  # type: ignore[arg-type]

    existing = tmp_path / "already_here"
    existing.mkdir()

    with pytest.raises(ValueError, match="destination already exists"):
        await _tool_call(
            server,
            "fetch_dir",
            host="r2d2",
            snapshot="rpool/data@daily-1",
            path="home/alice",
            local_path=str(existing),
        )


async def test_run_fetch_kills_subprocess_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hanging subprocess must be SIGKILLed and reaped, not leaked."""
    monkeypatch.setattr(srv_mod, "_FETCH_TIMEOUT_SECONDS", 0.1)

    # Use /bin/sleep so we have a real process that will outlive the timeout.
    with pytest.raises(RuntimeError, match="timed out"):
        await srv_mod._run_fetch(["sleep", "5"])

    # Give the event loop a tick to finish reaping. The kill+wait happens
    # inline in _run_fetch, so by the time we get here the child is gone.
    # No easy cross-platform way to assert PID is dead, but if reap didn't
    # happen we'd see a ResourceWarning under filterwarnings=error.


async def test_fetch_file_local_transport_uses_cp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_cfg = Config(
        hosts={"local": HostConfig(name="local", transport="local", ssh_target="")},
    )
    pool = _make_fetch_pool(str(tmp_path / "zfs_mount"))
    server = create_server(pool, local_cfg)  # type: ignore[arg-type]

    captured: list[tuple[list[str], str | None]] = []

    async def fake_run_fetch(cmd: list[str], stdin_data: str | None = None) -> None:
        captured.append((cmd, stdin_data))
        # Local transport uses cp, whose dest is the final argv element.
        await asyncio.to_thread(Path(cmd[-1]).write_bytes, b"x")

    monkeypatch.setattr(srv_mod, "_run_fetch", fake_run_fetch)

    dest = tmp_path / "out.conf"
    await _tool_call(
        server,
        "fetch_file",
        host="local",
        snapshot="rpool/data@s1",
        path="etc/foo",
        local_path=str(dest),
    )
    cmd, batch = captured[0]
    assert cmd[0] == "cp"
    assert "-r" not in cmd
    assert cmd[-1] == str(dest)
    assert batch is None  # local cp carries no stdin batch
