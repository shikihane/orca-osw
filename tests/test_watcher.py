from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import anyio
import pytest

from osw import state
from osw.orca_cli import OrcaError
from osw.watcher import Watcher, format_completion_report, main, one_line


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _ps_result(pane: str, ps_state: str, state_started_at: int = 0) -> list[dict]:
    return [{"agents": [{
        "paneKey": pane,
        "state": ps_state,
        "stateStartedAt": state_started_at,
    }]}]


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
         patch("osw.watcher.worktree_ps", AsyncMock(return_value=[])), \
         patch.object(Watcher, "_run_turn", fake_run_turn), \
         patch.object(Watcher, "_finalize", fake_finalize), \
         patch("osw.watcher._handoff_ok", return_value=True):
        await Watcher(tmp_path, "agent_001").run()

    agent = state.read_agent(tmp_path, "agent_001")
    assert agent["pane_key"] == "tab-a:leaf-b"
    assert agent["state"] == "done"
    assert agent["completion_source"] == "ps_done"


@pytest.mark.anyio
async def test_watcher_rebinds_stale_terminal_handle_by_pane(tmp_path):
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-old",
        "pane_key": "tab-a:leaf-b",
        "prompt": "",
        "state": "assigned",
    })
    stale = OrcaError(
        "Terminal handle belongs to an older runtime",
        1,
        code="terminal_handle_stale",
    )
    wait = AsyncMock(side_effect=[stale, {}])
    terminals = AsyncMock(return_value=[{
        "handle": "term-new",
        "tabId": "tab-a",
        "leafId": "leaf-b",
    }])

    with patch("osw.watcher.worktree_ps", AsyncMock(return_value=[])), \
         patch("osw.watcher.terminal_wait", wait), \
         patch("osw.watcher.terminal_list", terminals):
        await Watcher(tmp_path, "agent_001").run()

    agent = state.read_agent(tmp_path, "agent_001")
    assert agent["terminal"] == "term-new"
    assert agent["state"] == "done"
    assert wait.await_args_list[0].args[0] == "term-old"
    assert wait.await_args_list[1].args[0] == "term-new"


@pytest.mark.anyio
async def test_watcher_retries_prompt_after_stale_handle_rebind(tmp_path):
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-old",
        "prompt": "do the task",
        "state": "assigned",
    })
    stale = OrcaError(
        "Terminal handle belongs to an older runtime",
        1,
        code="terminal_handle_stale",
    )
    send = AsyncMock(side_effect=[stale, {}, {}])

    async def finish_turn(self, sent_at_ms, baseline_state_started=0, sent_text=""):
        return "ps_done"

    with patch("osw.watcher.terminal_show", AsyncMock(return_value={
             "result": {"terminal": {
                 "tabId": "tab-a", "leafId": "leaf-b", "lastOutputAt": 1,
             }},
         })), \
         patch("osw.watcher.worktree_ps", AsyncMock(
             return_value=_ps_result("tab-a:leaf-b", "done"),
         )), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.terminal_list", AsyncMock(return_value=[{
             "handle": "term-new", "tabId": "tab-a", "leafId": "leaf-b",
         }])), \
         patch.object(Watcher, "_wait_turn_done", finish_turn), \
         patch("osw.watcher._handoff_ok", return_value=True):
        await Watcher(tmp_path, "agent_001").run()

    agent = state.read_agent(tmp_path, "agent_001")
    assert agent["terminal"] == "term-new"
    assert agent["state"] == "done"
    assert [call.args[0] for call in send.await_args_list] == [
        "term-old", "term-new", "term-new",
    ]


@pytest.mark.anyio
async def test_watcher_rebinds_stale_handle_during_output_polling(tmp_path):
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-old",
        "pane_key": "tab-a:leaf-b",
        "state": "working",
    })
    watcher = Watcher(tmp_path, "agent_001")
    watcher.deadline = time.monotonic() + 1
    stale = OrcaError(
        "Terminal handle belongs to an older runtime",
        1,
        code="terminal_handle_stale",
    )
    new_output = iter([1000, 1000, 2000])

    async def show(handle):
        if handle == "term-old":
            raise stale
        return {
            "result": {
                "terminal": {"lastOutputAt": next(new_output, 2000)},
            }
        }

    with patch("osw.watcher.terminal_show", AsyncMock(side_effect=show)) as shown, \
         patch("osw.watcher.terminal_list", AsyncMock(return_value=[{
             "handle": "term-new", "tabId": "tab-a", "leafId": "leaf-b",
         }])), \
         patch("osw.watcher.worktree_ps", AsyncMock(return_value=[{"agents": []}])), \
         patch("osw.watcher.POLL_SECS", 0), \
         patch("osw.watcher.FALLBACK_IDLE_STABLE_MS", 0):
        result = await watcher._wait_turn_done(time.time() * 1000)

    assert result == "fallback_idle"
    assert [call.args[0] for call in shown.await_args_list[:2]] == [
        "term-old", "term-new",
    ]
    assert state.read_agent(tmp_path, "agent_001")["terminal"] == "term-new"


@pytest.mark.anyio
async def test_tracking_loss_tui_idle_rebinds_stale_handle(tmp_path):
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-old",
        "pane_key": "tab-a:leaf-b",
        "state": "working",
    })
    watcher = Watcher(tmp_path, "agent_001")
    watcher.deadline = time.monotonic() + 1
    now_ms = int(time.time() * 1000)
    output_times = iter([now_ms, now_ms, now_ms + 5000])

    async def show(_handle):
        return {
            "result": {
                "terminal": {"lastOutputAt": next(output_times, now_ms + 5000)},
            }
        }

    ps_results = iter([
        _ps_result("tab-a:leaf-b", "working"),
        [{"agents": []}],
        [{"agents": []}],
        [{"agents": []}],
    ])
    stale = OrcaError(
        "Terminal handle belongs to an older runtime",
        1,
        code="terminal_handle_stale",
    )

    async def wait(handle, event, timeout_ms):
        if handle == "term-old":
            raise stale
        return {}

    waiting = AsyncMock(side_effect=wait)
    with patch("osw.watcher.terminal_show", AsyncMock(side_effect=show)), \
         patch("osw.watcher.worktree_ps", AsyncMock(
             side_effect=lambda: next(ps_results, [{"agents": []}]),
         )), \
         patch("osw.watcher.terminal_wait", waiting), \
         patch("osw.watcher.terminal_list", AsyncMock(return_value=[{
             "handle": "term-new", "tabId": "tab-a", "leafId": "leaf-b",
         }])), \
         patch("osw.watcher.POLL_SECS", 0), \
         patch("osw.watcher.FALLBACK_IDLE_STABLE_MS", 60_000):
        result = await watcher._wait_turn_done(time.time() * 1000)

    assert result == "tui_idle"
    assert [call.args[0] for call in waiting.await_args_list] == [
        "term-old", "term-new",
    ]
    assert state.read_agent(tmp_path, "agent_001")["terminal"] == "term-new"


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

    async def fake_wait_turn_done(
        self, sent_at_ms, baseline_state_started=0, sent_text=""
    ):
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
async def test_startup_idle_is_readiness_not_completion(tmp_path):
    """A fresh `new` worker: startup tui-idle only makes the pane ready.

    The task prompt must be sent exactly once, and the handoff must not
    go out before a task-correlated done (working -> done with a
    stateStartedAt newer than the pre-send baseline) was observed.
    """
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
    })
    pane = "tab-a:leaf-b"
    now_ms = int(time.time() * 1000)
    task_done = now_ms + 10_000
    handoff_done = task_done + 5_000

    ps_results = iter([
        [],                                       # ready: pane not tracked yet
        [],                                       # task pre-send baseline
        _ps_result(pane, "working"),              # task turn running
        _ps_result(pane, "done", task_done),      # task turn finished
        _ps_result(pane, "done", task_done),      # handoff pre-send baseline
        _ps_result(pane, "done", task_done),      # stale done: must not count
        _ps_result(pane, "done", handoff_done),   # handoff turn finished
    ])
    ps = AsyncMock(
        side_effect=lambda: next(ps_results, _ps_result(pane, "done", handoff_done))
    )
    show = AsyncMock(return_value={
        "result": {"terminal": {
            "tabId": "tab-a", "leafId": "leaf-b", "lastOutputAt": 1,
        }}
    })
    wait = AsyncMock(return_value={})  # startup TUI reports idle immediately
    send = AsyncMock(return_value={})

    with patch("osw.watcher.terminal_show", show), \
         patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_wait", wait), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.POLL_SECS", 0), \
         patch("osw.watcher._handoff_ok", return_value=True):
        await Watcher(tmp_path, "agent_001").run()

    agent = state.read_agent(tmp_path, "agent_001")
    assert agent["state"] == "done"
    assert agent["completion_source"] == "ps_done"

    sent = [call.args[1] for call in send.await_args_list]
    assert len(sent) == 2
    assert sent[0] == "do the task"
    assert sent[1].startswith("Task wrap-up:")

    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert "launch_tui_idle" not in events
    assert '"event": "ready_observed"' in events
    assert '"event": "task_activity_observed"' in events
    assert '"event": "task_done_observed"' in events


@pytest.mark.anyio
async def test_no_post_send_task_evidence_fails_closed(tmp_path):
    """TUI looks idle but Orca never reports a task turn: error, not done."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
    })

    show = AsyncMock(return_value={
        "result": {"terminal": {
            "tabId": "tab-a", "leafId": "leaf-b", "lastOutputAt": 1000,
        }}
    })
    ps = AsyncMock(return_value=[{"agents": []}])  # no task ever observed
    wait = AsyncMock(return_value={})              # TUI idle throughout
    send = AsyncMock(return_value={})

    with patch("osw.watcher.terminal_show", show), \
         patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_wait", wait), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.POLL_SECS", 0), \
         patch("osw.watcher.HARD_TIMEOUT_SECS", 0.1):
        await Watcher(tmp_path, "agent_001").run()

    agent = state.read_agent(tmp_path, "agent_001")
    assert agent["state"] == "error"
    assert agent["completion_source"] == "timeout"
    # only the task prompt went out; no premature handoff
    send.assert_awaited_once()
    assert send.await_args.args[1] == "do the task"


@pytest.mark.anyio
async def test_ready_prefers_tracked_done_pane_over_tui_idle(tmp_path):
    """`use` on a pane worktree ps reports done: ready at once, even
    when `terminal wait --for tui-idle` would time out."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "continue this",
        "state": "assigned",
    })
    watcher = Watcher(tmp_path, "agent_001")

    show = AsyncMock(return_value={
        "result": {"terminal": {
            "tabId": "tab-a", "leafId": "leaf-b", "lastOutputAt": 1,
        }}
    })
    ps = AsyncMock(return_value=_ps_result("tab-a:leaf-b", "done", 12_345))
    wait = AsyncMock(side_effect=OrcaError("timeout waiting for tui-idle", 1))

    with patch("osw.watcher.terminal_show", show), \
         patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_wait", wait), \
         patch("osw.watcher.POLL_SECS", 0):
        source = await watcher._wait_ready()

    assert source == "ps_idle"
    wait.assert_not_awaited()
    assert state.read_agent(tmp_path, "agent_001")["pane_key"] == "tab-a:leaf-b"


@pytest.mark.anyio
async def test_untracked_pane_without_tui_idle_ready_on_output_silence(tmp_path):
    """A TUI Orca neither tracks nor recognizes (e.g. kimi): tui-idle
    never fires, so sustained output silence must make the pane ready."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
    })
    watcher = Watcher(tmp_path, "agent_001")

    silent_since = int(time.time() * 1000) - 120_000  # quiet for 2 minutes
    show = AsyncMock(return_value={
        "result": {"terminal": {
            "tabId": "tab-a", "leafId": "leaf-b",
            "lastOutputAt": silent_since,
        }}
    })
    ps = AsyncMock(return_value=[{"agents": []}])  # pane never tracked
    wait = AsyncMock(side_effect=OrcaError("timeout waiting for tui-idle", 1))

    with patch("osw.watcher.terminal_show", show), \
         patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_wait", wait), \
         patch("osw.watcher.POLL_SECS", 0):
        source = await watcher._wait_ready()

    assert source == "output_idle"


@pytest.mark.anyio
async def test_untracked_pane_ready_on_preview_marker(tmp_path):
    """kimi's fresh-session ready screen names itself in the preview:
    ready at once, without waiting out any silence window."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
    })
    watcher = Watcher(tmp_path, "agent_001")

    show = AsyncMock(return_value={
        "result": {"terminal": {
            "tabId": "tab-a", "leafId": "leaf-b",
            "lastOutputAt": int(time.time() * 1000),  # just painted
            "preview": "No session yet — one will be created on your "
                       "first message.\n > ",
        }}
    })
    ps = AsyncMock(return_value=[{"agents": []}])
    wait = AsyncMock(side_effect=OrcaError("timeout waiting for tui-idle", 1))

    with patch("osw.watcher.terminal_show", show), \
         patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_wait", wait), \
         patch("osw.watcher.POLL_SECS", 0):
        source = await watcher._wait_ready()

    assert source == "preview_marker"


@pytest.mark.anyio
async def test_untracked_pane_ready_on_composer_after_short_silence(tmp_path):
    """An idle composer (e.g. an adopted kimi pane mid-conversation,
    whose preview lacks the fresh-session marker) is ready once output
    has been silent for the short preview window — no 30s wait."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "continue this",
        "state": "assigned",
    })
    watcher = Watcher(tmp_path, "agent_001")

    silent_since = int(time.time() * 1000) - 15_000
    show = AsyncMock(return_value={
        "result": {"terminal": {
            "tabId": "tab-a", "leafId": "leaf-b",
            "lastOutputAt": silent_since,
            # mangled box-drawing glyphs, as on Windows previews
            "preview": "some earlier output\n\udc82 >    \udc82",
        }}
    })
    ps = AsyncMock(return_value=[{"agents": []}])
    wait = AsyncMock(side_effect=OrcaError("timeout waiting for tui-idle", 1))

    with patch("osw.watcher.terminal_show", show), \
         patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_wait", wait), \
         patch("osw.watcher.POLL_SECS", 0):
        source = await watcher._wait_ready()

    assert source == "preview_marker"


@pytest.mark.anyio
async def test_composer_without_silence_is_not_ready(tmp_path):
    """kimi keeps the composer painted mid-turn: a visible composer with
    fresh output must not count as ready."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "continue this",
        "state": "assigned",
    })
    watcher = Watcher(tmp_path, "agent_001")

    show = AsyncMock(return_value={
        "result": {"terminal": {
            "tabId": "tab-a", "leafId": "leaf-b",
            "lastOutputAt": int(time.time() * 1000),  # still painting
            "preview": "working...\n\udc82 >    \udc82",
        }}
    })
    ps = AsyncMock(return_value=[{"agents": []}])
    wait = AsyncMock(side_effect=OrcaError("timeout waiting for tui-idle", 1))

    with patch("osw.watcher.terminal_show", show), \
         patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_wait", wait), \
         patch("osw.watcher.POLL_SECS", 0), \
         patch("osw.watcher.READY_TIMEOUT_MS", 50):
        source = await watcher._wait_ready()

    assert source is None


@pytest.mark.anyio
async def test_untracked_pane_with_recent_output_is_not_ready(tmp_path):
    """Output silence shorter than the stable window must keep waiting."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
    })

    show = AsyncMock(return_value={
        "result": {"terminal": {
            "tabId": "tab-a", "leafId": "leaf-b",
            "lastOutputAt": int(time.time() * 1000),  # painting right now
        }}
    })
    ps = AsyncMock(return_value=[{"agents": []}])
    wait = AsyncMock(side_effect=OrcaError("timeout waiting for tui-idle", 1))
    send = AsyncMock(return_value={})

    with patch("osw.watcher.terminal_show", show), \
         patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_wait", wait), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.POLL_SECS", 0), \
         patch("osw.watcher.READY_TIMEOUT_MS", 50):
        await Watcher(tmp_path, "agent_001").run()

    agent = state.read_agent(tmp_path, "agent_001")
    assert agent["state"] == "error"
    assert agent["completion_source"] == "ready_failed"
    send.assert_not_awaited()


@pytest.mark.anyio
async def test_busy_tracked_pane_never_ready_fails_closed(tmp_path):
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "continue this",
        "state": "assigned",
    })

    show = AsyncMock(return_value={
        "result": {"terminal": {
            "tabId": "tab-a", "leafId": "leaf-b", "lastOutputAt": 1,
        }}
    })
    ps = AsyncMock(return_value=_ps_result("tab-a:leaf-b", "working"))
    wait = AsyncMock(return_value={})
    send = AsyncMock(return_value={})

    with patch("osw.watcher.terminal_show", show), \
         patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_wait", wait), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.POLL_SECS", 0), \
         patch("osw.watcher.READY_TIMEOUT_MS", 50):
        await Watcher(tmp_path, "agent_001").run()

    agent = state.read_agent(tmp_path, "agent_001")
    assert agent["state"] == "error"
    assert agent["completion_source"] == "ready_failed"
    send.assert_not_awaited()
    wait.assert_not_awaited()  # tracked pane: tui-idle is never consulted


@pytest.mark.anyio
async def test_stale_done_within_clock_skew_is_not_completion(tmp_path):
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
    })
    watcher = Watcher(tmp_path, "agent_001")
    watcher.pane = "tab-a:leaf-b"
    watcher.deadline = time.monotonic() + 0.05

    stale = int(time.time() * 1000)  # pane finished just before this send
    show = AsyncMock(return_value={
        "result": {"terminal": {"lastOutputAt": 1}},
    })
    ps = AsyncMock(return_value=_ps_result("tab-a:leaf-b", "done", stale))

    with patch("osw.watcher.terminal_show", show), \
         patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.POLL_SECS", 0):
        result = await watcher._wait_turn_done(
            time.time() * 1000, baseline_state_started=stale
        )

    assert result == "timeout"


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
async def test_tracking_loss_after_observed_work_uses_tui_idle(tmp_path):
    """The quick tui-idle exit stays available for genuine tracking
    loss: the pane was observed working this turn, then vanished from
    ps (e.g. the TUI exited when done), and new output arrived."""
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
    # baseline read, echo absorption, then the post-loss output check
    output_times = iter([now_ms, now_ms, now_ms + 5000])

    async def show(*args, **kwargs):
        last_output = next(output_times, now_ms + 5000)
        return {"result": {"terminal": {"lastOutputAt": last_output}}}
    ps_results = iter([
        _ps_result("tab-a:leaf-b", "working"),
        [{"agents": []}],
        [{"agents": []}],
        [{"agents": []}],
    ])
    ps = AsyncMock(side_effect=lambda: next(ps_results, [{"agents": []}]))
    wait = AsyncMock(return_value={})

    with patch("osw.watcher.terminal_show", show), \
         patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_wait", wait), \
         patch("osw.watcher.POLL_SECS", 0), \
         patch("osw.watcher.FALLBACK_IDLE_STABLE_MS", 60_000):
        result = await watcher._wait_turn_done(time.time() * 1000)

    assert result == "tui_idle"
    wait.assert_awaited_once_with("term-a", "tui-idle", timeout_ms=1000)


@pytest.mark.anyio
async def test_never_tracked_pane_gets_no_tui_idle_shortcut(tmp_path):
    """A pane Orca has not registered (yet) must never complete via the
    quick tui-idle check — a fresh worker whose ps entry lags behind
    would otherwise be marked done seconds after the prompt goes out."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
    })
    watcher = Watcher(tmp_path, "agent_001")
    watcher.pane = "tab-a:leaf-b"
    watcher.deadline = time.monotonic() + 0.05

    async def show(*args, **kwargs):
        # output keeps flowing: the worker is visibly busy the whole time
        return {"result": {"terminal": {"lastOutputAt": int(time.time() * 1000)}}}
    ps = AsyncMock(return_value=[{"agents": []}])
    wait = AsyncMock(return_value={})  # TUI would report idle if asked

    with patch("osw.watcher.terminal_show", show), \
         patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_wait", wait), \
         patch("osw.watcher.POLL_SECS", 0):
        result = await watcher._wait_turn_done(time.time() * 1000)

    assert result == "timeout"
    wait.assert_not_awaited()


@pytest.mark.anyio
async def test_never_tracked_pane_completes_after_stable_silence(tmp_path):
    """Never-tracked panes still finish through the slow path: new
    output after the send, then a stable-silence window."""
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

    # baseline, echo absorption, then one output burst followed by silence
    output_times = iter([1000, 1000])

    async def show(*args, **kwargs):
        return {"result": {"terminal": {"lastOutputAt": next(output_times, 2000)}}}
    ps = AsyncMock(return_value=[{"agents": []}])
    wait = AsyncMock(return_value={})

    with patch("osw.watcher.terminal_show", show), \
         patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_wait", wait), \
         patch("osw.watcher.POLL_SECS", 0), \
         patch("osw.watcher.FALLBACK_IDLE_STABLE_MS", 0):
        result = await watcher._wait_turn_done(time.time() * 1000)

    assert result == "fallback_idle"
    wait.assert_not_awaited()


@pytest.mark.anyio
async def test_prompt_echo_alone_is_not_agent_output(tmp_path):
    """The terminal rendering the prompt we sent advances lastOutputAt;
    that echo must re-baseline, not count as the agent starting work."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
    })
    watcher = Watcher(tmp_path, "agent_001")
    watcher.pane = "tab-a:leaf-b"
    watcher.deadline = time.monotonic() + 0.05

    # baseline read misses the echo; every later read sees it — and
    # nothing else ever arrives
    output_times = iter([1000])

    async def show(*args, **kwargs):
        return {"result": {"terminal": {"lastOutputAt": next(output_times, 5000)}}}
    ps = AsyncMock(return_value=[{"agents": []}])

    with patch("osw.watcher.terminal_show", show), \
         patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.POLL_SECS", 0), \
         patch("osw.watcher.FALLBACK_IDLE_STABLE_MS", 0):
        result = await watcher._wait_turn_done(time.time() * 1000)

    assert result == "timeout"


@pytest.mark.anyio
async def test_swallowed_prompt_fails_closed_and_notifies_caller(tmp_path):
    """A tracked pane that never acknowledges the prompt (no working
    state, no fresh done, no prompt echo in ps) means the input was
    swallowed by something else — a login screen, an update dialog:
    fail closed quickly and tell the caller, don't idle for an hour."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
        "caller_terminal": "term-caller",
    })
    watcher = Watcher(tmp_path, "agent_001")
    watcher.pane = "tab-a:leaf-b"
    watcher.deadline = time.monotonic() + 1

    stale = int(time.time() * 1000)
    show = AsyncMock(return_value={
        "result": {"terminal": {"lastOutputAt": 1000}},
    })
    ps = AsyncMock(return_value=_ps_result("tab-a:leaf-b", "done", stale))
    send = AsyncMock(return_value={})

    with patch("osw.watcher.terminal_show", show), \
         patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.POLL_SECS", 0), \
         patch("osw.watcher.RECEIPT_TIMEOUT_MS", 0):
        result = await watcher._wait_turn_done(
            time.time() * 1000, baseline_state_started=stale,
            sent_text="do the task",
        )

    assert result == "no_receipt"
    send.assert_awaited_once()
    assert send.await_args.args[0] == "term-caller"
    assert "prompt-not-received" in send.await_args.args[1]
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert '"event": "prompt_receipt_missing"' in events


@pytest.mark.anyio
async def test_notify_without_caller_records_error_event(tmp_path):
    """A missing caller_terminal must never silently drop a notification:
    no send is attempted and an ERROR event lands in events.jsonl."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
        "caller_terminal": None,
    })
    watcher = Watcher(tmp_path, "agent_001")
    send = AsyncMock(return_value={})

    with patch("osw.watcher.terminal_send", send):
        await watcher._notify("# [osw] task-finished agent_001")

    send.assert_not_awaited()
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert '"event": "notify_skipped"' in events
    assert '"level": "ERROR"' in events


@pytest.mark.anyio
async def test_notify_send_failure_records_error_event(tmp_path):
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
        "caller_terminal": "term-caller",
    })
    watcher = Watcher(tmp_path, "agent_001")
    send = AsyncMock(
        side_effect=OrcaError("terminal handle is stale", 1, code="terminal_handle_stale")
    )

    with patch("osw.watcher.terminal_send", send):
        await watcher._notify("# [osw] task-finished agent_001")

    send.assert_awaited_once()
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert '"event": "notify_failed"' in events
    assert '"level": "ERROR"' in events


@pytest.mark.anyio
async def test_prompt_echoed_in_ps_counts_as_receipt(tmp_path):
    """If ps reports our prompt text on the pane, the CLI took the
    input — no receipt alarm even without a working observation."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
    })
    watcher = Watcher(tmp_path, "agent_001")
    watcher.pane = "tab-a:leaf-b"
    watcher.deadline = time.monotonic() + 0.05

    stale = int(time.time() * 1000)
    entry = _ps_result("tab-a:leaf-b", "done", stale)
    entry[0]["agents"][0]["prompt"] = "do the task"
    show = AsyncMock(return_value={
        "result": {"terminal": {"lastOutputAt": 1000}},
    })
    ps = AsyncMock(return_value=entry)

    with patch("osw.watcher.terminal_show", show), \
         patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.POLL_SECS", 0), \
         patch("osw.watcher.RECEIPT_TIMEOUT_MS", 0):
        result = await watcher._wait_turn_done(
            time.time() * 1000, baseline_state_started=stale,
            sent_text="do the task",
        )

    assert result == "timeout"  # no false receipt alarm; still no completion


@pytest.mark.anyio
async def test_late_ps_registration_upgrades_to_ps_done(tmp_path):
    """Orca registers a freshly spawned pane a few polls late: the
    watcher keeps polling ps meanwhile and completes on real evidence
    instead of idle heuristics."""
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

    task_done = int(time.time() * 1000) + 10_000
    ps_results = iter([
        [{"agents": []}],                          # ps entry lags behind
        [{"agents": []}],
        _ps_result("tab-a:leaf-b", "working"),     # Orca catches up
        _ps_result("tab-a:leaf-b", "done", task_done),
    ])
    ps = AsyncMock(
        side_effect=lambda: next(
            ps_results, _ps_result("tab-a:leaf-b", "done", task_done)
        )
    )

    async def show(*args, **kwargs):
        return {"result": {"terminal": {"lastOutputAt": int(time.time() * 1000)}}}
    wait = AsyncMock(return_value={})

    with patch("osw.watcher.terminal_show", show), \
         patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_wait", wait), \
         patch("osw.watcher.POLL_SECS", 0):
        result = await watcher._wait_turn_done(time.time() * 1000)

    assert result == "ps_done"
    wait.assert_not_awaited()


@pytest.mark.anyio
async def test_background_task_badge_defers_fallback_idle(tmp_path):
    """kimi parks idle at the composer while its own background bash
    tasks run, painting a "[N task(s) running]" badge. Output silence
    then means "waiting on background tasks", not "turn over": fallback
    completion must hold off until the badge clears."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
    })
    watcher = Watcher(tmp_path, "agent_001")
    watcher.pane = "tab-a:leaf-b"
    watcher.deadline = time.monotonic() + 2

    # baseline read and echo absorption come first, then the per-poll
    # snapshots: one output burst, then silence; the badge shows for
    # the first two silent polls and clears on the third
    snapshots = iter([
        (1000, ""),
        (1000, ""),
        (2000, "all quiet\n[1 task running]"),
        (2000, "all quiet\n[1 task running]"),
    ])

    async def show(*args, **kwargs):
        last, preview = next(snapshots, (2000, "all quiet\n> "))
        return {"result": {"terminal": {
            "lastOutputAt": last,
            "preview": preview,
        }}}
    ps = AsyncMock(return_value=[{"agents": []}])

    with patch("osw.watcher.terminal_show", show), \
         patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.POLL_SECS", 0), \
         patch("osw.watcher.FALLBACK_IDLE_STABLE_MS", 0):
        result = await watcher._wait_turn_done(time.time() * 1000)

    assert result == "fallback_idle"
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert '"event": "background_tasks_defer_completion"' in events


@pytest.mark.anyio
async def test_fallback_idle_still_completes_without_badge(tmp_path):
    """The guard must not leak: an idle preview without the badge still
    completes through the slow stable-silence path."""
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

    output_times = iter([1000, 1000])

    async def show(*args, **kwargs):
        return {"result": {"terminal": {
            "lastOutputAt": next(output_times, 2000),
            "preview": "task results here\n> ",
        }}}
    ps = AsyncMock(return_value=[{"agents": []}])

    with patch("osw.watcher.terminal_show", show), \
         patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.POLL_SECS", 0), \
         patch("osw.watcher.FALLBACK_IDLE_STABLE_MS", 0):
        result = await watcher._wait_turn_done(time.time() * 1000)

    assert result == "fallback_idle"
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert "background_tasks_defer_completion" not in events


def test_handoff_template_covers_background_tasks():
    from osw.watcher import HANDOFF_TEMPLATE

    instruction = HANDOFF_TEMPLATE.format(path="C:/x/handoff.md")
    assert "background task" in instruction
    assert "\n" not in instruction
