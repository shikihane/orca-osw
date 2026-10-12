# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

See also `AGENTS.md` for repository guidelines shared across agent tools.

## What This Is

OSW ("Orca Agent Supervisor") is a Python CLI that dispatches and supervises
Orca-backed agent sessions (`claude`, `codex`, `pi`, `omp`, `kimi`, `grok`) in the
current working directory. There is **no daemon**: `new`/`use` creates or
adopts an Orca terminal, writes one agent record, spawns one detached watcher
process, and returns immediately. Orca itself is the source of truth for
agent state.

Runtime dependencies are `anyio` and `typer` only (`rich` optional for
colored logs, `pytest` for tests). `typesafe-sdk` is an optional extra used
only when the Jev mode is not `off`; install it into the same interpreter
that runs watchers (`sys.executable`).

## Commands

Run from the repo root:

```bash
python -m pip install anyio typer rich pytest   # dev setup

pytest -q                                       # full test suite
pytest tests/test_cli.py -v                     # one file
pytest tests/test_watcher.py::test_name -v      # one test

python skill/osw.py init                        # init .orca/osw/ state in cwd
python skill/osw.py models <claude|codex|pi|omp|kimi|grok>  # read-only model discovery
python skill/osw.py new <provider> --prefix <role> --model <m> --thinking <t> "<task>"
python skill/osw.py use <agent-id|terminal-handle> "<follow-up>"
python skill/osw.py list --json
python skill/osw.py status
python skill/osw.py enroll                       # record unmanaged agent terminals (state enrolled)
python skill/osw.py guard [<agent_id>...]        # become their guardian; follow a running turn
python skill/osw.py revive [<agent_id> [--session <sid>]]  # list / resume provider sessions
python skill/osw.py stop <agent_id>              # interrupt the current turn
python skill/osw.py jev status [--check]         # global OSW_JEV_MODE + env state; --check probes the endpoint
python skill/osw.py logs --agent <agent_id>
```

`conftest.py` inserts `skill/` into `sys.path`, so tests import the package as
`osw.*` — run pytest from the repo root. Unit tests mock Orca calls with
`AsyncMock`; never require a live Orca instance.

## Architecture

Module responsibilities (keep code in its lane):

- `skill/osw/cli.py` — all typer commands. `new` creates an Orca terminal
  running the bare provider TUI (never the task prompt — the watcher sends
  it), allocates an agent id, writes the agent record, spawns the watcher.
  For `omp` it instead binds the caller to the project's orchestration Run
  and starts a supervised worker (`_ensure_orchestration_run`,
  `_launch_supervised`, `_supervised_spec`), with Orca owning terminal
  creation, readiness and prompt injection; `del --close` releases a settled
  supervised worker (`worker-release`) or stops a running one
  (`worker-stop`).
  `use` adopts an existing terminal after `terminal_show` confirms it belongs
  to the current worktree; passing an existing agent id reuses that id (only
  allowed once the agent is done/error/lost/enrolled). The hidden `watch`
  command is the watcher process entry point. Takeover and recovery:
  `enroll` writes `enrolled` records (`origin: enroll`, `support:
  full|guard_only`) for this worktree's unmanaged agent panes; `guard`
  stores a `guardian` (`{agent_id}` for an OSW agent, else `{terminal,
  pane_key}`) and restarts or starts a watcher with `follow_turn` for a
  running turn; `revive` reopens a provider session — typed into the
  original pane when `_confirm_empty_shell` proves it an empty shell (pane
  listed, no `agentIdentity`, ps row not `working`, a known shell title to
  quote for via `providers.shell_command_line`, Jev `ready_state` says
  `shell_prompt`; Jev off/degraded means unconfirmed), else in a new
  terminal (`providers.resume_command`); the output and `agent_revived`
  event carry `placement: in_place|new_terminal` — and sets `resume_turn`
  when the record was `lost`; `stop` sends the provider's interrupt key (`STOP_KEYS`: Ctrl-C,
  or Esc for pi/omp) and, with a live watcher, writes the
  `agents/<id>.stop` sidecar. `list`/`status`/`use`/`revive` mark a
  non-final record whose watcher died as `lost`.
- `skill/osw/watcher.py` — per-agent detached supervisor, one process per
  dispatched task; `new` and `use` share this one state machine. Readiness
  first (`worktree ps` "done" for tracked panes, `tui-idle` fallback for
  untracked ones), then phase "task" (send prompt, wait for the turn to
  finish) then phase "handoff" (send the fixed `HANDOFF_TEMPLATE`
  instruction, verify the handoff markdown exists, retry once). Finally
  writes a JSON report and notifies the caller terminal with a single-line
  `# [osw] ...` message. Also notifies on `AskUserQuestion` waits and
  5-minute stalls. Agents recorded with `launch_mode: supervised`
  (`_run_supervised`) skip readiness and prompts entirely and follow the
  Orca Dispatch lifecycle instead. `follow_turn` (`_run_follow`) skips
  ready and send and only supervises the running turn — no receipt or echo
  check, no handoff; a pane already `done` at takeover finalizes as
  `already_done`. `resume_turn` sends `RESUME_TEMPLATE` as the task prompt
  through the normal flow. A stop sidecar ends the current turn once ps
  reports it interrupted or done, or the pane is silent for
  `STOP_SILENCE_MS`, as `error`/`interrupted` with no handoff; a sidecar
  older than the record's `created_at` is discarded at start. After every
  turn the watcher records `session_id` via `sessions.session_for_pane`
  (best effort). `follow_turn`/`resume_turn` are cleared and the sidecar
  deleted in `_finalize`. Blocking screens (`_check_blocker`, plan §2.2):
  an `agent_prompt_blocked` send error (its `agentWait`) or a Jev
  `blocked_dialog` answer at a ready release goes through
  `blockers.detect` → `jev.ask_blocker` when the kind is open →
  `blockers.decide(provider, kind, OSW_JEV_MODE)`; `act` presses the
  whitelisted keys (after `acquire_update_lock` for `update_prompt`; a busy
  lock notifies once and keeps waiting), `shadow` records
  `blocker_shadow_action` and notifies like `notify` (plan §3.2), `notify`
  sends one `# [osw] blocked agent=<id> kind=<kind> ... screen=<excerpt>`
  per blocker. A refused send is retried
  after ready (`BLOCKED_SEND_LIMIT`), else `completion_source: blocked`;
  still blocked after acting stops automation for that kind; background
  shadow judgments only defer, receipt/stall probes only report. The lock
  is released when the pane is ready again and in `_finalize`. Supervised
  workers' unanswered `question`/`escalation` messages are relayed once per
  id (`_relay_worker_asks`, ids in `notified_asks`). Tool activity (plan
  §3.1): `_wait_turn_done` records `_turn_tracked` (ps showed the pane)
  and `_turn_tools_seen` (a `toolName` while `working`; a done row's is
  stale); `_note_turn_tools` turns that into `true`/`false`/`null` per
  turn (`turn_finished.tools_observed`). A `done` task or followed turn
  with `false` gets `tools_observed: false` in the report, a
  `no_tool_activity` event and `warning=no_tool_activity` in the
  `task-finished` line — never a resend or a state change.
- `skill/osw/orca_cli.py` — thin async wrapper around `orca ... --json` via
  `anyio.run_process`; raises `OrcaError`. Caller-terminal detection prints a
  UUID marker and finds which terminal's preview contains it (`terminal show`
  alone can't identify "the terminal I'm running in"). The supervised
  orchestration wrappers (`orchestration_run_*`, `orchestration_worker_*`)
  carry the contract parsers `worker_start_receipt` and `worker_settlement`;
  `orchestration_inbox` + `worker_asks` find a worker's unanswered asks.
  `terminal_send_dialog_enter` (`--text "" --enter --interrupt`) is the
  only send observed to get through codex's update dialog, which Orca
  otherwise answers with `agent_prompt_blocked`. On Windows `orca.cmd`
  refuses `orchestration reply`; humans replying must use the Orca
  installation's `resources\bin\orca.exe`.
- `skill/osw/blockers.py` — pure blocking-screen policy: `detect`,
  `classify`, `decide` (`off` notify, `shadow` record, `enforce` act on
  `WHITELIST` only), `FORBIDDEN_KINDS` (`auth`, `permission`) plus a
  delete/payment text veto, and the machine-wide update lock
  (`%LOCALAPPDATA%\osw\locks\update-<provider>.lock`, `open(..., "x")`,
  15-minute expiry). `WHITELIST` holds only `(codex, update_prompt) →
  enter`; every entry needs a sample in `tests/fixtures/frames/`.
- `skill/osw/providers.py` — provider command construction (autonomy flags,
  `--model`/`--thinking` translation per provider) and read-only model
  variant discovery by probing CLIs. `SUPERVISED_PROVIDERS` maps providers
  OSW launches through Orca orchestration (`omp`); `validate_supervised_args`
  rejects flags that launch cannot express (`--thinking`).
- `skill/osw/state.py` — all file I/O under `.orca/osw/`: `state.json`,
  `agents/<agent_id>.json`, `reports/`, `handoffs/`, `logs/`. Writes are
  atomic (temp file + `os.replace`, with retry on transient Windows lock
  errors, winerror 5/32 → `StateWriteError`). `alloc_agent_id` uses
  `open(..., "x")` on the record file as the lock. Stop requests live in
  the `agents/<id>.stop` sidecar (`write_stop_request`,
  `read_stop_request`, `clear_stop_request`), not in the record.
- `skill/osw/sessions.py` — `session_for_pane(pane_key)` reads the
  provider session id from Orca's agent-hook cache
  `%APPDATA%/orca/agent-hooks/last-status.json` (here
  `C:\Users\shiki\AppData\Roaming/orca/agent-hooks/last-status.json`): not a public
  Orca interface, so anything but `version: 2`, a missing file, or a
  pane without an entry yields "".
- `skill/osw/log.py` — structured JSONL events plus per-agent log files.
- `skill/osw/ready_gate.py` — pure functions: the deterministic readiness
  gate (`gate_verdict`: ready/not_ready/abstain), frame sanitization,
  the normalized frame hash, and the Jev payload window.
- `skill/osw/jev.py` — optional Jev (TypeSafe) integration: the mode is
  the machine-wide `OSW_JEV_MODE` environment variable (`off|shadow|enforce`;
  unset = off) and nothing else — there is no per-project Jev config, no
  mode commands. Lazy SDK client, the `ready_state` and `blocker_kind`
  questions (`ask_ready`/`ask_blocker` over one shared `_ask_choice`), a fail-open
  call wrapper with raw storage under `.orca/osw/jev/`, and hold thresholds.
  `ensure_proxy_env` adopts the runtime `PI_PROXY` when no standard proxy
  variable is set.
  Operator override 2026-10-11: the ready gate consults Jev on every
  invocation — including `ready` verdicts (advisory; the deterministic
  predicate stays authoritative and no hold can start there) and the
  `tui_idle` release path, which previously bypassed the gate entirely —
  and a supervised launch gets one advisory judgment
  (`_supervised_launch_judgment`). This deliberately retires the design's
  "typical dispatch = zero calls" property: the mode must not be a no-op
  in practice. The machine-wide `OSW_JEV_MODE` environment variable
  (`off|shadow|enforce`) is the sole mode control; under a global
  `enforce` the full enforceable whitelist applies everywhere.
- `skill/osw/process.py` — `hidden_subprocess_kwargs()` so subprocesses spawn
  without console windows on Windows.

Completion detection: the watcher maps the terminal to a `paneKey`
(`tabId:leafId`) and polls `orca worktree ps --json` for that pane's agent
entry (`state == "done"` with `stateStartedAt` after the prompt was sent and
newer than the pane's pre-send "done" timestamp). A fresh done also has to
pass the completion guards: it must not be a foreign session's (a ps
`prompt` that is pure ASCII, non-empty, and sharing neither the receipt
prefix nor any 12-char run with what was sent — a nested agent inheriting
this pane's `ORCA_*` hooks reports its states under the same pane; a
rejected done is recorded as `foreign_done_ignored`), it must persist
across two polls, and the pane's output must have been silent for
`watcher.COMPLETION_SILENCE_MS` (3s). An idle observation alone
is never completion — a turn with no task-correlated evidence fails closed
as `error`, not `done`. CLIs Orca does not track in `worktree ps` fall back
to `lastOutputAt` stable-idle detection (60s of silence after new output);
the prompt's own terminal echo never counts as output, and the quick
tui-idle exit after tracking loss requires the pane to have been observed
`working` this turn — a pane Orca never registered (e.g. one spawned
moments ago, before ps catches up) rides the slow stable-silence path.
Orca recognizes `claude`, `codex`, and `pi` TUIs (as of Orca 1.4.x); Kimi
and Grok still work through the generic terminal-output fallback when
untracked. Supervised `omp` workers do not use any of this: they complete on
Orca's Dispatch lifecycle (below).
Tracked panes must additionally acknowledge each prompt within
`RECEIPT_TIMEOUT_MS` (report `working`, finish the turn, or echo the
prompt text in ps) — no acknowledgement means the input was swallowed by
something other than the agent's composer (login screen, update dialog):
the turn fails closed as `no_receipt` and the caller is notified.
Independently, every sent prompt's echo is verified against the
terminal's scrollback (`terminal read` — never the preview tail, which
a full-screen TUI permanently occupies with its composer box). The
check pages past the newest screenful when the read cursors
(`latestCursor - oldestCursor`) say more is retained, then judges the
surface for usability before matching: a tail reaching only a fraction
of the retained buffer (alternate-screen TUI), or one that is just
repaint frames (Claude Code mid-turn floods the buffer with its
redrawn screen, where the only matchable text would be a swallowed
prompt still sitting in the composer), has no echo surface, and the
check abstains (`prompt_echo_unverifiable`). A missing echo on a
usable surface alarms the caller only when ps does not track the pane
(on a tracked pane the receipt window is authoritative) and never
triggers a resend: a false miss would inject a duplicate prompt into a
working agent's input queue. A verified echo counts as prompt receipt,
except on the handoff retry, whose byte-identical resend would match
the first attempt's echo.
Supervised completion (`launch_mode: supervised`, set by `new omp`): the
watcher never sends the task turn and never reads `worktree ps` for
completion. `worker-start` waits for readiness and injects the task spec
(which carries the handoff/report contract) and the worker reports
`worker_done` itself; the watcher polls `orca orchestration worker-show`
(`SUPERVISED_POLL_SECS`) until the Dispatch settles, records
`orchestration_outcome`, verifies the handoff file (falling back to the
standard wrap-up turn plus file-stability polling when the worker skipped the
contract), keeps the terminal live with `worker-retain`, and notifies the
caller. This path exists for two measured reasons (2026-10-10/11 research,
see README §Supervised omp workers): `worktree ps` tracking of omp is
intermittent — a manually launched omp is tracked only after its first
turn, and at every turn end Orca reads OSC 133;D as "command finished",
retires the pane's launch authority and drops its status posts until the
local `~/.omp/agent/extensions/orca-status-watchdog.ts` extension replays
the turn boundary about 2s later (without that extension tracking is
unreliable) — and omp's status bar repaints about once a second, so every
output-silence heuristic waits forever.

Provider autonomy flags skip approval prompts, including OMP's
`--auto-approve` and Kimi's `--yolo`; use them only in trusted workspaces
(for supervised `omp` the flag is recorded as the equivalent manual launch
only — the worker's argv follows Orca's settings).

Caller notification: `new`/`use` resolve the dispatching terminal from
`ORCA_TERMINAL_HANDLE` (exported by Orca into every terminal it creates and
inherited through agent tool pipelines); explicit `--caller-terminal` wins,
and the interactive preview-marker probe is the last-ditch fallback for
tty sessions outside Orca-created terminals. A record with a `guardian`
notifies the guardian instead (and `use` keeps it unless
`--caller-terminal` is passed). The watcher resolves the target before
every send: `{agent_id}` is read from that agent's record for its current
`terminal`/`pane_key`, `{terminal, pane_key}` is used directly, no guardian
means `caller_terminal`. An unreadable guardian agent record falls back to
`caller_terminal` (`guardian_unresolved` event); a `terminal_handle_stale`
send is retried once after rebinding by `pane_key` (an inline guardian's
new handle is written back; another agent's record is never written).

Ownership invariant: the CLI writes the agent record at creation, and
afterwards only while no watcher process is alive; while a watcher runs it
is the only writer. That is why `stop` signals a live watcher through the
`agents/<id>.stop` sidecar instead of the record. `list`/`status` merge
agent files with a single `worktree ps` snapshot by `pane_key` plus watcher
PID liveness.

`state.json` holds only `{version: 3, project_root}`. `models` never writes
state.

## Orca CLI Gotchas

- Orca JSON responses are nested under `result`; unwrap `result.terminal`,
  `result.terminals`, or `result.worktrees`.
- `orca terminal show` without `--terminal` returns the UI-focused terminal,
  not the current process terminal.
- Multiline text through `orca ... --text` gets truncated on Windows. Every
  message sent to a terminal must be a single line (see `watcher.one_line`).
- Orca's agent-prompt send (text + `--enter`) runs a renderGate for
  claude/codex that waits for `ESC[?25h` with an 8s hard timeout; current
  Claude Code paints its own cursor and never emits it, so every
  single-shot send stalls the full 8s. OSW sends claude in two stages
  instead (`watcher.SPLIT_SEND_PROVIDERS`): raw text write without
  `--enter`, a bounded `terminal read --screen` echo wait, then `--enter`
  alone.
- `orca worktree ps --json` fields per agent: `state`, `prompt`, `toolName`,
  `lastAssistantMessage`, `paneKey`, `stateStartedAt`, `updatedAt`.

## Hard Constraints

- Do not reintroduce a `serve` daemon or Orca orchestration protocol
  wrappers.
- Do not add CLI options the user did not request.
- OSW may inject only: the fixed, single-line `watcher.HANDOFF_TEMPLATE`;
  `watcher.RESUME_TEMPLATE` (`continue`, sent only for a `resume_turn`
  after `revive`); the keys of `blockers.WHITELIST` entries (each backed by
  a screen sample); and the resume command `revive` types into a
  confirmed empty shell.
- Never launch a nested agent process inside a managed pane without
  clearing the inherited `ORCA_*` environment variables: a nested session's
  hooks otherwise report its state under the parent pane and can produce
  false `done` states (the watcher's completion guards mitigate, but do not
  rely on them).
- Never edit `.orca/osw/` runtime records by hand (including the
  `agents/<id>.stop` sidecar); never commit generated `.orca/` state, logs,
  reports, or handoff files (`.gitignore` covers this).
- Provider launch commands include autonomy/bypass flags by design — only
  for trusted, externally controlled workspaces.

## Known Limits

- A revived `omp` that was idle has no Dispatch (`dispatch_id` empty); the
  next `use <id>` creates one.
- To continue an `omp` after `stop`, use `revive`: whether Orca tracking
  reattaches after an Esc interrupt is unverified.
- `last-status.json` may change format between Orca versions; a `version`
  mismatch simply leaves `session_id` empty.
- codex exits to its shell after applying an update; OSW then waits for
  agent evidence (output silence alone is not ready) and the ready deadline
  ends the task — relaunch with `revive`. The codex update sample is a text
  transcription, not a raw capture.
- Ask answer detection relies on a reply threading under the question
  (`thread_id`), not yet observed live; `notified_asks` still prevents
  repeats.

## Conventions

- Agent ids are `<prefix>_NNN` (`agent_001`, `research_001`, `debug_001`);
  prefixes: `research`, `code`, `test`, `debug`, `review`, `misc`.
- 4-space indentation, type hints on public helpers, small focused functions.
- Commit style: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`.
- Add or update tests before changing behavior; put them next to the affected
  module (`tests/test_providers.py` for launch commands, `tests/test_cli.py`
  for user-visible CLI behavior).

## OSW Provider Policy

| Task type | Provider | Model | Thinking |
|---|---|---|---|
| test | kimi | kimi-code/k3 | n/a (kimi rejects --thinking; effort is set per model alias in kimi's config.toml) |
| review | claude | claude-opus-5-5[1m] | high |
| implementation | claude | claude-opus-5-5[1m] | high |
| verification | omp | scnet/DeepSeek-V4.1-Flash | n/a (supervised omp rejects --thinking; level follows omp/Orca settings) |
| source-research | claude | sonnet | medium |

Note: `osw models kimi` prints `<display-name> <command>` rows, e.g.
`kimi-k3   kimi --model kimi-code/k3`. The first column is a display
label only; `new --model` needs the real alias from the second column
(`kimi-code/k3`), matching a `[models."..."]` key in `~/.kimi-code/config.toml`.
Passing the display label causes kimi to fail with `[config.invalid]`.

## Skill Installation

Install or update the skill with `python install.py`. It physically copies
the complete `skill/` directory to each destination — never junctions,
symlinks, hard links, or any link-like mechanism, and never resolving back
to this repo. Link-shaped installs are removed as links and replaced with
real copies. Default destinations:

- Codex: `C:\Users\shiki\.codex\skills\osw\`
- Agents: `C:\Users\shiki\.agents\skills\osw\`
- Claude: `C:\Users\shiki\.claude\skills\osw\`

The script validates every destination after copying: an independent
physical copy (no links anywhere, nothing resolving into this repo),
byte-identical to the source, and a working `osw.py --help` — so installs
keep working even if this repository path is unavailable. Run it after
every skill change.
