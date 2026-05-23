"""Path traversal, symlink escape, and symlink-following refusal."""

from __future__ import annotations

from typing import Any

import pytest

import zfs_snoop_agent as agent


def test_resolve_returns_target_inside_root(mock_mountpoint: dict[str, Any]) -> None:
    root, target = agent.resolve_under_snapshot(mock_mountpoint["snapshot_name"], "hello.txt")
    assert target.resolve() == mock_mountpoint["files"]["hello"].resolve()
    assert target.is_relative_to(root)


def test_resolve_rejects_absolute_path(mock_mountpoint: dict[str, Any]) -> None:
    # absolute paths are normalised by stripping leading "/" so "/hello.txt"
    # is treated as "hello.txt" and resolves successfully. Anything truly
    # escaping has to use "..".
    _root, target = agent.resolve_under_snapshot(mock_mountpoint["snapshot_name"], "/hello.txt")
    assert target.resolve() == mock_mountpoint["files"]["hello"].resolve()


def test_resolve_rejects_dotdot_traversal(mock_mountpoint: dict[str, Any]) -> None:
    with pytest.raises(agent.PathError):
        agent.resolve_under_snapshot(mock_mountpoint["snapshot_name"], "../../../etc/passwd")


def test_resolve_rejects_symlink_that_escapes(mock_mountpoint: dict[str, Any]) -> None:
    # ``escape`` symlinks to /etc/passwd. resolve() follows it; the boundary
    # check must catch the escape.
    with pytest.raises(agent.PathError):
        agent.resolve_under_snapshot(mock_mountpoint["snapshot_name"], "escape")


def test_read_file_refuses_to_follow_symlink(mock_mountpoint: dict[str, Any]) -> None:
    # An in-snapshot symlink should also be refused for reads: we don't
    # follow symlinks ever, even when they stay in-snapshot.
    with pytest.raises(agent.PathError, match="symlink"):
        agent.m_read_file(
            {"snapshot": mock_mountpoint["snapshot_name"], "path": "sub/link_to_hello"},
        )


def test_list_dir_reports_symlink_without_following(mock_mountpoint: dict[str, Any]) -> None:
    result = agent.m_list_dir(
        {"snapshot": mock_mountpoint["snapshot_name"], "path": "sub"},
    )
    by_name = {e["name"]: e for e in result["entries"]}
    assert by_name["link_to_hello"]["type"] == "symlink"
    assert by_name["link_to_hello"]["target"] == "../hello.txt"


def test_list_dir_rejects_dotdot(mock_mountpoint: dict[str, Any]) -> None:
    with pytest.raises(agent.PathError):
        agent.m_list_dir(
            {"snapshot": mock_mountpoint["snapshot_name"], "path": "sub/.."},
        )


def test_read_file_rejects_dotdot(mock_mountpoint: dict[str, Any]) -> None:
    with pytest.raises(agent.PathError):
        agent.m_read_file(
            {"snapshot": mock_mountpoint["snapshot_name"], "path": "../etc/passwd"},
        )
