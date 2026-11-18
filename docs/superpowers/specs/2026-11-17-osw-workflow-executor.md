# OSW Workflow: Run Archive and Executor

Status: draft for review. Read-only surface (steps 1–2) is already delivered; this spec covers the executor (steps 3–6).
Date: 2026-11-17
Related: `2026-11-14-osw-control-plane-design.md` (Mailbox, Router, Dispatcher).

## Problem Statement

Today a fan-out is a single call: the operator gathers task text, hands it to
`super`, and gets back a list of created agents. Three things then go wrong for
any task that needs more than one round of work.

The operator has to hold the plan. Which files to scan, which findings to
re-check, which findings to merge — that lives in a chat turn or in the
operator's head. A run of 300 dispatches cannot be re-read, diffed, or reviewed
before it spends money, and a second operator cannot pick it up.

A dead agent costs the whole batch. There is no run record, so a step that
stalls after 40 minutes is re-dispatched by hand, along with every step whose
result the operator no longer trusts. `revive` resumes one agent's provider
session; nothing resumes a batch.

Nothing verifies anything. Each agent writes a handoff and finishes; a
confident wrong answer and a correct one look identical in `status`, and the
only review is the operator reading 300 handoffs by hand. There is no barrier,
so a merge step cannot even be expressed.

The upstream reference (Claude Code dynamic workflows) solves all three by
turning the plan into a file that a runtime executes. Its resume semantics —
replay in start order, rerun the first divergent agent and everything after it
— fit its in-process runtime and its prefix cache. OSW has neither: its agents
are independent CLI sessions with no shared prefix, so copying that rule would
throw away work for no reason.

## Solution

`osw workflow` executes a declarative definition against the existing dispatch
paths, and keeps a durable archive of what it did.

- A **definition** is a closed-schema JSON file: named phases, each either one
  agent (`single`) or one agent per item (`for_each`) drawn from the previous
  phase's structured output. A phase boundary is a barrier.
- A **run** is one execution of a definition, archived under the project's
  runtime state: which step ran, with which prompt, which agent, what it
  returned, how many attempts it took.
- Execution is a **scheduler process**, not a conversation. It joins the
  batch, judges each step, dispatches the next agent, and stops at a barrier.
  No model is consulted about what runs next.
- **Resume** re-derives every step key and reuses any step that already
  finished, independently — no cascade.
- **Verification** is a phase whose agents only append a verdict line. The
  scheduler, not the verifier, decides what a verdict means.
- A **person** accepts, rejects, or retries one step. Those verbs go through
  request side files, so the scheduler stays the only writer of run state.

The execution layer is unchanged: every step is one `osw new`, observed by one
watcher, reported as one handoff. The workflow layer adds bookkeeping around
those events and nothing else.

## User Stories

1. As an operator preparing a large audit, I want to write the plan into a
   reviewable file, so that I can diff and approve it before it spends money.
2. As an operator, I want `run --dry-run` to print the phases, the known task
   count, and the agent ceiling, so that I know the shape of the run before it
   starts.
3. As an operator, I want `for_each` to expand at run time from the previous
   phase's output, so that I do not have to enumerate 300 files by hand.
4. As an operator, I want a phase boundary to be a barrier, so that a merge or
   verification phase can assume every prior agent finished.
5. As an operator, I want a failed step to be retried on a fresh agent with
   the same prompt, so that a flaky provider or a bad pane does not cost me the
   other 299 results.
6. As an operator resuming after a crash, I want finished steps reused by step
   key, so that a resumed run costs only what actually remains.
7. As an operator, I want resume to be independent per step rather than a
   replay prefix, so that a stale agent in the middle of a batch does not
   invalidate completed work after it.
8. As an operator, I want the run archive to record prompt, schema, agent id,
   attempt count, and outcome per step, so that I can explain later why a step
   ran twice.
9. As an operator, I want `status` to show phase progress and per-step state
   with elapsed time and attempt count, so that I can tell a working run from a
   stuck one at a glance.
10. As an operator, I want `status --watch` to refresh in place, so that I can
    leave it open on a second screen.
11. As an operator, I want `status --wait` to exit when the run reaches a
    terminal state, so that a shell script can gate on it.
12. As an operator, I want a run that would exceed the agent ceiling to be
    refused with a readable reason rather than silently truncated, so that I
    never get a partial result that looks complete.
13. As an operator, I want a warning before a large run (agent count or
    projected tokens), so that a typo in `for_each` does not become a bill.
14. As an operator, I want a definition with an unstable placeholder refused at
    load time, so that resume is not silently broken later.
15. As an operator, I want a verifier agent to work blind — no view of the
    author's self-assessment — so that its verdict is independent.
16. As an operator, I want an artifact changed during verification to
    invalidate that verdict, so that verification cannot be satisfied by
    editing the thing being verified.
17. As an operator, I want an unparseable verdict retried once and then marked
    `unverifiable` for a human, so that the run never invents a result.
18. As an operator, I want to accept, reject, or retry a single step with a
    reason, so that a human decision enters the archive instead of a re-run.
19. As an operator, I want phase policy to be explicit — stop the phase on a
    failed step, or continue with the gap recorded — so that partial results
    are labelled partial.
20. As an operator, I want `stop` on a run to stop dispatching and interrupt
    the running agents, so that I can end a run that has gone wrong.
21. As an operator, I want `ls` and `show` to list runs and print one run's
    structure, so that I can find last week's audit.
22. As an operator running on a remote host, I want remote steps judged from
    artifacts copied back through the host seam, so that nothing needs to run
    on the far side.
23. As a maintainer, I want the scheduler to dispatch through one function, so
    that tests can substitute it and the production path stays a single call
    site.
24. As a maintainer, I want the archive to be the only state, so that a killed
    scheduler loses no information and no daemon is required.
25. As a maintainer, I want the workflow layer to call only `new`, `use`,
    `stop`, and the reply path, so that it never becomes a second dispatcher
    with its own authority.
26. As a maintainer, I want the scheduler to be the only writer of run state
    while the watcher stays the only writer of agent records, so that the
    single-writer rule holds across both layers.
27. As a maintainer, I want a definition to be project-level but overridable in
    the user directory, so that team workflows are committed and personal ones
    are not.
28. As a maintainer, I want the workflow layer to work with the Mailbox stopped,
    so that direct mode stays independent.

## Implementation Decisions

### Modules

| Module | Change |
|---|---|
| `workflow/defs.py` | Delivered. Closed-schema load, expansion, refusal codes. |
| `workflow/board.py` | Delivered. Read-only rendering of runs. |
| workflow_cli.py | Delivered for `ls`/`show`/`status`/`run --dry-run`; extended with `run`, `stop`, `accept`, `reject`, `retry`, `plan`, `go`, `go-worker` (the detached body `go` spawns) and `run --run <id>` (continue one named run). |
| `workflow/author.py` | New. `plan`: one agent drafts a definition against the closed schema; `defs.py` judges the file, and nothing is dispatched. The schema stays closed — a draft is refused by name, never half-run. `go` composes it with `start_run` and `spawn_scheduler`, so one sentence becomes a run that outlives the tool call: the scheduler is its own detached process and writes `runs/<run_id>/scheduler.log`. |
| `workflow/executor.py` | New. The scheduler: `join()`, `judge()`, `dispatch()`, one `tick()` per poll interval. |
| `workflow/archive.py` | New. `run.json`, step records, verdict ledger, request side files. The only writer of run state. |
| `watch`/watcher | One behaviour added (stall resend); no structural change. |
| `cli.py` | Two lines already added for registration. |

### Archive layout

Runtime state lives under the existing per-project state directory, beside
`agents/`, `reports/`, `handoffs/`, and `mailbox/`. Definitions are separate
and committable.

```
<project>/.orca/osw/workflows/<name>.json        # definition, committable
<project>/.orca/osw/runs/<run_id>/run.json       # run header + phases
<project>/.orca/osw/runs/<run_id>/steps/<key>.json
<project>/.orca/osw/runs/<run_id>/verdicts.jsonl  # append-only, verifiers write
<project>/.orca/osw/runs/<run_id>/requests/<ts>_<verb>_<step>.json
```

`run_id` is `<UTC timestamp>_<name>_<6 hex>`. A run directory is created by the
scheduler on first dispatch, never by `--dry-run`.

### Step identity

```
step_key = sha256(phase_id \0 index \0 expanded_prompt \0 schema_id).hex[:32]
```

`index` is the position within the expanded phase, zero-based; `schema_id` is
the definition's schema for that phase, or the empty string. The prompt must be
a pure function of the definition and the previous phase's structured output:
`defs.py` already refuses `{{now}}`, `{{uuid}}`, and `{{random}}` at load time.

Resume reuses a step whose recorded state is `done` and whose key matches. It
does **not** cascade: unlike the upstream reference, a step whose prompt changed
does not invalidate later steps, because OSW agents share no prefix cache and
have no ordering dependency beyond the barrier. A phase with any changed key
re-runs only those steps.

### Run state

Prototype-derived shape, the part that carries the decisions:

```json
{
  "run_id": "20261117T160245Z_audit-routes_9f3a11",
  "definition": {"name": "audit-routes", "path": "..."},
  "state": "running",
  "policy": "strict",
  "verify": "suspect",
  "max_attempts": 2,
  "max_agents": 64,
  "started_at": "...",
  "phases": [
    {"id": "scan", "state": "done", "steps": ["<key>"]},
    {"id": "audit", "state": "running", "expanded": 37, "steps": ["<key>", "..."]}
  ]
}
```

Per-step record:

```json
{
  "step_key": "...", "phase": "audit", "index": 12,
  "prompt_sha": "...", "schema_id": "",
  "state": "running",
  "attempts": [{"n": 1, "agent": "code_041", "terminal": "...",
                "reason": "lost", "started_at": "...", "ended_at": "..."}],
  "agent": "code_042",
  "handoff": ".orca/osw/handoffs/code_042_....md",
  "output": ".orca/osw/runs/<run_id>/outputs/audit-12.json",
  "verdict": "confirmed",
  "error": "",
  "warning": ""
}
```

States: `pending`, `running`, `done`, `failed`, `skipped`, `unverifiable`,
`artifact_mutated`. Only the scheduler writes these records.

### Scheduler loop

One pass is `join() → judge() → dispatch()`; the process repeats every
`POLL_LOCAL_SECONDS` (5) or `POLL_REMOTE_SECONDS` (20) when any step in the
current phase is on another host. Both constants already exist in `defs.py`.

- `join()` reads agent records through the existing single-writer contract and
  copies a finished step's handoff path, warning, and error into its record.
- `judge()` applies the attempt ladder and the phase policy, and decides
  whether the barrier has cleared.
- `dispatch()` creates at most one agent per pending step, through **one**
  function — `dispatch_step(step, definition, root) -> agent_id` — which is the
  only call site that shells out to `new`. Tests substitute this function; the
  production implementation is the existing `fanout` runner path, so
  `new_argv`, the Mailbox start hook, and the created-line parser are reused
  rather than reimplemented.
- `for_each` reads its items from the producer's **output file**, not from its
  handoff. The handoff is the watcher's four-section markdown; a producer cannot
  put machine-readable data there without fighting the template, and the smoke
  run proved it — the agent obeyed the handoff shape and the expansion refused.
  A phase task may therefore name `{{output}}`; the scheduler substitutes the
  run-scoped path (`runs/<run_id>/outputs/<phase>-<index>.json`), records it on
  the step, and refuses with `no_items` naming the path when the file is
  missing. A producer that predates the channel still expands from the last
  fenced ```json block of its handoff.
- `new` refuses to run without a caller terminal, because the completion
  notification would have nowhere to go. The scheduler does not need that
  notification — it reads agent records — so the dispatch inherits
  `ORCA_TERMINAL_HANDLE` when the run was started inside a pane and passes
  `--no-notify` when it was not (a plain shell, a detached process, CI). A
  workflow therefore runs headless without changing what a supervised agent
  does.

The scheduler never mutates an agent record and never writes another module's
state. Its verbs are the existing ones: `new`, `use`, `stop`, and the reply
path for a worker's `ask`.

### Attempt ladder

| Trigger | Action | Recorded |
|---|---|---|
| Watcher sees a stall and sends one resend on the same pane | none (watcher already did it) | `stall_restart` event |
| Step ends `lost` or `error` with attempts < `max_attempts` | new agent, same prompt, `attempts += 1` | reason on the attempt entry |
| Attempts exhausted | step `failed` | phase policy decides what happens next |
| Person `retry` | one more attempt regardless of the cap | `reason` from the person |

`max_attempts` defaults to 2 and is a definition field.

Phase policy is one of:

- `strict` (default) — a failed step fails the phase; the run stops at that
  barrier with every unfinished step left `pending`.
- `partial` — the phase completes with `state: "partial"`, gaps are recorded per
  step, and the dependent phase receives fewer items. A run that ends `partial`
  never renders as successful.

### Verification channel

A verification phase runs agents whose only write is one JSON line appended to
`verdicts.jsonl`:

```json
{"step": "<key>", "verdict": "confirmed|refuted|unsure", "reason": "...", "artifact_sha256": "..."}
```

Rules, each enforced by code rather than by prompt instruction:

- **Blind.** The verifier's prompt carries the artifact and the claim, never
  the author's self-assessment.
- **Read-only plus hash.** The scheduler records the artifact's sha256 before
  the verifier starts and compares after. A difference marks the step
  `artifact_mutated` and discards the verdict.
- **No CLI.** Verifiers do not call `osw`; on a remote host they cannot, and a
  self-accepting verifier must not exist. The scheduler alone writes run state.
- **One parse retry.** An unparseable verdict gets one more verifier, then the
  step becomes `unverifiable` and waits for a person. Nothing is inferred.

`verify` is `none`, `suspect` (default: only steps whose output failed a
defined check), or `all`.

### Human verbs

`workflow accept|reject|retry <run> <step> --reason` writes a request side file
and returns. The scheduler consumes it on its next tick and removes it — the
same idiom as the existing `.orca/osw/agents/<id>.stop` side file.

The verb requires human identity: with `OSW_ROLE=router` set, these verbs are
refused (`router_forbidden`), so a Router cannot accept its own work. When the
Mailbox is up, the decision is also recorded there through the existing
`send --kind request` / `decide --action accept` path, keeping one audit trail.

### Guards

- `max_agents` defaults to 64. A run whose expansion exceeds it is **refused**
  with `too_many_agents`, naming the count and the phase; never truncated.
- A run above 32 agents, or above a projected 1M tokens, prints a warning and
  proceeds — advisory, like the upstream size guideline.
- `approval: "human"` in a definition means the run may not start until the
  operator passes `--yes`; without it the run refuses with
  `approval_unavailable` and dispatches nothing. `run.json` records
  `approved_by: "flag"` so an audited run shows who opened the gate. Routing
  this through a Mailbox request would add a second approval state machine to
  a guardrail whose whole job is "a human meant to start this"; the flag is
  that, in one place.
- `--dry-run` stays free of side effects: no run directory, no spool entry.

### Deliberate deviations from the upstream reference

| Upstream | Here | Why |
|---|---|---|
| Resume = replay in start order, cascade rerun | Per-step key match, no cascade | Independent CLI sessions, no shared prefix cache |
| Plan is a model-written script executed in-process | Definition is human-authored closed-schema JSON | No second execution authority; diffable; no JS runtime in OSW |
| In-process `agent()` returns a value to code | Steps communicate through handoffs and structured fields | Agents are external processes |
| Hard cap enforced by the runtime's scheduler | Caps enforced by refusal before dispatch | A truncated run is a lie |
| Events wake the run | Polling 5 s / 20 s, idempotent | Cross-platform, works across the host seam |

## Testing Decisions

Good tests assert observable outcomes: which step keys exist, which state a
step reaches after a given sequence of events, what `status` prints, and which
refusal code comes back. They do not assert internal call order, log text, or
the shape of private helpers.

**One seam.** `dispatch_step` is the single substituted function. Tests provide
a fake that returns an agent id and, on the next `join()`, a hand-written agent
record and handoff file in a temporary project root. Everything above that —
`join`, `judge`, the barrier, the attempt ladder, resume, verification, guards —
is exercised through it without a live Orca. Prior art:
`tests/test_fanout.py` patches the process runner the same way, and the watcher
tests drive a fake Orca through one mock.

| Suite | Covers |
|---|---|
| `tests/test_workflow_executor.py` | Single-phase dispatch; barrier release; `strict` vs `partial`; attempt ladder; resume reuse and non-cascade; `too_many_agents` refusal; `approval_unavailable` without `--yes` |
| `tests/test_workflow_verify.py` | Blind prompt content; `artifact_mutated` on a changed hash; unparseable verdict → one retry → `unverifiable`; `verdicts.jsonl` append tolerance |
| `tests/test_workflow_requests.py` | `accept`/`reject`/`retry` side files consumed exactly once; `router_forbidden` |
| `tests/test_workflow.py` (existing) | Definitions, expansion, `status` rendering, `--dry-run` |
| `tests/test_watcher.py` | The stall-resend behaviour, added to the existing watcher suite |

CLI tests use `CliRunner` and a temporary project root, matching
`tests/test_cli.py`. No test requires a live Orca instance or a network.

## Out of Scope

- A resident supervisor or daemon (`workflow supervise`). The foreground
  `run` plus `--resume` covers restart.
- Model-authored orchestration: nothing generates a definition at run time.
- A general DAG. Linear phases with barriers only.
- Nested workflows.
- Strong schema enforcement of agent output. Handoffs are markdown; a missing
  structured field is a refusal naming the field, not a fabricated value.
- Cost accounting and token ceilings beyond the advisory projection.
- Parallel phases, phase-level concurrency, and cross-run scheduling.
- Agent teams, messaging between agents, and shared sessions.
- Any change to the Mailbox, Router, or Dispatcher rules.

## Further Notes

- The upstream documentation changed on 2026-11-10 (+10/−4, failure and
  usage-limit semantics) and 2026-11-14 (+1/−1, a link anchor). Mechanism-level
  behaviour held still; version thresholds did not. Nothing in this spec may
  depend on a version number of another product.
- The read-only surface (steps 1–2) is delivered and green: definitions,
  expansion refusals, `status`/`show`/`ls`, and `run --dry-run` print phases,
  known counts, `N` for run-time expansion, and the agent ceiling, with no run
  directory created.
- Open question for review: whether remote artifacts should be hashed over the
  host seam on every verification, or only when the verifier reports a change.
  The former costs one extra read per verified step; the latter trusts the
  verifier's own report and is therefore weaker.

## Rounds and gates (2026-11-18)

"No model is consulted about what runs next" is narrowed, not broken: a gate
answers a criterion written into the definition before the run, and the
policy — not the gate — maps that answer to the next step. A gate cannot add a
phase, rewrite a criterion or change the goal; it may propose a goal change,
which a person accepts with `workflow goal --set`. Rounds are new instances of
a declared phase, keyed with their round number, so resume stays exact.
