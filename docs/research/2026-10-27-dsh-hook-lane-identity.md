# DSH hook 通道被压制：OSC 7501 的 agent 名不匹配

> 调研日期：2026-10-27（Asia/Shanghai）
> 环境：Orca 1.4.238（`C:\Users\shiki\AppData\Local\Programs\orca`），DSH core 0.2.1-rc.2，dsh-TUI 0.17.0
> 触发：为 OSW 新增 `dsh` provider 后，`worktree ps` 只给出 working/done，`prompt`/`toolName`/`providerSession` 恒为空

## 结论先行

同一个 dsh 面板有两个状态通道，它们对“这是哪个 agent”回答不同：

| 通道 | 名称 | 出处 |
|---|---|---|
| DSH 的 Orca hook 桥（HTTP `/hook/dsh`） | **`dsh`** | `shared/agent-hook-listener/providers/dsh-events.js:55` |
| DSH-TUI 自报 Program Status Protocol（OSC 7501） | **`dsh-tui`** | dsh-TUI `lib/types/ink/hooks/use-program-status.js:5` 的 `PROGRAM_STATUS_APP = 'dsh-tui'` |

Orca 把 OSC 7501 的 `app` 直接当 agent id 用，只有一张两行的映射表：

```js
// shared/program-status/agent-status-projector.js:9
const APP_TO_AGENT_TYPE = { 'claude-code': 'claude', 'grok-build': 'grok' };
```

`dsh-tui` 不在表里 → 原样透传成 agentType `dsh-tui`。于是 `shared/agent-hook-listener.js:118-128` 的身份协调器
（`shared/agent-status-identity.js:18-45`）在“既有记录是 `working`（fresh non-done）且 agent 名不同”时，把 hook 事件
判成**嵌套 agent 继承了本面板**，`return null` 整条丢弃。

时序上必然踩中：hook 的 `UserPromptSubmit` 只在 DSH 真正开始回合时发出，而 OSC 通道在回合开始的同一刻已先写 `working`。
结果 `UserPromptSubmit`/`PreToolUse`/`PostToolUse`/`Stop` 全部被丢；只有启动窗口（尚无既有记录）里的 `SessionStart` 能落库。

**修法是这张表加一行 `'dsh-tui': 'dsh'`（属 Orca 上游）。** 本机曾临时打上这一行做模块级 A/B，测完已按原字节回滚——当前 Orca 安装没有任何修改。

## 证据链

### 1. hook 确实发出、Orca 确实收到

临时给 `~/.orca/agent-hooks/dsh-hook.cmd` 加 trace（测完已按字节还原）后，一个回合 9 次调用全部 `HTTP 204`：

```text
{"session_id":"f68ced4a-…","hook_event_name":"SessionStart","source":"startup","cwd":"D:\\…"}
{"session_id":"f68ced4a-…","hook_event_name":"UserPromptSubmit","prompt":"Run the shell command `git rev-parse --short HEAD` …"}
{"session_id":"f68ced4a-…","hook_event_name":"PreToolUse","tool_name":"pwsh","tool_input":{"command":"git rev-parse --short HEAD"}}
…
{"session_id":"ba17bbe4-…","hook_event_name":"Stop","stop_hook_active":false}
```

DSH 会话日志（`~/.dsh/sessions/<cwd>/<id>/session.v4.jsonl.zstd`）里每个点都有
`{"type":"hook/invoked",…}` + `{"type":"hook/result","decision":"pass","exitCode":0}`：插件挂载、命令执行都正常。

### 2. `last-status.json` 的写入全是 OSC 通道

用 `mtime_ns` 驱动做**全量**写入监控（不是采样），一个带工具调用的回合：

```text
   0.0s agent=dsh-tui state=done    sess=-
   3.6s agent=dsh-tui state=working sess=-      ← OSC 通道在回合开始时先写 working
   8.6s agent=dsh-tui state=done    sess=-
  …（45s 内 19 次写入，全部 dsh-tui，从未出现 dsh 记录）
```

同样监控、不发提示词，只启动 `dsh-tui .`：

```text
   3.3s agent=dsh-tui state=done  sess=-
   5.5s agent=dsh  state=done  sess=04a02d0c…   ← SessionStart 落库
  … 45s 内持续为 dsh + providerSession
```

### 3. 模块级 A/B（Node，直接加载本机 app 的模块）

OSC 7501 帧 `\x1b]7501;state=working:app=dsh-tui:msg=<b64>\x1b\` 喂给未打补丁的 projector：

```text
{"state":"working","agentType":"dsh-tui","mainAgent":{"state":"working",…}}
```

打补丁（`'dsh-tui': 'dsh'`）后同一帧：

```text
{"state":"working","agentType":"dsh","mainAgent":{"state":"working",…}}
// 且 app=claude-code 仍映射为 claude（无回归）
```

身份协调器本身（`shared/agent-status-identity.js`，纯函数）：

```text
existing=dsh-tui/working incoming=dsh      -> inherited=true   agentType=dsh-tui   ← 现状：丢
existing=dsh-tui/done    incoming=dsh      -> inherited=false  agentType=dsh        ← 空闲时接受
existing=dsh/working     incoming=dsh      -> inherited=false  agentType=dsh        ← 补丁后：接受
```

## 影响面

- `worktree ps` 的 dsh 行没有 `prompt` / `toolName` / `lastAssistantMessage`；
- 回合结束后 `last-status.json` 的 dsh 面板没有 `providerSession`（OSC 记录整条替换）；
- OSW 的完成判定不受影响（working→done 够用），`tools_observed` 已按“不可观测”记为 `null`；
- 唯一实质缺口曾是 `revive` 的会话 id —— 已由本仓库的就绪期捕获解决（见下）。

## 本仓库已做的缓解（不依赖 Orca 修复）

`Watcher._wait_ready()` 在就绪等待期间（回合开始前、面板仍空闲、hook 通道持有记录时）调用 `_record_session()`，
拿到即停、绝不覆盖已有 id。实测一个完整回合：

```text
starting    session= d49d76b1-6a33-4260-819b-dd4657939dd0
working task session= d49d76b1-…（回合结束仍保留）
```

`revive probe4_001` 随后输出 `command: "dsh-tui . --resume d49d76b1-…"`、`placement: new_terminal`，
恢复出的面板显示的正是上一回合的对话与总结。

前提是 `~/.dsh/profiles/dsh-tui` 里装了 hook 桥及其运行时 peer（profile 固定 `autoInstallPeers: false`，
且 dsh 0.2.1-rc.2 宿主本身不带 `dsh-hook-protocol`）：

```bash
dsh plugin --profile dsh-tui add @deepseek-ai/dsh-hooks-claude-code@0.2.1-rc.2 \
  @deepseek-ai/dsh-hook-protocol@0.2.1-rc.2 @deepseek-ai/dsh-llm@0.2.1-rc.2 \
  @deepseek-ai/cordis@4.0.5
```

## 曾用于 A/B 的补丁（已回滚，本机无改动）

文件：`<Orca>/resources/app.asar.unpacked/out/shared/program-status/agent-status-projector.js`（不在 `app.asar` 里，
不受 asar 完整性校验，但 Orca 更新会替换成官方版本）。

```diff
 const APP_TO_AGENT_TYPE = {
     'claude-code': 'claude',
-    'grok-build': 'grok'
+    'grok-build': 'grok',
+    'dsh-tui': 'dsh'
 };
```

- 只在模块级验证用途上写过一次，**现已按原字节回滚**（9739 字节、无 `dsh-tui` 字样、备份已删）；
- 没有重启 Orca，因此这条补丁从未在运行进程里生效；
- 若将来要复现该实验：写入 → 重启 Orca → 按下面的清单判定 → 再回滚。

实验生效后的验证清单（每条都可判定）：

1. `orca terminal create --worktree active --command "dsh-tui ."`，等 composer 出现；
2. 发一个带工具调用的提示词；
3. `orca worktree ps --json`：该 pane 的 `toolName` 在回合内出现（`pwsh`），回合结束后 `prompt` 是该提示词；
4. `%APPDATA%\orca\agent-hooks\last-status.json`：该 pane 的 `payload.agentType` 为 `dsh`（不再出现 `dsh-tui`）；
5. 回合结束后 `providerSession.id` 是否仍在（**未验证**：OSC 通道写记录时是否保留 `providerSession`。
   若不保留，OSW 就绪期捕获仍然覆盖这个需求，且每次 `UserPromptSubmit` 都会重新写入一次）。

## 不改任何代码的开关（dsh-TUI 自带，效果未完全验证）

dsh-TUI 的 OSC 7501 上报有官方逃生阀：`DSH_TUI_PROGRAM_STATUS` 取任意假值（`0`/`false`/`no`/`off`）即关闭
（`lib/types/ink/termio/osc7501.js` 的 `supportsProgramStatus`，`Chat.js:2118` 直接读它）。
关掉后唯一写这张记录的就是 hook 桥，理论上 `prompt`/`toolName`/`providerSession` 都会留下（这也正是 Orca 源码注释里
"its hooks are the authority either way" 的设计意图）。

一次性探针（`orca terminal create --command 'cmd /c "set DSH_TUI_PROGRAM_STATUS=0&& dsh-tui ."'`）**没能给出结论**：
该面板被 `cmd /c` 包了一层，Orca 在启动早期把它识别成 `gemini`（DSH 的 `✦` 与 Gemini 的工作字形同形），
我又在 harness 还没就绪时发了提示词，回合没起来。要判定这一点，应让 dsh 进程在**常规启动路径**下带该变量
（例如用户级环境变量，或等 OSW 支持逐次发射的环境前缀），再跑一次写入监控；同时要确认关掉 OSC 后
Orca 的 `tui-idle`/readiness 车道是否仍能从 hook 车道取到状态。在验证之前不建议把它写成推荐操作。

## 上游现状（2026-10-27）

- `main` 仍未加这条映射（`https://raw.githubusercontent.com/stablyai/orca/main/src/shared/program-status/agent-status-projector.ts`，
  `APP_TO_AGENT_TYPE` 只有两行）。
- 相关 issue：
  - [#30173](https://github.com/stablyai/orca/issues/30173)（open）hook 桥装不上：`orca agent hooks status` 说 installed，
    但插件因缺 `dsh-hook-protocol` 无法 import —— 与本仓库遇到的安装前置一致；
  - [#28020](https://github.com/stablyai/orca/issues/28020)（closed）WSL 运行时下 dsh 面板不出现，原因是 `ORCA_AGENT_PANE`/`ORCA_AGENT_LAUNCH`
    缺 WSLENV 透传；已由 #31205 修；
  - [#25257](https://github.com/stablyai/orca/issues/25257)（closed）readiness 载体不匹配；该报告里的状态记录是
    `agentType: dsh` + `agentPresence.agent: dsh`，说明当时两条通道还没有分歧（dsh-tui 0.13 时代）；
  - [#27942](https://github.com/stablyai/orca/issues/27942)（open）DSH 子 agent 的 hook 抢占面板。
- 没有找到覆盖本症状（OSC `app=dsh-tui` 与 hook `dsh` 不同名导致回合内 hook 事件被丢）的 issue。

## 可直接提交的上游报告

> **Title:** [Bug]: DSH-TUI's OSC 7501 `app=dsh-tui` and the DSH hook bridge's `dsh` split the pane identity, so hook status is dropped mid-turn
>
> **Body:**
> Orca 1.4.238, `@deepseek-ai/dsh` 0.2.1-rc.2, `@deepseek-harness-tui/dsh-tui` 0.17.0, Windows (local runtime), hooks bridge installed
> (`dsh-hooks-claude-code` + `dsh-hook-protocol` + `dsh-llm` + `cordis` in the `dsh-tui` profile).
>
> `dsh-tui` reports the Program Status Protocol as `ESC ] 7501 ; state=…:app=dsh-tui`, and `program-status/agent-status-projector.ts` has no
> `APP_TO_AGENT_TYPE` entry for it, so the pane's OSC status carries `agentType: "dsh-tui"`. The managed bridge posts Claude-shaped events with
> `agentType: "dsh"` (`agent-hook-listener/providers/dsh-events.ts`). While the OSC record is `working` — i.e. for the whole turn — 
> `agent-hook-listener.ts` drops every bridge event as `inheritedFromActivePane`, and `last-status.json` never sees
> `UserPromptSubmit`/`PreToolUse`/`PostToolUse`/`Stop`; `worktree ps` shows no `prompt`/`toolName` for DSH panes. `SessionStart` only lands during boot,
> before any OSC record exists.
>
> Reproduction: create a pane with `dsh-tui .`, watch `last-status.json` writes (mtime), send a prompt with one tool call; the writes are all
> `agentType: "dsh-tui"` and no `dsh` record appears. Not sending a prompt leaves a `dsh` record with `providerSession` for as long as the pane idles.
> Module-level control: feed `OSC 7501 app=dsh-tui` to `createProgramStatusProjector()` → `agentType: "dsh-tui"`; add `'dsh-tui': 'dsh'` to the table →
> `agentType: "dsh"`; `resolveAgentStatusIdentity({existing: {agentType: 'dsh-tui', state: 'working'}, incoming: 'dsh'})` → `inheritedFromActivePane: true`,
> and with the mapping → `false`.
>
> Suggested fix: map `dsh-tui` → `dsh` in `APP_TO_AGENT_TYPE` (dsh-tui's program-status app id is a constant in its own source), and consider having
> the OSC ingest preserve an existing `providerSession` instead of replacing the whole record.

## 复现步骤

1. 备好 hook 桥（上面的 `dsh plugin … add`），`orca terminal create --worktree active --command "dsh-tui ."`；
2. 对 `%APPDATA%\orca\agent-hooks\last-status.json` 做 `mtime` 驱动的全量写入监控；
3. 向该面板发送一个带工具调用的提示词；
4. 观察：写入序列只有 `dsh-tui`，无 `dsh`；不发送提示词时，`dsh` + `providerSession` 会稳定存在。
