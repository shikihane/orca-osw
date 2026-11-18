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
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import click
import typer

from osw.mailbox import identity
from osw.mailbox.errors import MailboxError
from osw.state import resolve_project_root
from osw.workflow import board
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


def _latest_or_exit(root: Path, run_id: str | None) -> dict:
    """The named run, or the newest one when ``status`` was called bare."""
    if run_id:
        return board.read_run(root, run_id)
    runs = board.list_runs(root)
    if not runs:
        typer.echo("No runs.")
        raise typer.Exit(1)
    return runs[0]


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
    """Per-phase progress of one run (default: the newest)."""
    root = resolve_project_root()

    if watch:
        # Deliberately simple: no screen clearing, no diffing. Ctrl-C is the
        # exit path and is a success, not a failure.
        try:
            while True:
                with _errors():
                    run = _latest_or_exit(root, run_id)
                typer.echo(board.render_status(run))
                typer.echo("")
                time.sleep(POLL_LOCAL_SECONDS)
        except KeyboardInterrupt:
            raise typer.Exit(0)
        return

    with _errors():
        run = _latest_or_exit(root, run_id)
    if json_output:
        _echo_json(board.render_status_json(run))
        return
    typer.echo(board.render_status(run))


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

    One agent drafts the definition, the validator judges it, the run is
    created, and its scheduler starts as its own process — a workflow outlives
    the shell or tool call that asked for it, so this command returns the run id
    instead of blocking. Progress comes from ``workflow status``; the scheduler
    writes its own output to the run's ``scheduler.log``.
    """
    root = resolve_project_root()
    from osw.workflow import author, executor

    with _errors():
        path, definition = author.author(root, request, name=name)
        _cap_guard(definition, expand(definition))
        run = executor.start_run(
            root, definition,
            policy=policy, verify=verify, max_attempts=max_attempts,
        )
        spawned = executor.spawn_scheduler(
            root, run, definition,
            policy=policy, verify=verify, max_attempts=max_attempts,
        )
    if json_output:
        _echo_json({
            "run_id": run["run_id"],
            "definition": definition.name,
            "path": str(path),
            "pid": spawned["pid"],
            "log": spawned["log"],
        })
        return
    typer.echo(f"STARTED {run['run_id']}  def {definition.name}  pid {spawned['pid']}")
    typer.echo(f"DEF {definition.name} {path}")
    typer.echo(f"LOG {spawned['log']}")
    typer.echo(f"NEXT python osw.py workflow status {run['run_id']}")


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
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON"),
) -> None:
    """Start a workflow, or print its plan with --dry-run.

    A definition carrying ``approval: "human"`` refuses ``approval_unavailable``
    until ``--yes`` says the run is approved; the executor enforces that. A run
    whose known agents exceed the definition's ``max_agents`` is refused
    ``too_many_agents`` before anything is created, never truncated.
    """
    if not dry_run:
        root = resolve_project_root()
        with _errors():
            definition = load_definition(root, name)
            _cap_guard(definition, expand(definition))
        from osw.workflow import executor

        with _errors():
            final = executor.run_run(
                root,
                definition,
                policy=policy,
                verify=verify,
                max_attempts=max_attempts,
                resume=resume,
                approved=bool(yes),
                run_id=attach.strip() or None,
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
