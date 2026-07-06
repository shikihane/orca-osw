# OSW — Orca Agent Supervisor

[English](README.md)

轻量 Python 工具，用于在当前目录管理 Orca 支持的 agent 会话。OSW 启动或接管 agent 终端，运行前台 AnyIO 监督器，通过空闲信号检测任务完成，强制执行交接流程，并将生成的交接文件报告给调用方。

## 特性

- **目录隔离** — 每个工作目录是独立的管理范围，即使跨 git worktree 也互不影响
- **异步监督器** — 基于 AnyIO 的并发架构；收件箱轮询、逐 agent 观察者、状态协调互不阻塞
- **自动交接** — 检测 agent 空闲后自动触发 `/handoff`，提取 `HANDOFF_*.md` 文件名，通知调用方终端
- **最少依赖** — 仅需 `anyio` 和 `typer`

## 环境要求

- Python 3.10+
- [Orca](https://orca.dev) 已运行且 `orca` CLI 在 PATH 中
- `anyio` 和 `typer`：

```
python -m pip install anyio typer
```

## 快速开始

详见 [QUICK_zh.md](QUICK_zh.md)。

```bash
python skill/osw.py init
python skill/osw.py serve          # 保持在此终端运行

# 在另一个终端，同一目录：
python skill/osw.py new "修复失败的测试"
python skill/osw.py list
python skill/osw.py status
```

## 命令

| 命令 | 说明 |
|---|---|
| `init` | 初始化 `.orca/osw/` 状态目录 |
| `serve` | 运行前台监督器（其他命令依赖此进程） |
| `new "<提示>"` | 创建新的 agent 终端并发送任务 |
| `use --terminal <句柄> "<提示>"` | 接管已有的 Orca 终端 |
| `all "<消息>"` | 向所有受管 agent 广播消息 |
| `del <agent_id> [--close]` | 移除 agent |
| `list [--json]` | 列出受管 agent |
| `status` | 显示监督器状态和 agent 统计 |

### 选项

- `new` 和 `use` 支持 `--caller-terminal <句柄>`，指定接收完成报告的父终端。
- `del --close` 同时让 Orca 关闭终端。
- `list --json` 输出原始 JSON。

## 架构

```
serve（前台）
  ├── inbox_loop      — 每 0.5 秒轮询 .orca/osw/inbox/ 处理 CLI 请求
  ├── reconcile_loop  — 每 30 秒验证终端是否仍然存在
  ├── watcher(agent_001)  — 逐 agent 动态生成
  ├── watcher(agent_002)
  └── ...
```

只有 `serve` 写入 `state.json`。CLI 命令（`new`、`use`、`all`、`del`）通过向 `inbox/` 写入 JSON 请求文件、轮询 `results/` 等待响应（15 秒超时）与 `serve` 通信。

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
      server.py       # AnyIO 监督器
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
      state.json      # 权威状态文件（仅 serve 写入）
      inbox/           # CLI → serve 请求文件
      results/         # serve → CLI 响应文件
      logs/
```

## 状态模型

```json
{
  "version": 1,
  "project_root": "D:\\project",
  "serve": { "pid": 12345, "started_at": "..." },
  "models": {
    "strong":  [{ "name": "strong-default",  "command": "codex" }],
    "medium":  [{ "name": "medium-default",  "command": "pi" }],
    "weak":    [{ "name": "weak-default",    "command": "pi" }]
  },
  "agents": {},
  "errors": []
}
```

编辑 `state.json` 中的 `models` 配置各强度等级对应的提供商命令。OSW 创建新终端时使用第一个 `strong` 条目。

## 测试

```bash
python -m pip install pytest
python -m pytest -v
```

共 62 个测试，覆盖每个模块的单元测试和基于 mock Orca CLI 的集成测试。

## 许可

MIT
