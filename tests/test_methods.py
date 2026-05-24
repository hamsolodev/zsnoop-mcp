"""Per-method happy paths and a representative truncation test."""

from __future__ import annotations

import base64
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
