# Installation

## Requirements

- **Local (where the MCP client runs):** Python 3.11+, `uv` (or `pip`), OpenSSH client.
- **Remote (each ZFS host you want to explore):** Python 3.11+, OpenSSH server,
  SSH key authentication (preferably via agent forwarding), and the user account
  you SSH in as must either hold the delegated `diff` permission on the relevant
  pools (user mode) or have passwordless `sudo` (sudo mode).

## Install the server locally

### From source

```sh
git clone git@example.com:youruser/zsnoop-mcp.git
cd zsnoop-mcp
uv sync
```

Run it with `uv run zsnoop-mcp`.

### From PyPI (when published)

```sh
uv tool install zsnoop-mcp   # or: pip install zsnoop-mcp
```

Run it with `zsnoop-mcp`. See [PUBLISHING.md](PUBLISHING.md) for the release flow.

## Choose a privilege mode (per host)

For each host you configure, decide whether the remote agent runs **as your SSH
user** (default) or **under `sudo` as root** (opt-in per host).

| | **User mode** (default) | **Sudo mode** |
| --- | --- | --- |
| Privilege | normal user account | root |
| `zfs diff` works? | only with delegated `diff` permission (below) | yes |
| Read files in system datasets (`rpool/ROOT/…`)? | only files the user can read | any file |
| Setup on remote | grant `zfs allow … diff` once per pool | passwordless `sudo` (e.g. `pam_ssh_agent_auth` + agent forwarding) |
| Trust footprint | minimal | root on the host |

User mode is recommended unless you specifically need to read root-owned files
from snapshots. The threat model in [SECURITY.md](SECURITY.md) discusses the
tradeoff in detail.

### Passwordless sudo for sudo mode

In sudo mode, the SSH user must be able to run `sudo python3 …` (bootstrap mode)
or `sudo /path/to/zfs-snoop-agent` (preinstalled mode) without a password
prompt. The recommended mechanism is `pam_ssh_agent_auth` with SSH agent
forwarding — the remote `sudo` verifies your forwarded key and grants
elevation without an interactive prompt.

## ZFS delegated permissions (user mode only)

`zfs diff` requires either root or the `diff` delegated permission. Everything
else `zsnoop-mcp` does — listing snapshots, walking `.zfs/snapshot/` directories,
reading files — uses default-allowed `zfs` subcommands or normal POSIX file
access governed by the owner of the SSH session.

For each pool you want to be able to diff, on each host:

```sh
sudo zfs allow -u $USER diff <pool>
# example: a Debian-on-ZFS box with the canonical layout
sudo zfs allow -u $USER diff rpool
sudo zfs allow -u $USER diff bpool
```

Verify with:

```sh
zfs allow rpool
```

This is **all** that's delegated. No `snapshot`, no `destroy`, no `mount`,
no `send`. The agent refuses anything outside its explicit method allowlist
regardless of permissions held, but minimising delegated rights is defence
in depth.

### What this does *not* grant

- Reading files inside snapshots is still governed by POSIX permissions. If
  you can't read `/etc/shadow` on the live filesystem, you can't read it from
  a snapshot either.
- The agent never modifies snapshots, pools, or filesystems. The `diff`
  delegation confers no write capability.

## Remote agent deployment

Two modes; pick per host (or mix).

### Bootstrap-on-connect (zero install on remote)

The local server streams `agent/zfs_snoop_agent.py` (≈26 KB) over the SSH
connection on first use. No file is left on the remote. Best ergonomics during
development — change the agent locally and the next call uses the new version.

This is the default (`agent_mode = "bootstrap"`) and requires no remote-side
setup beyond Python 3.11+.

### Pre-installed (slightly lower per-session cost)

```sh
# from your local checkout
scp agent/zfs_snoop_agent.py <host>:~/bin/zfs-snoop-agent
ssh <host> chmod +x ~/bin/zfs-snoop-agent
```

Then set `agent_mode = "preinstalled"` and `agent_path = "~/bin/zfs-snoop-agent"`
for that host in your config. Saves ~30 KB of source transfer per session.

## Host configuration

The MCP server looks for `hosts.toml` in this order:

1. `$ZSNOOP_CONFIG` (if set)
2. `$XDG_CONFIG_HOME/zsnoop-mcp/hosts.toml`
3. `~/.config/zsnoop-mcp/hosts.toml`

A minimal example:

```toml
[hosts.r2d2]
ssh_target = "r2d2.example.com"
agent_mode = "bootstrap"
sudo       = false
pools      = ["rpool", "bpool"]

[hosts.c3po]
ssh_target    = "c3po.example.com"
agent_mode    = "preinstalled"
agent_path    = "/home/youruser/bin/zfs-snoop-agent"
sudo          = true
remote_python = "python3"
ssh_options   = ["-o", "ConnectTimeout=5"]
pools         = ["rpool"]
```

Per-host fields:

| Field           | Default                           | Description                                                                                       |
| --------------- | --------------------------------- | ------------------------------------------------------------------------------------------------- |
| `transport`     | `"ssh"`                           | `"ssh"` (remote) or `"local"` (no SSH, agent runs on this machine).                               |
| `ssh_target`    | *(required if `transport="ssh"`)* | What gets passed to `ssh`, e.g. `user@host`.                                                      |
| `agent_mode`    | `"bootstrap"`                     | `"bootstrap"` or `"preinstalled"`.                                                                |
| `agent_path`    | *(required for preinstalled)*     | Absolute path to the agent script.                                                                |
| `sudo`          | `false`                           | Run the agent under `sudo` (needs passwordless setup).                                            |
| `remote_python` | `"python3"`                       | Interpreter to use in bootstrap mode.                                                             |
| `ssh_options`   | `[]`                              | Extra args inserted between `ssh` defaults and target. Ignored when `transport="local"`.          |
| `pools`         | `[]`                              | Hint to the LLM about which pools exist (optional — use the `list_pools` tool for live discovery). |

`pools` is metadata only at this layer; the agent itself queries whichever
datasets it has permission to see.

### Local mode (no SSH)

To run the agent on the *same* machine as the MCP server — useful if the
machine itself has ZFS, or for testing — set `transport = "local"`:

```toml
[hosts.this-box]
transport  = "local"
agent_mode = "bootstrap"   # still applies: bootstrap runs python3 -c …;
                           # preinstalled runs the agent script directly
sudo       = false         # set true to read root-owned snapshot files
```

`ssh_target` is not required in local mode (any value is ignored). All other
fields (`agent_mode`, `agent_path`, `sudo`, `remote_python`) behave the same;
SSH-specific fields (`ssh_options`) are ignored.

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

(After `uv tool install zsnoop-mcp` you can just use `"command": "zsnoop-mcp"`
with no `args`.)

Restart your Claude Code session; the tools appear under the `zsnoop` namespace.

### Environment requirements

The MCP server spawns `ssh` and relies on **agent forwarding** via
`SSH_AUTH_SOCK`. Some MCP clients (notably `mcp.client.stdio` from the SDK,
used in scripts and tests) strip env vars by default — they only pass
`HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM`, `USER`. If your client does this,
`ssh` will fail immediately with `BatchMode=yes` and you'll see a transport
error citing the agent's stderr.

Claude Code itself passes the user's env through to spawned MCP servers, so
no special config is usually needed. If you hit "agent unreachable"
errors that mention publickey/permission failures, ensure your client
passes `SSH_AUTH_SOCK`:

```jsonc
"env": {
  "SSH_AUTH_SOCK": "/path/to/your/ssh-agent.socket"
}
```

The server logs a warning at startup if `SSH_AUTH_SOCK` is unset while
hosts are configured.

## Verify

A quick end-to-end check without spinning up an MCP client:

```sh
uv run python -c "
import asyncio
from zsnoop_mcp.config import load_config
from zsnoop_mcp.server import find_agent_source
from zsnoop_mcp.transport import ConnectionPool
async def go():
    cfg = load_config('/home/youruser/.config/zsnoop-mcp/hosts.toml')
    async with ConnectionPool(cfg, find_agent_source()) as p:
        print(await p.call('r2d2', 'agent_info'))
asyncio.run(go())
"
```
