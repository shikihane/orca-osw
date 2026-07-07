from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

import anyio
import typer

from osw.log import enable_file_logging, get_logger, setup_logging
from osw.orca_cli import detect_current_terminal
from osw.providers import probe_variants, scan_agent_clis
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
    write_state,
)

log = get_logger("cli")

app = typer.Typer(help="OSW — Orca Agent Supervisor")
model_app = typer.Typer(help="Manage the model tier configuration")
app.add_typer(model_app, name="model")

RESULT_TIMEOUT = 15.0
TIERS = ("strong", "medium", "weak")


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
        # A human at a keyboard has a real terminal on both ends; agent
        # harnesses run commands with piped stdio.
        interactive = sys.stdin.isatty() and sys.stdout.isatty()

    if interactive and found:
        _interactive_tier_setup(root, found)
        return

    typer.echo("")
    typer.echo("Model tiers are EMPTY. Assign models before using `new`:")
    typer.echo("  discover variants:  python osw.py model variants <cli>")
    typer.echo('  assign a tier:      python osw.py model add --tier <strong|medium|weak>'
               ' --name <name> --command "<command>"')
    typer.echo("  verify:             python osw.py model list")
    typer.echo("(Humans: run `init -i` for a guided setup.)")


def _pick_variant(cli: dict, variants: list[dict]) -> dict | None:
    """Second-level pick: a discovered variant or a hand-typed command."""
    if variants:
        shown = variants
        if len(variants) > 20:
            keyword = typer.prompt("filter (optional)", default="").strip().lower()
            if keyword:
                shown = [v for v in variants if keyword in v["name"].lower()] or variants
            shown = shown[:30]
        typer.echo(f"variants for {cli['name']}:")
        for i, opt in enumerate(shown, 1):
            typer.echo(f"  {i}. {opt['name']:<28} {opt['command']}")
        typer.echo("  0. custom command")
        while True:
            choice = typer.prompt("select", default="0").strip()
            try:
                index = int(choice)
            except ValueError:
                typer.echo("  invalid choice, try again")
                continue
            if index == 0:
                break
            if 1 <= index <= len(shown):
                return dict(shown[index - 1])
            typer.echo("  invalid choice, try again")
    else:
        typer.echo(f"no discoverable variants for {cli['name']};"
                   " enter the command yourself")

    command = typer.prompt("command", default=cli["name"]).strip()
    if not command:
        return None
    name = typer.prompt("entry name", default=command.split()[0]).strip()
    return {"name": name, "command": command} if name else None


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


# ---------------------------------------------------------------------------
# model subcommands
# ---------------------------------------------------------------------------

def _read_state_or_exit(root):
    try:
        return read_state(root)
    except FileNotFoundError:
        typer.echo("Not initialized. Run `python osw.py init` first.")
        raise typer.Exit(1)


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
    tier: str = typer.Option("medium", "--tier", "-t", help="Model tier: strong, medium, or weak"),
    model: str = typer.Option(None, "--model", "-m", help="Specific model entry name (overrides --tier)"),
    caller_terminal: str = typer.Option(None, "--caller-terminal", help="Terminal handle to receive completion reports"),
) -> None:
    """Create a new agent terminal and send it a task prompt."""
    root = resolve_project_root()
    enable_file_logging(logs_dir(root))
    require_serve(root)

    if tier not in ("strong", "medium", "weak"):
        typer.echo(f"Error: unknown tier '{tier}' (expected strong, medium, or weak)")
        raise typer.Exit(1)

    if not caller_terminal:
        caller_terminal = _detect_caller_terminal()

    log.info("sending 'new' request  tier=%s model=%s prompt=%s", tier, model or "-", prompt[:60])
    payload: dict = {"prompt": prompt, "tier": tier}
    if model:
        payload["model"] = model
    if caller_terminal:
        payload["caller_terminal"] = caller_terminal
    request_id = write_request(root, "new", payload)
    log.debug("request_id=%s, waiting for result...", request_id)
    result = read_result(root, request_id, timeout=RESULT_TIMEOUT)

    def on_ok(r: dict) -> None:
        agent_id = r.get("agent_id")
        terminal = r.get("terminal")
        log.info("agent created  %s -> %s  model=%s", agent_id, terminal, r.get("model", ""))
        typer.echo(f"Created {agent_id} on terminal {terminal} (model: {r.get('model', '?')})")

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
