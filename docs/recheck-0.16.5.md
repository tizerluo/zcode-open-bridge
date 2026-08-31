# zcode-open-bridge 复测报告 — ZCode 3.10.2 / CLI 0.16.5 兼容性确认

日期：2026-09-01
调研人：主代理（Mac studio）
仓库：<https://github.com/tizerluo/zcode-open-bridge>

## 结论先行

- 0.16.1 → 0.16.5 **协议面兼容**：ACP bridge / MCP server / agent-help / review-gate 均无需代码改动；仓库本轮仅做文档适配与兜底模型名更新。
- 全部变化为**增量式**（新字段、新反向调用、新通知），无破坏性变更。
- 唯一的方法删除是 `automation/*`（本项目从未实现，零损失）。
- MCP 用户级注册键位实测为 `mcp.servers`（README 注册指引已同步修正）。

## 实证环境

| 平台 | App | CLI | CLI 路径 | 备注 |
| --- | --- | --- | --- | --- |
| macOS arm64（Mac studio） | 3.10.2（CFBundleShortVersionString） | 0.16.5（`zcode --version`） | `/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs` | 符号链接 `~/.local/bin/zcode` |

## ✅ 实测验证通过

### 1. 凭证

- `~/.zcode/v2/config.json` 结构未变：`provider.*.options.baseURL/apiKey`，`models` 的 key 即 canonical id。
- enabled provider 为 `builtin:zai-coding-plan`，models = [GLM-5.3, GLM-5.3-Flash, GLM-5-Turbo]，baseURL `https://api.z.ai/api/anthropic`。
- `shared/credentials.py` 实测读取成功。

### 2. headless CLI

- 仓库依赖的 `--prompt` / `--mode` / `--disallowed-tools` / `--no-color` / `--json` / `--attach` / `--resume` / `--target` 全部存在。
- `--json` 顶层 keys = [eventCount, projection, response, sessionId, traceId, turnId, usage]（response/usage 形态不变，新增字段为增量）。
- 带凭证 env 注入后实测 `response="OK"`。
- ⚠️ **`--allowed-tools` 与 `--max-turns` 仍然只是帮助文案、未注册 parseArgs**（实测 `Unknown option`）——agent-help 的「勿用」警告继续有效，适用范围扩为 0.16.1–0.16.5。

### 3. app-server 协议（活体探测，stdio 发无 jsonrpc 信封的 NDJSON）

- **旧信封探测**：发含 `jsonrpc` 键的请求 → `-32600 invalid_union`（unrecognized_keys: `jsonrpc`），错误帧 id 为固定字符串 `"invalid-message"`——与桥的探测实现预期一致。
- **`session/create`**：`{workspace:{workspacePath,workspaceKey}, mode}` 成功；result keys = [messages, projection, protocol, runtime, session, settings, slashCommands, todoGroups, todos]；sessionId 位于 `result.session.sessionId`（桥 `zcode-acp-bridge:1321-1322` 已用 `result.session.sessionId or result.sessionId` 兼容写法）；`protocol = {name:"ZCode Protocol", version:1}`。
- **反向调用 `session/requestRuntimePreferences`**（scope=runtime-materialization / user-execution）仍存在，三布尔应答体（nativeSearchEnhancementsEnabled/memoryEnabled/askUserQuestionAutoResolutionEnabled=false）仍被接受。
- **新增反向调用 `interaction/requestOfficialMcpAuthHeaders`**（params: mcpKey/pluginId/requestId/targetOrigin/workspace；官方插件 MCP 鉴权头）。实测不应答也不阻塞 `session/create`；桥对未知反向调用统一回 `-32601`（`zcode-acp-bridge:567-574` 逻辑），安全降级——官方鉴权类 MCP 在桥内不可用，无断链。
- **`session/subscribe`**：`{sessionId, deliveryKind:"desktop-continuous"}` OK，result 新增 `eventSeq`/`events` 字段（增量）；漏传 `deliveryKind` 仍报 `-32602`。
- **`session/send`**：`{sessionId, content}` → `{accepted:true, stateRevision}`，形态不变。
- **事件流实测序列**：`state.updated` ×2 → `session.titleUpdated` → `turn.started` → `session.updated` → `model.streaming`（kind=text_delta）→ `session.updated` → `turn.completed`。`turn.completed` payload keys = [cacheStats, duration, historyRoundCount, response, resultType, tokenCount, toolCallCount, usage]（response+usage 仍在，resultType 仍在，新增 cacheStats/duration/historyRoundCount/tokenCount/toolCallCount 为增量）。
- **新通知 `computer-use/operation-event` 与 `process/mcpTelemetry`**：走通用通知队列，不进事件翻译层，正确丢弃。

### 4. 方法存在性探测（空 params：-32602=存活、-32601=已删）

- **存活**：`session/fork`、`goal`、`compact`、`setModel`、`setMode`、`setThoughtLevel`、`cancelBackgroundTask`、`updateRuntimeModelConfig`、`list`、`resume`、`read`、`usage`、`subagents`、`messages`、`events`、`workspace/readState`、`generateText`、`setDefaultMode`、`upsertModelProvider`、`updateInteractionPreferences`、`mcp/list`。
- **0.16.5 新删除**（0.16.1 尚存）：`automation/create`、`automation/list`、`automation/checkTaskBinding`（-32601）——仅影响 upgrade-0.16.1-spec 的 P3 增强候选，本项目从未实现，零损失。
- 0.16 已删方法（`session/new`、`prompt`、`cancel`、`steer`、`rewind`、`prompt/enhance`、`initialize`）在 0.16.5 仍为 -32601，无变化。

### 5. MCP 注册键位

- `~/.zcode/cli/config.json` 用户级键位实测为 `mcp.servers`（嵌套 dict；bundle 含 `McpServers:"mcp.servers"` 配置键映射与 `mcp.servers must be a JSON object` 诊断串）。
- 顶层 `mcpServers` 键不被用户配置读取（该字符串属 plugin manifest/请求载荷）。README 注册指引已同步修正。

### 6. 测试套件

`python3 -m pytest tests/ -q` → 413 passed, 14 subtests passed。

## 复测方法（可复现）

- **活体探测**：spawn `zcode app-server --stdio`，注入 `ZCODE_MODEL` / `ZCODE_BASE_URL` / `ANTHROPIC_API_KEY`，按上述顺序发帧观察响应/通知。
- **headless**：`zcode --prompt "…" --mode yolo --no-color --json`。
