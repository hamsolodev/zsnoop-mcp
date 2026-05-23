# 10. Adding a new tool

## What

A concrete, end-to-end walkthrough of adding a new read-only RPC method
and exposing it as an MCP tool. We'll use the actual change that landed
in phase 6b — adding `list_pools` — as the worked example.

## Why this matters

Every previous section explained a single layer. This section is the
contract between layers: what has to change in each one, in what order,
and which tests have to be updated. If you've read it, you can add a new
tool in 30 minutes.

## How — the recipe

The order matters: **agent → tests → server → tests → docs**.

### Step 1 — Decide if this is a new RPC or a parameter on an existing one

If the new behaviour is "the same thing, with one more knob", just extend
an existing method's params. If it answers a *different question*, it's a
new method.

`list_pools` answers a different question (pool-level summary vs.
dataset-level), so it's a new method.

### Step 2 — Implement the agent method

Add the function and register it in `METHODS` in
[`agent/zfs_snoop_agent.py`]({{ config.repo_url }}/src/branch/{{ repo_branch }}/agent/zfs_snoop_agent.py):

```python
def m_list_pools(_params: dict[str, Any]) -> dict[str, Any]:
    """List ZFS pools available to the agent's user."""
    out = run_zpool(["list", "-H", "-p", "-o", "name,size,allocated,free,health"])
    pools = []
    for line in out.splitlines():
        if not line:
            continue
        name, size, alloc, free, health = line.split("\t")
        pools.append({
            "name": name,
            "size": _int_or_none(size),
            "allocated": _int_or_none(alloc),
            "free": _int_or_none(free),
            "health": health,
        })
    return {"pools": pools}
```

Conventions to follow:

- **Underscore-prefixed unused params** (`_params`) — so ruff doesn't
  flag, and mypy is happy.
- **`-H -p`** for `zfs`/`zpool` — `-H` strips headers, `-p` returns raw
  numbers instead of "1.2T".
- **Return a plain dict.** The transport serialises this to JSON. Stick to
  primitives — strings, numbers, lists, dicts.
- **Numbers via `_int_or_none`** — handles `"-"` and empty fields uniformly.

If the operation needs a new external command (like `zpool` did),
factor it out alongside `run_zfs` — `_run_cli` already handles the
common scaffolding.

Add the method to the dispatch table:

```python
METHODS: Final[dict[str, Any]] = {
    "agent_info": m_agent_info,
    "list_pools": m_list_pools,       # <-- new
    ...
}
```

### Step 3 — Update the allowlist test

[`tests/test_dispatch.py`]({{ config.repo_url }}/src/branch/{{ repo_branch }}/tests/test_dispatch.py) has an explicit
list of all expected methods:

```python
def test_methods_table_is_what_we_expect() -> None:
    expected = {
        "agent_info",
        "list_pools",                  # <-- add here too
        ...
    }
    assert set(agent.METHODS) == expected
```

This is intentional — adding a method to the agent without explicitly
acknowledging it in this test is a code review red flag.

### Step 4 — Write a method test

In [`tests/test_methods.py`]({{ config.repo_url }}/src/branch/{{ repo_branch }}/tests/test_methods.py):

```python
def test_list_pools_parses_zpool_output(monkeypatch: pytest.MonkeyPatch) -> None:
    canned = (
        "rpool\t10995116277760\t9876543210\t1118573067550\tONLINE\n"
        "bpool\t2952790016\t1351733248\t1601056768\tONLINE\n"
    )
    def fake_zpool(args: list[str]) -> str:
        assert args == ["list", "-H", "-p", "-o", "name,size,allocated,free,health"]
        return canned

    monkeypatch.setattr(agent, "run_zpool", fake_zpool)
    result = agent.m_list_pools({})
    assert result == {
        "pools": [
            {"name": "rpool", "size": 10995116277760, ...},
            {"name": "bpool", "size": 2952790016, ...},
        ],
    }
```

Note: we assert *both* the argv passed to the CLI and the parsed output.
That catches both shell-injection-style regressions (argv shape) and
parsing regressions in one test.

### Step 5 — Register the MCP tool

In [`src/zsnoop_mcp/server.py`]({{ config.repo_url }}/src/branch/{{ repo_branch }}/src/zsnoop_mcp/server.py), inside
`create_server`:

```python
@mcp.tool()
async def list_pools(host: str) -> dict[str, Any]:
    """List ZFS pools visible to the agent on `host`.

    Each pool reports ``size``, ``allocated``, ``free`` (bytes), and
    ``health``. Useful when you don't already know what pools exist —
    prefer this over the static ``pools`` field in the host config.
    """
    return await _call(host, "list_pools")
```

Three things to get right:

- **Function name = tool name.** The LLM sees `list_pools`. Pick something
  that reads naturally in a prompt.
- **Docstring is LLM-facing.** Two sentences: what it does, when to use
  it. Including a "use this over X" hint is a great way to steer the
  model.
- **`_call(host, method, params=None)`** — always go through this helper,
  not `pool.call` directly. That's how host validation and error mapping
  stay centralised.

### Step 6 — Update the server's tool-registration test

In [`tests/test_server.py`]({{ config.repo_url }}/src/branch/{{ repo_branch }}/tests/test_server.py):

```python
async def test_server_registers_expected_tools(...) -> None:
    server = create_server(fake_pool, cfg)
    names = _registered_tool_names(server)
    assert names == {
        ...
        "list_pools",                  # <-- new
    }
```

Same principle as the dispatch test — adding a tool without updating this
assertion is a red flag.

### Step 7 — Documentation

Two doc files always touch:

- **README.md** — add a row to the tool table.
- **docs/USAGE.md** — add an example prompt that exercises the new tool.

If the new tool changes how something else works (like `list_pools` made
the `pools` config field redundant), also touch INSTALL.md.

### Step 8 — Local smoke test

```sh
uv run python -c "
import asyncio
from zsnoop_mcp.config import load_config
from zsnoop_mcp.server import find_agent_source
from zsnoop_mcp.transport import ConnectionPool
async def go():
    cfg = load_config('/home/youruser/.config/zsnoop-mcp/hosts.toml')
    async with ConnectionPool(cfg, find_agent_source()) as p:
        print(await p.call('r2d2', 'list_pools'))
asyncio.run(go())
"
```

If that prints real pool data from r2d2, the change is end-to-end
working.

### Step 9 — Run the full suite and commit

```sh
uv run ruff check
uv run ruff format
uv run mypy
uv run pytest -q
git add -A
git commit -m "..."
git push
```

Pre-commit hooks will re-run ruff and mypy; if either fails, address and
re-stage. New commit, not amend (per project commit policy).

## Common pitfalls

| Pitfall | What goes wrong | Fix |
| --- | --- | --- |
| Forgot to add method to `METHODS` dict | `unknown method` error from agent | Update dict, run `test_methods_table_is_what_we_expect` |
| Forgot the allowlist test | Test passes, security review misses the new method | The test in step 3 enforces this — don't skip it |
| Returned a non-dict from a method | `TransportError: result is not a JSON object` | Wrap in `{"…": value}` |
| Returned a tuple, custom class, or pathlib.Path | `TypeError: not JSON serializable` | Convert to dict/list/str |
| Tool docstring is one-line and generic | LLM doesn't pick the new tool | Add "Useful for X" hints |
| Missed `_int_or_none` on a numeric field | `int("-")` exception in the wild | Use the helper |

## What to read next

You're done with the tutorial. From here:

- For the canonical install / config guide: [INSTALL.md](../INSTALL.md)
- For LLM-facing prompt examples: [USAGE.md](../USAGE.md)
- For the security details: [SECURITY.md](../SECURITY.md)
- For releases: [PUBLISHING.md](../PUBLISHING.md)
