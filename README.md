# zsnoop-mcp

An MCP server for **read-only exploration of ZFS snapshots on remote hosts**.

Browse, diff, search, and read files from any snapshot on any of your ZFS
hosts through your AI assistant, over a single persistent SSH connection per
host. No mutation operations are ever exposed.

## How it works

```
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

## Status

🚧 In active development. Phase 1 (scaffold) complete; phases 2–5 to follow.

## Features (planned)

Designed around three dominant workflows: **file recovery** ("get me /etc/foo
as it was yesterday"), **config drift audit** ("when did X change?"), and
**forensics** ("what was on the box when Y broke?").

- `list_datasets(host)`, `list_snapshots(host, dataset?, time_range?)`
- `diff_snapshots(host, snap_a, snap_b)`
- `diff_path_across_snapshots(host, dataset, path, time_range)` — focused diff
- `list_dir(host, snapshot, path)` — bounded directory listing
- `read_file(host, snapshot, path, max_bytes)` — bounded read
- `find_files(host, snapshot, pattern, path?)` — name-pattern search
- `content_grep(host, snapshot, pattern, path?)` — content search
- `file_history(host, dataset, path)` — every snapshot version of a file
- `snapshots_containing(host, dataset, path, time_range?)` — which snapshots have it
- `first_appearance(host, dataset, path)` — earliest snapshot containing path
- `size_delta(host, dataset, snap_a, snap_b)` — via `written@snap`
- `agent_info(host)`

Relative time phrases (`yesterday`, `last week`, `3 days ago`) are parsed on
the local side; the agent only sees absolute ISO 8601 timestamps. Snapshot
creation times are extracted from `zfs-auto-snapshot` naming when present and
fall back to the `creation` property for manual snapshots.

### Privileged mode

By default the remote agent runs as your SSH user and needs only the `diff`
ZFS delegation to compare snapshots. Each host may opt into **sudo mode** in
config, in which case the agent runs as root and can read system-dataset
files (e.g. `/etc/foo` in `rpool/ROOT/debian` snapshots) the SSH user
cannot. See [SECURITY.md](docs/SECURITY.md) for the tradeoff.

## Documentation

- [Installation](docs/INSTALL.md) — local setup, ZFS delegation, MCP client wiring
- [Security model](docs/SECURITY.md) — threat model, guarantees, limitations

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
