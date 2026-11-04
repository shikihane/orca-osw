# Quick Start

[中文版](QUICK_zh.md)

## 1. Install dependencies

```bash
python -m pip install anyio typer rich
```

## 2. Check Orca compatibility

```bash
python /path/to/orca-osw/skill/osw.py doctor
```

Run this read-only probe again after every Orca upgrade. Use `--json` in
scripts; a response-contract change produces a non-zero exit and a typed
`orca_contract_mismatch` error.

## 3. Initialize

```bash
cd /your/project
python /path/to/orca-osw/skill/osw.py init
```

This creates `.orca/osw/` with a default `state.json`.

## 4. Inspect provider models

```bash
python /path/to/orca-osw/skill/osw.py models claude
python /path/to/orca-osw/skill/osw.py models codex
python /path/to/orca-osw/skill/osw.py models pi
python /path/to/orca-osw/skill/osw.py models omp
python /path/to/orca-osw/skill/osw.py models kimi
python /path/to/orca-osw/skill/osw.py models grok
python /path/to/orca-osw/skill/osw.py models dsh
```

This is read-only and does not change `.orca/osw/state.json`.

## 5. Create an agent

```bash
python /path/to/orca-osw/skill/osw.py new claude --prefix code --model sonnet --thinking high "Fix the failing tests in src/auth.py"
```

Output:

```
Created code_001 on terminal term_xxx (provider: claude)
```

## 6. Monitor

```bash
python /path/to/orca-osw/skill/osw.py list
```

```
AGENT_ID     STATE        ORCA       WATCHER  TERMINAL       PROMPT
code_001     working      working    yes      term_xxx       Fix the failing tests in src/auth.py
```

```bash
python /path/to/orca-osw/skill/osw.py status
```

```
State file: /your/project/.orca/osw/state.json
Agents:
  working: 1
Watchers running: 1
```

## 7. What happens automatically

When Orca reports the agent's turn as done (`worktree ps`; untracked CLIs fall back to idle detection):

1. The watcher sends a fixed single-line wrap-up instruction with a pre-allocated handoff path
2. The agent writes the handoff markdown to `.orca/osw/handoffs/<agent>_<ts>.md` (verified, one retry)
3. The watcher writes a JSON report to `.orca/osw/reports/`
4. The caller terminal (auto-detected when run from a TTY, or set via `--caller-terminal`) receives a single-line report:

```
# [osw] task-finished agent=code_001 terminal=term_xxx report=... handoff=... summary=...
```

## 8. Broadcast to all agents

```bash
python /path/to/orca-osw/skill/osw.py all "请暂停当前工作，等待新指令"
```

## 9. Adopt an existing terminal

```bash
python /path/to/orca-osw/skill/osw.py use term_yyy --prefix debug "Review the PR changes"
python /path/to/orca-osw/skill/osw.py use code_001 "Continue the task"
```

The terminal must belong to the current directory's worktree.

## 10. Remove an agent

```bash
python /path/to/orca-osw/skill/osw.py del agent_001          # remove from management
python /path/to/orca-osw/skill/osw.py del agent_001 --close  # also close the terminal
```

## 11. State and model discovery

Project state is intentionally small:

```json
{
  "version": 3,
  "project_root": "/your/project"
}
```

Runtime agent records live under `.orca/osw/agents/`. Use `models <provider>` to inspect provider options; OSW does not write model configuration to state.

## 12. Run an agent on another host

Register the machine once, then dispatch to it by name:

```bash
python /path/to/orca-osw/skill/osw.py host add fnnas --ssh fnnas --worktree path:/srv/repo
python /path/to/orca-osw/skill/osw.py host doctor fnnas --provider claude
python /path/to/orca-osw/skill/osw.py new --on fnnas claude --prefix code "Fix the failing tests"
```

`host doctor` checks, in order, that the host answers over SSH, that Orca there
has a registered workspace, that a worktree matches the selector, and that the
provider CLI is installed. It stops at the first failing rung and prints that
rung's error code: `host_unreachable`, `host_permission_denied`,
`remote_no_project`, `remote_no_worktree`, or `provider_missing`. The complete
list and the handoff details are in
[README.md](README.md#cross-host-operation).

The dispatch is the classic lane: OSW creates the terminal on that host
(`terminal create --environment fnnas --worktree <selector> --command
"<absolute provider path> <autonomy flags>"`) and the local watcher sends the
prompt, reads the pane and collects the handoff over SSH. A remote agent has no
Dispatch id, and the Run stays local and is unused remotely. Orca's supervised
`worker-start --on` lane stays unused: on Orca 1.4.248 against a paired server
readiness is `host_indeterminate` — `worker-start` held at `agent_readiness` for
240s with the remote pane sitting at its composer, and
`terminal wait --for tui-idle` timed out on that same idle pane, while
`terminal send`/`read` and `worktree ps` answered.

```
# what new --on actually runs on the far side
orca terminal create --environment fnnas --worktree <selector> \
     --command "/usr/local/bin/claude --dangerously-skip-permissions"
```

If the provider has never run for that account, start it once by hand in that
pane: its first-run theme/security/bypass dialogs are nothing a readiness probe
can clear. Directory trust is the exception — `new` pre-records it over SSH, so
that prompt never appears.

On that machine you need only `sshd` reachable from here with this machine's
key authorised, a workspace already registered in Orca, and the provider CLI.
No OSW install, no resident process, no helper script, no clone. Orca pairing
is optional for operation — it lets you view the remote terminal in the Orca UI
and is how dispatch is addressed.

The agent is addressed as `<host>/<agent_id>` from then on (`fnnas/code_001`;
`local/` may be omitted). When the turn finishes, the handoff is copied into the
local `.orca/osw/handoffs/` and judged by the same rule as a local one, and the
completion report still reaches your guardian terminal here:

```bash
python /path/to/orca-osw/skill/osw.py list --all     # HOST column, local + remote rows
python /path/to/orca-osw/skill/osw.py status --all   # counts per host
python /path/to/orca-osw/skill/osw.py del fnnas/code_001 --close
```

`host list` shows what is registered and whether each host answers right now;
`host rm <name>` drops a record.
