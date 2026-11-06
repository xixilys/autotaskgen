# 项目负责人（lead）

一个长期运行的 agent 会话，替人坐在项目负责人的位置上：每隔几小时被唤醒一次，读框架自己算出的数（项目级档案、
live.json、自己的台账和收件箱），**先做、后报**，把每一条调整记进台账，到点回查。

代码：`autotaskgen/lead.py`（命令行，`lead`）、`autotaskgen/lead_wake.py`（唤醒）、`autotaskgen/hermes_plugin/lead-qq/`
（QQ 私信进收件箱）、`launchd/com.example.lead-wake.plist`（定时）、`templates/lead/BOOT.md`（每轮操作手册模板）。
全部只用 Python 标准库；`lead` 本身从不调用模型。

## 为什么要它

- **减轻人的负担。** 巡检和项目级巡检只会"报"，真正把数字变成"去问协调人为什么、改里程碑结构、给空线补活"的那一步，
  原来要人来做。负责人接下这一步，人只在少数几类事上被打扰（改最终目标或完成标准、花钱、对外动作、删不可恢复的数据）。
- **让人信得过。** 负责人做的每件事都可以查：
  - 每条调整都写明**改什么、为什么（引用数字）、落实标准 `done_when`、几小时后回查 `check_at`**；
  - 回查看的是事实（project.json、事件、节点在不在跑、数字变没变），"对方回了消息"不算落实；第二次没落实就升级（`escalated`），
    要求换办法而不是再催一遍；
  - 台账是一个人能直接读的 jsonl，人可以在 QQ 上回"L3 推翻 理由"推翻任何一条；
  - 每轮结束把状态写到看板的"项目负责人"栏：结论、一句话、未结的调整及其落实标准和回查时间。
  - 每日汇报里的命中率 = 落实 ÷ 已回查，是人判断负责人干得好不好的依据。

## 怎么被唤醒

`lead_wake.py` 由 launchd 每 5 分钟跑一次（只是检查，不一定唤醒），也会被 lead-qq 插件在收到消息时立即踢一下。

| 条件 | 唤醒原因 |
|---|---|
| `--force` | `manual`（忽略间隔和 WAKE_OFF） |
| 有未处理的人发来的消息，且距上次唤醒 ≥ `inbox_min_gap_s`（默认 600 秒） | `inbox` |
| 距上次唤醒 ≥ `interval_s`（默认 7200 秒） | `timer` |

不打断正在进行的一轮：`cycle.json` 是 `running` 且开始不到 `stale_running_s`（默认 3 小时）就跳过；终端不是空闲输入框
（`orca terminal wait --for tui-idle`）也跳过，忙超过 `busy_alert_s`（默认 4 小时）发一次 QQ。
负责人终端不在了（关掉、断开、孤儿）就在 `worktree` 里用 `agent`（默认 claude）新开一个，把新句柄写回 config.json，并发 QQ 说明。
终端里跑的不是配置的 agent 就拒绝唤醒。上次唤醒 30 分钟后还没开始新一轮，提醒一次。

一次唤醒 = 先发 `/clear`（新开的终端不发），再发一条提示：读 `<状态目录>/BOOT.md` 并按步骤完成一轮。
会话没有上一轮的对话记忆，只靠文件接上。停掉而不卸载：`touch <状态目录>/WAKE_OFF`。
日志 `<LEAD_ROOT>/log/wake.log`，唤醒状态 `<状态目录>/wake.json`（`last_wake`、`last_reason`、`busy_since`、`alerted_*`）。

## 每轮做什么

照 `BOOT.md`（从 `templates/lead/BOOT.md` 生成）。骨架：

1. `lead cycle start --reason <原因>`；
2. 读 `HANDOFF.md`、全局目标文件、`lead glance`、`lead inbox list --unread`；
3. 先处理人的消息（回复、照做、`lead inbox ack`；"L<n> 推翻"就撤回并把那条记为 `obsolete`）；
4. 回查到期台账（`lead ledger list --filter due` → `lead ledger check`）；
5. 看数追问、做项目级调整，每条 `lead ledger add`；
6. 收尾：写本轮报告、重写 `HANDOFF.md`、有值得说的才发 QQ、`lead cycle end`。

硬规则（模板里都有）：先做后报；只有四类事先问人；不读留出集；负责人不亲自读细节，派 subagent 拿结论；不替 router 派节点，
节点级的事找 router、router 不动找协调人；模型说"别人没做过"之类的结论要查证后才用。

## 读什么、写什么

读（只读）：

- `<progress_home>/<pid>/project_review/dossier-*.json` 里最新的一份（`headline`、`stages`、`critical_path`、`parallel`、`idle`、
  `graph_audit`、`overhead`、`partial`、`decision_delay`、`flags`；这些字段被 `tests/test_project_review.py::LeadContract` 锁住）；
- `live.json`：`board_host` 为空时读本地 `<progress_home>/<pid>/live.json`，否则 `ssh <board_host> cat <board_progress_home>/<pid>/live.json`；
- 自己的 `ledger.jsonl`、`inbox.jsonl`、`cycle.json`；
- 留出集：**从不**。`lead` 没有任何指向留出集的选项。

写（全在状态目录 `<LEAD_ROOT>/<pid>/`，默认 `~/.lead/<pid>/`，最好是一个本地 git 仓库）：

| 文件 | 谁写 | 内容 |
|---|---|---|
| `config.json` | 人 / `lead init` / lead_wake（换终端时） | 见下面"配置" |
| `BOOT.md` | `lead init`，人改 | 每轮操作手册 |
| `HANDOFF.md`、`reports/*.md` | 会话 | 交接、每轮报告、每日汇报 |
| `ledger.jsonl` | `lead ledger` | 台账 |
| `inbox.jsonl` | lead-qq（进）、`lead qq`（出）、`lead inbox ack` | 收件箱 |
| `cycle.json` | `lead cycle` | `{state: running|done, n, started, ended, reason, summary, verdict, report}` |
| `status.json` | `lead cycle end` | 看板状态，并复制到 `<progress>/<pid>/lead/status.json` |
| `wake.json`、`.wake.lock` | lead_wake | 唤醒状态、互斥锁 |

`lead cycle end` 还会在状态目录是 git 仓库时 `git add -A && git commit`（身份 `git_name`/`git_email`，默认 `lead`/`lead@localhost`），
台账和报告因此都有历史。

## 文件格式

时间一律 UTC，`YYYY-MM-DDTHH:MM:SSZ`。下面的例子是编的。

### 台账 `ledger.jsonl`（一行一条）

```json
{"id": "L4", "at": "2026-01-10T08:00:00Z", "to": "coordinator:core1", "kind": "adjust",
 "what": "B 线空了 6 小时：从 M3 拆两个独立节点给 B", "why": "B 零并发 72%，worker 主机空闲内存 40G",
 "done_when": "project.json 里 B 线出现 M3 下的新节点，且 2 小时内有一个在跑", "baseline": {"b_zero_share": "0.72"},
 "check_at": "2026-01-10T12:00:00Z", "status": "open", "sent": "req_abc",
 "checks": [{"at": "2026-01-10T12:05:00Z", "verdict": "not_done", "evidence": "project.json 没有新节点"}],
 "user": [{"at": "2026-01-10T09:00:00Z", "text": "同意"}]}
```

- `id`：`L<n>`，递增；`kind`：`adjust` | `question` | `pro`（找外部专家）| `human` | `tooling`；`to`：收件人，如
  `coordinator:<core>`、`router:<line>`、`human`、`tooling`。
- `status`：`open` → `done` | `obsolete`（被推翻或不再需要）| `escalated`（第二次 `not_done`）。
- `lead ledger check <id> --verdict done|not_done|obsolete|cannot_tell --evidence "…" [--recheck-h 6]`：`done`/`obsolete` 关闭；
  `not_done` 第一次留在 `open` 并改 `check_at`，第二次变 `escalated`；`cannot_tell` 只改 `check_at`。
- `lead ledger add` 必须给 `--to`、`--what`、`--done-when`。`lead ledger user <id> --text` 记下人对这一条说的话。

### 收件箱 `inbox.jsonl`

```json
{"id": "I3", "dir": "in", "at": "2026-01-10T09:00:00Z", "text": "L4 推翻 先别动 B", "ref": "L4", "message_id": "qq-123",
 "acked": "2026-01-10T09:20:00Z", "handled": "撤回 L4，已通知 core1"}
{"id": "O7", "dir": "out", "at": "2026-01-10T09:21:00Z", "text": "【结论】…", "ok": true, "err": ""}
```

`I<n>` 是 lead-qq 写的进件（同一 `message_id` 只记一次），`O<n>` 是 `lead qq` 发出去的消息（不管成没成功都记，`ok`/`err`）。
`acked`/`handled` 由 `lead inbox ack <id> --note` 写。

### 看板状态 `status.json`（schema `lead-status/v1`）

```json
{"schema": "lead-status/v1", "updated": "2026-01-10T12:30:00Z", "cycle": 12, "verdict": "at_risk",
 "summary": "B 线空转，已要求拆节点", "report": "/home/u/.lead/demo/reports/20260110T1230Z.md",
 "ledger": [{"id": "L4", "to": "coordinator:core1", "kind": "adjust", "what": "…", "done_when": "…",
             "check_at": "2026-01-10T16:00:00Z", "status": "escalated"}],
 "ledger_counts": {"open": 3, "done": 9, "escalated": 1, "obsolete": 2}}
```

- `verdict`：`on_track` | `at_risk` | `off_track`（看板也认 `ok`/`warn`/`attention`/`problem`）。
- `ledger`：`open` 和 `escalated` 的条目，最多最近 30 条。
- 看板 `server.api_lead`（`/api/p/<pid>/lead`，也并在 `/api/p/<pid>/state` 的 `lead` 里）读 `<progress>/<pid>/lead/status.json`，
  把每条映射成 `{id, to, text, done_when, check_after, status}`（`text` 取 `what`，`check_after` 取 `check_at`），去掉已关闭的。
  `tests/test_lead.py::Cycle` 做了完整的来回校验。

## 配置

优先级：命令行 > 环境变量 > `config.json` > 默认值。

| 项 | config.json | 环境变量 | 默认 |
|---|---|---|---|
| 项目 | — | `LEAD_PROJECT`（或 `--project`） | `LEAD_ROOT` 下唯一一个带 config.json 的目录 |
| 根目录 | — | `LEAD_ROOT` | `~/.lead` |
| 状态目录 | — | `LEAD_HOME`（或 `--home`） | `<LEAD_ROOT>/<pid>` |
| 负责人终端 | `terminal` | — | 空（lead_wake 会新开） |
| 新开终端的工作区 | `worktree` | — | 空（不能新开） |
| 新开用的 agent | `agent` | — | `claude` |
| 唤醒间隔 | `interval_s` / `inbox_min_gap_s` / `stale_running_s` / `busy_alert_s` | — | 7200 / 600 / 10800 / 14400 |
| 本地进度目录（档案） | `progress_home` | `LEAD_PROGRESS_HOME`，再 `PROGRESS_HOME` | `~/.progress` |
| 看板主机 | `board_host` | `LEAD_BOARD_HOST` | 空 = 看板读本地 `progress_home` |
| 看板主机上的进度目录 | `board_progress_home` | `LEAD_BOARD_PROGRESS_HOME` | `.progress`（相对远端家目录） |
| QQ 发送配置 | `qq_conf` | `LEAD_QQ_CONF` | `<progress_home>/qq.json`（`{"target","owner"}`，和巡检共用） |
| Hermes | `hermes_bin` | `HERMES_BIN` | PATH 上的 `hermes`，再 `~/.local/bin/hermes` |
| Orca | `orca_bin` | `ORCA_BIN` | PATH 上的 `orca` |
| 会话里怎么调 `lead` | `lead_cmd` | — | `lead`（多项目时写 `lead --project <pid>`） |
| 唤醒提示 | `wake_prompt` | — | 内置；占位符 `{reason}` `{project}` `{boot}` `{lead}` |
| QQ 告警前缀 | `alert_prefix` | — | `[负责人]` |
| 显示时区 | `utc_offset_h` | `LEAD_UTC_OFFSET_H` | 本机时区 |
| 提交身份 | `git_name` / `git_email` | — | `lead` / `lead@localhost` |

lead-qq 插件的配置在 `<LEAD_ROOT>/lead-qq.json`（或 `LEAD_QQ_CONFIG` 指的文件），都可省略：
`project`、`prefixes`（默认 `["负责人", "@负责人", "lead"]`）、`ref_prefix`（默认 `L`，匹配 "L3 …"）、`wake_label`
（默认 `com.example.lead-wake`，必须和 launchd 的 Label 一致；`LEAD_WAKE_LABEL` 可覆盖）、`wake_cmd`（argv 列表，非 macOS 用它代替
`launchctl kickstart`）、`owner_conf`（默认 `<PROGRESS_HOME>/qq.json`，只取 `owner`）、`platform`（默认 `qqbot`）、`ack_text`
（默认"负责人收到（{id}），几分钟内处理，结果在 QQ 回你"）。只收 owner 在该平台上的私信；不匹配的消息原样交给 Hermes。

## 安装

1. 复制脚本（launchd 和 Hermes 网关读不了 `/Volumes` 下的文件，所以复制而不是链接）：
   ```sh
   mkdir -p ~/.lead/bin ~/.lead/log
   cp autotaskgen/lead.py autotaskgen/lead_wake.py ~/.lead/bin/
   printf '#!/bin/sh\nexec /usr/bin/python3 "$HOME/.lead/bin/lead.py" "$@"\n' > ~/.local/bin/lead && chmod +x ~/.local/bin/lead
   ```
2. 建状态目录并生成 BOOT.md，再手工补 `lead init` 列出的占位符：
   ```sh
   lead --project <pid> init --worktree <工作区路径> [--terminal <句柄>] [--board-host <主机>] \
        --template templates/lead/BOOT.md --set goal_doc=<目标文件> repo=<仓库> branch=<分支> tz=<时区> \
        kb_dir=<知识库目录> holdout_dir=<留出集路径> board_url=<看板地址> framework_contact=<框架维护会话>
   ```
   **看板（live.json）不在负责人这台机器上时必须给 `--board-host <ssh 主机>`**（或之后在 config.json 设 `board_host`）。
   不设时 glance 只读本机 `<progress_home>/<pid>/live.json`，读不到就没有在跑数，只有一行"读不到在跑快照"的提示；
   `lead init` 遇到这种情况会打一条 WARNING。（11-07：Mac 上的负责人换成仓库版后，因为没设这一项，glance 少了"现在"一节。）
3. launchd：复制 `autotaskgen/launchd/com.example.lead-wake.plist`，把 `YOUR_HOME`、`YOUR_PROJECT` 和 Label 换成自己的，
   放进 `~/Library/LaunchAgents/` 后 `launchctl bootstrap gui/$(id -u) <plist>`。先 `python3 ~/.lead/bin/lead_wake.py --project <pid> --force`
   手动试一次。
4. QQ（可选）：把 `autotaskgen/hermes_plugin/lead-qq/` 的两个文件复制到 `~/.hermes/plugins/lead-qq/`，在 Hermes 的
   `plugins.enabled` 里加上 `lead-qq`，写好 `<LEAD_ROOT>/lead-qq.json`（至少 `wake_label`），重启网关。发送用的
   `qq.json` 和巡检共用，没有就不发（`lead qq` 返回 `qq_unconfigured`，但照样记进收件箱）。

## 和框架其他部分的关系

| 部分 | 管什么 | 和负责人 |
|---|---|---|
| 巡检 patrol（脚本，每 15 分钟） | 确定性的修补：代派、自动继续、关终端、催 router、QQ 告警 | 负责人不重复做这些；巡检的结果进 live.json 和档案，负责人读数 |
| 节点级巡检员 supervisor（terra，每 30 分钟） | 看每条线的节点、屏幕，报 problem/attention | 负责人不看节点细节；它的结论通过档案的 `flags`、决策延迟进来 |
| 项目级巡检 project review | 每轮算一份档案（并发、空等、关键路径、开销…），第 3 档才给协调人发项目级建议 | 档案是负责人每轮的主要输入；负责人把档案里的数变成有落实标准、会回查的调整 |
| 协调人（每个核心一个） | 本核心的里程碑和线 | 负责人的主要收件人：每轮每人最多一条汇总消息；router 不动才越级 |
| router / worker | 节点 | 负责人不派节点 |
| 人 | 最终目标、花钱、对外、不可恢复的删除 | 负责人只在这四类事先问；其余先做后报，人可推翻 |

## 换成仓库里的版本

仓库版和 11-06 在 `~/.lead/bin/` 跑起来的那一份命令行、文件格式都兼容（台账、收件箱、cycle.json、status.json、wake.json 字段不变；
`ledger list`、`inbox list` 的输出逐字相同）。要切换：按"安装"复制脚本；在 config.json 里补上原来写死在代码里的值——
看板在另一台机器上就设 `board_host`，launchd Label 不是默认值就在 lead-qq.json 里设 `wake_label`，想保持原来的显示时区就设
`utc_offset_h`；plist 里加 `--project <pid>`。差别：`ledger add` 不再默认收件人，必须给 `--to`；没有 config.json 的状态目录要先 `lead init`。
