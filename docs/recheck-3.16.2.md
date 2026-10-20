# zcode-open-bridge 复测报告 — ZCode App 3.16.2 / CLI 0.17.0

日期：2026-10-20
调研人：主代理 + 实现代理（Mac studio）
仓库：<https://github.com/tizerluo/zcode-open-bridge>

## 结论先行

- **App 3.16.2 内嵌 CLI `--version` = 0.17.0**（版本号自 0.16.9 再动）。**核心链路零改动兼容**：信封行为、反向调用、create→subscribe(deliveryKind=desktop-continuous)→send→事件流→stop→resume、删除面 12 项、headless CLI 全部实测通过；但**三处面破坏，本 PR 已在桥内适配**（见下）：
  1. `session/list` 的 strict Zod 拒收 `includeArchived` 键（桥原无条件发 `false` 使 list 整体失败）→ 桥去键（era 无关）；
  2. 模型身份改名 `builtin:zai-coding-plan/GLM-5.3` → **`zai/glm-5.3`**（小写、精确大小写敏感）→ 桥的 session/new 字符串 model 增加 casefold 折叠回退（catalog 中介，用规范 ID）；
  3. `workspace/generateText` 参数键 `modelRef` → **`selection`**（且该改名**早于 0.17.0**）→ 桥先发 selection、键级拒收特征时一次性回退 modelRef。
- 社区包装器 `zcode-app-cli` 3.16.2-38 已 vendor `zcode-runtime` 0.17.0（2026-10-20 当天升级），与官方 App 3.16.2 在桥相关面上实测行为一致（`includeArchived` 同样被拒、`{}`/`{limit}`/`{workspace}` 同样接受）。「同号构建漂移」再添实锤：`zcode-app-cli` 3.15.3-37 vendor 的 0.16.9 与官方 App 3.14.0 的 0.16.9 构建内容不同（前者 generateText 已收 selection、help 已列 `--mode guarded` 与 `/expert`）。
- CLI 旗标面：与 App 3.15.3 的 vendor 0.16.9 逐项 diff 后**唯一确证新增 `--toolsearch-mode <auto|native|local|off>`**（默认 off）；`--disallowed-tools` 的 help 标签现带 camelCase 别名形态（`--disallowed-tools, --disallowedTools <tools...>`）。slash 命令 15 个不变。
- 最小 headless 环境：**模型引导配方在 0.17.0 上仍可用，但机制注记需修正**——0.17.0 runtime 把入口解析到真实路径（符号链接不再保持入口目录），符号链接入口 + 邻置 `provider/` 的组合失效；两条实测可行路径见 §7。

## 实证环境

| 平台 | App | CLI | CLI 路径 | 备注 |
| --- | --- | --- | --- | --- |
| macOS arm64（Mac studio） | 3.16.2（CFBundleShortVersionString，build 3.16.2.8769） | 0.17.0 | `/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs` | 官方构建，经 `~/.local/bin/zcode-app` 符号链接可用 |
| （PATH 干扰项） | — | zcode-app-cli 3.16.2-38（社区，vendor zcode-runtime 0.17.0） | `~/.npm-global` 的 `zcode` | 2026-10-20 午间起与官方同 runtime；当日早些时候的「社区 0.16.9 后端」指认已失效 |
| （回溯用） | — | zcode-app-cli 3.15.3-37（社区，vendor zcode-runtime 0.16.9） | npm 可复装（`npm i zcode-app-cli@3.15.3-37`） | 用于 0.16.9 等价性实证与旗标 diff；注意 3.14.0-27 的 vendor runtime 在本机当前 `~/.zcode` 状态下无法启动（其内置 `provider/zcode-builtin.json` 的 `providerRules[8].access.mode` 被 0.16.9 schema 拒收——包装器打包 artifact，与本仓无关） |

## ✅ 实测验证通过（零改动兼容面）

### 1. 测试套件

适配后 `python3.14 -m pytest tests/ -q` → **606 passed + 58 subtests**（适配前基线 591 + 56；+15 用例见 §4）、`ruff check packages/ tests/` 全绿。

### 2. app-server 协议（活体探测，stdio 发无 jsonrpc 信封的 NDJSON）

- **旧信封探测**：含 `jsonrpc` 键的请求 → `-32600`，错误帧 id 仍为固定 `"invalid-message"`——桥的纪元探测不受影响。
- **反向调用**仍是 `session/requestRuntimePreferences`，必须以**无 jsonrpc 键**的三布尔体应答（带 jsonrpc 键被 -32600 拒收）——桥现役应答体直接可用。
- **`session/create`**（workspace 必须 object `{workspacePath, workspaceKey}`）：result keys 含 messages/projection/protocol/runtime/session/settings/slashCommands/todos/todoGroups；`protocol = {name:"ZCode Protocol", version:1}`；sessionId 在 `result.session.sessionId`。
- **核心链路**（原始协议 + 经桥 E2E 两种方式实测）：create → subscribe（deliveryKind=desktop-continuous + includeSnapshot + afterSeq）→ send `{sessionId, content}` → 事件流（turn.started/model.streaming/…）→ stop/list/resume 全通。`session/list` 裸参数 `{}`、`{workspace}`、`{limit}` 三形态全接受；result.session 条目 keys = [createdAt, mode, sessionId, sessionKind, status, title, titleSource, traceId, updatedAt, workspace]。
- **新通知**（桥走通用忽略路径，无害）：`process/mcpTelemetry`、`v4/telemetry/event`、`computer-use/operation-event`、`session.titleUpdated`。
- **headless CLI**：`--prompt/--mode yolo/--json` 全通；`--json` 顶层 keys 不变，usage 新 keys = `source`/`modelRequestCount`/`webFetchRequests`/`webSearchRequests`/`reasoningTokens`（`totalTokens`/`cacheRead/WriteTokens` 仍在）。实测 `response="收到"`（完整桌面配置机器）。

### 3. 方法存活性（空 params → -32602 即存活 / -32601 即已删）

- **存活**：`session/setModel`、`setMode`、`setThoughtLevel`、`fork`、`compact`、`goal`、`cancelBackgroundTask`、`workspace/generateText`、`session/list`、`session/update`——与 0.16.9 基线一致。
- **已删（-32601，删除面 12 项不变）**：`workspace/readState`、`setDefaultModel`、`setDefaultMode`、`setDefaultThoughtLevel`、`upsertModelProvider`、`removeModelProvider`、`updateProviderRegistry`（workspace/* 8 删 7）；`session/updateRuntimeModelConfig`；`updateInteractionPreferences`；`session/steer`、`session/rewind`、`prompt/enhance`。桥的泛化降级文案无需调整。

## ⚠️ 三处破坏与修法（本 PR 适配）

### ① `session/list` 拒收 `includeArchived`

- 0.17.0 strict Zod：`{"includeArchived": ...}` → `-32602 Invalid params — (root): Unrecognized key: "includeArchived"`。桥原代码无条件发该键 → **session/list 自 0.17.0 起整体失败**（真机复现：桥报 -32603 "zcode list failed"）。
- **era 无关性实证**（2026-10-20，`zcode-app-cli` 3.15.3-37 vendor 的真 0.16.9 runtime）：`{}`、`{includeArchived: false}`、`{includeArchived: true}` 三形态返回**逐项等价**（建会话前后各测一轮：50/51 条、result 形状相同、键均被接受）——0.16 默认行为与显式 false 无差，去键安全。
- **修法**：桥直接去掉该键（`_on_session_list`），客户端传入的 `includeArchived` 不再透传（archived 会话概念疑似已从 0.17.0 移除，传 true 也按默认行为处理；注释写明取舍）。`workspace`/`limit` 透传与 result 映射（sessionId / workspace.workspacePath→cwd / title / updatedAt）不变。

### ② 模型身份改名 `builtin:zai-coding-plan/GLM-5.3` → `zai/glm-5.3`

- 0.17.0 create 快照 catalog（`settings.model.available`，30 项）条目形态：`{ref:{providerId, modelId}, label, providerLabel, contextWindow, maxOutputTokens, reasoning:{defaultLevel, levels}}`。zai 条目：`glm-5.3` / `glm-5.3-flash`（contextWindow 1,000,000，maxOutputTokens 128,000，defaultLevel 均 max）+ **`glm-5-turbo` 回归**（contextWindow 200,000，maxOutputTokens 64,000，defaultLevel enabled；该模型曾于 App 3.12.3 时代从服务端下架）。
- 新 ID **精确大小写敏感**：`glm-5.3` 可用，`GLM-5.3` 报「不存在 Model」。显式选模型**强制要求 `options.reasoningLevel`**（实测 setModel `{providerId:"zai", modelId:"glm-5.3", options:{reasoningLevel:"max"}}` → ok；不带 options → 报 `Reasoning level is required for zai/glm-5.3`）。
- **桥的行为**：session/new 字符串形态传新 ID `"glm-5.3"` **全通**（catalog 解析 + defaultLevel 补全设计天然适应改名）；传旧习惯 `"GLM-5.3"` 原本报「不在本会话的模型目录中」。**修法**：精确匹配失败后按 casefold 对 catalog 的 `ref.modelId` 做第二轮匹配，命中则用 **catalog 规范 ID** 构建 selection（绝不用调用方原始串）——旧习惯 "GLM-5.3" 在 0.17.0 自动解析到 "glm-5.3"，0.16 大写 catalog 精确先命中、折叠轮不触发，行为不变（真机 E2E 复核：0.17.0 上 `model: "GLM-5.3"` 创建会话成功）。
- **不做** providerId 前缀映射魔法（`builtin:zai-coding-plan`→`zai` 无法从 catalog 泛化推导）：对象形态传旧 providerId 继续由后端报错透传（文案清晰）。
- 两条命名空间并存：headless env 的 `ZCODE_MODEL` 仍用 config models key 原样（`GLM-5.3` 大写，0.17.0 实测可用）；app-server 的 catalog/setModel/generateText 用小写 `glm-5.3`。

### ③ `workspace/generateText` 参数键 `modelRef` → `selection`

- **改名早于 0.17.0**：实测 App 3.12.3 的 0.16.5 构建仍收 `modelRef`（本仓 2026-09-17 记录），而 `zcode-app-cli` 3.15.3-37 vendor 的 0.16.9 **已只收 selection**（`modelRef` → `-32602 Unrecognized key`）——改名发生在两个同号 0.16.9 构建之间的某次漂移。桥按「先发 selection、键级拒收特征时一次性回退 modelRef」覆盖两端。
- **maxOutputTokens 之谜解开**：省略 `maxOutputTokens` → `-32603 maxOutputTokens is outside the model option range`（0.16.9 selection 构建与 0.17.0 **同样如此**，非 0.17.0 新增——服务端不再提供可用默认值，不是客户端参数越界）。实测能通组合：**1024 ≤ maxOutputTokens ≤ catalog maxOutputTokens（glm-5.3 / glm-5.3-flash 均 128,000）+ `options.reasoningLevel` 必填**（取 catalog `reasoning.defaultLevel`）；显式 128,000 与 1024 均通过并返回真实模型输出；超上限（对 128,000 的模型发 131,072）同样报 range 错。
- **temperature 已删**：selection 构建发 `temperature` → `-32602 Unrecognized key: "temperature"`（0.16.9 selection 构建同样拒收）。桥照旧透传让错误指名道姓，不静默丢弃。
- **era 取舍**：不做双发（strict Zod 拒未知键，selection+modelRef 并列必 -32602）；也**不替调用方发明 maxOutputTokens/reasoningLevel 默认值**（桥在无会话上下文时拿不到 catalog，发明默认值会悄悄改变输出截断/推理语义）——省略时让后端错误透传并附加一句修法指引（错误文案可读）。回退判定只认 `-32602` 且错误文本提及 `modelRef`、或 `selection`+`unrecognized`（键级拒收形态）；selection 内容非法（"expected object" 型）不触发重试，避免把内容错误改写成第二次更离谱的 `Unrecognized key: "modelRef"`。
- result 键随构建不同：selection 构建返回 `{text, selection, finishReason, usage}`（老构建为 `{text, modelRef}`），桥原样透传。

### 适配后的测试清单（+15 用例，tests/test_app_server_methods.py）

- **L1–L4（session/list）**：空参数发 `{}` 无 includeArchived 键 + result 映射不变；cwd/limit 照常透传；客户端传 includeArchived 不透传；后端错误保持 `-32603 "zcode list failed"` 透传。
- **C4o–C4s（casefold 折叠）**：0.17 小写 catalog + 旧习惯 "GLM-5.3" 命中规范 ID 并补 defaultLevel；精确命中不触发折叠；折叠无命中报原错误（带原始串）；catalog 同存仅大小写不同的两个 ID 时精确优先不串 ID；0.16 大写 catalog 反向折叠可用。
- **M7a 改写 + M7f–M7j（generateText）**：首发 selection 键（值取自客户端 modelRef）；键级拒收 → modelRef 一次性重试（全量参数）且重试失败透传第二次错误；内容型 -32602 不重试；maxOutputTokens range 错透传 + 追加「必填」指引（后端原文不改写）；`Reasoning level is required` 透传 + 指引；temperature 拒收不重试、错误指名道姓。

## CLI 旗标面（0.17.0 vs 0.16.9）

同方法提取 `--help` 旗标逐项 diff（0.17.0 官方 vs 3.15.3-37 vendor 0.16.9）：

- **新增**：`--toolsearch-mode <auto|native|local|off>`（默认 off，仅本次启动生效）——唯一确证增量。
- **标签形态变化**：`--disallowed-tools` 的 help 标签现为 `--disallowed-tools, --disallowedTools <tools...>`（camelCase 别名上标签；别名此前已兼容可用）。
- **其余全部一致**：`-h/-v/-p/--prompt`、`--memory-bench`、`--browser-use`+`--browser-executable`、`--surface`、`--attach`、`--cwd`、`--force-mcs`、`--locale`、`--mode`、`--resume`、`--target`+`--target-replace`、`--workflow-mode`、`-c/--continue`、`--json`、`--no-browser`、`--no-color`、`--verbose`。`--mode` 帮助文案两代均列 `build, edit, plan, yolo, or guarded`（`guarded` 未实测，勿盲用；0.16.1 实测的 `auto` 保留值未实现结论继续有效）。`--allowed-tools`/`--max-turns`/`--permission-mode`/`--allow-main-worktree-yolo` 继续不存在。
- **与主代理按本仓 0.16.9（App 3.14.0）记录对比出的 6 项「新增」（--attach/--force-mcs/--surface/--toolsearch-mode/--verbose/--workflow-mode）说明**：后 5 项在 App 3.15.3 的 vendor 0.16.9 help 里已存在——同号构建漂移使「新增」的归因依赖基准构建，唯一两代构建都确认新增的只有 `--toolsearch-mode`。子命令 9 个不变（`login` 自 0.16.9 起带 `[zai|bigmodel]` 标签）；slash 命令 15 个不变（`/expert` 在 vendor 0.16.9 已在列，不能归因 0.17.0）。

## 最小 headless 环境的模型引导（0.17.0 复测，#57 配方）

**控制变量下复测**（`HOME=/tmp/…` 全新环境 + 官方 0.17.0 二进制，`--prompt "回复两个字:收到" --mode yolo --json`）：

- **配方仍可用，但机制注记修正**：0.17.0 runtime 把调用入口解析到**真实路径**（`/Applications/.../glm/zcode.cjs`），README 原记录的「按入口路径（不解析符号链接）探测」不再成立——符号链接入口 + 邻置 `provider/zcode-builtin.json` 的组合失效，报 `无法定位 CLI ZCode Built-in Provider Config：/Applications/ZCode.app/Contents/Resources/glm/provider/zcode-builtin.json, /config/provider/zcode-builtin.json`（候选路径暴露了真实路径解析）。实测可行的两条路径（均 exit 0、`response="收到"`、usage 新 keys 在列）：
  1. **真实文件入口**：把 `zcode.cjs` 复制（非符号链接）到部署目录，`provider/zcode-builtin.json` 邻置于其旁 + `~/.zcode/v2/provider_config.json`（#57 原配方，入口换成真实文件即可）；
  2. **显式 env 指路**（桌面 App 同款机制）：`ZCODE_BUILTIN_PROVIDER_CONFIG_FILE=<zcode-builtin.json 绝对路径>` + `ZCODE_PERSONAL_PROVIDER_CONFIG_FILE=<provider_config.json 绝对路径>`，入口可以是符号链接。
- **env-only 注入仍不被读取**：仅 `ZCODE_MODEL`/`ZCODE_BASE_URL`/`ANTHROPIC_API_KEY`（无 provider_config.json）在最小环境照旧失败（卡在 builtin provider config 定位，报错同上）——0.16.9 的结论在 0.17.0 不变。
- 复测方法备注：验证「最小环境」时必须用**真正干净的环境变量**（仅 HOME/PATH）。桌面 App 会话里跑会继承 `ZCODE_BUILTIN_PROVIDER_CONFIG_FILE` 等 App 注入变量，把失败 case 污染成假阳性通过（本轮实测踩过）。

## 端到端（桥 ↔ 官方 0.17.0）

`ZCODE_BIN` 指向官方构建后：initialize → session/new（`model: "GLM-5.3"` 旧习惯）→ **创建成功**（casefold→zai/glm-5.3，setModel 被接受）→ session/list **恢复可用**（50 条、新会话在列、cwd/title/updatedAt 映射正确）。session/prompt 的 turn 管线在原始协议层实测通畅（turn.started 等事件正常流动）；当晚实测中的 turn 失败系本机凭证状态所致（业主当晚把 App 切到已过期的 OpenCode Go plan，报错 `This API key has expired. Create a new API key in OpenCode Console`），主代理早间在同一台机器的 zai plan 上已验证完整 turn.completed 流式链路——非桥或 0.17.0 协议缺陷。

## 复测方法（可复现）

- **环境**：`export ZCODE_BIN=~/.local/bin/zcode-app`（官方 3.16.2 构建的符号链接），确认 `--version` = 0.17.0 后再探测（PATH 里的社区包装器 3.16.2-38 现已同为 0.17.0；如需 0.16.9 对照，`npm i --prefix <tmp> zcode-app-cli@3.15.3-37` 后直接 `node <tmp>/node_modules/zcode-app-cli/vendor/zcode.cjs`）。
- **活体探测**：spawn `<binary> app-server --stdio`，发无 `jsonrpc` 键的 NDJSON 帧；反向调用以现役三布尔应答体应答；方法存在性用空 params（-32602 存活 / -32601 已删），参数级兼容性必须按真实 payload 实测（generateText 的 selection/maxOutputTokens 语义即被空参探测掩盖）。
- **0.16.9 等价性**：同一 probe 脚本对 `{}` / `{includeArchived:false}` / `{includeArchived:true}` 建会话前后各比一轮（数量、result 形状）。
- **headless**：`<binary> --prompt "…" --mode yolo --json`；最小环境验证须用仅 HOME/PATH 的干净 env。
- **桥端到端**：`ZCODE_BIN` 指官方构建后按 README 的 ACP bridge 用法跑 initialize → session/new → session/prompt → session/list。
