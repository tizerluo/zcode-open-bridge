# zcode-open-bridge

> 非官方社区项目，将 [ZCode](https://zcode.z.ai)（智谱 Z.AI 的 Agentic Coding CLI）接入开放 Agent 生态（MCP / ACP）。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![CI](https://github.com/tizerluo/zcode-open-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/tizerluo/zcode-open-bridge/actions/workflows/ci.yml)

## 这是什么

ZCode 是智谱 Z.AI 出品的 AI 编程 Agent，由 GLM 系列模型驱动。它能力很强，但默认是个"闭源孤岛"——只能在自己的桌面 App 里用，无法被其他 Agent、编辑器、脚本标准化调用。

本项目通过三个轻量组件，把 ZCode 接入开放生态：

```
                    ┌─────────────────────────────────────────┐
                    │            ZCode (GLM-5.2)              │
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

## 快速开始

### 前置条件

- 已安装 [ZCode](https://zcode.z.ai)（需含 CLI，App 内自带）
- Python 3.8+（仅用标准库，零第三方依赖）
- 已通过 ZCode 登录（凭证存在 `~/.zcode/v2/config.json`）

### 让 `zcode` 命令在终端可用

ZCode 的 CLI 藏在 App 内部，默认不在 PATH：

```bash
# macOS
ln -s /Applications/ZCode.app/Contents/Resources/glm/zcode.cjs ~/.local/bin/zcode
```

并在 shell 配置（`~/.zshrc`）里配置凭证（动态读取，不明文存 key）：

```bash
zcode() {
  local cfg="$HOME/.zcode/v2/config.json"
  # 从配置文件动态读取凭证注入环境变量
  eval "$(python3 -c "
import json
c=json.load(open('$cfg'))
for k,v in c['provider'].items():
    if v.get('enabled'):
        o=v['options']
        print(f'export ZCODE_MODEL=\"{next(iter(v.get(\"models\",{}))) or \"GLM-5.2\"}\"')
        print(f'export ZCODE_BASE_URL=\"{o.get(\"baseURL\",\"\")}\"')
        print(f'export ANTHROPIC_API_KEY=\"{o.get(\"apiKey\",\"\")}\"')
        break
")"
  command zcode "$@"
}
```

### 使用三个组件

```bash
# ① 能力发现
./packages/agent-help/zcode-agent-help --pretty

# ② MCP server (注册到 ~/.zcode/cli/config.json 的 mcp.servers)
#    或直接作为 stdio 进程运行
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

暴露两个 MCP tool：

| Tool | 作用 |
|------|------|
| `get_zcode_capabilities` | 返回 ZCode 能力清单（调 agent-help） |
| `zcode_review` | 调 ZCode 审查代码（只读 plan 模式，安全） |

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
| plan（任务清单）| ⚠️ 代码就位，数据驱动 |
| diff（文件变更）| ⚠️ 仅文件名，无 diff 内容 |

#### 双模式自动降级

ACP bridge 支持**事件驱动**（真流式）和**轮询**（伪流式）两种模式，自动选择：

- **事件驱动模式**（ZCode CLI ≥ 0.14.8）：通过 `session/subscribe` 订阅事件流，`model.streaming` 事件携带 `text_delta` 逐段推送，实现**真正的流式文本输出**。工具调用状态也实时推送（scheduled → started → progress → result）。
- **轮询模式**（旧版 CLI 或订阅失败）：自动降级为轮询 `session/read`，turn 完成后整段发文本（伪流式）。

#### 扩展方法（非标准 ACP）

ACP bridge 额外暴露了 ZCode 新版协议方法，供编辑器/脚本调用。按 ZCode 引入版本分组：

**session 级**（`{sessionId}` 定位会话）：

| 扩展方法 | 作用 | 引入版本 | params |
|----------|------|:--------:|--------|
| `session/fork` | 从 checkpoint 分叉新会话 | 0.14.8 | `{sessionId, target?}` |
| `session/rewind` | 回退工作区文件到 checkpoint | 0.14.8 | `{sessionId, target?, expectedRevision?}` |
| `session/goal` | 读取/设置 session 目标 | 0.14.8 | `{sessionId, action: show\|set\|replace\|clear, objective?}` |
| `session/compact` | 压缩对话上下文 | 0.14.8 | `{sessionId}` |
| `session/steer` | turn 进行中追加指令 | 0.14.8 | `{sessionId, content}` |
| `session/setThoughtLevel` | ⭐ 设置思考强度（实测 GLM-5.2: max/high/nothink，按模型不同） | 0.15.0 | `{sessionId, thoughtLevel}` |
| `session/updateRuntimeModelConfig` | 运行时覆盖会话模型配置 | 0.15.0 | `{sessionId, runtimeModel, applyModelSelection?}` |
| `session/cancelBackgroundTask` | 取消后台 Bash 任务 | 0.14.8 | `{sessionId, taskId}` |
| `session/rewindCascade` | 级联回退（与 rewind 同 schema） | 0.15.0 | `{sessionId, target?, scope?, expectedRevision?}` |
| `session/setModel` | 切换会话模型 | 0.14.8 | `{sessionId, modelId}` |
| `session/setMode` | 切换会话权限模式 | 0.14.8 | `{sessionId, mode}` |

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

> workspace 参数可三种方式传入：`workspace`（完整 dict）、`workspacePath`/`cwd`（路径字符串），或缺省时用 bridge 进程的 `cwd`。
> Provider 管理类方法（upsert/remove/updateRegistry）的 `provider`/`registry` 可能含 `apiKey`，bridge 仅透传、不读取/打印其明文。

## 会话存储

`--prompt` 和 ACP bridge **共享同一套会话存储**（`~/.zcode/cli/db/db.sqlite`），互通互恢复：
- `--prompt` 创建的会话可被 `session/list` 看到、被 `--resume` 或 ACP resume 恢复
- 反之亦然

## 凭证与限流（自动化集成必读）

### Model ID 格式（canonical）

**canonical model id = `~/.zcode/v2/config.json` 里 `models` 的 key 原样**（如 `GLM-5.2`），**不加 provider 前缀**。`shared/credentials.py`、MCP server、ACP bridge 三处统一用原始 id。实测 `zai/GLM-5.2` 也兼容，但非 canonical，本项目不使用。

### 凭证注入：显式环境变量优先

三个组件注入凭证的合并顺序为 `{**config_creds, **os.environ}`——**已显式设置的环境变量覆盖 config 读出的值**。便于不改 config 临时调试/覆盖：

```bash
# 临时用另一个模型跑 ACP bridge（覆盖 config 的 GLM-5.2）
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

`zcode_review` 调用 headless zcode 做 PR 审查时，多个 gate 并发可能触发 provider 限流。MCP server 做了三层防护（issue #3）：

| 机制 | 行为 | 配置 |
|------|------|------|
| **进程级文件锁** | 多个 MCP client 并发调用时，串行化 headless review，防并发触发限流 | `ZCODE_BRIDGE_REVIEW_LOCK=0` 关闭 |
| **provider 错误解析** | 识别 429 / 1302 / `Too Many Requests` / `请求过于频繁` / `retry-after`，区分限流/配额/其他 | — |
| **有限重试 + 退避** | 仅对**限流**错误重试（配额/Unauthorized 不重试），退避用 retry-after 或指数退避（`2^n+1`） | `ZCODE_BRIDGE_MAX_RETRIES`（默认 3） |

注意：zcode 内部已有自己的指数退避重试（`_retryWithExponentialBackoff`），MCP 层的重试是补充，默认保守（max 3）。

### 聚焦审查 prompt 建议

为降低限流风险，自动化审查时建议：把 diff/证据作为 `--attach` 附件，prompt 写明"不要 spawn 子代理、不要探索文件系统、只基于附件推理"。

## 版本兼容性

本项目兼容以下 ZCode 版本，**对旧版完全向后兼容**：

| ZCode CLI 版本 | 支持情况 | ACP bridge 流式 | 扩展方法 |
|:--------------:|:--------:|:---------------:|:--------:|
| **0.15.0+**（App 3.2.0+） | ✅ 完整 | **真流式**（事件驱动） | ✅ 全部（含 workspace/*、setThoughtLevel 等） |
| **0.14.8**（App 3.1.4） | ✅ 完整 | **真流式**（事件驱动） | ✅ fork/rewind/goal/compact/steer |
| **0.14.5 ~ 0.14.7** | ✅ 兼容 | 伪流式（自动降级轮询） | ❌（旧版协议未实现） |
| **< 0.14.5** | ⚠️ 未测 | — | — |

**降级行为**（自动，无需手动配置）：
- ACP bridge 检测到 `session/subscribe` 不可用时，自动切换到轮询 `session/read`（伪流式）
- 扩展方法在旧版 ZCode 上会透传后端错误（`-32603 zcode <method> failed: ...`），不影响标准 ACP 方法（new/prompt/cancel/list/resume）。例如在 0.14.8 上调用 `workspace/*` 或 `session/setThoughtLevel`（0.15.0 新增）会得到 `-32603`，调用方应据此做版本判断。

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
5. **GLM-5.2 无推理输出**：思考过程（agent_thought_chunk）在 GLM-5.2 下不触发，需 GLM-5-Turbo。
6. **TUI 不可用**：`zcode` 无参数直接运行（TUI 模式）在独立终端报错（缺 `@zcode/tui` 模块），仅 headless 模式可用。
7. **⚠️ ACP bridge 默认 `mode=yolo`（权限风险）**：为避免工具调用 turn 卡在权限确认，ACP bridge 的 `session/new` 强制以 `mode=yolo` 创建会话（见 `zcode-acp-bridge` 的 `_on_session_new`）。这意味着任意 prompt 都可能触发**无确认的文件修改和命令执行**。作为编辑器集成时请知悉此风险；如需更安全的 `build` 模式（带权限确认），需自行修改并实现 ACP↔ZCode 的 permission 转发（本项目 P4b 未实现）。
8. **⚠️ Provider 管理方法涉及 apiKey**：`workspace/upsertModelProvider`、`workspace/updateProviderRegistry` 的 `provider`/`registry` 参数会携带 `apiKey`（可能为 `{source:"inline", value:"sk-..."}` 明文）。ACP bridge 仅整体透传给 ZCode 后端、不读取也不在日志打印其明文；但调用方应自行确保传输通道（stdio）可信，并避免在日志中回显原始参数。

## 项目结构

```
zcode-open-bridge/
├── packages/
│   ├── agent-help/      # 能力发现说明书 (stable)
│   ├── mcp-server/      # MCP 桥接 (stable)
│   └── acp-bridge/      # ACP 桥接 (experimental)
├── shared/
│   └── credentials.py   # 凭证读取 (单一真相源)
├── skills/
│   └── zcode-bridge-guide/  # 驱动 ZCode 的通用 skill (说明书)
├── tests/
│   ├── test_projection_differ.py
│   └── test_event_translator.py
├── LICENSE              # MIT
└── README.md
```

每个组件都是**单文件、零依赖**，复制一个文件即可独立运行（`shared/credentials.py` 的逻辑已内嵌到 mcp-server/acp-bridge）。

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

# 2. 测试 (纯标准库 unittest, 无需安装依赖)
python3 tests/test_projection_differ.py

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
