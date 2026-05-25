# 8. Security model

## What

The full security model is in
[docs/SECURITY.md](../SECURITY.md) — this section is a tour of the
*reasoning*, with pointers to the implementation and test for each
guarantee.

## Why we wrote it this way

The trust model:

- **The user running the MCP client** is trusted.
- **Their SSH keys** are trusted.
- **The MCP client itself (an LLM)** is *not* trusted — it may be prompted
  into requesting malicious operations.
- **Arbitrary input to any tool** is *not* trusted.
- **Snapshot contents** are *not* trusted — files might be symlinks,
  FIFOs, or crafted to mislead path resolution.

Out of scope:

- A malicious operator who already has shell access on the remote.
  (We're a subset of what they can already do.)
- SSH key compromise.

## How — the six guarantees

### G1 — No mutation operations are ever exposed

Enforced by an explicit `METHODS` dict in
[`agent/zfs_snoop_agent.py`](https://github.com/hamsolodev/zsnoop-mcp/blob/main/agent/zfs_snoop_agent.py); only
read-only methods present. Tested by
[`test_methods_table_contains_no_mutating_operations`](https://github.com/hamsolodev/zsnoop-mcp/blob/main/tests/test_dispatch.py)
which asserts that no name matching common destructive zfs verbs (destroy,
snapshot, rollback, send, mount, …) ever leaks in.

### G2 — No shell interpretation of user input

Every subprocess invocation uses `shell=False` with an explicit argv list
(`_run_cli` in the agent, `build_ssh_argv` in the transport). Inputs that
become argv elements are validated *before* the call:

- Dataset names: `^[A-Za-z0-9_][A-Za-z0-9_.:/-]*$`
- Snapshot names: same plus `@<snap-part>`

The transport uses `shlex.quote` per token when building the remote shell
command for SSH. Tests:
[`test_validate_dataset_rejects_invalid`](https://github.com/hamsolodev/zsnoop-mcp/blob/main/tests/test_validation.py),
[`test_validate_snapshot_rejects_invalid`](https://github.com/hamsolodev/zsnoop-mcp/blob/main/tests/test_validation.py).

### G3 — Path inputs cannot escape their snapshot root

Two layers of defence in
[`agent.resolve_under_snapshot`](https://github.com/hamsolodev/zsnoop-mcp/blob/main/agent/zfs_snoop_agent.py):

1. Reject `..` and absolute paths up front.
2. After joining, `Path.resolve()` follows symlinks; the result must stay
   inside `realpath(snapshot_root)`.

The function returns the *unresolved* path so callers (`read_file`,
`list_dir`) can `lstat()` the final component and refuse to follow a
symlink at all. Tests:
[`test_resolve_rejects_dotdot_traversal`](https://github.com/hamsolodev/zsnoop-mcp/blob/main/tests/test_path_safety.py),
[`test_resolve_rejects_symlink_that_escapes`](https://github.com/hamsolodev/zsnoop-mcp/blob/main/tests/test_path_safety.py),
[`test_read_file_refuses_to_follow_symlink`](https://github.com/hamsolodev/zsnoop-mcp/blob/main/tests/test_path_safety.py).

### G4 — All reads are bounded

| Operation | Limit |
| --- | --- |
| `read_file` | caller-provided `max_bytes`, server-capped at 4 MiB |
| `list_dir` | `max_entries`, default 1000, server-capped at 10 000 |
| `size_breakdown` | `max_entries`, default 100 000, server-capped at 1 000 000; plus 30 s wall time |
| `find_files` / `content_grep` | `max_results`, default 100, capped at 1000 |
| `file_diff` | `max_bytes` per side, default 1 MiB, capped at 4 MiB |
| `versions_of` | `max_bytes` per snapshot, default 1 MiB, capped at 4 MiB |
| `find_deleted` | `max_results`, default 1000, capped at 10 000 |
| `top_consumers` | heap `n`, default 20, capped at 1000; same walk cap + 30 s wall time as `size_breakdown` |
| `stale_snapshots` | `max_results`, default 1000, capped at 10 000 |
| `bisect_change` | `max_bytes` per predicate read, default 1 MiB, capped at 4 MiB; visits O(log N) snapshots |
| Per `zfs` subprocess | 30 s wall time |
| Transport recv | 60 s wall time |

Truncation sets `truncated: true` in the response rather than failing.
Tests:
[`test_list_dir_truncates_at_max_entries`](https://github.com/hamsolodev/zsnoop-mcp/blob/main/tests/test_methods.py),
[`test_find_files_truncates`](https://github.com/hamsolodev/zsnoop-mcp/blob/main/tests/test_methods.py),
[`test_size_breakdown_truncates_on_budget`](https://github.com/hamsolodev/zsnoop-mcp/blob/main/tests/test_methods.py).

### G5 — Defence in depth via ZFS delegation (user mode)

In the default user mode, the remote account holds *only* the `diff` ZFS
delegation. Even a compromised agent can't `destroy` / `snapshot` /
`mount` / `send` through `zfs(8)`. In sudo mode this defence does not
apply — the allowlist (G1) and no-shell guarantee (G2) are the remaining
lines, and we document the tradeoff explicitly.

### G6 — All structured logs go to stderr, never stdout

stdout is reserved for JSON-RPC frames. The agent's `main()` sets up
logging with `stream=sys.stderr` from the start. The transport drains the
subprocess's stderr to its own logger; corruption of the wire protocol via
errant prints is structurally impossible.

## Sudo mode tradeoff

Sudo mode exists to support legitimate reads from root-owned snapshot
files (e.g. `/etc/foo` from a snapshot of `rpool/ROOT/debian`). In sudo
mode:

- Agent runs as uid 0.
- POSIX read restrictions no longer protect any file.
- ZFS delegation is irrelevant; only the allowlist + no-shell guarantee
  stand between the wire input and `zfs` mutation.
- Trust boundary becomes "anything that can write to the JSON-RPC stream
  or into the agent source at bootstrap time has root on the remote".

Use sparingly. Default to user mode. Full discussion in
[SECURITY.md](../SECURITY.md#sudo-mode-tradeoff).

## A reviewer's checklist

When reviewing a change that touches a tool or method:

- [ ] Is any new RPC method added to the agent's `METHODS` dict
  read-only? (G1)
- [ ] Does any new dataset/snapshot/path input route through the
  validators before it touches `subprocess` or the filesystem? (G2/G3)
- [ ] Does any new read have a default bound and a hard cap? (G4)
- [ ] Are any new error paths returning structured info (JSON-RPC error
  with a code), not raw stack traces? (G6)
- [ ] If sudo mode is the only way the change makes sense, is the
  tradeoff documented?

## What to read next

→ [Build, package, release](09-build.md) — the project's `uv` /
`hatchling` setup, including the force-include trick that ships the agent
inside the wheel.
