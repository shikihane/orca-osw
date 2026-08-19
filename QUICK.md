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
