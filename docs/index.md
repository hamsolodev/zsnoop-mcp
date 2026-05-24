# zsnoop-mcp

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

## What lives in these docs

<div class="grid cards" markdown>

-   :material-school: **[Onboarding tutorial](onboarding/index.md)**

    A guided walk through the codebase, from "what's MCP?" all the way to
    "here's how to add a new tool end-to-end". Targeted at developers new
    to the project — every section is structured **what / why / how** with
    real code excerpts.

-   :material-rocket-launch: **[Install & configure](INSTALL.md)**

    Get the server running locally, decide on user-mode vs sudo-mode per
    host, set up ZFS delegation, and wire into Claude Code.

-   :material-keyboard: **[Usage examples](USAGE.md)**

    Concrete LLM prompts grouped by workflow: file recovery, config drift
    audits, forensic dives.

-   :material-shield-check: **[Security model](SECURITY.md)**

    Threat model, the six guarantees, where each is enforced in code, and
    the test that asserts it.

-   :material-package-variant-closed: **[Publishing](PUBLISHING.md)**

    PyPI release flow — manual via `uv publish` or trusted publishing via CI.

</div>

## Architecture in 30 seconds

```text
MCP client (Claude Code, …)
        │
        │  MCP (stdio: JSON-RPC + content blocks)
        ▼
zsnoop-mcp server (local, async Python)
        │
        │  one persistent subprocess per host,
        │  carrying line-delimited JSON-RPC over its stdio
        ▼
zfs-snoop-agent (remote, stdlib-only Python)
        │
        │  zfs / zpool subprocess; POSIX walks under .zfs/snapshot/
        ▼
ZFS pool on the host
```

Three layers. The agent and the server share nothing but a JSON-RPC wire
protocol, which is what makes the transport pluggable (SSH today, local
process today, in principle anything that gives you a bidirectional
byte-stream).
