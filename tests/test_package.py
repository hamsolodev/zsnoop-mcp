"""Smoke check for the zsnoop_mcp package itself (real content lands in phase 4)."""

from __future__ import annotations

import re

import zsnoop_mcp


def test_package_version_matches_pyproject() -> None:
    # hatch-vcs derives version from git tags (vX.Y.Z -> X.Y.Z.devN+gSHA).
    # Accept any valid PEP 440 version string.
    assert re.match(
        r"^(\d+\.){1,}\d+((\.dev\d+)?(\+[\w.]+)?)?$",
        zsnoop_mcp.__version__,
    ), f"unexpected version format: {zsnoop_mcp.__version__!r}"
