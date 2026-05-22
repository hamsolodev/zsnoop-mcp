"""Smoke test to verify the package imports. Replaced by real tests in phase 2."""

from __future__ import annotations

import zsnoop_mcp


def test_package_version() -> None:
    assert zsnoop_mcp.__version__ == "0.1.0"
