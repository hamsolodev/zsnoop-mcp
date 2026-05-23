"""Smoke check for the zsnoop_mcp package itself (real content lands in phase 4)."""

from __future__ import annotations

import zsnoop_mcp


def test_package_version_matches_pyproject() -> None:
    assert zsnoop_mcp.__version__ == "0.1.0"
