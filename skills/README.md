# skills

两个给 Claude Code / Codex 用的技能，让 router 和 worker 都按同一套规则写进度记录（`progress` 事件）：

- `progress-router/`：router 什么时候写 plan / dispatch / review / supersede / cancel / claim，子 router 规则。
- `progress-worker/`：worker 开工、卡住、交付时怎么写；`SPEC_SNIPPET.md` 是贴在 worker spec 末尾的固定片段。

巡检（`autotaskgen/patrol.py`）代派 worker 时会自动把 SPEC_SNIPPET 附到 spec 末尾；`patrol.py` 里内置了同一份文字，`SPEC_SNIPPET.md` 必须与它一致（测试会检查，改一处要两处一起改）。

## 安装（按需手动执行，本仓库不会自动安装）

每个技能是一个目录，里面有 `SKILL.md`。把目录软链或复制到对应 agent 的技能目录，路径换成你的仓库位置：

```
REPO=/Volumes/remote/project/autotaskgen

# Claude Code
mkdir -p ~/.claude/skills
ln -s $REPO/skills/progress-router ~/.claude/skills/progress-router
ln -s $REPO/skills/progress-worker ~/.claude/skills/progress-worker

# Codex
mkdir -p ~/.codex/skills
ln -s $REPO/skills/progress-router ~/.codex/skills/progress-router
ln -s $REPO/skills/progress-worker ~/.codex/skills/progress-worker
```

- Orca 的 agent 技能目录：用 Orca 里该 agent 配置的技能目录（以 Orca 设置为准），同样软链这两个目录。
- 软链会随仓库更新；不能软链的机器（例如 WSL 上没有这个仓库）就整目录复制，更新后重新复制。
- 装好后新开的会话才会加载；已在跑的 router/worker 不受影响，需要的话把 SPEC_SNIPPET 手动贴进 spec。
- `progress` 命令本身要先装好（`~/.progress/bin/progress`，见仓库根目录 README 的安装一节）。

## 已安装位置（11-04）

- Mac：`~/.claude/skills/`、`~/.codex/skills/` 下是指向本仓库 `skills/` 的软链，仓库更新即生效。
- WSL：`~/.claude/skills/`、`~/.codex/skills/` 下是复制件，改了 skill 要重新 `scp -r skills/progress-router skills/progress-worker wsl:.claude/skills/`（`.codex/skills/` 同理）。
- 命令入口：Mac 和 WSL 都是 `~/.progress/bin/progress`（Mac 由 `install_mac.sh` 生成）。
