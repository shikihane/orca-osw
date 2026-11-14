# OSW Control Plane: Fast Path, Router, Dispatcher

Status: final design. This change contains design only.
Date: 2026-11-14
Supersedes: the earlier mailbox-steward design draft.

Scope: local control plane for OSW. Sources are local: human input, files, logs, processes, watcher/system events, and timers. Network services are a future extension.

## 1. Context

OSW starts Orca terminals, watches turns, forces a handoff, and notifies the
caller. Its messages are point-to-point: a human calls `new`/`use`, a watcher
reports to a caller or guardian, and a worker's `ask` is relayed by the
watcher. The control plane adds a global view of messages, tickets waiting for a person, and reports awaiting acceptance.

The control plane adds four pieces:

- **Mailbox**: durable message, ticket, mandate, decision, effect and outbox
  state; the only writer of control state.
- **Fast path**: deterministic code that classifies envelopes and does
  bookkeeping, including executing an exact human command as a Decision.
- **Router**: the skill-invoked top-level agent. It is the sole decision
  authority. Its state lives in the Mailbox/Ticket store; it reads decision cards and writes Decisions, and the Dispatcher executes them.
- **Dispatcher**: pure code that turns a Decision into an idempotent call on
  the existing OSW/Orca execution paths.

Orca and the watchers remain the execution layer. Scheduling, orchestration state, and provider launch logic stay with Orca and the existing OSW code.

## 2. Goals

- One durable, ordered, deduplicated mailbox for all local control messages.
- A single writer for control state; producers write only to the spool.
- At-least-once delivery with idempotent application: the idempotency key absorbs duplicates, so one worker, one answer, and one stop happen per Ticket.
- A deterministic fast path that classifies mechanical messages in code.
- A Router whose state lives in the Mailbox/Ticket store, with a narrow tool boundary and a doorbell that carries only a count and command.
- A formal, code-checked Mandate for every Ticket.
- Durable Effects and an Outbox with ordering and receipts.
- Optional source adapters that are independently disableable; a failure stays inside the adapter.
- Direct mode preserved: with the mailbox stopped, `new`, `use`, `status`,
  `list`, `revive`, `stop` and the current notification path keep working.
- Cross-host operation keeps the control plane local; the remote host runs sshd, Orca, and the provider CLI.

## 3. Scope boundaries

- Sources: human input, files, logs, processes, watcher/system events, and
  timers. Network services are a future extension.
- Router host: the skill-invoked session, woken by a doorbell that carries only a count and command.
- Scheduling: Orca owns Run/Task/DAG/Dispatch.
- Terminal state: Jev classifies ready/blocker state.
- Memory: the append-only Mailbox is the source of facts; the Router reads it.
- Ticket origin: a human command or a source named in Mandate `open_from`.
- Interface: CLI/JSON and `mailbox status`.
- Topology: one Mailbox per project root; remote events arrive through the
  host seam.

## 4. Terms

| Term | Meaning |
|---|---|
| Mailbox | The control-state store and its daemon; the only writer of that state |
| Fast path | Deterministic code in Mailbox ingest; classifies and bookkeeps |
| Router | The skill-invoked top-level agent; the sole decision authority |
| Router host | The skill-invoked session that runs the Router role on demand; a doorbell activates it |
| Router state | The Mailbox/Ticket store; facts and decisions persist across host restarts |
| Dispatcher | Pure code that applies Decisions through existing OSW paths |
| Worker | An Orca terminal plus its watcher |
| Ticket | The durable case file for one unit of authorized work |
| Mandate | The code-checked limits a Ticket carries |
| Decision | A typed, audited choice made by the Router or by an exact human command |
| Effect | A durable intent to perform one side effect |
| Outbox | The ordered, receipt-bearing queue for messages to a source |
| Doorbell | A line that carries only a count and command, telling a Router host that mail is pending |
| Router binding | The transport address and role identity currently serving as Router |
| Source adapter | Optional code that turns a local file/process event into an envelope |

## 5. Architecture

The control lane and the execution lane are separate. The Mailbox writes messages; the Dispatcher sends terminal input through the same functions the CLI already uses.

## 6. Sources

### 6.1 Core sources

| Source | Events |
|---|---|
| Human | `request`, `followup`, `answer`, `stop`, `status`, exact commands |
| Watcher / system | `report`, `ask`, `blocked`, `turn_finished`, `watcher_died`, `mailbox_down`, `dispatcher_failed`, `spool_full`, `lease_expired`, `host_unreachable`, `doctor_failed`, `jev_degraded` |
| Timer | deadlines, retry times, review timeouts |

These are already produced by OSW or its watchers. Use the events the watcher already emits; add a field at the source when one is missing. A component reports its own failure through the detector that sees it: the CLI, a producer, the host doctor, or the Dispatcher.

### 6.2 Optional adapters

| Adapter | Trigger |
|---|---|
| `log_file` | New file, new matching line, rotation or truncation |
| `crash_file` | New `crash.log`, panic/segfault/OOM signature |
| `process_exit` | Non-zero exit, timeout, signal |
| `build_result` | `BUILD FAILED`, non-zero compile exit |

Each adapter is a small, independently disableable module. It owns its read position, rotation handling, filtering, deduplication and aggregation. It writes envelopes only to the spool. If it fails, it emits one `system` event; the core keeps running.

Adapters aggregate a burst and emit one digest envelope, with `thread_key` (file path, build id, process id) so later events attach to the same Ticket. A noisy adapter is disabled at the adapter.

## 7. Envelope

```json
{
  "v": 1,
  "id": "msg_20261114T131500123Z_a1b2c3",
  "ts": "2026-11-14T13:15:00.123Z",
  "from": {"role": "human", "id": "terminal:term_abc", "trust": "trusted"},
  "to": {"role": "router", "id": ""},
  "kind": "request",
  "correlation": {"ticket": "", "agent": "", "dispatch": "", "ask": "", "decision": ""},
  "thread_key": "",
  "reply_to": "",
  "priority": 2,
  "ttl_s": 0,
  "dedupe_key": "",
  "body": {},
  "state": "queued",
  "attempts": 0,
  "lease": null,
  "trace": ""
}
```

Fields follow the previous design. Additions:

- `thread_key`: local thread identity (file path, build id, process id, chat
  id). Used for subscription and attachment.
- `from.role` may be `human`, `worker`, `watcher`, `system`, `router`, `timer`.
- `to.role` may be `router`, `worker`, `human`, `broadcast`, `dead`.
- `body` is at most 16 KiB; a larger body is rejected with a typed error.

Message kinds: `request`, `followup`, `answer`, `ask`, `report`, `blocked`,
`status`, `stop`, `ack`, `notice`, `timer`, `system`.

`report.outcome` is one of `progress`, `waiting`, `ready`, `blocked`, `failed`,
`dropped`.

Trust: watcher/system messages are `trusted`; worker-authored text is
`untrusted`. The Mailbox treats every body as data. The Router treats untrusted text as data.

Priority: 0 human, 1 ask/blocked, 2 report, 3 notice. Per correlation, FIFO;
across correlations, priority first, then oldest.

Dedupe: worker ask uses the Orca message id; report uses a `report_key`
persisted by the watcher; decision uses `decision_id`; adapters supply their
own key.

## 8. Fast path

The fast path is a human-owned code module, `skill/osw/fastpath.py`. Rules live in code and fixtures rather than runtime configuration. Each rule is a pure function and carries fixtures under `tests/fixtures/fastpath/`. Every shipped rule has a fixture.

Allowed:

- dedupe, attach to a Ticket by `correlation`/`thread_key`, derive stage;
- expire notices; settle a `progress` report; answer `status`;
- compute the notification floor and wake policy;
- emit the doorbell;
- execute an exact human command as a Decision with `author=human`.

The Router owns:

- creating a Ticket;
- answering a worker, retrying, stopping, or closing a Ticket;
- choosing a provider, model, or thinking level;
- calling Jev or any model.

The fast path stays a bookkeeping layer. Its output is a bookkeeping effect or a Decision that records the human's exact command; judgment stays in the Router.

## 9. Router

### 9.1 Role

The Router is the top-level agent invoked by the OSW skill. It is a role that runs inside a process: the same agent may run the Router loop in an interactive session, and another instance may take over after a restart. Its state lives in the Mailbox/Ticket store.

| Layer | What it is | Where it lives |
|---|---|---|
| Router role | The sole decision authority | Every decision card |
| Router host | The skill-invoked session that runs the role | An interactive session, started on demand |
| Router state | Facts, tickets, decisions | The Mailbox/Ticket store |

This design uses an on-demand host: a doorbell starts it, it processes the waiting cards, and it ends the turn. A resident host is a separate deployment option.

The Router uses a strong model by default. The fast path absorbs mechanical
classification, so the model is spent on acceptance, answers, scope judgment,
and escalation, where errors are expensive.

### 9.2 Tools

The Router uses `osw mailbox wait/show/trace/decide/status`, `osw list/status/logs`, and reads the handoff/report files referenced by a card.

Execution goes through the Dispatcher: `osw mailbox send`, `osw new`, `osw use`, `osw stop`, terminal sends, Orca commands, and file edits all belong to the Dispatcher. When the Mailbox is running, the CLI recognizes the Router identity and keeps `new`/`use` with the human or Dispatcher identity. The Router writes a Decision; the Dispatcher executes it.

The Router identity is separate from the transport identity: an `OSW_ROLE=router` marker plus a per-project role token. The role token distinguishes the Router from a human in the same terminal, so the CLI keeps the human identity.

### 9.3 Decision card

`wait` returns cards that summarize envelopes. A card is at most 1 KiB:

```json
{
  "message_id": "msg_...",
  "kind": "request",
  "from": "human:terminal:term_abc",
  "trust": "trusted",
  "intent": "request",
  "attention": "must_answer",
  "priority": 2,
  "summary": "把 watcher 的超时改成可配置",
  "tickets": [{"id": "code_001", "stage": "working"}],
  "detail_hint": "osw mailbox show msg_..."
}
```

`intent` and `attention` come from fast-path code. The Router may raise attention; the fast path sets the floor. The Router may pull the full message with `show`.

### 9.4 Doorbell

The Router runs on demand. When a card is waiting, the Mailbox sends a line carrying only the count and command through the same notify channel the watcher already uses:

```text
# [osw] mail pending=<n> run="osw mailbox wait --as router --timeout 0"
```

One doorbell may be pending; a second is suppressed. On lease expiry the Mailbox rings once more; after that it notifies a human. The doorbell carries only the count and the command, so the Router session receives only those.

The first `wait --as router` registers the Router binding: transport address
and role token. A restarted Router re-registers.

### 9.5 Answer authority

The Router may answer a worker only inside the Ticket's Mandate:

- fixed single-line template, at most 4 KiB, carrying the `decision_id`;
- sent through the Outbox and the existing `orchestration reply` path;
- the Dispatcher rechecks that the ask is still open before sending;
- `auth`, `permission`, delete or payment screens, and anything outside the
  Mandate are escalated to a human; blocker handling stays with the watcher
  whitelist.

### 9.6 Memory and learning

The Router keeps its memory in the append-only Mailbox and treats worker text as data. Policies (Mandate, fast-path
code, provider policy) are changed only by a human. Learning happens through a
`propose` Decision carrying evidence; it is replayed offline, evaluated, and
merged through code review. The card may include precedents found
deterministically by the Mailbox.

## 10. Decision

```json
{
  "decision_id": "dec_...",
  "message_id": "msg_...",
  "action": "accept|followup|answer|stop|retry|reject|escalate|noop|close",
  "ticket": "code_001",
  "reason": "one sentence",
  "payload": {},
  "author": "router|human",
  "confidence": 0.0
}
```

- `decision_id` is unique; applying it twice is an idempotent repeat.
- `reason` is required.
- `payload` validation accepts the fields the action defines.
- A human exact command produces a Decision with `author=human` and enters the ledger.
- The Mailbox commits Decision + Effect + message settlement in one transaction. The Dispatcher applies the Decision and reports the result back to the Router.

## 11. Ticket and Mandate

A Ticket is the durable case file:

```text
ticket_id
origin             # who, where, what text
mandate            # the code-checked limits
stage              # open/queued/working/waiting_answer/blocked/review/done/stopped/lost
assignments[]      # agent_id / dispatch_id / terminal / attempt
reports[]          # outcome, handoff, completion_source, tools_observed
open_asks[]
decisions[]
thread_key
created_at / updated_at / closed_at
```

The Mandate is a closed-schema JSON document stored in a git-managed file.
The Dispatcher reads the committed HEAD version; the Router may only narrow it
when opening a Ticket. Fields:

| Field | Meaning |
|---|---|
| `actions` | Allowed Decision actions |
| `answer` | `none`, `options_only`, or `free_text` |
| `max_retries` | Retry budget |
| `max_workers` | Concurrent worker budget |
| `providers` | Allowed providers |
| `open_from` | Sources allowed to open a Ticket; empty means human only |
| `close` | `router` or `human` |
| `expires_at` | Mandate expiry |

The Dispatcher checks every Effect against the Mandate; a violation is
recorded as `mandate_denied` and refused. The mandate file is human-owned;
changing it requires a commit.

System triggers and adapters attach to an existing Ticket by correlation/thread, or become an orphan card. A Ticket starts from a human command or from a source named in `open_from`. This prevents
"build failure -> open Ticket -> new worker -> fail -> open another" storms.

## 12. Dispatcher

The Dispatcher is pure code. It may run as a loop inside the Mailbox daemon or as a separate process; logically it is one component. It applies the Decision schema and calls existing OSW functions.

| Action | Behavior | Idempotency key |
|---|---|---|
| `accept` | Create or reuse an agent through the existing `new` path | `ticket` |
| `followup` | Send through the existing `use` path | `ticket + message.id` |
| `answer` | Reply through `orchestration reply` after rechecking the ask | `ask_id` |
| `stop` | Write the existing stop sidecar | `ticket` |
| `retry` | Start a new assignment in the same session/directory | `ticket + attempt` |
| `reject` | Settle the message; notify the sender once | `message.id` |
| `escalate` | Enqueue a `notice` to `human` | `message.id` |
| `noop` | Settle; bookkeeping only | `message.id` |
| `close` | Verify close preconditions, then close the Ticket | `ticket` |

Rules:

- Validate the Decision, the current state and the Mandate before acting.
- The Dispatcher reuses the existing agent for the Ticket.
- The Dispatcher sends an answer only while the ask is open.
- The Dispatcher stops a Ticket once; a stopped Ticket stays stopped.
- The Dispatcher sends the fixed answer template.
- A failure leaves the Decision unapplied; the lease and retry path recover.

## 13. Effects and Outbox

`Effects` is the durable side-effect queue:

```text
effect(id, decision_id, kind, key, payload, state, attempts, next_at)
```

Kinds: `launch`, `answer`, `stop`, `retry`, `escalate`, `notify`, `close_ticket`.
One row per kind/key; retries use exponential backoff; a failed effect becomes
a dead letter and is visible in `status`.

`Outbox` is the ordered message queue:

```text
outbox(id, ticket_id, conversation, seq, payload, state, sent_at, receipt)
```

Messages to one `ticket_id + conversation` are sent in order; `ack` precedes
`answer`; a deleted or closed conversation is marked `dropped`. Every send has a `sent`, `waiting`, or `gone` receipt.

Decision + Effect are written in one transaction; execution starts only after
commit. This is the transactional-outbox boundary: state change, worker launch, and ack ordering commit together.

## 14. Notifications

Only the Outbox's `notify_human` may wake a person. Each `(ticket, reason)`
gets one single-line `# [osw] attention ...` message to the requester.

Triggers:

- Router `escalate` or `close`;
- deterministic bottom lines: `mandate_denied`, `auth`/`permission` blocker,
  dead letter, Router unreachable.

`fyi` stays in the queue and card. If the requester is the Router session, the message becomes an `attention=human` card for the Router to relay. Watchers write to
the spool; a direct notification is kept only when the Mailbox lock pid is
dead. This collapses per-agent completion lines into one conclusion per
Ticket.

## 15. Scope and Cross-Host

- Mailbox: per project root, under `.orca/osw/mailbox/`.
- One Router binding per project.
- Update lock and `OSW_JEV_MODE` remain machine-wide.
- Remote events are written to the local spool by the local watcher through the host seam; the Mailbox, daemon, and spool stay on the local machine.
- A remote host needs only `sshd`, an authorised key, a registered Orca
  workspace, and the provider CLI.

## 16. Security and Trust

- The control plane uses local files and SQLite.
- Spool and store live under `.orca/osw/mailbox/` with the same filesystem
  expectations as the existing state directory.
- Envelope and body size caps.
- Worker text is untrusted; the Router treats it as data. The Dispatcher
  accepts only the Decision schema.
- `answer` is the only Router-authored worker input; fixed template, length
  cap, decision id, audit.
- Router identity is a role token; the terminal address is transport, and a human in the same terminal keeps the human identity.
- Jev receives terminal frames for `ready_state`/`blocker_kind`; the Mailbox and Router paths stay separate.
- Envelopes, cards, and Jev records carry content only; credentials stay in the environment.

## 17. Failure Handling

| Failure | Behavior |
|---|---|
| Mailbox dies | Spool remains; restart ingests; `status` reports `mailbox_down`; direct mode still works |
| Stale lock | Dead pid is reclaimed; a live foreign lock returns `mailbox_locked` |
| Duplicate spool | Message id primary key ignores it |
| Invalid envelope | Move to `spool/rejected/`; count it in `status` |
| Stale lease | Return the message to `queued` |
| Duplicate decision | `decision_id` makes apply idempotent |
| Dispatcher failure | Decision remains unapplied; lease expiry retries |
| Ask already answered | Late answer is recorded and dropped |
| Stop race | Stop sidecar wins; `accept` checks it before launching |
| Doorbell lost | One retry after lease expiry; then notify a human |
| Router unavailable | Cards wait; `must_answer` notifies a human |
| Adapter failure | Emit a `system` event; disable the adapter; core unaffected |
| DB corruption | Rebuild the derived index from agent records and spool; report the loss explicitly |

Delivery retry: 2 s doubling to 60 s. Launch retries follow the existing
watcher/provider policy; delivery retries follow the Mailbox. A message beyond `max_attempts`
becomes a dead letter.

## 18. Observability

`osw mailbox status --json`:

```json
{
  "running": true,
  "pid": 1234,
  "schema_version": 1,
  "queues": {"router": 3, "human": 1, "worker": 2, "broadcast": 0},
  "oldest_age_s": 42,
  "leased": 1,
  "dead": 0,
  "spool_lag": 0,
  "doorbell": "pending",
  "decisions_applied_5m": 4
}
```

Events: `mailbox_started`, `mailbox_stopped`, `mailbox_locked`,
`mailbox_ingested`, `mailbox_rejected`, `mailbox_duplicate`,
`mailbox_leased`, `mailbox_lease_expired`, `mailbox_acked`,
`mailbox_delivery_failed`, `mailbox_dead_letter`, `doorbell_sent`,
`doorbell_consumed`, `router_decision`, `router_applied`, `router_conflict`,
`mandate_denied`, `adapter_failed`, `jev_degraded` (existing).

Metrics: `router_wakeups_per_hour`, `human_attention_per_ticket`,
`mailbox_latency_p95`, `duplicate_decisions`, `lost_messages`, `spool_lag`,
`adapter_events_per_minute`, `mandate_denied_count`.

## 19. Testing

Unit:

1. Envelope validation, trust, priority, TTL, dedupe.
2. Fast path rules: one fixture per rule; exact human command -> Decision
   `author=human`; writes only bookkeeping and human-command Decisions.
3. Lease acquire/expire/re-acquire/ack; duplicate ack is idempotent.
4. Doorbell: one pending, suppress second, retry after lease expiry, fallback
   human.
5. Mandate: closed schema, `open_from`, `answer` modes, expiry, narrowing,
   `mandate_denied`.
6. Decision + Effect transaction; duplicate decision; Outbox ordering and
   receipts.
7. Router tool boundary: `send`/`new`/`use` refused for the Router role token;
   the human in the same terminal keeps the human identity.

Integration (mock Orca):

1. Human request -> card -> accept -> `new` once -> report -> close.
2. Worker ask -> card -> answer -> `orchestration reply` once.
3. System trigger outside `open_from` -> orphan card.
4. System trigger with `open_from` -> Ticket -> accept.
5. Mailbox killed mid-lease -> restart -> redelivery -> one apply.
6. Router killed after `decide` before Dispatcher -> retry -> one apply.
7. Duplicate spool from watcher restart -> one message.
8. Stop race: stop before accept -> launch is skipped.
9. Remote watcher through host seam -> local card.
10. Adapter failure -> one system event; core still works.

Jev: `ask_ready`/`ask_blocker` behavior unchanged; Jev answers ready/blocker questions.

Platform: Windows atomic replace, spool writes under a concurrent reader,
detached daemon start/lock recovery, long paths, Unicode bodies, `status` with
the daemon stopped and the DB write-locked.

Regression: the complete `pytest -q` suite stays green; current baseline 682.

## 20. Compatibility and Migration

- Mailbox starts on `osw mailbox start`; direct mode works independently.
- Watchers gain a spool sink; the direct notification path remains as fallback.
- `guardian` remains a notification address; the Router is a decision role.
- `notified_asks` remains; the Mailbox dedupe key is a second line of defense.
- Agent records keep their schema. Ticket/Mandate live in the Mailbox; the ticket index is derived and rebuildable.
- Existing logs/events are unchanged; new events use the same logger.

## 21. Required Repo-Rule Amendments

The design needs these changes outside the spec:

1. **AGENTS.md injection surface**: add the Router `answer` template as an
   allowed injection, with the 4 KiB cap and decision-id audit. This amendment authorizes Router answers.
2. **AGENTS.md daemon note**: state that the Mailbox daemon is a local
   control-plane process only; execution authority stays with Orca, and direct mode runs independently.
3. **Jev docstring/config**: `jev.py:9` says the config file is the single
   source, but `resolve_mode()` reads only `OSW_JEV_MODE`; remove the stale
   `jev` block from `.orca/osw/config.json`. Jev is ready/blocker only.

These are acceptance conditions for implementation.

## 22. Appendix: Examples

### Request envelope

```json
{
  "v": 1,
  "id": "msg_20261114T131500123Z_a1b2c3",
  "ts": "2026-11-14T13:15:00.123Z",
  "from": {"role": "human", "id": "terminal:term_abc", "trust": "trusted"},
  "to": {"role": "router", "id": ""},
  "kind": "request",
  "correlation": {"ticket": "", "agent": "", "dispatch": "", "ask": "", "decision": ""},
  "thread_key": "chat:main",
  "reply_to": "",
  "priority": 2,
  "ttl_s": 0,
  "dedupe_key": "",
  "body": {"text": "把 watcher 的超时改成可配置", "summary": "配置化 watcher 超时"},
  "state": "queued",
  "attempts": 0,
  "lease": null,
  "trace": ""
}
```

### Decision

```json
{
  "decision_id": "dec_20261114T131700789Z_aa11bb",
  "message_id": "msg_20261114T131500123Z_a1b2c3",
  "action": "accept",
  "ticket": "code_001",
  "reason": "明确人工请求，范围仅 watcher 配置",
  "payload": {"provider": "claude", "model": "sonnet", "thinking": "high"},
  "author": "router",
  "confidence": 0.9
}
```

### Doorbell

```text
# [osw] mail pending=1 run="osw mailbox wait --as router --timeout 0"
```

### Router transcript

```text
$ osw mailbox wait --as router --timeout 60 --max 4 --json
[{"message_id":"msg_...","kind":"request","ticket":"code_001","stage":"working","attention":"must_answer","summary":"配置化 watcher 超时"}]

$ osw mailbox decide msg_... --action accept --ticket code_001 --reason "明确人工请求" --payload-json '{"provider":"claude","model":"sonnet","thinking":"high"}'
dec_...

$ osw mailbox show msg_...
{...}
```
