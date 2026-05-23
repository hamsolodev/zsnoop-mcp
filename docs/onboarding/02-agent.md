# 2. The remote agent

## What

[`agent/zfs_snoop_agent.py`]({{ config.repo_url }}/src/branch/{{ repo_branch }}/agent/zfs_snoop_agent.py) — a single
Python file, stdlib only, that reads JSON-RPC requests from stdin and
writes responses to stdout. It runs on whichever host you want to query.

## Why this shape

We need code on the remote host that can call `zfs` and walk
`.zfs/snapshot/` paths. The cheapest possible deploy story is "ship a
single file with no dependencies". So the agent is:

- **One file.** No package, no `requirements.txt`, no `setup.py`.
- **Stdlib only.** Anything else means `pip install` on every host.
- **JSON over stdio.** Works under SSH, under sudo, under local subprocess —
  anywhere you can hand the process a bidirectional byte-stream.

The fancy "bootstrap-on-connect" trick (covered in
[The transport](03-transport.md)) means the file doesn't even need to be
*on the host* — the local server base64-encodes it into the SSH command,
and Python decodes and `exec()`s it. Zero install.

## How — guided tour

### Method allowlist (G1 — read-only by construction)

The whole reason we trust this thing is the `METHODS` dict in
[agent/zfs_snoop_agent.py]({{ config.repo_url }}/src/branch/{{ repo_branch }}/agent/zfs_snoop_agent.py):

```python
METHODS: Final[dict[str, Any]] = {
    "agent_info": m_agent_info,
    "list_pools": m_list_pools,
    "list_datasets": m_list_datasets,
    "list_snapshots": m_list_snapshots,
    "diff_snapshots": m_diff_snapshots,
    "list_dir": m_list_dir,
    "read_file": m_read_file,
    "find_files": m_find_files,
    "content_grep": m_content_grep,
    "file_history": m_file_history,
    "snapshots_containing": m_snapshots_containing,
    "first_appearance": m_first_appearance,
    "size_delta": m_size_delta,
}
```

Every one of those is read-only. There is **no configuration knob** that
adds a method to this dict. To add `destroy_pool`, you'd have to edit the
source and re-deploy — and a test in
[tests/test_dispatch.py]({{ config.repo_url }}/src/branch/{{ repo_branch }}/tests/test_dispatch.py) asserts that no
common mutation name ever appears in the table.

### JSON-RPC over NDJSON

The wire format is JSON-RPC 2.0, one object per line, framed by `\n`. The
main loop:

```python
def main() -> int:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, ...)
    for line in sys.stdin:
        if not line.strip():
            continue
        response = handle_request(line)
        if response is None:
            continue
        sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        sys.stdout.flush()
```

Important details:

- **stdout is reserved for protocol frames.** Logging goes to stderr. The
  transport on the other end depends on this — any stray `print()` to
  stdout would corrupt the JSON-RPC stream.
- **Notifications return `None`.** A JSON-RPC request without an `id` is a
  notification — we process it but don't reply.
- **`SIGPIPE` → default handler.** If the peer disappears, we exit cleanly
  instead of getting an unhandled `BrokenPipeError`.

### Dispatch and error mapping

The dispatcher is small enough to read in one sitting:

```python
def _dispatch(req: dict[str, Any]) -> dict[str, Any]:
    req_id = req.get("id")
    method = req.get("method")
    params = req.get("params", {})
    if not isinstance(method, str):
        return make_error(req_id, INVALID_REQUEST, "missing or non-string 'method'")
    if not isinstance(params, dict):
        return make_error(req_id, INVALID_PARAMS, "'params' must be a JSON object")
    handler = METHODS.get(method)
    if handler is None:
        return make_error(req_id, METHOD_NOT_FOUND, f"unknown method: {method!r}")
    try:
        result = handler(params)
    except AgentError as e:
        return make_error(req_id, e.code, str(e), e.data)
    except Exception as e:
        log.exception("unhandled exception in %s", method)
        return make_error(req_id, INTERNAL_ERROR, f"internal error: {e}")
    return make_result(req_id, result)
```

`AgentError` is the base of a small exception hierarchy (`InvalidParams`,
`PathError`, `ZfsError`, `AgentTimeoutError`) — each carries a JSON-RPC
error code in `.code` so the dispatcher can map them mechanically. Anything
truly unexpected becomes `INTERNAL_ERROR` so a bug never escapes the
process boundary as a raw stack trace.

### Subprocess invocation

`run_zfs` and `run_zpool` share `_run_cli`:

```python
def _run_cli(binary: str, args: list[str]) -> str:
    cmd = [binary, *args]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=ZFS_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise AgentTimeoutError(...) from e
    except FileNotFoundError as e:
        raise ZfsError(f"{binary} binary not found on PATH") from e
    if result.returncode != 0:
        raise ZfsError(
            f"{binary} failed with exit {result.returncode}",
            data={"stderr": result.stderr.strip(), "argv": cmd},
        )
    return result.stdout
```

Two non-negotiables:

- **`shell=False`** — argv is always a list, never a joined string. Tool
  inputs that become argv (dataset names, snapshot names) are validated
  against strict regexes *before* they reach this point. See G2 in the
  [Security model](08-security.md).
- **Wall-clock timeout** — a malicious or runaway `zfs find /` can't hang
  the agent forever. 30-second cap per call.

### Path safety

This is the trickiest part of the agent. `resolve_under_snapshot`:

```python
def resolve_under_snapshot(snapshot: str, user_path: str) -> tuple[Path, Path]:
    rel = validate_user_path(user_path)            # rejects "..", absolute paths
    root = snapshot_root(snapshot)                  # /pool/.../.zfs/snapshot/<name>
    real_root = root.resolve()
    candidate = real_root / rel                     # NOT resolved
    resolved = candidate.resolve(strict=False)      # would follow symlinks
    if real_root != resolved and real_root not in resolved.parents:
        raise PathError(f"path escapes snapshot root: {user_path!r}")
    return real_root, candidate                     # caller lstats() to detect symlinks
```

Two defences in one function:

1. **`..` rejection up front** — `validate_user_path` refuses any component
   equal to `..` or any absolute path.
2. **Boundary check on the resolved path** — even if every symlink were
   followed, the realpath has to stay inside the snapshot root. A symlink
   inside the snapshot pointing at `/etc/passwd` is rejected.

Crucially, we return the **unresolved** `candidate` so the caller can
`Path.lstat()` it to detect a final-component symlink and refuse to follow
it. `m_read_file` does exactly this and refuses to read symlinks at all.

### A method handler from start to finish

Here's `m_read_file` annotated:

```python
def m_read_file(params: dict[str, Any]) -> dict[str, Any]:
    snapshot = _require_str(params, "snapshot")     # (1) input validation
    path = _require_str(params, "path")
    max_bytes = validate_positive_int(
        params.get("max_bytes"),
        name="max_bytes", default=DEFAULT_READ_BYTES, hard_max=MAX_READ_BYTES,
    )
    _, target = resolve_under_snapshot(snapshot, path)   # (2) path safety
    try:
        st = target.lstat()                              # (3) NOT following symlinks
    except OSError as e:
        raise PathError(f"could not stat: {e}") from e
    if stat_mod.S_ISLNK(st.st_mode):                     # (4) explicit refusal
        raise PathError(f"refusing to read symlink: {path!r}")
    if not stat_mod.S_ISREG(st.st_mode):
        raise PathError(f"not a regular file: {path!r}")
    with target.open("rb") as fh:
        data = fh.read(max_bytes)                        # (5) bounded read
    try:
        text = data.decode("utf-8")
        encoding, content = "utf-8", text                # (6) text if it decodes,
    except UnicodeDecodeError:
        encoding, content = "base64", base64.b64encode(data).decode("ascii")  # else base64
    return {"snapshot": snapshot, "path": path, "size": st.st_size, ... }
```

Every method follows the same five-step rhythm — **validate inputs → resolve
paths safely → stat / list / read → enforce bounds → return a plain dict**.

## What to read next

→ [The transport](03-transport.md) — how the agent gets a stdio peer in
the first place, including the bootstrap-on-connect trick.
