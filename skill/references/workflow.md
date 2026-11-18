# Phased Runs

For work that is many independent units plus a check and a summary — audit
every route, port every handler, review every module — build a workflow
instead of hand-rolling a fan-out. `.orca/osw/workflows/<name>.json` holds
phases; the `scheduler` expands them and is the only writer of
`.orca/osw/runs/<run_id>/`. Nobody hand-writes that definition: one ordinary
agent drafts it against the closed schema, `defs.py` judges the file, and
`run --dry-run` shows the expansion first, so a bad draft is refused by name
rather than half-run.

Both entry points — a skill invocation that names an `osw workflow`, and the
`/osw-workflow` command — follow this page. There is one recipe on purpose:
two copies drift apart.

The request: **<the request>**

1. Find the osw skill directory: the first that exists of
   `~/.claude/skills/osw`, `~/.codex/skills/osw`, `~/.agents/skills/osw`.
   Call that directory's `osw.py`.
2. Start it:
   `python <dir>/osw.py workflow go "<the request>" --name <short-slug>`.
   One ordinary agent drafts `.orca/osw/workflows/<slug>.json`, the validator
   judges it, the run is archived and its scheduler starts as its own process.
   The command prints `STARTED <run_id> ... pid <n>` and returns.
   A refusal (bad schema, unknown placeholder, `too_many_agents`) means the
   draft is wrong: read the code it names, fix the draft, run it again. Never
   bypass the check.
3. Watch until it is terminal, then report:
   `python <dir>/osw.py workflow status <run_id> --json` (add `--watch` for the
   live table). Never read panes or the scheduler log for progress; the archive
   is the truth. Scheduler problems are in
   `.orca/osw/runs/<run_id>/scheduler.log`.
4. It is terminal when the state is `done`, `failed`, `partial` or `stopped`.
   `failed` names the step and its error: `workflow retry <run> <step>
   --reason <why>` re-arms one step, `workflow accept ...` takes a step as it
   is, `workflow reject ...` buries it; after a `stop`, `workflow run <slug>
   --resume` reuses finished steps by key and runs only what is left.
5. Finish with `python <dir>/osw.py sweep` to reclaim panes that are still
   open (a `done` turn closes its own; `sweep` catches the rest), then
   summarise per-step results and any verdicts in `verdicts.jsonl` — say
   plainly which claims were confirmed, refuted or left unverifiable.

```text
python <dir>/osw.py workflow go "<one sentence>" --name <slug>   # plan + start, returns
python <dir>/osw.py workflow status <run_id> --json              # the archive is the truth
python <dir>/osw.py workflow plan "<one sentence>"               # draft only, no run
python <dir>/osw.py workflow run <slug> --dry-run                # show the expansion
python <dir>/osw.py workflow run <slug>                          # the command is the scheduler
python <dir>/osw.py sweep                                        # reclaim finished panes
```

`go` creates the run, then starts the scheduler as its own process, so the run
outlives the tool call that asked for it; its output goes to
`.orca/osw/runs/<run_id>/scheduler.log`. A run started in a pane keeps the
caller terminal; from a plain shell or CI it dispatches with `--no-notify`.
`--resume` reuses finished steps by key, so an interrupted run continues where
it stopped. `max_agents` refuses rather than truncates. Do not hand-write the
definition to get past a validator refusal: fix the draft.
