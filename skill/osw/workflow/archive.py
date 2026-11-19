"""The durable run archive: the scheduler's only state.

One run is one directory beside the existing agent records::

    .orca/osw/runs/<run_id>/run.json          header + per-phase step keys
    .orca/osw/runs/<run_id>/steps/<key>.json  one step record
    .orca/osw/runs/<run_id>/verdicts.jsonl    append-only verifier ledger
    .orca/osw/runs/<run_id>/requests/         human verbs, one file each

``run.json`` is the index the read-only board renders, so it carries both the
archive's own names (``started_at``) and the board's (``started``,
``definition`` as a plain name). Every write is atomic — a temporary file in
the same directory, then ``os.replace`` — because a killed scheduler must
leave every record either whole or absent, never half-written.

Two writers touch this tree and nobody else does: the scheduler writes
``run.json``, the step records and consumes request files; verifiers append to
``verdicts.jsonl``. Agent records under ``agents/`` belong to their watcher and
are only ever read from here.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from osw.log import get_logger
from osw.state import pid_is_running, state_dir, write_json_atomic
from osw.workflow.board import RUN_FILE
from osw.workflow.defs import Definition, WorkflowError

log = get_logger("workflow.archive")

STEPS_DIR = "steps"
REQUESTS_DIR = "requests"
OUTPUTS_DIR = "outputs"
GATES_DIR = "gates"
VERDICT_FILE = "verdicts.jsonl"

# Gate steps live in the same phase as the work they judge; -1 keeps them out
# of the item index space, so an item key never collides with a gate key.
GATE_INDEX = -1
# Bumped when the step-key material changes shape. A run records the version
# it was created with so resume recomputes the keys it actually wrote.
KEYS_VERSION = 2

# A scheduler is alive when its pid runs and its last beat is this young. The
# executor beats every poll, so three missed local ticks still read alive.
SCHEDULER_STALE_SECONDS = 90.0

POLICIES = ("strict", "partial")
VERIFY_MODES = ("none", "suspect", "all")
REQUESTS_VERBS = ("accept", "reject", "retry", "stop")

_UNSAFE = re.compile(r"[/\\]|^\.\.?$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def schema_token(schema: Any) -> str:
    """The ``schema_id`` of a phase's schema: ``""`` when it declares none.

    A string schema is its own id; an object is canonicalised so two equal
    schemas written with different key order still produce one step key.
    """
    if schema is None:
        return ""
    if isinstance(schema, str):
        return schema.strip()
    return json.dumps(schema, sort_keys=True, separators=(",", ":"))


def step_key(phase_id: str, index: int, prompt: str, schema_id: str,
             *, round_no: int = 0, keys_version: int = KEYS_VERSION) -> str:
    """The identity of one declared step, ``sha256(material)[:32]``.

    Version 1 is the original material, kept byte-for-byte so every archive
    written before rounds existed still recomputes the keys it holds. Version 2
    adds the round, because a repeating phase is a new instance of the same
    declaration: the same prompt in round 2 must not reuse round 1's record.
    """
    if int(keys_version) >= KEYS_VERSION:
        material = "|".join((
            str(KEYS_VERSION), str(int(round_no)), phase_id,
            str(int(index)), prompt, schema_id,
        ))
    else:
        material = "\0".join((phase_id, str(int(index)), prompt, schema_id))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def new_run_id(name: str) -> str:
    """``<UTC timestamp>_<name>_<6 hex>``: sortable, traceable, collision-free."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", name or "").strip("-") or "run"
    return f"{stamp}_{safe}_{secrets.token_hex(3)}"


def _checked(part: str, what: str) -> str:
    if not part or _UNSAFE.search(part):
        raise WorkflowError("run_not_found", f"invalid {what} {part!r}")
    return part


def run_dir(root: Path, run_id: str) -> Path:
    """``<project>/.orca/osw/runs/<run_id>``."""
    return state_dir(Path(root)) / "runs" / _checked(str(run_id), "run id")


def run_path(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / RUN_FILE


def step_path(root: Path, run_id: str, key: str) -> Path:
    return run_dir(root, run_id) / STEPS_DIR / f"{_checked(str(key), 'step key')}.json"


def requests_dir(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / REQUESTS_DIR


def verdicts_path(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / VERDICT_FILE


def output_path(root: Path, run_id: str, phase_id: str, index: int) -> Path:
    """The file one step writes its structured output to.

    A workflow step's handoff is the watcher's four-section markdown, so a
    producer cannot put machine-readable data there without fighting the
    template. The scheduler therefore names an output file in the prompt
    (``{{output}}``) and reads the array for ``for_each`` from it.
    """
    name = f"{_checked(str(phase_id), 'phase id')}-{int(index)}.json"
    return run_dir(root, run_id) / OUTPUTS_DIR / name


def gate_output_path(root: Path, run_id: str, phase_id: str, round_no: int) -> Path:
    """The file a gate step writes its verdict to: one per phase and round."""
    name = f"{_checked(str(phase_id), 'phase id')}-gate-r{int(round_no)}.json"
    return run_dir(root, run_id) / OUTPUTS_DIR / name


def gate_brief_path(root: Path, run_id: str, phase_id: str, round_no: int) -> Path:
    """The file the gate is told to read: goal, criterion, evidence, outputs.

    The brief lives on disk instead of inside the prompt because the dispatch
    seam passes a prompt as one command-line argument, and Windows refuses a
    command line over ~32 KiB (WinError 206). A blind gate reads the same
    content either way; a file has no length ceiling.
    """
    name = f"{_checked(str(phase_id), 'phase id')}-gate-r{int(round_no)}.md"
    return run_dir(root, run_id) / GATES_DIR / name


# ---------------------------------------------------------------------------
# Run header
# ---------------------------------------------------------------------------

def create_run(
    root: Path,
    definition: Definition,
    *,
    policy: str = "strict",
    verify: str = "suspect",
    max_attempts: int = 2,
) -> dict:
    """Start the archive for one execution and write its ``run.json``.

    Called by the scheduler on its first dispatch — never by ``--dry-run``.
    """
    if policy not in POLICIES:
        raise WorkflowError(
            "definition_invalid",
            f"policy must be one of {', '.join(POLICIES)}, got {policy!r}",
        )
    if verify not in VERIFY_MODES:
        raise WorkflowError(
            "definition_invalid",
            f"verify must be one of {', '.join(VERIFY_MODES)}, got {verify!r}",
        )
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) \
            or max_attempts < 1:
        raise WorkflowError("definition_invalid", "max_attempts must be a positive integer")

    started = now_iso()
    run = {
        "run_id": new_run_id(definition.name),
        "definition": definition.name,
        "definition_path": str(definition.path),
        "state": "running",
        "policy": policy,
        "verify": verify,
        "max_attempts": int(max_attempts),
        "max_agents": int(definition.max_agents),
        # Which shape of step-key material this archive was written with.
        "keys_version": KEYS_VERSION,
        # The end state the run is trying to reach, and changes a gate or a
        # person proposed. A proposal is recorded, never applied.
        "goal": definition.goal,
        "goal_proposals": [],
        # Stamped by the scheduler on start and on every tick; read by
        # `status` and `resume` to tell a live run from an abandoned one.
        "scheduler": {"pid": 0, "host": "", "started_at": "", "beat_at": ""},
        # `started` is what the board renders; `started_at` is the archive's
        # own name for the same instant (the spec's run-header fields).
        "started": started,
        "started_at": started,
        "finished": None,
        "finished_at": None,
        "phases": [
            {
                "id": phase.id,
                "state": "pending",
                "steps": [],
                "round": 1,
                "gate": {"round": 0, "step": "", "pass": None, "reason": ""},
            }
            for phase in definition.phases
        ],
        "steps": [],
    }
    write_run(root, run)
    return run


def write_run(root: Path, run: dict) -> None:
    """Atomically write ``run.json``. The scheduler is the only caller."""
    write_json_atomic(run_path(root, run["run_id"]), run)


def _beat_age_seconds(stamp: Any, now: datetime) -> float | None:
    if not isinstance(stamp, str) or not stamp.strip():
        return None
    text = stamp.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        beat = datetime.fromisoformat(text)
    except ValueError:
        return None
    if beat.tzinfo is None:
        beat = beat.replace(tzinfo=timezone.utc)
    return (now - beat).total_seconds()


def scheduler_alive(root: Path, run: dict) -> bool:
    """Whether the scheduler recorded in ``run`` is still driving it.

    A pure read: the pid must be running *and* the last beat must be younger
    than ``SCHEDULER_STALE_SECONDS``. A live pid with an old beat is a process
    that stopped ticking (or a reused pid), and neither may block ``resume``.
    """
    del root  # part of the contract; liveness is a property of the record
    block = run.get("scheduler")
    if not isinstance(block, dict):
        return False
    try:
        pid = int(block.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if not pid_is_running(pid):
        return False
    age = _beat_age_seconds(block.get("beat_at"), datetime.now(timezone.utc))
    return age is not None and age <= SCHEDULER_STALE_SECONDS


# ---------------------------------------------------------------------------
# Step records
# ---------------------------------------------------------------------------

def read_step(root: Path, run_id: str, key: str) -> dict | None:
    """One step record, or ``None`` when the archive has none for that key."""
    path = step_path(root, run_id, key)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # An unreadable record is not an absent one: reusing `None` here would
        # silently re-dispatch work the archive still holds.
        raise WorkflowError("run_not_found", f"cannot read step record {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise WorkflowError("run_not_found", f"{path}: top level must be an object")
    return payload


def write_step(root: Path, run: dict, step: dict) -> None:
    """Write one step record, then refresh the run's phase/summary counters.

    ``run.json`` stays the index the read-only board reads: the phase gains the
    step key, and the top-level ``steps`` list gains that step's summary.
    """
    write_json_atomic(step_path(root, run["run_id"], step["step_key"]), step)
    _index_step(run, step)
    write_run(root, run)


def _index_step(run: dict, step: dict) -> None:
    phase_id = step["phase"]
    phases = run.setdefault("phases", [])
    entry = next((item for item in phases if item.get("id") == phase_id), None)
    if entry is None:
        entry = {"id": phase_id, "state": "pending", "steps": []}
        phases.append(entry)
    keys = entry.setdefault("steps", [])
    if step["step_key"] not in keys:
        keys.append(step["step_key"])

    summary = {
        "step_id": step["step_key"],
        "phase": phase_id,
        "index": step.get("index", 0),
        "state": step.get("state", "pending"),
        "attempts": len(step.get("attempts") or []),
        "agent": step.get("agent", ""),
        "error": step.get("error", ""),
        "note": step.get("warning", ""),
    }
    steps = run.setdefault("steps", [])
    for position, existing in enumerate(steps):
        if existing.get("step_id") == step["step_key"]:
            steps[position] = summary
            return
    steps.append(summary)


def phase_entry(run: dict, phase_id: str) -> dict | None:
    for entry in run.get("phases") or []:
        if entry.get("id") == phase_id:
            return entry
    return None


def phase_steps(root: Path, run: dict, phase_id: str) -> list[dict]:
    """Every readable step record of one phase, in archive order."""
    entry = phase_entry(run, phase_id) or {}
    records: list[dict] = []
    for key in entry.get("steps") or []:
        record = read_step(root, run["run_id"], key)
        if record is not None:
            records.append(record)
    return records


def phase_round_steps(root: Path, run: dict, phase_id: str, round_no: int) -> list[dict]:
    """The step records of one phase belonging to one round, in index order.

    A record without a ``round`` came from before rounds existed (or from a
    non-repeating phase), so it belongs to round 0 — the only round such a
    phase ever has.
    """
    target = int(round_no)
    records = [
        record for record in phase_steps(root, run, phase_id)
        if int(record.get("round") or 0) == target
    ]
    records.sort(key=lambda record: int(record.get("index") or 0))
    return records


# ---------------------------------------------------------------------------
# Verdict ledger: append-only, written by verifiers, read by the scheduler
# ---------------------------------------------------------------------------

def append_verdict(root: Path, run_id: str, verdict: dict) -> None:
    """Append one JSON line. A single ``write`` per line keeps it whole."""
    path = verdicts_path(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(verdict, ensure_ascii=False, sort_keys=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def read_verdicts(root: Path, run_id: str) -> list[dict]:
    """Every parseable verdict line, in file order.

    A truncated or malformed line is skipped rather than fatal: the ledger is
    appended by many processes and a half-line proves nothing.
    """
    path = verdicts_path(root, run_id)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    except OSError as exc:
        log.warning("cannot read %s: %s", path, exc)
        return []
    verdicts: list[dict] = []
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            log.warning("%s:%d: unparseable verdict line skipped", path, number)
            continue
        if isinstance(payload, dict):
            verdicts.append(payload)
        else:
            log.warning("%s:%d: verdict line is not an object", path, number)
    return verdicts


# ---------------------------------------------------------------------------
# Human verbs: a side file the scheduler consumes on its next tick
# ---------------------------------------------------------------------------

def write_request(
    root: Path, run_id: str, verb: str, key: str, reason: str = "",
) -> Path:
    """Drop one decision request; the scheduler applies and deletes it."""
    if verb not in REQUESTS_VERBS:
        raise WorkflowError(
            "definition_invalid",
            f"unknown workflow verb {verb!r}; use one of {', '.join(REQUESTS_VERBS)}",
        )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    safe_verb = re.sub(r"[^A-Za-z0-9_.-]+", "-", verb)
    safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(key))
    path = requests_dir(root, run_id) / f"{stamp}_{safe_verb}_{safe_key}.json"
    write_json_atomic(path, {
        "verb": verb,
        "step": str(key),
        "reason": str(reason or ""),
        "at": now_iso(),
    })
    return path


def consume_requests(root: Path, run_id: str) -> list[dict]:
    """Read and delete every well-formed request, oldest first.

    The file is deleted before it is returned, so a crash cannot apply one
    decision twice. A malformed file stays on disk with a warning: it is
    evidence of a bad write, and deleting it would erase that.
    """
    directory = requests_dir(root, run_id)
    if not directory.is_dir():
        return []
    applied: list[dict] = []
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("skipping malformed request %s: %s", path, exc)
            continue
        if not isinstance(payload, dict) \
                or payload.get("verb") not in REQUESTS_VERBS \
                or (payload.get("verb") != "stop" and not payload.get("step")):
            log.warning("skipping malformed request %s: no verb/step", path)
            continue
        try:
            path.unlink()
        except OSError as exc:
            log.warning("cannot consume request %s: %s", path, exc)
            continue
        applied.append(payload)
    return applied
