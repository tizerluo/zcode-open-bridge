# zcode-open-bridge

> 非官方社区项目，将 [ZCode](https://zcode.z.ai)（智谱 Z.AI 的 Agentic Coding CLI）接入开放 Agent 生态（MCP / ACP）。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![CI](https://github.com/tizerluo/zcode-open-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/tizerluo/zcode-open-bridge/actions/workflows/ci.yml)

## 这是什么

ZCode 是智谱 Z.AI 出品的 AI 编程 Agent，由 GLM 系列模型驱动。它能力很强，但默认是个"闭源孤岛"——只能在自己的桌面 App 里用，无法被其他 Agent、编辑器、脚本标准化调用。

本项目通过三个轻量组件，把 ZCode 接入开放生态：

```
                    ┌─────────────────────────────────────────┐
                    │            ZCode (GLM-5.3)              │
                    │       智谱 Z.AI 的 Agentic CLI           │
                    └───────────────┬─────────────────────────┘
                                    │
        ┌───────────────────────────┼───────────────────────────┐
        │                           │                           │
        ▼                           ▼                           ▼
  ① 原生 CLI                ② MCP Server                  ③ ACP Bridge
  zcode --prompt            zcode-mcp-server              zcode-acp-bridge
  单次问答/脚本化           让 MCP client 调用            让 Zed/JetBrains 调用
```

## 三个组件

| 组件 | 作用 | 成熟度 | 文件 |
|------|------|:------:|------|
| **zcode-agent-help** | 能力发现说明书：一次调用了解 ZCode 全部能力 | ✅ stable | [packages/agent-help](packages/agent-help) |
| **zcode-mcp-server** | 把 ZCode 暴露为标准 MCP server，供 MCP client（Claude Code/Cursor/自身）调用 | ✅ stable | [packages/mcp-server](packages/mcp-server) |
| **zcode-acp-bridge** | 把 ZCode 桥接为 ACP Agent，供 Zed/JetBrains 等编辑器调用 | ⚠️ experimental | [packages/acp-bridge](packages/acp-bridge) |
| **zcode-review-gate** | PR 自动审查闸门：轮询 open PR，新 head 调 `zcode_pr_review` 审查并回贴 verdict 评论 | ⚠️ experimental | [packages/review-gate](packages/review-gate) |

## 快速开始

### 前置条件

- 已安装 [ZCode](https://zcode.z.ai)（需含 CLI，App 内自带）
- Node.js ≥ 18（`zcode.cjs` 的 shebang 是 `#!/usr/bin/env node`，CLI 由 node 执行）
- Python 3.8+（仅用标准库，零第三方依赖）
- 已通过 ZCode 登录（凭证存在 `~/.zcode/v2/config.json`）

### 让 `zcode` 命令在终端可用

ZCode 的 CLI 藏在 App 内部，默认不在 PATH：

```bash
# macOS
ln -s /Applications/ZCode.app/Contents/Resources/glm/zcode.cjs ~/.local/bin/zcode

# Linux（AppImage 解压安装到 /opt/ZCode 时）
ln -s /opt/ZCode/app/resources/glm/zcode.cjs ~/.local/bin/zcode
```

并在 shell 配置（`~/.zshrc`）里配置凭证（动态读取，不明文存 key）：

```bash
zcode() {
  local cfg="$HOME/.zcode/v2/config.json"
  # 从配置文件动态读取凭证注入环境变量
  # 值一律经 shlex.quote 转义后再交给 eval，防 config 值含 shell 元字符时命令注入
  eval "$(python3 -c "
import json, shlex
c=json.load(open('$cfg'))
for k,v in c['provider'].items():
    if v.get('enabled'):
        o=v['options']
        print('export ZCODE_MODEL=' + shlex.quote(next(iter(v.get('models',{}))) or 'GLM-5.3'))
        print('export ZCODE_BASE_URL=' + shlex.quote(o.get('baseURL','')))
        print('export ANTHROPIC_API_KEY=' + shlex.quote(o.get('apiKey','')))
        break
")"
  command zcode "$@"
}
```

> ⚠️ **安全说明**（整体 review 安全新发现 1）：`eval` + 未转义插值是危险模板——若 `config.json` 的值含 `"`、`` ` ``、`$` 等元字符会被 `eval` 执行，所以上面示例对每个值都做了 `shlex.quote`。更稳妥的做法是**不复用这段 shell 函数**，直接用本项目的 `shared/credentials.py`（三个组件已内置，纯 `json.load` 读 config，不走 eval/shell），或参考 `--print-injected-env` 诊断输出手动 export。

### 使用三个组件

```bash
# ① 能力发现
./packages/agent-help/zcode-agent-help --pretty

# ② MCP server (注册到 ~/.zcode/cli/config.json 的 mcp.servers)
#    或直接作为 stdio 进程运行
#    {"mcp": {"servers": {"<名字>": {"command": ..., "args": [...]}}}}（0.16.5 实测；顶层 mcpServers 键不被读取）
./packages/mcp-server/zcode-mcp-server

# ③ ACP bridge (配置进 Zed/JetBrains 的 Agent 设置)
./packages/acp-bridge/zcode-acp-bridge
```

## 能力详情

### 原生 CLI（`zcode --prompt`）

```bash
zcode --prompt "审查这段代码" --attach app.py --mode plan --json
zcode --prompt "继续" --resume sess_xxxx
```

- 单次问答：`--prompt "<text>"`
- 长程任务（设定 session 目标）：`--target "<objective>"`，或等价地在 prompt 开头用 `/goal`：`--prompt "/goal <objective>"`（两者互斥）
- 权限：`--mode plan`（只读）/ `build` / `edit` / `yolo`（全自动）
- 输出：纯文本 或 `--json`（含 sessionId/response/usage）

### MCP server（`zcode-mcp-server`）

暴露四个 MCP tool：

| Tool | 作用 |
|------|------|
| `get_zcode_capabilities` | 返回 ZCode 能力清单（调 agent-help） |
| `zcode_review` | 调 ZCode 审查代码（yolo + 写/执行工具物理禁用，全程免授权但改不了文件，安全） |
| `zcode_security_review` | 安全专项审查：mimosa 确定性规则引擎预扫 → ZCode 拿 findings 逐条核实（确认/误报/存疑 + 攻击路径 + 修复建议）。`depth=normal` 秒级快扫（默认），`depth=deep` 含业务逻辑投研（异步任务管线） |
| `zcode_pr_review` | PR 审查模式：自动算 `git diff base...HEAD`（merge-base 语义，base 可自动探测）→ mimosa 全仓扫描且业务逻辑复核聚焦改动文件（focus_files）→ ZCode 出 PR 复核报告（P0/P1/P2 分级 + findings 核实 + 能否合并结论）。默认 `depth=deep`；diff 超 `ZCODE_BRIDGE_PR_DIFF_MAX`（默认 500KB）截断保清单 |

> **只读原理（2026-08-08 重构，告别 `--mode plan`）**：review 体系不再用 plan 模式——plan 只禁「改文件」，读探索/子代理照样放行（限流超时主因），且 plan→build 的规划惯性容易让 review 变成「边审边修」。新方案用 `--mode yolo`（全程免授权）+ `--disallowed-tools` 把 `Write/Edit/MultiEdit/ApplyPatch/Bash` 连同 Node REPL 一族（`js` / `mcp__node_repl__js*`）一起禁掉：`--disallowed-tools` 是工具集级物理移除、先于权限层，yolo 也绕不过；Node REPL 一族必须同禁，否则可被 `execSync` 打穿 Bash 黑名单（0.16.1 实测复现）。读工具（Read/Grep/Glob）全开，不影响审查能力。prompt 层另有「只审不修」职责约束（不修改文件、不提议帮忙修复）作双保险。

> **client 可信假设**：`zcode_review` 等 tool 的 `path`/`cwd`/`files` 参数**不做沙箱限制**（可让 ZCode 扫描本机任意目录）。本 server 是本地 stdio 桥，**假设 MCP client 可信**；若要用于远程/多租户部署，需自行在调用侧加路径白名单（整体 review P2 文档项）。

### ACP bridge（`zcode-acp-bridge`）

实现 ACP 协议的子集，把 ZCode 的私有协议翻译成标准 ACP：

| ACP 能力 | 状态 |
|----------|:----:|
| initialize / session/new / session/prompt / session/cancel | ✅ |
| session/list / session/resume | ✅ |
| tool_call / tool_call_update（工具调用展示）| ✅ 实时 |
| usage_update（token 用量）| ✅ |
| agent_message_chunk（文本输出）| ✅ **真流式**（0.14.8+）|
| agent_thought_chunk（思考过程）| ✅ 流式（GLM-5-Turbo）|

> `session/prompt` 的 `prompt` 参数除标准 ACP ContentBlock[] 外，bridge 还兼容纯字符串与 `{"content": "..."}` 键别名（内部统一归一，zcode review P3-5）。
| plan（任务清单）| ⚠️ 代码就位，数据驱动 |
| diff（文件变更）| ⚠️ 仅文件名，无 diff 内容 |

> **0.16 协议变更（bridge 内部适配，ACP 面不变）**：0.16 的真正断点是——新增 server→client 反向调用 `session/requestRuntimePreferences` 必须应答、事件模型调整、`steer`/`rewind*`/`prompt/enhance*` 移除。信封不再接受 `jsonrpc` 字段、核心方法更名 `session/create`（参数从 `cwd` 改为 `workspace`）/`session/send`/`session/stop`、`subscribe` 必传 `deliveryKind` 同为 0.16 协议事实（新接入者必读），但桥对内本就用这套调用面（无 `jsonrpc` 信封 + `create`/`send`/`stop` + `deliveryKind`），并非全断原因（准确史实见[规格书勘误](docs/upgrade-0.16.1-spec.md)）。上表是编辑器侧看到的标准 ACP 方法名，**不变**；rename 由 bridge 内部翻译。0.16.5 复测兼容（新增反向调用/通知/字段均为增量，桥无需改动），详见 [docs/recheck-0.16.5.md](docs/recheck-0.16.5.md)。

#### 双模式（真流式 / 轮询降级）

ACP bridge 支持**事件驱动**（真流式）和**轮询**（伪流式）两种模式：

- **事件驱动模式**（ZCode CLI ≥ 0.14.8）：通过 `session/subscribe` 订阅事件流，`model.streaming` 事件携带 `text_delta` 逐段推送，实现**真正的流式文本输出**。工具调用状态也实时推送（scheduled → started → progress → result）。
- **轮询模式（仅限 legacy < 0.16）**：旧协议模式下 `session/subscribe` 不可用时，降级为轮询 `session/read`，turn 完成后整段发文本（伪流式）。**0.16+ 不再自动降级**——新协议模式下 subscribe 失败直接报错 `-32603`（"0.16+ 必须走事件订阅；轮询降级仅限旧协议模式"）。

#### 扩展方法（非标准 ACP）

ACP bridge 额外暴露了 ZCode 新版协议方法，供编辑器/脚本调用。按 ZCode 引入版本分组：

**session 级**（`{sessionId}` 定位会话）：

| 扩展方法 | 作用 | 引入版本 | params |
|----------|------|:--------:|--------|
| `session/fork` | 从 checkpoint 分叉新会话 | 0.14.8 | `{sessionId, target?}` |
| `session/rewind` ❌ | 回退工作区文件到 checkpoint（**0.16 已移除**） | 0.14.8 | `{sessionId, target?, expectedRevision?}` |
| `session/goal` | 读取/设置 session 目标 | 0.14.8 | `{sessionId, action: show\|set\|replace\|clear, objective?}` |
| `session/compact` | 压缩对话上下文 | 0.14.8 | `{sessionId}` |
| `session/steer` ❌ | turn 进行中追加指令（**0.16 已移除**） | 0.14.8 | `{sessionId, content}` |
| `session/setThoughtLevel` | ⭐ 设置思考强度（实测 GLM-5.2: max/high/nothink，按模型不同） | 0.15.0 | `{sessionId, thoughtLevel}` |
| `session/updateRuntimeModelConfig` | 运行时覆盖会话模型配置 | 0.15.0 | `{sessionId, runtimeModel, applyModelSelection?}`（0.16 起 `runtimeModel.revision` 必填） |
| `session/cancelBackgroundTask` | 取消后台 Bash 任务 | 0.14.8 | `{sessionId, taskId}` |
| `session/rewindCascade` ❌ | 级联回退（与 rewind 同 schema，**0.16 已移除**） | 0.15.0 | `{sessionId, target?, scope?, expectedRevision?}` |
| `session/setModel` | 切换会话模型 | 0.14.8 | `{sessionId, modelId}` |
| `session/setMode` | 切换会话权限模式 | 0.14.8 | `{sessionId, mode}` |

> ❌ **0.16 已移除**：`session/steer`、`session/rewind`、`session/rewindCascade` 已从 app-server 删除。steer 语义并入 `session/send`（turn 进行中发送即 steer）；rewind 无协议替代，仅剩 slash 命令 `/rewind` 与 `rewind.triggered` 事件。0.16.1 上调用这些方法会收到 `-32601`。
>
> ℹ️ **0.16 schema 变更**：`session/updateRuntimeModelConfig` 在 0.16.1 仍存活（实测），但 schema 新要求 `runtimeModel.revision`（string）必填。

**workspace 级**（按工作区 `{workspacePath, workspaceKey}` 定位，不依赖 sessionId）：

| 扩展方法 | 作用 | params |
|----------|------|--------|
| `workspace/readState` | 读工作区状态（settings/modelCatalog/slashCommands） | `{workspace, runtimeModel?}` |
| `workspace/generateText` | 一次性文本生成（不建会话） | `{workspace, modelRef, prompt, querySource, maxOutputTokens?, temperature?}` |
| `workspace/setDefaultModel` | 设工作区默认模型（持久化） | `{workspace, model, runtimeModel?, expectedWorkspaceRevision?}` |
| `workspace/setDefaultMode` | 设工作区默认权限模式（持久化） | `{workspace, mode, expectedWorkspaceRevision?}` |
| `workspace/setDefaultThoughtLevel` | 设工作区默认思考强度（持久化） | `{workspace, thoughtLevel, expectedWorkspaceRevision?}` |
| `workspace/upsertModelProvider` | 新增/更新模型供应商 | `{workspace, provider, expectedWorkspaceRevision?}` |
| `workspace/removeModelProvider` | 移除模型供应商 | `{workspace, providerId, expectedWorkspaceRevision?}` |
| `workspace/updateProviderRegistry` | 批量更新供应商注册表 | `{workspace, registry, includeWorkspaceState?}` |

**prompt 级**（提示词增强，App 3.3.0 引入；❌ **0.16 已全部移除**，无替代）：

| 扩展方法 | 作用 | params |
|----------|------|--------|
| `prompt/enhance` ❌ | 同步增强提示词（阻塞返回增强后文本） | `{workspace, prompt, sessionId?, context?}` |
| `prompt/enhance/start` ❌ | 异步启动增强 job（阻塞等待结果） | `{workspace, prompt, requestId, sessionId?, context?}` |
| `prompt/enhance/cancel` ❌ | 取消进行中的增强 job | `{requestId}` |

> ⚠️ **0.16 已移除**：整个 `prompt/enhance*` 方法族已从 app-server 删除且无替代，以下语义说明仅适用于 0.15.0 + App ≥ 3.3.0；0.16.1 上调用会收到 `-32601`，bridge 映射为明确错误文案。
>
> `prompt/enhance/start` 是异步 job 模式：start 立即返回 `{requestId, accepted}`，结果由 ZCode 推送 `prompt/enhance/result` 通知。bridge 把它转成阻塞语义——start 后内部等待结果通知（总超时 120s），收到后一次性返回 `{enhanced}`（completed）/ `{status:"cancelled"}`（cancelled）或 `-32603`（failed/超时）。`prompt/enhance/result` 本身是 server 推送通知，不是 client 可调方法。

> workspace 参数可三种方式传入：`workspace`（完整 dict）、`workspacePath`/`cwd`（路径字符串），或缺省时用 bridge 进程的 `cwd`。
> Provider 管理类方法（upsert/remove/updateRegistry）的 `provider`/`registry` 可能含 `apiKey`，bridge 仅透传、不读取/打印其明文。

### Review gate（`zcode-review-gate`）

PR 自动审查闸门守护进程（第 4 组件，experimental）：常驻轮询配置仓库的 open PR，对每个新 head sha 经 `zcode-mcp-server --call zcode_pr_review` 完成审查（锁/重试/只读护栏全部复用 bridge 同源路径），把带 verdict（✅ pass / ⚠️ concerns）的结果回贴为 PR 评论。同一 head sha 不重复审（state 文件去重），失败按指数退避重试，head 更新自动复活重审。token 不落盘（经 git≥2.31 的 `GIT_CONFIG_*` 环境变量进程内注入，不进 argv）。公开、通用，任何 GitHub 仓库可用。

安装、配置参考、systemd 部署与运维详见 [packages/review-gate/README.md](packages/review-gate/README.md)。

> 配套能力：mcp-server 新增 `--call TOOL '<json>'` 一次性调用模式（脚本化入口，exit 0/1/2 分别对应 成功 / 用法错误或 handler 异常 / tool 执行失败），stdio 模式行为不变。

## 会话存储

`--prompt` 和 ACP bridge **共享同一套会话存储**（`~/.zcode/cli/db/db.sqlite`），互通互恢复：
- `--prompt` 创建的会话可被 `session/list` 看到、被 `--resume` 或 ACP resume 恢复
- 反之亦然

## 凭证与限流（自动化集成必读）

### Model ID 格式（canonical）

**canonical model id = `~/.zcode/v2/config.json` 里 `models` 的 key 原样**（如 `GLM-5.3`），**不加 provider 前缀**。`shared/credentials.py`、MCP server、ACP bridge、agent-help 四处统一用原始 id。实测（0.16.1 时代）`zai/GLM-5.2` 前缀形式也兼容，但非 canonical，本项目不使用。

模型面现状（0.16.5 实测）：当前 enabled provider（`builtin:zai-coding-plan`）的 models 为 `GLM-5.3` / `GLM-5.3-Flash` / `GLM-5-Turbo`。

### 凭证注入：显式环境变量优先

三个组件注入凭证的合并顺序为 `{**config_creds, **os.environ}`——**已显式设置的环境变量覆盖 config 读出的值**。便于不改 config 临时调试/覆盖：

```bash
# 临时用另一个模型跑 ACP bridge（覆盖 config 的 GLM-5.3）
ZCODE_MODEL=GLM-5-Turbo ./packages/acp-bridge/zcode-acp-bridge

# 临时覆盖 MCP server 的 baseURL
ZCODE_BASE_URL=https://api.z.ai/api/anthropic ./packages/mcp-server/zcode-mcp-server
```

诊断"实际会注入哪些凭证"（不改 config、apiKey 脱敏）：
```bash
./packages/agent-help/zcode-agent-help --print-injected-env
# 输出每个 key 的来源（config vs env）+ 最终值（apiKey 脱敏）
```

### ZCODE_BASE_URL 残留自动检测（切换过 plan 的用户）

**背景**：ZCode App 切换过订阅 plan 的用户，旧 plan 的 baseURL 会残留在子进程环境（即使该 plan 已失效）。例如 `ZCODE_BASE_URL=https://zcode.z.ai`（start-plan 残留）与 config 当前 enabled 的 `zai-coding-plan`（`api.z.ai`）不一致，会导致 headless 调用打到错误端点（404）。

**自动自愈**：bridge 启动时检测——若 env 的 `ZCODE_BASE_URL` 指向 config 里**另一个 provider** 的官方 endpoint（host 匹配），判定为 App 注入的残留 → **自动用 config enabled provider 的值** + stderr 告警。用户自建/代理 endpoint（不在 config 任何 provider 里）则正常尊重 env（issue #3 调试场景不受影响）。

```bash
# 用 --print-injected-env 看是否检测到残留 (标 🚫)
./packages/agent-help/zcode-agent-help --print-injected-env
```

### 限流与重试（MCP server）

`zcode_review` / `zcode_security_review` 调用 headless zcode 做审查时，多个 gate 并发可能触发 provider 限流。MCP server 做了三层防护（issue #3）：

| 机制 | 行为 | 配置 |
|------|------|------|
| **进程级文件锁** | 多个 MCP client 并发调用时，串行化 headless review，防并发触发限流 | `ZCODE_BRIDGE_REVIEW_LOCK=0` 关闭 |
| **provider 错误解析** | 识别 429 / 1302 / `Too Many Requests` / `请求过于频繁` / `retry-after`，区分限流/配额/其他 | — |
| **有限重试 + 退避** | 仅对**限流**错误重试（配额/Unauthorized 不重试），退避用 retry-after 或指数退避（`2^n+1`） | `ZCODE_BRIDGE_MAX_RETRIES`（默认 3） |
| **单次调用超时** | review 单次 zcode 调用超时 | `ZCODE_BRIDGE_REVIEW_TIMEOUT`（默认 300s，下限 30s） |
| **code 参数体积上限** | `zcode_review` 的 `code` 参数超过上限即截断，防超大内联代码撑爆调用 | `ZCODE_BRIDGE_CODE_MAX`（默认 500KB） |
| **zcode 输出体积上限** | zcode stdout 输出超过上限即截断 | `ZCODE_BRIDGE_MAX_OUTPUT`（默认 10MB） |

> **内存峰值取舍**（整体 review P2-1）：zcode 子进程用 `capture_output` 全量缓冲输出，`ZCODE_BRIDGE_MAX_OUTPUT` 是**事后截断**——内存峰值仍约为完整输出的一倍（`text=True` 解码再翻一倍），上限（100MB）只是给失控场景兜底，不是流式背压。审查超大项目时建议拆分文件/目录分批调用，而不是把该值调大硬扛。

注意：zcode 内部已有自己的指数退避重试（`_retryWithExponentialBackoff`），MCP 层的重试是补充，默认保守（max 3）。

`zcode_security_review` 额外有 mimosa 相关的 env：

| 配置 | 作用 |
|------|------|
| `ZCODE_BRIDGE_MIMOSA_ROOT` | 指向 mimosa 插件根目录（含 `payload/dist/mcp/server.js` 的那层）；不设则自动探测 `~/.local/share/mimosa/*` 与 `~/.zcode/cli/plugins/cache/*/mimosa/*`，找不到会明确报错并建议改用 `zcode_review` |
| `ZCODE_BRIDGE_MIMOSA_TIMEOUT` | mimosa `security_scan` 快扫（depth=normal）超时（默认 180s） |
| `ZCODE_BRIDGE_MIMOSA_SCAN_ROOT` | findings 回读的信任根（默认 `~/.mimosa/security-scans`）：从 mimosa 摘要解析出的 scanDir 必须落在其下才回读 `findings.json`，越界降级为仅用摘要（防路径注入导致任意文件回读） |
| `ZCODE_BRIDGE_MIMOSA_DEEP_TIMEOUT` | depth=deep 异步扫描的总预算（默认 900s），超时会 best-effort cancel 后台 job |
| `ZCODE_BRIDGE_MIMOSA_POLL_INTERVAL` | depth=deep 的 status 轮询间隔（默认 2s） |
| `ZCODE_BRIDGE_MIMOSA_RECV_TIMEOUT` | mimosa stdio 单次响应（一问一答）超时（默认 120s） |

ACP bridge 侧另有一个 env（不在上两表，仅 ACP 用）：`ZCODE_ACP_DEFAULT_MODE` —— `session/new` 的默认权限模式，默认 `yolo`，可设 `build` 收紧（详见下方「重要限制」#7）。

**depth 两档**（2026-08-08 接入，mimosa 1.0.3 实测）：

- `normal`（默认）：同步 `security_scan`，秒级（400 文件项目 ~2s），纯规则匹配
- `deep`：异步 `security_scan_start` → `security_scan_status` 轮询 → 完成后回读 findings，含业务逻辑投研（threatModel/validation/pathAnalysis 等阶段），400 文件项目 ~13s。与 normal 共用同一条 findings 回读管线。纯 native 引擎、零 LLM、零网络（`evidenceBoundary: static_only_no_runtime_execution`）
- `focus_files` 参数（仅 deep）：业务逻辑复核的**优先级提示**（典型用法：调用方自己算出本次改动的文件清单传入——bridge 不做 git diff 集成），不是过滤器，静态引擎永远全量扫（实测）

异步响应解析的两个坑（已在代码里处理）：start/status/cancel/resume 的 `content[0].text` 是**嵌套 JSON 字符串**（`mimosa-mcp-security-scan-job/v1`）而非 Markdown 摘要；完成判定必须 parse JSON 看 `job.status`——running 态也含 `"completedAt":null`，字符串匹配 `completed` 会误判。

> mimosa 的调用不依赖 zcode 插件体系：bridge 用自带极简 stdio MCP client 直接 spawn mimosa 的 `server.js`（env `ZCODE_PLUGIN_ROOT=<root>`、`MIMOSA_ENGINE=native`，cwd=被扫项目）。mimosa 快扫是确定性规则引擎、零 LLM 流量，故不走 review 文件锁。
>
> 实测备注（2026-08-08，GC-8G）：① 独立调用时 mimosa 也会在被扫项目写一个小会话状态文件（`.mimosa/hook-state/sess_*.continue.json`，约 200 字节，无害）——即 bridge 自身的代码路径对被扫目录只读，但 mimosa 引擎会落这个状态文件，说"完全只读"不准确；② 从非登录 shell（systemd unit、cron、`sudo -u` 直调）启动时 PATH 可能不含 `~/.local/bin`，需显式 `export PATH="$HOME/.local/bin:$PATH"` 否则找不到 `zcode`。
>
> 并发与阻塞边界（狗食 review P2-4/P2-5）：mimosa 预扫**不在** review 文件锁内（确定性引擎无 LLM 限流问题），只有 zcode 复核阶段持锁——并发扫同一项目时 mimosa 的 hook-state 文件各写各的会话，无冲突。最坏阻塞时长估算：锁等待 300s + 单次调用 `ZCODE_BRIDGE_REVIEW_TIMEOUT`（默认 300s）×（1 + `ZCODE_BRIDGE_MAX_RETRIES` 默认 3）+ 限流退避，极端情况单次 tool 调用可阻塞约 20 分钟；depth=deep 时前面还要再加 mimosa 异步扫描预算（`ZCODE_BRIDGE_MIMOSA_DEEP_TIMEOUT` 默认 900s）。调用方应把 MCP 超时设到相应量级。

### 聚焦审查 prompt 建议

`zcode_review` / `zcode_security_review` 两个 MCP tool 已把这条纪律内化：prompt 内置「只审不修」职责约束（不修改文件、不提议帮忙修复、只用只读工具读上下文），写/执行工具在工具集层物理禁用，且证据统一走 `--attach` 附件。若绕过 tool 直接手写 CLI 做自动化审查，仍建议自己加上同款约束：把 diff/证据作为 `--attach` 附件，prompt 写明"不要 spawn 子代理、不要探索文件系统、只基于附件推理"，以降低限流风险。

## 版本兼容性

本项目兼容以下 ZCode 版本，**对旧版完全向后兼容**：

| ZCode CLI 版本 | 支持情况 | ACP bridge 流式 | 扩展方法 |
|:--------------:|:--------:|:---------------:|:--------:|
| **0.16.5**（App 3.10.2） | ✅ 完整 | **真流式**（事件驱动） | ✅ session/* + workspace/*（与 0.16.1 同面；`automation/*` 未实现不受其删除影响） |
| **0.16.1**（App 3.6.5） | ✅ 完整 | **真流式**（事件驱动） | ✅ session/* + workspace/*（`steer`/`rewind*`/`prompt/enhance*` 已于 0.16 移除；`updateRuntimeModelConfig` 存活但 `runtimeModel.revision` 必填） |
| **0.15.x**（App 3.5.x） | ✅ 完整 | **真流式**（事件驱动） | ✅ 全部（协议面同 0.15.0 行；App 功能面：3.5.2 内置网页应用、PDF 预览，见规格书 changelog） |
| **0.15.x**（App 3.4.x） | ✅ 完整 | **真流式**（事件驱动） | ✅ 全部（协议面同 0.15.0 行；App 功能面：3.4.2 定时任务 cron、Kimi K3，见规格书 changelog） |
| **0.15.0**（App 3.3.x） | ✅ 完整 | **真流式**（事件驱动） | ✅ 全部（含 workspace/*、setThoughtLevel、**prompt/enhance** 等） |
| **0.15.0**（App 3.2.0 ~ 3.2.5） | ✅ 完整 | **真流式**（事件驱动） | ✅ session/* + workspace/*（无 prompt/enhance） |
| **0.14.8**（App 3.1.4） | ✅ 完整 | **真流式**（事件驱动） | ✅ fork/rewind/goal/compact/steer |
| **0.14.5 ~ 0.14.7** | ✅ 兼容 | 伪流式（自动降级轮询） | ❌（旧版协议未实现） |
| **< 0.14.5** | ⚠️ 未测 | — | — |

> 注：CLI 版本号相同不代表协议面相同——`prompt/enhance` 是 App 3.3.0 引入的协议方法（CLI 同为 0.15.0，仅 App 3.3.0+ 的 app-server 支持），又于 0.16 整体移除，仅 0.15.0 + App ≥ 3.3.0 的组合可用。0.16.1（App 3.6.5）协议面大改——真正断点是反向调用必须应答、事件模型调整、删除 steer/rewind/enhance（信封去 `jsonrpc`/方法 rename/`deliveryKind` 必填同为协议事实，但桥对内本就用这套调用面），详见 [docs/upgrade-0.16.1-spec.md](docs/upgrade-0.16.1-spec.md)（含勘误）。0.16.5 已于 2026-09-01 全链路复测（协议面兼容、桥无需代码改动），详见 [docs/recheck-0.16.5.md](docs/recheck-0.16.5.md)。

**降级行为**：
- 轮询降级**仅限 legacy（< 0.16）协议模式**：旧版下 `session/subscribe` 不可用时自动切换到轮询 `session/read`（伪流式）。**0.16+ 不再自动降级**——新协议模式下 subscribe 失败直接报错 `-32603`（"0.16+ 必须走事件订阅；轮询降级仅限旧协议模式"）。
- 轮询（legacy）路径的失败检测有固有局限：该路径收不到 `turn.failed` 事件（projection/messages 无失败标志），turn 失败只能靠「status=idle 但本轮无任何实质输出（text/tool/patch）」的启发式检测，可能误报（成功但无实质输出的 turn 被判失败）或漏报（失败前已吐出部分内容的 turn 被当成功）；0.16+ 事件路径无此局限（`turn.failed` 终止帧已能正确判失败）。
- 扩展方法在旧版 ZCode 上会透传后端错误（`-32603 zcode <method> failed: ...`），不影响标准 ACP 方法（new/prompt/cancel/list/resume）。例如在 App 3.2.x 上调用 `prompt/enhance`（3.3.0 新增）会得到 `-32603`，调用方应据此做版本判断。
- 调用 0.16 已删除的方法（`session/steer`、`session/rewind*`、`prompt/enhance*` 等）时，后端返回 `-32601 Method not found`，bridge 会映射为明确错误文案（"当前 ZCode 版本已移除该能力 (<方法名>); 该 ZCode 版本不支持此能力"），而非原始透传，调用方可据此做版本判断。

### MCP 规范兼容性说明

本项目的 `zcode-mcp-server` 基于 **stdio 传输**，纯 Python 标准库手写，零第三方依赖。协议版本走**逐请求协商**（2026-08-08 起）：支持 `2024-11-05` / `2025-03-26` / `2025-06-18` / `2025-11-25` 全段，client 报什么版本我们认什么（在列表内回显，列表外回我们最高的 `2025-11-25`）。

**这是有意识的路线选择**：
- **stdio 是当前标准传输**。MCP 规范演进（`2025-03-26` → `2025-11-25` → `2026-07-28` 无状态改版）的核心红利——协议层无状态化、授权加固、Tasks、Elicitation——全部面向 **HTTP 远程 server / 多租户企业场景**。我们是 **stdio 本地桥**，单连接、生命周期 = client 进程，这些特性的痛点一个都不存在。
- **生态兼容性已实测**（2026-08-08，本机四 client 二进制验证）：Claude Code / Kimi Code / Cursor 目前都是 legacy-only（最高认 `2025-11-25`），zcode 0.16.1 是双纪元（auto 探测 `server/discover` 失败会回落 legacy——我们对未知方法回 `-32601`，正好触发规范预期的回落路径）。
- **零依赖 = 免疫 SDK breaking changes**。官方 Python SDK v2.0.0（2026-07-28 发布）虽自带双纪元，但 13 个直接依赖（含 HTTP 全家桶）对纯 stdio 单文件 server 得不偿失，故继续手搓。

**已对齐 2025-11-25 的义务**：拒收 JSON-RPC batch（2025-06-18 起规范移除，回 `-32600`）、tools/list 确定性顺序、tool `title` + `annotations`（`readOnlyHint` 等）元数据、输入校验错误走 `isError: true` 而非协议错误。

**路线图**：~~2026-07-28 新纪元~~ **已完成（2026-08-08，dual-era 上线）**：server 同时服务两个纪元——`initialize` 开场走 legacy（2024-11-05~2025-11-25 协商），带 modern `_meta` 信封的请求走 2026-07-28 无状态新协议（`server/discover` 探针、信封缺失 `-32602`、版本不符 `-32022` 带 supported 列表、result 盖 `resultType`/`serverInfo` 戳、list 结果带 `ttlMs`/`cacheScope`）。已通过官方 Python SDK v2.0.0 client 互操作实测（`session.discover()` 协商出 2026-07-28，tools/list、tools/call 全程新协议）；zcode 0.16.1 的 auto 探测也会自动走新协议。MRTR/elicitation/tasks/subscriptions 按调研结论不实现（废弃或用不上）。

## Skill（驱动说明书）

项目附带一个通用 skill [zcode-bridge-guide](skills/zcode-bridge-guide/SKILL.md)，覆盖：
- 三种接入模式（CLI / ACP / MCP）的完整使用方法
- 凭证配置、非交互 shell 坑
- 真流式 vs 伪流式双模式说明
- 扩展协议方法（session 级 + workspace 级，含 setThoughtLevel 思考强度控制）
- 任务书模板、独立复核、版本兼容性

复制到 ZCode 的 skill 目录即可使用：
```bash
cp -r skills/zcode-bridge-guide ~/.zcode/skills/
```

## ⚠️ 重要限制（请务必阅读）

本项目是建立在**闭源 ZCode** 之上的非官方桥接器，存在以下固有限制：

1. **依赖闭源软件**：必须先安装 ZCode。ZCode 升级可能随时破坏本项目（协议字段靠逆向确认，无官方保证）。
2. **工具调用 turn 不稳定**：ZCode app-server 的工具调用 turn 时长在 38s～100s+ 波动，有时不完成。
3. **流式输出**：ZCode CLI ≥ 0.14.8 支持事件推送（`session/subscribe`），ACP bridge 在此版本下实现**真流式**（逐段推送）；旧版自动降级为伪流式（turn 完成后整段发）。
4. **diff 无内容**：ZCode 协议层不暴露 oldText/newText，只能列文件名。
5. **GLM-5.2 无推理输出**：思考过程（agent_thought_chunk）在 GLM-5.2 下不触发，需 GLM-5-Turbo（GLM-5.2 为旧默认模型；GLM-5.3 行为未复测）。
6. **TUI 不可用**：0.16.1 起 CLI 帮助虽列出 `tui` 命令（无参数即进入 TUI），但独立终端实测仍报错（`Cannot find package '@zcode/tui'`），仅 headless 模式可用。
7. **⚠️ ACP bridge 默认 `mode=yolo`（权限风险）**：为避免工具调用 turn 卡在权限确认，ACP bridge 的 `session/new` 强制以 `mode=yolo` 创建会话（见 `zcode-acp-bridge` 的 `_on_session_new`）。这意味着任意 prompt 都可能触发**无确认的文件修改和命令执行**。作为编辑器集成时请知悉此风险；现可用 `ZCODE_ACP_DEFAULT_MODE=build` 收紧默认值，且 bridge 启动日志（stderr）会对当前默认 mode 打显眼告警。更完整的方案是实现 ACP↔ZCode 的 permission 转发（本项目 P4b 未实现）。
8. **⚠️ Provider 管理方法涉及 apiKey**：`workspace/upsertModelProvider`、`workspace/updateProviderRegistry` 的 `provider`/`registry` 参数会携带 `apiKey`（可能为 `{source:"inline", value:"sk-..."}` 明文）。ACP bridge 仅整体透传给 ZCode 后端、不读取也不在日志打印其明文；但调用方应自行确保传输通道（stdio）可信，并避免在日志中回显原始参数。
9. **⚠️ 事件模式 turn 超时契约（2026-08-08 起）**：`session/prompt` 在事件模式下若 turn 已启动但 120s 未收到完成信号，返回 **JSON-RPC 错误 `-32603`（"事件流超时"）**，而**不是**正常 `stopReason=max_turn_requests`——后者只保留给"turn 从未启动"的场景。ACP client 侧应按此区分「卡死」与「真的太长」（整体 review P1 + 复审 P1-B 的契约变更）。

## 项目结构

```
zcode-open-bridge/
├── packages/
│   ├── agent-help/      # 能力发现说明书 (stable)
│   ├── mcp-server/      # MCP 桥接 (stable)
│   ├── acp-bridge/      # ACP 桥接 (experimental)
│   └── review-gate/     # PR 自动审查闸门 (experimental)
├── shared/
│   └── credentials.py   # 凭证读取 (单一真相源)
├── skills/
│   └── zcode-bridge-guide/  # 驱动 ZCode 的通用 skill (说明书)
├── tests/
│   ├── test_app_server_methods.py
│   ├── test_credentials.py
│   ├── test_event_translator.py
│   ├── test_mcp_retry_lock.py
│   ├── test_polling_failure.py
│   ├── test_projection_differ.py
│   ├── test_prompt_enhance.py
│   └── test_provider_error.py
├── LICENSE              # MIT
└── README.md
```

每个组件都是**单文件、零依赖**，复制一个文件即可独立运行（`shared/credentials.py` 的逻辑已内嵌到 mcp-server/acp-bridge/agent-help）。

## 开发

提交前建议在本地跑一遍与 CI 等价的检查（lint + 测试 + 可执行位校验）：

```bash
# 1. lint (需安装: pip install ruff==0.15.17)
ruff check \
  packages/acp-bridge/zcode-acp-bridge \
  packages/agent-help/zcode-agent-help \
  packages/mcp-server/zcode-mcp-server \
  shared/ \
  tests/

# 2. 测试 (纯标准库 unittest, 无需安装依赖; 跑全部 8 个测试文件)
python3 -m unittest discover -s tests -p 'test_*.py'

# 3. 确认三个组件保持可执行位 (100755)
git ls-files --stage packages/*/zcode-*
```

> ⚠️ 一些编辑器/Edit 类工具会把组件的 `100755` 改回 `100644`。若发现权限丢失，用
> `git update-index --chmod=+x -- <文件>` 修复——CI 的 `Executable bit check` job 也会拦截这个问题。

日常开发自测：

```bash
# 自测凭证读取
python3 shared/credentials.py
```

## 许可证

[MIT](LICENSE)

## 致谢

- [Agent Client Protocol](https://agentclientprotocol.com/)（Apache 2.0）—— ACP 协议规范
- [Model Context Protocol](https://modelcontextprotocol.io/) —— MCP 协议规范
- [ZCode](https://zcode.z.ai) / [智谱 Z.AI](https://z.ai) —— GLM 模型与 ZCode CLI

本项目与智谱 Z.AI 官方无任何关联。ZCode 是智谱 Z.AI 的产品。
