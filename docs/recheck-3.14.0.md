# zcode-open-bridge 复测报告 — ZCode App 3.14.0 / CLI 0.16.9

日期：2026-09-19
调研人：主代理（Mac studio）
仓库：<https://github.com/tizerluo/zcode-open-bridge>

## 结论先行

- **App 3.14.0 内嵌 CLI `--version` = 0.16.9**（版本号终于又动了，不再是 0.16.5 同号漂移）。**协议面与 App 3.12.3 的 0.16.5 构建完全一致**：方法存活性、信封行为、反向调用、通知种类、result keys 逐项相同——**桥零代码改动**，全链路实测通过（测试套件、headless CLI、app-server 活体探测、ACP 桥端到端真流式）。
- 变化集中在 **CLI 旗标面**：4 个旗标移除（实测 Unknown option）、7 个新增（`--cwd` 已验证接线）、`-p` 帮助标签改为 `--prompt`、slash 命令 14→15（新增 `/dwf`）。agent-help 静态清单已随本轮同步（`version_described` → 0.16.9）。
- npm 包装器 `zcode-app-cli` 同期升级 3.11.2-25 → **3.12.3-26**（vendor 的 `zcode-runtime` 仍标 0.16.5）：两代包装器 bundle 均已含 `nativeSearchEnhancementsEnabled`、无 `allowAgentTools` 字样，与官方 3.14.0 的 0.16.9 在桥相关面上实测行为一致（session/create + 现役应答体通过）。「同号构建漂移」结论再添一例，`--version` 仍不能作为唯一兼容性判据。

## 实证环境

| 平台 | App | CLI | CLI 路径 | 备注 |
| --- | --- | --- | --- | --- |
| macOS arm64（Mac studio） | 3.14.0（CFBundleShortVersionString） | 0.16.9 | `/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs` | 官方构建，经 `~/.local/bin/zcode-app` 符号链接可用；本轮探测全部 `ZCODE_BIN` 指向该官方构建 |
| （PATH 干扰项） | — | zcode-app-cli 3.12.3-26（社区，vendor zcode-runtime 0.16.5） | `~/.npm-global` 的 `zcode` | 本机 PATH 的 `zcode` 仍为社区包装器（自 3.11.2-25 升级），探测时须显式指 ZCODE_BIN |

## ✅ 实测验证通过（与 3.12.3 / 0.16.5 基线一致）

### 1. 测试套件

`python3 -m unittest discover -s tests` → **519 passed**（含本轮 agent-help 清单更新后的锁定断言）。

### 2. 凭证

- `~/.zcode/v2/config.json` 结构未变：`provider.*.options.baseURL/apiKey`，`models` 的 key 即 canonical id。
- enabled provider 仍为 `builtin:zai-coding-plan`，baseURL `https://api.z.ai/api/anthropic`；models 仍为 **[GLM-5.3, GLM-5.3-Flash]**。
- 本机 config 出现**第二个 enabled provider**（自建 `opencodex` 本地代理）：`shared/credentials.py` 取「第一个 enabled provider」（JSON 插入序）仍正确选中 `builtin:zai-coding-plan`，`--print-injected-env` 的 `ZCODE_BASE_URL` 残留检测（🚫 标注 + 自动用 config 值）工作正常。多 enabled provider 是用户自加配置所致，非 ZCode 版本变化；插入序依赖是既有行为，暂无需改动。

### 3. headless CLI

- `--prompt` / `--mode yolo` / `--disallowed-tools` / `--no-color` / `--json` 全部健在。
- `--json` 顶层 keys = [eventCount, projection, response, sessionId, traceId, turnId, usage]（与 0.16.5 基线一致）；带凭证 env 注入后实测 `response="收到"`，usage 结构含 cacheRead/WriteTokens。
- `--allowed-tools` 与 `--max-turns`：0.16.5 时代「帮助文案有、parseArgs 未接线」；**0.16.9 连帮助文案也移除了**（实测 Unknown option 照旧）。agent-help 的「勿用」警告继续有效。

### 4. app-server 协议（活体探测，stdio 发无 jsonrpc 信封的 NDJSON）

- **旧信封探测**：发含 `jsonrpc` 键的请求 → `-32600 invalid_union`（unrecognized_keys: `jsonrpc`），错误帧 id 仍为固定字符串 `"invalid-message"`——桥的纪元探测不受影响。
- **`session/create`**（`workspace` 为 object `{workspacePath, workspaceKey}`——注意发字符串会被 -32602 拒绝，桥本就发 object 形式）：result keys = [messages, projection, protocol, runtime, session, settings, slashCommands, todoGroups, todos]；`protocol = {name:"ZCode Protocol", version:1}`——与 0.16.5 完全一致。
- **反向调用**仍是同样两个：`session/requestRuntimePreferences`（桥现役三布尔应答体 `nativeSearchEnhancementsEnabled`/`memoryEnabled`/`askUserQuestionAutoResolutionEnabled` 被接受——该应答体自 0.16.1 适配起未变，0.16.9 schema 实测自 bundle zod 为 `.strict()`，三个键均在 schema 内）、`interaction/requestOfficialMcpAuthHeaders`（桥回 `-32601` 安全降级，无断链）。
- **通知**：`startup/storageState`（0.16.5 复测时的新增项，桥走通用通知路径安全丢弃）、`session/event` 等种类相同。

### 5. 方法存活性（空 params → -32602 即存活 / -32601 即已删）

- **存活**：`session/setModel`、`setMode`、`setThoughtLevel`、`fork`、`compact`、`goal`、`cancelBackgroundTask`、`workspace/generateText`——与 3.12.3 基线一致。
- **已删（-32601）**：`workspace/readState`、`setDefaultModel`、`setDefaultMode`、`setDefaultThoughtLevel`、`upsertModelProvider`、`removeModelProvider`、`updateProviderRegistry`（workspace/* 8 删 7）；`session/updateRuntimeModelConfig`；`updateInteractionPreferences`；`session/steer`、`session/rewind`、`prompt/enhance`——**删除面与 3.12.3 的 0.16.5 构建完全相同**，桥的泛化降级文案（「当前 ZCode 版本已移除该能力」）无需调整。

### 6. ACP 桥端到端（ZCODE_BIN = 官方 3.14.0 构建）

initialize → session/new → session/prompt 流式完成（`stopReason=end_turn`，sessionUpdate 正常分发）→ session/list 全通；桥 stderr 日志显示 `session/subscribe` 成功、`turn.started`/`turn.completed` 事件正常翻译。

## ⚠️ CLI 旗标面变化（0.16.9 vs 0.16.5，agent-help 清单已同步）

**已移除（实测 Unknown option）**：

| 旗标 | 0.16.5 状态 |
| --- | --- |
| `--permission-mode <mode>` | `--mode` 的 legacy 别名（帮助在列） |
| `--allow-main-worktree-yolo` | accepted no-op（帮助在列） |
| `--allowed-tools <list>` | 帮助在列但未接线（本清单一直标 ❌ 勿用） |
| `--max-turns <n>` | 帮助在列但未接线（本清单一直标 ❌ 勿用） |

**新增（0.16.9 help 在列；`--cwd` 已验证接线——传不可达路径报 `--cwd path is not accessible`）**：

`--cwd <path>`、`--target-replace`、`-c, --continue`、`--locale <locale>`、`--browser-use <mode>` + `--browser-executable <path>`、`--memory-bench`。

**标签/文案变化**：`-p` 的帮助标签为 `-p, --prompt`（旧清单记 `-p, --print`）；slash 命令 0.16.5 的 14 个 → **15 个**（新增 `/dwf [list|cancel|resume]`，dynamic workflow 管理）；子命令 9 个不变（app-server/commands/doctor/login/logout/plugins/skills/tui/version）。

## npm 包装器备注（PATH 干扰项）

`zcode-app-cli` 3.11.2-25 与 3.12.3-26 两代包装器 vendor 的 `zcode.cjs`（`--version` 均报 zcode-runtime 0.16.5）bundle 内**均无 `allowAgentTools` 字样、均含 `nativeSearchEnhancementsEnabled`**——与官方 3.14.0 的 0.16.9 同代 schema。对包装器 runtime 实测：`session/create` + 现役三布尔应答体通过。包装器「同号 0.16.5、内容与官方 App 构建不同」的现象与 [recheck-3.12.3.md](recheck-3.12.3.md) 的方法论结论一致：兼容性判断必须落到活体探测，`--version` 不是唯一判据。

## 复测方法（可复现）

- **环境**：`export ZCODE_BIN=~/.local/bin/zcode-app`（官方 3.14.0 构建的符号链接），确认 `--version` = 0.16.9 后再探测（PATH 里的社区 `zcode` 包装器会干扰）。
- **活体探测**：spawn `zcode app-server --stdio`，注入 `ZCODE_MODEL` / `ZCODE_BASE_URL` / `ANTHROPIC_API_KEY`，发无 `jsonrpc` 键的 NDJSON 帧，观察响应/通知；反向调用以现役三布尔应答体应答。
- **方法存在性**：空 params 发一次——`-32602`（参数校验）即存活，`-32601` 即已删。
- **headless**：`zcode --prompt "…" --mode yolo --no-color --json`。
- **桥端到端**：`ZCODE_BIN` 指向官方构建后按 README 的 ACP bridge 用法跑 initialize → session/new → session/prompt。
