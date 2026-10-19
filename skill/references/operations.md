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

OSW supports `claude`, `codex`, `pi`, `omp`, `kimi`, and `grok` for `new`.
`omp` launches as a supervised Orca worker: OSW binds the calling terminal to
the project's orchestration Run (`state.json: orchestration_run_id`) and runs
`orca orchestration worker-start --agent omp`, so Orca creates the terminal,
waits for readiness and injects the task spec (which carries the
handoff/report contract; the worker sends `worker_done --report-path` itself).
OMP model discovery queries `omp models --json`; pass a returned
`provider/model` selector through `--model` (forwarded to `worker-start`).
`omp` rejects `--thinking`: the supervised launch cannot pass an effort flag.
`kimi` takes `--model` (a config.toml alias such as `kimi-code/k3`) but
rejects `--thinking`: its effort is configured per model alias, not on the
command line. `grok` takes `--model` (a model id from `grok models`, such as
`grok-4.3`) and maps `--thinking` to `--reasoning-effort`; it starts with
`--always-approve --trust`, where `--trust` also grants folder trust for the
launched workspace.

Supervised `omp` workers complete on Orca's Dispatch lifecycle (the watcher
polls `orca orchestration worker-show`), not on `worktree ps` states: omp's
ps row disappears at every turn end (see Known limits under Takeover and
Recovery) and its status bar repaints about once a second, so neither ps
nor output silence is a dependable completion signal. The
watcher verifies the handoff the worker wrote, or sends the standard wrap-up
turn when the worker skipped the contract, then records
`orchestration_outcome` in the report and keeps the terminal live with
`worker-retain`. `del --close` releases a settled worker (`worker-release`)
or stops a running one (`worker-stop`). If `new omp` fails at
`agent_readiness` (a cold omp start can outrun Orca's readiness rule), the
error names the failed stage and OSW has already released the orphan
terminal and removed the provisional record — retrying the dispatch is safe.

The Jev mode is the machine-wide `OSW_JEV_MODE` environment variable
(`off|shadow|enforce`; unset or invalid means `off`) — the only mode control;
OSW keeps no per-project Jev config. When the mode is not `off`, Jev is
consulted at every readiness release — including `ready` verdicts (advisory,
never a hold) and tui-idle releases — and once at each supervised launch.
A Jev call that fails or times out degrades to the deterministic path and is
recorded as `jev_degraded` (never blocks a send). Frames showing an upstream
API retry ("Waiting for API response · will retry · check your network") are
their own verdict (`api_retry`): the watcher sends nothing, fails nothing and
holds nothing — it notifies once and re-arms the ready clock (up to the hard
deadline) so the pane has as long as it needs to recover.

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
OSW prints a warning and you must poll `list`/`status` yourself. A record
that already has a `guardian` keeps notifying that guardian on `use`
unless `--caller-terminal` names another target explicitly.

`use <agent-id>` requires a settled agent (`done`, `error`, `lost`, or
`enrolled`) and preserves its id and provider context; do not pass
`--prefix`. `use <terminal>` adopts a terminal in the same worktree and may
use `--prefix`; it refuses a pane that already has a record — with a live
watcher it is busy, otherwise use `use <agent-id>` for that record.

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
worker's work summary. `list` and `status` mark a mid-task record whose
watcher process is gone (for example after an Orca or PC restart) as
`lost` and write that back; `lost` records are candidates for `revive`.

## Takeover and Recovery

```text
python <skill-dir>/osw.py enroll
python <skill-dir>/osw.py guard [<agent-id>...] [--caller-terminal <handle>]
python <skill-dir>/osw.py revive
python <skill-dir>/osw.py revive <agent-id> [--session <session-id>]
python <skill-dir>/osw.py stop <agent-id>
```

`enroll` records the agent terminals of this worktree that OSW did not start.
It skips the caller's own terminal and panes that already have a record, takes
the provider from Orca's `agentIdentity` (falling back to the ps `agentType`),
and writes one `enrolled` record per pane (`origin: enroll`, no watcher, no
task, no guardian). Providers OSW cannot launch are recorded as
`support: guard_only` with `support_reason: unsupported_provider`. Output:
`{"enrolled": [<records>], "skipped": [{"terminal", "reason"}]}`, where a skip
reason is `caller_terminal`, `no_pane_key`, `already_recorded`, `no_agent`, or
`write_failed`.

`guard` makes the executor — `--caller-terminal`, else `ORCA_TERMINAL_HANDLE`
— the guardian (notification target) of the named agents; without ids it takes
every unguarded `enrolled`/`done`/`error` record. An executor that is itself an
OSW agent is stored as `{agent_id}`, anything else as `{terminal, pane_key}`.
Per agent: a live watcher is terminated and restarted to follow the turn in
progress (`watcher_restarted`); an unwatched pane ps reports `working` gets a
watcher that follows it (`follow_started`); an idle agent only records the
guardian (`guardian_set`). Repeating `guard` replaces the guardian. Output:
`{"guardian", "guarded": [{"agent_id", "action"}], "skipped": [{"agent_id",
"reason"}]}`.

`revive` without an id lists `{"revivable": [{agent_id, provider, state,
session_id}], "not_revivable": [{agent_id, reason}]}`; reasons are
`unsupported_provider`, `watcher_running`, `not_settled`, and `no_session`.
`revive <id>` needs `support: full`, state `lost`/`done`/`error`/`enrolled`,
and no live watcher. The session is `--session`, else the record's
`session_id`, else the pane's current session. The resume command is
`claude --resume <id>`, `codex resume <id>`, `pi --session <id>`,
`kimi --session session_<id>`, `grok --resume <id>`, or `omp --resume <id>`,
with the provider's usual autonomy and model flags. It is typed into the
agent's original pane when that pane is a confirmed empty shell
(`placement: in_place`, `pane_key` kept, only `terminal` updated): the pane
is still listed, shows no `agentIdentity`, its `worktree ps` row is not
`working` (a cold restart restores a stale `done` row, so ps can only veto),
its title names a known shell (pwsh/PowerShell, cmd, or a POSIX shell) so the
line can be quoted for it, and Jev `ready_state` judges the screen
`shell_prompt`. Otherwise — including `OSW_JEV_MODE=off` or a degraded Jev
call — it opens a new terminal (`placement: new_terminal`) and updates
`terminal` and `pane_key`. The agent id never changes; the output and the
`agent_revived` event carry `placement`, and the event names why in-place
was refused (`in_place_refused`). A record that was `lost` (it was
mid-task) gets `resume_turn: true` and a watcher that sends the fixed
`continue` prompt and then runs the normal completion and handoff flow; any
other record comes back `enrolled` and idle. An unresolvable guardian is
replaced by the executor. A revived `omp` that was mid-task is claimed with a
new supervised Dispatch (`worker-start --terminal`) after its TUI comes up.

`stop <id>` interrupts the agent's current turn: Ctrl-C (`terminal send
--interrupt`) for claude, codex, kimi, and grok; a bare Esc byte for pi and
omp, which ignore Ctrl-C. A stale handle is re-resolved once by pane. With a
live watcher it also writes the stop sidecar `.orca/osw/agents/<id>.stop`
(the request time): the watcher ends the turn once ps reports it interrupted
or done, or the pane is output-silent for 5s, records `error` with
`completion_source: interrupted`, skips the handoff, notifies the guardian,
and deletes the sidecar. Without a watcher only the key is sent, and the
pane's session id is recorded first. Output:
`{"agent_id", "key": "interrupt"|"esc", "watcher"}`. Unknown providers are
rejected.

Record fields used by these commands (all optional; older records still
load):

| Field | Meaning |
|---|---|
| `origin` | `new`, `use`, or `enroll`; reused records keep theirs |
| `session_id` | Provider session id, recorded by watchers after each turn and by `enroll`/`stop`/`revive` |
| `guardian` | `{agent_id}` or `{terminal, pane_key}`; the notification target |
| `support` | `full`, or `guard_only` (with `support_reason: unsupported_provider`) |
| `follow_turn` | The watcher takes over the running turn instead of sending a prompt |
| `resume_turn` | The watcher's task prompt is the fixed `continue` |
| `notified_asks` | Ids of supervised-worker questions already relayed |

`enrolled` means recorded without a watcher; `lost` means a non-final record
whose watcher process is gone.

Session ids come from Orca's agent-hook cache
`%APPDATA%\orca\agent-hooks\last-status.json` (here
`C:\Users\shiki\AppData\Roaming\orca\agent-hooks\last-status.json`), keyed by
pane. It is not a public Orca interface: OSW only reads it when its `version`
is the one it understands (2); otherwise, or when the pane has no entry yet,
the session id is empty.

Known limits: a revived `omp` that was idle has no Dispatch; the next
`use <id>` creates one. To continue an `omp` after `stop`, use `revive` —
reattaching Orca tracking after an Esc interrupt is unverified.
Manually launched `omp` panes are tracked by `worktree ps` only after their
first turn, and at every turn end Orca drops their status reports (it reads
OSC 133;D as "command finished"); the row comes back only through the local
`~/.omp/agent/extensions/orca-status-watchdog.ts` extension, about 2s
later. Without that extension omp tracking is unreliable.

## Blocking Screens

A dialog that owns the agent's input (an update prompt, a trust or login
screen) is discovered, classified, and handled by the watcher during the
ready wait and the task send, and reported at a missing receipt or a stall.

- Discovery: Orca's `agentWait.reason` first — an `agent_prompt_blocked`
  send error carries it, e.g. `agent-update-prompt` for codex's update
  dialog — else the Jev `ready_state` answer (`blocked_dialog` ≥ 0.8; a
  booting TUI's `blocked_startup` keeps its own bounded hold instead).
- Classification: the kind Orca named, else the Jev question
  `blocker_kind` (`update_prompt`, `trust_folder`, `auth`, `permission`,
  `announcement`, `rate_limit`, `crashed_to_shell`, `other`); an answer
  below 0.8 counts as `other`.
- Mode (`OSW_JEV_MODE`): `off` only discovers and notifies (no Jev calls);
  `shadow` records the keys it would press as `blocker_shadow_action`
  without pressing them and notifies the guardian exactly as `off` does;
  `enforce` presses whitelisted keys.
- Whitelist: only `(codex, update_prompt) → Enter`, which takes the
  dialog's default "1. Update now" and runs the update. Orca refuses a plain
  send to that dialog, so the Enter goes out as
  `terminal send --text "" --enter --interrupt`, the one form observed to get
  through. Every whitelist entry needs a screen sample under
  `tests/fixtures/frames/`; the codex one is a text transcription from the
  research notes, not a raw capture.
- Never automated: `auth`, `permission`, and any screen mentioning deletion
  or payment, whatever its kind. These, and every kind outside the
  whitelist, only notify.
- Update lock: one pane per provider per machine applies an update at a
  time (`%LOCALAPPDATA%\osw\locks\update-<provider>.lock`, created
  exclusively, expiring after 15 minutes so a crashed holder cannot
  dead-lock it). A pane that cannot take it notifies once and keeps
  waiting; the holder releases it when its pane is ready again or its
  watcher ends.
- After pressing keys the screen is judged again; still blocked means
  automatic handling of that kind stops for the run and the guardian is
  told. Output silence alone no longer counts as ready after an action:
  codex exits to its shell after updating, which then needs `revive` or a
  manual relaunch.
- A send Orca refused never reached the agent, so it is retried once the
  pane is ready again (up to 5 times, 20s apart); a send that stays blocked
  ends the task as `error` with `completion_source: blocked`. At a missing
  receipt or a stall the prompt is already in flight, so a blocker there is
  only reported, never acted on.

Every step is a structured event: `turn_send_blocked`, `blocker_detected`,
`blocker_action`, `blocker_shadow_action`, `blocker_action_deferred`,
`blocker_lock_acquired` / `blocker_lock_busy` / `blocker_lock_released`,
`blocker_still_blocked`, `blocker_action_failed`, and `blocker_notified`.

## Worker Questions

While a supervised worker runs, the watcher relays its unanswered Orca
orchestration messages of type `question` (what `orchestration ask` posts)
and `escalation` to the guardian, once per message id:

```text
# [osw] ask agent=<id> message=<msg-id> reply="orca orchestration reply --id <msg-id> --body <text>" text=<question>
```

The ids are recorded in the agent field `notified_asks` before notifying, so
a restarted watcher never repeats one. On Windows `orca.cmd` refuses
`orchestration reply` (it cannot safely forward message bodies); run the
reply with the Orca installation's `resources\bin\orca.exe` instead.

## Notifications

| Notification | Coordinator action |
|---|---|
| `agent-waiting` | Answer the worker directly in its terminal. |
| `agent-busy` | Informational: the pane was still finishing previous work at dispatch; the ready wait is extended and the prompt goes out once the pane becomes ready. |
| `agent-stalled` | Inspect `logs`, then inspect the terminal before intervening. |
| `prompt-delivery-unconfirmed` | Sent only for a pane `worktree ps` does not track: the prompt was written but its send confirmation was not observed. Check that terminal. On tracked panes this is only logged; the receipt window decides, and a swallowed prompt fails as `no_receipt` with its own notification. |
| `blocked` | A dialog owns the worker's input (`kind=`, `screen=` excerpt). Answer it in that terminal unless OSW is handling it; `stopped` means automatic handling gave up. |
| `ask` | A supervised worker asked a question; answer with the given `orchestration reply` command. |
| `task-finished` | Open the report and handoff; move the task to acceptance. |
| `task-error` | Read the report reason and logs before retrying. `pane_busy` means the worker was still provably busy at the ready deadline — retry after it finishes; it is not a dead terminal. `interrupted` means `stop` ended the turn; no handoff was requested. `blocked` means a dialog kept refusing the prompt. |

A `task-finished` notification can report `handoff_missing`; inspect the actual
workspace and terminal before deciding whether the result is usable.
`warning=no_tool_activity` means `worktree ps` tracked the task turn (or a
followed turn) but never reported a tool call while it was working: the
agent may have only answered in text. The report then has
`tools_observed: false`; the state is still `done` and nothing is resent —
check the handoff and the workspace before accepting. `tools_observed` is
`true` when a tool was seen and `null` when ps never tracked the pane, in
which case nothing is claimed. A
followed turn (`guard`) sends no handoff; `already_done` means the turn had
already finished when the watcher took over.

Each notification resolves its target afresh: a `{agent_id}` guardian is read
from that agent's record for its current terminal, a `{terminal, pane_key}`
guardian is used directly, and without a guardian the recorded caller
terminal is used. A guardian agent whose record cannot be read falls back to
the caller terminal and is logged as `guardian_unresolved`. A stale guardian
handle is re-resolved once by pane and the send retried once.

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
python <skill-dir>/osw.py stop <agent-id>
python <skill-dir>/osw.py del <agent-id> [--close]
```

Use `stop` to interrupt a worker's current turn (see Takeover and Recovery).
`del` removes the management record; `--close` also closes the Orca terminal.

All terminal messages must be single-line because Windows transport can
truncate text at newlines. OSW itself only ever types four things into a
terminal: the fixed wrap-up instruction (`watcher.HANDOFF_TEMPLATE`), the
fixed resume prompt `continue` (`watcher.RESUME_TEMPLATE`), whitelisted
dialog keys (`osw.blockers.WHITELIST`), and the resume command `revive`
types into a confirmed empty shell. Each working
directory is an independent OSW scope; OSW does not create or manage
worktrees.
