"""FastMCP server: register read-only ZFS snapshot tools backed by the agent.

Tools call through the shared :class:`ConnectionPool`; agent errors surface as
``ValueError`` (caught by FastMCP and returned as tool errors); local
transport failures surface as :class:`RuntimeError`. Time-range parameters
accepting human phrases like ``"yesterday"`` are parsed locally before
forwarding ISO 8601 timestamps to the agent.
"""

from __future__ import annotations

import asyncio
import re
import shlex
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from zsnoop_mcp.config import Config, ConfigError, HostConfig
from zsnoop_mcp.timeparse import TimePhraseError, maybe_to_iso
from zsnoop_mcp.transport import AgentRpcError, ConnectionPool, TransportError

# SSH options forwarded to scp/cp for fetch operations. Mirrors the subset of
# transport.DEFAULT_SSH_OPTIONS that are meaningful to scp (omits -T, which
# controls TTY allocation for interactive SSH, not file transfer).
_SCP_SSH_OPTIONS: tuple[str, ...] = (
    "-o",
    "BatchMode=yes",
    "-o",
    "ServerAliveInterval=30",
    "-o",
    "ServerAliveCountMax=3",
)

# Timeout for a single scp / cp fetch subprocess (seconds). Large files on
# slow links may take longer; callers should document this limit.
_FETCH_TIMEOUT_SECONDS: float = 300.0


# Snapshot-name shape, used to validate strings before they reach the SCP
# command builder for fetch_file / fetch_dir. Mirrors the agent's SNAPSHOT_RE
# (kept duplicated rather than imported because the agent is a standalone
# single-file script, not a package). Defence in depth: even though we
# shell-quote the eventual remote path, refusing malformed names at the
# server boundary fails fast with a clear error.
_SNAPSHOT_NAME_RE: re.Pattern[str] = re.compile(
    r"^[A-Za-z0-9_][A-Za-z0-9_.:/-]*@[A-Za-z0-9_][A-Za-z0-9_.:-]*$",
)


def _parse_snapshot_name(name: str) -> tuple[str, str]:
    """Split ``dataset@snapname`` into ``(dataset, snapname)``.

    Validates the full name against ZFS's restrictive snapshot-naming rules
    before splitting, so a name containing shell metacharacters (semicolons,
    backticks, ``$()``, spaces, etc.) is refused at the server boundary
    rather than being silently passed into a downstream SCP source path.
    """
    if "@" not in name:
        raise ValueError(f"invalid snapshot name (missing '@'): {name!r}")
    if not _SNAPSHOT_NAME_RE.match(name):
        raise ValueError(
            f"invalid snapshot name {name!r}: must match ZFS naming "
            f"(letters, digits, '_.:/' for dataset; '@' separator; "
            f"letters, digits, '_.:-' for snapshot)",
        )
    dataset, snapname = name.split("@", 1)
    return dataset, snapname


def _validate_fetch_path(path: str) -> str:
    """Reject path segments that would escape the snapshot root.

    Mirrors the agent's ``validate_user_path`` for the server side: strips
    a leading ``/`` (callers may pass either form) and rejects ``..``
    components. Returns the normalised relative path string.
    """
    stripped = path.lstrip("/")
    parts = Path(stripped).parts
    if ".." in parts:
        raise ValueError(f"parent-directory segments are not allowed: {path!r}")
    return stripped


def _validate_local_dest(local_path: str) -> Path:
    """Resolve and validate the caller-supplied local destination path.

    Ensures the path is absolute (after ``~`` expansion) and its parent is a
    real directory. Existence semantics for the destination itself are left to
    the caller — ``fetch_file`` and ``fetch_dir`` have different requirements.
    """
    expanded = Path(local_path).expanduser()
    if not expanded.is_absolute():
        raise ValueError(
            f"local_path must be absolute (or ~-expanded): {local_path!r}",
        )
    dest = expanded.resolve()
    parent = dest.parent
    if not parent.exists():
        raise ValueError(f"destination directory does not exist: {parent}")
    if not parent.is_dir():
        # parent exists on disk but isn't a directory (regular file, FIFO,
        # symlink to a non-dir, etc.) — can't create dest underneath it.
        raise ValueError(f"cannot write under {parent}: it is not a directory")
    return dest


async def _run_fetch(cmd: list[str]) -> None:
    """Run *cmd* as a subprocess, raising ``RuntimeError`` on failure or timeout.

    On timeout the child is SIGKILLed and reaped before we raise, so we never
    leak a background ``scp``/``cp`` that keeps using the network and disk.
    Stdin is wired to /dev/null so a misconfigured ``scp`` cannot block waiting
    for interactive input despite ``BatchMode=yes``.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_FETCH_TIMEOUT_SECONDS)
    except TimeoutError as e:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"fetch timed out after {_FETCH_TIMEOUT_SECONDS:.0f}s") from e
    if proc.returncode != 0:
        msg = stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"fetch failed (exit {proc.returncode}): {msg}")


def _build_fetch_cmd(
    host_config: HostConfig,
    remote_path: str,
    dest: Path,
    *,
    recursive: bool,
) -> list[str]:
    """Construct the scp (or cp for local transport) argv for a fetch.

    For the SSH transport the source argument uses scp's ``host:path`` form,
    and the *path* portion is passed to a remote shell by scp. Shell-quote
    it so a snapshot filename containing spaces, ``;``, ``$()``, backticks,
    or other shell metacharacters can't be interpreted as commands on the
    remote host. The local-transport ``cp`` invocation needs no quoting
    because the path is passed as a direct argv element.
    """
    if host_config.transport == "local":
        cmd: list[str] = ["cp", "-a"]
        if recursive:
            cmd.append("-r")
        cmd.extend([remote_path, str(dest)])
    else:
        cmd = ["scp", *_SCP_SSH_OPTIONS, *host_config.ssh_options]
        if recursive:
            cmd.append("-r")
        cmd.extend([f"{host_config.ssh_target}:{shlex.quote(remote_path)}", str(dest)])
    return cmd


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
            result = await pool.call(host, method, params)
            return {**result, "queried_at": datetime.now(UTC).isoformat()}
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
    async def pool_status(host: str, pool: str | None = None) -> dict[str, Any]:
        """Parsed ``zpool status`` for one pool or all pools on `host`.

        Returns ``{pools: [{name, state, scan, status?, action?, see?,
        errors, vdevs: [{name, state, read_errors, write_errors,
        cksum_errors, depth}]}]}``. ``depth`` reflects the vdev tree
        indentation: 0 = pool, 1 = top-level vdev (mirror, raidz, …),
        2 = device. Call this when ``list_pools`` shows a non-ONLINE
        health to find out *which* device.
        """
        return await _call(host, "pool_status", {"pool": pool} if pool else None)

    @mcp.tool()
    async def list_datasets(host: str) -> dict[str, Any]:
        """List ZFS filesystems and volumes on `host` (no snapshots)."""
        return await _call(host, "list_datasets")

    @mcp.tool()
    async def dataset_properties(
        host: str,
        dataset: str,
        properties: list[str] | None = None,
    ) -> dict[str, Any]:
        """All ZFS properties for `dataset` (or a chosen subset).

        Returns ``{dataset, properties: [{name, value, source}]}`` where
        *source* is ``default`` / ``local`` / ``inherited from <dataset>``
        / ``received`` / ``temporary`` / ``-``. Pass ``properties`` to
        fetch only specific names (e.g. ``["compression", "atime",
        "recordsize"]``); omit it for the full set.

        Use this for "why is this dataset behaving like that?" — quota,
        compression ratio, atime, recordsize, mountpoint, encryption,
        canmount, etc. are all here.
        """
        params: dict[str, Any] = {"dataset": dataset}
        if properties is not None:
            params["properties"] = properties
        return await _call(host, "dataset_properties", params)

    @mcp.tool()
    async def list_snapshots(
        host: str,
        dataset: str | None = None,
        after: str | None = None,
        before: str | None = None,
        max_results: int | None = None,
    ) -> dict[str, Any]:
        """List ZFS snapshots on `host`, optionally scoped to `dataset` (recursive).

        Each snapshot reports its creation time as a Unix timestamp.

        For time-bounded questions ("what was snapshotted yesterday?"),
        pass ``after`` / ``before`` — they accept ISO 8601 timestamps *or*
        phrases like ``yesterday``, ``last week``, ``3 days ago``. Filtering
        happens agent-side so the on-wire response stays small.

        ``max_results`` is an opt-in cap (hard max 10 000); the response
        includes ``truncated=true`` when exceeded. Omit it to get all
        matching snapshots — but note that on busy hosts with thousands of
        snapshots an unfiltered, uncapped call can exceed the per-tool
        token budget. Either narrow with ``dataset`` / ``after`` / ``before``
        or pass ``max_results``.

        For aggregate summary statistics (counts by retention class,
        biggest gap, total unique bytes), prefer ``snapshot_cadence``.
        """
        try:
            after_iso = maybe_to_iso(after)
            before_iso = maybe_to_iso(before)
        except TimePhraseError as e:
            raise ValueError(str(e)) from e
        params: dict[str, Any] = {}
        if dataset:
            params["dataset"] = dataset
        if after_iso is not None:
            params["after"] = after_iso
        if before_iso is not None:
            params["before"] = before_iso
        if max_results is not None:
            params["max_results"] = max_results
        return await _call(host, "list_snapshots", params if params else None)

    @mcp.tool()
    async def snapshot_cadence(host: str, dataset: str | None = None) -> dict[str, Any]:
        """Summary statistics for the snapshot inventory on `host`.

        Returns ``{total_snapshots, by_class, earliest_creation,
        latest_creation, biggest_gap_seconds, biggest_gap_between,
        total_unique_bytes}``. ``by_class`` buckets snapshots by
        retention category (frequent / hourly / daily / weekly / monthly
        / other) based on standard ``zfs-auto-snapshot`` naming. Use it
        to answer "is this dataset being snapshotted as expected?" or
        "what's the retention window?" without doing arithmetic on a
        long ``list_snapshots`` response.
        """
        return await _call(host, "snapshot_cadence", {"dataset": dataset} if dataset else None)

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
    async def size_breakdown(
        host: str,
        snapshot: str,
        path: str,
        max_entries: int | None = None,
    ) -> dict[str, Any]:
        """Total bytes for a snapshot directory, plus per-immediate-child sizes.

        Equivalent to ``du --max-depth=1 --block-size=1`` on the snapshot
        path. For each immediate child of ``path``, returns the recursive
        byte total of its subtree. Symlinks are never followed (their own
        inode size is counted). Use this to answer "how big is X?" and
        "what's inside X that's taking the space?" in a single call;
        drill down by calling again on a large child.

        Bounded by ``max_entries`` (default 100,000, hard cap 1,000,000)
        and by a 30s wall-clock budget. On hitting either limit, the
        response sets ``truncated=true`` and each affected child carries
        ``is_truncated=true`` so the caller can see which subtree was
        clipped.
        """
        params: dict[str, Any] = {"snapshot": snapshot, "path": path}
        if max_entries is not None:
            params["max_entries"] = max_entries
        return await _call(host, "size_breakdown", params)

    @mcp.tool()
    async def top_consumers(
        host: str,
        snapshot: str,
        path: str,
        n: int | None = None,
        max_entries: int | None = None,
    ) -> dict[str, Any]:
        """Top-`n` largest files and directories under a snapshot subtree.

        Walks the subtree (bounded the same way as ``size_breakdown``)
        and keeps a heap of the *n* largest entries seen — files,
        directories (subtree total), and symlinks (own lstat size).
        Result is like ``du -ab | sort -rn | head -n``, with paths
        relative to ``path``.

        Use after ``size_breakdown`` once you know *which subtree* is
        big and want to know *which specific files and dirs* inside it
        are responsible. ``n`` defaults to 20 (hard cap 1000);
        ``max_entries`` defaults to 100,000 walked (hard cap 1,000,000)
        and the same 30s wall-clock backstop applies.
        """
        params: dict[str, Any] = {"snapshot": snapshot, "path": path}
        if n is not None:
            params["n"] = n
        if max_entries is not None:
            params["max_entries"] = max_entries
        return await _call(host, "top_consumers", params)

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
        For *content*-level deduplication ("how many distinct versions
        exist?") prefer ``versions_of``.
        """
        return await _call(host, "file_history", {"dataset": dataset, "path": path})

    @mcp.tool()
    async def versions_of(
        host: str,
        dataset: str,
        path: str,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        """List *distinct* versions of `path` across every snapshot of `dataset`.

        Like ``file_history`` but deduplicated by content hash (SHA-256):
        on a daily-snapshot dataset where a file rarely changes, this
        collapses "365 entries, mostly identical" into "5 distinct
        versions, here's when each appeared". Each version reports
        ``first_seen``, ``last_seen``, and the full list of snapshots
        that share its hash.

        Each side is hashed up to ``max_bytes`` (default 1 MiB, capped at
        4 MiB). Larger files are fingerprinted by their prefix; the
        per-version ``truncated`` flag is set so two versions with the
        same prefix-hash but differing tails are flagged.
        """
        params: dict[str, Any] = {"dataset": dataset, "path": path}
        if max_bytes is not None:
            params["max_bytes"] = max_bytes
        return await _call(host, "versions_of", params)

    @mcp.tool()
    async def file_diff(
        host: str,
        snap_a: str,
        snap_b: str,
        path: str,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Unified diff of `path` between two snapshots, ``snap_a`` -> ``snap_b``.

        Returns ``{diff, identical, encoding, present_in_a, present_in_b,
        size_a, size_b, truncated_a, truncated_b}``. ``encoding`` is
        ``"utf-8"`` for textual diffs, ``"binary"`` when either side
        isn't UTF-8 (the ``diff`` field is empty but ``identical`` is
        still answered by SHA-256), and ``"missing"`` when both sides
        are absent. Each side reads up to ``max_bytes`` (default 1 MiB,
        capped at 4 MiB).
        """
        params: dict[str, Any] = {"snap_a": snap_a, "snap_b": snap_b, "path": path}
        if max_bytes is not None:
            params["max_bytes"] = max_bytes
        return await _call(host, "file_diff", params)

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
            raise ValueError(str(e)) from e
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
    async def last_appearance(host: str, dataset: str, path: str) -> dict[str, Any]:
        """Return the *latest* snapshot of `dataset` in which `path` exists, or null.

        Mirror of ``first_appearance``. Compare with the dataset's most
        recent snapshot to answer "when did this file disappear?".
        """
        return await _call(host, "last_appearance", {"dataset": dataset, "path": path})

    @mcp.tool()
    async def find_deleted(
        host: str,
        dataset: str,
        after: str | None = None,
        before: str | None = None,
        max_results: int | None = None,
    ) -> dict[str, Any]:
        """Paths deleted between an earlier snapshot in a window and a later one.

        Resolves ``from_snapshot`` to the earliest snapshot at or after
        ``after`` (or earliest overall) and ``to_snapshot`` to the latest
        snapshot at or before ``before`` (or latest overall), then runs
        ``zfs diff`` between them and returns the entries with op ``-``.

        ``after`` and ``before`` accept ISO 8601 timestamps OR phrases like
        ``yesterday``, ``last week``. Bounded by ``max_results`` (default
        1000, capped at 10 000); ``truncated=true`` when exceeded.
        """
        try:
            after_iso = maybe_to_iso(after)
            before_iso = maybe_to_iso(before)
        except TimePhraseError as e:
            raise ValueError(str(e)) from e
        params: dict[str, Any] = {
            "dataset": dataset,
            "after": after_iso,
            "before": before_iso,
        }
        if max_results is not None:
            params["max_results"] = max_results
        return await _call(host, "find_deleted", params)

    @mcp.tool()
    async def bisect_change(
        host: str,
        dataset: str,
        path: str,
        predicate: dict[str, Any],
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Find the snapshot where `predicate` about `path` flips its value.

        Evaluates the predicate at the earliest and latest snapshots; if
        they disagree, bisects in O(log N) calls and returns the
        snapshot pair on either side of the transition. If they agree,
        returns ``transition: null`` with a ``reason``.

        Predicate shapes:

        - ``{"kind": "exists"}`` — `path` is a regular file
        - ``{"kind": "contains", "needle": "..."}`` — UTF-8 substring in first ``max_bytes``
        - ``{"kind": "sha256_equals", "hash": "<64 hex chars>"}`` — SHA-256 of first ``max_bytes``
        - ``{"kind": "size_at_least", "size": N}`` — file size at least N bytes

        Bisect assumes the predicate is monotonic across the snapshot
        sequence (flips at most once). If it isn't, you get *some*
        transition, not necessarily the one you wanted.
        """
        params: dict[str, Any] = {
            "dataset": dataset,
            "path": path,
            "predicate": predicate,
        }
        if max_bytes is not None:
            params["max_bytes"] = max_bytes
        return await _call(host, "bisect_change", params)

    @mcp.tool()
    async def stale_snapshots(
        host: str,
        older_than: str,
        dataset: str | None = None,
        max_results: int | None = None,
    ) -> dict[str, Any]:
        """Snapshots older than `older_than`, sorted by unique bytes desc.

        ``older_than`` accepts ISO 8601 or a phrase like ``"6 months ago"``,
        ``"last year"``. Results are sorted so the biggest-by-``used``
        appear first — direct input to "what should I cull?". Scoped to
        ``dataset`` if given, else covers every visible snapshot.
        Bounded by ``max_results`` (default 1000, capped at 10 000).
        """
        try:
            older_than_iso = maybe_to_iso(older_than)
        except TimePhraseError as e:
            raise ValueError(str(e)) from e
        if older_than_iso is None:
            raise ValueError("older_than must be a non-empty time phrase or ISO 8601 string")
        params: dict[str, Any] = {"older_than": older_than_iso}
        if dataset is not None:
            params["dataset"] = dataset
        if max_results is not None:
            params["max_results"] = max_results
        return await _call(host, "stale_snapshots", params)

    @mcp.tool()
    async def size_delta(host: str, snap_a: str, snap_b: str) -> dict[str, Any]:
        """Return the bytes written between `snap_a` and `snap_b` of one dataset.

        Both snapshots must belong to the same dataset.
        """
        return await _call(host, "size_delta", {"snap_a": snap_a, "snap_b": snap_b})

    @mcp.tool()
    async def checksum_file(host: str, snapshot: str, path: str) -> dict[str, Any]:
        """SHA-256 of the full content of `path` in `snapshot` on `host`.

        Unlike ``read_file``, the entire file is hashed with no byte cap —
        making it suitable for verifying large files. Use this after
        ``fetch_file`` to confirm the recovered copy is bit-for-bit identical
        to the snapshot original, or to compare against a local ``sha256sum``
        result before downloading.

        Hard cap: 256 MiB per file. Symlinks are refused. Returns
        ``{snapshot, path, size, sha256, queried_at}``.
        """
        return await _call(host, "checksum_file", {"snapshot": snapshot, "path": path})

    @mcp.tool()
    async def fetch_file(
        host: str,
        snapshot: str,
        path: str,
        local_path: str,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Copy a single file from a ZFS snapshot to `local_path` on this machine.

        Uses SCP (or a local ``cp`` for the local transport) — handles any
        file size and any encoding, unlike ``read_file`` which is capped at
        4 MiB and returns content through the LLM context. Use this for
        actual file recovery; use ``read_file`` for quick inspection of small
        text files.

        ``path`` is relative to the snapshot root (leading ``/`` is stripped).
        ``local_path`` is an absolute or ``~``-expanded path on this machine;
        its parent directory must already exist. Directory destinations are
        rejected even with ``overwrite=true`` — pass an explicit file path so
        the returned ``local_path`` and ``size_bytes`` are unambiguous. Set
        ``overwrite=true`` to replace an existing file; the default is to refuse.

        Returns ``{snapshot, path, local_path, size_bytes, queried_at}``.
        """
        _validate_host(host)
        host_config = config.host(host)
        try:
            fetch_path = _validate_fetch_path(path)
            dest = _validate_local_dest(local_path)
        except ValueError as e:
            raise ValueError(str(e)) from e
        if dest.exists():
            if dest.is_dir():
                raise ValueError(
                    f"destination is a directory; fetch_file needs an explicit file path: {dest}",
                )
            if not overwrite:
                raise ValueError(
                    f"destination already exists: {dest} — pass overwrite=true to replace it",
                )
        dataset, snapname = _parse_snapshot_name(snapshot)
        props = await _call(
            host, "dataset_properties", {"dataset": dataset, "properties": ["mountpoint"]}
        )
        mountpoint = next(
            (p["value"] for p in props.get("properties", []) if p["name"] == "mountpoint"),
            None,
        )
        if not mountpoint or mountpoint in ("-", "none", "legacy"):
            raise ValueError(f"dataset {dataset!r} has no usable mountpoint ({mountpoint!r})")
        remote_path = f"{mountpoint}/.zfs/snapshot/{snapname}/{fetch_path}"
        cmd = _build_fetch_cmd(host_config, remote_path, dest, recursive=False)
        await _run_fetch(cmd)
        size = dest.stat().st_size
        return {
            "snapshot": snapshot,
            "path": path,
            "local_path": str(dest),
            "size_bytes": size,
            "queried_at": datetime.now(UTC).isoformat(),
        }

    @mcp.tool()
    async def fetch_dir(
        host: str,
        snapshot: str,
        path: str,
        local_path: str,
    ) -> dict[str, Any]:
        """Copy a directory subtree from a ZFS snapshot to `local_path` on this machine.

        Recursively copies the entire subtree under `path` using SCP (or
        ``cp -ar`` for the local transport). Use this to restore a whole
        directory from a snapshot in one call.

        ``path`` is relative to the snapshot root (leading ``/`` is stripped).
        ``local_path`` is an absolute or ``~``-expanded path on this machine;
        its parent directory must already exist, and ``local_path`` itself
        must NOT already exist — ``scp -r`` / ``cp -ar`` semantics for
        existing destinations are ambiguous (some copy *into*, some
        *populate*), so the caller is required to clear it first.

        Returns ``{snapshot, path, local_path, queried_at}``.
        """
        _validate_host(host)
        host_config = config.host(host)
        try:
            fetch_path = _validate_fetch_path(path)
            dest = _validate_local_dest(local_path)
        except ValueError as e:
            raise ValueError(str(e)) from e
        if dest.exists():
            raise ValueError(
                f"destination already exists: {dest} — remove it first; "
                f"fetch_dir requires a non-existent destination",
            )
        dataset, snapname = _parse_snapshot_name(snapshot)
        props = await _call(
            host, "dataset_properties", {"dataset": dataset, "properties": ["mountpoint"]}
        )
        mountpoint = next(
            (p["value"] for p in props.get("properties", []) if p["name"] == "mountpoint"),
            None,
        )
        if not mountpoint or mountpoint in ("-", "none", "legacy"):
            raise ValueError(f"dataset {dataset!r} has no usable mountpoint ({mountpoint!r})")
        remote_path = f"{mountpoint}/.zfs/snapshot/{snapname}/{fetch_path}"
        cmd = _build_fetch_cmd(host_config, remote_path, dest, recursive=True)
        await _run_fetch(cmd)
        return {
            "snapshot": snapshot,
            "path": path,
            "local_path": str(dest),
            "queried_at": datetime.now(UTC).isoformat(),
        }

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
