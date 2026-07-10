from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import anyio
import pytest

from osw import state
from osw.watcher import Watcher, format_completion_report, main, one_line


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_one_line_and_completion_report_are_single_line():
    assert one_line("do\n  this\t now") == "do this now"

    line = format_completion_report(
        "agent_001", "term-a", "C:\\report.json",
        handoff_file="C:\\handoff.md",
        summary="done\nclean",
    )

    assert line.startswith("# [osw] task-finished")
    assert "handoff=C:\\handoff.md" in line
    assert "done clean" in line
    assert "\n" not in line


def test_watcher_retains_and_retries_state_after_storage_error(tmp_path):
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "state": "assigned",
    })
    watcher = Watcher(tmp_path, "agent_001")
    real_write = state.write_agent
    attempts = 0

    def flaky_write(root, agent):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise state.StateWriteError(
                state.agent_file(root, agent["agent_id"]),
                PermissionError("locked"),
            )
        real_write(root, agent)

    with patch("osw.watcher.write_agent", flaky_write):
        watcher._update(state="working")
        assert state.read_agent(tmp_path, "agent_001")["state"] == "assigned"

        watcher._update(state="working")

    assert state.read_agent(tmp_path, "agent_001")["state"] == "working"
    events = [
        json.loads(line)
        for line in (state.logs_dir(tmp_path) / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [event["event"] for event in events] == [
        "state_storage_error",
        "state_storage_recovered",
    ]


def test_process_boundary_does_not_misclassify_state_storage_error(tmp_path):
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "state": "working",
    })
    failure = state.StateWriteError(
        state.agent_file(tmp_path, "agent_001"),
        PermissionError("locked"),
    )

    async def fail_watcher(root, agent_id):
        raise failure

    with patch("osw.watcher.setup_logging"), \
         patch("osw.watcher.enable_file_logging"), \
         patch("osw.watcher.run_watcher", fail_watcher), \
         pytest.raises(state.StateWriteError):
        main(tmp_path, "agent_001")

    agent = state.read_agent(tmp_path, "agent_001")
    assert agent["state"] == "working"
    assert agent.get("completion_source") != "watcher_crash"
    events = [
        json.loads(line)
        for line in (state.logs_dir(tmp_path) / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [event["event"] for event in events] == ["state_storage_error"]


@pytest.mark.anyio
async def test_watcher_persists_pane_key_before_running_turns(tmp_path):
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
    })

    async def fake_run_turn(self, text, phase):
        return "ps_done"

    async def fake_finalize(self, final_state, source, error=""):
        self._update(state=final_state, completion_source=source)

    show = AsyncMock(return_value={
        "result": {
            "terminal": {
                "tabId": "tab-a",
                "leafId": "leaf-b",
                "lastOutputAt": 1,
            }
        }
    })
    with patch("osw.watcher.terminal_wait", AsyncMock(return_value={})), \
         patch("osw.watcher.terminal_show", show), \
         patch.object(Watcher, "_run_turn", fake_run_turn), \
         patch.object(Watcher, "_finalize", fake_finalize), \
         patch("osw.watcher._handoff_ok", return_value=True):
        await Watcher(tmp_path, "agent_001").run()

    agent = state.read_agent(tmp_path, "agent_001")
    assert agent["pane_key"] == "tab-a:leaf-b"
    assert agent["state"] == "done"
    assert agent["completion_source"] == "ps_done"


@pytest.mark.anyio
async def test_untracked_polling_retries_dirty_watcher_state(tmp_path):
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "state": "assigned",
    })
    watcher = Watcher(tmp_path, "agent_001")
    watcher.deadline = time.monotonic() + 0.01
    real_write = state.write_agent
    attempts = 0

    def flaky_write(root, agent):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise state.StateWriteError(
                state.agent_file(root, agent["agent_id"]),
                PermissionError("locked"),
            )
        real_write(root, agent)

    show = AsyncMock(return_value={
        "result": {"terminal": {"lastOutputAt": 0}},
    })
    with patch("osw.watcher.write_agent", flaky_write), \
         patch("osw.watcher.terminal_show", show), \
         patch("osw.watcher.POLL_SECS", 0):
        watcher._update(state="working")
        result = await watcher._wait_turn_done(time.time() * 1000)

    assert result == "timeout"
    assert state.read_agent(tmp_path, "agent_001")["state"] == "working"


@pytest.mark.anyio
async def test_watcher_emits_trace_events(tmp_path):
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
    })

    async def fake_wait_turn_done(self, sent_at_ms):
        return "ps_done"

    with patch("osw.watcher.terminal_wait", AsyncMock(return_value={})), \
         patch("osw.watcher.terminal_show", AsyncMock(return_value={
             "result": {"terminal": {"lastOutputAt": 1}},
         })), \
         patch("osw.watcher.terminal_send", AsyncMock(return_value={})), \
         patch("osw.watcher._handoff_ok", return_value=True), \
         patch.object(Watcher, "_wait_turn_done", fake_wait_turn_done):
        await Watcher(tmp_path, "agent_001").run()

    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert '"agent_id": "agent_001"' in events
    assert '"event": "watcher_started"' in events
    assert '"event": "turn_sent"' in events
    assert '"event": "watcher_finished"' in events


@pytest.mark.anyio
async def test_launch_started_task_is_not_sent_again(tmp_path):
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
        "task_started_on_launch": True,
    })

    sent: list[tuple[str, str]] = []

    async def fake_run_turn(self, text, phase):
        sent.append((phase, text))
        return "ps_done"

    async def fake_wait_turn_done(self, sent_at_ms):
        return "ps_done"

    async def fake_finalize(self, final_state, source, error=""):
        self._update(state=final_state, completion_source=source)

    with patch("osw.watcher.terminal_wait", AsyncMock(return_value={})), \
         patch("osw.watcher.terminal_show", AsyncMock(return_value={
             "result": {"terminal": {"lastOutputAt": 1}},
         })), \
         patch.object(Watcher, "_run_turn", fake_run_turn), \
         patch.object(Watcher, "_wait_turn_done", fake_wait_turn_done), \
         patch.object(Watcher, "_finalize", fake_finalize), \
         patch("osw.watcher._handoff_ok", return_value=True):
        await Watcher(tmp_path, "agent_001").run()

    assert [phase for phase, _ in sent] == ["handoff"]


@pytest.mark.anyio
async def test_finalize_report_omits_terminal_tail(tmp_path):
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
    })
    watcher = Watcher(tmp_path, "agent_001")

    await watcher._finalize("error", "ready_failed", error="timeout")

    agent = state.read_agent(tmp_path, "agent_001")
    payload = json.loads(Path(agent["report_file"]).read_text(encoding="utf-8"))
    assert "output_tail" not in payload
    assert "terminal_status" not in payload


@pytest.mark.anyio
async def test_tracking_loss_without_new_output_does_not_complete_as_idle(tmp_path):
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
    })
    watcher = Watcher(tmp_path, "agent_001")
    watcher.pane = "tab-a:leaf-b"
    watcher.deadline = time.monotonic() + 0.01

    show = AsyncMock(return_value={
        "result": {"terminal": {"lastOutputAt": 1000}},
    })
    ps = AsyncMock(return_value=[{"agents": []}])

    with patch("osw.watcher.terminal_show", show), \
         patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.POLL_SECS", 0), \
         patch("osw.watcher.FALLBACK_IDLE_STABLE_MS", 0):
        result = await watcher._wait_turn_done(time.time() * 1000)

    assert result == "timeout"


@pytest.mark.anyio
async def test_tracking_loss_uses_tui_idle_before_slow_idle_fallback(tmp_path):
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
    })
    watcher = Watcher(tmp_path, "agent_001")
    watcher.pane = "tab-a:leaf-b"
    watcher.deadline = time.monotonic() + 1

    now_ms = int(time.time() * 1000)
    output_times = iter([now_ms, now_ms + 1])

    async def show(*args, **kwargs):
        last_output = next(output_times, 2000)
        return {"result": {"terminal": {"lastOutputAt": last_output}}}
    ps = AsyncMock(return_value=[{"agents": []}])
    wait = AsyncMock(return_value={})

    with patch("osw.watcher.terminal_show", show), \
         patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_wait", wait), \
         patch("osw.watcher.POLL_SECS", 0), \
         patch("osw.watcher.FALLBACK_IDLE_STABLE_MS", 60_000):
        result = await watcher._wait_turn_done(time.time() * 1000)

    assert result == "tui_idle"
    wait.assert_awaited_once_with("term-a", "tui-idle", timeout_ms=1000)
