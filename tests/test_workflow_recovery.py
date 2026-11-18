"""Recovery: the goal a run follows, and the scheduler that keeps it moving.

``workflow goal`` is the one place a person changes what a run is for — a gate
may only propose. ``workflow resume`` gives a run whose scheduler died a new
one; liveness comes from the heartbeat the scheduler stamps, so a killed
scheduler is not mistaken for a busy one. The spawn seam is substituted, so no
process is started here.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from osw import fanout
from osw.cli import app
from osw.mailbox import identity
from osw.workflow import archive, board, defs, executor

runner = CliRunner()

RUN_ID = "20261118T090000Z_audit-routes_ab12cd"
WORKFLOW = "audit-routes"

RUN_DOC = {
    "run_id": RUN_ID,
    "definition": WORKFLOW,
    "definition_path": ".orca/osw/workflows/audit-routes.json",
    "state": "running",
    "policy": "strict",
    "verify": "suspect",
    "max_attempts": 2,
    "max_agents": 64,
    "keys_version": 2,
    "goal": "every route is audited and the gaps are written down",
    "goal_proposals": [],
    "scheduler": {"pid": 0, "host": "", "started_at": "", "beat_at": ""},
    "started": "2026-11-18T09:00:00Z",
    "phases": [
        {
            "id": "scan", "state": "done", "steps": ["k1"], "round": 1,
            "gate": {"round": 0, "step": "", "pass": None, "reason": ""},
        },
        {
            "id": "advance", "state": "running", "steps": [], "round": 2,
            "gate": {"round": 1, "step": "gate1", "pass": False,
                     "reason": "two items are still open"},
        },
    ],
    "steps": [{"step_id": "k1", "phase": "scan", "state": "done"}],
}

REPEAT_DEFINITION = {
    "name": WORKFLOW,
    "goal": "every route is audited",
    "max_rounds": 3,
    "phases": [
        {
            "id": "scan", "mode": "single", "task": "List the routes",
            "criteria": "each route is listed", "evidence": ["routes.json"],
        },
        {
            "id": "advance", "mode": "for_each", "from": "scan",
            "task": "Audit {{item}}", "criteria": "every item is audited",
            "repeat": True,
        },
    ],
}


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Definitions and runs stay inside the temp project root."""
    monkeypatch.setattr(defs, "home_workflows_dir", lambda: tmp_path / "home")
    # A Router marker in the ambient environment would turn a human verb into
    # a refusal; each test sets one only when it means to.
    monkeypatch.delenv(identity.ROLE_ENV, raising=False)
    monkeypatch.delenv(identity.TOKEN_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _seed_run(root: Path, **overrides) -> dict:
    """A hand-written archive: the run header, and nothing else."""
    payload = {**RUN_DOC, **overrides}
    path = board.runs_dir(root) / payload["run_id"] / board.RUN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _install_definition(root: Path, payload: dict = REPEAT_DEFINITION) -> Path:
    path = root / ".orca" / "osw" / "workflows" / f"{WORKFLOW}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _beat(pid: int) -> dict:
    stamp = archive.now_iso()
    return {"pid": pid, "host": "local", "started_at": stamp, "beat_at": stamp}


# ---------------------------------------------------------------------------
# goal: what the run is for
# ---------------------------------------------------------------------------

def test_goal_prints_the_goal_proposals_and_rounds(_isolated_home):
    _seed_run(_isolated_home, goal_proposals=[
        {"round": 1, "phase": "advance", "text": "widen the audit to cron"},
    ])

    result = runner.invoke(app, ["workflow", "goal", RUN_ID, "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["goal"] == RUN_DOC["goal"]
    assert [item["text"] for item in payload["proposals"]] == [
        "widen the audit to cron",
    ]
    assert payload["rounds"] == {"scan": 1, "advance": 2}


def test_goal_text_names_the_goal_and_the_rounds(_isolated_home):
    _seed_run(_isolated_home)

    result = runner.invoke(app, ["workflow", "goal", RUN_ID])

    assert result.exit_code == 0, result.output
    assert f"GOAL {RUN_ID} {RUN_DOC['goal']}" in result.output
    assert "PROPOSALS 0" in result.output
    assert "ROUNDS scan=1, advance=2" in result.output


def test_goal_set_records_the_proposal_and_rewrites_the_goal(_isolated_home):
    _seed_run(_isolated_home)

    result = runner.invoke(app, [
        "workflow", "goal", RUN_ID,
        "--set", "every route and every cron job is audited",
        "--reason", "the request covered cron too",
    ])

    assert result.exit_code == 0, result.output
    saved = board.read_run(_isolated_home, RUN_ID)
    assert saved["goal"] == "every route and every cron job is audited"
    assert saved["goal_proposals"] == [{
        "round": 0,
        "phase": "human",
        "text": "every route and every cron job is audited",
        "accepted": True,
    }]
    assert "the request covered cron too" in result.output


def test_goal_set_appends_to_what_a_gate_proposed(_isolated_home):
    _seed_run(_isolated_home, goal_proposals=[
        {"round": 1, "phase": "advance", "text": "widen the audit to cron"},
    ])

    result = runner.invoke(app, ["workflow", "goal", RUN_ID, "--set", "keep it wide"])

    assert result.exit_code == 0, result.output
    saved = board.read_run(_isolated_home, RUN_ID)
    assert [item["phase"] for item in saved["goal_proposals"]] == ["advance", "human"]
    assert saved["goal"] == "keep it wide"


def test_router_identity_cannot_set_the_goal(_isolated_home, monkeypatch):
    root = _isolated_home
    _seed_run(root)
    monkeypatch.setenv(identity.ROLE_ENV, identity.ROUTER_ROLE)
    monkeypatch.setenv(identity.TOKEN_ENV, identity.generate_token(root))

    result = runner.invoke(app, ["workflow", "goal", RUN_ID, "--set", "mine"])

    assert result.exit_code == 1
    assert "router_forbidden" in result.output
    assert board.read_run(root, RUN_ID)["goal"] == RUN_DOC["goal"]


@pytest.mark.parametrize("argv", [
    ["workflow", "goal", "run_404"],
    ["workflow", "goal", "run_404", "--set", "anything"],
    ["workflow", "resume", "run_404"],
])
def test_unknown_run_is_refused(_isolated_home, argv):
    result = runner.invoke(app, argv)

    assert result.exit_code == 1
    assert "run_not_found" in result.output


# ---------------------------------------------------------------------------
# resume: one scheduler per archive
# ---------------------------------------------------------------------------

def test_resume_refuses_a_live_scheduler(_isolated_home, monkeypatch):
    _seed_run(_isolated_home, scheduler=_beat(os.getpid()))
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        "osw.workflow_cli._spawn_detached",
        lambda argv, root, sink: spawned.append(list(argv)),
    )

    result = runner.invoke(app, ["workflow", "resume", RUN_ID])

    assert result.exit_code == 1
    assert "run_running" in result.output
    assert spawned == []


def test_resume_spawns_the_detached_scheduler(_isolated_home, monkeypatch):
    root = _isolated_home
    _seed_run(root)
    calls: dict = {}

    def fake_spawn(argv, spawn_root, sink):
        calls.update(argv=list(argv), root=spawn_root)
        return SimpleNamespace(pid=4242)

    monkeypatch.setattr("osw.workflow_cli._spawn_detached", fake_spawn)

    result = runner.invoke(app, ["workflow", "resume", RUN_ID])

    assert result.exit_code == 0, result.output
    assert calls["argv"] == [
        sys.executable, str(fanout.script_path()), "workflow", "run",
        WORKFLOW, "--run", RUN_ID, "--detach",
    ]
    assert calls["root"] == root
    assert f"RESUMED {RUN_ID} pid 4242" in result.output
    log_path = board.runs_dir(root) / RUN_ID / "scheduler.log"
    assert str(log_path) in result.output


def test_resume_json_reports_the_pid_and_the_log(_isolated_home, monkeypatch):
    root = _isolated_home
    _seed_run(root)
    monkeypatch.setattr(
        "osw.workflow_cli._spawn_detached",
        lambda argv, spawn_root, sink: SimpleNamespace(pid=4242),
    )

    result = runner.invoke(app, ["workflow", "resume", RUN_ID, "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "resumed": True,
        "run": RUN_ID,
        "pid": 4242,
        "log": str(board.runs_dir(root) / RUN_ID / "scheduler.log"),
    }


def test_resume_does_not_rewrite_the_archive(_isolated_home, monkeypatch):
    root = _isolated_home
    seeded = _seed_run(root)
    monkeypatch.setattr(
        "osw.workflow_cli._spawn_detached",
        lambda argv, spawn_root, sink: SimpleNamespace(pid=4242),
    )

    runner.invoke(app, ["workflow", "resume", RUN_ID])

    assert board.read_run(root, RUN_ID) == seeded


# ---------------------------------------------------------------------------
# run --detach: the scheduler outlives the shell
# ---------------------------------------------------------------------------

def test_run_detach_creates_the_run_and_spawns_one_supervisor(_isolated_home, monkeypatch):
    root = _isolated_home
    _install_definition(root, {
        "name": WORKFLOW,
        "phases": [{"id": "scan", "mode": "single", "task": "List the routes"}],
    })
    created = {"run_id": "run_new", "definition": WORKFLOW}
    monkeypatch.setattr(
        "osw.workflow.executor.start_run", lambda *args, **kwargs: dict(created),
    )
    calls: dict = {}

    def fake_spawn(argv, spawn_root, sink):
        calls.update(argv=list(argv))
        return SimpleNamespace(pid=909)

    # The supervisor owns the scheduler now, so that is the child `--detach`
    # starts; the revive decision travels on the run, not on the argv.
    monkeypatch.setattr("osw.workflow.executor._spawn_detached", fake_spawn)

    result = runner.invoke(
        app, ["workflow", "run", WORKFLOW, "--detach", "--no-revive-lost"],
    )

    assert result.exit_code == 0, result.output
    assert calls["argv"] == [
        sys.executable, str(fanout.script_path()), "workflow",
        "supervise-worker", "--run", "run_new",
    ]
    assert "DETACHED run_new pid 909" in result.output
    recorded = json.loads(
        (board.runs_dir(root) / "run_new" / board.RUN_FILE).read_text(encoding="utf-8")
    )
    assert recorded["revive_lost"] is False


def test_a_run_named_with_run_is_driven_here_not_spawned_again(_isolated_home, monkeypatch):
    """``resume`` starts this exact argv; it must not fork a second scheduler."""
    root = _isolated_home
    _seed_run(root)
    _install_definition(root, {
        "name": WORKFLOW,
        "phases": [{"id": "scan", "mode": "single", "task": "List the routes"}],
    })
    calls: dict = {}

    def fake_run_run(root_arg, definition, *, policy, verify, max_attempts, resume,
                     approved, run_id=None, **kwargs):
        calls.update(run_id=run_id, **kwargs)
        return board.read_run(root, RUN_ID)

    monkeypatch.setattr("osw.workflow.executor.run_run", fake_run_run)
    monkeypatch.setattr(
        "osw.workflow_cli._spawn_detached",
        lambda argv, spawn_root, sink: pytest.fail("a resumed run must not respawn"),
    )

    result = runner.invoke(app, [
        "workflow", "run", WORKFLOW, "--run", RUN_ID, "--detach", "--no-revive-lost",
    ])

    assert result.exit_code == 0, result.output
    assert calls["run_id"] == RUN_ID
    assert calls["revive_lost"] is False
    assert f"run {RUN_ID}" in result.output


def test_run_detach_resume_spawns_the_run_it_would_have_continued(_isolated_home, monkeypatch):
    root = _isolated_home
    _seed_run(root)
    _install_definition(root, {
        "name": WORKFLOW,
        "phases": [{"id": "scan", "mode": "single", "task": "List the routes"}],
    })
    monkeypatch.setattr(
        "osw.workflow.executor.start_run",
        lambda *args, **kwargs: pytest.fail("--resume must not create a run"),
    )
    calls: dict = {}

    def fake_spawn(argv, spawn_root, sink):
        calls.update(argv=list(argv))
        return SimpleNamespace(pid=909)

    monkeypatch.setattr("osw.workflow.executor._spawn_detached", fake_spawn)

    result = runner.invoke(app, ["workflow", "run", WORKFLOW, "--resume", "--detach"])

    assert result.exit_code == 0, result.output
    assert calls["argv"][-2:] == ["--run", RUN_ID]
    assert "supervise-worker" in calls["argv"]
    assert f"DETACHED {RUN_ID} pid 909" in result.output


def test_run_detach_resume_refuses_a_live_scheduler(_isolated_home, monkeypatch):
    root = _isolated_home
    _seed_run(root, scheduler=_beat(os.getpid()))
    _install_definition(root, {
        "name": WORKFLOW,
        "phases": [{"id": "scan", "mode": "single", "task": "List the routes"}],
    })
    spawned: list = []
    monkeypatch.setattr(
        "osw.workflow.executor._spawn_detached",
        lambda argv, spawn_root, sink: spawned.append(list(argv)),
    )

    result = runner.invoke(app, ["workflow", "run", WORKFLOW, "--resume", "--detach"])

    assert result.exit_code == 1
    assert "run_running" in result.output
    assert spawned == []


# ---------------------------------------------------------------------------
# The read-only surface: rounds, gates and a planner that died
# ---------------------------------------------------------------------------

def test_dry_run_shows_rounds_and_gate_columns(_isolated_home):
    _install_definition(_isolated_home)

    result = runner.invoke(app, ["workflow", "run", WORKFLOW, "--dry-run"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    header = next(line for line in lines if "ROUNDS" in line)
    assert header.split() == ["PHASE", "MODE", "FROM", "TASKS", "ROUNDS", "GATE"]
    scan = next(line for line in lines if line.startswith("scan "))
    advance = next(line for line in lines if line.startswith("advance "))
    # A phase without a gate is one round and no gate.
    assert scan.split()[-2:] == ["1", "criteria"]
    assert advance.split()[-2:] == ["3", "criteria"]


def test_dry_run_marks_a_phase_without_a_gate(_isolated_home):
    _install_definition(_isolated_home, {
        "name": WORKFLOW,
        "phases": [{"id": "scan", "mode": "single", "task": "List the routes"}],
    })

    result = runner.invoke(app, ["workflow", "run", WORKFLOW, "--dry-run"])

    assert result.exit_code == 0, result.output
    scan = next(line for line in result.output.splitlines() if line.startswith("scan "))
    assert scan.split()[-2:] == ["1", "-"]


def test_status_json_exposes_goal_rounds_and_gate(_isolated_home):
    _seed_run(_isolated_home, goal_proposals=[
        {"round": 1, "phase": "advance", "text": "widen the audit to cron"},
    ])

    result = runner.invoke(app, ["workflow", "status", RUN_ID, "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["scheduler_alive"] is False
    assert payload["goal"] == RUN_DOC["goal"]
    assert payload["goal_proposals"] == 1
    phases = {phase["id"]: phase for phase in payload["phases"]}
    assert phases["scan"]["round"] == 1
    assert phases["scan"]["gate"] == {"round": 0, "pass": None, "reason": ""}
    assert phases["advance"]["round"] == 2
    assert phases["advance"]["gate"] == {
        "round": 1, "pass": False, "reason": "two items are still open",
    }


def test_status_json_reports_a_live_scheduler(_isolated_home):
    _seed_run(_isolated_home, scheduler=_beat(os.getpid()))

    result = runner.invoke(app, ["workflow", "status", RUN_ID, "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["scheduler_alive"] is True


def test_status_reports_a_dead_planner(_isolated_home):
    executor.write_planning(_isolated_home, stem="audit", pid=0, request="audit")

    payload = json.loads(runner.invoke(
        app, ["workflow", "status", "--json"],
    ).output)

    assert payload["planning"][0]["alive"] is False
    assert payload["planning"][0]["code"] == "planner_died"

    text = runner.invoke(app, ["workflow", "status"])
    assert text.exit_code == 0, text.output
    assert "plan_failed: planner_died" in text.output
