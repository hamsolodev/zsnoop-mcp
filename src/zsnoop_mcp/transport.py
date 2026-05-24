"""Async transport: one persistent subprocess per host carrying JSON-RPC.

Per-host model:
- One :class:`AgentConnection` owns one ``asyncio.subprocess.Process``.
- Calls are serialized by an :class:`asyncio.Lock` (one in-flight RPC
  per host). Different hosts have independent connections and can run
  concurrently.
- On a send/recv failure the connection respawns transparently once,
  then raises :class:`TransportError` if the retry also fails.

The transport is intentionally agnostic to *how* the subprocess is
started: :func:`build_ssh_argv` constructs the production SSH command,
:func:`build_local_argv` is for tests that run the agent directly.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import shlex
from collections.abc import Mapping
from typing import Any

from zsnoop_mcp.config import Config, HostConfig

log = logging.getLogger(__name__)


# Default OpenSSH options layered before per-host overrides.
DEFAULT_SSH_OPTIONS: tuple[str, ...] = (
    "-T",  # no remote TTY
    "-o",
    "BatchMode=yes",  # never prompt for a password
    "-o",
    "ServerAliveInterval=30",  # keep the channel alive
    "-o",
    "ServerAliveCountMax=3",
)

# NDJSON responses are one line; keep this comfortably above default tool caps.
MAX_LINE_BYTES: int = 16 * 1024 * 1024


# ----------------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------------


class TransportError(Exception):
    """Local transport failure: subprocess died, malformed line, id mismatch."""


class AgentRpcError(Exception):
    """A structured JSON-RPC error returned by the remote agent."""

    def __init__(self, code: int, message: str, data: object | None = None) -> None:
        super().__init__(f"agent error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


# ----------------------------------------------------------------------------
# Command construction
# ----------------------------------------------------------------------------


def _bootstrap_stub(agent_source: str) -> str:
    """Return the ``python3 -c`` payload that decodes and runs *agent_source*."""
    encoded = base64.b64encode(agent_source.encode("utf-8")).decode("ascii")
    # compile() so tracebacks show '<agent>' instead of '<string>'.
    return (
        f"import base64\n"
        f"exec(compile(base64.b64decode('{encoded}').decode(), '<zfs-snoop-agent>', 'exec'))\n"
    )


def _remote_command(config: HostConfig, agent_source: str) -> list[str]:
    """Return the argv that ``ssh`` should execute on the remote shell."""
    parts: list[str] = []
    if config.sudo:
        parts.append("sudo")
    if config.agent_mode == "bootstrap":
        parts.extend([config.remote_python, "-c", _bootstrap_stub(agent_source)])
    else:
        if not config.agent_path:
            raise ValueError(f"host {config.name!r}: agent_path required in preinstalled mode")
        parts.append(config.agent_path)
    return parts


def build_ssh_argv(config: HostConfig, agent_source: str) -> list[str]:
    """Argv to spawn the remote agent over SSH for *config*."""
    argv: list[str] = ["ssh", *DEFAULT_SSH_OPTIONS, *config.ssh_options, "--", config.ssh_target]
    remote_parts = _remote_command(config, agent_source)
    argv.append(" ".join(shlex.quote(p) for p in remote_parts))
    return argv


def build_local_argv(config: HostConfig, agent_source: str) -> list[str]:
    """Argv to spawn the agent on the *local* machine (no SSH)."""
    return _remote_command(config, agent_source)


def build_argv(config: HostConfig, agent_source: str) -> list[str]:
    """Return the argv appropriate for *config*'s transport."""
    if config.transport == "local":
        return build_local_argv(config, agent_source)
    return build_ssh_argv(config, agent_source)


# ----------------------------------------------------------------------------
# Per-host connection
# ----------------------------------------------------------------------------


class AgentConnection:
    """Long-lived JSON-RPC channel to one remote agent."""

    # How many stderr lines from the agent to remember for inclusion in
    # transport-failure messages. Bounded so a chatty agent can't OOM us.
    _STDERR_TAIL_LIMIT: int = 50
    # Wait for any pending stderr bytes before giving up after a failure.
    _STDERR_FINAL_DRAIN_SECS: float = 0.5

    def __init__(
        self,
        name: str,
        argv: list[str],
        *,
        max_reconnects: int = 1,
        spawn_timeout: float = 10.0,
        recv_timeout: float = 60.0,
    ) -> None:
        self.name = name
        self._argv = list(argv)
        self._max_reconnects = max_reconnects
        self._spawn_timeout = spawn_timeout
        self._recv_timeout = recv_timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._next_id = 1
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail: list[str] = []

    async def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send one JSON-RPC request and return the ``result`` field.

        Every agent method returns a JSON object, so the result is typed as
        ``dict[str, Any]``. Schema details live in the agent's docstrings.
        """
        async with self._lock:
            attempts = self._max_reconnects + 1
            last_error: Exception | None = None
            for attempt in range(attempts):
                try:
                    return await self._call_once(method, params)
                except (BrokenPipeError, ConnectionResetError, EOFError) as e:
                    last_error = e
                    log.warning(
                        "host=%s call=%s attempt=%d transport failure: %r",
                        self.name,
                        method,
                        attempt + 1,
                        e,
                    )
                    stderr_blob = await self._capture_remaining_stderr()
                    await self._close_proc()
                    if attempt == attempts - 1:
                        msg = f"agent on {self.name!r} unreachable after {attempts} attempts: {e}"
                        if stderr_blob:
                            msg += f"\nagent stderr:\n{stderr_blob}"
                        raise TransportError(msg) from e
            # Loop should always either return or raise; this is unreachable.
            raise TransportError(  # pragma: no cover
                f"agent on {self.name!r} unreachable: {last_error}"
            )

    async def close(self) -> None:
        """Terminate the underlying subprocess (idempotent)."""
        async with self._lock:
            await self._close_proc()

    async def __aenter__(self) -> AgentConnection:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    # -- internals -----------------------------------------------------------

    async def _call_once(
        self,
        method: str,
        params: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        await self._ensure_alive()
        assert self._proc is not None  # noqa: S101 - post-spawn invariant for mypy
        req_id = self._next_id
        self._next_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": dict(params) if params else {},
        }
        await self._send(request)
        response = await self._recv()
        if response.get("jsonrpc") != "2.0":
            raise TransportError(f"missing/invalid jsonrpc field: {response!r}")
        if response.get("id") != req_id:
            raise TransportError(
                f"id mismatch on {self.name!r}: sent {req_id}, got {response.get('id')!r}",
            )
        if "error" in response:
            err = response["error"]
            raise AgentRpcError(err["code"], err["message"], err.get("data"))
        if "result" not in response:
            raise TransportError(f"response missing both 'result' and 'error': {response!r}")
        result = response["result"]
        if not isinstance(result, dict):
            raise TransportError(f"result is not a JSON object: {result!r}")
        return result

    async def _ensure_alive(self) -> None:
        if self._proc is None or self._proc.returncode is not None:
            await self._spawn()

    async def _spawn(self) -> None:
        log.info("host=%s spawning agent: %s", self.name, self._argv)
        try:
            self._proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *self._argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=MAX_LINE_BYTES,
                ),
                timeout=self._spawn_timeout,
            )
        except (TimeoutError, OSError) as e:
            raise TransportError(f"could not spawn agent on {self.name!r}: {e}") from e
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None  # noqa: S101
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", "replace").rstrip()
                self._stderr_tail.append(text)
                if len(self._stderr_tail) > self._STDERR_TAIL_LIMIT:
                    del self._stderr_tail[: -self._STDERR_TAIL_LIMIT]
                log.info("host=%s agent: %s", self.name, text)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("host=%s stderr drainer crashed", self.name)

    async def _capture_remaining_stderr(self) -> str:
        """Return whatever the drainer has captured plus a brief settle-time.

        Two concurrent readers on the same :class:`StreamReader` produce
        undefined behaviour, so we don't read the stream directly here — we
        just give the background drainer a chance to catch up on any pending
        lines before we report. If the agent exited before the drainer was
        scheduled, this short await is what lets its output land in the tail.
        """
        deadline = asyncio.get_running_loop().time() + self._STDERR_FINAL_DRAIN_SECS
        last_seen = -1
        while asyncio.get_running_loop().time() < deadline:
            if len(self._stderr_tail) == last_seen:
                # Nothing new in this slice; let the drainer try once more
                # then call it done.
                await asyncio.sleep(0.02)
                if len(self._stderr_tail) == last_seen:
                    break
            last_seen = len(self._stderr_tail)
            await asyncio.sleep(0.02)
        return "\n".join(self._stderr_tail)

    async def _send(self, request: Mapping[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None  # noqa: S101
        line = json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n"
        self._proc.stdin.write(line)
        await self._proc.stdin.drain()

    async def _recv(self) -> dict[str, Any]:
        assert self._proc is not None and self._proc.stdout is not None  # noqa: S101
        try:
            line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=self._recv_timeout)
        except TimeoutError as e:
            raise TransportError(
                f"agent on {self.name!r} did not respond within {self._recv_timeout}s",
            ) from e
        except ValueError as e:
            raise TransportError(
                f"agent on {self.name!r} emitted a line larger than {MAX_LINE_BYTES} bytes",
            ) from e
        if not line:
            raise EOFError(f"agent on {self.name!r} closed stdout")
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as e:
            raise TransportError(f"agent on {self.name!r} emitted non-JSON line: {line!r}") from e
        if not isinstance(parsed, dict):
            raise TransportError(f"agent on {self.name!r} emitted non-object: {parsed!r}")
        return parsed

    async def _close_proc(self) -> None:
        if self._proc is None:
            return
        proc = self._proc
        self._proc = None
        # Reset the stderr buffer; next spawn gets a fresh tail.
        self._stderr_tail = []
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:
            proc.kill()
            await proc.wait()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._stderr_task
            self._stderr_task = None


# ----------------------------------------------------------------------------
# Pool: many hosts, one connection each
# ----------------------------------------------------------------------------


class ConnectionPool:
    """Owns an :class:`AgentConnection` per configured host."""

    def __init__(self, config: Config, agent_source: str) -> None:
        self._config = config
        self._agent_source = agent_source
        self._connections: dict[str, AgentConnection] = {}
        self._pool_lock = asyncio.Lock()

    async def call(
        self,
        host: str,
        method: str,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Dispatch a JSON-RPC call to *host*'s agent."""
        conn = await self._get(host)
        return await conn.call(method, params)

    async def _get(self, host: str) -> AgentConnection:
        async with self._pool_lock:
            if host not in self._connections:
                host_config = self._config.host(host)
                argv = build_argv(host_config, self._agent_source)
                self._connections[host] = AgentConnection(host, argv)
            return self._connections[host]

    async def close(self) -> None:
        """Close every open connection (idempotent)."""
        async with self._pool_lock:
            await asyncio.gather(
                *(c.close() for c in self._connections.values()),
                return_exceptions=True,
            )
            self._connections.clear()

    async def __aenter__(self) -> ConnectionPool:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()
