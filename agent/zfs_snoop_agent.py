#!/usr/bin/env python3
"""Remote ZFS snapshot exploration agent.

Single-file, stdlib-only. Runs on any host with Python 3.11+ and the `zfs`
CLI. Reads NDJSON JSON-RPC 2.0 requests from stdin, writes responses to
stdout, structured logs to stderr.

This agent is read-only by construction:
- Method dispatch uses an explicit allowlist; mutation methods are not in
  the table and there is no configuration to add them.
- All `zfs` subprocess invocations use shell=False with validated argv.
- Path inputs cannot escape their snapshot root; symlinks are not followed
  when reading files or listing directories.
- All reads are bounded by per-operation size limits (see G4 in SECURITY.md).
"""

from __future__ import annotations

import base64
import difflib
import fnmatch
import hashlib
import heapq
import json
import logging
import os
import re
import signal
import stat as stat_mod
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

# A JSON-RPC ``id`` is per spec a string, number, or null. We type-erase to
# ``object`` at the boundary because we don't introspect it.
JsonId = str | int | float | None

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------

AGENT_VERSION: Final = "0.2.0"
PROTOCOL_VERSION: Final = "1"

# Hard caps the agent will never exceed regardless of caller-provided values.
MAX_READ_BYTES: Final = 4 * 1024 * 1024
MAX_DIR_ENTRIES: Final = 10_000
MAX_FIND_RESULTS: Final = 1_000
MAX_GREP_RESULTS: Final = 1_000
# Total filesystem entries the recursive `size_breakdown` walk may visit
# before returning a truncated result. 1M lstat calls is roughly 5-10s of
# wall-clock on a hot cache; SIZE_WALK_TIMEOUT_SECONDS is the hard backstop.
MAX_SIZE_WALK_ENTRIES: Final = 1_000_000
DEFAULT_READ_BYTES: Final = 1 * 1024 * 1024
DEFAULT_DIR_ENTRIES: Final = 1_000
DEFAULT_FIND_RESULTS: Final = 100
DEFAULT_GREP_RESULTS: Final = 100
DEFAULT_SIZE_WALK_ENTRIES: Final = 100_000
# `find_deleted` returns a bounded list of paths removed between two snapshots
# of a dataset. Uses the same cap shape as list_dir.
MAX_DELETED_RESULTS: Final = 10_000
DEFAULT_DELETED_RESULTS: Final = 1_000
# Per-version content hashing in `versions_of` reads up to this many bytes
# from each snapshot's copy of the file. Files larger than this hash as
# "first N bytes" — the version's `truncated` flag is set so callers can
# treat the hash as a fingerprint of the prefix, not the whole file.
MAX_VERSION_HASH_BYTES: Final = 4 * 1024 * 1024
DEFAULT_VERSION_HASH_BYTES: Final = 1 * 1024 * 1024
# `file_diff` reads each side of the comparison up to this many bytes. Same
# cap shape as read_file because the underlying read is the same operation.
MAX_DIFF_BYTES: Final = 4 * 1024 * 1024
DEFAULT_DIFF_BYTES: Final = 1 * 1024 * 1024
# `top_consumers` keeps the N largest entries seen during a bounded walk.
# The walk itself reuses the size_breakdown entry budget; N is just the
# heap size.
MAX_TOP_CONSUMERS: Final = 1_000
DEFAULT_TOP_CONSUMERS: Final = 20
# `stale_snapshots` returns at most this many entries per call.
MAX_STALE_RESULTS: Final = 10_000
DEFAULT_STALE_RESULTS: Final = 1_000
# `bisect_change` evaluates predicates that may read file content; this
# bounds the per-evaluation read.
MAX_BISECT_BYTES: Final = 4 * 1024 * 1024
DEFAULT_BISECT_BYTES: Final = 1 * 1024 * 1024
# `checksum_file` hashes the full file in streaming chunks. The hard cap
# keeps the operation bounded on the remote; files larger than this should
# be checksummed out-of-band rather than through the MCP transport.
MAX_CHECKSUM_FILESIZE: Final = 256 * 1024 * 1024

# Subprocess wall-clock timeout for any single zfs / zpool invocation.
# Suits metadata commands (list, get) which are essentially constant-time.
ZFS_TIMEOUT_SECONDS: Final = 30.0
# Longer wall-clock timeout for `zfs diff`, which walks every changed object
# between two snapshots and can take minutes on a high-churn dataset. See
# GH issue #7. `diff_snapshots` and `find_deleted` use this budget.
ZFS_DIFF_TIMEOUT_SECONDS: Final = 300.0
# Wall-clock cap on a single size_breakdown walk. Belt-and-braces against
# pathological cache-cold filesystems where the entry budget alone is too
# loose. Truncates with `truncated: true` rather than failing.
SIZE_WALK_TIMEOUT_SECONDS: Final = 30.0

# ZFS naming rules (intentionally restrictive; matches typical usage).
DATASET_RE: Final = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:/-]*$")
SNAPSHOT_RE: Final = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:/-]*@[A-Za-z0-9_][A-Za-z0-9_.:-]*$")

# JSON-RPC 2.0 standard error codes.
PARSE_ERROR: Final = -32700
INVALID_REQUEST: Final = -32600
METHOD_NOT_FOUND: Final = -32601
INVALID_PARAMS: Final = -32602
INTERNAL_ERROR: Final = -32603
# Application-defined errors in -32000..-32099.
ZFS_ERROR: Final = -32001
PATH_ERROR: Final = -32002
TIMEOUT_ERROR: Final = -32003

# `zfs diff -H -F` columns: <change>\t<type>\t<path>[\t<new_path>].
DIFF_MIN_FIELDS: Final = 3
DIFF_RENAME_FIELDS: Final = 4


log = logging.getLogger("zfs-snoop-agent")


# ----------------------------------------------------------------------------
# Exceptions (caught at the dispatch boundary and converted to JSON-RPC errors)
# ----------------------------------------------------------------------------


class AgentError(Exception):
    """Base class for agent errors with a JSON-RPC error code."""

    code: int = INTERNAL_ERROR

    def __init__(self, message: str, *, data: object | None = None) -> None:
        super().__init__(message)
        self.data: object | None = data


class InvalidParams(AgentError):
    code = INVALID_PARAMS


class PathError(AgentError):
    code = PATH_ERROR


class ZfsError(AgentError):
    code = ZFS_ERROR


class AgentTimeoutError(AgentError):
    code = TIMEOUT_ERROR


# ----------------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------------


def validate_dataset(name: str) -> str:
    if not isinstance(name, str) or not DATASET_RE.match(name):
        raise InvalidParams(f"invalid dataset name: {name!r}")
    return name


def validate_snapshot(name: str) -> tuple[str, str]:
    """Return ``(dataset, snapname)`` after validating *name*."""
    if not isinstance(name, str) or not SNAPSHOT_RE.match(name):
        raise InvalidParams(f"invalid snapshot name: {name!r}")
    dataset, snapname = name.split("@", 1)
    return dataset, snapname


def validate_user_path(path: str) -> Path:
    """Validate a caller-provided relative path. No abs paths, no ``..``."""
    if not isinstance(path, str):
        raise InvalidParams(f"path must be a string, got {type(path).__name__}")
    # Normalise leading "/" away so callers can say "/etc/foo" or "etc/foo".
    stripped = path.lstrip("/")
    p = Path(stripped)
    if p.is_absolute():
        raise PathError(f"absolute paths are not allowed: {path!r}")
    if any(part == ".." for part in p.parts):
        raise PathError(f"parent-directory segments are not allowed: {path!r}")
    return p


def validate_positive_int(value: object, *, name: str, default: int, hard_max: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidParams(f"{name} must be an int, got {type(value).__name__}")
    if value <= 0:
        raise InvalidParams(f"{name} must be positive, got {value}")
    return min(value, hard_max)


# ----------------------------------------------------------------------------
# Snapshot path resolution
# ----------------------------------------------------------------------------


def get_dataset_mountpoint(dataset: str) -> Path:
    """Return the live mountpoint of *dataset*."""
    out = run_zfs(["get", "-H", "-p", "-o", "value", "mountpoint", dataset])
    mp = out.strip()
    if mp in ("", "-", "none", "legacy"):
        raise ZfsError(f"dataset {dataset!r} has no usable mountpoint ({mp!r})")
    return Path(mp)


def snapshot_root(snapshot: str) -> Path:
    """Return the on-disk root for *snapshot*'s ``.zfs/snapshot/<name>``."""
    dataset, snapname = validate_snapshot(snapshot)
    mp = get_dataset_mountpoint(dataset)
    root = mp / ".zfs" / "snapshot" / snapname
    if not root.is_dir():
        raise PathError(f"snapshot root not found or not a directory: {root}")
    return root


def resolve_under_snapshot(snapshot: str, user_path: str) -> tuple[Path, Path]:
    """Resolve *user_path* under *snapshot*'s root.

    Returns ``(root, target)`` where *target* is the joined path before
    symlink resolution; the boundary check below uses the *resolved* form
    to verify that even if every symlink were followed the result stays
    inside *root*. Callers receive the unresolved form so they can
    :meth:`Path.lstat` it to detect a final-component symlink without
    following it (G3 — symlinks are never followed by read/list).
    """
    rel = validate_user_path(user_path)
    root = snapshot_root(snapshot)
    real_root = root.resolve()
    candidate = real_root / rel
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as e:
        raise PathError(f"could not resolve path: {e}") from e
    if real_root != resolved and real_root not in resolved.parents:
        raise PathError(f"path escapes snapshot root: {user_path!r}")
    return real_root, candidate


# ----------------------------------------------------------------------------
# Subprocess wrappers
# ----------------------------------------------------------------------------


def run_zfs(args: list[str], *, timeout: float | None = None) -> str:
    """Run ``zfs`` with *args*, return stdout, raise on error or timeout.

    *timeout* overrides ``ZFS_TIMEOUT_SECONDS``. Used by ``zfs diff``
    callers to allow the longer ``ZFS_DIFF_TIMEOUT_SECONDS`` budget.
    """
    return _run_cli("zfs", args, timeout=timeout)


def run_zpool(args: list[str], *, timeout: float | None = None) -> str:
    """Run ``zpool`` with *args*, return stdout, raise on error or timeout."""
    return _run_cli("zpool", args, timeout=timeout)


def _run_cli(binary: str, args: list[str], *, timeout: float | None = None) -> str:
    cmd = [binary, *args]
    effective_timeout = ZFS_TIMEOUT_SECONDS if timeout is None else timeout
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=effective_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise AgentTimeoutError(
            f"{binary} {' '.join(args[:2])} timed out after {effective_timeout}s",
        ) from e
    except FileNotFoundError as e:
        raise ZfsError(f"{binary} binary not found on PATH") from e
    if result.returncode != 0:
        raise ZfsError(
            f"{binary} failed with exit {result.returncode}",
            data={"stderr": result.stderr.strip(), "argv": cmd},
        )
    return result.stdout


# ----------------------------------------------------------------------------
# Method handlers
# ----------------------------------------------------------------------------


@dataclass
class Limits:
    max_read_bytes: int = MAX_READ_BYTES
    max_dir_entries: int = MAX_DIR_ENTRIES
    max_find_results: int = MAX_FIND_RESULTS
    max_grep_results: int = MAX_GREP_RESULTS
    max_size_walk_entries: int = MAX_SIZE_WALK_ENTRIES
    max_deleted_results: int = MAX_DELETED_RESULTS
    max_version_hash_bytes: int = MAX_VERSION_HASH_BYTES
    max_diff_bytes: int = MAX_DIFF_BYTES
    max_top_consumers: int = MAX_TOP_CONSUMERS
    max_stale_results: int = MAX_STALE_RESULTS
    max_bisect_bytes: int = MAX_BISECT_BYTES
    max_checksum_filesize: int = MAX_CHECKSUM_FILESIZE
    zfs_timeout_seconds: float = ZFS_TIMEOUT_SECONDS
    zfs_diff_timeout_seconds: float = ZFS_DIFF_TIMEOUT_SECONDS
    size_walk_timeout_seconds: float = SIZE_WALK_TIMEOUT_SECONDS


def m_agent_info(_params: dict[str, Any]) -> dict[str, Any]:
    return {
        "agent_version": AGENT_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "methods": sorted(METHODS.keys()),
        "limits": {
            "max_read_bytes": MAX_READ_BYTES,
            "max_dir_entries": MAX_DIR_ENTRIES,
            "max_find_results": MAX_FIND_RESULTS,
            "max_grep_results": MAX_GREP_RESULTS,
            "max_size_walk_entries": MAX_SIZE_WALK_ENTRIES,
            "max_deleted_results": MAX_DELETED_RESULTS,
            "max_version_hash_bytes": MAX_VERSION_HASH_BYTES,
            "max_diff_bytes": MAX_DIFF_BYTES,
            "max_top_consumers": MAX_TOP_CONSUMERS,
            "max_stale_results": MAX_STALE_RESULTS,
            "max_bisect_bytes": MAX_BISECT_BYTES,
            "max_checksum_filesize": MAX_CHECKSUM_FILESIZE,
            "zfs_timeout_seconds": ZFS_TIMEOUT_SECONDS,
            "zfs_diff_timeout_seconds": ZFS_DIFF_TIMEOUT_SECONDS,
            "size_walk_timeout_seconds": SIZE_WALK_TIMEOUT_SECONDS,
        },
    }


def m_list_pools(_params: dict[str, Any]) -> dict[str, Any]:
    """List ZFS pools available to the agent's user."""
    out = run_zpool(["list", "-H", "-p", "-o", "name,size,allocated,free,health"])
    pools = []
    for line in out.splitlines():
        if not line:
            continue
        name, size, alloc, free, health = line.split("\t")
        pools.append(
            {
                "name": name,
                "size": _int_or_none(size),
                "allocated": _int_or_none(alloc),
                "free": _int_or_none(free),
                "health": health,
            },
        )
    return {"pools": pools}


_POOL_HEADER_RE: Final = re.compile(
    r"^\s*(pool|state|status|action|see|scan|config|errors):\s?(.*)$",
)
# Validate pool names with the same character class as datasets (no '@', no '/').
POOL_RE: Final = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:-]*$")
# `zfs get -H -p -o name,property,value,source <selector> <dataset>` rows: 4 tab cols.
_ZFS_GET_FIELDS: Final = 4
# `zpool status` config-table rows: <name>  <state>  <read>  <write>  <cksum>.
_VDEV_ROW_FIELDS: Final = 5


def validate_pool(name: str) -> str:
    if not isinstance(name, str) or not POOL_RE.match(name):
        raise InvalidParams(f"invalid pool name: {name!r}")
    return name


def m_pool_status(params: dict[str, Any]) -> dict[str, Any]:
    """Parsed ``zpool status`` for one pool or all pools.

    Returns a structured view: per-pool ``state``, ``scan`` summary, vdev
    tree with per-device error counts and depth, plus the raw multi-line
    ``status``/``action`` messages when present. This is what you call
    when ``list_pools`` shows HEALTH=DEGRADED and you want to know which
    device.

    If *pool* is omitted, returns every visible pool.
    """
    pool = params.get("pool")
    args = ["status"]
    if pool is not None:
        validate_pool(pool)
        args.append(pool)
    out = run_zpool(args)
    return {"pools": _parse_zpool_status(out)}


def _parse_zpool_status(text: str) -> list[dict[str, Any]]:
    """Parse `zpool status` output into a list of pool dicts.

    Format is human-formatted text (no parseable mode in zpool); we
    state-machine it. Pool blocks are separated by blank lines and
    begin with ``  pool: <name>``.
    """
    pools: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    section: str | None = None  # "header" | "config" | "errors"
    last_header_key: str | None = None
    config_lines: list[str] = []

    def _flush() -> None:
        nonlocal current, config_lines
        if current is None:
            return
        # Local capture so mypy keeps the narrowed type; closures can't
        # rely on nonlocal narrowing surviving any later reassignment.
        snap = current
        if config_lines:
            snap["vdevs"] = _parse_vdev_table(config_lines)
        pools.append(snap)
        current = None
        config_lines = []

    for line in text.splitlines():
        stripped = line.rstrip()
        if not stripped:
            # Blank line — only meaningful as a section terminator inside
            # the config block; otherwise just skip.
            continue
        header_match = _POOL_HEADER_RE.match(stripped)
        if header_match:
            key, value = header_match.group(1), header_match.group(2)
            if key == "pool":
                _flush()
                current = {"name": value.strip()}
                section = "header"
                last_header_key = None
                continue
            if current is None:
                continue
            if key == "config":
                section = "config"
                last_header_key = None
                continue
            if key == "errors":
                current["errors"] = value.strip()
                section = "errors"
                last_header_key = None
                continue
            # Plain header field (state/status/action/see/scan).
            current[key] = value.strip()
            last_header_key = key
            continue
        # Continuation: either a header continuation (indented) or a
        # config-table row.
        if section == "config":
            config_lines.append(line)
        elif section == "header" and last_header_key is not None:
            # section == "header" is only ever set when we've opened a
            # pool block, so current is non-None here. Assert to help mypy
            # carry the narrowing across the closure boundary.
            assert current is not None  # noqa: S101
            current[last_header_key] = (current[last_header_key] + " " + stripped.strip()).strip()
    _flush()
    return pools


def _parse_vdev_table(lines: list[str]) -> list[dict[str, Any]]:
    """Parse the indented vdev tree under ``config:`` into a flat list.

    Each row reports ``{name, state, read_errors, write_errors,
    cksum_errors, depth}`` where *depth* is the indentation level
    (0 = the pool itself, 1 = top-level vdev, 2 = device, etc.).
    """
    vdevs: list[dict[str, Any]] = []
    base_indent: int | None = None
    for raw in lines:
        if not raw.strip():
            continue
        stripped = raw.rstrip()
        leading = len(stripped) - len(stripped.lstrip())
        parts = stripped.split()
        if parts and parts[0] == "NAME":
            base_indent = leading
            continue
        if base_indent is None:
            # First non-blank, non-header row sets the baseline if there
            # was no NAME header (e.g. a vendor-specific zpool variant).
            base_indent = leading
        if len(parts) < _VDEV_ROW_FIELDS:
            continue
        depth = max(0, (leading - base_indent) // 2)
        vdevs.append(
            {
                "name": parts[0],
                "state": parts[1],
                "read_errors": _int_or_none(parts[2]),
                "write_errors": _int_or_none(parts[3]),
                "cksum_errors": _int_or_none(parts[4]),
                "depth": depth,
            },
        )
    return vdevs


def m_dataset_properties(params: dict[str, Any]) -> dict[str, Any]:
    """All ZFS properties for *dataset*, with values and sources.

    Wraps ``zfs get -H -p -o name,property,value,source all <dataset>``,
    returning every property visible to the agent's user. Use ``properties``
    (a list of names) to fetch a specific subset instead of all.

    Each entry: ``{name, value, source}`` where *source* is one of
    ``default``, ``local``, ``inherited from <dataset>``, ``received``,
    ``temporary``, ``-``.
    """
    dataset = validate_dataset(_require_str(params, "dataset"))
    selector = "all"
    requested = params.get("properties")
    if requested is not None:
        if not isinstance(requested, list) or not all(isinstance(p, str) for p in requested):
            raise InvalidParams("properties must be a list of strings or null")
        if not requested:
            raise InvalidParams("properties list must be non-empty if given")
        # Each property name is also validated through the dataset regex
        # because zfs property names are lowercase letters / digits / : / -.
        for p in requested:
            if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$", p):
                raise InvalidParams(f"invalid property name: {p!r}")
        selector = ",".join(requested)
    out = run_zfs(["get", "-H", "-p", "-o", "name,property,value,source", selector, dataset])
    properties: list[dict[str, Any]] = []
    for line in out.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < _ZFS_GET_FIELDS:
            continue
        _name, prop, value, source = parts[0], parts[1], parts[2], parts[3]
        properties.append({"name": prop, "value": value, "source": source})
    return {"dataset": dataset, "properties": properties}


def m_list_datasets(_params: dict[str, Any]) -> dict[str, Any]:
    """List filesystems and volumes (excludes snapshots)."""
    out = run_zfs(
        ["list", "-H", "-p", "-t", "filesystem,volume", "-o", "name,type,mountpoint,used,available"]
    )
    datasets = []
    for line in out.splitlines():
        if not line:
            continue
        name, dtype, mountpoint, used, avail = line.split("\t")
        datasets.append(
            {
                "name": name,
                "type": dtype,
                "mountpoint": mountpoint,
                "used": _int_or_none(used),
                "avail": _int_or_none(avail),
            }
        )
    return {"datasets": datasets}


def m_list_snapshots(params: dict[str, Any]) -> dict[str, Any]:
    """List snapshots; optionally scoped to a single dataset (recursive)."""
    dataset = params.get("dataset")
    args = ["list", "-H", "-p", "-t", "snapshot", "-o", "name,creation,used,referenced"]
    if dataset is not None:
        validate_dataset(dataset)
        args += ["-r", dataset]
    out = run_zfs(args)
    snaps = []
    for line in out.splitlines():
        if not line:
            continue
        name, creation, used, refer = line.split("\t")
        ds, snap = name.split("@", 1) if "@" in name else (name, "")
        snaps.append(
            {
                "name": name,
                "dataset": ds,
                "snap": snap,
                "creation": _int_or_none(creation),
                "used": _int_or_none(used),
                "refer": _int_or_none(refer),
            }
        )
    return {"snapshots": snaps}


_AUTOSNAP_CLASS_RE: Final = re.compile(
    r"zfs-auto-snap_(frequent|hourly|daily|weekly|monthly)\b",
)


def _snapshot_class(name: str) -> str:
    """Classify *name* (snap part of `<dataset>@<snap>`) into a retention bucket.

    Matches the conventional ``zfs-auto-snapshot`` and ``zrepl`` naming
    schemes; unknown formats are bucketed as ``"other"`` so the caller
    still sees them in the breakdown rather than silently dropping them.
    """
    m = _AUTOSNAP_CLASS_RE.search(name)
    if m:
        return m.group(1)
    lower = name.lower()
    for kw in ("frequent", "hourly", "daily", "weekly", "monthly"):
        if kw in lower:
            return kw
    return "other"


def m_snapshot_cadence(params: dict[str, Any]) -> dict[str, Any]:
    """Summary stats for the snapshot inventory of one or all datasets.

    Replaces the manual arithmetic of "I called ``list_snapshots`` and got
    58 entries; what's the cadence, what's the retention window, are
    there gaps?" with a single structured response.

    If *dataset* is omitted, summarises every snapshot the agent can see
    (i.e. across all datasets, treated as one population).
    """
    dataset = params.get("dataset")
    if dataset is not None:
        validate_dataset(dataset)
    snaps = m_list_snapshots({"dataset": dataset} if dataset else {})["snapshots"]
    by_class: dict[str, list[dict[str, Any]]] = {}
    total_used = 0
    for s in snaps:
        bucket = _snapshot_class(s["snap"])
        by_class.setdefault(bucket, []).append(s)
        if s["used"] is not None:
            total_used += s["used"]
    breakdown: list[dict[str, Any]] = []
    for cls in ("frequent", "hourly", "daily", "weekly", "monthly", "other"):
        if cls not in by_class:
            continue
        group = by_class[cls]
        creations = sorted(s["creation"] for s in group if s["creation"] is not None)
        breakdown.append(
            {
                "class": cls,
                "count": len(group),
                "earliest_creation": creations[0] if creations else None,
                "latest_creation": creations[-1] if creations else None,
                "unique_bytes": sum(s["used"] or 0 for s in group),
            },
        )
    # Overall: sorted creations across all snapshots.
    all_creations = sorted(s["creation"] for s in snaps if s["creation"] is not None)
    biggest_gap_seconds = None
    biggest_gap_between: list[str] | None = None
    # Need at least two timestamps to define a gap between them.
    if len(all_creations) >= 2:  # noqa: PLR2004
        gaps = [(all_creations[i] - all_creations[i - 1], i) for i in range(1, len(all_creations))]
        gap_seconds, gap_idx = max(gaps)
        biggest_gap_seconds = gap_seconds
        # Recover the snapshot names at the boundary.
        creation_to_name = {s["creation"]: s["name"] for s in snaps if s["creation"] is not None}
        biggest_gap_between = [
            creation_to_name[all_creations[gap_idx - 1]],
            creation_to_name[all_creations[gap_idx]],
        ]
    return {
        "dataset": dataset,
        "total_snapshots": len(snaps),
        "by_class": breakdown,
        "earliest_creation": all_creations[0] if all_creations else None,
        "latest_creation": all_creations[-1] if all_creations else None,
        "biggest_gap_seconds": biggest_gap_seconds,
        "biggest_gap_between": biggest_gap_between,
        "total_unique_bytes": total_used,
    }


def m_diff_snapshots(params: dict[str, Any]) -> dict[str, Any]:
    snap_a = _require_str(params, "snap_a")
    snap_b = _require_str(params, "snap_b")
    validate_snapshot(snap_a)
    validate_snapshot(snap_b)
    # `zfs diff` walks every changed object between the two snapshots and
    # can take minutes on a high-churn dataset; use the longer per-call
    # budget. See GH issue #7.
    out = run_zfs(["diff", "-H", "-F", snap_a, snap_b], timeout=ZFS_DIFF_TIMEOUT_SECONDS)
    changes = []
    for line in out.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < DIFF_MIN_FIELDS:
            continue
        change, ftype, path = parts[0], parts[1], parts[2]
        entry: dict[str, Any] = {"op": change, "type": ftype, "path": path}
        if change == "R" and len(parts) >= DIFF_RENAME_FIELDS:
            entry["new_path"] = parts[3]
        changes.append(entry)
    return {"snap_a": snap_a, "snap_b": snap_b, "changes": changes}


def m_list_dir(params: dict[str, Any]) -> dict[str, Any]:
    snapshot = _require_str(params, "snapshot")
    path = _require_str(params, "path")
    max_entries = validate_positive_int(
        params.get("max_entries"),
        name="max_entries",
        default=DEFAULT_DIR_ENTRIES,
        hard_max=MAX_DIR_ENTRIES,
    )
    _, target = resolve_under_snapshot(snapshot, path)
    if not target.is_dir():
        raise PathError(f"not a directory: {path!r}")
    entries: list[dict[str, Any]] = []
    truncated = False
    try:
        with os.scandir(target) as it:
            for entry in it:
                if len(entries) >= max_entries:
                    truncated = True
                    break
                entries.append(_dir_entry_info(entry))
    except OSError as e:
        raise PathError(f"could not list directory: {e}") from e
    entries.sort(key=lambda e: e["name"])
    return {
        "snapshot": snapshot,
        "path": path,
        "entries": entries,
        "truncated": truncated,
    }


def _sum_subtree(
    root: Path,
    budget: list[int],
    deadline: float,
) -> tuple[int, bool]:
    """Sum the lstat sizes of every entry under *root*.

    *budget* is a one-element list so callers can observe what's left after
    nested recursion drains it. *deadline* is a monotonic-clock cutoff.

    Symlinks are counted by their own lstat size and never followed (G3).
    Subdirectories that error on scandir are skipped silently.

    Returns ``(bytes, truncated)`` where *truncated* is true if either the
    entry budget or the wall-clock deadline ran out mid-walk.
    """
    total = 0
    truncated = False
    stack: list[Path] = [root]
    while stack:
        if budget[0] <= 0 or time.monotonic() >= deadline:
            truncated = True
            break
        current = stack.pop()
        try:
            it = os.scandir(current)
        except OSError:
            continue
        with it:
            for entry in it:
                if budget[0] <= 0 or time.monotonic() >= deadline:
                    truncated = True
                    break
                budget[0] -= 1
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                total += st.st_size
                # follow_symlinks=False means S_ISDIR is false for symlinks
                # to dirs, so this is naturally safe (G3).
                if stat_mod.S_ISDIR(st.st_mode):
                    stack.append(Path(entry.path))
    return total, truncated


def m_size_breakdown(params: dict[str, Any]) -> dict[str, Any]:
    """Recursive byte total for a snapshot dir plus per-immediate-child sizes.

    Equivalent in spirit to ``du --max-depth=1 --block-size=1 PATH``: for each
    immediate child of *path*, recursively sum lstat sizes. Symlinks are never
    followed; their own inode size is counted but they don't contribute their
    target's contents.

    Bounded by *max_entries* (default ``DEFAULT_SIZE_WALK_ENTRIES``, hard cap
    ``MAX_SIZE_WALK_ENTRIES``) and by ``SIZE_WALK_TIMEOUT_SECONDS``. Either
    limit hitting sets ``truncated: true`` on the response and on each
    affected child entry, so the caller can tell which subtree was clipped.
    """
    snapshot = _require_str(params, "snapshot")
    path = _require_str(params, "path")
    max_entries = validate_positive_int(
        params.get("max_entries"),
        name="max_entries",
        default=DEFAULT_SIZE_WALK_ENTRIES,
        hard_max=MAX_SIZE_WALK_ENTRIES,
    )
    _, target = resolve_under_snapshot(snapshot, path)
    if not target.is_dir():
        raise PathError(f"not a directory: {path!r}")
    deadline = time.monotonic() + SIZE_WALK_TIMEOUT_SECONDS
    budget = [max_entries]
    try:
        with os.scandir(target) as it:
            children = sorted(it, key=lambda e: e.name)
    except OSError as e:
        raise PathError(f"could not list directory: {e}") from e
    entries: list[dict[str, Any]] = []
    truncated = False
    total_bytes = 0
    for child in children:
        if budget[0] <= 0 or time.monotonic() >= deadline:
            truncated = True
            break
        try:
            st = child.stat(follow_symlinks=False)
        except OSError:
            continue
        budget[0] -= 1
        mode = st.st_mode
        child_bytes = st.st_size
        child_truncated = False
        if stat_mod.S_ISLNK(mode):
            kind = "symlink"
        elif stat_mod.S_ISDIR(mode):
            kind = "dir"
            sub_bytes, child_truncated = _sum_subtree(
                Path(child.path),
                budget,
                deadline,
            )
            child_bytes += sub_bytes
        elif stat_mod.S_ISREG(mode):
            kind = "file"
        else:
            kind = "other"
        entries.append(
            {
                "name": child.name,
                "type": kind,
                "bytes": child_bytes,
                "is_truncated": child_truncated,
            }
        )
        total_bytes += child_bytes
        if child_truncated:
            truncated = True
    return {
        "snapshot": snapshot,
        "path": path,
        "total_bytes": total_bytes,
        "entries": entries,
        "walked_entries": max_entries - budget[0],
        "truncated": truncated,
    }


class _TopHeap:
    """Bounded top-N heap used by :func:`m_top_consumers`.

    Stores ``(bytes, counter, payload)`` so dicts are never compared by
    heapq when two entries have the same byte size.
    """

    def __init__(self, n: int, root: Path) -> None:
        self._n = n
        self._root = root
        self._heap: list[tuple[int, int, dict[str, Any]]] = []
        self._counter = 0

    def push(self, entry_path: Path, kind: str, byte_count: int) -> None:
        try:
            rel = entry_path.relative_to(self._root)
        except ValueError:
            rel = entry_path
        item = {"path": str(rel), "type": kind, "bytes": byte_count}
        if len(self._heap) < self._n:
            heapq.heappush(self._heap, (byte_count, self._counter, item))
        elif byte_count > self._heap[0][0]:
            heapq.heapreplace(self._heap, (byte_count, self._counter, item))
        self._counter += 1

    def sorted(self) -> list[dict[str, Any]]:
        return [item for _b, _c, item in sorted(self._heap, key=lambda x: x[0], reverse=True)]


def _walk_for_top_consumers(
    dir_path: Path,
    heap: _TopHeap,
    budget: list[int],
    deadline: float,
) -> tuple[int, bool]:
    """Walk *dir_path*, push every entry into *heap*, return ``(bytes, truncated)``.

    Iterates lstat-style (G3 — never follows symlinks). The dir's own
    inode size is added to the returned subtree total to match ``du``
    semantics.
    """
    subtree_total = 0
    if budget[0] <= 0 or time.monotonic() >= deadline:
        return subtree_total, True
    try:
        it = os.scandir(dir_path)
    except OSError:
        return subtree_total, False
    truncated = False
    with it:
        for entry in it:
            if budget[0] <= 0 or time.monotonic() >= deadline:
                truncated = True
                break
            budget[0] -= 1
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            mode = st.st_mode
            if stat_mod.S_ISDIR(mode):
                sub_bytes, sub_trunc = _walk_for_top_consumers(
                    Path(entry.path),
                    heap,
                    budget,
                    deadline,
                )
                dir_total = st.st_size + sub_bytes
                heap.push(Path(entry.path), "dir", dir_total)
                subtree_total += dir_total
                if sub_trunc:
                    truncated = True
            else:
                kind = (
                    "symlink"
                    if stat_mod.S_ISLNK(mode)
                    else "file"
                    if stat_mod.S_ISREG(mode)
                    else "other"
                )
                heap.push(Path(entry.path), kind, st.st_size)
                subtree_total += st.st_size
    return subtree_total, truncated


def m_top_consumers(params: dict[str, Any]) -> dict[str, Any]:
    """Top-N largest files and directories within a snapshot subtree.

    Walks the subtree bounded by ``max_entries`` (same as ``size_breakdown``)
    and keeps a heap of the *N* largest entries seen. For directories, the
    reported ``bytes`` is the recursive subtree sum (so the result is like
    ``du -ab | sort -rn | head -n``, minus the request path itself).

    Use after ``size_breakdown`` once you've identified a big subtree:
    ``size_breakdown`` says "here's how the children split"; this says
    "here are the specific files and dirs hogging space, ranked".
    Symlinks are counted by their own lstat size and never followed (G3).
    """
    snapshot = _require_str(params, "snapshot")
    path = _require_str(params, "path")
    n = validate_positive_int(
        params.get("n"),
        name="n",
        default=DEFAULT_TOP_CONSUMERS,
        hard_max=MAX_TOP_CONSUMERS,
    )
    max_entries = validate_positive_int(
        params.get("max_entries"),
        name="max_entries",
        default=DEFAULT_SIZE_WALK_ENTRIES,
        hard_max=MAX_SIZE_WALK_ENTRIES,
    )
    _, target = resolve_under_snapshot(snapshot, path)
    if not target.is_dir():
        raise PathError(f"not a directory: {path!r}")
    deadline = time.monotonic() + SIZE_WALK_TIMEOUT_SECONDS
    budget = [max_entries]
    heap = _TopHeap(n, target.resolve())
    _bytes, truncated = _walk_for_top_consumers(target, heap, budget, deadline)
    return {
        "snapshot": snapshot,
        "path": path,
        "entries": heap.sorted(),
        "walked_entries": max_entries - budget[0],
        "truncated": truncated,
    }


def m_stale_snapshots(params: dict[str, Any]) -> dict[str, Any]:
    """Snapshots older than ``older_than``, sorted by unique bytes used.

    Resolves ``older_than`` (ISO 8601 or a human phrase like
    ``"6 months ago"``) to a Unix timestamp and returns the snapshots
    whose creation predates it. Sort order is descending ``used`` —
    the biggest-by-unique-bytes appear first, which is what you want
    when deciding what to cull.

    Scoped to *dataset* if given, else covers every visible snapshot.
    Bounded by ``max_results`` (default 1000, capped at 10 000).
    """
    dataset = params.get("dataset")
    if dataset is not None:
        validate_dataset(dataset)
    older_than = _require_str(params, "older_than")
    older_than_ts = _iso_to_ts(older_than, name="older_than")
    if older_than_ts is None:
        raise InvalidParams("older_than must be a non-null ISO 8601 string")
    max_results = validate_positive_int(
        params.get("max_results"),
        name="max_results",
        default=DEFAULT_STALE_RESULTS,
        hard_max=MAX_STALE_RESULTS,
    )
    snaps = m_list_snapshots({"dataset": dataset} if dataset else {})["snapshots"]
    stale = [s for s in snaps if s["creation"] is not None and s["creation"] < older_than_ts]
    stale.sort(key=lambda s: s["used"] or 0, reverse=True)
    truncated = len(stale) > max_results
    return {
        "dataset": dataset,
        "older_than_unix": older_than_ts,
        "snapshots": stale[:max_results],
        "truncated": truncated,
    }


# `bisect_change` predicate kinds. Validated at the dispatch boundary so a
# malformed predicate fails fast rather than mid-search.
_PREDICATE_KINDS: Final = frozenset(
    {
        "exists",
        "contains",
        "sha256_equals",
        "size_at_least",
    }
)


def _validate_predicate(pred: object) -> dict[str, Any]:
    if not isinstance(pred, dict) or "kind" not in pred:
        raise InvalidParams("predicate must be a dict with a 'kind' field")
    kind = pred["kind"]
    if kind not in _PREDICATE_KINDS:
        raise InvalidParams(
            f"predicate kind must be one of {sorted(_PREDICATE_KINDS)}, got {kind!r}",
        )
    if kind == "contains":
        if not isinstance(pred.get("needle"), str) or not pred["needle"]:
            raise InvalidParams("predicate 'contains' requires non-empty 'needle' string")
    elif kind == "sha256_equals":
        h = pred.get("hash")
        if not isinstance(h, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", h):
            raise InvalidParams("predicate 'sha256_equals' requires 64-hex-char 'hash'")
    elif kind == "size_at_least":
        size = pred.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise InvalidParams("predicate 'size_at_least' requires non-negative int 'size'")
    return pred


def _stat_regular_in_snapshot(snap: str, path: str) -> os.stat_result | None:
    """lstat *path* in *snap*; return the stat result iff it's a regular file."""
    try:
        _, target = resolve_under_snapshot(snap, path)
        st = target.lstat()
    except (PathError, ZfsError, OSError):
        return None
    if not stat_mod.S_ISREG(st.st_mode):
        return None
    return st


def _eval_predicate(snap: str, path: str, pred: dict[str, Any], max_bytes: int) -> bool:
    """Evaluate *pred* against the version of *path* in *snap*."""
    kind = pred["kind"]
    if kind == "exists":
        return _stat_regular_in_snapshot(snap, path) is not None
    if kind == "size_at_least":
        st = _stat_regular_in_snapshot(snap, path)
        return st is not None and st.st_size >= pred["size"]
    # Both 'contains' and 'sha256_equals' need bytes.
    side = _read_for_diff(snap, path, max_bytes)
    data = side["data"]
    if data is None:
        return False
    if kind == "contains":
        return pred["needle"].encode("utf-8") in data
    if kind == "sha256_equals":
        expected_hash: str = pred["hash"]
        return hashlib.sha256(data).hexdigest().lower() == expected_hash.lower()
    raise InvalidParams(f"unknown predicate kind: {kind!r}")


def m_bisect_change(params: dict[str, Any]) -> dict[str, Any]:
    """Binary-search snapshots of *dataset* for the snapshot where the
    predicate against *path* flips its value.

    Evaluates the predicate at the earliest and latest snapshots; if
    they agree, returns ``transition: null`` (no flip in window). If
    they disagree, performs a binary search, calling the predicate
    O(log N) times, and returns the snapshot pair on either side of
    the transition.

    Useful for "when did /etc/foo.conf first contain BUG?" or "when
    did the file first exceed 100 KB?". Predicate shapes:

    - ``{"kind": "exists"}`` — file is a regular file
    - ``{"kind": "contains", "needle": "..."}`` — UTF-8 substring in first ``max_bytes``
    - ``{"kind": "sha256_equals", "hash": "<64-hex>"}`` — SHA-256 of first ``max_bytes``
    - ``{"kind": "size_at_least", "size": N}`` — file size >= N

    Bisect assumes the predicate is *monotonic* across the snapshot
    sequence (flips at most once). If it isn't, the returned transition
    is one of possibly many; the result is well-defined but may not be
    the "right" one for the caller's intent.
    """
    dataset = validate_dataset(_require_str(params, "dataset"))
    path = _require_str(params, "path")
    predicate = _validate_predicate(params.get("predicate"))
    max_bytes = validate_positive_int(
        params.get("max_bytes"),
        name="max_bytes",
        default=DEFAULT_BISECT_BYTES,
        hard_max=MAX_BISECT_BYTES,
    )
    # list_snapshots with -r returns descendants too, but a sub-dataset's
    # snapshot won't contain the requested path (different mount tree), so
    # the predicate would oscillate and the monotonicity assumption breaks.
    # Limit to snapshots of the exact dataset.
    snaps = [
        s
        for s in m_list_snapshots({"dataset": dataset})["snapshots"]
        if s["creation"] is not None and s["dataset"] == dataset
    ]
    snaps.sort(key=lambda s: s["creation"])
    if len(snaps) < 2:  # noqa: PLR2004
        return {
            "dataset": dataset,
            "path": path,
            "predicate": predicate,
            "transition": None,
            "evaluated_snapshots": 0,
            "total_snapshots": len(snaps),
            "reason": "need at least two snapshots to bisect",
        }
    evaluated = 0
    earliest_val = _eval_predicate(snaps[0]["name"], path, predicate, max_bytes)
    evaluated += 1
    latest_val = _eval_predicate(snaps[-1]["name"], path, predicate, max_bytes)
    evaluated += 1
    if earliest_val == latest_val:
        return {
            "dataset": dataset,
            "path": path,
            "predicate": predicate,
            "transition": None,
            "earliest_value": earliest_val,
            "latest_value": latest_val,
            "evaluated_snapshots": evaluated,
            "total_snapshots": len(snaps),
            "reason": "predicate has the same value at both ends of the window",
        }
    # Bisect: find smallest index where eval == latest_val.
    lo, hi = 0, len(snaps) - 1
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        mid_val = _eval_predicate(snaps[mid]["name"], path, predicate, max_bytes)
        evaluated += 1
        if mid_val == latest_val:
            hi = mid
        else:
            lo = mid
    return {
        "dataset": dataset,
        "path": path,
        "predicate": predicate,
        "transition": {
            "from_snapshot": snaps[lo]["name"],
            "from_creation": snaps[lo]["creation"],
            "from_value": earliest_val,
            "to_snapshot": snaps[hi]["name"],
            "to_creation": snaps[hi]["creation"],
            "to_value": latest_val,
            "transition_seconds": snaps[hi]["creation"] - snaps[lo]["creation"],
        },
        "evaluated_snapshots": evaluated,
        "total_snapshots": len(snaps),
    }


def m_read_file(params: dict[str, Any]) -> dict[str, Any]:
    snapshot = _require_str(params, "snapshot")
    path = _require_str(params, "path")
    max_bytes = validate_positive_int(
        params.get("max_bytes"),
        name="max_bytes",
        default=DEFAULT_READ_BYTES,
        hard_max=MAX_READ_BYTES,
    )
    _, target = resolve_under_snapshot(snapshot, path)
    # Refuse to follow symlinks (G3): if the *requested* path is a symlink
    # we report it but never open it. resolve_under_snapshot already
    # canonicalised, so we check the target's actual type via lstat.
    try:
        st = target.lstat()
    except OSError as e:
        raise PathError(f"could not stat: {e}") from e
    if stat_mod.S_ISLNK(st.st_mode):
        raise PathError(f"refusing to read symlink: {path!r}")
    if not stat_mod.S_ISREG(st.st_mode):
        raise PathError(f"not a regular file: {path!r}")
    size = st.st_size
    try:
        with target.open("rb") as fh:
            data = fh.read(max_bytes)
    except OSError as e:
        raise PathError(f"could not read: {e}") from e
    truncated = size > len(data)
    # Try UTF-8 first; fall back to base64 for binary content.
    try:
        text = data.decode("utf-8")
        encoding = "utf-8"
        content = text
    except UnicodeDecodeError:
        encoding = "base64"
        content = base64.b64encode(data).decode("ascii")
    return {
        "snapshot": snapshot,
        "path": path,
        "size": size,
        "bytes_returned": len(data),
        "encoding": encoding,
        "content": content,
        "truncated": truncated,
    }


def m_find_files(params: dict[str, Any]) -> dict[str, Any]:
    snapshot = _require_str(params, "snapshot")
    pattern = _require_str(params, "pattern")
    base = params.get("path", "")
    max_results = validate_positive_int(
        params.get("max_results"),
        name="max_results",
        default=DEFAULT_FIND_RESULTS,
        hard_max=MAX_FIND_RESULTS,
    )
    root, target = _resolve_or_root(snapshot, base)
    if not target.is_dir():
        raise PathError(f"not a directory: {base!r}")
    matches: list[dict[str, Any]] = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(target, followlinks=False):
        for name in [*dirnames, *filenames]:
            if fnmatch.fnmatch(name, pattern):
                full = Path(dirpath) / name
                rel = full.relative_to(root)
                matches.append({"path": str(rel), "name": name})
                if len(matches) >= max_results:
                    truncated = True
                    break
        if truncated:
            break
    return {
        "snapshot": snapshot,
        "pattern": pattern,
        "base": base,
        "matches": matches,
        "truncated": truncated,
    }


def m_content_grep(params: dict[str, Any]) -> dict[str, Any]:
    snapshot = _require_str(params, "snapshot")
    pattern = _require_str(params, "pattern")
    base = params.get("path", "")
    max_results = validate_positive_int(
        params.get("max_results"),
        name="max_results",
        default=DEFAULT_GREP_RESULTS,
        hard_max=MAX_GREP_RESULTS,
    )
    try:
        rx = re.compile(pattern)
    except re.error as e:
        raise InvalidParams(f"invalid regex: {e}") from e
    root, target = _resolve_or_root(snapshot, base)
    if not target.is_dir() and not target.is_file():
        raise PathError(f"not a file or directory: {base!r}")
    matches: list[dict[str, Any]] = []
    truncated = False
    files: list[Path] = [target] if target.is_file() else []
    if target.is_dir():
        for dirpath, _dirnames, filenames in os.walk(target, followlinks=False):
            for name in filenames:
                files.append(Path(dirpath) / name)
    for f in files:
        if truncated:
            break
        try:
            with f.open("rb") as fh:
                for lineno, raw in enumerate(fh, start=1):
                    try:
                        line = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        break  # skip binary file
                    if rx.search(line):
                        matches.append(
                            {
                                "path": str(f.relative_to(root)),
                                "line": lineno,
                                "text": line.rstrip("\n"),
                            }
                        )
                        if len(matches) >= max_results:
                            truncated = True
                            break
        except OSError:
            continue
    return {
        "snapshot": snapshot,
        "pattern": pattern,
        "base": base,
        "matches": matches,
        "truncated": truncated,
    }


def m_file_history(params: dict[str, Any]) -> dict[str, Any]:
    """For each snapshot of *dataset*, report whether *path* exists and its size/mtime."""
    dataset = validate_dataset(_require_str(params, "dataset"))
    path = _require_str(params, "path")
    rel = validate_user_path(path)
    snaps = m_list_snapshots({"dataset": dataset})["snapshots"]
    versions = []
    for snap_meta in snaps:
        snap_full = snap_meta["name"]
        try:
            _, target = resolve_under_snapshot(snap_full, str(rel))
        except (PathError, ZfsError):
            versions.append(
                {"snapshot": snap_full, "creation": snap_meta["creation"], "present": False}
            )
            continue
        try:
            st = target.lstat()
            versions.append(
                {
                    "snapshot": snap_full,
                    "creation": snap_meta["creation"],
                    "present": True,
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                    "is_symlink": _is_link(st),
                }
            )
        except FileNotFoundError:
            versions.append(
                {"snapshot": snap_full, "creation": snap_meta["creation"], "present": False}
            )
    return {"dataset": dataset, "path": path, "versions": versions}


def m_snapshots_containing(params: dict[str, Any]) -> dict[str, Any]:
    """Return the subset of snapshots of *dataset* in which *path* exists."""
    dataset = validate_dataset(_require_str(params, "dataset"))
    path = _require_str(params, "path")
    after = params.get("after")  # ISO8601 -> epoch comparison done client-side
    before = params.get("before")
    after_ts = _iso_to_ts(after, name="after")
    before_ts = _iso_to_ts(before, name="before")
    history = m_file_history({"dataset": dataset, "path": path})["versions"]
    hits = [
        v
        for v in history
        if v["present"]
        and (after_ts is None or (v["creation"] is not None and v["creation"] >= after_ts))
        and (before_ts is None or (v["creation"] is not None and v["creation"] <= before_ts))
    ]
    return {"dataset": dataset, "path": path, "snapshots": hits}


def m_first_appearance(params: dict[str, Any]) -> dict[str, Any]:
    """Return the earliest snapshot of *dataset* containing *path*, or null."""
    dataset = validate_dataset(_require_str(params, "dataset"))
    path = _require_str(params, "path")
    history = m_file_history({"dataset": dataset, "path": path})["versions"]
    present = sorted(
        (v for v in history if v["present"] and v["creation"] is not None),
        key=lambda v: v["creation"],
    )
    return {
        "dataset": dataset,
        "path": path,
        "first": present[0] if present else None,
    }


def m_last_appearance(params: dict[str, Any]) -> dict[str, Any]:
    """Return the *latest* snapshot of *dataset* containing *path*, or null.

    Mirror of :func:`m_first_appearance`. Useful for answering "when did
    this file disappear?" — compare the result to the dataset's most
    recent snapshot.
    """
    dataset = validate_dataset(_require_str(params, "dataset"))
    path = _require_str(params, "path")
    history = m_file_history({"dataset": dataset, "path": path})["versions"]
    present = sorted(
        (v for v in history if v["present"] and v["creation"] is not None),
        key=lambda v: v["creation"],
    )
    return {
        "dataset": dataset,
        "path": path,
        "last": present[-1] if present else None,
    }


def m_file_diff(params: dict[str, Any]) -> dict[str, Any]:
    """Unified diff of *path* between two snapshots, ``snap_a`` -> ``snap_b``.

    Each side is read up to ``max_bytes`` (default 1 MiB; capped at
    ``MAX_DIFF_BYTES`` = 4 MiB). If a side is missing, the diff shows the
    full added/removed content. If either side is non-UTF-8, the diff is
    empty and ``encoding`` is reported as ``"binary"`` — the response
    still tells you whether the contents are identical (by SHA-256) and
    the two sizes, so a binary "did anything change?" question is
    answerable without the textual diff.
    """
    snap_a = _require_str(params, "snap_a")
    snap_b = _require_str(params, "snap_b")
    path = _require_str(params, "path")
    max_bytes = validate_positive_int(
        params.get("max_bytes"),
        name="max_bytes",
        default=DEFAULT_DIFF_BYTES,
        hard_max=MAX_DIFF_BYTES,
    )
    validate_snapshot(snap_a)
    validate_snapshot(snap_b)
    side_a = _read_for_diff(snap_a, path, max_bytes)
    side_b = _read_for_diff(snap_b, path, max_bytes)
    # Identical when both present and bytes match, OR both missing.
    if side_a["data"] is None and side_b["data"] is None:
        identical = True
        encoding = "missing"
        diff_text = ""
    else:
        identical = (
            side_a["data"] is not None
            and side_b["data"] is not None
            and side_a["data"] == side_b["data"]
        )
        text_a = _try_decode(side_a["data"])
        text_b = _try_decode(side_b["data"])
        if text_a is None or text_b is None:
            encoding = "binary"
            diff_text = ""
        else:
            encoding = "utf-8"
            diff_text = "".join(
                difflib.unified_diff(
                    text_a.splitlines(keepends=True),
                    text_b.splitlines(keepends=True),
                    fromfile=f"a/{path}",
                    tofile=f"b/{path}",
                ),
            )
    return {
        "snap_a": snap_a,
        "snap_b": snap_b,
        "path": path,
        "present_in_a": side_a["data"] is not None,
        "present_in_b": side_b["data"] is not None,
        "size_a": side_a["size"],
        "size_b": side_b["size"],
        "truncated_a": side_a["truncated"],
        "truncated_b": side_b["truncated"],
        "identical": identical,
        "encoding": encoding,
        "diff": diff_text,
    }


def m_find_deleted(params: dict[str, Any]) -> dict[str, Any]:
    """Paths deleted between an earlier snapshot in a window and a later one.

    Selects ``from_snapshot`` as the earliest snapshot of *dataset* at or
    after the ``after`` time (or the earliest overall if ``after`` is
    omitted). Selects ``to_snapshot`` as the latest snapshot at or before
    ``before`` (or the latest overall). Then runs ``zfs diff`` between the
    two and returns the entries with op ``-`` (removed).

    Useful for "what was deleted on this dataset since yesterday?" without
    having to call :func:`m_diff_snapshots` and manually filter the result.
    """
    dataset = validate_dataset(_require_str(params, "dataset"))
    after = params.get("after")
    before = params.get("before")
    after_ts = _iso_to_ts(after, name="after")
    before_ts = _iso_to_ts(before, name="before")
    max_results = validate_positive_int(
        params.get("max_results"),
        name="max_results",
        default=DEFAULT_DELETED_RESULTS,
        hard_max=MAX_DELETED_RESULTS,
    )
    snaps = m_list_snapshots({"dataset": dataset})["snapshots"]
    in_window = [
        s
        for s in snaps
        if (after_ts is None or s["creation"] >= after_ts)
        and (before_ts is None or s["creation"] <= before_ts)
    ]
    if not in_window:
        return {
            "dataset": dataset,
            "from_snapshot": None,
            "to_snapshot": None,
            "deleted": [],
            "truncated": False,
        }
    from_snap = min(in_window, key=lambda s: s["creation"])
    to_snap = max(in_window, key=lambda s: s["creation"])
    if from_snap["name"] == to_snap["name"]:
        # Single-snapshot window: nothing to diff against.
        return {
            "dataset": dataset,
            "from_snapshot": from_snap["name"],
            "to_snapshot": to_snap["name"],
            "deleted": [],
            "truncated": False,
        }
    diff = m_diff_snapshots({"snap_a": from_snap["name"], "snap_b": to_snap["name"]})
    deleted: list[dict[str, Any]] = []
    truncated = False
    for change in diff["changes"]:
        if change["op"] != "-":
            continue
        if len(deleted) >= max_results:
            truncated = True
            break
        deleted.append({"path": change["path"], "type": change["type"]})
    return {
        "dataset": dataset,
        "from_snapshot": from_snap["name"],
        "to_snapshot": to_snap["name"],
        "deleted": deleted,
        "truncated": truncated,
    }


def m_versions_of(params: dict[str, Any]) -> dict[str, Any]:
    """List *distinct* versions of *path* across every snapshot of *dataset*.

    Like :func:`m_file_history` but deduplicated by content hash. On a
    daily-snapshot dataset where a file rarely changes this collapses
    "365 entries, mostly identical" into "5 distinct versions, here's
    when each appeared".

    Content is hashed (SHA-256) up to ``max_bytes`` per version (default
    1 MiB; capped at 4 MiB). Files larger than ``max_bytes`` are
    fingerprinted by their prefix only — the per-version ``truncated``
    flag is set so callers know two versions with the same prefix-hash
    may actually differ past the cap.
    """
    dataset = validate_dataset(_require_str(params, "dataset"))
    path = _require_str(params, "path")
    max_bytes = validate_positive_int(
        params.get("max_bytes"),
        name="max_bytes",
        default=DEFAULT_VERSION_HASH_BYTES,
        hard_max=MAX_VERSION_HASH_BYTES,
    )
    history = m_file_history({"dataset": dataset, "path": path})["versions"]
    versions: list[dict[str, Any]] = []
    seen: dict[str, int] = {}  # sha256 -> index in `versions`
    any_truncated = False
    for v in history:
        if not v["present"] or v.get("is_symlink"):
            continue
        snap = v["snapshot"]
        side = _read_for_diff(snap, path, max_bytes)
        if side["data"] is None:
            continue
        digest = hashlib.sha256(side["data"]).hexdigest()
        ref = {"snapshot": snap, "creation": v["creation"]}
        if digest in seen:
            versions[seen[digest]]["snapshots"].append(ref)
        else:
            seen[digest] = len(versions)
            versions.append(
                {
                    "sha256": digest,
                    "size": side["size"],
                    "truncated": side["truncated"],
                    "first_seen": ref,
                    "last_seen": ref,
                    "snapshots": [ref],
                }
            )
        if side["truncated"]:
            any_truncated = True
    # Sort each version's snapshot list, set first/last by creation time.
    for version in versions:
        version["snapshots"].sort(key=lambda s: s["creation"] or 0)
        if version["snapshots"]:
            version["first_seen"] = version["snapshots"][0]
            version["last_seen"] = version["snapshots"][-1]
    versions.sort(key=lambda x: x["first_seen"]["creation"] or 0)
    return {
        "dataset": dataset,
        "path": path,
        "versions": versions,
        "truncated": any_truncated,
    }


def m_size_delta(params: dict[str, Any]) -> dict[str, Any]:
    """Return the bytes written between *snap_a* and *snap_b* of the same dataset."""
    snap_a = _require_str(params, "snap_a")
    snap_b = _require_str(params, "snap_b")
    ds_a, _ = validate_snapshot(snap_a)
    ds_b, _ = validate_snapshot(snap_b)
    if ds_a != ds_b:
        raise InvalidParams("snap_a and snap_b must belong to the same dataset")
    out = run_zfs(["get", "-H", "-p", "-o", "value", f"written@{snap_a.split('@', 1)[1]}", snap_b])
    written = _int_or_none(out.strip())
    return {"snap_a": snap_a, "snap_b": snap_b, "written_bytes": written}


def m_checksum_file(params: dict[str, Any]) -> dict[str, Any]:
    """SHA-256 of the full content of *path* in *snapshot*, streamed in chunks.

    Unlike ``read_file`` and ``versions_of``, there is no ``max_bytes``
    truncation: the entire file is hashed. Use this to verify that a
    recovered copy matches the snapshot original without transferring the
    content.

    Hard cap: ``MAX_CHECKSUM_FILESIZE`` (256 MiB). Files larger than this
    should be checksummed out-of-band. Symlinks are refused (G3).

    Returns ``{snapshot, path, size, sha256}``.
    """
    snapshot = _require_str(params, "snapshot")
    path = _require_str(params, "path")
    _, target = resolve_under_snapshot(snapshot, path)
    try:
        st = target.lstat()
    except OSError as e:
        raise PathError(f"could not stat: {e}") from e
    if stat_mod.S_ISLNK(st.st_mode):
        raise PathError(f"refusing to read symlink: {path!r}")
    if not stat_mod.S_ISREG(st.st_mode):
        raise PathError(f"not a regular file: {path!r}")
    size = st.st_size
    if size > MAX_CHECKSUM_FILESIZE:
        raise InvalidParams(
            f"file too large to checksum via this tool: {size} bytes "
            f"(limit {MAX_CHECKSUM_FILESIZE}); checksum it out-of-band"
        )
    h = hashlib.sha256()
    try:
        with target.open("rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                h.update(chunk)
    except OSError as e:
        raise PathError(f"could not read: {e}") from e
    return {
        "snapshot": snapshot,
        "path": path,
        "size": size,
        "sha256": h.hexdigest(),
    }


# ----------------------------------------------------------------------------
# Method allowlist (defence in depth: G1)
# ----------------------------------------------------------------------------

METHODS: Final[dict[str, Any]] = {
    "agent_info": m_agent_info,
    "list_pools": m_list_pools,
    "pool_status": m_pool_status,
    "list_datasets": m_list_datasets,
    "dataset_properties": m_dataset_properties,
    "list_snapshots": m_list_snapshots,
    "snapshot_cadence": m_snapshot_cadence,
    "diff_snapshots": m_diff_snapshots,
    "list_dir": m_list_dir,
    "size_breakdown": m_size_breakdown,
    "top_consumers": m_top_consumers,
    "read_file": m_read_file,
    "find_files": m_find_files,
    "content_grep": m_content_grep,
    "file_history": m_file_history,
    "versions_of": m_versions_of,
    "file_diff": m_file_diff,
    "snapshots_containing": m_snapshots_containing,
    "first_appearance": m_first_appearance,
    "last_appearance": m_last_appearance,
    "find_deleted": m_find_deleted,
    "bisect_change": m_bisect_change,
    "stale_snapshots": m_stale_snapshots,
    "size_delta": m_size_delta,
    "checksum_file": m_checksum_file,
}


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------


def _require_str(params: dict[str, Any], key: str) -> str:
    if key not in params:
        raise InvalidParams(f"missing required parameter: {key!r}")
    value = params[key]
    if not isinstance(value, str):
        raise InvalidParams(f"parameter {key!r} must be a string, got {type(value).__name__}")
    return value


def _int_or_none(s: str) -> int | None:
    if s in ("", "-"):
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _is_link(st: os.stat_result) -> bool:
    return stat_mod.S_ISLNK(st.st_mode)


def _resolve_or_root(snapshot: str, base: str) -> tuple[Path, Path]:
    """Return ``(root, target)``; *target* is *root* when *base* is empty."""
    if base:
        return resolve_under_snapshot(snapshot, base)
    root = snapshot_root(snapshot).resolve()
    return root, root


def _dir_entry_info(entry: os.DirEntry[str]) -> dict[str, Any]:
    try:
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return {"name": entry.name, "type": "unknown"}
    mode = st.st_mode
    if stat_mod.S_ISLNK(mode):
        try:
            target = str(Path(entry.path).readlink())
        except OSError:
            target = ""
        return {
            "name": entry.name,
            "type": "symlink",
            "target": target,
            "size": st.st_size,
            "mtime": int(st.st_mtime),
        }
    if stat_mod.S_ISDIR(mode):
        return {"name": entry.name, "type": "dir", "mtime": int(st.st_mtime)}
    if stat_mod.S_ISREG(mode):
        return {
            "name": entry.name,
            "type": "file",
            "size": st.st_size,
            "mtime": int(st.st_mtime),
            "mode": stat_mod.S_IMODE(mode),
        }
    return {"name": entry.name, "type": "other", "mode": stat_mod.S_IMODE(mode)}


def _iso_to_ts(value: object, *, name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidParams(f"{name} must be an ISO 8601 string or null")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as e:
        raise InvalidParams(f"{name} is not a valid ISO 8601 timestamp: {e}") from e
    return int(dt.timestamp())


def _read_for_diff(snapshot: str, path: str, max_bytes: int) -> dict[str, Any]:
    """Read *path* from *snapshot* up to *max_bytes*.

    Returns ``{data, size, truncated}``. ``data`` is ``None`` if the path
    doesn't exist, is a symlink, or isn't a regular file — the caller
    decides how to render those cases. Symlinks are never followed (G3);
    a path that resolves to a symlink is treated the same as missing for
    diff purposes (we can't meaningfully diff a link target the caller
    didn't ask about).
    """
    try:
        _, target = resolve_under_snapshot(snapshot, path)
    except (PathError, ZfsError):
        return {"data": None, "size": None, "truncated": False}
    try:
        st = target.lstat()
    except OSError:
        return {"data": None, "size": None, "truncated": False}
    if not stat_mod.S_ISREG(st.st_mode):
        return {"data": None, "size": None, "truncated": False}
    try:
        with target.open("rb") as fh:
            data = fh.read(max_bytes)
    except OSError:
        return {"data": None, "size": None, "truncated": False}
    return {"data": data, "size": st.st_size, "truncated": st.st_size > len(data)}


def _try_decode(data: bytes | None) -> str | None:
    """Decode *data* as UTF-8 or return None on failure (or None input)."""
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


# ----------------------------------------------------------------------------
# JSON-RPC framing and dispatch
# ----------------------------------------------------------------------------


def make_error(
    req_id: JsonId, code: int, message: str, data: object | None = None
) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def make_result(req_id: JsonId, result: object) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _dispatch(req: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a parsed JSON-RPC request object to its handler."""
    req_id = req.get("id")
    method = req.get("method")
    params = req.get("params", {})
    if not isinstance(method, str):
        return make_error(req_id, INVALID_REQUEST, "missing or non-string 'method'")
    if not isinstance(params, dict):
        return make_error(req_id, INVALID_PARAMS, "'params' must be a JSON object")
    handler = METHODS.get(method)
    if handler is None:
        return make_error(req_id, METHOD_NOT_FOUND, f"unknown method: {method!r}")
    try:
        result = handler(params)
    except AgentError as e:
        return make_error(req_id, e.code, str(e), e.data)
    except Exception as e:  # last-resort guard at the wire boundary
        log.exception("unhandled exception in %s", method)
        return make_error(req_id, INTERNAL_ERROR, f"internal error: {e}")
    return make_result(req_id, result)


def handle_request(raw: str) -> dict[str, Any] | None:
    """Parse and dispatch one JSON-RPC request line.

    Returns the response dict, or ``None`` for notifications (no ``id``).
    """
    try:
        req = json.loads(raw)
    except json.JSONDecodeError as e:
        return make_error(None, PARSE_ERROR, f"parse error: {e}")
    if not isinstance(req, dict):
        return make_error(None, INVALID_REQUEST, "request must be a JSON object")
    is_notification = "id" not in req
    response = _dispatch(req)
    return None if is_notification else response


def main() -> int:
    """Read NDJSON requests from stdin, write responses to stdout."""
    # Default SIGPIPE behaviour so we exit quietly when the peer goes away.
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    log.info(
        "zfs-snoop-agent %s (protocol %s) ready as uid=%d",
        AGENT_VERSION,
        PROTOCOL_VERSION,
        os.geteuid(),
    )
    for line in sys.stdin:
        if not line.strip():
            continue
        response = handle_request(line)
        if response is None:
            continue
        sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
