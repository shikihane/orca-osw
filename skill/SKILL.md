---
name: osw
description: Use when delegating, coordinating, resuming, monitoring, or recovering Orca-backed Claude, Codex, Pi, or Kimi sessions in the current worktree
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

For command semantics, lifecycle notifications, continuation, diagnosis, or
cleanup, read [references/operations.md](references/operations.md).

## Quick Start

Inspect real model choices when the human needs options:

```text
python <skill-dir>/osw.py models <claude|codex|pi|kimi>
```

Create a fresh worker:

```text
python <skill-dir>/osw.py new <provider> --prefix <role> --model <model> --thinking <level> "<task>"
```

Use `research`, `code`, `test`, `debug`, `review`, or `misc` for `<role>`.
Continue the same task context after the worker finishes:

```text
python <skill-dir>/osw.py use <agent-id> "<follow-up>"
```

Use `new` for an independent perspective and `use` for continuation or
correction. Keep every task and terminal message on one line.

## Invariants

- Keep the coordinator responsible for decomposition, ordering, acceptance,
  and integration.
- Treat the worktree as shared. Assign outcomes, not exclusive file ownership.
- Preserve unrelated existing changes.
- Never release dependent work from an unverified result.
- Never edit `.orca/osw/` runtime records by hand.
- Use provider autonomy flags only in a trusted, externally controlled
  worktree.
