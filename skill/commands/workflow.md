---
description: Draft and start a phased OSW workflow from one sentence
argument-hint: <what the batch should do>
---

The request: $ARGUMENTS

The OSW skill directory is the first of these that exists:

- `~/.claude/skills/osw`
- `~/.codex/skills/osw`
- `~/.agents/skills/osw`

Read `<that directory>/references/workflow.md` — for example
`~/.claude/skills/osw/references/workflow.md` — and follow it with the request
above as the task. That file ships with the skill, so read it at that absolute
path; `references/workflow.md` alone is not a project path and will not resolve.

If that file is missing (a stale install), do the work directly instead of
stopping: `python <that directory>/osw.py workflow go "$ARGUMENTS" --name
<slug>`, then poll `python <that directory>/osw.py workflow status <run_id>
--json` until the state is `done`, `failed`, `partial` or `stopped`.
