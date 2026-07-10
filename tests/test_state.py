from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from osw import state


def test_path_resolution(tmp_path):
    root = tmp_path

    assert state.state_dir(root) == root / ".orca" / "osw"
    assert state.state_file(root) == root / ".orca" / "osw" / "state.json"
    assert state.agents_dir(root) == root / ".orca" / "osw" / "agents"
    assert state.handoffs_dir(root) == root / ".orca" / "osw" / "handoffs"
    assert state.logs_dir(root) == root / ".orca" / "osw" / "logs"
    assert state.reports_dir(root) == root / ".orca" / "osw" / "reports"


def test_init_state_dir_creates_project_state(tmp_path):
    root = tmp_path
    state.init_state_dir(root)

    assert state.state_dir(root).is_dir()
    assert state.agents_dir(root).is_dir()
    assert state.handoffs_dir(root).is_dir()
    assert state.logs_dir(root).is_dir()
    assert state.reports_dir(root).is_dir()
    assert state.state_file(root).is_file()

    with state.state_file(root).open("r", encoding="utf-8") as f:
        data = json.load(f)

    assert data == {"version": 3, "project_root": str(root)}


def test_read_write_state_roundtrip(tmp_path):
    payload = {"version": 3, "project_root": str(tmp_path)}

    state.write_state(tmp_path, payload)

    assert state.read_state(tmp_path) == payload


@pytest.mark.skipif(os.name != "nt", reason="requires Windows sharing semantics")
def test_atomic_write_retries_until_transient_reader_releases_file(tmp_path):
    path = tmp_path / "agent.json"
    state._atomic_write_json(path, {"value": 1})

    reader = path.open("r", encoding="utf-8")
    release = threading.Timer(0.05, reader.close)
    release.start()
    try:
        state._atomic_write_json(path, {"value": 2})
    finally:
        reader.close()
        release.join()

    assert json.loads(path.read_text(encoding="utf-8")) == {"value": 2}


@pytest.mark.skipif(os.name != "nt", reason="requires Windows sharing semantics")
def test_atomic_write_reports_exhausted_replace_as_state_error(
    tmp_path, monkeypatch
):
    path = tmp_path / "agent.json"
    state._atomic_write_json(path, {"value": 1})
    monkeypatch.setattr(state, "REPLACE_RETRY_TIMEOUT_SECS", 0.02)

    with path.open("r", encoding="utf-8"):
        with pytest.raises(state.StateWriteError) as caught:
            state._atomic_write_json(path, {"value": 2})

    assert str(path) in str(caught.value)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows sharing semantics")
def test_atomic_write_cleans_temporary_file_after_replace_failure(
    tmp_path, monkeypatch
):
    path = tmp_path / "agent.json"
    state._atomic_write_json(path, {"value": 1})
    monkeypatch.setattr(state, "REPLACE_RETRY_TIMEOUT_SECS", 0.02)

    with path.open("r", encoding="utf-8"):
        with pytest.raises(state.StateWriteError):
            state._atomic_write_json(path, {"value": 2})

    assert list(tmp_path.glob("*.tmp")) == []


def test_concurrent_atomic_writers_do_not_share_temporary_file(
    tmp_path, monkeypatch
):
    path = tmp_path / "agent.json"
    state._atomic_write_json(path, {"writer": 0})
    replace_ready = threading.Barrier(2)
    real_replace = state._replace_with_retry
    sources: list[Path] = []
    errors: list[Exception] = []

    def synchronized_replace(source: Path, target: Path) -> None:
        sources.append(source)
        replace_ready.wait(timeout=1)
        real_replace(source, target)

    monkeypatch.setattr(state, "_replace_with_retry", synchronized_replace)

    def write(payload: dict) -> None:
        try:
            state._atomic_write_json(path, payload)
        except Exception as exc:
            errors.append(exc)

    payloads = ({"writer": 1}, {"writer": 2})
    writers = [
        threading.Thread(target=write, args=(payload,)) for payload in payloads
    ]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join()

    assert len(set(sources)) == 2
    assert errors == []
    assert json.loads(path.read_text(encoding="utf-8")) in payloads


def test_read_state_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        state.read_state(tmp_path)


def test_alloc_agent_id_claims_files_atomically(tmp_path):
    state.init_state_dir(tmp_path)

    assert state.alloc_agent_id(tmp_path) == "agent_001"
    assert state.agent_file(tmp_path, "agent_001").read_text(encoding="utf-8") == "{}"
    assert state.alloc_agent_id(tmp_path) == "agent_002"


def test_alloc_agent_id_uses_sanitized_prefix(tmp_path):
    state.init_state_dir(tmp_path)

    assert state.alloc_agent_id(tmp_path, prefix="Research") == "research_001"
    assert state.alloc_agent_id(tmp_path, prefix="code-review") == "code_review_001"
    assert state.alloc_agent_id(tmp_path, prefix="code review") == "code_review_002"
    assert state.alloc_agent_id(tmp_path, prefix="!!!") == "agent_001"


def test_agent_record_roundtrip_list_and_delete(tmp_path):
    state.init_state_dir(tmp_path)

    agent = {
        "agent_id": "research_001",
        "terminal": "term-a",
        "state": "assigned",
    }
    state.write_agent(tmp_path, agent)

    assert state.read_agent(tmp_path, "research_001") == agent
    assert state.list_agents(tmp_path) == {"research_001": agent}
    assert state.delete_agent(tmp_path, "research_001") is True
    assert state.delete_agent(tmp_path, "research_001") is False
    assert state.list_agents(tmp_path) == {}


def test_write_report_and_handoff_path(tmp_path):
    state.init_state_dir(tmp_path)

    report = state.write_report(tmp_path, "agent_001", {"ok": True})
    handoff = state.new_handoff_path(tmp_path, "agent_001")

    assert report.parent == state.reports_dir(tmp_path)
    assert report.name.startswith("agent_001_")
    assert json.loads(report.read_text(encoding="utf-8")) == {"ok": True}
    assert handoff.parent == state.handoffs_dir(tmp_path)
    assert handoff.name.startswith("agent_001_")
    assert handoff.suffix == ".md"


def test_pid_is_running_current_and_dead_process():
    assert state.pid_is_running(os.getpid()) is True
    assert state.pid_is_running(-1) is False
    assert state.pid_is_running(999999) is False
