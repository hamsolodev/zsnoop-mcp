# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] — 2026-06-04

First release with **writable** tools. Read-only-by-default is preserved
for any pre-existing host configuration; the new tools are inert without
explicit per-host opt-in.

### Added

- **`restore_file` / `restore_dir` tools** — restore a snapshot file or
  directory subtree to a path on the *server* (not the workstation —
  that's `fetch_*`). The first writable methods in the project. Disabled
  per host by default; require `allow_restore = true` and a non-empty
  `restore_paths` allowlist in `hosts.toml` to use. Target paths are
  canonicalised (`Path.resolve`) before the allowlist check (so
  ``/srv/../etc/passwd`` and symlinked escapes are rejected, not
  silently restored to the resolved path) and a universal denylist
  refuses ``/proc``, ``/sys``, ``/dev``, and any path containing
  ``/.zfs/snapshot/`` regardless of operator settings. `overwrite=False`
  default; opt-in `backup=True` atomically renames the existing target
  to ``<target>.zsnoop-backup-<UTC-isoformat>`` before replacing it, so
  a wrong restore is reversible. Symlink sources refused (restore the
  file the symlink points to instead). In-tree symlinks preserved as
  symlinks by `restore_dir`. Ownership (uid/gid) not preserved — use
  `sudo` mode for root-owned recoveries. See
  [docs/SECURITY.md](docs/SECURITY.md) G7 for the full validation flow
  and [docs/INSTALL.md](docs/INSTALL.md) for the config keys.
- **`HostConfig.allow_restore` / `restore_paths`** config fields with
  full validation at load time (must be non-empty when enabled, entries
  must be absolute, entries are trailing-slash normalised so
  `/home/mch` and `/home/mch/` are equivalent).

### Changed

- **G1 reframed** (`docs/SECURITY.md`): from "no mutation operations are
  ever exposed" to "no mutation operations *by default*; the two
  `restore_*` methods are opt-in per host and bounded by a mandatory
  per-host path allowlist". Default install of any pre-existing host
  configuration remains read-only because both gating keys default off /
  empty.
- **README** intro softened ("Read-only by default" rather than
  "Read-only"), with a one-sentence note that restore is opt-in.
- **README "Tools exposed to the LLM" table reorganised** into seven
  workflow-grouped subsections (discovery, snapshot inventory,
  browsing & sizing, reading content, comparing & tracing change,
  recovery to workstation, recovery in place on the server). Easier to
  scan and surfaces the read-only vs writable split visually.
- **Agent version** bumped to **0.4.0**. Dispatcher test
  (`test_methods_table_contains_no_mutating_zfs_operations`, renamed
  from `_no_mutating_operations`) clarifies that the forbidden set
  covers mutating ZFS *subcommands*; the writable restore methods use
  `shutil`, not `zfs`, and are gated by server config.
- **Pre-release hardening from the PR #17 review** (Copilot caught a
  real bug + an architectural inconsistency before tag):
  - **Existence checks and backup-path timestamp moved from the server
    to the agent.** The server was doing `Path.exists()` / `Path.is_dir()`
    on `target_path` — but that's a *remote* path; the server was
    checking its own local filesystem. With `backup=True` and an
    existing remote target, a server whose local FS happened not to
    have that path would skip `backup_path` computation and the agent
    would overwrite *without* the requested backup. The server now
    forwards `overwrite` and `backup` as intent flags only; the agent
    does the existence checks and computes its own ISO 8601 backup-path
    timestamp.
  - **Server canonicalisation switched from `Path.resolve()` to
    `posixpath.normpath`** for the same wrong-machine reason — a local
    symlink at the same path as the remote target could either follow
    an unrelated local link (rejecting a valid restore) or miss a
    remote symlink escape. Symlink-escape resistance properly belongs
    on the agent, where the symlinks actually exist; the server now
    does pure-string `..` / `.` / `//` collapsing.
  - **Agent's belt-and-braces target validation** now rejects
    `\n` / `\r` / NUL (was NUL-only), matching the server's invariant
    so a directly-invoked agent honours the same contract.
  - **`m_restore_dir` now explicitly refuses a non-directory existing
    target** with a clear error, rather than falling through to
    `shutil.rmtree` which raised `NotADirectoryError` further down.
    Symmetric with `m_restore_file`'s existing refusal of directory
    targets, and load-bearing now that this check has moved from
    server to agent.
  - **README intro and USAGE.md** corrected — the README still said
    "No mutation operations are ever exposed" near the top, and USAGE
    said `restore_file` "is the only writable tool" (singular).

## [0.3.1] — 2026-05-28

Field-testing every tool against a live pool surfaced four issues, all
fixed here. No API changes; agent version bumped to 0.3.1.

### Fixed

- **`fetch_file` / `fetch_dir` failed on any path containing a space or
  other special character** — a regression from the 0.3.0 shell-quoting
  fix. That fix wrapped the remote path in `shlex.quote()`, which is
  correct only when `scp` shells out under the legacy SCP protocol;
  modern `scp` (OpenSSH ≥ 9.0) uses the SFTP backend and treats the path
  *literally*, so the added quotes became part of the filename and the
  transfer failed with "No such file or directory". The fetch transport
  now drives `sftp` in batch mode (`sftp -b -`) with the path quoted for
  sftp's own client-side lexer. sftp never invokes a remote shell, so
  this is correct for spaces / glob chars / shell metacharacters *and*
  has no injection surface — independent of the client's OpenSSH version.
  Paths containing a newline, carriage-return, or NUL are rejected at the
  boundary (`_validate_fetch_path` / `_validate_local_dest` / `_sftp_quote`):
  the batch script is line-oriented, so those characters can't be contained
  by quoting, and refusing them turns a fragile implicit defence into an
  explicit one (thanks to the PR #16 review for flagging this).
- **`diff_snapshots` and `find_deleted` returned ZFS-escaped paths.**
  `zfs diff` renders any byte outside printable ASCII as `\NNNN`
  (octal), so a file under `Tax 2026/` came back as `Tax\00402026/…` —
  which then failed to round-trip into `read_file` / `fetch_file`. The
  agent now decodes the escaping (reconstructing multi-byte UTF-8
  correctly) so the returned `path` is the real on-disk path.
- **`file_history` flooded its response with child-dataset noise.** It
  walked the recursive snapshot list, so on a dataset with children every
  child snapshot appeared as a `present: false` entry (the bulk of the
  output on nested layouts). It now scopes to the exact dataset, matching
  `bisect_change`. This also speeds up `versions_of`,
  `snapshots_containing`, and `first`/`last_appearance`, which delegate to
  it.
- **`snapshot_cadence` reported a meaningless host-wide "biggest gap".**
  With `dataset` omitted the gap was computed across the merged timelines
  of unrelated datasets, and the boundary-snapshot names were unreliable
  when datasets shared snapshot timestamps (the common auto-snapshot
  case). `biggest_gap_*` is now computed only for a named dataset's own
  timeline (excluding descendants, carrying names alongside timestamps to
  avoid collisions) and is `null` host-wide.

## [0.3.0] — 2026-05-28

### Security

- **Shell injection in `fetch_file` / `fetch_dir` SCP source path.** Both
  tools built their SCP source as `f"{host}:{remote_path}"` and handed it
  to `scp`, which passes the path component to a *remote* shell — so
  shell metacharacters (`;`, `$()`, backticks, spaces) in either the
  snapshot name or the requested filename could execute commands on the
  remote host. Now `_parse_snapshot_name` rejects snapshot strings that
  don't match ZFS's restrictive naming rules (defence in depth, fails
  fast at the server boundary), and `_build_fetch_cmd` `shlex.quote()`s
  the full remote path before interpolation so legitimate filenames
  containing metacharacters can no longer reach the remote shell
  unescaped. Restores conformance with SECURITY.md G2.

### Performance

- **`get_dataset_mountpoint` is now memoised** (`functools.lru_cache`).
  Methods that iterate snapshots of a dataset (`file_history`,
  `versions_of`, `bisect_change`, `snapshots_containing`,
  `first_appearance`, `last_appearance`) called
  `snapshot_root` → `get_dataset_mountpoint` → `zfs get mountpoint` on
  *every* iteration, when the mountpoint of a dataset is fixed across
  all its snapshots. On a dataset with 1000 snapshots, one history
  operation was spending an extra ~50 s in redundant subprocess calls.
  Restart the agent (or call `get_dataset_mountpoint.cache_clear()`) to
  pick up an operator's `zfs set mountpoint=…` change.

### Fixed

- **Agent no longer crashes on a non-JSON-serialisable handler result.**
  The agent's main loop called `json.dumps(response)` directly; if a
  handler ever returned bytes / a set / a datetime / etc., the dumps
  would raise and kill the agent — leaving the server hung waiting for
  a reply. The serialise step is now wrapped: a synthetic `INTERNAL_ERROR`
  response is emitted instead and the agent stays up. No current handler
  produces such a value, but defensive coding here keeps a future bug
  from becoming a transport-level hang.
- **`content_grep` could OOM on pathological files.** Two issues: it
  pre-materialised the full file list under the search base before
  scanning (a 1 M-file snapshot would build a 1 M-element list before
  the first match), and it iterated each file's lines in binary mode —
  so a single binary file with no newline (or any 1 GiB single-line text
  file) was read entirely into memory before either a UnicodeDecodeError
  or a useless match was raised. Now: walk lazily so iteration stops the
  moment `max_results` is hit; sniff the first 8 KiB for null bytes and
  skip the file if found; cap each line read at 1 MiB
  (`MAX_GREP_LINE_BYTES`) and move on if exceeded.
- **`agent_path` was passed through config unchecked.** A non-string
  value (e.g. `agent_path = 42`) would slip past the loader and crash
  `subprocess.exec` later with a TypeError. Now validated as a nullable
  string at config-load time, matching the other host-stanza fields.
- **`today` / `yesterday` docstring corrected.** The docstring claimed
  "local time" but the implementation uses UTC midnight (consistent with
  ZFS's UTC `creation` timestamps). Docs now match.
- **Transport respawn now cleanly resets stderr state.** When a
  subprocess died naturally (returncode set, no `_close_proc` called),
  `_ensure_alive` jumped straight to `_spawn` without cancelling the
  old stderr drainer or resetting `_stderr_tail`. Lines captured from
  the dead process would then bleed into the next connection's error
  reports. Now the dead-but-not-closed branch runs `_close_proc()`
  first.
- **`zsnoop-mcp` CLI no longer leaks a traceback for `FileNotFoundError`.**
  A broken install (agent script missing from the wheel) or an explicit
  `--agent-source /missing/path` would propagate `FileNotFoundError`
  out of `asyncio.run`, surfacing as a Python traceback. Now caught at
  the top level and printed as a clean one-line error with exit code 2.
- **Agent's `_iso_to_ts` now treats naive ISO timestamps as UTC.**
  `datetime.timestamp()` on a naive datetime interprets it as *local*
  time, so the same ISO string would map to different epoch seconds
  depending on the agent host's TZ — inconsistent with the server's
  `timeparse`, which always assumes UTC. The server happens to always
  send tz-aware strings today, so this is a defensive fix at the
  boundary rather than a user-visible bug fix.
- **Removed a flaky fixed-sleep from the respawn test.** The transport
  respawn test used `await asyncio.sleep(0.1)` to wait for the stderr
  drainer; on busy CI runners that's not always long enough. Replaced
  with a polling `_wait_for(predicate)` helper that returns as soon as
  the marker appears.

- **Transport recv-timeout caused chained failures.** The 60 s default
  `recv_timeout` was shorter than the agent's `ZFS_DIFF_TIMEOUT_SECONDS`
  (300 s), so a legitimate long-running `diff_snapshots` / `find_deleted`
  could time out at the transport layer while the agent kept working.
  Worse, the timeout path explicitly *didn't* tear down the subprocess
  ("agent is still alive") — so the agent's late response would land in
  the pipe and surface as an `id mismatch` on the next call (two errors
  back-to-back from the LLM's perspective). Fix: bump the default
  `recv_timeout` to 360 s (300 s + buffer), and on timeout close the
  subprocess defensively so any late response can't desync the wire.

### Added

- **`list_snapshots` time filtering and optional cap.** New optional
  parameters `after`, `before` (ISO 8601 or human phrases like
  `yesterday` / `last week`), and `max_results`. Filtering happens
  agent-side so the on-wire response stays small — the motivating case
  was "what snapshots were created yesterday?" on a busy host returning
  ~400 KB of JSON (thousands of entries) just to extract ~200. With
  `after="yesterday"` the same query stays a fraction of that size.
  Defaults are all `None` (no filter, no cap), so existing callers
  including `m_snapshot_cadence` behave unchanged. New limit
  `max_list_snapshots = 10 000` exposed via `agent_info.limits`.

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

[Unreleased]: https://github.com/hamsolodev/zsnoop-mcp/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/hamsolodev/zsnoop-mcp/releases/tag/v0.3.1
[0.3.0]: https://github.com/hamsolodev/zsnoop-mcp/releases/tag/v0.3.0
[0.2.0]: https://github.com/hamsolodev/zsnoop-mcp/releases/tag/v0.2.0
[0.1.2]: https://github.com/hamsolodev/zsnoop-mcp/releases/tag/v0.1.2
[0.1.1]: https://github.com/hamsolodev/zsnoop-mcp/releases/tag/v0.1.1
[0.1.0]: https://github.com/hamsolodev/zsnoop-mcp/releases/tag/v0.1.0
