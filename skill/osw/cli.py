from __future__ import annotations

from collections import deque
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

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
from osw.orca_cli import (
    OrcaError,
    compatibility_report,
    detect_current_terminal,
    terminal_close,
    terminal_create,
    terminal_send,
    terminal_show,
    worktree_ps,
)
from osw.process import hidden_subprocess_kwargs
from osw.providers import (
    ProviderError,
    build_provider_command,
    ensure_workspace_trust,
    probe_variants,
    scan_agent_clis,
)
from osw.state import (
    alloc_agent_id,
    delete_agent,
    init_state_dir,
    list_agents,
    logs_dir,
    pid_is_running,
    read_agent,
    read_state,
    resolve_project_root,
    state_file,
    write_agent,
    write_state,
)
from osw.watcher import main as watcher_main

log = get_logger("cli")

app = typer.Typer(help="OSW - Orca Agent Supervisor")


@app.callback()
def main(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
) -> None:
    setup_logging(verbose=verbose)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    """Return ORCA_TERMINAL_HANDLE if Orca still accepts it.

    The handle is inherited by every orca subcommand; once the hosting
    terminal is gone, Orca rejects it with ``terminal_handle_stale`` and
    every osw command fails. Drop it from the environment and fall back
    to marker detection instead of erroring out.
    """
    env_handle = os.environ.get("ORCA_TERMINAL_HANDLE", "").strip()
    if not env_handle:
        return None
    try:
        anyio.run(terminal_show, env_handle)
    except OrcaError as exc:
        if exc.code != "terminal_handle_stale":
            # Orca unreachable or a transient failure: keep the handle and
            # let later commands surface the real error.
            return env_handle
        log.warning(
            "ORCA_TERMINAL_HANDLE=%s is stale, ignoring it", env_handle
        )
        typer.echo(
            f"WARNING: ORCA_TERMINAL_HANDLE ({env_handle}) is stale; "
            "ignoring it and falling back to caller detection.",
            err=True,
        )
        os.environ.pop("ORCA_TERMINAL_HANDLE", None)
        return None
    log.info("caller from ORCA_TERMINAL_HANDLE  terminal=%s", env_handle)
    return env_handle


def _maybe_detect_caller(caller_terminal: str | None) -> str | None:
    if caller_terminal:
        return caller_terminal
    # Orca exports the hosting terminal's handle into every terminal it
    # creates; child processes inherit it, so this works even when osw
    # runs deep inside an agent's tool pipeline where stdout is a pipe
    # and the marker-in-preview trick below cannot work.
    env_handle = _validate_env_caller()
    if env_handle:
        return env_handle
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
            "WARNING: no caller terminal identified; completion will NOT "
            "be reported. Poll `osw.py status` instead.",
            err=True,
        )
        return None
    typer.echo(
        "Error: could not identify the caller terminal, so the completion "
        "notification would be lost. Run from an Orca-managed terminal, "
        "pass --caller-terminal <handle>, or pass --no-notify to proceed "
        "without notifications.",
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


def _base_agent(
    root: Path,
    agent_id: str,
    terminal: str,
    prompt: str,
    caller_terminal: str | None,
    provider: str = "",
    provider_command: str = "",
    model_name: str = "",
    thinking: str = "",
    terminal_info: dict | None = None,
) -> dict:
    now = now_iso()
    agent = {
        "agent_id": agent_id,
        "terminal": terminal,
        "worktree_path": str(root),
        "provider": provider,
        "provider_command": provider_command,
        "model_name": model_name,
        "thinking": thinking,
        "state": "assigned",
        "phase": "",
        "caller_terminal": caller_terminal,
        "prompt": prompt,
        "created_at": now,
        "updated_at": now,
    }
    terminal_info = terminal_info or {}
    tab = terminal_info.get("tabId")
    leaf = terminal_info.get("leafId")
    if tab and leaf:
        agent["pane_key"] = f"{tab}:{leaf}"
    return agent


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


def _agent_rows(root: Path) -> dict[str, dict]:
    agents = list_agents(root)
    try:
        ps_entries = _ps_by_pane(anyio.run(worktree_ps))
    except Exception as exc:
        log.warning("worktree ps failed: %s", exc)
        ps_entries = {}

    rows: dict[str, dict] = {}
    for agent_id, agent in agents.items():
        row = dict(agent)
        pid = int(row.get("watcher_pid") or 0)
        row["watcher_running"] = pid_is_running(pid) if pid else False
        pane = row.get("pane_key") or row.get("paneKey")
        ps_entry = ps_entries.get(pane) if pane else None
        if ps_entry:
            row["orca_state"] = ps_entry.get("state", "")
            row["orca_prompt"] = ps_entry.get("prompt", "")
            row["tool_name"] = ps_entry.get("toolName", "")
            row["last_assistant_message"] = ps_entry.get("lastAssistantMessage", "")
        else:
            row.setdefault("orca_state", "")
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


@app.command("models")
def models(
    provider: str = typer.Argument(
        ...,
        help="Provider to query: claude, codex, pi, omp, or kimi",
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


@app.command()
def new(
    provider: str,
    prompt: str,
    model: str = typer.Option(None, "--model", "-m", help="Provider model value passed through unchanged"),
    thinking: str = typer.Option(None, "--thinking", help="Provider thinking/effort value passed through unchanged"),
    prefix: str = typer.Option(None, "--prefix", help="Agent id prefix, e.g. research, code, test, debug, review, misc"),
    caller_terminal: str = typer.Option(None, "--caller-terminal", help="Terminal handle to receive completion reports"),
    no_notify: bool = typer.Option(False, "--no-notify", help="Proceed without a caller terminal; completion will not be reported"),
) -> None:
    """Create a new provider agent terminal and return after starting its watcher."""
    root = resolve_project_root()
    enable_file_logging(logs_dir(root))
    _read_state_or_exit(root)
    emit_event(
        logs_dir(root),
        component="cli",
        event="new_started",
        message="creating agent terminal",
        data={"provider": provider, "model": model or "", "thinking": thinking or ""},
    )

    try:
        provider_command = build_provider_command(provider, model, thinking)
    except ProviderError as exc:
        typer.echo(f"Error: {exc}")
        raise typer.Exit(1)

    # Resolve the caller before creating anything: a dispatch without a
    # notification target is rejected, and must not leave an orphan
    # terminal behind.
    caller = _resolve_caller(caller_terminal, no_notify)

    # Pre-record workspace trust so the provider TUI does not block on
    # its interactive "trust this directory?" prompt in a fresh worktree.
    trust_note = ensure_workspace_trust(provider, root)
    if trust_note:
        emit_event(
            logs_dir(root),
            component="cli",
            event="workspace_trust",
            message=trust_note,
            data={"provider": provider, "root": str(root)},
        )

    # The terminal starts the bare provider TUI only; the watcher sends
    # the task prompt as its own observable turn once the TUI is ready.
    try:
        result = anyio.run(terminal_create, provider_command)
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

    agent_id = alloc_agent_id(root, prefix=prefix)
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
    )

    try:
        _start_watcher(root, agent)
    except OSError as exc:
        typer.echo(f"Error: failed to start watcher: {exc}")
        raise typer.Exit(1)

    typer.echo(
        f"Created {agent_id} on terminal {handle} "
        f"(provider: {provider})"
    )
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
) -> None:
    """Adopt an already-running terminal as a managed agent."""
    root = resolve_project_root()
    enable_file_logging(logs_dir(root))
    _read_state_or_exit(root)
    terminal, existing_agent = _resolve_terminal_arg(root, target)
    if existing_agent and prefix:
        typer.echo(
            "Error: --prefix is only valid when adopting a terminal handle, "
            "not an existing agent id"
        )
        raise typer.Exit(1)
    if existing_agent and existing_agent.get("state") not in ("done", "error", "lost"):
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
        data = anyio.run(terminal_show, terminal)
    except OrcaError as exc:
        emit_event(
            logs_dir(root),
            component="cli",
            event="terminal_show_failed",
            level="ERROR",
            terminal=terminal,
            message=exc.message,
        )
        typer.echo(f"Error: {exc.message}")
        raise typer.Exit(1)

    terminal_obj = _unwrap_terminal(data)
    worktree_path = _terminal_worktree_path(terminal_obj)
    if worktree_path and Path(worktree_path).resolve() != root:
        emit_event(
            logs_dir(root),
            component="cli",
            event="terminal_worktree_mismatch",
            level="ERROR",
            terminal=terminal,
            data={"terminal_worktree": worktree_path, "root": str(root)},
        )
        typer.echo(
            f"Error: terminal worktree '{worktree_path}' does not match project root '{root}'"
        )
        raise typer.Exit(1)

    handle = _terminal_handle(data, fallback=terminal)
    agent_id = (
        str(existing_agent.get("agent_id"))
        if existing_agent
        else alloc_agent_id(root, prefix=prefix)
    )
    agent = _base_agent(
        root,
        agent_id,
        handle,
        prompt,
        _resolve_caller(caller_terminal, no_notify),
        provider=str((existing_agent or {}).get("provider") or ""),
        provider_command=str((existing_agent or {}).get("provider_command") or ""),
        model_name=str((existing_agent or {}).get("model_name") or ""),
        thinking=str((existing_agent or {}).get("thinking") or ""),
        terminal_info=terminal_obj,
    )

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


@app.command(name="all")
def all_(message: str) -> None:
    """Broadcast a message to every currently managed, unfinished agent."""
    root = resolve_project_root()
    enable_file_logging(logs_dir(root))
    _read_state_or_exit(root)
    emit_event(
        logs_dir(root),
        component="cli",
        event="broadcast_started",
        message="broadcasting to unfinished agents",
        data={"chars": len(message)},
    )

    sent: list[str] = []
    errors: list[dict] = []
    for agent_id, agent in list_agents(root).items():
        if agent.get("state") in ("done", "error", "lost"):
            continue
        try:
            anyio.run(terminal_send, agent["terminal"], message)
            sent.append(agent_id)
            emit_event(
                logs_dir(root),
                component="cli",
                event="broadcast_sent",
                agent_id=agent_id,
                terminal=agent["terminal"],
            )
        except OrcaError as exc:
            errors.append({"agent_id": agent_id, "error": exc.message})
            emit_event(
                logs_dir(root),
                component="cli",
                event="broadcast_failed",
                level="ERROR",
                agent_id=agent_id,
                terminal=agent.get("terminal", ""),
                message=exc.message,
            )

    typer.echo(f"Sent to {len(sent)} agent(s): {', '.join(sent)}")
    if errors:
        typer.echo(f"Errors: {errors}")
    emit_event(
        logs_dir(root),
        component="cli",
        event="broadcast_finished",
        data={"sent": sent, "errors": errors},
    )


@app.command(name="del")
def del_(
    agent_id: str,
    close: bool = typer.Option(False, "--close", help="Also close the terminal"),
) -> None:
    """Remove an agent from management, optionally closing its terminal."""
    root = resolve_project_root()
    enable_file_logging(logs_dir(root))
    _read_state_or_exit(root)

    try:
        agent = read_agent(root, agent_id)
    except FileNotFoundError:
        emit_event(
            logs_dir(root),
            component="cli",
            event="delete_missing_agent",
            level="ERROR",
            agent_id=agent_id,
        )
        typer.echo(f"Error: agent '{agent_id}' not found")
        raise typer.Exit(1)

    emit_event(
        logs_dir(root),
        component="cli",
        event="delete_started",
        agent_id=agent_id,
        terminal=agent.get("terminal", ""),
        data={"close": close, "watcher_pid": agent.get("watcher_pid", 0)},
    )
    pid = int(agent.get("watcher_pid") or 0)
    if pid:
        _terminate_pid(pid)

    if close:
        try:
            anyio.run(terminal_close, agent["terminal"])
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
    )


@app.command(name="list")
def list_(
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON"),
) -> None:
    """List managed agents."""
    root = resolve_project_root()
    enable_file_logging(logs_dir(root))
    _read_state_or_exit(root)

    agents = _agent_rows(root)
    if json_output:
        typer.echo(json.dumps(agents, indent=2))
        return

    if not agents:
        typer.echo("No agents.")
        return

    typer.echo(f"{'AGENT_ID':<12} {'STATE':<12} {'ORCA':<10} {'WATCHER':<8} {'TERMINAL':<14} PROMPT")
    for agent_id, agent in agents.items():
        prompt = agent.get("prompt") or ""
        if len(prompt) > 40:
            prompt = prompt[:37] + "..."
        watcher = "yes" if agent.get("watcher_running") else "no"
        typer.echo(
            f"{agent_id:<12} {agent.get('state', ''):<12} "
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
def status() -> None:
    """Show OSW state for the current directory."""
    root = resolve_project_root()
    enable_file_logging(logs_dir(root))
    _read_state_or_exit(root)

    typer.echo(f"State file: {state_file(root)}")
    agents = _agent_rows(root)
    if not agents:
        typer.echo("Agents: none")
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


@app.command(hidden=True)
def watch(root: Path, agent_id: str) -> None:
    """Internal entry point for a detached per-agent watcher."""
    watcher_main(root.resolve(), agent_id)
