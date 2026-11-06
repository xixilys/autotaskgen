# 夜间整理（dreaming）

每天 04:00（本机时间，launchd `com.example.progress-dream`）跑一次 `dream.py`，把最近一天的事件、报告和 agent 会话
整理成一个带 git 版本的小记忆库，让下一个 agent 少踩同样的坑。

先试跑（不调模型、不写任何文件）：

    python3 ~/.progress/bin/dream.py --project dse --dry-run

正式命令：`dream.py --project dse [--dry-run] [--since ISO] [--line L]`。时间窗默认是上次成功运行至今，没有记录时取
24 小时，最多回看 72 小时。同一项目同时只会跑一个（锁文件 `~/.progress/<p>/dream.lock`）。

## 读什么

- 事件日志（和巡检同一条读取路径，Mac 上走 ssh 从 WSL 取）。
- 窗口内 node_deliver 引用的 REPORT.md（每份前 6000 字；WSL 绝对路径走 ssh）。
- 窗口内的巡检员报告和待跟进台账。
- 本机与 WSL 的 Codex、Claude 会话记录（`~/.codex/sessions`、`~/.claude/projects`），只取工作目录含
  `dse-harness` / `dse_harness` / `autotaskgen` 的会话。WSL 那边把一小段提取脚本通过 ssh 标准输入送过去执行，只
  回传摘要，原始文件不过网络。
- 摘要由脚本生成（无模型）：用户消息、助手文字、工具名加参数（≤200 字）、只留出错的工具输出（≤300 字）、压缩标记、
  最终回复；密钥类内容（sk-、ghp_、xox、Bearer、password=、≥32 位的 base64/十六进制）在提取端就打码。每个会话
  最多 40k 字（头 10k + 尾 30k），每条主线每晚会话最多 200k 字、交付报告最多 100k 字（都是新的优先，被砍掉的会话记在运行记录里）。相对路径的报告先到该节点的 worktree 里找，找不到再到 Mac 仓库找。
  会话按工作目录对应节点 worktree，或文本里的终端句柄对应 worker / router，归到主线，对不上的归 `unassigned`。

## 保留集排除（硬性要求）

项目有一份冻结的保留集，只放在 `~/.dse_holdout/`（私有目录，不在任何仓库里）。它的任何内容都不能进入记忆库、
LESSONS、建议或任何发给模型的提示词：

1. 提取脚本从不读 `~/.dse_holdout/`，凡是引用了该路径的会话整个丢弃，并计入统计 `excluded_holdout`。
2. 运行时用该目录里的文件名和文本（长度 ≥ 4 的名字 / id / 行）在本机生成禁用词表；任何包含禁用词的摘要行都丢弃。
   发往 WSL 的只有 sha256 哈希（小写词按词元/词组哈希匹配），明文词不出本机，也不打印、不写日志。
3. 提示词组装、模型输出、写入记忆库之前各再过滤一遍同一份词表。

本文档不列任何具体名字。

## 写什么

只写自己的记忆库 `~/.progress/<p>/memory/`（git 仓库，缺了会自动初始化）：

- `<line>/LESSONS.md`：每条规则一行，带证据引用和日期，≤ 120 行；被新证据推翻的标 `已过期`，7 天后删除。
- `<line>/STATE_SUGGESTIONS.md`：每晚整份替换，列出该线 STATE.md 里看起来过时或与事件日志矛盾的地方（附证据）。
- `GLOBAL.md`：出现在 ≥ 2 条主线的规则。
- `METRICS.md`：每周一追加一张表（教训之后仍重复的失败、接续耗时、复核不通过率、复核/交付比），纯脚本计算。
- `runs/<日期>.json`：模型原始输出和摘要统计。

绝不修改任何 STATE.md，也不动 dse-harness 仓库；只把建议发给协调人。写入成功后把记忆库 rsync 到 WSL 的
`~/.progress/<p>/memory/`（不含 `.git`）供 WSL 上的 worker 读取。

## 模型

- 提取：脚本，无模型。
- 每条主线阅读：Claude Sonnet（`claude -p --model sonnet --json-schema …`，环境变量 `PROGRESS_DREAM_READ_CMD` 可替换）。
- 合并：Claude Opus（`PROGRESS_DREAM_MERGE_CMD`）。合并在记忆库的临时克隆里做；结果校验（markdown、≤ 120 行、
  每条规则有证据和日期）不过就保留旧文件。提交后只有记忆库 HEAD 仍等于克隆时的 HEAD 才快进落地（代码里检查，不靠模型）；
  HEAD 变了就重新克隆重做一次，再不行就放弃并在 stderr 记错。
- 超时 600 秒，重试一次。

## 通知

有新经验或 STATE 建议时，给该线所属核心的 coordinator 发一条 `[每晚整理] …`，只在对方屏幕停在输入提示符（❯/›）时发，
否则留到下次运行重试。每次运行写一条 `observe` 事件（refs.key `dream:<日期>`）。

## 回滚

记忆库就是 git 仓库：

    cd ~/.progress/<p>/memory
    git log --oneline          # 每晚一个 "dream <日期>" 提交
    git revert <sha>           # 撤销某一晚
    git reset --hard <sha>     # 或整体回到某个版本

停用：`launchctl bootout gui/$(id -u)/com.example.progress-dream`。launchd 读不了 /Volumes/remote，所以脚本由
`install_mac.sh` 复制到 `~/.progress/bin`。
