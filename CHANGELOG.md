# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **`zfs diff` timed out on high-churn datasets** (#7). The agent's
  global `ZFS_TIMEOUT_SECONDS = 30 s` applied to every zfs/zpool
  subprocess uniformly, but `zfs diff` between two snapshots of a busy
  multi-TB dataset routinely runs longer. Introduce a separate
  `ZFS_DIFF_TIMEOUT_SECONDS = 300 s` and plumb a per-call timeout
  through `_run_cli`. `diff_snapshots` and `find_deleted` now use the
  longer budget. New constant exposed via `agent_info.limits`.
- **Transport line buffer was too small for large JSON-RPC responses**
  (#8). NDJSON framing puts a whole response on one line; asyncio's
  default 64 KiB `StreamReader` limit caused
  `Separator is found, but chunk is longer than limit` errors when
  `find_deleted` (and similar) returned anything near their default
  result caps. The transport's `create_subprocess_exec` now sets
  `limit=MAX_LINE_BYTES = 16 MiB`, big enough to clear every agent-side
  hard cap.
- **CI Python matrix was theatre.** The matrix labelled jobs `py3.11`,
  `py3.12`, `py3.13` but every job actually ran tests on **3.11**, because
  `uv sync` defaults to the lowest `requires-python`-compatible
  interpreter and ignored the matrix-installed Python. Set
  `UV_PYTHON: ${{ matrix.python }}` on the job; added a
  `uv run python --version` step so a future regression is visible
  in the log instead of silent.

### Changed

- **Documentation source-view links no longer use `mkdocs-macros`.**
  The Jinja-style `{{ config.repo_url }}{{ source_url_prefix }}/{{ repo_branch }}/…`
  placeholders rendered correctly on the MkDocs site but appeared as
  literal text when the same `.md` files were viewed directly on
  github.com (which has no mkdocs to substitute them). Rewrote all
  source-code links to absolute `https://github.com/hamsolodev/zsnoop-mcp/blob/main/…`
  URLs and dropped the `mkdocs-macros-plugin` dev dependency and its
  configuration in `mkdocs.yml`. Docs now look correct in both render
  contexts simultaneously.
- **README install order flipped:** PyPI install ("recommended")
  appears before the worktree-clone path, which is now labelled as
  "for hacking on the code".
- **README "Wire into Claude Code" favors the programmatic `claude mcp
  add` command** over the hand-edited `settings.json` JSON, which is
  kept as a fallback below.

## [0.1.1] — 2026-05-24

### Fixed

- **PyPI README links.** The `docs/...` and `LICENSE` links in the
  README were relative paths, so they rendered as 404s on
  <https://pypi.org/project/zsnoop-mcp/>. Rewritten to absolute
  `https://github.com/hamsolodev/zsnoop-mcp/blob/main/...` URLs so
  both GitHub and PyPI render them correctly.

No code changes; v0.1.0 and v0.1.1 are functionally identical.

## [0.1.0] — 2026-05-24

Initial public release.

### Added

#### MCP tools (25 total)

- **Discovery / introspection.** `list_hosts`, `list_pools`,
  `pool_status` (parsed `zpool status` with vdev tree + per-device
  error counts), `list_datasets`, `dataset_properties` (`zfs get` all-
  or-filtered with property sources), `list_snapshots`,
  `snapshot_cadence` (aggregate stats: counts by retention class,
  biggest gap, total unique bytes), `agent_info`.
- **Navigation / size.** `list_dir`, `size_breakdown`
  (`du --max-depth=1`-style: total + per-immediate-child bytes),
  `top_consumers` (top-N largest files/dirs under a subtree).
- **Content.** `read_file` (bounded, UTF-8 with base64 fallback for
  binary), `find_files` (`fnmatch` search), `content_grep` (regex
  search).
- **History / diff.** `file_history` (every version), `versions_of`
  (distinct content versions only, deduplicated by SHA-256),
  `file_diff` (unified diff of one file across two snapshots),
  `snapshots_containing`, `first_appearance`, `last_appearance`,
  `find_deleted` (paths removed between two snapshots in a window),
  `bisect_change` (binary-search snapshots for a structured-predicate
  flip — `exists`, `contains`, `sha256_equals`, `size_at_least`).
- **Housekeeping.** `stale_snapshots` (snapshots older than a time
  phrase, sorted by unique bytes), `size_delta`, `diff_snapshots`.

#### Transport

- SSH transport (default): one persistent subprocess per host carrying
  line-delimited JSON-RPC. Bootstrap mode streams the agent script over
  stdin on connect; preinstalled mode runs an installed agent script.
- Local transport: run the agent on the same host without SSH.
- Sudo mode (opt-in per host) for reading root-owned snapshot files.

#### Security model

- Six guarantees (G1–G6) covering: no mutation operations exposed
  (explicit allowlist + test), no shell interpretation of user input,
  path inputs cannot escape their snapshot root, all reads bounded,
  ZFS delegation as defence in depth in user mode, all structured logs
  to stderr.
- Documented in [SECURITY.md](docs/SECURITY.md); 32 dedicated security
  tests.

#### Tooling and quality

- `uv` + `hatchling` build pipeline; agent script force-included into
  the wheel.
- `ruff`, `mypy --strict`, `pytest` (211 tests, ~81% coverage).
- `pip-audit` CVE scan in pre-commit (lockfile-scoped) and pre-flight
  release checklist.
- MkDocs Material onboarding tutorial (10 chapters, what/why/how with
  source-linked code excerpts).
- Time-phrase parser (`yesterday`, `last week`, `3 days ago`, etc.)
  resolved locally to ISO 8601 before forwarding to the agent.

#### Disclosure and metadata

- AI-assisted authorship disclosed in README, SECURITY.md,
  pyproject.toml description and keywords.
- PII scrubbed from example values throughout the repo and from git
  history.

[Unreleased]: https://github.com/hamsolodev/zsnoop-mcp/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/hamsolodev/zsnoop-mcp/releases/tag/v0.1.1
[0.1.0]: https://github.com/hamsolodev/zsnoop-mcp/releases/tag/v0.1.0
