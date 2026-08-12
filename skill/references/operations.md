# Operations

## Prerequisites

Require Python 3.10+, `anyio`, `typer`, and an `orca` CLI connected to a running
Orca runtime. Install missing Python packages with
`python -m pip install anyio typer`; `rich` is optional. Check runtime readiness
and OSW's required response contract with `python <skill-dir>/osw.py doctor`.
Use `--json` for a machine-readable report. Start Orca with `orca open` when
needed.

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

## Compatibility and Recovery

Run `doctor` after every Orca upgrade and before dispatching the first worker.
It probes `status`, `worktree current`, `terminal list`, `terminal show`, and
`worktree ps` without writing OSW state. A non-zero result means do not
dispatch: `orca_contract_mismatch` identifies an upstream response shape or
required-field change; other typed errors identify runtime or CLI failures.
Checks that require a live terminal or recognized agent may be `unverified`.

CLI selection follows the Orca environment: `ORCA_CLI_COMMAND` overrides the
command, `ORCA_DEV_REPO_ROOT` selects `orca-dev`, and Linux outside an
Orca-managed terminal selects `orca-ide`; the default is `orca` on PATH.

Terminal handles are scoped to one Orca runtime. OSW records each managed
pane's `tabId:leafId`; if Orca reports `terminal_handle_stale`, the watcher
looks up the pane's current handle, persists it, and retries the read, wait, or
send operation once. If the pane no longer exists, normal terminal-lost/error
handling still applies.

## Dispatch and Continue

```text
python <skill-dir>/osw.py new <provider> [--prefix <role>] [--model <model>] [--thinking <level>] [--caller-terminal <handle>] [--no-notify] "<task>"
python <skill-dir>/osw.py use <agent-id-or-terminal> [--prefix <role>] [--caller-terminal <handle>] [--no-notify] "<task>"
```

OSW resolves the notification target in this order: `--caller-terminal`,
then the inherited `ORCA_TERMINAL_HANDLE` (validated first; a stale handle
is dropped with a warning instead of aborting the dispatch), then
marker-based auto-detection in an interactive Orca terminal. If no caller
can be identified, the dispatch is rejected — an agent that can never
report completion is not created. Pass `--no-notify` to proceed anyway;
OSW prints a warning and you must poll `list`/`status` yourself.

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

If a dispatch went out with `--no-notify`, use `list`, `status`, and the
reports directory instead of assuming the worker is still running.
Notifications that cannot be delivered (missing or stale caller handle)
are recorded as ERROR events in `osw logs`, never dropped silently.

These notifications are proactive OSW watcher messages delivered with
`orca terminal send`; they are not Orca-native cross-session notifications.
They require a resolved caller terminal at dispatch time (`--caller-terminal`,
a valid inherited `ORCA_TERMINAL_HANDLE`, or auto-detection), unless the
dispatch explicitly opted out with `--no-notify`.

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
