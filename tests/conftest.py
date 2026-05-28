"""Shared test fixtures: fake zfs, on-disk snapshot tree, patching helpers."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

import zfs_snoop_agent as agent


class FakeZfs:
    """Test double for ``run_zfs``.

    Register responses with :meth:`add`; the call site receives the canned
    stdout. Unregistered calls raise ``ZfsError`` so missing fixture setup
    fails loudly instead of silently returning empty output.
    """

    def __init__(self) -> None:
        self._responses: dict[tuple[str, ...], str] = {}
        self.calls: list[tuple[str, ...]] = []

    def add(self, args: list[str], stdout: str) -> None:
        self._responses[tuple(args)] = stdout

    def __call__(self, args: list[str], *, timeout: float | None = None) -> str:
        # `timeout` accepted (per GH #7 plumbing) but ignored: this fake never
        # actually waits, so we just record the call shape.
        del timeout
        self.calls.append(tuple(args))
        try:
            return self._responses[tuple(args)]
        except KeyError as e:
            raise agent.ZfsError(f"unexpected zfs call: {args!r}") from e


@pytest.fixture(autouse=True)
def _reset_mountpoint_cache() -> None:
    """Clear the in-process mountpoint LRU between tests.

    The agent caches ``get_dataset_mountpoint`` for its lifetime — fine in
    production (mountpoints rarely change at runtime), but in the test
    suite different tests register different responses for the same
    dataset and would see stale cached values without this reset.
    """
    agent.get_dataset_mountpoint.cache_clear()


@pytest.fixture
def fake_zfs(monkeypatch: pytest.MonkeyPatch) -> FakeZfs:
    """Replace ``agent.run_zfs`` with a :class:`FakeZfs` instance."""
    fake = FakeZfs()
    monkeypatch.setattr(agent, "run_zfs", fake)
    return fake


@pytest.fixture
def snapshot_tree(tmp_path: Path) -> dict[str, Any]:
    """Build a realistic on-disk snapshot layout.

    Layout::

        <tmp>/
          .zfs/snapshot/snap1/
            hello.txt
            big.bin            (binary, > 1 MiB to test truncation)
            sub/nested.txt
            sub/link_to_hello -> ../hello.txt    (in-snapshot symlink)
            escape -> /etc/passwd                (escape attempt)
            empty_dir/

    Returns a dict with ``mountpoint`` (the tmp_path), ``snapshot_name``
    (``testpool/test@snap1``), ``snap_root`` (the ``.zfs/snapshot/snap1`` dir),
    and ``files`` (dict of named paths inside it).
    """
    snap_root = tmp_path / ".zfs" / "snapshot" / "snap1"
    snap_root.mkdir(parents=True)
    (snap_root / "hello.txt").write_text("hello, world!\n")
    # 0xff is not a valid UTF-8 start byte; ensures base64 fallback triggers.
    (snap_root / "big.bin").write_bytes(b"\xff\xfe\xfd\xfc" * 300_000)  # ~1.14 MiB
    sub = snap_root / "sub"
    sub.mkdir()
    (sub / "nested.txt").write_text("nested content\n")
    (sub / "link_to_hello").symlink_to("../hello.txt")
    (snap_root / "escape").symlink_to("/etc/passwd")
    (snap_root / "empty_dir").mkdir()
    return {
        "mountpoint": tmp_path,
        "snapshot_name": "testpool/test@snap1",
        "snap_root": snap_root,
        "files": {
            "hello": snap_root / "hello.txt",
            "big": snap_root / "big.bin",
            "nested": sub / "nested.txt",
            "in_link": sub / "link_to_hello",
            "escape_link": snap_root / "escape",
            "empty_dir": snap_root / "empty_dir",
        },
    }


@pytest.fixture
def mock_mountpoint(
    snapshot_tree: dict[str, Any], monkeypatch: pytest.MonkeyPatch, fake_zfs: FakeZfs
) -> dict[str, Any]:
    """Wire :func:`agent.get_dataset_mountpoint` to the on-disk snapshot_tree.

    Also pre-registers a ``zfs get mountpoint`` response so any code path that
    goes through the real :func:`agent.snapshot_root` resolves correctly.
    """
    dataset = "testpool/test"
    fake_zfs.add(
        ["get", "-H", "-p", "-o", "value", "mountpoint", dataset],
        f"{snapshot_tree['mountpoint']}\n",
    )
    return snapshot_tree


@pytest.fixture
def fake_zfs_bin(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Create a tiny POSIX-shell ``zfs`` on a fresh dir and return that dir.

    The script echoes a configured response from ``FAKE_ZFS_RESPONSE`` and
    exits with ``FAKE_ZFS_EXIT`` (default 0). Used for the subprocess
    integration test only; all other tests should patch ``run_zfs`` directly.
    """
    bindir = tmp_path_factory.mktemp("bin")
    script = bindir / "zfs"
    script.write_text(
        textwrap.dedent("""\
            #!/bin/sh
            printf '%s' "${FAKE_ZFS_RESPONSE-}"
            exit "${FAKE_ZFS_EXIT-0}"
            """),
    )
    script.chmod(0o755)
    return bindir
