# OSW — Orca Agent Supervisor

[中文版](README_zh.md)

A lightweight Python tool for managing Orca-backed agent sessions from the current directory. OSW starts or adopts agent terminals, launches detached watchers, detects task completion through Orca's native agent state (with idle-signal fallback), forces a handoff pass, and reports the resulting handoff file back to the caller.

## Features

- **Directory-scoped** — each working directory is an independent management scope, even across git worktrees
- **Detached watchers** — each managed agent gets a watcher process that monitors completion without blocking the CLI
- **Automatic handoff** — detects task completion, sends a fixed wrap-up instruction with a pre-allocated handoff path, verifies the handoff markdown, notifies the caller terminal
- **omp-aware readiness and completion** — omp never goes output-idle and Orca emits no `tui-idle` for it, so OSW reads its terminal title (a spinner in front means a turn is running) for both the ready gate and the turn-done verdict
- **Takeover and recovery** — `enroll` records agent terminals OSW did not start, `guard` makes you their notification target and follows a running turn, `revive` reopens provider sessions after an Orca or PC restart, and `stop` interrupts a turn
- **Cross-host supervision** — register a machine once, dispatch with `--on <host>`, and collect the handoff locally: Orca pairing is the dispatch plane, SSH the data plane, and the remote side needs no OSW install
- **Compatibility guard** — probes the live Orca JSON contract and recovers runtime-scoped terminal handles after an Orca restart
- **Minimal dependencies** — only `anyio`, `typer`, and optionally `rich` for colored logs
  (`typesafe-sdk` is an optional extra used only when the Jev mode is not `off`)

## Requirements

- Python 3.10+
- [Orca](https://orca.dev) running and `orca` CLI on PATH
- `anyio` and `typer`:

```
python -m pip install anyio typer rich
```

For remote agents only: `ssh` and `sftp` on this machine, `sshd` reachable on
the other machine, and this machine's key authorised for its account.

## Quick Start

See [QUICK.md](QUICK.md) for a step-by-step walkthrough.

```bash
python skill/osw.py doctor
python skill/osw.py init
python skill/osw.py models pi
python skill/osw.py models omp

python skill/osw.py new claude --prefix code --model sonnet --thinking high "Fix the failing tests"
python skill/osw.py new codex --prefix research --model gpt-5 --thinking medium "Refactor the provider layer"
python skill/osw.py list
python skill/osw.py status
```

## Commands

| Command | Description |
|---|---|
| `doctor [--json]` | Check runtime readiness and the live Orca response contract without writing OSW state |
| `init` | Initialize `.orca/osw/` state directory |
| `models <provider> [--json]` | Inspect provider model options without writing state |
| `host add <name> --ssh <dest> --worktree <selector> [--environment <sel>] [--root <path>]` | Register a machine; nothing is probed or written on the far side |
| `host list [--json]` | Registered hosts with a one-attempt reachability |
| `host rm <name>` | Remove a host record and name the agents still referencing it |
| `host doctor <host> [--provider <provider>] [--json]` | Probe connection → workspace → worktree → provider, stopping at the first failure |
| `new <provider> [--on <host>] [--prefix <prefix>] [--model <model>] [--thinking <value>] "<prompt>"` | Create a new agent terminal with a task |
| `use <agent-id-or-terminal> [--on <host>] [--prefix <prefix>] "<prompt>"` | Adopt an existing Orca terminal or continue from an OSW agent id |
| `super --provider <p> [--model <m>] [--thinking <t>] [--prefix <p>] [--max-parallel <n>] [--caller-terminal <h>] [--no-notify] [--mailbox\|--no-mailbox] (--task "<text>" ... \| --plan <file>) [--json] [--dry-run]` | Dispatch many tasks as `new` agents, at most N subprocesses in flight |
| `enroll [--on <host>]` | Record this worktree's agent terminals that OSW did not start (`enrolled`, no watcher) |
| `guard [<agent_id>...] [--caller-terminal <handle>]` | Become the guardian (notification target) of agents; follow a running turn |
| `revive [<agent_id> [--session <id>]]` | List resumable agents, or reopen one agent's provider session in a new terminal |
| `stop <agent_id>` | Interrupt an agent's current turn |
| `del <agent_id> [--close]` | Remove an agent from management |
| `sweep [--force] [--json]` | Close the terminals of finished agents; records and sessions stay |
| `list [--all] [--json]` | List managed agents |
| `logs [--agent <agent_id>] [--tail N]` | Show OSW logs and events |
| `status [--all]` | Show OSW state and agent counts |
| `mailbox start [--json]` | Start the local Mailbox daemon (control plane only); a second `start` is a success |
| `mailbox stop [--json]` | Stop the daemon and wait for it to exit |
| `mailbox status [--json]` | Queue depths, oldest age, spool lag, doorbell state and dead letters |
| `mailbox send --kind <kind> --text "<text>" [--to-role <role>] [--ticket <id>] [--thread-key <key>] [--json]` | Write one envelope to the spool (default roles human → router) |
| `mailbox wait [--as router\|human] [--timeout <secs>] [--max <n>] [--json]` | Lease and ack that role's decision cards; `--timeout 0` is one scan |
| `mailbox show <message-id> [--json]` | Envelope, card and ticket for one message |
| `mailbox trace <message-id-or-ticket> [--json]` | Message → ticket → decisions → effects → outbox |
| `mailbox decide <message-id> --action <action> --reason "<reason>" [--ticket <id>] [--payload-json <json>] [--json]` | Validate and commit a Decision; prints its `decision_id` |
| `mailbox tickets [--json]`, `mailbox ticket <id> [--json]` | The derived ticket index, and one ticket |
| `jev status [--check]` | Show the global Jev switch, key/SDK/endpoint/proxy state; `--check` live-probes the endpoint |
| — | **The mode is the global switch**: the machine-wide `OSW_JEV_MODE` environment variable (`enforce\|shadow\|off`; unset = `off`) is the only control — there is no per-project Jev config |

### Options

- `new` accepts `claude`, `codex`, `pi`, `omp`, `kimi`, `grok`, or `dsh` as the provider.
- `--prefix` gives semantic agent ids such as `research_001`, `code_001`, `test_001`, `debug_001`, `review_001`, or `misc_001`; omit it for `agent_NNN`.
- `use <agent-id>` reuses that exact agent id and terminal context; `--prefix` is only for `new` or `use <terminal-handle>`.
- `--on <host>` on `new`, `use` and `enroll` targets a registered host instead of this machine.
- Agent references are `<host>/<agent_id>`: `fnnas/code_001` names an agent on that host, while `code_001` and `local/code_001` both name one here. An unregistered prefix is refused and the message lists the registered hosts — a typo never dispatches work elsewhere.
- `--model` is passed through unchanged.
- `--thinking` is translated per provider: `claude --effort`, `codex -c model_reasoning_effort=...`, `pi`/`omp --thinking`, and `grok --reasoning-effort`; `kimi` rejects it because effort lives in its model configuration, and `dsh` rejects it (and `--model`) because its TUI entry has neither flag.
- `models omp` reads `omp models --json`; pass one of its `provider/model` selectors to `new omp --model` (forwarded to the TUI as `--model`).
- `new omp` launches `omp --auto-approve` in an Orca terminal like every other provider: the readiness gate accepts the pane once its title has no spinner in front, the watcher sends the task prompt as its own turn, and the turn counts as done when Orca reports the pane `done` while the title stays idle. Neither signal can come from output silence — an omp status bar repaints continuously — and the supervised `orca orchestration worker-start --agent omp` lane is not used: Orca 1.4.246 waits for its own `tui-idle` readiness there, which an omp pane never emits (measured: failed at `agent_readiness` after both a 120s and a 240s budget, with the TUI sitting at its composer).
- `new dsh` launches the DeepSeek Harness TUI with `dsh-tui .` — the entry Orca itself starts and tracks as agent `dsh`. DSH carries no model, effort or approval flag at launch: those live in DSH settings and change inside the TUI (`/model`, `/permission`), so `--model`/`--thinking` are refused rather than dropped, and OSW adds no autonomy flag. Readiness holds while DSH's own boot notice (`正在启动 DSH…` / `Starting DSH…`) is on screen: the launchpad draws its composer before the harness is up and drops a prompt sent then. Orca identifies the pane from its title (`✦ 🐋 <session>`) and takes working/done from DSH-TUI's Program Status Protocol reports; neither names a tool, so the report carries `tools_observed: null` instead of claiming none. With DSH's Orca hook bridge installed (`@deepseek-ai/dsh-hooks-claude-code` plus its runtime peers in the `dsh-tui` profile), OSW records the DSH session id during the ready wait — before the first turn's Program Status reports take over the pane record; without the bridge, `revive` needs `--session <id>`.
- OSW adds the provider's non-blocking autonomy flag by default: `claude --dangerously-skip-permissions`, `codex --dangerously-bypass-approvals-and-sandbox`, `pi --approve`, `omp --auto-approve`, `kimi --yolo`, or `grok --always-approve --trust`.
- `new` and `use` accept `--caller-terminal <handle>` to receive completion reports on a parent terminal. Run from an Orca chat (a structured agent session, which has no terminal handle), the caller is the injected `ORCA_AGENT_SESSION_ID`, recorded as `caller_session: orca_session_id:<id>`; the watcher delivers through `orca orchestration send`, which Orca injects into that chat as a user turn.
- `del --close` also asks Orca to close the terminal.
- **A `done` turn closes its own terminal.** When a turn settles `done` the watcher applies the reclamation policy and closes the pane it was given: the completion line then carries `cleanup=auto` and the record gains `pane_reclaim: "auto"`. Records and `session_id` stay, so `revive <agent_id>` resumes the session in a fresh terminal. `osw sweep` is still the backstop (and the only way to close a failed one, with `--force`); it prints what it kept and why. Panes never closed automatically: adopted ones (`use`/`enroll`), supervised dispatch terminals (Orca owns them; `del <id> --close` releases them), failed turns (`error`/`lost`), human-reopened turns (`guard`/`revive`), and a `done` turn with `OSW_KEEP_TERMINAL=1` in its environment.
- `list --json` outputs raw JSON.

## Orca Compatibility

Run `python skill/osw.py doctor` after installing or upgrading Orca. It checks
the response shapes and fields OSW consumes from `status`, `worktree current`,
`terminal list`, `terminal show`, and `worktree ps`. A contract mismatch exits
non-zero; `--json` returns the typed error code `orca_contract_mismatch` for
automation. Checks that need a live terminal or recognized agent are reported
as `unverified` when none exists.

OSW honors `ORCA_CLI_COMMAND` (including arguments), selects `orca-dev` when
`ORCA_DEV_REPO_ROOT` is set, and uses `orca-ide` on Linux when running outside
an Orca-managed terminal. Otherwise it resolves `orca` from PATH.

Orca terminal handles are runtime-scoped. OSW persists the pane identity
(`tabId:leafId`) when creating or adopting a terminal and uses it to rebind a
stale handle after an Orca restart.

## Cross-Host Operation

OSW can supervise an agent whose terminal lives on another machine. A host is
registered once and then addressed by name.

### Two planes, one seam

- **Dispatch plane** — Orca's pairing connection (`--environment <selector>`).
  It creates the remote terminal and nothing else:
  `terminal create --environment <host> --worktree <selector> --command
  "<absolute provider path> <autonomy flags>"`. Readiness, prompt injection,
  the turn lifecycle and the handoff stay with the local watcher — the same
  classic lane a local agent uses. A remote agent has **no Dispatch id**, and
  the Run stays local and is unused remotely.
- **Data plane** — SSH: `ssh` for commands, `sftp` for file bytes (read,
  write-then-rename, existence). No terminal is scraped to move bytes, so a
  remote handoff is read at the same fidelity as a local one.

**Why not Orca's supervised lane (measured 2026-11-04, Orca 1.4.248).** On a
paired server the readiness verdict is `host_indeterminate`:
`orchestration worker-start --on <host>` held at `agent_readiness` for 240s
while the remote pane sat at its composer, and
`terminal wait --for tui-idle` timed out on that same idle pane. `terminal
send`, `terminal read` and `worktree ps` all answered. There is no ready
signal on the far side to wait for, so the pane must be created and driven
from here. Nobody re-enables the supervised lane for a remote host without a
new measurement that shows a readiness signal firing there.

Both planes, the host registry, agent-reference parsing and the error mapping
live in `skill/osw/hosts.py`. The seam rule that keeps this from rotting:
**nothing outside that module talks to Orca about another machine or touches a
file on one.** Local calls stay byte-identical to a single-machine install, so
the seam is invisible until a host is named.

### Prerequisites on the remote host

- `sshd` reachable from this machine, and this machine's key authorised for
  that account. A refused key surfaces as `host_permission_denied`; authorise
  it (`ssh-copy-id`) rather than debugging OSW.
- A workspace already registered in Orca on that machine, plus the worktree
  selector Orca uses for it — `path:/srv/repo`, or a `name:`/`branch:`/`id:`
  selector.
- The provider CLI installed for the SSH account. Doctor records its absolute
  path, because a non-interactive SSH `PATH` is minimal, and remote launches
  use that path.
- `ssh` and `sftp` on *this* machine's `PATH`.
- A provider that has never run for that account must be started once by hand
  in that pane: it shows its first-run theme/security/bypass dialogs, and no
  readiness probe can clear them. Directory trust is different — `new`
  pre-records it over SSH, so the "trust this directory?" prompt does not
  appear; onboarding does not.

Deliberately **not** needed there: no OSW install, no resident process, no
helper script, no clone or copy of the repository. OSW provisions nothing on
the far side — Orca creates the terminal from a workspace that already exists.

Orca pairing is optional for operation: it is how dispatch is addressed and how
a remote terminal can be viewed in the Orca UI, but no file read or write
depends on it. Losing a pairing costs you dispatch to that host, not OSW's
ability to read a handoff or judge completion.

### Hosts

```bash
python skill/osw.py host add fnnas --ssh fnnas --worktree path:/srv/repo
python skill/osw.py host list
python skill/osw.py host doctor fnnas
python skill/osw.py host rm fnnas
```

`host add` writes one record to `.orca/osw/hosts.json` (beside `state.json`,
not inside it): name, SSH destination, Orca environment selector (defaults to
the name), workspace selector, and an optional `--root` for path joins. It
probes nothing and writes nothing on the far side. Registering an existing
name is refused with the existing record; `local` cannot be registered — it is
this machine.

`host list` prints name, SSH destination, environment, worktree and a
one-attempt reachability: `reachable`, `unreachable` or `permission_denied`. A
dead host is a row, never an error and never an omitted row.

`host rm` removes the record and names any agent records that still reference
it.

### Doctor

`host doctor <host> [--provider <provider>] [--json]` walks four rungs in order
and stops at the first failure, so nobody is told to fix a provider on a host
that has no workspace. It also accepts an unregistered destination
(`host doctor myhost --ssh user@myhost --worktree path:/srv/repo`), which is
usable before `host add`.

| Rung | Check | Fails with |
|---|---|---|
| `connection` | one SSH command (`true`) answers | `host_unreachable`, `host_permission_denied` |
| `workspace` | Orca on that host has a registered workspace | `remote_no_project` |
| `worktree` | a worktree matches the recorded selector | `remote_no_worktree` |
| `provider` | one `command -v` for the provider | `provider_missing` |

Each failure prints its code and one next step. The `provider` rung records the
absolute paths of the Orca CLI and of the provider CLI on the host record, and
caches the remote home and platform, so later calls never depend on ambient
configuration. Rungs after a failure are never probed, and no probe runs on any
command path other than doctor.

### Error codes

Failures from the host seam are a closed set. Scripts and agents branch on the
code, never on prose.

| Code | Meaning |
|---|---|
| `host_unreachable` | ssh/sftp could not reach the host, timed out, or `ssh` is missing here |
| `host_permission_denied` | this machine's SSH key is not authorised for that account |
| `remote_no_project` | Orca has no workspace registered on that host |
| `remote_no_worktree` | no worktree matches the recorded selector |
| `provider_missing` | the provider CLI is not installed for the SSH account |
| `remote_session_unknown` | the hook cache a resume would need could not be read there, or holds no entry for that pane |
| `unknown_host` | the name is not registered; the message lists the registered ones |
| `host_registry_invalid` | `.orca/osw/hosts.json` is unreadable or of an unknown format |

Failures inside the seam that are neither transport nor probe related surface
as `remote_read_failed`, `remote_write_failed` or `remote_command_failed`.

### Register → doctor → dispatch → collect → clean up

```bash
# 1. register the machine once
python skill/osw.py host add fnnas --ssh fnnas --worktree path:/srv/repo

# 2. prove the rungs, in order
python skill/osw.py host doctor fnnas --provider claude

# 3. dispatch: create a pane on that host, driven from here
python skill/osw.py new --on fnnas claude --prefix code "Fix the failing tests"

# 4. watch the fleet from here
python skill/osw.py list --all
python skill/osw.py status --all

# 5. the handoff is a local file once the turn finishes
cat .orca/osw/handoffs/code_001_<ts>.md

# 6. clean up
python skill/osw.py del fnnas/code_001 --close
```

What each step guarantees:

- **Dispatch.** `new --on <host>` needs the host's workspace selector (recorded
  at `host add`, confirmed by doctor) and fails before anything is created when
  it is missing or unknown — it never guesses a directory on the far side. It
  then creates an Orca terminal on that host with `terminal create
  --environment <host> --worktree <selector> --command "<absolute provider
  path> <the same autonomy flags a local launch gets>"`, records the agent
  locally with its `host`, and starts the same local watcher a local agent
  gets. The watcher — not Orca — sends the prompt, reads the pane and decides
  the turn ended. Workspace trust is pre-recorded there over SSH, and a failed
  trust write degrades to the interactive prompt, which one Enter clears. The
  Run and every log stay here, which is why a remote agent appears in no
  Dispatch.
- **Addressing.** The record carries `host`, so later commands know which
  machine to talk to. Agent ids stay `<host>/<agent_id>` (`local/` may be
  omitted), and a prefix that contradicts the record is refused rather than
  silently ignored.
- **Fleet view.** `list --all` shows local and remote agents in one table with
  a `HOST` column (`--json` carries the host on every row); `status --all`
  counts agents and running watchers per host. A host that is currently
  unreachable is shown with an explicit unknown state, not its last known one.
- **Handoff collection.** After settlement the remote pane receives the
  standard wrap-up instruction naming a path on its own machine —
  `<remote home>/.orca/osw/handoffs/<agent>_<ts>.md`, outside the repository, so
  OSW never pollutes code it does not own. The watcher polls existence and byte
  length over SSH until two reads agree, copies the bytes into the local
  `.orca/osw/handoffs/`, and judges the file **there**, in the local handoffs
  directory, with the usual exists-and-size-stable rule. The record keeps
  `remote_handoff_path` and marks `evidence: ssh`. A remote agent that settles
  without a handoff finishes `done` with `handoff_missing`, exactly like a
  local one.
- **Reporting.** The completion report and relayed worker questions reach the
  guardian terminal here, on the existing notification path, unchanged.
- **Clean-up.** `del <host>/<agent_id> --close` stops the watcher, asks Orca to
  close that host's terminal, and removes the record; an already closed
  terminal is not an error. `host rm` then drops the record.

### Notes and limits

- **Only the watcher writes an agent record.** `list`, `status`, `use` and
  `revive` read records and may mark a dead watcher's agent `lost`; nothing
  else writes one. Behaviour is identical across hosts.
- **Provider path.** A remote `new` launches, and a remote `revive` re-types, the
  absolute provider path recorded by `host doctor`, never the bare name,
  because a paired runtime's shell need not resolve the provider from `PATH`.
  Local launches and local resumes keep the bare name, byte-identical to
  before; a host whose doctor never recorded a path falls back to the name.
- **Remote revive needs a session id.** It reads that host's hook cache
  (`~/.config/orca/agent-hooks/last-status.json`) over SSH. A host whose Orca
  never records hook status has no id for any pane: measured on host pi, the
  file exists with `version: 2` and zero entries, so `revive pi/<agent_id>`
  fails with `remote_session_unknown` naming the pane and the cache path. It
  fails rather than resuming a guess (pass `--session <sid>` to supply one),
  and remote revive is not verified end to end.
- **First run is the operator's.** OSW cannot clear a provider's first-run
  theme/security/bypass dialogs on a host where that account never ran it; start
  it once in that pane. Directory trust is written over SSH and needs no
  operator step.
- Host records live in `.orca/osw/hosts.json`; agent records keep their flat
  layout under `.orca/osw/agents/`. `<host>/<agent_id>` is addressing, not
  storage, so ids stay unique on this machine.
- The blocker update lock is keyed per host: `update-<provider>.lock` for this
  machine, `update-<provider>--<host>.lock` for a named one, so two hosts never
  serialise against each other while panes on the same host still do.
- POSIX remotes are the supported target; a Windows remote is best effort.
- `guard` stays local — the guardian is a terminal or chat session on this machine.

## Control Plane (Mailbox)

OSW has an optional local control plane for coordination: a durable mailbox, a
deterministic fast path, a Router role that makes decisions, and a Dispatcher
that applies them through the paths the CLI already uses. Execution does not
move — Orca owns Run/Task/DAG/Dispatch, the watchers own turns, providers own
the code.

```bash
python skill/osw.py mailbox start
python skill/osw.py mailbox send --kind request --text "review both PRs side by side" --thread-key chat:main
python skill/osw.py mailbox wait --as router --timeout 0
python skill/osw.py mailbox decide msg_... --action accept --ticket code_001 \
    --reason "explicit human request" --payload-json '{"provider":"claude","model":"sonnet"}'
python skill/osw.py mailbox status --json
python skill/osw.py mailbox stop
```

- **Lifecycle** — `start` spawns the daemon detached and waits for the lock;
  `status` reports `running`, `pid`, queue depths, `spool_lag`, doorbell state
  and dead letters; `stop` writes the stop sidecar and waits. A second `start`
  is a success, not an error.
- **Producers only write to the spool** (`.orca/osw/mailbox/spool/`); the
  daemon is the only writer of the SQLite store. Ingest is at-least-once with
  idempotent application: the dedupe key (an ask's Orca message id, a report's
  `report_key`, a decision's `decision_id`) absorbs repeats, so one worker,
  one answer and one stop happen per Ticket.
- **Cards and the doorbell** — `wait` returns cards of at most 1 KiB and
  `decide` requires a reason. When a card is waiting, the daemon sends one line
  through the same notify channel the watcher uses:
  `# [osw] mail pending=<n> run="osw mailbox wait --as router --timeout 0"` — an
  on-demand Router session gets a count and a command, nothing else. On lease
  expiry it rings once more, then notifies a human.
- **Ticket and Mandate** — a Ticket is the durable case file; the Mandate is a
  closed-schema, human-owned `skill/osw/mandate.json` read from committed HEAD.
  The Router may narrow it when opening a Ticket, never widen it, and the
  Dispatcher records a violation as `mandate_denied` and refuses it.
- **Answer authority** — the only Router-authored worker input is the fixed
  single-line template `# [osw] answer <decision_id>: <text>`, at most 4 KiB,
  sent after the Dispatcher rechecks that the ask is still open. Login,
  permission, delete and payment screens are escalated to a human; blocker
  handling stays with the watcher whitelist.
- **Direct mode is a guarantee** — with the mailbox never started, or stopped,
  `new`, `use`, `status`, `list`, `revive`, `stop` and the current notification
  path behave exactly as before. The mailbox is a second line of defense beside
  `notified_asks`; it never replaces the watcher, the handoff check or Orca's
  own state. If the daemon dies, the spool keeps what producers wrote, a restart
  ingests it, and `status` reports `mailbox_down`.
- **Stays local** — one mailbox per project root under `.orca/osw/mailbox/`.
  Remote events are written to the local spool by the local watcher through the
  host seam; nothing runs on the remote host.

## Super (One-Command Fan-Out)

`super` is the fan-out front door for `new`: one command fills the worktree
with independent workers instead of one dispatch per task. Tasks are
positional, and repeated `--task` values are appended after them.

```bash
python skill/osw.py super "Fix the failing tests" "Audit the provider layer"
```

Only a provider is required, and even that can come from the project. Tailor
the run with flags:

```bash
python skill/osw.py super "Fix the failing tests" "Audit the provider layer" \
    --provider pi --model <m> --thinking high \
    --prefix code --max-parallel 4 --caller-terminal term_xxx
```

Or put the same thing in a plan file and let each task pick its own defaults:

```json
{
  "provider": "pi",
  "model": "<m>",
  "thinking": "high",
  "prefix": "code",
  "max_parallel": 4,
  "tasks": [
    "Fix the failing tests",
    {"task": "Review the provider layer", "prefix": "review", "provider": "codex"}
  ]
}
```

```bash
python skill/osw.py super --plan plan.json --json
```

- **Zero-flag defaults** — when `provider`, `model`, `thinking`, `prefix` or
  `max_parallel` is not given by a flag or the plan, `super` resolves it from
  the project, in this order: the `super` object in `.orca/osw/config.json`
  (`{"super": {provider, model, thinking, prefix, max_parallel}}`; absent or
  malformed is ignored), then `OSW_PROVIDER` / `OSW_MODEL` / `OSW_THINKING` /
  `OSW_PREFIX` / `OSW_MAX_PARALLEL`, then the `## OSW Provider Policy` table
  row whose first cell contains `batch fan-out`. Prefix finally defaults to
  `code`, `max_parallel` to 32. Only `provider` is required: with none
  anywhere, the command names the three places to set it and dispatches
  nothing.
- **Flags are defaults** — `--provider`, `--model`, `--thinking`, `--prefix`,
  `--caller-terminal` and `--max-parallel` apply to every task and win over
  everything else. The plan object may carry `provider`, `model`, `thinking`,
  `prefix` and `max_parallel` as the same defaults (a flag you pass wins over
  the plan, which wins over the project), and a task entry may be a plain
  string or an object overriding `task`, `prefix`, `provider`, `model`,
  `thinking` and `caller_terminal` for itself. `--plan` cannot be combined
  with tasks, positional or flag.
- **Mailbox first, default on** — with the mailbox enabled, `super` runs
  `mailbox start --json` before dispatching. If it fails, the command aborts
  with that typed error text and dispatches nothing; `--no-mailbox` skips it.
- **Each task is a plain `new`** — one `[osw.py new <provider> "<task>"
  --prefix ... --model ... --thinking ... --caller-terminal ...]` subprocess per
  task, argv-only, in the project root. Empty flags are omitted, and `--no-notify`
  is forwarded only when no caller terminal is known. `super` writes no state of
  its own: records, watchers, reports and handoffs are the ones `new` produced.
- **Isolation, not all-or-nothing** — a failing or timed-out dispatch is
  reported in `errors` and never aborts the rest; the command exits 1 when any
  dispatch failed. At most `max_parallel` subprocesses run at once (default 32,
  clamped to 1..32), each with a 300s timeout.
- **Output** — `--json` prints `{mailbox: {running, pid} | null, requested,
  started, agents: [{index, task, agent_id, terminal}], errors: [{index, task,
  error}]}`; otherwise one line per agent plus a `super: requested=... started=...
  errors=...` summary. `--dry-run` prints the mailbox and per-task argv lists and
  executes nothing, so the resolved provider, model and thinking are visible
  before anything runs.

## Workflow (Phased Runs)

`super` dispatches one task list once. `workflow` reads a phase plan, dispatches
each phase, waits at the barrier, and decides what the next phase's agents are
from what the last one produced — so a run that dies halfway can be finished
instead of restarted.

```bash
python skill/osw.py workflow go "audit every HTTP route for missing auth"  # plan + start, then return
python skill/osw.py workflow plan "audit every HTTP route for missing auth"   # one agent drafts the plan
python skill/osw.py workflow run audit-routes --dry-run   # phases and agent count, nothing created
python skill/osw.py workflow run audit-routes             # execute
python skill/osw.py workflow run audit-routes --resume    # only the steps not yet done
python skill/osw.py workflow status --watch               # live table: phases, then the steps that need attention
python skill/osw.py workflow ls ; workflow show <run>
python skill/osw.py workflow accept|reject|retry <run> <step> --reason "..."
python skill/osw.py workflow stop <run>
```

`go` is the one-shot entry an agent calls: it drafts the definition, validates
it, creates the run and starts the scheduler as its own detached process, then
prints `STARTED <run_id> ... pid <n>` and exits. A workflow outlives the tool
call that asked for it, so progress is read from `workflow status <run_id>`
(the archive) and the scheduler's own output lands in
`.orca/osw/runs/<run_id>/scheduler.log`. `/osw workflow "<sentence>"` routes
here.

`plan` writes the definition for you: one ordinary agent drafts
`.orca/osw/workflows/<name>.json` from the sentence, `defs.py` judges it, and
the expanded phases print before anything is dispatched. An invalid draft stays
on disk so one line can be fixed rather than the whole thing; `--name` keeps two
plans apart. Nothing is dispatched until `run`.

The definition is a closed-schema JSON in `.orca/osw/workflows/<name>.json`
(project first, then `~/.orca/osw/workflows/`):

```json
{
  "name": "audit-routes",
  "max_agents": 64,
  "phases": [
    {"id": "scan", "mode": "single", "task": "List every HTTP route handler as JSON under 'json' and write it to {{output}}."},
    {"id": "audit", "mode": "for_each", "from": "scan", "task": "Audit {{item}} for a missing auth check."},
    {"id": "report", "mode": "single", "from": "audit", "task": "Merge the audits and rank them."}
  ]
}
```

- **Barrier** — a phase starts only when every phase named in `from` is terminal.
- **`for_each`** — one agent per item, read from the array in the file
  `{{output}}` names (the scheduler substitutes the run's path), falling back to
  the last fenced ` ```json ` block of the previous phase's handoff. A missing
  field or a missing file is refused (`no_items`) with the path in the message
  instead of guessed: the handoff itself stays the watcher's four-section
  markdown, so structured output gets its own file and OSW does not pretend to
  validate a strong schema.
- **Resume** — `step_key = sha256(phase 0 index 0 prompt 0 schema_id)`. Prompts
  must be pure: `defs.py` rejects `{{now}}`, `{{uuid}}` and `{{random}}` when it
  loads a definition, otherwise a key would never hit again. Unlike Claude Code,
  a finished step is reused on its own key and nothing downstream is re-run.
- **Failure policy** — a lost or errored step is re-dispatched as a new agent
  with the same prompt, up to `--max-attempts` (default 2). Then the step is
  `failed`, and `--policy strict` (default) stops the run at that barrier while
  `--policy partial` carries on and marks the phase `partial`.
- **Verification** — `--verify suspect|all` has a second agent grade the
  handoff, blind and read-only. A run that changes the artifact under review is
  recorded `artifact_mutated` and the verdict is discarded; an unreadable verdict
  becomes `unverifiable` and waits for `workflow accept|reject`.
- **Writers** — the run archive (`.orca/osw/runs/<run_id>/`) has one writer, the
  scheduler; agent records keep their single writer, the watcher. The human verbs
  drop a request file the next tick consumes.
- **Limits** — over `max_agents` is refused, not truncated; above 32 agents the
  run warns; `"approval": "human"` refuses to start without `--yes`.

## Architecture

```
new/use [--on <host>]
  ├── resolve the host (this machine, or a registered one with a known selector)
  ├── create or adopt Orca terminal — remote: `terminal create --environment
  │   <host> --worktree <selector> --command "<abs provider path> <flags>"`
  ├── write .orca/osw/agents/agent_NNN.json (record carries `host`)
  ├── spawn detached watcher(agent_NNN) here — on every host
  └── return immediately
```

The lane below that line is the same on both hosts: the watcher sends the
prompt, reads the pane, judges the turn and collects the handoff — over SSH
when the pane is remote. Orca is not asked to supervise a remote agent.

Every Orca call and every read, write or existence check on an agent-visible
file goes through `skill/osw/hosts.py`; no other module builds an `ssh` or
`sftp` argument list, and none touches a file on another machine directly. On a
single-machine install the seam resolves to the local implementation and
behaviour is byte-identical to before.

OSW owns orchestration only: state initialization, Orca terminal creation/adoption, detached watcher startup, logs, and completion reports. Provider command construction lives in `skill/osw/providers.py`.

### Watcher Lifecycle

```
wait until the pane is ready (`worktree ps` reports it done; a title-state TUI like omp: no spinner in the title; tui-idle and preview markers for untracked CLIs)
 → send the task prompt
 → wait until Orca reports the turn done (`worktree ps`, correlated with the send time; title-state TUIs skip the output-silence guard; idle fallback for untracked CLIs)
 → send the fixed wrap-up instruction with a pre-allocated handoff path
 → wait again, verify .orca/osw/handoffs/<agent>_<ts>.md exists (one retry)
 → write a JSON report and notify the caller terminal
```

The completion notification is sent proactively by the OSW watcher through
`orca terminal send` (a terminal caller) or `orca orchestration send` (a chat
session caller, delivered into that chat as a user turn); it is not an
Orca-native cross-session notification. Automatic delivery therefore requires
a detected caller — a terminal or a chat session — or an explicit
`--caller-terminal` address.

`new` starts the bare provider TUI only — the watcher always sends the task
prompt as its own turn, so a briefly idle TUI at startup can never be
mistaken for a completed task.

### omp: title-based readiness and completion

omp is the one provider whose state is painted into the terminal title and
nowhere else: `⠋ π - <task>` while a turn runs, `OMP - <task>` (or
`OMP > <worktree>`) once it is back at its composer, over a
model/folder/context band. Its status bar also repaints about once a second,
so the pane is never output-silent, and Orca emits no `tui-idle` for it. OSW
therefore reads the title:

- readiness: the pane counts as ready when the title carries no spinner and
  the composer band is drawn (a just-created pane whose title Orca already
  named has no band yet, so it is not mistaken for a ready one);
- completion: a `done` row from `worktree ps`, held across two polls,
  counts once the title has no spinner — the output-silence guard other
  providers use can never pass here.

Both rules are what make the classic send-and-watch lane work for omp.
Orca's supervised lane (`orca orchestration worker-start --agent omp`) is
not used: 1.4.246 waits for its own `tui-idle` readiness there, and an omp
pane parks at its composer with the signal never firing (measured: two runs
failing at `agent_readiness` after 120s and 240s budgets). A `terminal
create` pane for the same TUI does report hook status and `worktree ps` rows
per turn, which is all the watcher needs. Agents launched with that lane by
older OSW versions keep their `dispatch_id` and are still supervised through
`orca orchestration worker-show`; `del --close` releases or stops them
(`worker-release` / `worker-stop`).

## Takeover and Recovery

```bash
python skill/osw.py enroll                       # record unmanaged agent terminals
python skill/osw.py guard                        # become their guardian
python skill/osw.py revive                       # list resumable sessions (JSON)
python skill/osw.py revive code_001              # reopen one after a restart
python skill/osw.py stop code_001                # interrupt the current turn
```

- **`enroll`** scans `terminal list` for this worktree, skips the caller's own terminal and already-recorded panes, takes the provider from `agentIdentity` (else the ps `agentType`), and writes one `enrolled` record per pane with `origin: enroll`. Providers OSW cannot launch get `support: guard_only` (`support_reason: unsupported_provider`). Orca names a DSH pane after its launcher (`dsh-tui`); enroll maps that to provider `dsh`. It prints `{"enrolled": [...], "skipped": [{terminal, reason}]}`.
- **`guard [<id>...]`** makes the executor (`--caller-terminal`, else `ORCA_TERMINAL_HANDLE`, else the injected `ORCA_AGENT_SESSION_ID` of an Orca chat) the guardian; without ids it takes every unguarded `enrolled`/`done`/`error` agent. A live watcher is terminated and restarted to follow the running turn; an unwatched pane that ps reports `working` gets a following watcher; an idle agent only records the guardian. A followed turn is reported without a handoff (`already_done` when it had finished before the watcher took over). `use` keeps notifying the guardian unless `--caller-terminal` is passed.
- **`revive`** without an id prints `{"revivable": [...], "not_revivable": [{agent_id, reason}]}`. `revive <id> [--session <sid>]` needs `support: full`, state `lost`/`done`/`error`/`enrolled`, and no live watcher. It reopens the session (`--session`, else the recorded `session_id`, else the pane's current one) with the provider's resume syntax plus the usual autonomy and model flags, and keeps the agent id. The command is typed into the agent's original pane when that pane is a confirmed empty shell — still listed, no `agentIdentity`, its ps row not `working`, a shell OSW can quote for (pwsh/PowerShell, cmd, POSIX), and Jev `ready_state` judging `shell_prompt` — so `pane_key` stays (`placement: in_place`); otherwise, including `OSW_JEV_MODE=off`, it opens a new terminal (`placement: new_terminal`). The output and the `agent_revived` event carry `placement`. A `lost` agent (interrupted mid-task) is resumed with `resume_turn: true`: the watcher sends `continue`, then runs the normal completion detection and handoff. Otherwise the agent comes back `enrolled`.
- **`stop <id>`** sends Ctrl-C (`terminal send --interrupt`) to claude, codex, kimi, grok, and dsh, and a bare Esc to pi and omp. With a live watcher it writes the sidecar `.orca/osw/agents/<id>.stop`; the watcher ends the turn when ps reports it interrupted or done, or the pane has been silent for 5s, records `error` with `completion_source: interrupted`, skips the handoff, notifies the guardian, and deletes the sidecar. It prints `{"agent_id", "key", "watcher"}`.

New record fields (all optional; old records still load):

| Field | Values |
|---|---|
| `origin` | `new`, `use`, `enroll` |
| `session_id` | Provider session id |
| `guardian` | `{agent_id}` for an OSW agent (its current terminal is read from its record at notify time), `{session}` for a chat-session guardian, else `{terminal, pane_key}` |
| `caller_session` | `orca_session_id:<id>` when the dispatch came from an Orca chat instead of a terminal |
| `support` | `full`, or `guard_only` with `support_reason: unsupported_provider` |
| `follow_turn` | The watcher takes over the running turn; cleared when it finishes |
| `resume_turn` | The watcher's task prompt is `continue`; cleared when it finishes |
| `notified_asks` | Ids of supervised-worker questions already relayed to the guardian |
| `host` | Registered host the agent runs on; absent means this machine |
| `worktree_path` | Worktree path on that host (recorded for remote launches) |
| `remote_handoff_path` | Handoff path on the agent's own host, when it was collected over SSH |
| `evidence` | `ssh` when the handoff was copied from a remote host |

New states: `enrolled` (recorded, no watcher) and `lost` (non-final, watcher process gone — set by `list`/`status`/`use`/`revive`). New completion sources: `already_done` and `interrupted`.

Notifications resolve their target before every send: the guardian if one is recorded, else the caller (terminal, else chat session). A guardian agent whose record cannot be read falls back to the caller (`guardian_unresolved` event); a stale guardian handle is re-resolved once by pane and the send retried once. A chat-session target has no handle to rebind: its notice goes through `orca orchestration send --to orca_session_id:<id>`, and a failed delivery records `notify_failed` with `caller_session`.

Session ids come from Orca's agent-hook cache `%APPDATA%\orca\agent-hooks\last-status.json` (here `C:\Users\shiki\AppData\Roaming\orca\agent-hooks\last-status.json`). It is not a public Orca interface: OSW reads it only when its `version` matches (2), so on a different format the session id is simply empty.

A remote revive reads the same cache on the agent's own host, at `~/.config/orca/agent-hooks/last-status.json`, over SSH. Measured on host pi (2026-11-04): the file exists with `version: 2` but holds **zero entries**, so no pane there has a provider session id and `revive pi/<agent_id>` fails loudly with `remote_session_unknown`, naming the pane and the cache path. A host whose Orca never records hook status cannot revive an agent by itself: OSW fails instead of resuming a guess (pass `--session <sid>` if you know the id). Remote revive has not been verified end to end.

OSW itself only ever types four things into a terminal: the wrap-up instruction `watcher.HANDOFF_TEMPLATE`, the resume prompt `watcher.RESUME_TEMPLATE` (`continue`), whitelisted dialog keys (`osw.blockers.WHITELIST`), and the resume command `revive` types into a confirmed empty shell.

Known limits: a revived `omp` that was idle has no Dispatch — the next `use` creates one. After `stop` on an `omp`, continue with `revive`; reattaching Orca tracking after an Esc interrupt is unverified.

### Blocking screens

When a dialog owns the agent's input, the watcher finds it, names it, and acts only where that is known to be safe:

- **Discovery** — Orca's `agentWait.reason` first (an `agent_prompt_blocked` send error carries it, e.g. `agent-update-prompt`), else the Jev `ready_state` judgment `blocked_dialog`.
- **Kind** — Orca's name, else the Jev question `blocker_kind` (`update_prompt`, `trust_folder`, `auth`, `permission`, `announcement`, `rate_limit`, `crashed_to_shell`, `other`; below 0.8 it is `other`).
- **Mode** (`OSW_JEV_MODE`) — `off` discovers and notifies only; `shadow` records the planned keys (`blocker_shadow_action`) and notifies like `off`; `enforce` presses whitelisted keys.
- **Whitelist** — only codex's update prompt: Enter takes the default "1. Update now". Orca refuses a plain send to that dialog, so OSW sends `terminal send --text "" --enter --interrupt`, the one form observed to get through. Entries need a screen sample in `tests/fixtures/frames/`; the codex sample is a text transcription, not a raw capture.
- **Never automated** — `auth`, `permission`, and anything mentioning deletion or payment.
- **Update lock** — one pane per provider per machine updates at a time (`%LOCALAPPDATA%\osw\locks\update-<provider>.lock`, 15-minute expiry against crashed holders); others notify once and keep waiting.
- **Afterwards** — the screen is judged again; still blocked stops automatic handling and notifies. A refused send is retried after the pane is ready again (up to 5 times); if it stays blocked the task ends with `completion_source: blocked`. codex exits to its shell after updating, so output silence alone is not taken as ready after an action.

The guardian gets one `# [osw] blocked agent=<id> kind=<kind> terminal=<handle> screen=<excerpt>` line per blocker; every step is also a `blocker_*` event.

### Turns without tool activity

The watcher notes whether `worktree ps` reported a tool (`toolName`) while each turn was working; a done row's `toolName` can be the previous turn's and does not count. A task turn (or a followed turn) that ps tracked but that never showed a tool is reported with `tools_observed: false` and `warning=no_tool_activity` in the `task-finished` line. It stays `done` and is not resent; the agent may have answered in text only, so check the handoff and workspace. `tools_observed` is `true` when a tool was seen and `null` when ps never tracked the pane.

If `worktree ps` shows the agent's prompt starting partway into what was sent, the head of the prompt was lost on the way in. The watcher sends `# [osw] prompt-truncated ... received_from=<n>` right away, and the report gets `prompt_truncated_at` and `warning=prompt_truncated`. Nothing is resent; dispatch the task again. Claude prompts are sent as a bracketed paste, because Claude Code on Windows otherwise keeps only the text after char 2048.

### Worker questions

A supervised worker's unanswered orchestration messages of type `question` (from `orchestration ask`) and `escalation` reach the guardian once per message id as `# [osw] ask agent=<id> message=<msg-id> reply="orca orchestration reply --id <msg-id> --body <text>" text=<question>`; relayed ids are kept in `notified_asks`. On Windows, `orca.cmd` refuses `orchestration reply`; use the Orca installation's `resources\bin\orca.exe` for it.

## Directory Layout

### Project

```
orca-osw/
  skill/
    SKILL.md          # skill definition
    references/       # orchestration and operations guides
    osw.py            # entry point
    osw/
      cli.py          # typer CLI commands
      deps.py         # dependency checker
      log.py            # structured events and file logging
      hosts.py          # the host seam: local/SSH execution, host registry, refs, doctor
      orca_cli.py       # async Orca CLI wrapper
      process.py      # hidden subprocess helpers (Windows)
      providers.py    # provider commands and model discovery
      sessions.py     # provider session ids from Orca's agent-hook cache
      blockers.py     # blocking-screen discovery, whitelist, update lock
      fastpath.py     # deterministic classification and bookkeeping for ingest
      mandate.py      # closed-schema Ticket limits (skill/osw/mandate.json)
      dispatcher.py   # the only applier of Decisions
      mailbox_cli.py  # mailbox verbs: start/stop/status/send/wait/decide/tickets
      mailbox/        # control plane: envelope, store, spool, adapters, delivery, daemon
      state.py        # state model and file I/O
      watcher.py      # detached completion watcher
  tests/              # unit and integration tests
  conftest.py         # sys.path setup for tests
```

### Runtime State

Created in the working directory where you run `init`:

```
<cwd>/
  .orca/
    osw/
      state.json      # OSW project state
      hosts.json      # registered machines (created by `host add`)
      agents/          # one JSON record per managed agent (+ <id>.stop requests)
      handoffs/        # worker handoff markdown files
      mailbox/         # control plane: spool/, mailbox.db, daemon.lock, status.json
      logs/
      reports/
```

## State Model

```json
{
  "version": 3,
  "project_root": "D:\\project"
}
```

Runtime agent records live under `.orca/osw/agents/`. Model discovery is read-only; `models <provider>` never writes `state.json`.

## Testing

```bash
python -m pip install pytest
python -m pytest -v
```

The suite covers every module and uses a mocked Orca CLI for integration tests.

## License

MIT
