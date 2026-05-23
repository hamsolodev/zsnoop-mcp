# zsnoop-mcp

An MCP server for **read-only exploration of ZFS snapshots on remote hosts**.

Browse, diff, search, and read files from any snapshot on any of your ZFS
hosts through your AI assistant, over a single persistent SSH connection per
host. No mutation operations are ever exposed.

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
| `list_datasets`        | Filesystems and volumes                                  |
| `list_snapshots`       | Snapshots (optionally scoped to a dataset, recursive)    |
| `diff_snapshots`       | Path-level diff between two snapshots                    |
| `list_dir`             | Bounded directory listing within a snapshot              |
| `read_file`            | Bounded read; UTF-8 or base64 for binary                 |
| `find_files`           | `fnmatch` name search inside a snapshot                  |
| `content_grep`         | Regex content search inside a snapshot                   |
| `file_history`         | Every snapshot's version of a given file in a dataset    |
| `snapshots_containing` | Snapshots in which a path currently exists (time-ranged) |
| `first_appearance`     | Earliest snapshot containing a path                      |
| `size_delta`           | Bytes written between two snapshots of one dataset       |

Time-range parameters accept ISO 8601 *or* human phrases — `yesterday`,
`last week`, `3 days ago`, `2 hours ago`, etc. Parsing happens locally; the
agent only sees absolute ISO 8601 timestamps.

## Install

### From a clone (dev / current path)

```sh
git clone git@c3po.example.com:youruser/zsnoop-mcp.git
cd zsnoop-mcp
uv sync
```

### From PyPI (when published)

```sh
pip install zsnoop-mcp        # or: uv tool install zsnoop-mcp
```

See [docs/PUBLISHING.md](docs/PUBLISHING.md) for the publish flow.

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

See [docs/INSTALL.md](docs/INSTALL.md) for the full setup, including sudo
mode for reading root-owned snapshot files.

## Wire into Claude Code

Add to `~/.claude/settings.json`:

```jsonc
{
  "mcpServers": {
    "zsnoop": {
      "command": "uv",
      "args": ["run", "--directory", "/home/youruser/Documents/worktrees/zsnoop-mcp", "zsnoop-mcp"]
    }
  }
}
```

Or, after PyPI install with `uv tool install zsnoop-mcp`:

```jsonc
{
  "mcpServers": {
    "zsnoop": {
      "command": "zsnoop-mcp"
    }
  }
}
```

Restart your Claude Code session; the tools appear under the `zsnoop` namespace.

## Use

See [docs/USAGE.md](docs/USAGE.md) for example prompts that exercise the
file-recovery, drift-audit, and forensics workflows.

## Documentation

- **New here?** Start with the [onboarding tutorial](docs/onboarding/index.md) —
  a 10-chapter, what/why/how walk through the codebase, ending with a
  worked example of adding a new tool end-to-end. Renders nicely as HTML
  via `uv run mkdocs serve` (see `--group docs`).
- [Installation](docs/INSTALL.md) — local setup, ZFS delegation, sudo mode
- [Usage examples](docs/USAGE.md) — concrete prompts the tools handle
- [Security model](docs/SECURITY.md) — threat model, guarantees, sudo tradeoff
- [Publishing](docs/PUBLISHING.md) — releasing to PyPI

## Development

```sh
uv sync                    # install runtime + dev deps into .venv
uv run pytest              # tests
uv run ruff check          # lint
uv run ruff format         # format
uv run mypy                # type-check
uv run pre-commit install  # set up hooks
```

## License

MIT — see [LICENSE](LICENSE).
