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

- 两种模式：`--prompt`（单次）/ `--target`（长程，与 prompt 互斥）
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
| tool_call / tool_call_update（工具调用展示）| ✅ |
| usage_update（token 用量）| ✅ |
| agent_message_chunk（文本输出）| ✅ |
| agent_thought_chunk（思考过程）| ⚠️ 代码就位，GLM-5.2 不产数据 |
| plan（任务清单）| ⚠️ 代码就位，数据驱动 |
| diff（文件变更）| ⚠️ 仅文件名，无 diff 内容 |

## 会话存储

`--prompt` 和 ACP bridge **共享同一套会话存储**（`~/.zcode/cli/db/db.sqlite`），互通互恢复：
- `--prompt` 创建的会话可被 `session/list` 看到、被 `--resume` 或 ACP resume 恢复
- 反之亦然

## ⚠️ 重要限制（请务必阅读）

本项目是建立在**闭源 ZCode** 之上的非官方桥接器，存在以下固有限制：

1. **依赖闭源软件**：必须先安装 ZCode。ZCode 升级可能随时破坏本项目（协议字段靠逆向确认，无官方保证）。
2. **工具调用 turn 不稳定**：ZCode app-server 的工具调用 turn 时长在 38s～100s+ 波动，有时不完成。
3. **流式是伪流式**：ZCode 是快照式架构（turn 期间不推送中间事件），ACP bridge 的流式文本是 turn 完成后整段发。
4. **diff 无内容**：ZCode 协议层不暴露 oldText/newText，只能列文件名。
5. **GLM-5.2 无推理输出**：思考过程（agent_thought_chunk）在 GLM-5.2 下不触发，需 GLM-5-Turbo。
6. **TUI 不可用**：`zcode` 无参数直接运行（TUI 模式）在独立终端报错（缺 `@zcode/tui` 模块），仅 headless 模式可用。
7. **⚠️ ACP bridge 默认 `mode=yolo`（权限风险）**：为避免工具调用 turn 卡在权限确认，ACP bridge 的 `session/new` 强制以 `mode=yolo` 创建会话（见 `zcode-acp-bridge` 的 `_on_session_new`）。这意味着任意 prompt 都可能触发**无确认的文件修改和命令执行**。作为编辑器集成时请知悉此风险；如需更安全的 `build` 模式（带权限确认），需自行修改并实现 ACP↔ZCode 的 permission 转发（本项目 P4b 未实现）。

## 项目结构

```
zcode-open-bridge/
├── packages/
│   ├── agent-help/      # 能力发现说明书 (stable)
│   ├── mcp-server/      # MCP 桥接 (stable)
│   └── acp-bridge/      # ACP 桥接 (experimental)
├── shared/
│   └── credentials.py   # 凭证读取 (单一真相源)
├── tests/
│   └── test_projection_differ.py
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
