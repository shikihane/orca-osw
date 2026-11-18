"""Read-only view of workflow runs, and the plain-text renderers.

A run is a directory ``.orca/osw/runs/<run_id>/`` holding one ``run.json``::

    {
      "run_id": "run_001",
      "definition": "audit-routes",
      "definition_path": ".orca/osw/workflows/audit-routes.json",
      "state": "running",
      "started": "2026-11-17T12:00:00Z",
      "finished": null,
      "phases": [{"id": "scan", "steps": 1}, {"id": "audit", "steps": 5}],
      "steps": [
        {"step_id": "scan", "phase": "scan", "state": "done",
         "started": "...", "finished": "...", "error": ""}
      ]
    }

Everything here only reads. Nothing creates a run directory, and the
renderers return strings so the CLI owns printing and tests can assert on the
text without a terminal.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from osw.state import state_dir
from osw.workflow.defs import WorkflowError

RUN_FILE = "run.json"
#: The supervisor's own record, beside the run it owns. The supervisor writes
#: this one file and nothing else: ``run.json`` stays the scheduler's.
SUPERVISOR_FILE = "supervisor.json"
STEP_STATES = ("done", "running", "failed", "waiting")


# ---------------------------------------------------------------------------
# Paths and reading
# ---------------------------------------------------------------------------

def runs_dir(root: Path) -> Path:
    """Where run archives live for one project."""
    return state_dir(root) / "runs"


def run_file(root: Path, run_id: str) -> Path:
    return runs_dir(root) / run_id / RUN_FILE


def read_run(root: Path, run_id: str) -> dict:
    """Read one ``run.json``. A missing archive is a refusal, not an empty run."""
    path = run_file(root, run_id)
    if not run_id or "/" in run_id or "\\" in run_id or run_id in (".", ".."):
        raise WorkflowError("run_not_found", f"invalid run id {run_id!r}")
    if not path.is_file():
        raise WorkflowError("run_not_found", f"run {run_id!r} not found at {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError("run_not_found", f"cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise WorkflowError("run_not_found", f"{path}: top level must be an object")
    payload.setdefault("run_id", run_id)
    # Archives written before rounds carry no key version; their step keys are
    # the original material, which is version 1.
    payload.setdefault("keys_version", 1)
    return payload


def list_runs(root: Path) -> list[dict]:
    """Every readable run, newest first. No ``runs/`` directory means no runs."""
    directory = runs_dir(root)
    if not directory.is_dir():
        return []
    runs: list[dict] = []
    for child in directory.iterdir():
        path = child / RUN_FILE if child.is_dir() else None
        if path is None or not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A half-written archive is not a run the operator can act on.
            continue
        if isinstance(payload, dict):
            payload.setdefault("run_id", child.name)
            runs.append(payload)
    runs.sort(key=lambda run: (str(run.get("started") or ""), str(run.get("run_id") or "")),
              reverse=True)
    return runs


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _phase_ids(run: dict) -> list[str]:
    """Declared phase order from the archive, falling back to step records."""
    ordered: list[str] = []
    for entry in run.get("phases") or []:
        phase_id = entry.get("id") if isinstance(entry, dict) else entry
        if isinstance(phase_id, str) and phase_id not in ordered:
            ordered.append(phase_id)
    for step in run.get("steps") or []:
        if not isinstance(step, dict):
            continue
        phase_id = step.get("phase")
        if isinstance(phase_id, str) and phase_id not in ordered:
            ordered.append(phase_id)
    return ordered


def _declared_total(run: dict, phase_id: str) -> int | None:
    """``phases[i].steps`` when the archive recorded a count up front."""
    for entry in run.get("phases") or []:
        if not isinstance(entry, dict) or entry.get("id") != phase_id:
            continue
        steps = entry.get("steps")
        if isinstance(steps, int) and not isinstance(steps, bool):
            return steps
    return None


def phase_rows(run: dict) -> list[dict]:
    """Per-phase counts of done, running, failed and waiting steps.

    ``total`` comes from the phase's recorded step count when the archive has
    one, because a ``for_each`` phase may not have produced its step records
    yet; otherwise it is the number of step records seen.
    """
    steps = [step for step in run.get("steps") or [] if isinstance(step, dict)]
    rows: list[dict] = []
    for phase_id in _phase_ids(run):
        counts = {state: 0 for state in STEP_STATES}
        for step in steps:
            if step.get("phase") != phase_id:
                continue
            state = step.get("state")
            # Anything not finished, working or broken is still waiting.
            counts[state if state in counts else "waiting"] += 1
        total = _declared_total(run, phase_id)
        if total is None:
            total = sum(counts.values())
        rows.append({"id": phase_id, "total": total, **counts})
    return rows


def step_lines(run: dict) -> list[str]:
    """One line per step record, in archive order."""
    lines: list[str] = []
    for step in run.get("steps") or []:
        if not isinstance(step, dict):
            continue
        step_id = str(step.get("step_id") or "-")
        phase = str(step.get("phase") or "-")
        state = str(step.get("state") or "waiting")
        note = str(step.get("error") or step.get("note") or "")
        lines.append(f"  {step_id:<24} {phase:<12} {state:<8} {note}".rstrip())
    return lines


def has_active_steps(run: dict) -> bool:
    """True when a step is failed or still running: the status STEPS section."""
    for step in run.get("steps") or []:
        if isinstance(step, dict) and step.get("state") in ("failed", "running"):
            return True
    return False


# ---------------------------------------------------------------------------
# Renderers
#
# Column widths are fixed so the header line is stable and greppable; a value
# longer than its column pushes the rest of that row right, as `osw list` does.
# ---------------------------------------------------------------------------

LS_COLUMNS = ("RUN_ID", "DEFINITION", "STATE", "STARTED")
LS_WIDTHS = (8, 12, 7)
PHASE_COLUMNS = ("PHASE", "DONE/TOTAL", "RUNNING", "FAILED", "WAITING")
PHASE_WIDTHS = (12, 12, 9, 8)
DRY_COLUMNS = ("PHASE", "MODE", "FROM", "TASKS", "ROUNDS", "GATE")
DRY_WIDTHS = (8, 11, 9, 6, 6)


def _row(cells: tuple[str, ...], widths: tuple[int, ...]) -> str:
    """Cells padded to ``widths``; a value that fills or overflows its column
    still gets one separating space, so two values never run together."""
    assert len(cells) == len(widths) + 1
    padded = "".join(
        f"{cell:<{width}}" if len(cell) < width else f"{cell} "
        for cell, width in zip(cells, widths)
    )
    return f"{padded}{cells[-1]}"


def _header(columns: tuple[str, ...], widths: tuple[int, ...]) -> str:
    return _row(columns, widths)


def render_ls(runs: list[dict]) -> str:
    if not runs:
        return "No runs."
    lines = [_header(LS_COLUMNS, LS_WIDTHS)]
    for run in runs:
        lines.append(_row(
            (
                str(run.get("run_id") or "-"),
                str(run.get("definition") or "-"),
                str(run.get("state") or "-"),
                str(run.get("started") or "-"),
            ),
            LS_WIDTHS,
        ))
    return "\n".join(lines)


def run_header(run: dict) -> str:
    """The one-line identity of a run, shared by status and show."""
    return (
        f"run {run.get('run_id') or '-'}  "
        f"def {run.get('definition') or '-'}  "
        f"{run.get('state') or '-'}  "
        f"started {run.get('started') or '-'}"
    )


def render_phase_table(run: dict) -> str:
    lines = [_header(PHASE_COLUMNS, PHASE_WIDTHS)]
    for row in phase_rows(run):
        lines.append(_row(
            (
                str(row["id"]),
                f"{row['done']}/{row['total']}",
                str(row["running"]),
                str(row["failed"]),
                str(row["waiting"]),
            ),
            PHASE_WIDTHS,
        ))
    return "\n".join(lines)


def render_status(run: dict) -> str:
    """Identity, phase table, and steps only when something needs attention."""
    lines = [run_header(run)]
    supervisor = supervisor_view(run)
    if supervisor["restarts"]:
        # A restarted scheduler is why a run took as long as it did; one that
        # never restarted says nothing worth a line.
        lines.append(
            f"SUPERVISOR pid {supervisor['pid']} restarts {supervisor['restarts']}"
        )
    lines.append(render_phase_table(run))
    if has_active_steps(run):
        lines.append("STEPS")
        lines.extend(step_lines(run) or ["  none"])
    return "\n".join(lines)


def render_show(run: dict) -> str:
    """Everything the archive holds: definition, phases and every step."""
    lines = [run_header(run)]
    definition_path = run.get("definition_path")
    if definition_path:
        lines.append(f"DEFINITION  {definition_path}")
    lines.append("PHASES")
    lines.append(render_phase_table(run))
    lines.append("STEPS")
    lines.extend(step_lines(run) or ["  none"])
    return "\n".join(lines)


def _phase_entry(run: dict, phase_id: str) -> dict:
    """The archive's own record for one phase, or an empty one when absent."""
    for entry in run.get("phases") or []:
        if isinstance(entry, dict) and entry.get("id") == phase_id:
            return entry
    return {}


def _phase_round(entry: dict) -> int:
    """The round a phase is on. An archive from before rounds is on round 1."""
    value = entry.get("round")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return 1


def _gate_view(entry: dict) -> dict:
    """A phase's last gate verdict, reduced to what ``status`` reports."""
    gate = entry.get("gate")
    if not isinstance(gate, dict):
        gate = {}
    round_no = gate.get("round")
    passed = gate.get("pass")
    return {
        "round": round_no if isinstance(round_no, int) and not isinstance(round_no, bool) else 0,
        "pass": passed if isinstance(passed, bool) else None,
        "reason": str(gate.get("reason") or ""),
    }


def _scheduler_alive(run: dict) -> bool:
    """Whether the scheduler that owns ``run`` is still beating.

    The rule lives in the archive, which imports this module for ``RUN_FILE``,
    so the import stays local. Nothing is rewritten: liveness is computed from
    the heartbeat the scheduler stamps, never inferred back into the record.
    """
    from osw.workflow.archive import scheduler_alive

    return scheduler_alive(Path("."), run)


def supervisor_file(root: Path, run_id: str) -> Path:
    """The supervisor's sidecar for one run."""
    return runs_dir(root) / run_id / SUPERVISOR_FILE


def read_supervisor(root: Path, run_id: str) -> dict:
    """The supervisor's record for one run, as read: ``{}`` when it has none.

    A sidecar that is missing, half-written or not an object is not an error:
    the run simply has no supervisor, which is what ``status`` reports.
    """
    name = str(run_id or "")
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return {}
    try:
        payload = json.loads(supervisor_file(root, name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def supervisor_view(run: dict) -> dict:
    """The sidecar reduced to what ``status`` reports: pid, alive, restarts.

    ``alive`` is the supervisor's own pid, never the scheduler's: the two
    answer different questions, and a live supervisor with a dead scheduler is
    exactly the state a restart is about to fix.
    """
    from osw.state import pid_is_running

    record = read_supervisor(Path("."), str(run.get("run_id") or ""))
    pid = record.get("pid")
    restarts = record.get("restarts")
    pid = pid if isinstance(pid, int) and not isinstance(pid, bool) else 0
    restarts = restarts if isinstance(restarts, int) and not isinstance(restarts, bool) else 0
    return {
        "pid": pid,
        "alive": bool(pid) and pid_is_running(pid),
        "restarts": max(0, restarts),
    }


def render_status_json(run: dict) -> dict:
    """The same counts, machine-readable: phases plus the raw run identity.

    ``goal_proposals`` is a count because the list itself is what
    ``workflow goal`` prints; a run from before rounds reports no goals and
    every phase on round 1. ``supervisor`` is read from the sidecar the
    supervisor writes, so a run nobody supervises reports a dead pid.
    """
    phases: list[dict] = []
    for row in phase_rows(run):
        entry = _phase_entry(run, row["id"])
        phases.append({
            **row,
            "round": _phase_round(entry),
            "gate": _gate_view(entry),
        })
    proposals = run.get("goal_proposals")
    return {
        "run_id": run.get("run_id"),
        "definition": run.get("definition"),
        "state": run.get("state"),
        "started": run.get("started"),
        "scheduler_alive": _scheduler_alive(run),
        "supervisor": supervisor_view(run),
        "goal": str(run.get("goal") or ""),
        "goal_proposals": len(proposals) if isinstance(proposals, list) else 0,
        "phases": phases,
    }


def render_dry_run(
    name: str,
    planned: list[Any],
    *,
    definition_path: Path | str,
    max_agents: int,
    static_total: int,
    runtime_fanout: bool,
) -> str:
    """The expansion as text. Nothing is dispatched and nothing is written."""
    lines = [
        "DRY RUN (nothing dispatched)",
        f"DEF     {name}   {definition_path}",
        _header(DRY_COLUMNS, DRY_WIDTHS),
    ]
    for phase in planned:
        from_cell = ",".join(phase.from_ids) or "-"
        if phase.tasks is None:
            tasks_cell = "? (expanded at run time: one agent per item)"
        else:
            tasks_cell = str(phase.tasks)
        # `ROUNDS` is the worst case the definition asks for; `GATE` says the
        # phase has to pass a written criterion before the run may call it done.
        rounds_cell = str(phase.rounds)
        gate_cell = "criteria" if phase.criteria else "-"
        lines.append(_row(
            (phase.id, phase.mode, from_cell, tasks_cell, rounds_cell, gate_cell),
            DRY_WIDTHS,
        ))
    if runtime_fanout:
        agents = f"{static_total} + N"
    else:
        agents = str(static_total)
    lines.append(f"AGENTS  {agents}   max_agents {max_agents}")
    return "\n".join(lines)
