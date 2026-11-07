<!-- progress-worker -->
## 进度记录（必做，看板和巡检只读这份记录）
本节点：主线 `{line}`，节点 `{node}`。命令 `progress`（没有就用 `~/.progress/bin/progress`）。
- 开工前（以及中断、换终端、上下文压缩后）先运行 `progress-inspect resume --line {line} --node {node}` 读节点历史和本线经验；经验里写了的规则照做。
- 交付：**先**写 `progress deliver --line {line} --node {node} --outcome PASS|PARTIAL|FAIL|UNKNOWN --report <REPORT 路径> --head <git sha> --summary "一句白话"`，**再**给 router 发 worker_done；顺序不能反。
- `progress deliver` 会自动把本分支推到 GitHub origin（不推 main）：交付前先 `git commit`；输出里出现 `push_failed` 就按原因修好（或在交付消息里写明），不要原样反复重试。
- 交付时报并行信号（巡检据此叫 router 拆分或判定撞车）：`--failure "<错误类别或测试名>"`，写类别不写描述（如 `CG16_BINDING_MISSING`、`test_jacobi_single`，40 字以内），同一种失败在不同节点用同一个词，巡检才能归并；只写交付时仍没解决、还要有人动手的失败，每个不同的一条，已修好的不写；`--candidate "<候选>"`，只写还没做、彼此独立、能分给别人并行做的方向，每个一条，已经跑过的不写；`--touched <路径1,路径2>`，写实际改了的文件。没有就省略该项，`--touched` 只要改了文件就必须写。
- 不提前收工：预算没用完、剩下的验收条目也不靠别人的活、待定裁决或环境时，接着做完再交。只有剩下每一条都被外部挡住才交 PARTIAL，并在报告里逐条写挡在哪（缺谁的活／等哪个裁决／哪个环境）；能自己做的缺口不算挡住。deliver 会拒绝预算用不到三分之一就交的 PARTIAL/FAIL，除非加 `--blocked-by "挡在哪"` 写明外部挡住的是什么。
- 卡住：`progress block --line {line} --node {node} --blocker dep|design|env|resource|human|other --summary "卡在哪"`，然后停下等指示。等别的节点或别的线交东西标 dep；等协调人或 router 裁定、批准、GO、预算标 design；human 只用于只有人本人能给的东西（真实端点、材料、目标决定），它会发到人的 QQ 上。收到继续的指示（GO、裁定、依赖到了）接着干时，先发一条 `progress heartbeat`，节点才会从"卡住"回到"在跑"；分阶段等 GO 的，每进一个阶段都发一条。
- 遇到无法继续的报错，先写 progress block 再停，不要停在报错画面上。
- 开工可选：`progress heartbeat --line {line} --node {node} --summary "在做什么"`。
- summary 不超过 120 字，写做成了什么，不写内部字段名。
- 命令返回 "queued": true 表示已进本地队列，会自动补发，不用重试；返回 "ok": false 按 code 改参数重写一次，仍失败就在交付消息里写明。
