"""The ``osw workflow`` verbs: run a definition, inspect runs, decide a step.

``status``, ``ls`` and ``show`` read ``run.json`` archives; ``run --dry-run``
expands a definition and prints the plan without creating a run directory or
dispatching anything. ``run`` executes through ``workflow.executor``, which is
the only writer of run state.

The human verbs — ``accept``, ``reject``, ``retry`` — never touch run state.
They write one request side file under the run's ``requests/`` directory and
return; the scheduler consumes it on its next tick. ``stop`` works the same
way. Nothing here kills a process, and nothing here decides what a verdict
means.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import click
import typer

from osw.mailbox import identity
from osw.mailbox.errors import MailboxError
from osw.state import pid_is_running, resolve_project_root
from osw.workflow import board, defs
from osw.workflow.defs import (
    POLL_LOCAL_SECONDS,
    Definition,
    PlannedPhase,
    WorkflowError,
    expand,
    has_runtime_fanout,
    load_definition,
    static_agent_total,
)

POLICIES = ("strict", "partial")
VERIFY_MODES = ("none", "suspect", "all")

#: A run in one of these states does not move without a person or ``next``.
TERMINAL_STATES = ("done", "failed", "partial", "blocked", "stopped")
#: How long ``workflow wait`` blocks before reporting the run still running.
WAIT_DEFAULT_TIMEOUT = 900.0
#: What ``workflow next`` hands the planner: enough to continue, never a book.
NEXT_REQUEST_LIMIT = 4000

workflow_app = typer.Typer(
    help="Workflows: run a definition, inspect runs, decide a step",
    no_args_is_help=True,
)


@contextmanager
def _errors() -> Iterator[None]:
    """Turn a typed workflow or identity failure into one line and exit 1."""
    try:
        yield
    except (WorkflowError, MailboxError) as exc:
        typer.echo(f"Error: {exc.code}: {exc.message}")
        raise typer.Exit(1)


def _echo_json(value: Any) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _newest(root: Path) -> dict | None:
    """The newest run, or ``None`` when nothing has been scheduled yet."""
    runs = board.list_runs(root)
    return runs[0] if runs else None


def _pid(value: Any) -> int:
    """A recorded pid, or 0 when the marker holds something that is not one."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _planning_jobs(jobs: list[dict]) -> list[dict]:
    """Drafting jobs with the liveness that ``status`` reports.

    A marker still saying ``planning`` under a pid that is gone is a planner
    that died: it is reported as such and left on disk, because a deleted
    marker is a fact nobody can act on. Markers an older build wrote carry no
    ``alive``, so it is computed here; the executor's own answer always wins.
    """
    enriched: list[dict] = []
    for job in jobs:
        job = dict(job)
        alive = job.get("alive")
        if not isinstance(alive, bool):
            alive = job.get("state") == "planning" and pid_is_running(_pid(job.get("pid")))
        job["alive"] = alive
        if not alive and job.get("state") == "planning" and not job.get("code"):
            job["code"] = "planner_died"
        enriched.append(job)
    return enriched


def _planning_line(job: dict) -> str:
    """One line about a drafting job: what it is, and why it stopped."""
    where = f"run {job['run_id']}" if job.get("run_id") else str(job.get("state") or "")
    if job.get("error"):
        extra = f"  {job['error']}"
    elif job.get("alive") is False and job.get("code"):
        extra = f"  plan_failed: {job['code']}"
    else:
        extra = ""
    return f"planning {job.get('stem')}  pid {job.get('pid')}  {where}{extra}"


# ---------------------------------------------------------------------------
# Detached schedulers: `resume` and `run --detach` hand a run to a child
# ---------------------------------------------------------------------------

def _spawn_detached(argv: list[str], root: Path, sink: Any) -> Any:
    """One detached child with no window of its own: the executor's spawner.

    The indirection keeps the exact argv a run starts visible to a test without
    launching a process.
    """
    from osw.workflow import executor

    return executor._spawn_detached(argv, root, sink)


def _scheduler_log(root: Path, run_id: str) -> Path:
    """Where a detached scheduler's own output goes, beside its run."""
    return board.runs_dir(root) / run_id / "scheduler.log"


def _scheduler_argv(definition: str, run_id: str, *flags: str) -> list[str]:
    """The argv of a detached scheduler for one run that already exists.

    It carries ``--run`` and ``--detach`` together, and in that combination the
    named run is driven in this process: that is what stops a resumed run from
    spawning a second scheduler of itself. ``flags`` carry the decisions the
    caller already made, so the child does not fall back to a default.
    """
    from osw import fanout

    return [
        sys.executable, str(fanout.script_path()), "workflow", "run",
        definition, "--run", run_id, "--detach", *flags,
    ]


def _detach_target(root: Path, definition: Definition) -> dict | None:
    """The run a detached ``--resume`` continues: the same choice as in-process.

    The scheduler owns that choice, so it is asked rather than re-derived here.
    A live scheduler on the run is refused: two schedulers on one archive would
    both write it.
    """
    from osw.workflow import archive, executor

    run = executor._resume_target(root, definition)
    if run is None:
        return None
    if archive.scheduler_alive(root, run):
        raise WorkflowError(
            "run_running",
            f"run {run.get('run_id')!r} still has a live scheduler; stop it first",
        )
    return run


def _spawn_scheduler(root: Path, run: dict, *flags: str) -> Any:
    """Hand one existing run to a detached scheduler and return the child."""
    run_id = str(run.get("run_id") or "")
    definition = str(run.get("definition") or "")
    if not definition:
        raise WorkflowError(
            "run_not_found", f"run {run_id!r} records no definition to load",
        )
    log_path = _scheduler_log(root, run_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as sink:
        return _spawn_detached(
            _scheduler_argv(definition, run_id, *flags), root, sink,
        )


def _record_revive_lost(root: Path, run: dict) -> None:
    """Carry the person's revive decision on the run, not on a child's argv.

    The supervisor starts its scheduler with no flags, and a later ``resume``
    starts another scheduler the same way: the run is the only thing every one
    of those processes sees.
    """
    from osw.workflow import archive

    if run.get("revive_lost") is False:
        return
    run["revive_lost"] = False
    archive.write_run(root, run)


def _spawn_supervisor(root: Path, run: dict, *, no_revive_lost: bool = False) -> Any:
    """Hand one existing run to a detached supervisor and return the child."""
    from osw.workflow import executor

    if no_revive_lost:
        _record_revive_lost(root, run)
    return executor.spawn_supervisor(root, str(run.get("run_id") or ""))


# ---------------------------------------------------------------------------
# Guards: nothing is truncated, nothing is inferred
# ---------------------------------------------------------------------------

def _cap_guard(definition: Definition, planned: list[PlannedPhase]) -> None:
    """Refuse a run whose declared agents already exceed ``max_agents``.

    Only ``single`` phases have a count before the run starts; a ``for_each``
    width is the run's to discover, and the executor refuses that one at
    dispatch time with the same code.
    """
    total = 0
    for phase in planned:
        if phase.tasks is None:
            continue
        total += phase.tasks
        if total > definition.max_agents:
            raise WorkflowError(
                "too_many_agents",
                f"phase {phase.id!r} brings the run to {total} agents, above "
                f"max_agents {definition.max_agents}; nothing was dispatched",
            )


def _require_human(root: Path, verb: str) -> None:
    """A person decides; the Router writes a Decision and hands it on."""
    identity.require_human_identity(root, command=f"workflow {verb}")


def _request_line(verb: str, run_id: str, step_key: str, path: Path) -> str:
    where = f"{run_id} {step_key}".strip()
    return f"{verb} {where} -> {path}"


def _write_request(
    root: Path, run_id: str, step_key: str, verb: str, reason: str,
) -> tuple[Path, str]:
    """One request side file, one line. State stays the scheduler's to write."""
    from osw.workflow import archive

    run = board.read_run(root, run_id)
    if step_key and archive.read_step(root, run_id, step_key) is None:
        raise WorkflowError(
            "step_not_found", f"run {run_id!r} has no step {step_key!r}",
        )
    path = archive.write_request(root, run_id, verb, step_key, reason)
    return path, _request_line(verb, str(run.get("run_id") or run_id), step_key, path)


@workflow_app.command("ls")
def ls(
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON"),
) -> None:
    """List run archives, newest first."""
    root = resolve_project_root()
    with _errors():
        runs = board.list_runs(root)
    if json_output:
        _echo_json(runs)
        return
    typer.echo(board.render_ls(runs))


@workflow_app.command("show")
def show(
    run_id: str = typer.Argument(..., metavar="RUN"),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON"),
) -> None:
    """Show one run: its definition, phases and every step."""
    root = resolve_project_root()
    with _errors():
        run = board.read_run(root, run_id)
    if json_output:
        _echo_json(run)
        return
    typer.echo(board.render_show(run))


@workflow_app.command("status")
def status(
    run_id: str = typer.Argument(None, metavar="[RUN]"),
    watch: bool = typer.Option(
        False, "--watch", help=f"Re-render every {POLL_LOCAL_SECONDS:g}s until Ctrl-C",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON"),
) -> None:
    """Per-phase progress of one run (default: the newest), plus live drafts."""
    root = resolve_project_root()
    from osw.workflow import executor

    def render() -> None:
        jobs = _planning_jobs(executor.list_planning(root))
        with _errors():
            run = board.read_run(root, run_id) if run_id else _newest(root)
        if json_output:
            if run is None:
                _echo_json({"planning": jobs})
                return
            payload = board.render_status_json(run)
            payload["planning"] = jobs
            _echo_json(payload)
            return
        if run is None:
            if not jobs:
                typer.echo("No runs.")
                raise typer.Exit(1)
            for job in jobs:
                typer.echo(_planning_line(job))
            return
        typer.echo(board.render_status(run))
        for job in jobs:
            typer.echo(_planning_line(job))

    if watch:
        # Deliberately simple: no screen clearing, no diffing. Ctrl-C is the
        # exit path and is a success, not a failure.
        try:
            while True:
                render()
                typer.echo("")
                time.sleep(POLL_LOCAL_SECONDS)
        except KeyboardInterrupt:
            raise typer.Exit(0)
        return
    render()


@workflow_app.command("plan")
def plan(
    request: str = typer.Argument(..., metavar="TEXT"),
    name: str = typer.Option(
        "", "--name", help="Definition file stem (default: taken from the request)",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON"),
) -> None:
    """Turn one sentence into a definition, then show the plan it expands to.

    One ordinary agent writes the file against the closed schema; the validator
    decides whether it is usable. Nothing is dispatched and no run is created —
    the next step is still ``run``, and ``--dry-run`` on it costs nothing.
    """
    root = resolve_project_root()
    from osw.workflow import author

    with _errors():
        path, definition = author.author(root, request, name=name)
    planned = expand(definition)
    static_total = static_agent_total(planned)
    runtime_fanout = has_runtime_fanout(planned)

    if json_output:
        _echo_json({
            "definition": definition.name,
            "path": str(path),
            "max_agents": definition.max_agents,
            "phases": [
                {
                    "id": phase.id,
                    "mode": phase.mode,
                    "from": list(phase.from_ids),
                    "tasks": phase.tasks,
                }
                for phase in planned
            ],
            "agents": f"{static_total} + N" if runtime_fanout else str(static_total),
        })
        return

    typer.echo(f"WROTE {path}")
    typer.echo(board.render_dry_run(
        definition.name,
        planned,
        definition_path=definition.path,
        max_agents=definition.max_agents,
        static_total=static_total,
        runtime_fanout=runtime_fanout,
    ))


@workflow_app.command("go")
def go(
    request: str = typer.Argument(..., metavar="TEXT"),
    name: str = typer.Option(
        "", "--name", help="Definition file stem (default: taken from the request)",
    ),
    policy: str = typer.Option(
        "strict", "--policy", help="A failed step stops the phase (strict) or "
        "leaves a recorded gap (partial)",
        click_type=click.Choice(POLICIES),
    ),
    verify: str = typer.Option(
        "suspect", "--verify", help="Verify nothing, only suspect steps, or all",
        click_type=click.Choice(VERIFY_MODES),
    ),
    max_attempts: int = typer.Option(
        2, "--max-attempts", min=1, help="Attempts per step before it fails",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON"),
) -> None:
    """Plan and start a run from one sentence, then return.

    Drafting takes minutes, so ``go`` does not stay for it: one detached child
    drafts the definition, validates it, creates the run and schedules it, while
    this command reports the job and exits. A long foreground command would be
    promoted into a window of its own, and the run has to outlive this call
    anyway. Progress comes from ``workflow status``; a drafting failure keeps its
    reason there instead of vanishing.
    """
    root = resolve_project_root()
    from osw.workflow import author, executor

    with _errors():
        stem = author.stem_for(request, name)
        spawned = executor.spawn_planner(
            root, stem=stem, request=request,
            policy=policy, verify=verify, max_attempts=max_attempts,
        )
    if json_output:
        _echo_json({"job": stem, **spawned})
        return
    typer.echo(f"PLANNING {stem}  pid {spawned['pid']}")
    typer.echo(f"LOG {spawned['log']}")
    typer.echo("NEXT python osw.py workflow status")


@workflow_app.command("go-worker", hidden=True)
def go_worker(
    stem: str = typer.Option(..., "--stem"),
    request_file: str = typer.Option(..., "--request-file"),
    policy: str = typer.Option(
        "strict", "--policy",
        click_type=click.Choice(POLICIES),
    ),
    verify: str = typer.Option(
        "suspect", "--verify",
        click_type=click.Choice(VERIFY_MODES),
    ),
    max_attempts: int = typer.Option(
        2, "--max-attempts", min=1, help="Attempts per step before it fails",
    ),
) -> None:
    """Draft, create and hand one run to a supervisor: the body of a detached ``go``.

    The run is created here and handed to a supervisor, which starts the
    scheduler and restarts it while the run is still going. A refusal is
    recorded on the job marker and the process stops there. It does not retry
    with another provider, another model, or a hand-written config: failure
    stays where it happened, with the code that caused it.
    """
    root = resolve_project_root()
    from osw.workflow import author, executor

    request = Path(request_file).read_text(encoding="utf-8")
    landed = ""
    supervisor_pid = 0
    try:
        path, definition = author.author(root, request, name=stem)
        _cap_guard(definition, expand(definition))
        run = executor.start_run(
            root, definition,
            policy=policy, verify=verify, max_attempts=max_attempts,
        )
        landed = str(run["run_id"])
        executor.write_planning(
            root, stem=stem, pid=os.getpid(), request=request, run_id=landed,
        )
        # The supervisor owns the scheduler, not this process: drafting exits
        # here and the run keeps going if its scheduler dies and comes back.
        supervisor_pid = executor.spawn_supervisor(root, landed).pid
    except (WorkflowError, MailboxError) as exc:
        executor.write_planning(
            root, stem=stem, pid=os.getpid(), request=request,
            run_id=landed, state_name="failed",
            error=f"{exc.code}: {exc.message}",
        )
        typer.echo(f"Error: {exc.code}: {exc.message}")
        raise typer.Exit(1)
    except Exception as exc:  # noqa: BLE001 - the marker must carry the reason
        executor.write_planning(
            root, stem=stem, pid=os.getpid(), request=request,
            run_id=landed, state_name="failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    executor.clear_planning(root, stem)
    try:
        Path(request_file).unlink()
    except OSError:
        pass
    typer.echo(f"DONE {stem} run {landed}")
    typer.echo(f"SUPERVISOR {landed} pid {supervisor_pid}")


@workflow_app.command("run")
def run(
    name: str = typer.Argument(..., metavar="NAME"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Expand the definition and print the plan",
    ),
    resume: bool = typer.Option(
        False, "--resume", help="Reuse finished steps by step key",
    ),
    policy: str = typer.Option(
        "strict", "--policy", help="A failed step stops the phase (strict) or "
        "leaves a recorded gap (partial)",
        click_type=click.Choice(POLICIES),
    ),
    verify: str = typer.Option(
        "suspect", "--verify", help="Verify nothing, only suspect steps, or all",
        click_type=click.Choice(VERIFY_MODES),
    ),
    max_attempts: int = typer.Option(
        2, "--max-attempts", min=1, help="Attempts per step before it fails",
    ),
    yes: bool = typer.Option(
        False, "--yes", help="Approve a run whose definition asks for human approval",
    ),
    attach: str = typer.Option(
        "", "--run", metavar="RUN_ID",
        help="Continue this existing run instead of creating one (used by `go`)",
    ),
    detach: bool = typer.Option(
        False, "--detach",
        help="Create the run here and let a detached child schedule it; a run "
        "named with --run is scheduled in this process",
    ),
    no_revive_lost: bool = typer.Option(
        False, "--no-revive-lost",
        help="Spend a fresh agent on a lost step instead of reviving its pane",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON"),
) -> None:
    """Start a workflow, or print its plan with --dry-run.

    A definition carrying ``approval: "human"`` refuses ``approval_unavailable``
    until ``--yes`` says the run is approved; the executor enforces that. A run
    whose known agents exceed the definition's ``max_agents`` is refused
    ``too_many_agents`` before anything is created, never truncated.

    ``--detach`` creates the run here and returns once a supervisor owns it, so
    the run outlives the shell that asked for it: the supervisor starts the
    scheduler and restarts it while the run is still going. A run named with
    ``--run`` is scheduled in this process instead — that is how ``resume``
    starts a scheduler without starting a second one.
    """
    if not dry_run:
        root = resolve_project_root()
        with _errors():
            definition = load_definition(root, name)
            _cap_guard(definition, expand(definition))
        from osw.workflow import executor

        attach_id = attach.strip()
        if detach and not attach_id:
            with _errors():
                target = _detach_target(root, definition) if resume else None
                run = target or executor.start_run(
                    root, definition,
                    policy=policy, verify=verify, max_attempts=max_attempts,
                )
                # The supervisor owns the scheduler: the run outlives this
                # shell, and a scheduler that dies under it is restarted.
                child = _spawn_supervisor(root, run, no_revive_lost=no_revive_lost)
            run_id = str(run.get("run_id") or "")
            log_path = executor.supervisor_log_path(root, run_id)
            if json_output:
                _echo_json({
                    "run": run_id,
                    "definition": definition.name,
                    "pid": child.pid,
                    "log": str(log_path),
                })
                return
            typer.echo(f"DETACHED {run_id} pid {child.pid}")
            typer.echo(f"LOG {log_path}")
            typer.echo("NEXT python osw.py workflow status")
            return

        # Only forward the revive decision when it was made here: its default
        # belongs to the scheduler, and `--detach`'s child starts without it.
        options: dict[str, Any] = {}
        if no_revive_lost:
            options["revive_lost"] = False
        with _errors():
            final = executor.run_run(
                root,
                definition,
                policy=policy,
                verify=verify,
                max_attempts=max_attempts,
                resume=resume,
                approved=bool(yes),
                run_id=attach_id or None,
                **options,
            )
        if json_output:
            _echo_json(final)
        else:
            typer.echo(board.render_status(final))
        return

    root = resolve_project_root()
    with _errors():
        definition = load_definition(root, name)
    planned = expand(definition)
    static_total = static_agent_total(planned)
    runtime_fanout = has_runtime_fanout(planned)

    if json_output:
        _echo_json({
            "dry_run": True,
            "definition": definition.name,
            "path": str(definition.path),
            "max_agents": definition.max_agents,
            "phases": [
                {
                    "id": phase.id,
                    "mode": phase.mode,
                    "from": list(phase.from_ids),
                    "tasks": phase.tasks,
                }
                for phase in planned
            ],
            "agents": f"{static_total} + N" if runtime_fanout else str(static_total),
        })
        return

    typer.echo(board.render_dry_run(
        definition.name,
        planned,
        definition_path=definition.path,
        max_agents=definition.max_agents,
        static_total=static_total,
        runtime_fanout=runtime_fanout,
    ))


@workflow_app.command("supervise-worker", hidden=True)
def supervise_worker(
    run_id: str = typer.Option(..., "--run", metavar="RUN_ID"),
) -> None:
    """Own one run's scheduler: the body of a detached supervisor.

    The scheduler runs as this process's child. A child that exits while the
    run is still ``running`` is started again, up to ``OSW_WORKFLOW_RESTARTS``
    (default 3) with a pause between attempts; a child that exits with the run
    terminal is not restarted at all, whatever its exit code was. The only file
    this process writes is ``supervisor.json``: the run stays the scheduler's.
    """
    root = resolve_project_root()
    from osw.workflow import executor

    with _errors():
        final = executor.supervise_run(root, run_id)
    typer.echo(
        f"SUPERVISOR {run_id} restarts {final['restarts']} "
        f"last_exit {final['last_exit']}"
    )


# ---------------------------------------------------------------------------
# Goal and recovery: a person sets the goal, a person restarts a dead scheduler
# ---------------------------------------------------------------------------

def _goal_rounds(run: dict) -> dict[str, int]:
    """The round each phase is on, by phase id."""
    rounds: dict[str, int] = {}
    for entry in run.get("phases") or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        value = entry.get("round")
        if not isinstance(value, int) or isinstance(value, bool):
            value = 1
        rounds[entry["id"]] = value
    return rounds


def _goal_proposals(run: dict) -> list[dict]:
    proposals = run.get("goal_proposals")
    return proposals if isinstance(proposals, list) else []


@workflow_app.command("goal")
def goal(
    run_id: str = typer.Argument(..., metavar="RUN"),
    set_text: str = typer.Option(
        "", "--set", metavar="TEXT", help="Accept this as the run's goal",
    ),
    reason: str = typer.Option("", "--reason", help="Why the goal changes"),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON"),
) -> None:
    """Show a run's goal and the changes proposed for it, or set a new one.

    A gate may only *propose* a goal change: ``next_items`` and the criteria it
    answers are the definition's, and the goal is the person's. Accepting one
    here appends it to the proposal list instead of overwriting the record, so
    the run keeps the sentence it was started with and the one it now follows.
    """
    root = resolve_project_root()
    from osw.workflow import archive

    text = set_text.strip()
    accepted = False
    with _errors():
        run = board.read_run(root, run_id)
        if text:
            _require_human(root, "goal")
            run["goal_proposals"] = [
                *_goal_proposals(run),
                {"round": 0, "phase": "human", "text": text, "accepted": True},
            ]
            run["goal"] = text
            archive.write_run(root, run)
            accepted = True
        payload = {
            "run_id": run.get("run_id"),
            "goal": str(run.get("goal") or ""),
            "proposals": _goal_proposals(run),
            "rounds": _goal_rounds(run),
        }
    if json_output:
        _echo_json(payload)
        return
    typer.echo(f"GOAL {payload['run_id']} {payload['goal'] or '(none)'}")
    if accepted:
        typer.echo("ACCEPTED round 0 human")
        if reason:
            typer.echo(f"REASON {reason}")
        return
    typer.echo(f"PROPOSALS {len(payload['proposals'])}")
    rounds = ", ".join(f"{phase}={count}" for phase, count in payload["rounds"].items())
    typer.echo(f"ROUNDS {rounds or '-'}")


@workflow_app.command("resume")
def resume(
    run_id: str = typer.Argument(..., metavar="RUN"),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON"),
) -> None:
    """Restart the scheduler of a run whose scheduler is gone.

    A live scheduler is refused ``run_running``: two schedulers on one archive
    would both write it. Liveness is the heartbeat the scheduler stamps, so a
    killed scheduler is not mistaken for a busy one. The archive is untouched
    here; the new scheduler reuses every finished step by key.
    """
    root = resolve_project_root()
    from osw.workflow import archive

    with _errors():
        run = board.read_run(root, run_id)
        if archive.scheduler_alive(root, run):
            raise WorkflowError(
                "run_running",
                f"run {run_id!r} still has a live scheduler; stop it first",
            )
        child = _spawn_scheduler(root, run)
    log_path = str(_scheduler_log(root, run_id))
    if json_output:
        _echo_json({
            "resumed": True,
            "run": run_id,
            "pid": child.pid,
            "log": log_path,
        })
        return
    typer.echo(f"RESUMED {run_id} pid {child.pid}")
    typer.echo(f"LOG {log_path}")


# ---------------------------------------------------------------------------
# Next and wait: the acting agent continues the work, no person in between
#
# A batch can end without meeting its goal, and a batch takes minutes. Both used
# to end in a command a person had to type; these two are the calls an agent
# makes instead — one to start the next batch towards a goal it can read, one to
# block on the run it just started.
# ---------------------------------------------------------------------------

def _last_gate_reason(run: dict) -> str:
    """The reason on the highest-round gate that spoke, or ``""``.

    A gate answers a written criterion; its reason is the closest thing the
    archive has to "what is still unmet". The latest round wins, because that
    is the round the run stopped at.
    """
    best_round = -1
    reason = ""
    for entry in run.get("phases") or []:
        gate = entry.get("gate") if isinstance(entry, dict) else None
        if not isinstance(gate, dict):
            continue
        text = str(gate.get("reason") or "").strip()
        if not text:
            continue
        round_no = gate.get("round")
        value = round_no if isinstance(round_no, int) and not isinstance(round_no, bool) else 0
        if value >= best_round:
            best_round, reason = value, text
    return reason


def _open_proposals(run: dict) -> list[dict]:
    """Goal changes nobody accepted: carried as context, never applied."""
    return [
        item for item in _goal_proposals(run)
        if isinstance(item, dict) and not item.get("accepted")
    ]


def _next_request(run: dict) -> str:
    """The request the next batch is drafted from: goal, verdict, proposals."""
    goal = str(run.get("goal") or "").strip()
    lines = [f"Continue until this is true: {goal}", ""]
    reason = _last_gate_reason(run)
    if reason:
        lines.append(f"The last gate said: {reason}")
    proposals = _open_proposals(run)
    if proposals:
        lines.append("")
        lines.append("Goal changes proposed along the way:")
        for item in proposals:
            lines.append(
                f"- round {item.get('round')} {item.get('phase')}: {item.get('text')}"
            )
    lines.extend([
        "",
        "Draft the next batch for this work. The previous batch ran to its end "
        "without meeting the goal, so plan what is still missing: write the "
        "goal, give every deliverable phase criteria that can be judged "
        "offline, and let the phase that does the bulk of the work repeat "
        "until those criteria pass.",
    ])
    return "\n".join(lines)[:NEXT_REQUEST_LIMIT]


def _next_stem(root: Path, definition: str, name: str) -> str:
    """The next definition's stem: the one asked for, else ``<definition>-r<N>``.

    ``N`` counts the batches already drafted for this definition: the first
    continuation is ``-r2``, the one after it ``-r3``. Both the project's
    definitions and the personal ones count, because either is loadable.
    """
    asked = name.strip()
    if asked:
        return asked
    pattern = re.compile(rf"^{re.escape(definition)}-r[0-9]+$")
    taken: set[str] = set()
    for folder in (defs.project_workflows_dir(root), defs.home_workflows_dir()):
        if not folder.is_dir():
            continue
        for path in folder.glob("*.json"):
            if pattern.match(path.stem):
                taken.add(path.stem)
    return f"{definition}-r{2 + len(taken)}"


@workflow_app.command("next")
def next_run(
    run_id: str = typer.Argument(..., metavar="RUN"),
    name: str = typer.Option(
        "", "--name", help="Definition stem for the next batch (default: -r2, -r3, …)",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON"),
) -> None:
    """Draft and start the next batch towards a finished run's goal.

    This is the command an acting agent calls on a run that ended without
    meeting its goal: the goal, the last gate's reason and the goal changes
    nobody accepted become the request, and the new batch is drafted and
    started like ``go`` does. A run that has not finished is refused
    ``run_running``, and one with nothing to continue towards ``no_goal``.
    """
    root = resolve_project_root()
    from osw.workflow import executor

    with _errors():
        run = board.read_run(root, run_id)
        state_name = str(run.get("state") or "")
        if state_name not in TERMINAL_STATES:
            raise WorkflowError(
                "run_running",
                f"run {run_id!r} is {state_name or 'not finished'}; "
                "wait for it to finish first",
            )
        goal = str(run.get("goal") or "").strip()
        if not goal:
            raise WorkflowError(
                "no_goal",
                f"run {run_id!r} records no goal; "
                "set one with `workflow goal --set`",
            )
        request = _next_request(run)
        stem = _next_stem(root, str(run.get("definition") or ""), name)
        spawned = executor.spawn_planner(root, stem=stem, request=request)
    if json_output:
        _echo_json({"job": stem, "request": request, **spawned})
        return
    typer.echo(f"PLANNING {stem}  pid {spawned['pid']}")
    typer.echo(f"LOG {spawned['log']}")
    typer.echo(f"NEXT python osw.py workflow wait {stem}")


def _planning_run_id(root: Path, stem: str) -> str:
    """The run a drafting job announced, or ``""`` while it still drafts."""
    from osw.workflow import executor

    marker, _ = executor.planning_files(root, stem)
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(payload.get("run_id") or "") if isinstance(payload, dict) else ""


def _newest_run_of(root: Path, definition: str) -> dict | None:
    """The newest run scheduled from that definition, or ``None``."""
    for run in board.list_runs(root):
        if str(run.get("definition") or "") == definition:
            return run
    return None


def _wait_target(root: Path, target: str) -> dict | None:
    """The run a wait target names: a run id, a definition, or a drafting job.

    A stem is a definition name, so the run it produced is found in the archive
    even after the drafting marker was cleared; while the job is still drafting
    — or before the run exists at all — its marker's ``run_id`` is the only
    thing that can name the run, so it is read as the last resort.
    """
    try:
        return board.read_run(root, target)
    except WorkflowError as exc:
        if exc.code != "run_not_found":
            raise
    found = _newest_run_of(root, target)
    if found is not None:
        return found
    run_id = _planning_run_id(root, target)
    if not run_id:
        return None
    try:
        return board.read_run(root, run_id)
    except WorkflowError:
        return None


def _wait_for_run(
    root: Path,
    target: str,
    timeout: float,
    *,
    poll: float = POLL_LOCAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[dict | None, bool]:
    """Poll the archive until the run is terminal or ``timeout`` runs out.

    Reads only: no request file, no ``run.json`` write, no Orca call. A stem is
    followed through the drafting job's marker, so an agent can wait on the
    name it just handed ``go`` before the run exists at all.
    """
    deadline = clock() + max(0.0, float(timeout))
    while True:
        run = _wait_target(root, target)
        if run is not None and str(run.get("state") or "") in TERMINAL_STATES:
            return run, False
        remaining = deadline - clock()
        if remaining <= 0:
            return run, True
        sleep(min(max(0.0, poll), remaining))


@workflow_app.command("wait")
def wait(
    target: str = typer.Argument(..., metavar="RUN|STEM"),
    timeout: float = typer.Option(
        WAIT_DEFAULT_TIMEOUT, "--timeout", min=0.0,
        help="Seconds to wait before reporting the run still running",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON"),
) -> None:
    """Block until a run is terminal, then print it.

    One ordinary foreground call an agent makes after ``go``: it takes no
    background job, holds no Orca call, and writes nothing at all. The target
    is a run id or a definition stem — a stem is followed through the archive
    and, while its drafting job has no run yet, through the job's marker. Exit
    2 means the wait expired and the run is still going, not that anything
    failed.
    """
    root = resolve_project_root()

    run, expired = _wait_for_run(root, target, timeout)
    if run is None or expired:
        if json_output:
            _echo_json({
                "target": target,
                "waiting": True,
                "state": str((run or {}).get("state") or ""),
                "run_id": str((run or {}).get("run_id") or ""),
            })
        else:
            typer.echo(f"still running: {target} (waited {timeout:g}s)")
        raise typer.Exit(2)
    if json_output:
        _echo_json(board.render_status_json(run))
        return
    typer.echo(board.render_status(run))


# ---------------------------------------------------------------------------
# Requests: the human surface. One side file, never run state.
# ---------------------------------------------------------------------------

def _request_verb(
    verb: str, run_id: str, step_key: str, reason: str, json_output: bool,
) -> None:
    """Write one request side file for one human verb and say so once."""
    root = resolve_project_root()
    with _errors():
        _require_human(root, verb)
        path, line = _write_request(root, run_id, step_key, verb, reason)
    if json_output:
        _echo_json({
            "run": run_id,
            "step": step_key,
            "verb": verb,
            "reason": reason,
            "request": str(path),
        })
        return
    typer.echo(line)


@workflow_app.command("stop")
def stop(
    run_id: str = typer.Argument(..., metavar="RUN"),
    reason: str = typer.Option("", "--reason", help="Why the run should stop"),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON"),
) -> None:
    """Ask the scheduler to stop a run: no new dispatch, running agents stopped.

    The CLI writes a request side file and returns; the scheduler consumes it
    on its next tick and finishes the run ``stopped``. No process is killed
    here.
    """
    _request_verb("stop", run_id, "", reason, json_output)


@workflow_app.command("accept")
def accept(
    run_id: str = typer.Argument(..., metavar="RUN"),
    step_key: str = typer.Argument(..., metavar="STEP"),
    reason: str = typer.Option(..., "--reason", help="Why this step's result stands"),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON"),
) -> None:
    """Accept one step. Human-only: the Router writes a Decision instead."""
    _request_verb("accept", run_id, step_key, reason, json_output)


@workflow_app.command("reject")
def reject(
    run_id: str = typer.Argument(..., metavar="RUN"),
    step_key: str = typer.Argument(..., metavar="STEP"),
    reason: str = typer.Option(..., "--reason", help="Why this step's result does not stand"),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON"),
) -> None:
    """Reject one step. Human-only: the Router writes a Decision instead."""
    _request_verb("reject", run_id, step_key, reason, json_output)


@workflow_app.command("retry")
def retry(
    run_id: str = typer.Argument(..., metavar="RUN"),
    step_key: str = typer.Argument(..., metavar="STEP"),
    reason: str = typer.Option(..., "--reason", help="Why this step runs again"),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON"),
) -> None:
    """Retry one step on a fresh agent, regardless of the attempt cap.

    Human-only: the Router writes a Decision instead.
    """
    _request_verb("retry", run_id, step_key, reason, json_output)
