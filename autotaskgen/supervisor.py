#!/usr/bin/env python3
"""Supervisor (巡检员): every >= 30 min patrol hands a map of the project to a model that spot-checks the
lines with `progress-inspect` and writes a structured report. Called from patrol, not a launchd job.

- map: final goal, milestones, per line nodes (status/kind/deps/timing), last 5 events, router screen tail,
  live worker screens, script actions since the last call, the previous report (<= 40k chars).
- model: `codex exec -m gpt-6.1-terra` with --output-schema; sandbox is OFF (user decision: trust terra,
  constrain by prompt, audit afterwards): the codex --json event stream is scanned and any write-like command
  sets report["policy_violation"] and suppresses all messages that round.
- messages go out only under --apply: routers <= 1/h, coordinators <= 1/2 h; each action is recorded in the
  follow-up ledger st["followups"] (spec §C), re-checked by the model, re-sent once, then escalated to the
  coordinator, then left for the human digest (not built yet). Report format v2 (spec §B); v1 docs are migrated.
- PROGRESS_SUPERVISOR_CMD replaces the codex command (tests): prompt on stdin, env PROGRESS_SUPERVISOR_SCHEMA /
  _OUT (write the final message there) / _MAP; stdout is read as the JSON event stream.
"""
from __future__ import annotations

import datetime as _dt
import difflib
import hashlib
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import progress_core as core
import snapshot as snap

PULSE_S = 30 * 60
SEND_GAP_S = 3600
TIMEOUT_S = 10 * 60
BACKOFF_S = 60 * 60   # after a failed (rc != 0 / timeout) call, skip the model this long (11-06: 900 s timeouts stretched patrol rounds to 65 min)
MAP_MAX = 60000
MODEL = "gpt-6.1-terra"
SUP_HANDLE = "supervisor@mac"
STATUSES = ("ok", "attention", "problem")
KINDS = ("stalled", "looping", "hallucinating", "drifting", "idle_parallelism", "no_progress", "other",
         "waiting_human", "no_next_step", "dispatch_broken", "mislabeled", "metric_breach", "followup_overdue",
         "review_heavy", "stale_state", "graph_gap")
ACTION_TO = ("router", "coordinator", "human", "tooling")
BLOCK_CLASSES = ("human", "dep", "no_next_step", "env", "resource")
VERDICTS = ("done", "not_done", "obsolete", "cannot_tell")
SCHEMA_ID = "progress-supervisor/v2"
HUMAN_RE = re.compile(r"人类|授权|端点|首回复|材料|endpoint|批准", re.I)
# the human step is a later stage, not the current wait ("B8 交付后再请求人类授权", "之后才转 human")
LATER_RE = re.compile(r"(之后|以后|交付后|完成后|固定后|通过后|后续|后再|再转\s*human|再请求|才需要|才转|才请求)")
# a dep-labelled wait whose text also names a human step is a wait on both, not a mislabel (11-07: FE waited on the
# B13a handoff and on the human budget approval; terra flagged it mislabeled for being dep, then for being counted human)
DEP_RE = re.compile(r"交付|交接|未收|未交|待交|待[^；;，,。\s]{1,12}?交|到件|产物|产出|依赖")
# a non-human block whose human-sounding words are about budget or call quota waits on a ruling, not on the human
# (11-07: FE "撤回虚假L46批准，授权未到；B13a端点/固定版本亦待EVB经核二交，保留no-auto零调用" was counted as 17 h
# waiting on the human and flagged mislabeled; terra asked that a budget wait no longer count as waiting on the human)
BUDGET_RE = re.compile(r"预算|额度|算力|配额|调用|token|机器", re.I)
# ...unless a clause outside the dependency wait names something only a person can give (a real endpoint, materials)
HUMAN_ONLY_RE = re.compile(r"端点|首回复|材料|endpoint|目标|完成标准|留出集|对外|发布|账号|权限", re.I)
TIER3_RE = re.compile(r"目标|预算|对外|留出集|只有人|授权|端点|人类|完成标准|发布")
ACTIVE = ("running", "ready", "blocked")
CHECK_MIN, CHECK_MAX, CHECK_DEFAULT = 30, 360, 60
FU_EXPIRE_H = 24          # open follow-ups older than this close as expired (human-waiting ones are exempt)
COORD_GAP_S = 2 * 3600    # at most one message per coordinator per 2 h
ESCALATE_MAX = 3          # escalations per round
HUMAN_RECHECK_MIN = 360
# attention / problem thresholds (SUPERVISOR_TERRA_SPEC §D); project.defaults.supervisor_thresholds overrides
THRESH: Dict[str, Tuple[float, float]] = {
    "ready_undispatched_h": (0.25, 1), "critical_stall_min": (60, 180), "wait_human_h": (2, 8),
    "review_run": (0.20, 0.35), "delivered_6h_h": (6, 12), "empty_line_h": (1, 4), "rework": (2, 3),
    "stale_state_h": (6, 24), "unattached_ratio": (0.5, 9),
}


def _obj(props: Dict[str, Any]) -> Dict[str, Any]:
    # codex --output-schema is strict: every property required, no extras, optional = nullable
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


_S = {"type": "string"}
_NS = {"type": ["string", "null"]}
REPORT_SCHEMA = _obj({
    "summary": _S,
    "critical_path": _S,
    "blockers": {"type": "array", "items": _obj({
        "item": _S, "line": _S, "class": {"type": "string", "enum": list(BLOCK_CLASSES)},
        "since_hours": {"type": ["number", "null"]}, "who_unblocks": _S, "evidence": _S})},
    "lines": {"type": "array", "items": _obj({
        "line": _S,
        "status": {"type": "string", "enum": list(STATUSES)},
        "progress_note": _S,
        "milestone_position": _S,
        "issues": {"type": "array", "items": _obj({"kind": {"type": "string", "enum": list(KINDS)}, "evidence": _S})},
        "actions": {"type": "array", "items": _obj({
            "to": {"type": "string", "enum": list(ACTION_TO)}, "text": _S, "done_when": _S,
            "check_after_min": {"type": "integer"},
            "verify": _obj({"line": _NS, "node": _NS, "event_kind": _NS})})}})},
    "followups": {"type": "array", "items": _obj({
        "id": _S, "verdict": {"type": "string", "enum": list(VERDICTS)}, "evidence": _S})},
    "metric_findings": {"type": "array", "items": _obj({
        "line": _S, "metric": _S, "value": _S, "verdict": {"type": "string", "enum": ["real", "explained"]},
        "evidence": _S})},
    "goal_drafts": {"type": "array", "items": _obj({"topic": _S, "current": _S, "proposal": _S, "why": _S})},
})

PROMPT = """你是 dse 项目的巡检员。你不干活，只做三件事：判断项目离最终目标还差什么、现在谁在卡、上次的建议有没有落实；然后写一份结构化报告，由脚本发给该收的人。

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
summary、critical_path、progress_note、issues.evidence、blockers 会原样显示在看板上给人看，写成不了解内部机制的人也能读懂的白话：不写跟进编号（fu_…）、事件 id、终端句柄和内部词（"地图""STATE""tooling""blocked/human"之类），改说"巡检汇总""该线的状态记录""在等人"；比值写成带意思的百分比（"审查时间是执行时间的 31%，提醒线 20%"）；里程碑编号第一次出现时后面括号写几个字说明它是什么。actions 是发给 router 的，可以照旧带编号。
"""

# commands that change something: audit only (the prompt forbids them)
_WRITE_RES = [
    (re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?(commit|push|reset|checkout|clean|rebase|merge|add|stash|restore|switch|cherry-pick|am|apply|pull|fetch)\b"), "git 写操作"),
    (re.compile(r"(?:^|[\s;&|(`\"'])(rm|mv|cp|touch|mkdir|rmdir|chmod|chown|ln|truncate|dd|kill|pkill)\s"), "改文件/进程"),
    (re.compile(r"\bsed\s+(?:-[a-zA-Z]*i|--in-place)"), "sed -i"),
    (re.compile(r"\btee\b"), "tee 写文件"),
    (re.compile(r"(?<![0-9&>])>>?(?![&>])(?!\s*/dev/null)"), "重定向写文件"),
    (re.compile(r"\borca\b.*\b(?:terminal\s+(?:send|stop|close|create)|worker-start|worker-stop|worker-send|orchestration\s+(?:send|stop|start|dispatch|cancel))"), "orca 发送/启停"),
    (re.compile(r"\bworker-(?:start|stop)\b|\bterminal\s+send\b"), "orca 发送/启停"),
    (re.compile(r"\bprogress\s+(?:append|deliver|plan|dispatch|block|review|nudge|supersede|cancel|confirm|reject)\b"), "写 progress 事件"),
    (re.compile(r"open\([^)]*['\"][wa]b?\+?['\"]"), "脚本写文件"),
]


# ---------------------------------------------------------------- state (task cache, Mac local)

def _skey(pid: str) -> str:
    return "supervisor:" + pid


def load_state(pid: str) -> Dict[str, Any]:
    return snap.load_task_cache().get(_skey(pid)) or {}


def save_state(pid: str, st: Dict[str, Any]) -> None:
    cache = snap.load_task_cache()
    cache[_skey(pid)] = st
    snap.save_task_cache(cache)


def backing_off(st: Dict[str, Any], now: _dt.datetime) -> bool:
    """The previous model call failed less than BACKOFF_S ago."""
    failed = core.parse_iso(st.get("last_fail_at"))
    return failed is not None and 0 <= (now - failed).total_seconds() < BACKOFF_S


def due(st: Dict[str, Any], now: _dt.datetime, force: bool = False) -> bool:
    if force:
        return True
    last = core.parse_iso(st.get("last_call_at"))
    return last is None or (now - last).total_seconds() >= PULSE_S


# ---------------------------------------------------------------- map sections (A1-A4)

def _ev_t(at: Optional[str]) -> Optional[_dt.datetime]:
    return core.parse_iso(at)


def _hrs(now: _dt.datetime, at: Optional[str]) -> Optional[float]:
    t = _ev_t(at)
    return None if t is None else max(0.0, (now - t).total_seconds() / 3600.0)


def thresholds(project: Dict[str, Any]) -> Dict[str, Tuple[float, float]]:
    t = dict(THRESH)
    over = (((project.get("defaults") or {}).get("supervisor_thresholds")) or {})
    for k, v in over.items():
        if k in t and isinstance(v, (list, tuple)) and len(v) == 2:
            t[k] = (v[0], v[1])
    return t


def _short_t(at: Optional[str]) -> str:
    return (at or "-")[5:16].replace("T", " ") + "Z" if at else "-"


def milestone_frontier(project: Dict[str, Any], state: Dict[str, Any]) -> List[str]:
    """A1: goal, done_when, milestone rows, frontier (unconfirmed, all `after` confirmed) with the nodes on it."""
    g = project.get("final_goal") or {}
    dw = g.get("done_when")
    dw = "\n".join(str(x) for x in dw) if isinstance(dw, list) else str(dw or "")
    o = [str(g.get("text") or ""), "完成标准：%s" % dw]
    if not dw.strip() or re.search(r"由.{0,12}协调人.{0,12}(写明|确定|定)", dw):
        o.append("⚠ 完成标准未写成可核对的条件")
    ms_state = state.get("milestones") or {}
    mls = [m for m in project.get("milestones") or [] if isinstance(m, dict) and m.get("id")]

    def confirmed(mid: str) -> bool:
        return (ms_state.get(mid) or {}).get("status") == "confirmed"

    frontier = [m["id"] for m in mls if not confirmed(m["id"]) and all(confirmed(a) for a in m.get("after") or [])]
    o.append("里程碑（前沿 = 自身未确认、前置全部已确认）：")
    for m in mls:
        o.append("- %s [%s] %s | 前置:%s | 线:%s | %s" % (
            m["id"], (ms_state.get(m["id"]) or {}).get("status"), str(m.get("title"))[:80],
            ",".join(m.get("after") or []) or "-", ",".join(m.get("lines") or []) or "-",
            "前沿" if m["id"] in frontier else ""))
    for mid in frontier:
        m = next(x for x in mls if x["id"] == mid)
        o.append("前沿 %s 上的节点：" % mid)
        attached, unatt = [], []
        for lid, ls in sorted((state.get("lines") or {}).items()):
            for nid, n in sorted((ls.get("nodes") or {}).items()):
                if n.get("status") not in ACTIVE:
                    continue
                if mid in (n.get("milestones") or []):
                    attached.append("%s/%s [%s]" % (lid, nid, n.get("status")))
                elif not n.get("milestones") and lid in (m.get("lines") or []):
                    unatt.append("%s/%s [%s]" % (lid, nid, n.get("status")))
        o.append("  挂在它上面：%s" % ("；".join(attached) or "（无在跑/就绪/卡住节点）"))
        if unatt:
            o.append("  未挂里程碑的活跃节点（请判断归属）：%s" % "；".join(unatt))
    for cl in project.get("cross_links") or []:
        if isinstance(cl, dict):
            o.append("跨线依赖：%s → %s %s" % (cl.get("from"), cl.get("to"), cl.get("note") or ""))
    return o


def _wait_info(n: Dict[str, Any], now: _dt.datetime) -> Dict[str, Any]:
    hist = n.get("history") or []
    blocks = [h for h in hist if h.get("kind") == "node_block"]
    since = blocks[0].get("at") if blocks else n.get("planned_at")
    last_txt = str((blocks[-1].get("summary") if blocks else "") or "")
    txt = "%s %s" % (n.get("summary") or "", last_txt)
    # keyword inference only overrides an explicit non-human blocker when the text does not put the human step later
    # (11-06: FE "按516b先等B8…B8交付后核心二请求人类调用授权再转human" with blocker=dep was reported as waiting on the
    # human and "mislabeled", which made the board and a QQ reminder ask the user for something not yet due)
    hit = bool(HUMAN_RE.search(txt)) and not (n.get("blocker") and LATER_RE.search(last_txt or txt))
    both = hit and n.get("blocker") == "dep" and bool(DEP_RE.search(txt))
    own = [c for c in re.split(r"[；;。，,]", txt) if not DEP_RE.search(c)]
    budget = bool(BUDGET_RE.search(txt)) and (hit or n.get("blocker") == "human") and \
        not any(HUMAN_ONLY_RE.search(c) for c in own)
    human = n.get("blocker") == "human" or (hit and not both and not budget)
    mislabeled = human and n.get("status") == "blocked" and n.get("blocker") not in ("human", None)
    failed = False
    last_ad = max([i for i, h in enumerate(hist) if h.get("kind") == "auto_dispatch"] or [-1])
    if last_ad >= 0:
        after = hist[last_ad + 1:]
        failed = any(h.get("kind") == "node_block" and (n.get("blocker") == "env" or "代派失败" in str(h.get("summary")))
                     for h in after) or any("代派失败" in str(h.get("summary") or "") for h in after)
    return {"since": since, "hours": _hrs(now, since), "human": human, "both": both, "budget": budget,
            "mislabeled": mislabeled,
            "dispatch_failed": failed}


def blocker_table(project: Dict[str, Any], state: Dict[str, Any], now: _dt.datetime) -> List[str]:
    """A2: blocked nodes + non-running nodes on frontier milestones."""
    ms_state = state.get("milestones") or {}
    front = {m["id"] for m in project.get("milestones") or []
             if (ms_state.get(m["id"]) or {}).get("status") != "confirmed"
             and all((ms_state.get(a) or {}).get("status") == "confirmed" for a in m.get("after") or [])}
    o = []
    for lid, ls in sorted((state.get("lines") or {}).items()):
        for nid, n in sorted((ls.get("nodes") or {}).items()):
            on_front = [m for m in n.get("milestones") or [] if m in front]
            blocked = n.get("status") == "blocked"
            if not (blocked or (on_front and n.get("status") in ("ready", "planned", "failed"))):
                continue
            w = _wait_info(n, now)
            if not blocked and not w["dispatch_failed"]:
                if n.get("status") != "ready":
                    continue
            flags = []
            if w["human"]:
                flags.append("推断：等人（命中\"人类/授权/端点\"）" if n.get("blocker") != "human" else "等人（blocker=human）")
            if w["both"]:
                flags.append("并存：等依赖，文中也提到人类/授权（按 dep 计，不算标错，不计入等人时长；是否真要人给由你核实）")
            if w["budget"]:
                flags.append("文中是预算/调用额度：标 human 时谁来批由你按规则核实" if n.get("blocker") == "human" else
                             "等预算/调用额度裁定（按 blocker=%s 计，不计入等人时长，不算标错；不据此自动开跑真实调用）"
                             % (n.get("blocker") or "-"))
            if w["mislabeled"]:
                flags.append("⚠ blocker 可能标错")
            if w["dispatch_failed"]:
                flags.append("⚠ 派发失败：%s" % str(n.get("summary") or "")[:160])
            h = w["hours"]
            o.append("- %s/%s → %s | blocker=%s | 状态=%s | 自 %s，%s | %s | 摘要：%s" % (
                lid, nid, ",".join(on_front) or "-", n.get("blocker") or "-", n.get("status"),
                _short_t(w["since"]), ("%.1fh" % h) if h is not None else "?", " ".join(flags) or "-",
                str(n.get("summary") or "")[:100]))
    return o or ["（无）"]


# ---- A3: line docs

def _read_file(path: str, runner) -> Tuple[Optional[str], Optional[float], str]:
    """-> (text, mtime epoch, error). Local paths are read straight from the worktree (no git: launchd cannot
    open a git dir on /Volumes); /home/... paths over ssh."""
    if path.startswith("/home/"):
        rc, out, err = runner(snap.ssh_argv(snap.remote_host(), "p=%s; stat -c %%Y \"$p\" && cat \"$p\"" % shlex.quote(path)), timeout=30)
        if rc != 0:
            return None, None, (err or "rc=%s" % rc).strip()[:80]
        head, _, body = out.partition("\n")
        try:
            return body, float(head), ""
        except ValueError:
            return body, None, ""
    try:
        p = pathlib.Path(path)
        return p.read_text(encoding="utf-8", errors="replace"), p.stat().st_mtime, ""
    except (OSError, ValueError) as exc:
        return None, None, (getattr(exc, "strerror", None) or str(exc))[:80]


def _sections(text: str) -> Tuple[str, List[Tuple[str, str]]]:
    """-> (first paragraph below the first heading, [(heading, body)])."""
    secs: List[Tuple[str, List[str]]] = []
    pre: List[str] = []
    for ln in text.splitlines():
        if re.match(r"^#{1,6}\s", ln):
            secs.append((re.sub(r"^#+\s*", "", ln).strip(), []))
        elif secs:
            secs[-1][1].append(ln)
        else:
            pre.append(ln)
    first = ""
    pool = [(t, b) for t, b in secs]
    body = "\n".join(secs[0][1]) if len(secs) and not "\n".join(pre).strip() else "\n".join(pre)
    for para in re.split(r"\n\s*\n", body.strip()):
        if para.strip():
            first = para.strip()
            break
    return first, [(t, "\n".join(b).strip()) for t, b in pool]


def digest_state(text: str, limit: int = 1800) -> str:
    first, secs = _sections(text)
    parts = []
    if first:
        parts.append(first[:300])
    goal = [(t, b) for t, b in secs if "目标" in t]
    if goal:
        parts.append("\n".join("## %s\n%s" % (t, b) for t, b in goal)[:500])
    tail_budget, tail = max(0, min(1000, limit - sum(len(x) for x in parts))), []
    for t, b in reversed([(t, b) for t, b in secs if re.search(r"当前|下一步|阻塞|卡|待|窗口", t)]):
        if tail_budget <= 0:
            break
        chunk = ("## %s\n%s" % (t, b))[:tail_budget]
        tail.append(chunk)
        tail_budget -= len(chunk)
    parts += list(reversed(tail))
    return "\n".join(parts)[:limit]


def digest_brief(text: str) -> str:
    _f, secs = _sections(text)
    for t, b in secs:
        if "要交付" in t or "目标" in t:
            return ("## %s\n%s" % (t, b))[:600]
    return ""


def line_docs(project: Dict[str, Any], line: Dict[str, Any], runner, st: Dict[str, Any],
              now: Optional[_dt.datetime] = None) -> Dict[str, Any]:
    """-> {"state": {path, mtime, sha, lines, digest|error}, "brief": {...}|None}; digests cached in st["docs"]."""
    import patrol
    out: Dict[str, Any] = {"state": None, "brief": None}
    sf = line.get("state_file")
    if not sf:
        return out
    base = patrol.repo_path(project, line)
    spath = sf if sf.startswith("/") else os.path.join(base, sf)
    bf = line.get("brief_file")
    bpath = (bf if bf.startswith("/") else os.path.join(base, bf)) if bf else os.path.join(os.path.dirname(spath), "BRIEF.md")
    cache = st.setdefault("docs", {})
    for key, path, fn in (("state", spath, digest_state), ("brief", bpath, digest_brief)):
        text, mtime, err = _read_file(path, runner)
        if text is None:
            out[key] = {"path": path, "error": "读不到" + (("（%s）" % err) if err else "")} if key == "state" else None
            continue
        sha = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:8]
        c = cache.get(path) or {}
        if c.get("sha") == sha and c.get("digest") is not None:
            dg = c["digest"]
        else:
            dg = fn(text)
        mt = snap.iso(_dt.datetime.fromtimestamp(mtime, _dt.timezone.utc)) if mtime else None
        cache[path] = {"sha": sha, "mtime": mt, "digest": dg}
        out[key] = {"path": path, "mtime": mt, "sha": sha, "lines": len(text.splitlines()), "digest": dg}
    return out


def hard_metrics(project: Dict[str, Any], state: Dict[str, Any], events: List[Dict[str, Any]],
                 live: Optional[Dict[str, Any]], now: _dt.datetime,
                 docs: Optional[Dict[str, Dict[str, Any]]] = None,
                 missing_prev: Optional[List[str]] = None) -> Tuple[List[str], List[str]]:
    """A4/§D: -> (one row per open line, ["线:指标"...] of every flagged (⚠) metric)."""
    th = thresholds(project)
    lm = (live or {}).get("metrics") or snap.scheduling_metrics(project, state, now)
    rows, flagged = [], []
    since6 = snap.iso(now - _dt.timedelta(hours=6))
    since12 = snap.iso(now - _dt.timedelta(hours=12))
    for ln in project.get("lines") or []:
        lid = ln["id"]
        ls = (state.get("lines") or {}).get(lid) or {}
        if not ls.get("open", True):
            continue
        nodes = ls.get("nodes") or {}
        m = lm.get(lid) or {}
        parts: List[str] = []

        def flag(name: str, txt: str, val: Optional[float], key: str, unit: str = "") -> None:
            att, prob = th[key]
            hit = val is not None and val > att
            if hit:
                flagged.append("%s:%s" % (lid, name))
            parts.append("%s%s %s（阈 %s/%s）%s" % ("⚠" if hit else "", name, txt, att, prob,
                                                 "【上轮未判】" if hit and "%s:%s" % (lid, name) in (missing_prev or []) else ""))

        ru = m.get("ready_undispatched") or {}
        flag("就绪未派", "%s 个 %sh" % (ru.get("nodes", 0), ru.get("node_hours", 0)), ru.get("node_hours") or 0, "ready_undispatched_h")
        cp = m.get("critical_path") or {}
        cpn = [nodes.get(k) or {} for k in cp.get("nodes") or []]
        waits = [_wait_info(n, now) for n in nodes.values() if n.get("status") == "blocked"]
        all_wait = bool(cpn) and all(n.get("status") == "blocked" and _wait_info(n, now)["human"] for n in cpn)
        stall = cp.get("stall_min")
        if all_wait and stall:
            parts.append("关键路径停滞 %s 分（全是等人：推断，不算停滞）" % stall)
        else:
            flag("关键路径停滞", "%s 分" % (stall if stall is not None else "-"), stall or 0, "critical_stall_min")
        whs = [w["hours"] for w in waits if w["human"] and w["hours"] is not None]
        flag("等人", "%.1fh（%d 个）" % (max(whs) if whs else 0, len(whs)), max(whs) if whs else 0, "wait_human_h")
        rr = m.get("review_run") or {}
        flag("审查/执行", str(rr.get("ratio")), rr.get("ratio"), "review_run")
        rev = [e for e in events if e.get("line") == lid and e.get("kind") == "node_review"
               and e.get("outcome") in ("PASS", "PARTIAL")]
        n6 = sum(1 for e in rev if (e.get("at") or "") >= since6)
        n12 = sum(1 for e in rev if (e.get("at") or "") >= since12)
        live_nodes = [n for n in nodes.values() if n.get("status") in ACTIVE and n.get("status") != "blocked"]
        if live_nodes and n6 == 0:
            hit = True
            flagged.append("%s:6h已收交付" % lid)
            parts.append("⚠6h 已收交付 0%s（阈 6h/12h）" % ("，12h 也为 0" if n12 == 0 else ""))
        else:
            parts.append("6h 已收交付 %d" % n6)
        # empty line: open, no ready/running/delivered, nobody waiting
        if nodes and not any(n.get("status") in ("ready", "running", "delivered", "blocked", "planned") for n in nodes.values()):
            last = max([n.get("updated_at") or "" for n in nodes.values()] or [""])
            h = _hrs(now, last)
            flag("空线", "%.1fh" % (h or 0), h or 0, "empty_line_h")
        # rework: FAIL reviews per node, supersede chain per stem
        fails: Dict[str, int] = {}
        for e in events:
            if e.get("line") == lid and e.get("kind") == "node_review" and e.get("outcome") == "FAIL" and e.get("node"):
                fails[e["node"]] = fails.get(e["node"], 0) + 1
        stems: Dict[str, int] = {}
        for nid, n in nodes.items():
            if n.get("status") == "superseded":
                stem = re.sub(r"(?:[-_](?:repair|resume|recheck|review|retry|manual|r\d+))+(?:[-_]?\d+)*$", "", nid)
                stems[stem] = stems.get(stem, 0) + 1
        rw = max(list(fails.values()) + list(stems.values()) + [0])
        flag("打回", str(rw), rw, "rework")
        # stale STATE
        d = ((docs or {}).get(lid) or {}).get("state") or {}
        if d.get("mtime"):
            lastr = max([e.get("at") or "" for e in events if e.get("line") == lid and (e.get("actor") or {}).get("role") == "router"] or [""])
            lag = None
            if lastr:
                a, b = _ev_t(d["mtime"]), _ev_t(lastr)
                lag = max(0.0, (b - a).total_seconds() / 3600.0) if a and b else None
            flag("stale_state", "STATE %.0fh 前" % (_hrs(now, d["mtime"]) or 0), lag, "stale_state_h")
        act = [n for n in nodes.values() if n.get("status") in ACTIVE]
        un = sum(1 for n in act if not n.get("milestones"))
        ratio = un / len(act) if act else 0
        flag("未挂里程碑", "%d/%d" % (un, len(act)), ratio, "unattached_ratio")
        rows.append("- %s %s" % (lid, " | ".join(parts)))
    return rows or ["（没有开着的线）"], flagged


# ---------------------------------------------------------------- map

def _tail(lines: Optional[List[str]], n: int) -> str:
    rows = [l.rstrip()[:200] for l in (lines or []) if l and l.strip()][-n:]
    return "\n".join("    | " + r for r in rows) if rows else "    | （读不到屏幕）"


def _min(sec: Optional[float]) -> str:
    return "-" if sec is None else str(int(sec // 60))


def build_map(project: Dict[str, Any], events: List[Dict[str, Any]], live: Optional[Dict[str, Any]],
              now: _dt.datetime, runner, last_state: Dict[str, Any], round_actions: List[Dict[str, Any]],
              prev_report: Optional[Dict[str, Any]], max_chars: int = MAP_MAX,
              meta: Optional[Dict[str, Any]] = None) -> str:
    import patrol  # lazy: patrol imports this module
    state = core.fold(project, events, at=snap.iso(now))
    # read screens once; shrink the tails / drop finished nodes until the map fits
    rscreens: Dict[str, Optional[List[str]]] = {}
    for ln in project.get("lines") or []:
        h = ((((live or {}).get("routers") or {}).get(ln["id"]) or {}).get("handle")) or ln.get("router")
        rscreens[ln["id"]] = snap.read_terminal_tail(runner, h, live, 40) if h else None
    wscreens: Dict[str, Optional[List[str]]] = {}
    for w in ((live or {}).get("workers") or [])[:16]:
        d = (w.get("task") or {}).get("dispatch")
        if d and not snap.is_finished((w.get("task") or {}).get("state")):
            wscreens[d] = snap.read_worker_tail(runner, d, 40)
    since = last_state.get("last_call_at") or ""
    scripted = [e for e in events if (e.get("actor") or {}).get("role") == "patrol" and (e.get("at") or "") > since][-30:]

    docs = {ln["id"]: line_docs(project, ln, runner, last_state) for ln in project.get("lines") or []}
    metric_rows, flagged = hard_metrics(project, state, events, live, now, docs,
                                        (prev_report or {}).get("missing_metric_verdicts"))
    if meta is not None:
        meta["flagged"] = flagged
    fu_rows = render_followups(last_state.get("followups") or {}, now)

    def render(wtail: int, rtail: int, all_nodes: bool, state_chars: int, brief: bool, n_ev: int) -> str:
        o: List[str] = ["# 项目地图（%s，生成于 %s）" % (project.get("id"), snap.iso(now))]
        o += ["", "## 1 最终目标与关键路径"] + milestone_frontier(project, state)
        o += ["", "## 2 卡点"] + blocker_table(project, state, now)
        o += ["", "## 3 硬指标（⚠ = 超标）"] + metric_rows
        o += ["", "## 4 待回查建议"] + fu_rows
        o += ["", "## 5 各线背景（BRIEF / STATE 摘要；全文用 progress-inspect file <绝对路径>）"]
        for ln in project.get("lines") or []:
            d = docs.get(ln["id"]) or {}
            sd, bd = d.get("state"), d.get("brief")
            if not sd:
                o.append("- 线 %s：未配置 state_file" % ln["id"])
                continue
            if sd.get("error"):
                o.append("- 线 %s STATE %s：%s" % (ln["id"], sd["path"], sd["error"]))
                continue
            o.append("- 线 %s STATE %s（改于 %s，sha %s，%s 行）：" % (ln["id"], sd["path"], sd["mtime"], sd["sha"], sd["lines"]))
            o.append("\n".join("    " + x for x in str(sd["digest"])[:state_chars].splitlines()))
            if brief and bd:
                o.append("  BRIEF %s（改于 %s，sha %s，%s 行）：" % (bd["path"], bd["mtime"], bd["sha"], bd["lines"]))
                o.append("\n".join("    " + x for x in str(bd["digest"]).splitlines()))
        o += ["", "## 6 主机资源（磁盘/内存）"]
        res = (live or {}).get("resources") or {}
        for h, r in sorted(res.items()):
            o.append("- %s 内存可用 %s G；磁盘：%s" % (h, r.get("mem_avail_gb"), "；".join(
                "%s 剩 %s G（用 %s%%，%s）" % (d.get("mount"), d.get("avail_gb"), d.get("used_pct"), d.get("level"))
                for d in r.get("disks") or [])))
        if not res:
            o.append("（没有数据）")
        o += ["", "## 7 各条线"]
        for ln in project.get("lines") or []:
            lid = ln["id"]
            ls = state["lines"].get(lid) or {}
            o.append("")
            rl = ((live or {}).get("routers") or {}).get(lid) or {}
            o.append("### 线 %s %s [%s] router=%s run=%s" % (lid, str(ln.get("name") or "")[:60],
                                                         "开" if ls.get("open", True) else "已关", ln.get("router"), ln.get("run")))
            if rl.get("present") is False:
                o.append("  ！router 终端不在线（%s）：建议会转给协调人" % (rl.get("handle") or ln.get("router")))
            if rl.get("rebound_from"):
                o.append("  router 已换成新终端 %s（project.json 仍是 %s）" % (rl.get("handle"), rl["rebound_from"]))
            for nid, n in sorted((ls.get("nodes") or {}).items()):
                if not all_nodes and n.get("status") in ("reviewed", "superseded", "cancelled"):
                    continue
                exp = patrol.expected_minutes(project, n)
                el = patrol._age(now, n.get("dispatched_at")) if n.get("status") == "running" else None
                o.append("- %s %s/%s deps=%s 预计%s分 已跑%s分 派出%s 交付%s%s" % (
                    nid, n.get("status"), n.get("kind") or "?", ",".join(n.get("deps") or []) or "-",
                    "-" if exp is None else int(exp), _min(el), n.get("dispatched_at") or "-", n.get("delivered_at") or "-",
                    (" 审:" + str(n.get("review"))) if n.get("review") else ""))
            last = [e for e in events if e.get("line") == lid][-n_ev:]
            o.append("  最近%d条事件：" % n_ev)
            for e in last:
                o.append("  · %s %s%s %s" % (e.get("at"), e.get("kind"), ("/" + e["node"]) if e.get("node") else "",
                                           str(e.get("summary") or "")[:100]))
            o.append("  router 屏幕末尾：")
            o.append(_tail(rscreens.get(lid), rtail))
        o += ["", "## 8 在跑 worker（屏幕末尾）"]
        for w in (live or {}).get("workers") or []:
            t = w.get("task") or {}
            d = t.get("dispatch")
            if not d or d not in wscreens:
                continue
            o.append("- %s 线=%s 节点=%s 终端=%s 起于%s 标记=%s" % (d, w.get("line"), w.get("node"), w.get("terminal"),
                                                              w.get("started_at"), (w.get("screen") or {}).get("flag")))
            o.append(_tail(wscreens[d], wtail))
        o += ["", "## 9 上次巡检以来脚本做过的动作"]
        for e in scripted:
            o.append("- %s %s/%s %s" % (e.get("at"), e.get("kind"), e.get("reason") or "", str(e.get("summary") or "")[:100]))
        for a in round_actions:
            o.append("- 本轮 %s" % json.dumps(a, ensure_ascii=False)[:200])
        if not scripted and not round_actions:
            o.append("（无）")
        o += ["", "## 10 上一份报告摘要"]
        if prev_report:
            o.append(str(prev_report.get("summary") or ""))
            for b in prev_report.get("blockers") or []:
                if isinstance(b, dict):
                    o.append("- 卡点 %s/%s [%s] 等 %s" % (b.get("line"), b.get("item"), b.get("class"), b.get("who_unblocks")))
            for l in prev_report.get("lines") or []:
                if isinstance(l, dict) and l.get("status") != "ok":
                    o.append("- %s %s：%s" % (l.get("line"), l.get("status"), str(l.get("progress_note") or "")[:120]))
        else:
            o.append("（没有）")
        return "\n".join(o)

    # shrink order (spec A6): worker tails, router tails, finished nodes, STATE/BRIEF, recent events; 1-4 never cut
    steps = [(12, 12, True, 1800, True, 5), (6, 12, True, 1800, True, 5), (3, 12, True, 1800, True, 5),
             (3, 6, True, 1800, True, 5), (3, 3, True, 1800, True, 5), (3, 3, False, 1800, True, 5),
             (3, 3, False, 1000, False, 5), (3, 3, False, 1000, False, 3)]
    for stp in steps:
        text = render(*stp)
        if len(text) <= max_chars:
            return text
    return text[:max_chars - 20] + "\n…（已截断）"


# ---------------------------------------------------------------- model call + audit

def default_cmd(schema_path: str, out_path: str) -> List[str]:
    return ["codex", "exec", "-m", MODEL, "-c", 'model_reasoning_effort="high"',
            "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check", "--ephemeral", "--json",
            "--output-schema", schema_path, "-o", out_path, "-"]


def run_model(prompt: str, workdir: str, env: Dict[str, str], timeout: int = TIMEOUT_S) -> Tuple[int, str, str, str]:
    """-> (rc, json event stream, final message, stderr). rc 124 on timeout."""
    schema, out = os.path.join(workdir, "schema.json"), os.path.join(workdir, "last_message.txt")
    custom = os.environ.get("PROGRESS_SUPERVISOR_CMD")
    argv = shlex.split(custom) if custom else default_cmd(schema, out)
    env = dict(env, PROGRESS_SUPERVISOR_SCHEMA=schema, PROGRESS_SUPERVISOR_OUT=out)
    try:
        p = subprocess.run(argv, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=workdir, env=env)
        rc, so, se = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "", "timeout after %ss" % timeout
    except OSError as exc:
        return 127, "", "", str(exc)
    try:
        msg = pathlib.Path(out).read_text(encoding="utf-8")
    except OSError:
        msg = ""
    return rc, so, msg, se


def stream_commands(stream: str) -> List[str]:
    """Shell commands the model ran, from the codex --json event stream (item.* events with a `command`)."""
    seen: Dict[str, str] = {}
    order: List[str] = []
    for raw in (stream or "").splitlines():
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        item = ev.get("item") if isinstance(ev, dict) else None
        if not isinstance(item, dict) or "command" not in item:
            continue
        cmd = item["command"]
        cmd = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        key = str(item.get("id") or len(order))
        if key not in seen:
            order.append(key)
        seen[key] = cmd
    return [seen[k] for k in order]


def audit(commands: List[str]) -> List[Dict[str, str]]:
    bad = []
    for c in commands:
        for rx, why in _WRITE_RES:
            if rx.search(c):
                bad.append({"command": c[:200], "why": why})
                break
    return bad


# ---------------------------------------------------------------- report

def parse_report(text: str) -> Optional[Dict[str, Any]]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n|\n```$", "", text)
    try:
        doc = json.loads(text)
    except ValueError:
        doc = snap.parse_json_tail(text)
    return doc if isinstance(doc, dict) else None


def _str(v: Any, n: int) -> str:
    return v.strip()[:n] if isinstance(v, str) else ""


def _clamp_min(v: Any) -> int:
    try:
        return max(CHECK_MIN, min(CHECK_MAX, int(v)))
    except (TypeError, ValueError):
        return CHECK_DEFAULT


def _norm_verify(v: Any) -> Dict[str, Optional[str]]:
    out: Dict[str, Optional[str]] = {"line": None, "node": None, "event_kind": None}
    if isinstance(v, dict) and isinstance(v.get("event_kind"), str) and v["event_kind"].strip():
        for k in out:
            out[k] = v[k].strip() if isinstance(v.get(k), str) and v[k].strip() else None
    return out


def validate_report(doc: Any, line_ids: List[str], pending_ids: Optional[List[str]] = None,
                    flagged: Optional[List[str]] = None, open_ids: Optional[List[str]] = None) -> Tuple[Optional[Dict[str, Any]], str]:
    """-> (clean doc, "") or (None, why). v2 (spec §B). A v1 doc (lines[].suggestions, no actions) is migrated:
    each suggestion becomes an action to the router with no done_when and the default re-check time. Soft problems
    go to clean["warnings"]; only a bad structure / unknown line / bad `to` / bad issue kind rejects the report."""
    if not isinstance(doc, dict) or not isinstance(doc.get("summary"), str) or not doc["summary"].strip():
        return None, "summary 缺失"
    if not isinstance(doc.get("lines"), list) or not doc["lines"]:
        return None, "lines 缺失"
    warns: List[str] = []
    migrated = False
    lines = []
    for l in doc["lines"]:
        if not isinstance(l, dict) or l.get("line") not in line_ids:
            return None, "未知的线 %r" % (l.get("line") if isinstance(l, dict) else l)
        if l.get("status") not in STATUSES or not isinstance(l.get("progress_note"), str):
            return None, "线 %s 的 status/progress_note 不合法" % l["line"]
        iss = l.get("issues")
        if not isinstance(iss, list):
            return None, "线 %s 的 issues 不是数组" % l["line"]
        for i in iss:
            if not isinstance(i, dict) or i.get("kind") not in KINDS or not isinstance(i.get("evidence"), str):
                return None, "线 %s 的 issue 不合法" % l["line"]
        acts_in = l.get("actions")
        if acts_in is None:
            sug = l.get("suggestions")
            if not isinstance(sug, list) or not all(isinstance(x, str) for x in sug):
                return None, "线 %s 缺 actions（也不是合法的 v1 suggestions）" % l["line"]
            acts_in = [{"to": "router", "text": x, "done_when": "", "check_after_min": CHECK_DEFAULT} for x in sug]
            migrated = migrated or bool(sug)
        if not isinstance(acts_in, list):
            return None, "线 %s 的 actions 不是数组" % l["line"]
        acts = []
        for a in acts_in:
            if not isinstance(a, dict) or a.get("to") not in ACTION_TO or not isinstance(a.get("text"), str):
                return None, "线 %s 的 action 不合法（to 必须是 %s）" % (l["line"], "/".join(ACTION_TO))
            if not a["text"].strip():
                continue
            acts.append({"to": a["to"], "text": a["text"].strip()[:300], "done_when": _str(a.get("done_when"), 300),
                         "check_after_min": _clamp_min(a.get("check_after_min")), "verify": _norm_verify(a.get("verify"))})
        if len(acts) > 3:
            warns.append("线 %s 的 actions 超过 3 条，只留前 3 条" % l["line"])
        acts = acts[:3]
        n_other = sum(1 for i in iss if i["kind"] == "other")
        if iss and n_other * 2 > len(iss):
            warns.append("线 %s 的 other 类问题超过一半（%d/%d）" % (l["line"], n_other, len(iss)))
        lines.append({"line": l["line"], "status": l["status"], "progress_note": l["progress_note"][:300],
                      "milestone_position": _str(l.get("milestone_position"), 300),
                      "issues": [{"kind": i["kind"], "evidence": i["evidence"][:400]} for i in iss][:6],
                      "actions": acts, "suggestions": [a["text"] for a in acts]})
    pend = set(pending_ids or [])
    early = set(open_ids or []) - pend
    fus = []
    for f in doc.get("followups") or []:
        fid = f.get("id") if isinstance(f, dict) else None
        if fid in early and f.get("verdict") in VERDICTS:
            # Not due yet. A closing verdict with evidence is kept (the model saw it done early); anything else is
            # dropped quietly — terra re-states open entries every round (11-06: 4 rounds of the same 3 warnings).
            if f["verdict"] in ("done", "obsolete") and _str(f.get("evidence"), 400):
                fus.append({"id": fid, "verdict": f["verdict"], "evidence": _str(f.get("evidence"), 400)})
            continue
        if not isinstance(f, dict) or fid not in pend or f.get("verdict") not in VERDICTS:
            warns.append("丢弃不认识的回查判定 %r" % (fid if fid else f,))
            continue
        fus.append({"id": f["id"], "verdict": f["verdict"], "evidence": _str(f.get("evidence"), 400)})
    mfs = []
    for m in doc.get("metric_findings") or []:
        if isinstance(m, dict) and m.get("verdict") in ("real", "explained") and isinstance(m.get("line"), str):
            mfs.append({"line": m["line"], "metric": _str(m.get("metric"), 60), "value": _str(m.get("value"), 60),
                        "verdict": m["verdict"], "evidence": _str(m.get("evidence"), 400)})
    missing = []
    for fl in flagged or []:
        ln, _, name = fl.partition(":")
        if not any(m["line"] == ln and (name in m["metric"] or (m["metric"] and m["metric"].lstrip("⚠") in name)) for m in mfs):
            missing.append(fl)
    bls = []
    for b in doc.get("blockers") or []:
        if isinstance(b, dict) and b.get("class") in BLOCK_CLASSES:
            sh = b.get("since_hours")
            bls.append({"item": _str(b.get("item"), 120), "line": _str(b.get("line"), 40), "class": b["class"],
                        "since_hours": sh if isinstance(sh, (int, float)) and not isinstance(sh, bool) else None,
                        "who_unblocks": _str(b.get("who_unblocks"), 120), "evidence": _str(b.get("evidence"), 300)})
    gds = [{k: _str(g.get(k), 400) for k in ("topic", "current", "proposal", "why")}
           for g in doc.get("goal_drafts") or [] if isinstance(g, dict)][:6]
    out = {"summary": doc["summary"].strip()[:1200], "critical_path": _str(doc.get("critical_path"), 1200),
           "blockers": bls[:20], "lines": lines, "followups": fus, "metric_findings": mfs[:40], "goal_drafts": gds,
           "missing_metric_verdicts": missing}
    if warns:
        out["warnings"] = warns
    if migrated:
        out["migrated_from_v1"] = True
    return out, ""


# ---------------------------------------------------------------- follow-up ledger (spec §C), st["followups"]

def fu_id(line: str, to: str, text: str) -> str:
    norm = re.sub(r"\s+", "", text or "").lower()
    return "fu_" + hashlib.sha1(("%s|%s|%s" % (line, to, norm)).encode("utf-8")).hexdigest()[:8]


FU_SAME_RATIO = 0.8   # a reworded copy of an open follow-up (same line and recipient) counts as the same one


def same_open(led: Dict[str, Any], line: str, to: str, text: str) -> Optional[str]:
    """Id of an open follow-up for the same line and recipient whose text is a near copy of `text` (terra rewords its
    asks between rounds; the exact-hash id alone let the same ask to a human be registered and pushed twice)."""
    norm = re.sub(r"\s+", "", text or "").lower()
    best, best_r = None, 0.0
    for fid, e in led.items():
        if not _is_open(e) or e.get("line") != line or e.get("to") != to:
            continue
        r = difflib.SequenceMatcher(None, norm, re.sub(r"\s+", "", e.get("text") or "").lower()).ratio()
        if r >= FU_SAME_RATIO and r > best_r:
            best, best_r = fid, r
    return best


def _add_min(now: _dt.datetime, minutes: float) -> str:
    return snap.iso(now + _dt.timedelta(minutes=minutes))


def _is_open(e: Dict[str, Any]) -> bool:
    return not e.get("closed_at")


def _close(e: Dict[str, Any], now: _dt.datetime, how: str, evidence: str = "") -> None:
    e["closed_at"], e["closed_how"], e["closed_evidence"] = snap.iso(now), how, evidence[:300]
    e.setdefault("history", []).append({"at": snap.iso(now), "what": how, "evidence": evidence[:300]})


def register(st: Dict[str, Any], line: str, act: Dict[str, Any], target: str, now: _dt.datetime,
             human: bool = False) -> Dict[str, Any]:
    """C1: new open entry (replaces a closed one with the same id)."""
    fid = fu_id(line, act["to"], act["text"])
    e = {"id": fid, "line": line, "to": act["to"], "target": target, "text": act["text"],
         "done_when": act.get("done_when") or "", "verify": act.get("verify") or _norm_verify(None),
         "check_after_min": act.get("check_after_min") or CHECK_DEFAULT,
         "created_at": snap.iso(now), "sent_at": snap.iso(now),
         "check_at": _add_min(now, act.get("check_after_min") or CHECK_DEFAULT),
         "tries": 0, "cannot_tell": 0, "human": human, "history": [{"at": snap.iso(now), "what": "registered"}]}
    st.setdefault("followups", {})[fid] = e
    return e


def auto_check(st: Dict[str, Any], events: List[Dict[str, Any]], now: _dt.datetime) -> None:
    """C2: close entries whose verify event appeared after sent_at; expire after 24 h (entries waiting on a
    human are exempt, they keep the C4 reminders going); prune long-closed rows."""
    led = st.setdefault("followups", {})
    for fid in list(led):
        e = led[fid]
        if not _is_open(e):
            if (_hrs(now, e.get("closed_at")) or 0) > 24 * 7:
                del led[fid]
            continue
        v = e.get("verify") or {}
        if v.get("event_kind"):
            for ev in events:
                if (ev.get("at") or "") > (e.get("sent_at") or "") and ev.get("kind") == v["event_kind"] \
                        and ev.get("line") == (v.get("line") or e["line"]) \
                        and (not v.get("node") or ev.get("node") == v["node"]):
                    _close(e, now, "auto_done", "事件 %s" % (ev.get("id") or ev.get("at")))
                    break
        if _is_open(e) and not e.get("human") and (_hrs(now, e.get("sent_at")) or 0) > FU_EXPIRE_H:
            _close(e, now, "expired")


def pending_followups(st: Dict[str, Any], now: _dt.datetime) -> List[Dict[str, Any]]:
    """Open entries whose check time has come: the ones terra must give a verdict on this round."""
    return sorted((e for e in (st.get("followups") or {}).values()
                   if _is_open(e) and (core.parse_iso(e.get("check_at")) or now) <= now), key=lambda e: e["created_at"])


def render_followups(led: Dict[str, Any], now: _dt.datetime) -> List[str]:
    """A5."""
    o: List[str] = []
    for e in sorted(led.values(), key=lambda x: x.get("created_at") or ""):
        if _is_open(e):
            v = e.get("verify") or {}
            due = (core.parse_iso(e.get("check_at")) or now) <= now
            o.append("- [%s] %s%s 于 %s | %s | 到期 %s | 内容：%s | 落实标准：%s | 自动核对：%s%s" % (
                e["id"], "发给 " + e["to"] + " " if e.get("to") else "", e["line"], _short_t(e.get("sent_at")),
                "第 %d 次回查（请给 verdict）" % (e.get("tries", 0) + 1) if due else "未到期（不要重复提）",
                _short_t(e.get("check_at")), e["text"][:160], (e.get("done_when") or "（未写）")[:160],
                ("%s/%s %s 未命中" % (v.get("line") or e["line"], v.get("node") or "*", v["event_kind"])) if v.get("event_kind") else "无",
                "｜上轮已 %d 次 cannot_tell" % e["cannot_tell"] if e.get("cannot_tell") else ""))
        elif e.get("closed_how") in ("auto_done", "expired", "done", "obsolete") and not e.get("shown") \
                and (_hrs(now, e.get("closed_at")) or 0) <= 24:
            # done/obsolete include closures by a handler outside terra (tooling / human); without this terra
            # re-opened a just-closed tooling item as a "merged duplicate" (11-06 fu_56563038).
            label = {"auto_done": "已自动确认", "expired": "已过期关闭", "done": "已确认落实（勿重开）",
                     "obsolete": "已作废（勿重开）"}[e["closed_how"]]
            o.append("- %s [%s] %s/%s：%s（%s）" % (label, e["id"], e["line"], e.get("to"), e["text"][:100],
                                                  e.get("closed_evidence") or "-"))
    return o or ["（暂无）"]


def _coord_of(project: Dict[str, Any], live: Optional[Dict[str, Any]], line_id: str) -> Tuple[Optional[str], Dict[str, Any]]:
    ln = core.line_of(project, line_id) or {}
    cinfo = ((live or {}).get("coordinators") or {}).get(str(ln.get("core"))) or {}
    return (cinfo.get("handle") if cinfo.get("present") is True and cinfo.get("handle") else None), ln


def _fmt_acts(acts: List[Dict[str, Any]]) -> str:
    return "；".join("%d) %s%s" % (i + 1, a["text"], ("（落实标准：%s）" % a["done_when"]) if a.get("done_when") else "")
                    for i, a in enumerate(acts))


# ---------------------------------------------------------------- delivery

def deliver(project: Dict[str, Any], report: Dict[str, Any], live: Optional[Dict[str, Any]], st: Dict[str, Any],
            now: _dt.datetime, runner, apply: bool, store: snap.Store, violation: bool) -> List[Dict[str, Any]]:
    """Spec §C3/§C5. New actions (not already open in the ledger), follow-up re-sends and escalations are grouped
    per recipient: routers (1 msg/h), coordinators (1 msg/2 h), tooling terminal; human items only get flagged
    (the digest is a later step). -> report["delivery"] rows. Ledger changes only under --apply, no violation,
    and a re-send / escalation is only committed when the message really went out."""
    led = st.setdefault("followups", {})
    coord_handles = {c.get("handle") for c in ((live or {}).get("coordinators") or {}).values() if c.get("handle")}
    live_ok = apply and not violation
    items: List[Dict[str, Any]] = []   # {h, line, text, kind: new|resend|escalate, act, fid, fwd, present}
    rows: List[Dict[str, Any]] = []
    human_pending: List[Dict[str, Any]] = []

    # --- C3: verdicts on due entries (not when the round is suppressed)
    n_esc = 0
    if live_ok:
        for v in report.get("followups") or []:
            e = led.get(v["id"])
            if not e or not _is_open(e):
                continue
            verdict, ev = v["verdict"], v.get("evidence") or ""
            e.setdefault("history", []).append({"at": snap.iso(now), "what": verdict, "evidence": ev})
            if verdict in ("done", "obsolete"):
                _close(e, now, verdict, ev)
                continue
            if verdict == "cannot_tell":
                e["cannot_tell"] = e.get("cannot_tell", 0) + 1
                if e["cannot_tell"] < 2:
                    e["check_at"] = snap.iso(now)  # judge again next round
                    continue
                verdict = "not_done"
            e["cannot_tell"] = 0
            tries = e.get("tries", 0)
            ev_all = "；".join(x["evidence"] for x in e["history"] if x.get("what") == "not_done" and x.get("evidence")) or ev
            if e["to"] == "human" or tries >= 2:
                e.update(tries=tries + 1, human=True, check_at=_add_min(now, HUMAN_RECHECK_MIN if e["to"] == "human" else e.get("check_after_min") or HUMAN_RECHECK_MIN))
                continue
            if tries == 0:
                items.append({"h": e["target"], "line": e["line"], "kind": "resend", "fid": e["id"], "e": e,
                              "text": "[巡检员·回查] %s 线：%s；落实标准：%s；证据：%s" % (e["line"], e["text"], e["done_when"] or "（未写）", ev)})
                continue
            if n_esc >= ESCALATE_MAX:
                continue  # postponed to the next round
            ch, ln = _coord_of(project, live, e["line"])
            to_human = (e["to"] == "tooling" or (e["to"] == "coordinator" and TIER3_RE.search(e["text"] + e["done_when"]))
                        or (e["to"] == "router" and not ch))
            if to_human:
                th = os.environ.get("PROGRESS_TOOLING_HANDLE")
                e.update(tries=2, human=True, check_at=_add_min(now, e.get("check_after_min") or HUMAN_RECHECK_MIN),
                         **({"tooling_handle": th} if e["to"] == "tooling" and th else {}))
                e["history"].append({"at": snap.iso(now), "what": "escalate_to_human"})
                n_esc += 1
                continue
            n_esc += 1
            if e["to"] == "router":
                txt = "[巡检员·升级] %s 线 router 两次没落实：%s；落实标准：%s；证据：%s" % (e["line"], e["text"], e["done_when"] or "（未写）", ev_all)
            else:
                txt = "[巡检员·升级] %s 线协调人事项两次没落实：%s；落实标准：%s；证据：%s。建议开 decide 节点交 Opus+Astra 裁决" % (
                    e["line"], e["text"], e["done_when"] or "（未写）", ev_all)
            items.append({"h": ch, "line": e["line"], "kind": "escalate", "fid": e["id"], "e": e, "text": txt})

    # --- C1: new actions
    seen_new = set()
    for l in report["lines"]:
        if l["status"] == "ok" or not l.get("actions"):
            continue
        ln = core.line_of(project, l["line"]) or {}
        rinfo = ((live or {}).get("routers") or {}).get(l["line"]) or {}
        by_to: Dict[str, List[Dict[str, Any]]] = {}
        for a in l["actions"]:
            fid = fu_id(l["line"], a["to"], a["text"])
            if not (fid in led and _is_open(led[fid])):
                fid = same_open(led, l["line"], a["to"], a["text"]) or fid
            if fid in seen_new:
                continue
            seen_new.add(fid)
            if fid in led and _is_open(led[fid]):
                rows.append({"line": l["line"], "lines": [l["line"]], "target": led[fid].get("target"), "sent": False,
                             "reason": "unchanged", "followups": [fid]})
                continue
            by_to.setdefault(a["to"], []).append(dict(a, fid=fid))
        for to, acts in by_to.items():
            if to == "human":
                for a in acts:
                    if live_ok:
                        register(st, l["line"], a, "human", now, human=True)
                    rows.append({"line": l["line"], "lines": [l["line"]], "target": "human", "sent": False,
                                 "reason": "human_digest" if live_ok else ("policy_violation" if violation else "dry_run"),
                                 "followups": [a["fid"]]})
                continue
            if to == "router":
                h, present, fwd = rinfo.get("handle") or ln.get("router"), rinfo.get("present"), False
                if not h:
                    continue
                if present is False:
                    ch, _ln = _coord_of(project, live, l["line"])
                    if ch:
                        h, present, fwd = ch, True, True
                text = "[%s] %s" % (l["line"], _fmt_acts(acts))
            elif to == "coordinator":
                h, _ln = _coord_of(project, live, l["line"])
                present, fwd = True, False
                if not h:
                    rows.append({"line": l["line"], "lines": [l["line"]], "target": None, "sent": False,
                                 "reason": "router_absent", "followups": [a["fid"] for a in acts]})
                    continue
                text = "[%s 线，协调人] %s" % (l["line"], _fmt_acts(acts))
            else:  # tooling
                h, present, fwd = os.environ.get("PROGRESS_TOOLING_HANDLE"), True, False
                text = "[%s 线，tooling] %s" % (l["line"], _fmt_acts(acts))
                if not h:
                    if live_ok:
                        for a in acts:
                            register(st, l["line"], a, "observe", now)
                        try:
                            store.append({"kind": "observe", "line": l["line"], "at": snap.iso(now),
                                          "actor": {"role": "patrol", "handle": SUP_HANDLE},
                                          "summary": ("巡检员·tooling 事项：" + _fmt_acts(acts))[:core.SUMMARY_MAX],
                                          "refs": {"key": "supervisor-tooling:" + acts[0]["fid"]}})
                        except core.ProgressError:
                            pass
                    rows.append({"line": l["line"], "lines": [l["line"]], "target": None, "sent": False,
                                 "reason": "observed" if live_ok else ("policy_violation" if violation else "dry_run"),
                                 "followups": [a["fid"] for a in acts]})
                    continue
            items.append({"h": h, "line": l["line"], "kind": "new", "acts": acts, "fwd": fwd, "present": present,
                          "text": ("[巡检员] %s router 不在线，转给你：%s" % (l["line"], text)) if fwd else text})

    # --- C5: one message per recipient
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for it in items:
        groups.setdefault(it["h"], []).append(it)
    sent_state = st.setdefault("sent", {})
    for h, its in groups.items():
        news = [i for i in its if i["kind"] == "new"]
        others = [i for i in its if i["kind"] != "new"]
        pieces = [i["text"] for i in news if i.get("fwd")]
        plain = [i["text"] for i in news if not i.get("fwd")]
        if plain:
            pieces.append("[巡检员] " + " ".join(plain))
        pieces += [i["text"] for i in others]
        text = " ".join(pieces)
        fids = [a["fid"] for i in news for a in i["acts"]] + [i["fid"] for i in others]
        lines_ = list(dict.fromkeys(i["line"] for i in its))
        prev = sent_state.get(h) or {}
        age = (now - core.parse_iso(prev["at"])).total_seconds() if core.parse_iso(prev.get("at")) else None
        gap = COORD_GAP_S if h in coord_handles else SEND_GAP_S
        row = {"line": lines_[0], "lines": lines_, "target": h, "sent": False, "followups": fids}
        if violation:
            row["reason"] = "policy_violation"
        elif not apply:
            row["reason"] = "dry_run"
        elif any(i.get("present") is False for i in news):
            row["reason"] = "router_absent"
        elif age is not None and age < gap:
            row["reason"] = "rate_limited"
        else:
            ok, why, _m = snap.terminal_send(runner, h, text[:1500], snap.env_for_terminal(h, live))
            row["sent"] = ok
            row["reason"] = (("forwarded_to_coordinator" if any(i.get("fwd") for i in news) else "sent") if ok else "failed")
            if ok:
                sent_state[h] = {"at": snap.iso(now), "digest": hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]}
                for i in news:
                    for a in i["acts"]:
                        register(st, i["line"], a, h, now)
                refs_ev = []
                for i in others:
                    e = i["e"]
                    if i["kind"] == "resend":
                        e["tries"] = 1
                    else:
                        e.update(tries=2, target=h, escalated_to=h)
                    e["check_at"] = _add_min(now, e.get("check_after_min") or CHECK_DEFAULT)
                    e["history"].append({"at": snap.iso(now), "what": i["kind"], "target": h})
                    refs_ev.append(({"followup": e["id"], "target": h},
                                    "supervisor_followup" if i["kind"] == "resend" else "supervisor_escalate", i))
                evs = [({"target": h}, "supervisor", {"line": news[0]["line"], "text": "巡检员建议：" + " ".join(plain or [i["text"] for i in news])})] if news else []
                evs += [(r, reason, {"line": i["line"], "text": i["text"]}) for r, reason, i in refs_ev]
                for refs, reason, d in evs:
                    try:
                        store.append({"kind": "nudge", "line": d["line"], "reason": reason, "at": snap.iso(now),
                                      "actor": {"role": "patrol", "handle": SUP_HANDLE},
                                      "summary": d["text"][:core.SUMMARY_MAX], "refs": refs})
                    except core.ProgressError:
                        pass
            else:
                row["error"] = why
        rows.append(row)
    for e in led.values():
        if _is_open(e) and e.get("human") and e.get("tries", 0) >= 2:
            human_pending.append({"id": e["id"], "line": e["line"], "to": e["to"], "text": e["text"][:200],
                                  "tries": e["tries"], **({"tooling_handle": e["tooling_handle"]} if e.get("tooling_handle") else {})})
    report["human_pending"] = human_pending
    return rows


# ---------------------------------------------------------------- entry

def pulse(pid: str, project: Dict[str, Any], events: List[Dict[str, Any]], live: Optional[Dict[str, Any]],
          now: _dt.datetime, runner, store: snap.Store, apply: bool = False, force: bool = False,
          round_actions: Optional[List[Dict[str, Any]]] = None,
          model_call: Optional[Callable[..., Tuple[int, str, str, str]]] = None) -> Dict[str, Any]:
    """Run the supervisor if due. -> {"ran": bool, ...} (never raises for model trouble)."""
    st = load_state(pid)
    if not due(st, now, force):
        return {"ran": False, "reason": "not_due", "last_call_at": st.get("last_call_at")}
    if not force and backing_off(st, now):
        return {"ran": False, "reason": "backoff", "last_fail_at": st.get("last_fail_at"),
                "fail_streak": st.get("fail_streak") or 0}
    st.setdefault("followups", {})
    auto_check(st, events, now)  # C2, before the model is called
    before = dict(st)  # the map's "since the last call" window needs the previous timestamp
    st["last_call_at"] = snap.iso(now)
    save_state(pid, st)  # recorded before the call: a hung/failed call is not retried every round
    prev = core.load_supervisor(pid) if store.local else None
    t0 = time.time()
    work = tempfile.mkdtemp(prefix="progress-supervisor-")
    pending = pending_followups(st, now)
    meta: Dict[str, Any] = {}
    try:
        map_text = build_map(project, events, live, now, runner, before, round_actions or [], prev, meta=meta)
        for e in st["followups"].values():  # closed rows are listed in the map once
            if not _is_open(e):
                e["shown"] = True
        st["docs"] = before.get("docs") or {}  # digest cache filled by line_docs
        map_path = os.path.join(work, "map.md")
        pathlib.Path(map_path).write_text(map_text, encoding="utf-8")
        pathlib.Path(work, "schema.json").write_text(json.dumps(REPORT_SCHEMA), encoding="utf-8")
        log_path = os.path.join(work, "inspect.log")
        env = dict(os.environ)
        # progress-inspect from this checkout, first on PATH (works before/without install_mac.sh)
        bindir = os.path.join(work, "bin")
        os.makedirs(bindir)
        wrapper = os.path.join(bindir, "progress-inspect")
        pathlib.Path(wrapper).write_text('#!/bin/sh\nexec %s %s "$@"\n' % (
            shlex.quote(sys.executable or "/usr/bin/python3"),
            shlex.quote(str(pathlib.Path(__file__).resolve().parent / "progress_inspect.py"))))
        os.chmod(wrapper, 0o755)
        env["PATH"] = bindir + os.pathsep + os.path.expanduser("~/.progress/bin") + os.pathsep + env.get("PATH", "")
        env.update(PROGRESS_INSPECT_LOG=log_path, PROGRESS_PROJECT=pid, PROGRESS_SUPERVISOR_MAP=map_path)
        rc, stream, msg, err = (model_call or run_model)(PROMPT.format(map=map_path, map_text=map_text), work, env)
        try:
            inspect_calls = len(pathlib.Path(log_path).read_text().splitlines())
        except OSError:
            inspect_calls = 0
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
    dur = int(time.time() - t0)
    if rc != 0:   # timeout (124) or any failure: remembered for the backoff and for the patrol's outage alert
        st["last_fail_at"], st["fail_streak"] = snap.iso(now), int(st.get("fail_streak") or 0) + 1
    else:
        st["last_fail_at"], st["fail_streak"] = None, 0
    save_state(pid, st)
    info = {"ran": True, "rc": rc, "duration_s": dur, "inspect_calls": inspect_calls, "map_chars": len(map_text)}
    if rc != 0 and not msg.strip():
        info.update(ok=False, why="模型调用失败 rc=%s %s" % (rc, (err or "")[:160]))
        return info
    doc = parse_report(msg)
    clean, why = validate_report(doc, [l["id"] for l in project.get("lines") or []],
                                 [e["id"] for e in pending], meta.get("flagged") or [],
                                 list(st["followups"]))  # any known id: closed/not-due verdicts are dropped quietly
    if clean is None:
        info.update(ok=False, why="报告格式不合法：%s" % why, raw=msg[:300])
        return info
    commands = stream_commands(stream)
    viol = audit(commands)
    report = {"schema": SCHEMA_ID, "generated_at": snap.iso(now), "model": MODEL, "duration_s": dur,
              "inspect_calls": inspect_calls, "commands": len(commands), "map_chars": len(map_text), **clean}
    if viol:
        report["policy_violation"] = viol
    report["delivery"] = deliver(project, report, live, st, now, runner, apply, store, bool(viol))
    report["followup_ledger"] = {"open": sum(1 for e in st["followups"].values() if _is_open(e)),
                                 "pending_this_round": [e["id"] for e in pending]}
    try:
        store.put_supervisor(report)
    except core.ProgressError as exc:
        info.update(ok=False, why="报告存不进去：%s" % exc.message, report=report)
        return info
    st["last_report_at"] = report["generated_at"]
    save_state(pid, st)
    if apply:
        bad = [l["line"] for l in clean["lines"] if l["status"] != "ok"]
        try:
            store.append({"kind": "observe", "at": snap.iso(now), "actor": {"role": "patrol", "handle": SUP_HANDLE},
                          "summary": ("巡检员报告：需处理 %s" % ",".join(bad) if bad else "巡检员报告：各线正常")[:core.SUMMARY_MAX]
                                     + ("（含违规命令）" if viol else ""),
                          "refs": {"key": "supervisor:" + snap.iso(now)}})
        except core.ProgressError:
            pass
    info.update(ok=True, report=report)
    return info
