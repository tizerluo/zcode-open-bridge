# zcode-open-bridge 升级调研 — 适配 ZCode 3.6.5 / CLI 0.16.1

日期：2026-08-06
调研人：Kimi（Mac mini）
仓库：<https://github.com/tizerluo/zcode-open-bridge>

## 结论先行

- 桥上次适配到 **CLI 0.15.0 / App 3.3.0**；当前最新 **App 3.6.5（2026-08-03 发布）/ CLI 0.16.1**。
- macOS（本机 `/Applications/ZCode.app`）与 Linux（contabo、GC-8G，`/opt/ZCode/app`，AppImage 解压安装）**同为 3.6.5 + CLI 0.16.1，协议面完全一致**，一份适配两平台通用。
- **MCP server / agent-help 路径基本不受影响**（headless CLI 实测仍工作）；**ACP bridge 全断**，app-server 协议三层（信封、方法名、事件模型）全部变更，需要重写适配层。

## 实证环境

| 平台 | App | CLI | CLI 路径 | Node |
| --- | --- | --- | --- | --- |
| macOS arm64（Mac mini） | 3.6.5 | 0.16.1 | `/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs` | 系统 node v26 |
| Linux x64（GC-8G / contabo） | 3.6.5 | 0.16.1 | `/opt/ZCode/app/resources/glm/zcode.cjs` | 系统 node v22.23.1 |

`zcode.cjs` 带 `#!/usr/bin/env node` shebang，**Node.js 是使用 CLI 的硬前提**（README 未写，应补）。

## ✅ 不受影响的部分（实测验证）

1. **headless CLI**：`zcode --prompt "…" --attach … --mode plan --json` / `--resume` / `--target` 在 0.16.1 全部实测通过，JSON 输出结构多了 `traceId`、`turnId`、`eventCount`、`projection` 字段（向后兼容式新增）。
   → `mcp-server` 的 `zcode_review`、`agent-help` 能力发现不用动。
2. **凭证读取**：`~/.zcode/v2/config.json` 结构未变（`provider.*.options.baseURL/apiKey`、`models` 的 key 即 canonical model id）。**env 注入三件套（`ZCODE_MODEL` / `ZCODE_BASE_URL` / `ANTHROPIC_API_KEY`）在 0.16.1 的 app-server 里仍被接受**（实测：注入后 `session/create` 成功，模型解析为 `anthropic/GLM-5.2`，source=config）。
   → `shared/credentials.py` 不动。
3. **会话存储**：`~/.zcode/cli/db/db.sqlite` 仍在，`--prompt` 与 app-server 共享存储。
4. **流式事件核心**：`model.streaming` 事件的 `text_delta` 逐段推送在 0.16.1 依然存在（实测收到 `"kind":"text_delta","delta":"OK"`），真流式概念可保留，只需改字段映射。

## ❌ ACP bridge 全断：0.15.0 → 0.16.1 协议变更清单（全部实测）

### 1. 消息信封（第一层就过不去）

- 不再接受 `{"jsonrpc":"2.0"}` 字段：发标准 JSON-RPC 直接 `-32600 invalid_union`（zod 校验，`jsonrpc` 是 unrecognized key）。
- 新信封 = 去掉 `jsonrpc` 的 JSON-RPC：请求 `{id, method, params}`，通知 `{method, params}`，响应 `{id, result}` / `{id, error}`。
- bridge 的 JSON-RPC 帧层要删掉 `jsonrpc` 字段。

### 2. 方法名重命名 / 删除

| 旧（0.15.0，bridge 现用） | 0.16.1 状态 | 替代 |
| --- | --- | --- |
| `initialize` | ❌ -32601 | 无握手方法，`session/create` 直接返回 `protocol:{name:"ZCode Protocol",version:1}`（可做版本探测） |
| `session/new` | ❌ -32601 | `session/create`，参数从 `cwd` 改为 `workspace:{workspacePath, workspaceKey}`（本地场景 `workspaceKey = workspacePath`，已从 bundle 函数证实） |
| `session/prompt` | ❌ -32601 | `session/send`（`{sessionId, content}`，返回 `{accepted, stateRevision}`） |
| `session/cancel` | ❌ -32601 | `session/stop`；另有 `session/close` |
| `session/rewind` / `session/rewindCascade` | ❌ 字符串已从 bundle 消失 | 无（rewind 只剩 slash 命令 `/rewind` 与事件 `rewind.triggered`） |
| `session/steer` | ❌ 消失 | 疑似并入 `session/send`（turn 进行中发送即为 steer，事件有 `turn.steerQueued/steerDrained`） |
| `prompt/enhance`（3 个） | ❌ 消失 | 无 |

存活且实测仍在 bundle 的方法：`session/fork`、`session/goal`、`session/compact`、`session/setModel`、`session/setMode`、`session/setThoughtLevel`、`session/cancelBackgroundTask`、`session/list`、`session/resume`、`session/read`、`workspace/readState|generateText|setDefault*|upsertModelProvider|removeModelProvider|updateProviderRegistry`。

### 3. 新增 server→client 反向调用（旧 bridge 没有的概念）

`session/create` 和 `session/send` 时，server 会**反向请求 client**：

```json
{"id":"server-1","method":"session/requestRuntimePreferences",
 "params":{"sessionId":"sess_…","scope":"runtime-materialization"}}
```

（send 时 scope 为 `user-execution`）。client **必须应答**，应答 schema（bundle zod 实证）：

```json
{"id":"server-1","result":{"nativeSearchEnhancementsEnabled":false,
 "memoryEnabled":false,"askUserQuestionAutoResolutionEnabled":false}}
```

实测：应答后 create 顺利完成；旧 bridge 没有响应 server 请求的代码路径，会永久卡住。这是 ACP bridge 适配的**最高优先级单项**。

### 4. 订阅与事件模型

- `session/subscribe` 新增必填参数 `deliveryKind`，枚举 `desktop-continuous` / `web-remote-replayable`（实测 `desktop-continuous` 可用）。
- 事件统一走 `session/event` 通知，结构 `{seq, eventId, timestamp, traceId, payload:{…}}`，payload 类型实测到：
  - `turn.started`（turnNumber/input/messageId）
  - `model.streaming`（`kind:"text_delta"`, `delta`, `assistantMessageId`）→ 映射 ACP `agent_message_chunk`
  - `session.updated`（model/modelRef/iteration）
  - `turn.completed`（`response` 全文 + `usage` 完整 token 明细）→ 映射 turn 结束
  - `session.titleUpdated`
- 另有平行的 `state.updated` patch 通知（status/mode/model 投影）与 `v4/telemetry/event`（可忽略）。
- 旧 bridge 的事件翻译层（`test_event_translator.py` 覆盖的那套）字段名全变，需按新 payload 重写。

### 5. 新增方法族（可作为增强暴露）

- `automation/create|list|update|delete|checkTaskBinding` — 对应 3.4.2 引入的定时任务
- `session/usage`、`session/subagents`、`session/messages`、`session/events`、`session/requestRuntimePreferences`（server 侧）、`workspace/updateInteractionPreferences`、`mcp/list`

## ⚠️ 需要小改 / 复核的部分

1. **README 全面过时**：兼容性表（0.15.0→0.16.1、App 3.3.0→3.6.5）、`session/*` 扩展方法表、`prompt/enhance` 章节、"CLI 版本自 0.15.0 起未再升"的注释。
2. **Linux 安装指引缺失**：现在只有 macOS 符号链接。补：
   ```bash
   # Linux（AppImage 解压安装到 /opt/ZCode 时）
   ln -s /opt/ZCode/app/resources/glm/zcode.cjs ~/.local/bin/zcode
   ```
   并写明 **Node.js ≥ 18 为前置依赖**（shebang 是 `#!/usr/bin/env node`）。
3. **MCP 注册路径复核**：`~/.zcode/cli/config.json` 在本机现在只有 `{"plugins":…}`，没有 `mcp` key；0.16.1 无凭证时的报错文案是 "Create ~/.zcode/cli/config.json with an explicit model provider"。README 说"注册到 cli/config.json 的 mcp.servers"需要重新实测确认键位是否变化。
4. **TUI 限制可能已解除**：0.16.1 help 写明 "With no command, zcode opens the full-screen TUI"，`@zcode/tui` 已打包进 bundle（README 限制 #6 说独立终端缺该模块）。需交互式终端实测后更新限制说明。
5. **agent-help 补充新 CLI 参数**：`--max-turns`、`--allowed-tools` / `--disallowed-tools`、`--settings`、`--locale`、`--max-turns`、`-c/--continue`、`--target-replace`、`--browser-use`、`--verbose`；新 slash 命令 `/expert`、`/skill`、`/mcp`、3.6.5 的 `/side`、`/btw`。
6. **模型面**：内置供应商仍是 GLM-5.2 / GLM-5-Turbo（canonical id 逻辑不变）；3.4.2 起支持 Kimi K3、3.6.5 加 Kimi K3 256K 且官方供应商可添加其他官方模型 → 文档提一句即可，代码不用动。
7. **降级策略**：bridge 调已删除方法（steer/rewind/enhance）现在会收到 `-32601 Method not found`，README 说扩展方法失败透传 `-32603` 的描述要改；bridge 应把 `-32601` 映射为"该 ZCode 版本不支持此能力"。

## 建议升级优先级

| 优先级 | 内容 | 工作量估计 |
| --- | --- | --- |
| P0 | ACP bridge：信封去 `jsonrpc`、方法 rename（create/send/stop）、`session/requestRuntimePreferences` 应答、`subscribe` 加 `deliveryKind`、事件翻译层按新 payload 重写 | 中（核心 ~2 个文件，但全是实证过的确定改动） |
| P1 | README 兼容性表 + Linux 安装/Node 前置说明；`prompt/enhance`、`steer`、`rewind` 章节标记"0.16 已移除" | 小 |
| P2 | agent-help 补新参数/slash 命令；实测确认 MCP 注册键位 | 小 |
| P3 | 增强：`automation/*` 定时任务、`session/usage`、`session/subagents` 暴露为扩展方法 | 中 |
| P4 | TUI 实测，更新限制 #6 | 极小 |

## App 3.3.0 → 3.6.5 功能面变化（changelog 摘要，与桥相关性低但宜知悉）

- 3.3.4 后台任务（子智能体/bash 后台执行）
- 3.4.2 定时任务（cron）、辅助对话、Kimi K3、远程工作区/WSL、插件市场改版、非项目会话
- 3.5.2 内置网页应用、PDF 预览、统一外观设置、仓库知识库目录
- 3.6.5 项目维度记忆、Kimi K3 256K、最大输出长度设置、会话搜索增强开关、全局防休眠、`/side` `/btw` 辅助对话命令

## 实证记录（可复现）

本调研全部协议结论来自对 0.16.1 app-server 的活探测（stdio 发 newline-delimited JSON）：

```bash
# 信封探测：发 {"jsonrpc":"2.0",…} → -32600 invalid_union；去掉 jsonrpc 键 → 正常 -32601/-32602
# 方法探测：session/new、session/prompt、session/cancel、initialize → -32601 Method not found
# 全流程：session/create {workspace:{workspacePath,workspaceKey},mode}
#   → 应答 session/requestRuntimePreferences
#   → session/subscribe {sessionId, deliveryKind:"desktop-continuous"}
#   → session/send {sessionId, content}
#   → 收 turn.started / model.streaming(text_delta) / turn.completed(usage)
# headless：zcode --prompt "…" --mode plan --json → 正常返回 response+usage
```

凭证注入方式与桥一致：`ZCODE_MODEL` / `ZCODE_BASE_URL` / `ANTHROPIC_API_KEY` 从 `~/.zcode/v2/config.json` enabled provider 动态读出注入 env。

## 勘误（Wave 2 复审实测）

### §4 事件结构补记

- `session/event` 的 `type` 与 `deliveryKind` 在 **params 顶层**（`{seq, eventId, timestamp, traceId, type, deliveryKind, payload:{…}}`），payload 内不含这两个字段。例外：`session.updated` 的 payload 内有 `type` 子类型字段（`model_request_started` / `model_request_completed`）。
- 补记 `turn.failed`：无 `resultType`，载荷 `{error:{type, code, message, detail, stack}, turnPhase}`，是终止帧。
- `process` / `resourceSample` 通知存在，桥正确丢弃（不进事件翻译层）。

### §2 历史勘误

- 桥在 main 上**原本就用**无 `jsonrpc` 信封 + `session/create`/`session/send`/`session/stop`（0.15 服务端兼容这套调用面）；真正的断点是 0.16 新增 `session/requestRuntimePreferences` 反向调用（旧桥无应答代码路径，永久卡住）与已删方法（`steer`/`rewind*`/`prompt/enhance*`），并非 §1/§2 字面表述的"信封与 rename 导致全断"。

### §2 存活清单补记

- `session/updateRuntimeModelConfig`：commander 实测 0.16.1 仍存活，但 schema 新要求 `runtimeModel.revision`（string）必填。
