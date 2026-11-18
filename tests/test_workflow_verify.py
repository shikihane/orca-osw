"""The verification channel: blind prompt, artifact hash, verdict ledger.

The scheduler, not the verifier, decides what a verdict means. These tests pin
the four code-enforced rules: the verifier never sees the author's
self-assessment, a changed artifact voids the verdict, an unreadable verdict
gets one more verifier and then waits for a person, and the ledger tolerates a
half-written line.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from osw import state
from osw.workflow import archive, board, defs, executor

SCAN = {"id": "scan", "mode": "single", "task": "Audit the route table"}


def dead_pid() -> int:
    """A pid that existed and exited: a record it names is truly orphaned."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=60)
    return proc.pid


def install(root: Path, phases: list[dict], *, name: str = "flow",
            max_agents: int = 64) -> defs.Definition:
    path = root / ".orca" / "osw" / "workflows" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"name": name, "max_agents": max_agents, "phases": phases}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return defs.load_definition(root, name)


class Fake:
    """The dispatch seam: one agent id per call, plans recorded in order."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.plans: list[dict] = []
        self.agents: list[str] = []

    def __call__(self, plan, definition, root):
        self.plans.append(dict(plan))
        number = len(self.agents) + 1
        prefix = "verify" if plan.get("kind") == "verify" else "code"
        agent = f"{prefix}_{number:03d}"
        self.agents.append(agent)
        return {"agent": agent, "terminal": f"term_{number:03d}"}


def finish(root: Path, agent: str, *, text: str = "# handoff\n\none claim\n",
           state_name: str = "done", source: str = "ps_done",
           report: dict | None = None) -> str:
    payload: dict = {
        "agent_id": agent,
        "terminal": f"term_{agent}",
        "state": state_name,
        "completion_source": source,
    }
    path = state.new_handoff_path(root, agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    payload["handoff_path"] = str(path)
    if report is not None:
        report_path = root / ".orca" / "osw" / "reports" / f"{agent}.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report), encoding="utf-8")
        payload["report_file"] = str(report_path)
    state.write_agent(root, payload)
    return str(path)


def step_of(root: Path, run_id: str) -> dict:
    run = board.read_run(root, run_id)
    entry = archive.phase_entry(run, "scan")
    assert entry is not None and entry["steps"], "no step was created"
    return archive.read_step(root, run_id, entry["steps"][0])


def warned_step(root: Path, run_id: str, fake: Fake) -> dict:
    """Drive the candidate to `done` with the watcher's no-tool warning."""
    executor.tick(root, run_id, dispatch=fake)
    finish(root, fake.agents[0], report={"tools_observed": False})
    executor.tick(root, run_id, dispatch=fake)  # joins the warning, opens verify
    return step_of(root, run_id)


def test_suspect_verifies_a_warned_step_with_a_blind_prompt(tmp_path):
    definition = install(tmp_path, [SCAN])
    run = archive.create_run(tmp_path, definition, verify="suspect")
    fake = Fake(tmp_path)

    step = warned_step(tmp_path, run["run_id"], fake)

    assert [plan["kind"] for plan in fake.plans] == ["step", "verify"]
    prompt = fake.plans[1]["prompt"]
    assert step["handoff"] in prompt
    assert "Audit the route table" in prompt  # the claim, not the handoff text
    assert "no_tool_activity" not in prompt
    assert "tools_observed" not in prompt
    assert "```json" not in prompt
    assert step["state"] == "running"
    assert len(step["artifact_sha256"]) == 64


def test_suspect_leaves_a_clean_step_alone(tmp_path):
    definition = install(tmp_path, [SCAN])
    run = archive.create_run(tmp_path, definition, verify="suspect")
    fake = Fake(tmp_path)

    executor.tick(tmp_path, run["run_id"], dispatch=fake)
    finish(tmp_path, fake.agents[0], report={"tools_observed": True})

    assert executor.tick(tmp_path, run["run_id"], dispatch=fake) == "done"
    assert [plan["kind"] for plan in fake.plans] == ["step"]


def test_verify_all_checks_every_step(tmp_path):
    definition = install(tmp_path, [SCAN])
    run = archive.create_run(tmp_path, definition, verify="all")
    fake = Fake(tmp_path)

    executor.tick(tmp_path, run["run_id"], dispatch=fake)
    finish(tmp_path, fake.agents[0], report={"tools_observed": True})
    executor.tick(tmp_path, run["run_id"], dispatch=fake)

    assert [plan["kind"] for plan in fake.plans] == ["step", "verify"]


def test_verify_none_skips_the_channel(tmp_path):
    definition = install(tmp_path, [SCAN])
    run = archive.create_run(tmp_path, definition, verify="none")
    fake = Fake(tmp_path)

    warned_step(tmp_path, run["run_id"], fake)

    assert [plan["kind"] for plan in fake.plans] == ["step"]
    assert executor.tick(tmp_path, run["run_id"], dispatch=fake) == "done"


def test_a_confirmed_verdict_finishes_the_step(tmp_path):
    definition = install(tmp_path, [SCAN])
    run = archive.create_run(tmp_path, definition, verify="suspect")
    fake = Fake(tmp_path)

    step = warned_step(tmp_path, run["run_id"], fake)
    finish(tmp_path, fake.agents[1])
    archive.append_verdict(tmp_path, run["run_id"], {
        "step": step["step_key"], "verdict": "confirmed", "reason": "checked",
    })

    assert executor.tick(tmp_path, run["run_id"], dispatch=fake) == "done"
    settled = step_of(tmp_path, run["run_id"])
    assert settled["state"] == "done"
    assert settled["verdict"] == "confirmed"
    assert settled["verdict_reason"] == "checked"


def test_a_changed_artifact_discards_the_verdict(tmp_path):
    definition = install(tmp_path, [SCAN])
    run = archive.create_run(tmp_path, definition, verify="suspect")
    fake = Fake(tmp_path)

    step = warned_step(tmp_path, run["run_id"], fake)
    Path(step["handoff"]).write_text("# rewritten by the author\n", encoding="utf-8")
    finish(tmp_path, fake.agents[1])
    archive.append_verdict(tmp_path, run["run_id"], {
        "step": step["step_key"], "verdict": "confirmed", "reason": "checked",
    })

    assert executor.tick(tmp_path, run["run_id"], dispatch=fake) == "blocked"
    settled = step_of(tmp_path, run["run_id"])
    assert settled["state"] == "artifact_mutated"
    assert settled["verdict"] == ""
    assert "changed during verification" in settled["error"]
    # Nothing more is dispatched: a person decides what the gap means.
    assert [plan["kind"] for plan in fake.plans] == ["step", "verify"]


def test_an_unreadable_verdict_gets_one_more_verifier_then_waits(tmp_path):
    definition = install(tmp_path, [SCAN])
    run = archive.create_run(tmp_path, definition, verify="suspect")
    fake = Fake(tmp_path)

    step = warned_step(tmp_path, run["run_id"], fake)
    assert len(fake.agents) == 2

    finish(tmp_path, fake.agents[1])
    assert executor.tick(tmp_path, run["run_id"], dispatch=fake) == "running"
    assert [plan["kind"] for plan in fake.plans] == ["step", "verify", "verify"]
    assert step_of(tmp_path, run["run_id"])["verify_attempts"] == 1

    finish(tmp_path, fake.agents[2])
    assert executor.tick(tmp_path, run["run_id"], dispatch=fake) == "blocked"
    settled = step_of(tmp_path, run["run_id"])
    assert settled["state"] == "unverifiable"
    assert settled["verdict"] == ""
    assert len(fake.agents) == 3
    assert len(settled["verifications"]) == 2


def test_a_verifier_whose_watcher_died_is_not_waited_on_forever(tmp_path):
    definition = install(tmp_path, [SCAN])
    run = archive.create_run(tmp_path, definition, verify="suspect")
    fake = Fake(tmp_path)

    warned_step(tmp_path, run["run_id"], fake)
    assert [plan["kind"] for plan in fake.plans] == ["step", "verify"]

    # The verifier's pane is open but its watcher is gone: no verdict can
    # ever arrive, so the channel must move on to its one retry.
    verifier = fake.agents[1]
    state.write_agent(tmp_path, {
        "agent_id": verifier,
        "terminal": f"term_{verifier}",
        "state": "running",
        "watcher_pid": dead_pid(),
    })

    assert executor.tick(tmp_path, run["run_id"], dispatch=fake) == "running"
    assert [plan["kind"] for plan in fake.plans] == ["step", "verify", "verify"]
    step = step_of(tmp_path, run["run_id"])
    assert step["verify_attempts"] == 1
    assert [entry["terminal"] for entry in step["verifications"]] == ["lost", ""]


def test_an_unknown_verdict_word_is_not_a_verdict(tmp_path):
    definition = install(tmp_path, [SCAN])
    run = archive.create_run(tmp_path, definition, verify="suspect")
    fake = Fake(tmp_path)

    step = warned_step(tmp_path, run["run_id"], fake)
    finish(tmp_path, fake.agents[1])
    archive.append_verdict(tmp_path, run["run_id"], {
        "step": step["step_key"], "verdict": "looks-fine-to-me", "reason": "meh",
    })

    assert executor.tick(tmp_path, run["run_id"], dispatch=fake) == "running"
    assert step_of(tmp_path, run["run_id"])["verify_attempts"] == 1


def test_the_ledger_tolerates_a_half_written_line(tmp_path):
    definition = install(tmp_path, [SCAN])
    run = archive.create_run(tmp_path, definition)
    ledger = archive.verdicts_path(tmp_path, run["run_id"])
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        '{"step": "old", "verdict": "confirmed"}\n'
        '{"step": "cut", "verd\n',
        encoding="utf-8",
    )
    archive.append_verdict(tmp_path, run["run_id"], {
        "step": "new", "verdict": "unsure", "reason": "half checked",
    })

    verdicts = archive.read_verdicts(tmp_path, run["run_id"])

    assert [item["step"] for item in verdicts] == ["old", "new"]
    assert verdicts[1]["reason"] == "half checked"
