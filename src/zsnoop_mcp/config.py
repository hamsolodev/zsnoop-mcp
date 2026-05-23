"""Host configuration: dataclasses + TOML loading.

A complete config file (see ``docs/INSTALL.md`` for the canonical example)::

    # ~/.config/zsnoop-mcp/hosts.toml

    [hosts.r2d2]
    ssh_target = "r2d2.example.com"
    agent_mode = "bootstrap"
    sudo = false
    pools = ["rpool", "bpool"]

    [hosts.c3po]
    ssh_target = "c3po.example.com"
    agent_mode = "preinstalled"
    agent_path = "/home/youruser/bin/zfs-snoop-agent"
    sudo = true
    remote_python = "python3"
    ssh_options = ["-o", "ConnectTimeout=5"]
    pools = ["tank"]

``pools`` is metadata only at this layer; the agent itself queries whichever
datasets it has permission to see. The MCP tool layer (phase 4) uses ``pools``
to scope tool descriptions and validate caller-supplied dataset names.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

AgentMode = Literal["bootstrap", "preinstalled"]
_VALID_MODES: tuple[AgentMode, ...] = ("bootstrap", "preinstalled")

Transport = Literal["ssh", "local"]
_VALID_TRANSPORTS: tuple[Transport, ...] = ("ssh", "local")


class ConfigError(ValueError):
    """Raised when a config file or host stanza is malformed."""


@dataclass(frozen=True, slots=True)
class HostConfig:
    """One host the MCP server can talk to (remote over SSH, or local)."""

    name: str
    ssh_target: str = ""
    transport: Transport = "ssh"
    agent_mode: AgentMode = "bootstrap"
    agent_path: str | None = None
    sudo: bool = False
    remote_python: str = "python3"
    ssh_options: tuple[str, ...] = ()
    pools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate the host stanza after construction."""
        if self.transport not in _VALID_TRANSPORTS:
            raise ConfigError(
                f"host {self.name!r}: transport must be one of {_VALID_TRANSPORTS}, "
                f"got {self.transport!r}",
            )
        if self.transport == "ssh" and not self.ssh_target:
            raise ConfigError(
                f"host {self.name!r}: ssh_target is required when transport='ssh'",
            )
        if self.agent_mode not in _VALID_MODES:
            raise ConfigError(
                f"host {self.name!r}: agent_mode must be one of {_VALID_MODES}, "
                f"got {self.agent_mode!r}",
            )
        if self.agent_mode == "preinstalled" and not self.agent_path:
            raise ConfigError(
                f"host {self.name!r}: agent_path is required when agent_mode='preinstalled'",
            )


@dataclass(frozen=True, slots=True)
class Config:
    """Top-level config: a name -> HostConfig mapping."""

    hosts: dict[str, HostConfig] = field(default_factory=dict)

    def host(self, name: str) -> HostConfig:
        """Return the :class:`HostConfig` named *name*, or raise."""
        try:
            return self.hosts[name]
        except KeyError as e:
            raise ConfigError(f"unknown host: {name!r}") from e


def load_config(path: str | Path) -> Config:
    """Load and validate *path* (TOML). Raises :class:`ConfigError` on issues."""
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(f"config file not found: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML in {path}: {e}") from e
    return parse_config(raw)


def parse_config(raw: dict[str, Any]) -> Config:
    """Validate a parsed config dict and return a typed :class:`Config`."""
    if "hosts" not in raw or not isinstance(raw["hosts"], dict):
        raise ConfigError("config must have a [hosts] table with at least one entry")
    hosts: dict[str, HostConfig] = {}
    for name, stanza in raw["hosts"].items():
        if not isinstance(stanza, dict):
            raise ConfigError(f"host {name!r}: stanza must be a table")
        hosts[name] = _parse_host(name, stanza)
    if not hosts:
        raise ConfigError("config must define at least one host")
    return Config(hosts=hosts)


_KNOWN_HOST_KEYS = frozenset(
    {
        "ssh_target",
        "transport",
        "agent_mode",
        "agent_path",
        "sudo",
        "remote_python",
        "ssh_options",
        "pools",
    },
)


def _parse_host(name: str, stanza: dict[str, Any]) -> HostConfig:
    extra = stanza.keys() - _KNOWN_HOST_KEYS
    if extra:
        raise ConfigError(f"host {name!r}: unknown keys: {sorted(extra)}")
    return HostConfig(
        name=name,
        ssh_target=_optional_str(name, stanza, "ssh_target", ""),
        transport=_optional_str(name, stanza, "transport", "ssh"),  # type: ignore[arg-type]
        agent_mode=_optional_str(name, stanza, "agent_mode", "bootstrap"),  # type: ignore[arg-type]
        agent_path=stanza.get("agent_path"),
        sudo=_optional_bool(name, stanza, "sudo", default=False),
        remote_python=_optional_str(name, stanza, "remote_python", "python3"),
        ssh_options=_optional_str_list(name, stanza, "ssh_options"),
        pools=_optional_str_list(name, stanza, "pools"),
    )


def _optional_str(host: str, stanza: dict[str, Any], key: str, default: str) -> str:
    if key not in stanza:
        return default
    value = stanza[key]
    if not isinstance(value, str):
        raise ConfigError(f"host {host!r}: {key!r} must be a string")
    return value


def _optional_bool(host: str, stanza: dict[str, Any], key: str, *, default: bool) -> bool:
    if key not in stanza:
        return default
    value = stanza[key]
    if not isinstance(value, bool):
        raise ConfigError(f"host {host!r}: {key!r} must be a boolean")
    return value


def _optional_str_list(host: str, stanza: dict[str, Any], key: str) -> tuple[str, ...]:
    if key not in stanza:
        return ()
    value = stanza[key]
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ConfigError(f"host {host!r}: {key!r} must be a list of strings")
    return tuple(value)
