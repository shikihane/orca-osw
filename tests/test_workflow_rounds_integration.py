"""End to end: a repeat phase runs two rounds, a gate decides each one.

The seam is the same as ``tests/test_workflow_executor.py``: a fake dispatch
returns an agent id and then writes the record and the output file that a real
watcher would, so nothing here starts Orca or a provider.
"""

from __future__ import annotations

import json
from pathlib import Path

from osw import state
from osw.workflow import archive, board, defs, executor

SURVEY = [{"id": "a", "title": "alpha"}, {"id": "b", "title": "beta"}]
ROUND_TWO = [{"id": "c", "title": "gamma"}]


def install(root: Path) -> defs.Definition:
    payload = {
        "name": "rounds",
        "max_agents": 32,
        "goal": "every ledger row is reported",
        "max_rounds": 2,
        "phases": [
            {
                "id": "survey",
                "mode": "single",
                "task": "list the work items\nwrite them to {{output}}",
            },
            {
                "id": "advance",
                "mode": "for_each",
                "from": ["survey"],
                "repeat": True,
                "criteria": "item {{item.id}} is reported",
                "task": "handle {{item.id}}\nwrite the result to {{output}}",
            },
            {
                "id": "finish",
                "mode": "single",
                "from": ["advance"],
                "criteria": "every ledger row is reported",
                "task": "sum the work up",
            },
        ],
    }
    path = root / ".orca" / "osw" / "workflows" / "rounds.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return defs.load_definition(root, "rounds")


def finish_agent(root: Path, agent: str) -> None:
    handoff = state.new_handoff_path(root, agent)
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text("# handoff\n\ndone\n", encoding="utf-8")
    state.write_agent(root, {
        "agent_id": agent,
        "terminal": f"term_{agent}",
        "state": "done",
        "completion_source": "ps_done",
        "handoff_path": str(handoff),
    })


class Fake:
    """One agent per plan; the gate answers per round."""

    def __init__(self, root: Path, run_id: str) -> None:
        self.root = root
        self.run_id = run_id
        self.plans: list[dict] = []

    def __call__(self, plan, definition, root):
        self.plans.append(dict(plan))
        agent = f"agent_{len(self.plans):03d}"
        record = archive.read_step(root, self.run_id, str(plan.get("step") or ""))
        if plan.get("kind") == "gate":
            self._answer_gate(root, plan, record)
        else:
            self._write_output(root, plan, record)
        finish_agent(root, agent)
        return {"agent": agent, "terminal": f"term_{agent}"}

    def _write_output(self, root, plan, record) -> None:
        phase = str(plan.get("phase") or "")
        index = int(plan.get("index") or 0)
        path = archive.output_path(root, self.run_id, phase, index)
        path.parent.mkdir(parents=True, exist_ok=True)
        if phase == "survey":
            items = SURVEY
        else:
            items = [{"id": f"item{index}"}]
        path.write_text(json.dumps(items), encoding="utf-8")

    def _answer_gate(self, root, plan, record) -> None:
        phase = str(plan.get("phase") or "")
        round_no = int((record or {}).get("round") or 1)
        if phase == "advance" and round_no == 1:
            verdict = {
                "pass": False,
                "reason": "one more item is missing",
                "next_items": ROUND_TWO,
                "goal_proposals": ["extend the goal to cover c"],
            }
        else:
            verdict = {"pass": True, "reason": "criteria met",
                       "next_items": [], "goal_proposals": []}
        path = archive.gate_output_path(root, self.run_id, phase, round_no)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(verdict), encoding="utf-8")


def run_to_terminal(root: Path, run_id: str, fake: Fake) -> str:
    status = "running"
    for _ in range(60):
        status = executor.tick(root, run_id, dispatch=fake)
        if status != "running":
            return status
    raise AssertionError("the run never left `running`")


def test_two_rounds_then_done(tmp_path: Path) -> None:
    definition = install(tmp_path)
    run = archive.create_run(tmp_path, definition)
    fake = Fake(tmp_path, run["run_id"])

    status = run_to_terminal(tmp_path, run["run_id"], fake)

    final = board.read_run(tmp_path, run["run_id"])
    assert status == "done"
    assert final["state"] == "done"
    assert final["keys_version"] == archive.KEYS_VERSION

    phases = {entry["id"]: entry for entry in final["phases"]}
    assert phases["advance"]["round"] == 2
    assert phases["advance"]["gate"]["pass"] is True
    assert phases["advance"]["state"] == "done"

    advance = archive.phase_steps(tmp_path, final, "advance")
    work = [step for step in advance if step["kind"] == "step"]
    keys = [step["step_key"] for step in advance]
    assert len(keys) == len(set(keys))
    assert {step["round"] for step in work} == {1, 2}
    assert len([step for step in work if step["round"] == 1]) == len(SURVEY)
    assert len([step for step in work if step["round"] == 2]) == len(ROUND_TWO)

    verdicts = archive.read_verdicts(tmp_path, run["run_id"])
    gates = [line for line in verdicts if line.get("kind") == "gate"]
    assert len(gates) == 3  # advance r1, advance r2, finish
    assert final["goal_proposals"] and final["goal_proposals"][0]["text"]


def test_the_gate_prompt_is_blind(tmp_path: Path) -> None:
    definition = install(tmp_path)
    run = archive.create_run(tmp_path, definition)
    fake = Fake(tmp_path, run["run_id"])

    run_to_terminal(tmp_path, run["run_id"], fake)

    gate_prompts = [plan["prompt"] for plan in fake.plans
                    if plan.get("kind") == "gate"]
    assert gate_prompts
    for prompt in gate_prompts:
        assert "handle " not in prompt          # never the worker's task text
        assert "sum the work up" not in prompt
