# Operations

## Prerequisites

Require Python 3.10+, `anyio`, `typer`, and an `orca` CLI connected to a running
Orca runtime. Install missing Python packages with
`python -m pip install anyio typer`; `rich` is optional. Check runtime readiness
with `orca status` and start it with `orca open` when needed.

## Setup and Discovery

Run commands from the target worktree. `init` creates `.orca/osw/` and scans
available agent CLIs. `models` is read-only and reports locally available
choices.

```text
python <skill-dir>/osw.py init
python <skill-dir>/osw.py models <provider> [--json]
```

OSW supports `claude`, `codex`, `pi`, and `kimi` for `new`. `kimi` takes
`--model` (a config.toml alias such as `kimi-code/k3`) but rejects
`--thinking`: its effort is configured per model alias, not on the command
line.

## Dispatch and Continue

```text
python <skill-dir>/osw.py new <provider> [--prefix <role>] [--model <model>] [--thinking <level>] [--caller-terminal <handle>] "<task>"
python <skill-dir>/osw.py use <agent-id-or-terminal> [--prefix <role>] [--caller-terminal <handle>] "<task>"
```

In an interactive terminal, OSW attempts to detect the caller automatically;
pass `--caller-terminal` only when a known handle must receive notifications.

`use <agent-id>` requires a finished managed agent and preserves its id and
provider context; do not pass `--prefix`. `use <terminal>` adopts a terminal in
the same worktree and may use `--prefix`.

`new` and `use` return after starting one detached watcher. Do not block the
coordinator waiting in the worker terminal.

## Observe

```text
python <skill-dir>/osw.py list [--json]
python <skill-dir>/osw.py status
python <skill-dir>/osw.py logs [--agent <agent-id>] [--tail <n>]
```

Runtime artifacts are under `.orca/osw/`: agent records, handoffs, reports, and
logs. Reports are machine-readable completion records; handoffs contain the
worker's work summary.

## Notifications

| Notification | Coordinator action |
|---|---|
| `agent-waiting` | Answer the worker directly in its terminal. |
| `agent-stalled` | Inspect `logs`, then inspect the terminal before intervening. |
| `task-finished` | Open the report and handoff; move the task to acceptance. |
| `task-error` | Read the report reason and logs before retrying. |

A `task-finished` notification can report `handoff_missing`; inspect the actual
workspace and terminal before deciding whether the result is usable.

If caller detection failed, use `list`, `status`, and the reports directory
instead of assuming the worker is still running.

## Intervention and Cleanup

```text
python <skill-dir>/osw.py all "<urgent one-line message>"
python <skill-dir>/osw.py del <agent-id> [--close]
```

Use `all` only for urgent coordination across unfinished workers. `del` removes
the management record; `--close` also closes the Orca terminal.

All terminal messages must be single-line because Windows transport can
truncate text at newlines. Each working directory is an independent OSW scope;
OSW does not create or manage worktrees.
