from __future__ import annotations

import json
import subprocess
from unittest.mock import AsyncMock, Mock, patch

import pytest
from typer.testing import CliRunner

from osw import state as state_mod
from osw.cli import app, _spawn_watcher

runner = CliRunner()


def test_init_creates_state_and_reports_scan(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert (tmp_path / ".orca" / "osw" / "state.json").is_file()
    # models must start empty — nothing about the machine is assumed
    state = state_mod.read_state(tmp_path)
    assert state["models"] == {"strong": [], "medium": [], "weak": []}
    assert "model add" in result.output


def test_list_and_status(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["status"]).exit_code != 0  # not initialized

    state_mod.init_state_dir(tmp_path)
    assert "no agents" in runner.invoke(app, ["list"]).output.lower()
    assert json.loads(runner.invoke(app, ["list", "--json"]).output) == {}
    assert "agents: none" in runner.invoke(app, ["status"]).output.lower()


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
    ["new", "test"],
    ["use", "--terminal", "t1", "test"],
])
def test_commands_require_initialized_state(tmp_path, monkeypatch, args):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, args)

    assert result.exit_code == 1
    assert "Not initialized" in result.output


def _configure_models(root):
    state = state_mod.read_state(root)
    state["models"]["medium"].append({"name": "codex-mid", "command": "codex"})
    state_mod.write_state(root, state)


def test_new_creates_terminal_agent_file_and_detaches_watcher(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    _configure_models(tmp_path)

    create = AsyncMock(return_value={"result": {"terminal": {"handle": "term-new"}}})
    spawn = Mock(return_value=4567)
    with patch("osw.cli.terminal_create", create), \
         patch("osw.cli._spawn_watcher", spawn):
        result = runner.invoke(
            app, ["new", "--caller-terminal", "caller-1", "do the task"]
        )

    assert result.exit_code == 0
    assert "Created agent_001" in result.output
    create.assert_awaited_once_with('codex "do the task"')
    spawn.assert_called_once_with(tmp_path, "agent_001")

    agent = state_mod.read_agent(tmp_path, "agent_001")
    assert agent["terminal"] == "term-new"
    assert agent["prompt"] == "do the task"
    assert agent["task_started_on_launch"] is True
    assert agent["caller_terminal"] == "caller-1"
    assert agent["model_name"] == "codex-mid"
    assert agent["watcher_pid"] == 4567
    assert agent["state"] == "assigned"


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


def test_new_rejects_empty_models_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)

    result = runner.invoke(app, ["new", "do the task"])

    assert result.exit_code == 1
    assert "models config is empty" in result.output


def test_use_adopts_terminal_with_terminal_show_result_unwrap(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)

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
            app, ["use", "--terminal", "term-existing", "continue this"]
        )

    assert result.exit_code == 0
    assert "Adopted agent_001" in result.output
    show.assert_awaited_once_with("term-existing")
    spawn.assert_called_once_with(tmp_path, "agent_001")
    agent = state_mod.read_agent(tmp_path, "agent_001")
    assert agent["terminal"] == "term-existing"
    assert agent["prompt"] == "continue this"
    assert agent["watcher_pid"] == 9876


def test_use_rejects_wrong_worktree(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)
    other = tmp_path / "other"
    other.mkdir()

    show = AsyncMock(return_value={
        "result": {"terminal": {"handle": "term-x", "worktreePath": str(other)}}
    })
    with patch("osw.cli.terminal_show", show):
        result = runner.invoke(app, ["use", "--terminal", "term-x", "task"])

    assert result.exit_code == 1
    assert "does not match project root" in result.output
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


def test_init_interactive_assigns_tiers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    found = [
        {"name": "claude", "path": "C:\\bin\\claude.EXE", "version": ""},
        {"name": "codex", "path": "C:\\bin\\codex.CMD", "version": ""},
    ]
    variants = {
        "claude": [
            {"name": "claude-opus", "command": "claude --model opus"},
            {"name": "claude-sonnet", "command": "claude --model sonnet"},
        ],
        "codex": [
            {"name": "codex-default", "command": "codex"},
            {"name": "codex-gpt-5.5-medium",
             "command": "codex -c model_reasoning_effort=medium -m gpt-5.5"},
        ],
    }

    def fake_probe(name, path=None):
        return variants[name]

    # strong: agent 1 (claude) -> variant 2 (sonnet)
    # medium: agent 2 (codex)  -> variant 2 (gpt-5.5-medium)
    # weak:   skip
    user_input = "1\n2\n2\n2\ns\n"
    with patch("osw.cli.scan_agent_clis", return_value=found), \
         patch("osw.cli.probe_variants", side_effect=fake_probe):
        result = runner.invoke(app, ["init", "-i"], input=user_input)

    assert result.exit_code == 0
    models = state_mod.read_state(tmp_path)["models"]
    assert models["strong"] == [
        {"name": "claude-sonnet", "command": "claude --model sonnet"}
    ]
    assert models["medium"] == [
        {"name": "codex-gpt-5.5-medium",
         "command": "codex -c model_reasoning_effort=medium -m gpt-5.5"}
    ]
    assert models["weak"] == []


def test_model_variants_command(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    variants = [{"name": "pi-gpt-5", "command": "pi --model openai/gpt-5"}]
    with patch("osw.cli.probe_variants", return_value=variants):
        result = runner.invoke(app, ["model", "variants", "pi"])

    assert result.exit_code == 0
    assert "pi --model openai/gpt-5" in result.output


def test_init_non_interactive_never_prompts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    found = [{"name": "codex", "path": "C:\\bin\\codex.CMD", "version": ""}]
    # No input provided: default init must complete without blocking
    with patch("osw.cli.scan_agent_clis", return_value=found):
        result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert state_mod.read_state(tmp_path)["models"] == {
        "strong": [], "medium": [], "weak": [],
    }


def test_model_add_list_remove_roundtrip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)

    add = runner.invoke(app, [
        "model", "add", "--tier", "medium",
        "--name", "codex-mid", "--command", "codex -c model_reasoning_effort=medium",
    ])
    assert add.exit_code == 0

    # duplicate name rejected
    dup = runner.invoke(app, [
        "model", "add", "--tier", "weak", "--name", "codex-mid", "--command", "x",
    ])
    assert dup.exit_code == 1

    listed = runner.invoke(app, ["model", "list"])
    assert "codex-mid" in listed.output

    state = state_mod.read_state(tmp_path)
    assert state["models"]["medium"] == [
        {"name": "codex-mid", "command": "codex -c model_reasoning_effort=medium"}
    ]

    removed = runner.invoke(app, ["model", "remove", "codex-mid"])
    assert removed.exit_code == 0
    assert state_mod.read_state(tmp_path)["models"]["medium"] == []
