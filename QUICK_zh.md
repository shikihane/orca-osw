# 快速开始

[English](QUICK.md)

## 1. 安装依赖

```bash
python -m pip install anyio typer rich
```

## 2. 检查 Orca 兼容性

```bash
python /path/to/orca-osw/skill/osw.py doctor
```

这是只读检查；每次升级 Orca 后都应重新运行。脚本中可使用 `--json`；
响应契约变化时命令会以非零状态退出，并返回可识别的
`orca_contract_mismatch` 错误码。

## 3. 初始化

```bash
cd /你的/项目
python /path/to/orca-osw/skill/osw.py init
```

在当前目录创建 `.orca/osw/`，写入默认 `state.json`。

## 4. 查看 provider 模型

```bash
python /path/to/orca-osw/skill/osw.py models claude
python /path/to/orca-osw/skill/osw.py models codex
python /path/to/orca-osw/skill/osw.py models pi
```

这是只读操作，不会修改 `.orca/osw/state.json`。

## 5. 创建 agent

```bash
python /path/to/orca-osw/skill/osw.py new claude --prefix code --model sonnet --thinking high "修复 src/auth.py 的失败测试"
```

输出：

```
Created code_001 on terminal term_xxx (provider: claude)
```

## 6. 查看状态

```bash
python /path/to/orca-osw/skill/osw.py list
```

```
AGENT_ID     STATE        ORCA       WATCHER  TERMINAL       PROMPT
code_001     working      working    yes      term_xxx       修复 src/auth.py 的失败测试
```

```bash
python /path/to/orca-osw/skill/osw.py status
```

```
State file: /你的/项目/.orca/osw/state.json
Agents:
  working: 1
Watchers running: 1
```

## 7. 自动交接流程

当 Orca 报告该轮完成时（`worktree ps`；未识别的 CLI 回退到空闲检测）：

1. watcher 发送固定的单行收尾指令，附带预分配的交接文件路径
2. agent 把交接 markdown 写入 `.orca/osw/handoffs/<agent>_<ts>.md`（会校验，重试一次）
3. watcher 把 JSON 报告写入 `.orca/osw/reports/`
4. 调用方终端（TTY 下自动检测，或通过 `--caller-terminal` 指定）收到单行完成报告：

```
# [osw] task-finished agent=code_001 terminal=term_xxx report=... handoff=... summary=...
```

## 8. 广播消息

```bash
python /path/to/orca-osw/skill/osw.py all "请暂停当前工作，等待新指令"
```

## 9. 接管已有终端

```bash
python /path/to/orca-osw/skill/osw.py use term_yyy --prefix debug "审查 PR 变更"
python /path/to/orca-osw/skill/osw.py use code_001 "继续这个任务"
```

终端必须属于当前目录的 worktree。

## 10. 移除 agent

```bash
python /path/to/orca-osw/skill/osw.py del agent_001          # 仅从管理中移除
python /path/to/orca-osw/skill/osw.py del agent_001 --close  # 同时关闭终端
```

## 11. 状态与模型发现

项目状态刻意保持很小：

```json
{
  "version": 3,
  "project_root": "/你的/项目"
}
```

运行时 agent 记录位于 `.orca/osw/agents/`。使用 `models <provider>` 查看 provider 选项；OSW 不把模型配置写入 state。
