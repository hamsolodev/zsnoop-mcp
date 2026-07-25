"""MCP server for read-only ZFS snapshot exploration on remote hosts."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _v

from zsnoop_mcp.config import Config, ConfigError, HostConfig, load_config, parse_config
from zsnoop_mcp.transport import (
    AgentConnection,
    AgentRpcError,
    ConnectionPool,
    TransportError,
    build_argv,
    build_local_argv,
    build_ssh_argv,
)

try:
    __version__ = _v("zsnoop-mcp")
except PackageNotFoundError:  # running from a source checkout without install
    __version__ = "0.0.0+unknown"

__all__ = [
    "AgentConnection",
    "AgentRpcError",
    "Config",
    "ConfigError",
    "ConnectionPool",
    "HostConfig",
    "TransportError",
    "build_argv",
    "build_local_argv",
    "build_ssh_argv",
    "load_config",
    "parse_config",
]
