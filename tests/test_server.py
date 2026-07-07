from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest

from osw import state as state_mod
from osw.handoff import format_completion_report
from osw.server import (
    ServerContext,
    handle_all,
    handle_del,
    handle_new,
    handle_use,
    now_iso,
    watcher,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def initialized_root(tmp_path):
    """Create an initialised OSW state directory and return its root."""
    state_mod.init_state_dir(tmp_path)
    return tmp_path


@pytest.fixture
def server_ctx(initialized_root):
    """Build a ServerContext backed by *initialized_root* with a mock task group."""
    s = state_mod.read_state(initialized_root)
    tg = MagicMock()
    tg.start_soon = MagicMock()
    ctx = ServerContext(
        root=initialized_root,
        state=s,
        task_group=tg,
    )
    return ctx


# ------------------------------------------------------------------
# handle_new
# ------------------------------------------------------------------

@pytest.mark.anyio
async def test_handle_new_creates_agent(server_ctx):
    mock_create = AsyncMock(return_value={"handle": "term-1"})
    mock_send = AsyncMock(return_value={})

    request = {
        "request_id": "req-001",
        "command": "new",
        "prompt": "Fix the tests",
        "caller_terminal": "caller-t1",
    }

    with patch("osw.server.terminal_create", mock_create):
        await handle_new(server_ctx, request)

    # Agent should be registered in state
    assert "agent_001" in server_ctx.state["agents"]
    agent = server_ctx.state["agents"]["agent_001"]
    assert agent["terminal"] == "term-1"
    assert agent["state"] == "assigned"
    assert agent["last_prompt"] == "Fix the tests"
    assert agent["caller_terminal"] == "caller-t1"

    # Result file should be written with ok=True
    result_path = state_mod.results_dir(server_ctx.root) / "req-001.json"
    assert result_path.exists()
    with result_path.open() as f:
        result = json.load(f)
    assert result["ok"] is True
    assert result["agent_id"] == "agent_001"
    assert result["terminal"] == "term-1"

    # Watcher should have been scheduled
    server_ctx.task_group.start_soon.assert_called_once()


# ------------------------------------------------------------------
# handle_use
# ------------------------------------------------------------------

@pytest.mark.anyio
async def test_handle_use_rejects_wrong_dir(server_ctx):
    mock_info = AsyncMock(return_value={
        "handle": "term-x",
        "worktreePath": "/some/other/path",
    })

    request = {
        "request_id": "req-002",
        "command": "use",
        "terminal": "term-x",
    }

    with patch("osw.server.terminal_info", mock_info):
        await handle_use(server_ctx, request)

    # No agent should have been added
    assert len(server_ctx.state["agents"]) == 0

    # Error result should be written
    result_path = state_mod.results_dir(server_ctx.root) / "req-002.json"
    assert result_path.exists()
    with result_path.open() as f:
        result = json.load(f)
    assert result["ok"] is False
    assert "error" in result


@pytest.mark.anyio
async def test_handle_use_accepts_correct_dir(server_ctx):
    root_str = str(server_ctx.root)
    mock_info = AsyncMock(return_value={
        "handle": "term-y",
        "worktreePath": root_str,
    })
    mock_send = AsyncMock(return_value={})

    request = {
        "request_id": "req-003",
        "command": "use",
        "terminal": "term-y",
        "prompt": "Do something",
        "caller_terminal": "caller-t2",
    }

    with patch("osw.server.terminal_info", mock_info), \
         patch("osw.server.terminal_send", mock_send):
        await handle_use(server_ctx, request)

    # Agent should be added
    assert "agent_001" in server_ctx.state["agents"]
    agent = server_ctx.state["agents"]["agent_001"]
    assert agent["terminal"] == "term-y"
    assert agent["state"] == "assigned"

    # Success result
    result_path = state_mod.results_dir(server_ctx.root) / "req-003.json"
    assert result_path.exists()
    with result_path.open() as f:
        result = json.load(f)
    assert result["ok"] is True
    assert result["agent_id"] == "agent_001"


# ------------------------------------------------------------------
# handle_all
# ------------------------------------------------------------------

@pytest.mark.anyio
async def test_handle_all_broadcasts(server_ctx):
    server_ctx.state["agents"] = {
        "agent_001": {"terminal": "t1", "state": "assigned"},
        "agent_002": {"terminal": "t2", "state": "idle"},
    }

    mock_send = AsyncMock(return_value={})

    request = {
        "request_id": "req-004",
        "command": "all",
        "message": "Status update please",
    }

    with patch("osw.server.terminal_send", mock_send):
        await handle_all(server_ctx, request)

    # terminal_send should have been called for each agent
    assert mock_send.call_count == 2
    called_handles = {call.args[0] for call in mock_send.call_args_list}
    assert called_handles == {"t1", "t2"}

    # Result should be written
    result_path = state_mod.results_dir(server_ctx.root) / "req-004.json"
    assert result_path.exists()
    with result_path.open() as f:
        result = json.load(f)
    assert result["ok"] is True


# ------------------------------------------------------------------
# handle_del
# ------------------------------------------------------------------

@pytest.mark.anyio
async def test_handle_del_removes_agent(server_ctx):
    # Pre-populate an agent and its watcher scope
    server_ctx.state["agents"]["agent_001"] = {
        "terminal": "t1",
        "state": "assigned",
    }
    scope = anyio.CancelScope()
    server_ctx.watchers["agent_001"] = scope

    request = {
        "request_id": "req-005",
        "command": "del",
        "agent_id": "agent_001",
    }

    await handle_del(server_ctx, request)

    # Agent should be gone
    assert "agent_001" not in server_ctx.state["agents"]

    # Watcher scope should have been cancelled
    assert scope.cancel_called

    # Result should be written
    result_path = state_mod.results_dir(server_ctx.root) / "req-005.json"
    assert result_path.exists()
    with result_path.open() as f:
        result = json.load(f)
    assert result["ok"] is True
    assert result["agent_id"] == "agent_001"


# ------------------------------------------------------------------
# watcher — waits for ready, sends prompt, waits for done
# ------------------------------------------------------------------

@pytest.mark.anyio
async def test_watcher_sends_prompt_after_ready(server_ctx):
    server_ctx.state["agents"]["agent_001"] = {
        "agent_id": "agent_001",
        "terminal": "t1",
        "state": "assigned",
        "caller_terminal": "caller-t1",
        "last_prompt": "Fix the tests",
        "last_handoff_file": None,
        "updated_at": now_iso(),
    }

    mock_wait = AsyncMock(side_effect=[{}, {}])
    mock_send = AsyncMock(return_value={})
    # lastOutputAt: baseline=1000, then 2000 (started), then 2000 (idle stable)
    mock_show = AsyncMock(side_effect=[
        {"result": {"terminal": {"lastOutputAt": 1000}}},
        {"result": {"terminal": {"lastOutputAt": 2000}}},
        {"result": {"terminal": {"lastOutputAt": 2000}}},
    ])
    mock_read = AsyncMock(return_value={
        "result": {"terminal": {
            "status": "running",
            "tail": ["hello", "world"],
        }}
    })

    with patch("osw.server.terminal_wait", mock_wait), \
         patch("osw.server.terminal_send", mock_send), \
         patch("osw.server.terminal_show", mock_show), \
         patch("osw.server.terminal_read", mock_read), \
         patch("osw.server.START_POLL_SECS", 0.01):
        await watcher(server_ctx, "agent_001")

    agent = server_ctx.state["agents"]["agent_001"]
    assert agent["state"] == "done"

    assert mock_wait.call_count == 2
    assert mock_send.call_count == 2
    assert mock_send.call_args_list[0].args == ("t1", "Fix the tests")
    assert mock_send.call_args_list[1].args[0] == "caller-t1"

    # Structured report file written and referenced
    report_file = agent.get("report_file")
    assert report_file
    with open(report_file, encoding="utf-8") as f:
        report = json.load(f)
    assert report["agent_id"] == "agent_001"
    assert report["state"] == "done"
    assert report["prompt"] == "Fix the tests"
    assert report["output_tail"] == ["hello", "world"]
    assert report["terminal_status"] == "running"

    # Notification message points to the report file
    notification = mock_send.call_args_list[1].args[1]
    assert "agent_001" in notification
    assert report_file in notification
    assert "\n" not in notification


# ------------------------------------------------------------------
# watcher — no prompt, marks done on first idle
# ------------------------------------------------------------------

@pytest.mark.anyio
async def test_watcher_no_prompt_done_on_first_idle(server_ctx):
    server_ctx.state["agents"]["agent_001"] = {
        "agent_id": "agent_001",
        "terminal": "t1",
        "state": "assigned",
        "caller_terminal": None,
        "last_prompt": "",
        "last_handoff_file": None,
        "updated_at": now_iso(),
    }

    mock_wait = AsyncMock(return_value={})
    mock_send = AsyncMock(return_value={})
    mock_read = AsyncMock(return_value={
        "result": {"terminal": {"status": "running", "tail": []}}
    })

    with patch("osw.server.terminal_wait", mock_wait), \
         patch("osw.server.terminal_send", mock_send), \
         patch("osw.server.terminal_read", mock_read):
        await watcher(server_ctx, "agent_001")

    agent = server_ctx.state["agents"]["agent_001"]
    assert agent["state"] == "done"
    mock_wait.assert_called_once()
    mock_send.assert_not_called()
