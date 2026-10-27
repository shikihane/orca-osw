from __future__ import annotations

import json
import os
import subprocess
from unittest.mock import AsyncMock, Mock, patch

import pytest
from typer.testing import CliRunner

from osw import state as state_mod
from osw.cli import app, _spawn_watcher
from osw.log import read_events
from osw.orca_cli import OrcaError

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_live_terminal_list():
    """Commands that check panes (`revive`) never reach a live Orca;
    a test's own `terminal_list` patch takes precedence."""
    with patch("osw.cli.terminal_list", AsyncMock(return_value=[])):
        yield


def test_init_creates_state_and_reports_scan(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert (tmp_path / ".orca" / "osw" / "state.json").is_file()
    state = state_mod.read_state(tmp_path)
    assert state == {"version": 3, "project_root": str(tmp_path)}
    assert "Scanning for agent CLIs on PATH" in result.output
    assert "Inspect provider models with:" in result.output


def test_list_and_status(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["status"]).exit_code != 0  # not initialized

    state_mod.init_state_dir(tmp_path)
    assert "no agents" in runner.invoke(app, ["list"]).output.lower()
    assert json.loads(runner.invoke(app, ["list", "--json"]).output) == {}
    assert "agents: none" in runner.invoke(app, ["status"]).output.lower()


def test_doctor_json_reports_orca_contract_without_init(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    report = {
        "ok": True,
        "orca": {"command": ["orca"], "version": "1.4.166"},
        "runtime": {"state": "ready", "reachable": True},
        "graph": {"state": "ready"},
        "worktree": {"path": str(tmp_path)},
        "contract": {
            "terminal_list": "ok",
            "terminal_show": "ok",
            "worktree_ps": "ok",
            "agent_status": "ok",
        },
        "counts": {"terminals": 1, "worktrees": 2, "agents": 1},
    }

    with patch(
        "osw.cli.compatibility_report",
        AsyncMock(return_value=report),
        create=True,
    ):
        result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.output) == report


def test_doctor_text_reports_each_checked_contract(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    report = {
        "ok": True,
        "orca": {"command": ["orca"], "version": "1.4.166"},
        "runtime": {"state": "ready", "reachable": True},
        "graph": {"state": "ready"},
        "worktree": {"path": str(tmp_path)},
        "contract": {
            "terminal_list": "ok",
            "terminal_show": "ok",
            "worktree_ps": "ok",
            "agent_status": "ok",
        },
        "counts": {"terminals": 1, "worktrees": 2, "agents": 1},
    }

    with patch(
        "osw.cli.compatibility_report",
        AsyncMock(return_value=report),
    ):
        result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "terminal_show=ok" in result.output


def test_doctor_json_reports_typed_contract_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    failure = OrcaError(
        "missing result.terminals",
        1,
        code="orca_contract_mismatch",
    )

    with patch(
        "osw.cli.compatibility_report",
        AsyncMock(side_effect=failure),
    ):
        result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.output) == {
        "ok": False,
        "error": {
            "code": "orca_contract_mismatch",
            "message": "missing result.terminals",
        },
    }


def test_logs_command_tails_events_and_filters_agent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    log_dir = state_mod.logs_dir(tmp_path)
    (log_dir / "agent_001_123.log").write_text("line 1\nline 2\n", encoding="utf-8")
    (log_dir / "agent_002_456.log").write_text("other\n", encoding="utf-8")
    events = [
        {
            "ts": "2026-07-08T00:00:00Z",
            "level": "INFO",
            "component": "watcher",
            "event": "turn_sent",
            "agent_id": "agent_001",
            "terminal": "term-a",
            "phase": "task",
            "message": "sent",
        },
        {
            "ts": "2026-07-08T00:00:01Z",
            "level": "INFO",
            "component": "watcher",
            "event": "turn_sent",
            "agent_id": "agent_002",
            "terminal": "term-b",
            "phase": "task",
            "message": "sent",
        },
    ]
    (log_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["logs", "--agent", "agent_001", "--tail", "5"])

    assert result.exit_code == 0
    assert "agent_001_123.log" in result.output
    assert "agent_002_456.log" not in result.output
    assert "agent_001" in result.output
    assert "agent_002" not in result.output
    assert "line 2" in result.output


@pytest.mark.parametrize("args", [
    ["new", "claude", "test"],
    ["use", "t1", "test"],
])
def test_commands_require_initialized_state(tmp_path, monkeypatch, args):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, args)

    assert result.exit_code == 1
    assert "Not initialized" in result.output


def test_new_creates_provider_terminal_agent_file_and_detaches_watcher(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)

    create = AsyncMock(return_value={"result": {"terminal": {
        "handle": "term-new",
        "tabId": "tab-a",
        "leafId": "leaf-b",
    }}})
    spawn = Mock(return_value=4567)
    with patch("osw.cli.terminal_create", create), \
         patch("osw.cli._spawn_watcher", spawn):
        result = runner.invoke(
            app,
            [
                "new",
                "claude",
                "--model",
                "sonnet",
                "--thinking",
                "high",
                "--caller-terminal",
                "caller-1",
                "--prefix",
                "research",
                "do the task",
            ],
        )

    assert result.exit_code == 0
    assert "Created research_001" in result.output
    assert "(provider: claude)" in result.output
    # The launch command must not embed the task prompt: the watcher
    # sends it as its own observable turn once the TUI is ready.
    create.assert_awaited_once_with(
        "claude --dangerously-skip-permissions --model sonnet --effort high"
    )
    spawn.assert_called_once_with(tmp_path, "research_001")

    agent = state_mod.read_agent(tmp_path, "research_001")
    assert agent["terminal"] == "term-new"
    assert agent["pane_key"] == "tab-a:leaf-b"
    assert agent["prompt"] == "do the task"
    assert "task_started_on_launch" not in agent
    assert agent["caller_terminal"] == "caller-1"
    assert agent["provider"] == "claude"
    assert agent["model_name"] == "sonnet"
    assert agent["thinking"] == "high"
    assert agent["provider_command"] == (
        "claude --dangerously-skip-permissions --model sonnet --effort high"
    )
    assert agent["watcher_pid"] == 4567
    assert agent["state"] == "assigned"
    assert agent["origin"] == "new"


def _supervised_start_result(
    handle: str = "term-sup", pane: str = "tab-x:leaf-y",
) -> dict:
    return {"result": {
        "runId": "run_abc",
        "taskId": "task_1",
        "dispatchId": "ctx_1",
        "effects": [{
            "kind": "terminal", "role": "agent",
            "action": "created", "id": handle,
        }],
        "assigneePaneKey": pane,
    }}


def _worker_show_result() -> dict:
    return {"result": {
        "dispatch": {"status": "dispatched", "assigneePaneKey": "tab-x:leaf-y"},
        "worker": {"state": "working", "stage": "running",
                   "agentTerminalHandle": "term-sup"},
        "projection": {"stage": {"dispatch": "dispatched",
                                 "worker": "working"}},
        "observation": {"status": "live", "exactWorker": True,
                        "agentWait": None},
    }}


def test_new_supervised_omp_launches_through_worker_start(tmp_path, monkeypatch):
    """omp is launched by Orca's supervised orchestration.

    That launch authority is what makes Orca persist the pane's hook
    status rows at all (a plain `terminal create` pane has none), and
    the Dispatch lifecycle is the watcher's completion signal.
    """
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    run_current = AsyncMock(return_value={"run": None})
    run_create = AsyncMock(return_value={"run": {"id": "run_abc"}})
    start = AsyncMock(return_value=_supervised_start_result())
    show = AsyncMock(return_value=_worker_show_result())
    spawn = Mock(return_value=6789)

    with patch("osw.cli.orchestration_run_current", run_current), \
         patch("osw.cli.orchestration_run_create", run_create), \
         patch("osw.cli.orchestration_worker_start", start), \
         patch("osw.cli.orchestration_worker_show", show), \
         patch("osw.cli._spawn_watcher", spawn):
        result = runner.invoke(
            app,
            [
                "new", "omp",
                "--model", "deepseek/deepseek-flash",
                "--caller-terminal", "caller-1",
                "--prefix", "misc",
                "只回复：OK",
            ],
        )

    assert result.exit_code == 0
    assert "Created misc_001" in result.output
    assert "dispatch=ctx_1" in result.output
    run_create.assert_awaited_once()
    start_kwargs = start.await_args.kwargs
    assert start_kwargs["agent"] == "omp"
    assert start_kwargs["worktree"] == f"path:{tmp_path}"
    assert start_kwargs["model"] == "deepseek/deepseek-flash"
    assert "只回复：OK" in start_kwargs["spec"]
    assert "worker_done" in start_kwargs["spec"]
    assert "handoffs" in start_kwargs["spec"]
    spawn.assert_called_once_with(tmp_path, "misc_001")

    agent = state_mod.read_agent(tmp_path, "misc_001")
    assert agent["launch_mode"] == "supervised"
    assert agent["origin"] == "new"
    assert agent["orchestration_agent"] == "omp"
    assert agent["dispatch_id"] == "ctx_1"
    assert agent["task_id"] == "task_1"
    assert agent["run_id"] == "run_abc"
    assert agent["terminal"] == "term-sup"
    assert agent["pane_key"] == "tab-x:leaf-y"
    assert agent["handoff_path"].endswith(".md")
    assert state_mod.read_state(tmp_path)["orchestration_run_id"] == "run_abc"


def test_new_supervised_rebinds_foreign_caller_run(tmp_path, monkeypatch):
    """A caller already bound to another Run gets rebound to the
    project's Run — recorded as an event so the switch is visible."""
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    state = state_mod.read_state(tmp_path)
    state["orchestration_run_id"] = "run_osw"
    state_mod.write_state(tmp_path, state)
    run_current = AsyncMock(return_value={"run": {"id": "run_other"}})
    run_use = AsyncMock(return_value={"result": {}})
    start = AsyncMock(return_value=_supervised_start_result())
    show = AsyncMock(return_value=_worker_show_result())

    with patch("osw.cli.orchestration_run_current", run_current), \
         patch("osw.cli.orchestration_run_use", run_use), \
         patch("osw.cli.orchestration_worker_start", start), \
         patch("osw.cli.orchestration_worker_show", show), \
         patch("osw.cli._spawn_watcher", Mock(return_value=1)):
        result = runner.invoke(app, ["new", "omp", "--no-notify", "task"])

    assert result.exit_code == 0
    run_use.assert_awaited_once_with("run_osw")
    events = [
        json.loads(line)
        for line in (state_mod.logs_dir(tmp_path) / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    rebound = [
        event for event in events
        if event["event"] == "orchestration_run_rebound"
    ]
    assert rebound and rebound[0]["data"]["previous_run_id"] == "run_other"


def test_new_supervised_rejects_thinking(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)

    result = runner.invoke(app, ["new", "omp", "--thinking", "high", "task"])

    assert result.exit_code == 1
    assert "supervised" in result.output
    assert "--thinking" in result.output


def test_del_supervised_releases_settled_dispatch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    state_mod.write_agent(tmp_path, {
        "agent_id": "misc_001",
        "terminal": "t1",
        "state": "done",
        "launch_mode": "supervised",
        "dispatch_id": "ctx_1",
    })
    release = AsyncMock(return_value={})
    stop = AsyncMock(return_value={})
    close = AsyncMock(return_value={})

    with patch("osw.cli.orchestration_worker_release", release), \
         patch("osw.cli.orchestration_worker_stop", stop), \
         patch("osw.cli.terminal_close", close):
        result = runner.invoke(app, ["del", "misc_001", "--close"])

    assert result.exit_code == 0
    release.assert_awaited_once_with("ctx_1")
    stop.assert_not_awaited()
    close.assert_not_awaited()
    assert state_mod.list_agents(tmp_path) == {}


def test_del_supervised_stops_running_dispatch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    state_mod.write_agent(tmp_path, {
        "agent_id": "misc_001",
        "terminal": "t1",
        "state": "working",
        "launch_mode": "supervised",
        "dispatch_id": "ctx_1",
    })
    release = AsyncMock(return_value={})
    stop = AsyncMock(return_value={})

    with patch("osw.cli.orchestration_worker_release", release), \
         patch("osw.cli.orchestration_worker_stop", stop), \
         patch("osw.cli.terminal_close", AsyncMock(return_value={})):
        result = runner.invoke(app, ["del", "misc_001", "--close"])

    assert result.exit_code == 0
    stop.assert_awaited_once_with("ctx_1")
    release.assert_not_awaited()


def test_new_supervised_recreates_run_when_stored_run_unusable(
    tmp_path, monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    state = state_mod.read_state(tmp_path)
    state["orchestration_run_id"] = "run_stale"
    state_mod.write_state(tmp_path, state)
    run_current = AsyncMock(return_value={"run": {"id": "run_other"}})
    run_use = AsyncMock(side_effect=OrcaError("run gone", 1))
    run_create = AsyncMock(return_value={"run": {"id": "run_new"}})
    start = AsyncMock(return_value=_supervised_start_result())
    show = AsyncMock(return_value=_worker_show_result())

    with patch("osw.cli.orchestration_run_current", run_current), \
         patch("osw.cli.orchestration_run_use", run_use), \
         patch("osw.cli.orchestration_run_create", run_create), \
         patch("osw.cli.orchestration_worker_start", start), \
         patch("osw.cli.orchestration_worker_show", show), \
         patch("osw.cli._spawn_watcher", Mock(return_value=1)):
        result = runner.invoke(app, ["new", "omp", "--no-notify", "task"])

    assert result.exit_code == 0
    run_use.assert_awaited_once_with("run_stale")
    run_create.assert_awaited_once()
    assert state_mod.read_state(tmp_path)["orchestration_run_id"] == "run_new"


def test_new_supervised_launch_failure_leaves_no_agent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    state = state_mod.read_state(tmp_path)
    state["orchestration_run_id"] = "run_abc"
    state_mod.write_state(tmp_path, state)
    run_current = AsyncMock(return_value={"run": {"id": "run_abc"}})
    start = AsyncMock(side_effect=OrcaError(
        "worker-start refused", 1, code="consumer_fenced",
    ))

    with patch("osw.cli.orchestration_run_current", run_current), \
         patch("osw.cli.orchestration_worker_start", start), \
         patch("osw.cli._spawn_watcher", Mock(return_value=1)):
        result = runner.invoke(app, ["new", "omp", "--no-notify", "task"])

    assert result.exit_code == 1
    assert "worker-start refused" in result.output
    events = [
        json.loads(line)
        for line in (state_mod.logs_dir(tmp_path) / "events.jsonl")
        .read_text(encoding="utf-8").splitlines()
    ]
    assert any(e["event"] == "orchestration_launch_failed" for e in events)
    assert list(tmp_path.glob(".orca/osw/agents/*.json")) == []


def test_new_supervised_missing_terminal_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    state = state_mod.read_state(tmp_path)
    state["orchestration_run_id"] = "run_abc"
    state_mod.write_state(tmp_path, state)
    run_current = AsyncMock(return_value={"run": {"id": "run_abc"}})
    start = AsyncMock(return_value={"result": {
        "runId": "run_abc", "taskId": "task_1", "dispatchId": "ctx_1",
        "effects": [],
    }})
    show = AsyncMock(return_value={"result": {
        "dispatch": {"status": "dispatched"},
        "worker": {"state": "working", "stage": "running"},
        "projection": {"stage": {"dispatch": "dispatched"}},
        "observation": {"status": "live"},
    }})

    with patch("osw.cli.orchestration_run_current", run_current), \
         patch("osw.cli.orchestration_worker_start", start), \
         patch("osw.cli.orchestration_worker_show", show), \
         patch("osw.cli._spawn_watcher", Mock(return_value=1)):
        result = runner.invoke(app, ["new", "omp", "--no-notify", "task"])

    assert result.exit_code == 1
    assert "no agent terminal" in result.output
    assert list(tmp_path.glob(".orca/osw/agents/*.json")) == []


def test_new_supervised_settlement_supplies_missing_terminal(
    tmp_path, monkeypatch,
):
    """The receipt may lack a terminal effect; worker-show's dispatch
    carries the assignee handle then."""
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    state = state_mod.read_state(tmp_path)
    state["orchestration_run_id"] = "run_abc"
    state_mod.write_state(tmp_path, state)
    run_current = AsyncMock(return_value={"run": {"id": "run_abc"}})
    start = AsyncMock(return_value={"result": {
        "runId": "run_abc", "taskId": "task_1", "dispatchId": "ctx_1",
        "effects": [],
    }})
    show = AsyncMock(return_value={"result": {
        "dispatch": {"status": "dispatched", "assigneeHandle": "term-show"},
        "worker": {"state": "working", "stage": "running"},
        "projection": {"stage": {"dispatch": "dispatched"}},
        "observation": {"status": "live"},
    }})

    with patch("osw.cli.orchestration_run_current", run_current), \
         patch("osw.cli.orchestration_worker_start", start), \
         patch("osw.cli.orchestration_worker_show", show), \
         patch("osw.cli._spawn_watcher", Mock(return_value=1)):
        result = runner.invoke(app, ["new", "omp", "--no-notify", "task"])

    assert result.exit_code == 0
    assert state_mod.read_agent(tmp_path, "agent_001")["terminal"] == "term-show"


def test_new_supervised_launch_failure_releases_orphan(
    tmp_path, monkeypatch,
):
    """A readiness-timeout failure leaves a terminal behind; OSW follows
    Orca's own recovery (release) so nothing is stranded."""
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    state = state_mod.read_state(tmp_path)
    state["orchestration_run_id"] = "run_abc"
    state_mod.write_state(tmp_path, state)
    run_current = AsyncMock(return_value={"run": {"id": "run_abc"}})
    start = AsyncMock(side_effect=OrcaError(
        "orca command failed at agent_readiness: timeout (dispatch ctx_dead)",
        1,
        payload={"result": {
            "dispatchId": "ctx_dead",
            "state": "failed",
            "failedStage": "agent_readiness",
            "lastError": "timeout",
            "residualResources": [{
                "kind": "terminal", "role": "agent",
                "action": "created", "id": "term-orphan",
            }],
        }},
    ))
    release = AsyncMock(return_value={})

    with patch("osw.cli.orchestration_run_current", run_current), \
         patch("osw.cli.orchestration_worker_start", start), \
         patch("osw.cli.orchestration_worker_release", release), \
         patch("osw.cli._spawn_watcher", Mock(return_value=1)):
        result = runner.invoke(app, ["new", "omp", "--no-notify", "task"])

    assert result.exit_code == 1
    assert "agent_readiness" in result.output
    release.assert_awaited_once_with("ctx_dead")
    events = [
        json.loads(line)
        for line in (state_mod.logs_dir(tmp_path) / "events.jsonl")
        .read_text(encoding="utf-8").splitlines()
    ]
    failed = [
        e for e in events if e["event"] == "orchestration_launch_failed"
    ]
    assert failed and failed[0]["data"]["orphan_released"] == "ctx_dead"
    assert list(tmp_path.glob(".orca/osw/agents/*.json")) == []


def test_use_supervised_launch_failure_is_reported(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    state = state_mod.read_state(tmp_path)
    state["orchestration_run_id"] = "run_abc"
    state_mod.write_state(tmp_path, state)
    state_mod.write_agent(tmp_path, {
        "agent_id": "misc_001",
        "terminal": "term-sup",
        "state": "done",
        "provider": "omp",
        "launch_mode": "supervised",
        "dispatch_id": "ctx_old",
    })
    show_terminal = AsyncMock(return_value={"result": {"terminal": {
        "handle": "term-sup",
        "worktreePath": str(tmp_path),
    }}})
    run_current = AsyncMock(return_value={"run": {"id": "run_abc"}})
    start = AsyncMock(side_effect=OrcaError("terminal not reusable", 1))

    with patch("osw.cli.terminal_show", show_terminal), \
         patch("osw.cli.orchestration_run_current", run_current), \
         patch("osw.cli.orchestration_worker_start", start), \
         patch("osw.cli._spawn_watcher", Mock(return_value=1)):
        result = runner.invoke(
            app, ["use", "misc_001", "--no-notify", "继续"],
        )

    assert result.exit_code == 1
    assert "terminal not reusable" in result.output
    events = [
        json.loads(line)
        for line in (state_mod.logs_dir(tmp_path) / "events.jsonl")
        .read_text(encoding="utf-8").splitlines()
    ]
    assert any(e["event"] == "orchestration_launch_failed" for e in events)


def test_del_supervised_release_failure_falls_back_to_close(
    tmp_path, monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    state_mod.write_agent(tmp_path, {
        "agent_id": "misc_001",
        "terminal": "t1",
        "state": "done",
        "launch_mode": "supervised",
        "dispatch_id": "ctx_1",
    })
    release = AsyncMock(side_effect=OrcaError("release refused", 1))
    close = AsyncMock(return_value={})

    with patch("osw.cli.orchestration_worker_release", release), \
         patch("osw.cli.terminal_close", close):
        result = runner.invoke(app, ["del", "misc_001", "--close"])

    assert result.exit_code == 0
    assert "release refused" in result.output
    close.assert_awaited_once_with("t1")
    assert state_mod.list_agents(tmp_path) == {}


def test_new_detects_caller_from_orca_env(tmp_path, monkeypatch):
    """Orca exports ORCA_TERMINAL_HANDLE into every terminal it creates;
    osw invoked through an agent's tool pipeline (stdout is a pipe, the
    preview-marker trick cannot work) still finds its caller from it."""
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term-host")

    create = AsyncMock(return_value={"result": {"terminal": {"handle": "term-new"}}})
    show = AsyncMock(return_value={"result": {"terminal": {"handle": "term-host"}}})
    with patch("osw.cli.terminal_create", create), \
         patch("osw.cli.terminal_show", show), \
         patch("osw.cli._spawn_watcher", Mock(return_value=4567)):
        result = runner.invoke(app, ["new", "claude", "do the task"])

    assert result.exit_code == 0
    agent = state_mod.read_agent(tmp_path, "agent_001")
    assert agent["caller_terminal"] == "term-host"


def test_explicit_caller_terminal_beats_orca_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term-host")

    create = AsyncMock(return_value={"result": {"terminal": {"handle": "term-new"}}})
    with patch("osw.cli.terminal_create", create), \
         patch("osw.cli._spawn_watcher", Mock(return_value=4567)):
        result = runner.invoke(
            app,
            ["new", "claude", "--caller-terminal", "caller-1", "do the task"],
        )

    assert result.exit_code == 0
    agent = state_mod.read_agent(tmp_path, "agent_001")
    assert agent["caller_terminal"] == "caller-1"


def test_new_fails_loudly_when_caller_unknown(tmp_path, monkeypatch):
    """No env handle and no TTY (agent tool pipeline): dispatch is rejected
    instead of silently creating an agent that can never report back."""
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)

    create = AsyncMock(return_value={"result": {"terminal": {"handle": "term-new"}}})
    with patch("osw.cli.terminal_create", create), \
         patch("osw.cli._spawn_watcher", Mock(return_value=4567)):
        result = runner.invoke(app, ["new", "claude", "do the task"])

    assert result.exit_code == 1
    assert "could not identify the caller terminal" in result.output
    assert "--no-notify" in result.output
    create.assert_not_awaited()
    assert not list(tmp_path.glob(".orca/osw/agents/*.json"))


def test_new_no_notify_proceeds_with_warning(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)

    create = AsyncMock(return_value={"result": {"terminal": {"handle": "term-new"}}})
    with patch("osw.cli.terminal_create", create), \
         patch("osw.cli._spawn_watcher", Mock(return_value=4567)):
        result = runner.invoke(app, ["new", "claude", "--no-notify", "do the task"])

    assert result.exit_code == 0
    assert "completion will NOT be reported" in result.output
    agent = state_mod.read_agent(tmp_path, "agent_001")
    assert agent["caller_terminal"] is None


def test_new_stale_env_handle_falls_back_instead_of_erroring(tmp_path, monkeypatch):
    """A stale ORCA_TERMINAL_HANDLE used to abort dispatch with
    terminal_handle_stale; now it is dropped with a warning."""
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term-gone")

    show = AsyncMock(
        side_effect=OrcaError("terminal handle is stale", 1, code="terminal_handle_stale")
    )
    create = AsyncMock(return_value={"result": {"terminal": {"handle": "term-new"}}})
    with patch("osw.cli.terminal_show", show), \
         patch("osw.cli.terminal_create", create), \
         patch("osw.cli._spawn_watcher", Mock(return_value=4567)):
        result = runner.invoke(app, ["new", "claude", "--no-notify", "do the task"])

    assert result.exit_code == 0
    assert "term-gone" in result.output
    assert "stale" in result.output
    assert os.environ.get("ORCA_TERMINAL_HANDLE") is None
    agent = state_mod.read_agent(tmp_path, "agent_001")
    assert agent["caller_terminal"] is None


def test_new_keeps_env_handle_on_transient_orca_error(tmp_path, monkeypatch):
    """Only terminal_handle_stale drops the handle; other failures (e.g.
    Orca unreachable) keep it so the real error surfaces later."""
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term-host")

    show = AsyncMock(side_effect=OrcaError("connection refused", 1))
    create = AsyncMock(return_value={"result": {"terminal": {"handle": "term-new"}}})
    with patch("osw.cli.terminal_show", show), \
         patch("osw.cli.terminal_create", create), \
         patch("osw.cli._spawn_watcher", Mock(return_value=4567)):
        result = runner.invoke(app, ["new", "claude", "do the task"])

    assert result.exit_code == 0
    agent = state_mod.read_agent(tmp_path, "agent_001")
    assert agent["caller_terminal"] == "term-host"


def test_spawn_watcher_hides_detached_process_on_windows(tmp_path):
    hidden_flags = 0x100
    startupinfo = object()
    with patch("osw.cli.os.name", "nt"), \
         patch("osw.cli.sys.executable", "python.exe"), \
         patch("osw.cli.hidden_subprocess_kwargs",
               return_value={"creationflags": hidden_flags, "startupinfo": startupinfo}), \
         patch("osw.cli.subprocess.Popen") as popen:
        popen.return_value.pid = 42

        pid = _spawn_watcher(tmp_path, "agent_001")

    assert pid == 42
    cmd = popen.call_args.args[0]
    assert cmd[0] == "python.exe"
    assert cmd[2:] == ["watch", str(tmp_path), "agent_001"]
    kwargs = popen.call_args.kwargs
    assert kwargs["startupinfo"] is startupinfo
    assert kwargs["creationflags"] & hidden_flags
    detached = getattr(subprocess, "DETACHED_PROCESS", 0)
    new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    if detached:
        assert kwargs["creationflags"] & detached
    if new_group:
        assert kwargs["creationflags"] & new_group


def test_new_rejects_unknown_provider(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)

    result = runner.invoke(app, ["new", "gemini", "do the task"])

    assert result.exit_code == 1
    assert "unsupported provider 'gemini'" in result.output


def test_use_adopts_terminal_with_terminal_show_result_unwrap(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)

    show = AsyncMock(return_value={
        "result": {
            "terminal": {
                "handle": "term-existing",
                "worktreePath": str(tmp_path),
            }
        }
    })
    spawn = Mock(return_value=9876)
    with patch("osw.cli.terminal_show", show), \
         patch("osw.cli._spawn_watcher", spawn):
        result = runner.invoke(
            app,
            ["use", "term-existing", "--prefix", "debug", "--no-notify", "continue this"],
        )

    assert result.exit_code == 0
    assert "Adopted debug_001" in result.output
    show.assert_awaited_once_with("term-existing")
    spawn.assert_called_once_with(tmp_path, "debug_001")
    agent = state_mod.read_agent(tmp_path, "debug_001")
    assert agent["terminal"] == "term-existing"
    assert agent["prompt"] == "continue this"
    assert agent["watcher_pid"] == 9876
    assert agent["origin"] == "use"


def test_use_accepts_existing_agent_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    state_mod.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-existing",
        "worktree_path": str(tmp_path),
        "provider": "claude",
        "provider_command": "claude",
        "model_name": "",
        "thinking": "",
        "task_started_on_launch": True,
        "state": "done",
        "phase": "",
        "caller_terminal": None,
        "prompt": "old task",
        "created_at": "2026-07-09T00:00:00+00:00",
        "updated_at": "2026-07-09T00:00:00+00:00",
    })

    show = AsyncMock(return_value={
        "result": {
            "terminal": {
                "handle": "term-existing",
                "worktreePath": str(tmp_path),
            }
        }
    })
    spawn = Mock(return_value=9876)
    with patch("osw.cli.terminal_show", show), \
         patch("osw.cli._spawn_watcher", spawn):
        result = runner.invoke(app, ["use", "agent_001", "--no-notify", "continue this"])

    assert result.exit_code == 0
    assert "Reused agent_001" in result.output
    show.assert_awaited_once_with("term-existing")
    spawn.assert_called_once_with(tmp_path, "agent_001")
    agent = state_mod.read_agent(tmp_path, "agent_001")
    assert agent["terminal"] == "term-existing"
    assert agent["prompt"] == "continue this"
    assert agent["provider"] == "claude"
    # A legacy record without an origin is attributed to this `use`.
    assert agent["origin"] == "use"


def test_use_supervised_agent_starts_followup_dispatch(tmp_path, monkeypatch):
    """Continuing a supervised agent reuses its proven terminal for a
    second Dispatch instead of the classic send-and-watch path."""
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    state = state_mod.read_state(tmp_path)
    state["orchestration_run_id"] = "run_abc"
    state_mod.write_state(tmp_path, state)
    state_mod.write_agent(tmp_path, {
        "agent_id": "misc_001",
        "terminal": "term-sup",
        "worktree_path": str(tmp_path),
        "provider": "omp",
        "provider_command": "omp --auto-approve",
        "model_name": "",
        "thinking": "",
        "state": "done",
        "caller_terminal": None,
        "prompt": "old task",
        "launch_mode": "supervised",
        "dispatch_id": "ctx_old",
        "task_id": "task_old",
        "run_id": "run_abc",
        "handoff_path": "old.md",
    })
    show = AsyncMock(return_value={"result": {"terminal": {
        "handle": "term-sup",
        "worktreePath": str(tmp_path),
    }}})
    run_current = AsyncMock(return_value={"run": {"id": "run_abc"}})
    start = AsyncMock(return_value=_supervised_start_result())
    worker_show = AsyncMock(return_value=_worker_show_result())
    spawn = Mock(return_value=4242)

    with patch("osw.cli.terminal_show", show), \
         patch("osw.cli.orchestration_run_current", run_current), \
         patch("osw.cli.orchestration_worker_start", start), \
         patch("osw.cli.orchestration_worker_show", worker_show), \
         patch("osw.cli._spawn_watcher", spawn):
        result = runner.invoke(
            app, ["use", "misc_001", "--no-notify", "继续修复"],
        )

    assert result.exit_code == 0
    assert "Reused misc_001" in result.output
    assert "dispatch=ctx_1" in result.output
    start_kwargs = start.await_args.kwargs
    assert start_kwargs["terminal"] == "term-sup"
    assert start_kwargs["worktree"] == f"path:{tmp_path}"
    assert "继续修复" in start_kwargs["spec"]
    spawn.assert_called_once_with(tmp_path, "misc_001")

    agent = state_mod.read_agent(tmp_path, "misc_001")
    assert agent["launch_mode"] == "supervised"
    assert agent["origin"] == "use"
    assert agent["dispatch_id"] == "ctx_1"
    assert agent["task_id"] == "task_1"
    assert agent["state"] == "assigned"
    assert agent["handoff_path"].endswith(".md")
    assert agent["handoff_path"] != "old.md"


def test_use_existing_agent_id_reuses_same_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    state_mod.write_agent(tmp_path, {
        "agent_id": "research_099",
        "terminal": "term-existing",
        "worktree_path": str(tmp_path),
        "state": "done",
        "prompt": "old research",
    })
    state_mod.write_agent(tmp_path, {
        "agent_id": "research_100",
        "terminal": "term-other",
        "worktree_path": str(tmp_path),
        "state": "done",
        "prompt": "other research",
    })

    show = AsyncMock(return_value={
        "result": {
            "terminal": {
                "handle": "term-existing",
                "worktreePath": str(tmp_path),
            }
        }
    })
    spawn = Mock(return_value=9876)
    with patch("osw.cli.terminal_show", show), \
         patch("osw.cli._spawn_watcher", spawn):
        result = runner.invoke(app, ["use", "research_099", "--no-notify", "continue this"])

    assert result.exit_code == 0
    assert "Reused research_099" in result.output
    show.assert_awaited_once_with("term-existing")
    spawn.assert_called_once_with(tmp_path, "research_099")
    agent = state_mod.read_agent(tmp_path, "research_099")
    assert agent["terminal"] == "term-existing"
    assert agent["prompt"] == "continue this"


def test_use_existing_agent_id_rejects_prefix(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    state_mod.write_agent(tmp_path, {
        "agent_id": "research_001",
        "terminal": "term-existing",
        "worktree_path": str(tmp_path),
        "state": "done",
        "prompt": "old research",
    })

    show = AsyncMock(return_value={})
    with patch("osw.cli.terminal_show", show):
        result = runner.invoke(
            app,
            ["use", "research_001", "--prefix", "debug", "continue this"],
        )

    assert result.exit_code == 1
    assert "--prefix is only valid when adopting a terminal handle" in result.output
    show.assert_not_awaited()


def test_use_existing_agent_id_rejects_unfinished_agent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    state_mod.write_agent(tmp_path, {
        "agent_id": "research_001",
        "terminal": "term-existing",
        "worktree_path": str(tmp_path),
        "state": "working",
        "prompt": "old research",
    })

    show = AsyncMock(return_value={})
    with patch("osw.cli.terminal_show", show):
        result = runner.invoke(app, ["use", "research_001", "continue this"])

    assert result.exit_code == 1
    assert "agent 'research_001' is not finished" in result.output
    show.assert_not_awaited()


def test_use_rejects_wrong_worktree(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    other = tmp_path / "other"
    other.mkdir()

    show = AsyncMock(return_value={
        "result": {"terminal": {"handle": "term-x", "worktreePath": str(other)}}
    })
    with patch("osw.cli.terminal_show", show):
        result = runner.invoke(app, ["use", "term-x", "task"])

    assert result.exit_code == 1
    assert "does not match project root" in result.output
    assert state_mod.list_agents(tmp_path) == {}


def test_use_recovers_stale_handle_via_pane_identity(tmp_path, monkeypatch):
    """A done agent whose runtime-scoped handle went stale (Orca runtime
    restarted) must be re-resolved by its stable pane identity and
    adopted, instead of failing with terminal_handle_stale."""
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    state_mod.write_agent(tmp_path, {
        "agent_id": "agent_001",
        "terminal": "term-old",
        "pane_key": "tab-a:leaf-b",
        "worktree_path": str(tmp_path),
        "state": "done",
        "prompt": "old task",
    })
    stale = OrcaError(
        "Terminal handle belongs to an older runtime",
        1,
        code="terminal_handle_stale",
    )
    show = AsyncMock(side_effect=[
        stale,
        {"result": {"terminal": {
            "handle": "term-new",
            "worktreePath": str(tmp_path),
            "tabId": "tab-a",
            "leafId": "leaf-b",
        }}},
    ])
    terminals = AsyncMock(return_value=[{
        "handle": "term-new", "tabId": "tab-a", "leafId": "leaf-b",
    }])
    spawn = Mock(return_value=9876)
    with patch("osw.cli.terminal_show", show), \
         patch("osw.cli.terminal_list", terminals), \
         patch("osw.cli._spawn_watcher", spawn):
        result = runner.invoke(app, ["use", "agent_001", "--no-notify", "continue this"])

    assert result.exit_code == 0
    assert "Reused agent_001 on terminal term-new" in result.output
    assert [call.args[0] for call in show.await_args_list] == [
        "term-old", "term-new",
    ]
    agent = state_mod.read_agent(tmp_path, "agent_001")
    assert agent["terminal"] == "term-new"
    spawn.assert_called_once_with(tmp_path, "agent_001")


def test_use_stale_handle_without_pane_still_fails_loudly(tmp_path, monkeypatch):
    """No recorded pane identity means nothing to rebind to: keep the
    original error-and-exit behavior."""
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    stale = OrcaError(
        "Terminal handle belongs to an older runtime",
        1,
        code="terminal_handle_stale",
    )
    show = AsyncMock(side_effect=stale)
    with patch("osw.cli.terminal_show", show):
        result = runner.invoke(app, ["use", "term-gone", "--no-notify", "task"])

    assert result.exit_code == 1
    assert "older runtime" in result.output
    assert state_mod.list_agents(tmp_path) == {}


def test_del_removes_agent_kills_watcher_and_optionally_closes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    state_mod.write_agent(
        tmp_path,
        {"agent_id": "agent_001", "terminal": "t1", "watcher_pid": 1234},
    )

    close = AsyncMock(return_value={})
    with patch("osw.cli._terminate_pid") as terminate, \
         patch("osw.cli.terminal_close", close):
        result = runner.invoke(app, ["del", "agent_001", "--close"])

    assert result.exit_code == 0
    terminate.assert_called_once_with(1234)
    close.assert_awaited_once_with("t1")
    assert state_mod.list_agents(tmp_path) == {}


def test_list_merges_agent_files_with_worktree_ps_and_watcher_liveness(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    state_mod.write_agent(
        tmp_path,
        {
            "agent_id": "agent_001",
            "terminal": "t1",
            "state": "working",
            "prompt": "Fix the bug",
            "pane_key": "tab:leaf",
            "watcher_pid": 1234,
        },
    )
    ps = AsyncMock(return_value=[
        {"agents": [{"paneKey": "tab:leaf", "state": "done", "toolName": ""}]}
    ])
    with patch("osw.cli.worktree_ps", ps), \
         patch("osw.cli.pid_is_running", return_value=True):
        result = runner.invoke(app, ["list", "--json"])

    assert result.exit_code == 0
    agents = json.loads(result.output)
    assert agents["agent_001"]["orca_state"] == "done"
    assert agents["agent_001"]["watcher_running"] is True
    assert agents["agent_001"]["prompt"] == "Fix the bug"


def test_models_command_is_read_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    before = state_mod.read_state(tmp_path)
    variants = [{"name": "pi-gpt-5", "command": "pi --model openai/gpt-5"}]

    with patch("osw.cli.probe_variants", return_value=variants):
        result = runner.invoke(app, ["models", "pi"])

    assert result.exit_code == 0
    assert "pi --model openai/gpt-5" in result.output
    assert state_mod.read_state(tmp_path) == before


def test_models_command_outputs_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    variants = [{"name": "pi-gpt-5", "command": "pi --model openai/gpt-5"}]

    with patch("osw.cli.probe_variants", return_value=variants):
        result = runner.invoke(app, ["models", "pi", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.output) == variants


def test_init_non_interactive_never_prompts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    found = [{"name": "codex", "path": "C:\\bin\\codex.CMD", "version": ""}]
    # No input provided: default init must complete without blocking
    with patch("osw.cli.scan_agent_clis", return_value=found):
        result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert state_mod.read_state(tmp_path) == {"version": 3, "project_root": str(tmp_path)}
    assert "python osw.py models omp" in result.output


def test_jev_status_reports_global_switch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.setenv("OSW_JEV_MODE", "enforce")

    status = runner.invoke(app, ["jev", "status"])
    assert status.exit_code == 0
    assert "mode: enforce" in status.output
    assert "global: OSW_JEV_MODE=enforce" in status.output
    assert "enforce_providers: claude, omp" in status.output
    for label in ("api key:", "sdk:", "endpoint:", "proxy:"):
        assert label in status.output

    monkeypatch.delenv("OSW_JEV_MODE")
    unset = runner.invoke(app, ["jev", "status"])
    assert "mode: off" in unset.output
    assert "OSW_JEV_MODE is not set" in unset.output


def test_jev_status_check_distinguishes_probe_outcomes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)

    with patch("osw.cli.check_endpoint",
               return_value={"ok": True, "status": 200, "latency_ms": 12}):
        result = runner.invoke(app, ["jev", "status", "--check"])
    assert result.exit_code == 0
    assert "check: OK" in result.output

    with patch("osw.cli.check_endpoint",
               return_value={"ok": False, "status": 401, "latency_ms": 9,
                             "reason": "auth_failed"}):
        result = runner.invoke(app, ["jev", "status", "--check"])
    assert result.exit_code == 1
    assert "check: FAILED" in result.output
    assert "auth_failed" in result.output


def test_jev_commands_require_initialized_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for args in (["jev", "status"],):
        result = runner.invoke(app, args)
        assert result.exit_code == 1
        assert "Not initialized" in result.output


# ---------------------------------------------------------------------------
# Orphans, pane dedup and stale handles (plan 1.1)
# ---------------------------------------------------------------------------

def _stale_handle_error() -> OrcaError:
    return OrcaError(
        "Terminal handle belongs to an older runtime",
        1,
        code="terminal_handle_stale",
    )


def _show_pane(tmp_path, handle="term-x", tab="tab-a", leaf="leaf-b") -> dict:
    return {"result": {"terminal": {
        "handle": handle,
        "worktreePath": str(tmp_path),
        "tabId": tab,
        "leafId": leaf,
    }}}


def test_list_marks_orphaned_agent_lost_and_writes_back(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    for agent_id, agent_state, pid in [
        ("agent_001", "working", 1234),   # watcher died mid-task -> lost
        ("agent_002", "done", 1234),      # settled: untouched
        ("agent_003", "enrolled", 0),     # enrolled has no watcher by design
        ("agent_004", "assigned", 0),     # watcher not spawned yet: untouched
    ]:
        record = {"agent_id": agent_id, "terminal": "t", "state": agent_state}
        if pid:
            record["watcher_pid"] = pid
        state_mod.write_agent(tmp_path, record)

    with patch("osw.cli.worktree_ps", AsyncMock(return_value=[])), \
         patch("osw.cli.pid_is_running", return_value=False):
        result = runner.invoke(app, ["list", "--json"])

    assert result.exit_code == 0
    rows = json.loads(result.output)
    assert rows["agent_001"]["state"] == "lost"
    assert rows["agent_001"]["watcher_running"] is False
    assert state_mod.read_agent(tmp_path, "agent_001")["state"] == "lost"
    assert state_mod.read_agent(tmp_path, "agent_002")["state"] == "done"
    assert state_mod.read_agent(tmp_path, "agent_003")["state"] == "enrolled"
    assert state_mod.read_agent(tmp_path, "agent_004")["state"] == "assigned"


def test_list_keeps_working_agent_with_live_watcher(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    state_mod.write_agent(tmp_path, {
        "agent_id": "agent_001", "terminal": "t", "state": "working",
        "watcher_pid": 1234,
    })

    with patch("osw.cli.worktree_ps", AsyncMock(return_value=[])), \
         patch("osw.cli.pid_is_running", return_value=True):
        result = runner.invoke(app, ["list", "--json"])

    assert json.loads(result.output)["agent_001"]["state"] == "working"
    assert state_mod.read_agent(tmp_path, "agent_001")["state"] == "working"


def test_list_does_not_clobber_final_state_written_as_watcher_exits(
    tmp_path, monkeypatch,
):
    """The watcher writes its final state and then exits; a listing that
    read the record before that write must not overwrite it with lost."""
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    record = {
        "agent_id": "agent_001", "terminal": "t", "state": "working",
        "watcher_pid": 1234,
    }
    state_mod.write_agent(tmp_path, record)

    def watcher_finishes(pid):
        state_mod.write_agent(tmp_path, {**record, "state": "done"})
        return False

    with patch("osw.cli.worktree_ps", AsyncMock(return_value=[])), \
         patch("osw.cli.pid_is_running", side_effect=watcher_finishes):
        result = runner.invoke(app, ["list", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.output)["agent_001"]["state"] == "done"
    assert state_mod.read_agent(tmp_path, "agent_001")["state"] == "done"


def test_status_counts_orphaned_agent_as_lost(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    state_mod.write_agent(tmp_path, {
        "agent_id": "agent_001", "terminal": "t", "state": "working",
        "watcher_pid": 1234,
    })

    with patch("osw.cli.worktree_ps", AsyncMock(return_value=[])), \
         patch("osw.cli.pid_is_running", return_value=False):
        result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "lost: 1" in result.output
    assert "Watchers running: 0" in result.output
    assert state_mod.read_agent(tmp_path, "agent_001")["state"] == "lost"


def test_use_handle_rejects_pane_managed_by_live_watcher(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    state_mod.write_agent(tmp_path, {
        "agent_id": "code_001", "terminal": "term-x", "pane_key": "tab-a:leaf-b",
        "state": "working", "watcher_pid": 1234,
    })

    spawn = Mock(return_value=9876)
    with patch("osw.cli.terminal_show", AsyncMock(return_value=_show_pane(tmp_path))), \
         patch("osw.cli.pid_is_running", return_value=True), \
         patch("osw.cli._spawn_watcher", spawn):
        result = runner.invoke(app, ["use", "term-x", "--no-notify", "task"])

    assert result.exit_code == 1
    assert "code_001" in result.output
    spawn.assert_not_called()
    assert set(state_mod.list_agents(tmp_path)) == {"code_001"}


def test_use_handle_on_recorded_pane_without_watcher_points_to_agent_id(
    tmp_path, monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    state_mod.write_agent(tmp_path, {
        "agent_id": "code_001", "terminal": "term-x", "pane_key": "tab-a:leaf-b",
        "state": "done", "watcher_pid": 1234,
    })

    spawn = Mock(return_value=9876)
    with patch("osw.cli.terminal_show", AsyncMock(return_value=_show_pane(tmp_path))), \
         patch("osw.cli.pid_is_running", return_value=False), \
         patch("osw.cli._spawn_watcher", spawn):
        result = runner.invoke(app, ["use", "term-x", "--no-notify", "task"])

    assert result.exit_code == 1
    assert "use code_001" in result.output
    spawn.assert_not_called()
    assert set(state_mod.list_agents(tmp_path)) == {"code_001"}


def test_use_handle_on_unrecorded_pane_still_adopts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    state_mod.write_agent(tmp_path, {
        "agent_id": "code_001", "terminal": "term-y", "pane_key": "tab-a:leaf-other",
        "state": "working", "watcher_pid": 1234,
    })

    spawn = Mock(return_value=9876)
    with patch("osw.cli.terminal_show", AsyncMock(return_value=_show_pane(tmp_path))), \
         patch("osw.cli.pid_is_running", return_value=True), \
         patch("osw.cli._spawn_watcher", spawn):
        result = runner.invoke(app, ["use", "term-x", "--no-notify", "task"])

    assert result.exit_code == 0
    assert "Adopted agent_001" in result.output
    assert state_mod.read_agent(tmp_path, "agent_001")["pane_key"] == "tab-a:leaf-b"


def test_use_agent_id_accepts_enrolled_agent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    state_mod.write_agent(tmp_path, {
        "agent_id": "claude_001", "terminal": "term-x", "pane_key": "tab-a:leaf-b",
        "worktree_path": str(tmp_path), "provider": "claude",
        "origin": "enroll", "state": "enrolled", "support": "full",
        "session_id": "sess-1",
    })

    spawn = Mock(return_value=9876)
    with patch("osw.cli.terminal_show", AsyncMock(return_value=_show_pane(tmp_path))), \
         patch("osw.cli._spawn_watcher", spawn):
        result = runner.invoke(app, ["use", "claude_001", "--no-notify", "do it"])

    assert result.exit_code == 0
    assert "Reused claude_001" in result.output
    spawn.assert_called_once_with(tmp_path, "claude_001")
    agent = state_mod.read_agent(tmp_path, "claude_001")
    assert agent["prompt"] == "do it"
    assert agent["provider"] == "claude"
    # Reuse keeps how the record came to exist and what enroll learned.
    assert agent["origin"] == "enroll"
    assert agent["support"] == "full"
    assert agent["session_id"] == "sess-1"


def test_use_agent_id_accepts_orphaned_agent_as_lost(tmp_path, monkeypatch):
    """`use <handle>` on a dead-watcher pane points at `use <agent_id>`;
    that must work even when no list/status ran in between."""
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    state_mod.write_agent(tmp_path, {
        "agent_id": "code_001", "terminal": "term-x", "pane_key": "tab-a:leaf-b",
        "worktree_path": str(tmp_path), "state": "working", "watcher_pid": 1234,
    })

    spawn = Mock(return_value=9876)
    with patch("osw.cli.terminal_show", AsyncMock(return_value=_show_pane(tmp_path))), \
         patch("osw.cli.pid_is_running", return_value=False), \
         patch("osw.cli._spawn_watcher", spawn):
        result = runner.invoke(app, ["use", "code_001", "--no-notify", "again"])

    assert result.exit_code == 0
    assert "Reused code_001" in result.output
    spawn.assert_called_once_with(tmp_path, "code_001")


def test_use_agent_id_rejects_agent_with_live_watcher(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    state_mod.write_agent(tmp_path, {
        "agent_id": "code_001", "terminal": "term-x", "state": "working",
        "watcher_pid": 1234,
    })

    show = AsyncMock(return_value={})
    with patch("osw.cli.terminal_show", show), \
         patch("osw.cli.pid_is_running", return_value=True):
        result = runner.invoke(app, ["use", "code_001", "again"])

    assert result.exit_code == 1
    assert "agent 'code_001' is not finished" in result.output
    show.assert_not_awaited()
    assert state_mod.read_agent(tmp_path, "code_001")["state"] == "working"


def test_del_close_rebinds_stale_handle_by_pane_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    state_mod.write_agent(tmp_path, {
        "agent_id": "agent_001", "terminal": "term-old", "pane_key": "tab-a:leaf-b",
        "state": "done",
    })

    close = AsyncMock(side_effect=[_stale_handle_error(), {}])
    terminals = AsyncMock(return_value=[
        {"handle": "term-other", "tabId": "tab-a", "leafId": "leaf-z"},
        {"handle": "term-new", "tabId": "tab-a", "leafId": "leaf-b"},
    ])
    with patch("osw.cli.terminal_close", close), \
         patch("osw.cli.terminal_list", terminals):
        result = runner.invoke(app, ["del", "agent_001", "--close"])

    assert result.exit_code == 0
    assert "Warning" not in result.output
    assert [call.args[0] for call in close.await_args_list] == ["term-old", "term-new"]
    terminals.assert_awaited_once_with(f"path:{tmp_path}")
    assert state_mod.list_agents(tmp_path) == {}


def test_del_close_stale_handle_without_live_pane_warns(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    state_mod.write_agent(tmp_path, {
        "agent_id": "agent_001", "terminal": "term-old", "pane_key": "tab-a:leaf-b",
        "state": "done",
    })

    close = AsyncMock(side_effect=_stale_handle_error())
    with patch("osw.cli.terminal_close", close), \
         patch("osw.cli.terminal_list", AsyncMock(return_value=[])):
        result = runner.invoke(app, ["del", "agent_001", "--close"])

    assert result.exit_code == 0
    assert "Warning: failed to close terminal" in result.output
    close.assert_awaited_once_with("term-old")
    assert state_mod.list_agents(tmp_path) == {}


def test_del_close_non_stale_error_does_not_rebind(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    state_mod.write_agent(tmp_path, {
        "agent_id": "agent_001", "terminal": "term-old", "pane_key": "tab-a:leaf-b",
        "state": "done",
    })

    close = AsyncMock(side_effect=OrcaError("boom", 1, code="runtime_unavailable"))
    terminals = AsyncMock(return_value=[])
    with patch("osw.cli.terminal_close", close), \
         patch("osw.cli.terminal_list", terminals):
        result = runner.invoke(app, ["del", "agent_001", "--close"])

    assert result.exit_code == 0
    assert "Warning: failed to close terminal: boom" in result.output
    terminals.assert_not_awaited()


def test_del_supervised_fallback_close_rebinds_stale_handle(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    state_mod.write_agent(tmp_path, {
        "agent_id": "misc_001", "terminal": "term-old", "pane_key": "tab-a:leaf-b",
        "state": "done", "launch_mode": "supervised", "dispatch_id": "ctx_1",
    })

    release = AsyncMock(side_effect=OrcaError("gone", 1, code="dispatch_not_found"))
    close = AsyncMock(side_effect=[_stale_handle_error(), {}])
    terminals = AsyncMock(return_value=[
        {"handle": "term-new", "tabId": "tab-a", "leafId": "leaf-b"},
    ])
    with patch("osw.cli.orchestration_worker_release", release), \
         patch("osw.cli.terminal_close", close), \
         patch("osw.cli.terminal_list", terminals):
        result = runner.invoke(app, ["del", "misc_001", "--close"])

    assert result.exit_code == 0
    assert "failed to close terminal" not in result.output
    assert [call.args[0] for call in close.await_args_list] == ["term-old", "term-new"]


# ---------------------------------------------------------------------------
# enroll (plan 1.3)
# ---------------------------------------------------------------------------

def _term(tmp_path, handle, tab, leaf, identity=None, worktree=None) -> dict:
    entry = {
        "handle": handle,
        "tabId": tab,
        "leafId": leaf,
        "worktreePath": str(worktree or tmp_path),
    }
    if identity is not None:
        entry["agentIdentity"] = identity
    return entry


def _enroll_fixture(tmp_path):
    """Terminal list + ps snapshot covering every enroll branch."""
    other = tmp_path / "other"
    other.mkdir(exist_ok=True)
    terminals = [
        _term(tmp_path, "term-me", "tab", "me", identity="claude"),
        _term(tmp_path, "term-known", "tab", "known", identity="claude"),
        _term(tmp_path, "term-claude", "tab", "claude", identity="claude"),
        _term(tmp_path, "term-codex", "tab", "codex"),
        _term(tmp_path, "term-aider", "tab", "aider", identity="aider"),
        _term(tmp_path, "term-shell", "tab", "shell"),
        # Orca names a DSH pane after the launcher; enroll maps it to `dsh`.
        _term(tmp_path, "term-dsh", "tab", "dsh"),
        _term(tmp_path, "term-far", "tab", "far", identity="claude", worktree=other),
    ]
    ps = [{"agents": [
        {"paneKey": "tab:codex", "agentType": "codex", "state": "working"},
        {"paneKey": "tab:far", "agentType": "claude", "state": "done"},
        {"paneKey": "tab:dsh", "agentType": "dsh-tui", "state": "done"},
    ]}]
    return terminals, ps


def _me_show(tmp_path, handle="term-me", tab="tab", leaf="me") -> AsyncMock:
    """`terminal show` for the executor terminal (ORCA_TERMINAL_HANDLE)."""
    return AsyncMock(return_value=_show_pane(tmp_path, handle, tab, leaf))


def _session_ids(pane_key: str) -> str:
    return {"tab:claude": "sess-claude", "tab:codex": "sess-codex"}.get(pane_key, "")


def test_enroll_registers_unrecorded_agent_terminals(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term-me")
    state_mod.write_agent(tmp_path, {
        "agent_id": "code_001", "terminal": "term-known", "pane_key": "tab:known",
        "state": "done",
    })
    terminals, ps = _enroll_fixture(tmp_path)

    terminal_list = AsyncMock(return_value=terminals)
    spawn = Mock(return_value=1)
    with patch("osw.cli.terminal_list", terminal_list), \
         patch("osw.cli.terminal_show", _me_show(tmp_path)), \
         patch("osw.cli.worktree_ps", AsyncMock(return_value=ps)), \
         patch("osw.cli.session_for_pane", side_effect=_session_ids), \
         patch("osw.cli._spawn_watcher", spawn):
        result = runner.invoke(app, ["enroll"])

    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    terminal_list.assert_awaited_once_with(f"path:{tmp_path}")
    spawn.assert_not_called()

    assert [a["agent_id"] for a in out["enrolled"]] == [
        "claude_001", "codex_001", "aider_001", "dsh_001",
    ]
    assert out["skipped"] == [
        {"terminal": "term-me", "reason": "caller_terminal"},
        {"terminal": "term-known", "reason": "already_recorded"},
        {"terminal": "term-shell", "reason": "no_agent"},
    ]

    claude = state_mod.read_agent(tmp_path, "claude_001")
    assert claude["origin"] == "enroll"
    assert claude["state"] == "enrolled"
    assert claude["provider"] == "claude"
    assert claude["terminal"] == "term-claude"
    assert claude["pane_key"] == "tab:claude"
    assert claude["support"] == "full"
    assert "support_reason" not in claude
    assert claude["session_id"] == "sess-claude"
    assert claude["worktree_path"] == str(tmp_path)
    assert "watcher_pid" not in claude

    codex = state_mod.read_agent(tmp_path, "codex_001")
    assert codex["provider"] == "codex"   # from worktree ps agentType
    assert codex["support"] == "full"
    assert codex["session_id"] == "sess-codex"

    aider = state_mod.read_agent(tmp_path, "aider_001")
    assert aider["support"] == "guard_only"
    assert aider["support_reason"] == "unsupported_provider"
    assert aider["session_id"] == ""

    dsh = state_mod.read_agent(tmp_path, "dsh_001")
    assert dsh["provider"] == "dsh"       # Orca's `dsh-tui` -> OSW `dsh`
    assert dsh["support"] == "full"

    assert set(state_mod.list_agents(tmp_path)) == {
        "code_001", "claude_001", "codex_001", "aider_001", "dsh_001",
    }


def test_enroll_prefers_agent_identity_over_ps_agent_type(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    terminals = [_term(tmp_path, "term-1", "tab", "one", identity="omp")]
    ps = [{"agents": [{"paneKey": "tab:one", "agentType": "pi"}]}]

    with patch("osw.cli.terminal_list", AsyncMock(return_value=terminals)), \
         patch("osw.cli.worktree_ps", AsyncMock(return_value=ps)), \
         patch("osw.cli.session_for_pane", return_value=""):
        result = runner.invoke(app, ["enroll"])

    assert result.exit_code == 0
    out = json.loads(result.stdout)
    assert [a["agent_id"] for a in out["enrolled"]] == ["omp_001"]
    assert state_mod.read_agent(tmp_path, "omp_001")["provider"] == "omp"


def test_enroll_twice_skips_everything_already_recorded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term-me")
    terminals, ps = _enroll_fixture(tmp_path)

    with patch("osw.cli.terminal_list", AsyncMock(return_value=terminals)), \
         patch("osw.cli.terminal_show", _me_show(tmp_path)), \
         patch("osw.cli.worktree_ps", AsyncMock(return_value=ps)), \
         patch("osw.cli.session_for_pane", side_effect=_session_ids):
        first = json.loads(runner.invoke(app, ["enroll"]).stdout)
        second = json.loads(runner.invoke(app, ["enroll"]).stdout)

    assert len(first["enrolled"]) == 5
    assert second["enrolled"] == []
    assert {s["terminal"]: s["reason"] for s in second["skipped"]} == {
        "term-me": "caller_terminal",
        "term-known": "already_recorded",
        "term-claude": "already_recorded",
        "term-codex": "already_recorded",
        "term-aider": "already_recorded",
        "term-dsh": "already_recorded",
        "term-shell": "no_agent",
    }
    assert len(state_mod.list_agents(tmp_path)) == 5


def test_enroll_skips_terminal_without_pane_identity(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    terminals = [{
        "handle": "term-1", "worktreePath": str(tmp_path), "agentIdentity": "claude",
    }]

    with patch("osw.cli.terminal_list", AsyncMock(return_value=terminals)), \
         patch("osw.cli.worktree_ps", AsyncMock(return_value=[])), \
         patch("osw.cli.session_for_pane", return_value=""):
        result = runner.invoke(app, ["enroll"])

    assert json.loads(result.stdout) == {
        "enrolled": [],
        "skipped": [{"terminal": "term-1", "reason": "no_pane_key"}],
    }
    assert state_mod.list_agents(tmp_path) == {}


def test_enroll_falls_back_to_agent_identity_when_ps_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    terminals = [
        _term(tmp_path, "term-1", "tab", "one", identity="claude"),
        _term(tmp_path, "term-2", "tab", "two"),
    ]

    with patch("osw.cli.terminal_list", AsyncMock(return_value=terminals)), \
         patch("osw.cli.worktree_ps", AsyncMock(side_effect=OrcaError("down", 1))), \
         patch("osw.cli.session_for_pane", return_value=""):
        result = runner.invoke(app, ["enroll"])

    assert result.exit_code == 0
    # The ps failure goes to the event stream; stdout stays pure JSON.
    out = json.loads(result.stdout)
    events = read_events(state_mod.logs_dir(tmp_path))
    assert "enroll_ps_failed" in [e["event"] for e in events]
    assert [a["agent_id"] for a in out["enrolled"]] == ["claude_001"]
    assert out["skipped"] == [{"terminal": "term-2", "reason": "no_agent"}]


def test_enroll_terminal_list_failure_exits_without_writing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)

    failing = AsyncMock(side_effect=OrcaError("runtime unavailable", 1))
    with patch("osw.cli.terminal_list", failing):
        result = runner.invoke(app, ["enroll"])

    assert result.exit_code == 1
    assert "runtime unavailable" in result.output
    assert state_mod.list_agents(tmp_path) == {}


def test_enroll_requires_initialized_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["enroll"])

    assert result.exit_code == 1
    assert "Not initialized" in result.output


def test_enroll_recognizes_executor_by_pane_not_handle_string(tmp_path, monkeypatch):
    """The executor's pane is skipped even when `terminal list` names it
    with a different handle string than ORCA_TERMINAL_HANDLE."""
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term-me-alias")
    terminals = [_term(tmp_path, "term-me", "tab", "me", identity="claude")]

    show = _me_show(tmp_path)
    with patch("osw.cli.terminal_list", AsyncMock(return_value=terminals)), \
         patch("osw.cli.terminal_show", show), \
         patch("osw.cli.worktree_ps", AsyncMock(return_value=[])), \
         patch("osw.cli.session_for_pane", return_value=""):
        result = runner.invoke(app, ["enroll"])

    show.assert_awaited_once_with("term-me-alias")
    assert json.loads(result.stdout) == {
        "enrolled": [],
        "skipped": [{"terminal": "term-me", "reason": "caller_terminal"}],
    }


def test_enroll_stale_executor_handle_warns_on_stderr_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term-gone")
    terminals = [_term(tmp_path, "term-1", "tab", "one", identity="claude")]

    with patch("osw.cli.terminal_list", AsyncMock(return_value=terminals)), \
         patch("osw.cli.terminal_show", AsyncMock(side_effect=_stale_handle_error())), \
         patch("osw.cli.worktree_ps", AsyncMock(return_value=[])), \
         patch("osw.cli.session_for_pane", return_value=""):
        result = runner.invoke(app, ["enroll"])

    assert result.exit_code == 0
    assert "is stale" in result.stderr
    out = json.loads(result.stdout)
    assert [a["agent_id"] for a in out["enrolled"]] == ["claude_001"]


def test_list_json_stdout_stays_json_when_ps_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    state_mod.write_agent(tmp_path, {
        "agent_id": "agent_001", "terminal": "t", "state": "done",
    })

    with patch("osw.cli.worktree_ps", AsyncMock(side_effect=OrcaError("down", 1))):
        result = runner.invoke(app, ["list", "--json"])

    assert result.exit_code == 0
    assert "worktree ps failed" in result.stderr
    assert list(json.loads(result.stdout)) == ["agent_001"]


def test_use_keeps_guardian_as_notify_target_without_caller_flag(
    tmp_path, monkeypatch,
):
    """A guarded agent already has a notification target, so `use`
    needs no caller terminal and must not drop the guardian."""
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    state_mod.write_agent(tmp_path, {
        "agent_id": "code_001", "terminal": "term-x", "pane_key": "tab-a:leaf-b",
        "worktree_path": str(tmp_path), "state": "done",
        "guardian": {"agent_id": "boss_001"},
    })

    spawn = Mock(return_value=9876)
    with patch("osw.cli.terminal_show", AsyncMock(return_value=_show_pane(tmp_path))), \
         patch("osw.cli._spawn_watcher", spawn):
        result = runner.invoke(app, ["use", "code_001", "next task"])

    assert result.exit_code == 0, result.output
    agent = state_mod.read_agent(tmp_path, "code_001")
    assert agent["guardian"] == {"agent_id": "boss_001"}
    assert agent["prompt"] == "next task"
    spawn.assert_called_once_with(tmp_path, "code_001")


def test_use_explicit_caller_terminal_replaces_guardian(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    state_mod.write_agent(tmp_path, {
        "agent_id": "code_001", "terminal": "term-x", "pane_key": "tab-a:leaf-b",
        "worktree_path": str(tmp_path), "state": "done",
        "guardian": {"agent_id": "boss_001"},
    })

    with patch("osw.cli.terminal_show", AsyncMock(return_value=_show_pane(tmp_path))), \
         patch("osw.cli._spawn_watcher", Mock(return_value=9876)):
        result = runner.invoke(
            app, ["use", "code_001", "--caller-terminal", "caller-1", "next task"],
        )

    assert result.exit_code == 0
    agent = state_mod.read_agent(tmp_path, "code_001")
    assert "guardian" not in agent
    assert agent["caller_terminal"] == "caller-1"


# ---------------------------------------------------------------------------
# guard (plan 1.6, CLI side)
# ---------------------------------------------------------------------------

def _guard_record(agent_id, state, pane, **extra) -> dict:
    return {
        "agent_id": agent_id, "terminal": f"term-{agent_id}", "pane_key": pane,
        "state": state, **extra,
    }


def test_guard_without_ids_guards_unguarded_settled_records(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term-me")
    for record in [
        _guard_record("a_001", "enrolled", "tab:a"),
        _guard_record("b_001", "done", "tab:b"),
        _guard_record("c_001", "error", "tab:c"),
        _guard_record("d_001", "working", "tab:d"),
        _guard_record("e_001", "done", "tab:e", guardian={"agent_id": "x_001"}),
        _guard_record("f_001", "lost", "tab:f"),
    ]:
        state_mod.write_agent(tmp_path, record)

    spawn = Mock(return_value=1)
    with patch("osw.cli.terminal_show", _me_show(tmp_path)), \
         patch("osw.cli.worktree_ps", AsyncMock(return_value=[{"agents": [
             {"paneKey": "tab:a", "state": "done"},
         ]}])), \
         patch("osw.cli.pid_is_running", return_value=False), \
         patch("osw.cli._spawn_watcher", spawn):
        result = runner.invoke(app, ["guard"])

    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    guardian = {"terminal": "term-me", "pane_key": "tab:me"}
    assert out["guardian"] == guardian
    assert out["guarded"] == [
        {"agent_id": "a_001", "action": "guardian_set"},
        {"agent_id": "b_001", "action": "guardian_set"},
        {"agent_id": "c_001", "action": "guardian_set"},
    ]
    assert out["skipped"] == []
    spawn.assert_not_called()
    for agent_id in ("a_001", "b_001", "c_001"):
        agent = state_mod.read_agent(tmp_path, agent_id)
        assert agent["guardian"] == guardian
        assert "follow_turn" not in agent
    assert state_mod.read_agent(tmp_path, "e_001")["guardian"] == {"agent_id": "x_001"}
    assert "guardian" not in state_mod.read_agent(tmp_path, "d_001")
    assert "guardian" not in state_mod.read_agent(tmp_path, "f_001")


def test_guard_executor_that_is_an_osw_agent_is_stored_by_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term-me")
    state_mod.write_agent(tmp_path, _guard_record("boss_001", "done", "tab:me"))
    state_mod.write_agent(tmp_path, _guard_record("b_001", "done", "tab:b"))

    with patch("osw.cli.terminal_show", _me_show(tmp_path)), \
         patch("osw.cli.worktree_ps", AsyncMock(return_value=[])), \
         patch("osw.cli.pid_is_running", return_value=False), \
         patch("osw.cli._spawn_watcher", Mock(return_value=1)):
        result = runner.invoke(app, ["guard"])

    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["guardian"] == {"agent_id": "boss_001"}
    assert out["guarded"] == [{"agent_id": "b_001", "action": "guardian_set"}]
    # The executor never guards itself.
    assert out["skipped"] == [{"agent_id": "boss_001", "reason": "self"}]
    assert state_mod.read_agent(tmp_path, "b_001")["guardian"] == {"agent_id": "boss_001"}
    assert "guardian" not in state_mod.read_agent(tmp_path, "boss_001")


def test_guard_restarts_live_watcher_in_follow_mode(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term-me")
    state_mod.write_agent(tmp_path, _guard_record(
        "b_001", "working", "tab:b", watcher_pid=111, caller_terminal="old-caller",
    ))

    calls = []
    alive = {"value": True}

    def terminate(pid):
        calls.append(("terminate", pid))
        alive["value"] = False

    def spawn(root, agent_id):
        record = state_mod.read_agent(root, agent_id)
        calls.append(("spawn", record.get("follow_turn"), record.get("guardian")))
        return 222

    with patch("osw.cli.terminal_show", _me_show(tmp_path)), \
         patch("osw.cli.worktree_ps", AsyncMock(return_value=[{"agents": [
             {"paneKey": "tab:b", "state": "working"},
         ]}])), \
         patch("osw.cli.pid_is_running", side_effect=lambda pid: alive["value"]), \
         patch("osw.cli._terminate_pid", side_effect=terminate), \
         patch("osw.cli._spawn_watcher", side_effect=spawn):
        result = runner.invoke(app, ["guard", "b_001"])

    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["guarded"] == [{"agent_id": "b_001", "action": "watcher_restarted"}]
    guardian = {"terminal": "term-me", "pane_key": "tab:me"}
    # Terminated first; the new watcher starts on a record already
    # carrying the follow flag and the guardian.
    assert calls == [("terminate", 111), ("spawn", True, guardian)]
    agent = state_mod.read_agent(tmp_path, "b_001")
    assert agent["watcher_pid"] == 222
    assert agent["follow_turn"] is True
    assert agent["guardian"] == guardian
    assert agent["caller_terminal"] == "old-caller"


def test_guard_follows_working_pane_without_watcher(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term-me")
    state_mod.write_agent(tmp_path, _guard_record("b_001", "enrolled", "tab:b"))

    spawn = Mock(return_value=333)
    terminate = Mock()
    with patch("osw.cli.terminal_show", _me_show(tmp_path)), \
         patch("osw.cli.worktree_ps", AsyncMock(return_value=[{"agents": [
             {"paneKey": "tab:b", "state": "working"},
         ]}])), \
         patch("osw.cli.pid_is_running", return_value=False), \
         patch("osw.cli._terminate_pid", terminate), \
         patch("osw.cli._spawn_watcher", spawn):
        result = runner.invoke(app, ["guard"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["guarded"] == [
        {"agent_id": "b_001", "action": "follow_started"},
    ]
    terminate.assert_not_called()
    spawn.assert_called_once_with(tmp_path, "b_001")
    agent = state_mod.read_agent(tmp_path, "b_001")
    assert agent["follow_turn"] is True
    assert agent["watcher_pid"] == 333
    assert agent["guardian"] == {"terminal": "term-me", "pane_key": "tab:me"}


def test_guard_again_overrides_previous_guardian(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    state_mod.write_agent(tmp_path, _guard_record(
        "b_001", "done", "tab:b", guardian={"agent_id": "old_001"},
    ))

    show = _me_show(tmp_path, handle="term-boss", leaf="boss")
    with patch("osw.cli.terminal_show", show), \
         patch("osw.cli.worktree_ps", AsyncMock(return_value=[])), \
         patch("osw.cli.pid_is_running", return_value=False):
        result = runner.invoke(
            app, ["guard", "b_001", "--caller-terminal", "term-boss"],
        )

    assert result.exit_code == 0, result.output
    show.assert_awaited_once_with("term-boss")
    assert state_mod.read_agent(tmp_path, "b_001")["guardian"] == {
        "terminal": "term-boss", "pane_key": "tab:boss",
    }


def test_guard_reports_unknown_ids(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term-me")

    with patch("osw.cli.terminal_show", _me_show(tmp_path)), \
         patch("osw.cli.worktree_ps", AsyncMock(return_value=[])):
        result = runner.invoke(app, ["guard", "nope_001"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["skipped"] == [
        {"agent_id": "nope_001", "reason": "not_found"},
    ]


def test_guard_skips_watcher_that_does_not_exit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term-me")
    monkeypatch.setattr("osw.cli.WATCHER_EXIT_TIMEOUT_SECS", 0.0)
    record = _guard_record("b_001", "working", "tab:b", watcher_pid=111)
    state_mod.write_agent(tmp_path, record)

    spawn = Mock(return_value=1)
    with patch("osw.cli.terminal_show", _me_show(tmp_path)), \
         patch("osw.cli.worktree_ps", AsyncMock(return_value=[])), \
         patch("osw.cli.pid_is_running", return_value=True), \
         patch("osw.cli._terminate_pid"), \
         patch("osw.cli._spawn_watcher", spawn):
        result = runner.invoke(app, ["guard", "b_001"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["skipped"] == [
        {"agent_id": "b_001", "reason": "watcher_did_not_exit"},
    ]
    spawn.assert_not_called()
    assert state_mod.read_agent(tmp_path, "b_001") == record


def test_guard_without_identifiable_executor_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    state_mod.write_agent(tmp_path, _guard_record("b_001", "done", "tab:b"))

    result = runner.invoke(app, ["guard"])

    assert result.exit_code == 1
    assert "caller terminal" in result.output
    assert "guardian" not in state_mod.read_agent(tmp_path, "b_001")


def test_guard_stale_executor_handle_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term-gone")
    state_mod.write_agent(tmp_path, _guard_record("b_001", "done", "tab:b"))

    with patch("osw.cli.terminal_show", AsyncMock(side_effect=_stale_handle_error())):
        result = runner.invoke(app, ["guard"])

    assert result.exit_code == 1
    assert "is stale" in result.stderr
    assert "guardian" not in state_mod.read_agent(tmp_path, "b_001")


def test_guard_requires_initialized_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["guard"])

    assert result.exit_code == 1
    assert "Not initialized" in result.output


# ---------------------------------------------------------------------------
# stop (plan §1.8, CLI side)
# ---------------------------------------------------------------------------

def _stop_record(provider: str, **extra) -> dict:
    record = {
        "agent_id": "agent_001",
        "terminal": "term-a",
        "pane_key": "tab-a:leaf-b",
        "provider": provider,
        "state": "working",
        "watcher_pid": 4242,
    }
    record.update(extra)
    return record


def _invoke_stop(tmp_path, monkeypatch, record: dict, *, watcher: bool,
                 interrupt=None, escape=None, terminals=None,
                 session=lambda pane_key: ""):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    state_mod.write_agent(tmp_path, record)
    interrupt = interrupt or AsyncMock(return_value={})
    escape = escape or AsyncMock(return_value={})
    terminals = terminals or AsyncMock(return_value=[])
    with patch("osw.cli.terminal_interrupt", interrupt), \
         patch("osw.cli.terminal_send_escape", escape), \
         patch("osw.cli.terminal_list", terminals), \
         patch("osw.cli.session_for_pane", session), \
         patch("osw.cli.pid_is_running", return_value=watcher):
        result = runner.invoke(app, ["stop", record["agent_id"]])
    return result, interrupt, escape


@pytest.mark.parametrize("provider,key", [
    ("claude", "interrupt"), ("codex", "interrupt"), ("kimi", "interrupt"),
    ("grok", "interrupt"), ("dsh", "interrupt"),
    ("pi", "esc"), ("omp", "esc"),
])
def test_stop_sends_the_provider_interrupt_key(tmp_path, monkeypatch, provider, key):
    result, interrupt, escape = _invoke_stop(
        tmp_path, monkeypatch, _stop_record(provider), watcher=False)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "agent_id": "agent_001", "key": key, "watcher": False,
    }
    used, unused = (interrupt, escape) if key == "interrupt" else (escape, interrupt)
    used.assert_awaited_once_with("term-a")
    unused.assert_not_awaited()


def test_stop_with_live_watcher_writes_sidecar_not_record(tmp_path, monkeypatch):
    record = _stop_record("claude")
    result, interrupt, _ = _invoke_stop(
        tmp_path, monkeypatch, record, watcher=True,
        session=lambda pane_key: "sess-1")

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "agent_id": "agent_001", "key": "interrupt", "watcher": True,
    }
    interrupt.assert_awaited_once_with("term-a")
    assert state_mod.read_stop_request(tmp_path, "agent_001")
    # the live watcher owns the record: the CLI never writes it
    assert state_mod.read_agent(tmp_path, "agent_001") == record


def test_stop_without_watcher_records_session_and_writes_no_sidecar(tmp_path, monkeypatch):
    lookups = []

    def session(pane_key):
        lookups.append(pane_key)
        return "pi-sess"

    result, _, escape = _invoke_stop(
        tmp_path, monkeypatch, _stop_record("pi", state="lost"),
        watcher=False, session=session)

    assert result.exit_code == 0, result.output
    escape.assert_awaited_once_with("term-a")
    assert lookups == ["tab-a:leaf-b"]
    agent = state_mod.read_agent(tmp_path, "agent_001")
    assert agent["session_id"] == "pi-sess"
    assert agent["state"] == "lost"
    assert not state_mod.stop_request_path(tmp_path, "agent_001").exists()


def test_stop_session_lookup_failure_still_interrupts(tmp_path, monkeypatch):
    def session(pane_key):
        raise RuntimeError("cache unreadable")

    result, interrupt, _ = _invoke_stop(
        tmp_path, monkeypatch, _stop_record("codex", session_id="old"),
        watcher=False, session=session)

    assert result.exit_code == 0, result.output
    interrupt.assert_awaited_once_with("term-a")
    assert state_mod.read_agent(tmp_path, "agent_001")["session_id"] == "old"


def test_stop_rebinds_stale_handle_by_pane_and_retries_once(tmp_path, monkeypatch):
    interrupt = AsyncMock(side_effect=[_stale_handle_error(), {}])
    terminals = AsyncMock(return_value=[
        {"handle": "term-other", "tabId": "tab-a", "leafId": "leaf-z"},
        {"handle": "term-new", "tabId": "tab-a", "leafId": "leaf-b"},
    ])
    result, _, _ = _invoke_stop(
        tmp_path, monkeypatch, _stop_record("kimi"), watcher=True,
        interrupt=interrupt, terminals=terminals)

    assert result.exit_code == 0, result.output
    assert [c.args[0] for c in interrupt.await_args_list] == ["term-a", "term-new"]
    terminals.assert_awaited_once_with(f"path:{tmp_path}")
    assert state_mod.read_stop_request(tmp_path, "agent_001")


def test_stop_stale_handle_without_live_pane_fails_without_sidecar(tmp_path, monkeypatch):
    escape = AsyncMock(side_effect=_stale_handle_error())
    result, _, _ = _invoke_stop(
        tmp_path, monkeypatch, _stop_record("omp"), watcher=True,
        escape=escape, terminals=AsyncMock(return_value=[]))

    assert result.exit_code == 1
    assert "older runtime" in result.output
    escape.assert_awaited_once_with("term-a")
    assert not state_mod.stop_request_path(tmp_path, "agent_001").exists()


def test_stop_non_stale_send_failure_does_not_rebind(tmp_path, monkeypatch):
    interrupt = AsyncMock(side_effect=OrcaError(
        "not writable", 1, code="terminal_not_writable"))
    terminals = AsyncMock(return_value=[])
    result, _, _ = _invoke_stop(
        tmp_path, monkeypatch, _stop_record("grok"), watcher=True,
        interrupt=interrupt, terminals=terminals)

    assert result.exit_code == 1
    interrupt.assert_awaited_once()
    terminals.assert_not_awaited()
    assert not state_mod.stop_request_path(tmp_path, "agent_001").exists()


@pytest.mark.parametrize("provider", ["aider", ""])
def test_stop_unknown_provider_fails_without_sending(tmp_path, monkeypatch, provider):
    result, interrupt, escape = _invoke_stop(
        tmp_path, monkeypatch, _stop_record(provider), watcher=True)

    assert result.exit_code == 1
    assert "provider" in result.output
    interrupt.assert_not_awaited()
    escape.assert_not_awaited()
    assert not state_mod.stop_request_path(tmp_path, "agent_001").exists()


def test_stop_missing_agent_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)

    result = runner.invoke(app, ["stop", "agent_404"])

    assert result.exit_code == 1
    assert "not found" in result.output


# ---------------------------------------------------------------------------
# revive (plan 1.7, CLI side)
# ---------------------------------------------------------------------------

def _revive_record(agent_id, state, provider="claude", **extra) -> dict:
    return {
        "agent_id": agent_id,
        "terminal": f"term-{agent_id}",
        "pane_key": f"tab:{agent_id}",
        "worktree_path": "x",
        "provider": provider,
        "state": state,
        "prompt": "original task",
        "caller_terminal": "old-caller",
        **extra,
    }


def _created(handle="term-new", tab="tab", leaf="new") -> AsyncMock:
    return AsyncMock(return_value={"result": {"terminal": {
        "handle": handle, "tabId": tab, "leafId": leaf,
    }}})


def test_revive_lists_revivable_and_not_revivable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    for record in [
        _revive_record("a_001", "done", session_id="s-a"),
        _revive_record("b_001", "enrolled", provider="aider",
                       support="guard_only", support_reason="unsupported_provider",
                       session_id="s-b"),
        _revive_record("c_001", "error", provider="codex"),
        _revive_record("d_001", "working", session_id="s-d", watcher_pid=111),
        _revive_record("e_001", "working", session_id="s-e", watcher_pid=222),
    ]:
        state_mod.write_agent(tmp_path, record)

    with patch("osw.cli.pid_is_running", side_effect=lambda pid: pid == 111):
        result = runner.invoke(app, ["revive"])

    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["revivable"] == [
        {"agent_id": "a_001", "provider": "claude", "state": "done", "session_id": "s-a"},
        {"agent_id": "e_001", "provider": "claude", "state": "lost", "session_id": "s-e"},
    ]
    assert out["not_revivable"] == [
        {"agent_id": "b_001", "reason": "unsupported_provider"},
        {"agent_id": "c_001", "reason": "no_session"},
        {"agent_id": "d_001", "reason": "watcher_running"},
    ]
    # Listing is read-mostly but settles orphans like list/status do.
    assert state_mod.read_agent(tmp_path, "e_001")["state"] == "lost"


def test_revive_lost_agent_resumes_session_and_continues_task(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    state_mod.write_agent(tmp_path, _revive_record("boss_001", "done"))
    state_mod.write_agent(tmp_path, _revive_record(
        "code_001", "working", watcher_pid=111, session_id="s-1",
        model_name="sonnet", thinking="high", guardian={"agent_id": "boss_001"},
    ))

    create = _created()
    ps = AsyncMock(return_value=[])
    spawn = Mock(return_value=999)
    with patch("osw.cli.pid_is_running", return_value=False), \
         patch("osw.cli.terminal_create", create), \
         patch("osw.cli.worktree_ps", ps), \
         patch("osw.cli._spawn_watcher", spawn):
        result = runner.invoke(app, ["revive", "code_001"])

    assert result.exit_code == 0, result.output
    create.assert_awaited_once_with(
        "claude --dangerously-skip-permissions --model sonnet --effort high --resume s-1"
    )
    ps.assert_not_awaited()  # never judge from the old pane's ps state
    spawn.assert_called_once_with(tmp_path, "code_001")
    agent = state_mod.read_agent(tmp_path, "code_001")
    assert agent["terminal"] == "term-new"
    assert agent["pane_key"] == "tab:new"
    assert agent["session_id"] == "s-1"
    assert agent["resume_turn"] is True
    assert agent["watcher_pid"] == 999
    assert agent["prompt"] == "original task"
    assert agent["guardian"] == {"agent_id": "boss_001"}
    out = json.loads(result.stdout)
    assert out["agent_id"] == "code_001"
    assert out["resume_turn"] is True
    assert out["terminal"] == "term-new"


def test_revive_idle_agent_only_restores_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term-me")
    state_mod.write_agent(tmp_path, _revive_record("code_001", "done", provider="codex"))

    create = _created()
    spawn = Mock(return_value=999)
    sessions = Mock(return_value="s-pane")
    with patch("osw.cli.pid_is_running", return_value=False), \
         patch("osw.cli.terminal_create", create), \
         patch("osw.cli.terminal_show", _me_show(tmp_path)), \
         patch("osw.cli.session_for_pane", sessions), \
         patch("osw.cli._spawn_watcher", spawn):
        result = runner.invoke(app, ["revive", "code_001"])

    assert result.exit_code == 0, result.output
    sessions.assert_called_once_with("tab:code_001")
    create.assert_awaited_once_with(
        "codex resume s-pane --dangerously-bypass-approvals-and-sandbox"
    )
    spawn.assert_not_called()
    agent = state_mod.read_agent(tmp_path, "code_001")
    assert agent["state"] == "enrolled"
    assert agent["terminal"] == "term-new"
    assert agent["pane_key"] == "tab:new"
    assert agent["session_id"] == "s-pane"
    assert not agent.get("resume_turn")
    # No guardian on record: the executor becomes it.
    assert agent["guardian"] == {"terminal": "term-me", "pane_key": "tab:me"}


def test_revive_session_flag_beats_recorded_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    state_mod.write_agent(tmp_path, _revive_record(
        "code_001", "enrolled", provider="pi", session_id="s-old",
    ))

    create = _created()
    sessions = Mock(return_value="s-pane")
    with patch("osw.cli.pid_is_running", return_value=False), \
         patch("osw.cli.terminal_create", create), \
         patch("osw.cli.session_for_pane", sessions):
        result = runner.invoke(app, ["revive", "code_001", "--session", "s-flag"])

    assert result.exit_code == 0, result.output
    create.assert_awaited_once_with("pi --approve --session s-flag")
    sessions.assert_not_called()
    assert state_mod.read_agent(tmp_path, "code_001")["session_id"] == "s-flag"


def test_revive_without_any_session_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    record = _revive_record("code_001", "done")
    state_mod.write_agent(tmp_path, record)

    create = _created()
    with patch("osw.cli.pid_is_running", return_value=False), \
         patch("osw.cli.terminal_create", create), \
         patch("osw.cli.session_for_pane", return_value=""):
        result = runner.invoke(app, ["revive", "code_001"])

    assert result.exit_code == 1
    assert "no session" in result.output.lower()
    create.assert_not_awaited()
    assert state_mod.read_agent(tmp_path, "code_001") == record


@pytest.mark.parametrize("extra,running,message", [
    ({"support": "guard_only", "state": "enrolled"}, False, "unsupported"),
    ({"state": "working", "watcher_pid": 111}, True, "watcher"),
    ({"state": "assigned"}, False, "assigned"),
])
def test_revive_rejects_unmet_preconditions(tmp_path, monkeypatch, extra, running, message):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    record = {**_revive_record("code_001", "done", session_id="s-1"), **extra}
    state_mod.write_agent(tmp_path, record)

    create = _created()
    with patch("osw.cli.pid_is_running", return_value=running), \
         patch("osw.cli.terminal_create", create):
        result = runner.invoke(app, ["revive", "code_001"])

    assert result.exit_code == 1
    assert message in result.output
    create.assert_not_awaited()
    assert state_mod.read_agent(tmp_path, "code_001") == record


def test_revive_missing_agent_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)

    result = runner.invoke(app, ["revive", "nope_001"])

    assert result.exit_code == 1
    assert "not found" in result.output


def test_revive_terminal_create_failure_leaves_record(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    record = _revive_record("code_001", "done", session_id="s-1")
    state_mod.write_agent(tmp_path, record)

    with patch("osw.cli.pid_is_running", return_value=False), \
         patch("osw.cli.terminal_create", AsyncMock(side_effect=OrcaError("boom", 1))):
        result = runner.invoke(app, ["revive", "code_001"])

    assert result.exit_code == 1
    assert "boom" in result.output
    assert state_mod.read_agent(tmp_path, "code_001") == record


def test_revive_replaces_unresolvable_guardian_with_executor(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term-me")
    state_mod.write_agent(tmp_path, _revive_record(
        "code_001", "done", session_id="s-1", guardian={"agent_id": "gone_001"},
    ))

    with patch("osw.cli.pid_is_running", return_value=False), \
         patch("osw.cli.terminal_create", _created()), \
         patch("osw.cli.terminal_show", _me_show(tmp_path)):
        result = runner.invoke(app, ["revive", "code_001"])

    assert result.exit_code == 0, result.output
    assert state_mod.read_agent(tmp_path, "code_001")["guardian"] == {
        "terminal": "term-me", "pane_key": "tab:me",
    }


def test_revive_rebinds_inline_guardian_by_pane(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    state_mod.write_agent(tmp_path, _revive_record(
        "code_001", "done", session_id="s-1",
        guardian={"terminal": "term-g-old", "pane_key": "tab:g"},
    ))

    show = AsyncMock(side_effect=_stale_handle_error())
    terminals = AsyncMock(return_value=[
        {"handle": "term-g-new", "tabId": "tab", "leafId": "g"},
    ])
    with patch("osw.cli.pid_is_running", return_value=False), \
         patch("osw.cli.terminal_create", _created()), \
         patch("osw.cli.terminal_show", show), \
         patch("osw.cli.terminal_list", terminals):
        result = runner.invoke(app, ["revive", "code_001"])

    assert result.exit_code == 0, result.output
    assert state_mod.read_agent(tmp_path, "code_001")["guardian"] == {
        "terminal": "term-g-new", "pane_key": "tab:g",
    }


def test_revive_keeps_live_inline_guardian(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term-me")
    guardian = {"terminal": "term-g", "pane_key": "tab:g"}
    state_mod.write_agent(tmp_path, _revive_record(
        "code_001", "done", session_id="s-1", guardian=guardian,
    ))

    show = AsyncMock(return_value=_show_pane(tmp_path, "term-g", "tab", "g"))
    with patch("osw.cli.pid_is_running", return_value=False), \
         patch("osw.cli.terminal_create", _created()), \
         patch("osw.cli.terminal_show", show):
        result = runner.invoke(app, ["revive", "code_001"])

    assert result.exit_code == 0, result.output
    show.assert_awaited_once_with("term-g")
    assert state_mod.read_agent(tmp_path, "code_001")["guardian"] == guardian


def _omp_show(handle="term-new", pane="tab:new") -> AsyncMock:
    return AsyncMock(return_value={"result": {
        "dispatch": {"status": "dispatched", "assigneePaneKey": pane},
        "worker": {"state": "working", "stage": "running",
                   "agentTerminalHandle": handle},
        "projection": {"stage": {"dispatch": "dispatched", "worker": "working"}},
        "observation": {"status": "live", "exactWorker": True, "agentWait": None},
    }})


def _omp_identity_show(tmp_path) -> AsyncMock:
    """`terminal show` for the revived omp pane: agent already up."""
    data = _show_pane(tmp_path, "term-new", "tab", "new")
    data["result"]["terminal"]["agentIdentity"] = "omp"
    return AsyncMock(return_value=data)


def test_revive_lost_omp_claims_new_terminal_with_worker_start(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    state = state_mod.read_state(tmp_path)
    state["orchestration_run_id"] = "run_abc"
    state_mod.write_state(tmp_path, state)
    state_mod.write_agent(tmp_path, _revive_record(
        "misc_001", "working", provider="omp", watcher_pid=111, session_id="s-omp",
        launch_mode="supervised", dispatch_id="ctx_old", task_id="task_old",
        guardian={"terminal": "term-g", "pane_key": "tab:g"},
    ))

    order = []
    release = AsyncMock(side_effect=lambda d: order.append(("release", d)) or {})
    create = _created()
    start = AsyncMock(return_value=_supervised_start_result("term-new", "tab:new"))
    spawn = Mock(return_value=999)
    identity = _omp_identity_show(tmp_path)

    async def show(handle):
        if handle == "term-g":
            return _show_pane(tmp_path, "term-g", "tab", "g")
        return await identity(handle)

    with patch("osw.cli.pid_is_running", return_value=False), \
         patch("osw.cli.terminal_create", create), \
         patch("osw.cli.terminal_show", AsyncMock(side_effect=show)), \
         patch("osw.cli.orchestration_worker_release", release), \
         patch("osw.cli.orchestration_run_current",
               AsyncMock(return_value={"run": {"id": "run_abc"}})), \
         patch("osw.cli.orchestration_worker_start", start), \
         patch("osw.cli.orchestration_worker_show", _omp_show()), \
         patch("osw.cli._spawn_watcher", spawn):
        result = runner.invoke(app, ["revive", "misc_001"])

    assert result.exit_code == 0, result.output
    create.assert_awaited_once_with("omp --auto-approve --resume s-omp")
    release.assert_awaited_once_with("ctx_old")
    identity.assert_awaited_with("term-new")
    kwargs = start.await_args.kwargs
    assert kwargs["terminal"] == "term-new"
    assert kwargs["spec"].startswith("continue ")
    spawn.assert_called_once_with(tmp_path, "misc_001")
    agent = state_mod.read_agent(tmp_path, "misc_001")
    assert agent["launch_mode"] == "supervised"
    assert agent["dispatch_id"] == "ctx_1"
    assert agent["task_id"] == "task_1"
    assert agent["terminal"] == "term-new"
    assert agent["pane_key"] == "tab:new"
    assert agent["resume_turn"] is True
    assert agent["watcher_pid"] == 999


def test_revive_idle_omp_restores_session_without_dispatch(tmp_path, monkeypatch):
    """worker-start always injects a task spec, so an idle omp session
    gets no Dispatch; it is marked supervised so a later `use` claims
    the terminal with `worker-start --terminal`."""
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    state_mod.write_agent(tmp_path, _revive_record(
        "omp_001", "enrolled", provider="omp", session_id="s-omp",
        guardian={"agent_id": "omp_001"},
    ))

    start = AsyncMock()
    release = AsyncMock()
    with patch("osw.cli.pid_is_running", return_value=False), \
         patch("osw.cli.terminal_create", _created()), \
         patch("osw.cli.terminal_show", AsyncMock(return_value={})), \
         patch("osw.cli.orchestration_worker_release", release), \
         patch("osw.cli.orchestration_worker_start", start), \
         patch("osw.cli._spawn_watcher", Mock()) as spawn:
        result = runner.invoke(app, ["revive", "omp_001"])

    assert result.exit_code == 0, result.output
    start.assert_not_awaited()
    release.assert_not_awaited()
    spawn.assert_not_called()
    agent = state_mod.read_agent(tmp_path, "omp_001")
    assert agent["state"] == "enrolled"
    assert agent["launch_mode"] == "supervised"
    assert agent["orchestration_agent"] == "omp"
    assert agent["dispatch_id"] == ""
    assert agent["terminal"] == "term-new"


def test_revive_omp_worker_start_failure_keeps_revived_terminal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    state = state_mod.read_state(tmp_path)
    state["orchestration_run_id"] = "run_abc"
    state_mod.write_state(tmp_path, state)
    state_mod.write_agent(tmp_path, _revive_record(
        "misc_001", "lost", provider="omp", session_id="s-omp",
        launch_mode="supervised", dispatch_id="ctx_old",
        guardian={"agent_id": "misc_001"},
    ))

    failure = OrcaError("agent not ready", 1, code="agent_unconfigured")
    spawn = Mock()
    with patch("osw.cli.pid_is_running", return_value=False), \
         patch("osw.cli.terminal_create", _created()), \
         patch("osw.cli.terminal_show", _omp_identity_show(tmp_path)), \
         patch("osw.cli.orchestration_worker_release", AsyncMock(return_value={})), \
         patch("osw.cli.orchestration_run_current",
               AsyncMock(return_value={"run": {"id": "run_abc"}})), \
         patch("osw.cli.orchestration_worker_start", AsyncMock(side_effect=failure)), \
         patch("osw.cli._spawn_watcher", spawn):
        result = runner.invoke(app, ["revive", "misc_001"])

    assert result.exit_code == 1
    assert "agent not ready" in result.output
    spawn.assert_not_called()
    agent = state_mod.read_agent(tmp_path, "misc_001")
    assert agent["terminal"] == "term-new"
    assert agent["state"] == "enrolled"
    assert agent["dispatch_id"] == ""
    assert not agent.get("resume_turn")


def test_revive_omp_that_never_comes_up_is_not_claimed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    monkeypatch.setattr("osw.cli.REVIVE_AGENT_START_TIMEOUT_SECS", 0.0)
    state_mod.write_agent(tmp_path, _revive_record(
        "misc_001", "lost", provider="omp", session_id="s-omp",
        guardian={"agent_id": "misc_001"},
    ))

    start = AsyncMock()
    with patch("osw.cli.pid_is_running", return_value=False), \
         patch("osw.cli.terminal_create", _created()), \
         patch("osw.cli.terminal_show", AsyncMock(return_value=_show_pane(tmp_path, "term-new", "tab", "new"))), \
         patch("osw.cli.orchestration_worker_start", start):
        result = runner.invoke(app, ["revive", "misc_001"])

    assert result.exit_code == 1
    assert "omp" in result.output
    start.assert_not_awaited()
    agent = state_mod.read_agent(tmp_path, "misc_001")
    assert agent["terminal"] == "term-new"
    assert agent["state"] == "enrolled"


def test_revive_requires_initialized_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["revive"])

    assert result.exit_code == 1
    assert "Not initialized" in result.output


# ---------------------------------------------------------------------------
# revive in place (plan 2.1)
# ---------------------------------------------------------------------------

PWSH_TITLE = "D:\\DevTools\\PowerShell\\7\\pwsh.exe"


def _old_pane_terminal(handle="term-cur", title=PWSH_TITLE, identity=None) -> dict:
    entry = {
        "handle": handle, "tabId": "tab", "leafId": "code_001", "title": title,
    }
    if identity:
        entry["agentIdentity"] = identity
    return entry


def _shell_frame(source="screen") -> dict:
    terminal = {"tail": ["PS D:\\bit_master\\research\\orca_cli\\orca-osw>"]}
    if source:
        terminal["source"] = source
    return {"result": {"terminal": terminal}}


def _shell_answer(choice="shell_prompt") -> dict:
    return {"choice": choice, "probabilities": {choice: 0.97}, "confidence": 0.97}


def _in_place_patches(
    terminals=None, ps=None, frame=None, answer="default", send=None, create=None,
):
    return {
        "terminal_list": AsyncMock(return_value=[_old_pane_terminal()]
                                   if terminals is None else terminals),
        "worktree_ps": AsyncMock(return_value=[] if ps is None else ps),
        "terminal_read": AsyncMock(return_value=_shell_frame() if frame is None else frame),
        "ask_ready": AsyncMock(return_value=_shell_answer() if answer == "default" else answer),
        "terminal_send": send or AsyncMock(return_value={}),
        "terminal_create": create or _created(),
    }


def _run_revive(mocks, args, spawn=None):
    spawn = spawn or Mock(return_value=999)
    with patch("osw.cli.pid_is_running", return_value=False), \
         patch("osw.cli.terminal_list", mocks["terminal_list"]), \
         patch("osw.cli.worktree_ps", mocks["worktree_ps"]), \
         patch("osw.cli.terminal_read", mocks["terminal_read"]), \
         patch("osw.cli.ask_ready", mocks["ask_ready"]), \
         patch("osw.cli.terminal_send", mocks["terminal_send"]), \
         patch("osw.cli.terminal_create", mocks["terminal_create"]), \
         patch("osw.cli._spawn_watcher", spawn):
        return runner.invoke(app, args), spawn


def test_revive_in_place_types_resume_command_into_empty_shell(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.setenv("OSW_JEV_MODE", "shadow")
    state_mod.write_agent(tmp_path, _revive_record(
        "code_001", "done", session_id="s-1", model_name="claude-opus-5-5[1m]",
        guardian={"agent_id": "code_001"},
    ))

    mocks = _in_place_patches()
    result, spawn = _run_revive(mocks, ["revive", "code_001"])

    assert result.exit_code == 0, result.output
    mocks["terminal_list"].assert_awaited_once_with(f"path:{tmp_path}")
    mocks["terminal_read"].assert_awaited_once_with("term-cur", screen=True)
    ask = mocks["ask_ready"].await_args
    assert ask.args[2] == "claude"
    assert ask.args[3] == _shell_frame()
    assert ask.args[4] == "shadow"
    mocks["terminal_send"].assert_awaited_once_with(
        "term-cur",
        "claude --dangerously-skip-permissions --model 'claude-opus-5-5[1m]' --resume s-1",
    )
    mocks["terminal_create"].assert_not_awaited()
    spawn.assert_not_called()
    agent = state_mod.read_agent(tmp_path, "code_001")
    assert agent["terminal"] == "term-cur"
    assert agent["pane_key"] == "tab:code_001"
    assert agent["state"] == "enrolled"
    out = json.loads(result.stdout)
    assert out["placement"] == "in_place"
    assert out["terminal"] == "term-cur"


def test_revive_in_place_lost_agent_resumes_task(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.setenv("OSW_JEV_MODE", "enforce")
    state_mod.write_agent(tmp_path, _revive_record(
        "code_001", "lost", session_id="s-1", guardian={"agent_id": "code_001"},
    ))

    mocks = _in_place_patches()
    result, spawn = _run_revive(mocks, ["revive", "code_001"])

    assert result.exit_code == 0, result.output
    spawn.assert_called_once_with(tmp_path, "code_001")
    agent = state_mod.read_agent(tmp_path, "code_001")
    assert agent["resume_turn"] is True
    assert agent["terminal"] == "term-cur"
    assert agent["pane_key"] == "tab:code_001"
    assert json.loads(result.stdout)["placement"] == "in_place"


@pytest.mark.parametrize("title,expected", [
    ("C:\\Windows\\system32\\cmd.exe", "claude --dangerously-skip-permissions --resume s-1"),
    ("Administrator: C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
     "claude --dangerously-skip-permissions --resume s-1"),
    ("/usr/bin/bash", "claude --dangerously-skip-permissions --resume s-1"),
])
def test_revive_in_place_recognizes_shell_from_title(tmp_path, monkeypatch, title, expected):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.setenv("OSW_JEV_MODE", "shadow")
    state_mod.write_agent(tmp_path, _revive_record(
        "code_001", "done", session_id="s-1", guardian={"agent_id": "code_001"},
    ))

    mocks = _in_place_patches(terminals=[_old_pane_terminal(title=title)])
    result, _ = _run_revive(mocks, ["revive", "code_001"])

    assert result.exit_code == 0, result.output
    mocks["terminal_send"].assert_awaited_once_with("term-cur", expected)
    assert json.loads(result.stdout)["placement"] == "in_place"


def test_revive_in_place_cmd_quotes_with_double_quotes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.setenv("OSW_JEV_MODE", "shadow")
    state_mod.write_agent(tmp_path, _revive_record(
        "code_001", "done", session_id="s-1", model_name="my model",
        guardian={"agent_id": "code_001"},
    ))

    mocks = _in_place_patches(terminals=[_old_pane_terminal(title="C:\\Windows\\system32\\cmd.exe")])
    result, _ = _run_revive(mocks, ["revive", "code_001"])

    assert result.exit_code == 0, result.output
    mocks["terminal_send"].assert_awaited_once_with(
        "term-cur", 'claude --dangerously-skip-permissions --model "my model" --resume s-1',
    )


@pytest.mark.parametrize("case", [
    "jev_off", "pane_gone", "ps_working", "unknown_shell",
    "jev_not_shell", "jev_degraded", "read_failed", "no_screen_frame",
    "cmd_unescapable",
])
def test_revive_falls_back_to_new_terminal_when_shell_unconfirmed(tmp_path, monkeypatch, case):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    if case != "jev_off":
        monkeypatch.setenv("OSW_JEV_MODE", "shadow")
    extra = {"model_name": "100%"} if case == "cmd_unescapable" else {}
    state_mod.write_agent(tmp_path, _revive_record(
        "code_001", "done", session_id="s-1", guardian={"agent_id": "code_001"}, **extra,
    ))
    kwargs = {
        "pane_gone": {"terminals": [{**_old_pane_terminal(), "leafId": "other"}]},
        "ps_working": {"ps": [{"agents": [{"paneKey": "tab:code_001", "state": "working"}]}]},
        "unknown_shell": {"terminals": [_old_pane_terminal(title="node")]},
        "jev_not_shell": {"answer": _shell_answer("ready_composer")},
        "jev_degraded": {"answer": None},
        "no_screen_frame": {"frame": _shell_frame(source=None)},
        "cmd_unescapable": {"terminals": [_old_pane_terminal(title="C:\\Windows\\system32\\cmd.exe")]},
    }.get(case, {})
    mocks = _in_place_patches(**kwargs)
    if case == "read_failed":
        mocks["terminal_read"] = AsyncMock(side_effect=OrcaError("no screen", 1))

    result, _ = _run_revive(mocks, ["revive", "code_001"])

    assert result.exit_code == 0, result.output
    mocks["terminal_send"].assert_not_awaited()
    mocks["terminal_create"].assert_awaited_once()
    if case == "jev_off":
        mocks["terminal_read"].assert_not_awaited()
        mocks["ask_ready"].assert_not_awaited()
    agent = state_mod.read_agent(tmp_path, "code_001")
    assert agent["terminal"] == "term-new"
    assert agent["pane_key"] == "tab:new"
    assert json.loads(result.stdout)["placement"] == "new_terminal"


@pytest.mark.parametrize("ps_kwargs", [
    # A cold restart restores the pre-restart `done` row unchanged.
    {"ps": [{"agents": [{"paneKey": "tab:code_001", "state": "done"}]}]},
    {"ps": [{"agents": [{"paneKey": "tab:code_001", "state": "waiting"}]}]},
    {"ps": [{"agents": [{"paneKey": "tab:other", "state": "working"}]}]},
])
def test_revive_in_place_ignores_ps_rows_that_are_not_working(tmp_path, monkeypatch, ps_kwargs):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.setenv("OSW_JEV_MODE", "shadow")
    state_mod.write_agent(tmp_path, _revive_record(
        "code_001", "done", session_id="s-1", guardian={"agent_id": "code_001"},
    ))

    mocks = _in_place_patches(**ps_kwargs)
    result, _ = _run_revive(mocks, ["revive", "code_001"])

    assert result.exit_code == 0, result.output
    mocks["terminal_send"].assert_awaited_once()
    mocks["terminal_create"].assert_not_awaited()
    assert json.loads(result.stdout)["placement"] == "in_place"


def test_revive_in_place_proceeds_when_ps_is_unavailable(tmp_path, monkeypatch):
    """ps can only veto (a `working` row); without it, agentIdentity and
    the Jev shell_prompt judgment still decide."""
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.setenv("OSW_JEV_MODE", "shadow")
    state_mod.write_agent(tmp_path, _revive_record(
        "code_001", "done", session_id="s-1", guardian={"agent_id": "code_001"},
    ))

    mocks = _in_place_patches()
    mocks["worktree_ps"] = AsyncMock(side_effect=OrcaError("down", 1))
    result, _ = _run_revive(mocks, ["revive", "code_001"])

    assert result.exit_code == 0, result.output
    mocks["terminal_send"].assert_awaited_once()
    assert json.loads(result.stdout)["placement"] == "in_place"


def test_revive_in_place_send_failure_falls_back_to_new_terminal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.setenv("OSW_JEV_MODE", "shadow")
    state_mod.write_agent(tmp_path, _revive_record(
        "code_001", "done", session_id="s-1", guardian={"agent_id": "code_001"},
    ))

    mocks = _in_place_patches(send=AsyncMock(side_effect=OrcaError("not writable", 1)))
    result, _ = _run_revive(mocks, ["revive", "code_001"])

    assert result.exit_code == 0, result.output
    mocks["terminal_create"].assert_awaited_once()
    assert state_mod.read_agent(tmp_path, "code_001")["terminal"] == "term-new"
    assert json.loads(result.stdout)["placement"] == "new_terminal"


def test_revive_new_terminal_reports_placement(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    state_mod.write_agent(tmp_path, _revive_record(
        "code_001", "done", session_id="s-1", guardian={"agent_id": "code_001"},
    ))

    with patch("osw.cli.pid_is_running", return_value=False), \
         patch("osw.cli.terminal_create", _created()):
        result = runner.invoke(app, ["revive", "code_001"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["placement"] == "new_terminal"


# ---------------------------------------------------------------------------
# revive smoke-test fixes (2026-10-12): live agents, pane identity, release
# ---------------------------------------------------------------------------

def test_revive_listing_marks_pane_with_live_agent_not_revivable(tmp_path, monkeypatch):
    """After a restart Orca can bring the agent back by itself; resuming
    that session again would run it twice."""
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    state_mod.write_agent(tmp_path, _revive_record("a_001", "done", session_id="s-a"))
    state_mod.write_agent(tmp_path, _revive_record("b_001", "done", session_id="s-b"))
    terminals = AsyncMock(return_value=[
        {"handle": "term-a2", "tabId": "tab", "leafId": "a_001", "agentIdentity": "claude"},
        {"handle": "term-b2", "tabId": "tab", "leafId": "b_001", "title": PWSH_TITLE},
    ])

    with patch("osw.cli.pid_is_running", return_value=False), \
         patch("osw.cli.terminal_list", terminals):
        result = runner.invoke(app, ["revive"])

    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert [a["agent_id"] for a in out["revivable"]] == ["b_001"]
    assert out["not_revivable"] == [{"agent_id": "a_001", "reason": "agent_running"}]


def test_revive_listing_without_terminal_list_keeps_candidates(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    state_mod.write_agent(tmp_path, _revive_record("a_001", "done", session_id="s-a"))

    with patch("osw.cli.pid_is_running", return_value=False), \
         patch("osw.cli.terminal_list", AsyncMock(side_effect=OrcaError("down", 1))):
        result = runner.invoke(app, ["revive"])

    assert [a["agent_id"] for a in json.loads(result.stdout)["revivable"]] == ["a_001"]


def test_revive_refuses_pane_with_live_agent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    monkeypatch.setenv("OSW_JEV_MODE", "shadow")
    record = _revive_record("code_001", "done", session_id="s-1",
                            guardian={"agent_id": "code_001"})
    state_mod.write_agent(tmp_path, record)

    mocks = _in_place_patches(terminals=[_old_pane_terminal(identity="claude")])
    result, spawn = _run_revive(mocks, ["revive", "code_001"])

    assert result.exit_code == 1
    assert "already running claude" in result.stderr
    assert "use code_001" in result.stderr
    mocks["terminal_create"].assert_not_awaited()
    mocks["terminal_send"].assert_not_awaited()
    spawn.assert_not_called()
    assert state_mod.read_agent(tmp_path, "code_001") == record


def test_revive_refuses_when_panes_cannot_be_listed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    record = _revive_record("code_001", "done", session_id="s-1")
    state_mod.write_agent(tmp_path, record)

    create = _created()
    with patch("osw.cli.pid_is_running", return_value=False), \
         patch("osw.cli.terminal_list", AsyncMock(side_effect=OrcaError("runtime down", 1))), \
         patch("osw.cli.terminal_create", create):
        result = runner.invoke(app, ["revive", "code_001"])

    assert result.exit_code == 1
    assert "runtime down" in result.stderr
    create.assert_not_awaited()
    assert state_mod.read_agent(tmp_path, "code_001") == record


def test_revive_new_terminal_resolves_pane_key_via_terminal_show(tmp_path, monkeypatch):
    """`terminal create` returns no tabId/leafId (observed live on Orca
    1.4.221); the pane identity must come from `terminal show`."""
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    state_mod.write_agent(tmp_path, _revive_record(
        "code_001", "done", session_id="s-1", guardian={"agent_id": "code_001"},
    ))

    create = AsyncMock(return_value={"result": {"terminal": {"handle": "term-new"}}})
    show = AsyncMock(return_value=_show_pane(tmp_path, "term-new", "tab", "new"))
    with patch("osw.cli.pid_is_running", return_value=False), \
         patch("osw.cli.terminal_create", create), \
         patch("osw.cli.terminal_show", show):
        result = runner.invoke(app, ["revive", "code_001"])

    assert result.exit_code == 0, result.output
    show.assert_awaited_once_with("term-new")
    assert state_mod.read_agent(tmp_path, "code_001")["pane_key"] == "tab:new"
    assert json.loads(result.stdout)["pane_key"] == "tab:new"


def test_revive_release_unknown_is_one_concise_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    state_mod.write_agent(tmp_path, _revive_record(
        "misc_001", "done", provider="omp", session_id="s-omp",
        launch_mode="supervised", dispatch_id="ctx_old",
        guardian={"agent_id": "misc_001"},
    ))
    payload = {"ok": True, "result": {
        "dispatchId": "ctx_old", "state": "release_unknown",
        "lastError": "The recorded terminal no longer resolves; whether its "
                     "process is gone cannot be proven.",
        "recovery": "Inspect with: orca orchestration worker-show ...",
    }}
    failure = OrcaError(json.dumps(payload, indent=2), 1, payload=payload)

    with patch("osw.cli.pid_is_running", return_value=False), \
         patch("osw.cli.terminal_create", _created()), \
         patch("osw.cli.orchestration_worker_release", AsyncMock(side_effect=failure)):
        result = runner.invoke(app, ["revive", "misc_001"])

    assert result.exit_code == 0, result.output
    warnings = [r.getMessage() for r in caplog.records if "ctx_old" in r.getMessage()]
    assert len(warnings) == 1
    assert "release_unknown" in warnings[0]
    assert "{" not in warnings[0] and len(warnings[0].splitlines()) == 1


# ---------------------------------------------------------------------------
# del --close on an external supervised terminal (smoke test 2026-10-12)
# ---------------------------------------------------------------------------

def _supervised_record(state: str) -> dict:
    return {
        "agent_id": "misc_001", "terminal": "term-old", "pane_key": "tab:w",
        "state": state, "launch_mode": "supervised", "dispatch_id": "ctx_1",
    }


@pytest.mark.parametrize("state,verb", [("done", "release"), ("working", "stop")])
def test_del_close_closes_terminal_orchestration_left_open(tmp_path, monkeypatch, state, verb):
    """A terminal OSW created itself (`revive` → `terminal create`) is
    `external` to Orca: worker-release/stop detach it but leave it open."""
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    state_mod.write_agent(tmp_path, _supervised_record(state))
    release = AsyncMock(return_value={})
    stop = AsyncMock(return_value={})
    close = AsyncMock(return_value={})
    terminals = AsyncMock(return_value=[
        {"handle": "term-other", "tabId": "tab", "leafId": "x"},
        {"handle": "term-cur", "tabId": "tab", "leafId": "w"},
    ])

    with patch("osw.cli.orchestration_worker_release", release), \
         patch("osw.cli.orchestration_worker_stop", stop), \
         patch("osw.cli.terminal_list", terminals), \
         patch("osw.cli.terminal_close", close):
        result = runner.invoke(app, ["del", "misc_001", "--close"])

    assert result.exit_code == 0, result.output
    (release if verb == "release" else stop).assert_awaited_once_with("ctx_1")
    close.assert_awaited_once_with("term-cur")
    assert "Warning" not in result.output
    assert state_mod.list_agents(tmp_path) == {}


def test_del_close_matches_leftover_terminal_by_handle_without_pane(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    record = _supervised_record("done")
    record.pop("pane_key")
    record["terminal"] = "term-cur"
    state_mod.write_agent(tmp_path, record)
    close = AsyncMock(return_value={})

    with patch("osw.cli.orchestration_worker_release", AsyncMock(return_value={})), \
         patch("osw.cli.terminal_list", AsyncMock(return_value=[
             {"handle": "term-cur", "tabId": "tab", "leafId": "w"}])), \
         patch("osw.cli.terminal_close", close):
        result = runner.invoke(app, ["del", "misc_001", "--close"])

    assert result.exit_code == 0, result.output
    close.assert_awaited_once_with("term-cur")


def test_del_close_leftover_check_failure_still_removes_agent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    state_mod.write_agent(tmp_path, _supervised_record("done"))
    close = AsyncMock(return_value={})

    with patch("osw.cli.orchestration_worker_release", AsyncMock(return_value={})), \
         patch("osw.cli.terminal_list", AsyncMock(side_effect=OrcaError("down", 1))), \
         patch("osw.cli.terminal_close", close):
        result = runner.invoke(app, ["del", "misc_001", "--close"])

    assert result.exit_code == 0, result.output
    close.assert_not_awaited()
    assert state_mod.list_agents(tmp_path) == {}


def test_del_close_leftover_close_failure_warns(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    state_mod.write_agent(tmp_path, _supervised_record("done"))

    with patch("osw.cli.orchestration_worker_release", AsyncMock(return_value={})), \
         patch("osw.cli.terminal_list", AsyncMock(return_value=[
             {"handle": "term-cur", "tabId": "tab", "leafId": "w"}])), \
         patch("osw.cli.terminal_close", AsyncMock(side_effect=OrcaError("busy", 1))):
        result = runner.invoke(app, ["del", "misc_001", "--close"])

    assert result.exit_code == 0
    assert "Warning: failed to close terminal: busy" in result.output
    assert state_mod.list_agents(tmp_path) == {}
