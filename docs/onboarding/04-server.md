# 4. The MCP server

## What

[`src/zsnoop_mcp/server.py`]({{ config.repo_url }}{{ source_url_prefix }}/{{ repo_branch }}/src/zsnoop_mcp/server.py) — registers
every read-only operation as a FastMCP tool, validates host names,
translates human time phrases, and maps exceptions to MCP error responses.

## Why this layer exists

The agent speaks **JSON-RPC over stdio**. The MCP client speaks
**MCP over stdio** (a richer protocol with tool schemas, content blocks,
resource URIs, capabilities negotiation). Something has to translate. That
something is FastMCP, with our `create_server` registering each tool.

This layer also gets to be the **LLM-facing surface**. Tool names,
parameter names, docstrings — all of these become part of the prompt the
LLM works from when deciding what to call. Naming and docstring quality
here is a feature.

## How — guided tour

### `create_server(pool, config)` — the factory

The whole module is essentially one factory function. It takes:

- A `ConnectionPool` (the transport).
- A `Config` (so it knows what host names are valid).

And it returns a configured `FastMCP` instance. Why a factory and not a
module-level singleton: so tests can pass a `FakePool` and inspect
behaviour without spawning real subprocesses (see
[Testing patterns](07-testing.md)).

```python
def create_server(pool: ConnectionPool, config: Config) -> FastMCP:
    mcp = FastMCP("zsnoop-mcp", instructions=INSTRUCTIONS)

    def _validate_host(host: str) -> None:
        try:
            config.host(host)
        except ConfigError as e:
            raise ValueError(str(e)) from e

    async def _call(host, method, params=None) -> dict[str, Any]:
        _validate_host(host)
        try:
            return await pool.call(host, method, params)
        except AgentRpcError as e:
            raise ValueError(f"agent error ({e.code}): {e.message}") from e
        except TransportError as e:
            raise RuntimeError(f"transport error talking to {host!r}: {e}") from e

    @mcp.tool()
    async def list_datasets(host: str) -> dict[str, Any]:
        """List ZFS filesystems and volumes on `host` (no snapshots)."""
        return await _call(host, "list_datasets")

    # … one decorator per tool …
    return mcp
```

The `_call` closure is the **single chokepoint** through which every
agent-bound tool flows. Everything that needs to be true for any tool —
host validation, error mapping — lives there once.

### Error mapping (the contract with FastMCP)

| Source | Raised in `_call` as | What the client sees |
| --- | --- | --- |
| Unknown host | `ValueError("unknown host: …")` | tool-call error, message visible |
| `AgentRpcError` | `ValueError(f"agent error ({code}): {msg}")` | tool-call error, code preserved in text |
| `TransportError` | `RuntimeError(...)` | also a tool-call error; this distinguishes "your input was wrong" from "your network is broken" — useful when the LLM is deciding whether to retry |

### LLM-facing instructions

The `INSTRUCTIONS` string is the server's free-form preamble to the LLM:

```python
INSTRUCTIONS = (
    "Read-only exploration of ZFS snapshots on remote hosts over SSH. "
    "All operations are scoped to a host configured by the operator. "
    "Use `list_hosts` first to see what's reachable; pass `host` to every "
    "other tool. Time-range parameters accept ISO 8601 or human phrases "
    "like 'yesterday', 'last week', '3 days ago'."
)
```

Two LLM-steering tricks worth noting:

- **"Use `list_hosts` first"** — gives the model a known entry point so it
  doesn't guess names.
- **Mention the time-phrase parser** — without this, the model would
  probably reach for ISO 8601 mechanically, which is more typing.

### Tool docstrings carry weight

These are not just for humans. FastMCP injects them into the tool's
description, which the LLM reads when deciding what to call. Example:

```python
@mcp.tool()
async def snapshots_containing(
    host: str,
    dataset: str,
    path: str,
    after: str | None = None,
    before: str | None = None,
) -> dict[str, Any]:
    """Return the snapshots of `dataset` in which `path` currently exists.

    `after` and `before` accept ISO 8601 timestamps OR human phrases like
    ``yesterday``, ``last week``, ``3 days ago``. Useful for "find me a
    snapshot from before the change" queries.
    """
```

The trailing "Useful for …" sentence is the prompt-engineering hook — it
helps the model pick this tool over `list_snapshots` when the user's
phrasing matches.

### Time-phrase translation

For tools with `after` / `before`, we don't trust the agent to parse human
time phrases — the agent's wire schema is "ISO 8601 strings only". The
translation happens here:

```python
try:
    after_iso = maybe_to_iso(after)
    before_iso = maybe_to_iso(before)
except TimePhraseError as e:
    raise ValueError(f"could not parse time phrase: {e}") from e
return await _call(host, "snapshots_containing", {..., "after": after_iso, "before": before_iso})
```

`maybe_to_iso` is in [`timeparse.py`]({{ config.repo_url }}{{ source_url_prefix }}/{{ repo_branch }}/src/zsnoop_mcp/timeparse.py) —
see [Time parsing](06-timeparse.md) for what it accepts.

### `find_agent_source()` — where the agent script comes from

Two install scenarios, one resolver:

```python
def find_agent_source() -> str:
    # Wheel install: hatchling force-includes agent/ into the package.
    try:
        candidate = files("zsnoop_mcp") / "_agent_source" / "zfs_snoop_agent.py"
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    except (ModuleNotFoundError, FileNotFoundError):
        pass
    # Dev install: walk up from this file.
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate_path = parent / "agent" / "zfs_snoop_agent.py"
        if candidate_path.is_file():
            return candidate_path.read_text(encoding="utf-8")
    raise FileNotFoundError(...)
```

For wheel installs, [pyproject.toml]({{ config.repo_url }}{{ source_url_prefix }}/{{ repo_branch }}/pyproject.toml) has:

```toml
[tool.hatch.build.targets.wheel.force-include]
"agent/zfs_snoop_agent.py" = "zsnoop_mcp/_agent_source/zfs_snoop_agent.py"
```

…which copies the file into the package on build. For `uv sync` editable
installs the force-include doesn't apply, so we fall back to a relative
walk from `__file__`. That covers both `uv run` and `uv tool install`.

## What to read next

→ [Configuration](05-config.md) — the TOML schema and validation logic
that feeds `Config` into `create_server`.
