# 7. Testing patterns

## What

163 tests, ~81% project coverage, hermetic by default. The test suite uses
a small set of repeating patterns; once you've seen them they're easy to
extend.

## Why these patterns

The hierarchy of test reliability:

1. **Hermetic unit test** — no network, no subprocess, no real filesystem
   beyond `tmp_path`. Fast, deterministic, runs in CI without setup.
2. **Hermetic integration test** — a real subprocess (e.g. the actual
   agent script) but no SSH, no real ZFS. Catches regressions in
   interface contracts.
3. **Live integration test** — talks to a real `r2d2` over SSH. Not in
   the automated suite (would need a host); used during development for
   smoke-testing.

We want most tests in category (1) for speed, a handful in (2) for
end-to-end confidence in the wire protocol, and category (3) as a manual
ritual when shipping.

## How — the four reusable doubles

### `FakeZfs` — substitute for `run_zfs` / `run_zpool`

Used by every agent method test except path-safety. Lives in
[`tests/conftest.py`](https://github.com/hamsolodev/zsnoop-mcp/blob/main/tests/conftest.py):

```python
class FakeZfs:
    def __init__(self) -> None:
        self._responses: dict[tuple[str, ...], str] = {}
        self.calls: list[tuple[str, ...]] = []

    def add(self, args: list[str], stdout: str) -> None:
        self._responses[tuple(args)] = stdout

    def __call__(self, args: list[str]) -> str:
        self.calls.append(tuple(args))
        try:
            return self._responses[tuple(args)]
        except KeyError as e:
            raise agent.ZfsError(f"unexpected zfs call: {args!r}") from e
```

Two design choices worth copying elsewhere:

- **Strict by default.** An unregistered call raises rather than returning
  empty output. Missing fixture setup fails loudly instead of producing
  mysterious "no datasets found" assertions.
- **Records every call.** Tests can assert what argv the agent built, not
  just what it returned.

Typical usage:

```python
def test_list_snapshots_scoped_to_dataset(fake_zfs: FakeZfs) -> None:
    fake_zfs.add(
        ["list", "-H", "-p", "-t", "snapshot", ...],
        "rpool/home@a\t1716000000\t10\t1000\n",
    )
    result = agent.m_list_snapshots({"dataset": "rpool/home"})
    assert len(result["snapshots"]) == 1
```

### On-disk snapshot tree (`snapshot_tree` / `mock_mountpoint`)

Path-safety tests use a *real* directory tree to verify symlink and
traversal handling against the kernel, not just the parser. Layout:

```text
<tmp>/.zfs/snapshot/snap1/
    hello.txt
    big.bin            (binary, > 1 MiB)
    sub/nested.txt
    sub/link_to_hello -> ../hello.txt    (in-snapshot symlink)
    escape -> /etc/passwd                (escape attempt)
    empty_dir/
```

`mock_mountpoint` then wires `agent.get_dataset_mountpoint` (via fake-zfs)
to return that tmp_path, so the agent's real path code runs end-to-end on
real files.

### `FakePool` — substitute for `ConnectionPool`

Used by server tests. From
[`tests/test_server.py`](https://github.com/hamsolodev/zsnoop-mcp/blob/main/tests/test_server.py):

```python
class FakePool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.next_result: dict[str, Any] = {"ok": True}
        self.raise_: BaseException | None = None

    async def call(self, host, method, params=None) -> dict[str, Any]:
        self.calls.append((host, method, params))
        if self.raise_:
            raise self.raise_
        return self.next_result
```

Lets us assert that a tool forwards the right host/method/params to the
pool, without ever spawning a subprocess. The `raise_` field lets us test
error mapping (`AgentRpcError → ValueError`, `TransportError → RuntimeError`).

### Real agent as a local subprocess

The one integration test that *does* spawn a real process —
[`tests/test_transport_local.py`](https://github.com/hamsolodev/zsnoop-mcp/blob/main/tests/test_transport_local.py)
runs the real `agent/zfs_snoop_agent.py` under `[sys.executable, …]` and
talks to it via the real `AgentConnection`. No SSH, no ZFS — the agent
methods either reflect their inputs or hit our fake-zfs setup.

This catches:

- Wire-protocol bugs (NDJSON framing, JSON-RPC id matching).
- Subprocess lifecycle bugs (reconnect after death, stderr surfacing).
- Bootstrap-stub bugs in `_bootstrap_stub`.

…which pure mocks can't.

### Time injection — keeping clocks out of test results

Every `timeparse` test passes an explicit `now`:

```python
NOW = datetime(2026, 5, 13, 14, 30, 0, tzinfo=UTC)

def test_yesterday_is_previous_midnight() -> None:
    assert parse_phrase("yesterday", now=NOW) == datetime(2026, 5, 12, 0, 0, tzinfo=UTC)
```

The production `parse_phrase` defaults `now` to `datetime.now(UTC)`, but
the parameter is in the API specifically for this. Cheap, reliable
pattern; copy it any time you need to make time-dependent code testable.

## Running the suite

```sh
uv run pytest                 # full run with coverage
uv run pytest tests/test_methods.py -v
uv run pytest -k "snapshot"   # any test mentioning "snapshot"
```

Pre-commit hooks (ruff, ruff-format, mypy) run automatically on `git
commit`; `uv run pre-commit install` once after cloning.

## What to read next

→ [Security model](08-security.md) — what we promise, where each promise
is enforced, and which test makes the promise real.
