# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/hamsolodev/zsnoop-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/hamsolodev/zsnoop-mcp/releases/tag/v0.1.0
