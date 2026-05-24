# 9. Build, package, release

## What

The project uses [**uv**](https://docs.astral.sh/uv/) for environment
management and [**hatchling**](https://hatch.pypa.io/) as the build
backend. Both are configured in
[`pyproject.toml`]({{ config.repo_url }}/src/branch/{{ repo_branch }}/pyproject.toml).

## Why this stack

- **uv** because it's an order of magnitude faster than pip for installs,
  has built-in support for dependency groups (PEP 735) and tool-style
  installs (`uv tool install`), and it speaks `uv.lock` out of the box.
- **hatchling** because it has clean support for `force-include` (the
  trick we use to bundle the agent inside the wheel), it's the default in
  the Python ecosystem now, and pyproject.toml is the only config file.

## How — guided tour

### Editable install (development)

```sh
uv sync              # default + dev group
uv sync --group docs # add the docs deps for mkdocs
```

`uv sync` reads `pyproject.toml` + `uv.lock`, creates `.venv`, installs
everything. Subsequent runs are incremental — only fetch what changed.

### Running anything

`uv run` execs a command inside the project's venv with the project
itself on `PYTHONPATH`:

```sh
uv run zsnoop-mcp                  # the CLI entrypoint
uv run pytest                       # tests
uv run mkdocs serve                 # this docs site, live-reloaded
uv run python -c "import zsnoop_mcp; print(zsnoop_mcp.__version__)"
```

No need to manually `source .venv/bin/activate` — `uv run` is the
recommended pattern.

### Building a wheel

```sh
rm -rf dist/
uv build
unzip -l dist/zsnoop_mcp-0.1.0-py3-none-any.whl
```

The wheel should contain:

```text
zsnoop_mcp/__init__.py
zsnoop_mcp/__main__.py
zsnoop_mcp/_agent_source/zfs_snoop_agent.py   # <-- force-included
zsnoop_mcp/config.py
zsnoop_mcp/server.py
zsnoop_mcp/timeparse.py
zsnoop_mcp/transport.py
zsnoop_mcp-0.1.0.dist-info/...
```

If `_agent_source/zfs_snoop_agent.py` is missing, `find_agent_source()`
will fall back to walking up from `__file__` (which works during
development but breaks after `pip install` / `uv tool install`).

### The force-include trick

This is the line in [pyproject.toml]({{ config.repo_url }}/src/branch/{{ repo_branch }}/pyproject.toml) that ships the
agent inside the wheel:

```toml
[tool.hatch.build.targets.wheel.force-include]
"agent/zfs_snoop_agent.py" = "zsnoop_mcp/_agent_source/zfs_snoop_agent.py"
```

Why this is needed: the agent is intentionally **not** a Python module of
the package. It's a standalone script designed to be sent over SSH or run
on a remote host that doesn't have `zsnoop_mcp` installed. But the local
server needs to know its content so it can build the bootstrap stub.
Force-include solves "must be in the wheel for installs, must be a
standalone file for editing".

`find_agent_source()` in
[`server.py`]({{ config.repo_url }}/src/branch/{{ repo_branch }}/src/zsnoop_mcp/server.py) handles both cases —
`importlib.resources` for wheel installs, parent-directory walk for
editable installs.

### Dependency groups

```toml
[project]
dependencies = ["mcp>=1.0", "python-dateutil>=2.9"]

[dependency-groups]
dev  = ["mypy", "pre-commit", "pytest", "pytest-asyncio", "pytest-cov", "ruff", "types-python-dateutil"]
docs = ["mkdocs", "mkdocs-material", "pymdown-extensions"]
all  = [{include-group = "dev"}, {include-group = "docs"}]
```

- `dependencies` = runtime. Anyone installing `zsnoop-mcp` from PyPI gets
  these.
- `[dependency-groups]` (PEP 735) = dev-only. Activated via `uv sync
  --group dev` (the default) or `--group docs`.

### Linting / formatting / type-checking

```sh
uv run ruff check
uv run ruff format
uv run mypy
```

All three are wired into [`.pre-commit-config.yaml`]({{ config.repo_url }}/src/branch/{{ repo_branch }}/.pre-commit-config.yaml).
Mypy runs via `uv run` rather than mirrors-mypy so it sees the project's
actual installed deps (the pytest stubs and the editable `zsnoop_mcp`).

### CVE scanning

```sh
uv run pip-audit --skip-editable
```

[`pip-audit`](https://pypi.org/project/pip-audit/) is PyPA's vulnerability
scanner; it walks the live venv's resolved deps and queries the PyPI
advisory database (which mirrors OSV.dev). The pre-commit hook runs it
**only when `pyproject.toml` or `uv.lock` change**, so day-to-day commits
stay fast; we also re-run it manually before publishing (see
[PUBLISHING.md](../PUBLISHING.md)) to catch advisories that may have
landed against an otherwise-unchanged pinned dep.

`--skip-editable` excludes the in-tree `zsnoop-mcp` itself, which isn't
on PyPI yet and so can't be looked up. Findings exit nonzero and block
the commit; the resolutions are bump the dep (`uv lock --upgrade-package
<name>`), or — for a deliberately-accepted finding — `--ignore-vuln <ID>`
with a comment in the hook config explaining why.

### Releasing to PyPI

See [docs/PUBLISHING.md](../PUBLISHING.md) for the two release paths
(manual `uv publish` vs trusted publishing via CI). The pre-flight
checklist there walks you through everything.

## What to read next

→ [Adding a new tool](10-extending.md) — the worked example that
exercises every layer you've now read about.
