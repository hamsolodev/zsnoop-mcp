# 1. What this project is

## What

**zsnoop-mcp** is an [MCP](https://modelcontextprotocol.io) server that lets
an LLM safely explore ZFS snapshots on remote hosts (or the local machine)
without ever letting it modify anything.

Three concrete workflows the tool is shaped around:

- **File recovery** — "give me `~/.config/foo.toml` as it was yesterday"
- **Config drift audit** — "what changed in `/etc` between Monday and now?"
- **Forensics** — "what was on the box when the alerts started firing?"

## Why

Without a tool like this, the workflow for "what did this file look like
last week?" is:

```sh
ssh r2d2                                  # 1. log in
cd /home/youruser/.zfs/snapshot                   # 2. find a snapshot
ls                                           # 3. pick one
cat last-week-friday/.config/foo.toml        # 4. read it
```

…times **every snapshot you want to inspect**, **on every host**.

With MCP wiring, you ask "what did `foo.toml` look like last week?" in your
AI assistant, and the model uses the right tools (`snapshots_containing`,
`read_file`) to do the job in one round-trip. The reason that's *safe* is
that the tools are read-only by construction — the agent's dispatch table
has no `destroy`, no `snapshot`, no `rollback` (see
[Security model](08-security.md)).

## How — three layers

The whole project is three layers, talking to each other through small,
boring protocols.

```text
┌─────────────────┐
│   MCP client    │  Claude Code, Claude Desktop, Cursor, your own SDK app, …
│ (the LLM host)  │
└────────┬────────┘
         │ MCP (stdio: JSON-RPC + content blocks; framed via the mcp SDK)
         ▼
┌──────────────────┐
│ zsnoop-mcp       │  src/zsnoop_mcp/
│   server (local) │  FastMCP, config loading, tool registration, time-parsing
└────────┬─────────┘
         │ one persistent asyncio subprocess per host,
         │ line-delimited JSON-RPC 2.0 over its stdio
         ▼
┌─────────────────────┐
│ zfs-snoop-agent     │  agent/zfs_snoop_agent.py
│  (remote OR local;  │  Single-file, stdlib-only Python; method allowlist;
│   stdlib-only)      │  path-traversal & symlink-following defences
└────────┬────────────┘
         │ subprocess (`zfs`, `zpool`); POSIX walks under .zfs/snapshot/
         ▼
       ZFS pool
```

### Layer responsibilities

| Layer | Lives in | Responsibility |
| --- | --- | --- |
| MCP client | not in this repo | speaks MCP; usually the LLM's host process |
| MCP server | [src/zsnoop_mcp/](https://github.com/hamsolodev/zsnoop-mcp/blob/main/src/zsnoop_mcp) | translates MCP tool calls ⇄ agent JSON-RPC; manages connections; parses time phrases |
| Agent | [agent/zfs_snoop_agent.py](https://github.com/hamsolodev/zsnoop-mcp/blob/main/agent/zfs_snoop_agent.py) | does the actual `zfs` / file work; nothing else |

### Why this split

- **Boundary clarity** — the agent does only one thing (read ZFS state),
  knows nothing about MCP, and could be replaced with a Rust/Go binary
  tomorrow without touching the server.
- **Trust boundary at SSH** — the agent runs on the remote host and only
  ever speaks JSON-RPC over a single SSH stdio channel. No port to open, no
  daemon to manage, no TLS to configure.
- **Hermetic testability** — every layer is unit-tested in isolation
  (`FakeZfs`, `FakePool`), and there's one true end-to-end test that exercises
  the agent as a real subprocess. See [Testing patterns](07-testing.md).

## Where to next

- New here? → [The remote agent](02-agent.md) is the simplest layer; reading
  it first makes the rest obvious.
- Want to add a tool? → Skip to [Adding a new tool](10-extending.md).
