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
    detect_current_terminal,
    terminal_close,
    terminal_create,
    terminal_send,
    terminal_show,
    worktree_ps,
)
from osw.process import hidden_subprocess_kwargs
from osw.providers import probe_variants, scan_agent_clis
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
model_app = typer.Typer(help="Manage the model tier configuration")
app.add_typer(model_app, name="model")

TIERS = ("strong", "medium", "weak")


@app.callback()
def main(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
) -> None:
    setup_logging(verbose=verbose)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _maybe_detect_caller(caller_terminal: str | None) -> str | None:
    if caller_terminal:
        return caller_terminal
    if sys.stdin.isatty() and sys.stdout.isatty():
        return _detect_caller_terminal()
    return None


def _resolve_model(
    state: dict, tier: str, model_name: str | None
) -> tuple[dict | None, str]:
    models = state.get("models", {})
    if model_name:
        for t in TIERS:
            for entry in models.get(t) or []:
                if entry.get("name") == model_name:
                    return entry, ""
        return None, f"model '{model_name}' not found in models config"

    if tier not in TIERS:
        return None, f"unknown tier '{tier}' (expected strong, medium, or weak)"

    search_order = [tier] + [t for t in TIERS if t != tier]
    for t in search_order:
        entries = models.get(t) or []
        if entries:
            return entries[0], ""
    return None, "models config is empty"


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


def _script_path() -> Path:
    return Path(__file__).resolve().parents[1] / "osw.py"


def _quote_command_arg(value: str) -> str:
    return subprocess.list2cmdline([value])


def _append_prompt(command: str, prompt: str) -> str:
    return f"{command} {_quote_command_arg(prompt)}"


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
    provider_command: str = "",
    model_name: str = "",
    task_started_on_launch: bool = False,
) -> dict:
    now = now_iso()
    return {
        "agent_id": agent_id,
        "terminal": terminal,
        "worktree_path": str(root),
        "provider_command": provider_command,
        "model_name": model_name,
        "task_started_on_launch": task_started_on_launch,
        "state": "assigned",
        "phase": "",
        "caller_terminal": caller_terminal,
        "prompt": prompt,
        "created_at": now,
        "updated_at": now,
    }


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
        help="Force the guided prompt setup on or off. Default: on when "
             "run from a real terminal, off when stdio is piped (agents).",
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

    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()

    if interactive and found:
        _interactive_tier_setup(root, found)
        return

    if found:
        typer.echo("")
        typer.echo("Inspect provider models with:")
        typer.echo("  python osw.py models claude")
        typer.echo("  python osw.py models codex")
        typer.echo("  python osw.py models pi")


def _pick_variant(cli: dict, variants: list[dict]) -> dict | None:
    """Second-level pick from discovered variants (never free-typed)."""
    shown = variants
    if len(variants) > 20:
        keyword = typer.prompt("filter (optional)", default="").strip().lower()
        if keyword:
            shown = [v for v in variants if keyword in v["name"].lower()] or variants
        shown = shown[:30]
    typer.echo(f"variants for {cli['name']}:")
    for i, opt in enumerate(shown, 1):
        typer.echo(f"  {i}. {opt['name']:<28} {opt['command']}")
    typer.echo("  b. back (skip this tier)")
    while True:
        choice = typer.prompt("select", default="1").strip().lower()
        if choice == "b":
            return None
        try:
            index = int(choice)
        except ValueError:
            typer.echo("  invalid choice, try again")
            continue
        if 1 <= index <= len(shown):
            return dict(shown[index - 1])
        typer.echo("  invalid choice, try again")


def _interactive_tier_setup(root: Path, found: list[dict]) -> None:
    """Guided tier assignment: pick an agent, then one of its variants."""
    state = read_state(root)
    used_names: set[str] = set()
    variant_cache: dict[str, list[dict]] = {}

    for tier in TIERS:
        typer.echo("")
        typer.echo(f"[{tier}] choose an agent:")
        for i, cli in enumerate(found, 1):
            typer.echo(f"  {i}. {cli['name']}")
        typer.echo("  s. skip this tier")

        cli = None
        while cli is None:
            choice = typer.prompt("select", default="s").strip().lower()
            if choice == "s":
                break
            try:
                index = int(choice) - 1
                if 0 <= index < len(found):
                    cli = found[index]
            except ValueError:
                pass
            if cli is None:
                typer.echo("  invalid choice, try again")

        if cli is None:
            typer.echo(f"  {tier}: skipped")
            continue

        if cli["name"] not in variant_cache:
            typer.echo(f"  probing {cli['name']} for model variants...")
            variant_cache[cli["name"]] = probe_variants(cli["name"], cli["path"])
        entry = _pick_variant(cli, variant_cache[cli["name"]])
        if entry is None:
            typer.echo(f"  {tier}: skipped")
            continue

        if entry["name"] in used_names:
            entry["name"] = f"{entry['name']}-{tier}"
        used_names.add(entry["name"])
        state["models"][tier].append(entry)
        typer.echo(f"  {tier}: {entry['name']} -> {entry['command']}")

    write_state(root, state)
    typer.echo("")
    typer.echo("Saved. Review with: python osw.py model list")


@model_app.command("scan")
def model_scan(
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON"),
) -> None:
    """Scan PATH for known agent CLIs."""
    found = scan_agent_clis()
    if json_output:
        typer.echo(json.dumps(found, indent=2))
        return
    if not found:
        typer.echo("No known agent CLIs found on PATH.")
        return
    for entry in found:
        version = f"  ({entry['version']})" if entry["version"] else ""
        typer.echo(f"{entry['name']:<14} {entry['path']}{version}")


@model_app.command("variants")
def model_variants(
    cli_name: str = typer.Argument(..., help="Agent CLI to query (e.g. pi, claude)"),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON"),
) -> None:
    """Discover model variants by querying the CLI itself."""
    variants = probe_variants(cli_name)
    if json_output:
        typer.echo(json.dumps(variants, indent=2))
        return
    if not variants:
        typer.echo(f"No discoverable variants for '{cli_name}'."
                   f" Check `{cli_name} --help` for its model flags.")
        return
    for opt in variants:
        typer.echo(f"{opt['name']:<28} {opt['command']}")


@model_app.command("list")
def model_list(
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON"),
) -> None:
    """Show the configured model tiers."""
    root = resolve_project_root()
    state = _read_state_or_exit(root)
    models = state.get("models", {})
    if json_output:
        typer.echo(json.dumps(models, indent=2))
        return
    empty = True
    for tier in TIERS:
        for entry in models.get(tier) or []:
            empty = False
            typer.echo(f"{tier:<8} {entry.get('name', '?'):<20} {entry.get('command', '')}")
    if empty:
        typer.echo("No models configured. Add one with `model add`.")


@model_app.command("add")
def model_add(
    tier: str = typer.Option(..., "--tier", "-t", help="Tier: strong, medium, or weak"),
    name: str = typer.Option(..., "--name", "-n", help="Unique entry name"),
    command: str = typer.Option(..., "--command", "-c", help="Command line to launch the agent"),
) -> None:
    """Add a model entry to a tier."""
    if tier not in TIERS:
        typer.echo(f"Error: unknown tier '{tier}' (expected strong, medium, or weak)")
        raise typer.Exit(1)

    root = resolve_project_root()
    state = _read_state_or_exit(root)
    models = state.setdefault("models", {})
    for t in TIERS:
        for entry in models.get(t) or []:
            if entry.get("name") == name:
                typer.echo(f"Error: model '{name}' already exists in tier '{t}'")
                raise typer.Exit(1)

    models.setdefault(tier, []).append({"name": name, "command": command})
    write_state(root, state)
    log.info("model added  tier=%s name=%s command=%s", tier, name, command)
    typer.echo(f"Added {name} to {tier}: {command}")


@model_app.command("remove")
def model_remove(
    name: str = typer.Argument(..., help="Model entry name to remove"),
) -> None:
    """Remove a model entry by name."""
    root = resolve_project_root()
    state = _read_state_or_exit(root)
    models = state.get("models", {})
    for tier in TIERS:
        entries = models.get(tier) or []
        for entry in entries:
            if entry.get("name") == name:
                entries.remove(entry)
                write_state(root, state)
                log.info("model removed  tier=%s name=%s", tier, name)
                typer.echo(f"Removed {name} from {tier}")
                return
    typer.echo(f"Error: model '{name}' not found")
    raise typer.Exit(1)


@app.command()
def new(
    prompt: str,
    tier: str = typer.Option("medium", "--tier", "-t", help="Model tier: strong, medium, or weak"),
    model: str = typer.Option(None, "--model", "-m", help="Specific model entry name (overrides --tier)"),
    caller_terminal: str = typer.Option(None, "--caller-terminal", help="Terminal handle to receive completion reports"),
) -> None:
    """Create a new agent terminal and return after starting its watcher."""
    root = resolve_project_root()
    enable_file_logging(logs_dir(root))
    state = _read_state_or_exit(root)
    emit_event(
        logs_dir(root),
        component="cli",
        event="new_started",
        message="creating agent terminal",
        data={"tier": tier, "model": model or ""},
    )

    entry, error = _resolve_model(state, tier, model)
    if entry is None:
        typer.echo(f"Error: {error}")
        raise typer.Exit(1)

    command = entry.get("command", "")
    launch_command = _append_prompt(command, prompt)
    try:
        result = anyio.run(terminal_create, launch_command)
    except OrcaError as exc:
        emit_event(
            logs_dir(root),
            component="cli",
            event="terminal_create_failed",
            level="ERROR",
            message=exc.message,
            data={"command": launch_command},
        )
        typer.echo(f"Error: {exc.message}")
        raise typer.Exit(1)

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

    agent_id = alloc_agent_id(root)
    agent = _base_agent(
        root,
        agent_id,
        handle,
        prompt,
        _maybe_detect_caller(caller_terminal),
        provider_command=command,
        model_name=entry.get("name", ""),
        task_started_on_launch=True,
    )

    try:
        _start_watcher(root, agent)
    except OSError as exc:
        typer.echo(f"Error: failed to start watcher: {exc}")
        raise typer.Exit(1)

    typer.echo(
        f"Created {agent_id} on terminal {handle} "
        f"(model: {entry.get('name', '?')})"
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
    prompt: str,
    terminal: str = typer.Option(..., "--terminal", help="Terminal handle to adopt"),
    caller_terminal: str = typer.Option(None, "--caller-terminal", help="Terminal handle to receive completion reports"),
) -> None:
    """Adopt an already-running terminal as a managed agent."""
    root = resolve_project_root()
    enable_file_logging(logs_dir(root))
    _read_state_or_exit(root)
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
    agent_id = alloc_agent_id(root)
    agent = _base_agent(
        root,
        agent_id,
        handle,
        prompt,
        _maybe_detect_caller(caller_terminal),
    )

    try:
        _start_watcher(root, agent)
    except OSError as exc:
        typer.echo(f"Error: failed to start watcher: {exc}")
        raise typer.Exit(1)

    typer.echo(f"Adopted {agent_id} on terminal {handle}")
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
