from __future__ import annotations

import json
import shutil
import time

import anyio

from osw.log import get_logger
from osw.process import hidden_subprocess_kwargs

log = get_logger("orca")


class OrcaError(Exception):
    def __init__(self, message: str, returncode: int) -> None:
        self.message = message
        self.returncode = returncode
        super().__init__(message)


def _resolve_orca() -> str:
    path = shutil.which("orca")
    if path is None:
        raise OrcaError(
            "Orca CLI not found. Make sure 'orca' is installed and on PATH.",
            127,
        )
    return path


async def run_orca(*args: str) -> dict:
    orca = _resolve_orca()
    cmd = [orca, *args]
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
        raise OrcaError(error_msg, result.returncode)

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise OrcaError(
            f"Orca returned invalid JSON: {result.stdout.decode()[:200]}",
            1,
        )

    log.debug(
        "orca ok  args=%s  elapsed_ms=%d  keys=%s",
        args[:2],
        elapsed_ms,
        list(data.keys()) if isinstance(data, dict) else type(data).__name__,
    )
    return data


async def worktree_current() -> dict:
    return await run_orca("worktree", "current")


async def terminal_list(worktree: str = "active") -> list[dict]:
    data = await run_orca(
        "terminal", "list", "--worktree", worktree,
    )
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        inner = data.get("result", data)
        terminals = inner.get("terminals")
        if isinstance(terminals, list):
            return terminals
    return []


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
    return await run_orca(*args)


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
            return worktrees
    return []
