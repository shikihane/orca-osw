from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import time

import anyio

from osw.log import get_logger
from osw.process import hidden_subprocess_kwargs

log = get_logger("orca")


class OrcaError(Exception):
    def __init__(
        self,
        message: str,
        returncode: int,
        code: str = "",
        payload: dict | None = None,
    ) -> None:
        self.message = message
        self.returncode = returncode
        self.code = code
        # Parsed JSON body when the failing call returned one; callers
        # use it for typed recovery (e.g. worker-start's failedStage and
        # nextAction).
        self.payload = payload
        super().__init__(message)


def _contract_error(command: str, expected: str) -> OrcaError:
    return OrcaError(
        f"Orca contract mismatch for `{command}`: expected {expected}",
        1,
        code="orca_contract_mismatch",
    )


def resolve_orca_command() -> list[str]:
    """Resolve the Orca CLI selected for this managed session."""
    configured = os.environ.get("ORCA_CLI_COMMAND", "").strip()
    if configured:
        parts = shlex.split(configured, posix=os.name != "nt")
    elif os.environ.get("ORCA_DEV_REPO_ROOT", "").strip():
        parts = ["orca-dev"]
    elif sys.platform.startswith("linux") and not os.environ.get(
        "ORCA_TERMINAL_HANDLE", ""
    ).strip():
        parts = ["orca-ide"]
    else:
        parts = ["orca"]
    parts = [part.strip('"') for part in parts]
    path = shutil.which(parts[0])
    if path is None:
        raise OrcaError(
            f"Orca CLI not found: {parts[0]}",
            127,
        )
    return [path, *parts[1:]]


async def run_orca(*args: str) -> dict:
    cmd = [*resolve_orca_command(), *args]
    if "--json" not in cmd:
        cmd.append("--json")

    log.debug("exec: %s", " ".join(cmd))

    try:
        started = time.monotonic()
        result = await anyio.run_process(
            cmd,
            check=False,
            **hidden_subprocess_kwargs(),
        )
    except FileNotFoundError:
        raise OrcaError(
            "Orca CLI not found. Make sure 'orca' is installed and on PATH.",
            127,
        )
    except OSError as exc:
        raise OrcaError(f"Failed to run orca: {exc}", 1)
    elapsed_ms = int((time.monotonic() - started) * 1000)

    if result.returncode != 0:
        stderr_text = result.stderr.decode().strip()
        stdout_text = result.stdout.decode().strip()
        log.debug(
            "orca exited %d in %dms  stderr=%s  stdout=%s",
            result.returncode,
            elapsed_ms,
            stderr_text[:200] if stderr_text else "(empty)",
            stdout_text[:200] if stdout_text else "(empty)",
        )
        error_msg = stderr_text or stdout_text
        error_code = ""
        parsed_payload: dict | None = None
        for candidate in (stdout_text, stderr_text):
            try:
                payload = json.loads(candidate)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(payload, dict):
                parsed_payload = payload
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict):
                error_code = str(error.get("code") or "")
                error_msg = str(error.get("message") or error_msg)
                break
            # A failed orchestration call still returns an ok envelope
            # whose result carries the stage/lastError (e.g. worker-start
            # exits 1 for a readiness timeout). Surface that instead of
            # the raw JSON blob.
            result_obj = payload.get("result") if isinstance(payload, dict) else None
            if isinstance(result_obj, dict) and (
                result_obj.get("state") == "failed"
                or result_obj.get("failedStage")
            ):
                stage = str(
                    result_obj.get("failedStage")
                    or result_obj.get("stage")
                    or "unknown stage"
                )
                detail = str(result_obj.get("lastError") or "")
                dispatch = str(result_obj.get("dispatchId") or "")
                error_msg = f"orca command failed at {stage}"
                if detail:
                    error_msg += f": {detail}"
                if dispatch:
                    error_msg += f" (dispatch {dispatch})"
                break
        raise OrcaError(
            error_msg, result.returncode, code=error_code,
            payload=parsed_payload,
        )

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise OrcaError(
            f"Orca returned invalid JSON: {result.stdout.decode()[:200]}",
            1,
        )

    if isinstance(data, dict) and data.get("ok") is False:
        error = data.get("error")
        if isinstance(error, dict):
            raise OrcaError(
                str(error.get("message") or "Orca command failed"),
                1,
                code=str(error.get("code") or ""),
            )
        raise OrcaError("Orca command failed", 1)

    log.debug(
        "orca ok  args=%s  elapsed_ms=%d  keys=%s",
        args[:2],
        elapsed_ms,
        list(data.keys()) if isinstance(data, dict) else type(data).__name__,
    )
    return data


def _result_object(data: dict, command: str) -> dict:
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, dict):
        raise _contract_error(command, "result to be an object")
    return result


def _object_entries(values: list, command: str, entry_name: str) -> list[dict]:
    if not all(isinstance(value, dict) for value in values):
        raise _contract_error(command, f"{entry_name} entries to be objects")
    return values


async def orca_status() -> dict:
    return await run_orca("status")


async def compatibility_report() -> dict:
    """Probe the read-only Orca contract OSW depends on."""
    command = resolve_orca_command()
    status_data = await orca_status()
    status = _result_object(status_data, "status")
    runtime = status.get("runtime")
    graph = status.get("graph")
    if not isinstance(runtime, dict):
        raise _contract_error("status", "result.runtime to be an object")
    if not isinstance(graph, dict):
        raise _contract_error("status", "result.graph to be an object")

    current_data = await worktree_current()
    current = _result_object(current_data, "worktree current")
    worktree = current.get("worktree")
    if not isinstance(worktree, dict) or not worktree.get("path"):
        raise _contract_error(
            "worktree current", "result.worktree.path to be present"
        )

    terminals = await terminal_list("active")
    terminal_show_status = "unverified"
    if terminals:
        handle = terminals[0].get("handle")
        if not handle:
            raise _contract_error(
                "terminal list", "terminal entries to include handle"
            )
        shown_data = await terminal_show(str(handle))
        shown = _result_object(shown_data, "terminal show")["terminal"]
        required_terminal_fields = {
            "handle", "tabId", "leafId", "lastOutputAt",
        }
        missing = required_terminal_fields.difference(shown)
        if missing:
            fields = ", ".join(sorted(missing))
            raise _contract_error(
                "terminal show", f"terminal fields to include {fields}"
            )
        terminal_show_status = "ok"

    worktrees = await worktree_ps()
    agents = [
        agent
        for row in worktrees
        for agent in (row.get("agents") or [])
        if isinstance(agent, dict)
    ]
    required_agent_fields = {
        "paneKey", "state", "stateStartedAt", "updatedAt",
    }
    for agent in agents:
        missing = required_agent_fields.difference(agent)
        if missing:
            fields = ", ".join(sorted(missing))
            raise _contract_error(
                "worktree ps", f"agent status fields to include {fields}"
            )

    runtime_ready = runtime.get("state") == "ready" and bool(
        runtime.get("reachable")
    )
    graph_ready = graph.get("state") == "ready"
    return {
        "ok": runtime_ready and graph_ready,
        "orca": {
            "command": command,
            "version": str(runtime.get("appVersion") or ""),
        },
        "runtime": {
            "state": runtime.get("state"),
            "reachable": bool(runtime.get("reachable")),
        },
        "graph": {"state": graph.get("state")},
        "worktree": {"path": str(worktree.get("path"))},
        "contract": {
            "terminal_list": "ok",
            "terminal_show": terminal_show_status,
            "worktree_ps": "ok",
            "agent_status": "ok" if agents else "unverified",
        },
        "counts": {
            "terminals": len(terminals),
            "worktrees": len(worktrees),
            "agents": len(agents),
        },
    }


async def worktree_current() -> dict:
    return await run_orca("worktree", "current")


async def terminal_list(worktree: str = "active") -> list[dict]:
    data = await run_orca(
        "terminal", "list", "--worktree", worktree,
    )
    if isinstance(data, list):
        return _object_entries(data, "terminal list", "terminal")
    if isinstance(data, dict):
        inner = data.get("result", data)
        terminals = inner.get("terminals")
        if isinstance(terminals, list):
            return _object_entries(terminals, "terminal list", "terminal")
    raise _contract_error("terminal list", "result.terminals to be a list")


async def terminal_create(command: str, worktree: str = "active") -> dict:
    return await run_orca(
        "terminal",
        "create",
        "--worktree",
        worktree,
        "--command",
        command,
    )


async def terminal_send(handle: str, text: str, enter: bool = True) -> dict:
    """Write `text` to the terminal; append Enter when `enter` is set.

    `enter=False` is the raw-write half of the two-stage send used for
    renderGate providers (watcher._split_send): it takes Orca's raw
    sendTerminal path, which — unlike a text-plus-Enter agent prompt —
    is not gated on the TUI re-displaying its hardware cursor. An empty
    `text` with `enter=True` sends Enter alone.
    """
    args = ["terminal", "send", "--terminal", handle]
    if text:
        args += ["--text", text]
    if enter:
        args.append("--enter")
    return await run_orca(*args)


async def terminal_interrupt(handle: str) -> dict:
    """Send Ctrl-C (`\\x03`) to the terminal: the interrupt key for
    claude, codex, kimi and grok."""
    return await run_orca("terminal", "send", "--terminal", handle, "--interrupt")


async def terminal_send_dialog_enter(handle: str) -> dict:
    """Press Enter on a CLI dialog Orca's prompt gate refuses to type into.

    Orca answers a plain send to codex's update dialog with
    `agent_prompt_blocked`; `--text "" --enter --interrupt` is the one
    form observed to get through (research 2026-10-11 V3), and it takes
    the dialog's default option.
    """
    return await run_orca(
        "terminal", "send", "--terminal", handle,
        "--text", "", "--enter", "--interrupt",
    )


async def terminal_send_escape(handle: str) -> dict:
    """Send a bare Esc byte, no Enter: the interrupt key for pi and omp,
    which ignore Ctrl-C (research 2026-10-11 V1/V8)."""
    return await terminal_send(handle, "\x1b", enter=False)


async def terminal_wait(
    handle: str, event: str = "tui-idle", timeout_ms: int = 300000
) -> dict:
    return await run_orca(
        "terminal",
        "wait",
        "--terminal",
        handle,
        "--for",
        event,
        "--timeout-ms",
        str(timeout_ms),
    )


async def terminal_close(handle: str) -> dict:
    return await run_orca("terminal", "close", "--terminal", handle)


async def terminal_read(
    handle: str, limit: int = 200, cursor: str | None = None,
    screen: bool = False,
) -> dict:
    """Read terminal output; `screen=True` returns the rendered frame.

    `--screen` and `--cursor` are mutually exclusive upstream: a screen
    read is the current frame and has no history to page.
    """
    if screen and cursor is not None:
        raise ValueError("terminal_read: screen and cursor are mutually exclusive")
    args = ["terminal", "read", "--terminal", handle, "--limit", str(limit)]
    if cursor is not None:
        args += ["--cursor", cursor]
    if screen:
        args.append("--screen")
    return await run_orca(*args)


async def terminal_show(handle: str | None = None) -> dict:
    args = ["terminal", "show"]
    if handle:
        args += ["--terminal", handle]
    data = await run_orca(*args)
    result = _result_object(data, "terminal show")
    if not isinstance(result.get("terminal"), dict):
        raise _contract_error(
            "terminal show", "result.terminal to be an object"
        )
    return data


async def detect_current_terminal(marker: str) -> str | None:
    """Find the terminal whose preview contains *marker*.

    The caller prints the marker to its own terminal first; this is the
    only reliable way to identify "the terminal I am running in"
    (`orca terminal show` returns the UI-focused terminal instead).
    """
    terminals = await terminal_list()
    for term in terminals:
        if marker in term.get("preview", ""):
            return term.get("handle") or None
    return None


async def worktree_ps() -> list[dict]:
    """Orchestration summary across worktrees.

    Each worktree row carries an ``agents`` list: Orca's own structured
    view of agent TUIs it recognizes (claude, codex) — state
    (working/done), current prompt, toolName, lastAssistantMessage,
    stateStartedAt/updatedAt, and paneKey ("<tabId>:<leafId>").
    """
    data = await run_orca("worktree", "ps")
    if isinstance(data, dict):
        worktrees = data.get("result", {}).get("worktrees")
        if isinstance(worktrees, list):
            return _object_entries(worktrees, "worktree ps", "worktree")
    raise _contract_error("worktree ps", "result.worktrees to be a list")


# ---------------------------------------------------------------------------
# Supervised orchestration (`orca orchestration ...`)
#
# Provider TUIs OSW launches through `worker-start` get a real agent
# launch from Orca (terminal creation, readiness wait, prompt injection
# and a capability-bound preamble). That launch authority is also what
# makes Orca persist the pane's hook status rows, and the Dispatch
# lifecycle is a completion signal that never depends on terminal
# output going quiet — the only signal that works for a TUI like omp
# whose status bar repaints about once a second.

async def orchestration_run_current() -> dict:
    """The Run bound to the calling coordinator terminal (``run`` null
    when this terminal is not bound to any Run)."""
    data = await run_orca("orchestration", "run-current")
    result = _result_object(data, "orchestration run-current")
    run = result.get("run")
    if run is not None and not isinstance(run, dict):
        raise _contract_error(
            "orchestration run-current", "result.run to be an object or null"
        )
    return result


async def orchestration_run_create(objective: str) -> dict:
    """Create a Run and bind the calling terminal to it."""
    data = await run_orca(
        "orchestration", "run-create", "--objective", objective,
    )
    result = _result_object(data, "orchestration run-create")
    run = result.get("run")
    if not isinstance(run, dict) or not run.get("id"):
        raise _contract_error(
            "orchestration run-create", "result.run.id to be present"
        )
    return result


async def orchestration_run_use(run_id: str) -> dict:
    """Bind the calling terminal to an existing Run."""
    return await run_orca("orchestration", "run-use", "--id", run_id)


async def orchestration_worker_start(
    spec: str,
    agent: str,
    worktree: str,
    task_title: str = "",
    model: str = "",
    terminal: str = "",
    timeout_ms: int = 0,
) -> dict:
    """Start one supervised worker: fresh agent terminal, readiness wait
    and prompt injection are Orca's, not ours.

    `--agent` and `--terminal` are mutually exclusive upstream: with a
    terminal handle the Dispatch reuses that already-proven worker
    terminal instead of launching a new agent. `timeout_ms` overrides
    Orca's 60s readiness budget, which an omp cold start can exceed on
    a loaded machine.
    """
    args = [
        "orchestration", "worker-start",
        "--spec", spec,
        "--worktree", worktree,
    ]
    if terminal:
        args += ["--terminal", terminal]
    else:
        args += ["--agent", agent]
    if task_title:
        args += ["--task-title", task_title]
    if model:
        args += ["--model", model]
    if timeout_ms:
        args += ["--timeout-ms", str(timeout_ms)]
    return await run_orca(*args)


def worker_start_receipt(data: dict) -> dict:
    """Validated `worker-start` receipt: lifecycle ids + agent terminal.

    The agent terminal handle comes from the terminal effect the start
    created; it is the pane later `terminal send` calls address (the
    handoff fallback), while completion runs on the Dispatch id.
    """
    result = _result_object(data, "orchestration worker-start")
    dispatch_id = str(result.get("dispatchId") or "")
    task_id = str(result.get("taskId") or "")
    run_id = str(result.get("runId") or "")
    if not dispatch_id or not task_id:
        raise _contract_error(
            "orchestration worker-start",
            "result.dispatchId and result.taskId to be present",
        )
    handle = ""
    effects = result.get("effects")
    if isinstance(effects, list):
        for effect in effects:
            if (isinstance(effect, dict)
                    and effect.get("kind") == "terminal"
                    and effect.get("role") == "agent"
                    and effect.get("id")):
                handle = str(effect["id"])
    return {
        "run_id": run_id,
        "task_id": task_id,
        "dispatch_id": dispatch_id,
        "terminal": handle,
    }


async def orchestration_worker_show(dispatch_id: str) -> dict:
    return await run_orca(
        "orchestration", "worker-show", "--dispatch", dispatch_id,
    )


def worker_settlement(data: dict) -> dict:
    """Settlement view of one `worker-show` payload.

    ``settled`` is False while the Dispatch is still running; once True
    ``outcome`` carries Orca's own verdict (succeeded/failed/...), and
    ``agent_wait`` names a pane parked on a question only a human can
    answer. ``last_observed_at`` is Orca's freshness stamp for the
    worker (epoch ms), for stall notifications.
    """
    result = _result_object(data, "orchestration worker-show")
    dispatch = result.get("dispatch")
    worker = result.get("worker")
    projection = result.get("projection")
    observation = result.get("observation")
    if not isinstance(dispatch, dict) or not isinstance(worker, dict) \
            or not isinstance(projection, dict):
        raise _contract_error(
            "orchestration worker-show",
            "result.dispatch, result.worker and result.projection"
            " to be objects",
        )
    stage = projection.get("stage")
    stage = stage if isinstance(stage, dict) else {}
    dispatch_status = str(dispatch.get("status") or "")
    worker_state = str(worker.get("state") or "")
    worker_stage = str(worker.get("stage") or "")
    settled = (
        dispatch_status in ("completed", "failed", "stopped")
        or worker_stage == "settled"
        or worker_state in ("succeeded", "failed", "stopped", "abandoned")
    )
    outcome = str(
        projection.get("outcome")
        or stage.get("worker")
        or worker_state
        or dispatch_status
    )
    agent_wait = ""
    if isinstance(observation, dict) and observation.get("agentWait"):
        agent_wait = str(observation.get("agentWait"))
    liveness = projection.get("liveness")
    liveness = liveness if isinstance(liveness, dict) else {}
    evidence = projection.get("evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    return {
        "dispatch_status": dispatch_status,
        "worker_state": worker_state,
        "worker_stage": worker_stage,
        "settled": settled,
        "outcome": outcome,
        "agent_wait": agent_wait,
        "liveness": str(liveness.get("verdict") or ""),
        "last_observed_at": int(evidence.get("lastObservedAt") or 0),
        "terminal": str(
            worker.get("agentTerminalHandle")
            or dispatch.get("assigneeHandle")
            or ""
        ),
        "pane_key": str(dispatch.get("assigneePaneKey") or ""),
    }


async def orchestration_worker_retain(dispatch_id: str) -> dict:
    """Record OSW's decision to keep a settled worker terminal live."""
    return await run_orca(
        "orchestration", "worker-retain", "--dispatch", dispatch_id,
    )


async def orchestration_inbox(terminal: str = "", limit: int = 0) -> list[dict]:
    """Messages from `orchestration inbox`, newest first.

    `--terminal` filters by *recipient* handle; without it the inbox
    spans every recipient, which is where worker questions show up
    (they are addressed to the Run mailbox, `run:<id>`). Read-only:
    unlike `check`, it never marks anything read.
    """
    args = ["orchestration", "inbox"]
    if limit:
        args += ["--limit", str(limit)]
    if terminal:
        args += ["--terminal", terminal]
    result = _result_object(await run_orca(*args), "orchestration inbox")
    messages = result.get("messages")
    if not isinstance(messages, list):
        raise _contract_error("orchestration inbox", "result.messages to be a list")
    return messages


# Message types a worker raises for its coordinator: `orchestration ask`
# posts `question`; `send --type escalation` reports a blocker.
ASK_MESSAGE_TYPES = ("question", "escalation")


def _payload_dispatch_id(message: dict) -> str:
    payload = message.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("dispatchId") or "")


def worker_asks(
    messages: list,
    dispatch_id: str,
    terminal: str = "",
    pane_key: str = "",
) -> list[dict]:
    """Unanswered questions and escalations one worker raised, oldest first.

    A question comes `from_handle: dispatch:<id>`, an escalation from the
    worker's terminal handle with its `sender_pane_key`; both carry the
    Dispatch id in their JSON `payload`, and any of these identifies the
    worker. A message counts as answered once another message joins its
    thread (`thread_id` naming it), which is how a `reply` threads.
    """
    rows = [m for m in messages if isinstance(m, dict) and m.get("id")]
    answered = {
        str(m["thread_id"]) for m in rows
        if m.get("thread_id") and m.get("thread_id") != m.get("id")
    }
    senders = {terminal} if terminal else set()
    if dispatch_id:
        senders.add(f"dispatch:{dispatch_id}")
    asks = []
    for message in rows:
        if message.get("type") not in ASK_MESSAGE_TYPES:
            continue
        if str(message["id"]) in answered:
            continue
        if message.get("from_handle") in senders \
                or (pane_key and message.get("sender_pane_key") == pane_key) \
                or (dispatch_id and _payload_dispatch_id(message) == dispatch_id):
            asks.append(message)
    return sorted(asks, key=lambda m: int(m.get("sequence") or 0))


async def orchestration_worker_release(dispatch_id: str) -> dict:
    """Release the terminal of one settled supervised worker."""
    return await run_orca(
        "orchestration", "worker-release", "--dispatch", dispatch_id,
    )


async def orchestration_worker_stop(dispatch_id: str) -> dict:
    """Fence one Dispatch and stop its supervised agent terminal."""
    return await run_orca(
        "orchestration", "worker-stop", "--dispatch", dispatch_id,
    )
