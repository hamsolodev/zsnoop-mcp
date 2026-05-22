#!/usr/bin/env python3
"""Remote ZFS snapshot exploration agent.

Single-file, stdlib-only. Designed to run on any Debian 12+ (Python 3.11+) host
either pre-installed at ~/bin/zfs-snoop-agent or streamed over SSH stdin via
`ssh host python3 - < zfs_snoop_agent.py`.

Wire protocol: NDJSON over stdin/stdout. JSON-RPC 2.0.

Implementation lands in phase 2.
"""

from __future__ import annotations

import sys

AGENT_VERSION = "0.1.0"


def main() -> int:
    """Read NDJSON requests from stdin, write responses to stdout."""
    raise NotImplementedError("agent implementation will be written in phase 2")


if __name__ == "__main__":
    sys.exit(main())
