# live.json 格式（Mac 快照任务写，看板服务和巡检读）

路径：`$PROGRESS_HOME/<project>/live.json`，用 `progress live-put` 写入（原子替换）。

```json
{
  "schema": "progress-live/v1",
  "generated_at": "2026-11-04T08:00:00Z",
  "source": "mac-snapshot",
  "hosts": {"mac": "ok", "wsl": "ok", "ib-agent": "unknown"},
  "routers": {
    "EVB": {"handle": "term_7d3ec177-…", "present": true,
            "last_output_at": "2026-11-04T07:58:10Z",
            "screen_flag": null}
  },
  "workers": [
    {"host": "wsl", "pid": 2217, "workspace": "615bc546-…::/home/…/core1-bd-r1-review-1104",
     "worktree_name": "core1-bd-r1-review-1104", "terminal": "term_326744e7-…",
     "model": "codex/gpt-6-luna", "effort": "high", "started_at": "2026-11-04T04:01:00Z",
     "line": "EVB", "node": "dev_cg_memory_stream", "attribution": "task", "core": "core1", "router": "term_7d3ec177-…",
     "task": {"id": "task_1b405e499f5b", "run": "run_218d97685125", "dispatch": "ctx_7d07dd23c137",
              "state": "succeeded", "dispatched_at": "2026-11-04T06:08:53Z",
              "completed_at": "2026-11-04T07:30:12Z"},
     "lingering": true,
     "last_output_at": "2026-11-04T07:31:00Z", "title": "⠋ Fix the thing", "busy": true}
  ],
  "screen_reads": {"tried": 5, "failed": 0},
  "tasks": {
    "task_1b405e499f5b": {"state": "succeeded", "dispatched_at": "2026-11-04T06:08:53Z",
                          "completed_at": "2026-11-04T07:30:12Z"}
  },
  "resources": {
    "wsl": {"disks": [{"mount": "/", "avail_gb": 12.3, "used_pct": 97, "level": "critical"}],
            "mem_avail_gb": 14.2, "checked_at": "2026-11-04T07:30:12Z"},
    "mac": {"disks": [{"mount": "/System/Volumes/Data", "avail_gb": 210.0, "used_pct": 40, "level": "ok"}],
            "mem_avail_gb": null, "checked_at": "2026-11-04T07:30:12Z"}
  },
  "errors": []
}
```

- `present`：router 终端在 `orca terminal list` 里存在且 connected。
- `screen_flag`：末屏判断结果，`null`（正常）或 `"stream_error"`、`"stuck"`、`"waiting_input"`。
- `resources`：每台主机的磁盘和内存。`level`：可用空间小于 15 G 为 `critical`，小于 50 G 为 `low`，其余 `ok`（常量 `DISK_CRIT_GB`、`DISK_LOW_GB`）；同一挂载点只记一次；`mem_avail_gb` 取自 /proc/meminfo 的 MemAvailable（Mac 上为 `null`）。某台主机检查失败就不出现在这里，原因写进 `errors`。
- 路由器/协调人/worker 的 `screen`：`transient` 为真表示末 12 行里有可重试的模型/API 错误（含 "api error"、"held this request"、"without routing"、"gateway"，以及带报错字样的 500/502/504），`prompt` 为真表示末 8 行里有输入框（Codex 的 `›` 或 Claude Code 的 `❯`）。巡检据此报 `outage:model`。字段本身没有新增。
- `rebound_from` / `rebound_via`（只出现在主线 router 上）：project.json 登记的 router 终端已不在，但该线 `run` 的协调人（`run-list` 的 coordinator）是在线终端且不同，快照就改用它：`handle` 是新终端，`present` 为 true，`rebound_from` 是登记的旧句柄，`rebound_via` 固定为 `"run"`。核心协调人没有自己的 run，不做换绑。project.json 仍要人工更新。
- worker 用命令行里的 `ORCA_WORKSPACE_ID` 和 `ORCA_TERMINAL_HANDLE` 识别；`line`/`node` 是与事件里 `node_dispatch.refs.worktree` / `refs.terminal` 对上后填的，对不上为 `null`（看板显示"未归属"）。
- 一个 codex 进程有 node 包装和原生两层，只记一个（按 workspace 去重）。
- 采不到的主机写 `"unknown"`，不写空列表冒充"没有 worker"。

## patrol 输出里的 `project_review`

`patrol --apply` 的结果多一项 `project_review`：`{ran, ok, why, path, sync?}`（`sync` = `{synced, why?}`，档案复制到 WSL 的结果；命中留出集时多 `hits`）；干跑是 `{ran:false, reason:"dry_run"}`，档位 >= 2 是 `{ran:false, reason:"stage_unwired"}`。新的 finding：`idle_to_coordinator`、`idle_coord_rate_limited`、`project_review_stage_unwired`。看板接口 `/api/p/<项目>/project_review`：`{empty, generated_at, verdict_hint, stage_progress[], flags[], pending[], file, md_file, md_path}`。

## metrics（调度指标，FRAMEWORK §7）

快照和巡检输出（`patrol.py` 结果里的 `metrics`）都有，按线一项，算法在 `graph.line_metrics`：

```json
"metrics": {"EVB": {
  "ready_undispatched": {"nodes": 2, "node_hours": 3.5, "oldest": [{"node": "dev_x", "ready_min": 140}]},
  "critical_path": {"nodes": ["dev_x", "dev_y"], "minutes": 180.0, "stall_min": 140},
  "review_run": {"review_min": 30, "run_min": 130, "ratio": 0.231, "over": true}}}
```

- `ready_undispatched`：现在就绪、从没派过的节点（`decide` 不算，它等裁决不等 worker）；`node_hours` = 各自就绪至今的小时数之和（漏派）。
- `critical_path`：按预计分钟算的最长未完成链（plan 节点要等它的子节点）；`stall_min` = 链上有节点在跑为 0，否则为链上最后一次有事件至今的分钟数；没有未完成节点时整项 `nodes` 为空、`stall_min` 为 `null`。
- `review_run`：review/check 节点耗时之和 ÷ run 节点耗时之和（派出到交付，在跑的算到现在，导入记录不算）；没有 run 耗时时 `ratio` 为 `null`；`over` 为 true 表示超过 20%，巡检记 `review_heavy`。

## 任务归属与真实时间（orca 派单）

- `worker.attribution`：`line`/`node` 是怎么对上的。按优先级：`"terminal"`（终端句柄）、`"worktree"`（工作区路径）、`"task"`（进程 → orca 派单 → `taskId` 等于某节点 `refs.task`，归到该节点）、`"run"`（`node` 仍为 `null`；派单所属 run 的协调人在 `router_tree` 里，取它最近的主线祖先作 `line`，核心祖先作 `core`；挂在核心协调人下面的 router 只有 `core`，`line` 为 `null`；树里没有该协调人时，再看 run 目标是否以已声明的线 id 开头，如 `EVB：…`）；都对不上为 `null`。没有 `"core"` 这一类。
- `worker.core`：该进程所属核心 id，取自 `line` 的核心或 router 的核心祖先；否则 `null`。
- `worker.router`：派出该进程的 router 句柄，即派单所属 run 的协调人（对应 `router_tree[].handle`）。只要派单已知就填，不论 `attribution` 是什么；派单未知为 `null`。

## router_tree（调度树）

```json
"router_tree": [
  {"handle": "term_d7cf403c-…", "kind": "core", "name": "核心一 性能评估", "line": null, "core": "core1",
   "parent": null, "runs": ["run_a"], "present": true, "last_output_at": "2026-11-04T07:58:10Z", "edge": "declared"},
  {"handle": "term_7d3ec177-…", "kind": "line", "name": "事件级基线评估器", "line": "EVB", "core": "core1",
   "parent": "term_d7cf403c-…", "runs": [], "present": true, "last_output_at": null, "edge": "default"},
  {"handle": "term_59c7976d-…", "kind": "sub", "name": "EVB-多核扫描", "line": "EVB", "core": "core1",
   "parent": "term_7d3ec177-…", "runs": ["run_b"], "present": true, "last_output_at": null, "edge": "dispatch"}
]
```

- `kind`：`core`（核心协调人）、`line`（project.json 声明的主线 router）、`sub`（由 run 发现的子 router，只收挂在本项目已声明 router 下面的）。
- `name`：核心/主线取声明的名字；子 router 取终端 `paneTitle`，其次 `title`，其次第一个 run 目标（截断）。
- `line` / `core`：子 router 取最近的主线祖先 / 核心祖先；直接挂在核心下面的子 router 的 `line` 为 `null`。
- `parent`：父 router 句柄，根为 `null`。`runs`：该 router 作为协调人的 run id。`present` / `last_output_at`：同 `routers`。
- `edge`：父亲怎么定的，按优先级 `declared`（project.json 主线的 `parent`，值为主线 id 或核心 id）、`dispatch`（它的终端是父亲 run 里的 worker）、`worktree`（它所在 worktree 的父 worktree 里有父亲的终端）、`default`（主线默认挂核心协调人）。有环时断开。
- 来源：`orca orchestration run-list`、各 run 的 `worker-list`、`orca terminal list`、`orca worktree list`。`worktree list` 失败时写一条 error，跳过 worktree 这条边。
- `worker.task`：该进程对应的 orca 派单 `{id, run, dispatch, state, dispatched_at, completed_at}`（按终端句柄，其次工作区路径找）；找不到或 orca 查不到为 `null`。时间已统一成 UTC `YYYY-MM-DDTHH:MM:SSZ`，取不到为 `null`。
- `worker.lingering`：`true` = 任务已结束（succeeded/failed/cancelled/completed 等）但进程还活着（终端被保留，不是故障）；`false` = 任务未结束或查无派单；`null` = orca 数据没取到，不知道。
- `worker.screen`：worker 终端末屏判断（只对任务未结束、进程已跑满 10 分钟的 worker 读，每轮最多 30 个；用 `orca orchestration worker-read --dispatch <id> --source terminal --limit 40`，Mac 上也能读 WSL 的终端）。没读（太新、任务已结束、没有派单、读失败）时没有这个字段。形如 `{"flag": "error"|"idle"|"exited"|null, "line": "<命中的报错行，或末尾最后一行非空内容，≤160 字>", "checked_at": "<ISO>", "since": "<ISO，flag 非空时有>"}`：
  - `exited`：worker 状态的 `liveness` 不是 `live`，或终端状态是已退出/已释放。
  - `error`：末 12 行命中报错标记（stream error、rate limit、Bad Request 等），且整屏没有"esc to interrupt"/"working ("。
  - `idle`：整屏没有工作标记，且屏幕内容（取哈希）与上一轮相同并已连续 ≥10 分钟不变。哈希和首次出现时间存在任务缓存文件里（键 `screen:<dispatch>`，超过 1 天没更新的会清掉）。
  - `since`：这个状态开始的时间；巡检据此判断"已停多久"。
  - `--no-screen` 只跳过 router 屏幕，不影响 worker 屏幕。读失败写进 `errors`，不会让快照失败。
- `worker.last_output_at` / `worker.title` / `worker.busy`：来自 `orca terminal list --environment wsl --json`（按终端句柄对上）。`last_output_at` 是终端最后输出时间（ISO），`title` 是终端标题（≤80 字），`busy` 为 true 表示标题以盲文转圈字符（U+2800–U+28FF）开头，即 agent 在干活。该终端不在列表里时三项都是 `null`；这次调用失败时三项为 `null`，并写一条 error（host `wsl`，"orca terminal list --environment wsl 失败"）。巡检据此关闭空闲且任务已结束的 worker 终端。
- `routers[].screen` / `coordinators[].screen` 新增（接续包用）：`compact`（末屏出现 compact / Compacting / 压缩）、`ctx_used`（上下文已用百分比 0–100，取自 Claude 的 "Context ███░░░ NN%" 或 Codex 的 "NN% context left"（用 100−NN）；屏上没有进度条时没有这个字段）。
- `screen_reads`：`{"tried": n, "failed": m}`，本轮快照读 router/协调人/worker 屏幕的次数和失败次数；巡检在失败 ≥ 一半（且读了 ≥ 3 次）时发 QQ 告警。
- `tasks`：`{taskId: {state, dispatched_at, completed_at}}`，覆盖节点 `refs.task` 和在线 worker 的任务，时间来自 `orca orchestration worker-show`。已结束且有完成时间的结果缓存在 `$PROGRESS_CACHE_DIR/orca_tasks.json`（默认 `~/.progress/cache/`），每轮最多新查 60 个，其余下轮补；没有任何派单线索的 task 没有条目，不编造。
- 派单索引的取法：先 `run-list`，再对每个 run 单独 `worker-list --full --json --run <id>`（按 nextCursor 翻页，最多 20 页），保留 `terminalState` 为 active/retained/reclaimable/release_unknown 的行，以及 worker 状态未结束的行，按 dispatchId 去重。不带 `--run` 时 orca 只返回调用终端绑定的那个 run（launchd 下只有几条），所以不能依赖。`run-list` 失败时退回不带 `--run` 的按状态查询，若 `scope.source` 为 `bound` 则记一条 error（"只返回了绑定 run 的派单，结果不完整"），并按不完整处理。
- orca 调用失败时对应字段为 `null`，原因写入 `errors`；不会因缺数据把 worker 标成 lingering。

## 裁决面板与 QQ 回路（decide.py，patrol --apply）

- 事件 `decision`（角色 patrol，人也能写）：`line`、`node`、`card`（`D<n>`，每个项目 1+已有最大编号）、`result`：`agreed`（Opus 与 Astra 一致，带 `choice`、`option`）、`escalated`（分歧或评审要求人定）、`panel_failed`（某位评审两次都没给出有效结果）、`human`（用户在 QQ 选了）、`overruled`（用户推翻）；`verdicts` 是两位评审的精简意见，`options` 是选项文本，`refs.card`、`refs.spec`。
- 事件 `human_reply`（角色 patrol）：巡检每应用一条 QQ 回复写一条，`refs.message_id` 就是"已应用"标记，另有 `refs.card`、`refs.verb`（pick/overrule/why）、`refs.outcome`。
- 面板一致时巡检以 patrol 身份为 decide 节点写 `node_deliver`（PASS，必须带 `refs.card`，只允许 decide 节点）；其余情况写 `node_block`（blocker design，"等用户裁决 D<n>"）。
- Mac 本地文件（`~/.progress/`）：`qq.json`（`{"target","owner"}`，缺失则不发 QQ，记 finding `qq_unconfigured`）、`inbox.jsonl`（Hermes 插件 progress-qq 写入，一行一条 `{at,card,verb,arg,text,message_id}`）、`qq_cards.json`（`{card:{project,line,node,status,options}}`，插件据此识别裸"选 2"）。
- 用户回复：`D<n> 选 k`、`D<n> 推翻[ 理由]`、`D<n> 为什么`。人类身份：`project.humans` 含 `qq:<owner>` 时以 human 角色写，否则以 patrol 写并在 `refs.by` 记 `qq:<owner>`。

## 接续包（patrol / progress-inspect resume）

- `progress-inspect resume --line L [--node N] [--max-chars 12000]`：只读事件记录生成的中文 markdown（不调模型）：最终目标与本线关键节点、未完成节点、最近裁决与用户拍板、未落实跟进、最近交付、经验（`~/.progress/<项目>/memory/<线>/LESSONS.md` 和 `memory/GLOBAL.md` 各取前 40 行，文件不存在就没有这一节）。`--node` 把该节点放最前面并列出它的全部事件、spec 和依赖的交付。超长时先丢最旧的交付，再丢最旧的裁决。
- 巡检每轮（含 dry-run）写 `~/.progress/<项目>/resume/<线>.md`（原子写）；`--apply` 时另写 `resume/seen.state.json`（上一轮见到的 router 句柄、压缩标记、上下文进度）。
- 巡检结果新增 `resume_files`（写出的文件）和 finding `memory_loss`（`line`、`target`、`causes`：`terminal` / `compact` / `gauge`）；`--apply` 时对应发 `[接续]` 消息并写 `nudge` 事件 reason `resume`（key `resume:<句柄>:<原因>[:<小时>]`）。
