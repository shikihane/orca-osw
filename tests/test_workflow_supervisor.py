"""The supervisor, and the two verbs that keep a person out of the loop.

A run's scheduler has an owner process: it waits for the scheduler, restarts it
while the run is still ``running``, and writes one sidecar of its own so
``status`` can say what happened. Nothing here starts a real process: the spawn
seams are substituted, and ``sleep`` is replaced where timing would matter.

``workflow next`` continues a run that ended without meeting its goal, and
``workflow wait`` blocks on a run (or the stem a drafting job announced) until it
is terminal. Between them an agent can carry a goal to its end with no command
printed for a person to type.
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
from osw.workflow import board, defs, executor
from osw.workflow_cli import _wait_for_run

runner = CliRunner()

RUN_ID = "20261118T090000Z_audit-routes_ab12cd"
STEM = "audit-routes"
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
            "gate": {"round": 1, "step": "gate1", "pass": True,
                     "reason": "every route is listed"},
        },
        {
            "id": "advance", "state": "done", "steps": [], "round": 2,
            "gate": {"round": 2, "step": "gate2", "pass": False,
                     "reason": "two items are still open"},
        },
    ],
    "steps": [{"step_id": "k1", "phase": "scan", "state": "done"}],
}


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Definitions, runs and sidecars stay inside the temp project root."""
    monkeypatch.setattr(defs, "home_workflows_dir", lambda: tmp_path / "home")
    monkeypatch.delenv(identity.ROLE_ENV, raising=False)
    monkeypatch.delenv(identity.TOKEN_ENV, raising=False)
    monkeypatch.delenv(executor.RESTARTS_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _seed_run(root: Path, **overrides) -> dict:
    """A hand-written archive: the run header, and nothing else."""
    payload = {**RUN_DOC, **overrides}
    path = board.runs_dir(root) / payload["run_id"] / board.RUN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _run_fingerprint(root: Path) -> tuple[bytes, int]:
    """The bytes and mtime of run.json: what a read-only verb must not touch."""
    path = board.runs_dir(root) / RUN_ID / board.RUN_FILE
    return path.read_bytes(), path.stat().st_mtime_ns


def _install_definition(root: Path, name: str, payload: dict | None = None) -> Path:
    path = root / ".orca" / "osw" / "workflows" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload or {
        "name": name,
        "goal": RUN_DOC["goal"],
        "phases": [{"id": "scan", "mode": "single", "task": "List the routes"}],
    }), encoding="utf-8")
    return path


class _Child:
    """A scheduler child that exited with one code and did nothing else."""

    def __init__(self, code: int) -> None:
        self.code = code

    def wait(self) -> int:
        return self.code


# ---------------------------------------------------------------------------
# The supervisor: restarts are a process, not a suggestion
# ---------------------------------------------------------------------------

def test_supervisor_restarts_a_scheduler_that_dies_under_a_live_run(_isolated_home):
    root = _isolated_home
    _seed_run(root)
    sidecar = executor.supervisor_path(root, RUN_ID)
    seen: list[int] = []

    def fake_spawn(spawn_root, definition, run_id):
        # Snapshot what the record says *before* each scheduler starts: every
        # restart has to be on disk by then.
        seen.append(json.loads(sidecar.read_text(encoding="utf-8"))["restarts"])
        return _Child(1)

    written: list[float] = []
    record = executor.supervise_run(
        root, RUN_ID, restarts=2, backoff=5.0,
        spawn=fake_spawn, sleep=written.append,
    )

    assert seen == [0, 1, 2]
    assert record["restarts"] == 2
    assert record["last_exit"] == 1
    assert record["pid"] == os.getpid()
    assert written == [5.0, 5.0]
    log = executor.supervisor_log_path(root, RUN_ID).read_text(encoding="utf-8")
    assert "[supervisor] scheduler exited 1; restart 1/2" in log
    assert "[supervisor] scheduler exited 1; restart 2/2" in log


def test_supervisor_stops_once_the_restart_cap_is_spent(_isolated_home):
    root = _isolated_home
    _seed_run(root)
    spawns: list[int] = []

    def fake_spawn(spawn_root, definition, run_id):
        spawns.append(1)
        return _Child(9)

    record = executor.supervise_run(
        root, RUN_ID, restarts=0, backoff=0, spawn=fake_spawn, sleep=lambda _: None,
    )

    assert len(spawns) == 1
    assert record["restarts"] == 0
    assert record["last_exit"] == 9


def test_supervisor_does_not_restart_a_run_that_is_terminal(_isolated_home):
    root = _isolated_home
    _seed_run(root, state="done")
    spawns: list[int] = []

    def fake_spawn(spawn_root, definition, run_id):
        spawns.append(1)
        return _Child(1)

    record = executor.supervise_run(
        root, RUN_ID, restarts=3, backoff=0, spawn=fake_spawn, sleep=lambda _: None,
    )

    assert len(spawns) == 1
    assert record["last_exit"] == 1
    assert record["restarts"] == 0
    log = executor.supervisor_log_path(root, RUN_ID)
    assert not log.exists() or "restart" not in log.read_text(encoding="utf-8")


def test_supervisor_writes_its_sidecar_and_never_the_run(_isolated_home):
    root = _isolated_home
    _seed_run(root)
    before = _run_fingerprint(root)

    executor.supervise_run(
        root, RUN_ID, restarts=1, backoff=0,
        spawn=lambda *args: _Child(2), sleep=lambda _: None,
    )

    assert _run_fingerprint(root) == before
    assert executor.supervisor_path(root, RUN_ID) != board.run_file(root, RUN_ID)
    saved = json.loads(
        executor.supervisor_path(root, RUN_ID).read_text(encoding="utf-8")
    )
    assert saved == {
        "pid": os.getpid(),
        "started_at": saved["started_at"],
        "restarts": 1,
        "last_exit": 2,
    }
    assert saved["started_at"]


def test_supervisor_refuses_a_run_that_names_no_definition(_isolated_home, monkeypatch):
    root = _isolated_home
    _seed_run(root, definition="")

    with pytest.raises(defs.WorkflowError) as caught:
        executor.supervise_run(root, RUN_ID, spawn=lambda *args: _Child(0))

    assert caught.value.code == "run_not_found"


def test_restart_cap_comes_from_the_environment(monkeypatch):
    assert executor.supervisor_restarts() == 3
    monkeypatch.setenv(executor.RESTARTS_ENV, "7")
    assert executor.supervisor_restarts() == 7
    monkeypatch.setenv(executor.RESTARTS_ENV, "not-a-number")
    assert executor.supervisor_restarts() == executor.SUPERVISOR_RESTARTS_DEFAULT


def test_supervise_worker_command_runs_the_loop(_isolated_home, monkeypatch):
    calls: dict = {}

    def fake_supervise(root, run_id, **kwargs):
        calls.update(root=root, run_id=run_id)
        return {"pid": 1, "started_at": "now", "restarts": 2, "last_exit": 1}

    monkeypatch.setattr("osw.workflow.executor.supervise_run", fake_supervise)

    result = runner.invoke(app, ["workflow", "supervise-worker", "--run", RUN_ID])

    assert result.exit_code == 0, result.output
    assert calls["run_id"] == RUN_ID
    assert f"SUPERVISOR {RUN_ID} restarts 2 last_exit 1" in result.output


# ---------------------------------------------------------------------------
# Who owns the scheduler: `go-worker` and `run --detach`
# ---------------------------------------------------------------------------

def _capture_spawn(monkeypatch, *, pid: int = 909) -> dict:
    calls: dict = {}

    def fake_spawn(argv, root, sink):
        calls.update(argv=list(argv), root=root)
        return SimpleNamespace(pid=pid)

    monkeypatch.setattr("osw.workflow.executor._spawn_detached", fake_spawn)
    return calls


def test_go_worker_hands_the_run_to_a_supervisor(_isolated_home, monkeypatch):
    root = _isolated_home
    definition = defs.Definition(
        name=STEM, max_agents=8,
        phases=(defs.Phase(id="scan", mode="single", from_ids=(), task="List"),),
        path=Path("audit-routes.json"), goal=RUN_DOC["goal"],
    )
    monkeypatch.setattr(
        "osw.workflow.author.author",
        lambda root_arg, request, name="": (Path("audit-routes.json"), definition),
    )
    monkeypatch.setattr(
        "osw.workflow.executor.start_run",
        lambda *args, **kwargs: {"run_id": "run_new", "definition": STEM},
    )
    monkeypatch.setattr(
        "osw.workflow.executor.run_run",
        lambda *args, **kwargs: pytest.fail("go-worker must not schedule in-process"),
    )
    calls = _capture_spawn(monkeypatch)
    request_file = root / "request.txt"
    request_file.write_text(RUN_DOC["goal"], encoding="utf-8")

    result = runner.invoke(app, [
        "workflow", "go-worker", "--stem", STEM,
        "--request-file", str(request_file),
    ])

    assert result.exit_code == 0, result.output
    assert calls["argv"] == [
        sys.executable, str(fanout.script_path()), "workflow",
        "supervise-worker", "--run", "run_new",
    ]
    assert "DONE audit-routes run run_new" in result.output


def test_run_detach_starts_a_supervisor_not_a_scheduler(_isolated_home, monkeypatch):
    root = _isolated_home
    _install_definition(root, WORKFLOW)
    monkeypatch.setattr(
        "osw.workflow.executor.start_run",
        lambda *args, **kwargs: {"run_id": "run_new", "definition": WORKFLOW},
    )
    calls = _capture_spawn(monkeypatch)

    result = runner.invoke(app, ["workflow", "run", WORKFLOW, "--detach"])

    assert result.exit_code == 0, result.output
    assert "supervise-worker" in calls["argv"]
    assert calls["argv"][-2:] == ["--run", "run_new"]
    assert str(executor.supervisor_log_path(root, "run_new")) in result.output


# ---------------------------------------------------------------------------
# next: one command from a finished-but-unmet run to the next batch
# ---------------------------------------------------------------------------

def _capture_planner(monkeypatch, *, pid: int = 4242) -> dict:
    calls: dict = {}

    def fake_planner(root, *, stem, request, **kwargs):
        calls.update(root=root, stem=stem, request=request)
        return {
            "pid": pid, "log": "planning.log",
            "marker": "planning.json", "request_file": "request.txt",
        }

    monkeypatch.setattr("osw.workflow.executor.spawn_planner", fake_planner)
    return calls


@pytest.mark.parametrize("run_state,code", [
    ("running", "run_running"),
    ("done", "no_goal"),
])
def test_next_refuses_a_run_it_cannot_continue(_isolated_home, monkeypatch, run_state, code):
    root = _isolated_home
    overrides = {"state": run_state}
    if code == "no_goal":
        overrides["goal"] = ""
    _seed_run(root, **overrides)
    calls = _capture_planner(monkeypatch)

    result = runner.invoke(app, ["workflow", "next", RUN_ID])

    assert result.exit_code == 1
    assert code in result.output
    assert calls == {}


def test_next_plans_the_first_continuation(_isolated_home, monkeypatch):
    root = _isolated_home
    _seed_run(root, state="partial", goal_proposals=[
        {"round": 1, "phase": "advance", "text": "widen the audit to cron"},
    ])
    calls = _capture_planner(monkeypatch)

    result = runner.invoke(app, ["workflow", "next", RUN_ID])

    assert result.exit_code == 0, result.output
    assert calls["stem"] == f"{WORKFLOW}-r2"
    # The goal, the last gate's reason and the open proposal are the request.
    assert RUN_DOC["goal"] in calls["request"]
    assert "two items are still open" in calls["request"]
    assert "widen the audit to cron" in calls["request"]
    assert len(calls["request"]) <= 4000
    assert f"PLANNING {WORKFLOW}-r2  pid 4242" in result.output


def test_next_counts_the_batches_already_drafted(_isolated_home, monkeypatch):
    root = _isolated_home
    _seed_run(root, state="done")
    _install_definition(root, f"{WORKFLOW}-r2")
    calls = _capture_planner(monkeypatch)

    result = runner.invoke(app, ["workflow", "next", RUN_ID])

    assert result.exit_code == 0, result.output
    assert calls["stem"] == f"{WORKFLOW}-r3"


def test_next_honours_an_explicit_name(_isolated_home, monkeypatch):
    root = _isolated_home
    _seed_run(root, state="done")
    calls = _capture_planner(monkeypatch)

    result = runner.invoke(app, ["workflow", "next", RUN_ID, "--name", "audit-routes-2"])

    assert result.exit_code == 0, result.output
    assert calls["stem"] == "audit-routes-2"


def test_next_truncates_a_long_request(_isolated_home, monkeypatch):
    root = _isolated_home
    _seed_run(root, state="done", goal_proposals=[
        {"round": 1, "phase": "advance", "text": "x" * 6000},
    ])
    calls = _capture_planner(monkeypatch)

    result = runner.invoke(app, ["workflow", "next", RUN_ID])

    assert result.exit_code == 0, result.output
    assert len(calls["request"]) == 4000


# ---------------------------------------------------------------------------
# status: the sidecar is the only place restarts are recorded
# ---------------------------------------------------------------------------

def _write_sidecar(root: Path, **payload) -> Path:
    path = executor.supervisor_path(root, RUN_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "pid": 0, "started_at": "2026-11-18T09:00:00Z",
        "restarts": 0, "last_exit": None, **payload,
    }), encoding="utf-8")
    return path


def test_status_json_reports_no_supervisor(_isolated_home):
    _seed_run(_isolated_home)

    payload = json.loads(
        runner.invoke(app, ["workflow", "status", RUN_ID, "--json"]).output
    )

    assert payload["supervisor"] == {"pid": 0, "alive": False, "restarts": 0}


def test_status_json_reports_the_sidecar(_isolated_home):
    root = _isolated_home
    _seed_run(root)
    _write_sidecar(root, pid=os.getpid(), restarts=2, last_exit=1)

    payload = json.loads(
        runner.invoke(app, ["workflow", "status", RUN_ID, "--json"]).output
    )

    assert payload["supervisor"] == {
        "pid": os.getpid(), "alive": True, "restarts": 2,
    }


def test_status_text_shows_restarts_only_when_there_were_any(_isolated_home):
    root = _isolated_home
    _seed_run(root)

    quiet = runner.invoke(app, ["workflow", "status", RUN_ID]).output
    assert "SUPERVISOR" not in quiet

    _write_sidecar(root, pid=os.getpid(), restarts=3, last_exit=1)
    loud = runner.invoke(app, ["workflow", "status", RUN_ID]).output
    assert f"SUPERVISOR pid {os.getpid()} restarts 3" in loud


# ---------------------------------------------------------------------------
# wait: one foreground call, no background job, no writes
# ---------------------------------------------------------------------------

def test_wait_returns_when_the_run_is_terminal(_isolated_home):
    root = _isolated_home
    _seed_run(root, state="done")
    before = _run_fingerprint(root)

    result = runner.invoke(app, ["workflow", "wait", RUN_ID])

    assert result.exit_code == 0, result.output
    assert f"run {RUN_ID}" in result.output
    assert "done" in result.output
    assert _run_fingerprint(root) == before


def test_wait_expires_and_says_still_running(_isolated_home):
    root = _isolated_home
    _seed_run(root)
    before = _run_fingerprint(root)

    result = runner.invoke(app, ["workflow", "wait", RUN_ID, "--timeout", "0"])

    assert result.exit_code == 2
    assert "still running" in result.output
    assert _run_fingerprint(root) == before


def test_wait_json_reports_the_run_it_reached(_isolated_home):
    run = _seed_run(_isolated_home, state="done")

    result = runner.invoke(app, ["workflow", "wait", RUN_ID, "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["run_id"] == run["run_id"]
    assert payload["state"] == "done"


def test_wait_follows_a_stem_until_its_marker_names_a_run(_isolated_home):
    root = _isolated_home
    # No run of the stem exists yet: while the job drafts, only its marker can
    # say which run the stem will become.
    _seed_run(root, state="done", definition="earlier-batch")
    marker, _ = executor.planning_files(root, STEM)
    marker.parent.mkdir(parents=True, exist_ok=True)

    def marker_doc(run_id: str = "") -> str:
        return json.dumps({
            "stem": STEM, "pid": 0, "state": "planning", "run_id": run_id,
            "request": "", "error": "", "at": "2026-11-18T09:00:00Z",
        })

    marker.write_text(marker_doc(), encoding="utf-8")
    ticks = {"t": 0.0}
    sleeps: list[float] = []

    def clock() -> float:
        ticks["t"] += 0.01
        return ticks["t"]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            # The drafting job lands its run id while the agent is waiting.
            marker.write_text(marker_doc(RUN_ID), encoding="utf-8")

    run, expired = _wait_for_run(
        root, STEM, 60, poll=1.0, sleep=sleep, clock=clock,
    )

    assert expired is False
    assert run is not None and run["run_id"] == RUN_ID
    assert sleeps[:2] == [1.0, 1.0]


def test_wait_finds_a_stem_after_its_drafting_marker_is_gone(_isolated_home):
    """``go-worker`` clears the marker on success; the run it made still names it."""
    root = _isolated_home
    _seed_run(root, state="done", definition=STEM)

    run, expired = _wait_for_run(root, STEM, 0)

    assert expired is False
    assert run is not None and run["run_id"] == RUN_ID


def test_wait_on_a_stem_that_never_lands_a_run_expires(_isolated_home):
    root = _isolated_home
    marker, _ = executor.planning_files(root, STEM)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"stem": STEM, "pid": 0, "state": "planning",
                                  "run_id": ""}), encoding="utf-8")
    ticks = {"t": 0.0}

    def clock() -> float:
        ticks["t"] += 0.5
        return ticks["t"]

    run, expired = _wait_for_run(
        root, STEM, 2, poll=0.1, sleep=lambda _: None, clock=clock,
    )

    assert run is None
    assert expired is True


def test_wait_does_not_write_anything_while_it_polls(_isolated_home):
    root = _isolated_home
    _seed_run(root)
    before = _run_fingerprint(root)
    folder = board.runs_dir(root) / RUN_ID
    listing = sorted(path.name for path in folder.iterdir())

    runner.invoke(app, ["workflow", "wait", RUN_ID, "--timeout", "0"])

    assert _run_fingerprint(root) == before
    assert sorted(path.name for path in folder.iterdir()) == listing
    assert not executor.supervisor_path(root, RUN_ID).exists()
