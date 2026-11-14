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
python /path/to/orca-osw/skill/osw.py models omp
python /path/to/orca-osw/skill/osw.py models kimi
python /path/to/orca-osw/skill/osw.py models grok
python /path/to/orca-osw/skill/osw.py models dsh
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
4. 调用方（TTY 下自动检测、`ORCA_TERMINAL_HANDLE`、对话会话的 `ORCA_AGENT_SESSION_ID`，或通过 `--caller-terminal` 显式指定的地址）收到单行完成报告：

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

## 12. 在另一台主机上跑 agent

机器登记一次，之后按名字派单：

```bash
python /path/to/orca-osw/skill/osw.py host add fnnas --ssh fnnas --worktree path:/srv/repo
python /path/to/orca-osw/skill/osw.py host doctor fnnas --provider claude
python /path/to/orca-osw/skill/osw.py new --on fnnas claude --prefix code "修复失败的测试"
```

`host doctor` 依次检查：该机能否通过 SSH 应答、那台机器的 Orca 是否已登记 workspace、是否有 worktree 匹配选择器、provider CLI 是否已安装。首个失败即停止，并打印该阶的错误码：`host_unreachable`、`host_permission_denied`、`remote_no_project`、`remote_no_worktree` 或 `provider_missing`。完整错误码列表与交接细节见 [README_zh.md](README_zh.md#跨主机运行)。

派单走的是经典通道：OSW 在那台机器上创建终端（`terminal create --environment fnnas --worktree <选择器> --command "<provider 绝对路径> <自主权参数>"`），由本机 watcher 发送提示、读取面板，并通过 SSH 收取交接文件。远端 agent 没有 Dispatch id，Run 留在本机、远端用不到。Orca 的受监督 `worker-start --on` 通道不再使用：Orca 1.4.248 面对配对服务器时，就绪判定是 `host_indeterminate`——`worker-start` 停在 `agent_readiness` 达 240s，而远端面板明明停在输入框；对同一个空闲面板执行 `terminal wait --for tui-idle` 同样超时；`terminal send`/`read` 与 `worktree ps` 则都有应答。

```
# new --on 在远端实际执行的东西
orca terminal create --environment fnnas --worktree <选择器> \
     --command "/usr/local/bin/claude --dangerously-skip-permissions"
```

如果该账户从未运行过这个 provider，先在那个面板里手动启动一次：首次运行的主题/安全/绕过确认弹窗，任何就绪探测都清不掉。目录信任是例外——`new` 会通过 SSH 预写，因此那一步提示不会出现。

那台机器只需要：本机可达的 `sshd` 且本机密钥已授权、Orca 里已登记的 workspace、以及 provider CLI。不需要安装 OSW、不需要常驻进程、不需要辅助脚本、也不需要克隆仓库。Orca 配对对运行是可选的——它用于在 Orca 界面里查看远端终端，也是派单的寻址方式。

之后用 `<host>/<agent_id>` 寻址（`fnnas/code_001`；`local/` 可省略）。这一轮结束时，交接文件会被复制到本地 `.orca/osw/handoffs/`，并按与本地相同的规则判定，完成报告仍送到本机的监护方终端：

```bash
python /path/to/orca-osw/skill/osw.py list --all     # HOST 列，本地与远端同一张表
python /path/to/orca-osw/skill/osw.py status --all   # 按主机统计
python /path/to/orca-osw/skill/osw.py del fnnas/code_001 --close
```

`host list` 显示已登记主机以及它们当前是否可达；`host rm <name>` 删除记录。

## 13. 控制平面（mailbox）

六行命令跑通一遍。邮箱是可选的；从不启动它时，前面所有命令行为不变。

```bash
python /path/to/orca-osw/skill/osw.py mailbox start
python /path/to/orca-osw/skill/osw.py mailbox send --kind request --text "修复失败的测试"
python /path/to/orca-osw/skill/osw.py mailbox wait --as router --timeout 0
python /path/to/orca-osw/skill/osw.py mailbox decide msg_... --action accept --ticket code_001 --reason "明确人工请求"
python /path/to/orca-osw/skill/osw.py mailbox status --json
python /path/to/orca-osw/skill/osw.py mailbox stop
```

`wait` 为该角色租约并确认等待中的卡片；`decide` 输出 `decision_id`，随后由 Dispatcher 通过常规的 `new`/`use`/`stop` 路径执行。需要回查某个 Ticket 时用 `mailbox tickets`。细节见 [README_zh.md](README_zh.md)。
