---
name: osw
description: Manage Orca-backed agent sessions from the current directory
---

# OSW — Orca Agent Supervisor

Manage Orca agent terminals from the current project directory.
Scripts live alongside this file — `osw.py` and the `osw/` package are in the same directory as this SKILL.md.

## Commands

All commands operate on `.orca/osw/` in the current working directory.
Run from the skill directory, or use the full path to `osw.py`.

- `python <skill-dir>/osw.py init` — Initialize OSW state
- `python <skill-dir>/osw.py serve` — Run the foreground supervisor (required for other commands)
- `python <skill-dir>/osw.py new "<prompt>"` — Create a new agent terminal with a task
- `python <skill-dir>/osw.py use --terminal <handle> "<prompt>"` — Adopt an existing terminal
- `python <skill-dir>/osw.py all "<message>"` — Broadcast to all managed agents
- `python <skill-dir>/osw.py del <agent_id> [--close]` — Remove agent from management
- `python <skill-dir>/osw.py list [--json]` — List managed agents
- `python <skill-dir>/osw.py status` — Show supervisor status

Where `<skill-dir>` is the directory containing this SKILL.md.

## Directory Isolation

Each directory is an independent OSW management scope. Agents in one directory
cannot see or affect agents in another, even if they share the same git repo.

## Workflow

1. `python <skill-dir>/osw.py init` in your project root
2. Start the supervisor: `python <skill-dir>/osw.py serve` (keep running)
3. In another terminal: `python <skill-dir>/osw.py new "Fix the failing tests"`
4. Monitor: `python <skill-dir>/osw.py list`
5. The supervisor auto-detects task completion and generates handoff files
