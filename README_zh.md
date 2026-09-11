# OSW — Orca Agent Supervisor

[English](README.md)

轻量 Python 工具，用于在当前目录管理 Orca 支持的 agent 会话。OSW 启动或接管 agent 终端，启动 detached watcher，通过 Orca 原生 agent 状态检测任务完成（空闲信号作为回退），强制执行交接流程，并将生成的交接文件报告给调用方。

## 特性

- **目录隔离** — 每个工作目录是独立的管理范围，即使跨 git worktree 也互不影响
- **Detached watcher** — 每个受管 agent 都有独立 watcher 进程，监控完成状态且不阻塞 CLI
- **自动交接** — 检测任务完成后发送固定的收尾指令（含预分配的交接文件路径），校验交接 markdown 已写入，通知调用方终端
- **兼容性防线** — 检查 Orca 实时 JSON 契约，并在 Orca 重启后恢复运行时级终端句柄
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
python skill/osw.py doctor
python skill/osw.py init
python skill/osw.py models pi
python skill/osw.py models omp

python skill/osw.py new claude --prefix code --model sonnet --thinking high "修复失败的测试"
python skill/osw.py new codex --prefix research --model gpt-5 --thinking medium "重构 provider 层"
python skill/osw.py list
python skill/osw.py status
```

## 命令

| 命令 | 说明 |
|---|---|
| `doctor [--json]` | 只读检查 Orca 运行状态及 OSW 依赖的实时响应契约 |
| `init` | 初始化 `.orca/osw/` 状态目录 |
| `models <provider> [--json]` | 只读查看 provider 模型选项 |
| `new <provider> [--prefix <prefix>] [--model <model>] [--thinking <值>] "<提示>"` | 创建新的 agent 终端并发送任务 |
| `use <agent-id-or-terminal> [--prefix <prefix>] "<提示>"` | 接管已有 Orca 终端，或通过 OSW agent id 继续 |
| `all "<消息>"` | 向所有受管 agent 广播消息 |
| `del <agent_id> [--close]` | 移除 agent |
| `list [--json]` | 列出受管 agent |
| `logs [--agent <agent_id>] [--tail N]` | 查看 OSW 日志和事件 |
| `status` | 显示 OSW 状态和 agent 统计 |

### 选项

- `new` 的 provider 支持 `claude`、`codex`、`pi`、`omp`、`kimi`、`grok`。
- `--prefix` 生成语义化 agent id，例如 `research_001`、`code_001`、`test_001`、`debug_001`、`review_001`、`misc_001`；省略时使用 `agent_NNN`。
- `use <agent-id>` 会复用这个确切的 agent id 和终端上下文；`--prefix` 只用于 `new` 或 `use <terminal-handle>`。
- `--model` 原样传给 provider。
- `--thinking` 按 provider 转换：`claude --effort`、`codex -c model_reasoning_effort=...`、`pi`/`omp --thinking`、`grok --reasoning-effort`；`kimi` 的 effort 位于模型配置中，因此会拒绝该参数。
- `models omp` 读取 `omp models --json`；将其中的 `provider/model` selector 传给 `new omp --model`。
- OSW 默认添加 provider 的非阻塞/批准类启动参数：`claude --dangerously-skip-permissions`、`codex --dangerously-bypass-approvals-and-sandbox`、`pi --approve`、`omp --auto-approve`、`kimi --yolo`、`grok --always-approve --trust`。
- `new` 和 `use` 支持 `--caller-terminal <句柄>`，指定接收完成报告的父终端。
- `del --close` 同时让 Orca 关闭终端。
- `list --json` 输出原始 JSON。

## Orca 兼容性

安装或升级 Orca 后先运行 `python skill/osw.py doctor`。它会检查 OSW
实际使用的 `status`、`worktree current`、`terminal list`、`terminal show`
和 `worktree ps` 的响应结构与字段。契约不匹配时命令以非零状态退出；
`--json` 会输出可供自动化识别的 `orca_contract_mismatch` 错误码。没有活动
终端或 Orca 尚未识别出 agent 时，相应检查会标记为 `unverified`。

OSW 优先使用 `ORCA_CLI_COMMAND`（可包含参数）；设置
`ORCA_DEV_REPO_ROOT` 时选择 `orca-dev`；Linux 上不在 Orca 托管终端内时
选择 `orca-ide`；其余情况从 PATH 解析 `orca`。

Orca 的终端 handle 只在当前 runtime 内有效。OSW 在创建或接管终端时持久化
面板身份（`tabId:leafId`），Orca 重启后可据此重新绑定失效的 handle。

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
等待面板就绪（`worktree ps` 报告 done；未识别 CLI 回退到 tui-idle）
 → 发送任务提示
 → 等待 Orca 报告该轮完成（`worktree ps`，与发送时间相关联；未识别 CLI 回退到空闲检测）
 → 发送固定收尾指令，附带预分配的交接文件路径
 → 再次等待，校验 .orca/osw/handoffs/<agent>_<ts>.md 已写入（重试一次）
 → 写入 JSON 报告并通知调用方终端
```

完成提醒由 OSW watcher 通过 `orca terminal send` 主动发给调用方终端，
并不是 Orca 原生的跨会话通知。要自动送达，必须成功检测到调用方终端，或
显式传入 `--caller-terminal`。

`new` 只启动裸 provider TUI——任务提示始终由 watcher 作为独立一轮发送，
因此启动瞬间的 TUI 空闲状态不可能被误判为任务完成。

## 目录布局

### 项目结构

```
orca-osw/
  skill/
    SKILL.md          # skill 定义
    references/       # 编排与运维指南
    osw.py            # 入口
    osw/
      cli.py          # typer CLI 命令
      deps.py         # 依赖检查
      log.py          # 结构化事件与文件日志
      orca_cli.py     # 异步 Orca CLI 包装
      process.py      # 隐藏子进程窗口辅助（Windows）
      providers.py    # provider 命令构造与模型发现
      state.py        # 状态模型与文件 I/O
      watcher.py      # detached 完成状态 watcher
  tests/              # 单元与集成测试
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

测试覆盖各模块，并通过 mock Orca CLI 运行集成测试。

## 许可

MIT
