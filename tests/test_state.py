from __future__ import annotations

import json
import os

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


def test_read_state_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        state.read_state(tmp_path)


def test_alloc_agent_id_claims_files_atomically(tmp_path):
    state.init_state_dir(tmp_path)

    assert state.alloc_agent_id(tmp_path) == "agent_001"
    assert state.agent_file(tmp_path, "agent_001").read_text(encoding="utf-8") == "{}"
    assert state.alloc_agent_id(tmp_path) == "agent_002"


def test_agent_record_roundtrip_list_and_delete(tmp_path):
    state.init_state_dir(tmp_path)

    agent = {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "state": "assigned",
    }
    state.write_agent(tmp_path, agent)

    assert state.read_agent(tmp_path, "agent_001") == agent
    assert state.list_agents(tmp_path) == {"agent_001": agent}
    assert state.delete_agent(tmp_path, "agent_001") is True
    assert state.delete_agent(tmp_path, "agent_001") is False
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
