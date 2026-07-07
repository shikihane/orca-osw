from __future__ import annotations

import json
import os
import time

import pytest

from osw import state


def test_path_resolution(tmp_path):
    root = tmp_path

    assert state.state_dir(root) == root / ".orca" / "osw"
    assert state.state_file(root) == root / ".orca" / "osw" / "state.json"
    assert state.inbox_dir(root) == root / ".orca" / "osw" / "inbox"
    assert state.results_dir(root) == root / ".orca" / "osw" / "results"
    assert state.logs_dir(root) == root / ".orca" / "osw" / "logs"


def test_init_creates_dirs(tmp_path):
    root = tmp_path
    state.init_state_dir(root)

    assert state.state_dir(root).is_dir()
    assert state.inbox_dir(root).is_dir()
    assert state.results_dir(root).is_dir()
    assert state.logs_dir(root).is_dir()
    assert state.state_file(root).is_file()


def test_init_creates_valid_state(tmp_path):
    root = tmp_path
    state.init_state_dir(root)

    with state.state_file(root).open("r", encoding="utf-8") as f:
        data = json.load(f)

    assert data["version"] == 1
    assert data["project_root"] == str(root)
    assert data["serve"] is None
    assert data["agents"] == {}
    assert data["errors"] == []

    # tiers start empty: models are configured per-machine, never hardcoded
    assert data["models"] == {"strong": [], "medium": [], "weak": []}


def test_read_write_roundtrip(tmp_path):
    root = tmp_path
    payload = {
        "version": 1,
        "project_root": str(root),
        "serve": {"pid": 12345},
        "models": {"strong": [], "medium": [], "weak": []},
        "agents": {"agent_001": {"handle": "abc"}},
        "errors": ["oops"],
    }

    state.write_state(root, payload)
    result = state.read_state(root)

    assert result == payload


def test_read_state_missing_raises(tmp_path):
    root = tmp_path
    with pytest.raises(FileNotFoundError):
        state.read_state(root)


def test_next_agent_id_empty():
    assert state.next_agent_id({"agents": {}}) == "agent_001"


def test_next_agent_id_increments():
    s = {
        "agents": {
            "agent_001": {},
            "agent_002": {},
            "agent_004": {},
        }
    }
    assert state.next_agent_id(s) == "agent_005"


def test_request_result_roundtrip(tmp_path):
    root = tmp_path
    state.init_state_dir(root)

    request_id = state.write_request(root, "new", {"prompt": "hello"})

    request_path = state.inbox_dir(root) / f"{request_id}.json"
    assert request_path.is_file()
    with request_path.open("r", encoding="utf-8") as f:
        request_data = json.load(f)
    assert request_data["request_id"] == request_id
    assert request_data["command"] == "new"
    assert request_data["prompt"] == "hello"

    result_payload = {"agent_id": "agent_001", "handle": "term-1"}
    state.write_result(root, request_id, result_payload)

    result_path = state.results_dir(root) / f"{request_id}.json"
    assert result_path.is_file()

    result = state.read_result(root, request_id, timeout=2.0)
    assert result == result_payload
    # File should be removed after reading
    assert not result_path.is_file()


def test_read_result_timeout(tmp_path):
    root = tmp_path
    state.init_state_dir(root)

    start = time.monotonic()
    result = state.read_result(root, "nonexistent-request", timeout=0.5)
    elapsed = time.monotonic() - start

    assert result is None
    assert elapsed >= 0.4


def test_is_serve_running_no_state(tmp_path):
    root = tmp_path
    assert state.is_serve_running(root) is False


def test_is_serve_running_serve_null(tmp_path):
    root = tmp_path
    state.init_state_dir(root)
    assert state.is_serve_running(root) is False


def test_is_serve_running_current_process(tmp_path):
    root = tmp_path
    state.init_state_dir(root)
    s = state.read_state(root)
    s["serve"] = {"pid": os.getpid()}
    state.write_state(root, s)

    assert state.is_serve_running(root) is True


def test_is_serve_running_dead_pid(tmp_path):
    root = tmp_path
    state.init_state_dir(root)
    s = state.read_state(root)
    # A PID that is very unlikely to be in use.
    s["serve"] = {"pid": 999999}
    state.write_state(root, s)

    assert state.is_serve_running(root) is False
