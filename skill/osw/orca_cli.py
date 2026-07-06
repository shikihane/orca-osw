from __future__ import annotations

import json

import anyio


class OrcaError(Exception):
    """Raised when the `orca` CLI exits with a nonzero return code."""

    def __init__(self, message: str, returncode: int) -> None:
        self.message = message
        self.returncode = returncode
        super().__init__(message)


async def run_orca(*args: str) -> dict:
    """Run the `orca` CLI with the given arguments and return parsed JSON output.

    Always requests JSON output (appends "--json" if not already present).
    Raises OrcaError if the process exits with a nonzero return code.
    """
    cmd = ["orca", *args]
    if "--json" not in cmd:
        cmd.append("--json")

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
        raise OrcaError(result.stderr.decode(), result.returncode)

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise OrcaError(
            f"Orca returned invalid JSON: {result.stdout.decode()[:200]}",
            1,
        )


async def worktree_current() -> dict:
    """Return info about the current Orca-managed worktree."""
    return await run_orca("worktree", "current")


async def terminal_list(worktree_path: str) -> list[dict]:
    """List terminals for the given worktree path."""
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
    """Create a new terminal running `command` in the given worktree."""
    return await run_orca(
        "terminal",
        "create",
        "--worktree",
        f"path:{worktree_path}",
        "--command",
        command,
    )


async def terminal_send(handle: str, message: str) -> dict:
    """Send a message to the terminal identified by `handle`."""
    return await run_orca(
        "terminal", "send", "--terminal", handle, "--message", message
    )


async def terminal_wait(
    handle: str, event: str = "tui-idle", timeout_ms: int = 300000
) -> dict:
    """Wait for `event` to occur on the terminal identified by `handle`."""
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
    """Close the terminal identified by `handle`."""
    return await run_orca("terminal", "close", "--terminal", handle)


async def terminal_info(handle: str) -> dict:
    """Return info (including worktreePath) for the terminal identified by `handle`."""
    return await run_orca("terminal", "info", "--terminal", handle)
