---
name: progress-worker
description: worker（被 router 派来做一个节点的执行者）在开工、被卡住、交付时使用；必须先写 progress deliver 再给 router 发 worker_done，看板和巡检只读这份记录。
---

# progress-worker

你是 worker，被派来做一个节点。你的 spec 末尾通常有「本节点：主线 X，节点 Y」，下面命令里的 `<线>`、`<节点>` 就是它们。命令是 `progress`（没有就用 `~/.progress/bin/progress`），句柄自动取自环境变量，不要手填。`--summary` 必填，白话一句，不超过 120 字。

## 开工（可选）

```
progress heartbeat --line <线> --node <节点> --summary "开始做什么"
```
长任务可隔一段时间再发一条。

## 被卡住：写 block，然后停下等指示

```
progress block --line <线> --node <节点> --blocker dep|design|env|resource|human|other --summary "卡在哪"
```
收到继续的指示（GO、裁定、依赖到了）接着干时，**先发一条 `progress heartbeat`** 再干：节点才会从"卡住"回到"在跑"，不然看板和巡检一直把你算成卡住（11-07 EVB 分阶段等 GO 的节点干了三段，看板一直显示卡住）。分阶段等 GO 的，每进一个阶段都发一条。

blocker 含义：`dep` 等别的节点或别的线交东西（产物、清单、run_id、版本）；`design` 设计/规格要定，或等协调人、router 裁定、批准、GO、预算；`env` 环境、权限、工具坏了；`resource` 机器/配额不够；`human` 只有人本人能给的东西（真实端点、材料、目标或完成标准的决定），不要把"等核心一裁定""缺另一条线的清单"标成 human——human 会发到人的 QQ 上（11-07 EVB、NEWP 各有一次这样标错）；`other` 其它。

## 交付：先写 deliver，再发 worker_done（顺序不能反）

```
progress deliver --line <线> --node <节点> --outcome PASS|BLOCKED \
  --report <REPORT 路径> --head <git sha> --summary "做成了什么" \
  [--blocked-by "挡在哪"]
```
- 只有两种结果（11-07 起，工具拒收 PARTIAL、FAIL、UNKNOWN）：
  - `PASS`：做完了。实验做完但研究结果不达标，也是完整的负结果，记 `PASS` 加 `--result negative`。
  - `BLOCKED`：被挡住，加 `--blocker dep|env|human|budget|design` 和 `--blocked-by "还差什么"`。缺别的节点或文件的产物是 dep，等裁决是 design，环境坏了是 env，只有人本人能给的是 human，预算用完是 budget（`--blocked-by` 写剩下的工作量）。
- 做不完就接着做，不要先交一部分：预算没用完、剩下的验收条目也不靠别人的活，就做完再交；能自己做的缺口不算挡住。
- 要以"等裁定"为由报 `BLOCKED` 或写 block 之前，先重跑一次 `progress-inspect resume --line <线> --node <节点>`，看协调人和 router 在你开工后有没有已经裁了：已裁的按裁定接着做完，不算外部挡住（11-07 SR 两个 worker 09:17 开工，核心二 09:25 已裁类型，它们仍以"等裁定"提前交 PARTIAL，被打回重派）。
- `--report` 写报告文件路径，`--head` 写交付时的 `git rev-parse HEAD`。
- 返回 `"ok": true` 之后，才给 router 发 worker_done。
- 推送：`deliver` 写记录前会把你当前分支（给了 `--head` 时是该提交所在 worktree 的分支）推到 GitHub origin，只推已提交的内容，所以交付前先 `git commit`；main/master 永远不推，也不要自己推。输出里的 `push` 是推送结果：`"status": "pushed"` 表示已推上（记录里带推上的 sha）；出现 `push_failed` 就看原因（被排除规则拦下、rebase 冲突、网络等），修好后重跑同一条 deliver，修不了就在交付消息里写明，不要原样反复重试。

## 命令失败怎么办

- 返回 `"queued": true`：已进本地队列（`~/.progress/outbox.jsonl`），下次成功调用时自动补发。视为成功，**不要循环重试**。
- 返回 `"ok": false`：看 `code`，改参数重写**一次**。`not_worker` 表示你的句柄不是 router 登记的 worker：不要硬试，在 worker_done 里写明，让 router 用 `--role router` 代写。
- 仍失败：在发给 router 的交付消息里写明"progress deliver 失败：<code>"，然后照常发 worker_done。
