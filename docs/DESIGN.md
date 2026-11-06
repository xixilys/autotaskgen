# 全局进度看板与巡检框架 设计

2026-11-04。人类提出，Claude（研究负责人）整理。标"（我的决定）"的是整理人替人类做的选择，人类可改。

## 1. 为什么做

- 现在的 3101 看板字多、没效果，而且数据停在 11-03：首页数据由 Mac 的 `~/.local/share/flow_board/push_dispatch.py` 生成，它读 `~/.rsi/state/{task_graph,live_workers}.json`，这两份由已停的 `~/.rsi/bin/tick.py` 写。维护看板的 Luna 没找到生成器，交了 PARTIAL。
- 13 个 router 的 `docs/validation/router-*/graph.json` 格式各异（节点有字典有列表，状态字段 status/state 混用，值是 `delivered_k2_UNCLOSED_review_required` 这样的自由文本），脚本没法统一读。
- `orca orchestration worker-list` 只回最近 100 条，`--terminal-state active` 回 0 条，WSL 上实际有多个 worker 在跑。在跑状态不能只靠 orca 列表。（不带 `--run` 时列表还只限于调用终端绑定的那个 run，所以快照逐个 run 用 `--run <id>` 查，见 LIVE_SCHEMA.md。）
- 人类要：一份固定格式、由 router/worker 在派发和交付时维护的记录；一个直观、有设计感、能看出项目离最终目标多远的看板；一个低成本的定时巡检，替人盯着、催 router、在已排好的节点里自动派 worker，减少手动盯的工作。纯 agent 做科研不行，这层只做推进，不做科研判断。

## 2. 目标、非目标、反目标

目标
- 一个全局看板服务：列出所有项目，可切换，也可并排看；以后开新项目只注册、不改代码。
- 每个项目自己声明最终目标和关键节点；看板从这份声明和事件记录算进度。
- router、worker、协调人、巡检写同一种固定格式的事件；格式由工具校验。
- 巡检定时扫状态，催卡住或闲着的 router，派 router 已排好、依赖满足的节点。

非目标
- 不替 router 拆节点、选方法、判科研结论。
- 不修旧的 tick.py / pulse.py / flow_board 推送链（人类方针：新流程不修旧机制，旧的退役）。
- 本期不做"自动流程迭代反馈"的执行部分，只留数据（见 §8）。

反目标（技术上能跑也算失败）
- 看板又变成大段文字。
- 进度靠人或 agent 手写百分比；局部 PASS 显示成目标达成。
- 旧数据冒充现在；缺数据补 0。
- 巡检派了 router 没排好的节点，或绕过 router 改节点内容。

## 3. 层级与名词（人类定）

- **项目**：独立目标的一套工作。现在只有一个：DSE。
- **核心**：项目内的目标分组（核心一、核心二），各有一个协调人。
- **主线**：一个 router 负责的工作面（EVB、FE、SR、BASE…）。它们是 DSE 的主线，不是项目。
- **节点**：主线里一个 worker 交一个结果的单位。
- **关键节点**：属于项目，写在项目声明里。router 可报"已具备条件"，**只有该核心的协调人确认后才算达成**。

## 4. 做法（人类选 B）

考虑过三种：
- A 在现有 `push_dispatch.py` 上扩展：发布链路现成，但它依赖已停的旧定时检查，违背"不修旧机制"。
- **B 新建独立的全局服务（选定）**：干净的数据格式承载多项目和关键节点确认；代价是从头写，巡检的 orca 动作要在 Mac 上跑。
- C 用 agent-autoloop 当内核：它是"单 agent 接单"模型，不认识 router、主线和 orca 节点图，也没有界面，改造多于复用。只借两个思路：定时规则文件；agent 回复末尾附固定格式结构块。

## 5. 数据层

### 5.1 存放位置（我的决定）
- 权威存储放 WSL（看板服务所在）：`~/.progress/<project>/project.json` 和 `~/.progress/<project>/events.jsonl`。
- 不放 git 分支：几十条分支并行写同一文件会冲突，而且要等合并才可见。
- 每天把两份文件复制到 coord-sync 已有的私有备份链路（沿用现有 `coord-sync` 机制，只加路径）。

### 5.2 项目声明 `project.json`
```json
{
  "schema": "progress-project/v1",
  "id": "dse",
  "name": "通用软硬件协同 DSE 循环",
  "final_goal": {"text": "…一句话", "done_when": "…可核的条件"},
  "cores": [{"id": "core1", "name": "核心一 性能评估", "coordinator": "term_d7cf403c-…"},
            {"id": "core2", "name": "核心二 Agent 闭环", "coordinator": "term_6705ca88-…"}],
  "lines": [{"id": "EVB", "name": "事件级基线评估器", "core": "core1",
             "router": "term_7d3ec177-…", "run": "run_715d5987e7ae",
             "state_file": "docs/validation/router-EVB/STATE.md", "patrol": true}],
  "milestones": [{"id": "M1", "title": "EVB 首闭环通过", "core": "core1",
                  "done_when": "…可核条件", "lines": ["EVB"], "after": []}]
}
```
- 关键节点由各核心协调人起草、写入（用 CLI），人类可改。看板不内置任何项目的关键节点。
- 改声明本身也记一条事件（谁、何时、改了什么）。
- 项目级巡检（docs/PROJECT_REVIEW.md）读里程碑的两个可选字段，缺了不报错：`stage`（"S1"/"S2"/"S3"，没有任何里程碑带它时档案提示"阶段未声明"）和 `criteria`（`[{"id","text","status":"met|unknown|unmet","evidence"}]`，可选 `closed_at`；协调人把 done_when 拆成逐条可核对的条件，拆好之前脚本按句号、分号机械拆开并记 unknown）。字段写错会被 `save_project` 拒绝并指出哪一条。可选配置放 `defaults.project_review`，见 PROJECT_REVIEW.md。

### 5.3 事件 `events.jsonl`（只追加，一行一条）
```json
{"v":1,"id":"ev_…","at":"2026-11-04T07:10:00Z","project":"dse","line":"EVB",
 "node":"dev_cg_memory_stream","kind":"node_deliver",
 "actor":{"role":"worker","handle":"term_…","model":"gpt-6.1-sol","effort":"high"},
 "status":"delivered","outcome":"PARTIAL","milestone":null,
 "summary":"白话一句：做成了什么或卡在哪",
 "refs":{"task":"task_…","dispatch":"ctx_…","head":"<sha>","report":"docs/validation/…/REPORT.md"},
 "next_ready":["dev_cg_review"]}
```
固定的 `kind`：
| kind | 谁写 | 含义 |
|---|---|---|
| `line_open` / `line_close` | 协调人 | 主线开、关 |
| `node_plan` | router | 节点排好：带 `deps`、`spec`、`route`、`auto_dispatch`（是否允许巡检代派） |
| `node_dispatch` | router 或巡检 | 已派出，带 dispatch/worktree |
| `node_heartbeat` | worker（可选） | 长节点每约 30 分钟一次 |
| `node_deliver` | worker | 交付，`outcome` ∈ PASS/PARTIAL/FAIL/UNKNOWN |
| `node_review` | router | 审查结论，`outcome` 同上 |
| `node_block` | router 或 worker | 卡住，`summary` 写原因，`blocker` ∈ dep/design/env/resource/other |
| `node_supersede` / `node_cancel` | router | 被取代（带 `by`）或取消 |
| `milestone_claim` | router | 报某关键节点已具备条件，带证据 refs |
| `milestone_confirm` / `milestone_reject` | 该核心协调人 | 确认达成或驳回（带理由） |
| `nudge` / `auto_dispatch` | 巡检 | 催了谁、代派了哪个节点 |
| `observe` | 巡检 | 实测到的进程/终端状态与事件不一致 |
| `graph_propose` | router（或负责该 plan 节点的子 router） | 子图提议：过闸门后一次写入一组子节点（依赖、类型、写域、预算、验收标准、serial/parallel 段）。规则见 `autotaskgen/graph.py` 文件头和 FRAMEWORK §1–§2 |
| `worker_release` | 巡检 | 交付后释放 worker 终端（`orca orchestration worker-release`），每次尝试一条 |

节点类型 `node_kind`：`run`、`review`（旧名，等同 `check`）、`plan`（router 节点，结果是子图）、`check`、`decide`。旧的 `node_plan` 事件照旧有效，没有 parent/segment，按第 1 层算。`node_deliver` / `node_block` / `node_heartbeat` 可带 `failures`、`candidates`（不同失败、独立候选，调度器据此判断要不要叫父 router 拆单）和 `touched`（实际改过的路径，用于塌缩检测）。

节点状态由事件折叠得出，固定枚举：`planned ready running delivered reviewed blocked failed superseded cancelled`。

### 5.4 写入工具 `progress`（CLI）
- 例：`progress deliver --line EVB --node dev_cg_memory_stream --outcome PARTIAL --summary "…" --report <path> --head <sha>`。
- 工具校验：kind/状态/outcome 在枚举内；line、node、milestone 已声明（node 须先有 `node_plan`）；`milestone_confirm` 的 actor 必须是该核心声明里的协调人句柄；summary 必填、≤120 字。不合法就报结构化错误、不写入。
- Mac 和 WSL 都装同一个工具；WSL 本地加文件锁追加，Mac 通过 `ssh wsl` 转发。WSL 不可达时 Mac 侧先写本地队列 `~/.progress/outbox.jsonl`，下次成功时补发（保留原 `at`）。
- 句柄、模型从环境变量 `ORCA_TERMINAL_HANDLE` 等自动带上，不让 agent 手填。

### 5.5 写入规则（并入 `docs/design/rsi/ROUTER_DISPATCH_GUIDE.md` 新增 §7）
- router：每排一个节点 `node_plan`；派出 `node_dispatch`；收到交付并审查后 `node_review`；取代或取消时写对应事件；认为关键节点条件已到写 `milestone_claim`。
- worker：规格模板里加一条固定要求——交付前最后一步 `progress deliver`；被卡住时 `progress block`。
- 协调人：确认或驳回关键节点；开关主线。
- 这些规则已打包成技能，放在仓库 `skills/`：`progress-router`、`progress-worker`（安装见 `skills/README.md`）。worker 的 spec 末尾附 `skills/progress-worker/SPEC_SNIPPET.md`；巡检代派时自动附（`patrol.py` 内置同文，测试保证一致）。巡检还会兜底：Orca 任务已结束超 20 分钟仍无 deliver/block 的节点，记 `undelivered` 并催 router 补写。
- 各线现有 `graph.json`、STATE.md 照旧，事件记录不取代它们，只是脚本读的统一出口。

### 5.6 起步数据（我的决定）
- 一次性导入：派一个低档 worker 读 13 份 `graph.json` 和 STATE，给在推的主线生成 `node_plan` 及当前状态事件，事件标 `"source":"import"`；看板对导入数据显示"导入"。已关闭主线只导入汇总。
- DSE 的 `project.json` 初稿：主线和核心从 `~/.rsi/coordinators.json` 的 `research_directions` 生成；关键节点由两位协调人各自起草。

## 6. 服务与看板

### 6.1 服务（我的决定）
- WSL 上一个 Python 标准库 HTTP 服务（新端口 8790，systemd 用户单元 `progress-board.service`），读 `~/.progress/*`，提供：
  - `GET /api/projects`：所有项目及顶层进度；
  - `GET /api/p/<id>/state`：折叠后的整项目状态；
  - `GET /api/p/<id>/events?since=`：事件流；
  - `GET /api/stream`：SSE，新事件实时推到页面。
- 静态前端由同一服务提供；无构建步骤，库只从 cdnjs 取，锁版本。
- 反向代理加路由：验收通过后 `/board/` 指向新服务，旧看板挪到 `/board-old/` 保留一周再下线。

### 6.2 看板设计
设计方向：深色为主（可切浅色），"任务控制台"质感，用形状和颜色表达状态，文字只在点开详情时出现。参考：Claude-Code-Agent-Monitor 的实时看板、agent-flow 的实时分支图。

- **首页 项目总览**：每个项目一张卡，中心是"目标环"——环分成关键节点段：已确认实心、已报待确认斜纹、未达成空心；环心写"3 / 7 关键节点"。卡上一行小点表示在跑 worker 数（按模型档位着色）。可多选卡片并排进入。
- **项目页**
  - 顶部 **目标轨道**：像地铁线，最终目标是终点站，关键节点是站点；站点按 `after` 排先后，状态同目标环；待确认站点闪一个"等协调人"标记。
  - 中部 **主线泳道**（2026-11-04 改版）：只画顶层。每个核心一行表头：协调人在线情况、最近输出，以及协调人自己派出的执行者（标"由核心一协调人派出"）。下面每条主线一行：左边六边形 id 徽章、完整线名（括号里的 id 去掉，冒号后的说明另起一行，最多两行），以及活性灯（绿=近 2 小时有事件且终端在，黄=久无事件，红=终端掉了或连续卡住）。右边是摘要，不是全图：状态条和各状态计数，在跑节点和挂在它上面的执行者小牌（模型档位色、短模型名、已跑时长；虚线 = 任务已结束、进程还在），最近几个结果，有子 router 时标"子 router N"。行内不横向滚动。对不上任何主线或 router 的执行者放在泳道底部"未归属"一条。原来的"调度树"和"执行者星图"两块已删，机器分布缩成泳道标题栏上的几个计数。
  - **点进去看 router 大图**（地址 `#/p/<id>/core/<核心>`、`/line/<主线>`、`/r/<终端句柄>`，前进后退和刷新都保留位置）：顶部有面包屑（项目 › 核心一 › EVB › 子 router…）和返回按钮。主线页画完整节点图（按依赖连线；没有依赖信息的旧线按时间排，连续 6 个以上的未知或已排节点折成"+N"）。它派出的执行者画成卡片，悬停卡片时对应珠子亮起，外圈用档位色。子 router 画成卡片，带自己的摘要，点了再往下钻，层数不限。子 router 没有登记节点时，只显示它派出的执行者。核心页列出下属 router 卡片和协调人自己的执行者。
  - 悬停执行者小牌或卡片时，它在跑的珠子同时高亮，反过来悬停珠子时小牌也高亮。点小牌开执行者详情，点珠子开节点详情。
- 点任何节点/站点开抽屉：白话 summary、状态历史、REPORT 链接、审查结论。
- 每块标数据时间；超过 30 分钟无新数据显示"过期"并变灰，不显示旧内容冒充现在；缺字段显示"未知"，不补 0。
- 手机宽度可用。

## 7. 巡检

- 跑在 Mac（orca 与 router 都在这里），launchd 每 15 分钟一次（我的决定）。
- **第一步，确定性脚本**（不调模型）：读事件折叠的状态，加实测（`orca terminal list` 的 router 终端存在与最近输出时间；WSL `ps` 找 worker 进程），算出：
  - 闲置主线：没有 running 节点，但有 ready 节点或近 2 小时无事件；
  - 卡住节点：running 但超过规格 `expected_duration_minutes` 的 1.5 倍，或 worker 进程已退出却没有 deliver；
  - 未审：delivered 超过 2 小时没有 review；
  - 待确认关键节点：claim 超过 4 小时没被确认/驳回。
- **第二步，动作**：
  - 代派：节点状态 ready、deps 全部满足、`node_plan` 里 `auto_dispatch: true` 且带完整 spec 和 route → 巡检用 `orca orchestration worker-start` 按 spec 原样派，写 `auto_dispatch` 事件并通知该 router。不改 spec、不换模型。
  - 催 router：闲置、卡住、未审的，用 `orca terminal send` 给该主线 router 发一条短消息（列出具体节点和建议的下一步）。同一 router 每小时最多一条。
  - 待确认关键节点：提醒该核心协调人，每天最多两次。
  - 其他一律只记 `observe` 事件，在看板显示。
- **便宜模型只用在一处**：router 终端末屏是否在报错/卡死（vs 正在思考），由低档模型（`lunaroute/deepseek-4.1-flash` low）读末屏一段判断；它不决定派什么。
- 开关：每条主线 `project.json` 里的 `patrol` 字段，由该核心协调人设。默认核心二各线开；核心一各线由核心一协调人自己决定是否打开（核心一归它管）。全局急停：`~/.progress/PATROL_OFF` 文件存在即全停。
- 不重启旧的 DSH 定时派单和 tick.py。

- **调度（FRAMEWORK §2，规则见 `patrol.py` 文件头）**：每条线一个跨层级的就绪队列，关键路径长的先派；`max_parallel`（线上的，其次 `defaults.max_parallel`，默认不设）是可选的在跑 worker 节点数上限，不是目标；serial 段一次一个；plan/decide 节点不代派；只有交付里报出多个不同失败或多个独立候选时才叫父 router 拆分（并行位空着不叫）；同段 parallel 兄弟撞同一失败、同一屏幕报错或改同一处，记 `collapse`、停派该段并叫父 router 重拆或改串行；交付且 Orca 派单已结束的 worker 终端由巡检释放（只在 `--apply`，每轮最多 3 个，不碰 router/协调人）。指标见 LIVE_SCHEMA.md `metrics`。

## 7.5 巡检员

- 巡检每轮末尾检查：距上次调模型满 30 分钟（时间存在 Mac 的 `~/.progress/cache/orca_tasks.json` 里 `supervisor:<项目>`）就调用 `supervisor.py`；它不是单独的 launchd 任务。`patrol.py --force-supervisor` 可立即跑一次，`--no-supervisor` 跳过。
- **脚本层先行（不调模型）**：每轮对所有登记的 router、协调人和在跑 worker，只要停在输入框（没有 "esc to interrupt"）且屏幕上有可重试的错误（接口错误、网络断流、限流/容量），就用 `orca terminal send` 发 `继续`。每次停顿最多 2 次（以终端句柄加该错误出现时刻为键），之后改发原有的"已停"提醒给 router。每次记一条 `nudge` 事件（reason=auto_continue）。不受各线 `patrol` 开关影响，只受全局 PATROL_OFF 控制；只在 `--apply` 时执行。
- **输入**：地图文件（≤4 万字）：最终目标与关键节点；每条线的节点（状态、类型、依赖、预计/已跑分钟、派出/交付时间）、最近 5 条事件、router 屏幕末尾；在跑 worker 的屏幕末尾；上次以来脚本做过的动作；上一份报告。
- **模型**：`codex exec -m gpt-6.1-terra -c model_reasoning_effort="high" --output-schema …`，超时 15 分钟，可用环境变量 `PROGRESS_SUPERVISOR_CMD` 换成假命令。提示词（中文）要求：先看地图，再逐条线用 `progress-inspect` 抽查；只读检查，不改文件、不提交、不发消息、不停止或派发 agent；证据必须引用实际查到的内容；严格按并行度、推进程度、卡死/打转/无证据声称、偏离目标来判断。
- **输出**：`summary`（一段白话）和 `lines[]`：`{line, status ok|attention|problem, progress_note, issues[{kind, evidence}], suggestions[≤3]}`。格式不合法就整份作废，不发任何消息。报告存 `<项目>/supervisor/latest.json`（保留最近 48 份），并写一条 `observe` 事件（handle `supervisor@mac`）。
- **只读工具** `progress-inspect`（`~/.progress/bin/`，源码 `progress_inspect.py`）：`screen`、`events`、`node`、`file`（只限 PROGRESS_REPO 与在跑 worker 的 worktree，≤200KB，拒绝 `..` 与符号链接越界，`/home/` 路径走 ssh wsl）、`git-log`、`tasks`；白名单之外一律拒绝；每次调用写一行 JSON 到 `PROGRESS_INSPECT_LOG`。
- **下发**：仅 `--apply` 时，把 attention/problem 线的建议合成一条发给该线 router；每个 router 每小时最多一条，内容没变不重发；router 不在线不发。
- **主机资源与 router 掉线（脚本层，不调模型）**：快照每轮在 WSL 和 Mac 上查磁盘（`df -Pk`）和内存。可用空间小于 50 G 记 `low`，小于 15 G 记 `critical`。巡检对 `low` 通知所有在线核心协调人（6 小时内不重复），对 `critical` 再加所有在线主线 router（2 小时内不重复），不受每小时一条限制。主线 router 终端消失时，巡检把情况（哪条线、旧句柄、run、在跑节点数、待办）发给该线的核心协调人，建议用 `orca orchestration run-use --id <run>` 开新 router 接手，2 小时内不重复；协调人也不在就只记 observe。若该线 run 的协调人已是另一个在线终端，快照自动换绑到它（`rebound_from`），巡检提醒更新 project.json。巡检员建议遇到 router 不在线时，转给核心协调人。看板顶部有对应横幅。
- **沙箱：用户决定：信任 terra，放开沙箱，靠提示约束 + 事后审计。** 前一位实现者发现 orca 在 codex 只读沙箱里跑不起来，用户决定不再逐项试沙箱选项，直接用 `--dangerously-bypass-approvals-and-sandbox`，工作目录是空的临时目录。约束靠提示词；事后审计看 codex 的 `--json` 事件流：出现写类命令（git commit/push/reset/checkout 等、rm/mv/cp、`sed -i`、`tee`、重定向写文件、`orca terminal send`、`worker-start/stop` 等）时，报告照存，但标 `policy_violation`，当轮不发任何建议，看板卡片显示红色提示。其他只读命令不拒绝。

## 7.6 裁决面板与 QQ 回路

- 巡检在 `--apply` 时对每个 ready 的 `decide` 节点（spec 里要有编号选项）并行调用 Claude Opus 和 GPT Astra 各自独立判断，每轮最多 2 个面板；已有 `decision` 的 spec 不重判。两家一致且都不要求人定 → 直接执行（写 `decision`、代写 decide 节点的 `node_deliver`），并通过 `hermes send` 发 QQ 通知（每次执行都发，说明怎么推翻）。否则 → `node_block` + 一张 QQ 决策卡片（FRAMEWORK §6 格式，分两条发）。
- QQ 回复由 Hermes 插件 `progress-qq`（`hermes_plugin/progress-qq`）收进 `~/.progress/inbox.jsonl`，巡检每轮应用：选项直接生效；对已执行的卡片选别的或"推翻"→ 通知 router 调整（必要时 git revert）；"为什么"→ 再调一次 Opus 用白话解释后发回 QQ。每条都记 `human_reply` 事件。详见 LIVE_SCHEMA.md。
- 只有 FRAMEWORK §5 的触发条件才允许评审写 `needs_human`。

## 7.7 接续包

- 问题：router / 协调人 / worker 换终端或上下文被压缩后，会忘了未完成节点、刚做的裁决和没落实的跟进。
- 做法（脚本层，不调模型）：`progress-inspect resume --line L [--node N]` 从事件记录现算一份接续包；巡检每轮把每条线的包写到 `~/.progress/<项目>/resume/<线>.md`，没有 CLI 的 agent 也能读。夜间任务写的经验（`memory/<线>/LESSONS.md`、`memory/GLOBAL.md`）有就附在包里。
- 失忆检测（仅 `--apply`）：终端换了（快照 `rebound_from`，或和上一轮的句柄不同），或上下文被压缩（末屏出现 compact / Compacting / 压缩，或上下文用量比上一轮降了 30 个点以上，Claude 与 Codex 两种进度条都认），就给该 router / 协调人发一条 `[接续]`，叫它先跑 resume。同一（句柄、原因、小时）只发一次，末屏在等输入时不发、下一轮再试。worker 自动继续时，如果该节点以前派给过别的终端，消息里加上 `progress-inspect resume --line L --node N`。
- 派单：SPEC_SNIPPET 和 router 技能都写明"开工前、以及中断/换终端/压缩后先运行 resume"。

## 8. 后续：流程迭代反馈（本期只留数据）

事件记录天然能算：节点周期、返工率（同节点 FAIL→repair 次数）、空等时长（ready 到 dispatch）、审查滞后、各模型档位的一次通过率。本期在看板加一个"流程指标"页把这些画出来；每周由低档模型据此写一份改进建议交流程侧，是下一期的事，人类另定是否开启。

## 9. 约束

- 不显示、不读取 `R/state/READ_BANS.md` 列出的路径；不读凭据、auth、Codex sessions 和 memories。
- WSL 磁盘紧：事件文件超过 50 MB 按月滚动归档。
- 不 push、不改 main；代码放 dse-rsi 分支 `tools/progress/`（CLI、服务、巡检、测试），部署副本放 WSL `~/opt/progress/`、Mac `~/.progress/bin/`。

## 10. 留给实现者

- 前端具体视觉（配色、动效、字体、图形库），在 §6.2 骨架内自由发挥；交付时附桌面和手机截图。
- 折叠算法、事件 id 生成、SSE 重连、导入脚本的启发式。
- 分期：P1 数据格式 + CLI + 规则文字 + 起步导入；P2 服务 + 看板；P3 巡检；P4 流程指标页。P1 完成后 P2、P3 可并行。

## 11. 验收

- 用 CLI 写入一条非法事件（未知 kind、未声明节点、非协调人确认关键节点）均被拒，给结构化错误。
- 起步导入后，看板首页和项目页截图与 `orca` 实测、各线 STATE 对得上；在跑 worker 数与 WSL 进程数一致。
- 停写 31 分钟后对应块显示过期。
- 巡检在测试项目上：对一个 `auto_dispatch: true` 的 ready 节点派出一次且只一次；对闲置主线发一条催促且一小时内不重复；`PATROL_OFF` 存在时零动作。
- 协调人确认关键节点后，目标环和轨道站点从斜纹变实心。

## 12. 审阅后补定（均为我的决定）

- **在跑数据怎么到 WSL**：Mac 上另起一个 launchd 快照任务，每 5 分钟一次（不调模型），收集 `orca terminal list` 里各 router 终端的存在情况和最近输出时间，再经 `ssh wsl ps` 找 worker 进程，写成 `~/.progress/<project>/live.json` 推到 WSL。worker 进程按命令行里的 `ORCA_WORKSPACE_ID` 找到对应 worktree，再用 `node_dispatch` 事件里记的 worktree 对到具体节点；对不上时再按 orca 派单所属 run 的协调人在调度树（`router_tree`）里找到最近的主线和核心；调度树怎么画、怎么派子 router 见 [ROUTER_RULES.md](ROUTER_RULES.md)；仍对不上的进程归到"未归属"，单独显示。ib-agent 虚拟机本期不采集，看板上把这一块标"未知"。巡检读的也是这份快照。
- **"过期"量的是什么**：在跑数据看 `live.json` 的生成时间，超过 30 分钟算过期。事件流本身不判过期，主线安静只影响 router 活性灯（黄灯）。
- **依赖算满足的条件**：依赖节点 `node_deliver` 的结果是 PASS 或 PARTIAL 就算满足，不必等审查。这沿用派发准则 §5 的"审查不挡下一个开发节点"。FAIL 和 UNKNOWN 不算；被取代的节点由取代它的节点顶上。router 可以在 `node_plan` 里给某条依赖加 `require_review: true`，这时要等审查 PASS。
- **spec 和 route 的格式**：spec 是仓库里的规格文件，写成"分支@路径"，沿用现有 `specs/*.md` 的格式（首行 JSON 头带 route、expected_duration_minutes、write_scope）。巡检代派时，用文件全文作 `--spec`，route 映射到 `--agent/--model/--effort/--on`，worktree 用 `new-top-level`，从主线集成分支开出。派发失败或超时不重试：写一条 `node_block`（blocker=env），并通知 router。"只派一次"靠事件保证：派发前先写 `auto_dispatch` 事件占位，同一节点已有 dispatch 事件就跳过。
- **`auto_dispatch` 默认值**：规格齐全的 `node_plan` 默认为 true（人类同意在已排好的节点里自动派），router 可以对单个节点设成 false。
- **谁能写什么**：`node_plan/review/supersede/cancel`、`milestone_claim` 只能由该主线声明的 router 写；`node_deliver/heartbeat/block` 只能由该节点 dispatch 记录的 worker 终端写，router 也可代写；`milestone_*` 的确认和驳回只能由该核心协调人写；巡检和导入分别以 `patrol`、`import` 身份写，只能写各自的 kind。人类用 `project.json` 里登记的人工槽句柄。身份取自环境变量，只防误写、不防伪造，这是内部工具可以接受的边界。
- **返工怎么记**：修复不复用原节点，而是开一个新节点，用 `node_supersede` 指向原节点（和现在 `*_repair` 节点的做法一致）。返工率按"取代链长度"来算。
