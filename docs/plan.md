# OSW Implementation Plan

Spec: `D:\bit_master\research\orca_cli\docs\superpowers\specs\2026-07-06-osw-orca-agent-supervisor-design.md`

## Global Constraints

- Dependencies: only `anyio` and `typer`. Print install instructions on missing.
- All state under `<cwd>/.orca/osw/`. Only `serve` writes `state.json`.
- Other commands communicate with `serve` via inbox/results files.
- Current-directory isolation: filter everything by resolved cwd.
- AnyIO concurrency: never block the whole server for one agent.
- Agent IDs: `agent_001`, `agent_002`, ... (stable, sequential).
- Handoff filename: must start with `HANDOFF_` and end with `.md`. OSW extracts, never generates.
- Forced handoff prompt is a fixed Chinese string.
- CLI via typer. `osw.py` is a thin entry point.
- Orca calls use `anyio.run_process()` / `anyio.open_process()` with argument lists, not shell strings.
- 15-second timeout for `new`/`use` command results.
- `serve` required for `new`, `use`, `all`, `del` — fail immediately if not running.

## Tasks

### Task 1: Project scaffolding + deps.py

Create:
- `osw/__init__.py` (empty)
- `osw/deps.py` — check for `anyio` and `typer`, print install message and exit(1) on missing
- `tests/__init__.py` (empty)
- `tests/test_deps.py` — test that missing-dep message is correct

### Task 2: state.py — State model and path utilities

Create `osw/state.py` with:

- `resolve_project_root() -> Path` — returns `Path.cwd().resolve()`
- `state_dir(root: Path) -> Path` — returns `root / ".orca" / "osw"`
- `state_file(root: Path) -> Path` — returns `state_dir(root) / "state.json"`
- `inbox_dir(root: Path) -> Path` — returns `state_dir(root) / "inbox"`
- `results_dir(root: Path) -> Path` — returns `state_dir(root) / "results"`
- `logs_dir(root: Path) -> Path` — returns `state_dir(root) / "logs"`
- `init_state_dir(root: Path)` — create all subdirs, write default `state.json`
- `read_state(root: Path) -> dict` — read and return state dict
- `write_state(root: Path, state: dict)` — atomically write state dict
- `next_agent_id(state: dict) -> str` — return next `agent_NNN` id
- `write_request(root: Path, command: str, payload: dict) -> str` — write request JSON to inbox, return request_id
- `read_result(root: Path, request_id: str, timeout: float = 15.0) -> dict | None` — poll results dir for matching file, return parsed JSON or None on timeout
- `write_result(root: Path, request_id: str, payload: dict)` — write result JSON to results dir
- `is_serve_running(root: Path) -> bool` — check if serve PID is alive

Default state.json shape for the current provider-launch flow: `version` and `project_root` only. Runtime agent records live under `.orca/osw/agents/`.

Tests in `tests/test_state.py`:
- Path resolution
- State init creates dirs and valid JSON
- next_agent_id increments correctly
- Request/result file round-trip
- write_state / read_state round-trip

### Task 3: orca_cli.py — Orca CLI wrapper

Create `osw/orca_cli.py` with async functions:

- `run_orca(*args: str) -> dict` — run `orca <args> --json`, parse JSON stdout, raise on non-zero exit
- `worktree_current() -> dict` — `orca worktree current --json`
- `terminal_list(worktree_path: str) -> list[dict]` — `orca terminal list --worktree path:<worktree_path> --json`
- `terminal_create(worktree_path: str, command: str) -> dict` — create a new terminal in the worktree
- `terminal_send(handle: str, message: str) -> dict` — send a message to a terminal
- `terminal_wait(handle: str, event: str = "tui-idle", timeout_ms: int = 300000) -> dict` — `orca terminal wait --terminal <handle> --for <event> --timeout-ms <timeout_ms> --json`
- `terminal_close(handle: str) -> dict` — close terminal
- `terminal_info(handle: str) -> dict` — get terminal info including worktreePath

All functions use `anyio.run_process()` with argument lists.
Raise `OrcaError(message, returncode)` on failures.

Tests in `tests/test_orca_cli.py`:
- Test argument construction for each function (mock subprocess)
- Test JSON parsing
- Test error handling on non-zero exit

### Task 4: handoff.py — Handoff extraction and reporting

Create `osw/handoff.py` with:

- `FORCED_HANDOFF_PROMPT: str` — the fixed Chinese prompt from spec
- `extract_handoff_filename(text: str) -> str | None` — extract first `HANDOFF_*.md` filename from text using regex
- `format_completion_report(agent_id: str, terminal: str, handoff_file: str) -> str` — format the completion report per spec
- `format_handoff_failed_message() -> str` — return the fixed Chinese failure message

Tests in `tests/test_handoff.py`:
- Extract from various text formats (path, inline, multi-line)
- Returns None when no match
- Report formatting matches spec format exactly

### Task 5: server.py — AnyIO supervisor

Create `osw/server.py` with:

- `async def run_server(root: Path)` — main entry, run AnyIO task group with inbox_loop + reconcile_loop + per-agent watchers
- `async def inbox_loop(root: Path, state: dict, task_group: anyio.abc.TaskGroup)` — poll inbox dir every 0.5-1s, handle new/use/all/del requests, write results, start watchers
- `async def reconcile_loop(root: Path, state: dict)` — periodically verify terminals still exist and belong to cwd, mark lost agents
- `async def watcher(root: Path, state: dict, agent_id: str)` — wait for tui-idle via orca terminal wait, check completion, send forced handoff, extract filename, report to caller
- Handle request types:
  - `new`: create terminal via orca_cli, register in state, start watcher, return agent_id + handle
  - `use`: verify terminal belongs to cwd, register, start watcher, return agent_id + handle
  - `del`: cancel watcher, optionally close terminal, remove from state

State updates: only server writes state.json. Use a lock for state mutations.
PID recording: write own PID to state on startup, clear on shutdown.
Watcher cancellation: use anyio.CancelScope per watcher, track in a dict.

Tests in `tests/test_server.py`:
- inbox_loop processes a `new` request and writes result (mock orca_cli)
- `use` rejects terminal from wrong directory (mock orca_cli)
- watcher handles idle + forced handoff flow (mock orca_cli)
- `del` cancels watcher

### Task 6: cli.py + osw.py — CLI commands and entry point

Create `osw/cli.py` with typer app:

- `init()` — call `state.init_state_dir(root)`
- `serve()` — call `anyio.run(server.run_server, root)`
- `new(prompt: str)` — check serve running, write request, wait for result (15s), print agent_id + handle
- `use(terminal: str, prompt: str)` — check serve running, write request, wait for result (15s), print agent_id + handle
- `all(message: str)` — check serve running, write request, wait for result
- `del_(agent_id: str, close: bool = False)` — check serve running, write request, wait for result
- `list_(json_output: bool = False)` — read state, print agents table or JSON
- `status()` — read state, print serve status + agent counts + errors

Create `osw.py`:
```python
from osw.deps import check_deps
check_deps()
from osw.cli import app
app()
```

Create `skill/SKILL.md` — concise skill doc explaining osw.py commands and current-directory isolation.

Tests in `tests/test_cli.py`:
- `init` creates state dir
- `list` with no agents
- `status` when serve not running
- serve-required commands fail when serve not running

### Task 7: Integration tests

Create `tests/test_integration.py`:
- Full flow with mocked orca_cli: init → serve (background) → new → list → del
- Verify state transitions
- Verify request/result protocol end-to-end
