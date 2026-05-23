# Usage examples

These are concrete prompts a user would actually give an LLM that has the
`zsnoop` MCP server connected. Each one is grouped by the dominant workflow
the tool is designed around.

## File recovery — "give me X as it was at time T"

> "What did `/home/youruser/.config/foo/bar.conf` on r2d2 look like yesterday?"

The LLM will:

1. Call `list_hosts` to confirm `r2d2` exists.
2. Call `snapshots_containing(host="r2d2", dataset="rpool/home/youruser",
   path=".config/foo/bar.conf", before="today", after="2 days ago")` to find a
   snapshot from the right window.
3. Call `read_file(host="r2d2", snapshot="rpool/home/youruser@…", path=
   ".config/foo/bar.conf")` and present the content.

> "Recover the version of `/etc/nginx/nginx.conf` from before the last reboot."

LLM uses `file_history` to enumerate every version with its mtime, picks the
one whose mtime predates the reboot, and reads it. (System dataset reads
require **sudo mode** for that host — see [SECURITY.md](SECURITY.md).)

## Config drift audit — "when did X change?"

> "What changed in `/etc` on r2d2 between 3 days ago and now?"

LLM enumerates snapshots in that window with `list_snapshots`, picks the
oldest and newest, then `diff_snapshots(snap_a=…, snap_b=…)`. Output is a list
of `+`/`-`/`M`/`R` paths.

> "When did `/home/youruser/.zshrc` last change?"

LLM walks `file_history(dataset="rpool/home/youruser", path=".zshrc")` and reports
adjacent versions whose mtimes (or sizes) differ.

> "Which snapshot first introduced `~/.config/zsnoop-mcp/hosts.toml`?"

`first_appearance(dataset="rpool/home/youruser", path=".config/zsnoop-mcp/hosts.toml")`
returns the earliest snapshot containing it, with creation timestamp.

## Forensics — "what was on the box when Y broke?"

> "Find every file containing the string `BAD_HEADER` in the last 24 hours of
> snapshots on r2d2's /home dataset."

LLM enumerates the recent snapshot list, then calls `content_grep` on each.
(Snapshots are read-only, so this is safe to do at speed.)

> "Show me every snapshot of `rpool/home/youruser/Documents/incident-2026-05.md`."

`snapshots_containing(dataset="rpool/home/youruser", path="Documents/incident-2026-05.md")`.

> "Which snapshots have the file at `var/log/syslog`, between when the issue
> started yesterday and now?"

`snapshots_containing(... after="yesterday", before="now")`.

## Storage / housekeeping

> "How much was written between the daily snapshot from last week and today's?"

`size_delta(snap_a=<last week's daily>, snap_b=<today's daily>)`. Useful for
tracking churn rates on a dataset.

> "Is `rpool/home/youruser/transmission` actually being snapshotted?"

`list_snapshots(dataset="rpool/home/youruser/transmission")` — if empty, nothing
is. If the most recent creation is older than expected, your snapshot job
isn't running.

## Cross-cutting tips for the LLM

- Time-range parameters (`after`, `before`) accept ISO 8601 *or* phrases like
  `yesterday`, `last week`, `3 days ago`, `2 hours ago`.
- For paths inside a snapshot, leading `/` is stripped — `"/etc/foo"` and
  `"etc/foo"` are equivalent. Anything containing `..` is rejected.
- Bulk traversal? Use `find_files` or `content_grep` with `max_results`
  rather than walking with many `list_dir` calls.
- Symlinks are never followed. If the snapshot contains a symlink, you'll
  see its target as data; if you want the content of what it points to, ask
  for the target path directly.
- Sudo mode is per-host and required to read files the SSH user doesn't own
  (e.g., snapshot copies of `/etc/shadow` or anything in a system dataset).
- All reads are bounded — `read_file` to 4 MiB max, `list_dir` to 10 000
  entries, search tools to 1 000 results. Truncated responses carry
  `truncated: true`.

## Worked end-to-end example

> User: "What changed in my dotfiles repo on r2d2 between yesterday and
> today?"

1. `list_snapshots(host="r2d2", dataset="rpool/home/youruser")` →
   pick snapshot `A` from 24h ago and `B` from latest, both of dataset
   `rpool/home/youruser`.
2. `diff_snapshots(host="r2d2", snap_a=A, snap_b=B)` →
   filter for paths starting with `Documents/worktrees/dotfiles/`.
3. For each modified file of interest, `read_file(host="r2d2",
   snapshot=B, path=...)` and `read_file(snapshot=A, path=...)` and
   summarise the line-level differences for the user.
