#!/usr/bin/env python3
"""Patrol: scan folded progress + live snapshot, nudge routers, auto-dispatch planned nodes.

  python3 patrol.py [--project dse]            dry run: print planned actions as JSON
  python3 patrol.py [--project dse] --apply    perform them

Rules (design §7/§12):
- Global stop: $PROGRESS_HOME/PATROL_OFF (default ~/.progress/PATROL_OFF) exists -> zero actions.
- Only lines with `"patrol": true` in project.json (and open) are looked at.
- Auto-dispatch: node status ready (deps met per fold), auto_dispatch true, spec "branch@path"
  readable via `git show`, route with agent + on (+ model unless agent dsh), never dispatched
  before. The `auto_dispatch` event is written FIRST as a placeholder (deterministic id, so a
  second patrol gets a duplicate and stops); only then `orca orchestration worker-start` with
  the spec file text unchanged. Success -> `auto_dispatch` phase=started with refs. Failure,
  timeout or unreadable reply -> `node_block` blocker=env; no retry. The router is told either way.
- Backstop: node running but its Orca task finished > 20 min with no deliver/block -> finding `undelivered`, in the nudge.
- Auto-dispatch appends the progress-worker SPEC_SNIPPET (unless its marker is already in the spec).
- Nudge the line router (orca terminal send) for idle lines, stuck nodes, unreviewed deliveries;
  at most one per router per hour (counted from nudge events with reason "nudge").
- `worker_stalled` (snapshot saw the worker's screen in error/idle/exited for >= 10 min) and `overtime`
  (> 1.2 x expected) bypass that hourly limit; instead one nudge per node per stall (reason "stall" /
  "overtime", key per node), plus one reminder if still stalled 60 min later. They respect PATROL_OFF,
  the line's patrol flag, an absent router and a router waiting for input.
- Claimed milestones unconfirmed > 4h: remind the core coordinator, at most 2 per day.
- Empty line AND empty machine (open line, nothing running or ready, quiet > IDLE_S, worker host live.resources mem_avail_gb >
  IDLE_FREE_MEM_GB = 16): the idle nudge goes to the line's core coordinator instead of the router (finding `idle_to_coordinator`,
  nudge reason `idle_coord`, key `idle_coord:<line>`, at most one per NUDGE_GAP_S; a coordinator screen waiting for input is not
  messaged). Machine busy/unknown, ready nodes, or no coordinator terminal -> the router nudge as before.
- Project review (only --apply, stage 1 from project.defaults.project_review.stage): after execute() patrol calls
  project_review.run (due-check inside, 60 s cap, errors/timeouts only land in `project_review`); stage >= 2 is skipped with finding
  `project_review_stage_unwired`; a fresh dossier is scp'd to the board host's ~/.progress/<pid>/project_review/ (keep 10);
  holdout_hit -> QQ alert `project_review:holdout` (row count only). Dry run: `project_review` = {ran: false, reason: dry_run}.
- Everything else -> `observe` event (deduped: same key at most once per 6 h).
- Missing/stale live.json (>30 min) or WSL unknown -> no "process gone" conclusions.

Scheduler (FRAMEWORK §2, rule-based, see graph.py):
- One ready queue per line covering every depth, longest open downstream chain first (critical path).
- `max_parallel` (line, else project defaults, else none) is an optional CEILING on running worker nodes (run/review/check,
  imported dispatches not counted); extra ready nodes wait (finding `parallel_ceiling`), nobody is nudged.
- Only worker kinds are auto-dispatched; ready plan nodes go to the router like manual ready nodes.
- Decide panel (decide.py, only --apply): a ready `decide` node whose spec has >= 2 numbered options is judged by
  Opus + Astra in parallel (<= DECIDE_PER_ROUND = 2 panels/explanations per round; a node with a `decision`
  for its spec is never re-judged). Agreement -> `decision` + patrol `node_deliver` (decide nodes only, refs.card)
  + a QQ notice for every execution. Disagreement / needs_human / failed judge -> `decision` escalated or
  panel_failed + `node_block` (design) + a QQ decision card. Spec unreadable or no options -> router nudge as before.
- QQ replies (Hermes plugin progress-qq -> ~/.progress/inbox.jsonl) are applied every --apply round, each one
  logged as a `human_reply` event (refs.message_id marks it applied): `D<n> 选 k` on an escalated card decides it
  (human actor qq:<owner> if project.humans lists it, else patrol with refs.by); a different k or `推翻` on an agreed
  card -> `decision` overruled + router nudge; `为什么` -> one Opus explanation sent to QQ.
- ~/.progress/qq.json {"target","owner"} missing -> no QQ sends, finding `qq_unconfigured`. Dry run never runs a model or sends.
- Serial segment: at most one of its nodes running/dispatched at a time.
- Parent router woken (`expand`, once per signal) only on real parallelizable signals: a delivered/failed/blocked
  node reporting >= 2 distinct `failures` or >= 2 independent `candidates`, not yet acted on (no later proposal
  under that parent, no later node depending on it). Idle slots never wake anyone.
- Collapse: >= 2 siblings of one parallel segment with a shared failure signature, the same worker screen
  error, or overlapping `touched` paths -> finding `collapse`, message to the parent router (re-split or
  serialize, once per 6 h), and the segment's remaining ready nodes are held.
- Worker release (only --apply): node delivered after its latest dispatch, worker kind, worker handle known and
  not a router/coordinator, its Orca task finished in a fresh live.json -> `orca orchestration worker-release
  --dispatch <id> --json`; at most RELEASE_PER_ROUND per round, RELEASE_TRIES per dispatch; a
  `worker_release` event logs every attempt.
- Worker close (only --apply, `worker_close`, <= CLOSE_PER_ROUND = 6 per round): a live worker terminal that is not a
  router/coordinator, whose Orca task is settled (`lingering` true), not busy (spinner title) and silent for >=
  IDLE_CLOSE_MIN = 30 min (node mapped; its status must not be planned/ready/running/dispatched) or >= 120 min
  (no node mapping), and whose screen is readable and not mid-work, is closed. Routers dispatch into terminals they
  started; Orca keeps those `retained` and worker-release does nothing for them, so: worker-release first when the
  node's dispatch is still reclaimable, else (or when it is not released) `orca terminal close --environment <env>
  --terminal <h> --json`. Each attempt is a `worker_release` event (close=true); one terminal is tried at most 2
  times, then finding `close_failed` (and a QQ alert).
- QQ alerts (alerts.py, only --apply, one merged message per round): monitoring broken (live.json stale, a host
  "unknown", live.errors, >= half of the screen reads failing), a worker/router whose auto-continue is exhausted,
  close_failed, supervisor follow-ups addressed to "human" or to "tooling" without a handler and past check_at.
  First send at once, reminders 2/8/24 h after first_at (max 4 sends), "[已恢复]" once when the condition clears.
  Each send is an `observe` event (refs.key "alert:<key>"); result key `alerts`; planned set in `alerts_active`.
- Outage / stall alerts (11-06 night: 6.5 h without any event and nobody told): `outage:model` = >= 2 distinct agent screens
  (routers, coordinators, live workers) show a transient model/API error this round, or the supervisor's last 2 model calls
  failed; `stall:project` = some open line has running/ready nodes and no non-patrol event on ANY open line for
  STALL_PROJECT_MIN = 60 min. Both go through alerts.py and resolve with "[已恢复]".
- The supervisor (terra) never blocks the round: it runs last, after dispatch / continue / close / nudges / alerts; after a
  failed call (timeout or rc != 0) it is skipped for 60 min (supervisor {ran: false, reason: "backoff"}); TIMEOUT_S = 600.
- Disk: agents are nudged only about hosts where workers write (hosts of live worker terminals and of route `on`, in practice
  wsl; text "暂停新的大量写盘任务；清理由巡检负责"). Low/critical on any other host (the Mac) is a QQ alert `disk:<host>:<mount>`
  for the user, no agent nudge. For worker hosts the last 24 h of (time, avail_gb) per mount are kept in patrol.state.json
  (`disk_hist`, only with --apply); when the projected time to DISK_CRIT_GB is < 12 h -> finding `disk_fill_soon` + QQ alert
  `disk_fill:<host>:<mount>`.
- Strict worktree cleanup (only --apply, user-approved 11-06): worker worktrees under a `/workspaces/` directory (or
  $PROGRESS_WT_ROOT) that appear as refs.worktree ("<repoid>::<path>") of node_dispatch / auto_dispatch events are removed with
  `orca worktree rm --environment wsl --worktree path:<path> --json` (no --force, no --delete-branch) only if ALL hold:
  (1) every node that was dispatched into it is reviewed / superseded / cancelled / failed; (2) no event of any kind mentions the
  path or those nodes, and no dispatch, for IDLE_WT_H = 3 h (was 12 h until 11-06), and no file under it (find -maxdepth 3) is newer than that;
  (3) no live worker / router / coordinator entry and no line config mentions the path; (4) one ssh script per round proves
  `git status --porcelain --untracked-files=all` is empty, it is a linked worktree (not the main checkout), and HEAD is on a ref
  other than its own branch or its branch exists (checked before in the scan and again after the removal; a missing branch
  afterwards -> finding `worktree_branch_unverified` + QQ alert). `git -C` failing -> finding `worktree_broken` (count), never
  removed. (5) at most WT_RM_PER_ROUND = 20 per round, oldest first. Each removal is an `observe` event (refs.key
  "wt_rm:<path>") and one line "已清理 N 个已完成工作区，分支保留" in the router's notice; result key `wt_cleanup` (dry run lists
  candidates with verdict and reason and removes nothing). Needs a fresh live.json and wsl "ok".
- Resume pack (接续包, `progress-inspect resume`, progress_inspect.build_resume): every round (dry run too) patrol writes
  ~/.progress/<project>/resume/<line>.md for every declared line (atomic), so agents without the CLI can read it.
- Memory-loss nudge (only --apply, key `memory_loss` in findings): a router/coordinator terminal that changed (live entry has
  `rebound_from`, or its handle differs from the one seen last round) or whose context was compacted (screen mentions
  compact / Compacting / 压缩 newly, or the context gauge dropped >= 30 points: Claude "Context ███░░░ NN%" = used,
  Codex "NN% context left") gets one "[接续]" message telling it to run `progress-inspect resume --line L`. Nudge event
  reason "resume"; once per (handle, cause, hour), a changed terminal once per handle; respects absent / waiting-for-input.
  Last-seen handles and gauges live in ~/.progress/<project>/resume/seen.state.json (written only with --apply).
  An auto-continue sent to a worker whose node was dispatched to another terminal before also says to run
  `progress-inspect resume --line L --node N`.
- Metrics per patrolled line (`metrics`): ready-but-undispatched node-hours, critical-path stall minutes,
  review/run time ratio (> 0.2 -> observe `review_heavy`).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import pathlib
import re
import shlex
import sys
from typing import Any, Dict, List, Optional, Tuple

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import decide  # noqa: E402
import graph  # noqa: E402
import progress_core as core  # noqa: E402
import snapshot as snap  # noqa: E402
import alerts as qq_alerts  # noqa: E402
import progress_inspect as inspect_tool  # noqa: E402

IDLE_S = 2 * 3600
IDLE_FREE_MEM_GB = 16   # worker host with more available memory than this counts as "machine free"
UNREVIEWED_S = 2 * 3600
ESCALATE_UNREVIEWED_N = 3        # this many unreviewed deliveries (one older than UNREVIEWED_S) ...
ROUTER_QUIET_S = 45 * 60         # ... while the router wrote nothing this long -> tell the coordinator
CLAIM_S = 4 * 3600
LIVE_STALE_S = 30 * 60
NUDGE_GAP_S = 3600
REMIND_PER_DAY = 2
OBSERVE_GAP_S = 6 * 3600
GONE_GRACE_S = 20 * 60
STUCK_FACTOR = 1.2
STALL_S = 10 * 60
RES_GAP_S = {"low": 6 * 3600, "critical": 2 * 3600}
ABSENT_GAP_S = 2 * 3600
DEFAULT_EXPECTED = {"run": 120, "review": 30, "check": 30}
DEFAULT_MAX_PARALLEL = None   # no ceiling unless the line/project declares one (user 11-05)
COLLAPSE_GAP_S = 6 * 3600
RELEASE_PER_ROUND = 3
RELEASE_TRIES = 2
CLOSE_PER_ROUND = 6
CLOSE_TRIES = 2
IDLE_CLOSE_MIN = 30
IDLE_CLOSE_UNMAPPED_MIN = 120
OPEN_NODE_STATES = ("planned", "ready", "running", "dispatched")
RELEASE_MAX_AGE_H = 12   # fallback when live.reclaimable is missing: don't sweep tasks finished long ago
RELEASE_FINAL = ("released", "already_released", "release_pending", "retained")
STALL_REMIND_S = 60 * 60
STALL_FLAGS = ("error", "idle", "exited")
CONTINUE_TRIES = 2
CONTINUE_TEXT = "继续"
GAUGE_DROP = 30
RESUME_TEXT = ("[接续] 你可能刚换了终端或压缩了上下文。继续之前先运行 `progress-inspect resume --line %s`（或读 "
               "~/.progress/%s/resume/%s.md），核对未完成节点、最近裁决和未落实跟进，再接着排节点。")
UNDELIVERED_S = 20 * 60
READY_STALE_MIN = 45          # a ready node undispatched this long is listed to the router (ready_stale)
UNDISPATCHABLE_ROUNDS = 2     # same node+reason this many patrol rounds ...
UNDISPATCHABLE_MIN = 30       # ... or this many minutes -> QQ alert "undispatchable:<line>"
REJECT_MAX_TRIES = 3          # patrol's own worker-start attempts that Orca refused, in all
REJECT_RETRY_MIN = 60         # retry an unchanged argv only after this long
REJECT_MARK = "代派被 Orca 拒绝"
REJECT_RE = re.compile(r"requires|invalid|unknown (option|flag|argument)|usage:|not found|no such", re.I)
UNDISP_WHY = {"spec_unreadable": "spec 读不到", "decide_unready": "decide 不能交面板",
              "dispatch_rejected": REJECT_MARK}
STALL_PROJECT_MIN = 60        # no router/worker event on any open line this long (with running/ready nodes) -> QQ stall:project
OUTAGE_MIN_SCREENS = 2        # distinct agent screens with a transient model/API error -> QQ outage:model
OUTAGE_TERRA_FAILS = 2        # supervisor (terra) calls failed in a row -> QQ outage:model
DISK_HIST_H = 24              # hours of (time, avail_gb) kept per worker-host mount for the fill projection
DISK_FILL_H = 12              # projected hours until DISK_CRIT_GB below this -> disk_fill_soon
DISK_FILL_MIN_SPAN_H = 3.0    # a projection needs this much history (11-06: a 47-min burst of new worktrees projected "4.4 h" with 116 G free)
IDLE_WT_H = 3                 # worktree cleanup: untouched (events and files) this long. 11-06 08:20: 12 h kept >= 9 new
                              # worktrees/h x 3 G x 12 h ~ 320 G of finished worktrees with 77 G free; removal still needs a
                              # clean tree, all nodes finished, branch kept (and pushed to origin by deliver/review)
WT_RM_PER_ROUND = 20
WT_SCAN_MAX = 2 * WT_RM_PER_ROUND   # worktrees inspected over ssh per round
WT_SKIP_H = 24                # a worktree found broken / missing is not rescanned for this long (keeps scan slots free)
WT_HELD_H = 6                 # one scanned and kept (dirty, ignored files, recent files, HEAD only here) waits this long; 11-07:
                              # 16 dirty ones were rescanned every round and took the slots of 40 never-scanned ones (104 G)
WT_TERMINAL = ("reviewed", "superseded", "cancelled", "failed")
SNIPPET_MARKER = "<!-- progress-worker -->"
# Embedded copy of skills/progress-worker/SPEC_SNIPPET.md (the deployed ~/.progress/bin has no skills dir);
# a test keeps the two identical. Placeholders: {line} {node}.
SPEC_SNIPPET = """<!-- progress-worker -->
## 进度记录（必做，看板和巡检只读这份记录）
本节点：主线 `{line}`，节点 `{node}`。命令 `progress`（没有就用 `~/.progress/bin/progress`）。
- 开工前（以及中断、换终端、上下文压缩后）先运行 `progress-inspect resume --line {line} --node {node}` 读节点历史和本线经验；经验里写了的规则照做。
- 交付：**先**写 `progress deliver --line {line} --node {node} --outcome PASS|PARTIAL|FAIL|UNKNOWN --report <REPORT 路径> --head <git sha> --summary "一句白话"`，**再**给 router 发 worker_done；顺序不能反。
- `progress deliver` 会自动把本分支推到 GitHub origin（不推 main）：交付前先 `git commit`；输出里出现 `push_failed` 就按原因修好（或在交付消息里写明），不要原样反复重试。
- 交付时报并行信号（巡检据此叫 router 拆分或判定撞车）：`--failure "<错误类别或测试名>"`，写类别不写描述（如 `CG16_BINDING_MISSING`、`test_jacobi_single`，40 字以内），同一种失败在不同节点用同一个词，巡检才能归并；只写交付时仍没解决、还要有人动手的失败，每个不同的一条，已修好的不写；`--candidate "<候选>"`，只写还没做、彼此独立、能分给别人并行做的方向，每个一条，已经跑过的不写；`--touched <路径1,路径2>`，写实际改了的文件。没有就省略该项，`--touched` 只要改了文件就必须写。
- 不提前收工：预算没用完、剩下的验收条目也不靠别人的活、待定裁决或环境时，接着做完再交。只有剩下每一条都被外部挡住才交 PARTIAL，并在报告里逐条写挡在哪（缺谁的活／等哪个裁决／哪个环境）；能自己做的缺口不算挡住。deliver 会拒绝预算用不到三分之一就交的 PARTIAL/FAIL，除非加 `--blocked-by "挡在哪"` 写明外部挡住的是什么。要以"等裁定"为由交 PARTIAL 或写 block 之前，先重跑一次 `progress-inspect resume --line {line} --node {node}`，看协调人和 router 在你开工后有没有已经裁了（project_update）；已裁的按裁定接着做完，不算外部挡住。
- 卡住：`progress block --line {line} --node {node} --blocker dep|design|env|resource|human|other --summary "卡在哪"`，然后停下等指示。等别的节点或别的线交东西标 dep；等协调人或 router 裁定、批准、GO、预算标 design；human 只用于只有人本人能给的东西（真实端点、材料、目标决定），它会发到人的 QQ 上。收到继续的指示（GO、裁定、依赖到了）接着干时，先发一条 `progress heartbeat`，节点才会从"卡住"回到"在跑"；分阶段等 GO 的，每进一个阶段都发一条。
- 遇到无法继续的报错，先写 progress block 再停，不要停在报错画面上。
- 开工可选：`progress heartbeat --line {line} --node {node} --summary "在做什么"`。
- summary 不超过 120 字，写做成了什么，不写内部字段名。
- 命令返回 "queued": true 表示已进本地队列，会自动补发，不用重试；返回 "ok": false 按 code 改参数重写一次，仍失败就在交付消息里写明。
"""
DEFAULT_REPO = os.environ.get("PROGRESS_REPO", os.path.expanduser("~/project/repo"))


def off_file() -> pathlib.Path:
    return core.home() / "PATROL_OFF"


def actor() -> Dict[str, str]:
    return {"role": "patrol", "handle": os.environ.get("ORCA_TERMINAL_HANDLE") or "patrol@mac"}


def _age(now: _dt.datetime, at: Optional[str]) -> Optional[float]:
    t = core.parse_iso(at)
    return None if t is None else (now - t).total_seconds()


def _short(text: str, n: int = core.SUMMARY_MAX) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def repo_path(project: Dict[str, Any], line: Dict[str, Any]) -> str:
    return line.get("repo_path") or project.get("repo_path") or os.environ.get("PROGRESS_REPO") or DEFAULT_REPO


def read_spec(ref: Optional[str], repo: str, runner) -> Dict[str, Any]:
    """'branch@path' -> {text, header, branch} or {error}."""
    if not ref or "@" not in ref:
        return {"error": "spec 不是 分支@路径：%r" % ref}
    branch, path = ref.split("@", 1)
    if not branch or not path:
        return {"error": "spec 不是 分支@路径：%r" % ref}
    rc, out, err = runner(["git", "-C", repo, "show", "%s:%s" % (branch, path)], timeout=30)
    source = "git"
    pinned = re.fullmatch(r"[0-9a-f]{7,40}", branch) is not None
    if (rc != 0 or not out.strip()) and (branch == os.environ.get("PROGRESS_REPO_BRANCH") or pinned) \
            and "not a git repository" in (err or ""):
        # launchd cannot read a git dir on another volume (macOS privacy); the worktree itself is readable and
        # is checked out on PROGRESS_REPO_BRANCH, so read the committed-or-pending file from it. Routers pin
        # frozen specs to a commit sha on that branch (11-06: ten EVB nodes stuck ready because of this).
        try:
            out, rc, source = pathlib.Path(repo, path).read_text(encoding="utf-8"), 0, "worktree"
        except (OSError, ValueError):
            pass
    remote_repo = os.environ.get("PROGRESS_REMOTE_REPO")
    if (rc != 0 or not out.strip()) and remote_repo:
        # the branch may exist only in the worker host's repo (11-06: FE specs on you/fe-dev, a branch the Mac
        # repo does not have, left three FE nodes undispatchable)
        rc2, out2, err2 = runner(snap.ssh_argv(snap.remote_host(), "git -C %s show %s" % (
            shlex.quote(remote_repo), shlex.quote("%s:%s" % (branch, path)))), timeout=30)
        if rc2 == 0 and out2.strip():
            rc, out, source = 0, out2, "remote"
        else:
            err = "%s；%s 上也读不到：%s" % ((err or "").strip()[:80], snap.remote_host(), (err2 or "empty").strip()[:60])
    if rc != 0 or not out.strip():
        return {"error": "读不到 spec %s：%s" % (ref, (err or "empty").strip()[:120])}
    header = {}
    first = out.lstrip().splitlines()[0] if out.strip() else ""
    if first.startswith("{"):
        try:
            header = json.loads(first)
        except ValueError:
            header = {}
    return {"text": out, "header": header if isinstance(header, dict) else {}, "branch": branch, "source": source}


# 11-07 router rules R6 (trial): daily development defaults to Terra max. A Terra route with no effort gets max
# (codex's own Terra default is low); an explicit effort is kept. A route with no model still is not dispatched.
DEFAULT_EFFORT = {"gpt-6.1-terra": "max"}


# R6+R7 (11-07, trial): development ("run") nodes planned on Terra alternate Terra max / DS max (dsh agent, model fixed
# in the dsh profile) per line in dispatch order; a rework goes to the model not tried yet, then to Sol high.
# DS joins only when project.defaults.ds_worker = {"enabled": true, "on": "wsl"} (set once a probe proved it runs).
TERRA = "gpt-6.1-terra"
SOL_REWORK = {"agent": "codex", "model": "gpt-6.1-sol", "effort": "high"}
REWORK_RE = re.compile(r"(?:[-_](?:repair|resume|recheck|retry|fix|rework|redo|followup|r\d+))+(?:[-_]?\d+)*$")


def _family(route: Optional[Dict[str, Any]]) -> Optional[str]:
    r = route or {}
    if r.get("agent") == "dsh":
        return "ds"
    m = str(r.get("model") or "").split("/")[-1]
    return "terra" if m == TERRA else ("sol" if m.startswith("gpt-6.1-sol") else None)


def _attempt_routes(events: List[Dict[str, Any]], nodes: Dict[str, Any], lid: str, nid: str) -> List[str]:
    """Families of earlier real attempts at this node or its stem (a dispatch later delivered or reviewed)."""
    stem = REWORK_RE.sub("", nid)
    out: List[Tuple[str, str]] = []
    for i, e in enumerate(events):
        if e.get("line") != lid or e.get("kind") not in ("node_dispatch", "auto_dispatch") or e.get("phase") == "started":
            continue
        x = e.get("node") or ""
        if REWORK_RE.sub("", x) != stem:
            continue
        if not any(f.get("node") == x and f.get("line") == lid and f.get("kind") in ("node_deliver", "node_review")
                   for f in events[i + 1:]):
            continue
        fam = _family(e.get("route")) or _family((nodes.get(x) or {}).get("route"))
        if fam:
            out.append((e.get("at") or "", fam))
    return [f for _, f in sorted(out)]


def dev_route(project: Dict[str, Any], events: List[Dict[str, Any]], nodes: Dict[str, Any], lid: str, nid: str,
              n: Dict[str, Any], route: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[str]]:
    """-> (route, why). Only development nodes the router planned on Terra (or on DS) are re-routed."""
    if (n.get("kind") or core.infer_node_kind(nid)) != "run" or _family(route) not in ("terra", "ds"):
        return route, None
    ds_conf = (project.get("defaults") or {}).get("ds_worker") or {}
    ds_on = bool(ds_conf.get("enabled"))
    on = route.get("on") or ds_conf.get("on") or "wsl"
    terra = {"agent": "codex", "model": route["model"] if _family(route) == "terra" else TERRA, "effort": "max", "on": on}
    ds = {"agent": "dsh", "on": ds_conf.get("on") or on}
    tried = _attempt_routes(events, nodes, lid, nid)
    if tried:
        for fam, r in (("terra", terra), ("ds", ds)):
            if fam not in tried and (fam != "ds" or ds_on):
                return r, "rework_other_model"
        return dict(SOL_REWORK, on=on), "rework_sol_high"
    if not ds_on:
        return terra, "terra_max_ds_off"
    last = None
    for e in reversed(events):
        if e.get("line") == lid and e.get("kind") in ("node_dispatch", "auto_dispatch") and e.get("phase") != "started":
            f = _family(e.get("route")) or _family((nodes.get(e.get("node")) or {}).get("route"))
            if f in ("terra", "ds") and (nodes.get(e.get("node")) or {}).get("kind", "run") == "run":
                last = f
                break
    return (ds, "alternate_ds") if last == "terra" else (terra, "alternate_terra")


def resolve_route(node: Dict[str, Any], header: Dict[str, Any]) -> Dict[str, Any]:
    route = dict(header.get("route") or {}) if isinstance(header.get("route"), dict) else {}
    route.update({k: v for k, v in (node.get("route") or {}).items() if v})
    if route.get("model") and not route.get("effort"):
        eff = DEFAULT_EFFORT.get(str(route["model"]).split("/")[-1])
        if eff:
            route["effort"] = eff
    return route


def route_missing(route: Dict[str, Any]) -> List[str]:
    miss = [k for k in ("agent", "on") if not route.get(k)]
    if route.get("agent") != "dsh" and not route.get("model"):
        miss.append("model")
    return miss


def with_snippet(spec_text: str, lid: str, nid: str) -> str:
    """Append the progress-worker snippet unless the spec already carries its marker line."""
    if SNIPPET_MARKER in spec_text:
        return spec_text
    snippet = SPEC_SNIPPET.replace("{line}", lid).replace("{node}", nid)
    return spec_text.rstrip("\n") + "\n\n" + snippet


def worktree_name(lid: str, nid: str, now: _dt.datetime) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", ("%s-%s" % (lid, nid)).lower()).strip("-")[:60]
    return "%s-p%s" % (base, now.strftime("%m%d%H%M"))


def line_repo(project: Dict[str, Any], line: Dict[str, Any]) -> Optional[str]:
    """Orca repo for worker-start: the line's own, else the project's, else the one repo every line that names
    one agrees on (11-06: EVB had none, Orca rejected all ten auto-dispatches with 'requires --repo')."""
    if line.get("repo"):
        return line["repo"]
    if project.get("repo"):
        return project["repo"]
    named = {ln.get("repo") for ln in project.get("lines") or [] if ln.get("repo")}
    return named.pop() if len(named) == 1 else None


def start_argv(spec_text: str, route: Dict[str, Any], line: Dict[str, Any], node: Dict[str, Any],
               base: str, name: str) -> List[str]:
    argv = [snap.orca_bin(), "orchestration", "worker-start", "--spec", spec_text,
            "--agent", route["agent"]]
    if route.get("agent") != "dsh":
        argv += ["--model", route["model"]]
        if route.get("effort"):
            argv += ["--effort", route["effort"]]
    argv += ["--on", route["on"], "--worktree", "new-top-level", "--base-branch", base, "--name", name,
             "--task-title", _short(node.get("title") or node["id"], 80), "--timeout-ms", "180000", "--json"]
    if line.get("run"):
        argv += ["--run", line["run"]]
    if line.get("router"):
        argv += ["--from", line["router"]]
    if line.get("repo"):
        argv += ["--repo", line["repo"]]
    return argv


def parse_start(rc: int, out: str) -> Dict[str, Any]:
    doc = snap.parse_json_tail(out)
    if not isinstance(doc, dict):
        return {"ok": False, "why": "worker-start 回执无法解析（rc=%s），worker 可能已起" % rc, "unknown": True}
    res = doc.get("result") if isinstance(doc.get("result"), dict) else doc
    dispatch = res.get("dispatchId")
    state = res.get("state")
    if rc != 0 or not dispatch or state not in ("ready", None):
        stage = res.get("failedStage") or res.get("stage")
        err = doc.get("error")
        if isinstance(err, dict):
            err = err.get("message") or err.get("code")
        return {"ok": False, "unknown": state == "outcome_unknown" or not dispatch,
                "why": "worker-start 失败 rc=%s state=%s stage=%s %s" % (rc, state, stage, err or "")}
    terminal = worktree = None
    for e in res.get("effects") or []:
        if not isinstance(e, dict):
            continue
        if e.get("kind") == "terminal" and e.get("id"):
            terminal = terminal or e["id"]
        if e.get("kind") == "worktree" and e.get("id"):
            worktree = worktree or e["id"]
    return {"ok": True, "dispatch": dispatch, "task": res.get("taskId"), "terminal": terminal,
            "worktree": worktree}


def live_usable(live: Optional[Dict[str, Any]], now: _dt.datetime) -> Optional[str]:
    """None when usable for process conclusions, else the reason."""
    if not isinstance(live, dict):
        return "live_missing"
    age = _age(now, live.get("generated_at"))
    if age is None:
        return "live_no_time"
    if age > LIVE_STALE_S:
        return "live_stale"
    if (live.get("hosts") or {}).get("wsl") != "ok" or not isinstance(live.get("workers"), list):
        return "wsl_unknown"
    return None


def _on_wsl(node: Dict[str, Any]) -> bool:
    on = (node.get("route") or {}).get("on")
    wt = snap._ws_path(node.get("worktree"))
    return (on in (None, "wsl")) and not wt.startswith("/Users/")


def _dispatch_imported(n):
    """True when the node's latest dispatch came from the one-off import, not a live router/patrol."""
    for h in reversed(n.get("history") or []):
        if h.get("kind") in ("node_dispatch", "auto_dispatch"):
            return h.get("source") == "import"
    return False


def _has_worker(live: Dict[str, Any], lid: str, nid: str, node: Dict[str, Any]) -> bool:
    for w in live.get("workers") or []:
        if (w.get("line") == lid and w.get("node") == nid) or core._same(w.get("terminal"), node.get("worker")) \
                or snap.ws_match(w.get("workspace"), node.get("worktree")):
            return True
    return False


def _undelivered_min(n: Dict[str, Any], live: Optional[Dict[str, Any]], now: _dt.datetime) -> Optional[int]:
    """Minutes since the node's Orca task finished, when that is > UNDELIVERED_S and the node has no
    deliver/block after its latest dispatch; else None. Needs a fresh live.json carrying `tasks`."""
    if not isinstance(live, dict) or n.get("status") not in ("dispatched", "running"):
        return None
    la = _age(now, live.get("generated_at"))
    if la is None or la > LIVE_STALE_S:
        return None
    tid = (n.get("refs") or {}).get("task")
    t = (live.get("tasks") or {}).get(tid) if tid else None
    if not isinstance(t, dict) or not snap.is_finished(t.get("state")):
        return None
    done = _age(now, t.get("completed_at"))
    if done is None or done <= UNDELIVERED_S:
        return None
    since = n.get("dispatched_at") or ""
    for h in n.get("history") or []:
        if h.get("kind") in ("node_deliver", "node_block") and (h.get("at") or "") >= since:
            return None
    return int(done // 60)


def expected_minutes(project: Dict[str, Any], n: Dict[str, Any], header: Optional[Dict[str, Any]] = None) -> Optional[float]:
    """Node's own estimate (plan event, else spec header), else project.json defaults.expected_minutes[kind],
    else the built-in per-kind default."""
    for v in (n.get("expected_duration_minutes"), (header or {}).get("expected_duration_minutes")):
        try:
            if v is not None and float(v) > 0:
                return float(v)
        except (TypeError, ValueError):
            pass
    kind = n.get("kind") or core.infer_node_kind(n.get("id"))
    d = ((project.get("defaults") or {}).get("expected_minutes") or {})
    for src in (d, DEFAULT_EXPECTED):
        try:
            v = src.get(kind)
            if v is not None and float(v) > 0:
                return float(v)
        except (TypeError, ValueError, AttributeError):
            pass
    return None


def _worker_screen(live: Optional[Dict[str, Any]], lid: str, nid: str, n: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The stalled-looking screen block of this node's live worker, or None."""
    for w in (live or {}).get("workers") or []:
        if (w.get("line") == lid and w.get("node") == nid) or core._same(w.get("terminal"), n.get("worker")):
            sc = w.get("screen")
            if isinstance(sc, dict) and sc.get("flag") in STALL_FLAGS:
                return sc
    return None


def _alert_due(nudges, reason: str, key: str, since: Optional[str], now: _dt.datetime) -> bool:
    """One nudge per key since `since`; one more reminder once the last one is STALL_REMIND_S old."""
    sent = sorted((e.get("at") or "") for e in nudges if e.get("reason") == reason
                  and (e.get("refs") or {}).get("key") == key and (e.get("at") or "") >= (since or ""))
    if not sent:
        return True
    return len(sent) == 1 and (_age(now, sent[-1]) or 0) >= STALL_REMIND_S


def _repeat_due(nudges, reason: str, key: str, gap_s: float, now: _dt.datetime) -> bool:
    """True when no nudge with this reason+key exists, or the newest one is >= gap_s old."""
    sent = [e.get("at") or "" for e in nudges if e.get("reason") == reason and (e.get("refs") or {}).get("key") == key]
    return not sent or (_age(now, max(sent)) or 0) >= gap_s


def _patrol_events(events, kind=None):
    for e in events:
        if (e.get("actor") or {}).get("role") == "patrol" and (kind is None or e.get("kind") == kind):
            yield e


# ------------------------------------------------------------ scheduler helpers (FRAMEWORK §2)

def max_parallel(project: Dict[str, Any], line: Dict[str, Any]) -> Optional[int]:
    for v in (line.get("max_parallel"), (project.get("defaults") or {}).get("max_parallel")):
        if isinstance(v, int) and not isinstance(v, bool) and v >= 1:
            return v
    return DEFAULT_MAX_PARALLEL


def _is_worker_kind(n: Dict[str, Any]) -> bool:
    return (n.get("kind") or core.infer_node_kind(n.get("id"))) in graph.WORKER_TYPES


def _router_handles(project: Dict[str, Any], live: Optional[Dict[str, Any]]) -> set:
    """Every router / coordinator handle we know of: declared, live (incl. rebound), router tree."""
    hs = {ln.get("router") for ln in project.get("lines") or []}
    hs |= {c.get("coordinator") for c in project.get("cores") or []}
    hs |= set(project.get("humans") or [])
    for key in ("routers", "coordinators"):
        for r in ((live or {}).get(key) or {}).values():
            if isinstance(r, dict):
                hs.add(r.get("handle"))
                hs.add(r.get("rebound_from"))
    for t in (live or {}).get("router_tree") or []:
        if isinstance(t, dict):
            hs.add(t.get("handle"))
    return {h for h in hs if h}


def _is_router_handle(handle: Optional[str], routers: set) -> bool:
    return any(core._same(handle, h) for h in routers)


def parent_router(line: Dict[str, Any], n: Dict[str, Any], nodes: Dict[str, Any],
                  live: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    """(handle, label) of the router owning node n: the sub-router running its parent plan node when that
    terminal is present in the live router tree, else the line router (rebound handle when the snapshot has one)."""
    pn = nodes.get(n.get("parent") or "") or {}
    sub = pn.get("worker")
    if sub:
        for t in (live or {}).get("router_tree") or []:
            if isinstance(t, dict) and core._same(t.get("handle"), sub) and t.get("present") is True:
                return t["handle"], "sub"
    h = (((live or {}).get("routers") or {}).get(line["id"]) or {}).get("handle") or line.get("router")
    return h, "line"


def _task_finished(n: Dict[str, Any], live: Optional[Dict[str, Any]]) -> bool:
    """The node's Orca task is known finished: its worker no longer holds a slot (delivery may still be due)."""
    tid = (n.get("refs") or {}).get("task")
    t = ((live or {}).get("tasks") or {}).get(tid) if tid else None
    return isinstance(t, dict) and snap.is_finished(t.get("state"))


def _distinct(items) -> List[str]:
    out, seen = [], set()
    for x in items or []:
        k = graph.norm_signature(x)
        if k and k not in seen:
            seen.add(k)
            out.append(str(x))
    return out


def expansion_signals(nodes: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Real parallelizable work (FRAMEWORK §2): >= 2 distinct failures or >= 2 candidates reported at delivery /
    block, not acted on yet (no later node, and no review of that delivery: a reviewing router has already decided). -> [{node, kind, items, at}]"""
    out = []
    for nid, n in sorted(nodes.items()):
        if n.get("status") not in ("delivered", "reviewed", "failed", "blocked"):
            continue
        at = n.get("delivered_at") if n.get("status") != "blocked" else n.get("blocked_at")
        at = at or n.get("updated_at") or ""
        acted = False
        for m in nodes.values():
            if m is n:
                continue
            if (m.get("planned_at") or "") > at and (nid in (m.get("deps") or []) or (
                    m.get("proposal") and m.get("parent") == n.get("parent"))):
                acted = True
                break
        if acted:
            continue
        if any(h.get("kind") == "node_review" and (h.get("at") or "") >= at for h in n.get("history") or []):
            continue  # the router already reviewed this delivery and decided (11-06: 6 hints declined as "不拆")
        for kind, key in (("failures", "failures"), ("candidates", "candidates")):
            items = _distinct(n.get(key))
            if len(items) >= 2:
                out.append({"node": nid, "kind": kind, "items": items, "at": at})
    return out


def find_collapses(nodes: Dict[str, Any], live: Optional[Dict[str, Any]], lid: str) -> List[Dict[str, Any]]:
    """Parallel siblings that turned out not to be independent. -> [{segment, nodes, why[], parent}]"""
    groups: Dict[Any, List[str]] = {}
    for nid, n in nodes.items():
        if n.get("segment_mode") == "parallel" and n.get("status") not in ("superseded", "cancelled"):
            groups.setdefault(graph.segment_key(n), []).append(nid)
    out = []
    for key, ids in sorted(groups.items(), key=lambda kv: str(kv[0])):
        if key is None or len(ids) < 2 or not any(nodes[i].get("status") in graph.OPEN_STATES for i in ids):
            continue  # nothing left to hold or re-split
        ids = sorted(ids)
        involved, why = set(), []
        sigs = {i: {graph.norm_signature(f) for f in nodes[i].get("failures") or []} - {""} for i in ids}
        screens = {}
        for i in ids:
            if nodes[i].get("status") == "running":
                sc = _worker_screen(live, lid, i, nodes[i])
                if sc and sc.get("flag") == "error" and sc.get("line"):
                    screens[i] = graph.norm_signature(sc["line"])
        touched = {i: [p for p in (graph.norm_path(x) for x in nodes[i].get("touched") or []) if p is not None]
                   for i in ids}
        for x in range(len(ids)):
            for y in range(x + 1, len(ids)):
                a, b = ids[x], ids[y]
                common = sigs[a] & sigs[b]
                if common:
                    involved |= {a, b}
                    why.append("%s 和 %s 撞上同一个失败（%s）" % (a, b, sorted(common)[0][:60]))
                if a in screens and screens.get(a) == screens.get(b):
                    involved |= {a, b}
                    why.append("%s 和 %s 的 worker 屏幕报同一个错" % (a, b))
                ov = graph.scope_overlaps(touched[a], touched[b])
                if ov:
                    involved |= {a, b}
                    why.append("%s 和 %s 改了同一处（%s）" % (a, b, ov[0][0] or ov[0][1] or "<整个仓库>"))
        if involved:
            out.append({"segment": key, "nodes": sorted(involved), "why": why,
                        "parent": nodes[ids[0]].get("parent")})
    return out


def release_candidates(project: Dict[str, Any], line: Dict[str, Any], nodes: Dict[str, Any],
                       live: Optional[Dict[str, Any]], now: _dt.datetime, routers: set) -> List[Dict[str, Any]]:
    """Delivered worker nodes whose Orca dispatch is settled and whose terminal may be released."""
    if live_usable(live, now) not in (None, "wsl_unknown"):
        return []
    out = []
    for nid, n in sorted(nodes.items()):
        if n.get("status") not in ("delivered", "reviewed", "failed") or not _is_worker_kind(n):
            continue
        if graph.dispatch_imported(n) or not graph.delivered_after_dispatch(n):
            continue
        refs = n.get("refs") or {}
        dispatch, tid, handle = refs.get("dispatch"), refs.get("task"), n.get("worker")
        if not dispatch or not handle or _is_router_handle(handle, routers):
            continue
        t = ((live or {}).get("tasks") or {}).get(tid) if tid else None
        if not isinstance(t, dict) or not snap.is_finished(t.get("state")):
            continue
        reclaimable = (live or {}).get("reclaimable")
        if isinstance(reclaimable, list):
            if dispatch not in reclaimable:
                continue   # Orca says this terminal is already released / retained / still active
        else:
            done_at = core.parse_iso(t.get("completed_at"))
            if done_at and (now - done_at).total_seconds() > RELEASE_MAX_AGE_H * 3600:
                continue
        tries = [r for r in n.get("releases") or [] if r.get("dispatch") == dispatch]
        if any(r.get("final") for r in tries) or len(tries) >= RELEASE_TRIES:
            continue
        out.append({"type": "worker_release", "line": line["id"], "node": nid, "dispatch": dispatch,
                    "terminal": handle, "try": len(tries) + 1})
    return out


def _screen_working(tail: List[str]) -> bool:
    """The last lines of a terminal show work in progress (spinner glyph, Working, esc to interrupt)."""
    lines = [l.strip() for l in (tail or []) if isinstance(l, str) and l.strip()][-6:]
    for l in lines:
        low = l.lower()
        if "esc to interrupt" in low or "working" in low or any(0x2800 <= ord(c) <= 0x28FF for c in l):
            return True
    return False


def close_candidates(project: Dict[str, Any], state: Dict[str, Any], live: Optional[Dict[str, Any]],
                     now: _dt.datetime, routers: set, events: List[Dict[str, Any]], runner, apply: bool,
                     skip: set) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Idle worker terminals whose Orca task is settled -> (close actions, close_failed rows, findings).
    The screen read (only with `apply`) is the last gate; failed rows come from the event log and need no read."""
    if not isinstance(live, dict) or live_usable(live, now) not in (None, "wsl_unknown") \
            or not isinstance(live.get("workers"), list):
        return [], [], []
    plines = {l.get("id"): l for l in project.get("lines") or []}
    first_line = next(iter(plines), None)
    reclaimable = live.get("reclaimable")
    acts: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    findings: List[Dict[str, Any]] = []
    limited = 0
    for w in live["workers"]:
        h = w.get("terminal")
        if not h or _is_router_handle(h, routers) or w.get("lingering") is not True or w.get("busy") is True:
            continue
        lo = core.parse_iso(w.get("last_output_at"))
        if lo is None:
            continue
        idle_min = int((now - lo).total_seconds() // 60)
        lid, nid = w.get("line"), w.get("node")
        ls = state["lines"].get(lid) or {} if lid else {}
        n = (ls.get("nodes") or {}).get(nid) if nid else None
        if n is not None:
            if n.get("status") in OPEN_NODE_STATES or idle_min < IDLE_CLOSE_MIN:
                continue
            pl = plines.get(lid) or {}
            if pl.get("patrol") is not True or not ls.get("open", True):
                continue
        elif idle_min < IDLE_CLOSE_UNMAPPED_MIN:
            continue
        tries = [e for e in events if e.get("kind") == "worker_release" and e.get("close")
                 and core._same((e.get("refs") or {}).get("terminal"), h)]
        label = nid if n is not None else "未归属"
        if len(tries) >= CLOSE_TRIES:
            failed.append({"terminal": h, "node": nid if n is not None else None, "line": lid, "tries": len(tries),
                           "label": label})
            findings.append({"code": "close_failed", "terminal": h, "node": nid, "tries": len(tries)})
            continue
        if not apply or h in skip:
            continue
        if len(acts) >= CLOSE_PER_ROUND:
            limited += 1
            continue
        tail = snap.read_terminal_tail(runner, h, live)
        if tail is None or _screen_working(tail):
            continue
        dispatch = None
        if n is not None and not graph.dispatch_imported(n):
            d = (n.get("refs") or {}).get("dispatch")
            prior = [r for r in n.get("releases") or [] if r.get("dispatch") == d]
            if d and not any(r.get("released") or r.get("final") for r in prior) \
                    and (not isinstance(reclaimable, list) or d in reclaimable):
                dispatch = d
        line_id = lid if lid in plines else first_line
        if not line_id:
            continue
        acts.append({"type": "worker_close", "line": line_id, "node": nid if n is not None else None,
                     "dispatch": dispatch, "terminal": h, "env": snap.env_for_terminal(h, live),
                     "idle_min": idle_min, "try": len(tries) + 1, "label": label})
    if limited:
        findings.append({"code": "close_rate_limited", "count": limited})
    return acts, failed, findings


def parse_release(rc: int, out: str) -> Dict[str, Any]:
    doc = snap.parse_json_tail(out)
    status = None

    def walk(v, depth=0):
        nonlocal status
        if status or depth > 4:
            return
        if isinstance(v, str) and v in RELEASE_FINAL + ("release_unknown",):
            status = v
        elif isinstance(v, dict):
            for k in ("status", "release", "releaseStatus", "terminalState", "state", "outcome", "result"):
                if k in v:
                    walk(v[k], depth + 1)
        elif isinstance(v, list):
            for x in v:
                walk(x, depth + 1)

    walk(doc)
    if status is None:
        status = "release_unknown" if rc != 0 else ("released" if isinstance(doc, dict) and doc.get("ok") else "unparsed")
    return {"status": status, "released": status in ("released", "already_released", "release_pending"),
            "final": status in RELEASE_FINAL}


# ------------------------------------------------------------ auto continue (script layer, no model)

def _line_for(project: Dict[str, Any], hint_line: Optional[str], hint_core: Optional[str]) -> Optional[str]:
    if hint_line and core.line_of(project, hint_line):
        return hint_line
    for ln in project.get("lines") or []:
        if hint_core and ln.get("core") == hint_core:
            return ln["id"]
    lines = project.get("lines") or []
    return lines[0]["id"] if lines else None


def continue_targets(project: Dict[str, Any], live: Optional[Dict[str, Any]], runner) -> List[Dict[str, Any]]:
    """Terminals sitting at their input prompt with a transient error on screen: every registered router and
    coordinator (screen from the snapshot, else read now) and every live worker (screen from the snapshot).
    -> [{handle, role, line, since, text, node}]"""
    out: List[Dict[str, Any]] = []
    seen = set()
    rlive = (live or {}).get("routers") or {}
    clive = (live or {}).get("coordinators") or {}
    regs = [(ln["id"], ln.get("router"), ln["id"], None, rlive.get(ln["id"])) for ln in project.get("lines") or []]
    regs += [(str(c.get("id")), c.get("coordinator"), None, c.get("id"), clive.get(str(c.get("id"))))
             for c in project.get("cores") or []]
    for rkey, handle, lid, cid, info in regs:
        if not handle or handle in seen:
            continue
        seen.add(handle)
        info = info if isinstance(info, dict) else {}
        if info.get("present") is False:
            continue
        sc = info.get("screen") if isinstance(info.get("screen"), dict) else None
        if sc is None:
            tail = snap.read_terminal_tail(runner, info.get("handle") or handle, live)
            if tail is None:
                continue
            sig = snap.screen_signals(tail)
            sc = {"transient": sig["transient"], "prompt": sig["prompt"], "line": sig["line"],
                  "since": hashlib.sha1(sig["line"].encode("utf-8", "replace")).hexdigest()[:10]}
        if sc.get("transient") and sc.get("prompt"):
            out.append({"handle": info.get("handle") or handle, "role": "router" if lid else "coordinator",
                        "line": _line_for(project, lid, cid or None), "since": sc.get("since") or "", "text": sc.get("line"),
                        "node": None})
    for w in (live or {}).get("workers") or []:
        handle, sc = w.get("terminal"), w.get("screen")
        if not handle or handle in seen or not isinstance(sc, dict):
            continue
        seen.add(handle)
        if sc.get("flag") == "error" and sc.get("transient") and sc.get("prompt"):
            out.append({"handle": handle, "role": "worker", "line": _line_for(project, w.get("line"), w.get("core")),
                        "since": sc.get("since") or "", "text": sc.get("line"), "node": w.get("node"),
                        "env": snap.env_for_terminal(handle, live)})
    return out


def continue_key(t: Dict[str, Any]) -> str:
    return "auto_continue:%s:%s" % (t["handle"], t["since"])


def continue_tries(events, key: str) -> int:
    return sum(1 for e in _patrol_events(events, "nudge")
               if e.get("reason") == "auto_continue" and (e.get("refs") or {}).get("key") == key)


def monitoring_alerts(live: Optional[Dict[str, Any]], live_bad: Optional[str]) -> Dict[str, str]:
    """QQ alert keys for 'the patrol cannot see': stale/missing live.json, unknown hosts, snapshot errors,
    screen reads failing."""
    out: Dict[str, str] = {}
    if live_bad in ("live_missing", "live_no_time", "live_stale"):
        out["monitor:live"] = ("在跑快照（live.json）%s，巡检看不到 worker 和 router 的最新状态，"
                               "卡住的 worker 不会被自动继续" % (
                                   "超过 30 分钟没更新" if live_bad == "live_stale" else "缺失或没有时间"))
    if not isinstance(live, dict):
        return out
    for h, v in sorted((live.get("hosts") or {}).items()):
        if v == "unknown" and h != "ib-agent":   # ib-agent is always "unknown" (not monitored)
            out["monitor:host:%s" % h] = "巡检连不上 %s（状态未知），那台机器上的 worker 看不到，不会被自动继续或关闭" % h
    for e in live.get("errors") or []:
        if isinstance(e, dict) and e.get("error"):
            txt = str(e["error"])[:80]
            out["monitor:err:%s:%s" % (e.get("host") or "?", txt)] = "快照读取出错（%s）：%s；对应的信息本轮是缺的" % (e.get("host") or "?", txt)
    sr = live.get("screen_reads")
    if isinstance(sr, dict):
        tried, failed = sr.get("tried") or 0, sr.get("failed") or 0
        if tried >= 3 and failed * 2 >= tried:
            out["monitor:screen_reads"] = "巡检看不到 worker 屏幕（读 %d 个失败 %d 个），卡住的 worker 不会被自动继续" % (tried, failed)
    return out


HUMAN_LIFT_MATCH = 0.17   # 11-07 history: a block and the ask it answers overlap 0.19-0.67; unrelated pairs <= 0.15
_LIFT_STOP = None


def _lift_grams(text: Any) -> set:
    """ASCII words (>= 3 chars) and CJK character pairs, minus filler words common to any request."""
    global _LIFT_STOP
    t = str(text or "").lower()
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", t))
    g = set(re.findall(r"[a-z0-9_]{3,}", t)) | {cjk[i:i + 2] for i in range(len(cjk) - 1)}
    if _LIFT_STOP is None:
        _LIFT_STOP = set()
        _LIFT_STOP = _lift_grams("等待请提供确认继续无法不能缺少如果需要")
    return g - _LIFT_STOP


def lift_matches(ask: Any, block_summary: Any) -> bool:
    a, b = _lift_grams(ask), _lift_grams(block_summary)
    return bool(a and b) and len(a & b) / float(min(len(a), len(b))) >= HUMAN_LIFT_MATCH


def human_wait_lifted(events: List[Dict[str, Any]], line: str, since: Optional[str],
                      text: Optional[str] = None) -> bool:
    """True when, on `line`, a node blocked on the human got a later event (unblocked, re-blocked on something
    else, dispatched ...) at or after `since`, and no node of the line is blocked on the human any more.
    11-06: the coordinator settled the Jacobi inputs at 05:49 and the router lifted the human block at 06:01, but
    the follow-up only closes at terra's recheck (08:03), so the 07:21 QQ reminder asked the user for inputs
    already given.
    With `text` (the ask), only a lifted block whose summary is about the same thing counts (lift_matches).
    11-06/07: EVB marks waits for a coordinator ruling as blocker=human; each lift of such a block dropped the
    unrelated Instabox ask ("[不用再管]" at 22:34, dropped again at 23:25) while it was still needed."""
    human: Dict[str, bool] = {}
    why: Dict[str, str] = {}
    lifted = False
    for e in events:
        if e.get("line") != line or not e.get("node") or not str(e.get("kind") or "").startswith("node_"):
            continue
        nid, at = e["node"], e.get("at") or ""
        if e.get("kind") == "node_block" and e.get("blocker") == "human":
            human[nid] = True
            why[nid] = str(e.get("summary") or "")
            continue
        if human.get(nid):
            human[nid] = False
            if since and at >= since and (text is None or lift_matches(text, why.get(nid))):
                lifted = True
    return lifted and not any(human.values())


FU_SETTLED_RE = re.compile(r"已落实|已完成|已解决|已到|已批|已收到|已关闭|作废|不再需要|done|obsolete", re.I)
FU_NOT_RE = re.compile(r"未落实|没落实|未完成|未解决|未到|未批|not[ _]done", re.I)


def fu_settled(events: List[Dict[str, Any]], fu: Dict[str, Any]) -> bool:
    """True when a coordinator or router event names this follow-up by id and says it is settled, after the ask was
    sent and after terra's latest recheck (a later not_done from terra re-arms the reminder).
    11-07: core1 wrote "跟进fu_d3cf83f1已落实：L46人类批准按需…" at 08:03, but terra's recheck was 10:32, so the
    09:37 QQ reminder would have asked the user to approve a budget already approved. The FE block was never
    blocker=human, so human_wait_lifted could not see it."""
    fid = str(fu.get("id") or "")
    if not fid:
        return False
    since = max([str(fu.get("sent_at") or fu.get("created_at") or "")] +
                [str(h.get("at") or "") for h in fu.get("history") or [] if isinstance(h, dict) and h.get("at")])
    for e in events:
        if (e.get("actor") or {}).get("role") not in ("coordinator", "router") or str(e.get("at") or "") < since:
            continue
        s = str(e.get("summary") or "")
        i = s.find(fid)
        if i < 0:
            continue
        near = s[i + len(fid):i + len(fid) + 24]
        if FU_SETTLED_RE.search(near) and not FU_NOT_RE.search(near):
            return True
    return False


def followup_alerts(project: Dict[str, Any], now: _dt.datetime,
                    events: Optional[List[Dict[str, Any]]] = None) -> Dict[str, str]:
    """Supervisor follow-ups nobody automated: addressed to the human, or to tooling with no handler configured,
    still open and past their check time. A human ask whose line has since lifted its human block is dropped
    (alerts.py then sends one "[已恢复]"); terra still closes or re-opens it at the recheck."""
    try:
        st = snap.load_task_cache().get("supervisor:%s" % project.get("id")) or {}
        rows = list((st.get("followups") or {}).values())
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for e in rows:
        if not isinstance(e, dict) or e.get("closed_at") or not e.get("id"):
            continue
        if not (e.get("to") == "human" or (e.get("to") == "tooling" and not e.get("tooling_handle"))):
            continue
        ca = core.parse_iso(e.get("check_at"))
        # Once judged not done (tries >= 1 / human), terra pushes check_at hours ahead; the item is still open, so the
        # alert stays active (alerts.py handles reminder pacing). 11-06: FE/SR got a false "[已恢复]" from this.
        if ca is not None and ca > now and not (e.get("tries") or e.get("human")):
            continue
        if e.get("to") == "human" and events and e.get("line") and \
                human_wait_lifted(events, e["line"], e.get("sent_at") or e.get("created_at"), e.get("text")):
            continue
        if events and fu_settled(events, e):
            continue
        out["fu:%s" % e["id"]] = _short("%s 线：%s" % (e.get("line") or "?", e.get("text") or ""), 300)
    return out


# ------------------------------------------------------------ resume pack / memory loss

def resume_dir(pid: str) -> pathlib.Path:
    return core.project_dir(pid) / "resume"


def _atomic_write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


def write_resume_packs(pid: str, project: Dict[str, Any], events: List[Dict[str, Any]], now: _dt.datetime) -> List[str]:
    """Write resume/<line>.md for every declared line. Never raises; returns the paths written."""
    out = []
    for ln in project.get("lines") or []:
        lid = ln.get("id")
        if not lid or "/" in str(lid) or str(lid).startswith("."):
            continue
        try:
            text = inspect_tool.build_resume(project, events, lid, None, inspect_tool.RESUME_MAX, now=snap.iso(now),
                                             lessons=inspect_tool.read_lessons(pid, lid),
                                             followups=inspect_tool.open_followups(pid, lid))
            p = resume_dir(pid) / ("%s.md" % lid)
            _atomic_write(p, text)
            out.append(str(p))
        except Exception:  # noqa: BLE001 - a broken pack must not stop the patrol round
            continue
    return out


def load_resume_seen(pid: str) -> Dict[str, Any]:
    try:
        doc = json.loads((resume_dir(pid) / "seen.state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def save_resume_seen(pid: str, state: Dict[str, Any]) -> None:
    try:
        _atomic_write(resume_dir(pid) / "seen.state.json", json.dumps(state, ensure_ascii=False, sort_keys=True))
    except OSError:
        pass


def _prior_worker_handles(events: List[Dict[str, Any]], lid: str, nid: str) -> set:
    return {(e.get("refs") or {}).get("terminal") for e in events
            if e.get("line") == lid and e.get("node") == nid and e.get("kind") in ("node_dispatch", "auto_dispatch")
            and (e.get("refs") or {}).get("terminal")}


def detect_memory_loss(project: Dict[str, Any], live: Optional[Dict[str, Any]], seen: Dict[str, Any],
                       nudges: List[Dict[str, Any]], now: _dt.datetime) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Routers / coordinators that probably lost their context -> ([{line, target, causes, keys, flag, present}],
    new last-seen state). Needs a fresh live.json. A terminal whose message cannot be delivered now keeps its old
    last-seen values, so the cause is found again next round."""
    prev_h, prev_c, prev_x = (dict(seen.get(k) or {}) for k in ("handles", "compact", "ctx"))
    new_h, new_c, new_x = {}, {}, {}
    hits = []
    hour = now.strftime("%Y%m%d%H")
    sent = {(e.get("refs") or {}).get("key") for e in nudges if e.get("reason") == "resume"}
    regs = [(lid_, "line:" + lid_, info, lid_) for lid_, info in sorted(((live or {}).get("routers") or {}).items())
            if core.line_of(project, lid_)]
    for cid, info in sorted(((live or {}).get("coordinators") or {}).items()):
        lid_ = next((l["id"] for l in project.get("lines") or [] if str(l.get("core")) == str(cid)), None)
        if lid_:
            regs.append((lid_, "core:" + str(cid), info, lid_))
    for lid, rkey, info, _ in regs:
        if not isinstance(info, dict) or not info.get("handle") or info.get("present") is False:
            continue
        h = info["handle"]
        sc = info.get("screen") if isinstance(info.get("screen"), dict) else {}
        causes = []
        if info.get("rebound_from") or (prev_h.get(rkey) and prev_h[rkey] != h):
            causes.append("terminal")
        if sc.get("compact") and not prev_c.get(h):
            causes.append("compact")
        used = sc.get("ctx_used")
        if isinstance(used, int) and isinstance(prev_x.get(h), int) and prev_x[h] - used >= GAUGE_DROP:
            causes.append("gauge")
        keys = []
        for c in causes:
            k = "resume:%s:%s" % (h, c) if c == "terminal" else "resume:%s:%s:%s" % (h, c, hour)
            if k not in sent:
                keys.append((lid, k, "resume", c))
        sendable = info.get("screen_flag") != "waiting_input"
        if keys:
            hits.append({"line": lid, "target": h, "causes": [k[3] for k in keys], "keys": keys,
                         "sendable": sendable})
        if keys and not sendable:   # retry next round: do not advance the last-seen values
            for src, dst, kk in ((prev_h, new_h, rkey), (prev_c, new_c, h), (prev_x, new_x, h)):
                if kk in src:
                    dst[kk] = src[kk]
            continue
        new_h[rkey] = h
        if sc:
            new_c[h] = bool(sc.get("compact"))
            if isinstance(used, int):
                new_x[h] = used
            elif h in prev_x:
                new_x[h] = prev_x[h]
        else:
            for src, dst in ((prev_c, new_c), (prev_x, new_x)):
                if h in src:
                    dst[h] = src[h]
    return hits, {"handles": new_h, "compact": new_c, "ctx": new_x}


# ------------------------------------------------- patrol cache (S1 persistence, S2 rejected dispatches)

def state_file(pid: str) -> pathlib.Path:
    return core.project_dir(pid) / "patrol.state.json"


def load_patrol_state(pid: str) -> Dict[str, Any]:
    """{"rejected": {"<line>:<node>": {tries, last_why, argv_hash, spec_ref, last_at}},
        "undispatchable": {"<line>|<node>|<code>": {first_at, rounds, last_at}},
        "disk_hist": {"<host>:<mount>": [[iso time, avail_gb], ...]},   # last DISK_HIST_H hours, worker hosts only
        "wt_skip": {"<path>": {"verdict": "broken"|"missing", "why", "at"}},   # not rescanned for WT_SKIP_H
        "review": {"holdout_hits": n}}   # present while the last project review was refused by the holdout guard"""
    try:
        doc = json.loads(state_file(pid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    doc = doc if isinstance(doc, dict) else {}
    return {k: (doc.get(k) if isinstance(doc.get(k), dict) else {}) for k in ("rejected", "undispatchable", "disk_hist", "wt_skip", "review")}


def save_patrol_state(pid: str, state: Dict[str, Any]) -> None:
    try:
        _atomic_write(state_file(pid), json.dumps(state, ensure_ascii=False, sort_keys=True))
    except OSError:
        pass


def classify_start(rc: int, out: str, err: str, st: Dict[str, Any]) -> Optional[str]:
    """Why Orca REFUSED worker-start (no worker exists), else None (timeout / unparsed / partial ids = unknown)."""
    if rc in (0, 124):
        return None
    doc = snap.parse_json_tail(out)
    res = (doc.get("result") if isinstance(doc.get("result"), dict) else doc) if isinstance(doc, dict) else {}
    if res.get("dispatchId") or res.get("taskId") or any(
            isinstance(e, dict) and e.get("kind") == "terminal" and e.get("id") for e in res.get("effects") or []):
        return None
    text = "%s\n%s" % (out or "", err or "")
    if not REJECT_RE.search(text):
        return None
    return _short((err or "").strip() or (out or "").strip() or st.get("why") or "rc=%s" % rc, 100)


def argv_hash(argv: List[str]) -> str:
    """Hash of a worker-start argv without the --name value (that carries a timestamp)."""
    a = list(argv)
    if "--name" in a and a.index("--name") + 1 < len(a):
        a[a.index("--name") + 1] = ""
    return hashlib.sha1(json.dumps(a, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _rejected_block_only(n: Dict[str, Any]) -> bool:
    """Blocked solely by patrol's own refused-dispatch block (nothing from a router/worker after it)."""
    hist = n.get("history") or []
    last = hist[-1] if hist else {}
    return (n.get("status") == "blocked" and n.get("blocker") == "env" and last.get("kind") == "node_block"
            and last.get("actor") == "patrol" and str(last.get("summary") or "").startswith(REJECT_MARK))


def _stem(name: Any) -> str:
    return re.sub(r"-p\d{8}$", "", os.path.basename(str(name or "").rstrip("/")))


def worktree_taken(line: Dict[str, Any], stem: str, live: Optional[Dict[str, Any]], runner) -> Optional[str]:
    """A reason when an Orca task / worker already carries this worktree name (or the check cannot be made)."""
    for w in (live or {}).get("workers") or []:
        if isinstance(w, dict) and stem in (_stem(w.get("worktree_name")), _stem(w.get("workspace"))):
            return "已有 worker 用着工作区 %s" % stem
    errors: List[Dict[str, Any]] = []
    rows, complete = snap.fetch_dispatch_index(runner, snap.orca_bin(), errors, [line["run"]] if line.get("run") else None)
    for r in rows:
        if _stem(r.get("workspace")) == stem:
            return "Orca 任务 %s 已用工作区 %s" % (r.get("task") or r.get("dispatch"), stem)
    return None if complete else "worker-list 查不全，无法确认没有同名任务"


# ------------------------------------------------------------ outage / stall alerts (11-06 night: 6.5 h blind)

def _supervisor_fail_streak(pid: str) -> int:
    try:
        return int((snap.load_task_cache().get("supervisor:%s" % pid) or {}).get("fail_streak") or 0)
    except Exception:  # noqa: BLE001
        return 0


def outage_alerts(project: Dict[str, Any], state: Dict[str, Any], events: List[Dict[str, Any]],
                  live: Optional[Dict[str, Any]], live_bad: Optional[str], now: _dt.datetime) -> Dict[str, str]:
    """QQ alert keys `outage:model` (>= 2 distinct agent screens show a transient model/API error, or the supervisor's
    last 2 model calls failed) and `stall:project` (running/ready nodes exist but no router/worker event on any open
    line for STALL_PROJECT_MIN minutes)."""
    out: Dict[str, str] = {}
    screens: Dict[str, str] = {}
    if isinstance(live, dict) and not live_bad:
        rows = list((live.get("routers") or {}).values()) + list((live.get("coordinators") or {}).values())
        rows += [w for w in live.get("workers") or [] if isinstance(w, dict)]
        for r in rows:
            sc = r.get("screen") if isinstance(r, dict) else None
            handle = (r.get("handle") or r.get("terminal")) if isinstance(r, dict) else None
            if handle and isinstance(sc, dict) and sc.get("transient"):
                screens.setdefault(handle, str(sc.get("line") or ""))
    fails = _supervisor_fail_streak(project.get("id") or "")
    if len(screens) >= OUTAGE_MIN_SCREENS or fails >= OUTAGE_TERRA_FAILS:
        first = next((l for l in screens.values() if l), "")
        out["outage:model"] = "模型网关疑似故障：%d 个 agent 屏幕报 %s；terra 连续 %d 次调用失败。agent 会停在报错处，自动继续会在恢复后重试。" % (
            len(screens), _short(first, 80) or "（无报错行）", fails)
    open_lines = {lid: ls for lid, ls in (state.get("lines") or {}).items() if ls.get("open", True)}
    has_work = any(n.get("status") in ("running", "ready") for ls in open_lines.values()
                   for n in (ls.get("nodes") or {}).values())
    if has_work:
        last = None
        for e in events:
            if (e.get("actor") or {}).get("role") == "patrol" or e.get("line") not in open_lines:
                continue
            t = core.parse_iso(e.get("at"))
            if t is not None and (last is None or t > last[0]):
                last = (t, e.get("line"))
        if last is not None and (now - last[0]).total_seconds() >= STALL_PROJECT_MIN * 60:
            out["stall:project"] = "全部主线已 %d 分钟没有 router/worker 事件（最后一条 %s %s）" % (
                (now - last[0]).total_seconds() // 60, last[1], snap.iso(last[0])[5:16].replace("T", " ") + "Z")
    return out


# ------------------------------------------------------------ disk: who gets told, fill projection

def worker_hosts(state: Dict[str, Any], live: Optional[Dict[str, Any]]) -> set:
    """Hosts where workers write: hosts of live worker terminals and the `on` of node routes (in practice "wsl")."""
    hosts = {w.get("host") for w in (live or {}).get("workers") or [] if isinstance(w, dict) and w.get("host")}
    for ls in (state.get("lines") or {}).values():
        for n in (ls.get("nodes") or {}).values():
            on = (n.get("route") or {}).get("on")
            if on:
                hosts.add(on)
    return hosts or {snap.remote_host()}


def disk_tight(live: Optional[Dict[str, Any]], host: str) -> Optional[Dict[str, Any]]:
    """A disk of `host` at level low/critical (host thresholds, snapshot.disk_thresholds) -> {mount, avail_gb, level}.
    Auto-dispatch holds new nodes then: every new node is a fresh worktree (~2-3 G on dse), and the coordinators are
    told to pause heavy writes at the same level (11-06: WSL 96 -> 53 G in 2 h while patrol kept dispatching)."""
    for d in (((live or {}).get("resources") or {}).get(host) or {}).get("disks") or []:
        if isinstance(d.get("avail_gb"), (int, float)):
            lvl = snap.disk_level(d["avail_gb"], host)
            if lvl in ("low", "critical"):
                return {"mount": d.get("mount"), "avail_gb": d["avail_gb"], "level": lvl}
    return None


def free_worker_mem(state: Dict[str, Any], live: Optional[Dict[str, Any]]) -> Optional[float]:
    """Highest MemAvailable (GB) over the worker hosts' live.resources when it exceeds IDLE_FREE_MEM_GB, else None."""
    res = (live or {}).get("resources") or {}
    vals = [res[h].get("mem_avail_gb") for h in worker_hosts(state, live) if isinstance(res.get(h), dict)]
    vals = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
    best = max(vals) if vals else None
    return best if best is not None and best > IDLE_FREE_MEM_GB else None


def record_disk(hist: Dict[str, Any], key: str, at: str, gb: float, now: _dt.datetime) -> List[List[Any]]:
    """Append (at, gb) to hist[key] unless not newer than the last point; drop points older than DISK_HIST_H."""
    pts = [p for p in hist.get(key) or [] if isinstance(p, list) and len(p) == 2 and core.parse_iso(p[0])]
    t = core.parse_iso(at)
    if t is not None and (not pts or t > core.parse_iso(pts[-1][0])):
        pts.append([snap.iso(t), gb])
    cut = now - _dt.timedelta(hours=DISK_HIST_H)
    pts = [p for p in pts if core.parse_iso(p[0]) >= cut]
    hist[key] = pts
    return pts


def project_fill(pts: List[List[Any]], crit_gb: Optional[float] = None) -> Optional[Dict[str, float]]:
    """-> {rate_gb_h, hours_left, avail} when the disk is shrinking so that crit_gb (default DISK_CRIT_GB; per host via
    snapshot.disk_thresholds) is < DISK_FILL_H hours away.
    Only the points after the last upward jump (>= 2 GB: someone cleaned up) count."""
    start = 0
    for i in range(1, len(pts)):
        if pts[i][1] - pts[i - 1][1] >= 2:
            start = i
    pts = pts[start:]
    if len(pts) < 2:
        return None
    t0, t1 = core.parse_iso(pts[0][0]), core.parse_iso(pts[-1][0])
    span = (t1 - t0).total_seconds() / 3600.0
    if span < DISK_FILL_MIN_SPAN_H:
        return None
    rate = (pts[0][1] - pts[-1][1]) / span
    if rate <= 0:
        return None
    crit_gb = snap.DISK_CRIT_GB if crit_gb is None else crit_gb
    left = max(0.0, (pts[-1][1] - crit_gb) / rate)
    if left >= DISK_FILL_H:
        return None
    return {"rate_gb_h": round(rate, 1), "hours_left": round(left, 1), "avail": pts[-1][1]}


# ------------------------------------------------------------ strict worktree cleanup (user 11-06, --apply only)

WT_PATH_RE = re.compile(r"^/[A-Za-z0-9_./+-]+$")
WT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$")   # a bare worktree name: no "/" so no "..", no traversal
# ignored files that are safe to lose with a worktree (regenerable caches); anything else ignored blocks removal
WT_REGEN_RE = r"(^|/)(__pycache__|\\.pytest_cache|\\.mypy_cache|\\.ruff_cache)(/|$)|\\.py[co]$"


def wt_root_ok(path: str) -> bool:
    """Under the dse-harness workspaces dir (PROGRESS_WT_ROOT, default: any `/workspaces/` directory), not the dir itself."""
    if not WT_PATH_RE.match(path) or ".." in path.split("/"):
        return False
    root = os.environ.get("PROGRESS_WT_ROOT")
    if root:
        root = root.rstrip("/") + "/"
        return path.startswith(root) and len(path) > len(root)
    head, sep, tail = path.partition("/workspaces/")
    return bool(sep) and bool(tail.strip("/"))


def wt_dispatch_map(events: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """{path: {"nodes": {(line, node)}, "last": datetime}} for every node_dispatch / auto_dispatch whose refs.worktree is
    "<repoid>::<path>" (or a bare absolute path)."""
    out: Dict[str, Dict[str, Any]] = {}
    for e in events:
        if e.get("kind") not in ("node_dispatch", "auto_dispatch"):
            continue
        ref = (e.get("refs") or {}).get("worktree")
        if not isinstance(ref, str):
            continue
        path = ref.split("::", 1)[-1].rstrip("/")
        if not path.startswith("/"):
            # patrol's own auto_dispatch records the bare worktree name it asked Orca for (worktree_name());
            # Orca creates it directly under the workspaces dir. 11-07: 47 such worktrees (119 G) were invisible here.
            root = os.environ.get("PROGRESS_WT_ROOT")
            if not root or not WT_NAME_RE.match(path):
                continue
            path = root.rstrip("/") + "/" + path
        ent = out.setdefault(path, {"nodes": set(), "last": None})
        if e.get("line") and e.get("node"):
            ent["nodes"].add((e["line"], e["node"]))
        t = core.parse_iso(e.get("at"))
        if t is not None and (ent["last"] is None or t > ent["last"]):
            ent["last"] = t
    return out


def wt_pre_candidates(project: Dict[str, Any], state: Dict[str, Any], events: List[Dict[str, Any]],
                      live: Dict[str, Any], now: _dt.datetime) -> List[Dict[str, Any]]:
    """Conditions 1-3 (and the workspaces-dir rule), decided from events and the live snapshot alone.
    -> [{path, nodes, last, idle_h, ok: bool, why: str}] oldest first."""
    cut = now - _dt.timedelta(hours=IDLE_WT_H)
    recent = [(e, json.dumps(e, ensure_ascii=False)) for e in events if (core.parse_iso(e.get("at")) or now) >= cut]
    live_text = json.dumps({k: live.get(k) for k in ("workers", "routers", "coordinators", "router_tree")}, ensure_ascii=False)
    line_text = json.dumps(project.get("lines") or [], ensure_ascii=False)
    rows = []
    for path, ent in wt_dispatch_map(events).items():
        if not wt_root_ok(path):
            continue
        nodes = sorted(ent["nodes"])
        row = {"path": path, "nodes": ["%s/%s" % n for n in nodes], "last": ent["last"], "ok": True, "why": ""}
        row["idle_h"] = round((now - ent["last"]).total_seconds() / 3600.0, 1) if ent["last"] else None
        bad = [n for n in nodes
               if (((state.get("lines") or {}).get(n[0]) or {}).get("nodes") or {}).get(n[1], {}).get("status") not in WT_TERMINAL]
        if not nodes:
            row.update(ok=False, why="没有节点引用它")
        elif bad:
            row.update(ok=False, why="节点未收尾：%s" % ",".join("%s/%s" % n for n in bad))
        elif ent["last"] is None or ent["last"] > cut:
            row.update(ok=False, why="最近 %d 小时内有派发" % IDLE_WT_H)
        else:
            for e, text in recent:
                if path in text or (e.get("line") and e.get("node") and (e["line"], e["node"]) in set(nodes)):
                    row.update(ok=False, why="最近 %d 小时内有事件提到它" % IDLE_WT_H)
                    break
            else:
                if path in live_text or path in line_text or any(
                        isinstance(w, dict) and (str(w.get("workspace") or "").split("::", 1)[-1] == path
                                                 or w.get("worktree_name") == os.path.basename(path))
                        for w in live.get("workers") or []):
                    row.update(ok=False, why="有终端在用，或是某条线的 router 工作区")
        rows.append(row)
    rows.sort(key=lambda r: (r["last"] is None, r["last"] or now))
    return rows


def wt_scan_script(paths: List[str], cutoff: _dt.datetime) -> str:
    """One shell script for all candidates: newest-file check, git status / branch / reachability, du (only if all clear)."""
    epoch = int(cutoff.timestamp())
    parts = []
    for p in paths:
        q = shlex.quote(p)
        parts.append(
            'p=%s; echo "@@WT $p"; if [ -L "$p" ]; then echo "LINK $(readlink "$p")"; elif [ ! -e "$p" ]; then echo MISSING; else '
            'echo "NEWER $(find "$p" -maxdepth 3 -newermt @%d -print -quit 2>/dev/null | head -1)"; '
            'st=$(git -C "$p" status --porcelain --untracked-files=all 2>&1); echo "GITRC $?"; '
            'echo "DIRTY $(printf %%s "$st" | head -c 200 | tr "\\n" "|")"; '
            'echo "IGNORED $(git -C "$p" status --porcelain --ignored=matching --untracked-files=all 2>/dev/null | grep "^!!" | cut -c4- '
            '| grep -vE "%s" | head -c 200 | tr "\\n" "|")"; '
            'echo "COMMON $(git -C "$p" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"; '
            'echo "GITDIR $(git -C "$p" rev-parse --path-format=absolute --git-dir 2>/dev/null)"; '
            'echo "BRANCH $(git -C "$p" symbolic-ref -q --short HEAD 2>/dev/null)"; '
            'echo "HEAD $(git -C "$p" rev-parse HEAD 2>/dev/null)"; '
            'echo "REFS $(git -C "$p" for-each-ref --contains HEAD --format=%%\\(refname\\) 2>/dev/null | head -n 20 | tr "\\n" " ")"; '
            'b=$(git -C "$p" symbolic-ref -q --short HEAD 2>/dev/null); c=$(git -C "$p" rev-parse --path-format=absolute --git-common-dir 2>/dev/null); '
            'if [ -n "$b" ] && git --git-dir="$c" rev-parse --verify -q "refs/heads/$b" >/dev/null 2>&1; then echo BRANCH_OK 1; else echo BRANCH_OK 0; fi; '
            'echo "DU $(du -sk "$p" 2>/dev/null | cut -f1)"; fi' % (q, epoch, WT_REGEN_RE))
    return "; ".join(parts)


def parse_wt_scan(text: str) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    cur = None
    for ln in (text or "").splitlines():
        if ln.startswith("@@WT "):
            cur = out.setdefault(ln[5:].strip(), {})
        elif cur is not None and ln.strip():
            k, _, v = ln.partition(" ")
            cur[k] = v.strip()
    return out


def wt_verdict(info: Dict[str, str]) -> Tuple[str, str]:
    """Condition 4 plus the mtime part of 2, from one scan block -> ("remove"|"broken"|"skip", reason)."""
    if "MISSING" in info:
        return "skip", "目录已不存在"
    if "LINK" in info:   # someone moved it elsewhere (e.g. an archive disk) and left a link: takes no space here, never touched
        return "skip", "已移走，这里只剩链接（指向 %s）" % (info.get("LINK") or "?")
    if info.get("NEWER"):
        return "skip", "文件 %d 小时内有改动" % IDLE_WT_H
    if info.get("GITRC") != "0" or not info.get("HEAD") or not info.get("GITDIR") or not info.get("COMMON"):
        return "broken", "git 读不了（.git 链接可能坏了）"
    if info["GITDIR"].rstrip("/") == info["COMMON"].rstrip("/"):
        return "skip", "这是主仓库，不是附属工作区"
    if info.get("DIRTY"):
        return "skip", "有未提交或未跟踪的文件"
    if "IGNORED" not in info:
        return "skip", "没查到被忽略文件的情况"
    if info["IGNORED"]:
        return "skip", "有被 .gitignore 忽略的文件（可能是原始产物，删工作区会一起删掉）"
    branch = info.get("BRANCH") or ""
    others = [r for r in (info.get("REFS") or "").split() if branch == "" or r != "refs/heads/%s" % branch]
    if others:
        return "remove", "HEAD 已在别的引用上（%s）" % others[0]
    if branch and info.get("BRANCH_OK") == "1":
        return "remove", "分支 %s 保留" % branch
    return "skip", "HEAD 只在这个工作区里，且分支不能确认保留"


def plan_wt_cleanup(project: Dict[str, Any], state: Dict[str, Any], events: List[Dict[str, Any]],
                    live: Optional[Dict[str, Any]], live_bad: Optional[str], now: _dt.datetime, runner,
                    skip: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Strict worktree cleanup, decision part (read-only; uses one ssh script). -> {"candidates": [...], "broken": n,
    "remove": [rows to remove, at most WT_RM_PER_ROUND, oldest first]}. Nothing at all unless the live snapshot is
    fresh and the worker host is "ok"."""
    res: Dict[str, Any] = {"candidates": [], "broken": 0, "remove": []}
    host = snap.remote_host()
    if live_bad or not isinstance(live, dict) or (live.get("hosts") or {}).get(host) != "ok":
        res["skipped"] = "live 不可用或 %s 状态未知" % host
        return res
    pre = wt_pre_candidates(project, state, events, live, now)
    # `skip` (patrol.state.json "wt_skip", updated in place): broken / missing paths seen within WT_SKIP_H are not
    # rescanned, so they do not eat the scan slots of the candidates behind them every round
    skip = skip if skip is not None else {}
    for k in [k for k, v in skip.items()
              if _age(now, (v or {}).get("at")) is None
              or _age(now, (v or {}).get("at")) >= float((v or {}).get("h") or WT_SKIP_H) * 3600]:
        skip.pop(k, None)
    ready = [r for r in pre if r["ok"] and r["path"] not in skip][:WT_SCAN_MAX]
    info: Dict[str, Dict[str, str]] = {}
    if ready:
        rc, out, _err = runner(snap.ssh_argv(host, wt_scan_script([r["path"] for r in ready],
                                                                  now - _dt.timedelta(hours=IDLE_WT_H))), timeout=280)
        if rc == 0:
            info = parse_wt_scan(out)
        else:
            res["skipped"] = "扫描失败 rc=%s" % rc
    for r in pre:
        row = {"path": r["path"], "nodes": r["nodes"], "idle_h": r["idle_h"], "verdict": "skip", "why": r["why"]}
        if r["ok"] and r in ready and r["path"] in info:
            v, why = wt_verdict(info[r["path"]])
            row.update(verdict=v, why=why)
            du = info[r["path"]].get("DU")
            row["size_gb"] = round(int(du) / 1048576.0, 1) if du and du.isdigit() else None
            if v == "broken" or why == "目录已不存在":
                skip[r["path"]] = {"verdict": "broken" if v == "broken" else "missing", "why": why, "at": snap.iso(now)}
            elif "LINK" in info[r["path"]]:
                skip[r["path"]] = {"verdict": "moved", "why": why, "at": snap.iso(now)}
            elif v == "skip":
                skip[r["path"]] = {"verdict": "held", "why": why, "at": snap.iso(now), "h": WT_HELD_H}
            if v == "broken":
                res["broken"] += 1
            if v == "remove":
                row["common"], row["branch"] = info[r["path"]].get("COMMON"), info[r["path"]].get("BRANCH") or None
                row["head"] = info[r["path"]].get("HEAD") or None
        elif r["ok"] and r["path"] in skip:
            row.update(verdict=skip[r["path"]]["verdict"] if skip[r["path"]]["verdict"] == "broken" else "skip",
                       why="%s（%s 时记下，%s 小时内不重扫）" % (skip[r["path"]].get("why"), skip[r["path"]].get("at"),
                                                           skip[r["path"]].get("h") or WT_SKIP_H))
            if row["verdict"] == "broken":
                res["broken"] += 1
        elif r["ok"]:
            row.update(why="没扫到" if r in ready else "本轮扫描名额已满")
        res["candidates"].append(row)
    res["remove"] = [c for c in res["candidates"] if c["verdict"] == "remove"][:WT_RM_PER_ROUND]
    return res


def plan(project: Dict[str, Any], events: List[Dict[str, Any]], live: Optional[Dict[str, Any]],
         now: _dt.datetime, runner=snap.default_runner, apply: bool = False) -> Dict[str, Any]:
    """Pure decision step (reads specs via git and, with `apply`, idle worker screens): findings + actions."""
    state = core.fold(project, events, at=snap.iso(now))
    live_bad = live_usable(live, now)
    actions: List[Dict[str, Any]] = []
    findings: List[Dict[str, Any]] = []
    observes: List[Dict[str, Any]] = []
    per_router: Dict[str, Dict[str, Any]] = {}

    nudges = list(_patrol_events(events, "nudge"))
    router_last: Dict[str, str] = {}   # line -> newest event a router wrote itself
    for e in events:
        if (e.get("actor") or {}).get("role") == "router" and e.get("line"):
            router_last[e["line"]] = max(router_last.get(e["line"], ""), e.get("at") or "")
    observed = {}
    for e in _patrol_events(events, "observe"):
        k = (e.get("refs") or {}).get("key")
        if k:
            observed[k] = max(observed.get(k, ""), e.get("at") or "")

    def observe(key, line, node, summary, **extra):
        last = observed.get(key)
        if last and (_age(now, last) or 0) < OBSERVE_GAP_S:
            return
        observed[key] = snap.iso(now)
        ev = {"kind": "observe", "summary": _short(summary), "refs": {"key": key}}
        if line:
            ev["line"] = line
        if node:
            ev["node"] = node
        ev.update(extra)
        observes.append({"type": "observe", "event": ev})

    def router_bucket(line):
        h = (((live or {}).get("routers") or {}).get(line["id"]) or {}).get("handle") or line.get("router")
        b = per_router.setdefault(h, {"handle": h, "lines": [], "items": [], "notices": [],
                                      "line_ids": set()})
        b["line_ids"].add(line["id"])
        return b

    alerts: Dict[str, Dict[str, Any]] = {}

    def alert(line, nid, reason, since, text):
        """Queue a rate-limit-exempt nudge for one node, deduped per node per stall."""
        key = "%s:%s:%s" % (reason, line["id"], nid)
        if not _alert_due(nudges, reason, key, since, now):
            return
        h = (((live or {}).get("routers") or {}).get(line["id"]) or {}).get("handle") or line.get("router")
        a = alerts.setdefault(h, {"line": line["id"], "target": h, "texts": [], "keys": []})
        a["texts"].append(text)
        a["keys"].append((line["id"], key, reason))

    exhausted = set()  # worker handles whose auto-continue tries are used up: stalled alert at once
    qq_active: Dict[str, str] = {}   # QQ alerts that are true right now (alerts.sync decides what to send)
    if not live_bad:
        for t in continue_targets(project, live, runner):
            key = continue_key(t)
            n_tries = continue_tries(events, key)
            if n_tries >= CONTINUE_TRIES:
                exhausted.add(t["handle"])
                who = ("%s/%s 的 worker" % (t.get("line") or "?", t.get("node") or "?")) if t["role"] == "worker" \
                    else "%s 的 %s" % (t.get("line") or "?", "router" if t["role"] == "router" else "协调人")
                qq_active["stuck:%s" % t["handle"]] = (
                    "%s 连续 %d 次停在报错（%s），已停止自动继续%s" % (
                        who, CONTINUE_TRIES + 1, _short(t.get("text") or "", 80),
                        "，已告诉 router" if t["role"] == "worker" else ""))
                findings.append({"code": "auto_continue_exhausted", "target": t["handle"], "tries": n_tries})
                continue
            if not t["line"]:
                continue
            text = CONTINUE_TEXT
            if t["role"] == "worker" and t.get("node") and any(
                    h != t["handle"] for h in _prior_worker_handles(events, t["line"], t["node"])):
                text = "%s。先运行 progress-inspect resume --line %s --node %s" % (CONTINUE_TEXT, t["line"], t["node"])
            actions.append({"type": "auto_continue", "line": t["line"], "target": t["handle"], "role": t["role"],
                            "node": t.get("node"), "key": key, "try": n_tries + 1, "env": t.get("env"),
                            "screen": _short(t.get("text") or "", 100), "text": text})

    resume_state = None
    if live_bad in (None, "wsl_unknown") and isinstance(live, dict):
        hits, resume_state = detect_memory_loss(project, live, load_resume_seen(project.get("id") or ""), nudges, now)
        for hit in hits:
            findings.append({"code": "memory_loss", "line": hit["line"], "target": hit["target"], "causes": hit["causes"]})
            if hit["sendable"]:
                actions.append({"type": "alert", "line": hit["line"], "target": hit["target"],
                                "text": RESUME_TEXT % (hit["line"], project.get("id"), hit["line"]),
                                "keys": [(l, k, r) for l, k, r, _c in hit["keys"]], "label": "接续提醒："})

    patrolled = []
    pid_ = project.get("id") or ""
    pstate = load_patrol_state(pid_) if pid_ else {"rejected": {}, "undispatchable": {}, "disk_hist": {}}
    new_undisp: Dict[str, Any] = {}
    cur_rejected: set = set()
    metrics: Dict[str, Any] = {}
    decide_actions: List[Dict[str, Any]] = []
    routers_known = _router_handles(project, live)
    released_now = [0]
    for line in project.get("lines") or []:
        lid = line.get("id")
        ls = state["lines"].get(lid) or {}
        if line.get("patrol") is not True or not ls.get("open", True):
            continue
        patrolled.append(lid)
        nodes = ls.get("nodes") or {}
        repo = repo_path(project, line)
        dispatched_now = set()
        manual_ready = []
        exp_defaults = dict(DEFAULT_EXPECTED, **((project.get("defaults") or {}).get("expected_minutes") or {}))
        metrics[lid] = graph.line_metrics(nodes, now, exp_defaults)
        rr = metrics[lid]["review_run"]
        if rr["over"]:
            findings.append({"line": lid, "code": "review_heavy", "ratio": rr["ratio"]})
            observe("review_heavy:%s" % lid, lid, None,
                    "%s 线审查耗时是执行的 %d%%（上限 20%%），请放宽审查：普通交付不另排审查节点" % (lid, rr["ratio"] * 100),
                    code="review_heavy")
        cap = max_parallel(project, line)
        busy = {nid for nid, n in nodes.items() if n.get("status") == "running" and _is_worker_kind(n)
                and not _dispatch_imported(n) and not _task_finished(n, None if live_bad else live)}
        serial_busy = {graph.segment_key(n) for n in nodes.values() if n.get("status") == "running"
                       and n.get("segment_mode") == "serial" and graph.segment_key(n)}
        collapses = find_collapses(nodes, None if live_bad else live, lid)
        held = {c["segment"] for c in collapses}
        deferred = []
        for c in collapses:
            findings.append({"line": lid, "code": "collapse", "nodes": c["nodes"], "why": c["why"],
                             "segment": list(c["segment"])})
            first = nodes[c["nodes"][0]]
            target, tkind = parent_router(line, first, nodes, live)
            key = "collapse:%s:%s:%s" % (lid, c["segment"][0], c["segment"][1])
            if not target or not _repeat_due(nudges, "collapse", key, COLLAPSE_GAP_S, now):
                continue
            if tkind == "line":
                rinfo_c = ((live or {}).get("routers") or {}).get(lid) or {}
                if rinfo_c.get("present") is False or rinfo_c.get("screen_flag") == "waiting_input":
                    observe(key, lid, None, "并行塌缩但 router 不能收消息：%s" % "；".join(c["why"]), code="collapse")
                    continue
            text = ("[巡检] 并行塌缩：%s 标为 parallel 的 %s 不是真正独立的（%s）。已停止再派这一段；"
                    "请重新拆分（progress propose，写域不重叠）或改成 serial，必要时 supersede/cancel 重复的节点。") % (
                c["segment"][1], "、".join(c["nodes"]), "；".join(c["why"])[:300])
            actions.append({"type": "alert", "line": lid, "target": target, "text": text[:900],
                            "keys": [(lid, key, "collapse")], "label": "并行塌缩："})
        for sig in expansion_signals(nodes):
            n = nodes[sig["node"]]
            key = "expand:%s:%s:%s:%s" % (lid, sig["node"], sig["kind"], sig["at"])
            if any((e.get("refs") or {}).get("key") == key for e in nudges):
                continue
            target, tkind = parent_router(line, n, nodes, live)
            findings.append({"line": lid, "node": sig["node"], "code": "parallel_signal", "signal": sig["kind"],
                             "count": len(sig["items"]), "target": target})
            if not target:
                continue
            if tkind == "line":
                rinfo_e = ((live or {}).get("routers") or {}).get(lid) or {}
                if rinfo_e.get("present") is False or rinfo_e.get("screen_flag") == "waiting_input":
                    continue  # retried next round: the key is only spent by a sent nudge
            what = "%d 个不同的失败" % len(sig["items"]) if sig["kind"] == "failures" else "%d 个互不依赖的候选" % len(sig["items"])
            text = ("[巡检] 可并行：%s 报出 %s（%s）。若彼此独立，可每个开一个 run 节点并行（progress propose，"
                    "段标 parallel、写域不重叠、各自验收标准）；若相关就串行处理。") % (
                sig["node"], what, "；".join(_short(x, 60) for x in sig["items"][:6]))
            actions.append({"type": "alert", "line": lid, "target": target, "text": text[:900],
                            "keys": [(lid, key, "expand")], "label": "可并行信号："})
        undisp: Dict[str, Tuple[str, str]] = {}   # node -> (code, short reason): auto-dispatchable but patrol could not
        stale_ok: set = set()                      # ready nodes held on purpose (ceiling, serial, collapse, panel)
        retry_ok: Dict[str, Dict[str, Any]] = {}   # blocked only by patrol's own refused dispatch, may be retried
        for nid, n in sorted(nodes.items()):
            if not _rejected_block_only(n):
                continue
            rkey = "%s:%s" % (lid, nid)
            rec = pstate["rejected"].get(rkey)
            cur_rejected.add(rkey)
            undisp[nid] = ("dispatch_rejected", REJECT_MARK)
            if not rec:
                continue
            if int(rec.get("tries") or 0) >= REJECT_MAX_TRIES:
                qq_active["dispatch_rejected:%s:%s" % (lid, nid)] = (
                    "%s/%s 巡检代派被 Orca 拒绝 %d 次，已放弃，请手动派发（原因：%s）" % (
                        lid, nid, int(rec["tries"]), _short(rec.get("last_why") or "", 100)))
            elif n.get("auto_dispatch") and n.get("spec") and _is_worker_kind(n):
                retry_ok[nid] = rec
        for nid in list(graph.ready_queue(nodes, exp_defaults)) + sorted(retry_ok):
            n = nodes[nid]
            is_retry = nid in retry_ok
            prior = [] if is_retry else [h for h in n.get("history") or [] if h.get("kind") in ("node_dispatch", "auto_dispatch")]
            if prior:
                manual_ready.append(nid)
                observe("redispatch:%s:%s" % (lid, nid), lid, nid,
                        "节点 %s 曾派发过又回到 ready，巡检不再代派" % nid, code="already_dispatched")
                continue
            if n.get("kind") == "decide":
                if decide.has_decision(events, lid, nid, n.get("spec")):
                    stale_ok.add(nid)
                    continue  # already ruled for this spec: nothing for patrol or the router to do
                spec = read_spec(n.get("spec"), repo, runner)
                if spec.get("error") or len(decide.parse_options(spec["text"])) < 2:
                    manual_ready.append(nid)  # the panel cannot run: keep the router nudge
                    why = spec.get("error") or "spec 里没有编号选项（至少 2 个）"
                    undisp[nid] = ("decide_unready", UNDISP_WHY["decide_unready"])
                    findings.append({"line": lid, "node": nid, "code": "decide_unready", "why": why})
                    observe("decide:%s:%s" % (lid, nid), lid, nid, "decide 节点 %s 不能交面板：%s" % (nid, why),
                            code="decide_unready")
                    continue
                stale_ok.add(nid)
                if len(decide_actions) < decide.PER_ROUND:
                    decide_actions.append({"type": "decide", "line": lid, "node": nid, "spec": n.get("spec")})
                else:
                    findings.append({"line": lid, "node": nid, "code": "decide_queued"})
                continue
            if not n.get("auto_dispatch") or not n.get("spec") or not _is_worker_kind(n):
                manual_ready.append(nid)  # plan nodes need a router, not a worker
                continue
            sk = graph.segment_key(n)
            if sk in held:
                deferred.append({"node": nid, "why": "collapse"})
                stale_ok.add(nid)
                continue
            if n.get("segment_mode") == "serial" and sk in serial_busy:
                deferred.append({"node": nid, "why": "serial"})
                stale_ok.add(nid)
                continue
            if cap is not None and len(busy) + len(dispatched_now) >= cap:
                deferred.append({"node": nid, "why": "ceiling"})
                stale_ok.add(nid)
                continue
            tight = None if live_bad else disk_tight(live, (n.get("route") or {}).get("on") or snap.remote_host())
            if tight:
                deferred.append({"node": nid, "why": "disk", "avail_gb": tight["avail_gb"]})
                stale_ok.add(nid)
                continue
            spec = read_spec(n.get("spec"), repo, runner)
            if spec.get("error"):
                if is_retry:
                    continue  # still listed as dispatch_rejected
                undisp[nid] = ("spec_unreadable", UNDISP_WHY["spec_unreadable"])
                manual_ready.append(nid)
                findings.append({"line": lid, "node": nid, "code": "spec_unreadable", "why": spec["error"]})
                observe("spec:%s:%s" % (lid, nid), lid, nid, spec["error"], code="spec_unreadable")
                continue
            route = resolve_route(n, spec["header"])
            route, route_why = dev_route(project, events, nodes, lid, nid, n, route)
            miss = route_missing(route)
            repo_id = line_repo(project, line)  # None: worker-start without --repo (fine for local worktrees)
            if miss:
                if is_retry:
                    continue
                undisp[nid] = ("route_incomplete", "route 缺 %s" % ",".join(miss))
                manual_ready.append(nid)
                findings.append({"line": lid, "node": nid, "code": "route_incomplete", "missing": miss})
                observe("route:%s:%s" % (lid, nid), lid, nid, "节点 %s 的 route 缺 %s，不能代派" % (nid, ",".join(miss)),
                        code="route_incomplete")
                continue
            base = line.get("base_branch") or spec["branch"]
            name = worktree_name(lid, nid, now)
            text = with_snippet(spec["text"], lid, nid)
            argv = start_argv(text, route, dict(line, router=router_bucket(line)["handle"], repo=repo_id), n, base, name)
            if is_retry:
                rec = retry_ok[nid]
                last_age = _age(now, rec.get("last_at"))
                changed = rec.get("argv_hash") != argv_hash(argv) or rec.get("spec_ref") != n.get("spec")
                if not changed and (last_age is None or last_age < REJECT_RETRY_MIN * 60):
                    continue  # nothing changed since Orca refused it: wait out the hour
                taken = worktree_taken(line, _stem(name), live, runner)
                if taken:
                    findings.append({"line": lid, "node": nid, "code": "retry_skipped", "why": taken})
                    continue
            dispatched_now.add(nid)
            if n.get("segment_mode") == "serial" and sk:
                serial_busy.add(sk)
            if route_why:
                route = dict(route, why=route_why)
            actions.append({"type": "auto_dispatch", "line": lid, "node": nid, "route": route, "base": base,
                            "name": name, "spec_ref": n["spec"], "spec_bytes": len(text), "argv": argv,
                            "argv_hash": argv_hash(argv),
                            "retry": int(retry_ok[nid].get("tries") or 0) if is_retry else 0,
                            "router": router_bucket(line)["handle"]})

        # S1 + S3: ready nodes nobody is dispatching are reported, whether or not other nodes run
        listed = []   # (node, code, short reason, key, nudge reason, since)
        for nid, (code, why) in sorted(undisp.items()):
            if nid in dispatched_now:
                continue
            n = nodes[nid]
            findings.append({"line": lid, "node": nid, "code": "undispatchable", "why_code": code, "why": why})
            ukey = "%s|%s|%s" % (lid, nid, code)
            old = pstate["undispatchable"].get(ukey) or {}
            new_undisp[ukey] = {"first_at": old.get("first_at") or snap.iso(now), "rounds": int(old.get("rounds") or 0) + 1,
                                "last_at": snap.iso(now), "why": why}
            since = n.get("blocked_at") if code == "dispatch_rejected" else graph.ready_since(nodes, n)
            listed.append((nid, code, why, "undispatchable:%s:%s:%s" % (lid, nid, code), "undispatchable", since))
        idle_lists = set()   # the idle nudge below already names these (only when nothing runs on the line)
        if manual_ready and not dispatched_now and not any(x.get("status") == "running" for x in nodes.values()):
            idle_lists = set(manual_ready[:6])
        for nid, n in sorted(nodes.items()):
            if n.get("status") != "ready" or n.get("status_unknown") or nid in undisp or nid in stale_ok \
                    or nid in dispatched_now or nid in idle_lists:
                continue
            since = graph.ready_since(nodes, n)
            age = _age(now, since)
            if age is None or age < READY_STALE_MIN * 60:
                continue
            findings.append({"line": lid, "node": nid, "code": "ready_stale", "ready_min": int(age // 60)})
            listed.append((nid, "ready_stale", "就绪 %d 分钟未派" % (age // 60), "ready_stale:%s:%s" % (lid, nid),
                           "ready_stale", since))
        due = [x for x in listed if _alert_due(nudges, x[4], x[3], x[5], now)]
        if due:
            h_ = ((((live or {}).get("routers") or {}).get(lid) or {}).get("handle")) or line.get("router")
            a_ = alerts.setdefault(h_, {"line": lid, "target": h_, "texts": [], "keys": []})
            shown_ = "、".join("%s（%s）" % (x[0], x[2]) for x in due[:8]) + ("等" if len(due) > 8 else "")
            a_["texts"].append("%d 个就绪节点巡检派不出去：%s；请修 spec/route 或自己派" % (len(due), shown_))
            a_["keys"].extend((lid, x[3], x[4]) for x in due)

        if deferred:
            findings.append({"line": lid, "code": "parallel_ceiling" if any(d["why"] == "ceiling" for d in deferred)
                             else "dispatch_held_disk" if any(d["why"] == "disk" for d in deferred) else "dispatch_held", "max_parallel": cap, "running": len(busy) + len(dispatched_now),
                             "deferred": deferred})
        held = []
        for rel in release_candidates(project, line, nodes, live, now, routers_known):
            if released_now[0] >= RELEASE_PER_ROUND:
                held.append(rel["node"])
                continue
            released_now[0] += 1
            actions.append(rel)
        if held:
            findings.append({"line": lid, "code": "release_rate_limited", "count": len(held), "nodes": held[:5]})

        running = [nid for nid, n in nodes.items() if n.get("status") == "running"]
        last_other = None
        for e in events:
            if e.get("line") == lid and (e.get("actor") or {}).get("role") != "patrol":
                if (e.get("at") or "") > (last_other or ""):
                    last_other = e.get("at")
        quiet = _age(now, last_other)
        items = []
        if not running and not dispatched_now:
            if manual_ready:
                items.append("闲置：%s 已 ready 未派，请派发或说明原因" % ",".join(manual_ready[:6]))
            elif last_other is None:
                observe("noevents:%s" % lid, lid, None, "主线 %s 还没有任何事件，巡检不催" % lid, code="no_events")
            elif quiet is not None and quiet > IDLE_S:
                free_gb = None if live_bad else free_worker_mem(state, live)
                any_ready = any(n.get("status") == "ready" for n in nodes.values())
                cinfo_i = ((live or {}).get("coordinators") or {}).get(str(line.get("core"))) or {}
                coord_h = cinfo_i.get("handle") or (core.core_of(project, line.get("core")) or {}).get("coordinator")
                if free_gb is not None and not any_ready and coord_h and cinfo_i.get("present") is not False:
                    # line empty AND machine empty: the router cannot invent work, the core coordinator can
                    ikey = "idle_coord:%s" % lid
                    findings.append({"line": lid, "code": "idle_to_coordinator", "mem_avail_gb": free_gb,
                                     "quiet_s": quiet, "target": coord_h})
                    if cinfo_i.get("screen_flag") == "waiting_input":
                        observe("waiting_coord:%s" % lid, lid, None, "协调人末屏在等输入，不发消息以免误答",
                                code="coordinator_waiting")
                    elif _repeat_due(nudges, "idle_coord", ikey, NUDGE_GAP_S, now):
                        actions.append({"type": "alert", "line": lid, "target": coord_h, "keys": [(lid, ikey, "idle_coord")],
                                        "label": "线空机器空，通知协调人：",
                                        "text": ("[巡检] %s 线空着、机器也空（可用内存 %s GB），router 没法自己造活："
                                                 "请给这条线下一轮范围（挂在完成标准条目上、不依赖别人的活），或写 line_close。"
                                                 % (lid, free_gb))[:900]})
                    else:
                        findings.append({"line": lid, "code": "idle_coord_rate_limited"})
                else:
                    items.append("闲置：%d 小时无事件、无在跑节点，请排下一个节点或写 line_close" % int(quiet // 3600))
        if items:
            findings.append({"line": lid, "code": "idle", "ready": manual_ready, "quiet_s": quiet})

        for nid in sorted(running):
            n = nodes[nid]
            el = _age(now, n.get("dispatched_at"))
            gone_min = _undelivered_min(n, live, now)
            if gone_min is not None:
                findings.append({"line": lid, "node": nid, "code": "undelivered", "finished_min": gone_min})
                items.append("%s 节点在 Orca 已结束 %d 分钟但没写交付记录，请补 progress deliver" % (nid, gone_min))
                continue
            sc = None if (live_bad or _dispatch_imported(n)) else _worker_screen(live, lid, nid, n)
            stalled_for = _age(now, sc.get("since")) if sc else None
            if sc and (stalled_for is not None and stalled_for >= STALL_S or n.get("worker") in exhausted):
                mins = int((stalled_for or 0) // 60)
                findings.append({"line": lid, "node": nid, "code": "worker_stalled", "flag": sc["flag"],
                                 "screen": sc.get("line"), "stalled_min": mins})
                alert(line, nid, "stall", sc.get("since"),
                      "%s 的 worker 已停（屏幕：%s），停了约 %d 分钟；已写好的产物可直接收尾，或重派/写 progress block"
                      % (nid, _short(sc.get("line") or sc["flag"], 100), mins))
                continue
            header = None
            if n.get("expected_duration_minutes") is None and n.get("spec"):
                header = read_spec(n["spec"], repo, runner).get("header")
            exp = expected_minutes(project, n, header)
            is_review = (n.get("kind") or core.infer_node_kind(nid)) == "review"
            if el is not None and exp and el > STUCK_FACTOR * exp * 60 and not _dispatch_imported(n):
                findings.append({"line": lid, "node": nid, "code": "overtime", "elapsed_min": int(el // 60),
                                 "expected_min": exp, "kind": "review" if is_review else "run"})
                alert(line, nid, "overtime", n.get("dispatched_at"),
                      "卡住：%s%s 已跑 %d 分钟（预计 %d），请读一次 worker 屏幕，停在输入框或有报错就按已停处理%s"
                      % ("审查节点 " if is_review else "", nid, el // 60, exp,
                         "；审查节点不用等，已读完就收尾" if is_review else ""))
                continue
            if not (n.get("worker") or n.get("worktree")) or not _on_wsl(n):
                continue
            if _dispatch_imported(n):
                # running state copied from an old graph file: not evidence a worker exists or vanished
                continue
            if live_bad:
                continue
            if el is None or el < GONE_GRACE_S or (_age(now, live.get("generated_at")) or 0) > el:
                continue
            if not _has_worker(live, lid, nid, n):
                findings.append({"line": lid, "node": nid, "code": "process_gone"})
                items.append("卡住：%s 的 worker 进程已不在，但没有交付，请核对后重派或写 block" % nid)

        unrev_old, unrev_all = [], 0
        for nid, n in sorted(nodes.items()):
            if n.get("status") in ("delivered", "failed") and not n.get("review"):
                unrev_all += 1
                age = _age(now, n.get("delivered_at"))
                if age is not None and age > UNREVIEWED_S:
                    unrev_old.append(age)
                    findings.append({"line": lid, "node": nid, "code": "unreviewed", "age_h": round(age / 3600, 1)})
                    items.append("未审：%s 交付 %d 小时未审查" % (nid, age // 3600))

        # review backlog while the router writes nothing: it is probably deep in one long turn and the
        # router nudges queue behind it unseen (11-06 EVB: 16 unreviewed, router silent 50 min) -> coordinator
        quiet = _age(now, router_last.get(lid))
        if unrev_old and unrev_all >= ESCALATE_UNREVIEWED_N and quiet is not None and quiet > ROUTER_QUIET_S \
                and live_bad in (None, "wsl_unknown"):
            cinfo = ((live or {}).get("coordinators") or {}).get(str(line.get("core"))) or {}
            rh = (((live or {}).get("routers") or {}).get(lid) or {}).get("handle") or line.get("router")
            ekey = "unrev_escalate:%s" % lid
            if cinfo.get("present") is True and cinfo.get("handle") and cinfo["handle"] != rh \
                    and _repeat_due(nudges, "unreviewed_escalated", ekey, ABSENT_GAP_S, now):
                findings.append({"line": lid, "code": "unreviewed_escalated", "unreviewed": unrev_all,
                                 "router_quiet_min": int(quiet // 60)})
                text = ("[巡检] %s 线有 %d 个交付没审（最早 %.1f 小时前交），router %s 已 %d 分钟没写任何记录，"
                        "可能在一轮长任务里，发给它的未审催办排在后面看不到。请看一眼它的屏幕：需要就打断它先审、"
                        "给 PARTIAL 排后续节点，或者你代审（progress review --line %s …）。") % (
                    lid, unrev_all, max(unrev_old) / 3600, rh, quiet // 60, lid)
                actions.append({"type": "alert", "line": lid, "target": cinfo["handle"], "text": text[:900],
                                "keys": [(lid, ekey, "unreviewed_escalated")], "label": "审查积压，通知协调人："})

        # router gone / rebound: tell the core coordinator (the router cannot be told)
        rinfo_l = ((live or {}).get("routers") or {}).get(lid) or {}
        fresh = live_bad in (None, "wsl_unknown")
        if fresh and rinfo_l.get("rebound_from") and rinfo_l.get("present"):
            observe("rebound:%s:%s" % (lid, rinfo_l.get("handle")), lid, None,
                    "%s 线 router 已换成新终端 %s（原 %s），project.json 里还是旧的，请更新" % (
                        lid, rinfo_l.get("handle"), rinfo_l["rebound_from"]), code="router_rebound")
        if fresh and rinfo_l.get("present") is False:
            cinfo = ((live or {}).get("coordinators") or {}).get(str(line.get("core"))) or {}
            akey = "absent:%s:%s" % (lid, rinfo_l.get("handle") or line.get("router"))
            if cinfo.get("present") is not True or not cinfo.get("handle"):
                findings.append({"line": lid, "code": "coordinator_absent"})
                observe("coordabsent:%s" % lid, lid, None,
                        "router 和协调人终端都不在，只能记录：%s 线 router %s" % (lid, rinfo_l.get("handle")),
                        code="coordinator_absent")
            elif _repeat_due(nudges, "absent", akey, ABSENT_GAP_S, now):
                pend = items + ["闲置/待办：%s 已 ready" % ",".join(manual_ready[:6])] if manual_ready and not items else items
                findings.append({"line": lid, "code": "router_absent_escalated", "running": len(running)})
                text = ("[巡检] %s 线 router 终端 %s 不在线（run %s），在跑 %d 个节点；待办：%s。"
                        "请开一个新 router 接手（orca orchestration run-use --id %s），或改派这条线。") % (
                    lid, rinfo_l.get("handle") or line.get("router"), line.get("run") or "-", len(running),
                    "；".join(pend)[:300] if pend else "无", line.get("run") or "<run>")
                actions.append({"type": "alert", "line": lid, "target": cinfo["handle"], "text": text[:900],
                                "keys": [(lid, akey, "absent")], "label": "router 不在，通知协调人："})

        if items:
            b = router_bucket(line)
            b["items"].extend("[%s] %s" % (lid, it) for it in items)

    close_acts, close_failed, close_findings = close_candidates(project, state, live, now, routers_known, events,
                                                                runner, apply, set())
    # a terminal that is both releasable and idle gets ONE action: release first, close when that did not free it
    rel_by_term = {a["terminal"]: a for a in actions if a.get("type") == "worker_release"}
    for c in close_acts:
        r = rel_by_term.get(c["terminal"])
        if r:
            c["dispatch"], c["try"] = r["dispatch"], r["try"]
            actions.remove(r)
    actions.extend(close_acts)
    findings.extend(close_findings)
    for f in close_failed:
        qq_active["close_failed:%s" % f["terminal"]] = (
            "%s 的 worker 终端 %s 已空闲且任务结束，但关闭失败 %d 次，请手动关掉" % (
                f["label"], f["terminal"][:16], f["tries"]))

    if live_bad and patrolled:
        observe("live:%s" % live_bad, None, None,
                "在跑快照不可用（%s），本轮不判进程退出" % live_bad, code=live_bad)

    # one message per router: notices (own dispatches) always; problem items at most hourly
    for h, b in per_router.items():
        if not b["items"]:
            continue
        line_id = sorted(b["line_ids"])[0]
        rinfo = ((live or {}).get("routers") or {}).get(line_id) or {}
        if rinfo.get("present") is False:
            observe("absent:%s" % line_id, line_id, None, "router 终端不在，未能催：%s" % "；".join(b["items"])[:80],
                    code="router_absent")
            continue
        if rinfo.get("screen_flag") == "waiting_input":
            observe("waiting:%s" % line_id, line_id, None, "router 末屏在等输入，不发消息以免误答", code="router_waiting")
            continue
        recent = [e for e in nudges if e.get("reason", "nudge") == "nudge"
                  and core._same((e.get("refs") or {}).get("target"), h)
                  and (_age(now, e.get("at")) or 1e9) < NUDGE_GAP_S]
        if recent:
            findings.append({"router": h, "code": "nudge_rate_limited", "last": recent[-1].get("at")})
            continue
        flag = rinfo.get("screen_flag")
        text = "[巡检] " + "；".join(b["items"])
        if flag:
            text += "（末屏判断：%s，若已断流请继续）" % flag
        actions.append({"type": "nudge", "line": line_id, "target": h, "text": text[:900], "reason": "nudge"})

    # stalled / overtime nodes: exempt from the hourly limit, deduped per node (see alert())
    for h, a in alerts.items():
        rinfo = ((live or {}).get("routers") or {}).get(a["line"]) or {}
        if rinfo.get("present") is False:
            observe("absent:%s" % a["line"], a["line"], None, "router 终端不在，未能催：%s" % "；".join(a["texts"])[:80],
                    code="router_absent")
            continue
        if rinfo.get("screen_flag") == "waiting_input":
            observe("waiting:%s" % a["line"], a["line"], None, "router 末屏在等输入，不发消息以免误答",
                    code="router_waiting")
            continue
        actions.append({"type": "alert", "line": a["line"], "target": h, "keys": a["keys"],
                        "text": ("[巡检] " + "；".join(a["texts"]))[:900]})

    # claimed milestones -> core coordinator, at most 2/day
    by_coord: Dict[str, List] = {}
    for mid, m in state["milestones"].items():
        if m.get("status") != "claimed":
            continue
        age = _age(now, m.get("claimed_at"))
        if age is None or age <= CLAIM_S:
            continue
        decl = core.milestone_of(project, mid) or {}
        claim_line = (m.get("claims") or [{}])[-1].get("line")
        lines = [claim_line] + list(decl.get("lines") or [])
        lobj = next((core.line_of(project, l) for l in lines if l and (core.line_of(project, l) or {}).get("patrol") is True), None)
        if not lobj:
            continue
        coord = (core.core_of(project, decl.get("core")) or {}).get("coordinator")
        if coord:
            by_coord.setdefault(coord, []).append((mid, age, lobj["id"]))
    for coord, ms in by_coord.items():
        sent = [e for e in nudges if e.get("reason") == "milestone"
                and core._same((e.get("refs") or {}).get("target"), coord)
                and (_age(now, e.get("at")) or 1e9) < 86400]
        if len(sent) >= REMIND_PER_DAY:
            findings.append({"coordinator": coord, "code": "remind_rate_limited", "sent_24h": len(sent)})
            continue
        text = "[巡检] 待确认关键节点：" + "；".join("%s 已报 %d 小时" % (mid, age // 3600) for mid, age, _ in ms) + \
               "。请用 progress confirm/reject 处理。"
        actions.append({"type": "remind", "line": ms[0][2], "milestone": ms[0][0], "target": coord,
                        "text": text[:900], "reason": "milestone"})

    # disk space. Agents are told only about hosts where workers write (they can stop writing, patrol cleans up);
    # other hosts (the Mac: the user's laptop, no workers) go to the user by QQ. Worker hosts also get a fill projection.
    lage = _age(now, (live or {}).get("generated_at"))
    if isinstance(live, dict) and lage is not None and lage <= LIVE_STALE_S:
        coords = [c for c in ((live.get("coordinators") or {}).values()) if c.get("present") is True and c.get("handle")]
        lrouters = [(lid_, r) for lid_, r in ((live.get("routers") or {}).items()) if r.get("present") is True and r.get("handle")]
        first_line = next((l["id"] for l in project.get("lines") or []), None)
        w_hosts = worker_hosts(state, live)
        for h, rinfo in sorted((live.get("resources") or {}).items()):
            for d in rinfo.get("disks") or []:
                lvl = d.get("level")
                if isinstance(d.get("avail_gb"), (int, float)):
                    lvl = snap.disk_level(d["avail_gb"], h)  # host thresholds apply even to a live.json written by an older snapshot
                if h in w_hosts and isinstance(d.get("avail_gb"), (int, float)):
                    pts = record_disk(pstate["disk_hist"], "%s:%s" % (h, d.get("mount")),
                                      rinfo.get("checked_at") or live.get("generated_at"), d["avail_gb"], now)
                    fill = project_fill(pts, snap.disk_thresholds(h)[1])
                    if fill:
                        findings.append({"host": h, "mount": d.get("mount"), "code": "disk_fill_soon", **fill})
                        qq_active["disk_fill:%s:%s" % (h, d.get("mount"))] = (
                            "%s %s 磁盘每小时少约 %s G（现余 %s G），照这个速度约 %s 小时后降到 %d G 以下" % (
                                h, d.get("mount"), fill["rate_gb_h"], fill["avail"], fill["hours_left"], snap.disk_thresholds(h)[1]))
                if lvl not in RES_GAP_S:
                    continue
                findings.append({"host": h, "mount": d.get("mount"), "code": "disk_" + lvl, "avail_gb": d.get("avail_gb")})
                if h not in w_hosts:
                    qq_active["disk:%s:%s" % (h, d.get("mount"))] = "%s %s 只剩 %s G；这台机器不跑 worker，需要你自己清理" % (
                        h, d.get("mount"), d.get("avail_gb"))
                    continue
                key = "resource:%s:%s:%s" % (h, d.get("mount"), lvl)
                if not _repeat_due(nudges, "resource", key, RES_GAP_S[lvl], now):
                    continue
                text = "[巡检] %s %s 盘只剩 %s G（低于 %d G）：暂停新的大量写盘任务；清理由巡检负责" % (
                    "WSL" if h == "wsl" else h, d.get("mount"), d.get("avail_gb"),
                    snap.disk_thresholds(h)[1 if lvl == "critical" else 0])
                targets = [(c["handle"], c.get("core")) for c in coords]
                if lvl == "critical":
                    targets += [(r["handle"], lid_) for lid_, r in lrouters]
                seen_t = set()
                for t, owner in targets:
                    if t in seen_t:
                        continue
                    seen_t.add(t)
                    ln = owner if core.line_of(project, owner) else next(
                        (l["id"] for l in project.get("lines") or [] if l.get("core") == owner), first_line)
                    if not ln:
                        continue
                    actions.append({"type": "alert", "line": ln, "target": t, "text": text,
                                    "keys": [(ln, key, "resource")], "label": "磁盘告警："})

    if decide_actions and decide.load_qq() is None:
        findings.append({"code": "qq_unconfigured", "why": "~/.progress/qq.json 缺失：裁决结果不会发到 QQ"})
    waiting = decide.pending_inbox(events)
    if waiting:
        findings.append({"code": "inbox_pending", "count": len(waiting)})
    actions.extend(decide_actions)
    actions.extend(observes)
    qq_active.update(monitoring_alerts(live, live_bad))
    qq_active.update(followup_alerts(project, now, events))
    qq_active.update(outage_alerts(project, state, events, live, live_bad, now))
    wt_plan = plan_wt_cleanup(project, state, events, live, live_bad, now, runner,
                              skip=pstate.setdefault("wt_skip", {})) if patrolled else None
    if wt_plan:
        if wt_plan["broken"]:
            findings.append({"code": "worktree_broken", "count": wt_plan["broken"]})
        if apply and wt_plan["remove"]:
            for c in wt_plan["remove"]:
                ln_id = c["nodes"][0].split("/", 1)[0]
                ln_obj = core.line_of(project, ln_id) or {}
                router = (((live or {}).get("routers") or {}).get(ln_id) or {}).get("handle") or ln_obj.get("router")
                actions.append({"type": "worktree_rm", "line": ln_id, "node": c["nodes"][0].split("/", 1)[1],
                                "nodes": c["nodes"], "path": c["path"], "idle_h": c["idle_h"], "size_gb": c.get("size_gb"),
                                "router": router, "common": c.get("common"), "branch": c.get("branch"),
                                "head": c.get("head")})
    for lid_ in patrolled:
        qual = [(k, v) for k, v in new_undisp.items() if k.split("|")[0] == lid_ and (
            v["rounds"] >= UNDISPATCHABLE_ROUNDS
            or (_age(now, v["first_at"]) or 0) >= UNDISPATCHABLE_MIN * 60)]
        if qual:
            whys = sorted({v["why"] for _k, v in qual})
            qq_active["undispatchable:%s" % lid_] = "%s 有 %d 个就绪节点连续派不出去（%s），巡检代派失效" % (
                lid_, len(qual), "；".join(whys))
    # the cache keeps only what is still true: refused-dispatch records of nodes still blocked that way
    pstate["rejected"] = {k: v for k, v in pstate["rejected"].items()
                          if k in cur_rejected or k.split(":")[0] not in patrolled}
    pstate["undispatchable"] = new_undisp
    return {"findings": findings, "actions": actions, "live_problem": live_bad, "patrolled": patrolled,
            "state_events": state["event_count"], "metrics": metrics, "alerts_active": qq_active,
            "resume_state": resume_state, "patrol_state": pstate if pid_ else None, "wt_cleanup": wt_plan}


# -------------------------------------------------------------------- execute

def _send(runner, handle, text):
    rc, out, err = runner([snap.orca_bin(), "terminal", "send", "--terminal", handle, "--text", text,
                           "--enter", "--json"], timeout=60)
    return rc == 0, (err or out or "")[:200]


def execute(store: snap.Store, result: Dict[str, Any], now: _dt.datetime, runner) -> List[Dict[str, Any]]:
    done = []
    notices: Dict[str, List[str]] = {}
    notice_lines: Dict[str, str] = {}   # router -> a line id to log its notice under (worktree cleanup notices)
    who = actor()

    def write(ev):
        ev = dict(ev)
        ev.setdefault("actor", who)
        ev.setdefault("at", snap.iso(now))
        return store.append(ev)

    for a in result["actions"]:
        if a["type"] != "auto_dispatch":
            continue
        lid, nid = a["line"], a["node"]
        ph_id = "ev_patrol_%s" % hashlib.sha1(("%s|%s|%s%s" % (
            store.pid, lid, nid, "|retry%d" % a["retry"] if a.get("retry") else "")).encode()).hexdigest()[:16]
        try:
            res = write({"id": ph_id, "kind": "auto_dispatch", "line": lid, "node": nid, "route": a["route"],
                         "summary": _short("巡检代派 %s（%s）" % (nid, a["route"].get("model") or a["route"].get("agent"))),
                         "refs": {"spec": a["spec_ref"], "worktree": a["name"]}})
        except core.ProgressError as exc:
            done.append({"type": "auto_dispatch", "node": nid, "skipped": "placeholder_failed", "error": exc.as_dict()})
            continue
        if res.get("duplicate"):
            done.append({"type": "auto_dispatch", "node": nid, "skipped": "placeholder_exists"})
            continue
        rc, out, err = runner(a["argv"], timeout=260)
        st = parse_start(rc, out) if rc != 124 else {"ok": False, "unknown": True, "why": "worker-start 超时，worker 可能已起"}
        rec = {"type": "auto_dispatch", "line": lid, "node": nid, "start": st}
        if st["ok"]:
            refs = {k: st[k] for k in ("dispatch", "task", "terminal", "worktree") if st.get(k)}
            try:
                write({"kind": "auto_dispatch", "phase": "started", "line": lid, "node": nid,
                       "summary": _short("代派已起：%s" % (st.get("dispatch"))), "refs": refs})
            except core.ProgressError as exc:
                rec["started_event_error"] = exc.as_dict()
            notices.setdefault(a["router"], []).append(
                "已代派 %s/%s（%s，dispatch %s）" % (lid, nid, a["route"].get("model") or a["route"].get("agent"),
                                                st.get("dispatch")))
            pst = load_patrol_state(store.pid)
            if pst["rejected"].pop("%s:%s" % (lid, nid), None) is not None:
                save_patrol_state(store.pid, pst)
        else:
            why_rej = classify_start(rc, out, err, st)
            if why_rej is not None:
                tries = int(a.get("retry") or 0) + 1
                final = tries >= REJECT_MAX_TRIES
                rec["rejected"] = {"tries": tries, "why": why_rej}
                pst = load_patrol_state(store.pid)
                pst["rejected"]["%s:%s" % (lid, nid)] = {
                    "tries": tries, "last_why": why_rej, "argv_hash": a.get("argv_hash"),
                    "spec_ref": a.get("spec_ref"), "last_at": snap.iso(now)}
                save_patrol_state(store.pid, pst)
                try:
                    write({"kind": "node_block", "blocker": "env", "line": lid, "node": nid,
                           "summary": _short("%s（%s），%s" % (REJECT_MARK, why_rej, "已试 %d 次，不再重试" % tries
                                                              if final else "巡检会在修正后重试"))})
                except core.ProgressError as exc:
                    rec["block_event_error"] = exc.as_dict()
                notices.setdefault(a["router"], []).append(
                    "代派 %s/%s 被 Orca 拒绝（%s），第 %d/%d 次；%s" % (
                        lid, nid, why_rej[:120], tries, REJECT_MAX_TRIES,
                        "已不再重试，请自行派发" if final else "巡检会在修正后重试，你也可以先改 spec/route"))
            else:
                try:
                    write({"kind": "node_block", "blocker": "env", "line": lid, "node": nid,
                           "summary": _short("代派失败，不重试：%s" % st["why"])})
                except core.ProgressError as exc:
                    rec["block_event_error"] = exc.as_dict()
                notices.setdefault(a["router"], []).append(
                    "代派 %s/%s 失败（%s），已记 block，不重试；%s" % (
                        lid, nid, st["why"][:120], "请先核对是否已有 worker" if st.get("unknown") else "请处理后自行派发"))
        done.append(rec)

    rm_acts = [a for a in result["actions"] if a["type"] == "worktree_rm"]
    if rm_acts:
        removed: List[Dict[str, Any]] = []
        for a in rm_acts:
            argv = [snap.orca_bin(), "worktree", "rm", "--environment", snap.remote_host(),
                    "--worktree", "path:%s" % a["path"], "--json"]   # never --force, never --delete-branch
            rc, out, err = runner(argv, timeout=180)
            doc = snap.parse_json_tail(out)
            ok = rc == 0 and not (isinstance(doc, dict) and doc.get("ok") is False)
            rec = {"type": "worktree_rm", "path": a["path"], "nodes": a["nodes"], "removed": ok,
                   "freed_gb": a.get("size_gb") if ok else 0}
            if not ok:
                rec["error"] = _short(err or out or "rc=%s" % rc, 160)
            else:
                removed.append(a)
                name = os.path.basename(a["path"].rstrip("/"))
                try:
                    write({"kind": "observe", "line": a["line"], "node": a["node"],
                           "summary": _short("清理已完成节点的工作区 %s（节点 %s，空闲 %s 小时，释放约 %s G）" % (
                               name, ",".join(a["nodes"]), a["idle_h"], a.get("size_gb") if a.get("size_gb") is not None else "?")),
                           "refs": {"key": "wt_rm:%s" % a["path"]}})
                except core.ProgressError as exc:
                    rec["event_error"] = exc.as_dict()
            done.append(rec)
        checks = [a for a in removed if a.get("branch") and a.get("common") and WT_PATH_RE.match(a["common"])
                  and re.match(r"^[A-Za-z0-9_./+-]+$", a["branch"])]
        lost: List[str] = []
        restored: List[str] = []
        if checks:
            # The branch must still exist after the removal. Older Orca hosts ignore keep-by-default and delete a branch
            # they can prove merged (11-06: 5 SR branches lost), so a missing branch is recreated at the HEAD the scan saw
            # (the worktree was clean and on that branch, so that HEAD is the branch tip).
            parts = []
            for a in checks:
                gd, br, hd = shlex.quote(a["common"]), shlex.quote(a["branch"]), a.get("head") or ""
                hd = hd if re.fullmatch(r"[0-9a-f]{7,40}", hd) else ""
                parts.append(
                    'if git --git-dir=%s rev-parse --verify -q refs/heads/%s >/dev/null 2>&1; then echo "@@BR %s 0"; '
                    'elif [ -n "%s" ] && git --git-dir=%s branch %s %s >/dev/null 2>&1; then echo "@@BR %s R"; '
                    'else echo "@@BR %s 1"; fi' % (gd, br, a["path"], hd, gd, br, hd or "HEAD", a["path"], a["path"]))
            rc, out, _err = runner(snap.ssh_argv(snap.remote_host(), "; ".join(parts)), timeout=60)
            res_br = {ln.split()[1]: ln.split()[-1] for ln in (out or "").splitlines() if ln.startswith("@@BR ")}
            restored = [a["path"] for a in checks if res_br.get(a["path"]) == "R"]
            lost = [a["path"] for a in checks if res_br.get(a["path"]) not in ("0", "R")]
        done.append({"type": "worktree_cleanup", "removed": len(removed), "failed": len(rm_acts) - len(removed),
                     "freed_gb": round(sum(a.get("size_gb") or 0 for a in removed), 1),
                     "branch_unverified": lost, "branch_restored": restored})
        per_router: Dict[str, List[Dict[str, Any]]] = {}
        for a in removed:
            if a.get("router"):
                per_router.setdefault(a["router"], []).append(a)
        for h, rows in per_router.items():
            notices.setdefault(h, []).append("已清理 %d 个已完成工作区，分支保留" % len(rows))
            notice_lines.setdefault(h, rows[0]["line"])

    nudge_by_target = {a["target"]: a for a in result["actions"] if a["type"] == "nudge"}
    for target in set(notices) | set(nudge_by_target):
        a = nudge_by_target.get(target)
        parts = ["[巡检] " + "；".join(notices[target])] if notices.get(target) else []
        if a:
            parts.append(a["text"])
        text = " ".join(parts)[:1200]
        line_id = a["line"] if a else next((x["line"] for x in result["actions"]
                                            if x["type"] == "auto_dispatch" and x["router"] == target), None) or notice_lines[target]
        ok, why = _send(runner, target, text)
        rec = {"type": "nudge" if a else "notice", "target": target, "sent": ok}
        if not ok:
            rec["error"] = why
        try:
            write({"kind": "nudge" if ok else "observe", "line": line_id,
                   "reason": "nudge" if a else "notice",
                   "summary": _short(("催 router：" if ok else "发给 router 失败：") + text[6:]),
                   "refs": {"target": target, "key": "send_fail:%s" % target} if not ok else {"target": target}})
        except core.ProgressError as exc:
            rec["event_error"] = exc.as_dict()
        done.append(rec)

    for a in result["actions"]:
        if a["type"] == "auto_continue":
            ok, why, meta = snap.terminal_send(runner, a["target"], a["text"], a.get("env"))
            rec = {"type": "auto_continue", "target": a["target"], "role": a["role"], "try": a["try"], "sent": ok}
            if not ok:
                rec["error"] = why
            try:
                if ok:
                    write({"kind": "nudge", "line": a["line"], "reason": "auto_continue",
                           "summary": _short("自动继续（第%d次，%s）：%s" % (a["try"], a["role"], a["screen"])),
                           "refs": {"target": a["target"], "key": a["key"]}})
                else:
                    write({"kind": "observe", "line": a["line"], "summary": _short("自动继续发送失败：%s" % why),
                           "refs": {"key": "send_fail:%s" % a["target"]}})
            except core.ProgressError as exc:
                rec["event_error"] = exc.as_dict()
            done.append(rec)
        elif a["type"] == "alert":
            ok, why = _send(runner, a["target"], a["text"])
            rec = {"type": "alert", "target": a["target"], "sent": ok}
            if not ok:
                rec["error"] = why
            try:
                for lid, key, reason in a["keys"]:
                    if ok:
                        write({"kind": "nudge", "line": lid, "reason": reason, "summary": _short(a.get("label", "催 router：") + a["text"][6:]),
                               "refs": {"target": a["target"], "key": key}})
                    else:
                        write({"kind": "observe", "line": lid, "summary": _short("发给 router 失败：%s" % why),
                               "refs": {"key": "send_fail:%s" % a["target"]}})
            except core.ProgressError as exc:
                rec["event_error"] = exc.as_dict()
            done.append(rec)
        elif a["type"] == "remind":
            ok, why = _send(runner, a["target"], a["text"])
            rec = {"type": "remind", "target": a["target"], "sent": ok}
            try:
                if ok:
                    write({"kind": "nudge", "line": a["line"], "milestone": a["milestone"], "reason": "milestone",
                           "summary": _short("提醒协调人：" + a["text"][6:]), "refs": {"target": a["target"]}})
                else:
                    write({"kind": "observe", "line": a["line"], "summary": _short("提醒协调人失败：%s" % why),
                           "refs": {"key": "remind_fail:%s" % a["target"]}})
            except core.ProgressError as exc:
                rec["event_error"] = exc.as_dict()
            done.append(rec)
        elif a["type"] == "worker_release":
            rc, out, err = runner([snap.orca_bin(), "orchestration", "worker-release", "--dispatch", a["dispatch"],
                                   "--json"], timeout=60)
            st = parse_release(rc, out) if rc != 124 else {"status": "timeout", "released": False, "final": False}
            rec = {"type": "worker_release", "node": a["node"], "dispatch": a["dispatch"], "result": st["status"],
                   "released": st["released"]}
            if rc != 0:
                rec["error"] = (err or out or "")[:200]
            try:
                write({"kind": "worker_release", "line": a["line"], "node": a["node"], "released": st["released"],
                       "result": st["status"], "final": st["final"],
                       "summary": _short("交付后释放 worker 终端（%s，第%d次）：%s" % (a["node"], a["try"], st["status"])),
                       "refs": {"dispatch": a["dispatch"], "terminal": a["terminal"]}})
            except core.ProgressError as exc:
                rec["event_error"] = exc.as_dict()
            done.append(rec)
        elif a["type"] == "worker_close":
            rec = {"type": "worker_close", "node": a["node"], "terminal": a["terminal"], "idle_min": a["idle_min"]}
            outcome, released = None, False
            if a.get("dispatch"):
                rc, out, err = runner([snap.orca_bin(), "orchestration", "worker-release", "--dispatch", a["dispatch"],
                                       "--json"], timeout=60)
                st = parse_release(rc, out) if rc != 124 else {"status": "timeout", "released": False, "final": False}
                rec["release"] = st["status"]
                try:
                    write({"kind": "worker_release", "line": a["line"], "node": a["node"], "released": st["released"],
                           "result": st["status"], "final": st["final"],
                           "summary": _short("交付后释放 worker 终端（%s）：%s" % (a["node"], st["status"])),
                           "refs": {"dispatch": a["dispatch"], "terminal": a["terminal"]}})
                except core.ProgressError as exc:
                    rec["event_error"] = exc.as_dict()
                released = st["released"]
                outcome = "released" if released else None
            if not released:
                argv = [snap.orca_bin(), "terminal", "close"] + (["--environment", a["env"]] if a.get("env") else []) + \
                       ["--terminal", a["terminal"], "--json"]
                rc, out, err = runner(argv, timeout=60)
                doc = snap.parse_json_tail(out)
                ok = rc == 0 and not (isinstance(doc, dict) and doc.get("ok") is False)
                outcome = "closed" if ok else "close_failed: %s" % _short(err or out or "rc=%s" % rc, 100)
                released = ok
                try:
                    write({"kind": "worker_release", "line": a["line"], "node": a["node"], "released": ok,
                           "result": outcome, "final": ok, "close": True,
                           "summary": _short("关闭空闲 worker 终端（%s，空闲 %d 分钟）%s" % (
                               a["label"], a["idle_min"], "" if ok else "：失败，第%d次" % a["try"])),
                           "refs": {"terminal": a["terminal"]}})
                except core.ProgressError as exc:
                    rec["event_error"] = exc.as_dict()
            rec.update(result=outcome, closed=released)
            done.append(rec)
        elif a["type"] == "observe":
            try:
                write(a["event"])
                done.append({"type": "observe", "key": a["event"]["refs"]["key"]})
            except core.ProgressError as exc:
                done.append({"type": "observe", "key": a["event"]["refs"]["key"], "error": exc.as_dict()})
    if any(a["type"] == "decide" for a in result["actions"]) or decide.read_inbox():
        def send_router(target, text):
            ok, _why = _send(runner, target, text)
            return ok
        done.extend(decide.run_round(store, result["actions"], runner, write, read_spec, repo_path, send_router))
    return done


def send_alerts(store: snap.Store, active: Dict[str, str], now: _dt.datetime, runner,
                findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """QQ alerts (alerts.sync) + one `observe` event per message sent. Never raises."""
    recs = qq_alerts.sync(active, now, runner, decide.load_qq())
    for r in recs:
        if r.get("code"):
            if not any(f.get("code") == r["code"] for f in findings):
                findings.append(r)
            continue
        if not r.get("sent"):
            continue
        try:
            store.append({"kind": "observe", "actor": actor(), "at": snap.iso(now),
                          "summary": _short("QQ 告警（%s）：%s" % (r["kind"], r["text"])),
                          "refs": {"key": "alert:%s" % r["key"]}})
        except Exception as exc:  # noqa: BLE001
            r["event_error"] = str(exc)[:120]
    return [{k: v for k, v in r.items() if k != "text"} if r.get("code") else r for r in recs]


REVIEW_STAGE1_CAP_S = 60   # wall clock for the script-only project review inside a patrol round
REVIEW_KEEP_REMOTE = 10


def sync_review_to_wsl(pid: str, dossier_path: str, runner) -> Dict[str, Any]:
    """Copy the dossier .md/.json to ~/.progress/<pid>/project_review/ on the board host (scp), keep the newest 10."""
    host = snap.remote_host()
    md = pathlib.Path(dossier_path)
    files = [str(f) for f in (md, md.with_suffix(".json")) if f.is_file()]
    if not files:
        return {"synced": False, "why": "no_files"}
    rdir = ".progress/%s/project_review" % pid
    rc, _o, err = runner(snap.ssh_argv(host, "mkdir -p %s" % shlex.quote(rdir)), timeout=30)
    if rc != 0:
        return {"synced": False, "why": "mkdir rc=%s %s" % (rc, (err or "")[:80])}
    rc, _o, err = runner(["scp", "-q", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes"] + files + ["%s:%s/" % (host, rdir)],
                         timeout=60)
    if rc != 0:
        return {"synced": False, "why": "scp rc=%s %s" % (rc, (err or "")[:80])}
    prune = "cd %s && for p in 'dossier-*.md' 'dossier-*.json'; do ls -1 $p | sort | head -n -%d | xargs -r rm -f; done" % (
        shlex.quote(rdir), REVIEW_KEEP_REMOTE)
    runner(snap.ssh_argv(host, prune), timeout=30)
    return {"synced": True}


def project_review_round(pid: str, project: Dict[str, Any], events, apply: bool, runner, now: _dt.datetime,
                         review_fn=None, cap_s: float = REVIEW_STAGE1_CAP_S) -> Dict[str, Any]:
    """One patrol round's share of the project-level review (stage 1 only). Never raises, never blocks past cap_s."""
    import threading
    if not apply:
        return {"ran": False, "reason": "dry_run"}
    try:
        if review_fn is None:
            if os.environ.get("PROGRESS_PROJECT_REVIEW") == "off":
                return {"ran": False, "reason": "disabled"}
            import project_review as pr  # lazy: project_review imports supervisor, which imports patrol helpers
            review_fn = pr.run
            stage = int(pr.cfg(project).get("stage") or 1)
        else:
            stage = int(((project.get("defaults") or {}).get("project_review") or {}).get("stage") or 1)
        if stage >= 2:
            return {"ran": False, "reason": "stage_unwired", "stage": stage}
        box: Dict[str, Any] = {}

        def work():
            try:
                box["res"] = review_fn(pid, stage=stage, now=now, runner=runner)
            except BaseException as exc:  # noqa: BLE001
                box["err"] = "%s: %s" % (type(exc).__name__, str(exc)[:120])
        th = threading.Thread(target=work, daemon=True)
        th.start()
        th.join(cap_s)
        if th.is_alive():
            return {"ran": True, "ok": False, "why": "timeout"}
        if "err" in box:
            return {"ran": True, "ok": False, "why": box["err"]}
        res = box.get("res") or {}
        out = {"ran": bool(res.get("ran")), "ok": res.get("ok", True), "why": res.get("why") or res.get("reason") or res.get("trigger"),
               "path": res.get("dossier_path")}
        if res.get("why") == "holdout_hit":
            out["hits"] = res.get("hits")
        if out["ran"] and out["ok"] and res.get("dossier_path"):
            try:
                out["sync"] = sync_review_to_wsl(pid, res["dossier_path"], runner)
            except Exception as exc:  # noqa: BLE001
                out["sync"] = {"synced": False, "why": str(exc)[:80]}
        return out
    except Exception as exc:  # noqa: BLE001
        return {"ran": True, "ok": False, "why": "%s: %s" % (type(exc).__name__, str(exc)[:120])}


def run(pid: str, apply: bool = False, runner=snap.default_runner, now: Optional[_dt.datetime] = None,
        store: Optional[snap.Store] = None, supervisor: bool = False, force_supervisor: bool = False,
        model_call=None, review_fn=None) -> Dict[str, Any]:
    now = now or snap.utcnow()
    if off_file().exists():
        return {"ok": True, "project": pid, "off": True, "actions": [], "mode": "apply" if apply else "dry-run"}
    store = store or snap.Store(pid, runner)
    project, events, live = store.read()
    result = plan(project, events, live, now, runner, apply=apply)
    shown = []
    for a in result["actions"]:
        if a.get("type") == "auto_dispatch":
            a = dict(a, argv=a["argv"][:4] + ["<spec %d bytes>" % a["spec_bytes"]] + a["argv"][5:])
        shown.append(a)
    out = {"ok": True, "project": pid, "now": snap.iso(now), "mode": "apply" if apply else "dry-run"}
    out.update(result)
    out["actions"] = shown
    out["resume_files"] = write_resume_packs(pid, project, events, now)
    new_seen = out.pop("resume_state", None)
    new_pstate = out.pop("patrol_state", None)
    if apply and new_seen is not None:
        save_resume_seen(pid, new_seen)
    if apply and new_pstate is not None:
        save_patrol_state(pid, new_pstate)
    if apply:
        out["results"] = execute(store, result, now, runner)
        lost = [x for r in out["results"] for x in r.get("branch_unverified") or []]
        if lost:
            out["findings"].append({"code": "worktree_branch_unverified", "paths": lost})
            result.setdefault("alerts_active", {})["wt_branch_lost"] = (
                "清理工作区后没能确认分支还在（%d 个：%s），请手动核对" % (len(lost), "；".join(lost)[:200]))
        out["project_review"] = project_review_round(pid, project, events, True, runner, now, review_fn)
        pr_out = out["project_review"]
        if pr_out.get("reason") == "stage_unwired":
            out["findings"].append({"code": "project_review_stage_unwired", "stage": pr_out.get("stage")})
        try:
            ps = load_patrol_state(pid)
            if pr_out.get("why") == "holdout_hit":
                ps["review"] = {"holdout_hits": int(pr_out.get("hits") or 0)}
                save_patrol_state(pid, ps)
            elif pr_out.get("ran") and pr_out.get("ok") and ps["review"]:
                ps["review"] = {}
                save_patrol_state(pid, ps)
            if ps["review"].get("holdout_hits") is not None:
                result.setdefault("alerts_active", {})["project_review:holdout"] = (
                    "项目级档案命中留出集名单（%s 行），已拒绝生成，未写文件；内容不在此处显示" % ps["review"]["holdout_hits"])
        except Exception as exc:  # noqa: BLE001
            out["findings"].append({"code": "project_review_state_error", "error": str(exc)[:80]})
        out["alerts"] = send_alerts(store, result.get("alerts_active") or {}, now, runner, out["findings"])
    if not apply:
        out["project_review"] = project_review_round(pid, project, events, False, runner, now, review_fn)
    if supervisor or force_supervisor:
        import supervisor as sup  # lazy: supervisor imports patrol helpers
        rounds = out["results"] if apply else [dict(a, planned=True) for a in shown if a.get("type") != "observe"]
        rounds = [{k: v for k, v in r.items() if k not in ("text", "argv", "event")} for r in rounds]
        info = sup.pulse(pid, project, events, live, now, runner, store, apply=apply, force=force_supervisor,
                         round_actions=rounds, model_call=model_call)
        info.pop("report", None) if not force_supervisor else None
        out["supervisor"] = info
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project", default=os.environ.get("PROGRESS_PROJECT", "dse"))
    p.add_argument("--apply", action="store_true", help="perform the actions (default: dry run)")
    p.add_argument("--no-supervisor", action="store_true", help="skip the 30-minute supervisor (model) pulse")
    p.add_argument("--force-supervisor", action="store_true", help="run the supervisor now, whatever the 30-minute clock says")
    a = p.parse_args(argv)
    try:
        out = run(a.project, apply=a.apply, supervisor=not a.no_supervisor, force_supervisor=a.force_supervisor)
    except core.ProgressError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False))
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
