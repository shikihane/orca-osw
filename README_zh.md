# OSW — Orca Agent Supervisor

[English](README.md)

轻量 Python 工具，用于在当前目录管理 Orca 支持的 agent 会话。OSW 启动或接管 agent 终端，启动 detached watcher，通过 Orca 原生 agent 状态检测任务完成（空闲信号作为回退），强制执行交接流程，并将生成的交接文件报告给调用方。

## 特性

- **目录隔离** — 每个工作目录是独立的管理范围，即使跨 git worktree 也互不影响
- **Detached watcher** — 每个受管 agent 都有独立 watcher 进程，监控完成状态且不阻塞 CLI
- **自动交接** — 检测任务完成后发送固定的收尾指令（含预分配的交接文件路径），校验交接 markdown 已写入，通知调用方终端
- **受监督的 omp worker** — `new omp` 走 Orca 的受监督编排：Orca 创建 agent 终端并注入任务，worker 自行上报 `worker_done`，完成判定跟随 Dispatch 生命周期而不是终端输出
- **接管与恢复** — `enroll` 登记不是 OSW 启动的 agent 终端，`guard` 让你成为它们的通知对象并跟踪进行中的一轮，`revive` 在 Orca 或 PC 重启后重新拉起 provider 会话，`stop` 中断当前一轮
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
| `enroll` | 登记当前 worktree 中不是 OSW 启动的 agent 终端（`enrolled`，无 watcher） |
| `guard [<agent_id>...] [--caller-terminal <句柄>]` | 成为 agent 的监护方（通知对象）；跟踪进行中的一轮 |
| `revive [<agent_id> [--session <id>]]` | 列出可恢复的 agent，或在新终端中恢复某个 agent 的 provider 会话 |
| `stop <agent_id>` | 中断 agent 当前这一轮 |
| `del <agent_id> [--close]` | 移除 agent |
| `list [--json]` | 列出受管 agent |
| `logs [--agent <agent_id>] [--tail N]` | 查看 OSW 日志和事件 |
| `status` | 显示 OSW 状态和 agent 统计 |
| `jev status [--check]` | 显示全局 Jev 开关、key/SDK/endpoint/代理状态；`--check` 实时探测端点 |
| — | **模式就是全局开关**：机器级环境变量 `OSW_JEV_MODE`（`enforce\|shadow\|off`；未设置 = `off`）是唯一控制——不再有项目级 Jev 配置 |

### 选项

- `new` 的 provider 支持 `claude`、`codex`、`pi`、`omp`、`kimi`、`grok`。
- `--prefix` 生成语义化 agent id，例如 `research_001`、`code_001`、`test_001`、`debug_001`、`review_001`、`misc_001`；省略时使用 `agent_NNN`。
- `use <agent-id>` 会复用这个确切的 agent id 和终端上下文；`--prefix` 只用于 `new` 或 `use <terminal-handle>`。
- `--model` 原样传给 provider。
- `--thinking` 按 provider 转换：`claude --effort`、`codex -c model_reasoning_effort=...`、`pi --thinking`、`grok --reasoning-effort`；`kimi` 的 effort 位于模型配置中，因此会拒绝该参数；`omp` 的受监督发射通道无法表达 effort，因此同样拒绝。
- `models omp` 读取 `omp models --json`；将其中的 `provider/model` selector 传给 `new omp --model`（作为 `--model` 转发给受监督发射）。
- `new omp` 以受监督 Orca worker 运行：OSW 先把调用方终端绑定到项目的编排 Run（`state.json: orchestration_run_id`），再通过 `orca orchestration worker-start --agent omp` 发射——Orca 负责建终端、等就绪并注入任务 spec 本身。因此 `omp` 的任务轮不由 watcher 发送。OSW 使用比 Orca 默认 60s 更宽的就绪预算（`--timeout-ms 240000`）；若仍然失败，会报告失败阶段（`failedStage`/`lastError`）、释放 Orca 遗留的孤儿终端，并清掉占位 agent 记录。
- OSW 默认添加 provider 的非阻塞/批准类启动参数：`claude --dangerously-skip-permissions`、`codex --dangerously-bypass-approvals-and-sandbox`、`pi --approve`、`omp --auto-approve`、`kimi --yolo`、`grok --always-approve --trust`。`omp` 记录的是等价的手动启动命令；受监督 worker 的实际 argv 跟随 Orca 自己的 agent 标签页设置。
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
因此启动瞬间的 TUI 空闲状态不可能被误判为任务完成。唯一例外是受监督的
`omp`：任务 spec 在发射时由 Orca 注入。

### 受监督的 omp worker

`new omp` 不再创建裸终端。OSW 先把调用方终端绑定到项目的编排 Run，再通过
`orca orchestration worker-start --agent omp` 发射受监督 worker。Dispatch
生命周期是**既不依赖 `worktree ps`、也不依赖终端静默**的完成信号。手动启动的
omp 在完成第一轮之后才会被 `worktree ps` 追踪；而且每轮结束时 Orca 会因 OSC
133;D（被当作"命令结束"）暂时丢弃该面板的状态上报，要靠本机扩展
`~/.omp/agent/extensions/orca-status-watchdog.ts` 约 2 秒后补回，没有这个扩展时
omp 的追踪不可靠。omp 状态栏还约每秒重绘一次，任何"输出安静"判据都会永远等待。

```
Orca 等终端就绪并注入任务 spec（spec 自带交接/报告契约）
 → worker 完成任务、写出交接 markdown，并自行发送
   `worker_done --report-path <handoff>`
 → watcher 轮询 `orca orchestration worker-show` 直到 Dispatch 结算，然后校验
   交接文件（worker 未按契约执行时，回退发送标准收尾指令补齐）
 → 报告记录 `orchestration_outcome`；worker 终端以 `worker-retain` 保留；通知调用方
```

`del --close` 对已结算的受监督 worker 执行 `worker-release`，对仍在运行的
执行 `worker-stop`；若调用方终端已绑定到其他 Run，会被重新绑定并记录
`orchestration_run_rebound` 事件。

## 接管与恢复

```bash
python skill/osw.py enroll                       # 登记未受管的 agent 终端
python skill/osw.py guard                        # 成为它们的监护方
python skill/osw.py revive                       # 列出可恢复的会话（JSON）
python skill/osw.py revive code_001              # 重启后恢复某个 agent
python skill/osw.py stop code_001                # 中断当前一轮
```

- **`enroll`** 扫描当前 worktree 的 `terminal list`，跳过执行者自己的终端和已有记录的面板，provider 取 `agentIdentity`（没有则取 ps 的 `agentType`），每个面板写一条 `enrolled` 记录，`origin: enroll`。OSW 无法启动的 provider 记为 `support: guard_only`（`support_reason: unsupported_provider`）。输出 `{"enrolled": [...], "skipped": [{terminal, reason}]}`。
- **`guard [<id>...]`** 让执行者（`--caller-terminal`，否则 `ORCA_TERMINAL_HANDLE`）成为监护方；不带 id 时监护所有未被监护的 `enrolled`/`done`/`error` agent。有存活 watcher 时先终止它，再以跟踪方式重启；没有 watcher 但 ps 报 `working` 的面板会起一个跟踪 watcher；空闲 agent 只记录监护方。被跟踪的一轮结束后照常报告，但不发 handoff（watcher 接手时这一轮已结束则为 `already_done`）。`use` 未传 `--caller-terminal` 时继续通知监护方。
- **`revive`** 不带 id 时输出 `{"revivable": [...], "not_revivable": [{agent_id, reason}]}`。`revive <id> [--session <sid>]` 要求 `support: full`、状态为 `lost`/`done`/`error`/`enrolled` 且没有存活 watcher。会话取 `--session`，否则取记录的 `session_id`，否则取该面板当前的会话；按 provider 的恢复语法（附带原有的自主权参数和模型参数）重新打开，agent id 不变。原面板确认是空 shell 时——仍在列表中、没有 `agentIdentity`、ps 条目不是 `working`、是 OSW 能转义的 shell（pwsh/PowerShell、cmd、POSIX）、且 Jev `ready_state` 判为 `shell_prompt`——直接在原面板输入恢复命令，`pane_key` 不变（`placement: in_place`）；否则（包括 `OSW_JEV_MODE=off`）新建终端（`placement: new_terminal`）。输出和 `agent_revived` 事件都带 `placement`。`lost`（中途被打断）的 agent 以 `resume_turn: true` 恢复：watcher 发送 `continue`，随后照常完成检测并发 handoff；其余情况恢复为 `enrolled`。
- **`stop <id>`** 对 claude、codex、kimi、grok 发 Ctrl-C（`terminal send --interrupt`），对 pi、omp 发单个 Esc。有存活 watcher 时写旁路文件 `.orca/osw/agents/<id>.stop`；watcher 在 ps 报告该轮被中断或 done、或面板静默 5 秒后结束这一轮，记为 `error`、`completion_source: interrupted`，跳过 handoff，通知监护方并删除旁路文件。输出 `{"agent_id", "key", "watcher"}`。

记录新增字段（均可选，旧记录兼容）：

| 字段 | 取值 |
|---|---|
| `origin` | `new`、`use`、`enroll` |
| `session_id` | provider 会话 id |
| `guardian` | 监护方是 OSW agent 时为 `{agent_id}`（通知时从它的记录读取当前终端），否则为 `{terminal, pane_key}` |
| `support` | `full`，或 `guard_only` 并带 `support_reason: unsupported_provider` |
| `follow_turn` | watcher 接手进行中的一轮；结束后清除 |
| `resume_turn` | watcher 的任务提示为 `continue`；结束后清除 |
| `notified_asks` | 已转给监护方的 supervised worker 提问 id |

新增状态：`enrolled`（已登记，无 watcher）和 `lost`（非终态且 watcher 进程已不存在，由 `list`/`status`/`use`/`revive` 标记）。新增完成来源：`already_done` 和 `interrupted`。

每次发送通知前都会重新解析通知对象：有监护方时用监护方，否则用调用方终端。监护方 agent 的记录读不到时回退到调用方终端（记录 `guardian_unresolved` 事件）；监护方 handle 失效时按面板重新绑定一次并重试一次。

会话 id 来自 Orca 的 agent-hook 缓存 `%APPDATA%\orca\agent-hooks\last-status.json`（本机为 `C:\Users\shiki\AppData\Roaming\orca\agent-hooks\last-status.json`）。它不是 Orca 的公开接口：OSW 只在 `version` 匹配（2）时读取，格式变化时会话 id 为空。

OSW 自己只会向终端输入四类内容：收尾指令 `watcher.HANDOFF_TEMPLATE`、恢复提示 `watcher.RESUME_TEMPLATE`（`continue`）、白名单内的弹窗按键（`osw.blockers.WHITELIST`），以及 `revive` 向确认为空的 shell 输入的恢复命令。

已知限制：空闲的 `omp` 经 `revive` 恢复后没有 Dispatch，下一次 `use` 才会建立。`omp` 被 `stop` 后请用 `revive` 续接；Esc 中断后能否重新挂上 Orca 追踪尚未验证。

### 弹窗与阻塞处理

弹窗占住 agent 的输入时，watcher 先发现、再归类，只在确认安全的情况下才操作：

- **发现**：优先用 Orca 的 `agentWait.reason`（`agent_prompt_blocked` 发送错误会带上，如 `agent-update-prompt`）；没有时用 Jev `ready_state` 的 `blocked_dialog` 判断。
- **归类**：用 Orca 给出的类型，否则用 Jev 问题 `blocker_kind`（`update_prompt`、`trust_folder`、`auth`、`permission`、`announcement`、`rate_limit`、`crashed_to_shell`、`other`；概率低于 0.8 记为 `other`）。
- **模式**（`OSW_JEV_MODE`）：`off` 只发现和通知；`shadow` 记录拟执行的按键（`blocker_shadow_action`），并像 `off` 一样通知；`enforce` 执行白名单按键。
- **白名单**：只有 codex 的更新提示：Enter 选默认项"1. Update now"。Orca 会拒绝向这个弹窗的普通发送，所以 OSW 用已验证能穿透的 `terminal send --text "" --enter --interrupt`。每项都需要 `tests/fixtures/frames/` 下的画面样例；codex 样例是文字转录，不是原始抓帧。
- **禁止自动处理**：`auth`、`permission`，以及任何涉及删除或付费的画面。
- **更新锁**：同一 provider 在本机同一时间只允许一个面板执行更新（`%LOCALAPPDATA%\osw\locks\update-<provider>.lock`，15 分钟过期，防止持有者崩溃后死锁）；拿不到锁的面板通知一次后继续等待。
- **操作之后**：重新判断画面，仍被阻塞则停止自动处理并通知。被拒绝的发送会在面板重新就绪后重试（最多 5 次）；一直被拦则以 `completion_source: blocked` 结束。codex 更新后会退回 shell，所以操作过按键后，单凭输出静默不算就绪。

每个阻塞只给监护方发一条 `# [osw] blocked agent=<id> kind=<类型> terminal=<句柄> screen=<画面摘要>`；每一步也都记录为 `blocker_*` 事件。

### 无工具调用的轮次

watcher 记录每一轮在 `working` 期间 `worktree ps` 是否报告过工具调用（`toolName`）；`done` 条目上的 `toolName` 可能是上一轮留下的，不计入。ps 追踪到了、但从未出现工具调用的任务轮（包括被接手的那一轮），report 写 `tools_observed: false`，`task-finished` 通知行追加 `warning=no_tool_activity`。状态仍为 `done`，不会自动重发；agent 可能只是用文字作答，请核对 handoff 和工作区。看到工具调用时为 `true`，ps 从未追踪该面板时为 `null`。

如果 `worktree ps` 显示 agent 收到的 prompt 是从已发送文本的中间开始的，说明 prompt 开头在传输中丢了。watcher 会立即发出 `# [osw] prompt-truncated ... received_from=<n>`，report 写入 `prompt_truncated_at` 和 `warning=prompt_truncated`。不会自动重发，请重新派发任务。发给 Claude 的 prompt 以 bracketed paste 形式写入，否则 Windows 上的 Claude Code 只保留第 2048 个字符之后的内容。

### worker 提问转发

supervised worker 未回复的编排消息（`orchestration ask` 产生的 `question` 类型，以及 `escalation`）会按消息 id 各转给监护方一次：`# [osw] ask agent=<id> message=<消息 id> reply="orca orchestration reply --id <消息 id> --body <文本>" text=<问题>`；已转发的 id 记录在 `notified_asks`。Windows 上 `orca.cmd` 不允许执行 `orchestration reply`，请改用 Orca 安装目录下的 `resources\bin\orca.exe`。

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
      sessions.py     # 从 Orca agent-hook 缓存读取 provider 会话 id
      blockers.py     # 阻塞画面的发现、白名单与更新锁
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
      agents/          # 每个受管 agent 一个 JSON 记录（以及 <id>.stop 停止请求）
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
