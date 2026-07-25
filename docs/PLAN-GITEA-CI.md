# zsnoop-mcp Gitea CI, Packaging & Deployment — Implementation Plan

## Current State

| Component | Current Value |
|-----------|--------------|
| `pyproject.toml` version | `0.4.1` (hardcoded) |
| `src/zsnoop_mcp/__init__.py` `__version__` | `0.1.0` (**stale — mismatch**) |
| `agent/zfs_snoop_agent.py` `AGENT_VERSION` | `0.4.1` |
| Build system | hatchling |
| Dependency manager | uv (uv.lock) |
| GitHub CI | `ci.yml` (lint+test+mypy+audit+docs) |
| GitHub release | `release.yml` (build -> PyPI OIDC -> GH Release) |
| Gitea workflows | None exist yet |
| Gitea PyPI registry | Active (memory-mcp packages published) |
| `PYPI_UPLOAD_TOKEN` secret | Created |

## Design Decisions

1. **Dynamic versioning**: Switch from hardcoded `version = "0.4.1"` to `hatch-vcs` (hatchling is already the build backend). Reads version from git tags (`v0.4.1` -> `0.4.1`), matching memory-api's `setuptools-scm` approach but without switching build backends.
2. **Fix stale `__version__`**: `src/zsnoop_mcp/__init__.py` has `__version__ = "0.1.0"` — replace with `importlib.metadata.version("zsnoop-mcp")` matching memory-api/mcp pattern.
3. **Dual CI**: Keep `.github/workflows/` as-is (public PyPI via OIDC). Add `.gitea/workflows/` for private Gitea PyPI. Independent triggers, no conflict.
4. **No Docker image**: zsnoop-mcp is client-side. Only publish track needed.

---

## Phase 1: Dynamic Versioning + Stale Version Fix

**Gates**: Must pass local CI before proceeding.

**Changes**:

1. **`pyproject.toml`** — Add `hatch-vcs` and configure:
   - Add `hatch-vcs` to `[dependency-groups.dev]`
   - Replace `version = "0.4.1"` with `dynamic = ["version"]`
   - Add `[tool.hatch.version.sources]` and `[tool.hatch.version.build-source]` config

2. **`src/zsnoop_mcp/__init__.py`** — Fix stale version:
   - Replace `__version__ = "0.1.0"` with `from importlib.metadata import version as _v; __version__ = _v("zsnoop-mcp")`
   - Add `PackageNotFoundError` fallback to `"0.0.0+unknown"`

3. **`pyproject.toml`** — Add `importlib-metadata` to runtime deps if needed (Python 3.11+ so not needed).

**Verification**:
```bash
uv sync --group dev
uv run pytest -q
uv run ruff check && uv run ruff format --check
uv run mypy
uv run build
unzip -l dist/zsnoop_mcp-*.whl | grep _agent_source
```

---

## Phase 2: Gitea Test Workflow

**Gates**: Phase 1 must be committed and pushed.

**File**: `.gitea/workflows/test.yml`

- Mirrors memory-api's `test.yml`
- Runs on single Python 3.12 (not 3.11-3.13 matrix) to save Gitea runner minutes
- GitHub CI retains full matrix
- `fetch-depth: 0` for hatch-vcs

---

## Phase 3: Gitea Publish Workflow

**Gates**: Phase 2 must be committed and pushed.

**File**: `.gitea/workflows/publish.yml`

- Mirrors memory-api's `publish-mcp.yml`
- Trigger: tag push (`v*`) + workflow_dispatch
- Builds wheel only (not sdist — hatch-vcs can't resolve version without `.git` in sdist)
- Verifies agent is force-included in wheel
- Publishes to `https://git.svc.home.tropism.net/api/packages/mch/pypi` via twine + `PYPI_UPLOAD_TOKEN`

---

## Phase 4: Version Bump + Tag + Push (Manual Release Trigger)

**Gates**: All phases above committed and pushed. Gitea workflows must be green on main.

```bash
# 1. Update changelog — add vX.Y.Z section above "Unreleased"
# 2. Local pre-flight:
uv run pytest -q
uv run ruff check && uv run ruff format --check
uv run mypy
uv run pip-audit --skip-editable
uv run mkdocs build --strict
rm -rf dist/ && uv build
unzip -l dist/zsnoop_mcp-*.whl | grep _agent_source

# 3. Commit, tag, push:
git commit -am "release: vX.Y.Z"
git tag vX.Y.Z
git push origin main vX.Y.Z
```

What happens on tag push:
- Gitea `publish.yml` triggers -> builds wheel -> pushes to Gitea PyPI
- GitHub `release.yml` triggers (if tag pushed to GitHub mirror) -> builds -> pushes to public PyPI -> creates GitHub Release

---

## Files to Create/Modify

| File | Action |
|------|--------|
| `pyproject.toml` | Modify: dynamic version + hatch-vcs + fix runtime deps |
| `src/zsnoop_mcp/__init__.py` | Modify: fix stale `__version__` |
| `.gitea/workflows/test.yml` | **Create** |
| `.gitea/workflows/publish.yml` | **Create** |
| `docs/PUBLISHING.md` | Modify: update per-release checklist for hatch-vcs |

## Phase 5: Docs Correctness Pass

**Gates**: Phases 1–4 committed and pushed. Gitea workflows green on main.

**Scope**: All docs in the repo + relevant Vaults (Syncthing-synced `~/Vaults/reports`).

**Checks**:

1. **Internal consistency** — version numbers, file paths, URLs, commands
2. **CHANGELOG** — verify `v0.4.1` entry is accurate, no stale references
3. **PUBLISHING.md** — hatch-vcs section correct, Gitea URLs valid
4. **INSTALL.md** — pip install path uses correct registry URL
5. **INSTRUCTIONS** (runtime docs) — no stale version references
6. **mkdocs.yml** — nav includes all pages, no broken links
7. **Vaults** — check `~/Vaults/reports` for any zsnoop-mcp reports that reference old version numbers or outdated procedures

**Verification**:
```bash
uv run mkdocs build --strict          # no missing/404 pages
grep -rn "0\.1\.0" src/ docs/        # no stale version strings
grep -rn "0\.4\.1" src/ docs/        # verify intentional references
```

---

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| `hatch-vcs` needs `.git` dir at build time | GitHub/Gitea Actions checkout includes `.git`; local `uv build` also has `.git` |
| `__init__.py` version mismatch (0.1.0 vs 0.4.1) | Fix in Phase 1; `importlib.metadata.version()` reads from installed package metadata |
| Gitea runner doesn't have Docker socket | Not needed - no Docker builds for zsnoop-mcp |
| `twine` not available on Gitea runner | Installed via `uv sync --group dev` or `pip install twine` in the workflow |
| Tag already exists on PyPI | PyPI rejects duplicates. Bump to next version. |
