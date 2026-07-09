---
name: osw
description: Manage Orca-backed agent sessions from the current directory
---

# OSW - Orca Agent Supervisor

Manage Orca agent terminals from the current project directory.
Scripts live alongside this file: `osw.py` and the `osw/` package are
in the same directory as this `SKILL.md`.

## First-Time Setup

1. Initialize once in the target repo:
   `python <skill-dir>/osw.py init`
2. Optionally inspect provider models:
   `python <skill-dir>/osw.py models claude`
   `python <skill-dir>/osw.py models codex`
   `python <skill-dir>/osw.py models pi`
3. Start workers directly:
   `python <skill-dir>/osw.py new claude --prefix code --model sonnet --thinking high "<task>"`
   `python <skill-dir>/osw.py new pi --prefix research --model deepseek/deepseek-v4-flash --thinking low "<task>"`
4. Continue existing work:
   `python <skill-dir>/osw.py use <agent-id-or-terminal> --prefix debug "<prompt>"`

Prefer semantic prefixes for agent ids: `research`, `code`, `test`,
`debug`, `review`, or `misc`. Without `--prefix`, OSW uses the default
`agent_NNN` sequence.
When `use` targets an existing OSW agent id, OSW reuses that same agent
id and terminal context. In that mode, do not pass `--prefix`; it is only
for `new` and for adopting a bare terminal handle.

The CLI does not write `CLAUDE.md`; the operator or LLM should record
agent allocation there after the planning conversation.

`new` automatically adds provider launch flags intended to keep worker
sessions from blocking on permission prompts:
`claude --dangerously-skip-permissions`,
`codex --dangerously-bypass-approvals-and-sandbox`, and `pi --approve`.

## Commands

All commands operate on `.orca/osw/` in the current working directory.
There is no long-running `serve` daemon.

- `python <skill-dir>/osw.py init` - initialize state and scan agent CLIs
- `python <skill-dir>/osw.py models <provider> [--json]` - inspect provider model options without writing OSW state
- `python <skill-dir>/osw.py new <provider> [--prefix <prefix>] [--model <model>] [--thinking <value>] "<prompt>"` - create an agent terminal, register it, start one detached watcher, then return
- `python <skill-dir>/osw.py use <agent-id-or-terminal> [--prefix <prefix>] "<prompt>"` - reuse an existing agent id or adopt a terminal handle and start one detached watcher
- `python <skill-dir>/osw.py all "<message>"` - send a one-line message to every unfinished managed agent
- `python <skill-dir>/osw.py del <agent_id> [--close]` - stop the watcher record and optionally close the terminal
- `python <skill-dir>/osw.py list [--json]` - list managed agents by reading `.orca/osw/agents/*.json` and one `orca worktree ps --json` snapshot
- `python <skill-dir>/osw.py logs [--agent <agent_id>] [--tail N]` - show log files, recent structured events, and the latest text log tail
- `python <skill-dir>/osw.py status` - show state and watcher counts

Where `<skill-dir>` is the directory containing this `SKILL.md`.

## Runtime Model

`state.json` contains OSW project state only:

```json
{
  "version": 3,
  "project_root": "/path/to/project"
}
```

Runtime agent state lives in one file per worker under
`.orca/osw/agents/<agent_id>.json`.

`new` and `use` return quickly after they create or adopt a terminal,
write the agent file, and launch a detached watcher process. The watcher
is the only process that drives that agent after registration.

The watcher uses Orca's native structured state:

- `orca terminal show --terminal <handle>` maps a terminal to `tabId:leafId`
- `orca worktree ps --json` reports recognized agents by `paneKey`,
  including `state`, `prompt`, `toolName`, `lastAssistantMessage`,
  `stateStartedAt`, and `updatedAt`

For CLIs Orca does not recognize in `worktree ps`, the watcher falls
back to terminal `lastOutputAt` idle detection.

## Debugging Logs

Runtime logs live under `.orca/osw/logs/`:

- `events.jsonl` is the structured event stream. It records CLI and
  watcher milestones with `component`, `event`, `agent_id`, `terminal`,
  `phase`, `message`, and small JSON `data`.
- `osw_<timestamp>_<pid>.log` is the per-process text log.
- `agent_001_<timestamp>_<pid>.log` is the watcher text log for one
  worker.

Use `python <skill-dir>/osw.py logs --agent agent_001 --tail 100` when a
worker is stuck or finished unexpectedly. The event stream is the first
place to check; JSON reports under `.orca/osw/reports/` are final
completion artifacts, not a replacement for process logs.

## Completion Flow

OSW uses a two-phase completion flow:

1. The watcher sends the user's task prompt and waits for the worker turn
   to finish.
2. The watcher sends one fixed, single-line machine instruction asking
   the worker to write an English markdown handoff under
   `.orca/osw/handoffs/`, waits for that second turn to finish, verifies
   the file, writes a JSON report under `.orca/osw/reports/`, and sends a
   single-line notification to the caller terminal.

Notification format:

```text
# [osw] task-finished agent=agent_001 terminal=term_xxx report=<path> handoff=<path> summary=...
```

Error notifications use:

```text
# [osw] task-error agent=agent_001 terminal=term_xxx reason=<source> report=<path>
```

All terminal messages must be single-line. On Windows, multiline text
sent through `orca ... --text` can be truncated at newlines.

## Directory Isolation

Each directory is an independent OSW management scope. Agents in one
directory cannot see or affect agents in another, even if they share the
same git repo.
