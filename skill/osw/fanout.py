"""One-command fan-out entry point: fill a worktree with managed workers.

``osw super`` is the fan-out front door for ``new``. It optionally starts
the mailbox control plane once, then dispatches one ``new`` per task — at
most ``max_parallel`` at a time — and reports the agent ids it created.

It owns no state of its own: every worker, record, watcher, report and
handoff is exactly what the equivalent ``new`` invocation produces. A
failure in one dispatch never aborts the rest; the command exits non-zero
when any dispatch failed.

Only stdlib and anyio are used here; every subprocess is an argv list, and
nothing is ever run through a shell.
"""

from __future__ import annotations

import functools
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import anyio

from osw.log import emit_event
from osw.state import logs_dir

DEFAULT_MAX_PARALLEL = 32
MAX_PARALLEL_CAP = 32
DEFAULT_PREFIX = "code"
COMMAND_TIMEOUT_S = 300.0

# The zero-flag defaults come from the project, in this order: the
# `super` object in `.orca/osw/config.json`, then these environment
# variables, then the AGENTS.md Provider Policy row for batch fan-out.
CONFIG_FILE = "config.json"
CONFIG_KEY = "super"
POLICY_FILE = "AGENTS.md"
POLICY_SECTION = "osw provider policy"
POLICY_ROW = "batch fan-out"
ENV_KEYS = {
    "provider": "OSW_PROVIDER",
    "model": "OSW_MODEL",
    "thinking": "OSW_THINKING",
    "prefix": "OSW_PREFIX",
    "max_parallel": "OSW_MAX_PARALLEL",
}
PROVIDER_HELP = (
    "pass --provider, or set super.provider in .orca/osw/config.json, "
    "OSW_PROVIDER, or the \"batch fan-out\" row of AGENTS.md"
)

# `new` prints exactly one line the fan-out needs:
#   Created code_001 on terminal term_xxx (provider: claude)
CREATED_RE = re.compile(r"^Created (\S+) on terminal (\S+)")

PLAN_KEYS = ("provider", "model", "thinking", "prefix", "max_parallel", "tasks")
TASK_KEYS = ("task", "prefix", "provider", "model", "thinking", "caller_terminal")
TEXT_DEFAULTS = ("provider", "model", "thinking", "prefix")

# Tolerant markdown-table shapes: `| a | b |` rows, `---`/`:-:` separators.
TABLE_ROW_RE = re.compile(r"^\s*\|(.*)\|\s*$")
TABLE_SEP_RE = re.compile(r"^:?-{2,}:?$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


class FanoutError(ValueError):
    """A refusal: the message is one readable line for the operator."""


@dataclass(frozen=True)
class Job:
    """One dispatch: a task plus the flags its `new` invocation carries."""

    index: int
    task: str
    provider: str
    prefix: str = ""
    model: str = ""
    thinking: str = ""
    caller_terminal: str = ""


@dataclass(frozen=True)
class CommandResult:
    """What one subprocess returned, normalised to text."""

    returncode: int
    stdout: str
    stderr: str


# ---------------------------------------------------------------------------
# The spawn layer (tests patch `run_command`)
# ---------------------------------------------------------------------------

def script_path() -> Path:
    """The `skill/osw.py` entry point beside this package."""
    return Path(__file__).resolve().parents[1] / "osw.py"


def _decode(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


async def run_command(
    argv: Sequence[str], cwd: Path, *, timeout_s: float = COMMAND_TIMEOUT_S,
) -> CommandResult:
    """Run one argv list with no shell, capturing both streams as text."""
    with anyio.fail_after(timeout_s):
        completed = await anyio.run_process(list(argv), cwd=str(cwd), check=False)
    return CommandResult(
        returncode=int(completed.returncode),
        stdout=_decode(completed.stdout),
        stderr=_decode(completed.stderr),
    )


# ---------------------------------------------------------------------------
# argv construction
# ---------------------------------------------------------------------------

def mailbox_argv() -> list[str]:
    return [sys.executable, str(script_path()), "mailbox", "start", "--json"]


def new_argv(
    job: Job, *, no_notify: bool = False, ticket: str = "",
) -> list[str]:
    """The exact `new` invocation for one task; empty flags are omitted."""
    argv = [sys.executable, str(script_path()), "new", job.provider, job.task]
    if job.prefix:
        argv += ["--prefix", job.prefix]
    if job.model:
        argv += ["--model", job.model]
    if job.thinking:
        argv += ["--thinking", job.thinking]
    if job.caller_terminal:
        argv += ["--caller-terminal", job.caller_terminal]
    elif no_notify:
        argv += ["--no-notify"]
    if ticket:
        argv += ["--ticket", ticket]
    return argv


# ---------------------------------------------------------------------------
# Plan and flag resolution
# ---------------------------------------------------------------------------

def load_plan(path: str | Path) -> dict:
    """Read a plan file: a JSON object with defaults and a tasks array."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise FanoutError(f"cannot read plan {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise FanoutError(f"plan {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise FanoutError(f"plan {path} must be a JSON object")
    unknown = sorted(set(data) - set(PLAN_KEYS))
    if unknown:
        raise FanoutError("plan has unknown keys: " + ", ".join(unknown))
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise FanoutError("plan needs a non-empty tasks array")
    return data


def _text(value: Any, what: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise FanoutError(f"{what} must be a string")
    return value.strip()


def _pick(flag: str | None, plan_value: Any, what: str) -> str:
    """Flag first (it is the operator's explicit choice), else the plan."""
    if flag is not None and flag.strip():
        return flag.strip()
    return _text(plan_value, what)


def _resolve_text(
    key: str, flag: str | None, plan_value: Any, project: Mapping[str, Any] | None,
) -> str:
    """Flag, then the plan, then the project's zero-flag defaults."""
    value = _pick(flag, plan_value, key)
    if value:
        return value
    fallback = (project or {}).get(key)
    if isinstance(fallback, str) and fallback.strip():
        return fallback.strip()
    return ""


def clamp_parallel(value: Any) -> int:
    """Clamp a parallel limit into 1..32; anything unreadable is refused."""
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise FanoutError(f"max_parallel must be an integer, got {value!r}") from exc
    return max(1, min(MAX_PARALLEL_CAP, number))


def _parallel_limit(
    flag: int | None, plan_value: Any, project: Mapping[str, Any] | None = None,
) -> int:
    if flag is not None:
        return clamp_parallel(flag)
    if plan_value is not None:
        return clamp_parallel(plan_value)
    fallback = (project or {}).get("max_parallel")
    if fallback is not None:
        return clamp_parallel(fallback)
    return DEFAULT_MAX_PARALLEL


# ---------------------------------------------------------------------------
# Zero-flag defaults from the project
# ---------------------------------------------------------------------------

def _cell_name(cell: str) -> str:
    """A header cell as a plain lowercase column name."""
    return cell.strip().strip("`*_ ").strip().lower()


def _cell_value(cell: str) -> str:
    """A table cell as text; placeholders such as `-` count as empty."""
    value = cell.strip().strip("`").strip("*").strip()
    if not value or TABLE_SEP_RE.match(value):
        return ""
    return value


def _policy_section(text: str) -> list[str]:
    """The lines of the `## OSW Provider Policy` section, or none."""
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        match = HEADING_RE.match(line)
        if not match:
            continue
        if start is None:
            if match.group(2).strip().lower() == POLICY_SECTION:
                start = index + 1
            continue
        if len(match.group(1)) <= 2:
            return lines[start:index]
    return lines[start:] if start is not None else []


def _policy_table_row(table: Sequence[Sequence[str]]) -> dict:
    """provider/model/thinking from the batch fan-out row of one table."""
    if len(table) < 2:
        return {}
    header = [_cell_name(cell) for cell in table[0]]
    columns: dict[str, int] = {}
    for name in ("provider", "model", "thinking"):
        if name not in header:
            return {}
        columns[name] = header.index(name)
    for row in table[1:]:
        if not row or POLICY_ROW not in row[0].lower():
            continue
        picked: dict[str, str] = {}
        for name, index in columns.items():
            cell = row[index] if index < len(row) else ""
            value = _cell_value(cell)
            if value:
                picked[name] = value
        return picked
    return {}


def policy_defaults(root: str | Path) -> dict:
    """The AGENTS.md Provider Policy row for batch fan-out, if any."""
    try:
        text = (Path(root) / POLICY_FILE).read_text(encoding="utf-8")
    except OSError:
        return {}
    tables: list[list[list[str]]] = []
    current: list[list[str]] = []
    for line in _policy_section(text):
        match = TABLE_ROW_RE.match(line)
        if not match:
            if current:
                tables.append(current)
                current = []
            continue
        cells = [cell.strip() for cell in match.group(1).split("|")]
        if cells and all(not c or TABLE_SEP_RE.match(c) for c in cells):
            continue
        current.append(cells)
    if current:
        tables.append(current)
    for table in tables:
        found = _policy_table_row(table)
        if found:
            return found
    return {}


def config_defaults(root: str | Path) -> dict:
    """The `super` object of `.orca/osw/config.json`; anything else is ignored."""
    path = Path(root) / ".orca" / "osw" / CONFIG_FILE
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    block = data.get(CONFIG_KEY)
    if not isinstance(block, dict):
        return {}
    picked: dict[str, Any] = {}
    for key in TEXT_DEFAULTS:
        value = block.get(key)
        if isinstance(value, str) and value.strip():
            picked[key] = value.strip()
    parallel = block.get("max_parallel")
    if isinstance(parallel, int) and not isinstance(parallel, bool):
        picked["max_parallel"] = parallel
    return picked


def env_defaults(environ: Mapping[str, str] | None = None) -> dict:
    """`OSW_*` environment values; empty or unreadable ones are ignored."""
    source = os.environ if environ is None else environ
    picked: dict[str, Any] = {}
    for key, name in ENV_KEYS.items():
        raw = (source.get(name) or "").strip()
        if not raw:
            continue
        if key == "max_parallel":
            try:
                picked[key] = int(raw)
            except ValueError:
                continue
        else:
            picked[key] = raw
    return picked


def project_defaults(root: str | Path) -> dict:
    """Zero-flag defaults: config.json, then the environment, then AGENTS.md."""
    merged: dict[str, Any] = {}
    merged.update(policy_defaults(root))
    merged.update(env_defaults())
    merged.update(config_defaults(root))
    return merged


def build_jobs(
    plan: dict | None,
    *,
    provider: str | None = None,
    model: str | None = None,
    thinking: str | None = None,
    prefix: str | None = None,
    caller_terminal: str | None = None,
    tasks: Sequence[str] = (),
    max_parallel: int | None = None,
    project: Mapping[str, Any] | None = None,
) -> tuple[list[Job], int]:
    """(jobs, parallel limit) from the flags, the plan and the project.

    A flag wins, the plan's top-level keys stand in for a flag that was not
    given, and the project's zero-flag defaults fill in the rest; a per-task
    object overrides all three for itself.
    """
    data = plan or {}
    if plan and tasks:
        raise FanoutError("pass tasks or --plan, not both")
    items: Sequence[Any] = list(data.get("tasks") or []) if plan else list(tasks)
    if not items:
        raise FanoutError(
            "no tasks: pass task text as arguments, --task <text> (repeatable), "
            "or --plan <file>"
        )

    defaults = {
        key: _resolve_text(key, flag_value, data.get(key), project)
        for key, flag_value in (
            ("provider", provider),
            ("model", model),
            ("thinking", thinking),
            ("prefix", prefix),
        )
    }
    defaults["prefix"] = defaults["prefix"] or DEFAULT_PREFIX
    defaults["caller_terminal"] = _pick(
        caller_terminal, data.get("caller_terminal"), "caller_terminal",
    )
    limit = _parallel_limit(max_parallel, data.get("max_parallel"), project)

    jobs: list[Job] = []
    for index, item in enumerate(items, start=1):
        if isinstance(item, str):
            fields = {"task": item.strip()}
        elif isinstance(item, dict):
            unknown = sorted(set(item) - set(TASK_KEYS))
            if unknown:
                raise FanoutError(
                    f"task {index} has unknown keys: " + ", ".join(unknown)
                )
            fields = {
                key: _text(item[key], f"task {index} {key}")
                for key in TASK_KEYS if key in item
            }
        else:
            raise FanoutError(f"task {index} must be a string or an object")
        merged = {**defaults, **fields}
        if not merged.get("task"):
            raise FanoutError(f"task {index} is empty")
        if not merged.get("provider"):
            raise FanoutError(f"task {index} has no provider; {PROVIDER_HELP}")
        jobs.append(Job(index=index, **merged))
    return jobs, limit


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def parse_created(stdout: str) -> tuple[str, str] | None:
    """(agent_id, terminal) from `new`'s `Created ...` line, or None."""
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line.startswith("Created "):
            continue
        match = CREATED_RE.match(line)
        if match:
            return match.group(1), match.group(2)
    return None


def failure_text(result: CommandResult) -> str:
    """The most specific non-empty line a failed command printed."""
    for text in (result.stderr, result.stdout):
        for line in reversed([part.strip() for part in (text or "").splitlines()]):
            if line:
                return line
    return f"exit code {result.returncode}"


async def _start_mailbox(root: Path) -> dict:
    """Start the daemon; the record to report, or a typed refusal."""
    try:
        result = await run_command(mailbox_argv(), root)
    except TimeoutError as exc:
        raise FanoutError(
            f"mailbox start did not finish within {COMMAND_TIMEOUT_S:.0f}s"
        ) from exc
    except OSError as exc:
        raise FanoutError(f"mailbox start failed: {exc}") from exc
    if result.returncode != 0:
        raise FanoutError(failure_text(result))
    try:
        data = json.loads(result.stdout or "")
    except ValueError as exc:
        raise FanoutError(f"mailbox start printed no JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise FanoutError("mailbox start printed no JSON object")
    try:
        pid = int(data.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    return {"running": bool(data.get("running")), "pid": pid}


def _batch_warning(root: Path, event: str, message: str, data: dict | None = None) -> None:
    """One WARNING event; a log that cannot be written never fails a dispatch."""
    try:
        emit_event(
            logs_dir(root),
            component="fanout",
            event=event,
            level="WARNING",
            message=message,
            data=data or {},
        )
    except OSError:
        pass


def batch_caller(jobs: Sequence[Job]) -> str:
    """The one caller handle every job shares, or "" when they disagree."""
    handles = {job.caller_terminal for job in jobs if job.caller_terminal}
    return next(iter(handles)) if len(handles) == 1 else ""


def batch_origin(jobs: Sequence[Job], pane_key: str = "") -> dict:
    """The Ticket origin a human fan-out of `jobs` stands for."""
    handle = batch_caller(jobs)
    origin = {
        "role": "human",
        "id": f"terminal:{handle}" if handle else "",
        "text": f"super: {len(jobs)} tasks",
    }
    if pane_key:
        origin["pane_key"] = pane_key
    return origin


async def caller_pane_key(handle: str) -> str:
    """The pane identity of the caller terminal, or "" when it cannot be read.

    The pane (`tabId:leafId`) outlives its runtime handle, so it is what the
    daemon rebinds the conclusion's delivery address to. A failed listing is
    not a fan-out failure: the origin simply carries no pane key.
    """
    if not handle:
        return ""
    from osw.orca_cli import terminal_list

    try:
        terminals = await terminal_list()
    except Exception:
        return ""
    for entry in terminals:
        if not isinstance(entry, dict):
            continue
        if handle not in (entry.get("handle"), entry.get("terminal"),
                          entry.get("paneKey")):
            continue
        key = str(entry.get("paneKey") or "")
        if not key:
            tab = str(entry.get("tabId") or "")
            leaf = str(entry.get("leafId") or "")
            key = f"{tab}:{leaf}" if tab and leaf else ""
        return key
    return ""


def _open_batch(root: Path, jobs: Sequence[Job], pane_key: str = "") -> str:
    """One batch Ticket for the whole fan-out; "" (direct mode) on any failure."""
    from osw import dispatcher
    from osw.mailbox.paths import db_path
    from osw.mailbox.store import Store

    try:
        store = Store(db_path(root))
        try:
            opened = dispatcher.open_batch(
                store,
                root=root,
                origin=batch_origin(jobs, pane_key),
                tasks=len(jobs),
            )
        finally:
            store.close()
    except Exception as exc:  # best effort: the dispatch must still run
        _batch_warning(
            root,
            "batch_ticket_failed",
            str(exc) or type(exc).__name__,
            {"tasks": len(jobs)},
        )
        return ""
    if isinstance(opened, dict):
        opened = opened.get("id") or opened.get("ticket_id") or ""
    return str(opened or "")


def _seal_batch(root: Path, ticket_id: str) -> None:
    """Seal the batch Ticket after every dispatch finished; best effort."""
    from osw import dispatcher
    from osw.mailbox.paths import db_path
    from osw.mailbox.store import Store

    try:
        store = Store(db_path(root))
        try:
            dispatcher.seal_batch(store, ticket_id)
        finally:
            store.close()
    except Exception as exc:  # best effort: dispatches already happened
        _batch_warning(
            root,
            "batch_seal_failed",
            str(exc) or type(exc).__name__,
            {"ticket": ticket_id},
        )


def _record_assignment(
    root: Path, ticket_id: str, agent_id: str, terminal: str,
) -> None:
    """Register one worker on the batch Ticket; a failure never stops it.

    `new --ticket` already records its own assignment, so this is only the
    fallback for a dispatch whose record the store did not keep; an agent
    that is already assigned is never added twice.
    """
    from osw.mailbox.paths import db_path
    from osw.mailbox.store import Store

    try:
        store = Store(db_path(root))
        try:
            entries = store.assignments(ticket_id)
            if any(
                isinstance(entry, dict) and entry.get("agent_id") == agent_id
                for entry in entries
            ):
                return
            store.add_assignment(
                ticket_id,
                {"agent_id": agent_id, "terminal": terminal, "state": "working"},
            )
        finally:
            store.close()
    except Exception as exc:  # best effort: the worker is already running
        _batch_warning(
            root,
            "batch_assignment_failed",
            str(exc) or type(exc).__name__,
            {"ticket": ticket_id, "agent_id": agent_id},
        )


async def _dispatch_one(
    job: Job, root: Path, no_notify: bool, ticket: str = "",
) -> dict:
    """One `new`; a failure is reported, never raised."""
    base = {"index": job.index, "task": job.task}
    try:
        result = await run_command(
            new_argv(job, no_notify=no_notify, ticket=ticket), root,
        )
    except TimeoutError:
        return {**base, "error": f"timed out after {COMMAND_TIMEOUT_S:.0f}s"}
    except OSError as exc:
        return {**base, "error": str(exc)}
    if result.returncode != 0:
        return {**base, "error": failure_text(result)}
    created = parse_created(result.stdout or "")
    if created is None:
        return {
            **base,
            "error": "no `Created <agent_id> on terminal <handle>` line "
                     + f"(exit code {result.returncode})",
        }
    agent_id, terminal = created
    if ticket:
        _record_assignment(root, ticket, agent_id, terminal)
    return {**base, "agent_id": agent_id, "terminal": terminal}


async def _dispatch_all(
    jobs: Sequence[Job], root: Path, *, limit: int, no_notify: bool,
    ticket: str = "",
) -> tuple[list[dict], list[dict]]:
    """Run every job with at most `limit` subprocesses in flight."""
    limiter = anyio.CapacityLimiter(limit)
    agents: list[dict] = []
    errors: list[dict] = []

    async def one(job: Job) -> None:
        async with limiter:
            outcome = await _dispatch_one(job, root, no_notify, ticket)
        if "error" in outcome:
            errors.append(outcome)
        else:
            agents.append(outcome)

    async with anyio.create_task_group() as group:
        for job in jobs:
            group.start_soon(one, job)
    agents.sort(key=lambda row: row["index"])
    errors.sort(key=lambda row: row["index"])
    return agents, errors


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def payload(
    mailbox: dict | None, requested: int, agents: list[dict], errors: list[dict],
) -> dict:
    return {
        "mailbox": mailbox,
        "requested": int(requested),
        "started": len(agents),
        "agents": agents,
        "errors": errors,
    }


def print_human(data: dict) -> None:
    mailbox = data["mailbox"]
    if mailbox is None:
        print("mailbox: off")
    else:
        print(f"mailbox: running pid={mailbox['pid']}")
    for agent in data["agents"]:
        print(f"[{agent['index']}] {agent['agent_id']} {agent['terminal']}  {agent['task']}")
    for error in data["errors"]:
        print(
            f"[{error['index']}] FAILED  {error['task']}: {error['error']}",
            file=sys.stderr,
        )
    print(
        f"super: requested={data['requested']} started={data['started']} "
        f"errors={len(data['errors'])}"
    )


def print_dry_run(
    jobs: Sequence[Job], *, mailbox: bool, no_notify: bool, json_output: bool,
) -> None:
    start = mailbox_argv() if mailbox else None
    dispatches = [new_argv(job, no_notify=no_notify) for job in jobs]
    if json_output:
        print(json.dumps(
            {"dry_run": True, "mailbox": start, "dispatches": dispatches},
            ensure_ascii=False,
        ))
        return
    if start is not None:
        print("mailbox start: " + json.dumps(start))
    for job, argv in zip(jobs, dispatches):
        print(f"task {job.index}: " + json.dumps(argv))


def _error(text: str) -> None:
    line = text.strip()
    if not line.startswith("Error:"):
        line = f"Error: {line}"
    print(line, file=sys.stderr)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _execute(
    root: Path, jobs: Sequence[Job], limit: int, *, mailbox: bool,
    no_notify: bool, json_output: bool,
) -> int:
    record = await _start_mailbox(root) if mailbox else None
    ticket = ""
    if record is not None:
        pane_key = await caller_pane_key(batch_caller(jobs))
        ticket = _open_batch(root, jobs, pane_key)
    agents, errors = await _dispatch_all(
        jobs, root, limit=limit, no_notify=no_notify, ticket=ticket,
    )
    if ticket:
        _seal_batch(root, ticket)
    data = payload(record, len(jobs), agents, errors)
    if json_output:
        print(json.dumps(data, ensure_ascii=False))
    else:
        print_human(data)
    return 1 if errors else 0


def run(
    *,
    root: str | Path,
    provider: str | None = None,
    model: str | None = None,
    thinking: str | None = None,
    prefix: str | None = None,
    caller_terminal: str | None = None,
    tasks: Sequence[str] = (),
    plan: str | Path | None = None,
    mailbox: bool = True,
    max_parallel: int | None = None,
    no_notify: bool = False,
    json_output: bool = False,
    dry_run: bool = False,
) -> int:
    """Dispatch the fan-out; returns the process exit code."""
    try:
        plan_data = load_plan(plan) if plan else None
        jobs, limit = build_jobs(
            plan_data,
            provider=provider,
            model=model,
            thinking=thinking,
            prefix=prefix,
            caller_terminal=caller_terminal,
            tasks=list(tasks or ()),
            max_parallel=max_parallel,
            project=project_defaults(root),
        )
    except FanoutError as exc:
        _error(str(exc))
        return 1
    if dry_run:
        print_dry_run(jobs, mailbox=mailbox, no_notify=no_notify,
                      json_output=json_output)
        return 0
    try:
        return anyio.run(functools.partial(
            _execute,
            Path(root),
            jobs,
            limit,
            mailbox=mailbox,
            no_notify=no_notify,
            json_output=json_output,
        ))
    except FanoutError as exc:
        # The mailbox could not start: nothing was dispatched.
        _error(str(exc))
        return 1
