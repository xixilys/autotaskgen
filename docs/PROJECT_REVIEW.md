# 项目级巡检：怎么用（实现说明）

设计依据在 `PROJECT_REVIEW_SPEC.md`；本文只写实现：读什么、写什么、用哪些模型、什么时候跑、现在上到第几档、怎么退回。代码是 `autotaskgen/project_review.py`。

## 读什么

- 读：`events.jsonl`、`project.json`、`live.json`（走和巡检相同的 `snapshot.Store`，本地有就读本地，否则 ssh 读 WSL）。只读，不改这些文件。
- 节点级巡检员的台账（任务缓存里的 `supervisor:<项目>`）：只用来算决策延迟；最近两份巡检员报告：只用来判断"连续 2 轮报同一个 problem"。
- 开发分支的 `git log --numstat`：只在 `defaults.project_review.git_paths` 配了路径的线上读；没配就跳过并在档案里写明。
- 留出集名单：`~/.dse_holdout`（只在本机），用 `dream.Guard` 把名单变成哈希，档案文本逐行比对。

## 写什么

- `~/.progress/<项目>/project_review/`：`dossier-<时间>.md`（给模型的档案）、`dossier-<时间>.json`（同一份数的机读版，看板用）、`review-<时间>.json`（模型报告、合并结果、发送记录）。三类各留最近 40 份。
- 任务缓存 `~/.progress/cache/orca_tasks.json` 里的 `project_review:<项目>`：回查台账（`followups`）、待议（`pending`）、条目关闭时间（`criteria_ledger`）、上次运行时间和 `project.json` 的哈希。
- 第 3 档才会发消息：协调人终端（`orca terminal send`，和节点级巡检同一个函数）、QQ（`decide.send_qq`）。第 1、2 档不发任何东西。

## 模型

两个独立调用，并行，合计 20 分钟：

| 份 | 默认 | 环境变量覆盖（测试用） |
| --- | --- | --- |
| A | Opus（`decide.call_opus`） | `PROGRESS_REVIEW_A_CMD` |
| B | Astra（`decide.call_astra`） | `PROGRESS_REVIEW_B_CMD` |

提示词和 schema（`progress-project-review/v1`）在 `project_review.py` 的 `PROMPT`、`SCHEMA`，提示词与 spec §6 逐字一致。B 的提示词多一句 `family_note`。模型只读档案，不自己算数；`to` 的取值按项目的核心生成（`schema_for`）。

## 什么时候跑

`due()`：北京时间 02/08/14/20 点各一轮；另有四种情况立刻跑：有里程碑被确认或否决、`project.json` 变了、节点级巡检员最近两份报告里同一条线报了同一个 problem 类型。命令行 `--force` 无视这些。patrol 还没接线（本次不动 patrol.py）：现在手动或 cron 调用 `python3 project_review.py --project dse`，以后 patrol 调 `project_review.run(...)` 即可（发送函数、模型函数、git 读取函数都可注入）。

```
python3 project_review.py --project dse [--stage N] [--force] [--dry-run] [--print]
```

`--dry-run` 只建档案，不写文件、不改台账、不调模型、不发消息。`--print` 把档案正文打到标准输出。

## 上线档位（`defaults.project_review.stage`，默认 1）

1. 只出档案，放到看板上人工看一周，核对数对不对。**现在是这一档，已由 patrol --apply 接线运行**（见下节）。
2. 加单家模型（Opus），报告存盘，不发。
3. 双家合并并自动发送：两家一致 → 发给对应协调人并 QQ 通知；只有一家提 → 进待议，下一轮任意一家再提才发；目标、预算、对外、留出集或 `to=human`、`goal_draft` → 只进 QQ 人类摘要。回查沿用节点级巡检的台账规则，单位改成小时（6–48 夹紧），第一次没落实重发一次，第二次升级给人。

## patrol 接线（只有第 1 档）

- `patrol --apply` 每轮在所有确定性动作之后调一次 `project_review.run`，到点（02/08/14/20 点北京时间及触发条件）才真的出档案；整段最多 60 秒，超时或出错只在输出的 `project_review`（`{ran, ok, why, path, sync}`）里记一笔，不影响巡检自己的动作。不带 `--apply` 的干跑不调用，也不写任何文件。
- 档位 >= 2 在 patrol 里不运行（那一档要调模型，不能塞进 15 分钟一轮），只记 finding `project_review_stage_unwired`；手动用 `python3 project_review.py --stage N`。
- 档案出成功后，patrol 把最新的 `dossier-*.md/.json` scp 到 WSL 的 `~/.progress/<项目>/project_review/`（保留最近 10 份），看板读那里；接口 `/api/p/<项目>/project_review`，监督页有"项目级"面板。
- 命中留出集名单：QQ 告警 `project_review:holdout`（只写行数，不写词），之后一次成功运行才解除。
- 紧急停：`PROGRESS_PROJECT_REVIEW=off`（环境变量，测试用）或让 `project.defaults.project_review.stage` 大于 1。

## 可选配置（都放 `project.defaults.project_review`，缺了就跳过对应检查并在档案里写明）

| 键 | 含义 | 默认 |
| --- | --- | --- |
| `stage` | 上线档位 | 1 |
| `fast_milestones` | "快速方法"里程碑（算 S1 投入占比，低于 30% 标 ⚠） | 不配则跳过这项 |
| `workload_terms` | 负载名（只放开发集的），用于通用性泄漏占比 | spec §2.6 的名单 |
| `leak_lines` / `git_paths` | 查泄漏的线 / 每条线的 git 路径 | EVB、FE、SR / 不配则跳过 |
| `truth_milestones` | 列出"参照来源"字段的里程碑 | B7、B9 |
| `invest_window_h` | 投入分配的时间窗 | 72 |
| `default_est_h` | 里程碑没写 `est_hours` 时的估计工期 | 24 |

里程碑上的可选字段：`stage`、`criteria`（见 DESIGN.md 5.2）、`est_hours`、`keywords`（找"相关节点"用，除里程碑 id 外）。

## 留出集隔离

档案和提示词生成后，整段文本过 `dream.Guard.from_holdout()`：命中任何一处，整轮拒绝（返回 `holdout_hit`，不写文件、不调模型、不发消息），日志只写命中的行数，不写命中的词。要发出去的消息正文也各自过一遍。名单目录不存在时守卫是关的（返回里有 `guard_active`）。

## 退回

- 临时停：把 `defaults.project_review.stage` 设成 2 或更大，patrol 就不再调用（只记 `project_review_stage_unwired`）。
- 退一档：把 `defaults.project_review.stage` 改回 1 或 2；已发出的调整在台账里，到点回查，不会撤回。
- 清状态：删任务缓存里的 `project_review:<项目>` 键和 `~/.progress/<项目>/project_review/` 目录；不影响事件、看板和节点级巡检。
- 代码回退：`project_review.py` 是独立模块，`progress_core.py` 只多了两个可选字段的校验和一条警告，删掉模块不影响其余功能。

## Consumers (11-06)
- The project lead session (Claude, woken every 2 h by launchd `com.example.lead-wake`, or from QQ "负责人 …") reads the
  newest `dossier-*.json` each cycle: `headline`, `stages`, `parallel`, `idle`, `graph_audit`, `flags`. These field
  names and their first-level keys are locked by `tests/test_project_review.py::LeadContract`; tell the lead before
  changing them.
- The lead writes `~/.progress/<pid>/lead/status.json` (schema `lead-status/v1`) on WSL; the board serves it at
  `/api/p/<pid>/lead` and shows it in the "项目负责人" panel (verdict, summary, open ledger rows).
  The lead mechanism itself (CLI, wake job, QQ plugin, BOOT template) is in the repo; see `docs/LEAD.md`.
