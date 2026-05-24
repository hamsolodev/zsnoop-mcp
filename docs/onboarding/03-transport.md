# 3. The transport

## What

[`src/zsnoop_mcp/transport.py`]({{ config.repo_url }}{{ source_url_prefix }}/{{ repo_branch }}/src/zsnoop_mcp/transport.py) — the
async layer that owns one persistent subprocess per host, frames JSON-RPC
over its stdio, and reconnects on failure.

## Why this design

We want **low per-request latency** and **simple operational story**. The
combination we landed on:

| Choice | Reason |
| --- | --- |
| One persistent subprocess per host | Pay the SSH handshake once per session, not per call. Per-request overhead is < 1 ms after the connection is up. |
| Serial RPCs per connection (`asyncio.Lock`) | Avoids interleaved responses on stdin/stdout. Different hosts can still run concurrently because each has its own subprocess. |
| Transparent reconnect on `EOFError` / `BrokenPipe` | SSH connections do die. We retry once silently; the second failure raises. |
| Bounded `recv_timeout` (60 s) | A hung `zfs find /` shouldn't block the MCP server forever. |
| Bounded NDJSON line size (16 MiB) | Prevents unbounded memory use while still allowing large single-line JSON-RPC responses. |
| Drain stderr to a logger AND a tail buffer | Used to be just the logger; we now also keep the last 50 lines in memory so failure messages include the actual underlying error. |

## How — guided tour

### Command construction

The transport never spawns SSH directly. Three small builders:

```python
def build_ssh_argv(config: HostConfig, agent_source: str) -> list[str]:
    """ssh -T BatchMode=yes ... -- <target> <remote-shell-command>"""

def build_local_argv(config: HostConfig, agent_source: str) -> list[str]:
    """Just the remote command, no SSH wrapper."""

def build_argv(config: HostConfig, agent_source: str) -> list[str]:
    """Dispatch on config.transport."""
```

Both `ssh` and `local` share `_remote_command`:

```python
def _remote_command(config: HostConfig, agent_source: str) -> list[str]:
    parts: list[str] = []
    if config.sudo:
        parts.append("sudo")
    if config.agent_mode == "bootstrap":
        parts.extend([config.remote_python, "-c", _bootstrap_stub(agent_source)])
    else:
        parts.append(config.agent_path)
    return parts
```

For SSH, those parts are then joined with `shlex.quote` and appended as a
single argv to `ssh`, since `ssh host arg1 arg2 …` runs `sh -c "arg1 arg2 …"`
on the remote.

### Bootstrap-on-connect — the base64 trick

`_bootstrap_stub` is the cute bit:

```python
def _bootstrap_stub(agent_source: str) -> str:
    encoded = base64.b64encode(agent_source.encode("utf-8")).decode("ascii")
    return (
        f"import base64\n"
        f"exec(compile(base64.b64decode('{encoded}').decode(), '<zfs-snoop-agent>', 'exec'))\n"
    )
```

The remote shell ends up running:

```sh
python3 -c 'import base64
exec(compile(base64.b64decode("…<26 KB of base64>…").decode(), "<zfs-snoop-agent>", "exec"))
'
```

Why this and not `cat | python3`:

!!! warning "stdin is sacred"
    Once Python starts, its **stdin is the SSH stdin**, which is the
    JSON-RPC stream we need to send requests over. We can't use stdin to
    deliver the script — it has to come in via the command line.

The `compile(..., '<zfs-snoop-agent>', 'exec')` part gives tracebacks the
nice filename `<zfs-snoop-agent>` instead of `<string>`.

### `AgentConnection` — one process, one lock, one tail

The state machine, simplified:

```text
                ┌──────────┐
                │   idle   │
                └────┬─────┘
                     │ first call()
                     ▼
                ┌──────────┐    EOFError    ┌──────────┐
   send/recv ──►│   alive  │───────────────►│  closed  │
                └────┬─────┘                └────┬─────┘
                     │ close()                   │ next call() (one retry)
                     ▼                           ▼
                ┌──────────┐                ┌──────────┐
                │  closed  │                │  alive   │
                └──────────┘                └──────────┘
```

Implementation highlights from
[transport.py]({{ config.repo_url }}{{ source_url_prefix }}/{{ repo_branch }}/src/zsnoop_mcp/transport.py):

```python
class AgentConnection:
    def __init__(self, name, argv, *, max_reconnects=1, spawn_timeout=10.0, recv_timeout=60.0):
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()        # one in-flight RPC per host
        self._next_id = 1
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail: list[str] = []  # last N stderr lines, surfaced on failure
```

Each `call()` acquires the lock, then loops up to `max_reconnects + 1` times:

```python
async def call(self, method, params=None):
    async with self._lock:
        attempts = self._max_reconnects + 1
        for attempt in range(attempts):
            try:
                return await self._call_once(method, params)
            except (BrokenPipeError, ConnectionResetError, EOFError) as e:
                log.warning(...)
                stderr_blob = await self._capture_remaining_stderr()
                await self._close_proc()
                if attempt == attempts - 1:
                    msg = f"agent on {self.name!r} unreachable after {attempts} attempts: {e}"
                    if stderr_blob:
                        msg += f"\nagent stderr:\n{stderr_blob}"
                    raise TransportError(msg) from e
```

### The stderr surfacing fix

A real bug we hit (and have a test for): the agent dies on a stripped env
(e.g. no `SSH_AUTH_SOCK`); SSH writes `Permission denied (publickey)` to
stderr; we previously swallowed that and reported a useless "agent
unreachable". The fix lives in
[`_capture_remaining_stderr`]({{ config.repo_url }}{{ source_url_prefix }}/{{ repo_branch }}/src/zsnoop_mcp/transport.py):

```python
async def _capture_remaining_stderr(self) -> str:
    # Poll the tail briefly so the background drainer can flush pending
    # lines. We don't read the stream directly — two concurrent readers
    # on a StreamReader produce undefined behaviour.
    deadline = ... + self._STDERR_FINAL_DRAIN_SECS
    last_seen = -1
    while loop.time() < deadline:
        if len(self._stderr_tail) == last_seen:
            await asyncio.sleep(0.02)
            if len(self._stderr_tail) == last_seen:
                break
        last_seen = len(self._stderr_tail)
        await asyncio.sleep(0.02)
    return "\n".join(self._stderr_tail)
```

The drainer task itself appends to a bounded tail (max 50 lines) AND logs.
On failure we wait briefly for the drainer to flush, then return whatever
landed. Test:
[`test_transport_local_stderr.py`]({{ config.repo_url }}{{ source_url_prefix }}/{{ repo_branch }}/tests/test_transport_local_stderr.py).

### `ConnectionPool` — many hosts, one connection each

Thin wrapper around `AgentConnection`:

```python
class ConnectionPool:
    def __init__(self, config: Config, agent_source: str): ...

    async def call(self, host: str, method: str, params=None) -> dict[str, Any]:
        conn = await self._get(host)
        return await conn.call(method, params)

    async def _get(self, host: str) -> AgentConnection:
        async with self._pool_lock:
            if host not in self._connections:
                cfg = self._config.host(host)
                argv = build_argv(cfg, self._agent_source)  # SSH or local
                self._connections[host] = AgentConnection(host, argv)
            return self._connections[host]
```

Two locks: the pool lock guards the connection-map, each per-host
`AgentConnection` has its own lock guarding its subprocess. Different hosts
make calls concurrently; same host serialises.

## What to read next

→ [The MCP server](04-server.md) — how the layer above turns FastMCP tool
calls into `pool.call(host, method, params)`.
