from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from osw import state as state_mod
from osw.cli import app

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
    assert "not running" in runner.invoke(app, ["status"]).output.lower()


@pytest.mark.parametrize("args", [
    ["new", "test"],
    ["use", "--terminal", "t1", "test"],
    ["all", "test"],
    ["del", "agent_001"],
])
def test_commands_require_serve(tmp_path, monkeypatch, args):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)

    result = runner.invoke(app, args)

    assert result.exit_code == 1
    assert "OSW serve is not running" in result.output


def test_init_interactive_assigns_tiers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    found = [
        {"name": "codex", "path": "C:\\bin\\codex.CMD", "version": ""},
        {"name": "claude", "path": "C:\\bin\\claude.CMD", "version": ""},
    ]
    # menu: 1-3 codex presets, 4-6 claude presets
    # strong=1 (codex-high), medium=5 (claude-sonnet), weak=s (skip)
    with patch("osw.cli.scan_agent_clis", return_value=found):
        result = runner.invoke(app, ["init", "-i"], input="1\n5\ns\n")

    assert result.exit_code == 0
    models = state_mod.read_state(tmp_path)["models"]
    assert models["strong"] == [
        {"name": "codex-high", "command": "codex -c model_reasoning_effort=high"}
    ]
    assert models["medium"] == [
        {"name": "claude-sonnet", "command": "claude --model sonnet"}
    ]
    assert models["weak"] == []


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
