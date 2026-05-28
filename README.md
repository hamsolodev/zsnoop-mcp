# zsnoop-mcp

[![PyPI](https://img.shields.io/pypi/v/zsnoop-mcp.svg)](https://pypi.org/project/zsnoop-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/zsnoop-mcp.svg)](https://pypi.org/project/zsnoop-mcp/)
[![License: MIT](https://img.shields.io/pypi/l/zsnoop-mcp.svg)](https://github.com/hamsolodev/zsnoop-mcp/blob/main/LICENSE)
[![CI](https://github.com/hamsolodev/zsnoop-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/hamsolodev/zsnoop-mcp/actions/workflows/ci.yml)

**Ask your AI assistant things like:**

- ⏪ *"Recover my `.zshrc` from before I committed the rewrite three weeks ago."*
- 🧹 *"Which snapshots older than 6 months are wasting the most space?"*
- 🔎 *"When did the directory `/srv/backups` first appear on this host?"*
- ⌛ *"Find everything deleted under `/home/youruser` in the last week, and show me when each thing was last present."*
- 🏥 *"Are any of my pools throwing disk errors? When was the last scrub?"*

An MCP server for **read-only exploration of ZFS snapshots on remote hosts**.

Browse, diff, search, and read files from any snapshot on any of your ZFS
hosts through your AI assistant, over a single persistent SSH connection per
host. No mutation operations are ever exposed.

## Quickstart

```sh
# 1. Install
uv tool install zsnoop-mcp        # or: pipx install zsnoop-mcp

# 2. Configure one host (more in docs/INSTALL.md)
mkdir -p ~/.config/zsnoop-mcp
cat > ~/.config/zsnoop-mcp/hosts.toml <<'EOF'
[hosts.myhost]
ssh_target = "myhost.example.com"
agent_mode = "bootstrap"
sudo       = false
EOF

# 3. Register the MCP server with Claude Code
claude mcp add zsnoop --scope user -- zsnoop-mcp

# 4. Restart Claude Code, then ask your assistant any of the prompts above.
```

The agent is streamed over SSH on first connect — nothing needs to be
installed on the remote host beyond `python3` (3.11+) and the `zfs` CLI.
Read-only is enforced by an explicit allowlist on the agent side; the
LLM can't bypass it.

## About this codebase

This project was developed collaboratively with [Claude
Code](https://claude.com/claude-code) (Anthropic). The human author (Mark
Hellewell) defined the architecture, security model, and acceptance
criteria, and reviewed every change before it landed; Claude handled the
bulk of the drafting, test scaffolding, refactors, and documentation.
Read-only-by-construction was a hard requirement from day one, enforced
by an explicit method allowlist and the test suite — see
[SECURITY.md](https://github.com/hamsolodev/zsnoop-mcp/blob/main/docs/SECURITY.md). If you're reviewing or auditing the
code, treat that as context, not as a reason to skip the usual scrutiny.

## How it works

```text
┌─────────────────┐   MCP (stdio)    ┌────────────────────┐
│   MCP client    │ ───────────────► │  zsnoop-mcp server │
│ (Claude Code,…) │ ◄─────────────── │     (local)        │
└─────────────────┘                  └──────────┬─────────┘
                                                │
                       JSON-RPC over SSH stdio  │  one persistent
                       (one channel per host)   │  subprocess
                                                ▼
                                      ┌─────────────────────┐
                                      │  zfs-snoop-agent    │
                                      │  (remote, Python)   │
                                      └─────────┬───────────┘
                                                │
                                       zfs list / zfs diff,
                                       walk .zfs/snapshot/…
                                                ▼
                                            ZFS pool
```

The remote agent is a single-file, stdlib-only Python script. It can be
pre-installed at `~/bin/zfs-snoop-agent` on each host, or streamed over SSH
stdin on each connection — no permanent install required.

## Tools exposed to the LLM

Designed around three dominant workflows: **file recovery** ("get me /etc/foo
as it was yesterday"), **config drift audit** ("when did X change?"), and
**forensics** ("what was on the box when Y broke?").

| Tool                   | What it does                                             |
| ---------------------- | -------------------------------------------------------- |
| `list_hosts`           | Configured hosts                                         |
| `agent_info`           | Agent version, methods, limits                           |
| `list_pools`           | ZFS pools visible to the agent (live discovery)          |
| `pool_status`          | Parsed `zpool status`: vdev tree, scrub, errors          |
| `list_datasets`        | Filesystems and volumes                                  |
| `dataset_properties`   | `zfs get` (all or filtered) with values + sources        |
| `list_snapshots`       | Snapshots (optionally scoped to a dataset, recursive)    |
| `snapshot_cadence`     | Snapshot inventory summary: counts by class, biggest gap |
| `diff_snapshots`       | Path-level diff between two snapshots                    |
| `list_dir`             | Bounded directory listing within a snapshot              |
| `size_breakdown`       | Recursive bytes for a snapshot dir + per-child sizes     |
| `top_consumers`        | Top-N largest files/dirs under a snapshot subtree        |
| `read_file`            | Bounded read; UTF-8 or base64 for binary                 |
| `find_files`           | `fnmatch` name search inside a snapshot                  |
| `content_grep`         | Regex content search inside a snapshot                   |
| `file_history`         | Every snapshot's version of a given file in a dataset    |
| `versions_of`          | `file_history` deduped by hash (distinct versions only)  |
| `file_diff`            | Unified diff of one file across two snapshots            |
| `snapshots_containing` | Snapshots in which a path currently exists (time-ranged) |
| `first_appearance`     | Earliest snapshot containing a path                      |
| `last_appearance`      | Latest snapshot containing a path; reveals when deleted  |
| `find_deleted`         | Paths deleted between two snapshots in a time window     |
| `bisect_change`        | Binary-search for the snapshot where a predicate flips   |
| `stale_snapshots`      | Snapshots older than a time phrase, sorted by uniqueness |
| `size_delta`           | Bytes written between two snapshots of one dataset       |
| `checksum_file`        | Full-file SHA-256 (256 MiB cap) for integrity checks     |
| `fetch_file`           | Copy a snapshot file to a local path via SFTP            |
| `fetch_dir`            | Copy a snapshot directory tree to a local path via SFTP  |

Time-range parameters accept ISO 8601 *or* human phrases — `yesterday`,
`last week`, `3 days ago`, `2 hours ago`, etc. Parsing happens locally; the
agent only sees absolute ISO 8601 timestamps.

## Install

### From PyPI (recommended)

```sh
uv tool install zsnoop-mcp    # or: pipx install zsnoop-mcp / pip install zsnoop-mcp
```

Run it with `zsnoop-mcp`.

### From a clone (for hacking on the code)

```sh
git clone https://github.com/hamsolodev/zsnoop-mcp.git
cd zsnoop-mcp
uv sync
```

Run it with `uv run zsnoop-mcp` from the checkout.

See [docs/PUBLISHING.md](https://github.com/hamsolodev/zsnoop-mcp/blob/main/docs/PUBLISHING.md)
for the per-release flow (version bump → tag → CI publishes via OIDC).

## Configure

Create `~/.config/zsnoop-mcp/hosts.toml`:

```toml
[hosts.r2d2]
ssh_target = "r2d2.example.com"
agent_mode = "bootstrap"          # or "preinstalled"
sudo       = false                # set true to read root-owned snapshot files
pools      = ["rpool", "bpool"]   # used by the LLM for scoping hints

[hosts.c3po]
ssh_target = "c3po.example.com"
agent_mode = "bootstrap"
sudo       = false
pools      = ["rpool"]

[hosts.this-box]
transport  = "local"              # run the agent on this machine, no SSH
agent_mode = "bootstrap"
```

Per-host setup on the remote (one-time):

```sh
# user mode: grant diff for each pool you want to compare snapshots in
sudo zfs allow -u $USER diff rpool
```

See [docs/INSTALL.md](https://github.com/hamsolodev/zsnoop-mcp/blob/main/docs/INSTALL.md) for the full setup, including sudo
mode for reading root-owned snapshot files.

## Wire into Claude Code

After `uv tool install zsnoop-mcp`:

```sh
claude mcp add zsnoop --scope user -- zsnoop-mcp
```

That writes the entry directly to your Claude Code config; no JSON
editing needed. Restart your Claude Code session; the tools appear
under the `zsnoop` namespace.

If you're running from a worktree instead of an installed binary, point
the command at `uv run --directory <path>` instead:

```sh
claude mcp add zsnoop --scope user -- \
    uv run --directory ~/path/to/zsnoop-mcp zsnoop-mcp
```

Or, if you'd rather edit `~/.claude/settings.json` by hand:

```jsonc
{
  "mcpServers": {
    "zsnoop": { "command": "zsnoop-mcp" }
  }
}
```

## Use

See [docs/USAGE.md](https://github.com/hamsolodev/zsnoop-mcp/blob/main/docs/USAGE.md) for example prompts that exercise the
file-recovery, drift-audit, and forensics workflows.

## Documentation

- **New here?** Start with the [onboarding tutorial](https://github.com/hamsolodev/zsnoop-mcp/blob/main/docs/onboarding/index.md) —
  a 10-chapter, what/why/how walk through the codebase, ending with a
  worked example of adding a new tool end-to-end. Renders nicely as HTML
  via `uv run mkdocs serve` (see `--group docs`).
- [Installation](https://github.com/hamsolodev/zsnoop-mcp/blob/main/docs/INSTALL.md) — local setup, ZFS delegation, sudo mode
- [Usage examples](https://github.com/hamsolodev/zsnoop-mcp/blob/main/docs/USAGE.md) — concrete prompts the tools handle
- [Security model](https://github.com/hamsolodev/zsnoop-mcp/blob/main/docs/SECURITY.md) — threat model, guarantees, sudo tradeoff
- [Publishing](https://github.com/hamsolodev/zsnoop-mcp/blob/main/docs/PUBLISHING.md) — releasing to PyPI

## Development

```sh
uv sync                            # install runtime + dev deps into .venv
uv run pytest                      # tests
uv run ruff check                  # lint
uv run ruff format                 # format
uv run mypy                        # type-check
uv run pip-audit --skip-editable   # CVE scan of locked deps
uv run pre-commit install          # set up hooks
```

Pre-commit runs `pip-audit` automatically whenever `pyproject.toml` or
`uv.lock` change.

## License

MIT — see [LICENSE](https://github.com/hamsolodev/zsnoop-mcp/blob/main/LICENSE).
