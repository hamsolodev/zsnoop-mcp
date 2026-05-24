# Security model

Each guarantee below is paired with a pointer to where it's enforced in code,
and with the test that asserts the behaviour.

> **AI-assisted authorship.** Most of this codebase was drafted by
> [Claude Code](https://claude.com/claude-code) under human review (see
> "About this codebase" in the project
> [README]({{ config.repo_url }}{{ source_url_prefix }}/{{ repo_branch }}/README.md)).
> The security-critical invariants below — the method allowlist, the
> no-shell guarantee, path confinement, bounded reads — were specified
> by the human author and are enforced by tests that fail loudly if any
> new code violates them. If you're doing an independent security audit,
> treat that as additional motivation, not as reassurance: read the
> code, not just the docstrings.

## Threat model

**Trusted:** the user running the local MCP client, the SSH keys they hold,
the remote user accounts they can already log into. SSH transport security.

**Untrusted:**

1. The MCP client (an LLM) — may be prompted into requesting malicious
   operations or path traversals.
2. Arbitrary input to any tool — paths, snapshot names, datasets, search
   patterns.
3. Snapshot contents — files inside a snapshot may be symlinks, FIFOs, or
   crafted to mislead path resolution.

**Out of scope:**

- Defending against a malicious operator who already has shell access on the
  remote host. This tool exposes a *subset* of what they can already do.
- Defending against compromise of the SSH key material.

## Guarantees

### G1 — No mutation operations are ever exposed

The agent dispatches RPCs through an **explicit `METHODS` allowlist** in
`agent/zfs_snoop_agent.py`. Any method not in the dict returns JSON-RPC
`Method not found` (-32601).

Allowlist (read-only): `agent_info`, `list_pools`, `pool_status`,
`list_datasets`, `dataset_properties`, `list_snapshots`,
`snapshot_cadence`, `diff_snapshots`, `list_dir`, `size_breakdown`,
`top_consumers`, `read_file`, `find_files`, `content_grep`,
`file_history`, `versions_of`, `file_diff`, `snapshots_containing`,
`first_appearance`, `last_appearance`, `find_deleted`, `bisect_change`,
`stale_snapshots`, `size_delta`.

Adding a mutating method requires editing the agent source — there is no
configuration knob that turns mutation on. The test
`test_methods_table_contains_no_mutating_operations` asserts that no entry
matching common destructive zfs subcommands ever leaks into the table.

### G2 — No shell interpretation of user input

Every external command is invoked via `subprocess.run([...], shell=False)`
with an explicit argv list (`agent.run_zfs`). Tool inputs that become argv
elements are validated *before* the call:

- Dataset names match `^[A-Za-z0-9_][A-Za-z0-9_.:/-]*$`.
- Snapshot names match the same plus `@<snap-part>`.
- Tested by `test_validate_dataset_rejects_invalid` /
  `test_validate_snapshot_rejects_invalid`.

The local transport also uses an argv list for `ssh`, with the remote shell
command produced via `shlex.quote()` per token.

### G3 — Path inputs cannot escape their snapshot root

For any operation that takes a `(snapshot, path)`, the agent
(`agent.resolve_under_snapshot`):

1. Rejects absolute paths and any `..` segment up front.
2. Resolves the joined path with `Path.resolve(strict=False)` — which follows
   symlinks — and verifies it stays inside `realpath(snapshot_root)`.
3. Returns the *unresolved* path so callers can `lstat()` the final component
   to detect a symlink **without following it**.

`read_file` and `list_dir` then refuse to follow a final-component symlink at
all; symlinks are reported with their target string as data. Tests:
`test_resolve_rejects_dotdot_traversal`,
`test_resolve_rejects_symlink_that_escapes`,
`test_read_file_refuses_to_follow_symlink`,
`test_list_dir_reports_symlink_without_following`.

### G4 — All reads are bounded

| Operation          | Limit                                                  |
| ------------------ | ------------------------------------------------------ |
| `read_file`        | `max_bytes` (caller-provided, server-capped at 4 MiB)  |
| `list_dir`         | `max_entries` (default 1000, server-capped at 10 000)  |
| `size_breakdown`   | `max_entries` (default 100 000, server-capped at 1 000 000); 30 s wall time |
| `find_files`       | `max_results` (default 100, server-capped at 1000)     |
| `content_grep`     | `max_results` (default 100, server-capped at 1000)     |
| `file_diff`        | `max_bytes` per side (default 1 MiB, server-capped at 4 MiB) |
| `versions_of`      | `max_bytes` per snapshot read (default 1 MiB, server-capped at 4 MiB) |
| `find_deleted`     | `max_results` (default 1000, server-capped at 10 000)  |
| `top_consumers`    | `n` heap size (default 20, capped at 1000); `max_entries` walk cap as `size_breakdown`; 30 s wall time |
| `stale_snapshots`  | `max_results` (default 1000, server-capped at 10 000)  |
| `bisect_change`    | `max_bytes` per predicate read (default 1 MiB, server-capped at 4 MiB); evaluates O(log N) snapshots |
| Per zfs subprocess | 30 s wall time for general commands; 300 s for `zfs diff`, enforced via `subprocess.run(timeout=)` |
| Transport recv     | 60 s wall time, enforced in `AgentConnection._recv`    |
| Transport line size | 16 MiB max NDJSON line, enforced via `create_subprocess_exec(..., limit=...)` |

Exceeding a size limit truncates the response and sets `truncated: true`
rather than failing. Tested by `test_list_dir_truncates_at_max_entries`,
`test_find_files_truncates`, and
`test_read_file_falls_back_to_base64_for_binary` (covers max_bytes).

### G5 — Defence in depth via ZFS delegation (user mode)

In the default **user mode**, the remote account is expected to hold *only*
the `diff` ZFS delegation (see [INSTALL](INSTALL.md)). Even if the agent
were compromised, it could not destroy, snapshot, mount, or send any dataset
through `zfs(8)`.

In **sudo mode** the agent runs as root and this defence does not apply. The
allowlist (G1) and the no-shell guarantee (G2) are the remaining lines of
defence; mutation operations are still not in the dispatch table. See "Sudo
mode tradeoff" below.

### G6 — All structured logs go to stderr, never stdout

stdout is reserved for JSON-RPC frames. Any log message, debug output, or
unexpected stderr from a child process is captured and forwarded as a
structured field in the JSON-RPC error response, not interleaved with the
wire protocol.

## Sudo mode tradeoff

Sudo mode is opt-in per host and exists to support the legitimate use case
of reading files in root-owned system datasets (e.g., `/etc/foo` from a
snapshot of `rpool/ROOT/debian`). In sudo mode:

- The agent process is uid 0 on the remote host.
- POSIX read restrictions no longer protect any file.
- ZFS delegation is irrelevant; the agent could in principle invoke any
  `zfs(8)` subcommand. The allowlist (G1) still blocks this in the dispatch
  table, but the only line of defence against a code bug or compromised
  agent source is the allowlist itself, not the kernel.
- The trust boundary effectively becomes: anything that can put a malicious
  payload into stdin (the JSON-RPC stream) or into the agent source at
  bootstrap time has root on the remote host.

Use sudo mode only on hosts where you already trust the SSH user with root
(via `sudo`), and only when you need to read root-owned snapshot files. Keep
user mode for everything else.

## Known limitations

- The local MCP server does not currently verify host keys beyond what
  OpenSSH itself does. Use a properly populated `~/.ssh/known_hosts`.
- The bootstrap-on-connect path sends the agent source over SSH on every
  fresh connection. This is the same trust boundary as `git clone over ssh`:
  if the remote is compromised, it can run whatever it likes regardless of
  what you send it. The agent source is not confidential.
- A malicious snapshot containing a path component longer than `PATH_MAX`
  may cause path resolution to fail; this is reported as an error and does
  not crash the agent.

## Reporting a vulnerability

**Preferred:** open a private vulnerability report via the GitHub Security
Advisory tab on the repository:
<https://github.com/hamsolodev/zsnoop-mcp/security/advisories/new>.
This keeps the report confidential and pre-fills the CVE workflow.

**Alternative:** email `zsnoop-mcp.happiest328@passmail.net` with the
subject `[zsnoop-mcp] security`. Use this if you don't have a GitHub
account, or for a quick "I'm not sure if this is a vulnerability" check.

Please don't open public issues for security reports — both channels above
keep the discussion private until a fix lands and a coordinated disclosure
window has passed.
