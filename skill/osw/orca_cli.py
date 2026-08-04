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
    def __init__(self, message: str, returncode: int, code: str = "") -> None:
        self.message = message
        self.returncode = returncode
        self.code = code
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
        for candidate in (stdout_text, stderr_text):
            try:
                payload = json.loads(candidate)
            except (json.JSONDecodeError, TypeError):
                continue
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict):
                error_code = str(error.get("code") or "")
                error_msg = str(error.get("message") or error_msg)
                break
        raise OrcaError(error_msg, result.returncode, code=error_code)

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


async def terminal_send(handle: str, text: str) -> dict:
    return await run_orca(
        "terminal", "send", "--terminal", handle, "--text", text, "--enter",
    )


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
    handle: str, limit: int = 200, cursor: str | None = None
) -> dict:
    args = ["terminal", "read", "--terminal", handle, "--limit", str(limit)]
    if cursor is not None:
        args += ["--cursor", cursor]
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
