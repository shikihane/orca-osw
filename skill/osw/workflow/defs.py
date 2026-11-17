"""Workflow definitions: one closed schema and one pure expansion.

A definition is a small, committed JSON plan — a name, an agent ceiling and an
ordered list of phases. It is checked once, before anything runs, so a typo in
a phase reference is a refusal rather than a half-started run.

``expand`` is deliberately static: phases are declared up front, and only a
``for_each`` item count is known at run time, so expansion reports ``None``
for those instead of inventing a number.

Prompts are parameter-only. A placeholder that implies a clock or randomness
(``{{now}}``, ``{{random}}``, …) is refused, because a prompt that changes on
every run makes a resume keyed by step id miss its own history.
``{{output}}`` is the one run-scoped placeholder: it names the file the step
writes structured output to, and resume stays inside one run, so the key it
changes belongs to that run alone.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Polling cadence for a running workflow. Remote status reads cross SSH, so
# each one costs a process launch and a round trip and the floor is higher.
POLL_LOCAL_SECONDS = 5.0
POLL_REMOTE_SECONDS = 20.0

DEFAULT_MAX_AGENTS = 64
MODES = ("single", "for_each")
TOP_KEYS = frozenset({"name", "max_agents", "phases", "approval"})
PHASE_KEYS = frozenset({"id", "mode", "from", "task", "schema"})

# `approval: "human"` means the run may not start until a human accepts it.
APPROVAL_VALUES = ("human",)

# Anything that would make the prompt differ between two runs.
UNSTABLE_TOKENS = frozenset({"now", "today", "timestamp", "uuid", "random"})
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

CODES = frozenset({
    "no_definition",
    "definition_invalid",
    "unknown_field",
    "bad_phase_ref",
    "unstable_placeholder",
    "run_not_found",
    "not_implemented",
    "too_many_agents",
    "no_items",
    "step_not_found",
    "approval_unavailable",
    "router_forbidden",
})


class WorkflowError(Exception):
    """One typed workflow failure: ``code`` plus a one-line ``message``."""

    def __init__(self, code: str, message: str = "") -> None:
        if code not in CODES:
            raise ValueError(f"unknown workflow error code: {code}")
        self.code = code
        self.message = message or code
        super().__init__(f"{code}: {self.message}" if message else code)

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class Phase:
    """One declared phase. ``from_ids`` are earlier phases this one waits on."""

    id: str
    mode: str
    from_ids: tuple[str, ...]
    task: str
    schema: Any = None


@dataclass(frozen=True)
class Definition:
    name: str
    max_agents: int
    phases: tuple[Phase, ...]
    path: Path
    approval: str | None = None


@dataclass(frozen=True)
class PlannedPhase:
    """A phase with its task count known as far as the definition can say.

    ``tasks is None`` means "expanded at run time": the item count of a
    ``for_each`` phase comes from the upstream phase's output.
    """

    id: str
    mode: str
    from_ids: tuple[str, ...]
    tasks: int | None


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def project_workflows_dir(root: Path) -> Path:
    """Committed definitions for one project."""
    return root / ".orca" / "osw" / "workflows"


def home_workflows_dir() -> Path:
    """Personal definitions shared by every project on this machine."""
    return Path.home() / ".orca" / "osw" / "workflows"


def definition_paths(root: Path, name: str) -> list[Path]:
    """The search order: project first, then home. ``load_definition`` picks
    the first hit, so a project definition overrides a personal one."""
    return [
        project_workflows_dir(root) / f"{name}.json",
        home_workflows_dir() / f"{name}.json",
    ]


# ---------------------------------------------------------------------------
# Loading and validation
# ---------------------------------------------------------------------------

def load_definition(root: Path, name: str) -> Definition:
    """Read and validate one definition by name.

    The schema is closed: an unknown key is a refusal, never a silent extra.
    """
    if not NAME_RE.match(name or ""):
        raise WorkflowError("no_definition", f"invalid workflow name {name!r}")

    searched = definition_paths(root, name)
    for path in searched:
        if path.is_file():
            return _load_file(path)
    listed = ", ".join(str(path) for path in searched)
    raise WorkflowError("no_definition", f"workflow {name!r} not found; searched {listed}")


def _load_file(path: Path) -> Definition:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError("definition_invalid", f"{path}: {exc}") from exc
    return parse_definition(raw, path)


def parse_definition(raw: Any, path: Path) -> Definition:
    """Validate a decoded definition document and return the typed form."""
    if not isinstance(raw, dict):
        raise WorkflowError("definition_invalid", f"{path}: top level must be an object")

    unknown = sorted(set(raw) - TOP_KEYS)
    if unknown:
        raise WorkflowError(
            "unknown_field",
            f"{path}: unknown field {unknown[0]!r} at the top level",
        )

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise WorkflowError("definition_invalid", f"{path}: 'name' must be a non-empty string")

    max_agents = raw.get("max_agents", DEFAULT_MAX_AGENTS)
    if not isinstance(max_agents, int) or isinstance(max_agents, bool) or max_agents < 1:
        raise WorkflowError(
            "definition_invalid", f"{path}: 'max_agents' must be a positive integer",
        )

    approval = raw.get("approval")
    if approval is not None and approval not in APPROVAL_VALUES:
        allowed = ", ".join(repr(value) for value in APPROVAL_VALUES)
        raise WorkflowError(
            "definition_invalid",
            f"{path}: 'approval' must be {allowed} or absent, got {approval!r}",
        )

    raw_phases = raw.get("phases")
    if not isinstance(raw_phases, list) or not raw_phases:
        raise WorkflowError(
            "definition_invalid", f"{path}: 'phases' must be a non-empty list",
        )

    phases: list[Phase] = []
    seen: dict[str, int] = {}
    for index, entry in enumerate(raw_phases):
        phase = _parse_phase(entry, index, path, seen)
        seen[phase.id] = index
        phases.append(phase)
    return Definition(name=name, max_agents=max_agents,
                      phases=tuple(phases), path=path, approval=approval)


def _parse_phase(entry: Any, index: int, path: Path, seen: dict[str, int]) -> Phase:
    where = f"phase #{index + 1}"
    if not isinstance(entry, dict):
        raise WorkflowError("definition_invalid", f"{path}: {where} must be an object")

    phase_id = entry.get("id")
    if isinstance(phase_id, str) and phase_id.strip():
        where = f"phase {phase_id!r}"

    unknown = sorted(set(entry) - PHASE_KEYS)
    if unknown:
        raise WorkflowError(
            "unknown_field",
            f"{path}: unknown field {unknown[0]!r} in {where}",
        )

    if not isinstance(phase_id, str) or not phase_id.strip():
        raise WorkflowError("definition_invalid", f"{path}: {where} needs a string 'id'")
    if phase_id in seen:
        raise WorkflowError("definition_invalid", f"{path}: duplicate phase id {phase_id!r}")

    mode = entry.get("mode")
    if mode not in MODES:
        raise WorkflowError(
            "definition_invalid",
            f"{path}: {where} 'mode' must be one of {', '.join(MODES)}",
        )

    task = entry.get("task")
    if not isinstance(task, str) or not task.strip():
        raise WorkflowError("definition_invalid", f"{path}: {where} needs a non-empty 'task'")
    _check_placeholders(task, where, path)

    schema = entry.get("schema")
    if schema is not None and not isinstance(schema, (dict, str)):
        raise WorkflowError(
            "definition_invalid", f"{path}: {where} 'schema' must be an object or a string",
        )
    if isinstance(schema, str):
        _check_placeholders(schema, where, path)

    from_ids = _parse_from(entry.get("from"), where, path)
    if mode == "for_each" and not from_ids:
        raise WorkflowError(
            "definition_invalid", f"{path}: {where} is 'for_each' and needs 'from'",
        )
    for ref in from_ids:
        if ref not in seen:
            raise WorkflowError(
                "bad_phase_ref",
                f"{path}: {where} refers to {ref!r}, which is not an earlier phase",
            )
    return Phase(id=phase_id, mode=mode, from_ids=from_ids, task=task, schema=schema)


def _parse_from(value: Any, where: str, path: Path) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    raise WorkflowError(
        "definition_invalid", f"{path}: {where} 'from' must be a string or a list of strings",
    )


def _check_placeholders(text: str, where: str, path: Path) -> None:
    for match in PLACEHOLDER_RE.finditer(text):
        head = match.group(1).split(".", 1)[0].lower()
        if head in UNSTABLE_TOKENS:
            raise WorkflowError(
                "unstable_placeholder",
                f"{path}: {where} uses {match.group(0)}, which changes between runs; "
                "prompts must be parameter-only",
            )


# ---------------------------------------------------------------------------
# Expansion
# ---------------------------------------------------------------------------

def expand(definition: Definition) -> list[PlannedPhase]:
    """Static expansion: the phases in order, with the counts that are knowable.

    A ``single`` phase is always one agent; a ``for_each`` phase fans out once
    per upstream item, which only the run can count, so its ``tasks`` is
    ``None``.
    """
    planned: list[PlannedPhase] = []
    for phase in definition.phases:
        tasks = 1 if phase.mode == "single" else None
        planned.append(
            PlannedPhase(
                id=phase.id,
                mode=phase.mode,
                from_ids=phase.from_ids,
                tasks=tasks,
            )
        )
    return planned


def static_agent_total(planned: list[PlannedPhase]) -> int:
    """Agents a dry run can count without asking the run: the declared ones."""
    return sum(phase.tasks or 0 for phase in planned if phase.tasks is not None)


def has_runtime_fanout(planned: list[PlannedPhase]) -> bool:
    return any(phase.tasks is None for phase in planned)
