# Orchestration

## Project Decisions

Use provider decisions already injected into context. When a task type has no
matching decision, run `models <provider>` if options are needed, ask the human,
then record only the confirmed row in the current project instructions:

```markdown
## OSW Provider Policy

| Task type | Provider | Model | Thinking |
|---|---|---|---|
| implementation | codex | gpt-5.5 | high |
```

The row is an example of the format, not a default. Do not launch the worker
until the real choice is recorded.

## Task Ledger

Maintain this ledger in coordinator context; do not write it into source files:

| ID | Outcome | Main area | Depends on | State | Agent | Acceptance |
|---|---|---|---|---|---|---|

Use `pending`, `ready`, `running`, `accepting`, `blocked`, `done`, or `failed`.
A task becomes `ready` only when every dependency is `done`.

## Decomposition

- Define one observable outcome and one acceptance condition per task.
- Use a main change area as coordination guidance, not file ownership.
- Run tasks concurrently only when their outcomes and main change areas are
  independent and neither needs assumptions from the other's unfinished work.
- Serialize interface producer/consumer changes until the interface is stable.
- Keep the coordinator out of the worker count.

## Rolling Dispatch

Use 1-4 workers. Set concurrency to the number of useful ready tasks, capped at
four; never create filler work to occupy a slot.

1. Dispatch ready tasks until the cap is reached.
2. Continue coordinator work while watchers run.
3. Move a completion to `accepting`, inspect it, then mark it `done`, `blocked`,
   or `failed`.
4. Immediately release newly ready tasks after acceptance.
5. Reuse the same agent for corrections; create a new agent for independent
   review.

## Worker Brief

Send one bounded, single-line brief containing:

- outcome and acceptance condition;
- relevant context and known dependencies;
- main change area;
- required validation or evidence;
- shared-worktree warning: inspect current files, preserve unrelated changes,
  and change any related file needed for the outcome.

Do not prescribe a solution unless the task depends on a settled design.

## Acceptance

Treat the handoff as a claim to verify. Check the workspace, affected behavior,
and required evidence. Use an independent reviewer for shared interfaces,
cross-module or high-risk changes, and uncertain results. Release dependent
tasks only after acceptance.

Return to the human for missing provider decisions, destructive actions,
material scope changes, or conflicts between project instructions and the task.
