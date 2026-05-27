# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] — 2026-05-27

### Added

- **`queried_at` timestamp on every agent response.** Server's `_call()`
  injects a UTC ISO 8601 timestamp into every result before returning it,
  so the LLM can reason about data freshness instead of treating an
  in-context result as still-current on a later turn.
- **`checksum_file` tool** (agent-side). Streams a full-file SHA-256 in
  64 KiB chunks; no `max_bytes` parameter (unlike `read_file`'s 4 MiB
  cap) — verifies arbitrarily large recovered files without shipping
  bytes through the MCP layer. Refuses symlinks (G3) and non-regular
  files. Hard cap **256 MiB** per file (`MAX_CHECKSUM_FILESIZE`), exposed
  via `agent_info.limits.max_checksum_filesize`; for larger files, run
  `sha256sum` directly on the host.
- **`fetch_file` tool** (server-side). Copies one file from a snapshot to
  a local path via SCP — or `cp -a` for `transport = "local"` hosts. Gets
  the dataset's mountpoint via `dataset_properties`, then SCPs from
  `<mountpoint>/.zfs/snapshot/<snap>/<path>`. Refuses to overwrite an
  existing file unless `overwrite=true`; refuses directory destinations
  outright (would otherwise copy *into* the directory and break the
  returned `local_path` / `size_bytes`). Stdin wired to `/dev/null` so a
  misconfigured `scp` cannot hang on prompts despite `BatchMode=yes`.
  300 s timeout; on timeout the subprocess is `kill()`ed and reaped
  rather than leaked.
- **`fetch_dir` tool** (server-side). Recursive variant of `fetch_file`
  (`scp -r` / `cp -ar`). Requires `local_path` to not exist — `scp -r`
  and `cp -ar` have ambiguous semantics for existing destinations
  (copy-*into* vs populate), and rather than guess we make the caller
  clear it first.
- **`docs/USAGE.md`** extended with example prompts for the three new
  tools — file recovery to disk and post-recovery integrity verification.

### Changed

- **Agent version** bumped to **0.2.0**.
- **`local_path` validation tightened** for `fetch_file` / `fetch_dir`:
  rejects non-absolute paths (was silently resolving against the server's
  CWD), and requires the parent path component to actually be a directory
  on disk (clearer error than the post-SCP failure when the parent exists
  as a regular file).

## [0.1.2] — 2026-05-26

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
  hard cap. An over-budget response now raises a clear
  `TransportError("...emitted a line larger than ... bytes")` instead
  of a raw asyncio `ValueError`.
- **Transport protocol-corruption errors left the pipe desynced.** Any
  `TransportError` from `_recv` / `_call_once` (oversize line, garbage
  JSON, id mismatch, malformed JSON-RPC frame) previously propagated
  out without closing the subprocess. The agent's leftover bytes
  remained in the pipe and the next call would surface as
  `id mismatch on <host>: sent N, got M`. These error paths now
  `_close_proc()` before raising so `_ensure_alive` respawns a fresh
  subprocess on the next call. Regression test pins the recovery
  behaviour.
- **`_drain_stderr` race on close.** Pre-existing latent bug: the
  stderr drainer read `self._proc.stderr` on every loop iteration, so
  if `_close_proc` set `self._proc = None` before cancelling the
  drainer task, the next iteration NPE'd. Newly exposed by the
  protocol-error close path above. Fix: capture `proc.stderr` locally
  at drainer entry.
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

[Unreleased]: https://github.com/hamsolodev/zsnoop-mcp/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/hamsolodev/zsnoop-mcp/releases/tag/v0.2.0
[0.1.2]: https://github.com/hamsolodev/zsnoop-mcp/releases/tag/v0.1.2
[0.1.1]: https://github.com/hamsolodev/zsnoop-mcp/releases/tag/v0.1.1
[0.1.0]: https://github.com/hamsolodev/zsnoop-mcp/releases/tag/v0.1.0
