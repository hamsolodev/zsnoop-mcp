"""MCP server for read-only ZFS snapshot exploration on remote hosts."""

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

__version__ = "0.1.0"

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
