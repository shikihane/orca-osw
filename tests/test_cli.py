from __future__ import annotations

import json
import os
import subprocess
from unittest.mock import AsyncMock, Mock, patch

import pytest
from typer.testing import CliRunner

from osw import state as state_mod
from osw.cli import app, _spawn_watcher
from osw.orca_cli import OrcaError

runner = CliRunner()


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


def test_new_supports_omp_provider(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    create = AsyncMock(return_value={"result": {"terminal": {
        "handle": "term-omp",
        "tabId": "tab-omp",
        "leafId": "leaf-omp",
    }}})
    spawn = Mock(return_value=6789)

    with patch("osw.cli.terminal_create", create), \
         patch("osw.cli._spawn_watcher", spawn):
        result = runner.invoke(
            app,
            [
                "new",
                "omp",
                "--model",
                "openai-codex/gpt-5.3-codex",
                "--thinking",
                "xhigh",
                "--no-notify",
                "do the task",
            ],
        )

    assert result.exit_code == 0
    assert "Created agent_001" in result.output
    assert "(provider: omp)" in result.output
    create.assert_awaited_once_with(
        "omp --auto-approve --model openai-codex/gpt-5.3-codex "
        "--thinking xhigh"
    )
    spawn.assert_called_once_with(tmp_path, "agent_001")
    agent = state_mod.read_agent(tmp_path, "agent_001")
    assert agent["provider"] == "omp"
    assert agent["model_name"] == "openai-codex/gpt-5.3-codex"
    assert agent["thinking"] == "xhigh"


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
    send = AsyncMock(return_value={})
    spawn = Mock(return_value=9876)
    with patch("osw.cli.terminal_show", show), \
         patch("osw.cli.terminal_send", send), \
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


def test_all_broadcasts_to_agent_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    state_mod.write_agent(tmp_path, {"agent_id": "agent_001", "terminal": "t1", "state": "working"})
    state_mod.write_agent(tmp_path, {"agent_id": "agent_002", "terminal": "t2", "state": "done"})

    send = AsyncMock(return_value={})
    with patch("osw.cli.terminal_send", send):
        result = runner.invoke(app, ["all", "status"])

    assert result.exit_code == 0
    assert "Sent to 1 agent" in result.output
    send.assert_awaited_once_with("t1", "status")


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
