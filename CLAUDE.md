# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

See also `AGENTS.md` for repository guidelines shared across agent tools.

## What This Is

OSW ("Orca Agent Supervisor") is a Python CLI that dispatches and supervises
Orca-backed agent sessions (`claude`, `codex`, `pi`) in the current working
directory. There is **no daemon**: `new`/`use` creates or adopts an Orca
terminal, writes one agent record, spawns one detached watcher process, and
returns immediately. Orca itself is the source of truth for agent state.

Runtime dependencies are `anyio` and `typer` only (`rich` optional for
colored logs, `pytest` for tests).

## Commands

Run from the repo root:

```bash
python -m pip install anyio typer rich pytest   # dev setup

pytest -q                                       # full test suite
pytest tests/test_cli.py -v                     # one file
pytest tests/test_watcher.py::test_name -v      # one test

python skill/osw.py init                        # init .orca/osw/ state in cwd
python skill/osw.py models <claude|codex|pi>    # read-only model discovery
python skill/osw.py new <provider> --prefix <role> --model <m> --thinking <t> "<task>"
python skill/osw.py use <agent-id|terminal-handle> "<follow-up>"
python skill/osw.py list --json
python skill/osw.py status
python skill/osw.py logs --agent <agent_id>
```

`conftest.py` inserts `skill/` into `sys.path`, so tests import the package as
`osw.*` — run pytest from the repo root. Unit tests mock Orca calls with
`AsyncMock`; never require a live Orca instance.

## Architecture

Module responsibilities (keep code in its lane):

- `skill/osw/cli.py` — all typer commands. `new` creates an Orca terminal
  running the bare provider TUI (never the task prompt — the watcher sends
  it), allocates an agent id, writes the agent record, spawns the watcher.
  `use` adopts an existing terminal after `terminal_show` confirms it belongs
  to the current worktree; passing an existing agent id reuses that id (only
  allowed once the agent is done/error/lost). The hidden `watch` command is
  the watcher process entry point.
- `skill/osw/watcher.py` — per-agent detached supervisor, one process per
  dispatched task; `new` and `use` share this one state machine. Readiness
  first (`worktree ps` "done" for tracked panes, `tui-idle` fallback for
  untracked ones), then phase "task" (send prompt, wait for the turn to
  finish) then phase "handoff" (send the fixed `HANDOFF_TEMPLATE`
  instruction, verify the handoff markdown exists, retry once). Finally
  writes a JSON report and notifies the caller terminal with a single-line
  `# [osw] ...` message. Also notifies on `AskUserQuestion` waits and
  5-minute stalls.
- `skill/osw/orca_cli.py` — thin async wrapper around `orca ... --json` via
  `anyio.run_process`; raises `OrcaError`. Caller-terminal detection prints a
  UUID marker and finds which terminal's preview contains it (`terminal show`
  alone can't identify "the terminal I'm running in").
- `skill/osw/providers.py` — provider command construction (autonomy flags,
  `--model`/`--thinking` translation per provider) and read-only model
  variant discovery by probing CLIs.
- `skill/osw/state.py` — all file I/O under `.orca/osw/`: `state.json`,
  `agents/<agent_id>.json`, `reports/`, `handoffs/`, `logs/`. Writes are
  atomic (temp file + `os.replace`, with retry on transient Windows lock
  errors, winerror 5/32 → `StateWriteError`). `alloc_agent_id` uses
  `open(..., "x")` on the record file as the lock.
- `skill/osw/log.py` — structured JSONL events plus per-agent log files.
- `skill/osw/process.py` — `hidden_subprocess_kwargs()` so subprocesses spawn
  without console windows on Windows.

Completion detection: the watcher maps the terminal to a `paneKey`
(`tabId:leafId`) and polls `orca worktree ps --json` for that pane's agent
entry (`state == "done"` with `stateStartedAt` after the prompt was sent and
newer than the pane's pre-send "done" timestamp). An idle observation alone
is never completion — a turn with no task-correlated evidence fails closed
as `error`, not `done`. CLIs Orca does not recognize (e.g. `pi`) fall back
to `lastOutputAt` stable-idle detection (60s of silence after new output).

Ownership invariant: the CLI writes the agent record at creation; after the
watcher starts, the watcher process is the only writer. `list`/`status` merge
agent files with a single `worktree ps` snapshot by `pane_key` plus watcher
PID liveness.

`state.json` holds only `{version: 3, project_root}`. `models` never writes
state.

## Orca CLI Gotchas

- Orca JSON responses are nested under `result`; unwrap `result.terminal`,
  `result.terminals`, or `result.worktrees`.
- `orca terminal show` without `--terminal` returns the UI-focused terminal,
  not the current process terminal.
- Multiline text through `orca ... --text` gets truncated on Windows. Every
  message sent to a terminal must be a single line (see `watcher.one_line`).
- `orca worktree ps --json` fields per agent: `state`, `prompt`, `toolName`,
  `lastAssistantMessage`, `paneKey`, `stateStartedAt`, `updatedAt`.

## Hard Constraints

- Do not reintroduce a `serve` daemon or Orca orchestration protocol
  wrappers.
- Do not add CLI options the user did not request.
- The only OSW-authored text ever injected into a worker is the fixed,
  single-line `watcher.HANDOFF_TEMPLATE`.
- Never edit `.orca/osw/` runtime records by hand; never commit generated
  `.orca/` state, logs, reports, or handoff files (`.gitignore` covers this).
- Provider launch commands include autonomy/bypass flags by design — only
  for trusted, externally controlled workspaces.

## Conventions

- Agent ids are `<prefix>_NNN` (`agent_001`, `research_001`, `debug_001`);
  prefixes: `research`, `code`, `test`, `debug`, `review`, `misc`.
- 4-space indentation, type hints on public helpers, small focused functions.
- Commit style: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`.
- Add or update tests before changing behavior; put them next to the affected
  module (`tests/test_providers.py` for launch commands, `tests/test_cli.py`
  for user-visible CLI behavior).

## Skill Installation

Installing/updating the OSW skill means physically copying the complete
`skill/` directory to each destination — never junctions, symlinks, hard
links, or any link-like mechanism, and never resolving back to this repo:

- Codex: `C:\Users\shiki\.codex\skills\osw\`
- Claude: `C:\Users\shiki\.claude\skills\osw\`

After every update, validate both destinations are independent physical
copies that still work if this repository path is unavailable.
