from __future__ import annotations

import json

from typer.testing import CliRunner

from osw import state as state_mod
from osw.cli import app

runner = CliRunner()


def test_init_creates_state_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert (tmp_path / ".orca" / "osw" / "state.json").is_file()


def test_list_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)

    result = runner.invoke(app, ["list"])

    assert result.exit_code == 0
    assert "no agents" in result.output.lower()


def test_list_json_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)

    result = runner.invoke(app, ["list", "--json"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data == {}


def test_status_not_initialized(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["status"])

    assert result.exit_code != 0
    assert "not initialized" in result.output.lower()


def test_status_serve_not_running(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "not running" in result.output.lower()


def test_new_requires_serve(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)

    result = runner.invoke(app, ["new", "test"])

    assert result.exit_code == 1
    assert "OSW serve is not running for this directory." in result.output
    assert "python osw.py serve" in result.output


def test_use_requires_serve(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)

    result = runner.invoke(app, ["use", "--terminal", "t1", "test"])

    assert result.exit_code == 1
    assert "OSW serve is not running for this directory." in result.output


def test_all_requires_serve(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)

    result = runner.invoke(app, ["all", "test"])

    assert result.exit_code == 1
    assert "OSW serve is not running for this directory." in result.output


def test_del_requires_serve(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_mod.init_state_dir(tmp_path)

    result = runner.invoke(app, ["del", "agent_001"])

    assert result.exit_code == 1
    assert "OSW serve is not running for this directory." in result.output
