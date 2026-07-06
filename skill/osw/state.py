from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path


def resolve_project_root() -> Path:
    """Resolve the current project root (the resolved cwd)."""
    return Path.cwd().resolve()


def state_dir(root: Path) -> Path:
    """Return the OSW state directory for a given project root."""
    return root / ".orca" / "osw"


def state_file(root: Path) -> Path:
    """Return the path to the state.json file."""
    return state_dir(root) / "state.json"


def inbox_dir(root: Path) -> Path:
    """Return the path to the inbox directory (request files)."""
    return state_dir(root) / "inbox"


def results_dir(root: Path) -> Path:
    """Return the path to the results directory (result files)."""
    return state_dir(root) / "results"


def logs_dir(root: Path) -> Path:
    """Return the path to the logs directory."""
    return state_dir(root) / "logs"


def init_state_dir(root: Path) -> None:
    """Create the OSW state directory structure and write the default state.json."""
    state_dir(root).mkdir(parents=True, exist_ok=True)
    inbox_dir(root).mkdir(parents=True, exist_ok=True)
    results_dir(root).mkdir(parents=True, exist_ok=True)
    logs_dir(root).mkdir(parents=True, exist_ok=True)

    default_state = {
        "version": 1,
        "project_root": str(root),
        "serve": None,
        "models": {
            "strong": [{"name": "strong-default", "command": "codex"}],
            "medium": [{"name": "medium-default", "command": "pi"}],
            "weak": [{"name": "weak-default", "command": "pi"}],
        },
        "agents": {},
        "errors": [],
    }
    write_state(root, default_state)


def read_state(root: Path) -> dict:
    """Read and parse state.json. Raises FileNotFoundError if it doesn't exist."""
    path = state_file(root)
    if not path.exists():
        raise FileNotFoundError(f"State file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_state(root: Path, state: dict) -> None:
    """Atomically write the state dict to state.json."""
    path = state_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp_path, path)


_AGENT_ID_RE = re.compile(r"^agent_(\d+)$")


def next_agent_id(state: dict) -> str:
    """Return the next sequential agent id based on existing agents in state."""
    agents = state.get("agents", {}) or {}
    max_n = 0
    for key in agents:
        match = _AGENT_ID_RE.match(key)
        if match:
            n = int(match.group(1))
            if n > max_n:
                max_n = n
    return f"agent_{max_n + 1:03d}"


def write_request(root: Path, command: str, payload: dict) -> str:
    """Write a request to the inbox directory and return its request_id."""
    request_id = uuid.uuid4().hex[:12]
    inbox_dir(root).mkdir(parents=True, exist_ok=True)
    request = {"request_id": request_id, "command": command, **payload}
    path = inbox_dir(root) / f"{request_id}.json"
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(request, f, indent=2)
    os.replace(tmp_path, path)
    return request_id


def read_result(root: Path, request_id: str, timeout: float = 15.0) -> dict | None:
    """Poll the results directory for a matching result file.

    Returns the parsed JSON payload if the file appears within the timeout,
    deleting the file after reading. Returns None if the timeout expires.
    """
    path = results_dir(root) / f"{request_id}.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                time.sleep(0.3)
                continue
            try:
                path.unlink()
            except OSError:
                pass
            return data
        time.sleep(0.3)
    return None


def write_result(root: Path, request_id: str, payload: dict) -> None:
    """Write a result payload to the results directory."""
    results_dir(root).mkdir(parents=True, exist_ok=True)
    path = results_dir(root) / f"{request_id}.json"
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, path)


def _pid_is_running(pid: int) -> bool:
    """Check whether a process with the given PID is currently running."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not handle:
                return False
            exit_code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(
                handle, ctypes.byref(exit_code)
            )
            ctypes.windll.kernel32.CloseHandle(handle)
            STILL_ACTIVE = 259
            return exit_code.value == STILL_ACTIVE
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists but we don't have permission to signal it.
            return True
        except OSError:
            return False
        return True


def is_serve_running(root: Path) -> bool:
    """Return True if state.json records a live serve process."""
    try:
        state = read_state(root)
    except (FileNotFoundError, json.JSONDecodeError):
        return False

    serve = state.get("serve")
    if not serve:
        return False

    pid = serve.get("pid")
    if pid is None:
        return False

    return _pid_is_running(int(pid))
