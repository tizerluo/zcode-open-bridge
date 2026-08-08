# zcode-review-gate — PR 自动审查闸门

常驻轮询守护进程：监控配置仓库的 open PR，对每个新 head sha 调 bridge 的
`zcode_pr_review` 完成审查（git diff + mimosa 深扫 + ZCode 只读复核），
把带 verdict（pass / concerns）的结果回贴为 PR 评论。同一 head sha 不重复审
（state 文件去重），失败按指数退避重试。公开、通用，任何 GitHub 仓库可用。

```
┌─────────────┐   轮询 open PR    ┌──────────────┐
│  GitHub API │ ◄────────────── │              │
└──────┬──────┘                  │              │
       │ 新 head sha?            │ review-gate  │  (state 文件去重/退避)
       ▼                         │              │
 git clone/fetch ──────────────► │              │
       │                         └──────┬───────┘
       ▼                                │ --call zcode_pr_review
┌─────────────────┐                     ▼
│ zcode-mcp-server│  (git diff + mimosa 深扫 + ZCode 只读复核,
│  (子进程)        │   锁/重试/只读护栏全在 bridge 侧同源复用)
└──────┬──────────┘
       │ 报告 → 解析 P0/P1/P2 → verdict
       ▼
┌─────────────┐
│  PR 评论     │  ✅ pass / ⚠️ concerns + 完整报告 (details 折叠)
└─────────────┘
```

## 前置条件

- 已安装 [ZCode](https://zcode.z.ai) CLI 并完成登录（凭证在 `~/.zcode/v2/config.json`）
- 已安装 mimosa 安全扫描插件（`zcode_pr_review` 依赖；见顶层 README）
- `zcode-mcp-server` 可用（本仓库 `packages/mcp-server`，带 `--call` 模式）：
  cp/ln 到 `~/.local/bin/` 或在配置里用 `mcp_server` 指绝对路径
- git 可用
- **GitHub token**（repo scope）：拉取公开仓可匿名，但**发评论必须 token**。
  解析级联：`GITHUB_TOKEN` env → `GH_TOKEN` env → `gh auth token`

## 安装

```bash
# 1. 脚本就位 (二选一)
cp packages/review-gate/zcode-review-gate ~/.local/bin/
# 或: ln -s "$(pwd)/packages/review-gate/zcode-review-gate" ~/.local/bin/

# 2. 写配置
mkdir -p ~/.config/zcode-review-gate
cat > ~/.config/zcode-review-gate/config.json <<'EOF'
{
  "repos": ["owner/your-repo"],
  "poll_interval_seconds": 300
}
EOF

# 3. 先手动跑一轮验证 (看 stderr 日志, 不装 daemon 也能用)
zcode-review-gate --once

# 4. 装 systemd --user 单元
mkdir -p ~/.config/systemd/user
cp packages/review-gate/zcode-review-gate.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now zcode-review-gate

# 5. 服务器上希望注销后仍运行 (无 linger 时 user manager 随最后一个会话退出)
loginctl enable-linger "$USER"
```

注意事项：

- **PATH**：systemd --user 的非登录 shell 环境极简，`~/.local/bin` 不一定在
  PATH 里。`zcode-review-gate` 调 `git`、`zcode-mcp-server`（以及它间接调
  `zcode`）都依赖 PATH，必要时在 unit 里加
  `Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin`。
- **sudo -u 场景**：用 `sudo -u <user> systemctl --user ...` 操作别人的
  user manager 时，需要 `XDG_RUNTIME_DIR=/run/user/$(id -u <user>)`，
  否则连不上 user bus。
- **token**：不要把 token 写进 config.json（脚本也不读）。用
  `systemctl --user edit zcode-review-gate` 加
  `Environment=GITHUB_TOKEN=...` override，或 `gh auth login` 后靠级联解析。

## 配置参考

`~/.config/zcode-review-gate/config.json`（全部键可选，有默认值）：

| 键 | 默认 | 说明 |
|---|---|---|
| `repos` | `[]` | 监控的仓库列表，`owner/name` 格式。**缺省且 CLI 无 `--repo` 时拒绝启动** |
| `poll_interval_seconds` | `300` | 轮询间隔（秒） |
| `state_file` | `~/.local/state/zcode-review-gate/state.json` | 状态文件（去重/退避） |
| `clone_root` | `~/.local/state/zcode-review-gate/clones` | 仓库 clone 存放目录 |
| `mcp_server` | `zcode-mcp-server` | bridge mcp-server 可执行名/路径（须支持 `--call`） |
| `github_api` | `https://api.github.com` | GitHub API base（企业版可改） |
| `review.depth` | `deep` | 审查深度：`normal`（快扫）/ `deep`（含业务逻辑投研） |
| `review.focus` | `""` | 额外审查重点（透传给 zcode prompt） |
| `retry.base_seconds` | `300` | 退避基数：失败后 `base * 2^(attempts-1)` |
| `retry.max_seconds` | `3600` | 退避封顶 |
| `retry.max_attempts` | `5` | 最大尝试次数，达到后 `gave_up`（head 更新才复活） |
| `comment.enabled` | `true` | 是否回贴 PR 评论（false 时只审只落状态，dry-run 用） |
| `comment.max_body` | `60000` | 评论体上限（GitHub 硬上限 65536，留余量；超出截断报告正文） |

环境变量覆盖（优先级高于配置文件）：

| env | 覆盖的键 |
|---|---|
| `GATE_CONFIG` | 配置文件路径本身（等价 `--config`） |
| `GATE_STATE_FILE` | `state_file` |
| `GATE_CLONE_ROOT` | `clone_root` |
| `GATE_MCP_SERVER` | `mcp_server` |
| `GATE_POLL_INTERVAL` | `poll_interval_seconds` |

路径值支持 `~` 展开。

## 卸载

```bash
systemctl --user disable --now zcode-review-gate
rm ~/.config/systemd/user/zcode-review-gate.service
systemctl --user daemon-reload
# 按需清理: ~/.local/bin/zcode-review-gate
#           ~/.config/zcode-review-gate/
#           ~/.local/state/zcode-review-gate/   (state + clones, 可能很大)
```

## 运维

```bash
# 看日志
journalctl --user -u zcode-review-gate -f

# 手动跑一轮 (调试/cron)
zcode-review-gate --once

# 只审指定 PR (实测用, 不影响其他 PR)
zcode-review-gate --once --repo owner/repo --pr 5

# 更细的日志
zcode-review-gate --once --log-level DEBUG
```

state 文件（默认 `~/.local/state/zcode-review-gate/state.json`）记录每个 PR 的
审查状态：`head_sha` / `status`（`reviewed|failed|gave_up`）/ `verdict` /
`attempts` / `next_retry_at` / `comment_url` / `error`。
**想强制重审某个 PR：删掉对应条目**（或把 PR 推一个新 commit，head 变化会
自动复活重审）。

## 限制

- **评论以 token 身份发出**：用什么 token 评论就显示什么账号，建议专用 bot
  账号或 GitHub App token。
- **verdict 依赖报告文本解析**：从报告开头解析 `P0/P1/P2` 条数得出
  pass/concerns；解析失败时 fail-safe 为 **concerns**（宁错拦不错放），
  评论里会标注"严重度分布解析失败，请人工核对"。
- **单线程串行**：逐仓逐 PR 串行审查；并发安全靠 bridge mcp-server 侧的
  跨进程文件锁兜底（多实例同时跑也不会并发打爆 zcode 限流）。
- **fork PR**：走 `refs/pull/{n}/head` 拉取，无需加 fork 远端；
  审查的是 PR head 快照本身。
- token 不落盘：只经 `git -c http.extraHeader` 进程内注入，clone URL /
  git config / state 文件里都不会有 token。

## 测试

```bash
python3 tests/test_review_gate.py   # 组件单测 (全 fake, 不碰网络/git/真实路径)
python3 tests/test_mcp_call.py      # mcp-server --call 模式单测
```
