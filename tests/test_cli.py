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
