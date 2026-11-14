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
7. When the mailbox is running, route through it rather than dispatching
   inline: `mailbox wait` for cards, `mailbox decide` for the decision.

For multiple workers, dependencies, shared-worktree coordination, or model
selection, read [references/orchestration.md](references/orchestration.md)
before dispatching.

For command semantics, lifecycle notifications, continuation, takeover and
recovery (`enroll`, `guard`, `revive`, `stop`), diagnosis, or cleanup, read
[references/operations.md](references/operations.md).

## Quick Start

Inspect real model choices when the human needs options:

```text
python <skill-dir>/osw.py models <claude|codex|pi|omp|kimi|grok|dsh>
```

`dsh` (DeepSeek Harness) is launched as `dsh-tui .` and takes no
`--model`/`--thinking`: its model, effort and permission preset are DSH
settings changed inside the TUI (`/model`, `/permission`).

Create a fresh worker:

```text
python <skill-dir>/osw.py new <provider> --prefix <role> --model <model> --thinking <level> "<task>"
```

Use `research`, `code`, `test`, `debug`, `review`, or `misc` for `<role>`.
`omp` dispatches run in a normal Orca terminal like every other provider; the
watcher sends the task turn and reads the pane title for readiness and
completion (an omp status bar never goes output-idle). Write the handoff
yourself when you are the worker — the watcher verifies it either way.
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

## Mailbox

The mailbox is the optional control plane: it records requests and results and
holds the decision log. Use it once more than one worker is live or when a
decision has to be auditable.

```text
python <skill-dir>/osw.py mailbox start
python <skill-dir>/osw.py mailbox wait --as router --timeout 0
python <skill-dir>/osw.py mailbox decide <message-id> --action <action> --reason "<reason>"
python <skill-dir>/osw.py mailbox status --json
python <skill-dir>/osw.py mailbox stop
```

- `wait` hands you cards of at most 1 KiB for the role you ask for; a card is a
  summary, `mailbox show <id>` pulls the full message and ticket, and
  `mailbox trace <id|ticket>` follows message → ticket → decisions → effects.
- `decide` needs a reason and prints the `decision_id`. The Dispatcher applies
  it through `new`, `use`, `stop` and `orchestration reply`; while acting as the
  Router you do not call those yourself, and `new`/`use` are refused with
  `router_forbidden`.
- The only text the Router may put into a worker is the fixed answer template
  `# [osw] answer <decision_id>: <text>` — one line, at most 4 KiB. Anything
  outside the Ticket's `skill/osw/mandate.json` (login, permission, delete,
  payment) goes to a human; blocking screens stay with the watcher whitelist.
- With the mailbox never started, or stopped, every other command in this file
  behaves exactly as before; a dead daemon keeps the spool and ingests it on
  restart.

## Super

Invocation shortcut: a skill invocation whose text starts with `super`
(for example `/osw super "task A" "task B"`) means run this command with the
remaining text as one task per argument. Tasks are positional, and repeated
`--task` values are appended after them.

Free-form request (`/osw super 把 A、B、C 并行做完`): split it into independent
outcomes first (one task per outcome), state the list in one line, then run
`super` with those positional tasks. Do not ask the human for provider, model
or thinking when the project policy names them, and do not require flags they
did not write; ask only when the request cannot be split without guessing.

When several tasks are independent, dispatch them with one command instead of
one `new` per task:

```text
python <skill-dir>/osw.py super "Fix the failing tests" "Audit the provider layer"
```

Only a provider is required, and even that can come from the project. Flags
tailor the run:

```text
python <skill-dir>/osw.py super "task one" "task two" \
    --provider <provider> --prefix <role> --model <model> --thinking <level> \
    --max-parallel <n> --caller-terminal <handle>
```

Or from a plan file whose top-level keys are the same defaults and whose
`tasks` array mixes strings and per-task override objects:

```text
python <skill-dir>/osw.py super --plan plan.json --json
```

- **Zero-flag defaults** — with no flag and no plan value for
  `provider`/`model`/`thinking`/`prefix`/`max_parallel`, resolve them from the
  project in this order: the `super` object in
  `.orca/osw/config.json` (absent or malformed is ignored), then
  `OSW_PROVIDER`/`OSW_MODEL`/`OSW_THINKING`/`OSW_PREFIX`/`OSW_MAX_PARALLEL`,
  then the `## OSW Provider Policy` table row whose first cell contains
  `batch fan-out`. Prefix finally defaults to `code`, `max_parallel` to 32.
  Only `provider` is required: with none anywhere the command names the three
  places to set it and dispatches nothing.
- **Flags are defaults** — an explicit flag wins, a plan's
  `provider`/`model`/`thinking`/`prefix`/`max_parallel` stands in for a flag
  you did not pass, and a task object may override `task`, `prefix`,
  `provider`, `model`, `thinking` and `caller_terminal` for itself. `--plan`
  cannot be combined with tasks, positional or flag.
- The mailbox is started first by default (`--no-mailbox` skips it). If it
  fails to start, nothing is dispatched and the command exits 1 with that typed
  error.
- Each task is one plain `new` in the project root; `super` keeps no state of
  its own and its agents are watched, reported and handed off like any other.
  At most `--max-parallel` run at once (default 32, clamped to 1..32, 300s each),
  one failed dispatch never aborts the rest, and the command exits 1 if any
  dispatch failed.
- `--json` reports `{mailbox, requested, started, agents, errors}`; `--dry-run`
  prints the argv lists without executing them, so the resolved provider, model
  and thinking are visible before anything runs. Verify each result by reading
  its handoff, exactly as for `new`.

## Cross-host workers

Register the machine once, prove it, then dispatch by name:

```text
python <skill-dir>/osw.py host add fnnas --ssh fnnas --worktree path:/srv/repo
python <skill-dir>/osw.py host doctor fnnas --provider <provider>
python <skill-dir>/osw.py new --on fnnas <provider> --prefix <role> "<task>"
```

- The dispatch is the classic lane, not Orca's supervised one: `new --on
  <host>` runs `orca terminal create --environment <host> --worktree
  <selector> --command "<absolute provider path> <autonomy flags>"` and the
  local watcher sends the prompt, reads the pane and collects the handoff over
  SSH. A remote agent has no Dispatch id; the Run stays local and is unused
  remotely. Do not re-enable `orchestration worker-start --on`: on Orca 1.4.248
  a paired server reports `host_indeterminate` readiness — `worker-start` held
  at `agent_readiness` for 240s with the pane at its composer,
  `terminal wait --for tui-idle` timed out on that same idle pane, while
  `terminal send`/`read` and `worktree ps` answered.
- A provider that has never run for that account shows first-run
  theme/security/bypass dialogs, and no readiness probe can clear them: have the
  operator start it once in that pane. Directory trust is pre-recorded over
  SSH, so that prompt does not appear.
- Do not dispatch until `host doctor` has passed its four rungs, in order:
  connection → workspace → worktree → provider. It stops at the first failure
  and names the code (`host_unreachable`, `host_permission_denied`,
  `remote_no_project`, `remote_no_worktree`, `provider_missing`,
  `remote_session_unknown`, `unknown_host`, `host_registry_invalid`).
- Address the agent as `<host>/<agent_id>`; a bare id and `local/<id>` both
  mean this machine, and an unregistered prefix is refused with the list of
  registered ones.
- `list --all` / `status --all` cover every registered host. The remote
  handoff is copied into the local `.orca/osw/handoffs/` when the turn ends
  (record `evidence: ssh`) and judged there, by the same exists-and-size-stable
  rule; accept the result by reading that file, exactly as for a local worker;
  a remote worker that writes nothing still finishes `done` with
  `handoff_missing`.
- The other machine needs only `sshd` reachable from here with an authorised
  key, a workspace already registered in Orca there, and the provider CLI.
  Nothing is installed there — no OSW, no resident process, no helper script —
  and OSW clones nothing.
- `use --on <host>` adopts a pane there, `enroll --on <host>` records its
  unmanaged panes, `revive <host>/<agent_id>` reopens the session there, and
  `stop`/`del <host>/<agent_id> --close` act on that host. Remote `new` and
  remote `revive` use the absolute provider path `host doctor` recorded (a
  local launch or resume keeps the bare name).
- A remote `revive` needs a provider session id from that host's hook cache
  (`~/.config/orca/agent-hooks/last-status.json`, read over SSH). On a host
  whose Orca never records hook status no pane has one: measured on host pi the
  file exists with `version: 2` and **zero entries**, so `revive pi/<agent_id>`
  fails with `remote_session_unknown` naming the pane and the cache path. It
  fails rather than resuming a guess (pass `--session <sid>` to supply one),
  and remote revive is not verified end to end.
- Orca pairing is optional for operation; it is what lets a remote terminal be
  viewed in the Orca UI.

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
- Never reach another machine yourself: no `ssh`/`sftp`/`scp`, no terminal
  scraping, no writes to remote paths. Cross-host work goes through
  `--on <host>` and `<host>/<agent_id>`, which pass the host seam in
  `skill/osw/hosts.py`.
- Never launch a nested agent process inside a managed pane without
  clearing the inherited `ORCA_*` environment variables.
- Use provider autonomy flags only in a trusted, externally controlled
  worktree.
