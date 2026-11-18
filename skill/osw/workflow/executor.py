"""The workflow scheduler: one ``join`` → ``judge`` → ``dispatch`` pass per tick.

The archive (``archive.py``) holds every fact; this module holds only the rules
that turn those facts into the next dispatch. ``tick`` therefore recomputes
everything from disk on every call and is idempotent: killing the scheduler
loses no decision, and running it twice performs each transition once.

Decisions this module makes, and why they are here rather than in a prompt:

* **One seam.** ``default_dispatch`` is the only code that shells out to
  ``new``, and it is built on ``fanout.new_argv`` / ``run_command`` /
  ``parse_created`` — the argv builder and the ``Created …`` parser are reused,
  never reimplemented. Tests substitute the function; nothing else changes.
* **A barrier is structural.** A phase starts only when every phase it names
  in ``from`` is terminal, so a merge phase can assume its inputs finished.
* **Verification is judged by code.** A step becomes a *candidate*; the
  scheduler dispatches a blind verifier for it, records the candidate
  artifact's sha256 first, and reads the verdict ledger afterwards. A changed
  artifact voids the verdict, an unreadable verdict gets one more verifier and
  then leaves the step ``unverifiable`` for a person. ``suspect`` (the default)
  verifies only steps the watcher's own checks flagged — ``no_tool_activity``
  or ``prompt_truncated`` — because those are the steps whose output looks like
  output but may be none.
* **Holds are not failures.** ``unverifiable`` and ``artifact_mutated`` are
  settled states that stop the run and wait for ``accept``/``reject``/``retry``.
  Recording them as failures would hide a human decision inside a policy
  default.
* **Resume is per step, not per prefix.** A step whose key matches a ``done``
  record is reused wherever it sits; a changed prompt creates a new key and
  invalidates nothing after it, because OSW agents share no prefix cache.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping

import anyio

from osw import fanout, state
from osw.log import get_logger
from osw.workflow import archive
from osw.workflow.board import list_runs, read_run
from osw.workflow.defs import (
    POLL_LOCAL_SECONDS,
    Definition,
    Phase,
    PLACEHOLDER_RE,
    WorkflowError,
    load_definition,
)

log = get_logger("workflow.executor")

#: The one substituted seam: (plan, definition, root) -> {"agent", "terminal"}.
Dispatch = Callable[[Mapping[str, Any], Definition, Path], dict]

RUNNING = "running"
DONE = "done"
FAILED = "failed"
PARTIAL = "partial"
BLOCKED = "blocked"
STOPPED = "stopped"
RUN_STATES = (RUNNING, DONE, FAILED, PARTIAL, BLOCKED, STOPPED)
_RUN_TERMINAL = (DONE, FAILED, PARTIAL, BLOCKED, STOPPED)

PENDING = "pending"
ACTIVE = "running"
STEP_DONE = "done"
STEP_FAILED = "failed"
SKIPPED = "skipped"
UNVERIFIABLE = "unverifiable"
MUTATED = "artifact_mutated"
#: States a step cannot leave without a person's verb.
HOLD_STATES = (UNVERIFIABLE, MUTATED)
#: States that count as settled when a phase decides whether it finished.
SETTLED_STATES = (STEP_DONE, STEP_FAILED, SKIPPED) + HOLD_STATES

PHASE_PENDING = "pending"
PHASE_RUNNING = "running"
PHASE_DONE = "done"
PHASE_PARTIAL = "partial"
PHASE_FAILED = "failed"
PHASE_SKIPPED = "skipped"
_PHASE_TERMINAL = (PHASE_DONE, PHASE_PARTIAL, PHASE_FAILED, PHASE_SKIPPED)

#: Agent-record states that end an attempt. The watcher writes the first two;
#: `osw list` flips an orphaned record to `lost`.
AGENT_TERMINAL = {"done": "done", "error": "error", "lost": "lost"}
#: Watcher warnings that mean "this output failed a defined check" (`suspect`).
SUSPECT_WARNINGS = ("no_tool_activity", "prompt_truncated")
VERDICTS = ("confirmed", "refuted", "unsure")

JSON_FENCE_RE = re.compile(r"```json[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)

# A project that never configured a provider still has to be able to run: the
# batch provider every other OSW path uses here is `pi`. The scheduler says so
# in its log rather than refusing and leaving the caller to go find config.
DEFAULT_PROVIDER = "pi"

#: Seconds between two agent starts in the same tick. See ``_stagger_before_start``.
DISPATCH_STAGGER_SECONDS = 2.0


# ---------------------------------------------------------------------------
# The dispatch seam
# ---------------------------------------------------------------------------

def _caller_terminal() -> str:
    """The pane that asked for this run, if the scheduler is inside one.

    Empty when the run was started from a plain shell or from a detached
    process: `new` then needs `--no-notify` instead of a caller handle.
    """
    return (os.environ.get("ORCA_TERMINAL_HANDLE") or "").strip()


def default_dispatch(plan: Mapping[str, Any], definition: Definition, root: Path) -> dict:
    """Create one agent through the existing fan-out path.

    The provider, model and thinking level come from the same project defaults
    ``osw super`` resolves (config.json, then ``OSW_*``, then AGENTS.md), so a
    workflow step and a fan-out task carry identical flags. A failure is a
    ``FanoutError`` — the scheduler records it as a failed attempt instead of
    dying with the run half-dispatched.
    """
    root = Path(root)
    defaults = fanout.project_defaults(root)
    provider = str(defaults.get("provider") or "").strip()
    if not provider:
        provider = DEFAULT_PROVIDER
        print(
            f"osw workflow: no provider configured; dispatching with {provider!r}",
            file=sys.stderr, flush=True,
        )
    prompt = str(plan.get("prompt") or "").strip()
    if not prompt:
        raise fanout.FanoutError("empty prompt; nothing to dispatch")
    job = fanout.Job(
        index=int(plan.get("index") or 0) + 1,
        task=prompt,
        provider=provider,
        model=str(defaults.get("model") or "").strip(),
        thinking=str(defaults.get("thinking") or "").strip(),
        caller_terminal=_caller_terminal(),
    )
    # Without a caller terminal `new` refuses to run at all (the completion
    # notification would have nowhere to go). The scheduler reads agent records
    # instead of waiting for notifications, so a headless run dispatches with
    # `--no-notify` rather than failing.
    argv = fanout.new_argv(job, no_notify=not job.caller_terminal)
    try:
        result = anyio.run(lambda: fanout.run_command(argv, root))
    except TimeoutError as exc:
        raise fanout.FanoutError(
            f"`new` timed out after {fanout.COMMAND_TIMEOUT_S:.0f}s"
        ) from exc
    except OSError as exc:
        raise fanout.FanoutError(f"`new` failed: {exc}") from exc
    if result.returncode != 0:
        raise fanout.FanoutError(fanout.failure_text(result))
    created = fanout.parse_created(result.stdout or "")
    if created is None:
        raise fanout.FanoutError(
            "no `Created <agent_id> on terminal <handle>` line "
            f"(exit code {result.returncode})"
        )
    agent_id, terminal = created
    return {"agent": agent_id, "terminal": terminal}


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def run_run(
    root: Path,
    definition: Definition,
    *,
    policy: str = "strict",
    verify: str = "suspect",
    max_attempts: int = 2,
    resume: bool = False,
    dispatch: Dispatch = default_dispatch,
    approved: bool = False,
    interval: float | None = None,
    run_id: str | None = None,
) -> dict:
    """Execute a definition until the run reaches a terminal state.

    ``run_id`` continues one run whose header already exists — that is how a
    detached scheduler picks up the run ``workflow go`` created before it was
    spawned, without guessing which run was meant. ``resume`` reopens the newest
    run of this definition that a dead scheduler left ``running``; its finished
    steps are reused by key. The loop owns the polling cadence (``interval``,
    default ``POLL_LOCAL_SECONDS``); ``tick`` does one pass and is what tests
    drive.
    """
    if definition.approval == "human" and not approved:
        raise WorkflowError(
            "approval_unavailable",
            f"workflow {definition.name!r} requires human approval; "
            "approve it, then run with --yes",
        )
    root = Path(root)
    if run_id:
        run = read_run(root, run_id)
        _reopen_stopped(root, run)
    else:
        run = _resume_target(root, definition) if resume else None
        if run is None:
            run = start_run(
                root, definition,
                policy=policy, verify=verify, max_attempts=max_attempts,
            )
        else:
            _reopen_stopped(root, run)
    pause = POLL_LOCAL_SECONDS if interval is None else max(0.0, float(interval))
    while True:
        status = tick(root, run["run_id"], dispatch=dispatch)
        if status != RUNNING:
            return read_run(root, run["run_id"])
        time.sleep(pause)


def start_run(
    root: Path,
    definition: Definition,
    *,
    policy: str = "strict",
    verify: str = "suspect",
    max_attempts: int = 2,
) -> dict:
    """Create the run header and return it, dispatching nothing.

    A detached scheduler needs the run id before it exists as a process, so the
    caller that spawns it creates the archive first and hands the id over.
    """
    run = archive.create_run(
        root, definition,
        policy=policy, verify=verify, max_attempts=max_attempts,
    )
    run["definition_sha"] = _definition_sha(definition)
    archive.write_run(root, run)
    return run


def _spawn_detached(argv: list[str], root: Path, sink: Any) -> subprocess.Popen:
    """Start a child that outlives this process, with no window of its own."""
    if os.name == "nt":
        flags = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
        return subprocess.Popen(
            argv, cwd=str(root), stdin=subprocess.DEVNULL,
            stdout=sink, stderr=subprocess.STDOUT, creationflags=flags,
        )
    return subprocess.Popen(
        argv, cwd=str(root), stdin=subprocess.DEVNULL,
        stdout=sink, stderr=subprocess.STDOUT, start_new_session=True,
    )


# ---------------------------------------------------------------------------
# Planning jobs
# ---------------------------------------------------------------------------

PLANNING_DIR = "planning"
PLANNING_STALE_SECONDS = 1800.0


def planning_dir(root: Path) -> Path:
    """Where a drafting job announces itself, beside ``runs/``."""
    return Path(root) / ".orca" / "osw" / PLANNING_DIR


def planning_files(root: Path, stem: str) -> tuple[Path, Path]:
    folder = planning_dir(root)
    return folder / f"{stem}.json", folder / f"{stem}.log"


def write_planning(
    root: Path,
    *,
    stem: str,
    pid: int,
    request: str,
    run_id: str = "",
    state_name: str = "planning",
    error: str = "",
) -> Path:
    """One marker per drafting job: who is working, and where it landed.

    A failure keeps its marker: the reason stays visible until someone reads it,
    instead of the job disappearing as if it had never been asked.
    """
    marker, _ = planning_files(root, stem)
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "stem": stem,
        "pid": int(pid),
        "state": state_name,
        "run_id": run_id,
        "request": request[:300],
        "error": error,
        "at": archive.now_iso(),
    }
    marker.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return marker


def clear_planning(root: Path, stem: str) -> None:
    marker, _ = planning_files(root, stem)
    try:
        marker.unlink()
    except FileNotFoundError:
        pass


def list_planning(root: Path) -> list[dict]:
    """Drafting jobs worth reporting, newest first.

    A marker whose job stopped updating and never recorded a run is stale: it is
    left off the report rather than shown as live work forever.
    """
    folder = planning_dir(root)
    if not folder.is_dir():
        return []
    found: list[dict] = []
    now = time.time()
    for marker in folder.glob("*.json"):
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        age = now - marker.stat().st_mtime
        if payload.get("state") == "planning" and age > PLANNING_STALE_SECONDS:
            continue
        payload["age_seconds"] = round(age)
        found.append(payload)
    found.sort(key=lambda item: str(item.get("at") or ""), reverse=True)
    return found


def spawn_planner(
    root: Path,
    *,
    stem: str,
    request: str,
    policy: str = "strict",
    verify: str = "suspect",
    max_attempts: int = 2,
) -> dict:
    """Draft, create and schedule in one detached child, then return at once.

    Drafting alone can take minutes, and a shell that blocks on it gets promoted
    into a window of its own. The child owns the whole sequence, so ``go`` is a
    short call that reports a job and exits; the run it creates is read from the
    archive like any other.
    """
    root = Path(root)
    marker, log_path = planning_files(root, stem)
    marker.parent.mkdir(parents=True, exist_ok=True)
    request_file = marker.with_suffix(".request.txt")
    request_file.write_text(request, encoding="utf-8")
    argv = [
        sys.executable, str(fanout.script_path()), "workflow", "go-worker",
        "--stem", stem, "--request-file", str(request_file),
        "--policy", policy, "--verify", verify,
        "--max-attempts", str(max_attempts),
    ]
    with open(log_path, "ab") as sink:
        child = _spawn_detached(argv, root, sink)
    # The marker goes down immediately: drafting takes minutes and `status` has
    # to say a job exists before it has a run to point at.
    write_planning(
        root, stem=stem, pid=child.pid, request=request, state_name="planning",
    )
    return {
        "pid": child.pid,
        "log": str(log_path),
        "marker": str(marker),
        "request_file": str(request_file),
    }


def _resume_target(root: Path, definition: Definition) -> dict | None:
    """The newest run of this definition a crash or a stop left unfinished.

    A run whose scheduler died stays ``running``; a run somebody stopped is
    ``stopped``. Both are continued by `--resume`: the stopped one is undone
    first, because stopping is a pause in OSW's vocabulary, not a burial.
    """
    for candidate in list_runs(root):
        if candidate.get("definition") != definition.name:
            continue
        if candidate.get("state") in (RUNNING, STOPPED):
            return candidate
    return None


def _reopen_stopped(root: Path, run: dict) -> None:
    """Undo a stop so the run can continue: skipped work becomes pending."""
    if run.get("state") != STOPPED:
        return
    run["state"] = RUNNING
    run.pop("stopped_at", None)
    run.pop("stop_reason", None)
    for entry in run.get("phases") or []:
        for step in archive.phase_steps(root, run, entry["id"]):
            if step.get("state") == SKIPPED and step.get("error") == "run stopped":
                step["state"] = PENDING
                step["error"] = ""
                step["finished_at"] = ""
                archive.write_step(root, run, step)
        if entry.get("state") == PHASE_SKIPPED:
            entry["state"] = PHASE_PENDING
    archive.write_run(root, run)


def tick(
    root: Path,
    run_id: str,
    *,
    dispatch: Dispatch = default_dispatch,
    now: str | None = None,
) -> str:
    """One scheduler pass; returns the run state after it.

    Everything is read from disk at the start, so a tick after a crash applies
    exactly the transitions the archive still implies.
    """
    root = Path(root)
    run = read_run(root, run_id)
    stamp = now or archive.now_iso()

    # A human verb is the only way back from a terminal run, so requests are
    # applied before the terminal check and may reopen it.
    applied = _apply_requests(root, run, stamp)
    if run.get("state") in _RUN_TERMINAL:
        if applied:
            archive.write_run(root, run)
        return str(run["state"])

    try:
        definition = load_definition(root, run["definition"])
        _join(root, run, definition, stamp)
        _judge(root, run, definition, stamp)
        if run.get("state") not in _RUN_TERMINAL:
            _dispatch(root, run, definition, dispatch, stamp)
            _judge(root, run, definition, stamp)
        _settle(root, run, stamp)
    except WorkflowError as exc:
        # A refusal is the run's answer, not the scheduler's death: the archive
        # says `failed` with the code and the message, so `status` shows why and
        # nothing is retried or guessed behind the scenes.
        _fail_run(run, stamp, f"{exc.code}: {exc.message}")
    except Exception as exc:  # noqa: BLE001 - a crash must not leave `running`
        _fail_run(run, stamp, f"{type(exc).__name__}: {exc}")
    archive.write_run(root, run)
    return str(run["state"])


# ---------------------------------------------------------------------------
# Human verbs
# ---------------------------------------------------------------------------

def _apply_requests(root: Path, run: dict, stamp: str) -> int:
    """Apply each consumed request once; the archive already deleted the file.

    Returns how many requests were applied, so a caller can tell a tick that
    only drained a queue on an already-finished run from a tick that resumed it.
    """
    applied = 0
    for request in archive.consume_requests(root, run["run_id"]):
        applied += 1
        verb = str(request.get("verb") or "")
        key = str(request.get("step") or "")
        reason = str(request.get("reason") or "")
        if verb == "stop":
            run["state"] = STOPPED
            run["stopped_at"] = stamp
            run["stop_reason"] = reason
            _stop_live_agents(root, run, stamp)
            _skip_unstarted(root, run, stamp)
            return applied
        entry, step = _find_step(root, run, key)
        if step is None:
            log.warning(
                "run %s: %s names step %s, which the archive does not hold",
                run["run_id"], verb, key,
            )
            continue
        _apply_verb(root, run, step, verb, reason, stamp)
        _reopen(run, entry, verb)
    return applied


def _reopen(run: dict, entry: dict | None, verb: str) -> None:
    """Let a human verb take a finished run back into flight.

    `accept` and `retry` both leave work the scheduler can still act on, so a
    failed phase goes back to pending and the run to `running`; the judge then
    re-derives both from the steps rather than trusting the old verdict.
    """
    if verb not in ("accept", "retry"):
        return
    if entry is not None and entry.get("state") in _PHASE_TERMINAL:
        entry["state"] = PHASE_PENDING
    if run.get("state") in _RUN_TERMINAL:
        run["state"] = RUNNING
        run["error"] = ""
        run["finished"] = ""
        run["finished_at"] = ""


def _stop_live_agents(root: Path, run: dict, stamp: str) -> None:
    """Ask every agent still running for this run to stop, and record it.

    A stopped run otherwise leaves its dispatched agents working: the run is
    terminal, nothing joins them again, and a later resume would dispatch the
    same step a second time. The watcher already honours the `.stop` sidecar
    (the same one ``osw stop`` writes), so the scheduler only drops it and marks
    the step `skipped` with the reason.
    """
    for entry in run.get("phases") or []:
        for step in archive.phase_steps(root, run, entry["id"]):
            if step.get("state") != ACTIVE:
                continue
            agent = str(step.get("agent") or "").strip()
            if agent:
                try:
                    state.write_stop_request(root, agent)
                except OSError as exc:
                    log.warning(
                        "run %s: could not stop agent %s: %s",
                        run["run_id"], agent, exc,
                    )
            step["state"] = SKIPPED
            step["error"] = "run stopped"
            step["finished_at"] = stamp
            archive.write_step(root, run, step)


def _skip_unstarted(root: Path, run: dict, stamp: str) -> None:
    """A stopped run leaves nothing pending: unstarted steps are recorded."""
    for entry in run.get("phases") or []:
        if entry.get("state") in _PHASE_TERMINAL:
            continue
        for step in archive.phase_steps(root, run, entry["id"]):
            if step["state"] != PENDING:
                continue
            step["state"] = SKIPPED
            step["error"] = "run stopped"
            step["finished_at"] = stamp
            archive.write_step(root, run, step)
        entry["state"] = PHASE_SKIPPED


def _find_step(
    root: Path, run: dict, key: str,
) -> tuple[dict | None, dict | None]:
    """(phase entry, step record) for one step key, or (None, None)."""
    for entry in run.get("phases") or []:
        if key in (entry.get("steps") or []):
            return entry, archive.read_step(root, run["run_id"], key)
    return None, None


def _apply_verb(
    root: Path, run: dict, step: dict, verb: str, reason: str, stamp: str,
) -> None:
    step["decision"] = {"verb": verb, "reason": reason, "at": stamp}
    step["verify_pending"] = False
    step["verify_agent"] = ""
    if verb == "accept":
        step["state"] = STEP_DONE
        step["error"] = ""
        if reason:
            step["verdict_reason"] = reason
    elif verb == "reject":
        step["state"] = STEP_FAILED
        step["error"] = reason or "rejected by a person"
        step["verdict"] = ""
    elif verb == "retry":
        # One more attempt regardless of the attempt cap; consumed at dispatch.
        step["state"] = PENDING
        step["error"] = ""
        step["retry_granted"] = True
        step["retry_reason"] = reason
    step["finished_at"] = stamp
    archive.write_step(root, run, step)


# ---------------------------------------------------------------------------
# join: copy a finished agent's outcome into its step
# ---------------------------------------------------------------------------

def _join(root: Path, run: dict, definition: Definition, stamp: str) -> None:
    for entry in run.get("phases") or []:
        for step in archive.phase_steps(root, run, entry["id"]):
            if step["state"] not in (PENDING, ACTIVE):
                continue
            if step.get("verify_pending"):
                continue  # opened, not yet handed to a verifier
            if step.get("verify_agent"):
                _join_verifier(root, run, step, stamp)
                continue
            if step["state"] != ACTIVE:
                continue
            attempts = step.get("attempts") or []
            if not attempts or attempts[-1].get("ended_at"):
                continue  # this attempt is already adjudicated
            _join_candidate(root, run, step, stamp)


def _join_candidate(root: Path, run: dict, step: dict, stamp: str) -> None:
    agent = str(step.get("agent") or "")
    if not agent:
        # Dispatched without a recorded agent: the archive cannot say what ran.
        _settle_attempt(root, run, step, "error", "no agent recorded", stamp)
        return
    record = _agent_record(root, agent)
    if record is None:
        _settle_attempt(root, run, step, "lost", "agent record missing", stamp)
        return
    state_name = str(record.get("state") or "")
    if state_name not in AGENT_TERMINAL:
        return  # still working; the watcher is the only writer that decides
    terminal = AGENT_TERMINAL[state_name]
    reason = str(record.get("completion_source") or terminal)
    handoff = str(record.get("handoff_path") or "")
    report = _agent_report(root, record)
    step["handoff"] = handoff
    step["warning"] = _warning(report)
    step["error"] = str(report.get("error") or "")
    if terminal == "done" and _artifact_path(root, handoff) is None:
        # The watcher finalized `done` without a handoff file: nothing was
        # produced, so this attempt did not finish the step.
        terminal, reason = "error", "handoff_missing"
    _settle_attempt(root, run, step, terminal, reason, stamp)


def _agent_record(root: Path, agent: str) -> dict | None:
    try:
        return state.read_agent(root, agent)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("agent record %s unreadable: %s", agent, exc)
        return None


def _agent_report(root: Path, record: Mapping[str, Any]) -> dict:
    path = str(record.get("report_file") or "")
    if not path:
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _warning(report: Mapping[str, Any]) -> str:
    if report.get("prompt_truncated_at"):
        return "prompt_truncated"
    if report.get("tools_observed") is False:
        return "no_tool_activity"
    return ""


def _settle_attempt(
    root: Path, run: dict, step: dict, terminal: str, reason: str, stamp: str,
) -> None:
    """Close the attempt and apply the ladder: retry, verify, or fail."""
    attempts = step.setdefault("attempts", [])
    if attempts:
        attempt = attempts[-1]
        attempt["terminal"] = terminal
        if reason:
            attempt["reason"] = reason
        attempt["ended_at"] = stamp
    if terminal == "done":
        step["error"] = ""
        if _needs_verification(run, step):
            _begin_verification(root, run, step)
        else:
            step["state"] = STEP_DONE
            step["finished_at"] = stamp
        archive.write_step(root, run, step)
        return
    step["error"] = reason or terminal
    granted = bool(step.pop("retry_granted", False))
    if len(attempts) < int(run.get("max_attempts") or 1) or granted:
        step["state"] = PENDING
        archive.write_step(root, run, step)
        return
    step["state"] = STEP_FAILED
    step["finished_at"] = stamp
    archive.write_step(root, run, step)


# ---------------------------------------------------------------------------
# Verification channel
# ---------------------------------------------------------------------------

def _needs_verification(run: Mapping[str, Any], step: Mapping[str, Any]) -> bool:
    mode = str(run.get("verify") or "suspect")
    if mode == "all":
        return True
    if mode == "suspect":
        return step.get("warning") in SUSPECT_WARNINGS
    return False


def _begin_verification(root: Path, run: dict, step: dict) -> None:
    """Open the channel: hash the artifact, then ask for a blind verifier."""
    artifact = _artifact_path(root, step.get("handoff"))
    if artifact is None:
        step["state"] = UNVERIFIABLE
        step["error"] = "no artifact to verify"
        step["finished_at"] = archive.now_iso()
        return
    step["artifact"] = str(step.get("handoff") or "")
    step["artifact_sha256"] = _hash_path(artifact)
    step["verdict"] = ""
    step["verify_attempts"] = 0
    step["verify_agent"] = ""
    step["verify_pending"] = True


def _join_verifier(root: Path, run: dict, step: dict, stamp: str) -> None:
    record = _agent_record(root, str(step.get("verify_agent") or ""))
    if record is None:
        _finish_verification(root, run, step, stamp, "verifier record missing")
        return
    state_name = str(record.get("state") or "")
    if state_name not in AGENT_TERMINAL:
        return
    reason = ""
    if state_name != "done":
        reason = str(record.get("completion_source") or state_name)
    if step.get("verifications"):
        entry = step["verifications"][-1]
        entry["terminal"] = state_name
        entry["reason"] = reason
        entry["ended_at"] = stamp
    _finish_verification(root, run, step, stamp, reason)


def _finish_verification(
    root: Path, run: dict, step: dict, stamp: str, failure: str,
) -> None:
    step["verify_agent"] = ""
    before = str(step.get("artifact_sha256") or "")
    after = _hash_path(_artifact_path(root, step.get("artifact")))
    if before and before != after:
        step["state"] = MUTATED
        step["verdict"] = ""
        step["error"] = "artifact changed during verification; verdict discarded"
        step["finished_at"] = stamp
        step["verify_pending"] = False
        archive.write_step(root, run, step)
        return
    verdict = _verdict_for(root, run, step["step_key"])
    if verdict is None or failure:
        if int(step.get("verify_attempts") or 0) < 1:
            step["verify_attempts"] = int(step.get("verify_attempts") or 0) + 1
            step["verify_pending"] = True
            step["state"] = ACTIVE
        else:
            step["state"] = UNVERIFIABLE
            step["error"] = failure or "no parseable verdict"
            step["finished_at"] = stamp
            step["verify_pending"] = False
        archive.write_step(root, run, step)
        return
    step["verdict"] = str(verdict["verdict"])
    step["verdict_reason"] = str(verdict.get("reason") or "")
    step["state"] = STEP_DONE
    step["error"] = ""
    step["finished_at"] = stamp
    step["verify_pending"] = False
    archive.write_step(root, run, step)


def _verdict_for(root: Path, run: dict, key: str) -> dict | None:
    """The newest parseable verdict line for one step, or ``None``."""
    found: dict | None = None
    for line in archive.read_verdicts(root, run["run_id"]):
        if str(line.get("step") or "") != key:
            continue
        if str(line.get("verdict") or "") not in VERDICTS:
            continue
        found = line
    return found


def _verifier_prompt(root: Path, run: dict, step: dict) -> str:
    """Blind: the artifact and the claim, never the author's self-assessment."""
    ledger = archive.verdicts_path(root, run["run_id"])
    key = step["step_key"]
    return (
        "Verification task. Check one claim against one file, then append "
        "exactly one JSON line.\n\n"
        f"ARTIFACT: {step.get('artifact') or ''}\n"
        f"CLAIM: {step.get('prompt') or ''}\n\n"
        "Read the artifact. Do not modify any file. Do not run `osw`. Then "
        f"append exactly one line to {ledger} in this shape:\n"
        f'{{"step": "{key}", "verdict": "confirmed|refuted|unsure", '
        '"reason": "<one line>", "artifact_sha256": "<sha256 of the artifact>"}\n'
        "Do not add any other output to that file.\n"
    )


def _hash_path(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _artifact_path(root: Path, value: Any) -> Path | None:
    """The local file a recorded handoff path names, or ``None``."""
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text)
    if not path.is_absolute():
        path = Path(root) / path
    return path if path.is_file() else None


# ---------------------------------------------------------------------------
# judge: phase policy and barriers
# ---------------------------------------------------------------------------

def _judge(root: Path, run: dict, definition: Definition, stamp: str) -> None:
    if run.get("state") in _RUN_TERMINAL:
        return
    for entry, phase in zip(run.get("phases") or [], definition.phases):
        if entry.get("state") in _PHASE_TERMINAL:
            if entry.get("state") == PHASE_FAILED and run.get("policy") == "strict":
                _fail_run(run, stamp, f"phase {entry['id']!r} failed")
                return
            continue
        if not _barrier_clear(run, phase):
            return
        records = archive.phase_steps(root, run, entry["id"])
        if entry.get("state") == PHASE_PENDING and not records:
            continue  # dispatch expands it; the judge only reads what exists
        if run.get("policy") == "strict":
            # One failed step fails the phase now: waiting for its siblings
            # would keep dispatching work the operator already lost.
            broken = [
                item for item in records
                if item["state"] in (STEP_FAILED, SKIPPED)
            ]
            if broken:
                entry["state"] = PHASE_FAILED
                _fail_run(
                    run, stamp,
                    f"phase {entry['id']!r} failed: {broken[0]['step_key']}",
                )
                return
        if not records or not all(item["state"] in SETTLED_STATES for item in records):
            continue
        if any(item["state"] in HOLD_STATES for item in records):
            # A person's verb is the only way forward; the run stays open.
            continue
        gaps = [item for item in records if item["state"] != STEP_DONE]
        entry["state"] = PHASE_PARTIAL if gaps else PHASE_DONE


def _barrier_clear(run: Mapping[str, Any], phase: Phase) -> bool:
    for source in phase.from_ids:
        entry = archive.phase_entry(dict(run), source)
        if entry is None or entry.get("state") not in _PHASE_TERMINAL:
            return False
    return True


def _fail_run(run: dict, stamp: str, reason: str) -> None:
    run["state"] = FAILED
    run["error"] = reason
    run["finished"] = stamp
    run["finished_at"] = stamp


def _settle(root: Path, run: dict, stamp: str) -> None:
    """Derive the run state from its phases once no further pass can help."""
    if run.get("state") in _RUN_TERMINAL:
        return
    entries = run.get("phases") or []
    if _held(root, run):
        # A verification gap waits for a person: it is neither progress nor
        # failure, and calling it either would hide the decision.
        run["state"] = BLOCKED
        return
    if all(entry.get("state") == PHASE_DONE for entry in entries):
        run["state"] = DONE
    elif all(entry.get("state") in _PHASE_TERMINAL for entry in entries):
        if any(entry.get("state") == PHASE_PARTIAL for entry in entries):
            run["state"] = PARTIAL
        else:
            run["state"] = DONE
    else:
        run["state"] = RUNNING
        return
    run["finished"] = stamp
    run["finished_at"] = stamp


def _held(root: Path, run: Mapping[str, Any]) -> bool:
    """True when a settled verification gap is the only thing left."""
    held = False
    for entry in run.get("phases") or []:
        for step in archive.phase_steps(root, run, entry["id"]):
            if step["state"] in HOLD_STATES:
                held = True
            elif step["state"] in (ACTIVE, PENDING):
                return False
    return held


# ---------------------------------------------------------------------------
# dispatch: expand a phase, then create one agent per pending step
# ---------------------------------------------------------------------------

def _dispatch(
    root: Path, run: dict, definition: Definition, dispatch: Dispatch, stamp: str,
) -> None:
    if run.get("state") in _RUN_TERMINAL:
        return
    entry, phase = _active_phase(run, definition)
    if entry is None or phase is None:
        return
    if not _barrier_clear(run, phase):
        return
    if entry.get("state") == PHASE_PENDING:
        entry["state"] = PHASE_RUNNING
    if not entry.get("steps") or run.get("definition_sha") != _definition_sha(definition):
        _expand(root, run, definition, entry, phase, stamp)
    for step in archive.phase_steps(root, run, entry["id"]):
        if step["state"] not in (PENDING, ACTIVE):
            continue
        if step.get("verify_pending"):
            _dispatch_verifier(root, run, step, dispatch, definition, phase, stamp)
        elif step["state"] == PENDING:
            if dispatch is default_dispatch:
                _stagger_before_start()
            _dispatch_step(root, run, step, dispatch, definition, phase, stamp)
        else:
            continue
        if run.get("state") in _RUN_TERMINAL:
            return


def _stagger_before_start() -> None:
    """Space agent starts out instead of booting a whole phase at once.

    A provider harness opens shared state at startup (pi's context store is one
    SQLite file for every session), and several of them booting inside the same
    second collide with `database is locked` — the harness then dies before its
    first turn, which is the worst possible moment. Discrete starts cost seconds
    and remove that collision. ``OSW_WORKFLOW_STAGGER_SECONDS=0`` disables it.
    """
    try:
        seconds = float(os.environ.get("OSW_WORKFLOW_STAGGER_SECONDS", ""))
    except ValueError:
        seconds = DISPATCH_STAGGER_SECONDS
    if seconds > 0:
        time.sleep(seconds)


def _active_phase(
    run: Mapping[str, Any], definition: Definition,
) -> tuple[dict | None, Phase | None]:
    for entry, phase in zip(run.get("phases") or [], definition.phases):
        if entry.get("state") in _PHASE_TERMINAL:
            continue
        return entry, phase
    return None, None


def _dispatch_step(
    root: Path, run: dict, step: dict, dispatch: Dispatch,
    definition: Definition, phase: Phase, stamp: str,
) -> None:
    plan = {
        "kind": "step",
        "phase": phase.id,
        "step": step["step_key"],
        "index": int(step.get("index") or 0),
        "prompt": step.get("prompt") or "",
        "schema": step.get("schema_id") or "",
    }
    _start_attempt(root, run, step, plan, dispatch, definition, stamp)


def _dispatch_verifier(
    root: Path, run: dict, step: dict, dispatch: Dispatch,
    definition: Definition, phase: Phase, stamp: str,
) -> None:
    plan = {
        "kind": "verify",
        "phase": phase.id,
        "step": step["step_key"],
        "index": int(step.get("index") or 0),
        "prompt": _verifier_prompt(root, run, step),
        "artifact": step.get("artifact") or "",
        "schema": step.get("schema_id") or "",
    }
    _start_attempt(root, run, step, plan, dispatch, definition, stamp, verify=True)


def _start_attempt(
    root: Path, run: dict, step: dict, plan: Mapping[str, Any], dispatch: Dispatch,
    definition: Definition, stamp: str, *, verify: bool = False,
) -> None:
    retry_reason = ""
    if not verify:
        retry_reason = str(step.pop("retry_reason", "") or "")
    try:
        outcome = dispatch(plan, definition, root)
    except (fanout.FanoutError, WorkflowError, OSError) as exc:
        reason = f"dispatch failed: {exc}"
        if verify:
            _note_verification(step, "", stamp, "error", reason)
            _verifier_failed(root, run, step, stamp, reason)
        else:
            _note_attempt(step, "", stamp, "error", reason, close=True)
            _settle_attempt(root, run, step, "error", reason, stamp)
        return
    agent = str(outcome.get("agent") or "")
    if not agent:
        reason = "dispatch returned no agent"
        if verify:
            _note_verification(step, "", stamp, "error", reason)
            _verifier_failed(root, run, step, stamp, reason)
        else:
            _note_attempt(step, "", stamp, "error", reason, close=True)
            _settle_attempt(root, run, step, "error", reason, stamp)
        return
    terminal = str(outcome.get("terminal") or "")
    if verify:
        _note_verification(step, agent, stamp, "", "")
        step["verify_agent"] = agent
        step["verify_pending"] = False
    else:
        _note_attempt(step, agent, stamp, "", retry_reason)
        step["agent"] = agent
        step["terminal"] = terminal
        step.pop("retry_granted", None)
    step["state"] = ACTIVE
    archive.write_step(root, run, step)


def _note_attempt(
    step: dict, agent: str, stamp: str, terminal: str, reason: str,
    *, close: bool = False,
) -> None:
    step.setdefault("attempts", []).append({
        "n": len(step.get("attempts") or []) + 1,
        "agent": agent,
        "reason": reason,
        "started_at": stamp,
        "ended_at": stamp if close else "",
    })


def _note_verification(
    step: dict, agent: str, stamp: str, terminal: str, reason: str,
) -> None:
    """Verifier runs get their own ledger: they are not the step's attempts."""
    step.setdefault("verifications", []).append({
        "n": len(step.get("verifications") or []) + 1,
        "agent": agent,
        "terminal": terminal,
        "reason": reason,
        "started_at": stamp,
        "ended_at": stamp if terminal else "",
    })


def _verifier_failed(
    root: Path, run: dict, step: dict, stamp: str, reason: str,
) -> None:
    step["verify_agent"] = ""
    if int(step.get("verify_attempts") or 0) < 1:
        step["verify_attempts"] = int(step.get("verify_attempts") or 0) + 1
        step["verify_pending"] = True
        step["error"] = reason
        archive.write_step(root, run, step)
        return
    step["state"] = UNVERIFIABLE
    step["error"] = reason
    step["finished_at"] = stamp
    step["verify_pending"] = False
    archive.write_step(root, run, step)


def _definition_sha(definition: Definition) -> str:
    """A stable id for the definition as written, for resume reconciliation.

    The file bytes when they are readable; otherwise the canonical phases, so
    a definition that was moved still compares equal to itself.
    """
    try:
        return hashlib.sha256(definition.path.read_bytes()).hexdigest()
    except OSError:
        payload = json.dumps(
            {
                "name": definition.name,
                "max_agents": definition.max_agents,
                "approval": definition.approval,
                "phases": [
                    {
                        "id": phase.id,
                        "mode": phase.mode,
                        "from": list(phase.from_ids),
                        "task": phase.task,
                        "schema": phase.schema,
                    }
                    for phase in definition.phases
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Expansion: the previous phase's structured output
# ---------------------------------------------------------------------------

def _expand(
    root: Path, run: dict, definition: Definition,
    entry: dict, phase: Phase, stamp: str,
) -> None:
    """Create — or reconcile — this phase's steps.

    Keys are a pure function of the definition and the upstream handoffs, both
    immutable once the phase started, so a resumed run recomputes the same keys
    and reuses every record it finds. A changed prompt yields a new key that
    touches nothing else (no cascade); a key that disappeared leaves its record
    on disk as history but leaves the phase.
    """
    if phase.mode == "single":
        prompts = [_render(phase, None, archive.output_path(root, run["run_id"], phase.id, 0))]
    else:
        items = _upstream_items(root, run, definition, phase)
        entry["expanded"] = len(items)
        prompts = [
            _render(phase, item, archive.output_path(root, run["run_id"], phase.id, index))
            for index, item in enumerate(items)
        ]

    existing: dict[str, dict] = {}
    for key in entry.get("steps") or []:
        record = archive.read_step(root, run["run_id"], key)
        if record is not None:
            existing[key] = record
    records = [
        _step_record(phase, index, prompt, existing)
        for index, prompt in enumerate(prompts)
    ]
    for index, record in enumerate(records):
        # The output file is named in the prompt, so the record has to carry it
        # too: `for_each` reads it without re-deriving anything from the text.
        # `_render` has already substituted the path, so the phase task is what
        # says whether this step has one.
        if "{{output}}" in phase.task:
            record["output"] = str(
                archive.output_path(root, run["run_id"], phase.id, index)
            )
    added = [record for record in records if record["step_key"] not in existing]
    prospective = _used_agents(root, run) + len(added)
    if prospective > int(run.get("max_agents") or 0):
        run["state"] = FAILED
        run["error"] = "too_many_agents"
        run["finished"] = run["finished_at"] = stamp
        archive.write_run(root, run)
        raise WorkflowError(
            "too_many_agents",
            f"phase {phase.id!r} expands to {len(records)} steps; the run would "
            f"use {prospective} agents and max_agents is {run['max_agents']}",
        )
    entry["steps"] = [record["step_key"] for record in records]
    run["definition_sha"] = _definition_sha(definition)
    for record in added:
        archive.write_step(root, run, record)
    if not added:
        archive.write_run(root, run)


def _used_agents(root: Path, run: dict) -> int:
    """Agents this run has already committed to: started or pending."""
    used = 0
    for entry in run.get("phases") or []:
        for key in entry.get("steps") or []:
            step = archive.read_step(root, run["run_id"], key)
            if step is None:
                continue
            attempts = len(step.get("attempts") or [])
            used += attempts + len(step.get("verifications") or [])
            if step.get("state") != STEP_DONE and attempts == 0:
                used += 1  # a created step still needs its first agent
    return used


def _step_record(
    phase: Phase, index: int, prompt: str, existing: Mapping[str, dict],
) -> dict:
    """The record for one expanded step: the archive's when the key matches."""
    schema = archive.schema_token(phase.schema)
    key = archive.step_key(phase.id, index, prompt, schema)
    if key in existing:
        record = existing[key]
        if record.get("index") != index:
            record["index"] = index
        return record
    return {
        "step_key": key,
        "phase": phase.id,
        "index": index,
        "prompt": prompt,
        "prompt_sha": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "schema_id": schema,
        "state": PENDING,
        "attempts": [],
        "agent": "",
        "terminal": "",
        "handoff": "",
        "output": "",
        "verdict": "",
        "error": "",
        "warning": "",
    }


def _upstream_items(
    root: Path, run: dict, definition: Definition, phase: Phase,
) -> list[Any]:
    items: list[Any] = []
    for source_id in phase.from_ids:
        source = _phase_by_id(definition, source_id)
        for record in archive.phase_steps(root, run, source_id):
            if record.get("state") != STEP_DONE:
                continue
            path = _artifact_path(root, record.get("handoff"))
            if path is None and not str(record.get("output") or "").strip():
                continue
            items.extend(_items_from(record, path, source, phase))
    return items


def _phase_by_id(definition: Definition, phase_id: str) -> Phase:
    for phase in definition.phases:
        if phase.id == phase_id:
            return phase
    raise WorkflowError(
        "bad_phase_ref",
        f"phase {phase_id!r} is not part of definition {definition.name!r}",
    )


def _items_from(
    record: Mapping[str, Any], path: Path | None, source: Phase, phase: Phase,
) -> list[Any]:
    """The array a producer named, from its output file or a fenced JSON block.

    `{{output}}` is the primary channel: the step writes JSON to the file the
    scheduler named, and the handoff keeps its four-section shape. A producer
    that predates the output file (or wrote one that is missing) still works if
    its handoff carries a fenced ```json block.
    """
    output = str(record.get("output") or "").strip()
    if output:
        written = Path(output)
        if written.is_file():
            try:
                payload = json.loads(written.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise WorkflowError(
                    "no_items", f"{written}: output is not valid JSON: {exc}",
                ) from exc
            return _array_value(payload, written, source, phase)
        raise WorkflowError(
            "no_items",
            f"phase {phase.id!r} needs the output file {written} "
            f"(named by phase {source.id!r}); the step finished without writing it",
        )
    if path is None:
        raise WorkflowError(
            "no_items",
            f"phase {source.id!r} produced neither an output file nor a handoff",
        )
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise WorkflowError("no_items", f"cannot read {path}: {exc}") from exc
    match = JSON_FENCE_RE.search(text)
    if match is None:
        raise WorkflowError(
            "no_items",
            f"phase {phase.id!r} needs a fenced ```json block in {path} "
            f"(produced by phase {source.id!r}); none found",
        )
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise WorkflowError("no_items", f"{path}: json block is not valid JSON: {exc}") from exc
    return _array_value(payload, path, source, phase)


def _array_value(
    payload: Any, path: Path, source: Phase, phase: Phase,
) -> list[Any]:
    """Pick the array the producer wrote, refusing guesses.

    A top-level array is the most direct answer to "list these", so it is taken
    as the item list itself; an object still has to name its array field.
    """
    if isinstance(payload, list):
        return list(payload)
    if not isinstance(payload, dict):
        raise WorkflowError(
            "no_items", f"{path}: expected a JSON object or an array",
        )
    field = _array_field(source)
    if field is not None:
        value = payload.get(field)
        if isinstance(value, list):
            return list(value)
        raise WorkflowError(
            "no_items",
            f"phase {phase.id!r} expects the array field {field!r} in {path}; "
            f"phase {source.id!r} produced {sorted(payload)}",
        )
    for fallback in ("findings", "items"):
        value = payload.get(fallback)
        if isinstance(value, list):
            return list(value)
    raise WorkflowError(
        "no_items",
        f"phase {phase.id!r} expects an array field "
        f"{sorted(_declared_array_fields(source)) or ['findings', 'items']} "
        f"in {path}; phase {source.id!r} produced {sorted(payload)}",
    )


def _declared_array_fields(phase: Phase) -> list[str]:
    schema = phase.schema
    if not isinstance(schema, dict):
        return []
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return []
    return [
        name for name, spec in properties.items()
        if isinstance(spec, dict) and spec.get("type") == "array"
    ]


def _array_field(phase: Phase) -> str | None:
    """The single array field this phase's schema declares, if it declares one."""
    fields = _declared_array_fields(phase)
    return fields[0] if fields else None


def _render(phase: Phase, item: Any, output: Path | None = None) -> str:
    """Substitute ``{{item}}``, ``{{item.<field>}}`` and ``{{output}}``.

    Anything else is refused: a prompt the scheduler cannot expand is a
    definition bug, not something to pass through to an agent.
    """
    def replace(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        if name == "output":
            if output is None:
                raise WorkflowError(
                    "definition_invalid",
                    f"phase {phase.id!r} uses {match.group(0)} but the run has "
                    f"no output file to name",
                )
            return str(output)
        if name == "item":
            if item is None:
                raise WorkflowError(
                    "no_items",
                    f"phase {phase.id!r} is 'single' but its task uses "
                    f"{match.group(0)}",
                )
            return _text(item)
        if name.startswith("item."):
            field = name[len("item."):]
            if isinstance(item, dict) and field in item:
                return _text(item[field])
            raise WorkflowError(
                "no_items",
                f"phase {phase.id!r}: the item has no field {field!r}",
            )
        raise WorkflowError(
            "definition_invalid",
            f"phase {phase.id!r}: {match.group(0)} is not resolvable; only "
            "{{item}} and {{item.<field>}} are",
        )

    return PLACEHOLDER_RE.sub(replace, phase.task)


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or value is None:
        return json.dumps(value)
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
