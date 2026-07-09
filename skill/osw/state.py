from __future__ import annotations

import json
import os
import time
from pathlib import Path


def resolve_project_root() -> Path:
    """Resolve the current project root (the resolved cwd)."""
    return Path.cwd().resolve()


def state_dir(root: Path) -> Path:
    """Return the OSW state directory for a given project root."""
    return root / ".orca" / "osw"


def state_file(root: Path) -> Path:
    """Return the path to OSW's project state file."""
    return state_dir(root) / "state.json"


def agents_dir(root: Path) -> Path:
    """Return the per-agent record directory. One JSON file per agent;
    the agent's watcher process is the only writer after creation."""
    return state_dir(root) / "agents"


def handoffs_dir(root: Path) -> Path:
    """Return the directory where workers write their handoff markdown."""
    return state_dir(root) / "handoffs"


def logs_dir(root: Path) -> Path:
    """Return the path to the logs directory."""
    return state_dir(root) / "logs"


def reports_dir(root: Path) -> Path:
    """Return the path to the completion reports directory."""
    return state_dir(root) / "reports"


def init_state_dir(root: Path) -> None:
    """Create the OSW state directory structure and the default state.json."""
    for d in (state_dir(root), agents_dir(root), handoffs_dir(root),
              logs_dir(root), reports_dir(root)):
        d.mkdir(parents=True, exist_ok=True)

    default_state = {
        "version": 3,
        "project_root": str(root),
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
    _atomic_write_json(state_file(root), state)


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)


# ---------------------------------------------------------------------------
# Per-agent records
# ---------------------------------------------------------------------------

def agent_file(root: Path, agent_id: str) -> Path:
    return agents_dir(root) / f"{agent_id}.json"


def alloc_agent_id(root: Path) -> str:
    """Claim the next free agent id atomically.

    The agent record file itself is the lock: `open(..., "x")` fails if
    a concurrent `osw new` already claimed that id, in which case we
    move on to the next number.
    """
    agents_dir(root).mkdir(parents=True, exist_ok=True)
    n = 1
    for path in agents_dir(root).glob("agent_*.json"):
        stem = path.stem
        try:
            n = max(n, int(stem.split("_")[1]) + 1)
        except (IndexError, ValueError):
            continue
    while True:
        agent_id = f"agent_{n:03d}"
        try:
            with agent_file(root, agent_id).open("x", encoding="utf-8") as f:
                f.write("{}")
            return agent_id
        except FileExistsError:
            n += 1


def read_agent(root: Path, agent_id: str) -> dict:
    """Read one agent record. Raises FileNotFoundError if absent."""
    path = agent_file(root, agent_id)
    if not path.exists():
        raise FileNotFoundError(f"Agent record not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_agent(root: Path, agent: dict) -> None:
    """Atomically write one agent record."""
    _atomic_write_json(agent_file(root, agent["agent_id"]), agent)


def delete_agent(root: Path, agent_id: str) -> bool:
    """Remove an agent record. Returns True if a file was deleted."""
    try:
        agent_file(root, agent_id).unlink()
        return True
    except FileNotFoundError:
        return False


def list_agents(root: Path) -> dict[str, dict]:
    """Read every agent record, keyed by agent id, sorted by id."""
    agents: dict[str, dict] = {}
    directory = agents_dir(root)
    if not directory.exists():
        return agents
    for path in sorted(directory.glob("agent_*.json")):
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("agent_id"):
            agents[data["agent_id"]] = data
    return agents


def write_report(root: Path, agent_id: str, payload: dict) -> Path:
    """Write a completion report JSON file and return its path."""
    reports_dir(root).mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = reports_dir(root) / f"{agent_id}_{ts}.json"
    _atomic_write_json(path, payload)
    return path


def new_handoff_path(root: Path, agent_id: str) -> Path:
    """Pre-allocate the handoff markdown path for one task run."""
    handoffs_dir(root).mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    return handoffs_dir(root) / f"{agent_id}_{ts}.md"


def pid_is_running(pid: int) -> bool:
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
