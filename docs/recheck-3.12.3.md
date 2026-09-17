# zcode-open-bridge 复测报告 — ZCode App 3.12.3 兼容性确认（CLI 版本号 0.16.5 未变，构建漂移）

日期：2026-09-17
调研人：主代理（Mac studio）
仓库：<https://github.com/tizerluo/zcode-open-bridge>

## 结论先行

- App 3.12.3 内嵌 CLI 的 `--version` 仍为 **0.16.5**（与 App 3.10.2 同号，见 [recheck-0.16.5.md](recheck-0.16.5.md)），但**构建内容漂移**：扩展透传面出现真实删除（workspace/* 8 删 7、`session/updateRuntimeModelConfig`、`updateInteractionPreferences`）。**`--version` 不能作为唯一兼容性判据**——同版本号、不同构建、不同协议面，这是本轮最重要的方法论教训，也是 issue #21 推动能力探测动态化的直接论据。
- **核心链路零改动可用**：ACP bridge（initialize → session/new → session/prompt 流式全流程）、headless CLI、MCP server、review 体系全部实测通过，测试套件 `python3 -m pytest tests/ -q` 与 0.16.5 复测基线一致。
- 变化集中在**扩展透传面**：被删方法桥不崩，后端 `-32601` 原样透传给 ACP client（本轮适配前）；本轮仓库适配 = 文档更新 + `_passthrough_error` 泛化（透传方法的后端 `-32601` 统一翻译为「当前 ZCode 版本已移除该能力」文案，不再依赖硬编码方法清单，随本 PR）。

## 实证环境

| 平台 | App | CLI | CLI 路径 | 备注 |
| --- | --- | --- | --- | --- |
| macOS arm64（Mac studio） | 3.12.3（CFBundleShortVersionString） | 0.16.5（`zcode --version`，同号漂移构建） | `/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs` | 官方构建经 `~/.local/bin/zcode-app` 符号链接可用；本轮探测全部 `ZCODE_BIN` 指向该官方构建 |
| （PATH 干扰项） | — | zcode-app-cli 3.11.2-25（社区，vendor zcode-runtime 0.16.5） | `~/.npm-global` 的 `zcode` | 本机 PATH 的 `zcode` 现为社区包装器，与官方构建区分；探测时须显式指 ZCODE_BIN |

## ✅ 实测验证通过（与 0.16.5 基线一致）

### 1. 测试套件

`python3 -m pytest tests/ -q` → **414 passed, 14 subtests passed**（与 2026-09-01 的 0.16.5 复测基线完全一致；本轮加入泛化降级用例后为 417 + 28 subtests）。

### 2. 凭证

- `~/.zcode/v2/config.json` 结构未变：`provider.*.options.baseURL/apiKey`，`models` 的 key 即 canonical id。
- enabled provider 仍为 `builtin:zai-coding-plan`，baseURL `https://api.z.ai/api/anthropic`。
- 模型面变化（服务端下发，见下 ⚠️）：models 从 [GLM-5.3, GLM-5.3-Flash, GLM-5-Turbo] 变为 [GLM-5.3, GLM-5.3-Flash]。

### 3. headless CLI

- `--prompt` / `--mode yolo` / `--disallowed-tools` / `--no-color` / `--json` 全部健在。
- `--json` 顶层 keys = [eventCount, projection, response, sessionId, traceId, turnId, usage]（与 0.16.5 基线一致）；带凭证 env 注入后实测 `response="OK"`。
- `--allowed-tools` 与 `--max-turns` 仍是帮助文案有、parseArgs 未接线（实测 `Unknown option '--max-turns'`）——agent-help 的「勿用」警告继续有效，适用范围扩为 0.16.1–0.16.5（含 3.10.2 与 3.12.3 两个构建）。

### 4. app-server 协议（活体探测，stdio 发无 jsonrpc 信封的 NDJSON）

- **旧信封探测**：发含 `jsonrpc` 键的请求 → `-32600 invalid_union`（unrecognized_keys: `jsonrpc`），错误帧 id 仍为固定字符串 `"invalid-message"`——信封拒绝先于方法查找，与方法存亡无关，**桥的纪元探测不受本轮删除影响**。
- **`session/create`**：result keys = [messages, projection, protocol, runtime, session, settings, slashCommands, todoGroups, todos]；sessionId 位于 `result.session.sessionId`；`protocol = {name:"ZCode Protocol", version:1}`——与 0.16.5 完全一致。
- **`session/subscribe`**（`deliveryKind:"desktop-continuous"`）result keys = [eventSeq, events, sessionId]。
- **`session/send`** → `{accepted:true, stateRevision:1}`。
- **`turn.completed`**（在 `session/event` 通知内）payload 含 response + usage（信封 keys: deliveryKind/eventId/payload/seq/sessionId/timestamp/traceId/turnId/type）。
- **反向调用**仍是同样两个：`session/requestRuntimePreferences`（三布尔应答体仍被接受）、`interaction/requestOfficialMcpAuthHeaders`（桥回 `-32601` 安全降级，官方鉴权类 MCP 在桥内不可用，无断链）。

### 5. ACP 桥端到端

`ZCODE_BIN` 指向官方 3.12.3 构建：initialize → session/new → session/prompt 流式完成（`stopReason=end_turn`，agent_message_chunk 文本正常，sessionUpdate 种类含 agent_message_chunk/agent_thought_chunk/usage_update）。

### 6. 核心 session/* 方法面存活（空 params → -32602 即存活）

`setModel`、`setMode`、`setThoughtLevel`、`fork`、`compact`、`goal`、`cancelBackgroundTask`、`list`、`resume`、`read`、`usage`、`messages`、`events`、`subagents`、`mcp/list` 全部存活；`session/list` 空参直接 SUCCESS（比 0.16.5 的 `-32602` 更宽松，无害）。

## ⚠️ 协议面变化（App 3.12.3 的 0.16.5 构建 vs App 3.10.2 的 0.16.5 构建）

### 已删除（后端返 -32601 "Method not found"，均实测两次确认）

- `workspace/readState`、`workspace/setDefaultModel`、`workspace/setDefaultMode`、`workspace/setDefaultThoughtLevel`、`workspace/upsertModelProvider`、`workspace/removeModelProvider`、`workspace/updateProviderRegistry`——**workspace/* 面 8 个删 7 个，仅 `workspace/generateText` 存活**（空 params → -32602 参数校验，即方法仍在）。
- `session/updateRuntimeModelConfig`。
- `updateInteractionPreferences`（桥从未实现，无桥面影响）。
- 已确认 `setDefault*` **没有搬家**到 session/ 命名空间：`session/setDefaultModel` 等同样 `-32601`。

### 新增通知

- `startup/storageState`：桥走通用通知路径安全丢弃，端到端实测无碍。同批观察到的通知种类：computer-use/operation-event、process/mcpTelemetry、session/event、state.updated、v4/telemetry/event——除 `startup/storageState` 外均与 0.16.5 相同。

### 模型面

- enabled provider 的 models 从 [GLM-5.3, GLM-5.3-Flash, GLM-5-Turbo] 变为 **[GLM-5.3, GLM-5.3-Flash]**（服务端下掉了 GLM-5-Turbo；README 模型面注记已同步）。

### 桥的透传降级实测（本轮适配的触发点）

- workspace/readState 透传在 3.12.3 上返回 `-32601` 错误：桥不崩，错误原样透传给 ACP client（`-32603 "zcode readState failed: Method not found"` 形态）——与「已删方法应有明确文案」的既有降级（原 `_REMOVED_IN_016` 硬编码清单）不一致。
- **本轮修复**：`zcode-acp-bridge` 的 `_removed_method_error` 泛化为 `_passthrough_error`——任何透传/扩展方法的后端 `-32601` 都翻译为「当前 ZCode 版本已移除该能力 (<方法名>)」文案（错误码保持 `-32601`），不再依赖硬编码清单（zcode review P3-3 预警的「未来版本再删方法时硬编码清单会误导」在本轮成真）；非 `-32601` 错误码维持 `-32603 "zcode X failed"` 原文透传。核心协议路径（create/send/stop/list/resume）的 `-32601` 属深度异常，刻意保留原始错误，不套该文案。

## 方法论教训（写进 issue #21 的论据）

**CLI 版本号相同 ≠ 协议面相同**。0.16.1 → 0.16.5 的复测可以靠 `--version` 门控，是因为当时「一个版本号对应一个构建」的经验假设成立；App 3.12.3 打破了这个假设——`--version` 仍是 0.16.5，协议面却删了 9 个方法、加了 1 个通知。兼容性判断必须落到**活体探测**（方法存在性、result keys、通知种类），这正是 issue #21 要求把 agent-help 的静态能力清单动态化的理由。本报告第 6 节的「空 params → -32602/-32601」探测法即最小可行的动态探测。

## 复测方法（可复现）

- **环境**：`export ZCODE_BIN=~/.local/bin/zcode-app`（官方 3.12.3 构建的符号链接），确认 `--version` = 0.16.5 后再探测（注意 PATH 里的社区 `zcode` 包装器会干扰）。
- **活体探测**：spawn `zcode app-server --stdio`，注入 `ZCODE_MODEL` / `ZCODE_BASE_URL` / `ANTHROPIC_API_KEY`，发无 `jsonrpc` 键的 NDJSON 帧，观察响应/通知。
- **方法存在性**：空 params 发一次——`-32602`（参数校验）即存活，`-32601` 即已删；每个 -32601 结论复核第二次。
- **headless**：`zcode --prompt "…" --mode yolo --no-color --json`。
- **桥端到端**：`ZCODE_BIN` 指向官方构建后按 README 的 ACP bridge 用法跑 initialize → session/new → session/prompt。
