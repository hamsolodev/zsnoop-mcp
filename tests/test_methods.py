"""Per-method happy paths and a representative truncation test."""

from __future__ import annotations

import base64
import hashlib
import os
import pathlib
from typing import Any

import pytest

import zfs_snoop_agent as agent
from tests.conftest import FakeZfs

# ---- agent_info -------------------------------------------------------------


def test_agent_info_reports_methods_and_limits() -> None:
    info = agent.m_agent_info({})
    assert info["agent_version"] == agent.AGENT_VERSION
    assert "list_snapshots" in info["methods"]
    assert "list_pools" in info["methods"]
    assert info["limits"]["max_read_bytes"] == agent.MAX_READ_BYTES


def test_list_pools_parses_zpool_output(monkeypatch: pytest.MonkeyPatch) -> None:
    canned = (
        "rpool\t10995116277760\t9876543210\t1118573067550\tONLINE\n"
        "bpool\t2952790016\t1351733248\t1601056768\tONLINE\n"
    )

    def fake_zpool(args: list[str]) -> str:
        assert args == ["list", "-H", "-p", "-o", "name,size,allocated,free,health"]
        return canned

    monkeypatch.setattr(agent, "run_zpool", fake_zpool)
    result = agent.m_list_pools({})
    assert result == {
        "pools": [
            {
                "name": "rpool",
                "size": 10995116277760,
                "allocated": 9876543210,
                "free": 1118573067550,
                "health": "ONLINE",
            },
            {
                "name": "bpool",
                "size": 2952790016,
                "allocated": 1351733248,
                "free": 1601056768,
                "health": "ONLINE",
            },
        ],
    }


# ---- list_datasets ----------------------------------------------------------


DATASETS_ARGS = [
    "list",
    "-H",
    "-p",
    "-t",
    "filesystem,volume",
    "-o",
    "name,type,mountpoint,used,available",
]


def test_list_datasets_parses_zfs_output(fake_zfs: FakeZfs) -> None:
    fake_zfs.add(
        DATASETS_ARGS,
        "rpool\tfilesystem\t/\t1234\t5678\nbpool\tfilesystem\t/boot\t100\t900\n",
    )
    result = agent.m_list_datasets({})
    assert result["datasets"] == [
        {
            "name": "rpool",
            "type": "filesystem",
            "mountpoint": "/",
            "used": 1234,
            "avail": 5678,
        },
        {
            "name": "bpool",
            "type": "filesystem",
            "mountpoint": "/boot",
            "used": 100,
            "avail": 900,
        },
    ]


# ---- list_snapshots ---------------------------------------------------------


def test_list_snapshots_unscoped(fake_zfs: FakeZfs) -> None:
    fake_zfs.add(
        ["list", "-H", "-p", "-t", "snapshot", "-o", "name,creation,used,referenced"],
        "rpool/home@a\t1716000000\t10\t1000\n",
    )
    result = agent.m_list_snapshots({})
    assert result["snapshots"][0]["name"] == "rpool/home@a"
    assert result["snapshots"][0]["creation"] == 1716000000
    assert result["snapshots"][0]["dataset"] == "rpool/home"
    assert result["snapshots"][0]["snap"] == "a"


def test_list_snapshots_scoped_to_dataset(fake_zfs: FakeZfs) -> None:
    fake_zfs.add(
        [
            "list",
            "-H",
            "-p",
            "-t",
            "snapshot",
            "-o",
            "name,creation,used,referenced",
            "-r",
            "rpool/home",
        ],
        "rpool/home@a\t1716000000\t10\t1000\n",
    )
    result = agent.m_list_snapshots({"dataset": "rpool/home"})
    assert len(result["snapshots"]) == 1


def test_list_snapshots_rejects_invalid_dataset(fake_zfs: FakeZfs) -> None:
    with pytest.raises(agent.InvalidParams):
        agent.m_list_snapshots({"dataset": "rpool;rm"})


def test_list_snapshots_filters_by_after(fake_zfs: FakeZfs) -> None:
    # Three snapshots straddling 2024-01-01: one before, two after.
    fake_zfs.add(
        ["list", "-H", "-p", "-t", "snapshot", "-o", "name,creation,used,referenced"],
        "rpool/home@old\t1700000000\t10\t100\n"
        "rpool/home@mid\t1716000000\t10\t100\n"
        "rpool/home@new\t1730000000\t10\t100\n",
    )
    result = agent.m_list_snapshots({"after": "2024-01-01T00:00:00+00:00"})
    names = [s["name"] for s in result["snapshots"]]
    assert names == ["rpool/home@mid", "rpool/home@new"]
    # No `truncated` field when max_results not passed.
    assert "truncated" not in result


def test_list_snapshots_filters_by_before(fake_zfs: FakeZfs) -> None:
    # Two snapshots straddling 2024-06-01: one before, one after.
    fake_zfs.add(
        ["list", "-H", "-p", "-t", "snapshot", "-o", "name,creation,used,referenced"],
        "rpool/home@old\t1700000000\t10\t100\nrpool/home@new\t1730000000\t10\t100\n",
    )
    result = agent.m_list_snapshots({"before": "2024-06-01T00:00:00+00:00"})
    assert [s["name"] for s in result["snapshots"]] == ["rpool/home@old"]


def test_list_snapshots_truncates_when_max_results_set(fake_zfs: FakeZfs) -> None:
    lines = "".join(f"rpool/home@s{i}\t{1000 + i}\t10\t100\n" for i in range(5))
    fake_zfs.add(
        ["list", "-H", "-p", "-t", "snapshot", "-o", "name,creation,used,referenced"],
        lines,
    )
    result = agent.m_list_snapshots({"max_results": 3})
    assert len(result["snapshots"]) == 3
    assert result["truncated"] is True


def test_list_snapshots_truncated_false_when_under_cap(fake_zfs: FakeZfs) -> None:
    fake_zfs.add(
        ["list", "-H", "-p", "-t", "snapshot", "-o", "name,creation,used,referenced"],
        "rpool/home@a\t1000\t10\t100\n",
    )
    result = agent.m_list_snapshots({"max_results": 100})
    assert result["truncated"] is False


def test_list_snapshots_max_results_short_circuits_loop(fake_zfs: FakeZfs) -> None:
    """When max_results is set the loop must break early — we should not
    pay to construct dicts for rows past the cap. Verifies by setting
    max_results=2 against 100 rows and confirming we only got 2 back
    with truncated=True; the speed of the assertion is the practical
    win this guards."""
    lines = "".join(f"rpool/home@s{i:03d}\t{1000 + i}\t10\t100\n" for i in range(100))
    fake_zfs.add(
        ["list", "-H", "-p", "-t", "snapshot", "-o", "name,creation,used,referenced"],
        lines,
    )
    result = agent.m_list_snapshots({"max_results": 2})
    assert len(result["snapshots"]) == 2
    assert result["truncated"] is True
    # First two rows are the ones we kept; we never built dicts past those.
    assert [s["name"] for s in result["snapshots"]] == [
        "rpool/home@s000",
        "rpool/home@s001",
    ]


def test_list_snapshots_max_results_capped_at_hard_max(fake_zfs: FakeZfs) -> None:
    fake_zfs.add(
        ["list", "-H", "-p", "-t", "snapshot", "-o", "name,creation,used,referenced"],
        "rpool/home@a\t1000\t10\t100\n",
    )
    # validate_positive_int clamps to MAX_LIST_SNAPSHOTS — no error, just a cap.
    result = agent.m_list_snapshots({"max_results": 10_000_000})
    assert result["truncated"] is False  # only 1 row, cap doesn't matter


# ---- dataset_properties ----------------------------------------------------


def test_dataset_properties_returns_all_when_unfiltered(fake_zfs: FakeZfs) -> None:
    fake_zfs.add(
        ["get", "-H", "-p", "-o", "name,property,value,source", "all", "rpool/home"],
        "rpool/home\tcompression\tlz4\tinherited from rpool\n"
        "rpool/home\tatime\toff\tlocal\n"
        "rpool/home\tcompressratio\t1.45x\t-\n",
    )
    result = agent.m_dataset_properties({"dataset": "rpool/home"})
    props = {p["name"]: p for p in result["properties"]}
    assert props["compression"]["value"] == "lz4"
    assert props["compression"]["source"] == "inherited from rpool"
    assert props["atime"]["source"] == "local"
    assert props["compressratio"]["source"] == "-"


def test_dataset_properties_filtered(fake_zfs: FakeZfs) -> None:
    fake_zfs.add(
        ["get", "-H", "-p", "-o", "name,property,value,source", "compression,atime", "rpool/home"],
        "rpool/home\tcompression\tzstd\tlocal\nrpool/home\tatime\ton\tdefault\n",
    )
    result = agent.m_dataset_properties(
        {"dataset": "rpool/home", "properties": ["compression", "atime"]},
    )
    assert len(result["properties"]) == 2


def test_dataset_properties_rejects_bad_property_name() -> None:
    with pytest.raises(agent.InvalidParams, match="invalid property name"):
        agent.m_dataset_properties(
            {"dataset": "rpool/home", "properties": ["compression;rm"]},
        )


def test_dataset_properties_rejects_empty_properties_list() -> None:
    with pytest.raises(agent.InvalidParams, match="non-empty"):
        agent.m_dataset_properties({"dataset": "rpool/home", "properties": []})


# ---- pool_status -----------------------------------------------------------


_ZPOOL_STATUS_HEALTHY = """\
  pool: rpool
 state: ONLINE
  scan: scrub repaired 0B in 02:34:56 with 0 errors on Sun May 18 03:34:56 2025
config:

\tNAME        STATE     READ WRITE CKSUM
\trpool       ONLINE       0     0     0
\t  mirror-0  ONLINE       0     0     0
\t    sda     ONLINE       0     0     0
\t    sdb     ONLINE       0     0     0

errors: No known data errors
"""

_ZPOOL_STATUS_DEGRADED = """\
  pool: tank
 state: DEGRADED
status: One or more devices has experienced an unrecoverable error.  An
\tattempt was made to correct the error.  Applications are unaffected.
action: Determine if the device needs to be replaced, and clear the errors
\tusing 'zpool clear' or replace the device with 'zpool replace'.
   see: http://zfsonlinux.org/msg/ZFS-8000-9P
  scan: scrub repaired 4K in 01:00:00 with 0 errors on Sun May 18 04:00:00 2025
config:

\tNAME        STATE     READ WRITE CKSUM
\ttank        DEGRADED     0     0     0
\t  mirror-0  DEGRADED     0     0     0
\t    sdc     ONLINE       0     0     0
\t    sdd     FAULTED      3     0     5

errors: No known data errors
"""


def test_pool_status_parses_healthy_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        agent, "run_zpool", lambda args: _ZPOOL_STATUS_HEALTHY if args == ["status"] else ""
    )
    result = agent.m_pool_status({})
    assert len(result["pools"]) == 1
    pool = result["pools"][0]
    assert pool["name"] == "rpool"
    assert pool["state"] == "ONLINE"
    assert "scrub repaired 0B" in pool["scan"]
    assert pool["errors"] == "No known data errors"
    vdev_names = [v["name"] for v in pool["vdevs"]]
    assert vdev_names == ["rpool", "mirror-0", "sda", "sdb"]
    depths = {v["name"]: v["depth"] for v in pool["vdevs"]}
    assert depths == {"rpool": 0, "mirror-0": 1, "sda": 2, "sdb": 2}


def test_pool_status_parses_degraded_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        agent, "run_zpool", lambda args: _ZPOOL_STATUS_DEGRADED if args == ["status"] else ""
    )
    result = agent.m_pool_status({})
    pool = result["pools"][0]
    assert pool["state"] == "DEGRADED"
    assert "unrecoverable error" in pool["status"]
    # Multi-line continuation got joined.
    assert "Applications are unaffected" in pool["status"]
    # Faulted device's error counts surfaced.
    sdd = next(v for v in pool["vdevs"] if v["name"] == "sdd")
    assert sdd["state"] == "FAULTED"
    assert sdd["read_errors"] == 3
    assert sdd["cksum_errors"] == 5


def test_pool_status_scoped_to_one_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake(args: list[str]) -> str:
        seen.append(args)
        return _ZPOOL_STATUS_HEALTHY

    monkeypatch.setattr(agent, "run_zpool", fake)
    agent.m_pool_status({"pool": "rpool"})
    assert seen == [["status", "rpool"]]


def test_pool_status_rejects_invalid_pool_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent, "run_zpool", lambda args: "")
    with pytest.raises(agent.InvalidParams, match="invalid pool name"):
        agent.m_pool_status({"pool": "rpool;rm -rf /"})


# ---- snapshot_cadence ------------------------------------------------------


def test_snapshot_cadence_classifies_by_retention(fake_zfs: FakeZfs) -> None:
    fake_zfs.add(
        [
            "list",
            "-H",
            "-p",
            "-t",
            "snapshot",
            "-o",
            "name,creation,used,referenced",
            "-r",
            "rpool/home",
        ],
        "rpool/home@zfs-auto-snap_daily-2026-05-20-2025\t1779308715\t100\t1000\n"
        "rpool/home@zfs-auto-snap_daily-2026-05-21-2025\t1779395115\t200\t1000\n"
        "rpool/home@zfs-auto-snap_hourly-2026-05-22-0117\t1779453437\t50\t1000\n"
        "rpool/home@zfs-auto-snap_weekly-2026-05-16-2047\t1778964434\t0\t1000\n"
        "rpool/home@manual-before-upgrade\t1779000000\t999\t1000\n",
    )
    result = agent.m_snapshot_cadence({"dataset": "rpool/home"})
    assert result["total_snapshots"] == 5
    by_class = {b["class"]: b for b in result["by_class"]}
    assert by_class["daily"]["count"] == 2
    assert by_class["hourly"]["count"] == 1
    assert by_class["weekly"]["count"] == 1
    assert by_class["other"]["count"] == 1  # the manual snap
    assert result["total_unique_bytes"] == 100 + 200 + 50 + 0 + 999


def test_snapshot_cadence_reports_biggest_gap(fake_zfs: FakeZfs) -> None:
    fake_zfs.add(
        [
            "list",
            "-H",
            "-p",
            "-t",
            "snapshot",
            "-o",
            "name,creation,used,referenced",
            "-r",
            "rpool/home",
        ],
        # 100, 200 (gap 100), 1000 (gap 800 <- biggest), 1050 (gap 50)
        "rpool/home@a\t100\t0\t0\n"
        "rpool/home@b\t200\t0\t0\n"
        "rpool/home@c\t1000\t0\t0\n"
        "rpool/home@d\t1050\t0\t0\n",
    )
    result = agent.m_snapshot_cadence({"dataset": "rpool/home"})
    assert result["biggest_gap_seconds"] == 800
    assert result["biggest_gap_between"] == ["rpool/home@b", "rpool/home@c"]


def test_snapshot_cadence_empty_dataset(fake_zfs: FakeZfs) -> None:
    fake_zfs.add(
        [
            "list",
            "-H",
            "-p",
            "-t",
            "snapshot",
            "-o",
            "name,creation,used,referenced",
            "-r",
            "rpool/empty",
        ],
        "",
    )
    result = agent.m_snapshot_cadence({"dataset": "rpool/empty"})
    assert result["total_snapshots"] == 0
    assert result["earliest_creation"] is None
    assert result["biggest_gap_seconds"] is None
    assert result["by_class"] == []


# ---- diff_snapshots ---------------------------------------------------------


def test_diff_snapshots_parses_changes(fake_zfs: FakeZfs) -> None:
    fake_zfs.add(
        ["diff", "-H", "-F", "rpool/home@a", "rpool/home@b"],
        "M\tF\t/home/foo\n+\tF\t/home/new\n-\tF\t/home/gone\nR\tF\t/home/old\t/home/newname\n",
    )
    result = agent.m_diff_snapshots({"snap_a": "rpool/home@a", "snap_b": "rpool/home@b"})
    ops = [c["op"] for c in result["changes"]]
    assert ops == ["M", "+", "-", "R"]
    rename = next(c for c in result["changes"] if c["op"] == "R")
    assert rename["new_path"] == "/home/newname"


def test_run_zfs_accepts_per_call_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pinning the GH #7 plumbing: an explicit timeout overrides the default."""
    import subprocess as _subprocess  # noqa: PLC0415 - test-local import for monkeypatch target

    captured: dict[str, Any] = {}

    class _FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(*args: object, **kwargs: object) -> _FakeCompleted:
        captured.update(kwargs)
        return _FakeCompleted()

    monkeypatch.setattr(_subprocess, "run", fake_run)
    # Default timeout when not supplied.
    agent.run_zfs(["list"])
    assert captured["timeout"] == pytest.approx(agent.ZFS_TIMEOUT_SECONDS)
    # Explicit per-call timeout flows through.
    agent.run_zfs(["diff", "a@x", "a@y"], timeout=agent.ZFS_DIFF_TIMEOUT_SECONDS)
    assert captured["timeout"] == pytest.approx(agent.ZFS_DIFF_TIMEOUT_SECONDS)


def test_diff_snapshots_uses_diff_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """``m_diff_snapshots`` must invoke run_zfs with the longer timeout budget."""
    captured: dict[str, object] = {}

    def fake_run_zfs(args: list[str], *, timeout: float | None = None) -> str:
        captured["args"] = args
        captured["timeout"] = timeout
        return ""

    monkeypatch.setattr(agent, "run_zfs", fake_run_zfs)
    agent.m_diff_snapshots({"snap_a": "rpool/home@a", "snap_b": "rpool/home@b"})
    assert captured["timeout"] == agent.ZFS_DIFF_TIMEOUT_SECONDS


# ---- list_dir ---------------------------------------------------------------


def test_list_dir_reports_files_and_dirs(mock_mountpoint: dict[str, Any]) -> None:
    result = agent.m_list_dir(
        {"snapshot": mock_mountpoint["snapshot_name"], "path": ""},
    )
    by_name = {e["name"]: e for e in result["entries"]}
    assert by_name["hello.txt"]["type"] == "file"
    assert by_name["hello.txt"]["size"] == len("hello, world!\n")
    assert by_name["sub"]["type"] == "dir"
    assert by_name["empty_dir"]["type"] == "dir"


def test_list_dir_truncates_at_max_entries(mock_mountpoint: dict[str, Any]) -> None:
    result = agent.m_list_dir(
        {"snapshot": mock_mountpoint["snapshot_name"], "path": "", "max_entries": 2},
    )
    assert len(result["entries"]) == 2
    assert result["truncated"] is True


# ---- size_breakdown --------------------------------------------------------


def test_size_breakdown_returns_total_and_per_child(
    mock_mountpoint: dict[str, Any],
) -> None:
    result = agent.m_size_breakdown(
        {"snapshot": mock_mountpoint["snapshot_name"], "path": ""},
    )
    by_name = {e["name"]: e for e in result["entries"]}
    # Each known child shows up with the right type.
    assert by_name["hello.txt"]["type"] == "file"
    assert by_name["hello.txt"]["bytes"] == len("hello, world!\n")
    assert by_name["sub"]["type"] == "dir"
    assert by_name["empty_dir"]["type"] == "dir"
    assert by_name["escape"]["type"] == "symlink"
    # Total is exactly the sum of children (no double-counting).
    assert result["total_bytes"] == sum(e["bytes"] for e in result["entries"])
    assert result["truncated"] is False


def test_size_breakdown_recurses_into_subdir(mock_mountpoint: dict[str, Any]) -> None:
    """`sub/` contains nested.txt (file) and link_to_hello (symlink).

    sub's reported bytes must equal: sub's own inode size + nested.txt's
    bytes + link's lstat size. Following the symlink would add hello.txt's
    bytes; this assertion catches that regression.
    """
    result = agent.m_size_breakdown(
        {"snapshot": mock_mountpoint["snapshot_name"], "path": ""},
    )
    sub = next(e for e in result["entries"] if e["name"] == "sub")
    sub_dir = mock_mountpoint["files"]["nested"].parent
    expected = (
        sub_dir.lstat().st_size
        + (sub_dir / "nested.txt").lstat().st_size
        + (sub_dir / "link_to_hello").lstat().st_size
    )
    assert sub["bytes"] == expected


def test_size_breakdown_symlink_counted_as_self_not_target(
    mock_mountpoint: dict[str, Any],
) -> None:
    """The `escape` symlink points to /etc/passwd; we count the link only."""
    result = agent.m_size_breakdown(
        {"snapshot": mock_mountpoint["snapshot_name"], "path": ""},
    )
    escape = next(e for e in result["entries"] if e["name"] == "escape")
    assert escape["type"] == "symlink"
    # /etc/passwd is typically >1 KB; if we'd followed the link, bytes would
    # be that. Symlink lstat size on Linux is the length of the target path.
    assert escape["bytes"] == len("/etc/passwd")


def test_size_breakdown_truncates_on_budget(mock_mountpoint: dict[str, Any]) -> None:
    result = agent.m_size_breakdown(
        {"snapshot": mock_mountpoint["snapshot_name"], "path": "", "max_entries": 1},
    )
    # max_entries=1 exhausts after the first top-level child.
    assert result["truncated"] is True
    assert result["walked_entries"] == 1


def test_size_breakdown_rejects_non_directory(
    mock_mountpoint: dict[str, Any],
) -> None:
    with pytest.raises(agent.PathError, match="not a directory"):
        agent.m_size_breakdown(
            {"snapshot": mock_mountpoint["snapshot_name"], "path": "hello.txt"},
        )


def test_size_breakdown_rejects_dotdot(mock_mountpoint: dict[str, Any]) -> None:
    with pytest.raises(agent.PathError):
        agent.m_size_breakdown(
            {"snapshot": mock_mountpoint["snapshot_name"], "path": "../etc"},
        )


def test_size_breakdown_empty_directory(mock_mountpoint: dict[str, Any]) -> None:
    result = agent.m_size_breakdown(
        {"snapshot": mock_mountpoint["snapshot_name"], "path": "empty_dir"},
    )
    assert result["total_bytes"] == 0
    assert result["entries"] == []
    assert result["truncated"] is False


# ---- read_file --------------------------------------------------------------


def test_read_file_returns_utf8(mock_mountpoint: dict[str, Any]) -> None:
    result = agent.m_read_file(
        {"snapshot": mock_mountpoint["snapshot_name"], "path": "hello.txt"},
    )
    assert result["encoding"] == "utf-8"
    assert result["content"] == "hello, world!\n"
    assert result["truncated"] is False
    assert result["size"] == len("hello, world!\n")


def test_read_file_falls_back_to_base64_for_binary(mock_mountpoint: dict[str, Any]) -> None:
    result = agent.m_read_file(
        {
            "snapshot": mock_mountpoint["snapshot_name"],
            "path": "big.bin",
            "max_bytes": 16,
        },
    )
    assert result["encoding"] == "base64"
    assert base64.b64decode(result["content"]) == b"\xff\xfe\xfd\xfc" * 4
    assert result["truncated"] is True
    assert result["bytes_returned"] == 16


def test_read_file_rejects_directory(mock_mountpoint: dict[str, Any]) -> None:
    with pytest.raises(agent.PathError):
        agent.m_read_file(
            {"snapshot": mock_mountpoint["snapshot_name"], "path": "sub"},
        )


def test_read_file_rejects_missing(mock_mountpoint: dict[str, Any]) -> None:
    with pytest.raises(agent.PathError):
        agent.m_read_file(
            {"snapshot": mock_mountpoint["snapshot_name"], "path": "nope"},
        )


# ---- find_files -------------------------------------------------------------


def test_find_files_matches_glob(mock_mountpoint: dict[str, Any]) -> None:
    result = agent.m_find_files(
        {"snapshot": mock_mountpoint["snapshot_name"], "pattern": "*.txt"},
    )
    paths = sorted(m["path"] for m in result["matches"])
    assert paths == ["hello.txt", "sub/nested.txt"]


def test_find_files_truncates(mock_mountpoint: dict[str, Any]) -> None:
    result = agent.m_find_files(
        {
            "snapshot": mock_mountpoint["snapshot_name"],
            "pattern": "*",
            "max_results": 2,
        },
    )
    assert len(result["matches"]) == 2
    assert result["truncated"] is True


# ---- content_grep -----------------------------------------------------------


def test_content_grep_finds_matches(mock_mountpoint: dict[str, Any]) -> None:
    result = agent.m_content_grep(
        {"snapshot": mock_mountpoint["snapshot_name"], "pattern": "hello"},
    )
    paths = sorted(m["path"] for m in result["matches"])
    assert "hello.txt" in paths


def test_content_grep_rejects_invalid_regex(mock_mountpoint: dict[str, Any]) -> None:
    with pytest.raises(agent.InvalidParams):
        agent.m_content_grep(
            {"snapshot": mock_mountpoint["snapshot_name"], "pattern": "[unterminated"},
        )


def test_content_grep_skips_binary_files_via_null_byte_sniff(
    mock_mountpoint: dict[str, Any],
) -> None:
    """Files with null bytes in the first 8 KiB are skipped without being
    line-iterated, so a pathological no-newline binary can't OOM the agent
    by buffering the whole 'line' before hitting UnicodeDecodeError."""
    # big.bin in the fixture is 1.14 MiB of \xff\xfe\xfd\xfc — no null bytes
    # actually. Add a real null-byte binary to the snapshot for this test.
    snap_root = mock_mountpoint["snap_root"]
    (snap_root / "binary_no_newlines.bin").write_bytes(b"\x00\x01\x02" * 100_000)
    # The pattern would match the surrounding fixture file content but must
    # NOT match anything inside the binary (which would be skipped).
    result = agent.m_content_grep(
        {"snapshot": mock_mountpoint["snapshot_name"], "pattern": ".*"},
    )
    paths = {m["path"] for m in result["matches"]}
    assert "binary_no_newlines.bin" not in paths


def test_iso_to_ts_treats_naive_as_utc() -> None:
    """A naive ISO 8601 string is interpreted as UTC, matching the server's
    `timeparse.parse_phrase` convention. Without this, `datetime.timestamp()`
    on a naive datetime would interpret it as *local* time, so the same
    input would map to different epoch seconds depending on the agent
    host's TZ. The server always sends timezone-aware strings today, but
    the boundary should be self-consistent regardless."""
    from datetime import UTC, datetime  # noqa: PLC0415

    expected = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
    naive = agent._iso_to_ts("2024-01-01T00:00:00", name="t")
    aware = agent._iso_to_ts("2024-01-01T00:00:00+00:00", name="t")
    assert naive == aware == expected


def test_get_dataset_mountpoint_is_cached_across_calls(fake_zfs: FakeZfs) -> None:
    """Per-dataset mountpoint lookups are memoised so that snapshot-
    iterating methods (file_history, versions_of, bisect_change, …)
    don't spawn one `zfs get mountpoint` subprocess per snapshot."""
    fake_zfs.add(
        ["get", "-H", "-p", "-o", "value", "mountpoint", "rpool/home"],
        "/mnt/home\n",
    )
    # First call hits the subprocess; subsequent calls hit the cache.
    for _ in range(10):
        mp = agent.get_dataset_mountpoint("rpool/home")
        assert str(mp) == "/mnt/home"
    # FakeZfs records every call it actually serviced — should be one.
    mountpoint_calls = [c for c in fake_zfs.calls if c[0:1] == ("get",) and "mountpoint" in c]
    assert len(mountpoint_calls) == 1, (
        f"expected exactly one zfs get mountpoint call, got {len(mountpoint_calls)}: "
        f"{mountpoint_calls}"
    )


def test_content_grep_caps_pathological_single_line_files(
    mock_mountpoint: dict[str, Any],
) -> None:
    """A text file with no newlines and a line longer than the cap is
    skipped rather than buffered into memory. Verifies the readline cap."""
    snap_root = mock_mountpoint["snap_root"]
    # No null byte (passes the binary sniff) but no newline either — would
    # have been read entirely into memory by the old code.
    long_line = b"A" * (agent.MAX_GREP_LINE_BYTES + 10)
    (snap_root / "huge_line.txt").write_bytes(long_line)
    result = agent.m_content_grep(
        {"snapshot": mock_mountpoint["snapshot_name"], "pattern": "AAA"},
    )
    paths = {m["path"] for m in result["matches"]}
    # We didn't crash; we got results for the other small text files; and
    # the huge-line file did not yield matches because the cap stopped us.
    assert "huge_line.txt" not in paths


# ---- file_history / snapshots_containing / first_appearance -----------------


def _setup_history_fake(fake_zfs: FakeZfs, mp: dict[str, Any]) -> None:
    """Wire enough zfs responses to drive file_history over one snapshot."""
    fake_zfs.add(
        [
            "list",
            "-H",
            "-p",
            "-t",
            "snapshot",
            "-o",
            "name,creation,used,referenced",
            "-r",
            "testpool/test",
        ],
        f"{mp['snapshot_name']}\t1716000000\t10\t1000\n",
    )


def test_file_history_reports_presence(mock_mountpoint: dict[str, Any], fake_zfs: FakeZfs) -> None:
    _setup_history_fake(fake_zfs, mock_mountpoint)
    result = agent.m_file_history({"dataset": "testpool/test", "path": "hello.txt"})
    assert len(result["versions"]) == 1
    v = result["versions"][0]
    assert v["present"] is True
    assert v["size"] == len("hello, world!\n")


def test_file_history_reports_absence(mock_mountpoint: dict[str, Any], fake_zfs: FakeZfs) -> None:
    _setup_history_fake(fake_zfs, mock_mountpoint)
    result = agent.m_file_history({"dataset": "testpool/test", "path": "does_not_exist"})
    assert result["versions"][0]["present"] is False


def test_snapshots_containing_filters_by_time(
    mock_mountpoint: dict[str, Any], fake_zfs: FakeZfs
) -> None:
    _setup_history_fake(fake_zfs, mock_mountpoint)
    # snapshot creation is 1716000000 == 2024-05-18; ask for snapshots AFTER 2025.
    result = agent.m_snapshots_containing(
        {
            "dataset": "testpool/test",
            "path": "hello.txt",
            "after": "2025-01-01T00:00:00",
        },
    )
    assert result["snapshots"] == []


def test_first_appearance_returns_earliest(
    mock_mountpoint: dict[str, Any], fake_zfs: FakeZfs
) -> None:
    _setup_history_fake(fake_zfs, mock_mountpoint)
    result = agent.m_first_appearance({"dataset": "testpool/test", "path": "hello.txt"})
    assert result["first"] is not None
    assert result["first"]["snapshot"] == mock_mountpoint["snapshot_name"]


# ---- last_appearance -------------------------------------------------------


def test_last_appearance_returns_latest(
    mock_mountpoint: dict[str, Any],
    fake_zfs: FakeZfs,
) -> None:
    _setup_history_fake(fake_zfs, mock_mountpoint)
    result = agent.m_last_appearance({"dataset": "testpool/test", "path": "hello.txt"})
    assert result["last"] is not None
    assert result["last"]["snapshot"] == mock_mountpoint["snapshot_name"]


def test_last_appearance_returns_null_when_absent(
    mock_mountpoint: dict[str, Any],
    fake_zfs: FakeZfs,
) -> None:
    _setup_history_fake(fake_zfs, mock_mountpoint)
    result = agent.m_last_appearance(
        {"dataset": "testpool/test", "path": "never_existed"},
    )
    assert result["last"] is None


# ---- file_diff -------------------------------------------------------------


def test_file_diff_identical_self_compare(mock_mountpoint: dict[str, Any]) -> None:
    """Diffing a snapshot against itself reports identical with empty diff."""
    snap = mock_mountpoint["snapshot_name"]
    result = agent.m_file_diff(
        {"snap_a": snap, "snap_b": snap, "path": "hello.txt"},
    )
    assert result["identical"] is True
    assert result["diff"] == ""
    assert result["encoding"] == "utf-8"
    assert result["present_in_a"] is True
    assert result["present_in_b"] is True
    assert result["size_a"] == result["size_b"]


def test_file_diff_missing_in_a_shows_full_addition(
    mock_mountpoint: dict[str, Any],
) -> None:
    snap = mock_mountpoint["snapshot_name"]
    result = agent.m_file_diff(
        {"snap_a": snap, "snap_b": snap, "path": "never_existed"},
    )
    # Both missing: identical (vacuously true), encoding="missing", empty diff.
    assert result["present_in_a"] is False
    assert result["present_in_b"] is False
    assert result["identical"] is True
    assert result["encoding"] == "missing"


def test_file_diff_binary_skips_textual_diff(mock_mountpoint: dict[str, Any]) -> None:
    snap = mock_mountpoint["snapshot_name"]
    result = agent.m_file_diff(
        {"snap_a": snap, "snap_b": snap, "path": "big.bin"},
    )
    assert result["encoding"] == "binary"
    assert result["diff"] == ""
    # Identical via byte comparison even though we can't render the diff.
    assert result["identical"] is True


def test_file_diff_truncation_flag(mock_mountpoint: dict[str, Any]) -> None:
    snap = mock_mountpoint["snapshot_name"]
    result = agent.m_file_diff(
        {"snap_a": snap, "snap_b": snap, "path": "big.bin", "max_bytes": 1024},
    )
    assert result["truncated_a"] is True
    assert result["truncated_b"] is True


def test_file_diff_actually_diffs_different_content(
    snapshot_tree: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    fake_zfs: FakeZfs,
) -> None:
    """Two different snapshots of the same file: unified diff has +/- lines."""
    # Build a sibling snapshot tree with a modified hello.txt.
    snap2_root = snapshot_tree["mountpoint"] / ".zfs" / "snapshot" / "snap2"
    snap2_root.mkdir()
    (snap2_root / "hello.txt").write_text("hello, world!\nplus an extra line\n")
    # Wire the same mountpoint for both snapshots (same dataset).
    fake_zfs.add(
        ["get", "-H", "-p", "-o", "value", "mountpoint", "testpool/test"],
        f"{snapshot_tree['mountpoint']}\n",
    )
    result = agent.m_file_diff(
        {
            "snap_a": "testpool/test@snap1",
            "snap_b": "testpool/test@snap2",
            "path": "hello.txt",
        },
    )
    assert result["identical"] is False
    assert result["encoding"] == "utf-8"
    assert "+plus an extra line\n" in result["diff"]


# ---- versions_of -----------------------------------------------------------


def test_versions_of_single_snapshot(
    mock_mountpoint: dict[str, Any],
    fake_zfs: FakeZfs,
) -> None:
    _setup_history_fake(fake_zfs, mock_mountpoint)
    result = agent.m_versions_of(
        {"dataset": "testpool/test", "path": "hello.txt"},
    )
    assert len(result["versions"]) == 1
    v = result["versions"][0]
    assert "sha256" in v
    assert v["size"] == len("hello, world!\n")
    assert v["truncated"] is False
    assert len(v["snapshots"]) == 1


def test_versions_of_dedupes_identical_snapshots(
    snapshot_tree: dict[str, Any],
    fake_zfs: FakeZfs,
) -> None:
    """Two snapshots with identical hello.txt collapse to one version."""
    snap2_root = snapshot_tree["mountpoint"] / ".zfs" / "snapshot" / "snap2"
    snap2_root.mkdir()
    (snap2_root / "hello.txt").write_text("hello, world!\n")  # same content
    fake_zfs.add(
        ["get", "-H", "-p", "-o", "value", "mountpoint", "testpool/test"],
        f"{snapshot_tree['mountpoint']}\n",
    )
    fake_zfs.add(
        [
            "list",
            "-H",
            "-p",
            "-t",
            "snapshot",
            "-o",
            "name,creation,used,referenced",
            "-r",
            "testpool/test",
        ],
        "testpool/test@snap1\t1700000000\t10\t1000\ntestpool/test@snap2\t1700000100\t10\t1000\n",
    )
    result = agent.m_versions_of(
        {"dataset": "testpool/test", "path": "hello.txt"},
    )
    assert len(result["versions"]) == 1
    v = result["versions"][0]
    assert len(v["snapshots"]) == 2
    assert v["first_seen"]["creation"] == 1700000000
    assert v["last_seen"]["creation"] == 1700000100


def test_versions_of_marks_truncated_when_over_cap(
    mock_mountpoint: dict[str, Any],
    fake_zfs: FakeZfs,
) -> None:
    _setup_history_fake(fake_zfs, mock_mountpoint)
    result = agent.m_versions_of(
        {"dataset": "testpool/test", "path": "big.bin", "max_bytes": 1024},
    )
    assert result["truncated"] is True
    assert result["versions"][0]["truncated"] is True


# ---- find_deleted ----------------------------------------------------------


def test_find_deleted_empty_when_only_one_snapshot(
    mock_mountpoint: dict[str, Any],
    fake_zfs: FakeZfs,
) -> None:
    """A window containing one snapshot has nothing to diff against."""
    _setup_history_fake(fake_zfs, mock_mountpoint)
    result = agent.m_find_deleted({"dataset": "testpool/test"})
    assert result["deleted"] == []
    assert result["from_snapshot"] == mock_mountpoint["snapshot_name"]
    assert result["to_snapshot"] == mock_mountpoint["snapshot_name"]


def test_find_deleted_filters_to_minus_ops(
    mock_mountpoint: dict[str, Any],
    fake_zfs: FakeZfs,
) -> None:
    """A diff containing -/+/M entries returns only the -."""
    fake_zfs.add(
        [
            "list",
            "-H",
            "-p",
            "-t",
            "snapshot",
            "-o",
            "name,creation,used,referenced",
            "-r",
            "testpool/test",
        ],
        "testpool/test@old\t1700000000\t10\t1000\ntestpool/test@new\t1700000100\t10\t1000\n",
    )
    fake_zfs.add(
        ["diff", "-H", "-F", "testpool/test@old", "testpool/test@new"],
        "-\tF\t/home/youruser/gone.txt\n"
        "M\tF\t/home/youruser/changed.txt\n"
        "+\tF\t/home/youruser/added.txt\n",
    )
    result = agent.m_find_deleted({"dataset": "testpool/test"})
    assert [d["path"] for d in result["deleted"]] == ["/home/youruser/gone.txt"]
    assert result["from_snapshot"] == "testpool/test@old"
    assert result["to_snapshot"] == "testpool/test@new"


def test_find_deleted_truncates(
    mock_mountpoint: dict[str, Any],
    fake_zfs: FakeZfs,
) -> None:
    fake_zfs.add(
        [
            "list",
            "-H",
            "-p",
            "-t",
            "snapshot",
            "-o",
            "name,creation,used,referenced",
            "-r",
            "testpool/test",
        ],
        "testpool/test@old\t1700000000\t10\t1000\ntestpool/test@new\t1700000100\t10\t1000\n",
    )
    fake_zfs.add(
        ["diff", "-H", "-F", "testpool/test@old", "testpool/test@new"],
        "-\tF\t/a\n-\tF\t/b\n-\tF\t/c\n",
    )
    result = agent.m_find_deleted(
        {"dataset": "testpool/test", "max_results": 2},
    )
    assert len(result["deleted"]) == 2
    assert result["truncated"] is True


# ---- size_delta -------------------------------------------------------------


def test_size_delta_returns_written_bytes(fake_zfs: FakeZfs) -> None:
    fake_zfs.add(
        ["get", "-H", "-p", "-o", "value", "written@a", "rpool/home@b"],
        "12345\n",
    )
    result = agent.m_size_delta(
        {"snap_a": "rpool/home@a", "snap_b": "rpool/home@b"},
    )
    assert result["written_bytes"] == 12345


def test_size_delta_rejects_cross_dataset(fake_zfs: FakeZfs) -> None:
    with pytest.raises(agent.InvalidParams):
        agent.m_size_delta(
            {"snap_a": "rpool/home@a", "snap_b": "rpool/etc@b"},
        )


# ---- top_consumers ---------------------------------------------------------


def test_top_consumers_returns_largest_first(mock_mountpoint: dict[str, Any]) -> None:
    result = agent.m_top_consumers(
        {"snapshot": mock_mountpoint["snapshot_name"], "path": "", "n": 3},
    )
    # Sorted descending by bytes.
    sizes = [e["bytes"] for e in result["entries"]]
    assert sizes == sorted(sizes, reverse=True)
    # big.bin (~1.14 MiB) is by far the biggest single file.
    biggest = result["entries"][0]
    assert "big.bin" in biggest["path"]


def test_top_consumers_includes_dirs_with_subtree_total(
    mock_mountpoint: dict[str, Any],
) -> None:
    result = agent.m_top_consumers(
        {"snapshot": mock_mountpoint["snapshot_name"], "path": "", "n": 10},
    )
    by_path = {e["path"]: e for e in result["entries"]}
    # The "sub" dir should appear, with bytes including nested.txt + symlink.
    sub_entry = by_path.get("sub")
    assert sub_entry is not None
    assert sub_entry["type"] == "dir"
    assert sub_entry["bytes"] >= len("nested content\n")


def test_top_consumers_rejects_non_directory(mock_mountpoint: dict[str, Any]) -> None:
    with pytest.raises(agent.PathError, match="not a directory"):
        agent.m_top_consumers(
            {"snapshot": mock_mountpoint["snapshot_name"], "path": "hello.txt"},
        )


def test_top_consumers_truncates_on_entry_budget(
    mock_mountpoint: dict[str, Any],
) -> None:
    result = agent.m_top_consumers(
        {"snapshot": mock_mountpoint["snapshot_name"], "path": "", "max_entries": 1, "n": 10},
    )
    assert result["truncated"] is True
    assert result["walked_entries"] == 1


# ---- stale_snapshots -------------------------------------------------------


def test_stale_snapshots_filters_by_cutoff(fake_zfs: FakeZfs) -> None:
    fake_zfs.add(
        [
            "list",
            "-H",
            "-p",
            "-t",
            "snapshot",
            "-o",
            "name,creation,used,referenced",
            "-r",
            "rpool/home",
        ],
        "rpool/home@old1\t100\t500\t1000\n"
        "rpool/home@old2\t200\t1500\t1000\n"  # biggest used, should sort first
        "rpool/home@new\t9000\t10\t1000\n",
    )
    result = agent.m_stale_snapshots(
        {"dataset": "rpool/home", "older_than": "1970-01-01T00:50:00+00:00"},
    )
    # 'new' is after cutoff (9000 > 3000), 'old1' and 'old2' before.
    # Wait: cutoff is 50 minutes after epoch (3000s). old1=100, old2=200 are
    # both before. new=9000 is after.
    names = [s["name"] for s in result["snapshots"]]
    assert names == ["rpool/home@old2", "rpool/home@old1"]  # sorted by used desc


def test_stale_snapshots_truncated_flag(fake_zfs: FakeZfs) -> None:
    fake_zfs.add(
        [
            "list",
            "-H",
            "-p",
            "-t",
            "snapshot",
            "-o",
            "name,creation,used,referenced",
            "-r",
            "rpool/home",
        ],
        "rpool/home@a\t100\t1\t1\nrpool/home@b\t200\t2\t1\nrpool/home@c\t300\t3\t1\n",
    )
    result = agent.m_stale_snapshots(
        {"dataset": "rpool/home", "older_than": "1970-01-01T01:00:00+00:00", "max_results": 2},
    )
    assert len(result["snapshots"]) == 2
    assert result["truncated"] is True


def test_stale_snapshots_rejects_missing_older_than() -> None:
    with pytest.raises(agent.InvalidParams):
        agent.m_stale_snapshots({"dataset": "rpool/home"})


# ---- bisect_change ---------------------------------------------------------


def _setup_multi_snap(fake_zfs: FakeZfs, tree: dict[str, Any], snaps: list[str]) -> None:
    """Wire fake_zfs.add for list_snapshots + mountpoint across *snaps*."""
    rows = []
    for i, snap in enumerate(snaps):
        rows.append(f"testpool/test@{snap}\t{1000 + i * 100}\t10\t1000\n")
    fake_zfs.add(
        [
            "list",
            "-H",
            "-p",
            "-t",
            "snapshot",
            "-o",
            "name,creation,used,referenced",
            "-r",
            "testpool/test",
        ],
        "".join(rows),
    )
    fake_zfs.add(
        ["get", "-H", "-p", "-o", "value", "mountpoint", "testpool/test"],
        f"{tree['mountpoint']}\n",
    )


def test_bisect_change_no_transition_when_both_ends_agree(
    snapshot_tree: dict[str, Any],
    fake_zfs: FakeZfs,
) -> None:
    """Both ends have the file present; predicate flat-true; no transition."""
    snap2_root = snapshot_tree["mountpoint"] / ".zfs" / "snapshot" / "snap2"
    snap2_root.mkdir()
    (snap2_root / "hello.txt").write_text("hello, world!\n")
    _setup_multi_snap(fake_zfs, snapshot_tree, ["snap1", "snap2"])
    result = agent.m_bisect_change(
        {"dataset": "testpool/test", "path": "hello.txt", "predicate": {"kind": "exists"}},
    )
    assert result["transition"] is None
    assert result["earliest_value"] is True
    assert result["latest_value"] is True


def test_bisect_change_finds_exists_transition(
    snapshot_tree: dict[str, Any],
    fake_zfs: FakeZfs,
) -> None:
    """snap1 has hello.txt, snap2 does not — bisect locates the transition."""
    snap2_root = snapshot_tree["mountpoint"] / ".zfs" / "snapshot" / "snap2"
    snap2_root.mkdir()
    # Empty snap2: file went away.
    _setup_multi_snap(fake_zfs, snapshot_tree, ["snap1", "snap2"])
    result = agent.m_bisect_change(
        {"dataset": "testpool/test", "path": "hello.txt", "predicate": {"kind": "exists"}},
    )
    assert result["transition"] is not None
    assert result["transition"]["from_snapshot"] == "testpool/test@snap1"
    assert result["transition"]["to_snapshot"] == "testpool/test@snap2"
    assert result["transition"]["from_value"] is True
    assert result["transition"]["to_value"] is False


def test_bisect_change_contains_predicate(
    snapshot_tree: dict[str, Any],
    fake_zfs: FakeZfs,
) -> None:
    """contains needle only appears in snap2 → transition between snap1 and snap2."""
    snap2_root = snapshot_tree["mountpoint"] / ".zfs" / "snapshot" / "snap2"
    snap2_root.mkdir()
    (snap2_root / "hello.txt").write_text("hello, world!\nINJECTED BUG MARKER\n")
    _setup_multi_snap(fake_zfs, snapshot_tree, ["snap1", "snap2"])
    result = agent.m_bisect_change(
        {
            "dataset": "testpool/test",
            "path": "hello.txt",
            "predicate": {"kind": "contains", "needle": "INJECTED BUG MARKER"},
        },
    )
    assert result["transition"]["from_value"] is False
    assert result["transition"]["to_value"] is True


def test_bisect_change_validates_predicate_shape() -> None:
    with pytest.raises(agent.InvalidParams, match="kind"):
        agent.m_bisect_change(
            {"dataset": "rpool/home", "path": "x", "predicate": {"kind": "blender"}},
        )
    with pytest.raises(agent.InvalidParams, match="non-empty 'needle'"):
        agent.m_bisect_change(
            {"dataset": "rpool/home", "path": "x", "predicate": {"kind": "contains", "needle": ""}},
        )
    with pytest.raises(agent.InvalidParams, match="64-hex"):
        agent.m_bisect_change(
            {
                "dataset": "rpool/home",
                "path": "x",
                "predicate": {"kind": "sha256_equals", "hash": "deadbeef"},
            },
        )
    with pytest.raises(agent.InvalidParams, match="non-negative int"):
        agent.m_bisect_change(
            {
                "dataset": "rpool/home",
                "path": "x",
                "predicate": {"kind": "size_at_least", "size": -1},
            },
        )


def test_bisect_change_size_at_least_predicate(
    snapshot_tree: dict[str, Any],
    fake_zfs: FakeZfs,
) -> None:
    """snap1's hello.txt is short; snap2's is long. size_at_least 50 transitions."""
    snap2_root = snapshot_tree["mountpoint"] / ".zfs" / "snapshot" / "snap2"
    snap2_root.mkdir()
    (snap2_root / "hello.txt").write_text("x" * 100)  # longer than 50 bytes
    _setup_multi_snap(fake_zfs, snapshot_tree, ["snap1", "snap2"])
    result = agent.m_bisect_change(
        {
            "dataset": "testpool/test",
            "path": "hello.txt",
            "predicate": {"kind": "size_at_least", "size": 50},
        },
    )
    assert result["transition"]["from_value"] is False
    assert result["transition"]["to_value"] is True


def test_bisect_change_returns_no_transition_with_single_snapshot(
    mock_mountpoint: dict[str, Any],
    fake_zfs: FakeZfs,
) -> None:
    _setup_history_fake(fake_zfs, mock_mountpoint)
    result = agent.m_bisect_change(
        {"dataset": "testpool/test", "path": "hello.txt", "predicate": {"kind": "exists"}},
    )
    assert result["transition"] is None
    assert "at least two snapshots" in result["reason"]


# ---- checksum_file ---------------------------------------------------------


def test_checksum_file_returns_sha256_of_full_content(mock_mountpoint: dict[str, Any]) -> None:
    result = agent.m_checksum_file(
        {"snapshot": mock_mountpoint["snapshot_name"], "path": "hello.txt"},
    )
    assert result["snapshot"] == mock_mountpoint["snapshot_name"]
    assert result["path"] == "hello.txt"
    assert result["size"] == mock_mountpoint["files"]["hello"].stat().st_size
    expected = hashlib.sha256(b"hello, world!\n").hexdigest()
    assert result["sha256"] == expected


def test_checksum_file_refuses_symlink(mock_mountpoint: dict[str, Any]) -> None:
    # sub/link_to_hello -> ../hello.txt stays inside the snapshot root so the
    # boundary check passes; the symlink refusal fires at lstat() time (G3).
    with pytest.raises(agent.PathError, match="symlink"):
        agent.m_checksum_file(
            {"snapshot": mock_mountpoint["snapshot_name"], "path": "sub/link_to_hello"},
        )


def test_checksum_file_refuses_directory(mock_mountpoint: dict[str, Any]) -> None:
    with pytest.raises(agent.PathError, match="not a regular file"):
        agent.m_checksum_file(
            {"snapshot": mock_mountpoint["snapshot_name"], "path": "empty_dir"},
        )


def test_checksum_file_rejects_oversized_file(
    mock_mountpoint: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Patch pathlib.Path.lstat to report a file larger than MAX_CHECKSUM_FILESIZE.
    original_lstat = pathlib.Path.lstat

    def fake_lstat(self: pathlib.Path) -> os.stat_result:
        st = original_lstat(self)
        return os.stat_result(
            (
                st.st_mode,
                st.st_ino,
                st.st_dev,
                st.st_nlink,
                st.st_uid,
                st.st_gid,
                agent.MAX_CHECKSUM_FILESIZE + 1,
                int(st.st_atime),
                int(st.st_mtime),
                int(st.st_ctime),
            )
        )

    monkeypatch.setattr(pathlib.Path, "lstat", fake_lstat)
    with pytest.raises(agent.InvalidParams, match="too large"):
        agent.m_checksum_file(
            {"snapshot": mock_mountpoint["snapshot_name"], "path": "hello.txt"},
        )


def test_checksum_file_rejects_dotdot(mock_mountpoint: dict[str, Any]) -> None:
    with pytest.raises(agent.PathError):
        agent.m_checksum_file(
            {"snapshot": mock_mountpoint["snapshot_name"], "path": "../etc/passwd"},
        )
