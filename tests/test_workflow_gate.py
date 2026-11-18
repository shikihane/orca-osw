"""Phase gates and rounds: one blind answer per round, mapped by policy.

The seam is the same one ``tests/test_workflow_executor.py`` uses: a fake
dispatch returns an agent id and writes what a watcher would — the step's output
file, or the gate's verdict — so nothing here starts Orca or a provider.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from osw import state
from osw.workflow import archive, board, defs, executor

ITEMS = ["a", "b"]

SURVEY = {"id": "survey", "mode": "single",
          "task": "list the work items\nwrite them to {{output}}"}
ADVANCE = {"id": "advance", "mode": "for_each", "from": ["survey"],
           "task": "work {{item}}\nwrite the result to {{output}}",
           "criteria": "{{item}} is reported", "repeat": True}
FINISH = {"id": "finish", "mode": "single", "from": ["advance"],
          "task": "sum up the round", "criteria": "every item is reported"}
SCAN = {"id": "scan", "mode": "single", "task": "scan the routes",
        "criteria": "the scan covers every route"}
BARE = {"id": "scan", "mode": "single", "task": "scan the routes"}


# ---------------------------------------------------------------------------
# Fixtures: a temporary project, a definition, the dispatch seam, agent records
# ---------------------------------------------------------------------------

def install(
    root: Path,
    phases: list[dict],
    *,
    name: str = "flow",
    goal: str = "",
    max_rounds: int = 1,
    max_agents: int = 64,
) -> defs.Definition:
    payload: dict = {
        "name": name,
        "max_agents": max_agents,
        "max_rounds": max_rounds,
        "phases": phases,
    }
    if goal:
        payload["goal"] = goal
    path = root / ".orca" / "osw" / "workflows" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return defs.load_definition(root, name)


def start(root: Path, definition: defs.Definition, **kwargs) -> dict:
    return archive.create_run(root, definition, **kwargs)


def finish(
    root: Path, agent: str, *, state_name: str = "done", **extra,
) -> None:
    """Write one agent record (and handoff) the way a watcher would."""
    payload: dict = {
        "agent_id": agent,
        "terminal": f"term_{agent}",
        "state": state_name,
        "completion_source": "ps_done" if state_name == "done" else state_name,
    }
    if state_name == "done":
        handoff = state.new_handoff_path(root, agent)
        handoff.parent.mkdir(parents=True, exist_ok=True)
        handoff.write_text("# handoff\n\ndone\n", encoding="utf-8")
        payload["handoff_path"] = str(handoff)
    payload.update(extra)
    state.write_agent(root, payload)


def running(root: Path, agent: str, *, watcher_pid: int, session_id: str) -> None:
    """The record a watcher leaves while its turn is still open."""
    state.write_agent(root, {
        "agent_id": agent,
        "terminal": f"term_{agent}",
        "state": "running",
        "watcher_pid": watcher_pid,
        "session_id": session_id,
    })


def dead_pid() -> int:
    """A pid that existed and exited: a record it names is truly orphaned."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=60)
    return proc.pid


class Fake:
    """One agent per dispatch; gate answers come from a script of replies."""

    def __init__(self, root: Path, run_id: str, replies: list | None = None) -> None:
        self.root = root
        self.run_id = run_id
        self.plans: list[dict] = []
        self.agents: list[str] = []
        self.replies = list(replies or [])

    def __call__(self, plan, definition, root) -> dict:
        self.plans.append(dict(plan))
        number = len(self.plans)
        agent = f"code_{number:03d}"
        self.agents.append(agent)
        if plan.get("kind") == "gate":
            self._answer(root, plan)
        else:
            self._produce(root, plan)
        finish(root, agent)
        return {"agent": agent, "terminal": f"term_{number:03d}"}

    def kinds(self) -> list[str]:
        return [str(plan.get("kind") or "") for plan in self.plans]

    def prompts(self, kind: str) -> list[str]:
        return [str(plan.get("prompt") or "") for plan in self.plans
                if plan.get("kind") == kind]

    def _answer(self, root: Path, plan: dict) -> None:
        record = archive.read_step(root, self.run_id, str(plan.get("step") or ""))
        phase = str(plan.get("phase") or "")
        round_no = int((record or {}).get("round") or 1)
        reply = self.replies.pop(0) if self.replies else {
            "pass": True, "reason": "met",
        }
        text = reply if isinstance(reply, str) else json.dumps(reply)
        path = archive.gate_output_path(root, self.run_id, phase, round_no)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _produce(self, root: Path, plan: dict) -> None:
        phase = str(plan.get("phase") or "")
        if phase not in ("survey", "advance"):
            return  # this phase's task names no output file
        index = int(plan.get("index") or 0)
        path = archive.output_path(root, self.run_id, phase, index)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = ITEMS if phase == "survey" else [{"item": f"done{index}"}]
        path.write_text(json.dumps(payload), encoding="utf-8")


class Plain:
    """A dispatch that answers with one finished agent and nothing else."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.agents: list[str] = []

    def __call__(self, plan, definition, root) -> dict:
        agent = f"code_{len(self.agents) + 1:03d}"
        self.agents.append(agent)
        finish(root, agent)
        return {"agent": agent, "terminal": f"term_{agent}"}


def run_to_terminal(
    root: Path, run_id: str, fake, limit: int = 60,
) -> str:
    status = "running"
    for _ in range(limit):
        status = executor.tick(root, run_id, dispatch=fake)
        if status != "running":
            return status
    raise AssertionError("the run never left `running`")


def phase_entry(root: Path, run_id: str, phase_id: str) -> dict:
    entry = archive.phase_entry(board.read_run(root, run_id), phase_id)
    assert entry is not None, f"phase {phase_id!r} is not in the archive"
    return entry


def steps_of(root: Path, run_id: str, phase_id: str, round_no: int) -> list[dict]:
    run = board.read_run(root, run_id)
    return [step for step in archive.phase_steps(root, run, phase_id)
            if int(step.get("round") or 0) == round_no
            and str(step.get("kind") or "step") == "step"]


def gate_of(root: Path, run_id: str, phase_id: str, round_no: int) -> dict | None:
    run = board.read_run(root, run_id)
    gates = [step for step in archive.phase_steps(root, run, phase_id)
             if int(step.get("round") or 0) == round_no
             and step.get("kind") == "gate"]
    return gates[0] if gates else None


# ---------------------------------------------------------------------------
# A passing gate ends the phase
# ---------------------------------------------------------------------------

def test_a_passing_gate_ends_the_phase(tmp_path):
    definition = install(tmp_path, [SCAN], goal="every route is covered")
    run = start(tmp_path, definition)
    fake = Fake(tmp_path, run["run_id"])

    assert run_to_terminal(tmp_path, run["run_id"], fake) == "done"

    entry = phase_entry(tmp_path, run["run_id"], "scan")
    gate = gate_of(tmp_path, run["run_id"], "scan", 1)
    assert gate is not None
    assert entry["state"] == "done"
    assert entry["gate"] == {
        "round": 1, "step": gate["step_key"], "pass": True, "reason": "met",
    }
    verdicts = [line for line in archive.read_verdicts(tmp_path, run["run_id"])
                if line.get("kind") == "gate"]
    assert verdicts == [{
        "kind": "gate", "phase": "scan", "round": 1,
        "step": gate["step_key"], "pass": True, "reason": "met",
    }]
    assert fake.kinds() == ["step", "gate"]


def test_the_gate_prompt_is_blind_and_names_the_reply_file(tmp_path):
    definition = install(tmp_path, [SURVEY, ADVANCE], goal="every item is reported",
                        max_rounds=2)
    run = start(tmp_path, definition)
    fake = Fake(tmp_path, run["run_id"])

    assert run_to_terminal(tmp_path, run["run_id"], fake) == "done"

    prompts = fake.prompts("gate")
    assert prompts, "no gate was dispatched"
    reply_file = archive.gate_output_path(tmp_path, run["run_id"], "advance", 1)
    for prompt in prompts:
        assert "every item is reported" in prompt          # the run's goal
        assert "- a is reported" in prompt                 # the criterion, per item
        assert "write the result to" not in prompt         # never the worker prompt
        assert '"pass"' in prompt and '"next_items"' in prompt
    assert str(reply_file) in prompts[0]
    assert "-gate-r1.json" in prompts[0]
    assert all("-gate-r" not in prompt for prompt in fake.prompts("step"))


# ---------------------------------------------------------------------------
# Rounds: a failing gate on a repeat phase is a new round
# ---------------------------------------------------------------------------

def test_a_failing_gate_starts_the_next_round(tmp_path):
    definition = install(tmp_path, [SURVEY, ADVANCE], goal="every item is reported",
                        max_rounds=2)
    run = start(tmp_path, definition)
    fake = Fake(tmp_path, run["run_id"], replies=[
        {"pass": False, "reason": "one item is missing", "next_items": ["c"]},
        {"pass": True, "reason": "all three reported"},
    ])

    assert run_to_terminal(tmp_path, run["run_id"], fake) == "done"

    entry = phase_entry(tmp_path, run["run_id"], "advance")
    assert entry["round"] == 2
    assert entry["state"] == "done"
    assert entry["gate"]["pass"] is True
    assert entry["gate"]["round"] == 2

    first = steps_of(tmp_path, run["run_id"], "advance", 1)
    second = steps_of(tmp_path, run["run_id"], "advance", 2)
    assert len(first) == 2 and len(second) == 1
    keys = {step["step_key"] for step in first} | {step["step_key"] for step in second}
    assert len(keys) == 3, "a new round must not reuse an earlier round's record"
    assert "work c" in second[0]["prompt"]
    assert "work a" not in second[0]["prompt"]

    gates = [plan for plan in fake.plans if plan.get("kind") == "gate"]
    assert len(gates) == 2
    assert gates[1]["prompt"].count("- c is reported") == 1


def test_the_round_count_stops_at_max_rounds(tmp_path):
    definition = install(tmp_path, [SURVEY, ADVANCE], goal="every item is reported",
                        max_rounds=2)
    run = start(tmp_path, definition)
    fake = Fake(tmp_path, run["run_id"], replies=[
        {"pass": False, "reason": "still missing", "next_items": ["c"]},
        {"pass": False, "reason": "still missing", "next_items": ["d"]},
    ])

    assert run_to_terminal(tmp_path, run["run_id"], fake) == "failed"

    entry = phase_entry(tmp_path, run["run_id"], "advance")
    assert entry["round"] == 2
    assert entry["state"] == "failed"
    assert steps_of(tmp_path, run["run_id"], "advance", 3) == []


# ---------------------------------------------------------------------------
# Policy: a criterion judged unmet without a further round
# ---------------------------------------------------------------------------

def _unmet_reply() -> dict:
    return {"pass": False, "reason": "two items are missing", "next_items": []}


def test_an_unmet_gate_fails_the_run_under_strict(tmp_path):
    definition = install(
        tmp_path, [SURVEY, {**FINISH, "from": "survey"}],
        goal="every item is reported",
    )
    run = start(tmp_path, definition)
    fake = Fake(tmp_path, run["run_id"], replies=[_unmet_reply()])

    assert run_to_terminal(tmp_path, run["run_id"], fake) == "failed"

    saved = board.read_run(tmp_path, run["run_id"])
    assert phase_entry(tmp_path, run["run_id"], "finish")["state"] == "failed"
    assert "finish" in saved["error"] and "round 1" in saved["error"]
    assert "two items are missing" in saved["error"]


def test_an_unmet_gate_leaves_the_phase_partial_under_partial(tmp_path):
    definition = install(
        tmp_path, [SURVEY, {**FINISH, "from": "survey"}],
        goal="every item is reported",
    )
    run = start(tmp_path, definition, policy="partial")
    fake = Fake(tmp_path, run["run_id"], replies=[_unmet_reply()])

    assert run_to_terminal(tmp_path, run["run_id"], fake) == "partial"

    assert phase_entry(tmp_path, run["run_id"], "finish")["state"] == "partial"
    assert phase_entry(tmp_path, run["run_id"], "survey")["state"] == "done"


# ---------------------------------------------------------------------------
# gate_invalid: one retry, then a hold
# ---------------------------------------------------------------------------

def test_an_unreadable_gate_output_retries_once_then_blocks(tmp_path):
    definition = install(tmp_path, [SCAN], goal="every route is covered")
    run = start(tmp_path, definition)
    fake = Fake(tmp_path, run["run_id"], replies=["not a verdict", "{oops"])

    assert run_to_terminal(tmp_path, run["run_id"], fake) == "blocked"

    gate = gate_of(tmp_path, run["run_id"], "scan", 1)
    assert gate is not None
    assert gate["state"] == "unverifiable"
    assert gate["gate_retries"] == 1
    assert gate["error"].startswith("gate_invalid")
    assert fake.kinds().count("gate") == 2
    assert [line for line in archive.read_verdicts(tmp_path, run["run_id"])
            if line.get("kind") == "gate"] == []


def test_next_items_of_the_wrong_type_follows_the_same_path(tmp_path):
    definition = install(tmp_path, [SCAN], goal="every route is covered")
    run = start(tmp_path, definition)
    wrong = {"pass": False, "reason": "more work", "next_items": "c"}
    fake = Fake(tmp_path, run["run_id"], replies=[wrong, wrong])

    assert run_to_terminal(tmp_path, run["run_id"], fake) == "blocked"

    gate = gate_of(tmp_path, run["run_id"], "scan", 1)
    assert gate is not None
    assert gate["state"] == "unverifiable"
    assert gate["gate_retries"] == 1
    assert "gate_invalid" in gate["error"]


# ---------------------------------------------------------------------------
# Proposals: recorded and announced, never applied
# ---------------------------------------------------------------------------

def test_a_goal_proposal_is_recorded_and_the_goal_is_not_changed(tmp_path, capsys):
    definition = install(tmp_path, [SURVEY, ADVANCE], goal="every item is reported",
                        max_rounds=2)
    run = start(tmp_path, definition)
    fake = Fake(tmp_path, run["run_id"], replies=[
        {"pass": False, "reason": "one item is missing", "next_items": ["c"],
         "goal_proposals": ["cover c as well"]},
        {"pass": True, "reason": "all reported"},
    ])

    assert run_to_terminal(tmp_path, run["run_id"], fake) == "done"

    saved = board.read_run(tmp_path, run["run_id"])
    assert saved["goal"] == "every item is reported"
    assert saved["goal_proposals"] == [
        {"round": 1, "phase": "advance", "text": "cover c as well"},
    ]
    out = capsys.readouterr().out
    assert f"# [osw] goal-proposal {run['run_id']} r1 advance: cover c as well" in out


# ---------------------------------------------------------------------------
# The agent ceiling bounds a further round too
# ---------------------------------------------------------------------------

def test_a_next_round_above_the_agent_ceiling_is_refused(tmp_path):
    definition = install(tmp_path, [SURVEY, ADVANCE], goal="every item is reported",
                        max_rounds=2, max_agents=5)
    run = start(tmp_path, definition)
    fake = Fake(tmp_path, run["run_id"], replies=[
        {"pass": False, "reason": "two more", "next_items": ["c", "d"]},
    ])

    assert run_to_terminal(tmp_path, run["run_id"], fake) == "failed"

    saved = board.read_run(tmp_path, run["run_id"])
    entry = phase_entry(tmp_path, run["run_id"], "advance")
    assert "too_many_agents" in saved["error"]
    assert entry["state"] == "failed"
    assert entry["error"] == "too_many_agents"
    assert entry["round"] == 1
    assert steps_of(tmp_path, run["run_id"], "advance", 2) == []


# ---------------------------------------------------------------------------
# Recovery: the heartbeat, the dead planner, the revived attempt
# ---------------------------------------------------------------------------

def test_the_heartbeat_makes_the_scheduler_alive(tmp_path):
    definition = install(tmp_path, [SCAN])
    run = start(tmp_path, definition)
    assert archive.scheduler_alive(tmp_path, board.read_run(tmp_path, run["run_id"])) is False
    fake = Fake(tmp_path, run["run_id"])

    executor.tick(tmp_path, run["run_id"], dispatch=fake)

    saved = board.read_run(tmp_path, run["run_id"])
    block = saved["scheduler"]
    assert block["pid"] == os.getpid()
    assert block["host"] == "local"
    assert block["started_at"] and block["beat_at"]
    assert archive.scheduler_alive(tmp_path, saved) is True

    # Same live pid, stale beat: a scheduler that stopped ticking is not alive.
    stale = dict(saved, scheduler=dict(block, beat_at="2020-01-01T00:00:00+00:00"))
    assert archive.scheduler_alive(tmp_path, stale) is False


def test_a_planning_marker_reports_whether_its_planner_is_alive(tmp_path):
    executor.write_planning(
        tmp_path, stem="dead", pid=dead_pid(), request="draft it",
    )
    marker, _ = executor.planning_files(tmp_path, "dead")
    before = marker.read_bytes()

    jobs = executor.list_planning(tmp_path)

    assert [job["stem"] for job in jobs] == ["dead"]
    assert jobs[0]["alive"] is False
    assert marker.read_bytes() == before, "a read-only report must not rewrite the marker"

    executor.write_planning(tmp_path, stem="live", pid=os.getpid(), request="draft it")
    assert executor.list_planning(tmp_path)[0]["alive"] is True


def _lost_step(root: Path, run_id: str) -> dict:
    """A dispatched step whose agent record says `lost` with a session."""
    return archive.phase_steps(root, board.read_run(root, run_id), "scan")[0]


def test_a_lost_attempt_is_revived_as_the_same_agent(tmp_path, monkeypatch):
    definition = install(tmp_path, [SCAN])
    run = start(tmp_path, definition)
    fake = Fake(tmp_path, run["run_id"])
    executor.tick(tmp_path, run["run_id"], dispatch=fake)
    agent = fake.agents[0]
    running(tmp_path, agent, watcher_pid=dead_pid(), session_id="session-1")
    calls: list[str] = []
    monkeypatch.setattr(
        executor, "_revive_dispatch", lambda agent_id: calls.append(agent_id) or True,
    )

    executor.tick(tmp_path, run["run_id"], dispatch=fake)

    step = _lost_step(tmp_path, run["run_id"])
    assert calls == [agent]
    assert fake.plans and len(fake.plans) == 1, "a revive must not spend a second agent"
    assert step["agent"] == agent
    assert step["state"] == "running"
    assert not step.get("want_revive")
    assert [attempt["reason"] for attempt in step["attempts"]] == [
        "watcher process is gone", "revive",
    ]


def test_a_refused_revive_falls_back_to_a_fresh_agent(tmp_path, monkeypatch):
    definition = install(tmp_path, [SCAN])
    run = start(tmp_path, definition)
    fake = Fake(tmp_path, run["run_id"])
    executor.tick(tmp_path, run["run_id"], dispatch=fake)
    first = fake.agents[0]
    running(tmp_path, first, watcher_pid=dead_pid(), session_id="session-1")
    calls: list[str] = []
    monkeypatch.setattr(
        executor, "_revive_dispatch", lambda agent_id: calls.append(agent_id) or False,
    )

    executor.tick(tmp_path, run["run_id"], dispatch=fake)

    step = _lost_step(tmp_path, run["run_id"])
    assert calls == [first]
    assert len(fake.plans) == 2
    assert step["agent"] == fake.agents[1] != first
    assert step["state"] == "running"
    assert not step.get("want_revive")
    assert len(step["attempts"]) == 2


def test_run_run_records_the_revive_decision(tmp_path):
    definition = install(tmp_path, [BARE])

    saved = executor.run_run(
        tmp_path, definition, dispatch=Plain(tmp_path), interval=0,
        revive_lost=False,
    )

    assert saved["state"] == "done"
    assert saved["revive_lost"] is False
