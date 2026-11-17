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
DRY_COLUMNS = ("PHASE", "MODE", "FROM", "TASKS")
DRY_WIDTHS = (8, 11, 9)


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
    lines = [run_header(run), render_phase_table(run)]
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


def render_status_json(run: dict) -> dict:
    """The same counts, machine-readable: phases plus the raw run identity."""
    return {
        "run_id": run.get("run_id"),
        "definition": run.get("definition"),
        "state": run.get("state"),
        "started": run.get("started"),
        "phases": phase_rows(run),
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
        lines.append(_row((phase.id, phase.mode, from_cell, tasks_cell), DRY_WIDTHS))
    if runtime_fanout:
        agents = f"{static_total} + N"
    else:
        agents = str(static_total)
    lines.append(f"AGENTS  {agents}   max_agents {max_agents}")
    return "\n".join(lines)
