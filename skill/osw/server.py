from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import anyio
import anyio.abc

from osw.log import get_logger
from osw.orca_cli import (
    OrcaError,
    detect_current_terminal,
    orchestration_check,
    orchestration_dispatch,
    orchestration_task_create,
    terminal_close,
    terminal_create,
    terminal_info,
    terminal_list,
    terminal_read,
    terminal_send,
    terminal_show,
    terminal_wait,
)
from osw.state import (
    inbox_dir,
    next_agent_id,
    read_state,
    write_report,
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
    coordinator: str | None = None
    done_events: dict[str, anyio.Event] = field(default_factory=dict)
    worker_msgs: dict[str, dict] = field(default_factory=dict)
    task_agents: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

async def _detect_serve_terminal() -> str | None:
    """Identify the terminal this serve process runs in via a trace marker."""
    marker = f"osw_serve_{uuid.uuid4().hex[:12]}"
    log.info("supervisor terminal trace  %s", marker)
    await anyio.sleep(0.5)
    try:
        return await detect_current_terminal(marker)
    except OrcaError as exc:
        log.warning("serve terminal detection failed: %s", exc)
        return None


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

    ctx.coordinator = await _detect_serve_terminal()
    if ctx.coordinator:
        log.info("coordinator terminal: %s (orchestration mode)", ctx.coordinator)
    else:
        log.warning(
            "serve terminal not identified; falling back to prompt-injection mode"
        )
    log.info("inbox polling every 0.5s, reconciliation every 30s")

    try:
        async with anyio.create_task_group() as tg:
            ctx.task_group = tg
            tg.start_soon(inbox_loop, ctx)
            tg.start_soon(reconcile_loop, ctx)
            if ctx.coordinator:
                tg.start_soon(orchestration_loop, ctx)
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
                    "inbox command=%s  request_id=%s",
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

MODEL_TIERS = ("strong", "medium", "weak")
DEFAULT_TIER = "medium"


def _resolve_model(
    state: dict, tier: str, model_name: str | None
) -> tuple[dict | None, str]:
    """Pick a model entry. Returns (entry, error).

    A model name matches across all tiers and wins over tier. An
    empty requested tier falls back to the other tiers in
    MODEL_TIERS order.
    """
    models = state.get("models", {})

    if model_name:
        for t in MODEL_TIERS:
            for entry in models.get(t) or []:
                if entry.get("name") == model_name:
                    return entry, ""
        return None, f"model '{model_name}' not found in models config"

    if tier not in MODEL_TIERS:
        return None, f"unknown tier '{tier}' (expected one of {'/'.join(MODEL_TIERS)})"

    search_order = [tier] + [t for t in MODEL_TIERS if t != tier]
    for t in search_order:
        entries = models.get(t) or []
        if entries:
            if t != tier:
                log.warning("models: tier '%s' is empty, using '%s' instead", tier, t)
            return entries[0], ""
    return None, "models config is empty"


async def handle_new(ctx: ServerContext, request: dict) -> None:
    prompt = request.get("prompt", "")
    tier = request.get("tier") or DEFAULT_TIER
    model_name = request.get("model")

    entry, error = _resolve_model(ctx.state, tier, model_name)
    if entry is None:
        log.error("new: %s", error)
        write_result(ctx.root, request["request_id"], {
            "ok": False,
            "error": error,
        })
        return

    command = entry.get("command", "")
    log.info(
        "new: creating terminal  model=%s  provider=%s  prompt=%s",
        entry.get("name", "?"), command, _trunc(prompt),
    )

    try:
        result = await terminal_create(command)
    except OrcaError as exc:
        log.error("new: terminal_create failed: %s", exc)
        write_result(ctx.root, request["request_id"], {
            "ok": False,
            "error": str(exc),
        })
        return

    terminal_obj = result.get("result", {}).get("terminal", {})
    handle = terminal_obj.get("handle", result.get("handle", ""))
    log.info("new: terminal created  handle=%s", handle)

    async with ctx.lock:
        agent_id = next_agent_id(ctx.state)
        now = now_iso()
        ctx.state["agents"][agent_id] = {
            "agent_id": agent_id,
            "terminal": handle,
            "worktree_path": str(ctx.root),
            "provider_command": command,
            "model_name": entry.get("name", ""),
            "state": "assigned",
            "caller_terminal": request.get("caller_terminal"),
            "last_prompt": prompt,
            "last_handoff_file": None,
            "created_at": now,
            "updated_at": now,
        }
        write_state(ctx.root, ctx.state)

    write_result(ctx.root, request["request_id"], {
        "ok": True,
        "agent_id": agent_id,
        "terminal": handle,
        "worktree_path": str(ctx.root),
        "model": entry.get("name", ""),
        "state": "assigned",
    })

    log.info(
        "new: agent registered  %s -> %s  model=%s",
        agent_id, handle, entry.get("name", ""),
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

    write_result(ctx.root, request["request_id"], {
        "ok": True,
        "agent_id": agent_id,
        "terminal": handle,
        "worktree_path": worktree_path,
        "state": "assigned",
    })

    log.info(
        "use: agent registered  %s -> %s",
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
        log.info("del: removed %s", agent_id)
    else:
        log.warning("del: agent %s not found", agent_id)

    write_result(ctx.root, request["request_id"], {
        "ok": True,
        "agent_id": agent_id,
    })


# ---------------------------------------------------------------------------
# Orchestration message routing
# ---------------------------------------------------------------------------

ORCH_POLL_SECS = 2.0


def _msg_payload(msg: dict) -> dict:
    try:
        payload = json.loads(msg.get("payload") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _resolve_agent(ctx: ServerContext, msg: dict) -> str | None:
    """Map a message to an agent: by taskId payload, then by sender handle."""
    payload = _msg_payload(msg)
    task_id = payload.get("taskId") or payload.get("task_id")
    if task_id and task_id in ctx.task_agents:
        return ctx.task_agents[task_id]
    sender = msg.get("from_handle", "")
    for agent_id, agent in ctx.state.get("agents", {}).items():
        if agent.get("terminal") == sender:
            return agent_id
    return None


async def orchestration_loop(ctx: ServerContext) -> None:
    log.debug("orchestration_loop started (interval=%.0fs)", ORCH_POLL_SECS)
    while True:
        await anyio.sleep(ORCH_POLL_SECS)
        try:
            messages = await orchestration_check(ctx.coordinator)
        except OrcaError as exc:
            log.warning("orchestration: check failed: %s", exc)
            continue

        for msg in messages:
            await route_message(ctx, msg)


async def route_message(ctx: ServerContext, msg: dict) -> None:
    mtype = msg.get("type", "")
    agent_id = _resolve_agent(ctx, msg)
    if agent_id is None:
        log.warning(
            "orchestration: unroutable message id=%s type=%s from=%s subject=%s",
            msg.get("id"), mtype, msg.get("from_handle"), _trunc(msg.get("subject", "")),
        )
        return

    if mtype == "worker_done":
        log.info(
            "orchestration: worker_done  agent=%s subject=%s",
            agent_id, _trunc(msg.get("subject", "")),
        )
        ctx.worker_msgs[agent_id] = msg
        event = ctx.done_events.get(agent_id)
        if event is not None:
            event.set()
    elif mtype == "heartbeat":
        phase = _msg_payload(msg).get("phase", "")
        log.info("orchestration: heartbeat  agent=%s phase=%s", agent_id, phase)
        async with ctx.lock:
            if agent_id in ctx.state["agents"]:
                ctx.state["agents"][agent_id]["updated_at"] = now_iso()
                ctx.state["agents"][agent_id]["phase"] = phase
                write_state(ctx.root, ctx.state)
    else:
        # escalation / decision_gate / status — forward a pointer to the caller
        log.info(
            "orchestration: %s  agent=%s subject=%s",
            mtype, agent_id, _trunc(msg.get("subject", "")),
        )
        caller = ctx.state["agents"].get(agent_id, {}).get("caller_terminal")
        if caller:
            line = (
                f"# [osw] {mtype} agent={agent_id}"
                f" subject={_oneline(msg.get('subject', ''))}"
                f" msg_id={msg.get('id', '')}"
            )
            try:
                await terminal_send(caller, line)
            except OrcaError as exc:
                log.warning(
                    "orchestration: failed to forward %s to caller: %s", mtype, exc,
                )


# ---------------------------------------------------------------------------
# Per-agent watcher
# ---------------------------------------------------------------------------

# Completion detection tuning
START_TIMEOUT_SECS = 120   # max time to wait for the agent to start producing output
START_POLL_SECS = 1.0      # poll interval while waiting for the agent to start
IDLE_STABLE_MS = 5000      # idle only counts if no output for this long
WAIT_TIMEOUT_MS = 600_000

# Orchestration-mode completion tuning
WORKER_DONE_TIMEOUT_SECS = 3600    # give up waiting for worker_done after this
DONE_POLL_SECS = 2.0               # poll interval for event/idle checks
FALLBACK_IDLE_STABLE_MS = 60_000   # fallback: worker silent this long = finished


async def _last_output_at(handle: str) -> int:
    data = await terminal_show(handle)
    term = data.get("result", {}).get("terminal", {})
    return int(term.get("lastOutputAt") or 0)


async def _wait_task_finished(handle: str, agent_id: str) -> None:
    """Wait until the agent has actually worked and then gone idle.

    tui-idle alone is unreliable: right after the prompt is sent the TUI is
    still idle, so a bare wait returns immediately. Instead:
      1. Wait for lastOutputAt to advance past the post-send baseline
         (the agent started producing output).
      2. Wait for tui-idle, then verify the idle is stable — the last
         output must be at least IDLE_STABLE_MS in the past. Otherwise
         keep waiting.
    """
    baseline = await _last_output_at(handle)
    deadline = time.monotonic() + START_TIMEOUT_SECS
    started = False
    while time.monotonic() < deadline:
        await anyio.sleep(START_POLL_SECS)
        if await _last_output_at(handle) > baseline:
            started = True
            break
    if started:
        log.info("watcher(%s): agent started working", agent_id)
    else:
        log.warning(
            "watcher(%s): no output detected within %ds, proceeding to idle wait",
            agent_id, START_TIMEOUT_SECS,
        )

    while True:
        await terminal_wait(handle, "tui-idle", timeout_ms=WAIT_TIMEOUT_MS)
        last = await _last_output_at(handle)
        idle_for = time.time() * 1000 - last
        if idle_for >= IDLE_STABLE_MS:
            log.info(
                "watcher(%s): idle stable (%.0fms since last output)",
                agent_id, idle_for,
            )
            return
        log.debug(
            "watcher(%s): idle not stable (%.0fms since last output), re-waiting",
            agent_id, idle_for,
        )
        await anyio.sleep(1)


async def _mark_error(ctx: ServerContext, agent_id: str) -> None:
    async with ctx.lock:
        if agent_id in ctx.state["agents"]:
            ctx.state["agents"][agent_id]["state"] = "error"
            ctx.state["agents"][agent_id]["updated_at"] = now_iso()
            write_state(ctx.root, ctx.state)


async def _dispatch_task(
    ctx: ServerContext, agent_id: str, handle: str, prompt: str
) -> str:
    """Create an orchestration task and dispatch it to the worker terminal.

    Returns the task id. Raises OrcaError on failure.
    """
    task_id = await orchestration_task_create(prompt, title=_trunc(prompt))
    if not task_id:
        raise OrcaError("task-create returned no task id", 1)
    result = await orchestration_dispatch(
        task_id, handle, from_handle=ctx.coordinator, inject=True,
    )
    dispatch = result.get("result", {}).get("dispatch") or {}
    dispatch_id = dispatch.get("id", "")
    log.info(
        "watcher(%s): dispatched task=%s dispatch=%s", agent_id, task_id, dispatch_id,
    )
    ctx.task_agents[task_id] = agent_id
    async with ctx.lock:
        if agent_id in ctx.state["agents"]:
            ctx.state["agents"][agent_id]["task_id"] = task_id
            ctx.state["agents"][agent_id]["state"] = "working"
            ctx.state["agents"][agent_id]["updated_at"] = now_iso()
            write_state(ctx.root, ctx.state)
    return task_id


async def _wait_worker_done(
    ctx: ServerContext, agent_id: str, handle: str
) -> str:
    """Wait for the worker_done message; fall back to idle detection.

    Returns the completion source: "worker_done", "fallback_idle",
    or "timeout".
    """
    event = ctx.done_events[agent_id]
    try:
        baseline = await _last_output_at(handle)
    except OrcaError:
        baseline = 0
    started = False
    deadline = time.monotonic() + WORKER_DONE_TIMEOUT_SECS

    while time.monotonic() < deadline:
        if event.is_set():
            return "worker_done"
        await anyio.sleep(DONE_POLL_SECS)
        try:
            last = await _last_output_at(handle)
        except OrcaError:
            continue
        if last > baseline:
            started = True
        if started:
            idle_for = time.time() * 1000 - last
            if idle_for >= FALLBACK_IDLE_STABLE_MS:
                log.warning(
                    "watcher(%s): no worker_done, but idle %.0fs — assuming finished",
                    agent_id, idle_for / 1000,
                )
                return "fallback_idle"
    return "timeout"


async def _finalize_agent(
    ctx: ServerContext, agent_id: str, handle: str, prompt: str, source: str
) -> None:
    """Write the completion report, update state, and notify the caller."""
    agent = ctx.state["agents"].get(agent_id, {})
    worker_msg = ctx.worker_msgs.pop(agent_id, None)
    worker_payload = _msg_payload(worker_msg) if worker_msg else {}

    output_tail: list[str] = []
    terminal_status = ""
    try:
        read_data = await terminal_read(handle, limit=100)
        term = read_data.get("result", {}).get("terminal", {})
        output_tail = term.get("tail", []) or []
        terminal_status = term.get("status", "")
    except OrcaError as exc:
        log.warning("watcher(%s): failed to capture output: %s", agent_id, exc)

    final_state = "error" if source == "timeout" else "done"
    finished_at = now_iso()
    report_payload = {
        "version": 2,
        "event": "task_finished",
        "agent_id": agent_id,
        "terminal": handle,
        "state": final_state,
        "completion_source": source,
        "prompt": prompt,
        "task_id": agent.get("task_id"),
        "created_at": agent.get("created_at"),
        "finished_at": finished_at,
        "worker_subject": (worker_msg or {}).get("subject", ""),
        "worker_summary": (worker_msg or {}).get("body", ""),
        "files_modified": worker_payload.get("filesModified", []),
        "worker_report_path": worker_payload.get("reportPath", ""),
        "terminal_status": terminal_status,
        "output_tail": output_tail,
    }
    report_path = write_report(ctx.root, agent_id, report_payload)
    log.info("watcher(%s): report written to %s", agent_id, report_path)

    async with ctx.lock:
        if agent_id in ctx.state["agents"]:
            ctx.state["agents"][agent_id]["state"] = final_state
            ctx.state["agents"][agent_id]["updated_at"] = finished_at
            ctx.state["agents"][agent_id]["report_file"] = str(report_path)
            write_state(ctx.root, ctx.state)

    caller_terminal = agent.get("caller_terminal")
    if caller_terminal:
        summary = _oneline((worker_msg or {}).get("body", ""))
        message = format_completion_report(
            agent_id, handle, str(report_path), summary=summary,
        )
        try:
            await terminal_send(caller_terminal, message)
            log.info(
                "watcher(%s): notification sent to caller %s",
                agent_id, caller_terminal,
            )
        except OrcaError as exc:
            log.warning(
                "watcher(%s): failed to notify caller %s: %s",
                agent_id, caller_terminal, exc,
            )


async def watcher(ctx: ServerContext, agent_id: str) -> None:
    scope = anyio.CancelScope()
    ctx.watchers[agent_id] = scope
    ctx.done_events[agent_id] = anyio.Event()

    try:
        with scope:
            agent = ctx.state["agents"].get(agent_id)
            if agent is None:
                log.warning("watcher(%s): agent not found in state, exiting", agent_id)
                return

            handle = agent["terminal"]
            prompt = agent.get("last_prompt", "")
            log.info("watcher(%s): started  terminal=%s", agent_id, handle)

            # ---- Phase 1: wait for agent ready ----
            log.info("watcher(%s): waiting for agent ready", agent_id)
            try:
                await terminal_wait(handle, "tui-idle", timeout_ms=600_000)
            except OrcaError as exc:
                log.error("watcher(%s): ready-wait failed: %s", agent_id, exc)
                await _mark_error(ctx, agent_id)
                return

            source = "no_prompt"
            if prompt:
                if ctx.coordinator:
                    # ---- Orchestration mode: official task dispatch ----
                    try:
                        await _dispatch_task(ctx, agent_id, handle, prompt)
                        log.info(
                            "watcher(%s): waiting for worker_done", agent_id,
                        )
                        source = await _wait_worker_done(ctx, agent_id, handle)
                    except OrcaError as exc:
                        log.warning(
                            "watcher(%s): dispatch failed (%s), "
                            "falling back to prompt injection",
                            agent_id, exc,
                        )
                        source = await _legacy_prompt_flow(ctx, agent_id, handle, prompt)
                else:
                    # ---- Legacy mode: raw prompt + idle heuristics ----
                    source = await _legacy_prompt_flow(ctx, agent_id, handle, prompt)

                if source == "error":
                    return

            log.info("watcher(%s): agent done (source=%s)", agent_id, source)
            await _finalize_agent(ctx, agent_id, handle, prompt, source)
    finally:
        ctx.watchers.pop(agent_id, None)
        ctx.done_events.pop(agent_id, None)
        log.debug("watcher(%s): exited", agent_id)


async def _legacy_prompt_flow(
    ctx: ServerContext, agent_id: str, handle: str, prompt: str
) -> str:
    """Send the raw prompt and detect completion via idle heuristics."""
    log.info("watcher(%s): agent ready, sending prompt", agent_id)
    try:
        await terminal_send(handle, prompt)
    except OrcaError as exc:
        log.error("watcher(%s): send failed: %s", agent_id, exc)
        await _mark_error(ctx, agent_id)
        return "error"

    log.info("watcher(%s): waiting for task completion", agent_id)
    try:
        await _wait_task_finished(handle, agent_id)
    except OrcaError as exc:
        log.error("watcher(%s): completion-wait failed: %s", agent_id, exc)
        await _mark_error(ctx, agent_id)
        return "error"
    return "idle_detect"


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

async def reconcile_loop(ctx: ServerContext) -> None:
    log.debug("reconcile_loop started (interval=30s)")
    while True:
        await anyio.sleep(30)

        try:
            terminals = await terminal_list()
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
                if agent.get("state") in ("lost", "done", "error"):
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


def _oneline(text: str, max_len: int = 160) -> str:
    """Collapse whitespace/newlines into a single line and truncate."""
    return _trunc(" ".join((text or "").split()), max_len)


def format_completion_report(
    agent_id: str, terminal: str, report_file: str, summary: str = ""
) -> str:
    """Single-line completion notification sent to the caller terminal.

    Single-line because newlines get mangled through orca.CMD --text on
    Windows; the leading '#' keeps it inert in a shell. Full data lives
    in the JSON report file.
    """
    message = f"# [osw] task-finished agent={agent_id} terminal={terminal}"
    if report_file:
        message += f" report={report_file}"
    if summary:
        message += f" summary={' '.join(summary.split())}"
    return message
