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
        "restore_file",
        "restore_dir",
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


def test_sftp_quote_rejects_batch_breaking_chars() -> None:
    """The sftp batch script is line-oriented, so a newline/CR/NUL in a path
    can't be contained by double-quoting (it terminates the command line
    inside the quotes). _sftp_quote refuses them rather than emit a line
    sftp would mis-parse or read as an injected second command."""
    for bad in ("a\nb", "a\rb", "a\0b"):
        with pytest.raises(ValueError, match="newline, carriage-return, or NUL"):
            srv_mod._sftp_quote(bad)


@pytest.mark.parametrize("bad_char", ["\n", "\r", "\0"])
async def test_fetch_file_rejects_newline_in_remote_path(
    cfg: Config,
    tmp_path: Path,
    bad_char: str,
) -> None:
    """A remote path containing a batch-breaking char is refused at the
    server boundary, before any sftp subprocess is spawned. Closes the
    sftp-batch command-injection vector Copilot flagged on PR #16."""
    pool = _make_fetch_pool("/data")
    server = create_server(pool, cfg)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="newline, carriage-return, or NUL"):
        await _tool_call(
            server,
            "fetch_file",
            host="r2d2",
            snapshot="rpool/data@daily-1",
            path=f"etc/app.conf{bad_char}rm -rf important",
            local_path=str(tmp_path / "out.conf"),
        )


@pytest.mark.parametrize("bad_char", ["\n", "\r", "\0"])
async def test_fetch_file_rejects_newline_in_local_path(
    cfg: Config,
    tmp_path: Path,
    bad_char: str,
) -> None:
    """A local destination containing a batch-breaking char is likewise
    refused — it is the second quoted argument in the sftp `get` line."""
    pool = _make_fetch_pool("/data")
    server = create_server(pool, cfg)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="newline, carriage-return, or NUL"):
        await _tool_call(
            server,
            "fetch_file",
            host="r2d2",
            snapshot="rpool/data@daily-1",
            path="etc/app.conf",
            local_path=f"{tmp_path}/out{bad_char}rm.conf",
        )


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


# ---- restore_file / restore_dir (v0.4.0) -----------------------------------


def _restore_cfg(restore_paths: tuple[str, ...] = ("/srv/", "/home/mch/")) -> Config:
    """Config with a single host that has restore opted-in."""
    return Config(
        hosts={
            "bork": HostConfig(
                name="bork",
                ssh_target="bork.lan",
                allow_restore=True,
                restore_paths=restore_paths,
            ),
        },
    )


async def test_restore_file_rejects_when_allow_restore_disabled(
    cfg: Config,  # r2d2/c3po — both have allow_restore=False (default)
    fake_pool: FakePool,
) -> None:
    """Stock installs are unaffected: without explicit opt-in per host,
    restore_* refuses BEFORE doing anything (no agent call, no mutation
    even attempted)."""
    server = create_server(fake_pool, cfg)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="restore is disabled on host 'r2d2'"):
        await _tool_call(
            server,
            "restore_file",
            host="r2d2",
            snapshot="rpool@s1",
            snapshot_path="etc/foo",
            target_path="/srv/foo",
        )
    assert fake_pool.calls == []  # never reached the agent


async def test_restore_file_rejects_target_outside_allowlist(
    fake_pool: FakePool,
) -> None:
    """target_path must lie under one of the operator's restore_paths
    prefixes (here: /srv/, /home/mch/). /etc/passwd is not."""
    server = create_server(fake_pool, _restore_cfg())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="not under any configured restore_paths"):
        await _tool_call(
            server,
            "restore_file",
            host="bork",
            snapshot="rpool@s1",
            snapshot_path="etc/foo",
            target_path="/etc/passwd",
        )
    assert fake_pool.calls == []


async def test_restore_file_canonicalises_dotdot_before_allowlist_check(
    fake_pool: FakePool,
) -> None:
    """`/srv/../etc/passwd` resolves to `/etc/passwd`, which is NOT under
    the `/srv/` allowlist — must be rejected, not passed through to the
    agent as-is. Closes a path-traversal style bypass."""
    server = create_server(fake_pool, _restore_cfg(("/srv/",)))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="not under any configured restore_paths"):
        await _tool_call(
            server,
            "restore_file",
            host="bork",
            snapshot="rpool@s1",
            snapshot_path="etc/foo",
            target_path="/srv/../etc/passwd",
        )
    assert fake_pool.calls == []


@pytest.mark.parametrize(
    "bad_target",
    ["/proc/self/mem", "/sys/power/state", "/dev/sda"],
)
async def test_restore_file_rejects_kernel_virtual_fs_denylist(
    fake_pool: FakePool,
    bad_target: str,
) -> None:
    """The denylist applies even when restore_paths is `["/"]` (the
    operator chose to allow everything) — kernel virtual fs is never a
    sane restore target and writes there can have side effects far beyond
    a normal file."""
    server = create_server(fake_pool, _restore_cfg(("/",)))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="denied prefix"):
        await _tool_call(
            server,
            "restore_file",
            host="bork",
            snapshot="rpool@s1",
            snapshot_path="etc/foo",
            target_path=bad_target,
        )


async def test_restore_file_rejects_zfs_snapshot_substring(
    fake_pool: FakePool,
) -> None:
    """Writing into a snapshot tree is refused with a clear error (ZFS
    would refuse anyway — make the failure mode explicit and early)."""
    server = create_server(fake_pool, _restore_cfg(("/",)))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="snapshot tree"):
        await _tool_call(
            server,
            "restore_file",
            host="bork",
            snapshot="rpool@s1",
            snapshot_path="etc/foo",
            target_path="/home/.zfs/snapshot/x/foo",
        )


async def test_restore_file_rejects_relative_target(fake_pool: FakePool) -> None:
    server = create_server(fake_pool, _restore_cfg())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="must be absolute"):
        await _tool_call(
            server,
            "restore_file",
            host="bork",
            snapshot="rpool@s1",
            snapshot_path="etc/foo",
            target_path="srv/foo",
        )


@pytest.mark.parametrize("bad_char", ["\n", "\r", "\0"])
async def test_restore_file_rejects_target_with_batch_breaking_chars(
    fake_pool: FakePool,
    bad_char: str,
) -> None:
    """Same control-char rejection as fetch_* (no batch script here, but
    NUL is illegal in any path and newlines invite log/parse mishandling
    downstream)."""
    server = create_server(fake_pool, _restore_cfg())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="newline, carriage-return, or NUL"):
        await _tool_call(
            server,
            "restore_file",
            host="bork",
            snapshot="rpool@s1",
            snapshot_path="etc/foo",
            target_path=f"/srv/foo{bad_char}rm bar",
        )


async def test_restore_file_rejects_existing_target_without_overwrite(
    fake_pool: FakePool,
    tmp_path: Path,
) -> None:
    existing = tmp_path / "already-there.conf"
    existing.write_text("don't clobber me")
    server = create_server(fake_pool, _restore_cfg((str(tmp_path) + "/",)))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="target_path already exists"):
        await _tool_call(
            server,
            "restore_file",
            host="bork",
            snapshot="rpool@s1",
            snapshot_path="etc/foo",
            target_path=str(existing),
        )
    assert fake_pool.calls == []


async def test_restore_file_refuses_directory_destination_even_with_overwrite(
    fake_pool: FakePool,
    tmp_path: Path,
) -> None:
    """A directory at target_path is refused unconditionally for
    restore_file — restoring a *file* on top of a directory is almost
    always a typo. Use restore_dir if you meant a tree."""
    existing_dir = tmp_path / "a-directory"
    existing_dir.mkdir()
    server = create_server(fake_pool, _restore_cfg((str(tmp_path) + "/",)))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="is a directory"):
        await _tool_call(
            server,
            "restore_file",
            host="bork",
            snapshot="rpool@s1",
            snapshot_path="etc/foo",
            target_path=str(existing_dir),
            overwrite=True,
        )


async def test_restore_file_forwards_validated_params_to_agent(
    fake_pool: FakePool,
    tmp_path: Path,
) -> None:
    """Happy-path forwarding: the server-validated params reach the agent
    intact, with backup_path=null when not overwriting."""
    fake_pool.next_result = {
        "snapshot": "rpool@s1",
        "snapshot_path": "etc/app.conf",
        "target_path": str(tmp_path / "app.conf"),
        "size_bytes": 42,
        "backup_path": None,
    }
    server = create_server(fake_pool, _restore_cfg((str(tmp_path) + "/",)))  # type: ignore[arg-type]
    result = await _tool_call(
        server,
        "restore_file",
        host="bork",
        snapshot="rpool@s1",
        snapshot_path="etc/app.conf",
        target_path=str(tmp_path / "app.conf"),
    )
    assert len(fake_pool.calls) == 1
    host, method, params = fake_pool.calls[0]
    assert host == "bork"
    assert method == "restore_file"
    assert params == {
        "snapshot": "rpool@s1",
        "snapshot_path": "etc/app.conf",
        "target_path": str(tmp_path / "app.conf"),
        "overwrite": False,
        "backup_path": None,
    }
    assert "queried_at" in result  # injected by _call


async def test_restore_file_computes_backup_path_when_overwrite_and_backup(
    fake_pool: FakePool,
    tmp_path: Path,
) -> None:
    """With overwrite=True + backup=True + an existing target, the server
    computes the timestamped backup_path and passes it to the agent for
    atomic-rename-before-replace."""
    existing = tmp_path / "to-replace.conf"
    existing.write_text("old")
    fake_pool.next_result = {
        "snapshot": "rpool@s1",
        "snapshot_path": "etc/x",
        "target_path": str(existing),
        "size_bytes": 7,
        "backup_path": f"{existing}.zsnoop-backup-PLACEHOLDER",
    }
    server = create_server(fake_pool, _restore_cfg((str(tmp_path) + "/",)))  # type: ignore[arg-type]
    await _tool_call(
        server,
        "restore_file",
        host="bork",
        snapshot="rpool@s1",
        snapshot_path="etc/x",
        target_path=str(existing),
        overwrite=True,
        backup=True,
    )
    _h, _m, params = fake_pool.calls[0]
    assert params is not None
    assert params["overwrite"] is True
    assert params["backup_path"] is not None
    assert params["backup_path"].startswith(f"{existing}.zsnoop-backup-")
    # ISO 8601 timestamp suffix — verify it round-trips.
    suffix = params["backup_path"].split(".zsnoop-backup-", 1)[1]
    assert datetime.fromisoformat(suffix).tzinfo is not None


async def test_restore_file_backup_ignored_when_target_absent(
    fake_pool: FakePool,
    tmp_path: Path,
) -> None:
    """backup=True with a non-existent target is a no-op (nothing to back
    up). The agent gets backup_path=null."""
    fake_pool.next_result = {
        "snapshot": "rpool@s1",
        "snapshot_path": "etc/x",
        "target_path": str(tmp_path / "new.conf"),
        "size_bytes": 7,
        "backup_path": None,
    }
    server = create_server(fake_pool, _restore_cfg((str(tmp_path) + "/",)))  # type: ignore[arg-type]
    await _tool_call(
        server,
        "restore_file",
        host="bork",
        snapshot="rpool@s1",
        snapshot_path="etc/x",
        target_path=str(tmp_path / "new.conf"),
        backup=True,  # ignored: no existing target
    )
    _h, _m, params = fake_pool.calls[0]
    assert params is not None
    assert params["backup_path"] is None


async def test_restore_dir_rejects_when_allow_restore_disabled(
    cfg: Config,
    fake_pool: FakePool,
) -> None:
    server = create_server(fake_pool, cfg)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="restore is disabled"):
        await _tool_call(
            server,
            "restore_dir",
            host="r2d2",
            snapshot="rpool@s1",
            snapshot_path="etc",
            target_path="/srv/etc-restore",
        )


async def test_restore_dir_refuses_file_destination_with_overwrite(
    fake_pool: FakePool,
    tmp_path: Path,
) -> None:
    """Replacing a file with a directory tree is almost always a typo —
    rejected even with overwrite=True. Use restore_file if you meant a
    single file."""
    existing_file = tmp_path / "is-a-file.txt"
    existing_file.write_text("not a directory")
    server = create_server(fake_pool, _restore_cfg((str(tmp_path) + "/",)))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="restore_dir replaces a directory"):
        await _tool_call(
            server,
            "restore_dir",
            host="bork",
            snapshot="rpool@s1",
            snapshot_path="etc",
            target_path=str(existing_file),
            overwrite=True,
        )


async def test_restore_dir_forwards_validated_params_to_agent(
    fake_pool: FakePool,
    tmp_path: Path,
) -> None:
    fake_pool.next_result = {
        "snapshot": "rpool@s1",
        "snapshot_path": "etc",
        "target_path": str(tmp_path / "etc-restore"),
        "backup_path": None,
    }
    server = create_server(fake_pool, _restore_cfg((str(tmp_path) + "/",)))  # type: ignore[arg-type]
    await _tool_call(
        server,
        "restore_dir",
        host="bork",
        snapshot="rpool@s1",
        snapshot_path="etc",
        target_path=str(tmp_path / "etc-restore"),
    )
    _h, method, params = fake_pool.calls[0]
    assert method == "restore_dir"
    assert params == {
        "snapshot": "rpool@s1",
        "snapshot_path": "etc",
        "target_path": str(tmp_path / "etc-restore"),
        "overwrite": False,
        "backup_path": None,
    }
