---
name: progress-router
description: router（主线协调、子 router）在排节点、派 worker、审查交付、取代/取消节点、申报关键节点时使用；负责把每一步写进 progress 事件，看板和巡检只读这份记录。
---

# progress-router

你是 router。看板和巡检只认 `progress` 事件，不写就等于没发生。命令是 `progress`（没有就用 `~/.progress/bin/progress`）。句柄、worktree 自动从环境变量取，不要手填。
巡检员每 30 分钟会查看你和你的 worker，收到它的建议按建议处理。
`--summary` 必填，白话一句，不超过 120 字。命令返回 `"ok": false` 时按 `code` 改参数重写，不要跳过；返回 `"queued": true` 表示已进本地队列，会自动补发，不用重试。

- **等人的节点一律标 `--blocker human`**：只有人能给的东西（授权、真实端点、材料、目标或完成标准的决定）用 `progress block --blocker human`，不要标成 `dep`；巡检按这个字段统计等人时长，并把它列给人类。**等协调人或 router 的裁定、批准、GO（"新 source 计划获批后"、"精确 GO"、预算、算力、机器、调用额度）标 `design`，不标 `human`**：human 会被发到人的 QQ 上，标错了就是让人去回答协调人该答的事。

- **开工前、以及中断、换终端、上下文压缩后**，先运行 `progress-inspect resume --line <线>`（或读 `~/.progress/<项目>/resume/<线>.md`）：核对未完成节点、最近裁决和未落实跟进，经验里写了的规则照做，再接着排节点。收到 `[接续]` 消息也是这样做。

## 1. 排节点（plan）：每排一个节点就写一条

```
progress plan --line <线> --node <节点> --title "标题" --deps a,b \
  --spec <分支>@<规格路径> --agent codex --model <模型> --effort high --on wsl \
  --expected-minutes 60 --milestones M1 --summary "排了什么"
```
- `--deps` 写依赖节点（无依赖就省略）；依赖都交付后节点自动变 ready。
- 带 `--spec` 和 `--model` 的 ready 节点，巡检默认会代派；不想让巡检派就加 `--no-auto`。
- 返工、改派一律开新节点，不复用旧节点。
- **每个 run/plan 节点都要挂里程碑**（`--milestones`）：项目级巡检按"挂到完成标准条目的耗时"算投入，没挂的节点耗时会被记为可能没意义的工作（超过 25% 标 ⚠）；`progress plan` 对没挂里程碑的 run/plan 节点会回一条警告（不拒绝）。里程碑本身可带 `stage`（S1/S2/S3）和逐条 `criteria`（协调人维护），见 docs/PROJECT_REVIEW.md。
- `--kind run|review` 标明节点类型（不写时，节点 id 含 review/recheck/audit 的算 review，其余算 run）。两类耗时差很大：巡检没写 `--expected-minutes` 时，run 默认按 120 分钟、review 按 30 分钟算超时（项目可在 project.json 的 `defaults.expected_minutes` 里改）。
- **审查只在关键处排**：普通 run 节点不另排审查；关键结论才另排一个 review/check 节点：`--deps <run 节点> --kind check --expected-minutes <较短>`。审查的 worker 只读和判断，不重跑重活；需要重跑时写 FAIL/PARTIAL，由你另排新的 run 节点。

## 1.5 拆子图（propose）：一次排一组节点，标明串行/并行

要一次拆出多个节点（或开子 router）时，写一个 JSON 文件，用 `progress propose <文件>` 提交（先加 `--check` 只校验不写）。脚本闸门校验通过才写入；不通过会返回中文问题清单，按清单改完再交。通过了也要看 `warnings`：出现"已按 serial 处理"或"可以标 parallel"，说明写出来的和你想要的不一致，先改 JSON 再提交，不要提交后再 cancel 重提（11-06、11-07 共三次这样白建四个节点又全部取消）。
```json
{"line": "<线>", "parent": null, "summary": "怎么拆、为什么",
 "evidence": ["<报告路径 / 失败记录 / 提交号>"],
 "segments": [
  {"id": "fix", "mode": "parallel", "nodes": [
    {"id": "fix_a", "kind": "run", "title": "…", "deps": [], "write_scope": ["src/a"],
     "budget": {"minutes": 60}, "acceptance": ["test_a 通过"],
     "spec": "<分支>@<规格路径>", "route": {"agent": "codex", "model": "<模型>", "effort": "high", "on": "wsl"}}]},
  {"id": "merge", "mode": "serial", "nodes": [ … ]}]}
```
- `kind`：`run`（干活）、`plan`（子 router，要写 `budget.nodes` = 它的子树最多几个节点）、`check`（独立审查）、`decide`（要裁决）。`parent` 填你负责的 plan 节点 id，主线根就写 null。
- **技术取舍分三层**：①你自己范围内的实现取舍（换哪种写法、先做哪个、停掉失败的候选、派给谁）自己定，直接做，不开 decide；②超出你范围的（方法选择、跨线接口、会改变结论或评分含义的）交给上面的负责人（协调人）定；③负责人遇到关键结论、或原本要等用户的技术取舍，由负责人加 `kind=decide` 节点（spec：一行 `问题：…`、编号选项至少两个、证据位置、每个选项的代价），巡检请 Opus 和 Astra 独立判断：一致就执行并通知用户，不一致或碰到 FRAMEWORK §5 的触发条件才找用户。router 不自己开 decide 节点。用户事后推翻时巡检会发消息给你，请按消息调整（需要时 git revert）并 progress 记录。
- **段要标结构**：后一步要用前一步的结果、或共享同一份正在改的状态 → `serial`（一次只跑一个，最好一个 worker 连着做完）；输入独立、写域不重叠、能各自验收 → `parallel`。标了 parallel 但写域重叠或互相依赖的，闸门会自动改成 serial 并告诉你；**兄弟节点验收标准一字不差的直接拒收**——每个节点写只属于它的验收（它自己的产物、测试或报告），提交前 `--check` 看一眼。（11-07：SR、NEWP 各一次被改成串行后取消重排。）
- **先让线上有活在跑，再自己动手**：要自己做超过 10 分钟的事（冻结配置、写脚本、读长报告、起草接口）之前，先把待审的交付审完、就绪的节点派出去，这几步通常几分钟；自己动手的这段时间里线上要一直有节点在跑。能交给 worker 的长活，排成节点。（11-07：FE、EVB、NEWP 的 router 各自动手做 10–25 分钟的活，期间线上 0 个在跑，NEWP 另有 2 个交付等了 25 分钟没审。）
- **节点大小 30 分钟–2 小时**：一件能自己验收的事。太碎的合并，太大的拆开。
- **验收标准只写这个节点用现有输入能做到的**：要用别的还没交付的节点（在跑的通用工具、待冻结的清单、待裁定的口径）产物的那部分，另排一个后续节点、`deps` 指向那个上游，等它交付后自动 ready；不要把它写进本节点的验收，让节点注定交不了 PASS（11-07 起交付不收 PARTIAL）。审查时 PARTIAL 剩下的若只差某个上游，同样按这条接一个带 `deps` 的后续节点，不要原样重派。（11-06：一个半小时 13 个交付里 9 个 PARTIAL 是"等上游"造成的。）
- **多个节点共用一份刚交付的合同/接口时，先试接一个再扇出**：先派一个下游节点按合同实际接入（或把"被一个下游真实接入通过"写进上游的验收），接通了再并行派其余；试接暴露的缺口回上游补，不让所有兄弟节点各自撞上同一缺口。（11-06：G2 核心交付后同时派 7 个原语，6 个因同一份合同缺项以 PARTIAL 收尾。）
- 每个节点必须有验收标准（拆的时候写），有预算，run/plan 有写域且在父节点（或主线）写域内；预算合计不超父节点额度；单次最多 12 个，最多 8 层；必须引用依据。
- **默认不排 review/check 节点**：普通交付由节点自己跑验收标准、你看交付记录接受或打回。只有关键结论（里程碑、改方向、对外的数字）才排一个 `check`。
- 并行度是上限不是目标（project.json `max_parallel`，默认不设上限）：巡检只在真有独立的活时才叫你拆，并行位空着不会叫你。worker 交付时用 `--failure`（每个不同失败一条）、`--candidate`、`--touched` 报信号；巡检据此提示"可并行"或"并行塌缩"（同段兄弟撞同一失败、改同一处 → 请重拆或改串行）。

## 2. 派出（dispatch）：先核前置条件，派完立刻写

每份节点说明（spec）必须有一节「前置条件」，逐项列出 worker 要用的输入，每项带一个可机械核对的标记（11-07 起）：
`node:<节点>`（或 `node:<线>/<节点>`，该节点已交付 PASS 且没被审为 FAIL）、`sha:<提交>`、`file:<分支>@<路径>`（省掉分支就是 spec 所在分支）。
什么都不需要就写一项「无」。说明里写全前置条件、完整目标和验收，worker 不需要再去别处找上下文；预算按"一单做完"来估。

起 worker **之前**先跑 `progress precheck --line <线> --node <节点>`：缺哪一项它就记 `node_block blocker=dep` 并写明缺的是哪项，
这时不要派；那一项出现后巡检会自动派（节点有 spec 和 route 时），否则巡检提醒你"前置条件齐了"。`progress dispatch` 也会再核一次，
缺项时拒绝记派出、改记 block——已经起了的 worker 要停掉。

```
progress dispatch --line <线> --node <节点> --task <task_…> --dispatch <ctx_…> \
  --worktree <worktree id> --terminal <worker 句柄> --summary "派给谁、做什么"
```
`--task` 必须写：巡检靠它把 Orca 任务对回节点，任务结束没交付时才能催。`--terminal` 必须是真正的 worker 句柄，否则 worker 之后写不了 deliver。

**派 worker 时，把 progress-worker 的要点附进 spec**：把 `skills/progress-worker/SPEC_SNIPPET.md` 原文贴在 spec 末尾，并把 `{line}`、`{node}` 换成真实值（已带 `<!-- progress-worker -->` 标记的 spec 巡检不会重复附加）。

## 3. 审查（review）：收到交付后

```
progress review --line <线> --node <节点> --outcome PASS|PARTIAL|FAIL|UNKNOWN --summary "审查结论"
```
`review` 写记录前会把你当前目录所在分支推到 GitHub origin（这就是接受/合并结果的上传）；代写的 `deliver` 推 `--head` 所在 worktree 的分支。所以先 commit 再写 review；main/master 永远不推，也不要手推 main。输出里出现 `push_failed` 就按原因处理（排除规则拦下、rebase 冲突、网络），处理不了在汇报里写明，不要原样反复重试。
交付只有 `PASS`（可带 `--result negative`：实验做完、研究结果不达标）和 `BLOCKED`（带分类和还差什么）两种；`BLOCKED` 的节点回到"卡住"，按分类处理（dep 等上游、budget 补预算或拆单、design 去裁决）。
worker 没写 deliver 就自己做完了：你代写，加 `--role router`（同样只收 PASS / BLOCKED）：
```
progress deliver --role router --line <线> --node <节点> --outcome PASS --report <路径> --head <sha> --summary "代写：…"
```

## 4. 取代 / 取消

```
progress supersede --line <线> --node <旧节点> --by <新节点> --summary "为什么取代"
progress cancel --line <线> --node <节点> --summary "为什么取消"
```
supersede 前新节点要已 plan。

## 5. 申报关键节点（claim）

条件到了才写，附证据：
```
progress claim --line <线> --milestone <M1> --report <证据路径> --summary "条件已到的依据"
```
确认/驳回是核心协调人的事（`progress confirm|reject --milestone …`），你不写。

## 6. 巡检催你时

收到 `[巡检]` 消息，按内容补记录：`undelivered`（任务已结束没写交付）就核对后补 `progress deliver`；`unreviewed` 就审查并写 review；`idle` 就排下一个节点或写 line_close。每条主线每小时最多一条。

收到"可并行"：某个交付报出多个不同失败或多个独立候选，判断它们是否真的独立，独立就 `progress propose` 一段 parallel，不独立就串行处理。收到"并行塌缩"：该段已停派，重新拆分（写域不重叠）或改成 serial，重复的节点 supersede/cancel。交付且 Orca 派单已结束的 worker 终端由巡检自动释放（记 `worker_release`），router 和子 router 终端不会被关。

收到 `disk_low` / `disk_critical`（磁盘空间不足）告警：不要再起大量写盘的节点，也不要自己删除文件（清理由专人负责）；等空间恢复，或向协调人报告。

节点超过预计用时还没交付，读一次 worker 屏幕（`orca orchestration worker-read --dispatch <id> --source terminal --limit 40`）；停在输入框或有报错就按已停处理：产物已写好就直接收尾（deliver / review），否则重派或写 progress block。巡检现在会自动做这件事：worker 屏幕报错、停在输入框或进程退出满 10 分钟，会发 `worker_stalled` 催办（每个节点每次停摆只发一条，1 小时后仍未处理再提醒一次，不受每小时一条的限制）；超过预计用时 1.2 倍的 `overtime` 也是这样。看到"审查节点"的催办，说明是 review 超时，不用干等。

## 7. 子 router 规则（详见 docs/ROUTER_RULES.md）

- 新 router 必须放在父 router 所在 worktree 的**子 worktree**：`orca orchestration worker-start --worktree new-child …`，远程主机用 `orca worktree create --parent-worktree <父 worktree 选择器> …`。不要用 `terminal create` / `agent launch` / `terminal split`。
- 子 router 起来后自己 `run-create`，目标写 `<父主线 id>/<子名字>：<要做什么>`，并用 `progress dispatch` 记一条派出事件。长期存在的，在 project.json 该线加 `parent` 字段。
- router 是长期会话，**不要把自己登记成自己那个 run 的 worker**，否则 Orca 会不停报 "Worker stopped without reporting"。
