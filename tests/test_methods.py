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
    assert info["limits"]["max_read_bytes"] == agent.MAX_READ_BYTES


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
