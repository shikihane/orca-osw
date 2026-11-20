# OSW — Orca Agent Supervisor

[English](README.md)

轻量 Python 工具，用于在当前目录管理 Orca 支持的 agent 会话。OSW 启动或接管 agent 终端，启动 detached watcher，通过 Orca 原生 agent 状态检测任务完成（空闲信号作为回退），强制执行交接流程，并将生成的交接文件报告给调用方。

## 特性

- **目录隔离** — 每个工作目录是独立的管理范围，即使跨 git worktree 也互不影响
- **Detached watcher** — 每个受管 agent 都有独立 watcher 进程，监控完成状态且不阻塞 CLI
- **自动交接** — 检测任务完成后发送固定的收尾指令（含预分配的交接文件路径），校验交接 markdown 已写入，通知调用方终端
- **完成通知可追溯** — 投递成功的通知记录为 `notify_delivered` 事件，没找到调用方的通知留在 `osw inbox` 里等待，不再只存在于日志中
- **omp 标题态就绪与完成** — omp 从不进入输出空闲，Orca 也不为它发 `tui-idle`，因此 OSW 读取终端标题（前面有旋转符表示本轮正在运行）作为就绪判据与本轮完成判据
- **接管与恢复** — `enroll` 登记不是 OSW 启动的 agent 终端，`guard` 让你成为它们的通知对象并跟踪进行中的一轮，`revive` 在 Orca 或 PC 重启后重新拉起 provider 会话，`stop` 中断当前一轮
- **跨主机监管** — 机器登记一次，用 `--on <host>` 派单，交接文件落到本地：Orca 配对是派单面，SSH 是数据面，远端不需要安装 OSW
- **兼容性防线** — 检查 Orca 实时 JSON 契约，并在 Orca 重启后恢复运行时级终端句柄
- **最少依赖** — 仅需 `anyio`、`typer`，可选 `rich` 获得彩色日志

## 环境要求

- Python 3.10+
- [Orca](https://orca.dev) 已运行且 `orca` CLI 在 PATH 中
- `anyio` 和 `typer`：

```
python -m pip install anyio typer rich
```

只有远程 agent 需要：本机的 `ssh` 与 `sftp`、对方机器可达的 `sshd`，以及本机密钥已被该账户授权。

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
| `host add <name> --ssh <dest> --worktree <selector> [--environment <sel>] [--root <path>]` | 登记一台机器；不探测、不在远端写入 |
| `host list [--json]` | 查看已登记主机及一次性可达性 |
| `host rm <name>` | 删除主机记录，并列出仍引用它的 agent |
| `host doctor <host> [--provider <provider>] [--json]` | 按 connection → workspace → worktree → provider 顺序探测，首个失败即停止 |
| `new <provider> [--on <host>] [--prefix <prefix>] [--model <model>] [--thinking <值>] "<提示>"` | 创建新的 agent 终端并发送任务 |
| `use <agent-id-or-terminal> [--on <host>] [--prefix <prefix>] "<提示>"` | 接管已有 Orca 终端，或通过 OSW agent id 继续 |
| `enroll [--on <host>]` | 登记当前 worktree 中不是 OSW 启动的 agent 终端（`enrolled`，无 watcher） |
| `guard [<agent_id>...] [--caller-terminal <句柄>]` | 成为 agent 的监护方（通知对象）；跟踪进行中的一轮 |
| `revive [<agent_id> [--session <id>]]` | 列出可恢复的 agent，或在新终端中恢复某个 agent 的 provider 会话 |
| `stop <agent_id>` | 中断 agent 当前这一轮 |
| `del <agent_id> [--close]` | 移除 agent |
| `sweep [--force] [--json]` | 关闭已完成 agent 的终端；记录与会话保留 |
| `list [--all] [--json]` | 列出受管 agent |
| `logs [--agent <agent_id>] [--tail N]` | 查看 OSW 日志和事件 |
| `status [--all]` | 显示 OSW 状态、agent 统计与未读完成通知 |
| `inbox [--json] [--ack] [--limit N]` | 列出待投递的完成通知（最新在前）；`--ack` 把列出的通知标记为已读 |
| `mailbox start [--json]` | 启动本地邮箱守护进程（仅控制平面）；重复 `start` 视为成功 |
| `mailbox stop [--json]` | 停止守护进程并等待退出 |
| `mailbox status [--json]` | 各队列积压、最旧消息年龄、spool 滞后、门铃状态与死信 |
| `mailbox send --kind <kind> --text "<文本>" [--to-role <角色>] [--ticket <id>] [--thread-key <键>] [--json]` | 向 spool 写入一条信封（默认角色 human → router） |
| `mailbox wait [--as router\|human] [--timeout <秒>] [--max <n>] [--json]` | 为该角色租约并确认决策卡片；`--timeout 0` 为单次扫描 |
| `mailbox show <消息 id> [--json]` | 查看单条消息的信封、卡片与 Ticket |
| `mailbox trace <消息 id 或 Ticket> [--json]` | 消息 → Ticket → 决策 → 效果 → 发件箱 |
| `mailbox decide <消息 id> --action <动作> --reason "<原因>" [--ticket <id>] [--payload-json <json>] [--json]` | 校验并提交决策；输出其 `decision_id` |
| `mailbox tickets [--json]`、`mailbox ticket <id> [--json]` | 派生的 Ticket 索引，以及单个 Ticket |
| `jev status [--check]` | 显示全局 Jev 开关、key/SDK/endpoint/代理状态；`--check` 实时探测端点 |
| — | **模式就是全局开关**：机器级环境变量 `OSW_JEV_MODE`（`enforce\|shadow\|off`；未设置 = `off`）是唯一控制——不再有项目级 Jev 配置 |

### 选项

- `new` 的 provider 支持 `claude`、`codex`、`pi`、`omp`、`kimi`、`grok`、`dsh`。
- `--prefix` 生成语义化 agent id，例如 `research_001`、`code_001`、`test_001`、`debug_001`、`review_001`、`misc_001`；省略时使用 `agent_NNN`。
- `use <agent-id>` 会复用这个确切的 agent id 和终端上下文；`--prefix` 只用于 `new` 或 `use <terminal-handle>`。
- `new`、`use`、`enroll` 的 `--on <host>` 指向已登记主机，默认是本机。
- agent 引用形式是 `<host>/<agent_id>`：`fnnas/code_001` 指该主机上的 agent，`code_001` 与 `local/code_001` 都指本机。未登记的前缀会被拒绝，并在报错里列出已登记主机——打错字不会把任务派到别的机器。
- `--model` 原样传给 provider。
- `--thinking` 按 provider 转换：`claude --effort`、`codex -c model_reasoning_effort=...`、`pi`/`omp --thinking`、`grok --reasoning-effort`；`kimi` 的 effort 位于模型配置中，因此会拒绝该参数；`dsh` 的 TUI 入口既没有 model 也没有 effort 参数，因此 `--model` 与 `--thinking` 都会被拒绝。
- `models omp` 读取 `omp models --json`；将其中的 `provider/model` selector 传给 `new omp --model`（作为 `--model` 转发给 TUI）。
- `new omp` 与其他 provider 一样，在 Orca 终端里启动 `omp --auto-approve`：标题前面没有旋转符且输入框状态栏已画出时视为就绪，任务提示由 watcher 作为独立一轮发送，Orca 报告该面板 `done` 且标题保持空闲时视为本轮完成。这两个信号都不可能来自"输出安静"——omp 状态栏持续重绘——也不再走受监督的 `orca orchestration worker-start --agent omp` 通道：Orca 1.4.246 在那里等待自己的 `tui-idle` 就绪信号，而 omp 面板永远不会发出（实测：120s 与 240s 两种就绪预算下都停在 `agent_readiness` 失败，此时 TUI 已停在输入框）。
- `new dsh` 用 `dsh-tui .` 启动 DeepSeek Harness TUI——正是 Orca 自己启动并识别为 agent `dsh` 的入口。DSH 的启动命令没有 model、effort 或批准参数：这些都在 DSH 设置里，并在 TUI 内用 `/model`、`/permission` 改变，因此 `--model`/`--thinking` 直接拒绝而不是静默丢弃，OSW 也不添加自主权参数。DSH 自己的启动提示（`正在启动 DSH…` / `Starting DSH…`）还在屏幕上时，就绪会被推迟：launchpad 在 harness 起来之前就画出输入框，此时发出的 prompt 会被丢弃。Orca 用终端标题（`✦ 🐋 <session>`）识别该面板，working/done 取自 DSH-TUI 的 Program Status Protocol 上报；两者都不报告工具名，因此报告写 `tools_observed: null`，而不是断言“没有工具调用”。装好 DSH 的 Orca hook 桥（`@deepseek-ai/dsh-hooks-claude-code` 及其运行时 peer，装进 `dsh-tui` profile）后，OSW 会在就绪等待阶段就记录会话 id——赶在第一回合的 Program Status 上报接管该面板记录之前；没装桥时 `revive` 需要 `--session <id>`。
- OSW 默认添加 provider 的非阻塞/批准类启动参数：`claude --dangerously-skip-permissions`、`codex --dangerously-bypass-approvals-and-sandbox`、`pi --approve`、`omp --auto-approve`、`kimi --yolo`、`grok --always-approve --trust`。
- `new` 和 `use` 支持 `--caller-terminal <句柄>`，指定接收完成报告的父终端。在 Orca 对话（结构化 agent 会话，没有终端句柄）里运行时，调用方取自注入的 `ORCA_AGENT_SESSION_ID`，记为 `caller_session: orca_session_id:<id>`；watcher 通过 `orca orchestration send` 投递，Orca 会把它作为用户轮注入该对话。
- `del --close` 同时让 Orca 关闭终端。
- **已完成的一轮会保留自己的终端。** pane 故意留在屏幕上，结果就停在调用方能读到的地方：完成通知行末尾带 `cleanup=manual close=osw del <agent_id> --close`，关掉它是调用方的显式一步。记录和 `session_id` 保留，pane 关掉之后 `revive <agent_id>` 可在新终端恢复该会话。单个 agent 用 `osw del <agent_id> --close` 关闭；已完成的面板批量关闭用 `osw sweep`（失败的一轮 `error`/`lost` 只在加 `--force` 时关闭——那个 pane 正是还能读到失败原因的地方），它会打印保留了哪些、为什么。`del <agent_id> --close` 是单个 agent 的显式关闭，不论它来自哪里；`sweep` 走的是共享策略，只关 OSW 自己创建的面板，接管的（`use`/`enroll`）、supervised 派发终端（归 Orca 管）、人工重开的一轮（`guard`/`revive`）都会被保留，需要时用 `del --close` 手动关。
- `list --json` 输出原始 JSON。

## 完成通知

完成通知是可追溯的。投递成功后 watcher 追加一条 `notify_delivered` 事件，
携带实际发出的 `line` 与 `channel`（`terminal` 或 `session`）；某个 agent
只要存在一条该类事件，或一条来自 Mailbox 渠道的
`completion_notify_suppressed` 事件，且时间戳不早于其 `finished_at`，就算作
已投递。没找到调用方的通知留在待投递列表里，而不是只躺在事件日志中。

```bash
python skill/osw.py inbox                # 待投递通知，最新在前
python skill/osw.py inbox --limit 20     # 限制条数
python skill/osw.py inbox --json         # 输出 {count, pending}，供脚本使用
python skill/osw.py inbox --ack          # 把列出的通知标记为已读
```

有通知待投递时，`osw status` 报告 `Unread completions: N (osw inbox)`。
投递不借助其他命令重试：`new`、`use`、`status` 都不碰待投递通知，因此只在
等某一个 agent 的调用方会一直等到它自己的结果。`osw inbox` 就是不打乱这个
调用方的查看方式。

通知文本优先采用投递被跳过或失败时记录的历史 `line`——那条记录下来的完成
行以 `cleanup=manual close=osw del <agent_id> --close` 结尾；没有历史文本时，
按 `format_completion_report` 的形状，从 agent 记录和最新报告生成。已读状态
保存在 `.orca/osw/notify/acked.json`，键为 `<agent_id>@<finished_at>`；
该文件只由 ack 与 reconcile 路径写入，agent 记录仍只有 watcher 一个写入者。
该功能上线前已完成的记录会一次性归入 `notify/legacy_reconciled.json`，
因此待投递列表只列出真正的缺口。

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

## 跨主机运行

OSW 可以监管终端位于另一台机器上的 agent。机器登记一次，之后按名字寻址。

### 两个面，一条缝

- **派单面** — Orca 的配对连接（`--environment <选择器>`）。它只负责在远端创建终端：`terminal create --environment <host> --worktree <选择器> --command "<provider 绝对路径> <自主权参数>"`。就绪判定、注入提示、轮次生命周期和交接文件都由本机 watcher 负责——与本地 agent 完全相同的经典通道。远端 agent **没有 Dispatch id**，Run 留在本机，远端用不到。
- **数据面** — SSH：`ssh` 执行命令，`sftp` 传文件字节（读取、先写临时名再改名、存在性检查）。不靠抓取终端搬运字节，因此远端交接文件的读取保真度与本地一致。

**为什么不用 Orca 的受监督通道（实测 2026-11-04，Orca 1.4.248）。** 配对服务器上的就绪判定是 `host_indeterminate`：`orchestration worker-start --on <host>` 停在 `agent_readiness` 达 240s，而远端面板明明停在输入框；对同一个空闲面板执行 `terminal wait --for tui-idle` 同样超时。`terminal send`、`terminal read`、`worktree ps` 则都有应答。远端没有可等的就绪信号，因此面板必须由本机创建并驱动。除非有新的实测证明远端会发出就绪信号，否则不要为远端重新启用受监督通道。

两个面、主机注册表、agent 引用解析和错误映射都集中在 `skill/osw/hosts.py`。防止设计腐化的那条规则是：**该模块之外，任何代码都不得就另一台机器与 Orca 对话，也不得直接读写那台机器上的文件。** 本机调用与单机安装逐字节一致，因此不点名主机时这条缝不可见。

### 远端机器上的前置条件

- 本机可达该机的 `sshd`，且本机密钥已被该账户授权。密钥被拒会以 `host_permission_denied` 出现；去执行 `ssh-copy-id`，不要怀疑 OSW。
- 那台机器的 Orca 里已经登记了 workspace，并且知道 Orca 使用的 worktree 选择器——`path:/srv/repo`，或 `name:`/`branch:`/`id:` 选择器。
- 该 SSH 账户已安装 provider CLI。doctor 会记录其绝对路径，因为非交互 SSH 的 `PATH` 很精简，远端启动用的就是这个路径。
- **本机**的 `PATH` 中有 `ssh` 与 `sftp`。
- 如果该账户从未运行过这个 provider，必须先在那个面板里手动启动一次：首次运行会出现主题/安全/绕过确认等弹窗，任何就绪探测都清不掉它们。目录信任是另一回事——`new` 会通过 SSH 预先写入，因此不会出现"是否信任此目录？"；首次运行引导则不会被预写。

远端刻意**不需要**：不安装 OSW、不常驻进程、不需要辅助脚本、不克隆或复制代码仓库。OSW 不在远端准备任何东西——Orca 用那台机器上已有的 workspace 创建终端。

Orca 配对对运行是可选的：它既是派单的寻址方式，也是在 Orca 界面里查看远端终端的手段，但没有任何文件读写依赖它。失去配对只影响对该主机的派单，不影响 OSW 读取交接文件或判定完成。

### 主机命令

```bash
python skill/osw.py host add fnnas --ssh fnnas --worktree path:/srv/repo
python skill/osw.py host list
python skill/osw.py host doctor fnnas
python skill/osw.py host rm fnnas
```

`host add` 往 `.orca/osw/hosts.json`（与 `state.json` 并列，不在其中）写一条记录：名称、SSH 目标、Orca 环境选择器（默认为名称）、workspace 选择器，以及可选的 `--root`（用于拼接路径）。它不探测任何东西，也不在远端写入。重名登记会被拒绝并给出已有记录；`local` 不可登记——它就是本机。

`host list` 输出名称、SSH 目标、环境、worktree，以及一次性探测的可达性：`reachable`、`unreachable` 或 `permission_denied`。死掉的主机是一行状态，不是错误，也不会被省略。

`host rm` 删除记录，并列出仍引用它的 agent 记录。

### doctor

`host doctor <host> [--provider <provider>] [--json]` 按顺序走四阶，首个失败即停止，因此不会让人去给一台连 workspace 都没有的机器装 provider。它也接受未登记的目标（`host doctor myhost --ssh user@myhost --worktree path:/srv/repo`），因此可在 `host add` 之前使用。

| 阶 | 检查 | 失败码 |
|---|---|---|
| `connection` | 一条 SSH 命令（`true`）能返回 | `host_unreachable`、`host_permission_denied` |
| `workspace` | 那台机器的 Orca 已登记 workspace | `remote_no_project` |
| `worktree` | 有 worktree 匹配记录的选择器 | `remote_no_worktree` |
| `provider` | 对 provider 执行一次 `command -v` | `provider_missing` |

每个失败都会打印错误码和下一条可执行动作。`provider` 阶会把 Orca CLI 与 provider CLI 的绝对路径写进主机记录，并缓存远端 home 与平台，之后调用不再依赖环境配置。失败之后的阶不会被探测，doctor 之外的任何命令路径都不做探测。

### 错误码

来自主机缝的失败是一个封闭集合。脚本和 agent 按错误码分支，绝不解析文案。

| 错误码 | 含义 |
|---|---|
| `host_unreachable` | ssh/sftp 无法到达该机、超时，或本机没有 `ssh` |
| `host_permission_denied` | 本机 SSH 密钥未被该账户授权 |
| `remote_no_project` | 那台机器的 Orca 里没有登记 workspace |
| `remote_no_worktree` | 没有 worktree 匹配记录的选择器 |
| `provider_missing` | 该 SSH 账户下没有安装 provider CLI |
| `remote_session_unknown` | 恢复所需的 hook 缓存读不到，或其中没有该面板的条目 |
| `unknown_host` | 名称未登记；报错里会列出已登记主机 |
| `host_registry_invalid` | `.orca/osw/hosts.json` 不可读或格式未知 |

主机缝内部既非传输也非探测的失败，表现为 `remote_read_failed`、`remote_write_failed` 或 `remote_command_failed`。

### 登记 → doctor → 派单 → 收取 → 清理

```bash
# 1. 登记一次
python skill/osw.py host add fnnas --ssh fnnas --worktree path:/srv/repo

# 2. 逐阶验证
python skill/osw.py host doctor fnnas --provider claude

# 3. 派单：在那台机器上开一个面板，由本机驱动
python skill/osw.py new --on fnnas claude --prefix code "修复失败的测试"

# 4. 在本机看整个机队
python skill/osw.py list --all
python skill/osw.py status --all

# 5. 这一轮结束后，交接文件是本地文件
cat .orca/osw/handoffs/code_001_<ts>.md

# 6. 清理
python skill/osw.py del fnnas/code_001 --close
```

每一步保证什么：

- **派单**。`new --on <host>` 需要该主机的 workspace 选择器（`host add` 记录、doctor 确认）；缺失或未知时在创建任何东西之前失败——绝不在远端猜目录。随后用 `terminal create --environment <host> --worktree <选择器> --command "<provider 绝对路径> <与本地启动相同的自主权参数>"` 在那台机器上创建 Orca 终端，在本机记录带 `host` 的 agent，并启动与本地 agent 相同的本机 watcher。发提示、读面板、判定这一轮结束的都是 watcher，不是 Orca。workspace 信任通过 SSH 预先写入，写入失败则退化为交互提示，一个 Enter 即可清除。Run 与所有日志都留在本机，因此远端 agent 不出现在任何 Dispatch 里。
- **寻址**。记录携带 `host`，之后每条命令都知道该找哪台机器。agent id 仍是 `<host>/<agent_id>`（`local/` 可省略），与记录矛盾的前缀会被拒绝，而不是被静默忽略。
- **机队视图**。`list --all` 用一张表显示本地与远端 agent，并带 `HOST` 列（`--json` 每行都带 host）；`status --all` 按主机统计 agent 与运行中的 watcher。当前不可达的主机显示为显式的未知状态，而不是它上一次的状态。
- **交接收取**。这一轮结束后，远端面板收到标准收尾指令，其中命名的是它自己机器上的路径——`<远端 home>/.orca/osw/handoffs/<agent>_<ts>.md`，位于仓库之外，因此 OSW 不会污染别人的代码。watcher 通过 SSH 轮询存在性与字节长度，直到两次读取一致，再把字节复制进本地 `.orca/osw/handoffs/`，并在那里套用同样的"存在且大小稳定"判据。记录保留 `remote_handoff_path` 并标记 `evidence: ssh`。远端 agent 结束时没有交接文件，同样记为 `done` 加 `handoff_missing`，与本地 agent 一致。
- **报告**。完成报告和转发的 worker 提问仍沿原有通知路径送到本机的监护方终端，行为不变。
- **清理**。`del <host>/<agent_id> --close` 停掉 watcher，让 Orca 关闭那台机器上的终端，并删除记录；终端已经关闭不算错误。之后用 `host rm` 删除主机记录。

### 说明与限制

- **只有 watcher 写 agent 记录。** `list`、`status`、`use`、`revive` 只读记录，并可能把 watcher 已死的 agent 标记为 `lost`；其他代码不写记录。跨主机行为完全一致。
- **Provider 路径。** 远端 `new` 启动、远端 `revive` 重新输入的都是 `host doctor` 记录的 provider 绝对路径，而不是裸名字——配对运行时的 shell 未必能从 `PATH` 解析到 provider。本地启动与本地恢复仍用裸名字，与之前逐字节一致；doctor 未记录路径的主机回退到名字。
- **远端 revive 需要会话 id。** 它通过 SSH 读那台主机的 hook 缓存（`~/.config/orca/agent-hooks/last-status.json`）。Orca 从不记录 hook 状态的主机上，任何面板都没有 id：实测主机 pi 上该文件存在、`version: 2` 但零条目，因此 `revive pi/<agent_id>` 以 `remote_session_unknown` 失败，错误里点名面板与缓存路径。OSW 直接失败，而不是猜一个会话去恢复（可用 `--session <sid>` 自行提供）；远端 revive 尚未做过端到端验证。
- **首次运行归操作者。** 如果该账户在一台机器上从未运行过某 provider，OSW 无法替它清掉首次运行的主题/安全/绕过确认弹窗；先在那个面板里手动启动一次。目录信任不同：它通过 SSH 预写，不需要操作者介入。
- 主机记录在 `.orca/osw/hosts.json`；agent 记录保持扁平布局，位于 `.orca/osw/agents/`。`<host>/<agent_id>` 只是寻址形式，不是存储布局，因此 id 在本机仍然唯一。
- 阻塞更新锁按主机区分：本机是 `update-<provider>.lock`，具名主机是 `update-<provider>--<host>.lock`，因此两台主机不会互相串行等待，同一主机上的面板之间仍然串行。
- 支持的目标是 POSIX 远端；Windows 远端只做尽力支持。
- `guard` 保持在本机——监护方是本机的一个终端。

## 控制平面（Mailbox）

OSW 有一个可选的本地控制平面：持久化邮箱、确定性快路径、做决策的 Router 角色，以及通过 CLI 既有路径执行决策的 Dispatcher。执行层不变——Run/Task/DAG/Dispatch 仍归 Orca，每一轮仍由 watcher 负责，代码仍由 provider 写。

```bash
python skill/osw.py mailbox start
python skill/osw.py mailbox send --kind request --text "分别检查这两个 PR" --thread-key chat:main
python skill/osw.py mailbox wait --as router --timeout 0
python skill/osw.py mailbox decide msg_... --action accept --ticket code_001 \
    --reason "明确人工请求" --payload-json '{"provider":"claude","model":"sonnet"}'
python skill/osw.py mailbox status --json
python skill/osw.py mailbox stop
```

- **生命周期** —— `start` 以 detached 方式启动守护进程并等待锁；`status` 报告 `running`、`pid`、各队列积压、`spool_lag`、门铃状态与死信；`stop` 写停止旁路文件并等待退出。重复 `start` 视为成功，不是错误。
- **只有生产者写 spool**（`.orca/osw/mailbox/spool/`），守护进程是 SQLite store 的唯一写入方。摄入至少一次、应用幂等：去重键（提问用 Orca 消息 id，报告用 `report_key`，决策用 `decision_id`）吸收重复，因此每个 Ticket 只会启动一次 worker、回答一次、停止一次。
- **卡片与门铃** —— `wait` 返回不超过 1 KiB 的卡片，`decide` 必须给出原因。有卡片在等时，守护进程通过与 watcher 相同的通知通道发一行 `# [osw] mail pending=<n> run="osw mailbox wait --as router --timeout 0"`——按需启动的 Router 会话只拿到一个计数和一条命令。租约过期后再响一次，仍无人处理则通知人。
- **Ticket 与 Mandate** —— Ticket 是持久案卷；Mandate 是关闭 schema、由人维护的 `skill/osw/mandate.json`，从已提交的 HEAD 读取。Router 开 Ticket 时只能收窄、不能放宽；Dispatcher 把越界记为 `mandate_denied` 并拒绝执行。
- **回答权限** —— Router 唯一能写进 worker 的内容是固定单行模板 `# [osw] answer <decision_id>: <text>`，最多 4 KiB，发送前 Dispatcher 会复查提问仍未关闭。登录、权限、删除、付费画面一律升级给人；阻塞画面仍由 watcher 的白名单策略处理。
- **direct mode 是保证** —— 从不启动邮箱、或邮箱已停止时，`new`、`use`、`status`、`list`、`revive`、`stop` 与现有通知路径行为完全不变。邮箱只是 `notified_asks` 之外的第二道防线，不替代 watcher、交接校验和 Orca 自身状态。守护进程死掉时 spool 保留生产者写入的内容，重启后摄入，`status` 报告 `mailbox_down`。
- **保持在本机** —— 每个项目根一个邮箱，位于 `.orca/osw/mailbox/`。远端事件由本机 watcher 经主机缝写入本机 spool；远端不运行任何东西。

## 架构

```
new/use [--on <host>]
  ├── 解析主机（本机，或已登记且已知选择器的主机）
  ├── 创建或接管 Orca 终端——远端为 `terminal create --environment <host>
  │   --worktree <选择器> --command "<provider 绝对路径> <参数>"`
  ├── 写入 .orca/osw/agents/agent_NNN.json（记录带 `host`）
  ├── 在本机启动 detached watcher(agent_NNN)——所有主机都如此
  └── 立即返回
```

这一行之下两台机器完全相同：由 watcher 发送提示、读取面板、判定这一轮并收取交接文件——面板在远端时走 SSH。Orca 不会被要求去监督远端 agent。

每一次 Orca 调用，以及对 agent 可见文件的每一次读、写和存在性检查，都经过 `skill/osw/hosts.py`；其他模块不构造 `ssh`/`sftp` 参数列表，也不直接操作另一台机器上的文件。单机安装时这条缝解析到本地实现，行为与之前逐字节一致。

OSW 只负责编排：初始化状态、创建或接管 Orca 终端、启动 detached watcher、记录日志并报告完成。Provider 命令构造集中在 `skill/osw/providers.py`。

### 观察者生命周期

```
等待面板就绪（`worktree ps` 报告 done；omp 这类标题型 TUI 看标题无旋转符；未识别 CLI 回退到 tui-idle / 预览标记）
 → 发送任务提示
 → 等待 Orca 报告该轮完成（`worktree ps`，与发送时间相关联；标题型 TUI 跳过"输出安静"判据；未识别 CLI 回退到空闲检测）
 → 发送固定收尾指令，附带预分配的交接文件路径
 → 再次等待，校验 .orca/osw/handoffs/<agent>_<ts>.md 已写入（重试一次）
 → 写入 JSON 报告并通知调用方终端
```

完成提醒由 OSW watcher 主动发送：调用方是终端时用 `orca terminal send`，
是对话会话时用 `orca orchestration send`（Orca 会把它作为用户轮注入该对话）。
这不是 Orca 原生的跨会话通知，因此要自动送达，必须成功检测到调用方（终端或
对话会话），或显式传入 `--caller-terminal` 地址。

`new` 只启动裸 provider TUI——任务提示始终由 watcher 作为独立一轮发送，
因此启动瞬间的 TUI 空闲状态不可能被误判为任务完成。

### omp：按标题判断就绪与完成

omp 是唯一把状态只画在终端标题里的 provider：一轮运行时是
`⠋ π - <任务>`，回到输入框后是 `OMP - <任务>`（或 `OMP > <工作区>`），下方是
模型/目录/上下文状态栏。它的状态栏约每秒重绘一次，面板永远不会"输出安静"，
Orca 也不会为它发出 `tui-idle`。因此 OSW 读标题：

- 就绪：标题前面没有旋转符且输入框状态栏已画出（刚建好、Orca 已命名的面板
  还没有状态栏，因此不会被误判为就绪）；
- 完成：`worktree ps` 的 `done` 行连续两次看到、且标题不再带旋转符即算完成
  ——其他 provider 用的"输出安静"判据在这里永远不可能成立。

这两条规则才让经典的发-观望通道对 omp 可用。Orca 的受监督通道
（`orca orchestration worker-start --agent omp`）不再使用：1.4.246 在那里等待
自己的 `tui-idle` 就绪信号，而 omp 面板停在输入框时该信号永不触发（实测：
120s 与 240s 预算下都停在 `agent_readiness` 失败）。同一 TUI 的
`terminal create` 面板每轮都会上报 hook 状态和 `worktree ps` 行，这正是
watcher 需要的。更早版本 OSW 用该通道发射的 agent 仍保有 `dispatch_id`，
继续通过 `orca orchestration worker-show` 受监督；`del --close` 对它们执行
`worker-release` / `worker-stop`。

## 接管与恢复

```bash
python skill/osw.py enroll                       # 登记未受管的 agent 终端
python skill/osw.py guard                        # 成为它们的监护方
python skill/osw.py revive                       # 列出可恢复的会话（JSON）
python skill/osw.py revive code_001              # 重启后恢复某个 agent
python skill/osw.py stop code_001                # 中断当前一轮
```

- **`enroll`** 扫描当前 worktree 的 `terminal list`，跳过执行者自己的终端和已有记录的面板，provider 取 `agentIdentity`（没有则取 ps 的 `agentType`），每个面板写一条 `enrolled` 记录，`origin: enroll`。OSW 无法启动的 provider 记为 `support: guard_only`（`support_reason: unsupported_provider`）。Orca 用启动器名（`dsh-tui`）标记 DSH 面板，`enroll` 会把它归一到 provider `dsh`。输出 `{"enrolled": [...], "skipped": [{terminal, reason}]}`。
- **`guard [<id>...]`** 让执行者（`--caller-terminal`，否则 `ORCA_TERMINAL_HANDLE`，否则 Orca 对话注入的 `ORCA_AGENT_SESSION_ID`）成为监护方；不带 id 时监护所有未被监护的 `enrolled`/`done`/`error` agent。有存活 watcher 时先终止它，再以跟踪方式重启；没有 watcher 但 ps 报 `working` 的面板会起一个跟踪 watcher；空闲 agent 只记录监护方。被跟踪的一轮结束后照常报告，但不发 handoff（watcher 接手时这一轮已结束则为 `already_done`）。`use` 未传 `--caller-terminal` 时继续通知监护方。
- **`revive`** 不带 id 时输出 `{"revivable": [...], "not_revivable": [{agent_id, reason}]}`。`revive <id> [--session <sid>]` 要求 `support: full`、状态为 `lost`/`done`/`error`/`enrolled` 且没有存活 watcher。会话取 `--session`，否则取记录的 `session_id`，否则取该面板当前的会话；按 provider 的恢复语法（附带原有的自主权参数和模型参数）重新打开，agent id 不变。原面板确认是空 shell 时——仍在列表中、没有 `agentIdentity`、ps 条目不是 `working`、是 OSW 能转义的 shell（pwsh/PowerShell、cmd、POSIX）、且 Jev `ready_state` 判为 `shell_prompt`——直接在原面板输入恢复命令，`pane_key` 不变（`placement: in_place`）；否则（包括 `OSW_JEV_MODE=off`）新建终端（`placement: new_terminal`）。输出和 `agent_revived` 事件都带 `placement`。`lost`（中途被打断）的 agent 以 `resume_turn: true` 恢复：watcher 发送 `continue`，随后照常完成检测并发 handoff；其余情况恢复为 `enrolled`。
- **`stop <id>`** 对 claude、codex、kimi、grok、dsh 发 Ctrl-C（`terminal send --interrupt`），对 pi、omp 发单个 Esc。有存活 watcher 时写旁路文件 `.orca/osw/agents/<id>.stop`；watcher 在 ps 报告该轮被中断或 done、或面板静默 5 秒后结束这一轮，记为 `error`、`completion_source: interrupted`，跳过 handoff，通知监护方并删除旁路文件。输出 `{"agent_id", "key", "watcher"}`。

记录新增字段（均可选，旧记录兼容）：

| 字段 | 取值 |
|---|---|
| `origin` | `new`、`use`、`enroll` |
| `session_id` | provider 会话 id |
| `guardian` | 监护方是 OSW agent 时为 `{agent_id}`（通知时从它的记录读取当前终端），是对话会话时为 `{session}`，否则为 `{terminal, pane_key}` |
| `caller_session` | 从 Orca 对话（而不是终端）派发时为 `orca_session_id:<id>` |
| `support` | `full`，或 `guard_only` 并带 `support_reason: unsupported_provider` |
| `follow_turn` | watcher 接手进行中的一轮；结束后清除 |
| `resume_turn` | watcher 的任务提示为 `continue`；结束后清除 |
| `notified_asks` | 已转给监护方的 supervised worker 提问 id |
| `host` | agent 所在的主机；缺省表示本机 |
| `worktree_path` | 那台机器上的 worktree 路径（远程启动时记录） |
| `remote_handoff_path` | 通过 SSH 收取交接文件时，agent 所在主机上的交接路径 |
| `evidence` | 交接文件从远端复制而来时为 `ssh` |

新增状态：`enrolled`（已登记，无 watcher）和 `lost`（非终态且 watcher 进程已不存在，由 `list`/`status`/`use`/`revive` 标记）。新增完成来源：`already_done` 和 `interrupted`。

每次发送通知前都会重新解析通知对象：有监护方时用监护方，否则用调用方（优先终端，其次对话会话）。监护方 agent 的记录读不到时回退到调用方（记录 `guardian_unresolved` 事件）；监护方 handle 失效时按面板重新绑定一次并重试一次。对话会话目标没有句柄可重绑：通知走 `orca orchestration send --to orca_session_id:<id>`，投递失败记录 `notify_failed` 并带 `caller_session`。

会话 id 来自 Orca 的 agent-hook 缓存 `%APPDATA%\orca\agent-hooks\last-status.json`（本机为 `C:\Users\shiki\AppData\Roaming\orca\agent-hooks\last-status.json`）。它不是 Orca 的公开接口：OSW 只在 `version` 匹配（2）时读取，格式变化时会话 id 为空。

远端 revive 通过 SSH 读 agent 所在主机上的同一份缓存：`~/.config/orca/agent-hooks/last-status.json`。实测（2026-11-04，主机 pi）：文件存在且 `version: 2`，但**没有任何条目**，因此那台机器上没有任何面板有 provider 会话 id，`revive pi/<agent_id>` 会明确失败并报 `remote_session_unknown`，错误里点名该面板与缓存路径。Orca 从不记录 hook 状态的主机无法自行恢复 agent：OSW 直接失败，而不是猜一个会话去恢复（知道 id 时可用 `--session <sid>`）。远端 revive 尚未做过端到端验证。

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
      hosts.py        # 主机缝：本地/SSH 执行、主机注册表、引用解析、doctor
      orca_cli.py     # 异步 Orca CLI 包装
      process.py      # 隐藏子进程窗口辅助（Windows）
      providers.py    # provider 命令构造与模型发现
      sessions.py     # 从 Orca agent-hook 缓存读取 provider 会话 id
      blockers.py     # 阻塞画面的发现、白名单与更新锁
      fastpath.py     # 摄入阶段的确定性分类与簿记
      mandate.py      # 关闭 schema 的 Ticket 限制（skill/osw/mandate.json）
      dispatcher.py   # 唯一执行决策的组件
      mailbox_cli.py  # mailbox 命令：start/stop/status/send/wait/decide/tickets
      mailbox/        # 控制平面：信封、store、spool、适配器、投递、守护进程
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
      hosts.json      # 已登记主机（`host add` 创建）
      agents/          # 每个受管 agent 一个 JSON 记录（以及 <id>.stop 停止请求）
      handoffs/        # worker 写入的交接 markdown
      mailbox/         # 控制平面：spool/、mailbox.db、daemon.lock、status.json
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
