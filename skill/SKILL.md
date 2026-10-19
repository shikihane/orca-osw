---
name: osw
description: Use when delegating, coordinating, resuming, monitoring, or recovering Orca-backed Claude, Codex, Pi, OMP, or Kimi sessions in the current worktree
---

# OSW Agent Supervisor

Run the bundled CLI as `python <skill-dir>/osw.py`, where `<skill-dir>` is the
directory containing this file.

## Workflow

1. Use allocation decisions already present in context. Treat an absent
   provider, model, or thinking choice for the task type as undecided.
2. Ask the human for each undecided choice and record the answer in the current
   project instructions before dispatch. Do not infer a default or search for
   other instruction files.
3. After installing or upgrading Orca, validate its live contract with
   `python <skill-dir>/osw.py doctor` before dispatching.
4. Initialize the current worktree once with `python <skill-dir>/osw.py init`.
5. Decompose work, dispatch dependency-ready tasks, and keep integrating while
   detached watchers supervise workers.
6. Accept results only after checking the handoff and actual workspace state.

For multiple workers, dependencies, shared-worktree coordination, or model
selection, read [references/orchestration.md](references/orchestration.md)
before dispatching.

For command semantics, lifecycle notifications, continuation, takeover and
recovery (`enroll`, `guard`, `revive`, `stop`), diagnosis, or cleanup, read
[references/operations.md](references/operations.md).

## Quick Start

Inspect real model choices when the human needs options:

```text
python <skill-dir>/osw.py models <claude|codex|pi|omp|kimi|grok>
```

Create a fresh worker:

```text
python <skill-dir>/osw.py new <provider> --prefix <role> --model <model> --thinking <level> "<task>"
```

Use `research`, `code`, `test`, `debug`, `review`, or `misc` for `<role>`.
`omp` dispatches run through Orca's supervised orchestration: Orca creates
the terminal and injects the task, and the worker writes the handoff itself
before reporting `worker_done`. Do not pass `--thinking` for `omp`.
Continue the same task context after the worker finishes:

```text
python <skill-dir>/osw.py use <agent-id> "<follow-up>"
```

Use `new` for an independent perspective and `use` for continuation or
correction. Keep every task and terminal message on one line.

Take over, recover, or interrupt workers:

```text
python <skill-dir>/osw.py enroll
python <skill-dir>/osw.py guard [<agent-id>...]
python <skill-dir>/osw.py revive [<agent-id> [--session <session-id>]]
python <skill-dir>/osw.py stop <agent-id>
```

- `enroll` records this worktree's agent terminals that OSW did not start
  (state `enrolled`, no watcher).
- `guard` makes you the guardian (notification target) of those agents;
  a turn in progress is followed to completion and reported without a
  handoff.
- `revive` lists resumable sessions; `revive <id>` reopens one after a
  restart — in the original pane when it is a confirmed empty shell
  (`placement: in_place`), else in a new terminal (`new_terminal`) — and,
  if the agent was `lost` mid-task, sends `continue` and supervises the
  rest of the turn.
- `stop` interrupts the current turn; it ends as `error` with
  `completion_source: interrupted` and no handoff.

`list`/`status` show a mid-task agent whose watcher died as `lost`.

Notifications to expect besides `task-finished` / `task-error`:

- `# [osw] blocked agent=<id> kind=<kind> ... screen=<excerpt>` — a dialog
  owns the worker's input. Under `OSW_JEV_MODE=enforce` OSW handles only
  whitelisted ones itself (today: codex's update prompt); otherwise, and
  always for login, permission, delete or payment screens, answer it in
  that terminal. `shadow` reports exactly like `off`.
- `# [osw] ask agent=<id> message=<msg-id> reply="..."` — a supervised
  worker asked a question; answer with the given `orchestration reply`
  (on Windows through Orca's `resources\bin\orca.exe`, not `orca.cmd`).
- `task-finished ... warning=no_tool_activity` — the turn showed no tool
  call in Orca's view; it is still `done`, so verify the handoff and the
  workspace before accepting.
- `# [osw] prompt-truncated agent=<id> ... received_from=<n>` (and
  `task-finished ... warning=prompt_truncated`) — the agent got only the
  prompt from char `<n>` on; it worked on a fragment and nothing was
  resent. `stop` it, then send the full task again.
- `task-error ... reason=blocked` — a dialog kept refusing the prompt.

## Invariants

- Keep the coordinator responsible for decomposition, ordering, acceptance,
  and integration.
- Treat the worktree as shared. Assign outcomes, not exclusive file ownership.
- Preserve unrelated existing changes.
- Never release dependent work from an unverified result.
- Never edit `.orca/osw/` runtime records by hand, including the
  `agents/<id>.stop` sidecar; use `stop`.
- Never launch a nested agent process inside a managed pane without
  clearing the inherited `ORCA_*` environment variables.
- Use provider autonomy flags only in a trusted, externally controlled
  worktree.
