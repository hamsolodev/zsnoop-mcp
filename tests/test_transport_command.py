"""Unit tests for SSH/local argv construction (no subprocess spawned)."""

from __future__ import annotations

import base64
import shlex

import pytest

from zsnoop_mcp.config import HostConfig
from zsnoop_mcp.transport import (
    DEFAULT_SSH_OPTIONS,
    build_local_argv,
    build_ssh_argv,
)

AGENT_SOURCE = "#!/usr/bin/env python3\nprint('hello from agent')\n"
LOCAL_AGENT_PATH = "agent/zfs_snoop_agent.py"  # relative path; never written


def _unquote_remote(argv: list[str]) -> list[str]:
    """Reverse the shell quoting that build_ssh_argv applied to the remote cmd."""
    return shlex.split(argv[-1])


def _decode_bootstrap_payload(argv: list[str]) -> str:
    """Pull the base64-encoded agent source out of the bootstrap stub."""
    parts = _unquote_remote(argv)
    # Last element is the python3 -c stub (or the only remaining element if no sudo).
    stub = parts[-1]
    start = stub.index("base64.b64decode('") + len("base64.b64decode('")
    end = stub.index("')", start)
    return base64.b64decode(stub[start:end]).decode("utf-8")


def test_bootstrap_argv_contains_default_ssh_options() -> None:
    cfg = HostConfig(name="r2d2", ssh_target="r2d2.lan")
    argv = build_ssh_argv(cfg, AGENT_SOURCE)
    assert argv[0] == "ssh"
    for opt in DEFAULT_SSH_OPTIONS:
        assert opt in argv


def test_bootstrap_argv_appends_ssh_target_and_remote_command() -> None:
    cfg = HostConfig(name="r2d2", ssh_target="r2d2.lan")
    argv = build_ssh_argv(cfg, AGENT_SOURCE)
    # ssh ... -- <target> <remote-shell-command>
    assert "--" in argv
    sep = argv.index("--")
    assert argv[sep + 1] == "r2d2.lan"
    remote_cmd = argv[sep + 2]
    assert "python3" in remote_cmd
    assert "-c" in remote_cmd


def test_bootstrap_payload_decodes_to_agent_source() -> None:
    cfg = HostConfig(name="r2d2", ssh_target="r2d2.lan")
    argv = build_ssh_argv(cfg, AGENT_SOURCE)
    assert _decode_bootstrap_payload(argv) == AGENT_SOURCE


def test_bootstrap_argv_prepends_sudo_when_requested() -> None:
    cfg = HostConfig(name="c3po", ssh_target="c3po.lan", sudo=True)
    argv = build_ssh_argv(cfg, AGENT_SOURCE)
    parts = _unquote_remote(argv)
    assert parts[0] == "sudo"
    assert parts[1].endswith("python3")
    # And the payload still decodes correctly.
    assert _decode_bootstrap_payload(argv) == AGENT_SOURCE


def test_bootstrap_argv_honours_remote_python() -> None:
    cfg = HostConfig(name="r2d2", ssh_target="r2d2.lan", remote_python="python3.11")
    argv = build_ssh_argv(cfg, AGENT_SOURCE)
    parts = _unquote_remote(argv)
    assert parts[0] == "python3.11"


def test_bootstrap_argv_includes_ssh_options() -> None:
    cfg = HostConfig(
        name="r2d2",
        ssh_target="r2d2.lan",
        ssh_options=("-o", "ConnectTimeout=5"),
    )
    argv = build_ssh_argv(cfg, AGENT_SOURCE)
    # Per-host options come after defaults but before '--'.
    sep = argv.index("--")
    assert "ConnectTimeout=5" in argv[:sep]


def test_preinstalled_argv_runs_agent_path_directly() -> None:
    cfg = HostConfig(
        name="c3po",
        ssh_target="c3po.lan",
        agent_mode="preinstalled",
        agent_path="/usr/local/bin/zfs-snoop-agent",
    )
    argv = build_ssh_argv(cfg, AGENT_SOURCE)
    parts = _unquote_remote(argv)
    assert parts == ["/usr/local/bin/zfs-snoop-agent"]


def test_preinstalled_argv_with_sudo() -> None:
    cfg = HostConfig(
        name="c3po",
        ssh_target="c3po.lan",
        agent_mode="preinstalled",
        agent_path="/usr/local/bin/zfs-snoop-agent",
        sudo=True,
    )
    argv = build_ssh_argv(cfg, AGENT_SOURCE)
    parts = _unquote_remote(argv)
    assert parts == ["sudo", "/usr/local/bin/zfs-snoop-agent"]


def test_preinstalled_quotes_paths_with_spaces() -> None:
    cfg = HostConfig(
        name="c3po",
        ssh_target="c3po.lan",
        agent_mode="preinstalled",
        agent_path="/path with spaces/agent.py",
    )
    argv = build_ssh_argv(cfg, AGENT_SOURCE)
    parts = _unquote_remote(argv)
    assert parts == ["/path with spaces/agent.py"]


def test_build_local_argv_default() -> None:
    argv = build_local_argv(LOCAL_AGENT_PATH)
    assert argv == ["python3", LOCAL_AGENT_PATH]


def test_build_local_argv_with_sudo() -> None:
    argv = build_local_argv(LOCAL_AGENT_PATH, sudo=True)
    assert argv == ["sudo", "python3", LOCAL_AGENT_PATH]


def test_preinstalled_without_agent_path_raises() -> None:
    # HostConfig validates this at construction, so this never reaches
    # build_ssh_argv. We assert the validation does fire.
    with pytest.raises(Exception, match="agent_path is required"):
        HostConfig(name="x", ssh_target="x", agent_mode="preinstalled")
