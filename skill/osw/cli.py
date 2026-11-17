from __future__ import annotations

from collections import deque
from dataclasses import replace
import functools
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import anyio
import typer

from osw.log import (
    emit_event,
    enable_file_logging,
    events_file,
    format_event_line,
    get_logger,
    read_events,
    setup_logging,
)
from osw import hosts as host_seam
from osw import fanout as fanout_mod
from osw import reclaim
from osw.hosts import Host, HostError
from osw.orca_cli import (
    OrcaError,
    compatibility_report,
    detect_current_terminal,
    injected_session_address,
    is_session_address,
    orchestration_run_create,
    orchestration_run_current,
    orchestration_run_use,
    orchestration_worker_release,
    orchestration_worker_show,
    orchestration_worker_start,
    orchestration_worker_stop,
    terminal_close,
    terminal_create,
    terminal_interrupt,
    terminal_list,
    terminal_read,
    terminal_send,
    terminal_send_escape,
    terminal_show,
    worker_settlement,
    worker_start_receipt,
    worktree_ps,
)
from osw.mailbox import identity as mailbox_identity
from osw.mailbox.errors import MailboxError
from osw.mailbox_cli import mailbox_app
from osw.process import hidden_subprocess_kwargs
from osw.providers import (
    SUPPORTED_PROVIDERS,
    ProviderError,
    build_provider_command,
    ensure_workspace_trust,
    normalize_provider,
    probe_variants,
    resume_args,
    resume_command,
    scan_agent_clis,
    shell_command_line,
    supervised_agent_id,
    validate_supervised_args,
)
from osw.state import (
    alloc_agent_id,
    delete_agent,
    find_agent_by_pane,
    init_state_dir,
    list_agents,
    logs_dir,
    new_handoff_path,
    pid_is_running,
    read_agent,
    read_state,
    resolve_project_root,
    state_file,
    write_agent,
    write_state,
    write_stop_request,
)
from osw.jev import (
    api_key,
    ask_ready,
    base_url,
    check_endpoint,
    masked_key,
    proxy_set,
    resolve_enforce_providers,
    resolve_mode,
    sdk_status,
)
from osw.ready_gate import gate_verdict
from osw.sessions import session_for_agent
from osw.watcher import RESUME_TEMPLATE, main as watcher_main, one_line

log = get_logger("cli")

app = typer.Typer(help="OSW - Orca Agent Supervisor")

# Orca's worker-start waits for the agent pane to report ready and gives
# up after 60s by default — too tight for an omp cold start on a loaded
# machine (observed once: the TUI was parked at its composer, but the
# readiness rule never fired inside the budget). OSW asks for a longer
# budget; the launch still fails closed, loudly, if it expires.
SUPERVISED_START_TIMEOUT_MS = 240_000


@app.callback()
def main(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
) -> None:
    setup_logging(verbose=verbose)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Hosts: named machines OSW can dispatch to
# ---------------------------------------------------------------------------

host_app = typer.Typer(
    help="Machines OSW can dispatch to (SSH data plane, Orca dispatch plane)",
    no_args_is_help=True,
)
app.add_typer(host_app, name="host")
app.add_typer(mailbox_app, name="mailbox")


def _refuse_router_identity(root: Path, command: str) -> None:
    """A Router session writes Decisions; dispatch stays with the human.

    The Router role owns judgment, never execution (spec §9.2). The
    identity is the project role token, so a human in the same terminal
    keeps it.
    """
    try:
        mailbox_identity.require_human_identity(root, command=command)
    except MailboxError as exc:
        typer.echo(f"Error: {exc.code}: {exc.message}")
        raise typer.Exit(1)


def _host_or_exit(root: Path, on: str | None) -> Host:
    try:
        return host_seam.resolve_host(root, (on or "local").strip())
    except HostError as exc:
        typer.echo(f"Error: {exc.message}")
        raise typer.Exit(1)


def _agent_target(
    root: Path, reference: str, on: str | None = None,
) -> tuple[Host, str, dict]:
    """(host, agent id, record) for `<host>/<agent_id>` or a bare id.

    The record's own host wins when the reference carries no prefix, so
    a remote agent stays addressable after it was dispatched. An
    explicit `--on` names the host instead. A reference prefix — or an
    `--on` — that contradicts the record is refused rather than
    silently ignored.
    """
    try:
        host, agent_id = host_seam.resolve_agent_ref(root, reference)
    except HostError as exc:
        typer.echo(f"Error: {exc.message}")
        raise typer.Exit(1)
    try:
        agent = read_agent(root, agent_id)
    except (FileNotFoundError, ValueError):
        typer.echo(f"Error: agent '{reference}' not found")
        raise typer.Exit(1)
    recorded = str(agent.get("host") or host_seam.LOCAL_NAME)
    if not host.is_local and recorded != host.name:
        typer.echo(
            f"Error: agent '{agent_id}' belongs to host "
            f"'{recorded}', not '{host.name}'"
        )
        raise typer.Exit(1)
    if on:
        explicit = _host_or_exit(root, on)
        if explicit.name != recorded:
            typer.echo(
                f"Error: agent '{agent_id}' belongs to host "
                f"'{recorded}', not '{explicit.name}'"
            )
            raise typer.Exit(1)
        return explicit, agent_id, agent
    if recorded != host.name:
        try:
            host = host_seam.resolve_host(root, recorded)
        except HostError:
            pass
    return host, agent_id, agent


def _workspace_view(root: Path, host: Host) -> Host:
    """`host` as its workspace rules need it.

    A registered host already names its workspace. This machine's
    workspace is the repo the CLI runs in, which no registry record
    carries, so the local `path:` selector is bound here once instead of
    every call site comparing paths itself. The host is copied: the
    shared local singleton never learns a repo.
    """
    if host.is_local and not host.worktree:
        return replace(host, worktree=host.worktree_selector(str(root)))
    return host


def _run_async(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run one coroutine function, keyword arguments included.

    `anyio.run` forwards positional arguments only; the host seam's
    `host=` is keyword-only, so it needs a partial.
    """
    return anyio.run(functools.partial(fn, *args, **kwargs))


@host_app.command("add")
def host_add(
    name: str = typer.Argument(..., help="Short name later commands address"),
    ssh: str = typer.Option(..., "--ssh", help="SSH destination (alias or user@host)"),
    worktree: str = typer.Option(..., "--worktree", help="Orca worktree selector on that host, e.g. path:/srv/repo"),
    environment: str = typer.Option(None, "--environment", help="Orca paired-server selector (id or name); defaults to the host name"),
    root: str = typer.Option(None, "--root", help="Absolute workspace path on that host (path joins and trust)"),
) -> None:
    """Register a machine; no probe, no write on the far side."""
    project = resolve_project_root()
    enable_file_logging(logs_dir(project))
    try:
        existing = host_seam.read_hosts(project)
    except HostError as exc:
        typer.echo(f"Error: {exc.message}")
        raise typer.Exit(1)
    if name in existing:
        record = existing[name]
        typer.echo(
            f"Error: host '{name}' is already registered "
            f"(ssh {record.ssh or '-'}, worktree {record.worktree or '-'})"
        )
        raise typer.Exit(1)
    if name == host_seam.LOCAL_NAME:
        typer.echo("Error: 'local' is this machine and cannot be registered")
        raise typer.Exit(1)
    record = Host(
        name=name,
        ssh=ssh,
        environment=environment or name,
        worktree=worktree,
        remote_root=root or "",
    )
    host_seam.save_host(project, record)
    typer.echo(
        f"Registered host {name} (ssh {record.ssh}, "
        f"worktree {record.worktree})"
    )
    emit_event(
        logs_dir(project),
        component="cli",
        event="host_registered",
        message="host record written",
        data={
            "host": name,
            "ssh": record.ssh,
            "environment": record.environment,
            "worktree": record.worktree,
        },
    )


@host_app.command("list")
def host_list(
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON"),
) -> None:
    """Registered hosts with their current reachability."""
    project = resolve_project_root()
    enable_file_logging(logs_dir(project))
    try:
        registered = host_seam.read_hosts(project)
    except HostError as exc:
        typer.echo(f"Error: {exc.message}")
        raise typer.Exit(1)
    rows = []
    for name in sorted(registered):
        record = registered[name]
        rows.append({
            "name": name,
            "ssh": record.ssh,
            "environment": record.environment or name,
            "worktree": record.worktree,
            "root": record.remote_root,
            "reachability": host_seam.reachability(record),
        })
    if json_output:
        typer.echo(json.dumps({"hosts": rows}, indent=2, ensure_ascii=False))
        return
    if not rows:
        typer.echo("No hosts registered.")
        return
    typer.echo(
        f"{'HOST':<16} {'SSH':<24} {'ENVIRONMENT':<16} "
        f"{'WORKTREE':<40} REACHABILITY"
    )
    for row in rows:
        typer.echo(
            f"{row['name']:<16} {row['ssh']:<24} {row['environment']:<16} "
            f"{row['worktree']:<40} {row['reachability']}"
        )


@host_app.command("rm")
def host_rm(
    name: str = typer.Argument(..., help="Registered host name"),
) -> None:
    """Remove a host record and report the agents that referenced it."""
    project = resolve_project_root()
    enable_file_logging(logs_dir(project))
    try:
        removed = host_seam.remove_host(project, name)
    except HostError as exc:
        typer.echo(f"Error: {exc.message}")
        raise typer.Exit(1)
    if removed is None:
        typer.echo(f"Error: host '{name}' is not registered")
        raise typer.Exit(1)
    referencing = sorted(
        agent_id for agent_id, agent in list_agents(project).items()
        if str(agent.get("host") or "") == name
    )
    typer.echo(f"Removed host {name}")
    if referencing:
        typer.echo(
            "Agents still referencing it: " + ", ".join(referencing)
        )
    emit_event(
        logs_dir(project),
        component="cli",
        event="host_removed",
        message="host record removed",
        data={"host": name, "agents": referencing},
    )


@host_app.command("doctor")
def host_doctor(
    target: str = typer.Argument(..., help="Registered host name or SSH destination"),
    ssh: str = typer.Option(None, "--ssh", help="SSH destination for an unregistered host"),
    worktree: str = typer.Option(None, "--worktree", help="Orca worktree selector for an unregistered host"),
    environment: str = typer.Option(None, "--environment", help="Orca paired-server selector for an unregistered host"),
    provider: str = typer.Option("claude", "--provider", help="Provider CLI to require on that host"),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON"),
) -> None:
    """Check reachability, workspace, worktree and provider, in that order."""
    project = resolve_project_root()
    enable_file_logging(logs_dir(project))
    try:
        registered = host_seam.read_hosts(project)
    except HostError as exc:
        typer.echo(f"Error: {exc.message}")
        raise typer.Exit(1)
    if target in registered:
        host = registered[target]
        if worktree:
            host.worktree = worktree
        if environment:
            host.environment = environment
        if ssh:
            host.ssh = ssh
    else:
        if target == host_seam.LOCAL_NAME:
            host = host_seam.local_host()
        else:
            host = Host(
                name=target,
                ssh=ssh or target,
                environment=environment or target,
                worktree=worktree or "",
            )
    report = anyio.run(host_seam.doctor, project, host, provider)
    if json_output:
        typer.echo(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        for rung in report["rungs"]:
            if rung.get("ok"):
                detail = rung.get("path") or ""
                typer.echo(f"  ok    {rung['rung']}" + (f" ({detail})" if detail else ""))
            else:
                typer.echo(
                    f"  FAIL  {rung['rung']}: {rung.get('message', '')}"
                )
                if rung.get("next"):
                    typer.echo(f"        next: {rung['next']}")
    emit_event(
        logs_dir(project),
        component="cli",
        event="host_doctor",
        level="INFO" if report["ok"] else "ERROR",
        message="host doctor finished",
        data={
            "host": host.name,
            "provider": provider,
            "ok": report["ok"],
            "code": report.get("code", ""),
        },
    )
    if not report["ok"]:
        raise typer.Exit(1)


@app.command()
def doctor(
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON"),
) -> None:
    """Check the live Orca contract required by OSW."""
    try:
        report = anyio.run(compatibility_report)
    except OrcaError as exc:
        failure = {
            "ok": False,
            "error": {
                "code": exc.code or "orca_error",
                "message": exc.message,
            },
        }
        if json_output:
            typer.echo(json.dumps(failure, indent=2))
        else:
            typer.echo(
                f"OSW compatibility: FAILED "
                f"[{failure['error']['code']}] {exc.message}"
            )
        raise typer.Exit(1)

    if json_output:
        typer.echo(json.dumps(report, indent=2))
    else:
        state = "OK" if report.get("ok") else "NOT READY"
        typer.echo(f"OSW compatibility: {state}")
        typer.echo(f"Orca: {report['orca']['version'] or 'unknown'}")
        typer.echo(
            f"Runtime: {report['runtime']['state']} "
            f"(reachable={str(report['runtime']['reachable']).lower()})"
        )
        typer.echo(f"Graph: {report['graph']['state']}")
        typer.echo(f"Worktree: {report['worktree']['path']}")
        typer.echo(
            f"Contract: terminal_list={report['contract']['terminal_list']} "
            f"terminal_show={report['contract']['terminal_show']} "
            f"worktree_ps={report['contract']['worktree_ps']} "
            f"agent_status={report['contract']['agent_status']}"
        )
    if not report.get("ok"):
        raise typer.Exit(1)


def _read_state_or_exit(root: Path) -> dict:
    try:
        return read_state(root)
    except FileNotFoundError:
        typer.echo("Not initialized. Run `python osw.py init` first.")
        raise typer.Exit(1)


def _detect_caller_terminal() -> str | None:
    """Print a unique marker, then find which terminal's preview contains it."""
    marker = f"osw_trace_{uuid.uuid4().hex[:12]}"
    log.info("caller detect  trace=%s", marker)
    time.sleep(0.3)
    try:
        handle = anyio.run(detect_current_terminal, marker)
    except Exception:
        return None
    if handle:
        log.info("caller detected  terminal=%s", handle)
        return handle
    log.warning("caller detect failed, no terminal matched trace")
    return None


def _validate_env_caller() -> str | None:
    """Return ORCA_TERMINAL_HANDLE if Orca still accepts it."""
    return _check_env_caller()[0]


def _check_env_caller() -> tuple[str | None, dict | None]:
    """(ORCA_TERMINAL_HANDLE, its `terminal show`) if Orca still accepts it.

    The handle is inherited by every orca subcommand; once the hosting
    terminal is gone, Orca rejects it with ``terminal_handle_stale`` and
    every osw command fails. Drop it from the environment and fall back
    to marker detection instead of erroring out. The show result is None
    when Orca could not be asked.
    """
    env_handle = os.environ.get("ORCA_TERMINAL_HANDLE", "").strip()
    if not env_handle:
        return None, None
    try:
        data = anyio.run(terminal_show, env_handle)
    except OrcaError as exc:
        if exc.code != "terminal_handle_stale":
            # Orca unreachable or a transient failure: keep the handle and
            # let later commands surface the real error.
            return env_handle, None
        log.warning(
            "ORCA_TERMINAL_HANDLE=%s is stale, ignoring it", env_handle
        )
        typer.echo(
            f"WARNING: ORCA_TERMINAL_HANDLE ({env_handle}) is stale; "
            "ignoring it and falling back to caller detection.",
            err=True,
        )
        os.environ.pop("ORCA_TERMINAL_HANDLE", None)
        return None, None
    log.info("caller from ORCA_TERMINAL_HANDLE  terminal=%s", env_handle)
    return env_handle, data


def _executor_identity(caller_terminal: str | None) -> tuple[str | None, str]:
    """(handle or session address, pane_key) of this command's caller.

    Callers compare panes, not handle strings: the pane key is the
    stable identity, a handle only names it within one Orca runtime.
    `--caller-terminal` overrides ORCA_TERMINAL_HANDLE; a stale env
    handle falls back to caller detection like `_maybe_detect_caller`.
    A structured chat session has no terminal and no pane: its identity
    is the injected `orca_session_id:<id>` address, returned with an
    empty pane key.
    """
    data = None
    if caller_terminal:
        handle: str | None = caller_terminal
    else:
        handle, data = _check_env_caller()
        if not handle:
            handle = injected_session_address() or None
        if not handle and sys.stdin.isatty() and sys.stdout.isatty():
            handle = _detect_caller_terminal()
    if not handle:
        return None, ""
    if is_session_address(handle):
        return handle, ""
    if data is None:
        try:
            data = anyio.run(terminal_show, handle)
        except OrcaError:
            return handle, ""
    terminal = _unwrap_terminal(data)
    tab = terminal.get("tabId")
    leaf = terminal.get("leafId")
    pane_key = f"{tab}:{leaf}" if tab and leaf else ""
    return _terminal_handle(data, fallback=handle), pane_key


def _maybe_detect_caller(caller_terminal: str | None) -> str | None:
    """The notification address of this command's caller, or None.

    A terminal handle when one is known; otherwise the injected
    structured-session address, which Orca delivers mail into as a user
    turn. A structured chat (OMP launched as an Orca chat) has neither
    a terminal handle nor a TTY, so without that fallback the caller of
    every chat-dispatched agent would be unknown.
    """
    if caller_terminal:
        return caller_terminal
    # Orca exports the hosting terminal's handle into every terminal it
    # creates; child processes inherit it, so this works even when osw
    # runs deep inside an agent's tool pipeline where stdout is a pipe
    # and the marker-in-preview trick below cannot work.
    env_handle = _validate_env_caller()
    if env_handle:
        return env_handle
    session = injected_session_address()
    if session:
        log.info("caller from ORCA_AGENT_SESSION_ID  address=%s", session)
        return session
    if sys.stdin.isatty() and sys.stdout.isatty():
        return _detect_caller_terminal()
    log.warning("caller detect skipped: stdin/stdout are not TTYs")
    return None


def _resolve_caller(caller_terminal: str | None, no_notify: bool) -> str | None:
    """Resolve the notification target or fail loudly.

    A dispatch without a caller terminal can never report completion, so
    it is rejected unless the user explicitly opts out via --no-notify.
    """
    caller = _maybe_detect_caller(caller_terminal)
    if caller:
        return caller
    if no_notify:
        typer.echo(
            "WARNING: no caller identified; completion will NOT "
            "be reported. Poll `osw.py status` instead.",
            err=True,
        )
        return None
    typer.echo(
        "Error: could not identify the caller terminal or chat session, "
        "so the completion notification would be lost. Run from an "
        "Orca-managed terminal or chat, pass --caller-terminal <handle>, "
        "or pass --no-notify to proceed without notifications.",
        err=True,
    )
    raise typer.Exit(1)


def _unwrap_terminal(data: dict) -> dict:
    if not isinstance(data, dict):
        return {}
    result = data.get("result")
    if isinstance(result, dict):
        terminal = result.get("terminal")
        if isinstance(terminal, dict):
            return terminal
    terminal = data.get("terminal")
    if isinstance(terminal, dict):
        return terminal
    return data


def _terminal_handle(data: dict, fallback: str = "") -> str:
    terminal = _unwrap_terminal(data)
    return terminal.get("handle") or terminal.get("terminal") or fallback


def _terminal_worktree_path(terminal: dict) -> str:
    if terminal.get("worktreePath"):
        return str(terminal["worktreePath"])
    worktree = terminal.get("worktree")
    if isinstance(worktree, dict):
        return str(worktree.get("path") or "")
    return str(terminal.get("cwd") or "")


def _resolve_terminal_arg(root: Path, value: str) -> tuple[str, dict | None]:
    try:
        agent = read_agent(root, value)
    except FileNotFoundError:
        return value, None
    return str(agent.get("terminal") or value), agent


def _reject_managed_pane(root: Path, terminal: str, terminal_obj: dict) -> None:
    """Refuse to adopt a handle whose pane already has an agent record.

    A second record would put two watchers (or a watcher and a dormant
    record) on one pane; the existing record is reused by its agent id.
    """
    tab = terminal_obj.get("tabId")
    leaf = terminal_obj.get("leafId")
    if not (tab and leaf):
        return
    owner = find_agent_by_pane(root, f"{tab}:{leaf}")
    if owner is None:
        return
    owner_id = owner["agent_id"]
    pid = int(owner.get("watcher_pid") or 0)
    watching = bool(pid) and pid_is_running(pid)
    emit_event(
        logs_dir(root),
        component="cli",
        event="pane_already_managed",
        level="ERROR",
        agent_id=owner_id,
        terminal=terminal,
        data={"pane_key": f"{tab}:{leaf}", "watcher_running": watching},
    )
    if watching:
        typer.echo(
            f"Error: terminal {terminal} is already managed by agent "
            f"'{owner_id}' (watcher running)"
        )
    else:
        typer.echo(
            f"Error: terminal {terminal} is already recorded as agent "
            f"'{owner_id}'; run `use {owner_id} <prompt>` instead"
        )
    raise typer.Exit(1)


async def _refresh_stale_handle(
    root: Path, pane_key: str | None, host: Host | None = None,
) -> str | None:
    """Re-resolve a stale runtime-scoped handle via the pane's stable identity.

    Mirrors the watcher's _rebind_terminal: handles die with the Orca
    runtime, but tabId:leafId survives, so the current handle for the
    same pane can be looked up in `terminal list` on the agent's host.
    """
    if not pane_key:
        return None
    host = host or host_seam.local_host()
    try:
        terminals = await terminal_list(host.worktree_selector(str(root)), host=host)
    except OrcaError:
        return None
    for terminal in terminals:
        tab = terminal.get("tabId")
        leaf = terminal.get("leafId")
        handle = terminal.get("handle")
        if handle and f"{tab}:{leaf}" == pane_key:
            return str(handle)
    return None


async def _close_if_still_open(
    root: Path, agent: dict, host: Host | None = None,
) -> bool:
    """Close the agent's terminal if it is still listed; True if closed.

    Matched by pane identity first (handles go stale), then by handle.
    A listing failure means unknown, so nothing is closed; a close
    failure propagates, so the caller can warn about it.
    """
    host = host or host_seam.local_host()
    try:
        terminals = await reclaim.list_open(root, host)
    except OrcaError:
        return False
    return await reclaim.close_if_open(root, agent, host, terminals)


async def _close_agent_terminal(
    root: Path, agent: dict, host: Host | None = None,
) -> None:
    """Close an agent's terminal, re-resolving a stale handle once by pane."""
    host = host or host_seam.local_host()
    handle = str(agent.get("terminal") or "")
    try:
        await terminal_close(handle, host=host)
        return
    except OrcaError as exc:
        if exc.code != "terminal_handle_stale":
            raise
        rebound = await _refresh_stale_handle(root, agent.get("pane_key"), host)
        if rebound is None:
            raise
    emit_event(
        logs_dir(root),
        component="cli",
        event="terminal_rebound",
        agent_id=str(agent.get("agent_id") or ""),
        terminal=rebound,
        message="stale handle re-resolved by pane identity",
        data={"previous": handle, "current": rebound},
    )
    await terminal_close(rebound, host=host)


def _script_path() -> Path:
    return Path(__file__).resolve().parents[1] / "osw.py"


def _spawn_watcher(root: Path, agent_id: str) -> int:
    cmd = [sys.executable, str(_script_path()), "watch", str(root), agent_id]
    kwargs = {
        "cwd": str(root),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        **hidden_subprocess_kwargs(),
    }
    if os.name == "nt":
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)
    return int(proc.pid)


def _terminate_pid(pid: int) -> None:
    if not pid or not pid_is_running(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError as exc:
        log.warning("failed to terminate watcher pid=%s: %s", pid, exc)


def _supervised_spec(prompt: str, handoff: Path) -> str:
    """One-line task spec: the user's task plus the OSW artifact contract.

    Sent through Orca's prompt injection (`worker-start --spec`), so the
    worker receives one task turn that already carries the handoff
    requirement and calls `worker_done --report-path` itself. A worker
    that skips the contract is still caught by the watcher's wrap-up
    fallback. Single line because newlines get mangled through Orca's
    Windows CLI paths.
    """
    task = " ".join(prompt.split())
    return (
        f"{task} Orchestration contract (OSW): when the task above is "
        f"finished, write a concise English handoff document to {handoff} "
        "(markdown) covering: 1) what you did, 2) key findings and "
        "decisions, 3) files you modified, 4) what remains or is blocked. "
        "If any of your background tasks are still running, wait for them "
        "to finish and fold their results into the handoff before writing "
        f"the file. Create the file even if the task was trivial, then "
        "send worker_done exactly once with --outcome succeeded (or "
        f"failed) and --report-path {handoff}, and stop; do not start "
        "new work."
    )


async def _ensure_orchestration_run(root: Path) -> str:
    """The Run OSW's workers for this project belong to.

    Orca requires the calling terminal to be bound to the Task's Run for
    `worker-start`. OSW keeps one Run per project (state.json:
    orchestration_run_id) and binds the invoking caller to it; a caller
    already bound to a *different* Run gets rebound, recorded as an
    event so the switch is visible in `osw logs`.
    """
    state = read_state(root)
    run_id = str(state.get("orchestration_run_id") or "")
    current = await orchestration_run_current()
    run = current.get("run")
    bound = str(run.get("id") or "") if isinstance(run, dict) else ""
    if run_id and bound == run_id:
        return run_id
    if run_id:
        try:
            if bound != run_id:
                await orchestration_run_use(run_id)
                emit_event(
                    logs_dir(root),
                    component="cli",
                    event="orchestration_run_rebound",
                    message="bound the caller terminal to the project run",
                    data={"run_id": run_id, "previous_run_id": bound},
                )
            return run_id
        except OrcaError:
            log.warning(
                "stored orchestration run %s is unusable; creating a new one",
                run_id,
            )
    created = await orchestration_run_create(f"OSW: {root}")
    new_id = str((created.get("run") or {}).get("id") or "")
    state["orchestration_run_id"] = new_id
    write_state(root, state)
    emit_event(
        logs_dir(root),
        component="cli",
        event="orchestration_run_created",
        message="created the project orchestration run",
        data={"run_id": new_id},
    )
    return new_id


async def _launch_supervised(
    root: Path,
    agent_id: str,
    provider: str,
    prompt: str,
    model: str,
    terminal: str = "",
) -> dict:
    """Launch one worker through `orca orchestration worker-start`.

    Returns the receipt info the agent record needs: lifecycle ids, the
    agent terminal handle and the pre-allocated handoff path. With
    `terminal` set the Dispatch reuses that proven worker terminal
    (an OSW continuation) instead of launching a fresh agent.
    """
    await _ensure_orchestration_run(root)
    handoff = new_handoff_path(root, agent_id)
    result = await orchestration_worker_start(
        spec=_supervised_spec(prompt, handoff),
        agent=supervised_agent_id(provider) or provider,
        worktree=f"path:{root}",
        task_title=" ".join(prompt.split())[:60],
        model=model,
        terminal=terminal,
        timeout_ms=SUPERVISED_START_TIMEOUT_MS,
    )
    receipt = worker_start_receipt(result)
    show = await orchestration_worker_show(receipt["dispatch_id"])
    settlement = worker_settlement(show)
    if not receipt["terminal"] and settlement["terminal"]:
        receipt["terminal"] = settlement["terminal"]
    receipt["pane_key"] = settlement["pane_key"]
    receipt["handoff_path"] = str(handoff)
    return receipt


def _supervised_record_fields(receipt: dict, provider: str) -> dict:
    """Agent-record fields that mark a worker as supervision-backed."""
    return {
        "launch_mode": "supervised",
        "orchestration_agent": supervised_agent_id(provider) or provider,
        "run_id": receipt.get("run_id", ""),
        "task_id": receipt.get("task_id", ""),
        "dispatch_id": receipt.get("dispatch_id", ""),
        "handoff_path": receipt.get("handoff_path", ""),
    }

async def _release_failed_supervised_launch(exc: OrcaError) -> str:
    """Best-effort cleanup of the terminal a failed worker-start left.

    Orca's failed receipt names the orphan terminal in
    `residualResources` and the recovery action in `projection.nextAction`
    (release for a dispatch that failed at agent readiness); follow it so
    a failed launch never strands a visible terminal. Returns the
    dispatch id it released, or "".
    """
    payload = exc.payload if isinstance(exc.payload, dict) else {}
    result = payload.get("result")
    if not isinstance(result, dict):
        return ""
    dispatch_id = str(result.get("dispatchId") or "")
    if not dispatch_id:
        return ""
    try:
        await orchestration_worker_release(dispatch_id)
    except OrcaError:
        return ""
    return dispatch_id


def _base_agent(
    root: Path,
    agent_id: str,
    terminal: str,
    prompt: str,
    caller: str | None,
    provider: str = "",
    provider_command: str = "",
    model_name: str = "",
    thinking: str = "",
    terminal_info: dict | None = None,
    origin: str = "",
    host: str = "",
    worktree_path: str | None = None,
) -> dict:
    # The notification target is either a terminal handle or a
    # structured-session address; the watcher picks the delivery lane
    # from which field is set.
    caller_address = caller or ""
    caller_session = caller_address if is_session_address(caller_address) else ""
    caller_terminal = "" if caller_session else caller_address
    now = now_iso()
    agent = {
        "agent_id": agent_id,
        "terminal": terminal,
        "worktree_path": worktree_path or str(root),
        "provider": provider,
        "provider_command": provider_command,
        "model_name": model_name,
        "thinking": thinking,
        "origin": origin,
        "state": "assigned",
        "phase": "",
        "caller_terminal": caller_terminal or None,
        "prompt": prompt,
        "created_at": now,
        "updated_at": now,
    }
    if caller_session:
        agent["caller_session"] = caller_session
    if host:
        agent["host"] = host
    terminal_info = terminal_info or {}
    tab = terminal_info.get("tabId")
    leaf = terminal_info.get("leafId")
    if tab and leaf:
        agent["pane_key"] = f"{tab}:{leaf}"
    return agent


# Facts about the pane and its provider session rather than one task:
# a reused agent record keeps them.
REUSED_FIELDS = ("session_id", "support", "support_reason")


def _reuse_origin(existing: dict | None) -> str:
    return str((existing or {}).get("origin") or "use")


def _carry_reused_fields(agent: dict, existing: dict | None) -> None:
    for key in REUSED_FIELDS:
        if existing and key in existing:
            agent[key] = existing[key]


def _use_notify_target(
    existing: dict | None, caller_terminal: str | None, no_notify: bool
) -> tuple[str | None, dict | None]:
    """(caller terminal, guardian to keep) for a `use` dispatch.

    A recorded guardian stays the notification target unless
    --caller-terminal names one explicitly; with a guardian in place a
    missing caller terminal is no longer fatal.
    """
    guardian = (existing or {}).get("guardian")
    if guardian and not caller_terminal:
        return _maybe_detect_caller(None), guardian
    return _resolve_caller(caller_terminal, no_notify), None


def _start_watcher(root: Path, agent: dict) -> None:
    write_agent(root, agent)
    emit_event(
        logs_dir(root),
        component="cli",
        event="agent_registered",
        agent_id=agent["agent_id"],
        terminal=agent.get("terminal", ""),
        message="agent record written",
        data={
            "model": agent.get("model_name", ""),
            "provider_command": agent.get("provider_command", ""),
        },
    )
    try:
        pid = _spawn_watcher(root, agent["agent_id"])
    except OSError as exc:
        agent["state"] = "error"
        agent["completion_source"] = "watcher_spawn_failed"
        agent["error"] = str(exc)
        agent["updated_at"] = now_iso()
        write_agent(root, agent)
        emit_event(
            logs_dir(root),
            component="cli",
            event="watcher_spawn_failed",
            level="ERROR",
            agent_id=agent["agent_id"],
            terminal=agent.get("terminal", ""),
            message=str(exc),
        )
        raise
    agent["watcher_pid"] = pid
    agent["updated_at"] = now_iso()
    write_agent(root, agent)
    emit_event(
        logs_dir(root),
        component="cli",
        event="watcher_spawned",
        agent_id=agent["agent_id"],
        terminal=agent.get("terminal", ""),
        message="watcher process started",
        data={"pid": pid},
    )


def _ps_by_pane(worktrees: list[dict]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for worktree in worktrees:
        for entry in worktree.get("agents") or []:
            pane = entry.get("paneKey")
            if pane:
                result[pane] = entry
    return result


# States no watcher owns: the CLI may write a record in one of these.
SETTLED_STATES = frozenset({"done", "error", "lost", "enrolled"})


def _mark_lost_if_orphaned(root: Path, agent: dict) -> dict:
    """Mark a mid-task record whose watcher process died as `lost`.

    Only records with a spawned watcher qualify; one still waiting for
    its spawn has no pid yet. The record is re-read after the pid check
    because a watcher writes its final state just before exiting, and
    that state must win over `lost`.
    """
    if agent.get("state") in SETTLED_STATES:
        return agent
    pid = int(agent.get("watcher_pid") or 0)
    if not pid or pid_is_running(pid):
        return agent
    agent_id = str(agent["agent_id"])
    try:
        current = read_agent(root, agent_id)
    except (FileNotFoundError, ValueError):
        return agent
    if current.get("state") in SETTLED_STATES \
            or int(current.get("watcher_pid") or 0) != pid:
        return current
    previous = current.get("state", "")
    current["state"] = "lost"
    current["updated_at"] = now_iso()
    try:
        write_agent(root, current)
    except OSError as exc:
        log.warning("failed to mark %s lost: %s", agent_id, exc)
        return agent
    emit_event(
        logs_dir(root),
        component="cli",
        event="agent_lost",
        level="WARNING",
        agent_id=agent_id,
        terminal=current.get("terminal", ""),
        message="watcher process is gone; record marked lost",
        data={"previous_state": previous, "watcher_pid": pid},
    )
    _spool_watcher_died(root, agent_id)
    return current


def _spool_watcher_died(root: Path, agent_id: str) -> None:
    """Best-effort system envelope so the control plane learns of the death.

    Never allowed to break a listing: the record is already written.
    """
    try:
        from osw.mailbox.spool import write_envelope

        write_envelope(root, {
            "from": {"role": "system", "id": "osw-cli"},
            "to": {"role": "router", "id": ""},
            "kind": "system",
            "correlation": {"agent": agent_id},
            "thread_key": agent_id,
            "body": {"event": "watcher_died", "agent": agent_id},
        })
    except Exception as exc:  # never break a read path on a spool failure
        log.warning("watcher_died spool write failed for %s: %s", agent_id, exc)


def _row_host(agent: dict) -> str:
    """The host an agent record names; `local` when it names none."""
    return str(agent.get("host") or host_seam.LOCAL_NAME)


def _fleet_hosts(root: Path) -> dict[str, Host]:
    """Every host a fleet view can address: this machine plus the registry.

    Raises HostError for a malformed registry — a broken fleet listing is
    never silently reported as an empty one.
    """
    return {
        host_seam.LOCAL_NAME: host_seam.local_host(),
        **host_seam.read_hosts(root),
    }


def _ps_by_host(
    root: Path, agents: dict[str, dict], *, all_hosts: bool,
) -> tuple[dict[tuple[str, str], dict], set[str]]:
    """((host, pane) -> Orca ps row, hosts whose ps could not be read).

    One `worktree ps` per host in play. Without `all_hosts` only this
    machine is asked, so an ordinary listing never pays a remote round
    trip; a host that cannot answer is remembered by name instead of
    being reported with another host's state.
    """
    entries: dict[tuple[str, str], dict] = {}
    unreadable: set[str] = set()
    if all_hosts:
        hosts = _fleet_hosts(root)
        wanted = sorted({_row_host(agent) for agent in agents.values()})
    else:
        hosts = {host_seam.LOCAL_NAME: host_seam.local_host()}
        wanted = [host_seam.LOCAL_NAME]
    for name in wanted:
        host = hosts.get(name)
        if host is None:
            # A record can outlive its host registration (`host rm`).
            unreadable.add(name)
            continue
        try:
            rows = _ps_by_pane(_run_async(worktree_ps, host=host))
        except Exception as exc:
            log.warning("%s: worktree ps failed: %s", name, exc)
            if not host.is_local:
                unreadable.add(name)
            continue
        for pane, entry in rows.items():
            entries[(name, pane)] = entry
    return entries, unreadable


def _agent_rows(root: Path, *, all_hosts: bool = False) -> dict[str, dict]:
    agents = list_agents(root)
    ps_entries, unreadable = _ps_by_host(root, agents, all_hosts=all_hosts)

    rows: dict[str, dict] = {}
    for agent_id, agent in agents.items():
        row = dict(_mark_lost_if_orphaned(root, agent))
        pid = int(row.get("watcher_pid") or 0)
        row["watcher_running"] = pid_is_running(pid) if pid else False
        host_name = _row_host(row)
        pane = row.get("pane_key") or row.get("paneKey")
        ps_entry = ps_entries.get((host_name, pane)) if pane else None
        if ps_entry:
            row["orca_state"] = ps_entry.get("state", "")
            row["orca_prompt"] = ps_entry.get("prompt", "")
            row["tool_name"] = ps_entry.get("toolName", "")
            row["last_assistant_message"] = ps_entry.get("lastAssistantMessage", "")
        elif host_name in unreadable:
            # Never present another host's state as this agent's own.
            row["orca_state"] = "unknown"
        else:
            row.setdefault("orca_state", "")
        if all_hosts:
            row["host"] = host_name
        rows[agent_id] = row
    return rows


def _tail_text(path: Path, lines: int) -> list[str]:
    if lines <= 0:
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            return [line.rstrip("\n") for line in deque(f, maxlen=lines)]
    except OSError:
        return []


@app.command()
def init(
    interactive: bool = typer.Option(
        None, "--interactive/--no-interactive", "-i",
        help="Deprecated; kept for compatibility and ignored.",
    ),
) -> None:
    """Initialize OSW state for the current directory."""
    root = resolve_project_root()
    init_state_dir(root)
    log.info("initialized state at %s", state_file(root))
    typer.echo(f"Initialized OSW state at {state_file(root)}")

    typer.echo("")
    typer.echo("Scanning for agent CLIs on PATH...")
    found = scan_agent_clis()
    if found:
        for entry in found:
            version = f"  ({entry['version']})" if entry["version"] else ""
            typer.echo(f"  found: {entry['name']:<14} {entry['path']}{version}")
    else:
        typer.echo("  no known agent CLIs found on PATH")

    if found:
        typer.echo("")
        typer.echo("Inspect provider models with:")
        typer.echo("  python osw.py models claude")
        typer.echo("  python osw.py models codex")
        typer.echo("  python osw.py models pi")
        typer.echo("  python osw.py models omp")
        typer.echo("  python osw.py models kimi")
        typer.echo("  python osw.py models grok")
        typer.echo("  python osw.py models dsh")


@app.command("models")
def models(
    provider: str = typer.Argument(
        ...,
        help="Provider to query: claude, codex, pi, omp, kimi, grok, or dsh",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON"),
) -> None:
    """Discover provider model options without writing OSW state."""
    variants = probe_variants(provider)
    if json_output:
        typer.echo(json.dumps(variants, indent=2))
        return
    if not variants:
        typer.echo(
            f"No discoverable models for '{provider}'. "
            f"Check `{provider} --help` for its model flags."
        )
        return
    for opt in variants:
        typer.echo(f"{opt['name']:<28} {opt['command']}")


jev_app = typer.Typer(
    help="Jev (TypeSafe System One) integration: status only — the mode "
         "is the global OSW_JEV_MODE environment variable.",
    no_args_is_help=True,
)
app.add_typer(jev_app, name="jev")


@jev_app.command("status")
def jev_status(
    check: bool = typer.Option(
        False, "--check", help="Live-probe the endpoint (free, not billed)"),
) -> None:
    """Show mode, credentials and environment state (no usage stats)."""
    root = resolve_project_root()
    _read_state_or_exit(root)
    mode = resolve_mode(root)
    env_mode = os.environ.get("OSW_JEV_MODE", "").strip().lower()
    sdk_ok, version = sdk_status()
    typer.echo(f"mode: {mode}")
    if env_mode in ("off", "shadow", "enforce"):
        typer.echo(f"global: OSW_JEV_MODE={env_mode}")
    else:
        typer.echo("global: OSW_JEV_MODE is not set  (mode defaults to off)")
    if mode == "enforce":
        names = ", ".join(resolve_enforce_providers(root)) or "(none)"
        typer.echo(f"enforce_providers: {names}")
    typer.echo(f"api key: {masked_key()}  (TYPESAFE_API_KEY)")
    typer.echo(
        f"sdk: {'installed ' + version if sdk_ok else 'not importable'}"
        "  (typesafe-sdk)")
    typer.echo(f"endpoint: {base_url()}  (TYPESAFE_BASE_URL)")
    typer.echo(
        f"proxy: {'set' if proxy_set() else 'not set'}"
        "  (HTTPS_PROXY/HTTP_PROXY)")
    if check:
        result = check_endpoint()
        if result.get("ok"):
            typer.echo(
                f"check: OK  http={result['status']}  "
                f"latency={result['latency_ms']}ms")
        else:
            typer.echo(
                f"check: FAILED  status={result['status']}  "
                f"reason={result.get('reason', 'unknown')}")
            raise typer.Exit(1)


@app.command()
def new(
    provider: str,
    prompt: str,
    model: str = typer.Option(None, "--model", "-m", help="Provider model value passed through unchanged"),
    thinking: str = typer.Option(None, "--thinking", help="Provider thinking/effort value passed through unchanged"),
    prefix: str = typer.Option(None, "--prefix", help="Agent id prefix, e.g. research, code, test, debug, review, misc"),
    caller_terminal: str = typer.Option(None, "--caller-terminal", help="Terminal handle to receive completion reports"),
    no_notify: bool = typer.Option(False, "--no-notify", help="Proceed without a caller terminal; completion will not be reported"),
    on: str = typer.Option(None, "--on", help="Registered host to dispatch to (default: this machine)"),
) -> None:
    """Create a new provider agent terminal and return after starting its watcher."""
    root = resolve_project_root()
    _refuse_router_identity(root, "new")
    enable_file_logging(logs_dir(root))
    _read_state_or_exit(root)
    host = _host_or_exit(root, on)
    remote = not host.is_local
    emit_event(
        logs_dir(root),
        component="cli",
        event="new_started",
        message="creating agent terminal",
        data={
            "provider": provider,
            "model": model or "",
            "thinking": thinking or "",
            "host": host.name,
        },
    )

    # Remote dispatch uses the classic lane local agents use. Orca
    # 1.4.248 cannot start a supervised worker on a paired server:
    # `worker-start --on` waits at agent_readiness until it times out
    # (measured: 240s on host pi, while the remote claude pane sat at
    # its composer, and `terminal wait --for tui-idle` times out on that
    # same idle pane); `terminal send`/`read` and `worktree ps` do work
    # there, so `terminal create --environment <host> --worktree
    # <selector>` plus the watcher's prompt turn is the lane that runs.
    supervised = supervised_agent_id(provider) if not remote else False
    try:
        provider_command = build_provider_command(
            provider,
            model,
            thinking,
            binary=host.providers.get(provider) if remote else None,
        )
        if supervised:
            validate_supervised_args(provider, model, thinking)
    except ProviderError as exc:
        typer.echo(f"Error: {exc}")
        raise typer.Exit(1)

    # Resolve the caller before creating anything: a dispatch without a
    # notification target is rejected, and must not leave an orphan
    # terminal behind.
    caller = _resolve_caller(caller_terminal, no_notify)

    remote_root = ""
    if remote:
        if not host.worktree:
            typer.echo(
                f"Error: no workspace selector for host '{host.name}'; "
                "run `osw host add` with --worktree or "
                "`osw host doctor` first"
            )
            raise typer.Exit(1)
        remote_root = host.workspace_path()
        if not remote_root:
            typer.echo(
                f"Error: host '{host.name}' has no discovered workspace "
                "path; run `osw host doctor` first"
            )
            raise typer.Exit(1)
        if provider not in host.providers:
            typer.echo(
                f"Error: host '{host.name}' has no recorded path for "
                f"provider '{provider}'; run "
                f"`osw host doctor {host.name} --provider {provider}` first"
            )
            raise typer.Exit(1)

    # Pre-record workspace trust so the provider TUI does not block on
    # its interactive "trust this directory?" prompt. A failed remote
    # write is a warning, never a launch failure: the dialog is cleared
    # with one Enter in the pane. A remote root is a POSIX path on that
    # machine, never rebuilt through a local Path.
    trust_root = remote_root if remote else root
    try:
        trust_note = anyio.run(ensure_workspace_trust, provider, trust_root, host)
    except (OSError, HostError) as exc:
        trust_note = "trust pre-record failed"
        emit_event(
            logs_dir(root),
            component="cli",
            event="workspace_trust",
            level="WARNING",
            message=str(exc),
            data={"provider": provider, "root": str(trust_root),
                  "host": host.name},
        )
    if trust_note:
        emit_event(
            logs_dir(root),
            component="cli",
            event="workspace_trust",
            level="WARNING" if "failed" in trust_note else "INFO",
            message=trust_note,
            data={"provider": provider, "root": str(trust_root),
                  "host": host.name},
        )

    agent_id = alloc_agent_id(root, prefix=prefix)
    if supervised:
        # Orca's supervised launch creates the agent terminal, waits for
        # readiness and injects the spec as the task turn; that launch
        # authority is what makes Orca persist this pane's hook status,
        # and the Dispatch lifecycle is the completion signal the
        # watcher polls. The watcher never sends the task prompt here.
        try:
            receipt = anyio.run(
                _launch_supervised,
                root, agent_id, provider, prompt, model or "", "",
            )
        except OrcaError as exc:
            released = anyio.run(_release_failed_supervised_launch, exc)
            emit_event(
                logs_dir(root),
                component="cli",
                event="orchestration_launch_failed",
                level="ERROR",
                message=exc.message,
                data={
                    "code": getattr(exc, "code", ""),
                    "provider": provider,
                    "host": host.name,
                    "orphan_released": released,
                },
            )
            delete_agent(root, agent_id)
            typer.echo(f"Error: {exc.message}")
            raise typer.Exit(1)
        handle = str(receipt.get("terminal") or "")
        if not handle:
            emit_event(
                logs_dir(root),
                component="cli",
                event="orchestration_launch_missing_handle",
                level="ERROR",
                data={"dispatch_id": receipt.get("dispatch_id", "")},
            )
            delete_agent(root, agent_id)
            typer.echo("Error: worker-start returned no agent terminal")
            raise typer.Exit(1)
        agent = _base_agent(
            root,
            agent_id,
            handle,
            prompt,
            caller,
            provider=provider,
            provider_command=provider_command,
            model_name=model or "",
            thinking=thinking or "",
            origin="new",
        )
        if receipt.get("pane_key"):
            agent["pane_key"] = str(receipt["pane_key"])
        agent.update(_supervised_record_fields(receipt, provider))
    else:
        # The terminal starts the bare provider TUI only; the watcher
        # sends the task prompt as its own observable turn once the TUI
        # is ready. A remote pane is addressed through the paired
        # server's environment and its explicit worktree selector; the
        # launch command starts with the absolute provider path doctor
        # discovered, because the remote shell's PATH need not contain
        # it.
        create_kwargs = (
            {"worktree": host.worktree, "host": host}
            if remote else {}
        )
        try:
            result = _run_async(
                terminal_create, provider_command, **create_kwargs,
            )
        except OrcaError as exc:
            emit_event(
                logs_dir(root),
                component="cli",
                event="terminal_create_failed",
                level="ERROR",
                message=exc.message,
                data={"command": provider_command},
            )
            typer.echo(f"Error: {exc.message}")
            raise typer.Exit(1)

        terminal_obj = _unwrap_terminal(result)
        handle = _terminal_handle(result)
        if not handle:
            emit_event(
                logs_dir(root),
                component="cli",
                event="terminal_create_missing_handle",
                level="ERROR",
                data={"result": result},
            )
            typer.echo("Error: terminal create returned no handle")
            raise typer.Exit(1)

        agent = _base_agent(
            root,
            agent_id,
            handle,
            prompt,
            caller,
            provider=provider,
            provider_command=provider_command,
            model_name=model or "",
            thinking=thinking or "",
            terminal_info=terminal_obj,
            origin="new",
            host=host.name if remote else "",
            worktree_path=remote_root or None,
        )

    try:
        _start_watcher(root, agent)
    except OSError as exc:
        typer.echo(f"Error: failed to start watcher: {exc}")
        raise typer.Exit(1)

    created = (
        f"Created {agent_id} on terminal {handle} (provider: {provider})"
    )
    if supervised:
        created += f" dispatch={agent['dispatch_id']}"
    typer.echo(created)
    emit_event(
        logs_dir(root),
        component="cli",
        event="new_finished",
        agent_id=agent_id,
        terminal=handle,
        message="agent created",
    )


@app.command()
def use(
    target: str = typer.Argument(..., help="OSW agent id or Orca terminal handle to adopt"),
    prompt: str = typer.Argument(..., help="Prompt to send to the terminal"),
    prefix: str = typer.Option(None, "--prefix", help="Agent id prefix, e.g. research, code, test, debug, review, misc"),
    caller_terminal: str = typer.Option(None, "--caller-terminal", help="Terminal handle to receive completion reports"),
    no_notify: bool = typer.Option(False, "--no-notify", help="Proceed without a caller terminal; completion will not be reported"),
    on: str = typer.Option(None, "--on", help="Registered host the pane lives on (default: this machine)"),
) -> None:
    """Adopt an already-running terminal as a managed agent."""
    root = resolve_project_root()
    _refuse_router_identity(root, "use")
    enable_file_logging(logs_dir(root))
    _read_state_or_exit(root)
    host = _host_or_exit(root, on)
    host_prefix, target_id = host_seam.split_ref(target)
    if host_prefix != host_seam.LOCAL_NAME:
        host = _host_or_exit(root, host_prefix)
        target = target_id
    remote = not host.is_local
    terminal, existing_agent = _resolve_terminal_arg(root, target)
    recorded_host = str((existing_agent or {}).get("host") or "")
    if recorded_host and recorded_host != "local" and not remote:
        host = _host_or_exit(root, recorded_host)
        remote = True
    if existing_agent and prefix:
        typer.echo(
            "Error: --prefix is only valid when adopting a terminal handle, "
            "not an existing agent id"
        )
        raise typer.Exit(1)
    if existing_agent:
        existing_agent = _mark_lost_if_orphaned(root, existing_agent)
    if existing_agent and existing_agent.get("state") not in SETTLED_STATES:
        typer.echo(f"Error: agent '{target}' is not finished")
        raise typer.Exit(1)
    emit_event(
        logs_dir(root),
        component="cli",
        event="use_started",
        terminal=terminal,
        message="adopting terminal",
    )

    try:
        data = _run_async(terminal_show, terminal, host=host)
    except OrcaError as exc:
        # A stale handle is recoverable exactly once: look the pane up by
        # its stable tabId:leafId and retry with the current handle —
        # the same rebind the watcher performs internally, applied here
        # at the `use` entry point too.
        rebound = None
        if exc.code == "terminal_handle_stale":
            pane_key = str((existing_agent or {}).get("pane_key") or "")
            rebound = anyio.run(
                _refresh_stale_handle, root, pane_key or None, host,
            )
        if rebound is None:
            emit_event(
                logs_dir(root),
                component="cli",
                event="terminal_show_failed",
                level="ERROR",
                terminal=terminal,
                message=exc.message,
            )
            typer.echo(f"Error: {exc.message}")
            if existing_agent:
                typer.echo(
                    f"Hint: '{target}' has no open pane (a finished agent's "
                    f"pane is reclaimed); resume its session with: "
                    f"osw revive {target}"
                )
            raise typer.Exit(1)
        emit_event(
            logs_dir(root),
            component="cli",
            event="terminal_rebound",
            terminal=rebound,
            message="stale handle re-resolved by pane identity",
            data={"previous": terminal, "current": rebound},
        )
        terminal = rebound
        try:
            data = _run_async(terminal_show, terminal, host=host)
        except OrcaError as exc2:
            emit_event(
                logs_dir(root),
                component="cli",
                event="terminal_show_failed",
                level="ERROR",
                terminal=terminal,
                message=exc2.message,
            )
            typer.echo(f"Error: {exc2.message}")
            raise typer.Exit(1)

    terminal_obj = _unwrap_terminal(data)
    worktree_path = _terminal_worktree_path(terminal_obj)
    workspace = _workspace_view(root, host)
    expected_root = workspace.workspace_path()
    mismatch = bool(worktree_path and expected_root
                    and not workspace.same_workspace(worktree_path))
    if mismatch:
        emit_event(
            logs_dir(root),
            component="cli",
            event="terminal_worktree_mismatch",
            level="ERROR",
            terminal=terminal,
            data={"terminal_worktree": worktree_path, "root": expected_root},
        )
        typer.echo(
            f"Error: terminal worktree '{worktree_path}' does not match "
            f"project root '{expected_root}'"
        )
        raise typer.Exit(1)


    if not existing_agent:
        _reject_managed_pane(root, terminal, terminal_obj)

    handle = _terminal_handle(data, fallback=terminal)
    agent_id = (
        str(existing_agent.get("agent_id"))
        if existing_agent
        else alloc_agent_id(root, prefix=prefix)
    )
    provider = str((existing_agent or {}).get("provider") or "")
    if existing_agent and existing_agent.get("launch_mode") == "supervised" \
            and not remote:
        # Continuing a supervised agent runs as another supervised
        # Dispatch on the same proven terminal (`worker-start
        # --terminal`): the classic send-and-watch path would wait
        # forever on a TUI that never stops repainting. Remotely there
        # is no supervised lane to continue (worker-start on a paired
        # server never leaves agent_readiness), so a remote record —
        # including one written by an older supervised dispatch — is
        # followed by the classic watcher, which sends the prompt.
        caller, guardian = _use_notify_target(
            existing_agent, caller_terminal, no_notify,
        )
        try:
            receipt = anyio.run(
                _launch_supervised,
                root, agent_id, provider, prompt, "", handle,
            )
        except OrcaError as exc:
            released = anyio.run(_release_failed_supervised_launch, exc)
            emit_event(
                logs_dir(root),
                component="cli",
                event="orchestration_launch_failed",
                level="ERROR",
                message=exc.message,
                data={
                    "code": getattr(exc, "code", ""),
                    "provider": provider,
                    "host": host.name,
                    "orphan_released": released,
                },
            )
            typer.echo(f"Error: {exc.message}")
            raise typer.Exit(1)
        agent = _base_agent(
            root,
            agent_id,
            handle,
            prompt,
            caller,
            provider=provider,
            provider_command=str(
                existing_agent.get("provider_command") or ""
            ),
            model_name=str(existing_agent.get("model_name") or ""),
            thinking=str(existing_agent.get("thinking") or ""),
            terminal_info=terminal_obj,
            origin=_reuse_origin(existing_agent),
            host=host.name if remote else "",
            worktree_path=expected_root if remote else None,
        )
        _carry_reused_fields(agent, existing_agent)
        if guardian:
            agent["guardian"] = guardian
        if receipt.get("pane_key"):
            agent["pane_key"] = str(receipt["pane_key"])
        agent.update(_supervised_record_fields(receipt, provider))

        try:
            _start_watcher(root, agent)
        except OSError as exc:
            typer.echo(f"Error: failed to start watcher: {exc}")
            raise typer.Exit(1)

        typer.echo(
            f"Reused {agent_id} on terminal {handle} "
            f"dispatch={agent['dispatch_id']}"
        )
        emit_event(
            logs_dir(root),
            component="cli",
            event="use_finished",
            agent_id=agent_id,
            terminal=handle,
            message="agent reused (supervised dispatch)",
        )
        return

    caller, guardian = _use_notify_target(
        existing_agent, caller_terminal, no_notify,
    )
    agent = _base_agent(
        root,
        agent_id,
        handle,
        prompt,
        caller,
        provider=str((existing_agent or {}).get("provider") or ""),
        provider_command=str((existing_agent or {}).get("provider_command") or ""),
        model_name=str((existing_agent or {}).get("model_name") or ""),
        thinking=str((existing_agent or {}).get("thinking") or ""),
        terminal_info=terminal_obj,
        origin=_reuse_origin(existing_agent),
        host=host.name if remote else "",
        worktree_path=expected_root if remote else None,
    )
    _carry_reused_fields(agent, existing_agent)
    if guardian:
        agent["guardian"] = guardian

    try:
        _start_watcher(root, agent)
    except OSError as exc:
        typer.echo(f"Error: failed to start watcher: {exc}")
        raise typer.Exit(1)

    verb = "Reused" if existing_agent else "Adopted"
    typer.echo(f"{verb} {agent_id} on terminal {handle}")
    emit_event(
        logs_dir(root),
        component="cli",
        event="use_finished",
        agent_id=agent_id,
        terminal=handle,
        message="agent adopted",
    )


@app.command("super")
def super_cmd(
    tasks: list[str] = typer.Argument(None, metavar="[TASKS]...", help="Task prompts, one per argument; combined with --task"),
    task: list[str] = typer.Option(None, "--task", help="Task prompt; repeat once per task"),
    plan: Path = typer.Option(None, "--plan", help="JSON plan: provider/model/thinking/prefix/max_parallel plus a non-empty tasks array"),
    provider: str = typer.Option(None, "--provider", help="Provider for every task: claude, codex, pi, omp, kimi, grok, or dsh (default: the project's batch fan-out policy)"),
    model: str = typer.Option(None, "--model", "-m", help="Provider model value passed through unchanged"),
    thinking: str = typer.Option(None, "--thinking", help="Provider thinking/effort value passed through unchanged"),
    prefix: str = typer.Option(None, "--prefix", help="Agent id prefix for every task, e.g. code, review (default: code)"),
    caller_terminal: str = typer.Option(None, "--caller-terminal", help="Terminal handle to receive completion reports"),
    no_notify: bool = typer.Option(False, "--no-notify", help="Dispatch tasks without a caller terminal; completion will not be reported"),
    mailbox: bool = typer.Option(True, "--mailbox/--no-mailbox", help="Start the mailbox daemon before dispatching"),
    max_parallel: int = typer.Option(None, "--max-parallel", help="Concurrent dispatches, clamped to 1..32 (default: 32)"),
    json_output: bool = typer.Option(False, "--json", help="Print one JSON object"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the argv lists without executing anything"),
) -> None:
    """Dispatch many tasks in parallel, one supervised agent each."""
    code = fanout_mod.run(
        root=resolve_project_root(),
        provider=provider,
        model=model,
        thinking=thinking,
        prefix=prefix,
        caller_terminal=caller_terminal,
        tasks=[*list(tasks or ()), *list(task or ())],
        plan=plan,
        mailbox=mailbox,
        max_parallel=max_parallel,
        no_notify=no_notify,
        json_output=json_output,
        dry_run=dry_run,
    )
    raise typer.Exit(code)


def _enroll_record(
    root: Path, handle: str, pane_key: str, provider: str,
    host: Host | None = None, session_id: str = "",
) -> dict:
    """Write one `enrolled` record: registered, unwatched, no task."""
    host = host or host_seam.local_host()
    remote = not host.is_local
    agent_id = alloc_agent_id(root, prefix=provider)
    now = now_iso()
    agent = {
        "agent_id": agent_id,
        "terminal": handle,
        "pane_key": pane_key,
        "worktree_path": host.remote_root if remote else str(root),
        "provider": provider,
        "origin": "enroll",
        "state": "enrolled",
    }
    if remote:
        agent["host"] = host.name
    if provider in SUPPORTED_PROVIDERS:
        agent["support"] = "full"
    else:
        agent["support"] = "guard_only"
        agent["support_reason"] = "unsupported_provider"
    agent["session_id"] = session_id
    agent["created_at"] = now
    agent["updated_at"] = now
    try:
        write_agent(root, agent)
    except OSError:
        delete_agent(root, agent_id)
        raise
    emit_event(
        logs_dir(root),
        component="cli",
        event="agent_enrolled",
        agent_id=agent_id,
        terminal=handle,
        message="unmanaged agent terminal recorded",
        data={
            "pane_key": pane_key,
            "provider": provider,
            "support": agent["support"],
            "session_id": agent["session_id"],
        },
    )
    return agent


@app.command()
def enroll(
    on: str = typer.Option(None, "--on", help="Registered host whose panes to record (default: this machine)"),
) -> None:
    """Record this worktree's unmanaged agent terminals without watching them."""
    root = resolve_project_root()
    enable_file_logging(logs_dir(root))
    _read_state_or_exit(root)
    host = _host_or_exit(root, on)
    remote = not host.is_local
    workspace = _workspace_view(root, host)
    expected_root = workspace.workspace_path()

    try:
        terminals = _run_async(
            terminal_list, host.worktree_selector(str(root)), host=host,
        )
    except OrcaError as exc:
        typer.echo(f"Error: {exc.message}")
        raise typer.Exit(1)
    # Warnings go to the event stream, not the console log: the console
    # handler writes to stdout, which carries only the JSON result here.
    try:
        ps_entries = _ps_by_pane(_run_async(worktree_ps, host=host))
    except Exception as exc:
        emit_event(
            logs_dir(root),
            component="cli",
            event="enroll_ps_failed",
            level="WARNING",
            message=str(exc),
        )
        ps_entries = {}

    self_handle, self_pane = _executor_identity(None) if not remote else ("", "")
    known = [
        agent for agent in list_agents(root).values()
        if str(agent.get("host") or "local") == (host.name if remote else "local")
    ]
    recorded_panes = {a.get("pane_key") or a.get("paneKey") for a in known} - {None, ""}
    recorded_handles = {a.get("terminal") for a in known} - {None, ""}

    enrolled: list[dict] = []
    skipped: list[dict] = []
    for terminal in terminals:
        handle = str(terminal.get("handle") or "")
        worktree_path = _terminal_worktree_path(terminal)
        if not handle:
            continue
        if worktree_path and expected_root \
                and not workspace.same_workspace(worktree_path):
            continue
        tab = terminal.get("tabId")
        leaf = terminal.get("leafId")
        pane_key = f"{tab}:{leaf}" if tab and leaf else ""
        ps_entry = ps_entries.get(pane_key) or {}
        provider = normalize_provider(str(
            terminal.get("agentIdentity") or ps_entry.get("agentType") or ""
        ))
        session_id = ""
        if pane_key:
            try:
                session_id = _run_async(
                    session_for_agent, host, pane_key, root,
                )
            except HostError as exc:
                emit_event(
                    logs_dir(root),
                    component="cli",
                    event="remote_session_unknown",
                    level="WARNING",
                    message=exc.message,
                    data={"pane_key": pane_key, "host": host.name,
                          "code": exc.code},
                )
        if (self_pane and pane_key == self_pane) \
                or (self_handle and handle == self_handle):
            reason = "caller_terminal"
        elif not pane_key:
            reason = "no_pane_key"
        elif pane_key in recorded_panes or handle in recorded_handles:
            reason = "already_recorded"
        elif not provider:
            reason = "no_agent"
        else:
            try:
                agent = _enroll_record(
                    root, handle, pane_key, provider, host, session_id,
                )
            except OSError as exc:
                emit_event(
                    logs_dir(root),
                    component="cli",
                    event="enroll_write_failed",
                    level="ERROR",
                    terminal=handle,
                    message=str(exc),
                )
                reason = "write_failed"
            else:
                enrolled.append(agent)
                recorded_panes.add(pane_key)
                continue
        skipped.append({"terminal": handle, "reason": reason})

    typer.echo(json.dumps(
        {"enrolled": enrolled, "skipped": skipped}, indent=2, ensure_ascii=False,
    ))


# `guard` without ids takes over every unguarded record in these states.
GUARDABLE_STATES = frozenset({"enrolled", "done", "error"})
# How long `guard` waits for a terminated watcher before giving up on it.
WATCHER_EXIT_TIMEOUT_SECS = 10.0


def _wait_pid_exit(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while pid_is_running(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


def _guardian_ref(root: Path, handle: str, pane_key: str) -> dict:
    """Guardian reference: an OSW agent by id (its current handle is read
    from its own record at notify time), a structured chat by session
    address, anything else inline."""
    if is_session_address(handle):
        return {"session": handle}
    owner = find_agent_by_pane(root, pane_key)
    if owner is not None:
        return {"agent_id": owner["agent_id"]}
    return {"terminal": handle, "pane_key": pane_key}


def _guard_one(
    root: Path, agent: dict, guardian: dict, ps_entries: dict[str, dict]
) -> tuple[str, str]:
    """Put one agent under `guardian`. Returns ("guarded", action) or
    ("skipped", reason).

    A live watcher is terminated first: only once it is gone may the
    CLI write the record. A watcher that was running, or a pane Orca
    reports `working`, gets a new watcher that follows the turn in
    progress; an idle agent only records its guardian.
    """
    agent_id = str(agent["agent_id"])
    pid = int(agent.get("watcher_pid") or 0)
    if pid and pid_is_running(pid):
        _terminate_pid(pid)
        if not _wait_pid_exit(pid, WATCHER_EXIT_TIMEOUT_SECS):
            return "skipped", "watcher_did_not_exit"
        action = "watcher_restarted"
    else:
        pane = agent.get("pane_key") or agent.get("paneKey")
        ps_state = (ps_entries.get(pane) or {}).get("state") if pane else ""
        action = "follow_started" if ps_state == "working" else "guardian_set"

    try:
        agent = read_agent(root, agent_id)
    except (FileNotFoundError, ValueError):
        return "skipped", "not_found"
    agent["guardian"] = guardian
    agent["updated_at"] = now_iso()
    if action == "guardian_set":
        write_agent(root, agent)
    else:
        agent["follow_turn"] = True
        agent.pop("watcher_pid", None)
        try:
            _start_watcher(root, agent)
        except OSError:
            return "skipped", "watcher_spawn_failed"
    emit_event(
        logs_dir(root),
        component="cli",
        event="agent_guarded",
        agent_id=agent_id,
        terminal=agent.get("terminal", ""),
        message=action,
        data={"guardian": guardian, "action": action},
    )
    return "guarded", action


@app.command()
def guard(
    agent_ids: list[str] = typer.Argument(
        None,
        help="Agent ids to guard (default: every unguarded enrolled/done/error agent)",
    ),
    caller_terminal: str = typer.Option(None, "--caller-terminal", help="Terminal handle to act as guardian"),
) -> None:
    """Become the guardian (notification target) of managed agents."""
    root = resolve_project_root()
    enable_file_logging(logs_dir(root))
    _read_state_or_exit(root)

    handle, pane_key = _executor_identity(caller_terminal)
    if not handle:
        typer.echo(
            "Error: could not identify the caller (terminal or chat "
            "session) to act as guardian. Run from an Orca-managed "
            "terminal or chat, or pass --caller-terminal <handle>.",
            err=True,
        )
        raise typer.Exit(1)
    guardian = _guardian_ref(root, handle, pane_key)

    agents = list_agents(root)
    if agent_ids:
        targets = list(agent_ids)
    else:
        targets = [
            agent_id for agent_id, agent in agents.items()
            if agent.get("state") in GUARDABLE_STATES and not agent.get("guardian")
        ]
    try:
        ps_entries = _ps_by_pane(anyio.run(worktree_ps))
    except Exception as exc:
        emit_event(
            logs_dir(root),
            component="cli",
            event="guard_ps_failed",
            level="WARNING",
            message=str(exc),
        )
        ps_entries = {}

    guarded: list[dict] = []
    skipped: list[dict] = []
    for agent_id in targets:
        agent = agents.get(agent_id)
        if agent is None:
            skipped.append({"agent_id": agent_id, "reason": "not_found"})
            continue
        host_name = _row_host(agent)
        if host_name != host_seam.LOCAL_NAME:
            # Guard is local-only by spec: the guardian is a terminal on
            # this machine, and nothing here may read another host's ps.
            typer.echo(
                f"Error: agent '{agent_id}' runs on host '{host_name}'; "
                "guard is local-only and cannot monitor a remote pane",
                err=True,
            )
            skipped.append({"agent_id": agent_id, "reason": "remote_host"})
            continue
        own_pane = agent.get("pane_key") or agent.get("paneKey")
        if agent_id == guardian.get("agent_id") or (pane_key and own_pane == pane_key):
            skipped.append({"agent_id": agent_id, "reason": "self"})
            continue
        outcome, detail = _guard_one(root, agent, guardian, ps_entries)
        if outcome == "guarded":
            guarded.append({"agent_id": agent_id, "action": detail})
        else:
            skipped.append({"agent_id": agent_id, "reason": detail})

    typer.echo(json.dumps(
        {"guardian": guardian, "guarded": guarded, "skipped": skipped},
        indent=2, ensure_ascii=False,
    ))


# `revive <id>` needs one of these states and no live watcher.
REVIVABLE_STATES = frozenset({"lost", "done", "error", "enrolled"})
# A resumed omp must be up before `worker-start --terminal` can claim
# its pane: Orca refuses an empty shell (`agent_unconfigured`).
REVIVE_AGENT_START_TIMEOUT_SECS = 60.0
REVIVE_AGENT_POLL_SECS = 1.0


def _revive_blocker(agent: dict) -> str:
    """Why an agent cannot be revived now (session aside), or ""."""
    if (agent.get("support") or "full") != "full":
        return "unsupported_provider"
    pid = int(agent.get("watcher_pid") or 0)
    if pid and pid_is_running(pid):
        return "watcher_running"
    if agent.get("state") not in REVIVABLE_STATES:
        return "not_settled"
    return ""


async def _live_guardian(root: Path, guardian) -> dict | None:
    """The recorded guardian if it still resolves (handle refreshed by
    pane when stale), else None."""
    if not isinstance(guardian, dict):
        return None
    if guardian.get("agent_id"):
        try:
            read_agent(root, str(guardian["agent_id"]))
        except (FileNotFoundError, ValueError):
            return None
        return guardian
    if guardian.get("session"):
        # A structured chat has no terminal to probe, and mailing it
        # just to check liveness would inject a turn. Keep it.
        return guardian
    handle = str(guardian.get("terminal") or "")
    if not handle:
        return None
    try:
        await terminal_show(handle)
        return guardian
    except OrcaError as exc:
        if exc.code != "terminal_handle_stale":
            # Orca unreachable: nothing says the guardian is gone.
            return guardian
    rebound = await _refresh_stale_handle(root, guardian.get("pane_key") or None)
    if rebound is None:
        return None
    return {**guardian, "terminal": rebound}


async def _wait_agent_identity(
    handle: str, provider: str, timeout: float, host: Host | None = None,
) -> bool:
    """Wait until Orca reports `provider` running in the terminal."""
    host = host or host_seam.local_host()
    deadline = time.monotonic() + timeout
    while True:
        try:
            data = await terminal_show(handle, host=host)
        except OrcaError:
            data = {}
        identity = str(_unwrap_terminal(data).get("agentIdentity") or "")
        if identity.strip().lower() == provider:
            return True
        if time.monotonic() >= deadline:
            return False
        await anyio.sleep(REVIVE_AGENT_POLL_SECS)


# Shell executable name (from a shell-owned terminal title) -> quoting family.
_SHELL_FAMILIES = {
    "pwsh": "powershell",
    "powershell": "powershell",
    "cmd": "cmd",
    "bash": "posix",
    "zsh": "posix",
    "sh": "posix",
    "dash": "posix",
}


def _shell_from_title(title: str) -> str:
    """Quoting family of the shell a terminal title names, or "".

    An idle shell titles its console with its executable path (e.g.
    `D:\\...\\pwsh.exe`, `Administrator: C:\\...\\cmd.exe`); any other
    title (an agent's, a custom one) leaves the shell unknown.
    """
    name = re.split(r"[\\/]", str(title or "").strip())[-1].strip().lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return _SHELL_FAMILIES.get(name, "")


async def _confirm_empty_shell(
    root: Path, agent_id: str, provider: str, pane_key: str, args: list[str],
    terminals: list[dict], host: Host | None = None,
) -> tuple[str, str, str]:
    """(handle, quoted line, "") when the agent's old pane is a confirmed
    empty shell, else ("", "", reason).

    The pane must be listed with no `agentIdentity` and Jev must judge
    its rendered frame `shell_prompt`; a `worktree ps` row vetoes only
    while `working`. Its shell must be known so the command can be
    quoted for it. Jev off or degraded means unconfirmed. `host` is the
    machine the pane lives on: its ps and its screen are the ones that
    count.
    """
    host = host or host_seam.local_host()
    if not pane_key:
        return "", "", "no_pane_key"
    mode = resolve_mode(root)
    if mode == "off":
        return "", "", "jev_off"
    terminal = reclaim.pane_terminal(terminals, pane_key)
    if terminal is None:
        return "", "", "pane_gone"
    if terminal.get("agentIdentity"):
        return "", "", "agent_running"
    shell = _shell_from_title(terminal.get("title", ""))
    if not shell:
        return "", "", "unknown_shell"
    try:
        line = shell_command_line(args, shell)
    except ProviderError:
        return "", "", "unquotable"
    # ps can only veto: a cold restart restores the pre-restart `done`
    # row unchanged, so only a `working` row says an agent is on the pane.
    try:
        ps_entries = _ps_by_pane(await worktree_ps(host=host))
    except Exception:
        ps_entries = {}
    if (ps_entries.get(pane_key) or {}).get("state") == "working":
        return "", "", "ps_working"
    handle = str(terminal["handle"])
    try:
        frame = await terminal_read(handle, screen=True, host=host)
    except OrcaError:
        return "", "", "screen_read_failed"
    verdict, gate_reason = gate_verdict(provider, frame)
    if gate_reason == "no_usable_frame":
        return "", "", "no_screen_frame"
    answer = await ask_ready(root, agent_id, provider, frame, mode, verdict)
    if answer is None:
        return "", "", "jev_degraded"
    if answer.get("choice") != "shell_prompt":
        return "", "", f"jev_{answer.get('choice') or 'unknown'}"
    return handle, line, ""


def _pane_agent(terminals: list[dict], agent: dict) -> str:
    """Agent identity Orca reports running in the record's pane, or "".

    After a restart Orca may already have the agent back in its pane;
    resuming that session again would run it twice.
    """
    pane = str(agent.get("pane_key") or agent.get("paneKey") or "")
    terminal = reclaim.pane_terminal(terminals, pane)
    return str((terminal or {}).get("agentIdentity") or "")


def _release_summary(exc: OrcaError) -> str:
    """One line for a failed worker-release (its message is the receipt)."""
    payload = exc.payload if isinstance(exc.payload, dict) else {}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    state = str(result.get("state") or exc.code or "error")
    detail = str(result.get("lastError") or "")
    if not detail and not result:
        detail = exc.message
    return one_line(f"{state}: {detail}" if detail else state, 160)


def _revive_listing(root: Path) -> dict:
    revivable: list[dict] = []
    not_revivable: list[dict] = []
    try:
        terminals = anyio.run(terminal_list, f"path:{root}")
    except OrcaError:
        terminals = []  # listing only: no pane veto without Orca
    for agent_id, agent in list_agents(root).items():
        agent = _mark_lost_if_orphaned(root, agent)
        reason = _revive_blocker(agent)
        if not reason and _row_host(agent) == host_seam.LOCAL_NAME \
                and _pane_agent(terminals, agent):
            # The listing only knows this machine's panes; a remote pane
            # is checked when that host is actually addressed.
            reason = "agent_running"
        if not reason and not agent.get("session_id"):
            reason = "no_session"
        if reason:
            not_revivable.append({"agent_id": agent_id, "reason": reason})
        else:
            revivable.append({
                "agent_id": agent_id,
                "provider": agent.get("provider", ""),
                "state": agent.get("state", ""),
                "session_id": agent.get("session_id", ""),
            })
    return {"revivable": revivable, "not_revivable": not_revivable}


def _revive_fail(root: Path, agent: dict, message: str) -> None:
    """Persist a revived-but-unclaimed record as `enrolled`, then exit 1."""
    agent["state"] = "enrolled"
    agent.pop("resume_turn", None)
    agent["updated_at"] = now_iso()
    write_agent(root, agent)
    emit_event(
        logs_dir(root),
        component="cli",
        event="revive_failed",
        level="ERROR",
        agent_id=agent["agent_id"],
        terminal=agent.get("terminal", ""),
        message=message,
    )
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(1)


def _remote_session_unknown(
    root: Path, agent_id: str, host: Host, pane_key: str, message: str,
    code: str,
) -> None:
    """Name a remote cache failure and exit: never guess a resume."""
    emit_event(
        logs_dir(root),
        component="cli",
        event=host_seam.REMOTE_SESSION_UNKNOWN,
        level="ERROR",
        agent_id=agent_id,
        message=message,
        data={"host": host.name, "pane_key": pane_key, "code": code},
    )
    typer.echo(
        f"Error: {host_seam.REMOTE_SESSION_UNKNOWN}: {message}", err=True,
    )
    raise typer.Exit(1)


@app.command()
def revive(
    agent_id: str = typer.Argument(None, help="Agent id to revive (omit to list candidates)"),
    session: str = typer.Option(None, "--session", help="Provider session id to resume"),
    on: str = typer.Option(None, "--on", help="Registered host the agent runs on (default: the record's host)"),
) -> None:
    """List revivable agents, or resume one agent's provider session."""
    root = resolve_project_root()
    enable_file_logging(logs_dir(root))
    _read_state_or_exit(root)

    if not agent_id:
        if session or on:
            typer.echo("Error: --session and --on need an agent id", err=True)
            raise typer.Exit(1)
        typer.echo(json.dumps(_revive_listing(root), indent=2, ensure_ascii=False))
        return

    host, agent_id, agent = _agent_target(root, agent_id, on)
    agent = _mark_lost_if_orphaned(root, agent)
    blocker = _revive_blocker(agent)
    if blocker:
        state = agent.get("state", "")
        message = {
            "unsupported_provider": "is guard-only (unsupported provider)",
            "watcher_running": "still has a running watcher",
            "not_settled": f"is {state}; revive needs lost/done/error/enrolled",
        }[blocker]
        typer.echo(f"Error: agent '{agent_id}' {message}", err=True)
        raise typer.Exit(1)

    provider = str(agent.get("provider") or "")
    if not host.is_local and provider not in host.providers:
        # A paired server's shell need not resolve the provider from
        # PATH: `host doctor --provider` records the absolute path OSW
        # resumes with, so refusing here beats resuming nothing.
        typer.echo(
            f"Error: host '{host.name}' has no recorded path for provider "
            f"'{provider}'; run `osw host doctor {host.name} "
            f"--provider {provider}` first",
            err=True,
        )
        raise typer.Exit(1)

    try:
        terminals = _run_async(
            terminal_list, host.worktree_selector(str(root)), host=host,
        )
    except OrcaError as exc:
        typer.echo(
            f"Error: cannot list terminals to check the pane: {exc.message}",
            err=True,
        )
        raise typer.Exit(1)
    running = _pane_agent(terminals, agent)
    if running:
        typer.echo(
            f"Error: agent '{agent_id}' pane is already running {running}; "
            f"run `use {agent_id} <prompt>` instead of reviving it",
            err=True,
        )
        raise typer.Exit(1)

    pane_key = str(agent.get("pane_key") or agent.get("paneKey") or "")
    session_id = (session or "").strip() or str(agent.get("session_id") or "")
    if not session_id and pane_key:
        try:
            session_id = _run_async(
                session_for_agent, host, pane_key, root,
            )
        except HostError as exc:
            _remote_session_unknown(
                root, agent_id, host, pane_key, exc.message, exc.code,
            )
        if not session_id and not host.is_local:
            # A remote cache that holds no id for this pane is a
            # transport fact, not "no session yet".
            _remote_session_unknown(
                root, agent_id, host, pane_key,
                f"{host.name}: no provider session recorded for pane "
                f"{pane_key} in "
                f"{host.session_cache or 'its Orca agent-hook status cache'}",
                host_seam.REMOTE_SESSION_UNKNOWN,
            )
    if not session_id:
        typer.echo(
            f"Error: no session id for agent '{agent_id}'; "
            "pass --session <id>",
            err=True,
        )
        raise typer.Exit(1)

    # A paired server's non-interactive shell need not resolve the
    # provider from PATH the way this machine does; `host doctor` already
    # recorded the absolute path on the host record (`new` uses the same
    # lookup). Local revive keeps the bare provider name.
    binary = None if host.is_local else host.providers.get(provider)
    try:
        command = resume_command(
            provider, session_id,
            str(agent.get("model_name") or ""), str(agent.get("thinking") or ""),
            binary=binary,
        )
    except ProviderError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    guardian = anyio.run(_live_guardian, root, agent.get("guardian"))
    if guardian is None:
        executor, executor_pane = _executor_identity(None)
        if executor:
            guardian = _guardian_ref(root, executor, executor_pane)
        else:
            typer.echo(
                "WARNING: guardian unresolved and no caller terminal "
                "identified; notifications fall back to the recorded "
                "caller terminal.",
                err=True,
            )

    # In place first: the original pane, when it is confirmed to be an
    # empty shell, gets the resume command typed in and keeps its key.
    args = resume_args(
        provider, session_id,
        str(agent.get("model_name") or ""), str(agent.get("thinking") or ""),
        binary=binary,
    )
    handle, line, why_not = _run_async(
        _confirm_empty_shell, root, agent_id, provider, pane_key, args,
        terminals, host,
    )
    if handle:
        try:
            _run_async(terminal_send, handle, line, host=host)
        except OrcaError as exc:
            handle, why_not = "", f"send_failed: {exc.message}"
    if handle:
        placement = "in_place"
        command = line
    else:
        placement = "new_terminal"
        try:
            if host.is_local:
                result = _run_async(terminal_create, command, host=host)
            else:
                # A remote terminal needs an explicit worktree upstream:
                # `active`/`current` are refused for a paired server.
                result = _run_async(
                    terminal_create, command, host.worktree, host=host,
                )
        except OrcaError as exc:
            emit_event(
                logs_dir(root),
                component="cli",
                event="terminal_create_failed",
                level="ERROR",
                agent_id=agent_id,
                message=exc.message,
                data={"command": command, "host": host.name},
            )
            typer.echo(f"Error: {exc.message}", err=True)
            raise typer.Exit(1)
        handle = _terminal_handle(result)
        if not handle:
            typer.echo("Error: terminal create returned no handle", err=True)
            raise typer.Exit(1)
        terminal_obj = _unwrap_terminal(result)
        if not (terminal_obj.get("tabId") and terminal_obj.get("leafId")):
            # `terminal create` names no pane (Orca 1.4.221); ask for it.
            try:
                terminal_obj = _unwrap_terminal(
                    _run_async(terminal_show, handle, host=host)
                )
            except OrcaError:
                terminal_obj = {}
        tab = terminal_obj.get("tabId")
        leaf = terminal_obj.get("leafId")
        pane_key = f"{tab}:{leaf}" if tab and leaf else ""

    # Interrupted mid-task: the record says so (`lost`); the old pane's
    # ps state is not consulted, it can report a pre-restart "done".
    was_running = agent.get("state") == "lost"
    agent["terminal"] = handle
    if pane_key:
        agent["pane_key"] = pane_key
    else:
        agent.pop("pane_key", None)
    agent["session_id"] = session_id
    if guardian:
        agent["guardian"] = guardian
    agent.pop("watcher_pid", None)
    agent.pop("follow_turn", None)
    agent.pop("resume_turn", None)
    agent["updated_at"] = now_iso()
    emit_event(
        logs_dir(root),
        component="cli",
        event="agent_revived",
        agent_id=agent_id,
        terminal=handle,
        message=f"provider session resumed ({placement})",
        data={
            "command": command,
            "session_id": session_id,
            "was_running": was_running,
            "placement": placement,
            "in_place_refused": why_not,
        },
    )

    if agent.get("launch_mode") == "supervised" or supervised_agent_id(provider):
        # Old Dispatches name dead handles; release, then claim the new
        # terminal with a fresh one. Only an interrupted task gets a
        # Dispatch now: worker-start always injects a task spec, and a
        # later `use` claims an idle supervised terminal the same way.
        # A record from a provider the current Orca no longer supports
        # for supervised launches (see providers.SUPERVISED_PROVIDERS)
        # keeps nothing from that lane: the watcher runs it as a plain
        # agent instead of polling a Dispatch that will never settle.
        old_dispatch = str(agent.get("dispatch_id") or "")
        if old_dispatch:
            try:
                anyio.run(orchestration_worker_release, old_dispatch)
            except OrcaError as exc:
                log.warning(
                    "old dispatch %s not released (%s)",
                    old_dispatch, _release_summary(exc),
                )
        supervised_agent = supervised_agent_id(provider)
        if supervised_agent:
            agent.update({
                "launch_mode": "supervised",
                "orchestration_agent": supervised_agent,
                "run_id": "",
                "task_id": "",
                "dispatch_id": "",
                "handoff_path": "",
            })
        else:
            for field in ("launch_mode", "orchestration_agent", "run_id",
                          "task_id", "dispatch_id"):
                agent.pop(field, None)
        if was_running and supervised_agent:
            up = _run_async(
                _wait_agent_identity, handle, provider,
                REVIVE_AGENT_START_TIMEOUT_SECS, host,
            )
            if not up:
                _revive_fail(
                    root, agent,
                    f"resumed {provider} on terminal {handle} did not come "
                    f"up within {REVIVE_AGENT_START_TIMEOUT_SECS:.0f}s; "
                    "no Dispatch was started",
                )
            try:
                receipt = anyio.run(
                    _launch_supervised,
                    root, agent_id, provider, RESUME_TEMPLATE, "", handle,
                )
            except OrcaError as exc:
                anyio.run(_release_failed_supervised_launch, exc)
                _revive_fail(root, agent, exc.message)
            agent.update(_supervised_record_fields(receipt, provider))
            if receipt.get("terminal"):
                agent["terminal"] = str(receipt["terminal"])
            if receipt.get("pane_key"):
                agent["pane_key"] = str(receipt["pane_key"])

    if was_running:
        agent["resume_turn"] = True
        agent["state"] = "assigned"
        agent["phase"] = ""
        try:
            _start_watcher(root, agent)
        except OSError as exc:
            typer.echo(f"Error: failed to start watcher: {exc}", err=True)
            raise typer.Exit(1)
    else:
        agent["state"] = "enrolled"
        write_agent(root, agent)

    summary = {
        "agent_id": agent_id,
        "provider": provider,
        "terminal": agent["terminal"],
        "pane_key": agent.get("pane_key", ""),
        "session_id": session_id,
        "command": command,
        "state": agent["state"],
        "resume_turn": was_running,
        "guardian": agent.get("guardian"),
        "placement": placement,
    }
    if agent.get("launch_mode") == "supervised":
        summary["dispatch_id"] = agent.get("dispatch_id", "")
    typer.echo(json.dumps(summary, indent=2, ensure_ascii=False))


@app.command(name="del")
def del_(
    agent_id: str,
    close: bool = typer.Option(False, "--close", help="Also close the terminal"),
    on: str = typer.Option(None, "--on", help="Registered host the agent runs on (default: the record's host)"),
) -> None:
    """Remove an agent from management, optionally closing its terminal."""
    root = resolve_project_root()
    enable_file_logging(logs_dir(root))
    _read_state_or_exit(root)

    host, agent_id, agent = _agent_target(root, agent_id, on)

    emit_event(
        logs_dir(root),
        component="cli",
        event="delete_started",
        agent_id=agent_id,
        terminal=agent.get("terminal", ""),
        data={
            "close": close,
            "watcher_pid": agent.get("watcher_pid", 0),
            "host": host.name,
        },
    )
    pid = int(agent.get("watcher_pid") or 0)
    if pid:
        _terminate_pid(pid)

    if close:
        dispatch_id = str(agent.get("dispatch_id") or "")
        if agent.get("launch_mode") == "supervised" and dispatch_id:
            # Supervised cleanup goes through the orchestration verbs:
            # a settled worker's terminal is released, a running one is
            # fenced and stopped. `terminal close` stays the fallback
            # when the dispatch was already released or unknown.
            try:
                if agent.get("state") in ("done", "error"):
                    anyio.run(orchestration_worker_release, dispatch_id)
                else:
                    anyio.run(orchestration_worker_stop, dispatch_id)
                # A terminal OSW created itself (`revive`) is `external`
                # to Orca: release/stop detach it but leave it open.
                try:
                    anyio.run(_close_if_still_open, root, agent, host)
                except OrcaError as close_exc:
                    typer.echo(
                        f"Warning: failed to close terminal: {close_exc.message}"
                    )
            except OrcaError as exc:
                typer.echo(
                    f"Warning: orchestration cleanup failed: {exc.message}"
                )
                try:
                    anyio.run(_close_agent_terminal, root, agent, host)
                except OrcaError as close_exc:
                    typer.echo(
                        f"Warning: failed to close terminal: {close_exc.message}"
                    )
        else:
            try:
                anyio.run(_close_agent_terminal, root, agent, host)
            except OrcaError as exc:
                typer.echo(f"Warning: failed to close terminal: {exc.message}")

    delete_agent(root, agent_id)
    typer.echo(f"Removed {agent_id}")
    emit_event(
        logs_dir(root),
        component="cli",
        event="delete_finished",
        agent_id=agent_id,
        terminal=agent.get("terminal", ""),
        data={"host": host.name},
    )


@app.command()
def sweep(
    force: bool = typer.Option(
        False, "--force",
        help="Also reclaim panes kept for a failed turn (error/lost)",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON"),
) -> None:
    """Close the terminals of finished agents; records and sessions stay.

    A finished agent's pane holds nothing its record does not: `revive`
    resumes the provider session in a fresh one. `use`/`enroll` panes and
    supervised dispatch terminals are never touched (see `reclaim`).
    """
    root = resolve_project_root()
    enable_file_logging(logs_dir(root))
    _read_state_or_exit(root)

    closed: list[dict] = []
    kept: list[dict] = []
    terminals_by_host: dict[str, list[dict] | None] = {}
    list_error = ""
    for agent_id, record in sorted(list_agents(root).items()):
        agent = _mark_lost_if_orphaned(root, record)
        pid = int(agent.get("watcher_pid") or 0)
        if pid and pid_is_running(pid):
            # A live watcher is the only writer of its record and may still
            # be finishing its turn: never pull the pane out from under it.
            kept.append({"agent_id": agent_id, "reason": "watcher_running"})
            continue
        allowed, reason = reclaim.decision(agent, force=force)
        if not allowed:
            kept.append({"agent_id": agent_id, "reason": reason})
            continue
        try:
            host = host_seam.host_for_agent(root, agent)
        except HostError as exc:
            kept.append({"agent_id": agent_id, "reason": exc.code})
            continue
        name = _row_host(agent)
        if name not in terminals_by_host:
            try:
                terminals_by_host[name] = _run_async(
                    reclaim.list_open, root, host,
                )
            except OrcaError as exc:
                # One unreadable host never hides the others.
                list_error = exc.message
                terminals_by_host[name] = None
                emit_event(
                    logs_dir(root),
                    component="cli",
                    event="terminal_list_failed",
                    level="ERROR",
                    agent_id=agent_id,
                    message=exc.message,
                    data={"host": name},
                )
        terminals = terminals_by_host[name]
        if terminals is None:
            kept.append({
                "agent_id": agent_id, "reason": "list_failed",
                "detail": list_error,
            })
            continue
        try:
            was_closed = _run_async(
                reclaim.close_if_open, root, agent, host, terminals,
            )
        except OrcaError as exc:
            kept.append({
                "agent_id": agent_id, "reason": "close_failed",
                "detail": exc.message,
            })
            emit_event(
                logs_dir(root),
                component="cli",
                event="pane_reclaim_failed",
                level="WARNING",
                agent_id=agent_id,
                terminal=agent.get("terminal", ""),
                message=exc.message,
                data={"host": name},
            )
            continue
        if not was_closed:
            kept.append({"agent_id": agent_id, "reason": "pane_gone"})
            continue
        write_agent(root, {
            **agent,
            "pane_reclaim": "sweep",
            "pane_reclaimed_at": now_iso(),
        })
        closed.append({"agent_id": agent_id, "terminal": agent.get("terminal", "")})
        emit_event(
            logs_dir(root),
            component="cli",
            event="pane_reclaimed",
            agent_id=agent_id,
            terminal=agent.get("terminal", ""),
            data={"host": name, "by": "sweep"},
        )

    payload = {"closed": closed, "kept": kept}
    if json_output:
        typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    for entry in closed:
        typer.echo(f"closed {entry['agent_id']} {entry['terminal']}")
    for entry in kept:
        detail = entry.get("detail") or ""
        suffix = f": {detail}" if detail else ""
        typer.echo(f"kept   {entry['agent_id']} ({entry['reason']}{suffix})")
    typer.echo(
        f"Closed {len(closed)} panes, kept {len(kept)}. "
        f"Records stay: resume one with `osw revive <agent_id>`."
    )


# Interrupt key per provider (plan §1.8, research 2026-10-11 V1/V8):
# Ctrl-C stops claude, codex, kimi, grok and dsh (DSH-TUI: "Interrupt
# while working", verified aborting a running tool call); pi and omp
# ignore it and stop only on a bare Esc. Esc is not an option for dsh:
# its ladder first clears the draft or a selection.
STOP_KEYS = {
    "claude": "interrupt",
    "codex": "interrupt",
    "kimi": "interrupt",
    "grok": "interrupt",
    "dsh": "interrupt",
    "pi": "esc",
    "omp": "esc",
}


async def _send_stop_key(
    root: Path, agent: dict, key: str, host: Host | None = None,
) -> str:
    """Send the interrupt key; a stale handle is re-resolved once by
    pane. Returns the handle the key went to."""
    host = host or host_seam.local_host()
    send = terminal_interrupt if key == "interrupt" else terminal_send_escape
    handle = str(agent.get("terminal") or "")
    try:
        await send(handle, host=host)
        return handle
    except OrcaError as exc:
        if exc.code != "terminal_handle_stale":
            raise
        rebound = await _refresh_stale_handle(root, agent.get("pane_key"), host)
        if rebound is None:
            raise
    emit_event(
        logs_dir(root),
        component="cli",
        event="terminal_rebound",
        agent_id=str(agent.get("agent_id") or ""),
        terminal=rebound,
        message="stale handle re-resolved by pane identity",
        data={"previous": handle, "current": rebound},
    )
    await send(rebound, host=host)
    return rebound


@app.command()
def stop(
    agent_id: str,
    on: str = typer.Option(None, "--on", help="Registered host the agent runs on (default: the record's host)"),
) -> None:
    """Interrupt an agent's current turn."""
    root = resolve_project_root()
    enable_file_logging(logs_dir(root))
    _read_state_or_exit(root)

    host, agent_id, agent = _agent_target(root, agent_id, on)
    provider = str(agent.get("provider") or "")
    key = STOP_KEYS.get(provider)
    if key is None:
        typer.echo(
            f"Error: agent '{agent_id}' has unknown provider "
            f"'{provider}'; no interrupt key"
        )
        raise typer.Exit(1)

    pid = int(agent.get("watcher_pid") or 0)
    watching = bool(pid) and pid_is_running(pid)
    pane_key = str(agent.get("pane_key") or "")
    # Capture the session before the interrupt, while the hook cache
    # still has it. A live watcher owns the record and records the
    # session itself when it finalizes. A cache that cannot be read is
    # named but never blocks the interrupt: stopping matters more.
    session_id = ""
    if pane_key:
        try:
            session_id = _run_async(
                session_for_agent, host, pane_key, root,
            )
        except HostError as exc:
            emit_event(
                logs_dir(root),
                component="cli",
                event="session_lookup_failed",
                level="WARNING",
                agent_id=agent_id,
                message=exc.message,
                data={"host": host.name, "pane_key": pane_key,
                      "code": exc.code},
            )
    if session_id and not watching:
        agent["session_id"] = session_id
        agent["updated_at"] = now_iso()
        write_agent(root, agent)

    try:
        handle = anyio.run(_send_stop_key, root, agent, key, host)
    except OrcaError as exc:
        emit_event(
            logs_dir(root),
            component="cli",
            event="stop_send_failed",
            level="ERROR",
            agent_id=agent_id,
            terminal=str(agent.get("terminal") or ""),
            message=exc.message,
            data={"key": key, "code": exc.code},
        )
        typer.echo(f"Error: {exc.message}")
        raise typer.Exit(1)

    # Only after the key landed: a failed send leaves no stop request
    # behind. The watcher needs two polls to accept a done, so the
    # request is in place before it could close the turn as completed.
    if watching:
        write_stop_request(root, agent_id)
    emit_event(
        logs_dir(root),
        component="cli",
        event="stop_sent",
        agent_id=agent_id,
        terminal=handle,
        data={"key": key, "watcher": watching},
    )
    typer.echo(json.dumps(
        {"agent_id": agent_id, "key": key, "watcher": watching},
        ensure_ascii=False,
    ))


@app.command(name="list")
def list_(
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON"),
    all_hosts: bool = typer.Option(
        False, "--all",
        help="Include agents on every registered host, with a HOST column",
    ),
) -> None:
    """List managed agents."""
    root = resolve_project_root()
    enable_file_logging(logs_dir(root))
    _read_state_or_exit(root)

    try:
        agents = _agent_rows(root, all_hosts=all_hosts)
    except HostError as exc:
        typer.echo(f"Error: {exc.message}")
        raise typer.Exit(1)
    if json_output:
        typer.echo(json.dumps(agents, indent=2))
        return

    if not agents:
        typer.echo("No agents.")
        return

    host_column = "HOST       " if all_hosts else ""
    typer.echo(
        f"{'AGENT_ID':<12} {host_column}{'STATE':<12} {'ORCA':<10} "
        f"{'WATCHER':<8} {'TERMINAL':<14} PROMPT"
    )
    for agent_id, agent in agents.items():
        prompt = agent.get("prompt") or ""
        if len(prompt) > 40:
            prompt = prompt[:37] + "..."
        watcher = "yes" if agent.get("watcher_running") else "no"
        host_cell = f"{agent.get('host', ''):<11}" if all_hosts else ""
        typer.echo(
            f"{agent_id:<12} {host_cell}{agent.get('state', ''):<12} "
            f"{agent.get('orca_state', '') or '-':<10} {watcher:<8} "
            f"{agent.get('terminal', ''):<14} {prompt}"
        )


@app.command()
def logs(
    agent_id: str = typer.Option(None, "--agent", help="Only show logs for one agent id"),
    tail: int = typer.Option(50, "--tail", "-n", min=0, help="Lines/events to show"),
) -> None:
    """Show OSW log files and recent structured events."""
    root = resolve_project_root()
    _read_state_or_exit(root)
    directory = logs_dir(root)
    directory.mkdir(parents=True, exist_ok=True)

    typer.echo(f"Log dir: {directory}")
    typer.echo(f"Events: {events_file(directory)}")

    pattern = f"{agent_id}_*.log*" if agent_id else "*.log*"
    files = sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime)
    typer.echo("Files:")
    if files:
        for path in files:
            typer.echo(f"  {path}")
    else:
        typer.echo("  none")

    events = read_events(directory, agent_id=agent_id, tail=tail)
    typer.echo("Recent events:")
    if events:
        for event in events:
            typer.echo(f"  {format_event_line(event)}")
    else:
        typer.echo("  none")

    if files and tail:
        latest = files[-1]
        typer.echo(f"Tail: {latest}")
        for line in _tail_text(latest, tail):
            typer.echo(f"  {line}")


@app.command()
def status(
    all_hosts: bool = typer.Option(
        False, "--all", help="Summarise agents and watchers per host",
    ),
) -> None:
    """Show OSW state for the current directory."""
    root = resolve_project_root()
    enable_file_logging(logs_dir(root))
    _read_state_or_exit(root)

    typer.echo(f"State file: {state_file(root)}")
    try:
        agents = _agent_rows(root, all_hosts=all_hosts)
    except HostError as exc:
        typer.echo(f"Error: {exc.message}")
        raise typer.Exit(1)
    if not agents:
        typer.echo("Agents: none")
        return

    if all_hosts:
        _status_by_host(agents)
        return

    counts: dict[str, int] = {}
    running_watchers = 0
    for agent in agents.values():
        agent_state = agent.get("state", "unknown")
        counts[agent_state] = counts.get(agent_state, 0) + 1
        if agent.get("watcher_running"):
            running_watchers += 1

    typer.echo("Agents:")
    for agent_state, count in sorted(counts.items()):
        typer.echo(f"  {agent_state}: {count}")
    typer.echo(f"Watchers running: {running_watchers}")


def _status_by_host(agents: dict[str, dict]) -> None:
    """Agent counts and running watchers, one line per host."""
    per_host: dict[str, dict[str, int]] = {}
    watchers: dict[str, int] = {}
    unknown: set[str] = set()
    for agent in agents.values():
        host = str(agent.get("host") or host_seam.LOCAL_NAME)
        counts = per_host.setdefault(host, {})
        state = agent.get("state", "unknown")
        counts[state] = counts.get(state, 0) + 1
        if agent.get("watcher_running"):
            watchers[host] = watchers.get(host, 0) + 1
        if agent.get("orca_state") == "unknown":
            unknown.add(host)
    typer.echo("Agents by host:")
    total = 0
    for host in sorted(per_host):
        counts = per_host[host]
        total += sum(counts.values())
        detail = ", ".join(f"{state} {count}" for state, count in sorted(counts.items()))
        note = "; orca state unknown" if host in unknown else ""
        typer.echo(
            f"  {host}: {sum(counts.values())} ({detail}); "
            f"watchers running: {watchers.get(host, 0)}{note}"
        )
    typer.echo(f"Total agents: {total}")
    typer.echo(f"Watchers running: {sum(watchers.values())}")


@app.command(hidden=True)
def watch(root: Path, agent_id: str) -> None:
    """Internal entry point for a detached per-agent watcher."""
    watcher_main(root.resolve(), agent_id)
