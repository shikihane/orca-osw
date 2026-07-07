from __future__ import annotations

import json
from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest

from osw import state as state_mod
from osw.server import (
    ServerContext,
    format_completion_report,
    handle_all,
    handle_del,
    handle_new,
    handle_use,
    now_iso,
    route_message,
    watcher,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def ctx(tmp_path):
    """ServerContext with initialised state dir and a populated models config."""
    state_mod.init_state_dir(tmp_path)
    s = state_mod.read_state(tmp_path)
    s["models"] = {
        "strong": [{"name": "codex-high", "command": "codex"}],
        "medium": [{"name": "pi-mid", "command": "pi"}],
        "weak": [],
    }
    tg = MagicMock()
    tg.start_soon = MagicMock()
    return ServerContext(root=tmp_path, state=s, task_group=tg)


@contextmanager
def orca_mocks(**overrides):
    """Patch every orca call on osw.server with working defaults.

    Yields a dict of the AsyncMocks so tests can override or inspect them.
    """
    mocks = {
        "terminal_create": AsyncMock(
            return_value={"result": {"terminal": {"handle": "t-worker"}}}),
        "terminal_send": AsyncMock(return_value={}),
        "terminal_wait": AsyncMock(return_value={}),
        "terminal_show": AsyncMock(
            return_value={"result": {"terminal": {"lastOutputAt": 1000}}}),
        "terminal_read": AsyncMock(
            return_value={"result": {"terminal": {"status": "running", "tail": []}}}),
        "terminal_info": AsyncMock(return_value={}),
        "terminal_list": AsyncMock(return_value=[]),
        "terminal_close": AsyncMock(return_value={}),
        "orchestration_task_create": AsyncMock(return_value="task_1"),
        "orchestration_dispatch": AsyncMock(return_value={"result": {}}),
        "orchestration_check": AsyncMock(return_value=[]),
    }
    mocks.update(overrides)
    with ExitStack() as stack:
        for name, mock in mocks.items():
            stack.enter_context(patch(f"osw.server.{name}", mock))
        stack.enter_context(patch("osw.server.START_POLL_SECS", 0.01))
        stack.enter_context(patch("osw.server.DONE_POLL_SECS", 0.02))
        yield mocks


def _agent(ctx, agent_id="agent_001", **kw):
    agent = {
        "agent_id": agent_id,
        "terminal": "t-worker",
        "state": "assigned",
        "caller_terminal": None,
        "last_prompt": "",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        **kw,
    }
    ctx.state["agents"][agent_id] = agent
    return agent


# ---------------------------------------------------------------------------
# handle_new: model selection + registration
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_handle_new_defaults_to_medium(ctx):
    with orca_mocks() as m:
        await handle_new(ctx, {"request_id": "r1", "prompt": "task"})

    assert m["terminal_create"].call_args.args[0] == "pi"
    agent = ctx.state["agents"]["agent_001"]
    assert agent["model_name"] == "pi-mid"
    assert agent["state"] == "assigned"
    ctx.task_group.start_soon.assert_called_once()

    with (state_mod.results_dir(ctx.root) / "r1.json").open() as f:
        assert json.load(f)["ok"] is True


@pytest.mark.anyio
async def test_handle_new_model_name_overrides_tier(ctx):
    with orca_mocks() as m:
        await handle_new(ctx, {
            "request_id": "r2", "prompt": "task",
            "tier": "medium", "model": "codex-high",
        })

    assert m["terminal_create"].call_args.args[0] == "codex"


@pytest.mark.anyio
async def test_handle_new_rejects_unknown_model_and_empty_config(ctx):
    with orca_mocks() as m:
        await handle_new(ctx, {"request_id": "r3", "prompt": "x", "model": "nope"})
        ctx.state["models"] = {"strong": [], "medium": [], "weak": []}
        await handle_new(ctx, {"request_id": "r4", "prompt": "x"})

    m["terminal_create"].assert_not_called()
    assert ctx.state["agents"] == {}
    for rid in ("r3", "r4"):
        with (state_mod.results_dir(ctx.root) / f"{rid}.json").open() as f:
            assert json.load(f)["ok"] is False


@pytest.mark.anyio
async def test_handle_new_empty_tier_falls_back(ctx):
    ctx.state["models"]["medium"] = []
    with orca_mocks() as m:
        await handle_new(ctx, {"request_id": "r5", "prompt": "x", "tier": "medium"})

    assert m["terminal_create"].call_args.args[0] == "codex"


# ---------------------------------------------------------------------------
# handle_use / handle_all / handle_del
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_handle_use_checks_worktree(ctx):
    info = {"handle": "t-x", "worktreePath": "/wrong/path"}
    with orca_mocks(terminal_info=AsyncMock(return_value=info)):
        await handle_use(ctx, {"request_id": "r6", "terminal": "t-x"})
    assert ctx.state["agents"] == {}

    info = {"handle": "t-y", "worktreePath": str(ctx.root)}
    with orca_mocks(terminal_info=AsyncMock(return_value=info)):
        await handle_use(ctx, {"request_id": "r7", "terminal": "t-y", "prompt": "p"})
    assert ctx.state["agents"]["agent_001"]["terminal"] == "t-y"


@pytest.mark.anyio
async def test_handle_all_skips_lost_agents(ctx):
    _agent(ctx, "agent_001", terminal="t1")
    _agent(ctx, "agent_002", terminal="t2", state="lost")

    with orca_mocks() as m:
        await handle_all(ctx, {"request_id": "r8", "message": "hi"})

    assert m["terminal_send"].call_count == 1
    assert m["terminal_send"].call_args.args[0] == "t1"


@pytest.mark.anyio
async def test_handle_del_cancels_watcher_and_removes(ctx):
    _agent(ctx, "agent_001")
    scope = anyio.CancelScope()
    ctx.watchers["agent_001"] = scope

    with orca_mocks():
        await handle_del(ctx, {"request_id": "r9", "agent_id": "agent_001"})

    assert scope.cancel_called
    assert "agent_001" not in ctx.state["agents"]


# ---------------------------------------------------------------------------
# watcher: orchestration mode (coordinator set)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_watcher_orchestration_mode(ctx):
    ctx.coordinator = "t-coord"
    _agent(ctx, "agent_001", last_prompt="Fix tests", caller_terminal="t-caller")

    async def deliver_worker_done():
        await anyio.sleep(0.1)
        await route_message(ctx, {
            "id": "m1", "type": "worker_done", "from_handle": "t-worker",
            "subject": "Done", "body": "Fixed. Clean. Nothing left.",
            "payload": '{"taskId":"task_1","filesModified":["a.py"]}',
        })

    with orca_mocks() as m:
        async with anyio.create_task_group() as tg:
            tg.start_soon(deliver_worker_done)
            await watcher(ctx, "agent_001")

    agent = ctx.state["agents"]["agent_001"]
    assert agent["state"] == "done"
    assert agent["task_id"] == "task_1"

    # dispatched, no raw prompt injection; single caller notification
    m["orchestration_dispatch"].assert_called_once()
    assert m["terminal_send"].call_count == 1
    sent_to, sent_text = m["terminal_send"].call_args.args
    assert sent_to == "t-caller"
    assert "task-finished" in sent_text and "\n" not in sent_text

    with open(agent["report_file"], encoding="utf-8") as f:
        report = json.load(f)
    assert report["completion_source"] == "worker_done"
    assert report["worker_summary"] == "Fixed. Clean. Nothing left."
    assert report["files_modified"] == ["a.py"]


# ---------------------------------------------------------------------------
# watcher: legacy mode (no coordinator) sends prompt + idle detection
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_watcher_legacy_mode(ctx):
    ctx.coordinator = None
    _agent(ctx, "agent_001", last_prompt="Fix tests", caller_terminal="t-caller")

    # lastOutputAt: baseline 1000 -> 2000 (started) -> 2000 (idle stable)
    show = AsyncMock(side_effect=[
        {"result": {"terminal": {"lastOutputAt": 1000}}},
        {"result": {"terminal": {"lastOutputAt": 2000}}},
        {"result": {"terminal": {"lastOutputAt": 2000}}},
    ])
    with orca_mocks(terminal_show=show) as m:
        await watcher(ctx, "agent_001")

    assert ctx.state["agents"]["agent_001"]["state"] == "done"
    # prompt sent to worker, then notification to caller
    assert m["terminal_send"].call_args_list[0].args == ("t-worker", "Fix tests")
    assert m["terminal_send"].call_args_list[1].args[0] == "t-caller"
    m["orchestration_dispatch"].assert_not_called()


@pytest.mark.anyio
async def test_watcher_no_prompt_marks_done(ctx):
    _agent(ctx, "agent_001")
    with orca_mocks() as m:
        await watcher(ctx, "agent_001")

    assert ctx.state["agents"]["agent_001"]["state"] == "done"
    m["terminal_send"].assert_not_called()


# ---------------------------------------------------------------------------
# orchestration message routing
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_route_worker_done_by_task_or_sender(ctx):
    _agent(ctx, "agent_001", state="working")
    ctx.done_events["agent_001"] = anyio.Event()
    ctx.task_agents["task_9"] = "agent_001"

    msg = {"id": "m1", "type": "worker_done", "from_handle": "t-other",
           "subject": "d", "body": "b", "payload": '{"taskId":"task_9"}'}
    await route_message(ctx, msg)
    assert ctx.done_events["agent_001"].is_set()
    assert ctx.worker_msgs["agent_001"] is msg

    # no taskId -> routed by sender handle
    ctx.done_events["agent_001"] = anyio.Event()
    await route_message(ctx, {"id": "m2", "type": "worker_done",
                              "from_handle": "t-worker", "payload": ""})
    assert ctx.done_events["agent_001"].is_set()


@pytest.mark.anyio
async def test_route_escalation_forwards_to_caller(ctx):
    _agent(ctx, "agent_001", state="working", caller_terminal="t-caller")

    with orca_mocks() as m:
        await route_message(ctx, {
            "id": "m3", "type": "escalation", "from_handle": "t-worker",
            "subject": "Blocked: need input", "body": "d", "payload": "",
        })

    sent_to, sent_text = m["terminal_send"].call_args.args
    assert sent_to == "t-caller"
    assert "escalation" in sent_text and "\n" not in sent_text


# ---------------------------------------------------------------------------
# notification format
# ---------------------------------------------------------------------------

def test_completion_report_is_single_inert_line():
    line = format_completion_report("a1", "t1", "C:\\r.json", summary="did\nthings")
    assert line.startswith("# [osw] task-finished")
    assert "report=C:\\r.json" in line
    assert "did things" in line
    assert "\n" not in line
