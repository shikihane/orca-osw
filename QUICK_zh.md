# 快速开始

[English](QUICK.md)

## 1. 安装依赖

```bash
python -m pip install anyio typer rich
```

## 2. 初始化

```bash
cd /你的/项目
python /path/to/orca-osw/skill/osw.py init
```

在当前目录创建 `.orca/osw/`，写入默认 `state.json`。

## 3. 查看 provider 模型

```bash
python /path/to/orca-osw/skill/osw.py models claude
python /path/to/orca-osw/skill/osw.py models codex
python /path/to/orca-osw/skill/osw.py models pi
```

这是只读操作，不会修改 `.orca/osw/state.json`。

## 4. 创建 agent

```bash
python /path/to/orca-osw/skill/osw.py new claude --prefix code --model sonnet --thinking high "修复 src/auth.py 的失败测试"
```

输出：

```
Created code_001 on terminal term_xxx (provider: claude)
```

## 5. 查看状态

```bash
python /path/to/orca-osw/skill/osw.py list
```

```
AGENT_ID     STATE            TERMINAL       CALLER         LAST_PROMPT
code_001     assigned         term_xxx       -              修复 src/auth.py 的失败测...
```

```bash
python /path/to/orca-osw/skill/osw.py status
```

```
State file: /你的/项目/.orca/osw/state.json
Agents:
  assigned: 1
```

## 6. 自动交接流程

当 agent 完成任务并进入空闲：

1. watcher 检测到 tui-idle
2. 发送强制 `/handoff` 提示
3. agent 生成 `HANDOFF_*.md` 文件
4. 如果设置了 `--caller-terminal`，watcher 发送完成报告：

```
子任务完成。
agent: agent_001
terminal: term_xxx
handoff: HANDOFF_fix_auth_tests.md
```

## 7. 广播消息

```bash
python /path/to/orca-osw/skill/osw.py all "请暂停当前工作，等待新指令"
```

## 8. 接管已有终端

```bash
python /path/to/orca-osw/skill/osw.py use term_yyy --prefix debug "审查 PR 变更"
python /path/to/orca-osw/skill/osw.py use code_001 "继续这个任务"
```

终端必须属于当前目录的 worktree。

## 9. 移除 agent

```bash
python /path/to/orca-osw/skill/osw.py del agent_001          # 仅从管理中移除
python /path/to/orca-osw/skill/osw.py del agent_001 --close  # 同时关闭终端
```

## 10. 状态与模型发现

项目状态刻意保持很小：

```json
{
  "version": 3,
  "project_root": "/你的/项目"
}
```

运行时 agent 记录位于 `.orca/osw/agents/`。使用 `models <provider>` 查看 provider 选项；OSW 不把模型配置写入 state。
