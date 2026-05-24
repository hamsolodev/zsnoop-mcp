"""JSON-RPC framing, dispatch, error codes, allowlist defence."""

from __future__ import annotations

import json

import pytest

import zfs_snoop_agent as agent


def _ok(req_id: int | str | None, method: str, **params: object) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})


# ---- happy path -------------------------------------------------------------


def test_known_method_returns_result() -> None:
    resp = agent.handle_request(_ok(1, "agent_info"))
    assert resp is not None
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    assert "result" in resp
    assert resp["result"]["agent_version"] == agent.AGENT_VERSION


# ---- framing errors ---------------------------------------------------------


def test_invalid_json_returns_parse_error() -> None:
    resp = agent.handle_request("{not json")
    assert resp is not None
    assert resp["error"]["code"] == agent.PARSE_ERROR
    assert resp["id"] is None


def test_non_object_request_returns_invalid_request() -> None:
    resp = agent.handle_request("[1, 2, 3]")
    assert resp is not None
    assert resp["error"]["code"] == agent.INVALID_REQUEST


def test_missing_method_returns_invalid_request() -> None:
    resp = agent.handle_request(json.dumps({"jsonrpc": "2.0", "id": 1}))
    assert resp is not None
    assert resp["error"]["code"] == agent.INVALID_REQUEST


def test_non_string_method_returns_invalid_request() -> None:
    resp = agent.handle_request(json.dumps({"jsonrpc": "2.0", "id": 1, "method": 42}))
    assert resp is not None
    assert resp["error"]["code"] == agent.INVALID_REQUEST


def test_unknown_method_returns_method_not_found() -> None:
    resp = agent.handle_request(_ok(1, "destroy_everything"))
    assert resp is not None
    assert resp["error"]["code"] == agent.METHOD_NOT_FOUND


def test_non_object_params_returns_invalid_params() -> None:
    raw = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "agent_info", "params": [1, 2]})
    resp = agent.handle_request(raw)
    assert resp is not None
    assert resp["error"]["code"] == agent.INVALID_PARAMS


def test_notification_returns_none() -> None:
    # No 'id' key = notification per JSON-RPC 2.0.
    raw = json.dumps({"jsonrpc": "2.0", "method": "agent_info", "params": {}})
    resp = agent.handle_request(raw)
    assert resp is None


def test_notification_for_error_path_still_returns_none() -> None:
    raw = json.dumps({"jsonrpc": "2.0", "method": "no_such_method", "params": {}})
    resp = agent.handle_request(raw)
    assert resp is None


# ---- handler exceptions -----------------------------------------------------


def test_handler_raising_agent_error_returns_that_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(_p: dict[str, object]) -> dict[str, object]:
        raise agent.PathError("nope", data={"why": "test"})

    monkeypatch.setitem(agent.METHODS, "test_method", boom)
    resp = agent.handle_request(_ok(7, "test_method"))
    assert resp is not None
    assert resp["error"]["code"] == agent.PATH_ERROR
    assert resp["error"]["data"] == {"why": "test"}


def test_handler_raising_unexpected_returns_internal_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(_p: dict[str, object]) -> dict[str, object]:
        raise RuntimeError("kaboom")

    monkeypatch.setitem(agent.METHODS, "test_method", boom)
    resp = agent.handle_request(_ok(7, "test_method"))
    assert resp is not None
    assert resp["error"]["code"] == agent.INTERNAL_ERROR


# ---- allowlist defence ------------------------------------------------------


def test_methods_table_contains_no_mutating_operations() -> None:
    """G1: the dispatch table must never expose write/mutate operations."""
    forbidden = {
        "destroy",
        "snapshot",
        "rollback",
        "send",
        "receive",
        "mount",
        "unmount",
        "umount",
        "promote",
        "clone",
        "set",
        "create",
        "rename",
        "release",
        "allow",
        "unallow",
        "load_key",
        "unload_key",
    }
    declared = set(agent.METHODS.keys())
    assert not declared & forbidden, (
        f"forbidden mutation methods leaked into METHODS: {declared & forbidden}"
    )


def test_methods_table_is_what_we_expect() -> None:
    """If you add or remove a method, update this set deliberately."""
    expected = {
        "agent_info",
        "list_pools",
        "list_datasets",
        "list_snapshots",
        "diff_snapshots",
        "list_dir",
        "size_breakdown",
        "read_file",
        "find_files",
        "content_grep",
        "file_history",
        "snapshots_containing",
        "first_appearance",
        "size_delta",
    }
    assert set(agent.METHODS) == expected
