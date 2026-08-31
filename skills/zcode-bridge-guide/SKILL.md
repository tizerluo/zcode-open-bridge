---
name: zcode-bridge-guide
version: 1.3.0
description: 驱动 ZCode（智谱 GLM 系列 coding agent）的通用说明书。覆盖三种接入模式（CLI --prompt / ACP bridge / MCP tools）、凭证配置、真流式/伪流式双模式、扩展协议方法（session 级 + workspace 级）、思考强度控制、任务书模板、已知坑。需要把 ZCode 当子代理编排、跑编码/审查任务、或集成进编辑器时用。兼容 ZCode CLI 0.14.5 ~ 0.16.1（App 3.6.5）。
user-invocable: true
---

# 驱动 ZCode（三模式通用说明书）

> 本 skill 是 [zcode-open-bridge](https://github.com/tizerluo/zcode-open-bridge) 项目的配套说明书。
> 兼容 ZCode CLI **0.14.5 ~ 0.16.1**（App 3.6.5，实测含 3.2.1~3.6.5）。新版功能（事件驱动真流式、fork/goal/compact、workspace/*、setThoughtLevel 思考强度控制）在旧版上自动降级或返回 `-32603`；`session/steer`、`session/rewind*`、`prompt/enhance*` 已于 0.16 移除，调用返回 `-32601`。

## 前置条件

1. 已安装 [ZCode](https://zcode.z.ai) 桌面 App（含 CLI）
2. Node.js ≥ 18（`zcode.cjs` 的 shebang 是 `#!/usr/bin/env node`，CLI 由 node 执行）
3. 已通过 ZCode 登录（凭证存在 `~/.zcode/v2/config.json`）
4. （可选）已 clone zcode-open-bridge 仓库

## 入口与认证

### 让 `zcode` 命令可用

ZCode 的 CLI 藏在 App 内部，默认不在 PATH：

```bash
# macOS
ln -s /Applications/ZCode.app/Contents/Resources/glm/zcode.cjs ~/.local/bin/zcode

# Linux（AppImage 解压安装到 /opt/ZCode 时）
ln -s /opt/ZCode/app/resources/glm/zcode.cjs ~/.local/bin/zcode
```

### 凭证配置（⚠️ `--prompt` 模式必读）

**关键坑**：`--prompt` / `--target` 模式**不读** `~/.zcode/cli/config.json` 的 model 字段，必须用环境变量注入。ZCode 桌面 App 的凭证存在 `~/.zcode/v2/config.json` 的 `provider` 段。

**推荐方案**：在 `~/.zshrc` 配置一个 shell 函数，动态读取凭证（不明文存 key）：

```bash
zcode() {
  local cfg="$HOME/.zcode/v2/config.json"
  eval "$(python3 -c "
import json
c=json.load(open('$cfg'))
for k,v in c['provider'].items():
    if v.get('enabled'):
        o=v['options']
        print(f'export ZCODE_MODEL=\"{next(iter(v.get(\"models\",{}))) or \"GLM-5.3\"}\"')
        print(f'export ZCODE_BASE_URL=\"{o.get(\"baseURL\",\"\")}\"')
        print(f'export ANTHROPIC_API_KEY=\"{o.get(\"apiKey\",\"\")}\"')
        break
")"
  command zcode "$@"
}
```

> ACP bridge、MCP server 和 agent-help 内部已实现同样的凭证读取逻辑（`shared/credentials.py`），无需额外配置。

### ⚠️ 非交互 shell 坑（Codex/agent 编排注意）

非交互 shell（`zsh -lc`）不会 source `~/.zshrc`，`zcode` 可能解析成 npm binary 而非 shell function。检查：
- `zsh -ic 'type -a zcode'`（交互路径，才会看到 function）
- 若非交互不可用，改用显式 env 包装（见下方 CLI 模式）

---

## 三种模式，按场景选

| 模式 | 适合场景 | 会话能力 | 流式 | 接入方式 |
|------|---------|---------|:----:|---------|
| **CLI `--prompt`** | 单轮任务（审查、解释、生成） | 无（一次跑完） | ❌ | `zcode --prompt` |
| **ACP bridge** | 多轮编排、子代理、编辑器集成 | 有（session/resume/fork） | ✅ 真流式 (0.14.8+) | `zcode-acp-bridge` |
| **MCP tools** | MCP client 内直接调用（轻量） | 有（MCP server 管理） | ❌ | `zcode-mcp-server` |

**选择指南**：
- 简单任务（一轮跑完）→ **CLI**
- 需要多轮交互 / 流式输出 / 结构化事件 / resume → **ACP bridge**
- 在 MCP client（Claude Code/Cursor）内直接调用 → **MCP tools**

---

## 模式一：CLI `--prompt`（最简单）

### 执行形态（改代码）
```bash
zcode --prompt "<prompt>" --attach <file> --mode build --json
# --mode build   允许写文件 + 执行命令
# --mode plan    只读分析，不改文件（注意: 只禁写, 不禁止读探索/子代理;
#                自动化审查推荐用下方"评审形态"的 yolo+黑名单组合）
# --mode edit    允许写文件，不执行命令
# --mode yolo    全自动（--prompt 的默认模式）
# --json         输出 JSON（含 sessionId, response, usage）
# --attach       附带文件（可重复）
# --resume       续接指定 session（--json 返回 sessionId）
# --cwd          指定工作目录
```

### 长程任务（设定 session 目标）
```bash
# 方式1: --target (与 --prompt 互斥)
zcode --target "重构认证模块" --mode yolo

# 方式2: prompt 内用 /goal (等价于 --target)
zcode --prompt "/goal 重构认证模块" --mode yolo

# 替换已有目标
zcode --target "新目标" --target-replace
```

### 评审形态（只读）
```bash
# MCP server 的 zcode_review 内部用的形态（推荐，物理只读）：
zcode --prompt "<review-prompt>" --attach <file> --mode yolo \
  --disallowed-tools "Write Edit MultiEdit ApplyPatch Bash js js_reset js_add_node_module_dir mcp__node_repl__js mcp__node_repl__js_reset mcp__node_repl__js_add_node_module_dir" \
  --no-color --json
# 原理: --mode yolo 全程免授权; --disallowed-tools 是工具集级物理移除 (先于权限层,
# yolo 也绕不过), 写/执行工具连同 Node REPL 一族一起禁 = 改不了任何文件。
# 注意: js / mcp__node_repl__js* 必须同禁, 否则可被 execSync 打穿 Bash 黑名单 (0.16.1 实测)。
# 旧形态 --mode plan 已不推荐: plan 只禁"改文件", 读探索/子代理照样放行 (限流超时主因),
# 且 plan→build 惯性容易让 review 变成"边审边修"。
```

### 显式 env 包装（非交互 shell 用）
```bash
read -r model base_url api_key < <(python3 - <<'PY'
import json, pathlib
cfg = json.loads((pathlib.Path.home() / ".zcode/v2/config.json").read_text())
for pid, p in cfg.get("provider", {}).items():
    if p.get("enabled"):
        o = p["options"]
        # canonical model id = config 里 models 的 key 原样 (如 GLM-5.3), 不加 provider 前缀。
        # 实测 "zai/GLM-5.2" 也兼容, 但与本项目的权威实现 (shared/credentials.py) 不一致, 故统一用原始 id。
        m = next(iter(p.get("models", {})), "")
        print(m, o.get("baseURL", ""), o.get("apiKey", ""))
        break
PY
)
ANTHROPIC_API_KEY="$api_key" ZCODE_BASE_URL="$base_url" ZCODE_MODEL="$model" \
  zcode --prompt "<prompt>" --mode plan --json
```

### ⚠️ CLI 模式已知限制
1. **不支持 stdin 管道**：不能 `cat file | zcode --prompt`，必须用 `--attach`
2. **无流式输出**（`--stream-json` 不支持）：turn 结束一次性返回
3. **tool call 轮次不稳定**：duration 38s~100s+，有时不完成
4. **自由 prompt 可能触发限流**：开放式 prompt 会 spawn 多个 explore 子代理，撞 z.ai 限流。修法：把证据塞进 `--attach`，prompt 写明「**除读附件外**不要调用工具、不要 spawn 子代理，只基于附件推理」。⚠️ 措辞必须留"读附件"口子——附件要靠 Read 工具读，裸写「不要调用工具」会让 ZCode 自我纠结浪费推理（2026-07-08 实测）。MCP server 的 review 类 tool（`zcode_review` / `zcode_security_review` / `zcode_pr_review`）已内置等效纪律（prompt 内置「只审不修」约束 + 写/执行工具物理禁用 + 证据走附件——prompt 措辞与手工版不同，靠物理禁用达成同一目的），走 MCP 调用时无需手工处理

---

## 模式二：ACP bridge（多轮编排，真流式）

ACP bridge 是把 ZCode 当子代理编排的**最佳方式**——支持多轮会话、session resume、真流式文本、实时工具调用追踪。

### 启动
```bash
# 方式1: 直接运行（需要 zcode 在 PATH 或用 ZCODE_BIN 指定）
./packages/acp-bridge/zcode-acp-bridge

# 方式2: 指定 zcode 路径
ZCODE_BIN=/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs \
  ./packages/acp-bridge/zcode-acp-bridge

# 启动后等待 ACP JSON-RPC 消息（stdio）
```

### ACP 协议流程

```
1. initialize         → 握手，声明 capabilities
2. session/new        → 创建会话（返回 sessionId）
3. session/prompt     → 发送 prompt（非阻塞，返回 accepted）
4. 收 session/update  → 流式事件（文本逐段、工具实时状态、usage）
5. 等 stopReason      → end_turn / cancelled / max_turn_requests
6. session/resume     → 续接已有会话（跨多轮）
7. session/cancel     → 取消正在执行的 turn
```

> 注：0.16.1 的 ZCode app-server 已把核心方法改名（`session/new`→`session/create`、`session/prompt`→`session/send`、`session/cancel`→`session/stop`）并去掉消息信封的 `jsonrpc` 字段。以上是 ACP 面标准流程，**保持不变**，rename 由 bridge 内部翻译。

### 从脚本驱动 ACP bridge（Python 示例）

```python
import json, subprocess, select, time

proc = subprocess.Popen(
    ["./packages/acp-bridge/zcode-acp-bridge"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    text=True, bufsize=1,
    env={**__import__("os").environ, "ZCODE_BIN": "zcode"}
)

def send(msg):
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()

# 1. initialize
send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {"protocolVersion": 1}})
# ... 读取 id=1 响应

# 2. session/new
send({"jsonrpc": "2.0", "id": 2, "method": "session/new",
      "params": {"cwd": "/path/to/project"}})
# ... 读取 sessionId

# 3. session/prompt (会收到流式 session/update notification)
sid = "sess_xxx"
send({"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
      "params": {"sessionId": sid,
                 "prompt": [{"type": "text", "text": "审查这段代码"}]}})
# 注: prompt 参数也兼容纯字符串 "审查这段代码" 或 {"content": "..."} 键别名,
#     bridge 内部统一归一为 ContentBlock[] (zcode review P3-5)

# 4. 收集流式事件 + 等最终响应
chunks = []
while True:
    line = proc.stdout.readline()
    if not line:
        break
    msg = json.loads(line)
    if msg.get("id") == 3:
        print("stopReason:", msg["result"]["stopReason"])
        break
    elif msg.get("method") == "session/update":
        update = msg["params"]["update"]
        su = update.get("sessionUpdate", "")
        if su == "agent_message_chunk":
            chunks.append(update["content"]["text"])
        elif su in ("tool_call", "tool_call_update"):
            print("工具调用:", update.get("toolCallId"), update.get("status"))

print("回复:", "".join(chunks))
proc.terminate()
```

### 真流式 vs 伪流式（双模式）

ACP bridge 根据运行时检测选择模式：

| 模式 | 条件 | 文本到达 | 工具状态 |
|------|------|----------|----------|
| **事件驱动（真流式）** | CLI ≥ 0.14.8 且 subscribe 成功 | 逐段推送（2~8+ 段） | 实时（scheduled→started→result） |
| **轮询（伪流式）** | 仅限 legacy（< 0.16）旧协议模式 | turn 完成后整段发 | turn 完成后一次性 |

**无需手动选择**——bridge 先尝试 `session/subscribe`；legacy（< 0.16）模式下失败自动降级轮询。**0.16+ 不再自动降级**：新协议模式下 subscribe 失败直接报错 `-32603`（"0.16+ 必须走事件订阅；轮询降级仅限旧协议模式"）。

### 扩展方法（非标准 ACP）

ACP bridge 暴露的 ZCode 新版协议方法，按定位维度分组。**session 级**用 `{sessionId}` 定位会话；**workspace 级**用 `{workspace, workspaceKey}`（或 `workspacePath`/`cwd`）定位工作区，不依赖会话。

**session 级扩展方法**：

| 方法 | 作用 | 引入版本 | params |
|------|------|:--------:|--------|
| `session/fork` | 从 checkpoint 分叉新会话 | 0.14.8 | `{sessionId, target?}` |
| `session/rewind` ❌ | 回退工作区文件到 checkpoint（**0.16 已移除**） | 0.14.8 | `{sessionId, target?, expectedRevision?}` |
| `session/goal` | 读取/设置 session 目标 | 0.14.8 | `{sessionId, action: show\|set\|replace\|clear, objective?}` |
| `session/compact` | 压缩对话上下文 | 0.14.8 | `{sessionId}` |
| `session/steer` ❌ | turn 进行中追加指令（**0.16 已移除**，语义并入 `session/send`） | 0.14.8 | `{sessionId, content}` |
| `session/setThoughtLevel` | ⭐ 设置思考强度 | 0.15.0 | `{sessionId, thoughtLevel}` |
| `session/setModel` / `setMode` | 切换模型 / 权限模式 | 0.14.8 | `{sessionId, modelId}` / `{sessionId, mode}` |
| `session/cancelBackgroundTask` | 取消后台 Bash 任务 | 0.14.8 | `{sessionId, taskId}` |
| `session/rewindCascade` ❌ | 级联回退（同 rewind schema，**0.16 已移除**） | 0.15.0 | `{sessionId, target?, scope?, expectedRevision?}` |
| `session/updateRuntimeModelConfig` | 运行时覆盖模型配置 | 0.15.0 | `{sessionId, runtimeModel, applyModelSelection?}`（0.16 起 `runtimeModel.revision` 必填） |

> ❌ **0.16 已移除**：`session/steer`、`session/rewind`、`session/rewindCascade` 已从 app-server 删除（steer 并入 `session/send`——turn 进行中发送即 steer；rewind 仅剩 slash 命令 `/rewind`），0.16.1 上调用会收到 `-32601`。
>
> ℹ️ **0.16 schema 变更**：`session/updateRuntimeModelConfig` 在 0.16.1 仍存活（实测），但 schema 新要求 `runtimeModel.revision`（string）必填。

**workspace 级扩展方法**（0.15.0+）：

| 方法 | 作用 | params |
|------|------|--------|
| `workspace/readState` | 读工作区状态（模型目录/设置） | `{workspace, runtimeModel?}` |
| `workspace/generateText` | 一次性文本生成（不建会话） | `{workspace, modelRef, prompt, querySource, ...}` |
| `workspace/setDefaultModel` / `setDefaultMode` / `setDefaultThoughtLevel` | 设工作区默认值（持久化） | `{workspace, model\|mode\|thoughtLevel, expectedWorkspaceRevision?}` |
| `workspace/upsertModelProvider` / `removeModelProvider` / `updateProviderRegistry` | 管理模型供应商（含 apiKey，敏感） | `{workspace, provider\|providerId\|registry, ...}` |

**prompt 级扩展方法**（App 3.3.0 引入；❌ **0.16 已全部移除**，无替代）：

| 方法 | 作用 | params |
|------|------|--------|
| `prompt/enhance` ❌ | 同步增强提示词（阻塞返回增强后文本） | `{workspace, prompt, sessionId?, context?}` |
| `prompt/enhance/start` ❌ | 异步启动增强 job（阻塞等待结果通知） | `{workspace, prompt, requestId, sessionId?, context?}` |
| `prompt/enhance/cancel` ❌ | 取消进行中的增强 job | `{requestId}` |

> 💡 `prompt/enhance` 让外部调用方复用 ZCode 内置的"提示词增强"能力（模型把简短 prompt 扩展成精确、结构化的指令）。同步版（`prompt/enhance`）直接阻塞返回 `{enhanced}`；异步版（`start`）转成阻塞语义——start 后内部等待 server 推送的 `result` 通知，期间收到的 `cancel` 会被转发给后端。`prompt/enhance/result` 本身是 server 推送通知，非 client 可调。App < 3.3.0 调用会返回 `-32603`；CLI ≥ 0.16 调用会返回 `-32601`（方法已删除），bridge 映射为明确错误文案。

> ⚠️ `session/goal action=set` 会启动内部 AI turn（异步），耗时 10~45s。bridge 会自动等待 prompt lock 释放后返回，但调用方需预期较长延迟。
>
> 💡 **思考强度控制**（0.15.0+）：`session/setThoughtLevel` 可在跑任务前调高（如 `high`/`max`）让模型多推理，跑完后调回（如 `nothink`）。`thoughtLevel` 是动态值，按当前模型的 reasoning 能力决定可选值（实测 GLM-5.2：`max`/`high`/`nothink`，其他模型可能不同）。这与 drive-claude 的 `--effort`、drive-codex 的 reasoning effort 形成对称能力。设置后会话内的后续 turn 都生效。
>
> ⚠️ **apiKey 透传**：Provider 管理类方法的参数会携带 `apiKey`，bridge 仅透传不打印明文；调用方应确保 stdio 通道可信。

### ⚠️ ACP 模式已知限制
1. **默认 `mode=yolo`**：所有 prompt 可能触发无确认的文件修改和命令执行。信任环境要隔离（worktree / 受信目录）
2. **diff 无内容**：协议层不暴露 oldText/newText，只能列文件名
3. **轮次时间不稳定**：同 CLI 模式，38s~100s+
4. **会话存储共享**：ACP bridge 和 CLI `--prompt` 共享 `~/.zcode/cli/db/db.sqlite`，可跨方式 resume

---

## 模式三：MCP tools（MCP client 内直接调用）

MCP server 暴露四个标准 MCP tool，供 Claude Code / Cursor 等 MCP client 及 zcode 自身调用。

### 注册到 MCP client

按注册目标分两种场景，配置键位不同：

**A. 通用 MCP client（Claude Code / Cursor 等）**——用 MCP client 自身格式（如 Claude Code 的 `.mcp.json` / Cursor 配置），顶层 `mcpServers` 键：

```json
{
  "mcpServers": {
    "zcode-mcp": {
      "command": "/path/to/zcode-open-bridge/packages/mcp-server/zcode-mcp-server",
      "args": []
    }
  }
}
```

**B. zcode 自身（`~/.zcode/cli/config.json`）**——键位是嵌套 `mcp.servers`，不是顶层 `mcpServers`：

```json
{
  "mcp": {
    "servers": {
      "zcode-mcp": {
        "command": "/path/to/zcode-open-bridge/packages/mcp-server/zcode-mcp-server",
        "args": []
      }
    }
  }
}
```

> 📌 zcode 用户配置以 `mcp.servers` 为准（0.16.5 实测 + bundle 佐证；与 README 口径一致）：bundle 中设置键位映射（`McpServers:"mcp.servers"`）、分层配置合并与诊断代码均只读 `mcp.servers`，顶层 `mcpServers` 键不被用户配置读取（该字符串仅见于 plugin manifest / 请求载荷）。早期版本此处称「`mcpServers` 键在 CLI 0.16.1 实测仍受支持」，与 0.16.5 bundle 证据矛盾且已无法复核，弃用该说法。

### 可用 tools

| Tool | 用途 | 安全性 |
|------|------|--------|
| `get_zcode_capabilities` | 返回 ZCode 完整能力清单 | 只读 |
| `zcode_review` | 调用 ZCode 审查代码 | 只读（`--mode yolo` + `--disallowed-tools` 物理禁用写/执行工具，全程免授权但改不了文件） |
| `zcode_security_review` | 安全专项审查：mimosa 规则引擎全仓预扫 → ZCode 逐条核实 findings | 只读（同上；需本机装有 mimosa 或设 `ZCODE_BRIDGE_MIMOSA_ROOT`） |
| `zcode_pr_review` | PR 审查：git diff 改动清单 + mimosa 聚焦深扫（focus_files）→ ZCode 出 P0/P1/P2 复核报告 + 能否合并结论 | 只读（同上；base 不传自动探测，默认 depth=deep） |

### `zcode_pr_review` 参数
- `path`：git 仓库目录（默认当前目录）
- `base`：PR base 分支/commit，不传自动探测（origin/HEAD → main/master → origin/main|master）
- `head`：默认 HEAD
- `depth`：mimosa 扫描深度，PR 审查默认 `deep`
- `focus`：可选，额外审查重点
- `cwd`：zcode 工作目录（默认与 path 相同）

> 流程：`git diff base...head`（三点 = merge-base 语义）算改动清单与完整 diff → mimosa 全仓扫描（`focus_files`=改动文件，deep 档业务逻辑投研聚焦）→ diff+findings 作附件喂 zcode，出「P0 阻断/P1 应修/P2 建议 + findings 确认/误报/存疑 + 能否合并」报告。diff 超 `ZCODE_BRIDGE_PR_DIFF_MAX`（默认 500KB）截断但改动文件清单完整。相对 base 无改动时直接返回提示、不消耗 LLM 调用。

### `zcode_review` 参数
- `files`：要审查的文件路径列表
- `code`：直接传入代码文本（与 files 二选一或组合）
- `focus`：审查重点（如 "安全性"、"性能"、"找 bug"）
- `cwd`：工作目录（影响 zcode 的项目上下文）

### `zcode_security_review` 参数
- `path`：要扫描审查的项目目录（默认当前目录）
- `depth`：`normal`（默认，同步快扫秒级）/ `deep`（含业务逻辑投研，异步 start/status 管线，400 文件项目 ~13s）
- `focus_files`：可选，仅 deep：优先复核的项目内文件（如本次改动的文件）。**是优先级提示不是过滤器**，静态引擎仍全量扫
- `focus`：可选，额外审查重点（如 "重点关注注入与鉴权"）
- `cwd`：zcode 工作目录（默认与 path 相同）

> 两阶段流程：① mimosa 扫描（确定性规则引擎，零 LLM 流量零网络）出 findings；② findings 作 `--attach` 附件喂 zcode 逐条核实（确认漏洞/误报/存疑 + 攻击路径 + 修复建议），并可发现清单之外的问题。mimosa 定位：`ZCODE_BRIDGE_MIMOSA_ROOT` 优先，否则探测 `~/.local/share/mimosa/*` 与 `~/.zcode/cli/plugins/cache/*/mimosa/*`；超时 env：normal 档 `ZCODE_BRIDGE_MIMOSA_TIMEOUT`（默认 180s），deep 档 `ZCODE_BRIDGE_MIMOSA_DEEP_TIMEOUT`（默认 900s）+ 轮询间隔 `ZCODE_BRIDGE_MIMOSA_POLL_INTERVAL`（默认 2s）。

---

## 任务书模板（派 ZCode 跑任务时用）

派 ZCode 跑编码任务时，**不要只给一个 prompt**——写结构化任务书。

### 必备要素
```markdown
## 必读清单
- 现状代码锚点：<要扩展的函数/要复用的测试基建的具体路径>

## 执行要点
- <安全纪律、约束、依赖白名单>

## 完成标准（硬门）
- build 命令：<cmd>
- test 命令：<cmd>
- 期望结果：<具体数字/状态>

## 职责边界
- 只改文件 + 跑 build/test，不碰 git
- 遇到改变边界的问题写 BLOCKED.md 后停止

## 结束总结要求
- 做了什么、build/test 结果、关键决策
```

### 为什么这样写
- **必读清单**：省 agent 自己摸索，减少幻觉（无锚点时 agent 可能自创接口）
- **完成标准硬门**：agent 自报 build/test 结果不可信，必须本地重跑验证
- **职责边界**：agent 不碰 git（编排层拥有 git），遇到问题写 BLOCKED.md 停止

---

## 独立复核（不信 agent 自报）

**核心原则**：agent 报的 build/test 结果不可信，必须本地重跑验证。

```bash
cd <worktree>
npm install  # agent 可能加了依赖
npm run build
npm test     # 全量，看实际数字
# 对比 agent 报的数字 vs 实际数字
```

---

## 版本兼容性

本 skill 及 zcode-open-bridge 项目兼容以下 ZCode 版本：

| ZCode CLI 版本 | 支持情况 | 差异 |
|:--------------:|:--------:|------|
| **0.16.1** (App 3.6.5) | ✅ 完整 | ACP bridge 真流式；session/* + workspace/* 可用（steer/rewind*/prompt/enhance* 已于 0.16 移除；updateRuntimeModelConfig 存活但 `runtimeModel.revision` 必填） |
| **0.15.x** (App 3.5.x) | ✅ 完整 | ACP bridge 真流式；扩展方法同 0.15.0 行（App 功能面：3.5.2 内置网页应用、PDF 预览，见规格书 changelog） |
| **0.15.x** (App 3.4.x) | ✅ 完整 | ACP bridge 真流式；扩展方法同 0.15.0 行（App 功能面：3.4.2 定时任务 cron、Kimi K3，见规格书 changelog） |
| **0.15.0** (App 3.3.x) | ✅ 完整 | ACP bridge 真流式；全部扩展方法可用（含 workspace/*、setThoughtLevel、**prompt/enhance**） |
| **0.15.0** (App 3.2.0 ~ 3.2.5) | ✅ 完整 | 同上，但无 prompt/enhance（3.3.0 引入） |
| **0.14.8** (App 3.1.4) | ✅ 完整 | ACP bridge 真流式；fork/rewind/goal/compact/steer 可用；workspace/* 与 setThoughtLevel 返回 -32603 |
| **0.14.5 ~ 0.14.7** | ✅ 兼容 | ACP bridge 自动降级伪流式；扩展方法不可用（协议未实现） |
| **< 0.14.5** | ⚠️ 未测 | CLI `--prompt` 基本可用；ACP bridge 未验证 |

> 注：CLI 版本号相同不代表协议面相同——`prompt/enhance` 是 App 3.3.0 引入的协议方法（CLI 同为 0.15.0，仅 App 3.3.0+ 的 app-server 支持），又于 0.16 整体移除，仅 0.15.0 + App ≥ 3.3.0 的组合可用。

**降级行为**：
- 轮询降级**仅限 legacy（< 0.16）协议模式**：旧版下 `session/subscribe` 不可用时自动切换到轮询 `session/read`（伪流式）。**0.16+ 不再自动降级**——新协议模式下 subscribe 失败直接报错 `-32603`（"0.16+ 必须走事件订阅；轮询降级仅限旧协议模式"）
- 轮询（legacy）路径收不到 `turn.failed` 事件（projection/messages 无失败标志），turn 失败只能靠「idle 但本轮无实质输出（text/tool/patch）」的启发式检测，可能误报（成功但无实质输出的 turn 被判失败）/漏报（失败前已吐出部分内容的 turn 被当成功）；0.16+ 事件路径无此局限（`turn.failed` 终止帧已能正确判失败）
- 扩展方法（fork/rewind/goal/compact/steer）在旧版 ZCode 上会透传后端错误（`-32603 zcode <method> failed: ...`），不影响标准 ACP 方法
- 调用 0.16 已删除的方法（`session/steer`、`session/rewind*`、`prompt/enhance*`）会收到 `-32601 Method not found`，bridge 映射为明确错误文案（"当前 ZCode 版本已移除该能力 (<方法名>); 该 ZCode 版本不支持此能力"）

---

## 成功判定

### CLI 模式
- `returncode == 0` + stdout 含有效内容 → 成功
- `--json` 模式：检查 `response` 字段是否存在
- `sessionId` 可用于后续 `--resume`

### ACP 模式
- `session/prompt` 最终响应 `stopReason: end_turn` → 正常完成
- `stopReason: cancelled` → 被取消
- `stopReason: max_turn_requests` → 超时（120s）
- 流式事件中的 `agent_message_chunk` → 文本输出（真流式有多段）

### MCP 模式
- `zcode_review` / `zcode_security_review` / `zcode_pr_review` 返回 `{content: [{type: "text", text: "..."}]}`；text 是 zcode `--json` 输出中提取的 `response` 字段（输出非 JSON 时原样返回）
- `isError: true` → 失败

---

## 自检

```bash
# 1. CLI 模式自检
zcode --prompt "Reply with exactly: ZCODE_OK" --json 2>&1 | tail -3
# 输出含 ZCODE_OK + rc=0 → 通

# 2. ACP bridge 握手自检
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1}}' | \
  ./packages/acp-bridge/zcode-acp-bridge 2>/dev/null | head -1 | python3 -c "
import sys, json
d = json.loads(sys.stdin.readline())
print('ACP OK:', d.get('result', {}).get('agentInfo', {}).get('name', 'unknown'))
"

# 3. 能力发现自检
./packages/agent-help/zcode-agent-help --pretty 2>/dev/null | head -5
```

---

## 参考

- **项目仓库**：[zcode-open-bridge](https://github.com/tizerluo/zcode-open-bridge)
- **能力发现说明书**：`zcode-agent-help --pretty`（输出 ZCode 全部能力清单）
- **协议文档**：README 的「ACP bridge」和「扩展方法」章节
- **同系列驱动 skill**：`drive-zcode`（个人实战版，含 spec-pr-pipeline 经验）
