"""Reload: rebuild a terminal run from its definition as it now reads.

The specimen is the failure this feature exists for: the phases before the
ceiling ran to `done`, the phase that hit the ceiling never started, and the
definition was wrong. Reload keeps the finished work and lets ``resume`` drive
the rest; nothing here starts Orca.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from osw import state
from osw.workflow import archive, board, defs, executor

SURVEY = {"id": "survey", "mode": "single",
          "task": "list the work items\nwrite them to {{output}}"}
WORK = {"id": "work", "mode": "for_each", "from": ["survey"],
        "task": "work {{item}}\nwrite the result to {{output}}",
        "criteria": "{{item}} is reported"}


def install(root: Path, phases: list[dict], *, max_agents: int) -> defs.Definition:
    payload = {"name": "flow", "max_agents": max_agents, "goal": "all work is done",
               "phases": phases}
    path = root / ".orca" / "osw" / "workflows" / "flow.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return defs.load_definition(root, "flow")


class Fake:
    """One agent per dispatch; gate answers pass unless scripted."""

    def __init__(self, root: Path, run_id: str) -> None:
        self.root = root
        self.run_id = run_id
        self.plans: list[dict] = []

    def __call__(self, plan, definition, root) -> dict:
        self.plans.append(dict(plan))
        agent = f"code_{len(self.plans):03d}"
        if plan.get("kind") == "gate":
            record = archive.read_step(root, self.run_id, str(plan.get("step")))
            out = Path(str((record or {}).get("output") or ""))
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps({"pass": True, "reason": "met"}),
                           encoding="utf-8")
        else:
            phase = str(plan.get("phase") or "")
            if phase == "survey":
                path = archive.output_path(root, self.run_id, phase, 0)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(["a", "b", "c"]), encoding="utf-8")
        handoff = state.new_handoff_path(root, agent)
        handoff.parent.mkdir(parents=True, exist_ok=True)
        handoff.write_text("# handoff\n\ndone\n", encoding="utf-8")
        state.write_agent(root, {
            "agent_id": agent, "terminal": f"term_{agent}", "state": "done",
            "completion_source": "ps_done", "handoff_path": str(handoff),
        })
        return {"agent": agent, "terminal": f"term_{agent}"}

    def count(self, phase: str) -> int:
        return sum(1 for plan in self.plans if plan.get("phase") == phase)


def run_to_terminal(root: Path, run_id: str, fake) -> str:
    status = "running"
    for _ in range(60):
        status = executor.tick(root, run_id, dispatch=fake)
        if status != "running":
            return status
    raise AssertionError("the run never left `running`")


def dead_scheduler(root: Path, run: dict) -> None:
    """The run was driven by this process a moment ago; reload needs it gone."""
    run["scheduler"] = {"pid": 0, "host": "local", "started_at": "", "beat_at": ""}
    archive.write_run(root, run)


def test_reload_keeps_finished_work_and_raises_the_ceiling(tmp_path):
    definition = install(tmp_path, [SURVEY, WORK], max_agents=3)
    run = archive.create_run(tmp_path, definition)
    fake = Fake(tmp_path, run["run_id"])

    assert run_to_terminal(tmp_path, run["run_id"], fake) == "failed"
    failed = board.read_run(tmp_path, run["run_id"])
    assert failed["error"].startswith("too_many_agents")
    assert fake.count("survey") == 1
    assert fake.count("work") == 0

    install(tmp_path, [SURVEY, WORK], max_agents=10)
    dead_scheduler(tmp_path, board.read_run(tmp_path, run["run_id"]))
    executor.reload_run(tmp_path, run["run_id"])

    reloaded = board.read_run(tmp_path, run["run_id"])
    assert reloaded["state"] == "running"
    assert reloaded["max_agents"] == 10
    assert reloaded["error"] == ""
    phases = {entry["id"]: entry for entry in reloaded["phases"]}
    assert phases["survey"]["state"] == "done"
    assert len(phases["survey"]["steps"]) == 1
    assert phases["work"]["state"] == "pending"
    assert phases["work"]["steps"] == []

    assert run_to_terminal(tmp_path, run["run_id"], fake) == "done"
    assert fake.count("survey") == 1, "finished work must not be dispatched again"
    assert fake.count("work") == 4  # three steps and the phase's gate


def test_reload_rejudges_a_phase_whose_gate_was_the_failure(tmp_path):
    """A done gate is still a verdict: reload must not keep the old answer."""
    definition = install(tmp_path, [SURVEY, WORK], max_agents=12)
    run = archive.create_run(tmp_path, definition)

    class No(Fake):
        def __call__(self, plan, definition, root):
            out = super().__call__(plan, definition, root)
            if plan.get("kind") == "gate":
                record = archive.read_step(root, self.run_id, str(plan.get("step")))
                Path(str((record or {}).get("output"))).write_text(
                    json.dumps({"pass": False, "reason": "not met", "next_items": []}),
                    encoding="utf-8")
            return out

    fake = No(tmp_path, run["run_id"])
    assert run_to_terminal(tmp_path, run["run_id"], fake) == "failed"

    old_gate = [step for step in archive.phase_steps(
        tmp_path, board.read_run(tmp_path, run["run_id"]), "work")
        if step["kind"] == "gate"][0]["step_key"]

    dead_scheduler(tmp_path, board.read_run(tmp_path, run["run_id"]))
    reloaded = executor.reload_run(tmp_path, run["run_id"])

    phases = {entry["id"]: entry for entry in reloaded["phases"]}
    assert old_gate not in phases["work"]["steps"]
    assert old_gate not in {item["step_id"] for item in reloaded["steps"]}
    assert archive.read_step(tmp_path, run["run_id"], old_gate) is None
    assert run_to_terminal(tmp_path, run["run_id"], Fake(tmp_path, run["run_id"])) == "done"


def test_reload_refuses_while_a_scheduler_is_alive(tmp_path):
    definition = install(tmp_path, [SURVEY], max_agents=8)
    run = archive.create_run(tmp_path, definition)
    run["scheduler"] = {
        "pid": os.getpid(), "host": "local",
        "started_at": archive.now_iso(), "beat_at": archive.now_iso(),
    }
    archive.write_run(tmp_path, run)

    with pytest.raises(defs.WorkflowError) as caught:
        executor.reload_run(tmp_path, run["run_id"])

    assert caught.value.code == "run_running"


def test_reload_never_lowers_the_ceiling_below_what_was_spent(tmp_path):
    definition = install(tmp_path, [SURVEY, WORK], max_agents=10)
    run = archive.create_run(tmp_path, definition)
    fake = Fake(tmp_path, run["run_id"])
    run_to_terminal(tmp_path, run["run_id"], fake)
    spent = 5  # survey, three work steps, one gate

    install(tmp_path, [SURVEY, WORK], max_agents=2)
    dead_scheduler(tmp_path, board.read_run(tmp_path, run["run_id"]))
    reloaded = executor.reload_run(tmp_path, run["run_id"])

    assert reloaded["max_agents"] >= spent
