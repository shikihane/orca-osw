# Repository Guidelines

## Project Structure & Module Organization

This repository contains OSW, a Python CLI for managing Orca-backed agent sessions.
Source code lives under `skill/osw/`, with `skill/osw.py` as the entry point.
The deterministic readiness gate and the optional Jev (TypeSafe) integration
live in `skill/osw/ready_gate.py` and `skill/osw/jev.py`; the machine-wide
`OSW_JEV_MODE` is the only source of the Jev mode, and no project config block
sets or overrides it. The offline evaluation harness is `tools/jev_eval.py`.
`skill/osw/sessions.py` reads provider session ids from Orca's agent-hook cache
(`%APPDATA%/orca/agent-hooks/last-status.json`, a non-public format checked
by `version`). `skill/osw/blockers.py` holds the blocking-screen policy
(detection, classification, the per-mode decision, the `(provider, kind)`
key whitelist — only codex's update prompt — never-automated kinds, and
the machine-wide update lock); every whitelist entry needs a screen sample
under `tests/fixtures/frames/`. In `shadow` mode a whitelisted blocker is
recorded (`blocker_shadow_action`) and notified, never acted on. A task or
followed turn that ps tracked without ever showing a tool call finishes
`done` with `tools_observed: false` and `warning=no_tool_activity`; it is
never resent.
`skill/osw/hosts.py` is the host seam: the `Host` value with four
operations (`exec`, `read_file`, `write_file`, `exists`), a local and an
SSH implementation, the host registry `hosts.json` beside `state.json`,
`<host>/<agent_id>` reference parsing, the doctor ladder and the closed
error taxonomy. Orca dispatch goes through pairing (`--environment`), data
through SSH (`ssh`, `sftp`); nothing outside this module may build an
`ssh`/`sftp` argument list or read or write a file on another machine.
The control plane lives in the package `skill/osw/mailbox/` (envelope
validation, paths, identity, the SQLite store, the spool, adapters, delivery
and the daemon), with its rules and seams beside it in `skill/osw/`:
`fastpath.py` (pure, first-match deterministic classification; one fixture per
rule under `tests/fixtures/fastpath/`), `mandate.py` plus the human-owned
`skill/osw/mandate.json` (closed-schema Ticket limits, read from the committed
HEAD), `dispatcher.py` (the only applier of Decisions, through the existing
`new`/`use`/`stop`/`orchestration reply` paths) and `mailbox_cli.py` (the
`mailbox` verbs). Producers append envelopes to the spool under
`.orca/osw/mailbox/spool/`; the daemon is the only writer of the store.
The Codex skill guide is `skill/SKILL.md`. Tests live in `tests/`, with
`conftest.py` adding the local package path. User-facing docs are in
`README.md`, `README_zh.md`, `QUICK.md`, and `QUICK_zh.md`. Runtime state such
as `.orca/osw/agents/`, logs, reports, and handoffs is generated locally and
should not be treated as source.

## Build, Test, and Development Commands

- `python -m pip install anyio typer rich pytest` installs local development
  dependencies.
- `pytest -q` runs the full test suite.
- `pytest tests/test_cli.py -v` runs focused CLI tests.
- `python skill/osw.py --help` checks the top-level CLI.
- `python skill/osw.py host --help`, `python skill/osw.py host doctor <host>
  --json` verify the host verbs and the doctor ladder against a real machine.
- `python skill/osw.py new --help` and `python skill/osw.py use --help` verify
  provider and adoption options; both, plus `enroll --help`, list `--on <host>`.
- `python skill/osw.py enroll|guard|revive|stop --help` cover takeover and
  recovery: `enroll` records unmanaged agent panes (`enrolled`), `guard`
  sets the `guardian` and follows a running turn (`follow_turn`), `revive`
  resumes a provider session — in the original pane when it is a confirmed
  empty shell, else in a new terminal (`placement: in_place|new_terminal`;
  `resume_turn` for a `lost` agent) — and `stop` interrupts the current turn
  (`completion_source: interrupted`). Supervised workers' questions reach
  the guardian as `# [osw] ask ...` (ids in `notified_asks`); on Windows
  answer with `resources\bin\orca.exe orchestration reply`, since
  `orca.cmd` refuses it.
- `python skill/osw.py mailbox --help` lists the control-plane verbs.
  `mailbox start|stop|status --json` cover the daemon lifecycle (a second
  `start` is a success); `mailbox send --kind request --text ...`,
  `mailbox wait --as router --timeout 0`, `mailbox show|trace <id>`,
  `mailbox decide <id> --action accept --reason ...` and `mailbox tickets`
  exercise one request end to end. `mailbox status` reports daemon liveness;
  the queue and ticket verbs only read the store.
- `python skill/osw.py workflow --help` lists the run verbs, which turn a
  phase plan into agents: `workflow run <name> --dry-run` prints the phases
  and the agent count without creating a run, `workflow run <name>` executes
  it (`--resume` reuses finished steps, `--policy strict|partial` sets what a
  failed step does to its phase, `--verify none|suspect|all` grades handoffs),
  `workflow status [<run>] [--watch]`, `workflow ls|show`, `workflow stop`,
  and `workflow accept|reject|retry <run> <step> --reason ...` for a human
  decision on one step. Definitions live in `.orca/osw/workflows/<name>.json`
  (project first, then `~`); runs in `.orca/osw/runs/<run_id>/`.

## Coding Style & Naming Conventions

Use Python 3 style with 4-space indentation, type hints for public helpers, and
small functions with clear names. Keep CLI behavior in `skill/osw/cli.py`,
provider command construction in `skill/osw/providers.py`, persistent state
helpers in `skill/osw/state.py`, and watcher logic in `skill/osw/watcher.py`.
Agent ids use `<prefix>_NNN`, for example `agent_001`, `research_001`, or
`debug_001`. While a watcher runs it is the only writer of its agent
record; the CLI signals it through side files such as the stop request
`.orca/osw/agents/<id>.stop`. OSW itself may inject only
`watcher.HANDOFF_TEMPLATE`, `watcher.RESUME_TEMPLATE`, the keys of
`blockers.WHITELIST` entries, the resume command `revive` types into a
confirmed empty shell, and the Router answer template built by
`dispatcher.ANSWER_TEMPLATE` (`# [osw] answer <decision_id>: <text>`): one
line, at most 4 KiB, always carrying the decision id for audit.

## Control Plane (Mailbox)

- **Local control plane only.** The Mailbox daemon is a local process per
  project root, under `.orca/osw/mailbox/`. It never executes work:
  execution authority stays with Orca (Run/Task/DAG/Dispatch) and the existing
  OSW paths, and the Dispatcher only calls `new`, `use`, `stop` and
  `orchestration reply`.
- **Direct mode is independent.** With the daemon never started or stopped,
  `new`, `use`, `status`, `list`, `revive`, `stop` and the current watcher
  notification path behave exactly as before. The spool keeps what producers
  wrote, a restart ingests it, and `status` reports `mailbox_down`.
- **One writer.** CLI, watchers and adapters only append envelopes to the
  spool; only the Mailbox writes the SQLite store. Delivery is at-least-once
  with idempotent application, so one worker, one answer and one stop happen
  per Ticket. `notified_asks` stays; the dedupe key is a second line of
  defense.
- **Roles.** `guardian` remains a notification address; the Router is a
  decision role, identified by `OSW_ROLE=router` plus a per-project role
  token, so a human in the same terminal keeps the human identity and
  `new`/`use` are refused with `router_forbidden`. The doorbell carries only a
  count and the `mailbox wait` command.
- **Mandate.** Ticket limits are a closed-schema, human-owned
  `skill/osw/mandate.json`; the Router may narrow it when opening a Ticket,
  never widen it, and a violation is recorded as `mandate_denied`.
- **Stays local.** Spool, store, daemon, role token and locks are local
  (`update-<provider>--<host>.lock` keeps the host key); remote events reach
  the spool through the host seam. Nothing runs on the far side.

## Workflow Runs

`super` fans one task list out once; a workflow turns a phase plan into agents
and keeps going across phases. The plan is a closed-schema JSON —
`.orca/osw/workflows/<name>.json` in the project (preferred) or the home
directory — never a script an agent composes and executes at run time. A person
does not have to write it by hand: `osw workflow plan "<one sentence>"` has one
ordinary agent draft that file, `defs.py` judges it, and `run --dry-run` shows
the expansion before anything is dispatched. The schema stays closed either
way, so a bad draft is refused by name, not half-run. `osw workflow go
"<one sentence>"` is the one-shot entry an agent calls: it writes one job
marker under `.orca/osw/planning/` and returns in about a second with
`PLANNING <slug> pid <n>`; a detached worker then drafts, validates, creates
the run and drives it — a run outlives the tool call that asked for it, and the
scheduler's own output goes to `.orca/osw/runs/<run_id>/scheduler.log`.
Progress is read from the archive (`workflow status --json`, which reports the
newest run plus any job still drafting), never from panes or that log.

- **Refuse where it happens.** A refusal is recorded, never routed around: a
  drafting failure keeps its job marker with `<code>: <reason>`, and a run that
  hits one writes it into its own `run.json` as `failed`. Nothing is retried
  behind the scenes, and nothing edits `config.json` or `AGENTS.md` to make a
  dispatch go through. A project that configured no provider dispatches with
  `pi` and says so once on stderr.

- **Shape.** `{name, max_agents?, approval?, phases[]}`; each phase is
  `{id, mode: single|for_each, from?, task, schema?}`. Phases run in declared
  order with a barrier: a phase starts only when every phase it names in
  `from` is terminal. `for_each` expands one agent per item taken from the
  array in the producer's output file — the path `{{output}}` names — or, for
  a producer that predates it, the last fenced ```json block of its handoff. A
  missing field or a missing file is refused (`no_items`) with the path named,
  never guessed. The handoff stays the watcher's four-section markdown, so
  machine-readable output has a channel of its own.
- **Identity and resume.** `step_key = sha256(phase 0 index 0 prompt 0
  schema_id)`, so a prompt must be a pure function of the definition and the
  previous phase's structured output; `defs.py` rejects `{{now}}`, `{{uuid}}`,
  `{{random}}` at load time for exactly that reason. `--resume` reuses every
  step whose key is recorded `done` and dispatches the rest — no cascade, a
  deliberate departure from Claude Code, whose agents share a prefix cache and
  ours do not.
- **Failure.** The watcher judges one pane; the scheduler judges the run. A
  lost or errored step gets a new agent with the same prompt up to
  `max_attempts` (default 2), then the step is `failed` and `--policy` decides:
  `strict` stops the run at that barrier, `partial` continues and marks the
  phase `partial` — a partial phase is never reported as success.
- **Verification.** A verifier gets the artifact and the claim only (blind),
  appends one line to `verdicts.jsonl`, and never calls OSW itself; the
  artifact's sha256 is taken before and after, and a change discards the
  verdict as `artifact_mutated`. An unparseable verdict gets one retry, then
  the step is `unverifiable` and waits for a human.
- **One writer per record.** The scheduler is the only writer of
  `.orca/osw/runs/<run_id>/` (`run.json`, `steps/<key>.json`); the watcher
  stays the only writer of agent records. Human verbs (`accept`, `reject`,
  `retry`, `stop`) drop a request file that the next tick consumes, the same
  side-file pattern as `.orca/osw/agents/<id>.stop` — never a second writer.
- **Dispatch.** Every step is still one ordinary `new`. `new` needs a caller
  terminal (its completion notification has nowhere to go otherwise), so the
  scheduler passes `--caller-terminal` when the run was started inside a pane
  and `--no-notify` when it was not: a workflow runs headless from a plain
  shell or CI without changing what a supervised agent does. Starts are spaced
  by `OSW_WORKFLOW_STAGGER_SECONDS` (default 2, `0` disables): a provider
  harness opens shared state at boot — pi's context store is one SQLite file
  for every session — and several booting in the same second collide with
  `database is locked`, which kills the agent before its first turn.
- **Guards.** `max_agents` (default 64) refuses rather than truncates; above
  32 agents the run warns; `approval: "human"` refuses to start until the
  operator passes `--yes`. `--dry-run` prints the phases and the agent count
  and creates nothing.

## OSW Provider Policy

Confirmed choice for this project. An explicit human choice in the current
request overrides it; a task type with no row stays undecided (run
`models <provider>` and ask once).

| Task type | Provider | Model | Thinking |
|---|---|---|---|
| implementation | pi | magpie/workbuddy/deepseek-v4.1-flash | high |
| batch fan-out (`osw super`) | pi | magpie/workbuddy/deepseek-v4.1-flash | high |

## Host Seam and Cross-Host Operation

Cross-host operation adds one dimension, not a second code path. Keep these
rules when changing it:

- **One seam.** Every Orca invocation and every read, write or existence check
  on an agent-visible file goes through the `Host` value in
  `skill/osw/hosts.py` (`exec`, `read_file`, `write_file`, `exists`). No call
  site builds an `ssh`/`sftp` argument list, quotes a remote path itself, or
  touches a remote file directly. A local host resolves to the
  subprocess/filesystem implementation, so a single-machine install stays
  byte-identical and the existing suite must keep passing unchanged.
- **Two planes.** Orca pairing (`--environment <selector>`) is the dispatch
  plane, but it only creates the pane: a remote agent is a classic-lane
  terminal (`terminal create --environment <host> --worktree <selector>
  --command "<absolute provider path> <autonomy flags>"`) whose prompt,
  readiness, turn and handoff the local watcher owns. A remote agent has no
  Dispatch id, and the Run stays local and is unused remotely. SSH is the data
  plane; the remote handoff is copied into the local handoffs directory and
  judged there. Terminal scraping is never a file channel — completion
  evidence keeps its local strength.
- **No supervised lane on a remote host.** Do not re-enable
  `orchestration worker-start --on` without a new measurement. On Orca 1.4.248
  a paired server is `host_indeterminate`: `worker-start --on` held at
  `agent_readiness` for 240s while the remote pane sat at its composer,
  `terminal wait --for tui-idle` timed out on that same idle pane, and only
  `terminal send`/`read` and `worktree ps` answered. A provider's first-run
  theme/security/bypass dialogs are the operator's to clear once in that pane;
  directory trust is pre-recorded over SSH, onboarding is not.
- **Registry and references.** Host records live in `.orca/osw/hosts.json`
  beside `state.json`, never inside it. Agent records keep the flat
  `.orca/osw/agents/` layout and gain a `host` field, so ids stay unique on
  this machine; `<host>/<agent_id>` is addressing, not storage, and `local/`
  is optional. An unregistered prefix is refused (`unknown_host`) with the
  registered names listed.
- **Closed taxonomy.** `host_unreachable`, `host_permission_denied`,
  `remote_no_project`, `remote_no_worktree`, `provider_missing`,
  `remote_session_unknown`, `unknown_host`, `host_registry_invalid`, plus
  `remote_read_failed`, `remote_write_failed` and `remote_command_failed`
  raised inside the seam. Call sites surface these codes; they never invent
  their own.
- **Doctor is the only probe.** `host doctor` walks connection → workspace →
  worktree → provider, stops at the first failing rung, and persists the
  discovered Orca/provider paths plus the remote home and platform on the host
  record. Remote launch and remote revive start argv with that recorded
  provider path, never the bare name. No other command path probes before
  acting.
- **Single writer.** Only the watcher writes an agent record, locally and
  remotely. `list`, `status`, `use` and `revive` read records and may mark a
  dead watcher's agent `lost`.
- **Nothing runs on the far side.** No OSW install, no resident process, no
  helper script, no clone or copy of a repository. The watcher, the logs, the
  blocker policy and the update lock stay where the CLI runs; the lock key
  carries the host (`update-<provider>--<host>.lock`) so different hosts never
  serialise against each other.
- **Pairing is optional.** Orca pairing only views a remote terminal in the
  UI. Losing it must never change how OSW reads files, judges completion or
  reports.
- **POSIX remotes only.** A Windows remote is best effort; do not build
  features that require one.

## Testing Guidelines

Tests use `pytest` and `typer.testing.CliRunner`. Add or update tests before
changing behavior. Prefer focused tests near the affected module, such as
`tests/test_providers.py` for provider command formatting and `tests/test_cli.py`
for user-visible CLI behavior. Host-seam changes (argv construction, exit-status
mapping, the doctor ladder) belong in `tests/test_hosts.py`, driven through the
single patched process runner. Mock Orca calls with `AsyncMock`; do not require
a live Orca instance for unit tests.

## Commit & Pull Request Guidelines

Follow the existing commit style: `feat:`, `fix:`, `refactor:`, `docs:`,
`test:`, and `chore:`. Keep commits scoped and describe the behavioral change.
Pull requests should include a short summary, test results such as `pytest -q`,
and any CLI examples affected by the change. Link related issues when available.

## Security & Configuration Tips

Provider launch commands may include autonomy flags such as Claude permission
bypass or Codex sandbox bypass. Before launch, `osw new` also pre-records
workspace trust for claude/codex/kimi (via `ensure_workspace_trust` in
`skill/osw/providers.py`) so the TUI skips its interactive "trust this
directory?" prompt; grok instead launches with `--always-approve --trust`,
where `--trust` grants folder trust for the workspace. DSH gets neither: its
launch (`dsh-tui .`) has no trust or approval flag, so its permission preset
(`/permission`) and first-run setup stay the operator's choice, and OSW
records no autonomy for it. Use them only in trusted, externally controlled
workspaces.

Remote dispatch keeps that posture unchanged: the same autonomy flags, and
workspace trust written on the host that will run the agent through the same
seam (a failed remote write degrades to the interactive prompt, never to a
failed launch). A provider that has never run for that account still shows its
own first-run theme/security/bypass dialogs, which no readiness probe can
clear — clear them once in that pane. A remote host needs only `sshd`
reachable from here with an authorised key, a workspace already registered in
Orca there, and the provider CLI: never install OSW, a resident process or a
helper script on it, and never clone or copy a repository there from OSW.

Do not commit generated `.orca/` runtime state, local logs, reports,
or handoff files. Never launch a nested agent process inside a managed pane
without clearing the inherited `ORCA_*` environment variables.

## Skill Installation Requirements

Install or update OSW with `python install.py`, which physically copies the
complete `skill/` directory into each tool's own skill directory. Each
destination must be an independent, ordinary directory containing ordinary
copied files.

- Codex destination: `C:\Users\shiki\.codex\skills\osw\`
- Agents destination: `C:\Users\shiki\.agents\skills\osw\`
- Claude destination: `C:\Users\shiki\.claude\skills\osw\`
- A skill directory cannot define a slash command, so `install.py` also drops
  `skill/commands/workflow.md` into the folder each harness searches for one:
  `~/.claude/commands/osw-workflow.md` and
  `~/.codex/prompts/osw-workflow.md` (`/osw-workflow`). The agents (omp) skill
  needs no command file: its skill itself is the entry point.
- Never use junctions, symbolic links, hard links, directory links, reparse
  points, or any other link-like mechanism for either the directory or its
  files.
- Never configure an installed skill to execute from or resolve back to
  this repository's `skill/` directory.
- Validate after every update that all destinations are real directories,
  contain independent physical copies, and still work if the repository path
  is unavailable (`install.py` performs these checks automatically).
