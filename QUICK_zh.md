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

## 3. 启动监督器

```bash
python /path/to/orca-osw/skill/osw.py serve
```

保持运行。其他命令都通过它工作。

## 4. 创建 agent

在同目录打开第二个终端：

```bash
python /path/to/orca-osw/skill/osw.py new "修复 src/auth.py 的失败测试"
```

输出：

```
Created agent_001 on terminal term_xxx
```

## 5. 查看状态

```bash
python /path/to/orca-osw/skill/osw.py list
```

```
AGENT_ID     STATE            TERMINAL       CALLER         LAST_PROMPT
agent_001    assigned         term_xxx       -              修复 src/auth.py 的失败测...
```

```bash
python /path/to/orca-osw/skill/osw.py status
```

```
Serve: running
State file: /你的/项目/.orca/osw/state.json
Agents:
  assigned: 1
```

## 6. 自动交接流程

当 agent 完成任务并进入空闲：

1. 监督器检测到 tui-idle
2. 发送强制 `/handoff` 提示
3. agent 生成 `HANDOFF_*.md` 文件
4. 如果设置了 `--caller-terminal`，监督器发送完成报告：

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
python /path/to/orca-osw/skill/osw.py use --terminal term_yyy "审查 PR 变更"
```

终端必须属于当前目录的 worktree。

## 9. 移除 agent

```bash
python /path/to/orca-osw/skill/osw.py del agent_001          # 仅从管理中移除
python /path/to/orca-osw/skill/osw.py del agent_001 --close  # 同时关闭终端
```

## 10. 配置模型等级

编辑 `.orca/osw/state.json` 的 `models` 部分：

```json
{
  "models": {
    "strong": [{ "name": "my-strong", "command": "codex" }],
    "medium": [{ "name": "my-medium", "command": "claude" }],
    "weak":   [{ "name": "my-weak",   "command": "pi" }]
  }
}
```

`new` 使用第一个 `strong` 条目。可随意编辑，OSW 每次请求时重新读取。
