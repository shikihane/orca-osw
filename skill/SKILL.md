---
name: osw
description: Manage Orca-backed agent sessions from the current directory
---

# OSW — Orca Agent Supervisor

Manage Orca agent terminals from the current project directory.
Scripts live alongside this file — `osw.py` and the `osw/` package are in the same directory as this SKILL.md.

## First-time setup (REQUIRED — you, the operating agent, do this)

OSW ships with ZERO model configuration. Nothing about the machine is
assumed; you must detect the environment and configure it before `new`
will work.

1. `python <skill-dir>/osw.py init --no-interactive` — creates
   `.orca/osw/` and scans PATH for known agent CLIs (claude, codex,
   pi, gemini, aider, ...). ALWAYS pass `--no-interactive`: without it
   init auto-detects a real terminal and may start a blocking
   question-and-answer setup meant for humans typing at a keyboard.
2. Discover model variants: `python <skill-dir>/osw.py model variants <cli>`
   queries the CLI itself (pi --list-models, claude --help aliases).
   For CLIs it cannot discover (codex, gemini), probe `<cli> --help`
   yourself to learn the model flags
   (e.g. `codex -c model_reasoning_effort=high`).
3. Assign tiers explicitly — strong for the most capable/expensive,
   medium for everyday tasks, weak for cheap bulk work:

   ```
   python <skill-dir>/osw.py model add --tier strong --name codex-high --command "codex -c model_reasoning_effort=high"
   python <skill-dir>/osw.py model add --tier medium --name claude-sonnet --command "claude --model sonnet"
   python <skill-dir>/osw.py model add --tier weak   --name claude-haiku  --command "claude --model haiku"
   ```

4. Verify: `python <skill-dir>/osw.py model list`

`new` uses the medium tier by default; override with `--tier` or
`--model <name>`.

## Commands

All commands operate on `.orca/osw/` in the current working directory.

- `python <skill-dir>/osw.py init` — Initialize state + scan agent CLIs
- `python <skill-dir>/osw.py model scan|variants|list|add|remove` — Manage model tiers
- `python <skill-dir>/osw.py serve` — Run the foreground supervisor (required for other commands)
- `python <skill-dir>/osw.py new "<prompt>" [--tier strong|medium|weak] [--model <name>]` — Create an agent with a task
- `python <skill-dir>/osw.py use --terminal <handle> "<prompt>"` — Adopt an existing terminal
- `python <skill-dir>/osw.py all "<message>"` — Broadcast to all managed agents
- `python <skill-dir>/osw.py del <agent_id> [--close]` — Remove agent from management
- `python <skill-dir>/osw.py list [--json]` — List managed agents
- `python <skill-dir>/osw.py status` — Show supervisor status

Where `<skill-dir>` is the directory containing this SKILL.md.

## How completion works

`serve` registers itself as the Orca orchestration coordinator. `new`
dispatches tasks through Orca's official protocol (task-create +
dispatch), so the worker self-reports a structured `worker_done`
(summary, files modified). The supervisor then writes a JSON report to
`.orca/osw/reports/` and pushes a single-line notification to the
caller terminal:

```
# [osw] task-finished agent=agent_001 terminal=term_xxx report=<path> summary=...
```

Read the report file for full context. If a worker never reports,
a 60s stable-idle fallback marks it done (`completion_source` in the
report tells you which path fired).

## Directory Isolation

Each directory is an independent OSW management scope. Agents in one
directory cannot see or affect agents in another, even if they share
the same git repo.
