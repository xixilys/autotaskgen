# 巡检员（terra）改法 spec（2026-11-05）

要改的主要是 `autotaskgen/supervisor.py`；§E 列了 supervisor.py 之外的相关问题，改不改由 autotaskgen 会话决定。以下函数名、字段名都是建议，可以换名，但行为要照写。

三个目标，对应 brief 里的三个缺口：
1. 地图里有目标、关键路径、各线 STATE/BRIEF 摘要，terra 不用再从最近 5 条事件去猜项目在干什么（§A）；
2. 每条建议都带收件人、落实标准和回查时间，由脚本记成台账，到期回查，没落实就升级（§B、§C）；
3. 硬指标由脚本先算好并标出超标项，terra 必须逐项判断，不能跳过（§A4、§D）。

---

## A. 地图（`build_map`）

新地图按下面顺序排。前五段是新加的，也是不能被截掉的部分；后面几段沿用现在的内容。

```
# 项目地图（dse，生成于 …）
## 1 最终目标与关键路径          ← 新
## 2 卡点                       ← 新
## 3 硬指标（⚠ = 超标）          ← 新
## 4 待回查建议                  ← 新（替代"上一份报告"的大部分作用）
## 5 各线背景（BRIEF / STATE 摘要）← 新
## 6 主机资源
## 7 各条线（节点、最近事件、router 屏幕）
## 8 在跑 worker 屏幕
## 9 上次巡检以来脚本做过的动作
## 10 上一份报告摘要（只留 summary 和 blockers）
```

### A1. 最终目标与关键路径（新函数 `milestone_frontier(project, state)`）

- 最终目标 `final_goal.text` 和 `done_when` 原样给出。`done_when` 如果不能核对（现在写的是"由两位核心协调人在关键节点里写明"），加一行"⚠ 完成标准未写成可核对的条件"。
- 里程碑逐行列出：`id [状态] 标题 | 前置 | 线 | 前沿?`。**前沿**指自身没确认、`after` 里全部已确认的里程碑。
- 每个前沿里程碑下面，列出挂了它的在跑、就绪、卡住节点（`node.milestones` 含它），再列出它所在各线里**没挂任何里程碑**的活跃节点，标"未挂里程碑"，让 terra 判断归属。
- 可选：project.json 新增 `cross_links: [{from: "线:节点或里程碑", to: "里程碑", note}]`，用来写图上没连、实际上相连的跨线依赖（由协调人维护；不填就不显示）。例：`{from: "EVB:evb-frontend-debug-native-binding-continuation-1105", to: "F5", note: "F5 的真实后端入口"}`。

### A2. 卡点（新函数 `blocker_table(project, state, now)`）

列出所有 `blocked` 节点，以及前沿里程碑上所有不在跑的节点：

```
- FE/fe-complete-real-evaluation-loop-1105 → F5 | blocker=dep | 自 11-04 21:08Z，13.4h | 推断：等人（命中"人类/授权/端点"） | 摘要：…
```

- 卡住多久：从历史里第一条 `node_block` 起算；一直没被 block 的从 `planned_at` 起算。
- 推断等人：`blocker == "human"`；或者 `summary` 或最后一条 block 历史命中 `人类|授权|端点|首回复|材料|endpoint|批准` → 标"推断：等人"。如果这时 `blocker != human`，再加一个"⚠ blocker 可能标错"。
- 派发失败：节点最近一条 `auto_dispatch` 之后，紧跟着出现 `node_block blocker=env`（或 summary 里有"代派失败"）→ 标"⚠ 派发失败"，附 summary 原文前 160 字。

### A3. 各线背景（新函数 `line_docs(project, line, runner, st)`）

- 路径：`patrol.repo_path(project, line)` 拼上 `line.state_file`。BRIEF 默认取 `state_file` 同目录的 `BRIEF.md`，可以用 `line.brief_file` 覆盖。**注意**：现在各线 STATE 只在 Mac 的 `PROGRESS_REPO`（`dse-rsi` 工作区）里是最新的，WSL 主树里没有。读取复用 `progress_inspect.Inspector.file` 的逻辑（本地路径直接读，`/home/…` 走 ssh），不另写一套。
- 摘要是机械摘取，不调用模型：
  - STATE（每线 ≤1800 字）：标题下第一段 ≤300 字 + 标题含"目标"的节 ≤500 字 + 标题含 `当前|下一步|阻塞|卡|待|窗口` 的节，**从文件末尾往前取**，合计 ≤1000 字。末尾优先，是因为 SR、FE 的 STATE 都是在后面追加"当前立即执行""覆盖上方"一类的节；
  - BRIEF（每线 ≤600 字）：标题含"要交付|目标"的第一节；
  - 每份文件都附上：绝对路径、修改时间、sha1 前 8 位、行数。terra 要读全文，用 `progress-inspect file <绝对路径>`。
- 缓存：`st["docs"][路径] = {sha, mtime, digest}`，sha 没变就复用摘要。
- 过期：STATE 修改时间比该线最后一条 router 事件早 6 小时以上 → 在 §3 硬指标里标 `stale_state`。

### A4. 硬指标（新函数 `hard_metrics(project, state, events, live, now)`）

在 `live.metrics[线]` 现有的三项上，加 §D 表里的派生指标，每线一行，超标的前面加 ⚠，并写出阈值：

```
- EVB ⚠审查/执行 0.355（阈 0.20/0.35）| 关键路径停滞 0 分 | 就绪未派 0 | 6h 已收交付 28（PASS15/PARTIAL13）| 等人 0 | STATE 3h 前
- FE  ⚠关键路径停滞 490 分（全是等人：推断，不算停滞）| ⚠等人 13.4h（阈 2/8/24）| ⚠审查/执行 0.31 | 6h 已收 8
```

关键路径上的节点全是等人的，停滞不另报 `stalled`，只算进"等人"。

### A5. 待回查建议（从 §C 台账生成）

```
- [fu_3a9c1e07] 发给 router EVB 于 17:42 | 第 1 次回查 | 到期 18:42 | 内容：确认两名被停轮 worker 是否恢复 | 落实标准：两个节点出现新的 node_heartbeat 或 node_deliver | 自动核对：未命中
```

只列已到期或本轮要判的。已自动核对通过的，列一行"已自动确认"，再列 24 小时内过期关闭的（各一行）。

### A6. 截断

`MAP_MAX` 从 40000 调到 60000。先看一下现在的地图实际有多大：`pulse` 返回的 `info["map_chars"]` 现在没写进报告，请写进 `report["map_chars"]`。超长时按这个顺序缩，前一步缩完还超才做下一步：

1. worker 屏幕末尾 12 行 → 6 行 → 3 行；
2. router 屏幕同上；
3. §7 去掉已结束节点；
4. 每线 STATE 摘要 1800 → 1000 字，BRIEF 去掉；
5. 最近事件每线 5 → 3 条。

§1–§4 不截。

---

## B. 报告格式 v2（`REPORT_SCHEMA`、`validate_report`）

`SCHEMA_ID = "progress-supervisor/v2"`。codex 的 `--output-schema` 走严格模式，所以所有字段都列进 required，可选的写成 `["string","null"]`。

```jsonc
{
  "summary": "string",                       // 白话 ≤6 句，先说离最终目标最近的阻塞
  "critical_path": "string",                 // 一段：前沿里程碑各卡在哪
  "blockers": [{                             // 全项目的卡点，按影响排序
    "item": "string",                        // 节点或里程碑
    "line": "string",
    "class": "human|dep|no_next_step|env|resource",
    "since_hours": "number|null",
    "who_unblocks": "string",                // 人类 / 协调人X / 线Y的节点Z / tooling
    "evidence": "string"
  }],
  "lines": [{
    "line": "string",
    "status": "ok|attention|problem",
    "progress_note": "string",
    "milestone_position": "string",          // 这条线在推进哪个里程碑、到哪一步
    "issues": [{"kind": "<KINDS>", "evidence": "string"}],
    "actions": [{                            // 替代 v1 的 suggestions
      "to": "router|coordinator|human|tooling",
      "text": "string",
      "done_when": "string",
      "check_after_min": "integer",          // 30–360，脚本会夹在这个范围里
      "verify": {                            // 能用一个事件核对的就填，不能就全填 null
        "line": "string|null", "node": "string|null", "event_kind": "string|null"
      }
    }]
  }],
  "followups": [{"id": "string", "verdict": "done|not_done|obsolete|cannot_tell", "evidence": "string"}],
  "metric_findings": [{"line": "string", "metric": "string", "value": "string",
                       "verdict": "real|explained", "evidence": "string"}],
  "goal_drafts": [{"topic": "string", "current": "string", "proposal": "string", "why": "string"}]
}
```

`KINDS` 加这些：`waiting_human`、`no_next_step`、`dispatch_broken`、`mislabeled`、`metric_breach`、`followup_overdue`、`review_heavy`、`stale_state`、`graph_gap`。原来的 `stalled`、`looping`、`hallucinating`、`drifting`、`idle_parallelism`、`no_progress`、`other` 保留。

`validate_report` 要做的事：
- `lines[].suggestions` 继续生成，取 `[a.text for a in actions]`，给看板和旧代码用（看板不用改就能显示）；
- 每线 actions 最多 3 条；`check_after_min` 夹到 [30, 360]；`to` 不在枚举里就判整份报告不合法；
- `followups[].id` 必须在本轮的待回查列表里，否则丢掉这条，并记一条 warning；
- 地图里每个 ⚠ 指标，`metric_findings` 都得有对应的一项。缺了不判报告不合法，但要写 `report["missing_metric_verdicts"]`，下一轮在地图 §3 里标"上轮未判"；
- `other` 的问题如果超过该线问题数的一半，记 warning（只记，不拒）。

---

## C. 回查台账与升级（新加在 supervisor.py 里，状态放在 `st["followups"]`）

### C1. 登记
- 每条 action 的 id = `"fu_" + sha1(line|to|规范化的 text)[:8]`。如果同一个 id 已经在台账里、还没关，就不新建，也不重发（这样 terra 换个说法重复提，也只算一条）。
- 登记时机：`to=router/coordinator/tooling` 的，**真发出去了**才登记（`delivery.sent=True`）；被限流或演练没发出的，不登记，下轮 terra 提了再说。`to=human` 的直接登记（走 §C4）。
- 台账字段：`{id, line, to, target, text, done_when, verify, created_at, sent_at, check_at, tries, cannot_tell, history:[…]}`；`check_at = sent_at + check_after_min`。

### C2. 自动核对（调用 terra 之前做）
- 有 `verify.event_kind` 的：在 `sent_at` 之后找 `line`/`node`/`kind` 都对上的事件，找到就关闭，记 `auto_done`，附上事件 id。
- 超过 24 小时还没关的：关闭为 `expired`，在地图 §4 列一次，之后不再出现。

### C3. 按 terra 的判定处理

| verdict | 动作 |
|---|---|
| done / obsolete | 关闭，记证据 |
| cannot_tell | 下轮再判；连续两次 cannot_tell 当 not_done 处理 |
| not_done，第 1 次 | 原收件人重发一次，前缀"[巡检员·回查]"，不受"内容没变不重发"的限制，但仍遵守同一收件人 1 小时 1 条；`tries=1`，`check_at = now + check_after_min` |
| not_done，第 2 次 | **升级一级**，见下表；`tries=2` |
| not_done，第 3 次及以后 | 不再发消息，只放进人类摘要（§C4），在看板上标红 |

升级到哪：

| 原收件人 | 升级给 | 消息 |
|---|---|---|
| router | 该线所属核心的协调人（`live.coordinators[core].handle`；不在线就进人类摘要） | "[巡检员·升级] X 线 router 两次没落实：<text>；落实标准：<done_when>；证据：<两次 evidence>" |
| coordinator | 属于 §5 第三档（目标、预算、对外、留出集、只有人有的东西）的，进人类摘要；其他的，回给协调人，建议开 decide 节点交 Opus+Astra 裁决 | |
| tooling | 人类摘要（附 autotaskgen 会话句柄，env `PROGRESS_TOOLING_HANDLE`，没设就不附） | |

- 第一次发给 tooling：如果设了 `PROGRESS_TOOLING_HANDLE`，就直接发给那个终端；没设就只记一条 `observe` 事件。
- 每轮最多升级 3 条，多出来的顺延到下轮。
- 升级和回查各记一条事件：`kind=nudge`，`reason=supervisor_followup` 或 `supervisor_escalate`，`refs={followup: id, target}`。

### C4. 人类摘要（`to=human` 的 action、升级上来的条目、`goal_drafts`）
- 不发给任何 router。每轮汇成一条，用 `decide.send_qq`（Hermes→QQ）推送。FRAMEWORK §6 已经定了，第三档事项直接推 QQ。
- 什么时候推：等人的条目，等待时长每跨过 **2h、8h、24h** 推一次，之后每 24 小时推一次；`goal_drafts` 第一次出现时推一次，内容没变就不再推。QQ 每 2 小时最多一条。
- 格式（决策卡片的简版）：每条一段，"要你给什么 / 为什么现在 / 不给会怎样（按目标契约的默认处理；没有契约就写'该分支继续挂起'）/ 证据路径"，术语当场解释。
- 推送结果写进 `report["human_digest"] = {sent, items, reason}`，看板 supervisor 页单独一栏显示。

### C5. `deliver()` 的改法
- 按 `to` 分组：router 组沿用现在的逻辑（router 不在线转给协调人）；coordinator 组发给 `live.coordinators[core].handle`，同一协调人每 2 小时最多一条，合并发送；tooling 组见上；human 组走 §C4。
- 原来"内容没变不重发"的 digest 判断，改成以台账为准：已登记、没关的 id 不重发，回查和升级按 §C3 发。

---

## D. 指标和阈值

脚本算好写进地图 §3；超过"关注"阈值的标 ⚠；超过"问题"阈值的，terra 判 real 时该线至少是 problem。

| 指标 | 怎么算 | 关注 | 问题 | 现在的值（11-05 18:35） | 默认收件人 |
|---|---|---|---|---|---|
| 就绪未派 | `live.metrics.ready_undispatched`；任何节点就绪超过 30 分钟 | node_hours > 0.25 | > 1 | 四线都是 0 | tooling（巡检会自动派，还剩下就说明派发坏了） |
| 派发失败 | `auto_dispatch` 后跟 `node_block env`，或 summary 有"代派失败" | 出现 1 次 | 30 分钟内没恢复 | SR `dot_source_increment_old_fields_1105` | tooling |
| 关键路径停滞 | `critical_path.stall_min`；全是等人的节点不算 | > 60 分 | > 180 分 | FE 490（等人，不算） | router；第二次升级给协调人 |
| 等人时长 | blocked 节点里推断为等人的，按 A2 的算法 | 2h | 8h；24h 再推一次 | F5 13.4h，F3 11.5h（F3 实际是等 dot 补证、由人类转交） | human 摘要 |
| 审查/执行 | `live.metrics.review_run.ratio` | > 0.20 | > 0.35 | EVB 0.355，FE 0.31，SR 0.329 | router（只有里程碑申报才排 check） |
| 6h 已收交付 | 6 小时内 `node_review` 为 PASS 或 PARTIAL 的数量（FRAMEWORK §7 "每小时新增的已验收发现"） | 线开着、有不在卡的节点，6h 为 0 | 12h 为 0 | EVB 28，FE 8，SR 13，NEWP 12 | router；NEWP 这类"没想好下一步"的给协调人 |
| 空线 | 线开着，没有就绪、在跑、已交的节点，也没有在等人的节点 | 1h | 4h | NEWP（第一轮做完，等协调人给范围） | coordinator |
| 打回次数 | 同一节点 FAIL 的审查数；或同一个词干（去掉 `-repair/-resume/-recheck/-review` 等后缀后）被 supersede 的链长 | ≥ 2 | ≥ 3 | 最多 1 | router；problem 时给协调人 |
| STATE 过期 | STATE 修改时间早于该线最后一条 router 事件 | 6h | 24h | 四线都在 1h 内 | router |
| 未挂里程碑 | 活跃节点里 `milestones` 为空的比例 | > 50% | – | EVB 4/6，SR 2/2，FE 0/1 | router（每天最多提一次，优先级低） |
| 回查超期 | 台账里 not_done 的条数 | ≥ 1 | 有条目升级过 | 还没有台账 | 按 §C3 |

阈值集中放在 `supervisor.py` 顶部的 `THRESH = {...}`，可以用 `project.json.defaults.supervisor_thresholds` 覆盖。

---

## E. supervisor.py 之外的相关问题（自己判断改不改）

1. **`progress_core.BLOCKERS` 加 `human`**，`progress-router` skill 写明：要人给东西的用 `--blocker human`。改了以后 A2 的文字推断只当兜底。现在 F5、F3 都被标成 `dep`。
2. **SR 补证节点派发失败**：`worker-start 失败 rc=1 … This terminal is attested as term_f35f375e…`（18:33）。看起来是代派时用的身份是核心二协调人的终端，被 orca 拒了。SR router 按规则不手派，这一环没人接。
3. **`live.router_tree` 把上级写错了**：FE、SR 的 parent 写成了核心一协调人 `term_ac3ac606`，project.json 里它们属于 core2（`term_f35f375e`），edge 是 `worktree`，可能是按工作区推出来的。如果 `deliver()` 转发走的是 `ln.core`，现在不受影响；但看板和以后用 router_tree 的地方会错。
4. **FE、SR、NEWP 节点几乎都不挂里程碑**（4/89、4/91、0/12）。`progress-router` skill 可以要求 run/plan 节点必填 `milestones`，闸门对没挂的给 warning。不然 A1 只能靠 terra 去猜。
5. `report["map_chars"]` 写进报告（见 A6）。

---

## F. 提示词全文（替换 `PROMPT`）

占位符还是 `{map}`、`{map_text}`；正文里没有其他花括号。

```text
你是 dse 项目的巡检员。你不干活，只做三件事：判断项目离最终目标还差什么、现在谁在卡、上次的建议有没有落实；然后写一份结构化报告，由脚本发给该收的人。

下面是项目地图（已全文给出；副本在 {map}）。

{map_text}

----（地图结束）----

一、怎么查
1. 先读地图第 1–4 段（最终目标与关键路径、卡点、硬指标、待回查建议），再看第 5 段各线背景和第 7 段各线节点。
2. 地图给不出答案时，用 progress-inspect 抽查：
   progress-inspect screen <term_… 或 ctx_…> | events --line X [--node Y] [--since ISO] | node --line X --node Y |
   file <绝对路径> | git-log <worktree> [-n N] | tasks --run R
   各线 STATE、BRIEF 摘要不够用时，用 file 读地图里给的绝对路径。
3. 只读：不改文件、不提交、不发消息、不启停或派发 agent，所有动作由脚本执行。检查只用 progress-inspect。
4. 证据写你实际看到的东西：事件时间或 id、屏幕原文、提交号、"路径:行"。没查到就写"未查到"，不下结论。

二、每轮必须回答的四件事
A. 关键路径：前沿里程碑各卡在哪。把每个卡点归为一类，写进 blockers：
   human = 只有人能给（授权、端点、材料、目标决定）；
   dep = 等别的节点或别的线交付，而且那边在推进；
   no_next_step = 没人想好下一步，或者在等协调人给下一轮范围；
   env = 派发、终端、机器故障；
   resource = 测量窗口、内存、磁盘。
   地图里的 blocker 字段可能标错（例如等人被标成 dep），以你核实的为准，标错的记一条 mislabeled。
   图上没连、实际相连的跨线依赖（例如某个前端里程碑实际要用后端某个节点的产物），记 graph_gap，写进 goal_drafts。
B. 回查：第 4 段每一条都要给 verdict（done / not_done / obsolete / cannot_tell）并附证据。看 done_when 写的事实有没有出现，不要因为对方回了消息就判 done。
C. 硬指标：第 3 段每个 ⚠ 项判 real（真问题）或 explained（有正当原因；写清原因和证据），写进 metric_findings。real 的必须出现在该线 issues 里，并配一条 action。
D. 偏离：各线最近的工作有没有在推进它挂的里程碑；第 1 段里"未挂里程碑"的活跃节点，判断它在为哪个里程碑干活，或者判为 drifting。

三、判状态
- ok：关键节点在推进，或者在合理地等（依赖方在跑；等的人已经被提醒、还没超阈值）；没有 real 指标。
- attention：有 real 指标、或有 not_done 的回查，但都在"问题"阈值以内；或者 router 有一件具体的事要做。
- problem：关键路径停滞超过问题阈值、派发坏了、同一条建议第二次 not_done、原地打转、声称完成但证据对不上。
只是"还没做完"不算 attention。一条线合理地在等人时判 ok，等人这件事写进 blockers，不要每轮对 router 重复"保持阻塞条件"之类的话。

四、写 action
每条 action 指定一个收件人 to：
- router：本线范围内的事：派发、拆分、实现取舍、停掉失败的候选、更新 STATE、补挂里程碑。
- coordinator：方法选择、跨线接口、会改变结论或评分含义的事、需要两家模型裁决的技术取舍（建议协调人开 decide 节点）、某条线下一轮做什么。
- human：只限这几类：目标或完成标准；花钱或算力超预算；对外发布或调用外部服务；读留出集；只有人有的数据、授权或联系渠道（例如真实端点和调用授权）。写清"要人给什么、不给会怎样"。
- tooling：巡检、派发、看板脚本本身的问题：派发失败、指标算错、地图缺字段、事件标错类型。
每条 action 都要写 done_when：一个下次能从事件、节点状态、STATE 或屏幕核对的事实，例如"节点 X 出现 node_dispatch 事件"。能用一个事件核对的，填 verify（line、node、event_kind）；不能的，三个字段都填 null。check_after_min 写多久之后回查（30–360 分钟）。
每条线最多 3 条 action，按对关键路径的影响排序。第 4 段里已有、还没到期的，不要重复提。没有 real 问题的线，actions 留空。

五、目标草案
如果你认为最终目标、完成标准或里程碑结构本身有缺口，写进 goal_drafts（现状、建议、理由）。这只给人看，不会自动生效，也不要改写成给 router 的 action。

六、issues.kind 取值
stalled（该动没动）/ looping（原地打转、反复打回）/ hallucinating（声称的结果和证据对不上）/ drifting（偏离里程碑）/ idle_parallelism（有独立的活没派）/ no_progress（长时间没有已验收的产出）/ waiting_human / no_next_step / dispatch_broken / mislabeled / metric_breach / followup_overdue / review_heavy / stale_state / graph_gap / other。尽量不用 other；用 other 时在 evidence 开头写明为什么其他类都不合适。

七、输出
只输出符合 schema 的 JSON。summary 用一段白话，不超过 6 句：先说离最终目标最近的阻塞是什么、在等谁，再说其他的。critical_path 用一段话把前沿里程碑各卡在哪串起来。
```

---

## G. 测试（加进 `tests/test_supervisor.py`）

1. `build_map`：有 §1–§5 五段；在 60000 字上限下，§1–§4 不被截；STATE 摘要的末尾节优先（构造一个 STATE，前面有旧的"当前"节、后面有新的"当前立即执行"节，摘要里必须有后者）；STATE 文件不存在时写"读不到"，不报错。
2. `milestone_frontier`：构造 B4 已确认、B5 未确认、F2 已确认、F5 未确认 → 前沿是 {B5, F5, …}；未挂里程碑的活跃节点出现在前沿下。
3. `blocker_table`：`blocker=dep` 但 summary 含"授权" → 标推断等人和可能标错；`auto_dispatch` 后紧跟 `node_block env` → 标派发失败。
4. 台账：同一个 id 不重复登记；`verify` 命中事件就自动关闭；not_done 第 1 次回查发给原收件人、第 2 次升级给协调人、第 3 次只进人类摘要；24h 关闭为 expired；cannot_tell 连续两次按 not_done 处理。
5. `validate_report`：v2 正常通过；`to` 非法时拒绝；`followups` 里出现不认识的 id 时丢弃并记 warning；从 actions 生成 `suggestions`；缺 ⚠ 指标判定时写 `missing_metric_verdicts`。
6. 人类摘要：等待跨过 2h/8h/24h 各推一次，不跨不推；2 小时内不推第二条；`decide.send_qq` 用替身。
7. 回归：`PROGRESS_SUPERVISOR_CMD` 替身输出 v2 报告时，`pulse` 全流程正常；有违规命令时，所有消息（包括升级和 QQ）都被压住。

## H. 上线顺序（建议）

1. 先上 §A 和 §D（只改地图，terra 的输出仍用 v1 格式，看判断质量能不能提高）；
2. 再上 §B 和 §C 的登记、自动核对、回查，升级只做到协调人；
3. 最后接人类摘要（QQ）。第一周看推送条数，太吵就把 2h 那一档调到 4h。

每一步上线后，在下一份报告里核对：attention 占比、other 占比、回查关闭率（done ÷ 已到期），以及人类摘要条数。
