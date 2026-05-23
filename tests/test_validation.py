"""Input-validation tests: dataset/snapshot names, paths, positive ints."""

from __future__ import annotations

import pytest

import zfs_snoop_agent as agent

# ---- validate_dataset -------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["rpool", "rpool/home", "rpool/home/youruser", "bpool/BOOT/debian", "tank.test-1:foo"],
)
def test_validate_dataset_accepts_valid(name: str) -> None:
    assert agent.validate_dataset(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",  # empty
        "-leading-dash",  # leading char must be alnum/underscore
        "rpool@snap",  # snapshot name, not dataset
        "rpool home",  # space
        "rpool;rm -rf",  # shell metacharacter
        "rpool\nfoo",  # newline
        "rpool/$(whoami)",  # command substitution chars
    ],
)
def test_validate_dataset_rejects_invalid(name: str) -> None:
    with pytest.raises(agent.InvalidParams):
        agent.validate_dataset(name)


# ---- validate_snapshot ------------------------------------------------------


def test_validate_snapshot_returns_components() -> None:
    ds, snap = agent.validate_snapshot("rpool/home/youruser@auto_2026-05-22")
    assert ds == "rpool/home/youruser"
    assert snap == "auto_2026-05-22"


@pytest.mark.parametrize(
    "name",
    [
        "rpool/home/youruser",  # missing @
        "@snap",  # missing dataset
        "rpool/home/youruser@",  # empty snap
        "rpool/home/youruser@snap@extra",  # double @
        "rpool/home/youruser@snap with space",
        "rpool/home/youruser@snap;ls",
    ],
)
def test_validate_snapshot_rejects_invalid(name: str) -> None:
    with pytest.raises(agent.InvalidParams):
        agent.validate_snapshot(name)


# ---- validate_user_path -----------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["", "etc/foo", "/etc/foo", "a/b/c", "deeply/nested/dir/file.txt"],
)
def test_validate_user_path_accepts_relative_and_strips_leading_slash(path: str) -> None:
    p = agent.validate_user_path(path)
    assert not p.is_absolute()


@pytest.mark.parametrize(
    "path",
    [
        "../etc/passwd",
        "etc/../../../etc/passwd",
        "foo/..",
        "../",
        "a/b/../../c",
    ],
)
def test_validate_user_path_rejects_parent_segments(path: str) -> None:
    with pytest.raises(agent.PathError):
        agent.validate_user_path(path)


def test_validate_user_path_rejects_non_string() -> None:
    with pytest.raises(agent.InvalidParams):
        agent.validate_user_path(123)  # type: ignore[arg-type]


# ---- validate_positive_int --------------------------------------------------


def test_validate_positive_int_defaults_when_none() -> None:
    assert agent.validate_positive_int(None, name="x", default=42, hard_max=1000) == 42


def test_validate_positive_int_caps_at_hard_max() -> None:
    assert agent.validate_positive_int(99999, name="x", default=10, hard_max=100) == 100


def test_validate_positive_int_rejects_zero_and_negative() -> None:
    with pytest.raises(agent.InvalidParams):
        agent.validate_positive_int(0, name="x", default=10, hard_max=100)
    with pytest.raises(agent.InvalidParams):
        agent.validate_positive_int(-5, name="x", default=10, hard_max=100)


def test_validate_positive_int_rejects_bool_and_non_int() -> None:
    with pytest.raises(agent.InvalidParams):
        agent.validate_positive_int(True, name="x", default=10, hard_max=100)
    with pytest.raises(agent.InvalidParams):
        agent.validate_positive_int("5", name="x", default=10, hard_max=100)
    with pytest.raises(agent.InvalidParams):
        agent.validate_positive_int(1.5, name="x", default=10, hard_max=100)
