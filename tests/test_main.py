"""CLI entry-point smoke tests: error paths produce clean exit codes + messages."""

from __future__ import annotations

from pathlib import Path

import pytest

from zsnoop_mcp.__main__ import main


def test_missing_agent_source_file_returns_clean_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pointing --agent-source at a non-existent path must exit with a
    clean message instead of an uncaught FileNotFoundError traceback."""
    # Minimal valid config so we get past config loading.
    cfg = tmp_path / "hosts.toml"
    cfg.write_text(
        '[hosts.r2d2]\nssh_target = "r2d2.lan"\nagent_mode = "bootstrap"\n',
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "zsnoop-mcp",
            "--config",
            str(cfg),
            "--agent-source",
            str(tmp_path / "does_not_exist.py"),
        ],
    )
    rc = main()
    assert rc == 2
    err = capsys.readouterr().err
    assert "does_not_exist.py" in err
    # No Python traceback frame.
    assert "Traceback" not in err
