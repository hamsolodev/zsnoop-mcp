# Security model

> 🚧 The threat model and guarantees here are aspirational for phase 1 and
> become contractual as phases 2–4 land. Each guarantee is paired with an
> implementation note describing where it's enforced.

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

The agent dispatches RPCs through an **explicit allowlist** of method names.
Any method not in the allowlist returns a JSON-RPC `Method not found` error.

The allowlist contains only read methods (`list_datasets`, `list_snapshots`,
`diff_snapshots`, `list_dir`, `read_file`, `find`, `file_history`, `agent_info`).

Adding a mutating method requires editing the agent source — there is no
configuration knob that turns mutation on.

### G2 — No shell interpretation of user input

Every external command is invoked via `subprocess.run([...], shell=False)`
with an argv list. Tool inputs that become argv elements (dataset names,
snapshot names) are validated against a strict character allowlist before
they are passed.

### G3 — Path inputs cannot escape their snapshot root

For any operation that takes a `(snapshot, path)`, the agent computes the
absolute, resolved path on the remote side and verifies it begins with the
snapshot's mountpoint (`.../<dataset>/.zfs/snapshot/<snapname>/`). Requests
that resolve outside the snapshot root are rejected.

Symbolic links inside snapshots are **not followed** during traversal; their
targets are reported as data.

### G4 — All reads are bounded

| Operation          | Limit                                                  |
| ------------------ | ------------------------------------------------------ |
| `read_file`        | `max_bytes` (caller-provided, server-capped at 4 MiB)  |
| `list_dir`         | `max_entries` (default 1000, server-capped at 10 000)  |
| `find`             | `max_results` (default 100, server-capped at 1000)     |
| Per-call wall time | 30 s, enforced via subprocess `timeout=`               |

Exceeding a limit truncates the response and sets a `truncated: true` flag
rather than failing.

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

Email `zsnoop-mcp.happiest328@passmail.net` with the subject `[zsnoop-mcp] security`.
