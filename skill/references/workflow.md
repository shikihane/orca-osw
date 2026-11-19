# Phased Runs

For work that is many independent units plus a check and a summary — audit
every route, port every handler, review every module — build a workflow
instead of hand-rolling a fan-out. `.orca/osw/workflows/<name>.json` holds
phases; the `scheduler` expands them and is the only writer of
`.orca/osw/runs/<run_id>/`. Nobody hand-writes that definition: one ordinary
agent drafts it against the closed schema, `defs.py` judges the file, and
`run --dry-run` shows the expansion first, so a bad draft is refused by name
rather than half-run.

This page is the only recipe: the skill is the entry, and nothing else mirrors
it.

The request: **<the request>**

1. Find the osw skill directory: the first that exists of
   `~/.claude/skills/osw`, `~/.codex/skills/osw`, `~/.agents/skills/osw`.
   Call that directory's `osw.py`.
2. Start it:
   `python <dir>/osw.py workflow go "<the request>" --name <short-slug>`.
   This returns in about a second with `PLANNING <slug>  pid <n>`: a detached
   worker drafts `.orca/osw/workflows/<slug>.json`, the validator judges it,
   the run is archived, and the scheduler drives it — all after your command
   exited — `go` itself never blocks, and waiting is the next step.
   A refusal (bad schema, unknown placeholder, `too_many_agents`, no items)
   leaves `planning <slug> ... <code>: <reason>` in `status` and stops there.
   Read the code, fix the draft, run it again. Never bypass the check, never
   edit `.orca/osw/config.json` or `AGENTS.md` to make a dispatch work, and
   never re-run with a different provider or model on your own.
3. Wait until it is terminal, then report:
   `python <dir>/osw.py workflow wait <run-or-stem> [--timeout SECONDS]` blocks
   until the run is terminal (exit 0) or the timeout expires (exit 2), and
   writes nothing — `go` prints a stem, so pass the stem and it resolves the
   run id once the draft lands. Then read
   `python <dir>/osw.py workflow status <run_id> --json` (without a run id it
   reports the newest run plus any drafting job); `--watch` is a live table
   for a person, not the agent's loop. Never read panes or the scheduler log
   for progress; the archive is the truth, and its `scheduler_alive` says
   whether anything is still driving the run. Scheduler problems are in
   `.orca/osw/runs/<run_id>/scheduler.log`.
4. It is terminal when the state is `done`, `failed`, `partial` or `stopped`.
   `done` is the end of the declared phases, not the end of the project; the
   rest of this page says how to read that. `failed` names the step and its
   error: `workflow retry <run> <step> --reason <why>` re-arms one step,
   `workflow accept ...` takes a step as it is, `workflow reject ...` buries
   it; after a `stop`, or a run whose supervisor is gone, `workflow resume
   <run>` restarts the scheduler and reuses finished steps by key, running
   only what is left.
5. Finish with `python <dir>/osw.py sweep` to reclaim panes that are still
   open (a `done` turn closes its own; `sweep` catches the rest), then
   summarise per-step results and any verdicts in `verdicts.jsonl` — gate
   verdicts are its `"kind": "gate"` lines — and say plainly which claims
   were confirmed, refuted or left unverifiable.

```text
python <dir>/osw.py workflow go "<one sentence>" --name <slug>   # plan + start, returns
python <dir>/osw.py workflow status <run_id> --json              # the archive is the truth
python <dir>/osw.py workflow wait <run-or-stem>                  # block until terminal (0) or timeout (2)
python <dir>/osw.py workflow goal <run_id>                       # what the run is for
python <dir>/osw.py workflow goal <run_id> --set "<sentence>" --reason "<why>"  # accept it
python <dir>/osw.py workflow resume <run_id>                     # restart a dead scheduler
python <dir>/osw.py workflow reload <run_id>                     # rebuild a wrong definition
python <dir>/osw.py workflow next <run_id>                       # continue towards the goal
python <dir>/osw.py workflow run <slug> --resume --run <id>        # the same, foreground
python <dir>/osw.py workflow plan "<one sentence>"               # draft only, no run
python <dir>/osw.py workflow run <slug> --dry-run                # show the expansion
python <dir>/osw.py workflow run <slug>                          # the command is the scheduler
python <dir>/osw.py sweep                                        # reclaim finished panes
```

`go` writes one job marker under `.orca/osw/planning/` and returns; a detached
worker drafts, creates the run and schedules it, so the run outlives the tool
call that asked for it and the run's scheduler output goes to
`.orca/osw/runs/<run_id>/scheduler.log`. A job that fails keeps its marker with
the code and the reason, and a run that hits a refusal records it in its own
`run.json` as `failed` — it never dies silently and nothing is retried behind
the scenes. A project that configured no provider dispatches with `pi` and says
so once. A run started in a pane keeps the caller terminal; from a plain shell
or CI it dispatches with `--no-notify`. `--resume` reuses finished steps by key,
so an interrupted run continues where it stopped. `max_agents` refuses rather
than truncates. Do not hand-write the definition to get past a validator
refusal: fix the draft.

## Rounds, gates and recovery

One `go` is one batch: the phases the definition declares, run once. `done`
means those phases — and the rounds a repeat phase declared — finished, not
that the request is finished; read the run's goal against the request before
reporting the work complete.

A phase that declares `criteria` is not finished when its work settles. A
blind gate agent answers them — it reads the run's goal, the criteria, that
round's `evidence` files and the round's outputs, and never the workers'
prompts or verdicts — with one JSON object `{"pass", "reason", "next_items",
"goal_proposals"}`. Those inputs live in a brief on disk,
`runs/<run>/gates/<phase>-gate-r<n>.md`; the dispatched prompt only names it,
because a prompt travels as one command-line argument and Windows refuses a
command line over ~32 KiB. The scheduler, not the gate, maps that answer to
the next step: `pass` true closes the phase; `pass` false on a phase with `repeat: true`
and non-empty `next_items` starts round `n+1` from those items while
`max_rounds` and the agent ceiling allow it (a ceiling that would be exceeded
marks the phase `failed` with `too_many_agents`); otherwise `--policy`
decides — `strict` fails the run at that phase and round, `partial` marks the
phase partial. An answer that is missing, unparseable, not a boolean `pass`, or
carrying a non-array `next_items` (`gate_invalid`) gets one retry and then
holds the run as `unverifiable`. The same holds for a `next_items` list whose
elements cannot fill the phase's task — the gate is told the shape of the items
it was given, and a list that does not fit is refused rather than starting a
round that cannot expand. Gate proposals are recorded and notified,
never applied.

`status --json` is where all of that is read: `scheduler_alive`, the
`supervisor` block (`{pid, alive, restarts}`), each phase's `round` and `gate`
(`{round, pass, reason}`), and the run's `goal` and `goal_proposals` count.
`verdicts.jsonl` is append-only: one line per gate answer ever given, so a
phase that was re-judged (after `reload` or a second round) has several lines
for the same phase and round — read the last one for the current answer, keep
the rest as the audit trail. A
supervisor owns the scheduler's lifetime: while the run is still `running` it
restarts a scheduler that died, up to `OSW_WORKFLOW_RESTARTS` (default 3) with
a 5 s backoff, logging to `supervisor.log` and keeping `supervisor.json`
(`{pid, started_at, restarts, last_exit}`) beside the run — it never writes
`run.json`. With no supervisor alive, `workflow resume <run>` is the manual
entry: it starts a detached scheduler and `workflow run <slug> --resume --run
<id>` is the same in the foreground; either reuses every finished step by key.
A live scheduler refuses with `run_running`.

A run that ended because the definition itself was wrong is rebuilt in place:
fix the definition, then `workflow reload <run>`. Every `done` step is kept,
every step that had not finished is dropped so it is re-rendered from the
definition as it now reads (a changed prompt is a new step key, which is the
contract), the ceiling becomes the definition's when it is higher — never below
what the run already spent — and the run goes back to `running` for `resume`.
It refuses `run_running` while a scheduler is alive, because run state has one
writer at a time.

`workflow goal <run>` prints the goal, its proposals and the round count;
adding `--set "<sentence>" --reason "<why>"` records the acceptance and
rewrites the goal every later gate reads. An unknown run (`run_not_found`) or
a non-human identity is refused. A drafting job whose planner died is reported
as `planning <stem> ... plan_failed: planner_died` — `status --json` carries
the job's `alive` and `code` — and nothing retries it.

A step whose attempt ends `lost` is revived in its own pane before a fresh
agent is spent — the revival is the next attempt, and a failed revival falls
back to a new agent. `revive_lost` defaults true; `--no-revive-lost` on
`workflow run` sets it false for that run.

The acting agent runs this loop; it never prints a command for a person to run.
While the run is not terminal, wait with `workflow wait <run-or-stem>` — exit 0
at a terminal run, 2 at the timeout, nothing written — then read `status
--json`. When the state is `running` and neither the supervisor's `alive` nor
`scheduler_alive` is true, call `workflow resume <run>` yourself. When the
state is terminal and the last gate verdict's `reason` or the run's `goal` says
the request is unmet, call `workflow next <run>`: it reads the goal, the gate
reason and the proposals, assembles a request and starts it under a new stem
(`<definition>-r2`, `-r3`, …), refusing `no_goal` when there is nothing to
continue towards. Never report a batch as finished work while the goal is open.
