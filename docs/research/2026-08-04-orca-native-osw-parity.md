# Orca 原生能力与 OSW 对等性调研

> 调研日期：2026-08-04（Asia/Shanghai）
> 结论基线：Orca 最新稳定版 `v1.4.167`，并单独检查 `v1.4.168-rc.1` 与未发布 `main`
> 资料范围：只使用 Orca 官方仓库、tag 源码、发布说明、官网文档与本机官方 CLI 内置帮助；未使用第三方文章
> 本次调研阶段只新增本报告，未修改源代码

## 结论先行

**Orca 已经原生覆盖并明显超过 OSW 的核心目标，但还没有做到“逐项、逐产物完全对等”。**

更准确的判断是：

1. **核心编排层已经被上游替代。** Orca 现在原生有持久 Run、Task、依赖 DAG、Dispatch、`worker_done`、阻塞式 ask/reply、决策 gate、跨服务器 worker、条件化重试和失败 fencing；OSW 当前依赖协调者上下文维护的任务账本、`worktree ps` 轮询和每任务一个 Python watcher，已经不是更强的编排实现。[官方编排指南](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orchestration.md#L17-L52) [Task/Dispatch 定义](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orchestration.md#L156-L180) [Worker loop](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orchestration.md#L182-L254)
2. **上游在 OSW 范围之外远强于本仓库。** 它还原生管理 project/repo/folder context/worktree/terminal、远程运行时、定时自动化、内嵌浏览器、Computer Use、会话恢复和系统通知。[Orca README](https://github.com/stablyai/orca/blob/v1.4.167/README.md#L29-L167) [Orca CLI guide](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orca-cli.md#L92-L225)
3. **仍有三个 OSW 特有的精确能力没有被稳定版原样替代：**
   - 跨 Claude/Codex/Pi/Kimi 的 `models` 探测，以及统一的 `--model/--thinking` 到各 provider argv 的翻译；
   - 任务完成后强制生成、校验并重试 handoff Markdown，同时固定生成 JSON completion report；
   - repo-local 的 `.orca/osw/` JSON/JSONL/旋转日志与向 caller terminal 注入一条带 artifact 路径的完成消息。
4. **不建议继续投资 OSW 的 watcher/状态机/广播/人工任务账本。** 更合理的方向是把 OSW 收缩成一个很薄的兼容层：保留 model/effort 选择与 artifact contract，把编排、等待、消息、失败恢复和 worker 生命周期交给 Orca 原生 orchestration。
5. **还不宜立刻无保护地删除 OSW。** Orca 官方仍把 orchestration 标为 Experimental，并明确要求每次从当前二进制读取 version-matched guide，因为子命令和 flag 会跨版本变化；旧 automatic coordinator 已被退休，当前 `main` 又正在增加 `worker-release/retain/list`。[稳定性警告](https://github.com/stablyai/orca/blob/v1.4.167/skills/orchestration/SKILL.md#L51-L65) [Experimental 前置条件](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orchestration.md#L47-L52) [退休 scheduler](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orchestration.md#L256-L268) 另外，`v1.4.167` 发布后才合并的 [PR #12148](https://github.com/stablyai/orca/pull/12148) 修复了 `worker-start` 在 current/existing worktree 中把 agent id 当作原始 shell command 的问题；该修复目前只进入 `v1.4.168-rc.1`，尚不是稳定版承诺。

一句话决策：**“冻结并瘦身 OSW”，而不是继续平行造编排器；等原生 orchestration 的兼容性验证通过后，再退役 OSW 核心。**

## 证据快照与版本边界

### 本仓库

- 当前 HEAD：`b52a7322402e13a58d41e4776cc7ed51eb71181f`，提交时间 `2026-07-17T14:05:42+08:00`，主题 `feat: add kimi provider support`。
- 以调研日计算，本仓库不是日历意义上的“多年未更新”，而是 **18 天未提交**；但 Orca 官方自称 daily shipping，这 18 天已经跨过多轮稳定版、RC 和协议迁移。[官方 README 的 daily shipping 声明](https://github.com/stablyai/orca/blob/v1.4.167/README.md#L160-L167)
- `pytest -q` 在 2026-08-04 实测：**102 passed in 5.46s**。README 仍写“79 tests”，同时 README 的 provider 列表也未包含源码已经支持的 Kimi，说明本仓库自身已有文档漂移。
- 当前能力基线来自源码与测试，而不只来自 README：[`skill/osw/cli.py`](../../skill/osw/cli.py#L307-L445)、[`skill/osw/providers.py`](../../skill/osw/providers.py#L35-L89)、[`skill/osw/watcher.py`](../../skill/osw/watcher.py#L1-L36)、[`tests/test_cli.py`](../../tests/test_cli.py#L96-L143)、[`tests/test_watcher.py`](../../tests/test_watcher.py#L226-L320)。

### Orca 上游

| 层级 | 版本 / commit | 时间 | 用途 |
|---|---|---|---|
| 最新稳定版 | [`v1.4.167`](https://github.com/stablyai/orca/releases/tag/v1.4.167), dereferenced commit [`6b2f442ec77d2d00e07df4712e6c83ff4f48612a`](https://github.com/stablyai/orca/commit/6b2f442ec77d2d00e07df4712e6c83ff4f48612a) | 发布于 2026-08-03 10:37:07 UTC | 本报告的“可用稳定能力”基线 |
| 最新 RC | [`v1.4.168-rc.1`](https://github.com/stablyai/orca/releases/tag/v1.4.168-rc.1), commit [`17df980b7d76cf0e076b50915775f61dae885556`](https://github.com/stablyai/orca/commit/17df980b7d76cf0e076b50915775f61dae885556) | 发布于 2026-08-03 21:40:38 UTC | 预发布观察；包含 `worker-start` agent-id/launch-command 映射修复，不视为稳定承诺 |
| `main` 快照 | commit [`0db12feee80d1286c152b2cba33bf4eb5840e73f`](https://github.com/stablyai/orca/commit/0db12feee80d1286c152b2cba33bf4eb5840e73f) | commit 时间 2026-08-03 19:44:30-07:00，即北京时间 2026-08-04 10:44 | 未发布趋势观察 |
| 本机官方安装 | `v1.4.166`, 对应 tag commit [`8391d7c1795bb647f7d67b0bffa80fd767984806`](https://github.com/stablyai/orca/commit/8391d7c1795bb647f7d67b0bffa80fd767984806) | 官方 release 发布于 2026-08-03 09:24:36 UTC | 实际 CLI help / read-only runtime 验证 |

本机 `Orca.exe` 的 Windows ProductVersion 为 `1.4.166.0`；`orca status` 返回 runtime 和 graph 均为 ready。`orca orchestration run-list --json` 能读出持久 Run；`orca automations list --json` 正常返回。这些本地观察只用于确认官方 tag 源码确实已进入可安装产品，不替代上表的公开来源。

官网文档是滚动发布的，可能先于最新稳定 tag。下文凡是稳定 CLI 合同，优先引用 `v1.4.167` tag 源码；官网只用于会话恢复、系统通知等产品行为，并明确标为“滚动文档”。

## 能力对等总表

判定口径：

- **超过**：Orca 稳定版覆盖 OSW 语义，且提供更强的持久性、寻址或恢复能力。
- **大体覆盖**：核心结果可实现，但交付方式或 artifact contract 不同。
- **未完全对等**：OSW 的一项明确行为在稳定版公开命令面中没有原生一对一替代。
- “未发现”只表示在 `v1.4.167` tagged guide、CLI command specs、本机 `v1.4.166 --help` 和官方文档中未找到；不把它夸大为对所有内部实现的绝对否定。

| 能力 | Orca `v1.4.167` | 相对 OSW | 判定 |
|---|---|---|---|
| 多 agent 创建与继续 | `worker-start` 可在 current、既有、新 child/top-level worktree 启动新 agent，也可复用明确 terminal；可先启动多个 worker 再统一等待。[证据](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orchestration.md#L178-L204) | OSW `new/use` 只管理当前目录的 terminal。[本地证据](../../skill/osw/cli.py#L360-L539) | **超过** |
| 会话/进程恢复 | App 退出、更新或崩溃时由 PTY daemon 保持进程并 warm reattach；主机重启只恢复布局与 scrollback，不恢复进程。[滚动官方文档](https://www.onorca.dev/docs/model/session-restore) | OSW 只能 `use` 仍存在的 terminal；自身 watcher 是每任务一次性进程。 | **超过，但有主机重启边界** |
| Provider / model / thinking | `worker-start` 只暴露 `--agent` 或 `--terminal`，没有 model/effort flag；自定义 Codex model/effort 要自己构造 `terminal create --command ...`。[CLI spec](https://github.com/stablyai/orca/blob/v1.4.167/src/cli/specs/orchestration-worker-specs.ts#L3-L36) [官方 recipe](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orca-cli.md#L57-L90) | OSW 原生探测 provider models，并翻译四家 argv。[本地证据](../../skill/osw/providers.py#L35-L89) [探测](../../skill/osw/providers.py#L115-L168) | **未完全对等** |
| 后台/持久 worker | `worker-start` 返回启动 receipt，worker 在 Orca runtime/远端 server 上继续；可跨服务器按 Dispatch ID 控制。[证据](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orchestration.md#L182-L221) | OSW 每任务启动一个 detached Python watcher，并有 1 小时 hard timeout。[本地证据](../../skill/osw/watcher.py#L67-L85) | **超过** |
| 并发与依赖 DAG | Task 原生保存 `deps`、parent 与状态；`task-list --ready` 支持依赖就绪波次；多个独立 worker 先启动后等待。[证据](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orchestration.md#L156-L190) | OSW 的 ledger 和 1-4 并发上限只存在于 skill 文本、保存在 coordinator context，不是持久 runtime state。[本地证据](../../skill/references/orchestration.md#L20-L50) | **超过状态能力；调度仍由 agent 决策** |
| 自动 scheduler | Run 只做 namespace/inbox；官方明确说 Orca 不调度、不推断冲突，旧 `coordinator-start` 已退休且无副作用。[证据](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orchestration.md#L178-L180) [退休说明](https://github.com/stablyai/orca/blob/v1.4.167/src/cli/specs/orchestration.ts#L207-L233) | OSW 也不是自动 scheduler，由协调者按 skill 手动 rolling dispatch。 | **双方都没有** |
| Watcher / 完成判定 | 普通 agent 有 working→idle 通知；受监督任务使用带 capability 的显式 `worker_done`，自动 settle Task/Dispatch，Run Delivery 可 wait/ack/replay。[证据](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orchestration.md#L126-L154) [通知文档](https://www.onorca.dev/docs/notifications) | OSW 轮询 `worktree ps`，用 output silence fallback，并防 stale done、swallowed prompt 等误判。[本地证据](../../skill/osw/watcher.py#L407-L574) | **核心语义已覆盖，机制更可靠** |
| Caller 完成通知 | Orca UI 有系统/声音/chip/未读通知；orchestration 完成进入 coordinator Run inbox，协调者用 `check --wait` 收取。[证据](https://github.com/stablyai/orca/blob/v1.4.167/README.md#L160-L167) [Delivery 语义](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orchestration.md#L136-L154) | OSW 会直接向 caller terminal 注入一条包含 report/handoff 路径的 inert 单行消息。[本地证据](../../skill/osw/watcher.py#L101-L118) | **大体覆盖，不是同一 UX** |
| Handoff / report | `worker_done` 有 body、outcome、files、可选 `report-path`；消息类型中也有 `handoff`。[CLI spec](https://github.com/stablyai/orca/blob/v1.4.167/src/cli/specs/orchestration.ts#L44-L76) [message types](https://github.com/stablyai/orca/blob/v1.4.167/src/main/runtime/orchestration/types.ts#L1-L25) | OSW 强制第二个 turn 写指定 Markdown，检查非空，缺失时再试一次，并固定写 JSON report。[本地证据](../../skill/osw/watcher.py#L278-L307) [report](../../skill/osw/watcher.py#L576-L631) | **未完全对等，最重要缺口** |
| State 持久化 | Runs/Messages/Deliveries/Tasks/Dispatches/Gates 存 SQLite；实际路径是 Electron `userData/orchestration.db`。[DB schema](https://github.com/stablyai/orca/blob/v1.4.167/src/main/runtime/orchestration/db.ts#L262-L521) [路径](https://github.com/stablyai/orca/blob/v1.4.167/src/main/runtime/orca-runtime.ts#L3721-L3730) | OSW 在每个 repo 的 `.orca/osw/` 保存独立 JSON 状态，采用原子 replace 与 Windows 锁重试。[本地证据](../../skill/osw/state.py#L32-L73) [原子写](../../skill/osw/state.py#L90-L129) | **语义超过，存储边界不同** |
| Log / 输出持久化 | `worker-read` 从可证明的 provider transcript 读取，否则返回有类型原因的 terminal output；terminal scrollback 跨 app restart。[证据](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orchestration.md#L206-L221) [README](https://github.com/stablyai/orca/blob/v1.4.167/README.md#L63-L67) | OSW 有 repo-local 旋转文本日志、结构化 JSONL events 和固定 report 文件。[本地证据](../../skill/osw/log.py#L17-L20) [日志实现](../../skill/osw/log.py#L54-L125) | **大体覆盖，未提供同构导出物** |
| Repo / project / folder | 原生 durable project、host setup、existing folder import、git/folder kind、clone/update/delete。[CLI spec](https://github.com/stablyai/orca/blob/v1.4.167/src/cli/specs/project.ts#L4-L117) | OSW 没有这些能力。 | **远超** |
| Worktree | 原生 list/current/show/create/set/rm/parent lineage/setup hooks/agent-first create。[证据](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orca-cli.md#L92-L154) | OSW 明确不创建或管理 worktree。[本地证据](../../skill/references/operations.md#L70-L82) | **远超** |
| Terminal | 原生 list/show/read/cursor/send/wait/stop/create/split/rename/switch/close。[证据](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orca-cli.md#L170-L204) | OSW 只包装创建、发送、等待、读取/显示、关闭等少量命令。 | **远超** |
| Browser / Computer Use | 内嵌浏览器有 tab/profile/snapshot/interact/wait/storage/download 等完整 agent CLI；另有 desktop Computer Use。[浏览器指南](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orca-cli.md#L227-L294) [README](https://github.com/stablyai/orca/blob/v1.4.167/README.md#L147-L166) | OSW 无此能力。 | **远超** |
| Automations | 支持 preset/cron/RRULE、provider、precheck、新 worktree 或既有 workspace、session reuse、运行历史。[CLI spec](https://github.com/stablyai/orca/blob/v1.4.167/src/cli/specs/automations.ts#L26-L115) | OSW 无定时任务。 | **远超** |
| 跨 agent 消息 | 持久 send/reply/ask/inbox、thread、priority、FIFO Delivery ack、group address、Dispatch 寻址、跨服务器 durable relay。[证据](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orchestration.md#L126-L154) | OSW 只有向所有未完成 agent 的无确认广播 `all`。[本地证据](../../skill/osw/cli.py#L542-L591) | **远超** |
| 失败恢复 | `worker-show/read/stop/abandon`、`retry-of`、unknown-outcome 条件处理、三次失败 circuit breaker、升级期间 legacy Run adoption/takeover。[证据](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orchestration.md#L247-L254) [升级恢复](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orchestration.md#L54-L100) | OSW 有 watcher 状态写重试、handoff 重试、stall/no-receipt 通知，但没有 attempt/dispatch authority、durable mutation receipt 或跨版本接管。 | **远超** |
| CLI/API 稳定性 | orchestration 仍是 Experimental，命令会跨 release 变化，官方要求运行 `skills get orchestration` 读取当前二进制匹配指南。[证据](https://github.com/stablyai/orca/blob/v1.4.167/skills/orchestration/SKILL.md#L51-L65) | OSW CLI 自身较小且有 102 个单测，但依赖上游 CLI JSON shape。 | **Orca 是主要迁移风险** |

## 分项分析

### 1. 多 agent 会话创建、继续与持久运行

**事实**

- Orca 把 Run、Task、Dispatch 分开：Run 是持久 namespace/inbox，Task 是工作项，Dispatch 是某一次分配。`worker-start` 可把 Task 分配到当前 worktree、精确既有 worktree、新 child、新 top-level，或通过 `--on` 分配到另一个已连接 Orca server。[稳定版指南](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orchestration.md#L156-L221)
- 对同一 worktree，默认创建 fresh agent terminal；只有显式 `--terminal` 才复用已有 agent。对新 worktree，使用 agent-first create，并复用启动时返回的 agent terminal。[稳定版指南](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orchestration.md#L192-L204)
- Orca 产品层有 out-of-process PTY daemon。正常退出 App、自动更新或 App crash 时 agent process 继续运行并 warm reattach；主机重启时 process 会丢失，但 worktree、布局、tab 和最后持久化 scrollback 仍恢复。[滚动官方 Session Restore 文档](https://www.onorca.dev/docs/model/session-restore)
- Agent hibernation 可以把已完成且后台闲置的 Claude/Codex/Gemini/Antigravity/OpenCode/Droid/Grok 停掉，再用 provider 原生 resume flag 恢复；该功能仍是 Experimental，Pi、Kimi 等并不都支持。[滚动官方 Hibernation 文档](https://www.onorca.dev/docs/agents/hibernation)

**与 OSW 比较**

OSW `new` 只在 active worktree 启动一个 provider TUI，`use` 只收养同一目录中的 live terminal；复用 OSW agent id 还要求上一轮已经处于 done/error/lost。[`new`](../../skill/osw/cli.py#L359-L445) [`use`](../../skill/osw/cli.py#L447-L539)。这对“继续同一个还活着的 TUI”很好用，但不是跨 App/远端/新 worktree 的完整 session manager。

**推断**

对于 OSW 原本服务的“启动多个 coding-agent TUI，让它们后台工作，再回收结果”，原生 `worker-start` 加 PTY daemon 已经是更深、更稳定的所有权层；继续维护独立 watcher 只会重复上游生命周期逻辑。

**未知 / 边界**

- `worker-start` 的公开 CLI 没有直接接受 provider session id 的 `resume` flag；provider 历史恢复目前主要是 UI/hibernation 能力，或由调用者通过自定义 `terminal create --command` 表达。
- 主机 reboot 仍会终止 live process；不能把“session restore”误写成跨重启进程容灾。

### 2. Provider、model 与 thinking/effort 选择

**事实**

- Orca 支持任何 terminal CLI agent，内建 agent picker 的覆盖面远大于 OSW 的四家 provider。[稳定版 README](https://github.com/stablyai/orca/blob/v1.4.167/README.md#L171-L205)
- 但稳定版 `orchestration worker-start` command spec 只接受 `--agent <agent>` 或 `--terminal <handle>`，没有 `--model`、`--thinking` 或 `--effort`。[稳定版 command spec](https://github.com/stablyai/orca/blob/v1.4.167/src/cli/specs/orchestration-worker-specs.ts#L3-L36)
- 官方 guide 明确说 `worktree create --agent codex --prompt ...` 不接收 Codex-specific model/effort 参数；需要先建 worktree，再执行类似 `codex --model gpt-5.5 -c model_reasoning_effort="xhigh"` 的自定义 terminal command。[官方 custom argv recipe](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orca-cli.md#L71-L90)
- Orca UI Settings 可以为 agent 保存默认 launch arguments，但这是每 agent 的启动覆盖，不是一个跨 provider 的模型枚举 API。[滚动官方 Agents & Sessions 文档](https://www.onorca.dev/docs/model/agents-sessions)

**OSW 的独特价值**

OSW 目前会：

- 读 Claude help aliases、Pi `--list-models`、Codex/Kimi 本地 config；
- 把统一 `model/thinking` 翻译成 Claude `--effort`、Codex `model_reasoning_effort`、Pi `--thinking`，并拒绝 Kimi 不可表达的 thinking；
- 默认加入各 provider 的 autonomy flag。

实现与测试见 [`providers.py`](../../skill/osw/providers.py#L35-L168) 和 [`test_providers.py`](../../tests/test_providers.py#L72-L180)。

**结论**

这是稳定版 Orca **没有一对一替代** 的明确差异。上游可以运行任意 argv，因此“能实现”，但没有 OSW 的统一发现与翻译 UX。若瘦身 OSW，这一层值得保留为纯命令构造器，不再负责 worker 生命周期。

### 3. 并发、依赖和调度

**事实**

- Task 状态原生包含 `pending/ready/dispatched/completed/failed/blocked`，Task 保存 parent 和 `deps`；合法 `worker_done` 自动完成当前 Task 与 Dispatch。[稳定版指南](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orchestration.md#L152-L176) [类型定义](https://github.com/stablyai/orca/blob/v1.4.167/src/main/runtime/orchestration/types.ts#L244-L276)
- 官方建议先创建所有独立 Task，再启动所有独立 worker，之后等待每个 Dispatch settle。[稳定版指南](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orchestration.md#L182-L190)
- 决策 gate 是持久对象，可阻塞 Task；worker 的即时问题则走 ask/reply。[稳定版指南](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orchestration.md#L256-L265)
- 上游明确声明 agent 自己选择 placement 和 concurrency，Orca 不做 scheduler 或冲突推断；旧 automatic coordinator 命令已经退休。[稳定版指南](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orchestration.md#L178-L180) [CLI spec](https://github.com/stablyai/orca/blob/v1.4.167/src/cli/specs/orchestration.ts#L207-L233)

**与 OSW 比较**

OSW 的依赖 ledger 只要求“maintain in coordinator context”；1-4 worker 上限也是 skill policy，不存入 state，不由 CLI 校验。[本地 orchestration reference](../../skill/references/orchestration.md#L20-L50)。因此上游不是缺少 OSW 的自动 scheduler；两者本来都由协调 agent 做决策，而上游多了 durable DAG 与 Dispatch authority。

### 4. Watcher、完成信号与通知

**事实**

- 普通 Orca agent session 通过 agent 状态的 working→idle 触发系统通知、声音和 worktree chip，并保留 unread inbox。[滚动官方 Notifications 文档](https://www.onorca.dev/docs/notifications)
- supervised orchestration 不把 idle 当作最终任务协议，而是注入 capability-bound preamble，要求 worker 从自己的 terminal 发一次 `worker_done`，显式带 `succeeded|failed`；有效信号自动 settle Task/Dispatch。[稳定版 worker contract](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orchestration.md#L231-L245) [Agent guidance](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orchestration.md#L362-L370)
- coordinator 通过持久 FIFO Delivery 接收完成、问题和 escalation；同一批 Delivery 在 ack 前会重放，`check --wait` 支持阻塞等待而不需要 sleep/poll loop。[稳定版 messaging contract](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orchestration.md#L136-L154)

**与 OSW 比较**

OSW watcher 的质量并不差：它区分 startup idle 与 task completion，要求 prompt receipt，拒绝 stale done，tracked pane 丢失后才做受限 idle fallback，并对 AskUserQuestion/stall/no-receipt 通知 caller。[`watcher.py`](../../skill/osw/watcher.py#L407-L574)。但这仍是在推断一个外部 TUI 是否完成；原生 `worker_done` 是 task/dispatch-bound 的显式协议，生命周期所有权更清楚。

**未完全等价之处**

Orca 的 Run inbox 和 UI notification 可以替代“知道完成”，但稳定版没有一个与 OSW 完全相同的动作：自动向 caller terminal 注入 `# [osw] task-finished ... report=... handoff=...`。这属于 UX/artifact routing 差异，不是编排状态缺失。

### 5. Handoff、report 与结果验收

这是最需要保留警惕的一项。

**事实**

- Orca 的 message type 包含 `handoff`；`worker_done` 可以携带 subject/body、显式 outcome、files modified 和可选 report path。[类型定义](https://github.com/stablyai/orca/blob/v1.4.167/src/main/runtime/orchestration/types.ts#L1-L25) [CLI spec](https://github.com/stablyai/orca/blob/v1.4.167/src/cli/specs/orchestration.ts#L44-L76)
- 官方 worker guidance 把 `--report-path` 标为 optional，核心验收载荷是 worker_done body、outcome 与 files。[稳定版 guidance](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orchestration.md#L362-L370)
- `worker-read` 可以读取 provider transcript 或 bounded terminal output，适合 coordinator 验证 worker 声明。[稳定版 guide](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orchestration.md#L206-L221)

**OSW 的额外合同**

OSW 在检测任务 turn 完成后，会再发一个固定 wrap-up turn，要求写入预分配 Markdown；文件为空或缺失时再试一次，随后无论成功与否都写 JSON completion report，再把路径通知 caller。[`HANDOFF_TEMPLATE`](../../skill/osw/watcher.py#L76-L118) [执行与重试](../../skill/osw/watcher.py#L278-L307) [finalize/report](../../skill/osw/watcher.py#L576-L646)。

**未发现 / 推断**

- 在稳定版 tagged guide、command specs 和本机 v1.4.166 help 中，**未发现**原生命令会自动创建、验证和重试 handoff Markdown，也未发现自动生成一个 repo-local JSON completion report 的等价命令。
- `handoff` message type 只证明这种语义可以被传递，不证明文件 artifact 会被创建或校验。
- 因而，如果团队确实依赖 handoff 文件作为跨上下文的可审计交付物，这个 contract 需要保留在薄 adapter 或 worker task template 中。

**上游趋势，但尚未发布**

在调研时的 `main` commit `0db12fe`，官方已经新增 `worker-release`、`worker-retain`、`worker-list`，并要求 release 前保存 inspectable output archive，使 terminal 关闭后 `worker-read` 仍可读。[未发布 main command spec](https://github.com/stablyai/orca/blob/0db12feee80d1286c152b2cba33bf4eb5840e73f/src/cli/specs/orchestration-worker-specs.ts#L71-L104) [未发布 main guide](https://github.com/stablyai/orca/blob/0db12feee80d1286c152b2cba33bf4eb5840e73f/skill-guides/orchestration.md#L223-L260)。这在缩小“输出留存”差距，但仍不是自动 handoff Markdown，也不属于 `v1.4.167` 或 `v1.4.168-rc.1` 的稳定/RC 能力。

### 6. State、log 与可审计性

**事实**

- Orca orchestration state 是 SQLite-backed。schema 包含 runs、messages、deliveries、worker dispatches、tasks/deps、dispatch contexts 和 decision gates，并有多代 schema migration、durable question thread 与 mutation receipt。[稳定版 DB](https://github.com/stablyai/orca/blob/v1.4.167/src/main/runtime/orchestration/db.ts#L262-L521)
- 数据库路径是 Electron `app.getPath('userData')/orchestration.db`，也就是 Orca runtime 全局 userData，而非当前 repo。[稳定版源码](https://github.com/stablyai/orca/blob/v1.4.167/src/main/runtime/orca-runtime.ts#L3721-L3730)
- 可观测面包括 Run/Task/Dispatch/inbox CLI、`worker-read` transcript/terminal output，以及跨 App restart 的 terminal scrollback。[稳定版 worker-read](https://github.com/stablyai/orca/blob/v1.4.167/src/cli/specs/orchestration-worker-specs.ts#L38-L53) [稳定版 README](https://github.com/stablyai/orca/blob/v1.4.167/README.md#L63-L67)

**与 OSW 比较**

OSW 的 `.orca/osw/` 是目录作用域的普通文件：agent JSON、handoff Markdown、report JSON、旋转 log 与 events JSONL。[`state.py`](../../skill/osw/state.py#L32-L73) [`write_report/new_handoff_path`](../../skill/osw/state.py#L210-L223) [`log.py`](../../skill/osw/log.py#L54-L153)。它更容易随 repo/worktree 打包、审阅或由普通脚本消费，但不具备上游 Run/Delivery/Dispatch 的事务语义。

**未知 / 未发现**

- 未发现稳定 CLI 用于把整个 Run 的状态和事件导出成一个 repo-local bundle。
- 未发现与 OSW `events.jsonl` 同构的公开 orchestration event-log 命令；官方公开的 worker inspection 是 `worker-read`，不能据此断言 Orca 内部没有其他诊断日志。

### 7. Repo、folder、worktree、terminal、browser 与 automation

这些领域上游不是“刚好对等”，而是远超 OSW：

- durable projects 和 host setups，可导入既有 git checkout 或普通 folder、clone、更新或删除 setup；[`project` CLI spec](https://github.com/stablyai/orca/blob/v1.4.167/src/cli/specs/project.ts#L4-L117)
- repo 注册、default base ref、ref search，worktree 的 create/list/show/current/set/rm、parent lineage 与 setup policy；[稳定版 worktree guide](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orca-cli.md#L92-L154)
- terminal list/show/read/cursor/send/wait/stop/create/split/rename/switch/close；[稳定版 terminal guide](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orca-cli.md#L170-L204)
- 定时 automation 支持 hourly/daily/weekdays/weekly/cron/RRULE、precheck、provider、新 worktree 或既有 workspace、reuse/fresh session 和 run history；[稳定版 automation spec](https://github.com/stablyai/orca/blob/v1.4.167/src/cli/specs/automations.ts#L26-L115)
- worktree-scoped Chromium browser 有 tab/profile、snapshot、click/fill/type、network/storage/download 等命令；另有 desktop Computer Use。[稳定版 browser guide](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orca-cli.md#L227-L294) [Computer Use 文档入口](https://www.onorca.dev/docs/cli/computer-use)

本仓库 operations 文档则明确写着“每个 working directory 是独立 OSW scope；OSW 不创建或管理 worktrees”。[`operations.md`](../../skill/references/operations.md#L70-L82)

### 8. 跨 agent 消息与失败恢复

**事实**

- Orca 原生消息支持 send/reply/ask/inbox、thread id、priority、group address、Dispatch-specific address 和多种 lifecycle type；coordinator Delivery 是持久、FIFO、ack 后推进，未 ack 会精确重放。[稳定版 guide](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orchestration.md#L126-L154)
- 远端 worker 的消息按 Dispatch ID 跨已连接 server durable relay，不要求 coordinator 重复指定 remote environment。[稳定版 guide](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orchestration.md#L206-L221)
- `worker-show` 区分 ready、failed/stopped 和 outcome_unknown；替换尝试必须显式 `--retry-of` 并重新声明 placement；`worker-abandon` 只 fencing，不谎称进程已经停止。[稳定版 recovery rules](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orchestration.md#L247-L254)
- 同一 task 连续三次失败会 circuit-break 并标记 failed；升级兼容层可 adoption 旧 Run、保留 worker process/PTY/Task/Dispatch，必要时 takeover coordinator。[稳定版 guide](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orchestration.md#L169-L176) [contract migration](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orchestration.md#L54-L100)

**与 OSW 比较**

OSW 的 `all` 是对所有未完成 agent 做 best-effort terminal send，没有 thread、reply、delivery ack、Run scope 或远端 Dispatch address。[`cli.py`](../../skill/osw/cli.py#L542-L591)。OSW watcher 自身有很好的 fail-closed 防护和 Windows state-write retry，但这些是单 watcher 的可靠性，不是分布式编排恢复协议。[`watcher.py`](../../skill/osw/watcher.py#L430-L574) [`state.py`](../../skill/osw/state.py#L90-L129)

### 9. CLI / API 稳定性

这是迁移时最大的反向因素。

**事实**

- 官方 orchestration guide 要求在 Settings > Experimental 启用功能。[稳定版前置条件](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orchestration.md#L47-L52)
- 官方 skill stub 明确说不要凭记忆或缓存猜 command/flag，因为它们会跨 release 变化；必须运行 `orca skills get orchestration` 读取与当前 binary 匹配的完整指南。[稳定版 stub](https://github.com/stablyai/orca/blob/v1.4.167/skills/orchestration/SKILL.md#L51-L65)
- 旧 automatic coordinator 命令已经变成无副作用的 recovery guidance；这说明 orchestration contract 已经发生过实质性重构。[稳定版 command spec](https://github.com/stablyai/orca/blob/v1.4.167/src/cli/specs/orchestration.ts#L207-L233)
- `v1.4.167` 之后合并的 [PR #12148](https://github.com/stablyai/orca/pull/12148) 证明稳定版仍有一个实际启动缺口：current/existing worktree 的 `worker-start --agent <id>` 曾把 id 直接当 shell command。Claude/Codex/Pi/Kimi 这类 id 与命令同名的 agent 通常不受影响，但 Cursor、Continue、Kiro、Qwen Code 等 id 与真实启动命令不同的 agent 会启动错误或在 readiness 阶段超时。修复目前随 `v1.4.168-rc.1` 预发布。
- `v1.4.167` 发布不到一天后，`main` 已新增一套 worker terminal release/accounting 和 output archive 语义；说明上游仍在高速重塑生命周期边界。[固定 main commit](https://github.com/stablyai/orca/commit/0db12feee80d1286c152b2cba33bf4eb5840e73f)

**未知**

- 官方没有在所检查的一手资料中给出 orchestration CLI 的 SemVer 兼容承诺。
- 不能假设今天的 JSON shape 和 recovery command 在未来小版本中保持不变；官方自身建议以 binary-served guide 为准。

**迁移含义**

不要把本仓库当前对 `worktree ps` JSON 的硬编码直接替换成另一组长期硬编码。新的 adapter 应在启动时做能力探测，并把命令失败返回的 typed recovery/`nextCommandArgs` 当作协议的一部分。

## 建议路线

### 建议 A：立即冻结 OSW 编排核心

不再扩展以下部分：

- watcher 的新完成启发式；
- OSW 私有 agent lifecycle/state machine；
- `all` 广播；
- coordinator-context-only 任务 ledger；
- OSW 自己的 retry/remote/worktree 管理。

这些领域已经由上游稳定版提供更深的原生对象和恢复权威。

### 建议 B：把 OSW 收缩成两个可选薄层

1. **Provider argv adapter**
   - 保留 `models` 探测；
   - 保留 provider-specific model/effort/autonomy argv 翻译；
   - 输出完整 command，交给 `orca terminal create` 或自定义 worker bootstrap；
   - 不再持有 worker 状态。
2. **Artifact contract adapter**
   - 在 Task spec/preamble 中要求 worker 写固定 report/handoff；
   - worker_done 必须带 `--report-path`；
   - coordinator 收到 worker_done 后验证文件；
   - 如仍需 repo-local审计，导出最小 manifest，而不是复制整个 Orca runtime state。

### 建议 C：在退役前做一个有限原生试点

在可信测试 repo 中验证以下矩阵，不要直接改当前生产 skill：

1. Windows 本地 `run-create -> task-create -> worker-start -> check/ack`；
2. 同 worktree 并行两个 worker，以及有 deps 的第二波；
3. finished worker 复用同 terminal；
4. App restart 后 Run、Delivery、worker terminal 与 scrollback；
5. question/ask/reply 与 failed worker retry-of；
6. 自定义 Codex model/effort argv；
7. 强制 report/handoff 缺失时的验收失败；
8. 至少用一个 agent id 与实际命令不同的 provider（如 Cursor 或 Kiro）验证 `worker-start` 启动解析；
9. 从 `v1.4.166/167` 升级到含 `worker-release` 的正式版后的兼容行为。

### 建议 D：明确版本门槛与 feature detection

- `v1.4.167` 可作为能力盘点基线；本机 `v1.4.166` 虽已有核心命令，但比最新稳定版落后一版。实际迁移切换应等待一个包含 [PR #12148](https://github.com/stablyai/orca/pull/12148) 的稳定版（按当前版本线应不早于 `v1.4.168`），不要把 RC 当作生产门槛。
- 每次会话先执行 `orca status --json` 与 `orca skills get orchestration`。
- 以 `--help` / machine-readable command schema 检查 `worker-start`、`worker-read`、未来的 `worker-release`，而不是只看 app version 字符串。
- 对 mutation 使用 request/retry identity 和 typed recovery，不盲目重复可能已生效的命令。
- 在 orchestration 退出 Experimental 或至少经过两个连续稳定版的试点前，保留旧 OSW 路径作为可回滚选项。

## 最终判断

如果“对等”指的是本仓库的业务目标——**并行启动 agent、后台监督、知道何时完成、管理依赖、收取结果、失败后恢复**——答案是：**Orca 已经对等，而且在大多数方面明显超过。**

如果“完全对等”指的是逐行为和逐文件产物——**统一 model/thinking 探测、强制且验证过的 handoff Markdown、固定 JSON report、repo-local JSONL/log、向 caller terminal 注入带 artifact 路径的完成行**——答案是：**还没有。**

因此，当前仓库最合理的定位不再是“Orca agent supervisor”，而是可能的 **Orca orchestration compatibility + provider/artifact adapter**。若团队不需要那三个差异，则 OSW 可以进入 deprecation；若需要，也只值得保留很薄的一层。

## 一手来源索引

- [Orca 官方仓库](https://github.com/stablyai/orca)
- [Orca v1.4.167 release](https://github.com/stablyai/orca/releases/tag/v1.4.167)
- [Orca v1.4.168-rc.1 release](https://github.com/stablyai/orca/releases/tag/v1.4.168-rc.1)
- [Orca v1.3.32 release：首次正式列出 inter-agent orchestration](https://github.com/stablyai/orca/releases/tag/v1.3.32)
- [v1.4.167 orchestration full guide](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orchestration.md)
- [v1.4.167 orchestration skill stub](https://github.com/stablyai/orca/blob/v1.4.167/skills/orchestration/SKILL.md)
- [v1.4.167 Orca CLI guide](https://github.com/stablyai/orca/blob/v1.4.167/skill-guides/orca-cli.md)
- [v1.4.167 orchestration command specs](https://github.com/stablyai/orca/blob/v1.4.167/src/cli/specs/orchestration.ts)
- [v1.4.167 worker command specs](https://github.com/stablyai/orca/blob/v1.4.167/src/cli/specs/orchestration-worker-specs.ts)
- [v1.4.167 orchestration SQLite implementation](https://github.com/stablyai/orca/blob/v1.4.167/src/main/runtime/orchestration/db.ts)
- [Orca 官方 Session Restore 文档](https://www.onorca.dev/docs/model/session-restore)
- [Orca 官方 Notifications 文档](https://www.onorca.dev/docs/notifications)
- [Orca 官方 Agent Hibernation 文档](https://www.onorca.dev/docs/agents/hibernation)
