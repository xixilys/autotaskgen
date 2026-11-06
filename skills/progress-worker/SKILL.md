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
blocker 含义：`dep` 依赖没好；`design` 设计/规格有问题要人定；`env` 环境、权限、工具坏了；`resource` 机器/配额不够；`human` 只有人能给的东西（授权、端点、材料、目标决定），等人的一律用它，不要标成 dep；`other` 其它。

## 交付：先写 deliver，再发 worker_done（顺序不能反）

```
progress deliver --line <线> --node <节点> --outcome PASS|PARTIAL|FAIL|UNKNOWN \
  --report <REPORT 路径> --head <git sha> --summary "做成了什么" \
  [--blocked-by "挡在哪"]
```
- `PASS` 全部达标；`PARTIAL` 部分完成，说明缺什么；`FAIL` 没做成；`UNKNOWN` 无法判断。
- 不提前收工：预算没用完、剩下的验收条目也不靠别人的活、待定裁决或环境时，接着做完再交。只有剩下每一条都被外部挡住才交 `PARTIAL`，报告里逐条写挡在哪；能自己做的缺口不算挡住。`deliver` 会拒绝预算用不到三分之一就交的 PARTIAL/FAIL，除非加 `--blocked-by "挡在哪"` 写明外部挡住的是什么。
- `--report` 写报告文件路径，`--head` 写交付时的 `git rev-parse HEAD`。
- 返回 `"ok": true` 之后，才给 router 发 worker_done。
- 推送：`deliver` 写记录前会把你当前分支（给了 `--head` 时是该提交所在 worktree 的分支）推到 GitHub origin，只推已提交的内容，所以交付前先 `git commit`；main/master 永远不推，也不要自己推。输出里的 `push` 是推送结果：`"status": "pushed"` 表示已推上（记录里带推上的 sha）；出现 `push_failed` 就看原因（被排除规则拦下、rebase 冲突、网络等），修好后重跑同一条 deliver，修不了就在交付消息里写明，不要原样反复重试。

## 命令失败怎么办

- 返回 `"queued": true`：已进本地队列（`~/.progress/outbox.jsonl`），下次成功调用时自动补发。视为成功，**不要循环重试**。
- 返回 `"ok": false`：看 `code`，改参数重写**一次**。`not_worker` 表示你的句柄不是 router 登记的 worker：不要硬试，在 worker_done 里写明，让 router 用 `--role router` 代写。
- 仍失败：在发给 router 的交付消息里写明"progress deliver 失败：<code>"，然后照常发 worker_done。
