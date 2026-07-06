# OSW — Orca Agent Supervisor

[中文版](README_zh.md)

A lightweight Python tool for managing Orca-backed agent sessions from the current directory. OSW starts or adopts agent terminals, runs a foreground AnyIO supervisor, detects task completion through idle signals, forces a handoff pass, and reports the resulting handoff file back to the caller.

## Features

- **Directory-scoped** — each working directory is an independent management scope, even across git worktrees
- **Async supervisor** — AnyIO-based concurrency; inbox polling, per-agent watchers, and reconciliation run without blocking each other
- **Automatic handoff** — detects agent idle, triggers `/handoff`, extracts `HANDOFF_*.md`, notifies the caller terminal
- **Minimal dependencies** — only `anyio` and `typer`

## Requirements

- Python 3.10+
- [Orca](https://orca.dev) running and `orca` CLI on PATH
- `anyio` and `typer`:

```
python -m pip install anyio typer
```

## Quick Start

See [QUICK.md](QUICK.md) for a step-by-step walkthrough.

```bash
python skill/osw.py init
python skill/osw.py serve          # keep running in this terminal

# in another terminal, same directory:
python skill/osw.py new "Fix the failing tests"
python skill/osw.py list
python skill/osw.py status
```

## Commands

| Command | Description |
|---|---|
| `init` | Initialize `.orca/osw/` state directory |
| `serve` | Run the foreground supervisor (required by other commands) |
| `new "<prompt>"` | Create a new agent terminal with a task |
| `use --terminal <handle> "<prompt>"` | Adopt an existing Orca terminal |
| `all "<message>"` | Broadcast a message to all managed agents |
| `del <agent_id> [--close]` | Remove an agent from management |
| `list [--json]` | List managed agents |
| `status` | Show supervisor status and agent counts |

### Options

- `new` and `use` accept `--caller-terminal <handle>` to receive completion reports on a parent terminal.
- `del --close` also asks Orca to close the terminal.
- `list --json` outputs raw JSON.

## Architecture

```
serve (foreground)
  ├── inbox_loop      — polls .orca/osw/inbox/ every 0.5s for CLI requests
  ├── reconcile_loop  — verifies terminals still exist every 30s
  ├── watcher(agent_001)  — per-agent, spawned dynamically
  ├── watcher(agent_002)
  └── ...
```

Only `serve` writes `state.json`. CLI commands (`new`, `use`, `all`, `del`) communicate with `serve` by writing JSON request files to `inbox/` and polling `results/` for responses (15-second timeout).

### Watcher Lifecycle

```
task prompt sent
 → wait for tui-idle (up to 10 min)
 → send forced /handoff prompt
 → wait for tui-idle again (up to 2 min)
 → extract HANDOFF_*.md filename from output
 → report to caller terminal
```

## Directory Layout

### Project

```
orca-osw/
  skill/
    SKILL.md          # Codex skill definition
    osw.py            # entry point
    osw/
      cli.py          # typer CLI commands
      deps.py         # dependency checker
      handoff.py      # handoff extraction and reporting
      orca_cli.py     # async Orca CLI wrapper
      server.py       # AnyIO supervisor
      state.py        # state model and file I/O
  tests/              # 62 tests (unit + integration)
  conftest.py         # sys.path setup for tests
```

### Runtime State

Created in the working directory where you run `init`:

```
<cwd>/
  .orca/
    osw/
      state.json      # authoritative state (only serve writes it)
      inbox/           # CLI → serve request files
      results/         # serve → CLI response files
      logs/
```

## State Model

```json
{
  "version": 1,
  "project_root": "D:\\project",
  "serve": { "pid": 12345, "started_at": "..." },
  "models": {
    "strong":  [{ "name": "strong-default",  "command": "codex" }],
    "medium":  [{ "name": "medium-default",  "command": "pi" }],
    "weak":    [{ "name": "weak-default",    "command": "pi" }]
  },
  "agents": {},
  "errors": []
}
```

Edit `models` in `state.json` to configure which provider command maps to each strength tier. OSW uses the first `strong` entry when creating new terminals.

## Testing

```bash
python -m pip install pytest
python -m pytest -v
```

62 tests covering unit tests for every module and integration tests with mocked Orca CLI.

## License

MIT
