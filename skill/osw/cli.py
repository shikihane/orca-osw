from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import anyio
import typer

from osw.log import enable_file_logging, get_logger, setup_logging
from osw.orca_cli import detect_current_terminal
from osw.server import run_server
from osw.state import (
    init_state_dir,
    is_serve_running,
    logs_dir,
    read_result,
    read_state,
    resolve_project_root,
    state_file,
    write_request,
)

log = get_logger("cli")

app = typer.Typer(help="OSW — Orca Agent Supervisor")

RESULT_TIMEOUT = 15.0


@app.callback()
def main(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
) -> None:
    setup_logging(verbose=verbose)


def require_serve(root: Path) -> None:
    if not is_serve_running(root):
        log.error("supervisor is not running for %s", root)
        typer.echo("OSW serve is not running for this directory.")
        typer.echo("Start it with:")
        typer.echo("  python osw.py serve")
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


def _handle_result(result: dict | None, on_ok) -> None:
    if result is None:
        log.error("supervisor did not respond within %.0fs", RESULT_TIMEOUT)
        typer.echo("OSW serve did not return a result within 15 seconds.")
        typer.echo("Check python osw.py status.")
        raise typer.Exit(1)

    if result.get("ok"):
        on_ok(result)
    else:
        error_msg = result.get("error") or "unknown error"
        log.error("command failed: %s", error_msg)
        typer.echo(f"Error: {error_msg}")
        raise typer.Exit(1)


@app.command()
def init() -> None:
    """Initialize OSW state for the current directory."""
    root = resolve_project_root()
    init_state_dir(root)
    log.info("initialized state at %s", state_file(root))
    typer.echo(f"Initialized OSW state at {state_file(root)}")


@app.command()
def serve() -> None:
    """Run the foreground supervisor for the current directory."""
    root = resolve_project_root()
    log_path = enable_file_logging(logs_dir(root))
    typer.echo(f"OSW supervisor starting for {root}")
    typer.echo(f"Log file: {log_path}")
    typer.echo("Press Ctrl+C to stop.")
    try:
        anyio.run(run_server, root)
    except KeyboardInterrupt:
        pass
    typer.echo("OSW supervisor stopped.")


@app.command()
def new(
    prompt: str,
    caller_terminal: str = typer.Option(None, "--caller-terminal", help="Terminal handle to receive completion reports"),
) -> None:
    """Create a new agent terminal and send it a task prompt."""
    root = resolve_project_root()
    enable_file_logging(logs_dir(root))
    require_serve(root)

    if not caller_terminal:
        caller_terminal = _detect_caller_terminal()

    log.info("sending 'new' request  prompt=%s", prompt[:60])
    payload: dict = {"prompt": prompt}
    if caller_terminal:
        payload["caller_terminal"] = caller_terminal
    request_id = write_request(root, "new", payload)
    log.debug("request_id=%s, waiting for result...", request_id)
    result = read_result(root, request_id, timeout=RESULT_TIMEOUT)

    def on_ok(r: dict) -> None:
        agent_id = r.get("agent_id")
        terminal = r.get("terminal")
        log.info("agent created  %s -> %s", agent_id, terminal)
        typer.echo(f"Created {agent_id} on terminal {terminal}")

    _handle_result(result, on_ok)


@app.command()
def use(
    prompt: str,
    terminal: str = typer.Option(..., "--terminal", help="Terminal handle to adopt"),
    caller_terminal: str = typer.Option(None, "--caller-terminal", help="Terminal handle to receive completion reports"),
) -> None:
    """Adopt an already-running terminal as a managed agent."""
    root = resolve_project_root()
    enable_file_logging(logs_dir(root))
    require_serve(root)

    if not caller_terminal:
        caller_terminal = _detect_caller_terminal()

    log.info("sending 'use' request  terminal=%s", terminal)
    payload: dict = {"terminal": terminal, "prompt": prompt}
    if caller_terminal:
        payload["caller_terminal"] = caller_terminal
    request_id = write_request(root, "use", payload)
    log.debug("request_id=%s, waiting for result...", request_id)
    result = read_result(root, request_id, timeout=RESULT_TIMEOUT)

    def on_ok(r: dict) -> None:
        agent_id = r.get("agent_id")
        term = r.get("terminal")
        log.info("agent adopted  %s -> %s", agent_id, term)
        typer.echo(f"Adopted {agent_id} on terminal {term}")

    _handle_result(result, on_ok)


@app.command(name="all")
def all_(message: str) -> None:
    """Broadcast a message to every managed agent."""
    root = resolve_project_root()
    enable_file_logging(logs_dir(root))
    require_serve(root)

    log.info("sending 'all' request  msg=%s", message[:60])
    request_id = write_request(root, "all", {"message": message})
    result = read_result(root, request_id, timeout=RESULT_TIMEOUT)

    def on_ok(result: dict) -> None:
        sent = result.get("sent", [])
        errors = result.get("errors", [])
        log.info("broadcast complete  sent=%d  errors=%d", len(sent), len(errors))
        typer.echo(f"Sent to {len(sent)} agent(s): {', '.join(sent)}")
        if errors:
            for err in errors:
                log.warning("broadcast error: %s", err)
            typer.echo(f"Errors: {errors}")

    _handle_result(result, on_ok)


@app.command(name="del")
def del_(
    agent_id: str,
    close: bool = typer.Option(False, "--close", help="Also close the terminal"),
) -> None:
    """Remove an agent from management, optionally closing its terminal."""
    root = resolve_project_root()
    enable_file_logging(logs_dir(root))
    require_serve(root)

    log.info("sending 'del' request  agent_id=%s  close=%s", agent_id, close)
    request_id = write_request(root, "del", {"agent_id": agent_id, "close": close})
    result = read_result(root, request_id, timeout=RESULT_TIMEOUT)

    def on_ok(result: dict) -> None:
        aid = result.get("agent_id")
        log.info("agent removed: %s", aid)
        typer.echo(f"Removed {aid}")

    _handle_result(result, on_ok)


@app.command(name="list")
def list_(
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON"),
) -> None:
    """List managed agents."""
    root = resolve_project_root()
    enable_file_logging(logs_dir(root))
    try:
        state = read_state(root)
    except FileNotFoundError:
        typer.echo("Not initialized. Run `python osw.py init` first.")
        raise typer.Exit(1)

    agents = state.get("agents", {})

    if json_output:
        typer.echo(json.dumps(agents, indent=2))
        return

    if not agents:
        typer.echo("No agents.")
        return

    typer.echo(f"{'AGENT_ID':<12} {'STATE':<16} {'TERMINAL':<14} {'CALLER':<14} LAST_PROMPT")
    for agent_id, agent in agents.items():
        last_prompt = agent.get("last_prompt") or ""
        if len(last_prompt) > 40:
            last_prompt = last_prompt[:37] + "..."
        typer.echo(
            f"{agent_id:<12} {agent.get('state', ''):<16} "
            f"{agent.get('terminal', ''):<14} {agent.get('caller_terminal') or '-':<14} "
            f"{last_prompt}"
        )


@app.command()
def status() -> None:
    """Show supervisor status for the current directory."""
    root = resolve_project_root()
    enable_file_logging(logs_dir(root))
    try:
        state = read_state(root)
    except FileNotFoundError:
        typer.echo("Not initialized. Run `python osw.py init` first.")
        raise typer.Exit(1)

    running = is_serve_running(root)
    typer.echo(f"Serve: {'running' if running else 'not running'}")
    typer.echo(f"State file: {state_file(root)}")

    serve_info = state.get("serve")
    if serve_info:
        typer.echo(f"PID: {serve_info.get('pid')}")
        typer.echo(f"Started: {serve_info.get('started_at')}")

    counts: dict[str, int] = {}
    for agent in state.get("agents", {}).values():
        agent_state = agent.get("state", "unknown")
        counts[agent_state] = counts.get(agent_state, 0) + 1

    if counts:
        typer.echo("Agents:")
        for agent_state, count in sorted(counts.items()):
            typer.echo(f"  {agent_state}: {count}")
    else:
        typer.echo("Agents: none")

    errors = state.get("errors", [])
    if errors:
        typer.echo("Recent errors:")
        for error in errors[-5:]:
            typer.echo(f"  {error}")
