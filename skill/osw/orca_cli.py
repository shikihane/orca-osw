from __future__ import annotations

import json
import shutil

import anyio

from osw.log import get_logger

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
        result = await anyio.run_process(cmd, check=False)
    except FileNotFoundError:
        raise OrcaError(
            "Orca CLI not found. Make sure 'orca' is installed and on PATH.",
            127,
        )
    except OSError as exc:
        raise OrcaError(f"Failed to run orca: {exc}", 1)

    if result.returncode != 0:
        stderr_text = result.stderr.decode().strip()
        log.debug(
            "orca exited %d  stderr=%s",
            result.returncode, stderr_text[:200] if stderr_text else "(empty)",
        )
        raise OrcaError(stderr_text, result.returncode)

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise OrcaError(
            f"Orca returned invalid JSON: {result.stdout.decode()[:200]}",
            1,
        )

    log.debug(
        "orca ok  args=%s  keys=%s",
        args[:2],
        list(data.keys()) if isinstance(data, dict) else type(data).__name__,
    )
    return data


async def worktree_current() -> dict:
    return await run_orca("worktree", "current")


async def terminal_list(worktree_path: str) -> list[dict]:
    result = await run_orca(
        "terminal", "list", "--worktree", f"path:{worktree_path}"
    )
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for value in result.values():
            if isinstance(value, list):
                return value
    return []


async def terminal_create(worktree_path: str, command: str) -> dict:
    return await run_orca(
        "terminal",
        "create",
        "--worktree",
        f"path:{worktree_path}",
        "--command",
        command,
    )


async def terminal_send(handle: str, message: str) -> dict:
    return await run_orca(
        "terminal", "send", "--terminal", handle, "--message", message
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


async def terminal_info(handle: str) -> dict:
    return await run_orca("terminal", "info", "--terminal", handle)
