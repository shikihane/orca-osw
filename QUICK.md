# Quick Start

[中文版](QUICK_zh.md)

## 1. Install dependencies

```bash
python -m pip install anyio typer rich
```

## 2. Initialize

```bash
cd /your/project
python /path/to/orca-osw/skill/osw.py init
```

This creates `.orca/osw/` with a default `state.json`.

## 3. Start the supervisor

```bash
python /path/to/orca-osw/skill/osw.py serve
```

Keep this running. All other commands talk to it.

## 4. Create an agent

Open a second terminal in the same directory:

```bash
python /path/to/orca-osw/skill/osw.py new "Fix the failing tests in src/auth.py"
```

Output:

```
Created agent_001 on terminal term_xxx
```

## 5. Monitor

```bash
python /path/to/orca-osw/skill/osw.py list
```

```
AGENT_ID     STATE            TERMINAL       CALLER         LAST_PROMPT
agent_001    assigned         term_xxx       -              Fix the failing tests in sr...
```

```bash
python /path/to/orca-osw/skill/osw.py status
```

```
Serve: running
State file: /your/project/.orca/osw/state.json
Agents:
  assigned: 1
```

## 6. What happens automatically

When the agent finishes its task and goes idle:

1. The supervisor detects tui-idle
2. Sends a forced `/handoff` prompt
3. The agent generates a `HANDOFF_*.md` file
4. If `--caller-terminal` was set, the supervisor sends a completion report:

```
子任务完成。
agent: agent_001
terminal: term_xxx
handoff: HANDOFF_fix_auth_tests.md
```

## 7. Broadcast to all agents

```bash
python /path/to/orca-osw/skill/osw.py all "请暂停当前工作，等待新指令"
```

## 8. Adopt an existing terminal

```bash
python /path/to/orca-osw/skill/osw.py use --terminal term_yyy "Review the PR changes"
```

The terminal must belong to the current directory's worktree.

## 9. Remove an agent

```bash
python /path/to/orca-osw/skill/osw.py del agent_001          # remove from management
python /path/to/orca-osw/skill/osw.py del agent_001 --close  # also close the terminal
```

## 10. Configure model tiers

Edit `.orca/osw/state.json` and change the `models` section:

```json
{
  "models": {
    "strong": [{ "name": "my-strong", "command": "codex" }],
    "medium": [{ "name": "my-medium", "command": "claude" }],
    "weak":   [{ "name": "my-weak",   "command": "pi" }]
  }
}
```

`new` uses the first `strong` entry. Edit freely — OSW reads it on each request.
