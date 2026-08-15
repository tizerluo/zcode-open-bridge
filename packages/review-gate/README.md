# zcode-review-gate — PR 自动审查闸门

常驻轮询守护进程：监控配置仓库的 open PR，对每个新 head sha 调 bridge 的
`zcode_pr_review` 完成审查（git diff + mimosa 深扫 + ZCode 只读复核），
把带 verdict（pass / concerns / 需人工核对）的结果回贴为 PR 评论。同一
head sha 不重复审（state 文件去重），失败按指数退避重试。审查前会把
clone 工作区 **checkout 到被审 head sha 并回读校验**——mimosa 扫的是
工作区文件，不 checkout 会扫在旧代码上（issue #17）。公开、通用，任何
GitHub 仓库可用。

```
GitHub API ──轮询 open PR──► review-gate (state 文件去重/指数退避)
                               │ git clone/fetch
                               ▼
             checkout 到被审 head sha + rev-parse 回读校验 (issue #17)
                               │ --call zcode_pr_review
                               ▼
             zcode-mcp-server 子进程 (git diff + mimosa 深扫 + ZCode 只读复核;
                             锁/限流重试/只读护栏全在 bridge 侧同源复用)
                               │ 报告 → 解析 P0/P1/P2 → verdict
                               ▼
             PR 评论: ✅ pass / ⚠️ concerns / ❓ 需人工核对
                     + 完整报告 (details 折叠)
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

- **PATH**：systemd --user 默认 `PATH=/usr/local/bin:/usr/bin:/bin`，不含
  `~/.local/bin`（GC-8G 实测踩坑）。unit 模板已默认带
  `Environment=PATH=%h/.local/bin:...`；此外 gate 解析 `mcp_server` 纯
  命令名时若 PATH 找不到会自动回退试 `~/.local/bin/<name>`，双保险。
  但 `git`、`zcode` 仍依赖 PATH——自己改 unit 时别把默认 PATH 行删掉。
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
| `mcp_server` | `zcode-mcp-server` | bridge mcp-server 可执行名/路径（须支持 `--call`）。纯命令名且 PATH 找不到时自动回退试 `~/.local/bin/<name>`（存在且可执行才用，log DEBUG 记录解析结果） |
| `github_api` | `https://api.github.com` | GitHub API base（企业版可改）。clone 的 web 宿主按惯例推导：`api.github.com`→`github.com`，`<host>/api/v3`→`<host>`（GHE），其他形态回退 `github.com` |
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
审查状态：`head_sha` / `status` / `verdict` / `counts` / `report` /
`attempts` / `next_retry_at` / `comment_url` / `error`。
`status ∈ pending|reviewed|failed|comment_failed|gave_up`：`pending` 是
一轮处理中的瞬态；`comment_failed` 表示审查已成功但评论没发出去——此时
`report`/`verdict`/`counts` 已缓存，重试时同 head **只补评论不重跑审查**；
`reviewed` 后 `report` 缓存清空。
**想强制重审某个 PR：删掉对应条目**（或把 PR 推一个新 commit，head 变化会
自动复活重审）。
注意：`gave_up` 时缓存的 `report`（每条最多 `comment.max_body` 字符）会
留在 state 文件里供人工排查——长期积攒关注 state 文件体量，可定期清理
已完结 PR 的条目。

deep 档审查耗时长：gate 起审查子进程时已自动透传
`ZCODE_BRIDGE_REVIEW_TIMEOUT=3600`，自身等子进程的总超时再加 120s 留给
mimosa 扫描与进程收尾（=3720），mcp-server 侧不会以默认 300s 提前掐断
zcode。

## 限制

- **评论以 token 身份发出**：用什么 token 评论就显示什么账号，建议专用 bot
  账号或 GitHub App token。
- **评论 at-least-once**：评论请求发出后响应丢失（超时/连接断开）会按失败
  重试，而 GitHub issue comments 没有幂等键——极端情况下同一 head 可能
  出现重复评论，属已知限制（方向仍是宁多勿漏）。
- **verdict 依赖报告文本解析**：从报告开头解析 `P0/P1/P2` 条数得出
  pass/concerns；解析失败时 fail-safe 为 **concerns**（宁错拦不错放），
  评论里会标注"严重度分布解析失败，请人工核对"。
- **单线程串行**：逐仓逐 PR 串行审查；并发安全靠 bridge mcp-server 侧的
  跨进程文件锁兜底（多实例同时跑也不会并发打爆 zcode 限流）。**同一
  state 文件（同一部署）只允许一个 gate 实例**：启动时对
  `<state_file>.lock` 非阻塞 flock，拿不到锁直接退出（exit 2）——checkout
  发生在 bridge 锁之外，第二个实例会在第一个实例 mimosa 扫描中途换掉
  工作区，静默扫错代码（狗食 review P1-1）。
- **fork PR**：走 `refs/pull/{n}/head` 拉取，无需加 fork 远端；
  审查的是 PR head 快照本身。
- token 不落盘：经 git≥2.31 的 `GIT_CONFIG_COUNT/KEY/VALUE` 环境变量逐
  命令注入 `http.extraHeader`（env 只对本用户可见，优于 argv），
  clone URL / git config / state 文件里都不会有 token。
  认证形态分两路：git smart-HTTP 走 **Basic** header
  （`x-access-token:<token>` 的 base64——实测 GitHub 的 git 端点拒绝
  OAuth token 的 Bearer 形式），REST API 走 **Bearer** header。

## 测试

```bash
python3 tests/test_review_gate.py   # 组件单测 (全 fake, 不碰网络/git/真实路径)
python3 tests/test_mcp_call.py      # mcp-server --call 模式单测
```
