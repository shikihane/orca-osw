from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import anyio
import pytest
from typer.testing import CliRunner

from osw import fanout as fanout_mod
from osw.cli import app

runner = CliRunner()
SCRIPT = str(Path(fanout_mod.__file__).resolve().parents[1] / "osw.py")


def _all(result) -> str:
    """stdout plus stderr: click 8.4 keeps them in separate buffers."""
    return (result.output or "") + (getattr(result, "stderr", "") or "")


class Spawn:
    """The patched spawn layer: records argv lists and answers from a table.

    Dispatch answers are keyed by the task text (argv[4] of a `new`
    invocation); the mailbox start answers from `mailbox`.
    """

    def __init__(self, *, mailbox=None, created=None, failures=None, delay=0.0):
        self.calls: list[list[str]] = []
        self.mailbox = mailbox or fanout_mod.CommandResult(
            0, '{"running": true, "pid": 4242}', "",
        )
        self.created = created or {}
        self.failures = failures or {}
        self.delay = delay
        self.active = 0
        self.peak = 0

    async def __call__(self, argv, cwd, *, timeout_s=300.0):
        self.calls.append([str(part) for part in argv])
        if self.delay:
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                await anyio.sleep(self.delay)
            finally:
                self.active -= 1
        if argv[2] == "mailbox":
            return self.mailbox
        task = argv[4]
        if task in self.failures:
            return self.failures[task]
        agent_id, terminal = self.created.get(task, ("code_001", "term_aaa"))
        return fanout_mod.CommandResult(
            0, f"Created {agent_id} on terminal {terminal} (provider: pi)\n", "",
        )

    def dispatches(self) -> list[list[str]]:
        return [call for call in self.calls if call[2] == "new"]


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _run(args, spawn: Spawn, tmp_path):
    with patch.object(fanout_mod, "run_command", spawn):
        return runner.invoke(app, ["super", *args])


# ---------------------------------------------------------------------------
# Flags and plan resolution
# ---------------------------------------------------------------------------

def test_flag_defaults_build_one_new_argv_per_task(project):
    spawn = Spawn()
    result = _run([
        "--provider", "pi", "--model", "fast", "--thinking", "high",
        "--prefix", "code", "--caller-terminal", "term_boss",
        "--task", "first task", "--task", "second task",
        "--dry-run", "--json",
    ], spawn, project)

    assert result.exit_code == 0, _all(result)
    assert spawn.calls == []
    data = json.loads(result.output)
    assert set(data) == {"dry_run", "mailbox", "dispatches"}
    assert data["mailbox"] == [
        sys.executable, SCRIPT, "mailbox", "start", "--json",
    ]
    assert data["dispatches"] == [
        [
            sys.executable, SCRIPT, "new", "pi", "first task",
            "--prefix", "code", "--model", "fast", "--thinking", "high",
            "--caller-terminal", "term_boss",
        ],
        [
            sys.executable, SCRIPT, "new", "pi", "second task",
            "--prefix", "code", "--model", "fast", "--thinking", "high",
            "--caller-terminal", "term_boss",
        ],
    ]


def test_no_notify_is_forwarded_only_without_a_caller(project):
    spawn = Spawn()
    result = _run([
        "--provider", "pi", "--task", "a", "--task", "b",
        "--caller-terminal", "term_boss", "--no-notify",
        "--dry-run", "--json",
    ], spawn, project)

    assert result.exit_code == 0, _all(result)
    dispatches = json.loads(result.output)["dispatches"]
    assert dispatches[0][-2:] == ["--caller-terminal", "term_boss"]
    assert "--no-notify" not in dispatches[0]

    quiet = _run([
        "--provider", "pi", "--task", "a", "--no-notify",
        "--no-mailbox", "--dry-run", "--json",
    ], spawn, project)
    assert quiet.exit_code == 0, _all(quiet)
    data = json.loads(quiet.output)
    assert data["mailbox"] is None
    assert data["dispatches"][0][-1] == "--no-notify"


def test_plan_tasks_override_the_plan_and_the_flags(project):
    plan = project / "plan.json"
    plan.write_text(json.dumps({
        "provider": "pi",
        "model": "plan-model",
        "thinking": "high",
        "prefix": "code",
        "max_parallel": 3,
        "tasks": [
            "plain task",
            {
                "task": "second task",
                "provider": "codex",
                "model": "gpt-5",
                "thinking": "low",
                "prefix": "review",
                "caller_terminal": "term_y",
            },
        ],
    }), encoding="utf-8")
    spawn = Spawn()
    result = _run([
        "--plan", str(plan), "--provider", "claude", "--dry-run", "--json",
    ], spawn, project)

    assert result.exit_code == 0, _all(result)
    first, second = json.loads(result.output)["dispatches"]
    # The explicit flag wins over the plan's top-level default ...
    assert first == [
        sys.executable, SCRIPT, "new", "claude", "plain task",
        "--prefix", "code", "--model", "plan-model", "--thinking", "high",
    ]
    # ... and the per-task object wins over both.
    assert second == [
        sys.executable, SCRIPT, "new", "codex", "second task",
        "--prefix", "review", "--model", "gpt-5", "--thinking", "low",
        "--caller-terminal", "term_y",
    ]


def test_plan_must_be_an_object_with_a_non_empty_tasks_array(project):
    spawn = Spawn()
    bad = project / "bad.json"
    bad.write_text(json.dumps({"tasks": []}), encoding="utf-8")
    result = _run(["--plan", str(bad), "--dry-run"], spawn, project)
    assert result.exit_code == 1
    assert "non-empty tasks array" in _all(result)

    empty = project / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    result = _run(["--plan", str(empty), "--dry-run"], spawn, project)
    assert result.exit_code == 1
    assert "JSON object" in _all(result)


def test_no_tasks_and_missing_provider_are_refused(project):
    spawn = Spawn()
    result = _run(["--provider", "pi"], spawn, project)
    assert result.exit_code == 1
    assert "no tasks" in _all(result)

    result = _run(["--task", "a"], spawn, project)
    assert result.exit_code == 1
    assert "no provider" in _all(result)


# ---------------------------------------------------------------------------
# Positional tasks and zero-flag project defaults
# ---------------------------------------------------------------------------

POLICY = """# Guide

## OSW Provider Policy

| Task type | Provider | Model | Thinking |
|---|---|---|---|
| implementation | claude | opus | low |
| batch fan-out (`osw super`) | pi | policy-model | high |

## Next

| Task type | Provider | Model | Thinking |
|---|---|---|---|
| batch fan-out | codex | ignored | low |
"""

ENV_NAMES = (
    "OSW_PROVIDER", "OSW_MODEL", "OSW_THINKING", "OSW_PREFIX",
    "OSW_MAX_PARALLEL",
)


@pytest.fixture()
def policy_project(project, monkeypatch):
    """A project whose AGENTS.md carries a batch fan-out policy row."""
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    (project / "AGENTS.md").write_text(POLICY, encoding="utf-8")
    return project


def test_positional_tasks_use_the_agents_policy(policy_project):
    spawn = Spawn()
    result = _run([
        "task one", "task two", "--task", "task three", "--dry-run", "--json",
    ], spawn, policy_project)

    assert result.exit_code == 0, _all(result)
    dispatches = json.loads(result.output)["dispatches"]
    assert [argv[4] for argv in dispatches] == ["task one", "task two", "task three"]
    assert dispatches[0] == [
        sys.executable, SCRIPT, "new", "pi", "task one",
        "--prefix", "code", "--model", "policy-model", "--thinking", "high",
    ]


def test_config_file_supplies_the_defaults(project, policy_project):
    config = project / ".orca" / "osw" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"super": {
        "provider": "codex", "model": "cfg-model", "thinking": "low",
        "prefix": "rev", "max_parallel": 1,
    }}), encoding="utf-8")
    spawn = Spawn()
    result = _run(["a", "--dry-run", "--json"], spawn, project)

    assert result.exit_code == 0, _all(result)
    assert json.loads(result.output)["dispatches"][0] == [
        sys.executable, SCRIPT, "new", "codex", "a",
        "--prefix", "rev", "--model", "cfg-model", "--thinking", "low",
    ]

    # A malformed config file is ignored, not fatal: the policy row stands in.
    config.write_text("{not json", encoding="utf-8")
    result = _run(["a", "--dry-run", "--json"], spawn, project)
    assert result.exit_code == 0, _all(result)
    assert json.loads(result.output)["dispatches"][0][3] == "pi"


def test_config_max_parallel_caps_concurrency(project):
    config = project / ".orca" / "osw" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"super": {
        "provider": "pi", "max_parallel": 1,
    }}), encoding="utf-8")
    spawn = Spawn(delay=0.01)
    result = _run(["a", "b", "c", "--no-mailbox", "--json"], spawn, project)

    assert result.exit_code == 0, _all(result)
    assert spawn.peak == 1


def test_env_beats_agents_and_flags_beat_everything(project, policy_project, monkeypatch):
    spawn = Spawn()
    monkeypatch.setenv("OSW_PROVIDER", "codex")
    monkeypatch.setenv("OSW_MODEL", "env-model")
    monkeypatch.setenv("OSW_THINKING", "medium")

    result = _run(["a", "--dry-run", "--json"], spawn, project)
    assert result.exit_code == 0, _all(result)
    assert json.loads(result.output)["dispatches"][0] == [
        sys.executable, SCRIPT, "new", "codex", "a",
        "--prefix", "code", "--model", "env-model", "--thinking", "medium",
    ]

    result = _run([
        "a", "--provider", "claude", "--model", "flag-model",
        "--thinking", "low", "--prefix", "impl", "--dry-run", "--json",
    ], spawn, project)
    assert result.exit_code == 0, _all(result)
    assert json.loads(result.output)["dispatches"][0] == [
        sys.executable, SCRIPT, "new", "claude", "a",
        "--prefix", "impl", "--model", "flag-model", "--thinking", "low",
    ]


def test_a_missing_provider_names_the_three_places_and_dispatches_nothing(project):
    spawn = Spawn()
    result = _run(["a", "b", "--no-mailbox"], spawn, project)

    assert result.exit_code == 1
    text = _all(result)
    assert "no provider" in text
    assert ".orca/osw/config.json" in text
    assert "OSW_PROVIDER" in text
    assert "AGENTS.md" in text
    assert spawn.calls == []


def test_plan_and_a_positional_task_are_refused(project):
    plan = project / "plan.json"
    plan.write_text(json.dumps({"provider": "pi", "tasks": ["a"]}), encoding="utf-8")
    spawn = Spawn()
    result = _run(["b", "--plan", str(plan)], spawn, project)

    assert result.exit_code == 1
    assert "not both" in _all(result)
    assert spawn.calls == []


def test_dry_run_shows_the_resolved_provider_model_and_thinking(project, policy_project):
    spawn = Spawn()
    result = _run(["only task", "--no-mailbox", "--dry-run"], spawn, project)

    assert result.exit_code == 0, _all(result)
    assert spawn.calls == []
    line = next(l for l in result.output.splitlines() if l.startswith("task 1: "))
    assert '"new", "pi", "only task"' in line
    assert '"--model", "policy-model"' in line
    assert '"--thinking", "high"' in line


# ---------------------------------------------------------------------------
# Mailbox
# ---------------------------------------------------------------------------

def test_mailbox_start_runs_first_and_its_record_is_reported(project):
    spawn = Spawn()
    result = _run([
        "--provider", "pi", "--task", "a", "--json",
    ], spawn, project)

    assert result.exit_code == 0, _all(result)
    assert spawn.calls[0] == [
        sys.executable, SCRIPT, "mailbox", "start", "--json",
    ]
    assert json.loads(result.output)["mailbox"] == {"running": True, "pid": 4242}


def test_mailbox_start_failure_aborts_before_dispatch(project):
    spawn = Spawn()
    spawn.mailbox = fanout_mod.CommandResult(
        1, "", "Error: mailbox_unavailable: the daemon did not take the lock\n",
    )
    result = _run(["--provider", "pi", "--task", "a", "--task", "b"], spawn, project)

    assert result.exit_code == 1
    assert "mailbox_unavailable" in _all(result)
    assert len(spawn.calls) == 1
    assert spawn.dispatches() == []


def test_no_mailbox_never_starts_the_daemon(project):
    spawn = Spawn()
    result = _run([
        "--provider", "pi", "--task", "a", "--no-mailbox", "--json",
    ], spawn, project)

    assert result.exit_code == 0, _all(result)
    assert all(call[2] == "new" for call in spawn.calls)
    data = json.loads(result.output)
    assert data["mailbox"] is None
    assert data["started"] == 1


# ---------------------------------------------------------------------------
# Dispatch isolation, concurrency and output
# ---------------------------------------------------------------------------

def test_one_failed_dispatch_does_not_abort_the_rest(project):
    spawn = Spawn(
        created={"a": ("code_001", "term_a"), "c": ("code_003", "term_c")},
        failures={"b": fanout_mod.CommandResult(1, "", "Error: boom\n")},
    )
    result = _run([
        "--provider", "pi", "--prefix", "code",
        "--task", "a", "--task", "b", "--task", "c",
        "--no-mailbox", "--json",
    ], spawn, project)

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["requested"] == 3
    assert data["started"] == 2
    assert data["agents"] == [
        {"index": 1, "task": "a", "agent_id": "code_001", "terminal": "term_a"},
        {"index": 3, "task": "c", "agent_id": "code_003", "terminal": "term_c"},
    ]
    assert data["errors"] == [{"index": 2, "task": "b", "error": "Error: boom"}]
    assert len(spawn.dispatches()) == 3


def test_unparsable_output_is_an_error_for_that_task_only(project):
    spawn = Spawn()
    spawn.created["a"] = ("code_001", "term_a")
    spawn.failures["b"] = fanout_mod.CommandResult(0, "something else\n", "")
    result = _run([
        "--provider", "pi", "--task", "a", "--task", "b",
        "--no-mailbox", "--json",
    ], spawn, project)

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["started"] == 1
    assert "Created <agent_id>" in data["errors"][0]["error"]


def test_max_parallel_caps_concurrent_subprocesses(project):
    spawn = Spawn(delay=0.02)
    result = _run([
        "--provider", "pi", "--max-parallel", "2",
        *[arg for task in "abcdef" for arg in ("--task", task)],
        "--no-mailbox", "--json",
    ], spawn, project)

    assert result.exit_code == 0, _all(result)
    assert len(spawn.dispatches()) == 6
    assert spawn.peak == 2


def test_parallel_limit_is_clamped_to_one_through_thirty_two():
    assert fanout_mod.clamp_parallel(0) == 1
    assert fanout_mod.clamp_parallel(-5) == 1
    assert fanout_mod.clamp_parallel(8) == 8
    assert fanout_mod.clamp_parallel(999) == 32
    assert fanout_mod.clamp_parallel("3") == 3
    with pytest.raises(fanout_mod.FanoutError):
        fanout_mod.clamp_parallel("many")


def test_parallel_limit_comes_from_the_plan_when_no_flag_is_given(project):
    plan = project / "plan.json"
    plan.write_text(json.dumps({
        "provider": "pi", "max_parallel": 1, "tasks": ["a", "b", "c"],
    }), encoding="utf-8")
    spawn = Spawn(delay=0.01)
    result = _run(["--plan", str(plan), "--no-mailbox", "--json"], spawn, project)

    assert result.exit_code == 0, _all(result)
    assert spawn.peak == 1


def test_human_output_is_one_line_per_agent_plus_a_summary(project):
    spawn = Spawn(created={"a": ("code_001", "term_a")})
    result = _run([
        "--provider", "pi", "--task", "a", "--no-mailbox",
    ], spawn, project)

    assert result.exit_code == 0, _all(result)
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert lines == [
        "mailbox: off",
        "[1] code_001 term_a  a",
        "super: requested=1 started=1 errors=0",
    ]


def test_a_timed_out_dispatch_is_an_error_for_that_task_only(project):
    spawn = Spawn()

    async def slow(argv, cwd, *, timeout_s=300.0):
        if argv[4] == "b":
            raise TimeoutError
        return await spawn(argv, cwd, timeout_s=timeout_s)

    with patch.object(fanout_mod, "run_command", slow):
        result = runner.invoke(app, [
            "super", "--provider", "pi", "--task", "a", "--task", "b",
            "--no-mailbox", "--json",
        ])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["started"] == 1
    assert data["errors"][0]["error"] == "timed out after 300s"


def test_dry_run_executes_nothing(project):
    async def explode(argv, cwd, *, timeout_s=300.0):  # pragma: no cover
        raise AssertionError(f"dry run spawned {argv}")

    with patch.object(fanout_mod, "run_command", explode):
        result = runner.invoke(app, [
            "super", "--provider", "pi", "--task", "a", "--dry-run",
        ])

    assert result.exit_code == 0, _all(result)
    assert "mailbox start: " in result.output
    assert "task 1: " in result.output
