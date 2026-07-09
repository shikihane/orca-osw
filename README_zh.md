# OSW — Orca Agent Supervisor

[English](README.md)

轻量 Python 工具，用于在当前目录管理 Orca 支持的 agent 会话。OSW 启动或接管 agent 终端，启动 detached watcher，通过空闲信号检测任务完成，强制执行交接流程，并将生成的交接文件报告给调用方。

## 特性

- **目录隔离** — 每个工作目录是独立的管理范围，即使跨 git worktree 也互不影响
- **Detached watcher** — 每个受管 agent 都有独立 watcher 进程，监控完成状态且不阻塞 CLI
- **自动交接** — 检测 agent 空闲后自动触发 `/handoff`，提取 `HANDOFF_*.md` 文件名，通知调用方终端
- **最少依赖** — 仅需 `anyio`、`typer`，可选 `rich` 获得彩色日志

## 环境要求

- Python 3.10+
- [Orca](https://orca.dev) 已运行且 `orca` CLI 在 PATH 中
- `anyio` 和 `typer`：

```
python -m pip install anyio typer rich
```

## 快速开始

详见 [QUICK_zh.md](QUICK_zh.md)。

```bash
python skill/osw.py init
python skill/osw.py models pi

python skill/osw.py new claude --model sonnet --thinking high "修复失败的测试"
python skill/osw.py new codex --model gpt-5 --thinking medium "重构 provider 层"
python skill/osw.py list
python skill/osw.py status
```

## 命令

| 命令 | 说明 |
|---|---|
| `init` | 初始化 `.orca/osw/` 状态目录 |
| `models <provider> [--json]` | 只读查看 provider 模型选项 |
| `new <provider> [--model <model>] [--thinking <值>] "<提示>"` | 创建新的 agent 终端并发送任务 |
| `use <agent-id-or-terminal> "<提示>"` | 接管已有 Orca 终端，或通过 OSW agent id 继续 |
| `all "<消息>"` | 向所有受管 agent 广播消息 |
| `del <agent_id> [--close]` | 移除 agent |
| `list [--json]` | 列出受管 agent |
| `logs [--agent <agent_id>] [--tail N]` | 查看 OSW 日志和事件 |
| `status` | 显示 OSW 状态和 agent 统计 |

### 选项

- `new` 的 provider 支持 `claude`、`codex`、`pi`。
- `--model` 原样传给 provider。
- `--thinking` 按 provider 转换：`claude --effort`、`codex -c model_reasoning_effort=...`、`pi --thinking`。
- `new` 和 `use` 支持 `--caller-terminal <句柄>`，指定接收完成报告的父终端。
- `del --close` 同时让 Orca 关闭终端。
- `list --json` 输出原始 JSON。

## 架构

```
new/use
  ├── 创建或接管 Orca 终端
  ├── 写入 .orca/osw/agents/agent_NNN.json
  ├── 启动 detached watcher(agent_NNN)
  └── 立即返回
```

OSW 只负责编排：初始化状态、创建或接管 Orca 终端、启动 detached watcher、记录日志并报告完成。Provider 命令构造集中在 `skill/osw/providers.py`。

### 观察者生命周期

```
发送任务提示
 → 等待 tui-idle（最长 10 分钟）
 → 发送强制 /handoff 提示
 → 再次等待 tui-idle（最长 2 分钟）
 → 从输出中提取 HANDOFF_*.md 文件名
 → 向调用方终端报告
```

## 目录布局

### 项目结构

```
orca-osw/
  skill/
    SKILL.md          # Codex skill 定义
    osw.py            # 入口
    osw/
      cli.py          # typer CLI 命令
      deps.py         # 依赖检查
      handoff.py      # 交接提取与报告
      orca_cli.py     # 异步 Orca CLI 包装
      watcher.py      # detached 完成状态 watcher
      state.py        # 状态模型与文件 I/O
  tests/              # 62 个测试（单元 + 集成）
  conftest.py         # 测试用 sys.path 配置
```

### 运行时状态

在运行 `init` 的工作目录下创建：

```
<当前目录>/
  .orca/
    osw/
      state.json      # OSW 项目状态
      agents/          # 每个受管 agent 一个 JSON 记录
      handoffs/        # worker 写入的交接 markdown
      logs/
      reports/
```

## 状态模型

```json
{
  "version": 3,
  "project_root": "D:\\project"
}
```

运行时 agent 记录位于 `.orca/osw/agents/`。模型发现是只读的；`models <provider>` 不写入 `state.json`。

## 测试

```bash
python -m pip install pytest
python -m pytest -v
```

共 68 个测试，覆盖每个模块的单元测试和基于 mock Orca CLI 的集成测试。

## 许可

MIT
