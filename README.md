# OSW — Orca Agent Supervisor

[中文版](README_zh.md)

A lightweight Python tool for managing Orca-backed agent sessions from the current directory. OSW starts or adopts agent terminals, launches detached watchers, detects task completion through Orca's native agent state (with idle-signal fallback), forces a handoff pass, and reports the resulting handoff file back to the caller.

## Features

- **Directory-scoped** — each working directory is an independent management scope, even across git worktrees
- **Detached watchers** — each managed agent gets a watcher process that monitors completion without blocking the CLI
- **Automatic handoff** — detects task completion, sends a fixed wrap-up instruction with a pre-allocated handoff path, verifies the handoff markdown, notifies the caller terminal
- **Supervised omp workers** — `new omp` launches through Orca's supervised orchestration: Orca creates the agent terminal and injects the task, the worker reports `worker_done` itself, and completion follows the Dispatch lifecycle instead of terminal output
- **Takeover and recovery** — `enroll` records agent terminals OSW did not start, `guard` makes you their notification target and follows a running turn, `revive` reopens provider sessions after an Orca or PC restart, and `stop` interrupts a turn
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
| `new <provider> [--prefix <prefix>] [--model <model>] [--thinking <value>] "<prompt>"` | Create a new agent terminal with a task |
| `use <agent-id-or-terminal> [--prefix <prefix>] "<prompt>"` | Adopt an existing Orca terminal or continue from an OSW agent id |
| `enroll` | Record this worktree's agent terminals that OSW did not start (`enrolled`, no watcher) |
| `guard [<agent_id>...] [--caller-terminal <handle>]` | Become the guardian (notification target) of agents; follow a running turn |
| `revive [<agent_id> [--session <id>]]` | List resumable agents, or reopen one agent's provider session in a new terminal |
| `stop <agent_id>` | Interrupt an agent's current turn |
| `del <agent_id> [--close]` | Remove an agent from management |
| `list [--json]` | List managed agents |
| `logs [--agent <agent_id>] [--tail N]` | Show OSW logs and events |
| `status` | Show OSW state and agent counts |
| `jev status [--check]` | Show the global Jev switch, key/SDK/endpoint/proxy state; `--check` live-probes the endpoint |
| — | **The mode is the global switch**: the machine-wide `OSW_JEV_MODE` environment variable (`enforce\|shadow\|off`; unset = `off`) is the only control — there is no per-project Jev config |

### Options

- `new` accepts `claude`, `codex`, `pi`, `omp`, `kimi`, `grok`, or `dsh` as the provider.
- `--prefix` gives semantic agent ids such as `research_001`, `code_001`, `test_001`, `debug_001`, `review_001`, or `misc_001`; omit it for `agent_NNN`.
- `use <agent-id>` reuses that exact agent id and terminal context; `--prefix` is only for `new` or `use <terminal-handle>`.
- `--model` is passed through unchanged.
- `--thinking` is translated per provider: `claude --effort`, `codex -c model_reasoning_effort=...`, `pi --thinking`, and `grok --reasoning-effort`; `kimi` rejects it because effort lives in its model configuration, `omp` rejects it because its supervised launch cannot pass an effort flag, and `dsh` rejects it (and `--model`) because its TUI entry has neither flag.
- `models omp` reads `omp models --json`; pass one of its `provider/model` selectors to `new omp --model` (forwarded to the supervised launch as `--model`).
- `new omp` runs as a supervised Orca worker: OSW binds the calling terminal to the project's orchestration Run (`state.json: orchestration_run_id`) and starts the worker with `orca orchestration worker-start --agent omp`, so Orca creates the terminal, waits for readiness and injects the task spec. The task turn is therefore never sent by the watcher for `omp`. OSW asks for a longer readiness budget than Orca's 60s default (`--timeout-ms 240000`); if the launch still fails, it reports the failed stage (`failedStage`/`lastError`), releases the orphan terminal Orca left behind, and removes the provisional agent record.
- `new dsh` launches the DeepSeek Harness TUI with `dsh-tui .` — the entry Orca itself starts and tracks as agent `dsh`. DSH carries no model, effort or approval flag at launch: those live in DSH settings and change inside the TUI (`/model`, `/permission`), so `--model`/`--thinking` are refused rather than dropped, and OSW adds no autonomy flag. Readiness holds while DSH's own boot notice (`正在启动 DSH…` / `Starting DSH…`) is on screen: the launchpad draws its composer before the harness is up and drops a prompt sent then. Orca identifies the pane from its title (`✦ 🐋 <session>`) and takes working/done from DSH-TUI's Program Status Protocol reports; neither names a tool, so the report carries `tools_observed: null` instead of claiming none. With DSH's Orca hook bridge installed (`@deepseek-ai/dsh-hooks-claude-code` plus its runtime peers in the `dsh-tui` profile), OSW records the DSH session id during the ready wait — before the first turn's Program Status reports take over the pane record; without the bridge, `revive` needs `--session <id>`.
- OSW adds the provider's non-blocking autonomy flag by default: `claude --dangerously-skip-permissions`, `codex --dangerously-bypass-approvals-and-sandbox`, `pi --approve`, `omp --auto-approve`, `kimi --yolo`, or `grok --always-approve --trust`. For `omp` this is recorded as the equivalent manual launch; the supervised worker's actual argv follows Orca's own agent-tab settings.
- `new` and `use` accept `--caller-terminal <handle>` to receive completion reports on a parent terminal.
- `del --close` also asks Orca to close the terminal.
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

## Architecture

```
new/use
  ├── create or adopt Orca terminal
  ├── write .orca/osw/agents/agent_NNN.json
  ├── spawn detached watcher(agent_NNN)
  └── return immediately
```

OSW owns orchestration only: state initialization, Orca terminal creation/adoption, detached watcher startup, logs, and completion reports. Provider command construction lives in `skill/osw/providers.py`.

### Watcher Lifecycle

```
wait until the pane is ready (`worktree ps` reports it done; tui-idle fallback for untracked CLIs)
 → send the task prompt
 → wait until Orca reports the turn done (`worktree ps`, correlated with the send time; idle fallback for untracked CLIs)
 → send the fixed wrap-up instruction with a pre-allocated handoff path
 → wait again, verify .orca/osw/handoffs/<agent>_<ts>.md exists (one retry)
 → write a JSON report and notify the caller terminal
```

The completion notification is sent proactively by the OSW watcher through
`orca terminal send`; it is not an Orca-native cross-session notification.
Automatic delivery therefore requires a detected caller terminal or an
explicit `--caller-terminal` handle.

`new` starts the bare provider TUI only — the watcher always sends the task
prompt as its own turn, so a briefly idle TUI at startup can never be
mistaken for a completed task. The one exception is supervised `omp`:
Orca injects the task spec at launch.

### Supervised omp workers

`new omp` does not create a plain terminal. OSW binds the calling terminal to
the project's orchestration Run and starts a supervised worker through
`orca orchestration worker-start --agent omp`. The Dispatch lifecycle is a
completion signal that depends neither on `worktree ps` nor on terminal
silence. A manually launched omp is tracked by `worktree ps` only after its
first turn, and at every turn end Orca drops the pane's status reports
(it reads OSC 133;D as "command finished"); the row comes back only through
the local `~/.omp/agent/extensions/orca-status-watchdog.ts` extension, about
2s later, and without it omp tracking is unreliable. An omp status bar also
repaints about once a second, so every output-idle heuristic would wait
forever.

```
Orca waits for terminal readiness and injects the task spec (which carries
the handoff/report contract)
 → the worker does the task, writes the handoff markdown, and sends
   `worker_done --report-path <handoff>` itself
 → the watcher polls `orca orchestration worker-show` until the Dispatch
   settles, then verifies the handoff file (sending the standard wrap-up
   turn as a fallback when the worker skipped the contract)
 → the report records `orchestration_outcome`; the worker terminal is kept
   live with `worker-retain`; the caller is notified
```

`del --close` releases a settled supervised worker (`worker-release`) or
stops a running one (`worker-stop`); a caller terminal already bound to a
different Run is rebound, recorded as an `orchestration_run_rebound` event.

## Takeover and Recovery

```bash
python skill/osw.py enroll                       # record unmanaged agent terminals
python skill/osw.py guard                        # become their guardian
python skill/osw.py revive                       # list resumable sessions (JSON)
python skill/osw.py revive code_001              # reopen one after a restart
python skill/osw.py stop code_001                # interrupt the current turn
```

- **`enroll`** scans `terminal list` for this worktree, skips the caller's own terminal and already-recorded panes, takes the provider from `agentIdentity` (else the ps `agentType`), and writes one `enrolled` record per pane with `origin: enroll`. Providers OSW cannot launch get `support: guard_only` (`support_reason: unsupported_provider`). Orca names a DSH pane after its launcher (`dsh-tui`); enroll maps that to provider `dsh`. It prints `{"enrolled": [...], "skipped": [{terminal, reason}]}`.
- **`guard [<id>...]`** makes the executor (`--caller-terminal`, else `ORCA_TERMINAL_HANDLE`) the guardian; without ids it takes every unguarded `enrolled`/`done`/`error` agent. A live watcher is terminated and restarted to follow the running turn; an unwatched pane that ps reports `working` gets a following watcher; an idle agent only records the guardian. A followed turn is reported without a handoff (`already_done` when it had finished before the watcher took over). `use` keeps notifying the guardian unless `--caller-terminal` is passed.
- **`revive`** without an id prints `{"revivable": [...], "not_revivable": [{agent_id, reason}]}`. `revive <id> [--session <sid>]` needs `support: full`, state `lost`/`done`/`error`/`enrolled`, and no live watcher. It reopens the session (`--session`, else the recorded `session_id`, else the pane's current one) with the provider's resume syntax plus the usual autonomy and model flags, and keeps the agent id. The command is typed into the agent's original pane when that pane is a confirmed empty shell — still listed, no `agentIdentity`, its ps row not `working`, a shell OSW can quote for (pwsh/PowerShell, cmd, POSIX), and Jev `ready_state` judging `shell_prompt` — so `pane_key` stays (`placement: in_place`); otherwise, including `OSW_JEV_MODE=off`, it opens a new terminal (`placement: new_terminal`). The output and the `agent_revived` event carry `placement`. A `lost` agent (interrupted mid-task) is resumed with `resume_turn: true`: the watcher sends `continue`, then runs the normal completion detection and handoff. Otherwise the agent comes back `enrolled`.
- **`stop <id>`** sends Ctrl-C (`terminal send --interrupt`) to claude, codex, kimi, grok, and dsh, and a bare Esc to pi and omp. With a live watcher it writes the sidecar `.orca/osw/agents/<id>.stop`; the watcher ends the turn when ps reports it interrupted or done, or the pane has been silent for 5s, records `error` with `completion_source: interrupted`, skips the handoff, notifies the guardian, and deletes the sidecar. It prints `{"agent_id", "key", "watcher"}`.

New record fields (all optional; old records still load):

| Field | Values |
|---|---|
| `origin` | `new`, `use`, `enroll` |
| `session_id` | Provider session id |
| `guardian` | `{agent_id}` for an OSW agent (its current terminal is read from its record at notify time), else `{terminal, pane_key}` |
| `support` | `full`, or `guard_only` with `support_reason: unsupported_provider` |
| `follow_turn` | The watcher takes over the running turn; cleared when it finishes |
| `resume_turn` | The watcher's task prompt is `continue`; cleared when it finishes |
| `notified_asks` | Ids of supervised-worker questions already relayed to the guardian |

New states: `enrolled` (recorded, no watcher) and `lost` (non-final, watcher process gone — set by `list`/`status`/`use`/`revive`). New completion sources: `already_done` and `interrupted`.

Notifications resolve their target before every send: the guardian if one is recorded, else the caller terminal. A guardian agent whose record cannot be read falls back to the caller terminal (`guardian_unresolved` event); a stale guardian handle is re-resolved once by pane and the send retried once.

Session ids come from Orca's agent-hook cache `%APPDATA%\orca\agent-hooks\last-status.json` (here `C:\Users\shiki\AppData\Roaming\orca\agent-hooks\last-status.json`). It is not a public Orca interface: OSW reads it only when its `version` matches (2), so on a different format the session id is simply empty.

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
      log.py          # structured events and file logging
      orca_cli.py     # async Orca CLI wrapper
      process.py      # hidden subprocess helpers (Windows)
      providers.py    # provider commands and model discovery
      sessions.py     # provider session ids from Orca's agent-hook cache
      blockers.py     # blocking-screen discovery, whitelist, update lock
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
      agents/          # one JSON record per managed agent (+ <id>.stop requests)
      handoffs/        # worker handoff markdown files
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
