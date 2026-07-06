from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import anyio
import anyio.abc

from osw.handoff import (
    FORCED_HANDOFF_PROMPT,
    extract_handoff_filename,
    format_completion_report,
    format_handoff_failed_message,
)
from osw.orca_cli import (
    OrcaError,
    terminal_close,
    terminal_create,
    terminal_info,
    terminal_list,
    terminal_send,
    terminal_wait,
)
from osw.state import (
    inbox_dir,
    next_agent_id,
    read_state,
    write_result,
    write_state,
)


def now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ServerContext:
    """Shared context passed to every coroutine running inside the supervisor."""

    root: Path
    state: dict
    watchers: dict[str, anyio.CancelScope] = field(default_factory=dict)
    lock: anyio.Lock = field(default_factory=anyio.Lock)
    task_group: anyio.abc.TaskGroup | None = None


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

async def run_server(root: Path) -> None:
    """Start the AnyIO supervisor: inbox_loop + reconcile_loop + dynamic watchers."""
    state = read_state(root)
    state["serve"] = {
        "pid": os.getpid(),
        "started_at": now_iso(),
    }
    write_state(root, state)

    ctx = ServerContext(root=root, state=state)

    try:
        async with anyio.create_task_group() as tg:
            ctx.task_group = tg
            tg.start_soon(inbox_loop, ctx)
            tg.start_soon(reconcile_loop, ctx)
    finally:
        ctx.state["serve"] = None
        write_state(root, ctx.state)


# ---------------------------------------------------------------------------
# Inbox polling
# ---------------------------------------------------------------------------

async def inbox_loop(ctx: ServerContext) -> None:
    """Poll *inbox/* for ``*.json`` request files, dispatch each by command."""
    while True:
        inbox = inbox_dir(ctx.root)
        if inbox.exists():
            for path in sorted(inbox.glob("*.json")):
                try:
                    with path.open("r", encoding="utf-8") as f:
                        request = json.load(f)
                    path.unlink()
                except (json.JSONDecodeError, OSError):
                    continue

                command = request.get("command")
                if command == "new":
                    await handle_new(ctx, request)
                elif command == "use":
                    await handle_use(ctx, request)
                elif command == "all":
                    await handle_all(ctx, request)
                elif command == "del":
                    await handle_del(ctx, request)

        await anyio.sleep(0.5)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def handle_new(ctx: ServerContext, request: dict) -> None:
    """Create a brand-new terminal, register the agent, send the prompt."""
    command = ctx.state["models"]["strong"][0]["command"]
    prompt = request.get("prompt", "")

    try:
        result = await terminal_create(str(ctx.root), command)
    except OrcaError as exc:
        write_result(ctx.root, request["request_id"], {
            "ok": False,
            "error": str(exc),
        })
        return

    handle = result.get("handle", result.get("terminal", ""))

    async with ctx.lock:
        agent_id = next_agent_id(ctx.state)
        now = now_iso()
        ctx.state["agents"][agent_id] = {
            "agent_id": agent_id,
            "terminal": handle,
            "worktree_path": str(ctx.root),
            "provider_command": command,
            "state": "assigned",
            "caller_terminal": request.get("caller_terminal"),
            "last_prompt": prompt,
            "last_handoff_file": None,
            "created_at": now,
            "updated_at": now,
        }
        write_state(ctx.root, ctx.state)

    if prompt:
        try:
            await terminal_send(handle, prompt)
        except OrcaError:
            pass

    write_result(ctx.root, request["request_id"], {
        "ok": True,
        "agent_id": agent_id,
        "terminal": handle,
        "worktree_path": str(ctx.root),
        "state": "assigned",
    })

    ctx.task_group.start_soon(watcher, ctx, agent_id)


async def handle_use(ctx: ServerContext, request: dict) -> None:
    """Adopt an already-running terminal as a managed agent."""
    handle = request.get("terminal", "")
    prompt = request.get("prompt", "")

    try:
        info = await terminal_info(handle)
    except OrcaError as exc:
        write_result(ctx.root, request["request_id"], {
            "ok": False,
            "error": str(exc),
        })
        return

    worktree_path = info.get("worktreePath", "")
    if worktree_path != str(ctx.root):
        write_result(ctx.root, request["request_id"], {
            "ok": False,
            "error": (
                f"Terminal worktree '{worktree_path}' "
                f"does not match project root '{ctx.root}'"
            ),
        })
        return

    async with ctx.lock:
        agent_id = next_agent_id(ctx.state)
        now = now_iso()
        ctx.state["agents"][agent_id] = {
            "agent_id": agent_id,
            "terminal": handle,
            "worktree_path": worktree_path,
            "provider_command": request.get("command", ""),
            "state": "assigned",
            "caller_terminal": request.get("caller_terminal"),
            "last_prompt": prompt,
            "last_handoff_file": None,
            "created_at": now,
            "updated_at": now,
        }
        write_state(ctx.root, ctx.state)

    if prompt:
        try:
            await terminal_send(handle, prompt)
        except OrcaError:
            pass

    write_result(ctx.root, request["request_id"], {
        "ok": True,
        "agent_id": agent_id,
        "terminal": handle,
        "worktree_path": worktree_path,
        "state": "assigned",
    })

    ctx.task_group.start_soon(watcher, ctx, agent_id)


async def handle_all(ctx: ServerContext, request: dict) -> None:
    """Broadcast a message to every agent whose state is not ``lost``."""
    message = request.get("message", "")
    sent: list[str] = []
    errors: list[dict] = []

    for agent_id, agent in ctx.state.get("agents", {}).items():
        if agent.get("state") == "lost":
            continue
        try:
            await terminal_send(agent["terminal"], message)
            sent.append(agent_id)
        except OrcaError as exc:
            errors.append({"agent_id": agent_id, "error": str(exc)})

    write_result(ctx.root, request["request_id"], {
        "ok": True,
        "sent": sent,
        "errors": errors,
    })


async def handle_del(ctx: ServerContext, request: dict) -> None:
    """Remove an agent, optionally closing its terminal."""
    agent_id = request.get("agent_id", "")

    # Cancel watcher if one is running for this agent.
    scope = ctx.watchers.get(agent_id)
    if scope is not None:
        scope.cancel()

    # Close terminal if the caller requested it.
    agent = ctx.state.get("agents", {}).get(agent_id)
    if agent and request.get("close"):
        try:
            await terminal_close(agent["terminal"])
        except OrcaError:
            pass

    # Remove agent from state.
    async with ctx.lock:
        ctx.state.get("agents", {}).pop(agent_id, None)
        write_state(ctx.root, ctx.state)

    write_result(ctx.root, request["request_id"], {
        "ok": True,
        "agent_id": agent_id,
    })


# ---------------------------------------------------------------------------
# Per-agent watcher
# ---------------------------------------------------------------------------

async def watcher(ctx: ServerContext, agent_id: str) -> None:
    """Watch a single agent: wait for idle, trigger handoff, report result."""
    scope = anyio.CancelScope()
    ctx.watchers[agent_id] = scope

    try:
        with scope:
            agent = ctx.state["agents"].get(agent_id)
            if agent is None:
                return

            handle = agent["terminal"]

            # ---- Phase 1: wait for the agent to become idle ----
            try:
                await terminal_wait(handle, "tui-idle", timeout_ms=600_000)
            except OrcaError:
                async with ctx.lock:
                    if agent_id in ctx.state["agents"]:
                        ctx.state["agents"][agent_id]["state"] = "error"
                        ctx.state["agents"][agent_id]["updated_at"] = now_iso()
                        write_state(ctx.root, ctx.state)
                return

            async with ctx.lock:
                if agent_id in ctx.state["agents"]:
                    ctx.state["agents"][agent_id]["state"] = "idle"
                    ctx.state["agents"][agent_id]["updated_at"] = now_iso()
                    write_state(ctx.root, ctx.state)

            # ---- Phase 2: send forced handoff prompt ----
            try:
                await terminal_send(handle, FORCED_HANDOFF_PROMPT)
            except OrcaError:
                async with ctx.lock:
                    if agent_id in ctx.state["agents"]:
                        ctx.state["agents"][agent_id]["state"] = "error"
                        ctx.state["agents"][agent_id]["updated_at"] = now_iso()
                        write_state(ctx.root, ctx.state)
                return

            # ---- Phase 3: wait for idle again (shorter timeout) ----
            try:
                wait_result = await terminal_wait(
                    handle, "tui-idle", timeout_ms=120_000,
                )
            except OrcaError:
                async with ctx.lock:
                    if agent_id in ctx.state["agents"]:
                        ctx.state["agents"][agent_id]["state"] = "error"
                        ctx.state["agents"][agent_id]["updated_at"] = now_iso()
                        write_state(ctx.root, ctx.state)
                return

            # ---- Phase 4: extract handoff filename ----
            output = ""
            if isinstance(wait_result, dict):
                output = wait_result.get("output", wait_result.get("text", ""))
                if not output:
                    output = json.dumps(wait_result)

            filename = extract_handoff_filename(output)
            caller_terminal = (
                ctx.state["agents"].get(agent_id, {}).get("caller_terminal")
            )

            if filename:
                async with ctx.lock:
                    if agent_id in ctx.state["agents"]:
                        ctx.state["agents"][agent_id]["state"] = "done"
                        ctx.state["agents"][agent_id]["last_handoff_file"] = filename
                        ctx.state["agents"][agent_id]["updated_at"] = now_iso()
                        write_state(ctx.root, ctx.state)

                if caller_terminal:
                    report = format_completion_report(agent_id, handle, filename)
                    try:
                        await terminal_send(caller_terminal, report)
                    except OrcaError:
                        pass
            else:
                async with ctx.lock:
                    if agent_id in ctx.state["agents"]:
                        ctx.state["agents"][agent_id]["state"] = "handoff_failed"
                        ctx.state["agents"][agent_id]["updated_at"] = now_iso()
                        write_state(ctx.root, ctx.state)

                if caller_terminal:
                    msg = format_handoff_failed_message()
                    try:
                        await terminal_send(caller_terminal, msg)
                    except OrcaError:
                        pass
    finally:
        ctx.watchers.pop(agent_id, None)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

async def reconcile_loop(ctx: ServerContext) -> None:
    """Periodically check that every agent's terminal is still alive."""
    while True:
        await anyio.sleep(30)

        try:
            terminals = await terminal_list(str(ctx.root))
        except OrcaError:
            continue

        live_handles = {
            t.get("handle", t.get("terminal", "")) for t in terminals
        }

        async with ctx.lock:
            for agent_id, agent in list(ctx.state.get("agents", {}).items()):
                if agent.get("state") == "lost":
                    continue
                if agent["terminal"] not in live_handles:
                    ctx.state["agents"][agent_id]["state"] = "lost"
                    ctx.state["agents"][agent_id]["updated_at"] = now_iso()
            write_state(ctx.root, ctx.state)
