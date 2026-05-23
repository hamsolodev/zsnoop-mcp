"""Tests for the TOML host config loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from zsnoop_mcp.config import (
    Config,
    ConfigError,
    HostConfig,
    load_config,
    parse_config,
)


def test_minimal_bootstrap_host() -> None:
    cfg = parse_config({"hosts": {"r2d2": {"ssh_target": "r2d2.lan"}}})
    host = cfg.host("r2d2")
    assert host.ssh_target == "r2d2.lan"
    assert host.agent_mode == "bootstrap"
    assert host.sudo is False
    assert host.remote_python == "python3"
    assert host.ssh_options == ()
    assert host.pools == ()


def test_full_preinstalled_host() -> None:
    cfg = parse_config(
        {
            "hosts": {
                "c3po": {
                    "ssh_target": "c3po.lan",
                    "agent_mode": "preinstalled",
                    "agent_path": "/usr/local/bin/zfs-snoop-agent",
                    "sudo": True,
                    "remote_python": "python3.11",
                    "ssh_options": ["-o", "ConnectTimeout=5"],
                    "pools": ["tank", "boot"],
                },
            },
        },
    )
    host = cfg.host("c3po")
    assert host.agent_mode == "preinstalled"
    assert host.agent_path == "/usr/local/bin/zfs-snoop-agent"
    assert host.sudo is True
    assert host.ssh_options == ("-o", "ConnectTimeout=5")
    assert host.pools == ("tank", "boot")


def test_preinstalled_requires_agent_path() -> None:
    with pytest.raises(ConfigError, match="agent_path is required"):
        parse_config(
            {"hosts": {"x": {"ssh_target": "x", "agent_mode": "preinstalled"}}},
        )


def test_invalid_agent_mode_rejected() -> None:
    with pytest.raises(ConfigError, match="agent_mode"):
        parse_config(
            {"hosts": {"x": {"ssh_target": "x", "agent_mode": "carrier-pigeon"}}},
        )


def test_unknown_keys_rejected() -> None:
    with pytest.raises(ConfigError, match="unknown keys"):
        parse_config({"hosts": {"x": {"ssh_target": "x", "wat": 1}}})


def test_missing_ssh_target_rejected_when_transport_ssh() -> None:
    with pytest.raises(ConfigError, match="ssh_target"):
        parse_config({"hosts": {"x": {}}})


def test_ssh_target_optional_when_transport_local() -> None:
    cfg = parse_config({"hosts": {"localbox": {"transport": "local"}}})
    host = cfg.host("localbox")
    assert host.transport == "local"
    assert host.ssh_target == ""


def test_invalid_transport_rejected() -> None:
    with pytest.raises(ConfigError, match="transport"):
        parse_config({"hosts": {"x": {"transport": "carrier-pigeon"}}})


def test_non_string_ssh_target_rejected() -> None:
    with pytest.raises(ConfigError, match="must be a string"):
        parse_config({"hosts": {"x": {"ssh_target": 1234}}})


def test_non_bool_sudo_rejected() -> None:
    with pytest.raises(ConfigError, match="must be a boolean"):
        parse_config({"hosts": {"x": {"ssh_target": "x", "sudo": "yes"}}})


def test_ssh_options_must_be_list_of_strings() -> None:
    with pytest.raises(ConfigError, match="ssh_options"):
        parse_config({"hosts": {"x": {"ssh_target": "x", "ssh_options": [1, 2]}}})


def test_pools_must_be_list_of_strings() -> None:
    with pytest.raises(ConfigError, match="pools"):
        parse_config({"hosts": {"x": {"ssh_target": "x", "pools": "tank"}}})


def test_empty_config_rejected() -> None:
    with pytest.raises(ConfigError, match="at least one"):
        parse_config({"hosts": {}})


def test_missing_hosts_table_rejected() -> None:
    with pytest.raises(ConfigError, match="\\[hosts\\] table"):
        parse_config({})


def test_load_config_round_trip(tmp_path: Path) -> None:
    cfg_file = tmp_path / "hosts.toml"
    cfg_file.write_text(
        """\
[hosts.r2d2]
ssh_target = "r2d2.lan"

[hosts.c3po]
ssh_target = "c3po.lan"
sudo = true
""",
    )
    cfg = load_config(cfg_file)
    assert isinstance(cfg, Config)
    assert set(cfg.hosts) == {"r2d2", "c3po"}
    assert cfg.hosts["c3po"].sudo is True


def test_load_config_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "does_not_exist.toml")


def test_load_config_invalid_toml(tmp_path: Path) -> None:
    cfg_file = tmp_path / "bad.toml"
    cfg_file.write_text("[hosts.r2d2\nbroken")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(cfg_file)


def test_unknown_host_lookup() -> None:
    cfg = parse_config({"hosts": {"x": {"ssh_target": "x"}}})
    with pytest.raises(ConfigError, match="unknown host"):
        cfg.host("y")


def test_host_config_is_immutable() -> None:
    host = HostConfig(name="x", ssh_target="x")
    with pytest.raises(AttributeError):
        host.sudo = True  # type: ignore[misc]
