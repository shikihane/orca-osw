from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import anyio
import pytest

from osw import state
from osw.orca_cli import OrcaError
from osw.watcher import (
    Watcher, _foreign_done, _prompt_truncated_at, format_completion_report,
    main, one_line,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _no_host_session_cache(monkeypatch):
    """Keep watcher tests hermetic: never read the host's real Orca
    agent-hook cache (`last-status.json`) for session ids."""
    monkeypatch.setattr("osw.watcher.session_for_pane", lambda pane_key: "")


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

    async def fake_run_turn(self, text, phase, **_):
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
    # The unset global switch is `off`, keeping the gate out of the
    # picture so this pins the *send* path's stale-retry.
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

    async def finish_turn(self, sent_at_ms, baseline_state_started=0, sent_text="", **_):
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
         patch.object(
             Watcher, "_verify_prompt_echo", AsyncMock(return_value=True),
         ), \
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
        self, sent_at_ms, baseline_state_started=0, sent_text="", **_
    ):
        return "ps_done"

    with patch("osw.watcher.terminal_wait", AsyncMock(return_value={})), \
         patch("osw.watcher.terminal_show", AsyncMock(return_value={
             "result": {"terminal": {"lastOutputAt": 1}},
         })), \
         patch("osw.watcher.terminal_send", AsyncMock(return_value={})), \
         patch("osw.watcher._handoff_ok", return_value=True), \
         patch.object(
             Watcher, "_verify_prompt_echo", AsyncMock(return_value=True),
         ), \
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
         patch.object(
             Watcher, "_verify_prompt_echo", AsyncMock(return_value=True),
         ), \
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
         patch.object(
             Watcher, "_verify_prompt_echo", AsyncMock(return_value=True),
         ), \
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
async def test_untracked_pane_ready_on_claude_status_bar_after_short_silence(
    tmp_path,
):
    """Claude Code v2 exposes no ps entry, no tui-idle, and no composer
    line in the preview — only its status bar with the model string.
    Gated on the short silence window, that signature is readiness:
    a fresh dispatch must not pay the full 30s stable window."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
    })
    watcher = Watcher(tmp_path, "agent_001")

    silent_since = int(time.time() * 1000) - 15_000
    show = AsyncMock(return_value={
        "result": {"terminal": {
            "tabId": "tab-a", "leafId": "leaf-b",
            "lastOutputAt": silent_since,
            "preview": " ▐claude-fable-5nginote_mpt_tool",
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
async def test_claude_status_bar_without_silence_is_not_ready(tmp_path):
    """The status bar is painted mid-turn as well: the signature only
    counts after the short silence window, never while output flows."""
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
            "lastOutputAt": int(time.time() * 1000),  # still painting
            "preview": " ▐claude-fable-5nginote_mpt_tool",
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
async def test_untracked_pane_with_recent_output_reports_pane_busy(tmp_path):
    """A ready wait that times out while the terminal kept producing
    output is a busy pane, not a dead one: report pane_busy."""
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
    assert agent["completion_source"] == "pane_busy"
    send.assert_not_awaited()


@pytest.mark.anyio
async def test_silent_pane_ready_timeout_fails_closed(tmp_path):
    """No ps entry and no output at all by the ready deadline: the
    terminal never came up — ready_failed."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
    })

    show = AsyncMock(return_value={
        "result": {"terminal": {
            "tabId": "tab-a", "leafId": "leaf-b", "lastOutputAt": 0,
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
async def test_busy_tracked_pane_extends_ready_wait_until_hard_deadline(tmp_path):
    """A tracked pane ps keeps reporting "working" is alive: the ready
    wait re-arms instead of failing after READY_TIMEOUT_MS, and only
    the hard deadline ends it — reported as pane_busy."""
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
         patch("osw.watcher.READY_TIMEOUT_MS", 50), \
         patch("osw.watcher.HARD_TIMEOUT_SECS", 0.2):
        await Watcher(tmp_path, "agent_001").run()

    agent = state.read_agent(tmp_path, "agent_001")
    assert agent["state"] == "error"
    assert agent["completion_source"] == "pane_busy"
    send.assert_not_awaited()
    wait.assert_not_awaited()  # tracked pane: tui-idle is never consulted
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert '"event": "ready_wait_busy"' in events


@pytest.mark.anyio
async def test_busy_tracked_pane_becoming_ready_receives_prompt(tmp_path):
    """The follow-up is not lost when the pane is busy past
    READY_TIMEOUT_MS: once ps reports "done" the prompt goes out."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "continue this",
        "state": "assigned",
    })

    show = AsyncMock(return_value={
        "result": {"terminal": {
            "tabId": "tab-a", "leafId": "leaf-b",
            "lastOutputAt": int(time.time() * 1000),
        }}
    })
    working = _ps_result("tab-a:leaf-b", "working")
    done = _ps_result("tab-a:leaf-b", "done", int(time.time() * 1000))
    ps_calls = iter([working] * 5 + [done])

    async def ps_seq():
        # 5 busy polls ≈ 400ms total — well past the bare 250ms
        # timeout, so only the re-arm keeps the wait alive; each
        # single poll stays far under the window even with Windows
        # timer granularity and the first iteration's state writes.
        await anyio.sleep(0.08)
        return next(ps_calls, done)

    ps = AsyncMock(side_effect=ps_seq)
    wait = AsyncMock(return_value={})

    watcher = Watcher(tmp_path, "agent_001")
    with patch("osw.watcher.terminal_show", show), \
         patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_wait", wait), \
         patch("osw.watcher.POLL_SECS", 0), \
         patch("osw.watcher.READY_TIMEOUT_MS", 250):
        source = await watcher._wait_ready()

    assert source == "ps_idle"


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
async def test_verified_scrollback_echo_counts_as_receipt(tmp_path):
    """A prompt whose echo was verified in the terminal scrollback was
    demonstrably delivered — a ps that stays blind to the new turn
    (stale "done") must not fail it closed as no_receipt."""
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
    watcher.deadline = time.monotonic() + 0.05

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
            sent_text="do the task", receipt_hint=True,
        )

    assert result == "timeout"  # no receipt alarm; just no completion yet
    send.assert_not_awaited()
    events_file = state.logs_dir(tmp_path) / "events.jsonl"
    if events_file.exists():
        assert "prompt_receipt_missing" not in events_file.read_text(
            encoding="utf-8"
        )


@pytest.mark.anyio
async def test_handoff_retry_does_not_reuse_stale_echo_as_receipt(tmp_path):
    """The handoff retry resends byte-identical text, so attempt 1's
    scrollback echo matches attempt 2's check just as well — a forged
    receipt would let a swallowed retry ride to the hard timeout
    instead of failing closed. Attempt 2 must run with echo-receipt
    disabled."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
    })

    turns = []

    async def fake_run_turn(self, text, phase, echo_receipt=True):
        turns.append((phase, echo_receipt))
        return "ps_done"  # handoff file never appears -> retry fires

    with patch.object(Watcher, "_wait_ready", AsyncMock(return_value="ps_idle")), \
         patch.object(Watcher, "_run_turn", fake_run_turn):
        await Watcher(tmp_path, "agent_001").run()

    assert turns == [
        ("task", True),
        ("handoff", True),
        ("handoff", False),  # identical resend: stale echo proves nothing
    ]


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
        # Completion guards require >= COMPLETION_SILENCE_MS of quiet
        # output before a done counts; this pane has been quiet.
        return {"result": {"terminal": {
            "lastOutputAt": int(time.time() * 1000) - 10_000,
        }}}
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


@pytest.mark.anyio
async def test_missing_prompt_echo_alarms_without_resending(tmp_path):
    """The trust-dialog failure mode: the prompt went somewhere that is
    not the composer, so it never appears in the terminal scrollback.
    The watcher must alarm the caller — but never resend on an echo
    miss alone: when the miss is false (a TUI whose echo the check
    cannot see), the resent text lands in a working agent's input
    queue as a duplicate of the user's task."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
        "caller_terminal": "term-caller",
    })
    watcher = Watcher(tmp_path, "agent_001")

    read = AsyncMock(return_value={
        "result": {"terminal": {
            "tail": ["Trust this folder?", " > Yes  > No"],
        }}
    })
    send = AsyncMock(return_value={})

    async def finish_turn(self, sent_at_ms, baseline_state_started=0, sent_text="", **_):
        return "fallback_idle"

    with patch("osw.watcher.terminal_read", read), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.PROMPT_ECHO_DELAY_SECS", 0), \
         patch.object(Watcher, "_wait_turn_done", finish_turn):
        result = await watcher._run_turn("do the task", phase="task")

    assert result == "fallback_idle"
    sent = [call.args[1] for call in send.await_args_list]
    # prompt exactly once, then the caller alarm — no duplicate prompt
    assert sent[0] == "do the task"
    assert sent.count("do the task") == 1
    assert any("prompt-echo-unverified" in line for line in sent[1:])
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert events.count('"event": "prompt_echo_missing"') == 1
    assert '"level": "ERROR"' in events
    assert "turn_resent" not in events


@pytest.mark.anyio
async def test_missing_prompt_echo_on_tracked_pane_does_not_alarm(tmp_path):
    """On a ps-tracked pane the receipt window is authoritative: a
    missing scrollback echo (normal for a tracked alternate-screen TUI
    like Claude Code, whose conversation never enters scrollback) is
    logged as an INFO event but must not alarm the caller."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
        "caller_terminal": "term-caller",
    })
    watcher = Watcher(tmp_path, "agent_001")
    watcher.pane = "pane-key"  # ps tracks this pane

    read = AsyncMock(return_value={
        "result": {"terminal": {
            "tail": ["✻ Wrangling… (0m 4s · ↓ 0.2k tokens)", "", "", ""],
        }}
    })
    send = AsyncMock(return_value={})

    async def finish_turn(self, sent_at_ms, baseline_state_started=0, sent_text="", **_):
        return "ps_done"

    with patch("osw.watcher.terminal_read", read), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.PROMPT_ECHO_DELAY_SECS", 0), \
         patch.object(Watcher, "_wait_turn_done", finish_turn):
        result = await watcher._run_turn("do the task", phase="task")

    assert result == "ps_done"
    sent = [call.args[1] for call in send.await_args_list]
    assert sent == ["do the task"]  # no caller notification
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert events.count('"event": "prompt_echo_missing"') == 1
    assert '"level": "ERROR"' not in events


@pytest.mark.anyio
async def test_prompt_echo_verified_from_scrollback(tmp_path):
    """A full-screen TUI (kimi) keeps its composer box painted in the
    preview tail even mid-turn, so the submitted message can never be
    seen there — but it sits in the scrollback, rendered with a
    decorator and wrapped across lines. Verification must pass on the
    scrollback alone."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
    })
    watcher = Watcher(tmp_path, "agent_001")

    prompt = "Summarize the build layout of this repo"
    read = AsyncMock(return_value={
        "result": {"terminal": {"tail": [
            " ✨ Summarize the build layout of this",
            "    repo",
            " ● Inspecting the tree...",
            "╭────────────────────────────────────────╮",
            "│ >                                      │",
        ]}}
    })
    send = AsyncMock(return_value={})

    async def finish_turn(self, sent_at_ms, baseline_state_started=0, sent_text="", **_):
        return "fallback_idle"

    with patch("osw.watcher.terminal_read", read), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.PROMPT_ECHO_DELAY_SECS", 0), \
         patch.object(Watcher, "_wait_turn_done", finish_turn):
        result = await watcher._run_turn(prompt, phase="task")

    assert result == "fallback_idle"
    send.assert_awaited_once()  # the prompt; no alarm, no resend
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert "prompt_echo_missing" not in events


@pytest.mark.anyio
async def test_prompt_echo_matches_clipped_echo_via_ascii_island(tmp_path):
    """kimi renders a long submission as one line clipped at the
    terminal width with a trailing ellipsis. The raw prefix then
    reaches past the clip, but the first ASCII island is short enough
    to survive it."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
    })
    watcher = Watcher(tmp_path, "agent_001")

    prompt = "分析 E_BURN.FWBurnChipID USB / pipeline 烧录流程"
    read = AsyncMock(return_value={
        "result": {"terminal": {"tail": [
            " ✨ 分析 E_BURN.FWBurnChipID…",
            " > ",
        ]}}
    })
    send = AsyncMock(return_value={})

    async def finish_turn(self, sent_at_ms, baseline_state_started=0, sent_text="", **_):
        return "fallback_idle"

    with patch("osw.watcher.terminal_read", read), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.PROMPT_ECHO_DELAY_SECS", 0), \
         patch.object(Watcher, "_wait_turn_done", finish_turn):
        result = await watcher._run_turn(prompt, phase="task")

    assert result == "fallback_idle"
    send.assert_awaited_once()
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert "prompt_echo_missing" not in events


@pytest.mark.anyio
async def test_prompt_echo_verified_for_pure_cjk_prompt(tmp_path):
    """The scrollback preserves raw text including CJK, so a fully
    non-ASCII prompt verifies against its normalized prefix."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
    })
    watcher = Watcher(tmp_path, "agent_001")

    prompt = "请总结一下这个项目的整体架构并且列出关键模块"
    read = AsyncMock(return_value={
        "result": {"terminal": {"tail": [
            f" ✨ {prompt}",
            " > ",
        ]}}
    })
    send = AsyncMock(return_value={})

    async def finish_turn(self, sent_at_ms, baseline_state_started=0, sent_text="", **_):
        return "fallback_idle"

    with patch("osw.watcher.terminal_read", read), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.PROMPT_ECHO_DELAY_SECS", 0), \
         patch.object(Watcher, "_wait_turn_done", finish_turn):
        result = await watcher._run_turn(prompt, phase="task")

    assert result == "fallback_idle"
    send.assert_awaited_once()
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert "prompt_echo_missing" not in events


@pytest.mark.anyio
async def test_prompt_echo_check_abstains_without_match_unit(tmp_path):
    """A prompt too short to yield a trustworthy match unit cannot be
    verified; a mismatch then proves nothing: no alarm."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
    })
    watcher = Watcher(tmp_path, "agent_001")

    read = AsyncMock(return_value={
        "result": {"terminal": {"tail": ["??"]}}
    })
    send = AsyncMock(return_value={})

    async def finish_turn(self, sent_at_ms, baseline_state_started=0, sent_text="", **_):
        return "fallback_idle"

    with patch("osw.watcher.terminal_read", read), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.PROMPT_ECHO_DELAY_SECS", 0), \
         patch.object(Watcher, "_wait_turn_done", finish_turn):
        result = await watcher._run_turn("继续", phase="task")

    assert result == "fallback_idle"
    send.assert_awaited_once()
    read.assert_not_awaited()
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert "prompt_echo_missing" not in events


@pytest.mark.anyio
async def test_prompt_echo_check_abstains_on_thin_scrollback_surface(tmp_path):
    """A pane exposing only its current screenful (alternate-screen TUI,
    or an Orca pane whose read cannot reach the retained buffer) has no
    echo surface: the read returns a handful of lines while its cursors
    report a far larger retained buffer. Judging the echo there is a
    guaranteed false miss — abstain instead of alarming."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
    })
    watcher = Watcher(tmp_path, "agent_001")

    read = AsyncMock(return_value={
        "result": {"terminal": {
            "tail": [" 🌒 thinking..."],
            "oldestCursor": "0",
            "latestCursor": "1586",
            "returnedLineCount": 1,
        }}
    })
    send = AsyncMock(return_value={})

    async def finish_turn(self, sent_at_ms, baseline_state_started=0, sent_text="", **_):
        return "fallback_idle"

    with patch("osw.watcher.terminal_read", read), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.PROMPT_ECHO_DELAY_SECS", 0), \
         patch.object(Watcher, "_wait_turn_done", finish_turn):
        result = await watcher._run_turn("do the task", phase="task")

    assert result == "fallback_idle"
    send.assert_awaited_once()
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert "prompt_echo_missing" not in events
    assert '"event": "prompt_echo_unverifiable"' in events


@pytest.mark.anyio
async def test_prompt_echo_pages_scrollback_past_screenful(tmp_path):
    """A default `terminal read` exposes only the pane's newest
    screenful (kimi's waiting screen reads back as one line) even when
    the cursors span a large retained buffer holding the echo. The
    check must re-read with an explicit cursor and verify against the
    real scrollback instead of judging on the screenful."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
    })
    watcher = Watcher(tmp_path, "agent_001")

    prompt = "Summarize the build layout of this repo"

    def word(n: int) -> str:  # letters vary per line, like real output
        return chr(97 + n % 26) + chr(97 + (n // 26) % 26)

    scrollback = [f" ● earlier output line {n} about {word(n)} topics"
                  for n in range(300)]
    scrollback += [
        " ✨ Summarize the build layout of this",
        "    repo",
    ]

    async def read(handle, limit=200, cursor=None):
        if cursor is None:
            return {"result": {"terminal": {
                "tail": [" 🌔 · Tip: ask Kimi to schedule tasks"],
                "oldestCursor": "1607",
                "latestCursor": "3607",
                "returnedLineCount": 1,
            }}}
        return {"result": {"terminal": {
            "tail": scrollback,
            "oldestCursor": "1607",
            "latestCursor": "3607",
            "returnedLineCount": len(scrollback),
        }}}

    send = AsyncMock(return_value={})

    async def finish_turn(self, sent_at_ms, baseline_state_started=0, sent_text="", **_):
        return "fallback_idle"

    with patch("osw.watcher.terminal_read", read), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.PROMPT_ECHO_DELAY_SECS", 0), \
         patch.object(Watcher, "_wait_turn_done", finish_turn):
        result = await watcher._run_turn(prompt, phase="task")

    assert result == "fallback_idle"
    send.assert_awaited_once()
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert "prompt_echo_missing" not in events
    assert "prompt_echo_unverifiable" not in events


@pytest.mark.anyio
async def test_prompt_echo_skips_paging_when_default_read_is_full(tmp_path):
    """A default read that already returned the full `limit` lines is
    the newest window paging would fetch again: the paged read must be
    skipped, not fired and discarded."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
    })
    watcher = Watcher(tmp_path, "agent_001")

    def word(n: int) -> str:  # letters vary per line even without digits
        return chr(97 + n % 26) + chr(97 + (n // 26) % 26)

    tail = [f" ● earlier output line {n} about {word(n)} topics"
            for n in range(399)]
    tail.append(" ✨ do the task")
    read = AsyncMock(return_value={
        "result": {"terminal": {
            "tail": tail,
            "oldestCursor": "0",
            "latestCursor": "2000",
            "returnedLineCount": len(tail),
        }}
    })
    send = AsyncMock(return_value={})

    async def finish_turn(self, sent_at_ms, baseline_state_started=0, sent_text="", **_):
        return "fallback_idle"

    with patch("osw.watcher.terminal_read", read), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.PROMPT_ECHO_DELAY_SECS", 0), \
         patch.object(Watcher, "_wait_turn_done", finish_turn):
        result = await watcher._run_turn("do the task", phase="task")

    assert result == "fallback_idle"
    assert read.await_count == 1  # echo hit on the first, unpaged look
    assert all(
        call.kwargs.get("cursor") is None for call in read.await_args_list
    )


@pytest.mark.anyio
async def test_prompt_echo_check_abstains_on_redraw_only_scrollback(tmp_path):
    """An alternate-screen TUI (Claude Code mid-turn) repaints its
    status line into the scrollback every frame: the paged read returns
    hundreds of lines that are all the same spinner with different
    counters. That flood passes the thinness gauge yet never contains
    the conversation — judging the echo there is a guaranteed false
    miss, so the check must abstain instead of alarming."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
    })
    watcher = Watcher(tmp_path, "agent_001")

    glyphs = ["✶", "✢", "✽", "*", "·"]
    frames = []
    for n in range(50):
        # every frame repaints the whole screen: spinner (glyph cycles,
        # counter digits change, padding width follows), then the
        # permanent status-bar furniture
        frames += [
            f"{glyphs[n % 5]}─Ionizing…─" + "─" * (n % 7)
            + f" (1m {n % 60}s · ↓ 4.{n % 10}k tokens)",
            "  ⏵⏵ bypass permissions on (shift+tab to cycle) · 2 agents",
            f"  main · D:\\bit\\master\\repo · {n * 137} tokens used",
        ] + [""] * 5

    async def read(handle, limit=200, cursor=None):
        if cursor is None:
            return {"result": {"terminal": {
                "tail": ["· Wrangling… (1m 52s · ↓ 4.8k tokens)", "", "", ""],
                "oldestCursor": "5504",
                "latestCursor": "7504",
                "returnedLineCount": 4,
            }}}
        return {"result": {"terminal": {
            "tail": frames,
            "oldestCursor": "5504",
            "latestCursor": "7504",
            "returnedLineCount": len(frames),
        }}}

    send = AsyncMock(return_value={})

    async def finish_turn(self, sent_at_ms, baseline_state_started=0, sent_text="", **_):
        return "fallback_idle"

    with patch("osw.watcher.terminal_read", read), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.PROMPT_ECHO_DELAY_SECS", 0), \
         patch.object(Watcher, "_wait_turn_done", finish_turn):
        result = await watcher._run_turn("do the task", phase="task")

    assert result == "fallback_idle"
    send.assert_awaited_once()
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert "prompt_echo_missing" not in events
    assert '"event": "prompt_echo_unverifiable"' in events


@pytest.mark.anyio
async def test_swallowed_prompt_in_repainted_composer_is_not_delivery(tmp_path):
    """A prompt whose Enter was swallowed sits in the composer box and
    gets repainted into the scrollback with every frame. Matching it
    there must not count as a verified echo (and must not feed the
    receipt hint): the surface is repaint frames, so the check
    abstains."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
    })
    watcher = Watcher(tmp_path, "agent_001")

    frames = []
    for n in range(50):
        frames += [
            f"· Ionizing… (1m {n % 60}s · ↓ 4.{n % 10}k tokens)",
            "│ > do the task                                  │",
            "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
        ] + [""] * 5

    read = AsyncMock(return_value={
        "result": {"terminal": {
            "tail": frames,
            "oldestCursor": "5504",
            "latestCursor": "7504",
            "returnedLineCount": len(frames),
        }}
    })
    send = AsyncMock(return_value={})

    hints = []

    async def finish_turn(self, sent_at_ms, baseline_state_started=0,
                          sent_text="", receipt_hint=False, **_):
        hints.append(receipt_hint)
        return "fallback_idle"

    with patch("osw.watcher.terminal_read", read), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.PROMPT_ECHO_DELAY_SECS", 0), \
         patch.object(Watcher, "_wait_turn_done", finish_turn):
        result = await watcher._run_turn("do the task", phase="task")

    assert result == "fallback_idle"
    assert hints == [False]  # the composer match never became receipt
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert "prompt_echo_missing" not in events
    assert '"event": "prompt_echo_unverifiable"' in events


@pytest.mark.anyio
async def test_prompt_echo_missing_still_alarms_on_real_scrollback(tmp_path):
    """The redraw check must not swallow genuine misses: a long
    scrollback of real conversation lines (each seen once) is a usable
    echo surface, so a prompt absent from it still alarms."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
    })
    watcher = Watcher(tmp_path, "agent_001")

    def word(n: int) -> str:  # letters vary per line even without digits
        return chr(97 + n % 26) + chr(97 + (n // 26) % 26)

    tail = [f" ● earlier output line {n} about {word(n)} topics"
            for n in range(300)]
    read = AsyncMock(return_value={
        "result": {"terminal": {
            "tail": tail,
            "oldestCursor": "18654",
            "latestCursor": "20654",
            "returnedLineCount": len(tail),
        }}
    })
    send = AsyncMock(return_value={})

    async def finish_turn(self, sent_at_ms, baseline_state_started=0, sent_text="", **_):
        return "fallback_idle"

    with patch("osw.watcher.terminal_read", read), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.PROMPT_ECHO_DELAY_SECS", 0), \
         patch.object(Watcher, "_wait_turn_done", finish_turn):
        result = await watcher._run_turn("do the task", phase="task")

    assert result == "fallback_idle"
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert '"event": "prompt_echo_missing"' in events
    assert "prompt_echo_unverifiable" not in events


@pytest.mark.anyio
async def test_prompt_echo_verified_with_full_retained_scrollback(tmp_path):
    """A read whose tail covers the retained buffer is a usable echo
    surface: the wrapped echo inside it verifies the prompt."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
    })
    watcher = Watcher(tmp_path, "agent_001")

    prompt = "Summarize the build layout of this repo"

    def word(n: int) -> str:  # letters vary per line, like real output
        return chr(97 + n % 26) + chr(97 + (n // 26) % 26)

    tail = [f" ● earlier output line {n} about {word(n)} topics"
            for n in range(300)]
    tail += [
        " ✨ Summarize the build layout of this",
        "    repo",
        "╭────────────────────────────────────────╮",
        "│ >                                      │",
    ]
    read = AsyncMock(return_value={
        "result": {"terminal": {
            "tail": tail,
            "oldestCursor": "18654",
            "latestCursor": "20654",
            "returnedLineCount": len(tail),
        }}
    })
    send = AsyncMock(return_value={})

    async def finish_turn(self, sent_at_ms, baseline_state_started=0, sent_text="", **_):
        return "fallback_idle"

    with patch("osw.watcher.terminal_read", read), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.PROMPT_ECHO_DELAY_SECS", 0), \
         patch.object(Watcher, "_wait_turn_done", finish_turn):
        result = await watcher._run_turn(prompt, phase="task")

    assert result == "fallback_idle"
    send.assert_awaited_once()
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert "prompt_echo_missing" not in events
    assert "prompt_echo_unverifiable" not in events


@pytest.mark.anyio
async def test_prompt_echo_verified_when_wrap_splits_cjk_prefix(tmp_path):
    """CJK glyphs are double-width, so on a narrow pane the terminal
    wraps the echo inside the 24-char match prefix. Verification must
    ignore the wrap instead of reporting a false miss."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
    })
    watcher = Watcher(tmp_path, "agent_001")

    prompt = "请分析这个仓库里位置词先验与重排层如何配合的问题并给出改进方案"
    read = AsyncMock(return_value={
        "result": {"terminal": {"tail": [
            " ✨ 请分析这个仓库里位置词",
            "    先验与重排层如何配合的问",
            "    题并给出改进方案",
            " ● Inspecting the tree...",
            " > ",
        ]}}
    })
    send = AsyncMock(return_value={})

    async def finish_turn(self, sent_at_ms, baseline_state_started=0, sent_text="", **_):
        return "fallback_idle"

    with patch("osw.watcher.terminal_read", read), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.PROMPT_ECHO_DELAY_SECS", 0), \
         patch.object(Watcher, "_wait_turn_done", finish_turn):
        result = await watcher._run_turn(prompt, phase="task")

    assert result == "fallback_idle"
    send.assert_awaited_once()
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert "prompt_echo_missing" not in events


@pytest.mark.anyio
async def test_prompt_echo_check_abstains_when_terminal_unreadable(tmp_path):
    """A scrollback read failure means no verdict is possible: abstain
    instead of alarming."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
    })
    watcher = Watcher(tmp_path, "agent_001")

    read = AsyncMock(side_effect=OrcaError("terminal gone", 1))
    send = AsyncMock(return_value={})

    async def finish_turn(self, sent_at_ms, baseline_state_started=0, sent_text="", **_):
        return "fallback_idle"

    with patch("osw.watcher.terminal_read", read), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.PROMPT_ECHO_DELAY_SECS", 0), \
         patch.object(Watcher, "_wait_turn_done", finish_turn):
        result = await watcher._run_turn("do the task", phase="task")

    assert result == "fallback_idle"
    send.assert_awaited_once()
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert "prompt_echo_missing" not in events


@pytest.mark.anyio
async def test_blocking_dialog_is_never_ready(tmp_path):
    """A silent terminal showing kimi's 'Trust this folder?' dialog is
    a blocked screen, not an idle composer: readiness must not fire,
    and the dialog must be surfaced to the caller."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
        "caller_terminal": "term-caller",
    })
    watcher = Watcher(tmp_path, "agent_001")

    silent_since = int(time.time() * 1000) - 120_000
    show = AsyncMock(return_value={
        "result": {"terminal": {
            "tabId": "tab-a", "leafId": "leaf-b",
            "lastOutputAt": silent_since,
            "preview": "Trust this folder?\n > Yes, trust  > No",
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
        source = await watcher._wait_ready()

    assert source is None
    send.assert_awaited_once()
    assert "agent-dialog" in send.await_args.args[1]
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert '"event": "blocking_dialog_detected"' in events


@pytest.mark.anyio
async def test_send_stalled_proceeds_to_turn_supervision(tmp_path):
    """`agent_prompt_stalled` means the write landed but Orca saw no
    fresh idle->working edge within its short window (the prompt
    arrived at a TUI that was not ready for it yet). Not a send
    failure: the turn proceeds to the receipt window. On a ps-tracked
    pane that window is authoritative (a swallowed prompt fails closed
    as no_receipt with its own notification), so the unconfirmed send
    is only logged — the caller is not alarmed for a prompt that may
    well have landed."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
        "caller_terminal": "term-caller",
    })
    watcher = Watcher(tmp_path, "agent_001")
    watcher.pane = "pane-key"  # ps tracks this pane

    read = AsyncMock(return_value={
        "result": {"terminal": {"tail": ["✻ Grooving… (0m 5s)", "", "", ""]}}
    })
    stalled = OrcaError("agent prompt stalled", 1, code="agent_prompt_stalled")
    send = AsyncMock(side_effect=stalled)

    async def finish_turn(self, sent_at_ms, baseline_state_started=0, sent_text="", **_):
        return "ps_done"

    with patch("osw.watcher.terminal_read", read), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.PROMPT_ECHO_DELAY_SECS", 0), \
         patch.object(Watcher, "_wait_turn_done", finish_turn):
        result = await watcher._run_turn("do the task", phase="task")

    assert result == "ps_done"
    sent = [call.args[1] for call in send.await_args_list]
    assert sent[0] == "do the task"  # prompt sent once, never resent
    assert sent.count("do the task") == 1
    assert not any("prompt-delivery-unconfirmed" in line for line in sent)
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert events.count('"event": "prompt_delivery_unconfirmed"') == 1
    assert '"level": "WARNING"' not in events
    assert '"event": "turn_send_failed"' not in events


@pytest.mark.anyio
async def test_send_stalled_on_untracked_pane_notifies(tmp_path):
    """The same stalled write on a pane ps does not track has no
    receipt window to fall back on, so the caller is alerted (mirrors
    the echo-miss policy), but the turn still proceeds."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
        "caller_terminal": "term-caller",
    })
    watcher = Watcher(tmp_path, "agent_001")

    read = AsyncMock(return_value={
        "result": {"terminal": {"tail": ["✻ Grooving… (0m 5s)", "", "", ""]}}
    })
    stalled = OrcaError("agent prompt stalled", 1, code="agent_prompt_stalled")
    send = AsyncMock(side_effect=stalled)

    async def finish_turn(self, sent_at_ms, baseline_state_started=0, sent_text="", **_):
        return "fallback_idle"

    with patch("osw.watcher.terminal_read", read), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.PROMPT_ECHO_DELAY_SECS", 0), \
         patch.object(Watcher, "_wait_turn_done", finish_turn):
        result = await watcher._run_turn("do the task", phase="task")

    assert result == "fallback_idle"
    sent = [call.args[1] for call in send.await_args_list]
    assert sent[0] == "do the task"  # prompt exactly once
    assert any("prompt-delivery-unconfirmed" in line for line in sent[1:])
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert events.count('"event": "prompt_delivery_unconfirmed"') == 1
    assert '"level": "WARNING"' in events
    assert '"event": "turn_send_failed"' not in events


@pytest.mark.anyio
async def test_send_real_failure_still_fails_closed(tmp_path):
    """Genuine write failures (e.g. the terminal rejected the write)
    keep failing the turn closed as send_failed — only
    `agent_prompt_stalled` is delivery-unconfirmed."""
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
        "caller_terminal": "term-caller",
    })
    watcher = Watcher(tmp_path, "agent_001")

    not_writable = OrcaError(
        "terminal not writable", 1, code="terminal_not_writable"
    )
    send = AsyncMock(side_effect=not_writable)

    with patch("osw.watcher.terminal_send", send):
        result = await watcher._run_turn("do the task", phase="task")

    assert result == "send_failed"
    send.assert_awaited_once()
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert events.count('"event": "turn_send_failed"') == 1
    assert '"level": "ERROR"' in events
    assert '"event": "prompt_delivery_unconfirmed"' not in events


# ---------------------------------------------------------------------------
# Two-stage send (design §12.4-1)
# ---------------------------------------------------------------------------

def _claude_agent(tmp_path):
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
        "provider": "claude",
    })
    watcher = Watcher(tmp_path, "agent_001")
    watcher.pane = "tab-a:leaf-b"
    return watcher


@pytest.mark.anyio
async def test_split_send_writes_confirms_then_enters(tmp_path):
    watcher = _claude_agent(tmp_path)
    sends = AsyncMock(return_value={"result": {"send": {"accepted": True}}})
    reads = AsyncMock(return_value={
        "result": {"terminal": {"source": "screen",
                                "tail": ["❯ do the task now"]}},
    })

    with patch("osw.watcher.terminal_send", sends), \
         patch("osw.watcher.terminal_read", reads), \
         patch("osw.watcher.SPLIT_SEND_CONFIRM_SECS", 0.05), \
         patch("osw.watcher.SPLIT_SEND_POLL_SECS", 0):
        confirmed = await watcher._send_terminal("do the task now")

    assert confirmed is True
    assert [c.kwargs for c in sends.await_args_list] == [
        {"enter": False}, {"enter": True},
    ]
    assert [c.args for c in sends.await_args_list] == [
        ("term-a", "[200~do the task now[201~"), ("term-a", ""),
    ]


@pytest.mark.anyio
async def test_split_send_long_prompt_is_one_bracketed_paste(tmp_path):
    """A raw (unbracketed) write over 2048 chars reaches Claude Code as
    keystrokes it mistakes for a paste, keeping only the text after
    char 2048. The whole prompt must go out as one bracketed paste,
    with embedded ESC bytes stripped so they cannot end it early."""
    watcher = _claude_agent(tmp_path)
    sends = AsyncMock(return_value={"result": {"send": {"accepted": True}}})
    reads = AsyncMock(return_value={
        "result": {"terminal": {"source": "screen",
                                "tail": ["❯ [Pasted text #1]"]}},
    })
    prompt = "Repo X. ISSUE 1: " + "y" * 2400 + "[201~ tail"

    with patch("osw.watcher.terminal_send", sends),          patch("osw.watcher.terminal_read", reads),          patch("osw.watcher.SPLIT_SEND_CONFIRM_SECS", 0.05),          patch("osw.watcher.SPLIT_SEND_POLL_SECS", 0):
        confirmed = await watcher._send_terminal(prompt)

    assert confirmed is True
    written = sends.await_args_list[0].args[1]
    assert written == (
        "[200~" + prompt.replace("", "") + "[201~"
    )
    assert written.count("") == 2


@pytest.mark.anyio
async def test_split_send_types_slash_commands_raw(tmp_path):
    """A pasted `/command` is plain text to Claude Code; slash commands
    keep the raw write so they still run."""
    watcher = _claude_agent(tmp_path)
    sends = AsyncMock(return_value={"result": {"send": {"accepted": True}}})
    reads = AsyncMock(return_value={
        "result": {"terminal": {"source": "screen", "tail": ["❯ /compact"]}},
    })

    with patch("osw.watcher.terminal_send", sends),          patch("osw.watcher.terminal_read", reads),          patch("osw.watcher.SPLIT_SEND_CONFIRM_SECS", 0.05),          patch("osw.watcher.SPLIT_SEND_POLL_SECS", 0):
        await watcher._send_terminal("/compact")

    assert sends.await_args_list[0].args == ("term-a", "/compact")


@pytest.mark.anyio
async def test_split_send_timeout_still_enters_and_reports_unconfirmed(tmp_path):
    watcher = _claude_agent(tmp_path)
    sends = AsyncMock(return_value={"result": {"send": {"accepted": True}}})
    reads = AsyncMock(return_value={
        "result": {"terminal": {"source": "screen", "tail": ["starting…"]}},
    })

    with patch("osw.watcher.terminal_send", sends), \
         patch("osw.watcher.terminal_read", reads), \
         patch("osw.watcher.SPLIT_SEND_CONFIRM_SECS", 0.0), \
         patch("osw.watcher.SPLIT_SEND_POLL_SECS", 0):
        confirmed = await watcher._send_terminal("do the task now")

    assert confirmed is False
    assert [c.kwargs for c in sends.await_args_list] == [
        {"enter": False}, {"enter": True},
    ]


@pytest.mark.anyio
async def test_split_send_keeps_legacy_path_for_other_providers(tmp_path):
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
        "provider": "omp",
    })
    watcher = Watcher(tmp_path, "agent_001")
    sends = AsyncMock(return_value={})

    with patch("osw.watcher.terminal_send", sends):
        confirmed = await watcher._send_terminal("do the task now")

    assert confirmed is True
    assert sends.await_args_list[0].args == ("term-a", "do the task now")
    assert sends.await_args_list[0].kwargs == {}


@pytest.mark.anyio
async def test_split_send_rejected_write_is_a_send_failure(tmp_path):
    watcher = _claude_agent(tmp_path)
    sends = AsyncMock(return_value={"result": {"send": {"accepted": False}}})

    with patch("osw.watcher.terminal_send", sends):
        with pytest.raises(OrcaError):
            await watcher._send_terminal("do the task now")

    assert len(sends.await_args_list) == 1


# ---------------------------------------------------------------------------
# Completion attribution guards (design §1.5)
# ---------------------------------------------------------------------------

def test_foreign_done_helper_rules():
    # ours, verbatim or by receipt prefix -> not foreign
    assert not _foreign_done({"prompt": "do the task now"}, "do the task now")
    assert not _foreign_done(
        {"prompt": "do the task now plus trailing text"},
        "do the task now",
    )
    # pure-ASCII ps prompt sharing nothing with ours -> foreign
    assert _foreign_done(
        {"prompt": "Reply with exactly one word: OK"},
        "任务：按设计文档施工",
    )
    # a partially mangled echo still shares a long run with ours
    assert not _foreign_done(
        {"prompt": "docs/research/2026-10-09 design"},
        "请按 docs/research/2026-10-09 施工",
    )
    # CJK or empty ps prompts are never judged
    assert not _foreign_done({"prompt": "任务（实现）"}, "do the task")
    assert not _foreign_done({}, "do the task")


def test_prompt_truncated_at_helper_rules():
    sent = "".join(f"[{i:04d}]" for i in range(0, 2400, 6))
    # ps keeps the first 200 chars of what the agent received
    assert _prompt_truncated_at({"prompt": sent[:200]}, sent) == 0
    assert _prompt_truncated_at({"prompt": sent[2048:2248]}, sent) == 2048
    # someone else's prompt, a CJK (maybe mangled) one, or a too-short
    # fragment proves nothing
    assert _prompt_truncated_at(
        {"prompt": "Reply with exactly one word: OK"}, sent) == 0
    assert _prompt_truncated_at(
        {"prompt": "任务" + sent[2048:2100]}, sent) == 0
    assert _prompt_truncated_at({"prompt": sent[2048:2060]}, sent) == 0
    assert _prompt_truncated_at({}, sent) == 0


@pytest.mark.anyio
async def test_truncated_prompt_is_reported_even_on_a_tracked_pane(tmp_path):
    """ps shows the agent received only the prompt's tail: alarm the
    caller once, never resend, and let the turn complete normally."""
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
    watcher.deadline = time.monotonic() + 2

    sent = "".join(f"[{i:04d}]" for i in range(0, 2400, 6))
    entry = _ps_result("tab-a:leaf-b", "done", int(time.time() * 1000))
    entry[0]["agents"][0]["prompt"] = sent[2048:2248]
    show = AsyncMock(return_value={"result": {"terminal": {
        "lastOutputAt": int(time.time() * 1000) - 10_000,
    }}})
    send = AsyncMock(return_value={})

    with patch("osw.watcher.terminal_show", show),          patch("osw.watcher.worktree_ps", AsyncMock(return_value=entry)),          patch("osw.watcher.terminal_send", send),          patch("osw.watcher.POLL_SECS", 0):
        result = await watcher._wait_turn_done(
            time.time() * 1000 - 1000, sent_text=sent,
        )

    assert result == "ps_done"
    assert watcher._turn_truncated_at == 2048
    alarms = [c.args for c in send.await_args_list]
    assert len(alarms) == 1
    assert alarms[0][0] == "term-caller"
    assert "prompt-truncated agent=agent_001" in alarms[0][1]
    assert "received_from=2048" in alarms[0][1]
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8")
    assert events.count('"event": "prompt_truncated"') == 1


@pytest.mark.anyio
async def test_stale_ps_row_is_not_judged_for_truncation(tmp_path):
    """A row from before the send still carries the previous turn's
    prompt, which may well appear inside the new one."""
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

    previous = "Investigate the boot-time guard in the recorder service"
    entry = _ps_result("tab-a:leaf-b", "done",
                       int(time.time() * 1000) - 600_000)
    entry[0]["agents"][0]["prompt"] = previous
    show = AsyncMock(return_value={"result": {"terminal": {
        "lastOutputAt": int(time.time() * 1000) - 10_000,
    }}})

    with patch("osw.watcher.terminal_show", show),          patch("osw.watcher.worktree_ps", AsyncMock(return_value=entry)),          patch("osw.watcher.POLL_SECS", 0):
        await watcher._wait_turn_done(
            time.time() * 1000,
            sent_text="Follow-up on: " + previous + ", now add tests",
        )

    assert watcher._turn_truncated_at == 0


@pytest.mark.anyio
async def test_foreign_done_never_completes_the_turn(tmp_path):
    """A nested session's done (ASCII ps prompt that is not ours) is
    ignored even while the pane is output-silent (design §1.5)."""
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

    entry = _ps_result("tab-a:leaf-b", "done", int(time.time() * 1000))
    entry[0]["agents"][0]["prompt"] = "Reply with exactly one word: OK"
    ps = AsyncMock(return_value=entry)
    show = AsyncMock(return_value={"result": {"terminal": {
        "lastOutputAt": int(time.time() * 1000) - 10_000,
    }}})

    with patch("osw.watcher.terminal_show", show), \
         patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.POLL_SECS", 0):
        result = await watcher._wait_turn_done(
            time.time() * 1000, sent_text="实现加固批次：按设计文档施工",
        )

    assert result == "timeout"
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8")
    assert '"event": "foreign_done_ignored"' in events
    assert '"event": "task_done_observed"' not in events


@pytest.mark.anyio
async def test_done_with_recent_output_is_not_completion(tmp_path):
    """The silence guard: a done whose pane is still producing output
    never completes the turn."""
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

    ps = AsyncMock(return_value=_ps_result(
        "tab-a:leaf-b", "done", int(time.time() * 1000)))
    show = AsyncMock(return_value={"result": {"terminal": {
        "lastOutputAt": int(time.time() * 1000),  # output right now
    }}})

    with patch("osw.watcher.terminal_show", show), \
         patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.POLL_SECS", 0):
        result = await watcher._wait_turn_done(
            time.time() * 1000, sent_text="do the task",
        )

    assert result == "timeout"


@pytest.mark.anyio
async def test_persistent_done_on_quiet_pane_completes(tmp_path):
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

    task_done = int(time.time() * 1000)
    ps = AsyncMock(return_value=_ps_result("tab-a:leaf-b", "done", task_done))
    show = AsyncMock(return_value={"result": {"terminal": {
        "lastOutputAt": int(time.time() * 1000) - 10_000,
    }}})

    with patch("osw.watcher.terminal_show", show), \
         patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.POLL_SECS", 0):
        result = await watcher._wait_turn_done(
            time.time() * 1000, sent_text="do the task",
        )

    assert result == "ps_done"


# ---------------------------------------------------------------------------
# Phase 0 deterministic gate wiring (design §5.0)
# ---------------------------------------------------------------------------

_GATE_ROOT = Path(__file__).parent / "fixtures" / "frames" / "claude"


def _gate_fixture(name: str) -> dict:
    data = json.loads(
        (_GATE_ROOT / f"{name}.json").read_text(encoding="utf-8"))
    return {"result": {"terminal": {
        "source": data["source"], "tail": data["tail"],
    }}}


@pytest.mark.anyio
async def test_gate_ready_releases_ps_idle_immediately(tmp_path):
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
        "provider": "claude",
    })
    watcher = Watcher(tmp_path, "agent_001")
    watcher.pane = "tab-a:leaf-b"
    watcher.deadline = time.monotonic() + 5

    reads = AsyncMock(return_value=_gate_fixture("idle_composer"))
    ps = AsyncMock(return_value=_ps_result(
        "tab-a:leaf-b", "done", int(time.time() * 1000)))

    with patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_read", reads), \
         patch("osw.watcher.POLL_SECS", 0):
        source = await watcher._wait_ready()

    assert source == "ps_idle"
    assert reads.await_count == 1
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8")
    assert '"event": "gate_verdict"' in events
    assert '"verdict": "ready"' in events
    assert '"event": "gate_released"' not in events


@pytest.mark.anyio
async def test_gate_not_ready_waits_bounded_then_releases(tmp_path):
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
        "provider": "claude",
    })
    watcher = Watcher(tmp_path, "agent_001")
    watcher.pane = "tab-a:leaf-b"
    watcher.deadline = time.monotonic() + 5

    reads = AsyncMock(return_value={
        "result": {"terminal": {"source": "screen", "tail": ["booting"]}},
    })
    ps = AsyncMock(return_value=_ps_result(
        "tab-a:leaf-b", "done", int(time.time() * 1000)))

    with patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_read", reads), \
         patch("osw.watcher.POLL_SECS", 0), \
         patch("osw.watcher.GATE_POLL_SECS", 0), \
         patch("osw.watcher.COMPOSER_WAIT_MS", 30):
        source = await watcher._wait_ready()

    assert source == "ps_idle"
    assert reads.await_count >= 2
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8")
    assert '"event": "gate_released"' in events
    assert '"reason": "wait_expired"' in events


@pytest.mark.anyio
async def test_gate_abstains_without_frame_read_for_unknown_provider(tmp_path):
    state.init_state_dir(tmp_path)
    # The unset global switch is `off`: no frame read, no judgment.
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
    })
    watcher = Watcher(tmp_path, "agent_001")
    watcher.pane = "tab-a:leaf-b"
    watcher.deadline = time.monotonic() + 5

    reads = AsyncMock(side_effect=AssertionError("must not read"))
    ps = AsyncMock(return_value=_ps_result(
        "tab-a:leaf-b", "done", int(time.time() * 1000)))

    with patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_read", reads), \
         patch("osw.watcher.POLL_SECS", 0):
        source = await watcher._wait_ready()

    assert source == "ps_idle"
    reads.assert_not_awaited()
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8")
    assert '"reason": "provider_without_predicate"' in events


@pytest.mark.anyio
async def test_gate_wait_survives_busy_branch_and_keeps_clock(tmp_path):
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
        "provider": "claude",
    })
    watcher = Watcher(tmp_path, "agent_001")
    watcher.pane = "tab-a:leaf-b"
    watcher.deadline = time.monotonic() + 5

    done_ts = int(time.time() * 1000)
    ps_results = iter([
        _ps_result("tab-a:leaf-b", "done", done_ts),
        _ps_result("tab-a:leaf-b", "working"),
    ])
    ps = AsyncMock(side_effect=lambda: next(
        ps_results, _ps_result("tab-a:leaf-b", "done", done_ts)))
    reads = AsyncMock(return_value={
        "result": {"terminal": {"source": "screen", "tail": ["booting"]}},
    })

    with patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_read", reads), \
         patch("osw.watcher.POLL_SECS", 0), \
         patch("osw.watcher.GATE_POLL_SECS", 0), \
         patch("osw.watcher.COMPOSER_WAIT_MS", 30):
        source = await watcher._wait_ready()

    assert source == "ps_idle"
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8")
    assert '"event": "ready_wait_busy"' in events
    assert '"reason": "wait_expired"' in events


# ---------------------------------------------------------------------------
# Jev consultation at the gate (design §5.1): shadow and enforce holds
# ---------------------------------------------------------------------------

def _claude_gate_setup(tmp_path, mode: str = "off",
                       providers: list[str] | None = None):
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
        "provider": "claude",
    })
    # The mode is the global switch; `providers` is ignored now (the
    # whitelist is fixed) and kept for call-site compatibility.
    if mode == "off":
        os.environ.pop("OSW_JEV_MODE", None)
    else:
        os.environ["OSW_JEV_MODE"] = mode
    watcher = Watcher(tmp_path, "agent_001")
    watcher.pane = "tab-a:leaf-b"
    watcher.deadline = time.monotonic() + 30
    return watcher


_BOOTING = {"result": {"terminal": {"source": "screen",
                                    "tail": ["booting"]}}}
_BLOCKED_ANSWER = {
    "choice": "blocked_startup",
    "probabilities": {"blocked_startup": 0.9},
    "confidence": 0.8,
}


@pytest.mark.anyio
async def test_shadow_mode_fires_background_judgment_without_gating(tmp_path):
    watcher = _claude_gate_setup(tmp_path, mode="shadow")
    reads = AsyncMock(return_value=_BOOTING)
    ps = AsyncMock(return_value=_ps_result(
        "tab-a:leaf-b", "done", int(time.time() * 1000)))

    with patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_read", reads), \
         patch("osw.watcher.POLL_SECS", 0), \
         patch("osw.watcher.GATE_POLL_SECS", 0), \
         patch("osw.watcher.COMPOSER_WAIT_MS", 0):
        async with anyio.create_task_group() as tg:
            watcher._task_group = tg
            source = await watcher._wait_ready()
            await anyio.sleep(0.05)

    assert source == "ps_idle"
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8")
    # No key/SDK in the environment: the shadow judgment degrades cleanly.
    assert '"event": "jev_degraded"' in events
    assert '"reason": "no_key"' in events


@pytest.mark.anyio
async def test_gate_api_retry_waits_for_recovery(tmp_path):
    """An upstream-API retry screen is its own verdict: the watcher keeps
    waiting for recovery — no prompt, no failure, no Jev call."""
    watcher = _claude_gate_setup(
        tmp_path, mode="enforce", providers=["claude"])
    retry_frame = {"result": {"terminal": {"source": "screen", "tail": [
        "  Waiting for API response · will retry · check your network",
    ]}}}
    frames = iter([retry_frame, _gate_fixture("idle_composer")])
    reads = AsyncMock(
        side_effect=lambda *a, **k: next(
            frames, _gate_fixture("idle_composer")))
    ps = AsyncMock(return_value=_ps_result(
        "tab-a:leaf-b", "done", int(time.time() * 1000)))
    ask = AsyncMock(return_value=_BLOCKED_ANSWER)

    with patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_read", reads), \
         patch("osw.watcher.ask_ready", ask), \
         patch("osw.watcher.POLL_SECS", 0), \
         patch("osw.watcher.GATE_POLL_SECS", 0):
        source = await watcher._wait_ready()

    assert source == "ps_idle"
    # No Jev call for the retry frame itself; the recovered ready frame
    # does consult Jev once (the 2026-10-11 every-consult override).
    assert ask.await_count == 1
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8")
    assert '"event": "gate_api_retry"' in events


@pytest.mark.anyio
async def test_gate_ready_consults_jev_as_advisory_only(tmp_path):
    """Operator override 2026-10-11: a ready verdict consults Jev too
    (the typical path used to stay at zero calls), but the deterministic
    predicate stays authoritative — a blocked judgment there is recorded
    and can never start a hold."""
    watcher = _claude_gate_setup(
        tmp_path, mode="enforce", providers=["claude"])
    reads = AsyncMock(return_value=_gate_fixture("idle_composer"))
    ps = AsyncMock(return_value=_ps_result(
        "tab-a:leaf-b", "done", int(time.time() * 1000)))
    ask = AsyncMock(return_value=_BLOCKED_ANSWER)

    with patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_read", reads), \
         patch("osw.watcher.ask_ready", ask), \
         patch("osw.watcher.POLL_SECS", 0):
        source = await watcher._wait_ready()

    assert source == "ps_idle"
    assert ask.await_count == 1
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8")
    assert '"action": "held"' not in events


@pytest.mark.anyio
async def test_enforce_hold_starts_then_fast_path_releases(tmp_path):
    watcher = _claude_gate_setup(
        tmp_path, mode="enforce", providers=["claude"])
    frames = iter([_BOOTING, _gate_fixture("idle_composer")])
    reads = AsyncMock(side_effect=lambda *a, **k: next(frames, _BOOTING))
    ps = AsyncMock(return_value=_ps_result(
        "tab-a:leaf-b", "done", int(time.time() * 1000)))
    ask = AsyncMock(return_value=_BLOCKED_ANSWER)

    with patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_read", reads), \
         patch("osw.watcher.ask_ready", ask), \
         patch("osw.watcher.POLL_SECS", 0), \
         patch("osw.watcher.GATE_POLL_SECS", 0):
        source = await watcher._wait_ready()

    assert source == "ps_idle"
    assert ask.await_count == 1
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8")
    assert '"event": "jev_veto"' in events
    assert '"action": "held"' in events
    assert '"reason": "fast_path"' in events


@pytest.mark.anyio
async def test_enforce_hold_releases_on_mode_off_mid_hold(tmp_path):
    watcher = _claude_gate_setup(
        tmp_path, mode="enforce", providers=["claude"])
    reads = AsyncMock(return_value=_BOOTING)
    ps = AsyncMock(return_value=_ps_result(
        "tab-a:leaf-b", "done", int(time.time() * 1000)))
    ask = AsyncMock(return_value=_BLOCKED_ANSWER)

    state_checked = {"n": 0}

    def ps_side_effect():
        state_checked["n"] += 1
        if state_checked["n"] == 2:
            # Kill switch flips mid-hold (global switch).
            os.environ["OSW_JEV_MODE"] = "off"
        return _ps_result("tab-a:leaf-b", "done", int(time.time() * 1000))

    ps = AsyncMock(side_effect=ps_side_effect)

    with patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_read", reads), \
         patch("osw.watcher.ask_ready", ask), \
         patch("osw.watcher.POLL_SECS", 0), \
         patch("osw.watcher.GATE_POLL_SECS", 0):
        source = await watcher._wait_ready()

    assert source == "ps_idle"
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8")
    assert '"action": "held"' in events
    assert '"reason": "mode_off"' in events


@pytest.mark.anyio
async def test_enforce_hold_false_cap_releases_on_static_frame(tmp_path):
    watcher = _claude_gate_setup(
        tmp_path, mode="enforce", providers=["claude"])
    reads = AsyncMock(return_value=_BOOTING)  # static normalized content
    ps = AsyncMock(return_value=_ps_result(
        "tab-a:leaf-b", "done", int(time.time() * 1000)))
    ask = AsyncMock(return_value=_BLOCKED_ANSWER)

    with patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_read", reads), \
         patch("osw.watcher.ask_ready", ask), \
         patch("osw.watcher.POLL_SECS", 0), \
         patch("osw.watcher.GATE_POLL_SECS", 0), \
         patch("osw.watcher.HOLD_FALSE_HOLD_CAP_MS", 30):
        source = await watcher._wait_ready()

    assert source == "ps_idle"
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8")
    assert '"reason": "false_hold_cap"' in events


@pytest.mark.anyio
async def test_send_receipt_accepted_false_fails_closed(tmp_path):
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "prompt": "do the task",
        "state": "assigned",
        "provider": "omp",
    })
    watcher = Watcher(tmp_path, "agent_001")
    sends = AsyncMock(return_value={"result": {"send": {"accepted": False}}})

    with patch("osw.watcher.terminal_send", sends):
        with pytest.raises(OrcaError) as excinfo:
            await watcher._send_terminal("do the task now")

    assert excinfo.value.code == "send_not_accepted"


@pytest.mark.anyio
async def test_phase0_ready_wait_is_bounded_for_handoff(tmp_path):
    watcher = _claude_gate_setup(tmp_path)
    reads = AsyncMock(return_value=_BOOTING)

    with patch("osw.watcher.terminal_read", reads), \
         patch("osw.watcher.COMPOSER_WAIT_MS", 0), \
         patch("osw.watcher.GATE_POLL_SECS", 0):
        await watcher._phase0_ready_wait()  # must return, never hang

    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8")
    assert '"reason": "wait_expired"' in events


# ---------------------------------------------------------------------------
# Hold rhythm branches (design §5.1.2): re-ask, forced release, budget
# gate, and the advisory notice line
# ---------------------------------------------------------------------------

def _frame_with(tail: str) -> dict:
    return {"result": {"terminal": {"source": "screen", "tail": [tail]}}}


@pytest.mark.anyio
async def test_hold_reask_clears_when_judgment_changes(tmp_path):
    watcher = _claude_gate_setup(
        tmp_path, mode="enforce", providers=["claude"])
    frames = iter([_BOOTING, _frame_with("booting deeper")])
    reads = AsyncMock(side_effect=lambda *a, **k: next(frames, _BOOTING))
    ps = AsyncMock(return_value=_ps_result(
        "tab-a:leaf-b", "done", int(time.time() * 1000)))
    ask = AsyncMock(side_effect=[
        _BLOCKED_ANSWER,
        {"choice": "ready_composer",
         "probabilities": {"blocked_startup": 0.1}, "confidence": 0.9},
    ])

    with patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_read", reads), \
         patch("osw.watcher.ask_ready", ask), \
         patch("osw.watcher.POLL_SECS", 0), \
         patch("osw.watcher.GATE_POLL_SECS", 0), \
         patch("osw.watcher.HOLD_MIN_REASK_MS", 0):
        source = await watcher._wait_ready()

    assert source == "ps_idle"
    assert ask.await_count == 2
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8")
    assert '"reason": "reask_clear"' in events


@pytest.mark.anyio
async def test_hold_forced_release_when_budget_expires(tmp_path):
    watcher = _claude_gate_setup(
        tmp_path, mode="enforce", providers=["claude"])
    counter = {"n": 0}

    def next_frame(*args, **kwargs):
        counter["n"] += 1
        letters = "abcdefghijklmnopqrstuvwxyz"
        suffix = letters[counter["n"] % 26] * 2
        return _frame_with(f"booting {suffix}")

    reads = AsyncMock(side_effect=next_frame)
    ps = AsyncMock(return_value=_ps_result(
        "tab-a:leaf-b", "done", int(time.time() * 1000)))
    ask = AsyncMock(return_value=_BLOCKED_ANSWER)

    with patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_read", reads), \
         patch("osw.watcher.ask_ready", ask), \
         patch("osw.watcher.POLL_SECS", 0), \
         patch("osw.watcher.GATE_POLL_SECS", 0), \
         patch("osw.watcher.HOLD_BUDGET_MS", 30), \
         patch("osw.watcher.HOLD_MIN_REASK_MS", 60_000):
        source = await watcher._wait_ready()

    assert source == "ps_idle"
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8")
    assert '"action": "forced"' in events
    assert '"reason": "forced"' in events


@pytest.mark.anyio
async def test_hold_not_started_without_ready_budget(tmp_path):
    watcher = _claude_gate_setup(
        tmp_path, mode="enforce", providers=["claude"])
    reads = AsyncMock(return_value=_BOOTING)
    ps = AsyncMock(return_value=_ps_result(
        "tab-a:leaf-b", "done", int(time.time() * 1000)))
    ask = AsyncMock(return_value=_BLOCKED_ANSWER)

    with patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_read", reads), \
         patch("osw.watcher.ask_ready", ask), \
         patch("osw.watcher.POLL_SECS", 0), \
         patch("osw.watcher.GATE_POLL_SECS", 0), \
         patch("osw.watcher.COMPOSER_WAIT_MS", 0), \
         patch("osw.watcher.READY_TIMEOUT_MS", 5_000):
        source = await watcher._wait_ready()

    assert source == "ps_idle"
    assert ask.await_count == 1
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8")
    assert '"action": "held"' not in events


@pytest.mark.anyio
async def test_advisory_notice_for_blocked_dialog(tmp_path):
    watcher = _claude_gate_setup(
        tmp_path, mode="enforce", providers=["claude"])
    reads = AsyncMock(return_value=_BOOTING)
    ps = AsyncMock(return_value=_ps_result(
        "tab-a:leaf-b", "done", int(time.time() * 1000)))
    asking = AsyncMock(return_value={
        "choice": "blocked_dialog",
        "probabilities": {"blocked_dialog": 0.93},
        "confidence": 0.9,
    })

    with patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_read", reads), \
         patch("osw.watcher.ask_ready", asking), \
         patch("osw.watcher.POLL_SECS", 0), \
         patch("osw.watcher.GATE_POLL_SECS", 0), \
         patch("osw.watcher.COMPOSER_WAIT_MS", 0):
        source = await watcher._wait_ready()

    assert source == "ps_idle"
    events = (state.logs_dir(tmp_path) / "events.jsonl").read_text(
        encoding="utf-8")
    assert '"event": "jev_notice"' in events
    assert '"class": "blocked_dialog"' in events
    assert '"action": "held"' not in events


# ---------------------------------------------------------------------------
# Supervised completion (Orca orchestration): the Dispatch lifecycle is the
# completion signal; the pane's output is never judged for silence.


def _supervised_agent(tmp_path, handoff_path: str = "", caller: str = "") -> None:
    state.write_agent(tmp_path, {
        "agent_id": "misc_001",
        "terminal": "term-sup",
        "state": "assigned",
        "provider": "omp",
        "prompt": "do the task",
        "launch_mode": "supervised",
        "dispatch_id": "ctx_1",
        "handoff_path": handoff_path,
        "caller_terminal": caller or None,
    })


def _show_result(
    dispatch_status: str,
    worker_state: str,
    worker_stage: str,
    outcome: str = "",
    last_observed: int | None = None,
    agent_wait: str | None = None,
) -> dict:
    projection = {
        "stage": {"dispatch": dispatch_status, "worker": worker_state},
        "evidence": {
            "lastObservedAt": (
                int(last_observed) if last_observed is not None
                else int(time.time() * 1000)
            )
        },
    }
    if outcome:
        projection["outcome"] = outcome
    return {"result": {
        "dispatch": {"status": dispatch_status},
        "worker": {
            "state": worker_state,
            "stage": worker_stage,
            "agentTerminalHandle": "term-sup",
        },
        "projection": projection,
        "observation": {"status": "live", "agentWait": agent_wait},
    }}


def _events(tmp_path) -> list[dict]:
    return [
        json.loads(line)
        for line in (state.logs_dir(tmp_path) / "events.jsonl")
        .read_text(encoding="utf-8").splitlines()
    ]


@pytest.mark.anyio
async def test_supervised_settlement_finalizes_with_handoff(tmp_path):
    state.init_state_dir(tmp_path)
    handoff = state.new_handoff_path(tmp_path, "misc_001")
    handoff.write_text("# handoff\n", encoding="utf-8")
    _supervised_agent(tmp_path, handoff_path=str(handoff))

    show = AsyncMock(side_effect=[
        _show_result("dispatched", "working", "running"),
        _show_result("completed", "succeeded", "settled",
                     outcome="succeeded"),
    ])
    retain = AsyncMock(return_value={})
    with patch("osw.watcher.orchestration_worker_show", show), \
         patch("osw.watcher.orchestration_worker_retain", retain), \
         patch("osw.watcher.SUPERVISED_POLL_SECS", 0.01):
        await Watcher(tmp_path, "misc_001").run()

    agent = state.read_agent(tmp_path, "misc_001")
    assert agent["state"] == "done"
    assert agent["completion_source"] == "orchestration"
    assert agent["orchestration_outcome"] == "succeeded"
    retain.assert_awaited_once_with("ctx_1")

    reports = list(state.reports_dir(tmp_path).glob("misc_001_*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["launch_mode"] == "supervised"
    assert payload["dispatch_id"] == "ctx_1"
    assert payload["orchestration_outcome"] == "succeeded"
    assert payload["handoff_path"] == str(handoff)


@pytest.mark.anyio
async def test_supervised_missing_handoff_sends_wrapup_then_accepts_file(
    tmp_path,
):
    state.init_state_dir(tmp_path)
    handoff = state.new_handoff_path(tmp_path, "misc_001")
    _supervised_agent(tmp_path, handoff_path=str(handoff))

    show = AsyncMock(return_value=_show_result(
        "completed", "succeeded", "settled", outcome="succeeded",
    ))

    async def send(handle, text, enter=True):
        assert "Task wrap-up" in text
        handoff.write_text("# late handoff\n", encoding="utf-8")
        return {}

    with patch("osw.watcher.orchestration_worker_show", show), \
         patch("osw.watcher.orchestration_worker_retain",
               AsyncMock(return_value={})), \
         patch("osw.watcher.terminal_send", AsyncMock(side_effect=send)), \
         patch("osw.watcher.SUPERVISED_POLL_SECS", 0.01), \
         patch("osw.watcher.HANDOFF_FALLBACK_POLL_SECS", 0.01), \
         patch("osw.watcher.HANDOFF_FALLBACK_TIMEOUT_SECS", 5.0):
        await Watcher(tmp_path, "misc_001").run()

    agent = state.read_agent(tmp_path, "misc_001")
    assert agent["state"] == "done"
    assert agent["completion_source"] == "orchestration"
    assert handoff.read_text(encoding="utf-8") == "# late handoff\n"


@pytest.mark.anyio
async def test_supervised_missing_handoff_stays_missing(tmp_path):
    state.init_state_dir(tmp_path)
    handoff = state.new_handoff_path(tmp_path, "misc_001")
    _supervised_agent(tmp_path, handoff_path=str(handoff))

    show = AsyncMock(return_value=_show_result(
        "completed", "succeeded", "settled", outcome="succeeded",
    ))

    with patch("osw.watcher.orchestration_worker_show", show), \
         patch("osw.watcher.orchestration_worker_retain",
               AsyncMock(return_value={})), \
         patch("osw.watcher.terminal_send", AsyncMock(return_value={})), \
         patch("osw.watcher.SUPERVISED_POLL_SECS", 0.01), \
         patch("osw.watcher.HANDOFF_FALLBACK_POLL_SECS", 0.01), \
         patch("osw.watcher.HANDOFF_FALLBACK_TIMEOUT_SECS", 0.05):
        await Watcher(tmp_path, "misc_001").run()

    agent = state.read_agent(tmp_path, "misc_001")
    assert agent["state"] == "done"
    assert agent["completion_source"] == "handoff_missing"
    assert agent["handoff_path"] == ""


@pytest.mark.anyio
async def test_supervised_failed_outcome_finalizes_error(tmp_path):
    state.init_state_dir(tmp_path)
    handoff = state.new_handoff_path(tmp_path, "misc_001")
    handoff.write_text("# handoff\n", encoding="utf-8")
    _supervised_agent(tmp_path, handoff_path=str(handoff))

    show = AsyncMock(return_value=_show_result(
        "failed", "failed", "settled", outcome="failed",
    ))
    with patch("osw.watcher.orchestration_worker_show", show), \
         patch("osw.watcher.orchestration_worker_retain",
               AsyncMock(return_value={})), \
         patch("osw.watcher.SUPERVISED_POLL_SECS", 0.01):
        await Watcher(tmp_path, "misc_001").run()

    agent = state.read_agent(tmp_path, "misc_001")
    assert agent["state"] == "error"
    assert agent["completion_source"] == "orchestration_failed"


@pytest.mark.anyio
async def test_supervised_wait_timeout_fails_closed(tmp_path):
    state.init_state_dir(tmp_path)
    _supervised_agent(tmp_path)

    show = AsyncMock(return_value=_show_result(
        "dispatched", "working", "running",
    ))
    with patch("osw.watcher.orchestration_worker_show", show), \
         patch("osw.watcher.SUPERVISED_POLL_SECS", 0.01), \
         patch("osw.watcher.HARD_TIMEOUT_SECS", 0.02):
        await Watcher(tmp_path, "misc_001").run()

    agent = state.read_agent(tmp_path, "misc_001")
    assert agent["state"] == "error"
    assert agent["completion_source"] == "timeout"


@pytest.mark.anyio
async def test_supervised_missing_dispatch_fails_closed(tmp_path):
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "misc_001",
        "terminal": "term-sup",
        "state": "assigned",
        "provider": "omp",
        "prompt": "do the task",
        "launch_mode": "supervised",
    })
    await Watcher(tmp_path, "misc_001").run()

    agent = state.read_agent(tmp_path, "misc_001")
    assert agent["state"] == "error"
    assert agent["completion_source"] == "supervised_missing_dispatch"


@pytest.mark.anyio
async def test_supervised_show_failure_is_transient(tmp_path):
    state.init_state_dir(tmp_path)
    handoff = state.new_handoff_path(tmp_path, "misc_001")
    handoff.write_text("# handoff\n", encoding="utf-8")
    _supervised_agent(tmp_path, handoff_path=str(handoff))

    show = AsyncMock(side_effect=[
        OrcaError("runtime busy", 1),
        _show_result("completed", "succeeded", "settled",
                     outcome="succeeded"),
    ])
    with patch("osw.watcher.orchestration_worker_show", show), \
         patch("osw.watcher.orchestration_worker_retain",
               AsyncMock(return_value={})), \
         patch("osw.watcher.SUPERVISED_POLL_SECS", 0.01):
        await Watcher(tmp_path, "misc_001").run()

    assert state.read_agent(tmp_path, "misc_001")["state"] == "done"


@pytest.mark.anyio
async def test_supervised_agent_wait_notifies_caller_once(tmp_path):
    state.init_state_dir(tmp_path)
    handoff = state.new_handoff_path(tmp_path, "misc_001")
    handoff.write_text("# handoff\n", encoding="utf-8")
    _supervised_agent(tmp_path, handoff_path=str(handoff), caller="caller-1")

    show = AsyncMock(side_effect=[
        _show_result("dispatched", "working", "running",
                     agent_wait="which database?"),
        _show_result("dispatched", "working", "running",
                     agent_wait="which database?"),
        _show_result("completed", "succeeded", "settled",
                     outcome="succeeded"),
    ])
    send = AsyncMock(return_value={})
    with patch("osw.watcher.orchestration_worker_show", show), \
         patch("osw.watcher.orchestration_worker_retain",
               AsyncMock(return_value={})), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.SUPERVISED_POLL_SECS", 0.01):
        await Watcher(tmp_path, "misc_001").run()

    waits = [
        call for call in send.await_args_list
        if "agent-waiting" in str(call.args[1])
    ]
    assert len(waits) == 1
    assert "which database?" in waits[0].args[1]


@pytest.mark.anyio
async def test_supervised_stall_notifies_caller(tmp_path):
    state.init_state_dir(tmp_path)
    handoff = state.new_handoff_path(tmp_path, "misc_001")
    handoff.write_text("# handoff\n", encoding="utf-8")
    _supervised_agent(tmp_path, handoff_path=str(handoff), caller="caller-1")

    stale = int(time.time() * 1000) - 20_000
    show = AsyncMock(side_effect=[
        _show_result("dispatched", "working", "running",
                     last_observed=stale),
        _show_result("completed", "succeeded", "settled",
                     outcome="succeeded"),
    ])
    send = AsyncMock(return_value={})
    with patch("osw.watcher.orchestration_worker_show", show), \
         patch("osw.watcher.orchestration_worker_retain",
               AsyncMock(return_value={})), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.STUCK_NOTIFY_SECS", 5), \
         patch("osw.watcher.SUPERVISED_POLL_SECS", 0.01):
        await Watcher(tmp_path, "misc_001").run()

    stalls = [
        call for call in send.await_args_list
        if "agent-stalled" in str(call.args[1])
    ]
    assert len(stalls) == 1
    assert "no-activity=" in stalls[0].args[1]


@pytest.mark.anyio
async def test_supervised_retain_failure_is_recorded(tmp_path):
    state.init_state_dir(tmp_path)
    handoff = state.new_handoff_path(tmp_path, "misc_001")
    handoff.write_text("# handoff\n", encoding="utf-8")
    _supervised_agent(tmp_path, handoff_path=str(handoff))

    show = AsyncMock(return_value=_show_result(
        "completed", "succeeded", "settled", outcome="succeeded",
    ))
    retain = AsyncMock(side_effect=OrcaError("retain refused", 1))
    with patch("osw.watcher.orchestration_worker_show", show), \
         patch("osw.watcher.orchestration_worker_retain", retain), \
         patch("osw.watcher.SUPERVISED_POLL_SECS", 0.01):
        await Watcher(tmp_path, "misc_001").run()

    assert state.read_agent(tmp_path, "misc_001")["state"] == "done"
    assert any(
        e["event"] == "supervised_retain_failed" for e in _events(tmp_path)
    )


@pytest.mark.anyio
async def test_supervised_wrapup_send_failure_still_finalizes(tmp_path):
    state.init_state_dir(tmp_path)
    handoff = state.new_handoff_path(tmp_path, "misc_001")
    _supervised_agent(tmp_path, handoff_path=str(handoff))

    show = AsyncMock(return_value=_show_result(
        "completed", "succeeded", "settled", outcome="succeeded",
    ))
    send = AsyncMock(side_effect=OrcaError("terminal lost", 1))
    with patch("osw.watcher.orchestration_worker_show", show), \
         patch("osw.watcher.orchestration_worker_retain",
               AsyncMock(return_value={})), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.SUPERVISED_POLL_SECS", 0.01), \
         patch("osw.watcher.HANDOFF_FALLBACK_POLL_SECS", 0.01), \
         patch("osw.watcher.HANDOFF_FALLBACK_TIMEOUT_SECS", 0.05):
        await Watcher(tmp_path, "misc_001").run()

    agent = state.read_agent(tmp_path, "misc_001")
    assert agent["state"] == "done"
    assert agent["completion_source"] == "handoff_missing"
    assert any(
        e["event"] == "supervised_wrapup_send_failed"
        for e in _events(tmp_path)
    )


# ---------------------------------------------------------------------------
# follow_turn: the watcher takes over the turn already running in the pane
# ---------------------------------------------------------------------------

_FOLLOW_PANE = "tab-a:leaf-b"
_FOLLOW_PROMPT = "refactor the parser module and run the tests"


def _follow_agent(tmp_path) -> None:
    state.init_state_dir(tmp_path)
    state.write_agent(tmp_path, {
        "agent_id": "claude_001",
        "terminal": "term-a",
        "provider": "claude",
        "pane_key": _FOLLOW_PANE,
        "caller_terminal": "caller-1",
        "prompt": "",
        "state": "enrolled",
        "follow_turn": True,
    })


def _follow_entry(ps_state: str, started: int, prompt: str = _FOLLOW_PROMPT):
    return [{"agents": [{
        "paneKey": _FOLLOW_PANE,
        "state": ps_state,
        "stateStartedAt": started,
        "prompt": prompt,
    }]}]


def _assert_follow_finished(tmp_path, send, source: str) -> dict:
    """Common follow invariants: nothing was typed into the worker, no
    handoff turn ran, the report is written and the caller notified."""
    agent = state.read_agent(tmp_path, "claude_001")
    assert agent["state"] == "done"
    assert agent["completion_source"] == source
    assert agent.get("follow_turn") is False
    assert not agent.get("handoff_path")
    assert [c.args[0] for c in send.await_args_list] == ["caller-1"]
    line = send.await_args_list[0].args[1]
    assert line.startswith("# [osw] task-finished agent=claude_001")
    assert "handoff=" not in line
    report = json.loads(Path(agent["report_file"]).read_text(encoding="utf-8"))
    assert report["completion_source"] == source
    assert report["follow_turn"] is True
    assert report["handoff_path"] == ""
    events = [e["event"] for e in _events(tmp_path)]
    assert "ready_wait_started" not in events
    assert "turn_sent" not in events
    return agent


@pytest.mark.anyio
async def test_follow_turn_already_done_finalizes_without_sending(tmp_path):
    """The followed turn finished before the watcher took over: report
    and notify at once, never wait for (or start) another turn."""
    _follow_agent(tmp_path)
    watcher = Watcher(tmp_path, "claude_001")
    watcher.deadline = time.monotonic() + 1

    ps = AsyncMock(return_value=_follow_entry(
        "done", int(time.time() * 1000) - 60_000))
    send = AsyncMock(return_value={})
    read = AsyncMock(side_effect=AssertionError("no screen/echo read"))
    wait = AsyncMock(side_effect=AssertionError("no ready wait"))

    with patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.terminal_read", read), \
         patch("osw.watcher.terminal_wait", wait), \
         patch("osw.watcher.POLL_SECS", 0):
        await watcher.run()

    _assert_follow_finished(tmp_path, send, "already_done")


@pytest.mark.anyio
async def test_follow_turn_working_completes_on_fresh_done(tmp_path):
    """Taken over mid-turn: no receipt check (nothing was sent), the
    foreign-done guard judges against the ps prompt seen at takeover."""
    _follow_agent(tmp_path)
    watcher = Watcher(tmp_path, "claude_001")
    watcher.deadline = time.monotonic() + 2

    now_ms = int(time.time() * 1000)
    ps_results = iter([
        _follow_entry("working", now_ms - 30_000),          # takeover
        _follow_entry("done", now_ms + 1_000, "Reply with exactly one word: OK"),
        _follow_entry("done", now_ms + 1_000, "Reply with exactly one word: OK"),
    ])
    ps = AsyncMock(side_effect=lambda: next(
        ps_results, _follow_entry("done", now_ms + 2_000)))
    show = AsyncMock(return_value={"result": {"terminal": {
        "lastOutputAt": now_ms - 10_000,
    }}})
    send = AsyncMock(return_value={})
    read = AsyncMock(side_effect=AssertionError("no screen/echo read"))

    with patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_show", show), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.terminal_read", read), \
         patch("osw.watcher.POLL_SECS", 0), \
         patch("osw.watcher.RECEIPT_TIMEOUT_MS", 0):
        await watcher.run()

    _assert_follow_finished(tmp_path, send, "ps_done")
    events = [e["event"] for e in _events(tmp_path)]
    assert "foreign_done_ignored" in events
    assert "prompt_receipt_missing" not in events


@pytest.mark.anyio
async def test_follow_turn_tracking_loss_uses_tui_idle(tmp_path):
    """Taken over while working, then the pane falls out of ps: the
    existing tracking-loss path (new output + tui-idle) ends the turn."""
    _follow_agent(tmp_path)
    watcher = Watcher(tmp_path, "claude_001")
    watcher.deadline = time.monotonic() + 2

    now_ms = int(time.time() * 1000)
    ps_results = iter([
        _follow_entry("working", now_ms - 30_000),          # takeover
        _follow_entry("working", now_ms - 30_000),
    ])
    ps = AsyncMock(side_effect=lambda: next(ps_results, [{"agents": []}]))
    output_times = iter([now_ms])                           # turn baseline

    async def show(*args, **kwargs):
        last = next(output_times, now_ms + 5_000)
        return {"result": {"terminal": {"lastOutputAt": last}}}
    send = AsyncMock(return_value={})
    wait = AsyncMock(return_value={})

    with patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_show", show), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.terminal_wait", wait), \
         patch("osw.watcher.POLL_SECS", 0), \
         patch("osw.watcher.FALLBACK_IDLE_STABLE_MS", 60_000):
        await watcher.run()

    _assert_follow_finished(tmp_path, send, "tui_idle")
    wait.assert_awaited_once_with("term-a", "tui-idle", timeout_ms=1000)


@pytest.mark.anyio
async def test_follow_turn_untracked_pane_uses_output_silence(tmp_path):
    """No ps entry for the pane: the output-silence fallback decides,
    and the first output after takeover counts (no prompt echo to
    absorb, since nothing was sent)."""
    _follow_agent(tmp_path)
    watcher = Watcher(tmp_path, "claude_001")
    watcher.deadline = time.monotonic() + 2

    ps = AsyncMock(return_value=[{"agents": []}])
    output_times = iter([1000])                             # turn baseline

    async def show(*args, **kwargs):
        return {"result": {"terminal": {
            "lastOutputAt": next(output_times, 2000),
        }}}
    send = AsyncMock(return_value={})
    wait = AsyncMock(return_value={})

    with patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_show", show), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.terminal_wait", wait), \
         patch("osw.watcher.POLL_SECS", 0), \
         patch("osw.watcher.FALLBACK_IDLE_STABLE_MS", 0):
        await watcher.run()

    _assert_follow_finished(tmp_path, send, "fallback_idle")
    wait.assert_not_awaited()


# ---------------------------------------------------------------------------
# Session id recording (plan §1.5)
# ---------------------------------------------------------------------------

def _turn_agent(tmp_path, **extra) -> Watcher:
    state.init_state_dir(tmp_path)
    record = {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "pane_key": "tab-a:leaf-b",
        "prompt": "do the task",
        "state": "assigned",
        "caller_terminal": "caller-1",
    }
    record.update(extra)
    state.write_agent(tmp_path, record)
    return Watcher(tmp_path, "agent_001")


def _turn_mocks():
    return (
        patch.object(Watcher, "_send_terminal", AsyncMock(return_value=True)),
        patch.object(Watcher, "_verify_prompt_echo", AsyncMock(return_value=None)),
        patch.object(Watcher, "_wait_turn_done", AsyncMock(return_value="ps_done")),
    )


def _raise(exc):
    def lookup(pane_key):
        raise exc
    return lookup


@pytest.mark.anyio
@pytest.mark.parametrize("phase", ["task", "handoff"])
async def test_each_turn_records_session_id(tmp_path, phase):
    watcher = _turn_agent(tmp_path)
    lookups = []

    def session_for_pane(pane_key):
        lookups.append(pane_key)
        return "sess-1"

    send, echo, wait = _turn_mocks()
    with send, echo, wait, \
         patch("osw.watcher.session_for_pane", session_for_pane):
        source = await watcher._run_turn("do the task", phase=phase)

    assert source == "ps_done"
    assert lookups == ["tab-a:leaf-b"]
    assert state.read_agent(tmp_path, "agent_001")["session_id"] == "sess-1"


@pytest.mark.anyio
@pytest.mark.parametrize("lookup", [
    lambda pane_key: "",
    _raise(RuntimeError("cache exploded")),
])
async def test_session_lookup_empty_or_failing_never_blocks_turn(tmp_path, lookup):
    watcher = _turn_agent(tmp_path, session_id="sess-old")

    send, echo, wait = _turn_mocks()
    with send, echo, wait, patch("osw.watcher.session_for_pane", lookup):
        source = await watcher._run_turn("do the task", phase="task")

    assert source == "ps_done"
    # an empty or failed lookup never clears a session id already known
    assert state.read_agent(tmp_path, "agent_001")["session_id"] == "sess-old"


@pytest.mark.anyio
async def test_follow_turn_records_session_id(tmp_path):
    _follow_agent(tmp_path)
    watcher = Watcher(tmp_path, "claude_001")
    watcher.deadline = time.monotonic() + 1
    ps = AsyncMock(return_value=_follow_entry(
        "done", int(time.time() * 1000) - 60_000))
    send = AsyncMock(return_value={})

    with patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.session_for_pane",
               lambda pane_key: "sess-f" if pane_key == _FOLLOW_PANE else ""):
        await watcher.run()

    agent = _assert_follow_finished(tmp_path, send, "already_done")
    assert agent["session_id"] == "sess-f"


@pytest.mark.anyio
async def test_follow_turn_session_lookup_failure_still_finishes(tmp_path):
    _follow_agent(tmp_path)
    watcher = Watcher(tmp_path, "claude_001")
    watcher.deadline = time.monotonic() + 2
    now_ms = int(time.time() * 1000)
    ps_results = iter([_follow_entry("working", now_ms - 30_000)])
    ps = AsyncMock(side_effect=lambda: next(
        ps_results, _follow_entry("done", now_ms + 2_000)))
    show = AsyncMock(return_value={"result": {"terminal": {
        "lastOutputAt": now_ms - 10_000,
    }}})
    send = AsyncMock(return_value={})

    with patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_show", show), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.POLL_SECS", 0), \
         patch("osw.watcher.session_for_pane", _raise(ValueError("bad cache"))):
        await watcher.run()

    agent = _assert_follow_finished(tmp_path, send, "ps_done")
    assert "session_id" not in agent
    assert "session_lookup_failed" in [e["event"] for e in _events(tmp_path)]


def _supervised_with_pane(tmp_path) -> None:
    state.init_state_dir(tmp_path)
    handoff = state.new_handoff_path(tmp_path, "misc_001")
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text("# handoff\n", encoding="utf-8")
    _supervised_agent(tmp_path, handoff_path=str(handoff))
    record = state.read_agent(tmp_path, "misc_001")
    record["pane_key"] = "tab-s:leaf-s"
    state.write_agent(tmp_path, record)


@pytest.mark.anyio
async def test_supervised_settlement_records_session_id(tmp_path):
    _supervised_with_pane(tmp_path)
    show = AsyncMock(return_value=_show_result(
        "completed", "succeeded", "settled", outcome="succeeded",
    ))
    with patch("osw.watcher.orchestration_worker_show", show), \
         patch("osw.watcher.orchestration_worker_retain",
               AsyncMock(return_value={})), \
         patch("osw.watcher.SUPERVISED_POLL_SECS", 0.01), \
         patch("osw.watcher.session_for_pane",
               lambda pane_key: "omp-sess" if pane_key == "tab-s:leaf-s" else ""):
        await Watcher(tmp_path, "misc_001").run()

    agent = state.read_agent(tmp_path, "misc_001")
    assert agent["state"] == "done"
    assert agent["session_id"] == "omp-sess"


@pytest.mark.anyio
async def test_supervised_session_lookup_failure_still_finalizes(tmp_path):
    _supervised_with_pane(tmp_path)
    show = AsyncMock(return_value=_show_result(
        "completed", "succeeded", "settled", outcome="succeeded",
    ))
    with patch("osw.watcher.orchestration_worker_show", show), \
         patch("osw.watcher.orchestration_worker_retain",
               AsyncMock(return_value={})), \
         patch("osw.watcher.SUPERVISED_POLL_SECS", 0.01), \
         patch("osw.watcher.session_for_pane", _raise(OSError("locked"))):
        await Watcher(tmp_path, "misc_001").run()

    agent = state.read_agent(tmp_path, "misc_001")
    assert agent["state"] == "done"
    assert agent["completion_source"] == "orchestration"
    assert "session_id" not in agent


# ---------------------------------------------------------------------------
# Guardian-aware notification (plan §1.6, watcher side)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_notify_resolves_guardian_agent_record_each_time(tmp_path):
    watcher = _turn_agent(tmp_path, guardian={"agent_id": "code_001"})
    state.write_agent(tmp_path, {
        "agent_id": "code_001", "terminal": "g-term-1",
        "pane_key": "tab-g:leaf-g", "state": "working",
    })
    send = AsyncMock(return_value={})

    with patch("osw.watcher.terminal_send", send):
        await watcher._notify("# [osw] first")
        # the guardian's own watcher rebinds its handle in between
        guardian = state.read_agent(tmp_path, "code_001")
        guardian["terminal"] = "g-term-2"
        state.write_agent(tmp_path, guardian)
        await watcher._notify("# [osw] second")

    assert [c.args for c in send.await_args_list] == [
        ("g-term-1", "# [osw] first"),
        ("g-term-2", "# [osw] second"),
    ]


@pytest.mark.anyio
async def test_notify_uses_inline_guardian_terminal(tmp_path):
    watcher = _turn_agent(
        tmp_path, guardian={"terminal": "g-term", "pane_key": "tab-g:leaf-g"})
    send = AsyncMock(return_value={})

    with patch("osw.watcher.terminal_send", send):
        await watcher._notify("# [osw] hello")

    send.assert_awaited_once_with("g-term", "# [osw] hello")


@pytest.mark.anyio
async def test_notify_without_guardian_uses_caller_terminal(tmp_path):
    watcher = _turn_agent(tmp_path, guardian=None)
    send = AsyncMock(return_value={})

    with patch("osw.watcher.terminal_send", send):
        await watcher._notify("# [osw] hello")

    send.assert_awaited_once_with("caller-1", "# [osw] hello")


@pytest.mark.anyio
async def test_notify_unresolvable_guardian_agent_falls_back_to_caller(tmp_path):
    watcher = _turn_agent(tmp_path, guardian={"agent_id": "gone_001"})
    send = AsyncMock(return_value={})

    with patch("osw.watcher.terminal_send", send):
        await watcher._notify("# [osw] hello")

    send.assert_awaited_once_with("caller-1", "# [osw] hello")
    assert "guardian_unresolved" in [e["event"] for e in _events(tmp_path)]


def _stale(*_args, **_kwargs):
    raise OrcaError("stale", 1, code="terminal_handle_stale")


@pytest.mark.anyio
async def test_notify_stale_guardian_handle_rebinds_by_pane_and_retries(tmp_path):
    watcher = _turn_agent(
        tmp_path, guardian={"terminal": "g-old", "pane_key": "tab-g:leaf-g"})
    send = AsyncMock(side_effect=[
        OrcaError("stale", 1, code="terminal_handle_stale"), {},
    ])
    terminals = AsyncMock(return_value=[
        {"handle": "other", "tabId": "tab-x", "leafId": "leaf-x"},
        {"handle": "g-new", "tabId": "tab-g", "leafId": "leaf-g"},
    ])

    with patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.terminal_list", terminals):
        await watcher._notify("# [osw] hello")

    assert [c.args for c in send.await_args_list] == [
        ("g-old", "# [osw] hello"), ("g-new", "# [osw] hello"),
    ]
    terminals.assert_awaited_once_with(f"path:{tmp_path}")
    # the inline guardian reference follows the pane to its new handle
    assert state.read_agent(tmp_path, "agent_001")["guardian"] == {
        "terminal": "g-new", "pane_key": "tab-g:leaf-g",
    }
    events = [e["event"] for e in _events(tmp_path)]
    assert "notify_target_rebound" in events
    assert "notify_failed" not in events


@pytest.mark.anyio
async def test_notify_stale_guardian_agent_handle_rebinds_without_writing_it(tmp_path):
    watcher = _turn_agent(tmp_path, guardian={"agent_id": "code_001"})
    state.write_agent(tmp_path, {
        "agent_id": "code_001", "terminal": "g-old",
        "pane_key": "tab-g:leaf-g", "state": "working",
    })
    send = AsyncMock(side_effect=[
        OrcaError("stale", 1, code="terminal_handle_stale"), {},
    ])
    terminals = AsyncMock(return_value=[
        {"handle": "g-new", "tabId": "tab-g", "leafId": "leaf-g"},
    ])

    with patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.terminal_list", terminals):
        await watcher._notify("# [osw] hello")

    assert send.await_args_list[-1].args == ("g-new", "# [osw] hello")
    # another agent's record belongs to its own watcher: never written here
    assert state.read_agent(tmp_path, "code_001")["terminal"] == "g-old"


@pytest.mark.anyio
async def test_notify_stale_handle_retries_only_once(tmp_path):
    watcher = _turn_agent(
        tmp_path, guardian={"terminal": "g-old", "pane_key": "tab-g:leaf-g"})
    send = AsyncMock(side_effect=_stale)
    terminals = AsyncMock(return_value=[
        {"handle": "g-new", "tabId": "tab-g", "leafId": "leaf-g"},
    ])

    with patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.terminal_list", terminals):
        await watcher._notify("# [osw] hello")

    assert send.await_count == 2
    assert "notify_failed" in [e["event"] for e in _events(tmp_path)]


@pytest.mark.anyio
async def test_notify_stale_handle_without_live_pane_reports_failure(tmp_path):
    watcher = _turn_agent(
        tmp_path, guardian={"terminal": "g-old", "pane_key": "tab-g:leaf-g"})
    send = AsyncMock(side_effect=_stale)
    terminals = AsyncMock(return_value=[
        {"handle": "other", "tabId": "tab-x", "leafId": "leaf-x"},
    ])

    with patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.terminal_list", terminals):
        await watcher._notify("# [osw] hello")

    send.assert_awaited_once()
    assert "notify_failed" in [e["event"] for e in _events(tmp_path)]


@pytest.mark.anyio
async def test_notify_non_stale_failure_does_not_rebind(tmp_path):
    watcher = _turn_agent(
        tmp_path, guardian={"terminal": "g-term", "pane_key": "tab-g:leaf-g"})
    send = AsyncMock(side_effect=OrcaError(
        "boom", 1, code="terminal_not_writable"))
    terminals = AsyncMock(return_value=[])

    with patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.terminal_list", terminals):
        await watcher._notify("# [osw] hello")

    send.assert_awaited_once()
    terminals.assert_not_awaited()
    assert "notify_failed" in [e["event"] for e in _events(tmp_path)]


# ---------------------------------------------------------------------------
# Resume turn (plan §1.7, watcher side)
# ---------------------------------------------------------------------------

def _fake_turns(texts: list, on_task=None):
    """A _run_turn stand-in: records what was sent, writes the handoff
    file on the handoff phase, and runs `on_task` on the task phase."""
    async def fake_run_turn(self, text, phase, **_):
        texts.append((phase, text))
        if phase == "task" and on_task is not None:
            on_task()
        if phase == "handoff":
            Path(self.agent["handoff_path"]).write_text(
                "# handoff\n", encoding="utf-8")
        return "ps_done"
    return fake_run_turn


def test_resume_template_is_the_fixed_continue_line():
    from osw.watcher import RESUME_TEMPLATE

    assert RESUME_TEMPLATE == "continue"


@pytest.mark.anyio
async def test_resume_turn_sends_continue_then_runs_handoff(tmp_path):
    watcher = _turn_agent(tmp_path, prompt="original task", resume_turn=True)
    texts: list = []

    with patch.object(Watcher, "_wait_ready", AsyncMock(return_value="ps_idle")), \
         patch.object(Watcher, "_phase0_ready_wait", AsyncMock()), \
         patch.object(Watcher, "_run_turn", _fake_turns(texts)), \
         patch("osw.watcher.terminal_send", AsyncMock(return_value={})), \
         patch("osw.watcher.worktree_ps", AsyncMock(return_value=[])):
        await watcher.run()

    assert texts[0] == ("task", "continue")
    assert texts[1][0] == "handoff"
    assert texts[1][1].startswith("Task wrap-up:")
    agent = state.read_agent(tmp_path, "agent_001")
    assert agent["state"] == "done"
    assert agent["completion_source"] == "ps_done"
    assert agent["resume_turn"] is False
    assert agent["prompt"] == "original task"
    report = json.loads(Path(agent["report_file"]).read_text(encoding="utf-8"))
    assert report["resume_turn"] is True


# ---------------------------------------------------------------------------
# Stop requests (plan §1.8, watcher side)
# ---------------------------------------------------------------------------

def _ps_with(ps_state: str, started: int, **fields) -> list[dict]:
    result = _ps_result("tab-a:leaf-b", ps_state, started)
    result[0]["agents"][0].update(fields)
    return result


def _quiet_show(now_ms: int):
    return AsyncMock(return_value={"result": {"terminal": {
        "lastOutputAt": now_ms - 10_000,
    }}})


@pytest.mark.anyio
async def test_stop_ends_turn_on_ps_interrupted_flag(tmp_path):
    watcher = _turn_agent(tmp_path)
    watcher.deadline = time.monotonic() + 1
    state.write_stop_request(tmp_path, "agent_001")
    now_ms = int(time.time() * 1000)
    ps = AsyncMock(return_value=_ps_with("working", now_ms, interrupted=True))
    # output still flowing: the interrupted flag alone settles the turn
    show = AsyncMock(return_value={"result": {"terminal": {
        "lastOutputAt": int(time.time() * 1000) + 60_000,
    }}})

    with patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_show", show), \
         patch("osw.watcher.POLL_SECS", 0):
        result = await watcher._wait_turn_done(
            time.time() * 1000, sent_text="do the task")

    assert result == "interrupted"
    agent = state.read_agent(tmp_path, "agent_001")
    assert agent["stop_requested_at"] == state.read_stop_request(
        tmp_path, "agent_001")


@pytest.mark.anyio
async def test_stop_ends_turn_when_ps_keeps_stale_working_but_pane_is_quiet(tmp_path):
    """claude/kimi: ps keeps the pre-interrupt `working` snapshot."""
    watcher = _turn_agent(tmp_path)
    watcher.deadline = time.monotonic() + 1
    state.write_stop_request(tmp_path, "agent_001")
    now_ms = int(time.time() * 1000)

    with patch("osw.watcher.worktree_ps",
               AsyncMock(return_value=_ps_with("working", now_ms - 60_000))), \
         patch("osw.watcher.terminal_show", _quiet_show(now_ms)), \
         patch("osw.watcher.POLL_SECS", 0):
        result = await watcher._wait_turn_done(
            time.time() * 1000, sent_text="do the task")

    assert result == "interrupted"


@pytest.mark.anyio
async def test_stop_waits_while_pane_keeps_producing_output(tmp_path):
    watcher = _turn_agent(tmp_path)
    watcher.deadline = time.monotonic() + 0.05
    state.write_stop_request(tmp_path, "agent_001")

    async def show(*args, **kwargs):
        return {"result": {"terminal": {"lastOutputAt": int(time.time() * 1000)}}}

    with patch("osw.watcher.worktree_ps", AsyncMock(return_value=_ps_with(
            "working", int(time.time() * 1000) - 60_000))), \
         patch("osw.watcher.terminal_show", show), \
         patch("osw.watcher.POLL_SECS", 0):
        result = await watcher._wait_turn_done(
            time.time() * 1000, sent_text="do the task")

    assert result == "timeout"


@pytest.mark.anyio
async def test_stop_turns_a_natural_done_into_interrupted(tmp_path):
    """pi ignores the interrupt and finishes normally: still a stopped turn."""
    watcher = _turn_agent(tmp_path)
    watcher.deadline = time.monotonic() + 1
    state.write_stop_request(tmp_path, "agent_001")
    now_ms = int(time.time() * 1000)

    with patch("osw.watcher.worktree_ps", AsyncMock(return_value=_ps_with(
            "done", now_ms, interrupted=False, prompt="do the task"))), \
         patch("osw.watcher.terminal_show", _quiet_show(now_ms)), \
         patch("osw.watcher.POLL_SECS", 0):
        result = await watcher._wait_turn_done(
            time.time() * 1000, sent_text="do the task")

    assert result == "interrupted"


@pytest.mark.anyio
async def test_stop_on_untracked_pane_settles_on_silence(tmp_path):
    watcher = _turn_agent(tmp_path)
    watcher.deadline = time.monotonic() + 1
    state.write_stop_request(tmp_path, "agent_001")
    now_ms = int(time.time() * 1000)

    with patch("osw.watcher.worktree_ps", AsyncMock(return_value=[{"agents": []}])), \
         patch("osw.watcher.terminal_show", _quiet_show(now_ms)), \
         patch("osw.watcher.POLL_SECS", 0):
        result = await watcher._wait_turn_done(
            time.time() * 1000, sent_text="do the task")

    assert result == "interrupted"


@pytest.mark.anyio
async def test_stop_mid_task_finalizes_error_skips_handoff_and_notifies_guardian(tmp_path):
    watcher = _turn_agent(
        tmp_path, guardian={"terminal": "g-term", "pane_key": "tab-g:leaf-g"})
    watcher.deadline = time.monotonic() + 2
    now_ms = int(time.time() * 1000)
    calls = 0

    async def ps():
        nonlocal calls
        calls += 1
        if calls == 3:
            # the stop request lands while the task turn is working
            state.write_stop_request(tmp_path, "agent_001")
        if calls < 4:
            return _ps_with("working", now_ms)
        return _ps_with("done", now_ms + 1_000, interrupted=True)
    sent = AsyncMock(return_value=True)
    notify = AsyncMock(return_value={})

    with patch.object(Watcher, "_wait_ready", AsyncMock(return_value="ps_idle")), \
         patch.object(Watcher, "_send_terminal", sent), \
         patch.object(Watcher, "_verify_prompt_echo", AsyncMock(return_value=None)), \
         patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_show", _quiet_show(now_ms)), \
         patch("osw.watcher.terminal_send", notify), \
         patch("osw.watcher.POLL_SECS", 0):
        await watcher.run()

    sent.assert_awaited_once()   # the task prompt only: no handoff turn
    agent = state.read_agent(tmp_path, "agent_001")
    assert agent["state"] == "error"
    assert agent["completion_source"] == "interrupted"
    assert not agent.get("handoff_path")
    assert agent["stop_requested_at"]
    assert not state.stop_request_path(tmp_path, "agent_001").exists()
    target, line = notify.await_args_list[-1].args
    assert target == "g-term"
    assert line.startswith("# [osw] task-error agent=agent_001")
    assert "reason=interrupted" in line
    report = json.loads(Path(agent["report_file"]).read_text(encoding="utf-8"))
    assert report["stop_requested_at"] == agent["stop_requested_at"]


@pytest.mark.anyio
async def test_stop_during_ready_wait_never_sends_the_prompt(tmp_path):
    watcher = _turn_agent(tmp_path)
    watcher.deadline = time.monotonic() + 2
    state.write_stop_request(tmp_path, "agent_001")
    sent = AsyncMock(return_value=True)

    with patch.object(Watcher, "_send_terminal", sent), \
         patch("osw.watcher.worktree_ps", AsyncMock(return_value=_ps_with(
             "working", int(time.time() * 1000)))), \
         patch("osw.watcher.terminal_send", AsyncMock(return_value={})), \
         patch("osw.watcher.POLL_SECS", 0):
        await watcher.run()

    sent.assert_not_awaited()
    agent = state.read_agent(tmp_path, "agent_001")
    assert agent["state"] == "error"
    assert agent["completion_source"] == "interrupted"
    assert not state.stop_request_path(tmp_path, "agent_001").exists()


@pytest.mark.anyio
async def test_stop_after_task_turn_skips_handoff(tmp_path):
    watcher = _turn_agent(tmp_path)
    texts: list = []

    with patch.object(Watcher, "_wait_ready", AsyncMock(return_value="ps_idle")), \
         patch.object(Watcher, "_phase0_ready_wait", AsyncMock()), \
         patch.object(Watcher, "_run_turn", _fake_turns(
             texts, on_task=lambda: state.write_stop_request(
                 tmp_path, "agent_001"))), \
         patch("osw.watcher.terminal_send", AsyncMock(return_value={})), \
         patch("osw.watcher.worktree_ps", AsyncMock(return_value=[])):
        await watcher.run()

    assert [phase for phase, _ in texts] == ["task"]
    agent = state.read_agent(tmp_path, "agent_001")
    assert agent["state"] == "error"
    assert agent["completion_source"] == "interrupted"
    assert not agent.get("handoff_path")


@pytest.mark.anyio
async def test_stale_stop_request_from_an_earlier_run_is_discarded(tmp_path):
    watcher = _turn_agent(tmp_path, created_at="2026-10-11T22:00:00+00:00")
    state.write_stop_request(tmp_path, "agent_001", "2026-10-11T21:00:00+00:00")
    texts: list = []

    with patch.object(Watcher, "_wait_ready", AsyncMock(return_value="ps_idle")), \
         patch.object(Watcher, "_phase0_ready_wait", AsyncMock()), \
         patch.object(Watcher, "_run_turn", _fake_turns(texts)), \
         patch("osw.watcher.terminal_send", AsyncMock(return_value={})), \
         patch("osw.watcher.worktree_ps", AsyncMock(return_value=[])):
        await watcher.run()

    assert [phase for phase, _ in texts] == ["task", "handoff"]
    agent = state.read_agent(tmp_path, "agent_001")
    assert agent["state"] == "done"
    assert "stop_requested_at" not in agent
    assert not state.stop_request_path(tmp_path, "agent_001").exists()
    assert "stale_stop_request_discarded" in [
        e["event"] for e in _events(tmp_path)]


@pytest.mark.anyio
async def test_stop_on_followed_turn_finalizes_interrupted(tmp_path):
    _follow_agent(tmp_path)
    watcher = Watcher(tmp_path, "claude_001")
    watcher.deadline = time.monotonic() + 2
    state.write_stop_request(tmp_path, "claude_001")
    now_ms = int(time.time() * 1000)
    send = AsyncMock(return_value={})

    with patch("osw.watcher.worktree_ps", AsyncMock(return_value=_follow_entry(
            "working", now_ms - 30_000))), \
         patch("osw.watcher.terminal_show", _quiet_show(now_ms)), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.POLL_SECS", 0):
        await watcher.run()

    agent = state.read_agent(tmp_path, "claude_001")
    assert agent["state"] == "error"
    assert agent["completion_source"] == "interrupted"
    assert [c.args[0] for c in send.await_args_list] == ["caller-1"]


@pytest.mark.anyio
async def test_stop_on_supervised_worker_skips_wrapup_fallback(tmp_path):
    state.init_state_dir(tmp_path)
    _supervised_agent(tmp_path)   # no handoff file: would trigger wrap-up
    state.write_stop_request(tmp_path, "misc_001")
    show = AsyncMock(return_value=_show_result(
        "completed", "succeeded", "settled", outcome="succeeded",
    ))
    retain = AsyncMock(return_value={})
    send = AsyncMock(return_value={})

    with patch("osw.watcher.orchestration_worker_show", show), \
         patch("osw.watcher.orchestration_worker_retain", retain), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.SUPERVISED_POLL_SECS", 0.01), \
         patch("osw.watcher.HANDOFF_FALLBACK_POLL_SECS", 0.01), \
         patch("osw.watcher.HANDOFF_FALLBACK_TIMEOUT_SECS", 0.05):
        await Watcher(tmp_path, "misc_001").run()

    agent = state.read_agent(tmp_path, "misc_001")
    assert agent["state"] == "error"
    assert agent["completion_source"] == "interrupted"
    retain.assert_awaited_once()
    send.assert_not_awaited()
    assert not state.stop_request_path(tmp_path, "misc_001").exists()


# ---------------------------------------------------------------------------
# Worker asks relayed to the guardian (plan 2.3)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _quiet_orchestration_inbox():
    """Supervised polling also reads the orchestration inbox; tests that
    do not care about worker asks get an empty one, never a live call."""
    with patch("osw.watcher.orchestration_inbox", AsyncMock(return_value=[])):
        yield


def _ask_row(msg_id, msg_type="question", body="", seq=1, **extra) -> dict:
    row = {
        "id": msg_id, "type": msg_type, "from_handle": "dispatch:ctx_1",
        "to_handle": "run:run_1", "subject": "Question", "body": body,
        "thread_id": msg_id if msg_type == "question" else None,
        "payload": json.dumps({"taskId": "task_1", "dispatchId": "ctx_1"}),
        "sequence": seq, "sender_pane_key": None,
    }
    row.update(extra)
    return row


def _guarded_supervised_agent(tmp_path, **extra) -> None:
    handoff = state.new_handoff_path(tmp_path, "misc_001")
    handoff.write_text("# handoff\n", encoding="utf-8")
    state.write_agent(tmp_path, {
        "agent_id": "misc_001",
        "terminal": "term-sup",
        "pane_key": "tab:w",
        "state": "assigned",
        "provider": "omp",
        "prompt": "do the task",
        "launch_mode": "supervised",
        "dispatch_id": "ctx_1",
        "handoff_path": str(handoff),
        "caller_terminal": "caller-1",
        "guardian": {"terminal": "term-g", "pane_key": "tab:g"},
        **extra,
    })


def _settling_show(polls: int = 2) -> AsyncMock:
    return AsyncMock(side_effect=[
        *[_show_result("dispatched", "working", "running") for _ in range(polls)],
        _show_result("completed", "succeeded", "settled", outcome="succeeded"),
    ])


async def _run_supervised_with_inbox(tmp_path, inbox: AsyncMock, polls: int = 2):
    send = AsyncMock(return_value={})
    with patch("osw.watcher.orchestration_worker_show", _settling_show(polls)), \
         patch("osw.watcher.orchestration_worker_retain", AsyncMock(return_value={})), \
         patch("osw.watcher.orchestration_inbox", inbox), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.SUPERVISED_POLL_SECS", 0.01):
        await Watcher(tmp_path, "misc_001").run()
    return send


def _ask_lines(send: AsyncMock) -> list[tuple[str, str]]:
    return [
        (call.args[0], call.args[1]) for call in send.await_args_list
        if str(call.args[1]).startswith("# [osw] ask ")
    ]


@pytest.mark.anyio
async def test_supervised_worker_ask_notifies_guardian_once(tmp_path):
    state.init_state_dir(tmp_path)
    _guarded_supervised_agent(tmp_path)
    inbox = AsyncMock(return_value=[
        _ask_row("msg_q", body="Which branch\nshould I use?", seq=3),
    ])

    send = await _run_supervised_with_inbox(tmp_path, inbox)

    assert _ask_lines(send) == [(
        "term-g",
        '# [osw] ask agent=misc_001 message=msg_q '
        'reply="orca orchestration reply --id msg_q --body <text>" '
        'text=Which branch should I use?',
    )]
    # Read unfiltered: worker asks are addressed to the Run mailbox, so a
    # recipient filter on the worker's own handle would never see them.
    assert inbox.await_count >= 2
    assert all(call.kwargs.get("terminal", "") == "" for call in inbox.await_args_list)
    agent = state.read_agent(tmp_path, "misc_001")
    assert agent["notified_asks"] == ["msg_q"]
    assert agent["state"] == "done"
    events = [e["event"] for e in _events(tmp_path)]
    assert events.count("worker_ask_notified") == 1


@pytest.mark.anyio
async def test_supervised_worker_ask_already_notified_is_not_repeated(tmp_path):
    """notified_asks survives a watcher restart (e.g. `guard`)."""
    state.init_state_dir(tmp_path)
    _guarded_supervised_agent(tmp_path, notified_asks=["msg_q"])
    inbox = AsyncMock(return_value=[_ask_row("msg_q", body="Which branch?")])

    send = await _run_supervised_with_inbox(tmp_path, inbox)

    assert _ask_lines(send) == []
    assert state.read_agent(tmp_path, "misc_001")["notified_asks"] == ["msg_q"]


@pytest.mark.anyio
async def test_supervised_worker_asks_and_escalations_each_notify_in_order(tmp_path):
    state.init_state_dir(tmp_path)
    _guarded_supervised_agent(tmp_path)
    long_body = "x" * 400
    rows = [
        _ask_row("msg_esc", "escalation", body=long_body, seq=5,
                 from_handle="term-sup", subject="Blocked"),
        _ask_row("msg_q", body="", seq=4, subject="Need the API key name"),
        _ask_row("msg_other", body="not ours", seq=3, from_handle="dispatch:ctx_9",
                 payload=json.dumps({"dispatchId": "ctx_9"})),
    ]
    inbox = AsyncMock(side_effect=[rows[2:], rows, rows])

    send = await _run_supervised_with_inbox(tmp_path, inbox, polls=3)

    lines = [line for _, line in _ask_lines(send)]
    assert [line.split()[4] for line in lines] == ["message=msg_q", "message=msg_esc"]
    assert lines[0].endswith("text=Need the API key name")
    assert "\n" not in lines[1]
    assert len(lines[1].split("text=", 1)[1]) <= 160
    assert lines[1].endswith("...")
    assert state.read_agent(tmp_path, "misc_001")["notified_asks"] == ["msg_q", "msg_esc"]


@pytest.mark.anyio
async def test_supervised_worker_ask_without_guardian_notifies_caller(tmp_path):
    state.init_state_dir(tmp_path)
    _guarded_supervised_agent(tmp_path, guardian=None)
    inbox = AsyncMock(return_value=[_ask_row("msg_q", body="Which branch?")])

    send = await _run_supervised_with_inbox(tmp_path, inbox)

    assert [target for target, _ in _ask_lines(send)] == ["caller-1"]


@pytest.mark.anyio
async def test_supervised_inbox_failure_is_transient(tmp_path):
    state.init_state_dir(tmp_path)
    _guarded_supervised_agent(tmp_path)
    inbox = AsyncMock(side_effect=OrcaError("inbox down", 1))

    send = await _run_supervised_with_inbox(tmp_path, inbox)

    assert _ask_lines(send) == []
    agent = state.read_agent(tmp_path, "misc_001")
    assert agent["state"] == "done"
    assert "notified_asks" not in agent


# ---------------------------------------------------------------------------
# Blocking screens (plan §2.2, watcher integration)
# ---------------------------------------------------------------------------

from osw import blockers as blockers_mod

_UPDATE_FRAME = {"result": {"terminal": {"source": "screen", "tail": [
    "✨ Update available! 0.153.4 -> 0.162.1", "1. Update now", "2. Skip",
]}}}
_UPDATE_WAIT = {"agentWait": {"reason": "agent-update-prompt"}}
_DIALOG_ANSWER = {
    "choice": "blocked_dialog",
    "probabilities": {"blocked_dialog": 0.92},
    "confidence": 0.9,
}


def _update_kind_answer(kind="update_prompt", p=0.95):
    return {"choice": kind, "probabilities": {kind: p}, "confidence": 0.9}


def _blocked_error(reason="agent-update-prompt"):
    return OrcaError(
        "agent prompt blocked", 1, code="agent_prompt_blocked",
        payload={"ok": False, "error": {
            "code": "agent_prompt_blocked",
            "data": {"agentWait": {"reason": reason}},
        }},
    )


@pytest.fixture
def blocker_env(tmp_path, monkeypatch):
    """User-level lock dir under tmp; Jev mode set per test."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.delenv("OSW_JEV_MODE", raising=False)

    def set_mode(mode):
        if mode == "off":
            monkeypatch.delenv("OSW_JEV_MODE", raising=False)
        else:
            monkeypatch.setenv("OSW_JEV_MODE", mode)
    return set_mode


def _blocked_lines(send):
    return [c.args for c in send.await_args_list
            if str(c.args[1]).startswith("# [osw] blocked")]


def _event_names(tmp_path):
    return [e["event"] for e in _events(tmp_path)]


@pytest.mark.anyio
async def test_blocker_enforce_update_prompt_locks_and_sends_dialog_enter(
        tmp_path, blocker_env):
    blocker_env("enforce")
    watcher = _turn_agent(tmp_path, provider="codex")
    enter = AsyncMock(return_value={})
    ask = AsyncMock()

    with patch("osw.watcher.terminal_send_dialog_enter", enter), \
         patch("osw.watcher.ask_blocker", ask):
        outcome = await watcher._check_blocker(
            _UPDATE_WAIT, None, _UPDATE_FRAME, point="send")

    assert outcome == "acted"
    enter.assert_awaited_once_with("term-a")
    ask.assert_not_awaited()            # Orca already named the dialog
    lock = blockers_mod.update_lock_path("codex")
    assert json.loads(lock.read_text(encoding="utf-8"))["owner"] == "agent_001"
    names = _event_names(tmp_path)
    assert names[-3:] == ["blocker_detected", "blocker_lock_acquired", "blocker_action"]
    action = _events(tmp_path)[-1]["data"]
    assert action["kind"] == "update_prompt"
    assert action["keys"] == ["enter"]

    watcher._release_update_lock("ready_restored")
    assert not lock.exists()
    assert _event_names(tmp_path)[-1] == "blocker_lock_released"


@pytest.mark.anyio
async def test_blocker_lock_busy_only_notifies_once_and_keeps_waiting(
        tmp_path, blocker_env):
    blocker_env("enforce")
    watcher = _turn_agent(tmp_path, provider="codex")
    assert blockers_mod.acquire_update_lock("codex", "other_001")
    enter = AsyncMock(return_value={})
    send = AsyncMock(return_value={})

    with patch("osw.watcher.terminal_send_dialog_enter", enter), \
         patch("osw.watcher.terminal_send", send):
        first = await watcher._check_blocker(_UPDATE_WAIT, None, _UPDATE_FRAME)
        second = await watcher._check_blocker(_UPDATE_WAIT, None, _UPDATE_FRAME)

    assert first == second == "waiting"
    enter.assert_not_awaited()
    lines = _blocked_lines(send)
    assert len(lines) == 1
    assert lines[0][0] == "caller-1"
    assert "kind=update_prompt" in lines[0][1]
    assert "blocker_lock_busy" in _event_names(tmp_path)


@pytest.mark.anyio
async def test_blocker_shadow_mode_records_keys_and_notifies_once(tmp_path, blocker_env):
    """Plan §3.2: shadow records the planned keys and, like notify, tells
    the guardian once — it never presses anything."""
    blocker_env("shadow")
    watcher = _turn_agent(
        tmp_path, provider="codex",
        guardian={"terminal": "g-term", "pane_key": "tab-g:leaf-g"})
    enter = AsyncMock(return_value={})
    send = AsyncMock(return_value={})

    with patch("osw.watcher.terminal_send_dialog_enter", enter), \
         patch("osw.watcher.terminal_send", send):
        outcome = await watcher._check_blocker(_UPDATE_WAIT, None, _UPDATE_FRAME)
        await watcher._check_blocker(_UPDATE_WAIT, None, _UPDATE_FRAME)

    assert outcome == "shadow"
    enter.assert_not_awaited()
    lines = _blocked_lines(send)
    assert len(lines) == 1
    target, line = lines[0]
    assert target == "g-term"
    assert line.startswith("# [osw] blocked agent=agent_001 kind=update_prompt")
    assert "Update available" in line
    shadow = [e for e in _events(tmp_path) if e["event"] == "blocker_shadow_action"]
    assert len(shadow) == 1
    assert shadow[0]["data"]["keys"] == ["enter"]
    assert not blockers_mod.update_lock_path("codex").exists()


@pytest.mark.anyio
async def test_blocker_off_mode_notifies_guardian_once_with_screen_excerpt(
        tmp_path, blocker_env):
    watcher = _turn_agent(
        tmp_path, provider="codex",
        guardian={"terminal": "g-term", "pane_key": "tab-g:leaf-g"})
    enter = AsyncMock(return_value={})
    ask = AsyncMock()
    send = AsyncMock(return_value={})

    with patch("osw.watcher.terminal_send_dialog_enter", enter), \
         patch("osw.watcher.ask_blocker", ask), \
         patch("osw.watcher.terminal_send", send):
        outcome = await watcher._check_blocker(_UPDATE_WAIT, None, _UPDATE_FRAME)
        await watcher._check_blocker(_UPDATE_WAIT, None, _UPDATE_FRAME)

    assert outcome == "notified"
    enter.assert_not_awaited()
    ask.assert_not_awaited()            # off: no Jev calls at all
    lines = _blocked_lines(send)
    assert len(lines) == 1
    target, line = lines[0]
    assert target == "g-term"
    assert line.startswith("# [osw] blocked agent=agent_001 kind=update_prompt")
    assert "screen=" in line and "Update available" in line
    assert "\n" not in line


@pytest.mark.anyio
async def test_blocker_kind_comes_from_jev_when_orca_names_nothing(tmp_path, blocker_env):
    blocker_env("enforce")
    watcher = _turn_agent(tmp_path, provider="claude")
    ask = AsyncMock(return_value=_update_kind_answer("trust_folder", 0.9))
    send = AsyncMock(return_value={})
    frame = {"result": {"terminal": {"source": "screen", "tail": [
        "Do you trust the files in this folder?", "1. Yes, proceed", "2. No, exit"]}}}

    with patch("osw.watcher.ask_blocker", ask), \
         patch("osw.watcher.terminal_send", send):
        outcome = await watcher._check_blocker(None, _DIALOG_ANSWER, frame)

    assert outcome == "notified"
    ask.assert_awaited_once()
    assert "kind=trust_folder" in _blocked_lines(send)[0][1]


@pytest.mark.anyio
async def test_blocker_still_blocked_after_acting_stops_and_notifies(tmp_path, blocker_env):
    blocker_env("enforce")
    watcher = _turn_agent(tmp_path, provider="codex")
    enter = AsyncMock(return_value={})
    send = AsyncMock(return_value={})

    with patch("osw.watcher.terminal_send_dialog_enter", enter), \
         patch("osw.watcher.terminal_send", send):
        first = await watcher._check_blocker(_UPDATE_WAIT, None, _UPDATE_FRAME)
        second = await watcher._check_blocker(_UPDATE_WAIT, None, _UPDATE_FRAME)
        third = await watcher._check_blocker(_UPDATE_WAIT, None, _UPDATE_FRAME)

    assert (first, second, third) == ("acted", "stopped", "notified")
    enter.assert_awaited_once()
    assert "blocker_still_blocked" in _event_names(tmp_path)
    lines = _blocked_lines(send)
    assert len(lines) == 1
    assert "stopped" in lines[0][1]
    assert not blockers_mod.update_lock_path("codex").exists()


@pytest.mark.anyio
async def test_blocker_never_acts_on_a_delete_screen(tmp_path, blocker_env):
    blocker_env("enforce")
    watcher = _turn_agent(tmp_path, provider="codex")
    enter = AsyncMock(return_value={})
    frame = {"result": {"terminal": {"source": "screen", "tail": [
        "Delete all local sessions?", "1. Yes", "2. No"]}}}

    with patch("osw.watcher.terminal_send_dialog_enter", enter), \
         patch("osw.watcher.terminal_send", AsyncMock(return_value={})):
        outcome = await watcher._check_blocker(_UPDATE_WAIT, None, frame)

    assert outcome == "notified"
    enter.assert_not_awaited()


@pytest.mark.anyio
async def test_blocker_without_a_signal_does_nothing(tmp_path, blocker_env):
    blocker_env("enforce")
    watcher = _turn_agent(tmp_path, provider="codex")

    outcome = await watcher._check_blocker(
        {}, {"probabilities": {"ready_composer": 0.99}}, _UPDATE_FRAME)

    assert outcome == "none"
    events_file = state.logs_dir(tmp_path) / "events.jsonl"
    assert not events_file.exists() \
        or not any(n.startswith("blocker_") for n in _event_names(tmp_path))


@pytest.mark.anyio
@pytest.mark.parametrize("act,outcome,notified", [
    ("defer", "deferred", 0), ("notify", "notified", 1),
])
async def test_blocker_where_keys_must_not_be_sent(tmp_path, blocker_env, act, outcome, notified):
    blocker_env("enforce")
    watcher = _turn_agent(tmp_path, provider="codex")
    enter = AsyncMock(return_value={})
    send = AsyncMock(return_value={})

    with patch("osw.watcher.terminal_send_dialog_enter", enter), \
         patch("osw.watcher.terminal_send", send):
        result = await watcher._check_blocker(
            _UPDATE_WAIT, None, _UPDATE_FRAME, act=act)

    assert result == outcome
    enter.assert_not_awaited()
    assert len(_blocked_lines(send)) == notified
    assert "blocker_action_deferred" in _event_names(tmp_path)
    assert not blockers_mod.update_lock_path("codex").exists()


def _blocked_run_patches(sends, *, frame=_UPDATE_FRAME, enter=None, notify=None):
    return [
        patch.object(Watcher, "_wait_ready", AsyncMock(return_value="ps_idle")),
        patch.object(Watcher, "_phase0_ready_wait", AsyncMock()),
        patch.object(Watcher, "_send_terminal", sends),
        patch.object(Watcher, "_verify_prompt_echo", AsyncMock(return_value=None)),
        patch.object(Watcher, "_wait_turn_done", AsyncMock(return_value="ps_done")),
        patch.object(Watcher, "_read_gate_frame", AsyncMock(return_value=frame)),
        patch("osw.watcher.terminal_send_dialog_enter", enter or AsyncMock(return_value={})),
        patch("osw.watcher.terminal_send", notify or AsyncMock(return_value={})),
        patch("osw.watcher.worktree_ps", AsyncMock(return_value=[])),
        patch("osw.watcher._handoff_ok", lambda path: True),
        patch("osw.watcher.BLOCKED_RETRY_SECS", 0),
    ]


async def _run_with(watcher, patches):
    from contextlib import ExitStack

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        await watcher.run()


@pytest.mark.anyio
async def test_blocked_send_clears_update_dialog_then_resends(tmp_path, blocker_env):
    blocker_env("enforce")
    watcher = _turn_agent(tmp_path, provider="codex")
    sends = AsyncMock(side_effect=[_blocked_error(), True, True])
    enter = AsyncMock(return_value={})

    await _run_with(watcher, _blocked_run_patches(sends, enter=enter))

    agent = state.read_agent(tmp_path, "agent_001")
    assert agent["state"] == "done"
    assert sends.await_count == 3          # blocked task, task, handoff
    assert sends.await_args_list[1].args[0] == "do the task"
    enter.assert_awaited_once_with("term-a")
    assert not blockers_mod.update_lock_path("codex").exists()
    names = _event_names(tmp_path)
    assert "turn_send_blocked" in names
    assert names.index("blocker_lock_released") > names.index("blocker_action")


@pytest.mark.anyio
async def test_blocked_send_that_never_clears_fails_as_blocked(tmp_path, blocker_env):
    watcher = _turn_agent(tmp_path, provider="codex")   # mode off: notify only
    sends = AsyncMock(side_effect=_blocked_error())
    notify = AsyncMock(return_value={})

    await _run_with(watcher, _blocked_run_patches(sends, notify=notify))

    agent = state.read_agent(tmp_path, "agent_001")
    assert agent["state"] == "error"
    assert agent["completion_source"] == "blocked"
    from osw.watcher import BLOCKED_SEND_LIMIT
    assert sends.await_count == BLOCKED_SEND_LIMIT + 1
    assert len(_blocked_lines(notify)) == 1


@pytest.mark.anyio
async def test_blocked_send_still_blocked_after_action_fails(tmp_path, blocker_env):
    blocker_env("enforce")
    watcher = _turn_agent(tmp_path, provider="codex")
    sends = AsyncMock(side_effect=_blocked_error())
    enter = AsyncMock(return_value={})

    await _run_with(watcher, _blocked_run_patches(sends, enter=enter))

    agent = state.read_agent(tmp_path, "agent_001")
    assert agent["state"] == "error"
    assert agent["completion_source"] == "blocked"
    assert sends.await_count == 2
    enter.assert_awaited_once()
    assert not blockers_mod.update_lock_path("codex").exists()


@pytest.mark.anyio
async def test_blocked_send_without_agent_wait_is_classified_by_jev(tmp_path, blocker_env):
    blocker_env("enforce")
    watcher = _turn_agent(tmp_path, provider="codex")
    error = OrcaError("agent prompt blocked", 1, code="agent_prompt_blocked")
    ask = AsyncMock(return_value=_update_kind_answer("announcement", 0.9))
    notify = AsyncMock(return_value={})

    with patch("osw.watcher.ask_blocker", ask):
        await _run_with(watcher, _blocked_run_patches(
            AsyncMock(side_effect=[error, True, True]), notify=notify))

    ask.assert_awaited()
    assert "kind=announcement" in _blocked_lines(notify)[0][1]
    assert state.read_agent(tmp_path, "agent_001")["state"] == "done"


@pytest.mark.anyio
async def test_gate_enforce_answer_can_act_on_a_whitelisted_blocker(
        tmp_path, blocker_env, monkeypatch):
    blocker_env("enforce")
    monkeypatch.setitem(blockers_mod.WHITELIST, ("claude", "update_prompt"), ("enter",))
    watcher = _claude_gate_setup(tmp_path, mode="enforce")
    enter = AsyncMock(return_value={})

    with patch.object(Watcher, "_read_gate_frame", AsyncMock(return_value=_UPDATE_FRAME)), \
         patch("osw.watcher.ask_ready", AsyncMock(return_value=_DIALOG_ANSWER)), \
         patch("osw.watcher.ask_blocker", AsyncMock(return_value=_update_kind_answer())), \
         patch("osw.watcher.terminal_send_dialog_enter", enter), \
         patch("osw.watcher.terminal_send", AsyncMock(return_value={})):
        vetoed = await watcher._gate_veto("ps_idle")

    assert vetoed is True
    enter.assert_awaited_once_with("term-a")


@pytest.mark.anyio
async def test_shadow_judgment_defers_keys_to_the_foreground(tmp_path, blocker_env):
    blocker_env("enforce")              # codex is not an enforce-hold provider
    watcher = _turn_agent(tmp_path, provider="codex")
    enter = AsyncMock(return_value={})
    send = AsyncMock(return_value={})

    with patch("osw.watcher.ask_ready", AsyncMock(return_value=_DIALOG_ANSWER)), \
         patch("osw.watcher.ask_blocker", AsyncMock(return_value=_update_kind_answer())), \
         patch("osw.watcher.terminal_send_dialog_enter", enter), \
         patch("osw.watcher.terminal_send", send):
        await watcher._shadow_ready_call("codex", _UPDATE_FRAME, "abstain")

    enter.assert_not_awaited()
    assert _blocked_lines(send) == []
    assert "blocker_action_deferred" in _event_names(tmp_path)


@pytest.mark.anyio
async def test_receipt_timeout_reports_the_blocking_screen(tmp_path, blocker_env):
    blocker_env("enforce")
    watcher = _turn_agent(tmp_path, provider="codex")
    watcher.deadline = time.monotonic() + 2
    ps = AsyncMock(return_value=_ps_with("done", 0))
    show = AsyncMock(return_value={"result": {"terminal": {
        "lastOutputAt": int(time.time() * 1000) - 10_000}}})
    enter = AsyncMock(return_value={})
    send = AsyncMock(return_value={})

    with patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_show", show), \
         patch.object(Watcher, "_read_gate_frame", AsyncMock(return_value=_UPDATE_FRAME)), \
         patch("osw.watcher.ask_ready", AsyncMock(return_value=_DIALOG_ANSWER)), \
         patch("osw.watcher.ask_blocker", AsyncMock(return_value=_update_kind_answer())), \
         patch("osw.watcher.terminal_send_dialog_enter", enter), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.POLL_SECS", 0), \
         patch("osw.watcher.RECEIPT_TIMEOUT_MS", 0):
        result = await watcher._wait_turn_done(
            time.time() * 1000 - 1000, sent_text="do the task")

    assert result == "no_receipt"
    enter.assert_not_awaited()          # the prompt is already in flight
    lines = _blocked_lines(send)
    assert len(lines) == 1 and "kind=update_prompt" in lines[0][1]


@pytest.mark.anyio
async def test_output_silence_is_not_ready_after_a_dialog_action(tmp_path, blocker_env):
    """codex exits to its shell after updating: a silent pane is then a
    shell, not a ready agent, so only agent evidence may release."""
    watcher = _turn_agent(tmp_path, provider="codex")
    watcher._blockers_acted.add("update_prompt")
    watcher.deadline = time.monotonic() + 0.2

    with patch("osw.watcher.worktree_ps", AsyncMock(return_value=[{"agents": []}])), \
         patch("osw.watcher.terminal_wait", AsyncMock(side_effect=OrcaError("no", 1))), \
         patch("osw.watcher.terminal_show", AsyncMock(return_value={"result": {
             "terminal": {"lastOutputAt": 1000, "preview": "PS D:\\> "}}})), \
         patch("osw.watcher.READY_TIMEOUT_MS", 100), \
         patch("osw.watcher.POLL_SECS", 0):
        source = await watcher._wait_ready()

    assert source is None
    assert "ready_output_idle_ignored" in _event_names(tmp_path)


@pytest.mark.anyio
async def test_finalize_releases_a_held_update_lock(tmp_path, blocker_env):
    watcher = _turn_agent(tmp_path, provider="codex")
    assert blockers_mod.acquire_update_lock("codex", "agent_001")
    watcher._update_lock_held = True

    with patch("osw.watcher.terminal_send", AsyncMock(return_value={})), \
         patch("osw.watcher.worktree_ps", AsyncMock(return_value=[])):
        await watcher._finalize("error", "ready_failed")

    assert not blockers_mod.update_lock_path("codex").exists()


# ---------------------------------------------------------------------------
# Turns without tool activity (plan §3.1)
# ---------------------------------------------------------------------------

def test_completion_report_can_carry_a_warning():
    line = format_completion_report(
        "agent_001", "term-a", "C:\\r.json", handoff_file="C:\\h.md",
        summary="all good", warning="no_tool_activity")

    assert " warning=no_tool_activity " in line
    assert line.index("warning=") < line.index("report=")
    assert "warning=" not in format_completion_report("a", "t", "r")


def _ps_tool(ps_state, started, tool=""):
    result = _ps_result("tab-a:leaf-b", ps_state, started)
    result[0]["agents"][0]["toolName"] = tool
    result[0]["agents"][0]["prompt"] = "do the task"
    return result


@pytest.mark.anyio
@pytest.mark.parametrize("tool,expected", [("Bash", True), ("", False)])
async def test_wait_turn_done_records_tool_activity(tmp_path, tool, expected):
    watcher = _turn_agent(tmp_path)
    watcher.deadline = time.monotonic() + 2
    now_ms = int(time.time() * 1000)
    ps_results = iter([_ps_tool("working", now_ms, tool)])
    ps = AsyncMock(side_effect=lambda: next(
        ps_results, _ps_tool("done", now_ms + 1000)))

    with patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_show", _quiet_show(now_ms)), \
         patch("osw.watcher.POLL_SECS", 0):
        source = await watcher._wait_turn_done(
            time.time() * 1000 - 1000, sent_text="do the task")

    assert source == "ps_done"
    assert watcher._turn_tracked is True
    assert watcher._turn_tools_seen is expected


@pytest.mark.anyio
async def test_stale_tool_name_on_a_done_row_is_not_activity(tmp_path):
    """ps keeps the previous turn's toolName on a done row; only a tool
    seen while the turn is working counts."""
    watcher = _turn_agent(tmp_path)
    watcher.deadline = time.monotonic() + 2
    now_ms = int(time.time() * 1000)

    with patch("osw.watcher.worktree_ps", AsyncMock(return_value=_ps_tool(
            "done", now_ms + 1000, "Edit"))), \
         patch("osw.watcher.terminal_show", _quiet_show(now_ms)), \
         patch("osw.watcher.POLL_SECS", 0):
        source = await watcher._wait_turn_done(
            time.time() * 1000 - 1000, sent_text="do the task")

    assert source == "ps_done"
    assert watcher._turn_tools_seen is False


def _fake_wait(task_tracked, task_tools, task_truncated_at=0):
    """A _wait_turn_done stand-in setting the per-turn observation flags:
    the task turn as given, the handoff turn with tools (it writes a file)."""
    async def fake(self, sent_at_ms, baseline=0, sent_text="", **_):
        handoff = sent_text.startswith("Task wrap-up:")
        self._turn_tracked = True if handoff else task_tracked
        self._turn_tools_seen = True if handoff else task_tools
        self._turn_truncated_at = 0 if handoff else task_truncated_at
        return "ps_done"
    return fake


async def _run_task_with(tmp_path, tracked, tools, truncated_at=0):
    watcher = _turn_agent(tmp_path)
    sends = AsyncMock(return_value=True)
    notify = AsyncMock(return_value={})
    with patch.object(Watcher, "_wait_ready", AsyncMock(return_value="ps_idle")), \
         patch.object(Watcher, "_phase0_ready_wait", AsyncMock()), \
         patch.object(Watcher, "_send_terminal", sends), \
         patch.object(Watcher, "_verify_prompt_echo", AsyncMock(return_value=None)), \
         patch.object(Watcher, "_wait_turn_done",
                      _fake_wait(tracked, tools, truncated_at)), \
         patch("osw.watcher._handoff_ok", lambda path: True), \
         patch("osw.watcher.terminal_send", notify), \
         patch("osw.watcher.worktree_ps", AsyncMock(return_value=[])):
        await watcher.run()
    agent = state.read_agent(tmp_path, "agent_001")
    report = json.loads(Path(agent["report_file"]).read_text(encoding="utf-8"))
    finished = [c.args[1] for c in notify.await_args_list
                if str(c.args[1]).startswith("# [osw] task-finished")]
    return agent, report, finished, sends


@pytest.mark.anyio
async def test_task_turn_without_tool_activity_is_flagged_not_resent(tmp_path):
    agent, report, finished, sends = await _run_task_with(
        tmp_path, tracked=True, tools=False)

    assert agent["state"] == "done"
    assert agent["completion_source"] == "ps_done"
    assert report["tools_observed"] is False
    assert len(finished) == 1 and "warning=no_tool_activity" in finished[0]
    assert sends.await_count == 2          # task + handoff, never a resend
    assert "no_tool_activity" in _event_names(tmp_path)


@pytest.mark.anyio
async def test_truncated_task_prompt_is_flagged_in_the_report(tmp_path):
    agent, report, finished, sends = await _run_task_with(
        tmp_path, tracked=True, tools=False, truncated_at=2048)

    assert agent["state"] == "done"
    assert report["prompt_truncated_at"] == 2048
    assert len(finished) == 1 and "warning=prompt_truncated" in finished[0]
    assert sends.await_count == 2          # task + handoff, never a resend


@pytest.mark.anyio
async def test_task_turn_with_tool_activity_has_no_warning(tmp_path):
    agent, report, finished, _ = await _run_task_with(
        tmp_path, tracked=True, tools=True)

    assert report["tools_observed"] is True
    assert "warning=" not in finished[0]


@pytest.mark.anyio
async def test_untracked_task_turn_leaves_tool_activity_unknown(tmp_path):
    """No ps row at all: tool calls could not be observed, so nothing is
    claimed and no warning is raised."""
    agent, report, finished, _ = await _run_task_with(
        tmp_path, tracked=False, tools=False)

    assert agent["state"] == "done"
    assert report["tools_observed"] is None
    assert "warning=" not in finished[0]


@pytest.mark.anyio
async def test_followed_turn_records_tool_activity_too(tmp_path):
    _follow_agent(tmp_path)
    watcher = Watcher(tmp_path, "claude_001")
    watcher.deadline = time.monotonic() + 2
    now_ms = int(time.time() * 1000)
    ps_results = iter([
        _follow_entry("working", now_ms - 30_000),           # takeover
        _follow_entry("working", now_ms - 30_000),
    ])
    ps = AsyncMock(side_effect=lambda: next(
        ps_results, _follow_entry("done", now_ms + 1_000)))
    send = AsyncMock(return_value={})

    with patch("osw.watcher.worktree_ps", ps), \
         patch("osw.watcher.terminal_show", _quiet_show(now_ms)), \
         patch("osw.watcher.terminal_send", send), \
         patch("osw.watcher.POLL_SECS", 0):
        await watcher.run()

    agent = state.read_agent(tmp_path, "claude_001")
    report = json.loads(Path(agent["report_file"]).read_text(encoding="utf-8"))
    assert agent["state"] == "done"
    assert report["tools_observed"] is False
    assert "warning=no_tool_activity" in send.await_args_list[-1].args[1]


@pytest.mark.anyio
async def test_already_done_follow_claims_nothing_about_tools(tmp_path):
    _follow_agent(tmp_path)
    watcher = Watcher(tmp_path, "claude_001")
    send = AsyncMock(return_value={})

    with patch("osw.watcher.worktree_ps", AsyncMock(return_value=_follow_entry(
            "done", int(time.time() * 1000) - 60_000))), \
         patch("osw.watcher.terminal_send", send):
        await watcher.run()

    agent = state.read_agent(tmp_path, "claude_001")
    report = json.loads(Path(agent["report_file"]).read_text(encoding="utf-8"))
    assert "tools_observed" not in report
    assert "warning=" not in send.await_args_list[-1].args[1]
