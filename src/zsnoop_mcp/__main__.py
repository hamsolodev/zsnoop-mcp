"""CLI entrypoint: load config, build server, serve MCP over stdio."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from zsnoop_mcp.config import ConfigError, load_config
from zsnoop_mcp.server import create_server, find_agent_source
from zsnoop_mcp.transport import ConnectionPool


def _default_config_path() -> Path:
    if override := os.environ.get("ZSNOOP_CONFIG"):
        return Path(override)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "zsnoop-mcp" / "hosts.toml"


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zsnoop-mcp",
        description="MCP server for read-only ZFS snapshot exploration on remote hosts.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to hosts.toml. Defaults to $ZSNOOP_CONFIG or "
        "$XDG_CONFIG_HOME/zsnoop-mcp/hosts.toml.",
    )
    parser.add_argument(
        "--agent-source",
        type=Path,
        default=None,
        help="Path to zfs_snoop_agent.py. Defaults to the packaged version.",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("ZSNOOP_LOG_LEVEL", "WARNING"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level for stderr (default: WARNING).",
    )
    return parser


async def _amain(args: argparse.Namespace) -> None:
    config = load_config(args.config or _default_config_path())
    agent_source = (
        args.agent_source.read_text(encoding="utf-8") if args.agent_source else find_agent_source()
    )
    async with ConnectionPool(config, agent_source) as pool:
        server = create_server(pool, config)
        await server.run_stdio_async()


def main() -> int:
    """Console-script entry point for ``zsnoop-mcp``."""
    args = _build_argparser().parse_args()
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    try:
        asyncio.run(_amain(args))
    except ConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
