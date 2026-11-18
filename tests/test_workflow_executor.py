"""The scheduler: dispatch, the barrier, the attempt ladder, resume, guards.

The single substituted seam is ``dispatch``: a fake returns an agent id the way
``default_dispatch`` returns one from ``fanout.parse_created``, and a test then
writes that agent's record and handoff into the temporary project root the way
a watcher would. Nothing here starts Orca.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from osw import state
from osw.workflow import archive, board, defs, executor


# ---------------------------------------------------------------------------
# Fixtures: a temporary project, a definition, and the dispatch seam
# ---------------------------------------------------------------------------

def install(
    root: Path,
    phases: list[dict],
    *,
    name: str = "flow",
    max_agents: int = 64,
    approval: str | None = None,
) -> defs.Definition:
    payload: dict = {"name": name, "max_agents": max_agents, "phases": phases}
    if approval is not None:
        payload["approval"] = approval
    path = root / ".orca" / "osw" / "workflows" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return defs.load_definition(root, name)


def start(root: Path, definition: defs.Definition, **kwargs) -> dict:
    return archive.create_run(root, definition, **kwargs)


class Fake:
    """The one seam: answers each dispatch with a fresh agent id."""

    def __init__(self, root: Path, *, auto_finish: bool = False) -> None:
        self.root = root
        self.plans: list[dict] = []
        self.agents: list[str] = []
        self.auto_finish = auto_finish

    def __call__(self, plan, definition, root):
        self.plans.append(dict(plan))
        number = len(self.agents) + 1
        prefix = "verify" if plan.get("kind") == "verify" else "code"
        agent = f"{prefix}_{number:03d}"
        self.agents.append(agent)
        if self.auto_finish:
            finish(self.root, agent)
        return {"agent": agent, "terminal": f"term_{number:03d}"}

    def prompts(self, kind: str = "step") -> list[str]:
        return [plan["prompt"] for plan in self.plans if plan.get("kind") == kind]


def finish(
    root: Path,
    agent: str,
    *,
    text: str = "# handoff\n\nnothing to report\n",
    state_name: str = "done",
    source: str = "ps_done",
    report: dict | None = None,
    handoff: bool = True,
) -> str:
    """Write one agent record (and handoff, and report) as a watcher would."""
    payload: dict = {
        "agent_id": agent,
        "terminal": f"term_{agent}",
        "state": state_name,
        "completion_source": source,
    }
    if handoff:
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
    return str(payload.get("handoff_path") or "")


def dead_pid() -> int:
    """A pid that existed and exited: a record it names is truly orphaned."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=60)
    return proc.pid


def running(root: Path, agent: str, *, watcher_pid: int) -> None:
    """The record a watcher leaves while its turn is still open."""
    state.write_agent(root, {
        "agent_id": agent,
        "terminal": f"term_{agent}",
        "state": "running",
        "watcher_pid": watcher_pid,
    })


def step_of(root: Path, run_id: str, phase_id: str, index: int = 0) -> dict:
    run = board.read_run(root, run_id)
    entry = archive.phase_entry(run, phase_id)
    assert entry is not None, f"phase {phase_id!r} is not in the archive"
    return archive.read_step(root, run_id, entry["steps"][index])


def phase_state(root: Path, run_id: str, phase_id: str) -> str:
    entry = archive.phase_entry(board.read_run(root, run_id), phase_id)
    assert entry is not None
    return entry["state"]


SCAN = {"id": "scan", "mode": "single", "task": "scan the routes"}
AUDIT = {"id": "audit", "mode": "for_each", "from": "scan",
         "task": "Audit {{item}} for missing auth"}
REPORT = {"id": "report", "mode": "single", "from": "audit",
          "task": "merge the findings"}


def handoff_json(**payload) -> str:
    return "# handoff\n\n```json\n" + json.dumps(payload) + "\n```\n"


# ---------------------------------------------------------------------------
# Dispatch and barriers
# ---------------------------------------------------------------------------

def test_a_single_phase_dispatches_one_agent(tmp_path):
    definition = install(tmp_path, [SCAN])
    run = start(tmp_path, definition)
    fake = Fake(tmp_path)

    assert executor.tick(tmp_path, run["run_id"], dispatch=fake) == "running"

    assert fake.prompts() == ["scan the routes"]
    step = step_of(tmp_path, run["run_id"], "scan")
    assert step["state"] == "running"
    assert [attempt["n"] for attempt in step["attempts"]] == [1]
    assert step["attempts"][0]["agent"] == fake.agents[0]
    assert step["attempts"][0]["ended_at"] == ""
    assert step["terminal"] == "term_001"


def test_a_barrier_holds_the_next_phase_until_its_input_is_terminal(tmp_path):
    definition = install(tmp_path, [SCAN, {**REPORT, "from": "scan"}])
    run = start(tmp_path, definition)
    fake = Fake(tmp_path)

    executor.tick(tmp_path, run["run_id"], dispatch=fake)
    assert fake.prompts() == ["scan the routes"]
    assert phase_state(tmp_path, run["run_id"], "report") == "pending"

    finish(tmp_path, fake.agents[0])

    assert executor.tick(tmp_path, run["run_id"], dispatch=fake) == "running"
    assert fake.prompts() == ["scan the routes", "merge the findings"]
    assert phase_state(tmp_path, run["run_id"], "scan") == "done"
    assert phase_state(tmp_path, run["run_id"], "report") == "running"


def test_a_for_each_phase_expands_from_the_upstream_handoff(tmp_path):
    definition = install(tmp_path, [SCAN, AUDIT])
    run = start(tmp_path, definition)
    fake = Fake(tmp_path)

    executor.tick(tmp_path, run["run_id"], dispatch=fake)
    finish(tmp_path, fake.agents[0], text=handoff_json(items=["/a", "/b"]))
    executor.tick(tmp_path, run["run_id"], dispatch=fake)

    assert fake.prompts()[1:] == [
        "Audit /a for missing auth",
        "Audit /b for missing auth",
    ]
    entry = archive.phase_entry(board.read_run(tmp_path, run["run_id"]), "audit")
    assert entry["expanded"] == 2
    assert len(entry["steps"]) == 2


# ---------------------------------------------------------------------------
# Phase policy: strict versus partial
# ---------------------------------------------------------------------------

def test_strict_policy_fails_the_phase_and_stops_the_run(tmp_path):
    definition = install(tmp_path, [SCAN, AUDIT, REPORT])
    run = start(tmp_path, definition, policy="strict", max_attempts=1)
    fake = Fake(tmp_path)

    executor.tick(tmp_path, run["run_id"], dispatch=fake)
    finish(tmp_path, fake.agents[0], text=handoff_json(items=["/a", "/b"]))
    executor.tick(tmp_path, run["run_id"], dispatch=fake)
    assert len(fake.agents) == 3

    finish(tmp_path, fake.agents[1], state_name="error", source="timeout", handoff=False)

    assert executor.tick(tmp_path, run["run_id"], dispatch=fake) == "failed"

    assert phase_state(tmp_path, run["run_id"], "audit") == "failed"
    assert phase_state(tmp_path, run["run_id"], "report") == "pending"
    # The run stops dispatching: the merge phase never starts.
    assert len(fake.agents) == 3
    assert executor.tick(tmp_path, run["run_id"], dispatch=fake) == "failed"
    assert len(fake.agents) == 3


def test_partial_policy_records_the_gap_and_continues(tmp_path):
    definition = install(tmp_path, [SCAN, AUDIT, REPORT])
    run = start(tmp_path, definition, policy="partial", max_attempts=1)
    fake = Fake(tmp_path)

    executor.tick(tmp_path, run["run_id"], dispatch=fake)
    finish(tmp_path, fake.agents[0], text=handoff_json(items=["/a", "/b"]))
    executor.tick(tmp_path, run["run_id"], dispatch=fake)
    finish(tmp_path, fake.agents[1], state_name="error", source="timeout", handoff=False)
    finish(tmp_path, fake.agents[2])

    assert executor.tick(tmp_path, run["run_id"], dispatch=fake) == "running"
    assert phase_state(tmp_path, run["run_id"], "audit") == "partial"
    assert fake.prompts()[-1] == "merge the findings"

    finish(tmp_path, fake.agents[3])

    assert executor.tick(tmp_path, run["run_id"], dispatch=fake) == "partial"
    failed = [record for record in archive.phase_steps(
        tmp_path, board.read_run(tmp_path, run["run_id"]), "audit")
        if record["state"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["error"] == "timeout"


# ---------------------------------------------------------------------------
# Attempt ladder
# ---------------------------------------------------------------------------

def test_the_attempt_ladder_redispatches_the_same_prompt_then_fails(tmp_path):
    definition = install(tmp_path, [SCAN])
    run = start(tmp_path, definition, max_attempts=2)
    fake = Fake(tmp_path)

    executor.tick(tmp_path, run["run_id"], dispatch=fake)
    finish(tmp_path, fake.agents[0], state_name="error", source="timeout", handoff=False)
    assert executor.tick(tmp_path, run["run_id"], dispatch=fake) == "running"
    assert fake.prompts() == ["scan the routes", "scan the routes"]

    finish(tmp_path, fake.agents[1], state_name="error", source="lost", handoff=False)
    assert executor.tick(tmp_path, run["run_id"], dispatch=fake) == "failed"

    step = step_of(tmp_path, run["run_id"], "scan")
    assert step["state"] == "failed"
    assert [attempt["n"] for attempt in step["attempts"]] == [1, 2]
    assert [attempt["terminal"] for attempt in step["attempts"]] == ["error", "error"]
    assert [attempt["reason"] for attempt in step["attempts"]] == ["timeout", "lost"]
    assert len(fake.agents) == 2


def test_a_missing_agent_record_is_a_lost_attempt(tmp_path):
    definition = install(tmp_path, [SCAN])
    run = start(tmp_path, definition, max_attempts=2)
    fake = Fake(tmp_path)

    executor.tick(tmp_path, run["run_id"], dispatch=fake)
    assert executor.tick(tmp_path, run["run_id"], dispatch=fake) == "running"

    step = step_of(tmp_path, run["run_id"], "scan")
    assert step["attempts"][0]["terminal"] == "lost"
    assert step["attempts"][0]["reason"] == "agent record missing"
    assert step["state"] == "running"
    assert len(fake.agents) == 2


def test_a_running_agent_whose_watcher_died_is_a_lost_attempt(tmp_path):
    definition = install(tmp_path, [SCAN])
    run = start(tmp_path, definition, max_attempts=2)
    fake = Fake(tmp_path)

    executor.tick(tmp_path, run["run_id"], dispatch=fake)
    running(tmp_path, fake.agents[0], watcher_pid=dead_pid())
    assert executor.tick(tmp_path, run["run_id"], dispatch=fake) == "running"

    step = step_of(tmp_path, run["run_id"], "scan")
    assert step["attempts"][0]["terminal"] == "lost"
    assert step["attempts"][0]["reason"] == "watcher process is gone"
    assert step["state"] == "running"
    assert len(fake.agents) == 2


def test_a_running_agent_with_a_live_watcher_is_left_alone(tmp_path):
    definition = install(tmp_path, [SCAN])
    run = start(tmp_path, definition, max_attempts=2)
    fake = Fake(tmp_path)

    executor.tick(tmp_path, run["run_id"], dispatch=fake)
    running(tmp_path, fake.agents[0], watcher_pid=os.getpid())
    assert executor.tick(tmp_path, run["run_id"], dispatch=fake) == "running"

    step = step_of(tmp_path, run["run_id"], "scan")
    assert not step["attempts"][0]["ended_at"]
    assert step["state"] == "running"
    assert len(fake.agents) == 1


def test_a_done_agent_without_a_handoff_file_does_not_finish_the_step(tmp_path):
    definition = install(tmp_path, [SCAN])
    run = start(tmp_path, definition, max_attempts=1)
    fake = Fake(tmp_path)

    executor.tick(tmp_path, run["run_id"], dispatch=fake)
    finish(tmp_path, fake.agents[0], handoff=False)

    assert executor.tick(tmp_path, run["run_id"], dispatch=fake) == "failed"
    step = step_of(tmp_path, run["run_id"], "scan")
    assert step["state"] == "failed"
    assert step["error"] == "handoff_missing"


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------

def test_resume_reuses_finished_steps_and_redispatches_only_the_lost_one(tmp_path):
    phases = [
        {"id": "one", "mode": "single", "task": "first"},
        {"id": "two", "mode": "single", "from": "one", "task": "second"},
        {"id": "three", "mode": "single", "from": "two", "task": "third"},
    ]
    definition = install(tmp_path, phases)
    run = start(tmp_path, definition)
    fake = Fake(tmp_path)

    executor.tick(tmp_path, run["run_id"], dispatch=fake)
    finish(tmp_path, fake.agents[0])
    executor.tick(tmp_path, run["run_id"], dispatch=fake)
    finish(tmp_path, fake.agents[1])
    executor.tick(tmp_path, run["run_id"], dispatch=fake)
    # The scheduler dies here: `three` was dispatched and never reported.

    resumed = Fake(tmp_path, auto_finish=True)
    result = executor.run_run(
        tmp_path, definition, resume=True, dispatch=resumed, interval=0,
    )

    assert result["run_id"] == run["run_id"]
    assert result["state"] == "done"
    # Only the unfinished step ran again; the finished ones were reused by key.
    assert resumed.prompts() == ["third"]
    assert len(fake.agents) == 3
    keys = {
        phase_id: archive.phase_entry(
            board.read_run(tmp_path, run["run_id"]), phase_id)["steps"]
        for phase_id in ("one", "two", "three")
    }
    assert all(len(value) == 1 for value in keys.values())


def test_run_run_refuses_an_approval_gated_definition(tmp_path):
    definition = install(tmp_path, [SCAN], approval="human")
    fake = Fake(tmp_path)

    with pytest.raises(defs.WorkflowError) as caught:
        executor.run_run(tmp_path, definition, dispatch=fake)

    assert caught.value.code == "approval_unavailable"
    assert "approval" in caught.value.message
    assert board.list_runs(tmp_path) == []

    result = executor.run_run(
        tmp_path, definition, dispatch=Fake(tmp_path, auto_finish=True),
        approved=True, interval=0,
    )
    assert result["state"] == "done"


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def test_an_expansion_over_max_agents_is_refused(tmp_path):
    definition = install(tmp_path, [SCAN, AUDIT], max_agents=2)
    run = start(tmp_path, definition)
    fake = Fake(tmp_path)

    executor.tick(tmp_path, run["run_id"], dispatch=fake)
    finish(tmp_path, fake.agents[0], text=handoff_json(items=["/a", "/b", "/c"]))

    assert executor.tick(tmp_path, run["run_id"], dispatch=fake) == "failed"
    saved = board.read_run(tmp_path, run["run_id"])
    assert saved["error"].startswith("too_many_agents:")
    assert "audit" in saved["error"]
    assert "3" in saved["error"]
    assert len(fake.agents) == 1


def test_a_handoff_without_the_array_field_is_refused(tmp_path):
    definition = install(tmp_path, [SCAN, AUDIT])
    run = start(tmp_path, definition)
    fake = Fake(tmp_path)

    executor.tick(tmp_path, run["run_id"], dispatch=fake)
    finish(tmp_path, fake.agents[0], text=handoff_json(items="not a list"))

    assert executor.tick(tmp_path, run["run_id"], dispatch=fake) == "failed"
    saved = board.read_run(tmp_path, run["run_id"])
    assert saved["error"].startswith("no_items:")
    assert "findings" in saved["error"]
    assert len(fake.agents) == 1


def test_a_handoff_without_a_json_block_is_refused(tmp_path):
    definition = install(tmp_path, [SCAN, AUDIT])
    run = start(tmp_path, definition)
    fake = Fake(tmp_path)

    executor.tick(tmp_path, run["run_id"], dispatch=fake)
    finish(tmp_path, fake.agents[0], text="# plain markdown, no block\n")

    assert executor.tick(tmp_path, run["run_id"], dispatch=fake) == "failed"
    saved = board.read_run(tmp_path, run["run_id"])
    assert saved["error"].startswith("no_items:")
    assert "fenced" in saved["error"]


# ---------------------------------------------------------------------------
# Human verbs: consumed exactly once
# ---------------------------------------------------------------------------

def test_a_retry_request_grants_one_attempt_beyond_the_cap(tmp_path):
    definition = install(tmp_path, [SCAN])
    run = start(tmp_path, definition, max_attempts=1)
    fake = Fake(tmp_path)

    executor.tick(tmp_path, run["run_id"], dispatch=fake)
    finish(tmp_path, fake.agents[0], state_name="error", source="timeout", handoff=False)
    assert executor.tick(tmp_path, run["run_id"], dispatch=fake) == "failed"
    key = step_of(tmp_path, run["run_id"], "scan")["step_key"]

    archive.write_request(tmp_path, run["run_id"], "retry", key, "flaky pane")

    assert executor.tick(tmp_path, run["run_id"], dispatch=fake) == "running"
    step = step_of(tmp_path, run["run_id"], "scan")
    assert step["state"] == "running"
    assert len(step["attempts"]) == 2
    assert step["attempts"][1]["reason"] == "flaky pane"
    assert len(fake.agents) == 2
    # The request is consumed with the first application, so the next tick
    # cannot grant a second one.
    assert archive.consume_requests(tmp_path, run["run_id"]) == []

    finish(tmp_path, fake.agents[1], state_name="error", source="timeout", handoff=False)
    assert executor.tick(tmp_path, run["run_id"], dispatch=fake) == "failed"
    assert len(fake.agents) == 2


def test_accept_marks_a_failed_step_done(tmp_path):
    definition = install(tmp_path, [SCAN])
    run = start(tmp_path, definition, max_attempts=1)
    fake = Fake(tmp_path)

    executor.tick(tmp_path, run["run_id"], dispatch=fake)
    finish(tmp_path, fake.agents[0], state_name="error", source="timeout", handoff=False)
    executor.tick(tmp_path, run["run_id"], dispatch=fake)
    key = step_of(tmp_path, run["run_id"], "scan")["step_key"]

    archive.write_request(tmp_path, run["run_id"], "accept", key, "checked by hand")

    assert executor.tick(tmp_path, run["run_id"], dispatch=fake) == "done"
    step = step_of(tmp_path, run["run_id"], "scan")
    assert step["state"] == "done"
    assert step["decision"]["verb"] == "accept"
    assert step["decision"]["reason"] == "checked by hand"


def test_reject_marks_a_running_step_failed_with_the_reason(tmp_path):
    definition = install(tmp_path, [SCAN, {**REPORT, "from": "scan"}])
    run = start(tmp_path, definition)
    fake = Fake(tmp_path)

    executor.tick(tmp_path, run["run_id"], dispatch=fake)
    key = step_of(tmp_path, run["run_id"], "scan")["step_key"]

    archive.write_request(tmp_path, run["run_id"], "reject", key, "wrong scan")

    assert executor.tick(tmp_path, run["run_id"], dispatch=fake) == "failed"
    step = step_of(tmp_path, run["run_id"], "scan")
    assert step["state"] == "failed"
    assert step["error"] == "wrong scan"


def test_stop_skips_unstarted_steps_and_ends_the_run(tmp_path):
    definition = install(tmp_path, [SCAN, {**REPORT, "from": "scan"}])
    run = start(tmp_path, definition)
    fake = Fake(tmp_path)

    executor.tick(tmp_path, run["run_id"], dispatch=fake)
    archive.write_request(tmp_path, run["run_id"], "stop", "*", "gone wrong")

    assert executor.tick(tmp_path, run["run_id"], dispatch=fake) == "stopped"
    assert phase_state(tmp_path, run["run_id"], "report") == "skipped"
    assert len(fake.agents) == 1


# ---------------------------------------------------------------------------
# The dispatch seam: argv the scheduler actually builds
# ---------------------------------------------------------------------------

def _record_argv(monkeypatch, tmp_path, definition, *, env, provider="pi"):
    seen: list[list[str]] = []

    async def fake_run(argv, cwd, **kwargs):
        seen.append([str(part) for part in argv])
        return executor.fanout.CommandResult(
            0, "Created code_001 on terminal term_001", "",
        )

    if provider:
        monkeypatch.setenv("OSW_PROVIDER", provider)
    else:
        monkeypatch.delenv("OSW_PROVIDER", raising=False)
    if env:
        monkeypatch.setenv("ORCA_TERMINAL_HANDLE", env)
    else:
        monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    monkeypatch.setattr(executor.fanout, "run_command", fake_run)
    executor.default_dispatch({"prompt": "audit the routes"}, definition, tmp_path)
    return seen[0]


def test_a_headless_dispatch_asks_for_no_notification(tmp_path, monkeypatch):
    definition = install(tmp_path, [SCAN])

    argv = _record_argv(monkeypatch, tmp_path, definition, env="")

    assert "--no-notify" in argv
    assert "--caller-terminal" not in argv


def test_a_dispatch_inside_a_pane_names_the_caller(tmp_path, monkeypatch):
    definition = install(tmp_path, [SCAN])

    argv = _record_argv(monkeypatch, tmp_path, definition, env="term_abc")

    assert argv[argv.index("--caller-terminal") + 1] == "term_abc"
    assert "--no-notify" not in argv


def test_a_project_without_a_provider_still_dispatches(tmp_path, monkeypatch):
    """An unconfigured project runs on the default provider instead of refusing."""
    definition = install(tmp_path, [SCAN])

    argv = _record_argv(monkeypatch, tmp_path, definition, env="", provider="")

    assert "pi" in argv


def test_a_producer_output_file_feeds_for_each(tmp_path):
    """The handoff is markdown; structured output goes to the file it names."""
    scan = {**SCAN, "task": "List the routes; write {\"items\": [...]} to {{output}}"}
    definition = install(tmp_path, [scan, AUDIT])
    run = start(tmp_path, definition)
    fake = Fake(tmp_path)

    executor.tick(tmp_path, run["run_id"], dispatch=fake)
    step = step_of(tmp_path, run["run_id"], "scan")
    assert step["output"] in fake.plans[0]["prompt"]

    Path(step["output"]).parent.mkdir(parents=True, exist_ok=True)
    Path(step["output"]).write_text('{"items": ["/a", "/b"]}', encoding="utf-8")
    finish(tmp_path, fake.agents[0])

    executor.tick(tmp_path, run["run_id"], dispatch=fake)

    assert [plan["phase"] for plan in fake.plans] == ["scan", "audit", "audit"]
    assert "Audit /a" in fake.plans[1]["prompt"]
    assert "Audit /b" in fake.plans[2]["prompt"]


def test_a_top_level_array_is_the_item_list(tmp_path):
    """`["/a", "/b"]` answers "list these" directly; no wrapper object needed."""
    scan = {**SCAN, "task": "List the routes and write them to {{output}}"}
    definition = install(tmp_path, [scan, AUDIT])
    run = start(tmp_path, definition)
    fake = Fake(tmp_path)

    executor.tick(tmp_path, run["run_id"], dispatch=fake)
    output = Path(step_of(tmp_path, run["run_id"], "scan")["output"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text('["/a", "/b"]', encoding="utf-8")
    finish(tmp_path, fake.agents[0])

    executor.tick(tmp_path, run["run_id"], dispatch=fake)

    assert [plan["phase"] for plan in fake.plans] == ["scan", "audit", "audit"]
    assert "Audit /b" in fake.plans[2]["prompt"]


def test_a_refusal_fails_the_run_and_names_the_path(tmp_path):
    """A missing producer file ends the run with the reason, not the scheduler."""
    scan = {**SCAN, "task": "List the routes; write them to {{output}}"}
    definition = install(tmp_path, [scan, AUDIT])
    run = start(tmp_path, definition)
    fake = Fake(tmp_path)

    executor.tick(tmp_path, run["run_id"], dispatch=fake)
    finish(tmp_path, fake.agents[0])

    assert executor.tick(tmp_path, run["run_id"], dispatch=fake) == "failed"
    saved = board.read_run(tmp_path, run["run_id"])
    assert saved["error"].startswith("no_items:")
    assert "outputs" in saved["error"]
    assert "without writing it" in saved["error"]


def test_a_stop_request_stops_the_agents_it_dispatched(tmp_path):
    """A stopped run must not leave its dispatched agent working."""
    definition = install(tmp_path, [SCAN, {"id": "report", "mode": "single",
                                            "task": "summarise the scan"}])
    run = start(tmp_path, definition)
    fake = Fake(tmp_path)

    executor.tick(tmp_path, run["run_id"], dispatch=fake)
    stepping = step_of(tmp_path, run["run_id"], "scan")
    assert stepping["state"] == "running"

    archive.write_request(tmp_path, run["run_id"], "stop", "", "operator asked")
    final = executor.tick(tmp_path, run["run_id"], dispatch=fake)

    assert final == "stopped"
    stopped = step_of(tmp_path, run["run_id"], "scan")
    assert stopped["state"] == "skipped"
    assert stopped["error"] == "run stopped"
    sidecar = tmp_path / ".orca" / "osw" / "agents" / f"{stopped['agent']}.stop"
    assert sidecar.is_file()


def test_resume_continues_a_stopped_run(tmp_path):
    definition = install(tmp_path, [SCAN, {"id": "report", "mode": "single",
                                            "task": "summarise the scan"}])
    run = start(tmp_path, definition)
    fake = Fake(tmp_path)

    executor.tick(tmp_path, run["run_id"], dispatch=fake)
    archive.write_request(tmp_path, run["run_id"], "stop", "", "pause please")
    assert executor.tick(tmp_path, run["run_id"], dispatch=fake) == "stopped"

    def dispatch_and_finish(plan, definition, root):
        outcome = fake(plan, definition, root)
        finish(tmp_path, outcome["agent"])
        return outcome

    resumed = executor.run_run(
        tmp_path, definition, resume=True, dispatch=dispatch_and_finish,
        interval=0.0,
    )

    assert resumed["run_id"] == run["run_id"]
    assert resumed["state"] == "done"
    phases = [plan["phase"] for plan in fake.plans]
    assert phases == ["scan", "scan", "report"], phases


# ---------------------------------------------------------------------------
# One-shot start: go creates the run, the child scheduler continues it
# ---------------------------------------------------------------------------

def test_start_run_creates_a_running_header(tmp_path):
    definition = install(tmp_path, [SCAN])

    run = executor.start_run(tmp_path, definition, policy="partial", verify="none")

    saved = board.read_run(tmp_path, run["run_id"])
    assert saved["state"] == "running"
    assert saved["policy"] == "partial"
    assert saved["verify"] == "none"
    assert saved["definition_sha"]


def test_run_run_continues_the_run_it_is_given(tmp_path):
    definition = install(tmp_path, [SCAN])
    seeded = executor.start_run(tmp_path, definition)
    fake = Fake(tmp_path, auto_finish=True)

    final = executor.run_run(
        tmp_path, definition, run_id=seeded["run_id"], interval=0.0, dispatch=fake,
    )

    assert final["run_id"] == seeded["run_id"]
    assert final["state"] == "done"
    assert [saved["run_id"] for saved in board.list_runs(tmp_path)] == [seeded["run_id"]]


def test_go_spawns_one_worker_that_owns_planning_and_scheduling(tmp_path, monkeypatch):
    captured: dict = {}

    class Child:
        pid = 4242

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["log"] = kwargs["stdout"].name
        return Child()

    monkeypatch.setattr(executor.subprocess, "Popen", fake_popen)
    spawned = executor.spawn_planner(
        tmp_path, stem="audit-routes", request="audit every route", policy="partial",
    )

    argv = captured["argv"]
    assert spawned["pid"] == 4242
    assert "go-worker" in argv
    assert argv[argv.index("--stem") + 1] == "audit-routes"
    assert argv[argv.index("--policy") + 1] == "partial"
    written = Path(argv[argv.index("--request-file") + 1]).read_text(encoding="utf-8")
    assert written == "audit every route"
    assert Path(spawned["log"]).parent.name == "planning"
    # `status` has to see the job while it is still drafting.
    assert executor.list_planning(tmp_path)[0]["stem"] == "audit-routes"


def test_a_failed_draft_stays_visible_until_someone_reads_it(tmp_path):
    """A drafting failure keeps its marker: it does not vanish like a success."""
    executor.write_planning(
        tmp_path, stem="audit", pid=1, request="audit",
        state_name="failed", error="no_items: nothing found",
    )

    jobs = executor.list_planning(tmp_path)

    assert [job["stem"] for job in jobs] == ["audit"]
    assert jobs[0]["state"] == "failed"
    assert jobs[0]["error"] == "no_items: nothing found"


def test_the_stagger_keeps_starts_apart_by_default(monkeypatch):
    """A harness opening shared state at boot cannot share the second."""
    slept: list[float] = []
    monkeypatch.setattr(executor.time, "sleep", slept.append)
    monkeypatch.delenv("OSW_WORKFLOW_STAGGER_SECONDS", raising=False)

    executor._stagger_before_start()

    assert slept == [executor.DISPATCH_STAGGER_SECONDS]


def test_the_stagger_can_be_turned_off(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(executor.time, "sleep", slept.append)
    monkeypatch.setenv("OSW_WORKFLOW_STAGGER_SECONDS", "0")

    executor._stagger_before_start()

    assert slept == []


def test_a_first_run_initializes_a_fresh_checkout(tmp_path):
    """`new` refuses to dispatch without state.json; a run creates it itself."""
    definition = install(tmp_path, [SCAN])
    assert not (tmp_path / ".orca" / "osw" / "state.json").exists()

    run = executor.start_run(tmp_path, definition)

    assert run["state"] == "running"
    assert (tmp_path / ".orca" / "osw" / "state.json").is_file()
