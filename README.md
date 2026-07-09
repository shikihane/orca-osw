# OSW — Orca Agent Supervisor

[中文版](README_zh.md)

A lightweight Python tool for managing Orca-backed agent sessions from the current directory. OSW starts or adopts agent terminals, launches detached watchers, detects task completion through idle signals, forces a handoff pass, and reports the resulting handoff file back to the caller.

## Features

- **Directory-scoped** — each working directory is an independent management scope, even across git worktrees
- **Detached watchers** — each managed agent gets a watcher process that monitors completion without blocking the CLI
- **Automatic handoff** — detects agent idle, triggers `/handoff`, extracts `HANDOFF_*.md`, notifies the caller terminal
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

python skill/osw.py new claude --model sonnet --thinking high "Fix the failing tests"
python skill/osw.py new codex --model gpt-5 --thinking medium "Refactor the provider layer"
python skill/osw.py list
python skill/osw.py status
```

## Commands

| Command | Description |
|---|---|
| `init` | Initialize `.orca/osw/` state directory |
| `models <provider> [--json]` | Inspect provider model options without writing state |
| `new <provider> [--model <model>] [--thinking <value>] "<prompt>"` | Create a new agent terminal with a task |
| `use <agent-id-or-terminal> "<prompt>"` | Adopt an existing Orca terminal or continue from an OSW agent id |
| `all "<message>"` | Broadcast a message to all managed agents |
| `del <agent_id> [--close]` | Remove an agent from management |
| `list [--json]` | List managed agents |
| `logs [--agent <agent_id>] [--tail N]` | Show OSW logs and events |
| `status` | Show OSW state and agent counts |

### Options

- `new` accepts `claude`, `codex`, or `pi` as the provider.
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
      watcher.py      # detached completion watcher
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

68 tests covering unit tests for every module and integration tests with mocked Orca CLI.

## License

MIT
