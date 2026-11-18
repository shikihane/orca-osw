---
description: Draft and start a phased OSW workflow from one sentence
argument-hint: <what the batch should do>
---

The request: $ARGUMENTS

Run this as an OSW workflow — phases of independent agents — not as inline
dispatch. One command does the whole handoff; do not stop to ask.

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
5. Finish with `python <dir>/osw.py sweep` to reclaim the panes, then summarise
   per-step results and any verdicts in `verdicts.jsonl` — say plainly which
   claims were confirmed, refuted or left unverifiable.
