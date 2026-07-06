---
name: osw
description: Manage Orca-backed agent sessions from the current directory
---

# OSW — Orca Agent Supervisor

Manage Orca agent terminals from the current project directory.

## Commands

All commands operate on `.orca/osw/` in the current directory.

- `python osw.py init` — Initialize OSW state
- `python osw.py serve` — Run the foreground supervisor (required for other commands)
- `python osw.py new "<prompt>"` — Create a new agent terminal with a task
- `python osw.py use --terminal <handle> "<prompt>"` — Adopt an existing terminal
- `python osw.py all "<message>"` — Broadcast to all managed agents
- `python osw.py del <agent_id> [--close]` — Remove agent from management
- `python osw.py list [--json]` — List managed agents
- `python osw.py status` — Show supervisor status

## Directory Isolation

Each directory is an independent OSW management scope. Agents in one directory
cannot see or affect agents in another, even if they share the same git repo.

## Workflow

1. `python osw.py init` in your project root
2. Start the supervisor: `python osw.py serve` (keep running)
3. In another terminal: `python osw.py new "Fix the failing tests"`
4. Monitor: `python osw.py list`
5. The supervisor auto-detects task completion and generates handoff files
