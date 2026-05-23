# 5. Configuration

## What

[`src/zsnoop_mcp/config.py`]({{ config.repo_url }}/src/branch/{{ repo_branch }}/src/zsnoop_mcp/config.py) — the
dataclasses and TOML loader for `hosts.toml`.

## Why frozen dataclasses

Two design decisions that stand out:

- **`@dataclass(frozen=True, slots=True)`** — config is immutable once
  loaded. Mutating a `HostConfig` raises `AttributeError` at runtime, which
  prevents accidental drift if something held onto a stale reference.
- **Strict validation in `__post_init__`** — every field is type-checked
  and constraint-checked at construction time, not at use time. A bad
  config produces a single error at startup, not a mysterious crash six
  tool-calls in.

## How — guided tour

### The HostConfig shape

```python
@dataclass(frozen=True, slots=True)
class HostConfig:
    name: str
    ssh_target: str = ""
    transport: Transport = "ssh"        # "ssh" | "local"
    agent_mode: AgentMode = "bootstrap" # "bootstrap" | "preinstalled"
    agent_path: str | None = None
    sudo: bool = False
    remote_python: str = "python3"
    ssh_options: tuple[str, ...] = ()
    pools: tuple[str, ...] = ()
```

Notes:

- Sequence fields are `tuple`, not `list`. Frozen dataclasses can't hold
  mutable defaults *anyway*, and tuple is hashable so a `HostConfig` can
  go in a set if we ever need it.
- `Literal[...]` types for `transport` and `agent_mode` give the type
  checker something concrete; the runtime check in `__post_init__` keeps
  TOML inputs honest.

### Cross-field validation

The constraints we enforce post-init:

```python
def __post_init__(self) -> None:
    if self.transport not in _VALID_TRANSPORTS:
        raise ConfigError(f"host {self.name!r}: transport must be one of …")
    if self.transport == "ssh" and not self.ssh_target:
        raise ConfigError(f"host {self.name!r}: ssh_target is required when transport='ssh'")
    if self.agent_mode not in _VALID_MODES:
        raise ConfigError(...)
    if self.agent_mode == "preinstalled" and not self.agent_path:
        raise ConfigError(...)
```

Why this matters: `ssh_target` becomes conditionally required based on
`transport`. The whole module of validation logic exists because we want
"this stanza is malformed" to fail loudly with the *exact* reason, and
fail at config-load time rather than at first-use time.

### The parser — explicit allow-list of keys

```python
_KNOWN_HOST_KEYS = frozenset({
    "ssh_target", "transport", "agent_mode", "agent_path",
    "sudo", "remote_python", "ssh_options", "pools",
})

def _parse_host(name: str, stanza: dict[str, Any]) -> HostConfig:
    extra = stanza.keys() - _KNOWN_HOST_KEYS
    if extra:
        raise ConfigError(f"host {name!r}: unknown keys: {sorted(extra)}")
    return HostConfig(
        name=name,
        ssh_target=_optional_str(name, stanza, "ssh_target", ""),
        transport=_optional_str(name, stanza, "transport", "ssh"),
        ...
    )
```

Rejecting unknown keys turns *typos* into errors instead of silent no-ops.
A user who writes `agnt_mode = "bootstrap"` gets
`unknown keys: ['agnt_mode']` not a confused agent.

### `load_config(str | Path)`

Small but worth knowing: `load_config` accepts either string or `Path`.
This was a bug-fix during phase 5 — the type signature was `Path` only,
and external callers (a diagnostic script we wrote) passed a `str`, which
exploded with `AttributeError: 'str' object has no attribute 'read_text'`.
Fixed by normalising at the entry:

```python
def load_config(path: str | Path) -> Config:
    path = Path(path)
    ...
```

Test:
[`test_load_config_round_trip`]({{ config.repo_url }}/src/branch/{{ repo_branch }}/tests/test_config.py).

## What to read next

→ [Time parsing](06-timeparse.md) — the smallest module in the project,
worth understanding because every time-range parameter passes through it.
