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
from osw.log import get_logger
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

log = get_logger("server")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ServerContext:
    root: Path
    state: dict
    watchers: dict[str, anyio.CancelScope] = field(default_factory=dict)
    lock: anyio.Lock = field(default_factory=anyio.Lock)
    task_group: anyio.abc.TaskGroup | None = None


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

async def run_server(root: Path) -> None:
    state = read_state(root)
    state["serve"] = {
        "pid": os.getpid(),
        "started_at": now_iso(),
    }
    write_state(root, state)

    ctx = ServerContext(root=root, state=state)
    log.info("supervisor started  pid=%d  root=%s", os.getpid(), root)
    log.info("state: %s", root / ".orca" / "osw" / "state.json")
    log.info("inbox polling every 0.5s, reconciliation every 30s")

    try:
        async with anyio.create_task_group() as tg:
            ctx.task_group = tg
            tg.start_soon(inbox_loop, ctx)
            tg.start_soon(reconcile_loop, ctx)
    except BaseException as exc:
        log.error("supervisor crashed: %s", exc)
        raise
    finally:
        ctx.state["serve"] = None
        write_state(root, ctx.state)
        log.info("supervisor stopped, state cleaned up")


# ---------------------------------------------------------------------------
# Inbox polling
# ---------------------------------------------------------------------------

async def inbox_loop(ctx: ServerContext) -> None:
    log.debug("inbox_loop started")
    while True:
        inbox = inbox_dir(ctx.root)
        if inbox.exists():
            for path in sorted(inbox.glob("*.json")):
                try:
                    with path.open("r", encoding="utf-8") as f:
                        request = json.load(f)
                    path.unlink()
                except (json.JSONDecodeError, OSError) as exc:
                    log.warning("skipping malformed request %s: %s", path.name, exc)
                    continue

                command = request.get("command")
                req_id = request.get("request_id", "?")
                log.info(
                    "[bold cyan]inbox[/bold cyan] command=%s  request_id=%s",
                    command, req_id,
                )

                if command == "new":
                    await handle_new(ctx, request)
                elif command == "use":
                    await handle_use(ctx, request)
                elif command == "all":
                    await handle_all(ctx, request)
                elif command == "del":
                    await handle_del(ctx, request)
                else:
                    log.warning("unknown command: %s", command)

        await anyio.sleep(0.5)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def handle_new(ctx: ServerContext, request: dict) -> None:
    command = ctx.state["models"]["strong"][0]["command"]
    prompt = request.get("prompt", "")
    log.info("new: creating terminal  provider=%s  prompt=%s", command, _trunc(prompt))

    try:
        result = await terminal_create(str(ctx.root), command)
    except OrcaError as exc:
        log.error("new: terminal_create failed: %s", exc)
        write_result(ctx.root, request["request_id"], {
            "ok": False,
            "error": str(exc),
        })
        return

    handle = result.get("handle", result.get("terminal", ""))
    log.info("new: terminal created  handle=%s", handle)

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
            log.info("new: prompt sent to %s", handle)
        except OrcaError as exc:
            log.warning("new: failed to send prompt to %s: %s", handle, exc)

    write_result(ctx.root, request["request_id"], {
        "ok": True,
        "agent_id": agent_id,
        "terminal": handle,
        "worktree_path": str(ctx.root),
        "state": "assigned",
    })

    log.info(
        "[bold green]new: agent registered[/bold green]  %s → %s",
        agent_id, handle,
    )
    ctx.task_group.start_soon(watcher, ctx, agent_id)


async def handle_use(ctx: ServerContext, request: dict) -> None:
    handle = request.get("terminal", "")
    prompt = request.get("prompt", "")
    log.info("use: adopting terminal=%s  prompt=%s", handle, _trunc(prompt))

    try:
        info = await terminal_info(handle)
    except OrcaError as exc:
        log.error("use: terminal_info failed for %s: %s", handle, exc)
        write_result(ctx.root, request["request_id"], {
            "ok": False,
            "error": str(exc),
        })
        return

    worktree_path = info.get("worktreePath", "")
    if worktree_path != str(ctx.root):
        log.warning(
            "use: worktree mismatch  terminal=%s  expected=%s  got=%s",
            handle, ctx.root, worktree_path,
        )
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
            "provider_command": "",
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
            log.info("use: prompt sent to %s", handle)
        except OrcaError as exc:
            log.warning("use: failed to send prompt to %s: %s", handle, exc)

    write_result(ctx.root, request["request_id"], {
        "ok": True,
        "agent_id": agent_id,
        "terminal": handle,
        "worktree_path": worktree_path,
        "state": "assigned",
    })

    log.info(
        "[bold green]use: agent registered[/bold green]  %s → %s",
        agent_id, handle,
    )
    ctx.task_group.start_soon(watcher, ctx, agent_id)


async def handle_all(ctx: ServerContext, request: dict) -> None:
    message = request.get("message", "")
    agents = ctx.state.get("agents", {})
    log.info("all: broadcasting to %d agent(s)  msg=%s", len(agents), _trunc(message))

    sent: list[str] = []
    errors: list[dict] = []

    for agent_id, agent in agents.items():
        if agent.get("state") == "lost":
            log.debug("all: skipping lost agent %s", agent_id)
            continue
        try:
            await terminal_send(agent["terminal"], message)
            sent.append(agent_id)
            log.debug("all: sent to %s (%s)", agent_id, agent["terminal"])
        except OrcaError as exc:
            errors.append({"agent_id": agent_id, "error": str(exc)})
            log.warning("all: failed to send to %s: %s", agent_id, exc)

    log.info("all: sent=%d  errors=%d", len(sent), len(errors))
    write_result(ctx.root, request["request_id"], {
        "ok": True,
        "sent": sent,
        "errors": errors,
    })


async def handle_del(ctx: ServerContext, request: dict) -> None:
    agent_id = request.get("agent_id", "")
    close = request.get("close", False)
    log.info("del: removing %s  close=%s", agent_id, close)

    scope = ctx.watchers.get(agent_id)
    if scope is not None:
        scope.cancel()
        log.debug("del: cancelled watcher for %s", agent_id)

    agent = ctx.state.get("agents", {}).get(agent_id)
    if agent and close:
        try:
            await terminal_close(agent["terminal"])
            log.info("del: closed terminal %s", agent["terminal"])
        except OrcaError as exc:
            log.warning("del: failed to close terminal %s: %s", agent["terminal"], exc)

    async with ctx.lock:
        removed = ctx.state.get("agents", {}).pop(agent_id, None)
        write_state(ctx.root, ctx.state)

    if removed:
        log.info("[bold red]del: removed %s[/bold red]", agent_id)
    else:
        log.warning("del: agent %s not found", agent_id)

    write_result(ctx.root, request["request_id"], {
        "ok": True,
        "agent_id": agent_id,
    })


# ---------------------------------------------------------------------------
# Per-agent watcher
# ---------------------------------------------------------------------------

async def watcher(ctx: ServerContext, agent_id: str) -> None:
    scope = anyio.CancelScope()
    ctx.watchers[agent_id] = scope

    try:
        with scope:
            agent = ctx.state["agents"].get(agent_id)
            if agent is None:
                log.warning("watcher(%s): agent not found in state, exiting", agent_id)
                return

            handle = agent["terminal"]
            log.info("watcher(%s): started  terminal=%s", agent_id, handle)

            # ---- Phase 1: wait for the agent to become idle ----
            log.info("watcher(%s): waiting for tui-idle (timeout 10min)", agent_id)
            try:
                await terminal_wait(handle, "tui-idle", timeout_ms=600_000)
            except OrcaError as exc:
                log.error("watcher(%s): wait failed: %s", agent_id, exc)
                async with ctx.lock:
                    if agent_id in ctx.state["agents"]:
                        ctx.state["agents"][agent_id]["state"] = "error"
                        ctx.state["agents"][agent_id]["updated_at"] = now_iso()
                        write_state(ctx.root, ctx.state)
                return

            log.info(
                "[bold yellow]watcher(%s): agent idle[/bold yellow], triggering handoff",
                agent_id,
            )
            async with ctx.lock:
                if agent_id in ctx.state["agents"]:
                    ctx.state["agents"][agent_id]["state"] = "idle"
                    ctx.state["agents"][agent_id]["updated_at"] = now_iso()
                    write_state(ctx.root, ctx.state)

            # ---- Phase 2: send forced handoff prompt ----
            try:
                await terminal_send(handle, FORCED_HANDOFF_PROMPT)
                log.info("watcher(%s): handoff prompt sent", agent_id)
            except OrcaError as exc:
                log.error("watcher(%s): failed to send handoff: %s", agent_id, exc)
                async with ctx.lock:
                    if agent_id in ctx.state["agents"]:
                        ctx.state["agents"][agent_id]["state"] = "error"
                        ctx.state["agents"][agent_id]["updated_at"] = now_iso()
                        write_state(ctx.root, ctx.state)
                return

            # ---- Phase 3: wait for idle again (shorter timeout) ----
            log.info("watcher(%s): waiting for handoff completion (timeout 2min)", agent_id)
            try:
                wait_result = await terminal_wait(
                    handle, "tui-idle", timeout_ms=120_000,
                )
            except OrcaError as exc:
                log.error("watcher(%s): handoff wait failed: %s", agent_id, exc)
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
                log.info(
                    "[bold green]watcher(%s): handoff complete[/bold green]  file=%s",
                    agent_id, filename,
                )
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
                        log.info(
                            "watcher(%s): report sent to caller %s",
                            agent_id, caller_terminal,
                        )
                    except OrcaError as exc:
                        log.warning(
                            "watcher(%s): failed to report to caller %s: %s",
                            agent_id, caller_terminal, exc,
                        )
            else:
                log.warning(
                    "[bold red]watcher(%s): handoff failed[/bold red]  "
                    "no HANDOFF_*.md found in output",
                    agent_id,
                )
                async with ctx.lock:
                    if agent_id in ctx.state["agents"]:
                        ctx.state["agents"][agent_id]["state"] = "handoff_failed"
                        ctx.state["agents"][agent_id]["updated_at"] = now_iso()
                        write_state(ctx.root, ctx.state)

                if caller_terminal:
                    msg = format_handoff_failed_message()
                    try:
                        await terminal_send(caller_terminal, msg)
                    except OrcaError as exc:
                        log.warning(
                            "watcher(%s): failed to notify caller of handoff failure: %s",
                            agent_id, exc,
                        )
    finally:
        ctx.watchers.pop(agent_id, None)
        log.debug("watcher(%s): exited", agent_id)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

async def reconcile_loop(ctx: ServerContext) -> None:
    log.debug("reconcile_loop started (interval=30s)")
    while True:
        await anyio.sleep(30)

        try:
            terminals = await terminal_list(str(ctx.root))
        except OrcaError as exc:
            log.warning("reconcile: terminal_list failed: %s", exc)
            continue

        live_handles = {
            t.get("handle", t.get("terminal", "")) for t in terminals
        }
        log.debug("reconcile: %d live terminal(s)", len(live_handles))

        async with ctx.lock:
            lost_agents = []
            for agent_id, agent in list(ctx.state.get("agents", {}).items()):
                if agent.get("state") == "lost":
                    continue
                if agent["terminal"] not in live_handles:
                    ctx.state["agents"][agent_id]["state"] = "lost"
                    ctx.state["agents"][agent_id]["updated_at"] = now_iso()
                    scope = ctx.watchers.get(agent_id)
                    if scope is not None:
                        scope.cancel()
                    lost_agents.append(agent_id)
            write_state(ctx.root, ctx.state)

        if lost_agents:
            log.warning(
                "reconcile: marked %d agent(s) as lost: %s",
                len(lost_agents), ", ".join(lost_agents),
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _trunc(text: str, max_len: int = 60) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."
