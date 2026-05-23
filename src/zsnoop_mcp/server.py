"""FastMCP server: register read-only ZFS snapshot tools backed by the agent.

Tools call through the shared :class:`ConnectionPool`; agent errors surface as
``ValueError`` (caught by FastMCP and returned as tool errors); local
transport failures surface as :class:`RuntimeError`. Time-range parameters
accepting human phrases like ``"yesterday"`` are parsed locally before
forwarding ISO 8601 timestamps to the agent.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from zsnoop_mcp.config import Config, ConfigError
from zsnoop_mcp.timeparse import TimePhraseError, maybe_to_iso
from zsnoop_mcp.transport import AgentRpcError, ConnectionPool, TransportError

INSTRUCTIONS = (
    "Read-only exploration of ZFS snapshots on remote hosts over SSH. "
    "All operations are scoped to a host configured by the operator. "
    "Use `list_hosts` first to see what's reachable; pass `host` to every "
    "other tool. Time-range parameters accept ISO 8601 or human phrases "
    "like 'yesterday', 'last week', '3 days ago'."
)


def create_server(pool: ConnectionPool, config: Config) -> FastMCP:  # noqa: PLR0915 - one body, twelve tools
    """Build a FastMCP server with all snapshot tools registered."""
    mcp = FastMCP("zsnoop-mcp", instructions=INSTRUCTIONS)

    def _validate_host(host: str) -> None:
        try:
            config.host(host)
        except ConfigError as e:
            raise ValueError(str(e)) from e

    async def _call(host: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        _validate_host(host)
        try:
            return await pool.call(host, method, params)
        except AgentRpcError as e:
            raise ValueError(f"agent error ({e.code}): {e.message}") from e
        except TransportError as e:
            raise RuntimeError(f"transport error talking to {host!r}: {e}") from e

    @mcp.tool()
    async def list_hosts() -> dict[str, Any]:
        """List the hosts this server is configured to talk to.

        Always call this first if you don't know which host to query.
        """
        return {
            "hosts": [
                {
                    "name": h.name,
                    "ssh_target": h.ssh_target,
                    "agent_mode": h.agent_mode,
                    "sudo": h.sudo,
                    "pools": list(h.pools),
                }
                for h in config.hosts.values()
            ],
        }

    @mcp.tool()
    async def agent_info(host: str) -> dict[str, Any]:
        """Return version, supported methods, and limits of the agent on `host`."""
        return await _call(host, "agent_info")

    @mcp.tool()
    async def list_pools(host: str) -> dict[str, Any]:
        """List ZFS pools visible to the agent on `host`.

        Each pool reports ``size``, ``allocated``, ``free`` (bytes), and
        ``health``. Useful when you don't already know what pools exist —
        prefer this over the static ``pools`` field in the host config.
        """
        return await _call(host, "list_pools")

    @mcp.tool()
    async def list_datasets(host: str) -> dict[str, Any]:
        """List ZFS filesystems and volumes on `host` (no snapshots)."""
        return await _call(host, "list_datasets")

    @mcp.tool()
    async def list_snapshots(host: str, dataset: str | None = None) -> dict[str, Any]:
        """List ZFS snapshots on `host`, optionally scoped to `dataset` (recursive).

        Each snapshot reports its creation time as a Unix timestamp.
        """
        return await _call(host, "list_snapshots", {"dataset": dataset} if dataset else None)

    @mcp.tool()
    async def diff_snapshots(host: str, snap_a: str, snap_b: str) -> dict[str, Any]:
        """Return paths that differ between two snapshots of the same dataset.

        `snap_a` and `snap_b` are full names like ``rpool/home@daily-1``.
        Result entries are ``{op, type, path}`` with op in ``+``/``-``/``M``/``R``;
        renames also carry ``new_path``.
        """
        return await _call(host, "diff_snapshots", {"snap_a": snap_a, "snap_b": snap_b})

    @mcp.tool()
    async def list_dir(
        host: str,
        snapshot: str,
        path: str = "",
        max_entries: int | None = None,
    ) -> dict[str, Any]:
        """List a directory inside a snapshot.

        `path` is relative to the dataset's root (leading ``/`` is stripped).
        Symlinks are reported with their targets but never followed. Result
        flags ``truncated=true`` when more than ``max_entries`` (default 1000,
        capped at 10000) would have been returned.
        """
        params: dict[str, Any] = {"snapshot": snapshot, "path": path}
        if max_entries is not None:
            params["max_entries"] = max_entries
        return await _call(host, "list_dir", params)

    @mcp.tool()
    async def read_file(
        host: str,
        snapshot: str,
        path: str,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Read a single file from a snapshot, bounded by `max_bytes`.

        Default ``max_bytes`` is 1 MiB; the server caps reads at 4 MiB.
        UTF-8 files are returned as text; binary content as base64.
        Symlinks are refused (never followed).
        """
        params: dict[str, Any] = {"snapshot": snapshot, "path": path}
        if max_bytes is not None:
            params["max_bytes"] = max_bytes
        return await _call(host, "read_file", params)

    @mcp.tool()
    async def find_files(
        host: str,
        snapshot: str,
        pattern: str,
        path: str = "",
        max_results: int | None = None,
    ) -> dict[str, Any]:
        """Find files in a snapshot by `fnmatch` name pattern (e.g. ``*.conf``).

        Optionally scoped to `path` within the snapshot. Result truncates at
        ``max_results`` (default 100, capped at 1000).
        """
        params: dict[str, Any] = {"snapshot": snapshot, "pattern": pattern, "path": path}
        if max_results is not None:
            params["max_results"] = max_results
        return await _call(host, "find_files", params)

    @mcp.tool()
    async def content_grep(
        host: str,
        snapshot: str,
        pattern: str,
        path: str = "",
        max_results: int | None = None,
    ) -> dict[str, Any]:
        """Search file *contents* in a snapshot for a Python regex.

        Optionally scoped to `path`. Binary files are skipped. Result truncates
        at ``max_results`` (default 100, capped at 1000).
        """
        params: dict[str, Any] = {"snapshot": snapshot, "pattern": pattern, "path": path}
        if max_results is not None:
            params["max_results"] = max_results
        return await _call(host, "content_grep", params)

    @mcp.tool()
    async def file_history(host: str, dataset: str, path: str) -> dict[str, Any]:
        """Walk every snapshot of `dataset` and report the version of `path` in each.

        Each version reports ``present``, and if present also ``size`` and
        ``mtime``. Useful for tracking when a file changed or disappeared.
        """
        return await _call(host, "file_history", {"dataset": dataset, "path": path})

    @mcp.tool()
    async def snapshots_containing(
        host: str,
        dataset: str,
        path: str,
        after: str | None = None,
        before: str | None = None,
    ) -> dict[str, Any]:
        """Return the snapshots of `dataset` in which `path` currently exists.

        `after` and `before` accept ISO 8601 timestamps OR human phrases like
        ``yesterday``, ``last week``, ``3 days ago``. Useful for "find me a
        snapshot from before the change" queries.
        """
        try:
            after_iso = maybe_to_iso(after)
            before_iso = maybe_to_iso(before)
        except TimePhraseError as e:
            raise ValueError(f"could not parse time phrase: {e}") from e
        return await _call(
            host,
            "snapshots_containing",
            {"dataset": dataset, "path": path, "after": after_iso, "before": before_iso},
        )

    @mcp.tool()
    async def first_appearance(host: str, dataset: str, path: str) -> dict[str, Any]:
        """Return the earliest snapshot of `dataset` in which `path` exists, or null."""
        return await _call(host, "first_appearance", {"dataset": dataset, "path": path})

    @mcp.tool()
    async def size_delta(host: str, snap_a: str, snap_b: str) -> dict[str, Any]:
        """Return the bytes written between `snap_a` and `snap_b` of one dataset.

        Both snapshots must belong to the same dataset.
        """
        return await _call(host, "size_delta", {"snap_a": snap_a, "snap_b": snap_b})

    return mcp


def find_agent_source() -> str:
    """Return the agent script as a string.

    Tries the installed-package resource location first (wheel install), then
    falls back to walking up from this file to find ``agent/zfs_snoop_agent.py``
    (editable / dev install).
    """
    # Wheel install: hatchling force-includes agent/ into the package.
    try:
        candidate = files("zsnoop_mcp") / "_agent_source" / "zfs_snoop_agent.py"
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    except (ModuleNotFoundError, FileNotFoundError):
        pass
    # Dev install: walk up from this file.
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate_path = parent / "agent" / "zfs_snoop_agent.py"
        if candidate_path.is_file():
            return candidate_path.read_text(encoding="utf-8")
    raise FileNotFoundError(
        "could not locate agent/zfs_snoop_agent.py; "
        "either install from a wheel or run from the source tree",
    )
