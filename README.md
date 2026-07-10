# OSW — Orca Agent Supervisor

[中文版](README_zh.md)

A lightweight Python tool for managing Orca-backed agent sessions from the current directory. OSW starts or adopts agent terminals, launches detached watchers, detects task completion through Orca's native agent state (with idle-signal fallback), forces a handoff pass, and reports the resulting handoff file back to the caller.

## Features

- **Directory-scoped** — each working directory is an independent management scope, even across git worktrees
- **Detached watchers** — each managed agent gets a watcher process that monitors completion without blocking the CLI
- **Automatic handoff** — detects task completion, sends a fixed wrap-up instruction with a pre-allocated handoff path, verifies the handoff markdown, notifies the caller terminal
- **Minimal dependencies** — only `anyio`, `typer`, and optionally `rich` for colored logs

## Requirements

- Python 3.10+
- [Orca](https://orca.dev) running and `orca` CLI on PATH
- `anyio` and `typer`:

```
python -m pip install anyio typer rich
```

## Quick Start

See [QUICK.md](QUICK.md) for a step-by-step walkthrough.

```bash
python skill/osw.py init
python skill/osw.py models pi

python skill/osw.py new claude --prefix code --model sonnet --thinking high "Fix the failing tests"
python skill/osw.py new codex --prefix research --model gpt-5 --thinking medium "Refactor the provider layer"
python skill/osw.py list
python skill/osw.py status
```

## Commands

| Command | Description |
|---|---|
| `init` | Initialize `.orca/osw/` state directory |
| `models <provider> [--json]` | Inspect provider model options without writing state |
| `new <provider> [--prefix <prefix>] [--model <model>] [--thinking <value>] "<prompt>"` | Create a new agent terminal with a task |
| `use <agent-id-or-terminal> [--prefix <prefix>] "<prompt>"` | Adopt an existing Orca terminal or continue from an OSW agent id |
| `all "<message>"` | Broadcast a message to all managed agents |
| `del <agent_id> [--close]` | Remove an agent from management |
| `list [--json]` | List managed agents |
| `logs [--agent <agent_id>] [--tail N]` | Show OSW logs and events |
| `status` | Show OSW state and agent counts |

### Options

- `new` accepts `claude`, `codex`, or `pi` as the provider.
- `--prefix` gives semantic agent ids such as `research_001`, `code_001`, `test_001`, `debug_001`, `review_001`, or `misc_001`; omit it for `agent_NNN`.
- `use <agent-id>` reuses that exact agent id and terminal context; `--prefix` is only for `new` or `use <terminal-handle>`.
- `--model` is passed through unchanged.
- `--thinking` is translated per provider: `claude --effort`, `codex -c model_reasoning_effort=...`, `pi --thinking`.
- OSW adds the provider's non-blocking autonomy flag by default: `claude --dangerously-skip-permissions`, `codex --dangerously-bypass-approvals-and-sandbox`, `pi --approve`.
- `new` and `use` accept `--caller-terminal <handle>` to receive completion reports on a parent terminal.
- `del --close` also asks Orca to close the terminal.
- `list --json` outputs raw JSON.

## Architecture

```
new/use
  ├── create or adopt Orca terminal
  ├── write .orca/osw/agents/agent_NNN.json
  ├── spawn detached watcher(agent_NNN)
  └── return immediately
```

OSW owns orchestration only: state initialization, Orca terminal creation/adoption, detached watcher startup, logs, and completion reports. Provider command construction lives in `skill/osw/providers.py`.

### Watcher Lifecycle

```
wait until the pane is ready (`worktree ps` reports it done; tui-idle fallback for untracked CLIs)
 → send the task prompt
 → wait until Orca reports the turn done (`worktree ps`, correlated with the send time; idle fallback for untracked CLIs)
 → send the fixed wrap-up instruction with a pre-allocated handoff path
 → wait again, verify .orca/osw/handoffs/<agent>_<ts>.md exists (one retry)
 → write a JSON report and notify the caller terminal
```

`new` starts the bare provider TUI only — the watcher always sends the task
prompt as its own turn, so a briefly idle TUI at startup can never be
mistaken for a completed task.

## Directory Layout

### Project

```
orca-osw/
  skill/
    SKILL.md          # skill definition
    references/       # orchestration and operations guides
    osw.py            # entry point
    osw/
      cli.py          # typer CLI commands
      deps.py         # dependency checker
      log.py          # structured events and file logging
      orca_cli.py     # async Orca CLI wrapper
      process.py      # hidden subprocess helpers (Windows)
      providers.py    # provider commands and model discovery
      state.py        # state model and file I/O
      watcher.py      # detached completion watcher
  tests/              # 79 tests (unit + integration)
  conftest.py         # sys.path setup for tests
```

### Runtime State

Created in the working directory where you run `init`:

```
<cwd>/
  .orca/
    osw/
      state.json      # OSW project state
      agents/          # one JSON record per managed agent
      handoffs/        # worker handoff markdown files
      logs/
      reports/
```

## State Model

```json
{
  "version": 3,
  "project_root": "D:\\project"
}
```

Runtime agent records live under `.orca/osw/agents/`. Model discovery is read-only; `models <provider>` never writes `state.json`.

## Testing

```bash
python -m pip install pytest
python -m pytest -v
```

79 tests covering unit tests for every module and integration tests with mocked Orca CLI.

## License

MIT
