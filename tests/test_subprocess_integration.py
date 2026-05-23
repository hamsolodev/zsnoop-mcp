"""Integration test for ``run_zfs``: real subprocess + fake ``zfs`` on PATH."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import zfs_snoop_agent as agent


def _prepend_path(monkeypatch: pytest.MonkeyPatch, bindir: Path) -> None:
    existing = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", f"{bindir}:{existing}")


def test_run_zfs_returns_stdout(fake_zfs_bin: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepend_path(monkeypatch, fake_zfs_bin)
    monkeypatch.setenv("FAKE_ZFS_RESPONSE", "hello from fake zfs\n")
    out = agent.run_zfs(["list"])
    assert out == "hello from fake zfs\n"


def test_run_zfs_raises_zfserror_on_nonzero_exit(
    fake_zfs_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepend_path(monkeypatch, fake_zfs_bin)
    monkeypatch.setenv("FAKE_ZFS_RESPONSE", "")
    monkeypatch.setenv("FAKE_ZFS_EXIT", "2")
    with pytest.raises(agent.ZfsError):
        agent.run_zfs(["list"])


def test_run_zfs_raises_when_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/this/path/does/not/exist")
    with pytest.raises(agent.ZfsError, match="not found"):
        agent.run_zfs(["list"])
