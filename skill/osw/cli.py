from __future__ import annotations

import json
from pathlib import Path

import anyio
import typer

from osw.server import run_server
from osw.state import (
    init_state_dir,
    is_serve_running,
    read_result,
    read_state,
    resolve_project_root,
    state_file,
    write_request,
)

app = typer.Typer(help="OSW — Orca Agent Supervisor")

RESULT_TIMEOUT = 15.0


def require_serve(root: Path) -> None:
    """Abort with a helpful message if the OSW supervisor is not running."""
    if not is_serve_running(root):
        typer.echo("OSW serve is not running for this directory.", err=True)
        typer.echo("Start it with:", err=True)
        typer.echo("  python osw.py serve", err=True)
        raise typer.Exit(1)


def _handle_result(result: dict | None, on_ok) -> None:
    """Shared result handling for new/use/all/del: timeout, error, or success."""
    if result is None:
        typer.echo(
            "OSW serve did not return a result within 15 seconds.",
            err=True,
        )
        typer.echo("Check python osw.py status.", err=True)
        raise typer.Exit(1)

    if result.get("ok"):
        on_ok(result)
    else:
        typer.echo(f"Error: {result.get('error', 'unknown error')}", err=True)
        raise typer.Exit(1)


@app.command()
def init() -> None:
    """Initialize OSW state for the current directory."""
    root = resolve_project_root()
    init_state_dir(root)
    typer.echo(f"Initialized OSW state at {state_file(root)}")


@app.command()
def serve() -> None:
    """Run the foreground supervisor for the current directory."""
    root = resolve_project_root()
    anyio.run(run_server, root)


@app.command()
def new(
    prompt: str,
    caller_terminal: str = typer.Option(None, "--caller-terminal", help="Terminal handle to receive completion reports"),
) -> None:
    """Create a new agent terminal and send it a task prompt."""
    root = resolve_project_root()
    require_serve(root)

    payload: dict = {"prompt": prompt}
    if caller_terminal:
        payload["caller_terminal"] = caller_terminal
    request_id = write_request(root, "new", payload)
    result = read_result(root, request_id, timeout=RESULT_TIMEOUT)

    def on_ok(result: dict) -> None:
        typer.echo(
            f"Created {result.get('agent_id')} on terminal {result.get('terminal')}"
        )

    _handle_result(result, on_ok)


@app.command()
def use(
    prompt: str,
    terminal: str = typer.Option(..., "--terminal", help="Terminal handle to adopt"),
    caller_terminal: str = typer.Option(None, "--caller-terminal", help="Terminal handle to receive completion reports"),
) -> None:
    """Adopt an already-running terminal as a managed agent."""
    root = resolve_project_root()
    require_serve(root)

    payload: dict = {"terminal": terminal, "prompt": prompt}
    if caller_terminal:
        payload["caller_terminal"] = caller_terminal
    request_id = write_request(root, "use", payload)
    result = read_result(root, request_id, timeout=RESULT_TIMEOUT)

    def on_ok(result: dict) -> None:
        typer.echo(
            f"Adopted {result.get('agent_id')} on terminal {result.get('terminal')}"
        )

    _handle_result(result, on_ok)


@app.command(name="all")
def all_(message: str) -> None:
    """Broadcast a message to every managed agent."""
    root = resolve_project_root()
    require_serve(root)

    request_id = write_request(root, "all", {"message": message})
    result = read_result(root, request_id, timeout=RESULT_TIMEOUT)

    def on_ok(result: dict) -> None:
        sent = result.get("sent", [])
        errors = result.get("errors", [])
        typer.echo(f"Sent to {len(sent)} agent(s): {', '.join(sent)}")
        if errors:
            typer.echo(f"Errors: {errors}", err=True)

    _handle_result(result, on_ok)


@app.command(name="del")
def del_(
    agent_id: str,
    close: bool = typer.Option(False, "--close", help="Also close the terminal"),
) -> None:
    """Remove an agent from management, optionally closing its terminal."""
    root = resolve_project_root()
    require_serve(root)

    request_id = write_request(root, "del", {"agent_id": agent_id, "close": close})
    result = read_result(root, request_id, timeout=RESULT_TIMEOUT)

    def on_ok(result: dict) -> None:
        typer.echo(f"Removed {result.get('agent_id')}")

    _handle_result(result, on_ok)


@app.command(name="list")
def list_(
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON"),
) -> None:
    """List managed agents."""
    root = resolve_project_root()
    try:
        state = read_state(root)
    except FileNotFoundError:
        typer.echo("Not initialized. Run `python osw.py init` first.", err=True)
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
    try:
        state = read_state(root)
    except FileNotFoundError:
        typer.echo("Not initialized. Run `python osw.py init` first.", err=True)
        raise typer.Exit(1)

    running = is_serve_running(root)
    typer.echo(f"Serve: {'running' if running else 'not running'}")
    typer.echo(f"State file: {state_file(root)}")

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
