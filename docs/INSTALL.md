# Installation

> 🚧 This document is filled in across phases 1–5. The ZFS delegation and MCP
> client wiring sections below are authoritative; the deployment commands will
> be finalised in phase 5.

## Requirements

- **Local (where the MCP client runs):** Python 3.11+, `uv`, OpenSSH client.
- **Remote (each ZFS host you want to explore):** Python 3.11+, OpenSSH server,
  SSH key auth (preferably via agent forwarding), the user account you SSH in
  as must hold or be delegated `diff` permission on the pool(s) you want to
  query.

## Choose a privilege mode (per host)

For each host, decide whether the remote agent runs **as your SSH user**
(default) or **under `sudo` as root** (opt-in per host).

| | **User mode** (default) | **Sudo mode** |
|---|---|---|
| Privilege | normal user account | root |
| `zfs diff` works? | only with delegated `diff` permission (below) | yes |
| Read files in system datasets (`rpool/ROOT/…`)? | only files the user can read | any file |
| Setup on remote | grant `zfs allow … diff` once per pool | passwordless `sudo` (e.g. `pam_ssh_agent_auth` + agent forwarding) |
| Trust footprint | minimal | root on the host |

User mode is recommended unless you specifically need to read root-owned files
from snapshots. The threat model section of [SECURITY.md](SECURITY.md)
discusses the tradeoff.

### Passwordless sudo for sudo mode

If you choose sudo mode for a host, the SSH user must be able to run
`sudo python3 …` (or `sudo /path/to/zfs-snoop-agent`) without a password
prompt. The recommended mechanism is `pam_ssh_agent_auth` with SSH agent
forwarding — Claude verifies your forwarded key on the remote host and grants
`sudo` without prompting.

## ZFS delegated permissions (user mode only)

`zfs diff` requires either root or the `diff` delegated permission. Everything
else `zsnoop-mcp` does — listing snapshots, walking `.zfs/snapshot/` directories,
reading files — uses default-allowed `zfs` subcommands or normal POSIX file
access governed by the owner of the SSH session.

For each pool you want to be able to diff, on each host, as root:

```sh
sudo zfs allow -u <your-user> diff <pool>
# example for the canonical layout on a Debian-ZFS-on-root box:
sudo zfs allow -u mch diff rpool
sudo zfs allow -u mch diff bpool
```

Verify with:

```sh
zfs allow rpool
zfs allow bpool
```

This is **all** that's delegated. No `snapshot`, no `destroy`, no `mount`,
no `send`. The agent is built to refuse anything outside an explicit allowlist
regardless of permissions held, but minimising delegated rights is defence
in depth.

### What this *does not* grant

- Reading files inside snapshots is still governed by POSIX permissions. If
  you can't read `/etc/shadow` on the live filesystem, you can't read it from
  a snapshot either.
- The agent never modifies snapshots, pools, or filesystems. The delegated
  `diff` permission also confers no write capability.

## Local install

```sh
git clone git@c3po.example.com:youruser/zsnoop-mcp.git
cd zsnoop-mcp
uv sync
```

## Remote agent deployment

Two modes; pick per host (or mix).

### Bootstrap-on-connect (zero install on remote)

The local server streams `agent/zfs_snoop_agent.py` over the SSH connection on
first use. No file is left on the remote. Best for ergonomics during
development — change the agent locally and the next call uses the new version.

This is the default and requires no remote-side setup beyond Python 3.11+.

### Pre-installed (lower per-connection cost)

```sh
# from your local checkout
scp agent/zfs_snoop_agent.py <host>:~/bin/zfs-snoop-agent
ssh <host> chmod +x ~/bin/zfs-snoop-agent
```

Then set `agent_mode = "preinstalled"` for that host in your config. (Config
format finalised in phase 4.)

## MCP client configuration

> Finalised in phase 5.

The intended shape for Claude Code's `~/.claude/settings.json`:

```jsonc
{
  "mcpServers": {
    "zsnoop": {
      "command": "uv",
      "args": ["run", "--directory", "/home/youruser/Documents/worktrees/zsnoop-mcp", "zsnoop-mcp"],
      "env": {
        "ZSNOOP_CONFIG": "/home/youruser/.config/zsnoop-mcp/hosts.toml"
      }
    }
  }
}
```

## Host configuration

> Finalised in phase 4. Shape will be roughly:

```toml
# ~/.config/zsnoop-mcp/hosts.toml
[hosts.r2d2]
ssh_target  = "r2d2.example.com"
agent_mode  = "bootstrap"   # or "preinstalled"
pools       = ["rpool", "bpool"]

[hosts.c3po]
ssh_target  = "c3po.example.com"
agent_mode  = "preinstalled"
sudo        = true             # run agent as root via passwordless sudo
pools       = ["tank"]
```
