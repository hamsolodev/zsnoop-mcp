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

`versions_of(dataset="rpool/home/youruser", path=".zshrc")` collapses every
snapshot's copy into one entry per distinct content (SHA-256). The
gap between consecutive versions' `first_seen` timestamps is the answer.
Cheaper than walking `file_history` and comparing sizes/mtimes when the
file is in a daily-snapshot dataset and rarely changes.

> "Show me the diff between the version of `/etc/foo.conf` from last week
> and today's."

`file_diff(snap_a=<last week's daily>, snap_b=<latest>, path="etc/foo.conf")`
returns a unified diff in one call (no need to `read_file` twice and
diff locally). Binary files report `encoding="binary"` with a still-correct
`identical` boolean.

> "Which snapshot first introduced `~/.config/zsnoop-mcp/hosts.toml`?"

`first_appearance(dataset="rpool/home/youruser", path=".config/zsnoop-mcp/hosts.toml")`
returns the earliest snapshot containing it, with creation timestamp.
Symmetric `last_appearance` answers "when did this file *disappear*?".

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

> "What got deleted in `rpool/home/youruser` in the last week?"

`find_deleted(dataset="rpool/home/youruser", after="last week")` resolves
the earliest snapshot in the window and the latest snapshot overall,
runs `zfs diff` between them, and returns just the `-` entries. Bounded
by `max_results`.

## Storage / housekeeping

> "How much was written between the daily snapshot from last week and today's?"

`size_delta(snap_a=<last week's daily>, snap_b=<today's daily>)`. Useful for
tracking churn rates on a dataset.

> "How big is `/home/youruser/Photos` in the latest snapshot, and what's inside it that's eating the space?"

`size_breakdown(host=…, snapshot=<latest-of-the-dataset>, path="Photos")`
returns the recursive total plus per-immediate-child bytes. Drill down by
calling it again on whichever child is biggest. Bounded by `max_entries`
(default 100,000) and a 30 s wall-clock budget — `truncated=true` on the
response (or `is_truncated=true` on a specific child) tells you which
subtree got clipped.

> "Now tell me the specific files and dirs hogging the space inside Photos."

`top_consumers(host=…, snapshot=…, path="Photos", n=20)` walks the
subtree and returns the 20 largest entries (files and directory subtree
totals), ranked. Use this after `size_breakdown` when you've drilled
down enough and want the actual filenames.

> "Which snapshots on `rpool/home/youruser` are older than six months — and which are biggest?"

`stale_snapshots(host=…, dataset="rpool/home/youruser", older_than="6 months ago")`
returns the matching snapshots sorted by unique-`used` bytes descending,
so the top of the list is the best place to start culling.

> "When did `/etc/foo.conf` first contain the string `BAD_HEADER`?"

`bisect_change(host=…, dataset="rpool/ROOT/debian", path="etc/foo.conf",
predicate={"kind": "contains", "needle": "BAD_HEADER"})` runs a binary
search across the snapshot timeline — O(log N) predicate evaluations
instead of N — and returns the snapshot pair that frames the
transition. Other predicate kinds: `exists`, `sha256_equals`, and
`size_at_least`.

> "Is `rpool/home/youruser/transmission` actually being snapshotted?"

`list_snapshots(dataset="rpool/home/youruser/transmission")` — if empty, nothing
is. If the most recent creation is older than expected, your snapshot job
isn't running.

## Discovery

> "What pools and datasets exist on r2d2?"

Use `list_pools(host="r2d2")` for pool-level summary (size, allocated,
free, health), then `list_datasets(host="r2d2")` for filesystems and
volumes. The static `pools` field in the host config is just a hint — call
`list_pools` for the live truth.

> "Is the rpool on r2d2 healthy? Last scrub status?"

`pool_status(host="r2d2", pool="rpool")` returns the parsed `zpool status`
output: pool state, `scan` summary (last scrub result + when), vdev tree
with per-device read/write/checksum error counts and depth (0=pool, 1=
top-level vdev, 2=leaf device). Call this when `list_pools` shows
HEALTH=DEGRADED to find out *which* device.

> "What's the compression / atime / recordsize on `rpool/home/youruser`?"

`dataset_properties(host="r2d2", dataset="rpool/home/youruser", properties=
["compression", "atime", "recordsize", "compressratio"])` returns each
property's value and source (`local`, `inherited from rpool`, `default`,
…). Omit `properties` to fetch the full `zfs get all` set.

> "Is `rpool/home/youruser` being snapshotted as expected?"

`snapshot_cadence(host="r2d2", dataset="rpool/home/youruser")` summarises
the snapshot inventory: counts bucketed by retention class (frequent /
hourly / daily / weekly / monthly / other), earliest/latest creation,
biggest gap (with the two snapshot names that frame it), and total
unique bytes. Faster than walking `list_snapshots` and doing arithmetic
on a long response.

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
