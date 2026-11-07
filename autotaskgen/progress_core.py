"""Progress records: project declarations, append-only events, and the folded state.

Storage (one directory per project under PROGRESS_HOME, default ~/.progress):
  <project>/project.json   declaration: final goal, cores, lines, milestones
  <project>/events.jsonl   append-only events, one JSON object per line
  <project>/live.json      liveness snapshot pushed by the Mac snapshotter

Everything here is standard library and runs on Python 3.9 (Mac) and 3.12 (WSL).
"""
from __future__ import annotations

import datetime as _dt
import fcntl
import json
import os
import pathlib
import re
import secrets
from typing import Any, Dict, List, Optional

import graph as _graph

SCHEMA_PROJECT = "progress-project/v1"
EVENT_VERSION = 1

KINDS = {
    "line_open", "line_close", "project_update",
    "node_plan", "node_dispatch", "node_heartbeat", "node_deliver", "node_review",
    "node_block", "node_supersede", "node_cancel",
    "milestone_claim", "milestone_confirm", "milestone_reject",
    "nudge", "auto_dispatch", "observe", "graph_propose", "worker_release",
    "decision", "human_reply",
}
NODE_KINDS = {"node_plan", "node_dispatch", "node_heartbeat", "node_deliver", "node_review",
              "node_block", "node_supersede", "node_cancel", "auto_dispatch", "decision", "human_reply"}
MILESTONE_KINDS = {"milestone_claim", "milestone_confirm", "milestone_reject"}
OUTCOMES = {"PASS", "PARTIAL", "FAIL", "UNKNOWN", "BLOCKED"}
BLOCKERS = {"dep", "design", "env", "resource", "human", "other", "budget"}
# router rules v2 R2 (11-07): a delivery is either done (PASS, optionally result=negative: the experiment ran, the
# research result missed) or BLOCKED with a category and what is still missing. PARTIAL / FAIL / UNKNOWN deliveries
# are refused for worker and router; reviews keep their four verdicts; imported history is untouched.
DELIVER_OUTCOMES = {"PASS", "BLOCKED"}
DELIVER_BLOCKERS = {"dep", "env", "human", "budget", "design"}
REVIEW_OUTCOMES = {"PASS", "PARTIAL", "FAIL", "UNKNOWN"}
RESULTS = {"negative"}
NODE_STATES = ["planned", "ready", "running", "delivered", "reviewed", "blocked", "failed",
               "superseded", "cancelled"]
ROLES = {"router", "worker", "coordinator", "patrol", "import", "human", "maintainer"}
SUMMARY_MAX = 120

# Which roles may write which kinds. Handle checks against the declaration happen in validate().
ROLE_KINDS = {
    "router": {"node_plan", "node_dispatch", "node_review", "node_supersede", "node_cancel",
               "node_block", "node_deliver", "node_heartbeat", "milestone_claim", "graph_propose"},
    "worker": {"node_heartbeat", "node_deliver", "node_block"},
    "coordinator": {"line_open", "line_close", "project_update", "milestone_confirm",
                    "milestone_reject", "node_plan", "node_cancel", "node_supersede", "graph_propose"},
    # patrol's node_deliver is only for decide nodes (panel agreement / human pick); validate() enforces it
    "patrol": {"nudge", "auto_dispatch", "observe", "node_block", "worker_release", "node_deliver",
               "decision", "human_reply"},
    "import": {"node_plan", "node_dispatch", "node_deliver", "node_review", "node_block",
               "node_supersede", "node_cancel", "line_open", "line_close"},
    "human": set(KINDS),
    # framework maintainer session acting on a recorded request (11-06: final_goal from the project lead); goal-set only
    "maintainer": {"project_update"},
}


class ProgressError(Exception):
    """Structured error: code plus human message; the CLI prints it as JSON."""

    def __init__(self, code: str, message: str, **detail: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def as_dict(self) -> Dict[str, Any]:
        d = {"ok": False, "code": self.code, "error": self.message}
        if self.detail:
            d["detail"] = self.detail
        return d


def home() -> pathlib.Path:
    return pathlib.Path(os.environ.get("PROGRESS_HOME") or "~/.progress").expanduser()


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def worker_phase(w: Dict[str, Any]) -> str:
    """One live.json worker: 'running' = screen busy and its Orca task not settled; 'lingering' = task settled,
    terminal still open; 'idle' = task not settled but the screen is not busy (at a prompt, stalled, or unknown).
    Only 'running' counts as 在跑 (11-07 lead: 13 counted, 3 actually busy). Same rule in lead.py."""
    if w.get("lingering") is True:
        return "lingering"
    return "running" if w.get("busy") is True else "idle"


def parse_iso(text: Optional[str]) -> Optional[_dt.datetime]:
    if not text:
        return None
    try:
        return _dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        try:
            v = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
            return v if v.tzinfo else v.replace(tzinfo=_dt.timezone.utc)
        except ValueError:
            return None


def project_dir(pid: str) -> pathlib.Path:
    if not pid or "/" in pid or pid.startswith("."):
        raise ProgressError("bad_project", "项目 id 不合法：%r" % pid)
    return home() / pid


def list_projects() -> List[str]:
    root = home()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if (p / "project.json").is_file())


# ---------------------------------------------------------------- declaration

def validate_project(doc: Dict[str, Any]) -> List[str]:
    errs = []
    if doc.get("schema") != SCHEMA_PROJECT:
        errs.append("schema 必须是 %s" % SCHEMA_PROJECT)
    for key in ("id", "name"):
        if not isinstance(doc.get(key), str) or not doc.get(key):
            errs.append("缺 %s" % key)
    fg = doc.get("final_goal")
    if not isinstance(fg, dict) or not fg.get("text"):
        errs.append("缺 final_goal.text")
    cores = {c.get("id") for c in doc.get("cores") or [] if isinstance(c, dict)}
    if not cores:
        errs.append("至少声明一个核心")
    for c in doc.get("cores") or []:
        if not c.get("coordinator"):
            errs.append("核心 %s 缺 coordinator" % c.get("id"))
    seen = set()
    for ln in doc.get("lines") or []:
        lid = ln.get("id")
        if not lid or lid in seen:
            errs.append("主线 id 缺失或重复：%r" % lid)
        seen.add(lid)
        if ln.get("core") not in cores:
            errs.append("主线 %s 的 core 未声明：%r" % (lid, ln.get("core")))
    ms_ids = set()
    for m in doc.get("milestones") or []:
        mid = m.get("id")
        if not mid or mid in ms_ids:
            errs.append("关键节点 id 缺失或重复：%r" % mid)
        ms_ids.add(mid)
        if m.get("core") not in cores:
            errs.append("关键节点 %s 的 core 未声明" % mid)
        for l in m.get("lines") or []:
            if l not in seen:
                errs.append("关键节点 %s 引用未声明主线 %s" % (mid, l))
        errs.extend(_validate_milestone_review_fields(m))
    for scope in [doc.get("defaults") or {}] + list(doc.get("lines") or []):
        mp = scope.get("max_parallel") if isinstance(scope, dict) else None
        if mp is not None and (not isinstance(mp, int) or isinstance(mp, bool) or mp < 1):
            errs.append("max_parallel 必须是正整数：%r" % mp)
    for m in doc.get("milestones") or []:
        for a in m.get("after") or []:
            if a not in ms_ids:
                errs.append("关键节点 %s 的 after 引用未知 %s" % (m.get("id"), a))
    return errs


CRITERION_STATUS = ("met", "unknown", "unmet")


def _validate_milestone_review_fields(m: Dict[str, Any]) -> List[str]:
    """Optional `stage` ("S1".."S9") and `criteria` [{id, text, status, evidence, closed_at?}] on a milestone
    (project-level review, docs/PROJECT_REVIEW.md). Missing is fine; malformed is rejected."""
    errs = []
    mid = m.get("id")
    stage = m.get("stage")
    if stage is not None and not (isinstance(stage, str) and re.fullmatch(r"S\d", stage)):
        errs.append("关键节点 %s 的 stage 必须是 S1/S2/S3 这样的字符串：%r" % (mid, stage))
    crit = m.get("criteria")
    if crit is None:
        return errs
    if not isinstance(crit, list):
        return errs + ["关键节点 %s 的 criteria 必须是数组" % mid]
    cids = set()
    for i, c in enumerate(crit):
        where = "关键节点 %s 的 criteria[%d]" % (mid, i)
        if not isinstance(c, dict):
            errs.append("%s 必须是对象 {id,text,status,evidence}" % where)
            continue
        cid = c.get("id")
        if not isinstance(cid, str) or not cid.strip():
            errs.append("%s 缺 id" % where)
        elif cid in cids:
            errs.append("%s 的 id 重复：%s" % (where, cid))
        else:
            cids.add(cid)
        if not isinstance(c.get("text"), str) or not c["text"].strip():
            errs.append("%s 缺 text" % where)
        if c.get("status") not in CRITERION_STATUS:
            errs.append("%s 的 status 只能是 %s：%r" % (where, "/".join(CRITERION_STATUS), c.get("status")))
        if c.get("evidence") is not None and not isinstance(c.get("evidence"), str):
            errs.append("%s 的 evidence 必须是字符串" % where)
        if c.get("closed_at") is not None and parse_iso(c.get("closed_at")) is None:
            errs.append("%s 的 closed_at 不是 ISO 时间：%r" % (where, c.get("closed_at")))
    return errs


def load_project(pid: str) -> Dict[str, Any]:
    path = project_dir(pid) / "project.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ProgressError("no_project", "项目未注册：%s" % pid, path=str(path))
    except ValueError as exc:
        raise ProgressError("bad_project", "project.json 不是合法 JSON：%s" % exc)
    return doc


def save_project(doc: Dict[str, Any]) -> None:
    errs = validate_project(doc)
    if errs:
        raise ProgressError("bad_project", "项目声明不合法", errors=errs)
    d = project_dir(doc["id"])
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "project.json.new"
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(d / "project.json")


def line_of(project: Dict[str, Any], lid: Optional[str]) -> Optional[Dict[str, Any]]:
    for ln in project.get("lines") or []:
        if ln.get("id") == lid:
            return ln
    return None


def core_of(project: Dict[str, Any], cid: Optional[str]) -> Optional[Dict[str, Any]]:
    for c in project.get("cores") or []:
        if c.get("id") == cid:
            return c
    return None


def milestone_of(project: Dict[str, Any], mid: Optional[str]) -> Optional[Dict[str, Any]]:
    for m in project.get("milestones") or []:
        if m.get("id") == mid:
            return m
    return None


def human_handles(project: Dict[str, Any]) -> set:
    return set(project.get("humans") or [])


# --------------------------------------------------------------------- events

def read_events(pid: str) -> List[Dict[str, Any]]:
    path = project_dir(pid) / "events.jsonl"
    out = []
    try:
        with path.open(encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    out.append(json.loads(raw))
                except ValueError:
                    continue  # a torn line never takes the board down
    except FileNotFoundError:
        pass
    return out


def new_id() -> str:
    return "ev_" + _dt.datetime.now(_dt.timezone.utc).strftime("%m%d%H%M%S") + "_" + secrets.token_hex(3)


def _same(a: Optional[str], b: Optional[str]) -> bool:
    """Handles compare by prefix so term_7d3ec177 matches the full uuid form."""
    if not a or not b:
        return False
    return a == b or a.startswith(b) or b.startswith(a)


def _sub_router_owns(nodes: Dict[str, Any], nid: Optional[str], handle: Optional[str], include_self: bool) -> bool:
    """True when `handle` is the worker of a plan node that is `nid` itself (include_self) or an ancestor of it:
    that sub-router owns the subtree it proposed."""
    if not handle or not nid or nid not in nodes:
        return False
    cur = nid if include_self else nodes[nid].get("parent")
    seen = set()
    while cur and cur in nodes and cur not in seen:
        seen.add(cur)
        n = nodes[cur]
        if n.get("kind") == "plan" and _same(handle, n.get("worker")):
            return True
        cur = n.get("parent")
    return False


def validate(event: Dict[str, Any], project: Dict[str, Any], state: Dict[str, Any]) -> List[Dict[str, str]]:
    """Return a list of {code, error}; empty means the event may be appended."""
    errs: List[Dict[str, str]] = []

    def bad(code, msg):
        errs.append({"code": code, "error": msg})

    kind = event.get("kind")
    if kind not in KINDS:
        bad("bad_kind", "未知 kind：%r" % kind)
        return errs
    if event.get("project") != project.get("id"):
        bad("bad_project", "事件 project 与声明不符")
    actor = event.get("actor") or {}
    role = actor.get("role")
    handle = actor.get("handle")
    if role not in ROLES:
        bad("bad_role", "未知角色：%r" % role)
        return errs
    if kind not in ROLE_KINDS[role]:
        bad("role_kind", "角色 %s 不能写 %s" % (role, kind))
    summary = event.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        bad("no_summary", "summary 必填（白话一句）")
    elif len(summary) > SUMMARY_MAX:
        bad("long_summary", "summary 超过 %d 字" % SUMMARY_MAX)
    if event.get("outcome") is not None and event.get("outcome") not in OUTCOMES:
        bad("bad_outcome", "outcome 只能是 %s" % "/".join(sorted(OUTCOMES)))
    if kind in ("node_deliver", "node_review") and event.get("outcome") not in OUTCOMES:
        bad("no_outcome", "%s 必须带 outcome" % kind)
    if kind == "node_block" and event.get("blocker") not in BLOCKERS:
        bad("bad_blocker", "blocker 只能是 %s" % "/".join(sorted(BLOCKERS)))
    if kind == "node_review" and event.get("outcome") == "BLOCKED":
        bad("bad_outcome", "审查结论只能是 %s" % "/".join(sorted(REVIEW_OUTCOMES)))
    if kind == "node_deliver" and role in ("worker", "router") and event.get("outcome") in OUTCOMES:
        oc = event.get("outcome")
        if oc not in DELIVER_OUTCOMES:
            bad("deliver_outcome", "交付不收 %s，只收 PASS 或 BLOCKED：没做完就接着做到完成再交；实验做完但研究结果不达标，"
                "记 PASS 加 --result negative；被挡住或预算用完，记 BLOCKED 加 --blocker dep|env|human|budget|design "
                "和 --blocked-by \"还差什么\"。" % oc)
        elif oc == "BLOCKED":
            if event.get("blocker") not in DELIVER_BLOCKERS:
                bad("deliver_blocker", "BLOCKED 交付必须带 --blocker，只能是 %s" % "/".join(sorted(DELIVER_BLOCKERS)))
            if not str(event.get("blocked_by") or "").strip():
                bad("deliver_blocker", "BLOCKED 交付必须用 --blocked-by 写明还差什么（预算用完就写剩下的工作量）")
    if kind == "node_deliver" and event.get("result") is not None and (
            event.get("result") not in RESULTS or event.get("outcome") != "PASS"):
        bad("bad_result", "result 只能是 negative，且只用在 PASS 交付上")
    for msg in _push_field_errors(event):
        bad("bad_push", msg)

    lid = event.get("line")
    line = line_of(project, lid) if lid else None
    needs_line = kind in NODE_KINDS or kind in ("line_open", "line_close", "nudge", "milestone_claim",
                                                "graph_propose", "worker_release")
    if needs_line and not line:
        bad("unknown_line", "主线未声明：%r" % lid)
    elif lid and not line:
        bad("unknown_line", "主线未声明：%r" % lid)

    node = event.get("node")
    nodes = (state.get("lines", {}).get(lid) or {}).get("nodes", {}) if lid else {}
    if kind in NODE_KINDS:
        if not node:
            bad("no_node", "节点事件必须带 node")
        elif kind != "node_plan" and node not in nodes:
            bad("unknown_node", "节点 %s 还没有 node_plan" % node)
    if kind == "node_plan" and line:
        for d in event.get("deps") or []:
            if d not in nodes and d != node:
                bad("unknown_dep", "依赖节点 %s 未计划" % d)
    if kind == "graph_propose" and line:
        prop = event.get("proposal")
        if not isinstance(prop, dict):
            bad("no_proposal", "graph_propose 必须带 proposal 对象")
        else:
            res = _graph.gate(dict(prop, line=lid, parent=node or prop.get("parent")), project, state)
            for msg in res["errors"]:
                bad("gate", msg)
    if kind == "node_deliver" and role == "patrol" and node in nodes:
        if nodes[node].get("kind") != "decide":
            bad("patrol_deliver", "巡检只能为 decide 节点写 node_deliver（节点 %s 不是 decide）" % node)
        elif not (event.get("refs") or {}).get("card"):
            bad("patrol_deliver", "巡检写 decide 节点的 node_deliver 必须带 refs.card")
    # (the 11-06 early_deliver gate on PARTIAL/FAIL is gone: R2 refuses those deliveries outright)
    if kind == "worker_release" and node and node not in nodes:
        bad("unknown_node", "节点 %s 还没有 node_plan" % node)
    if kind == "node_supersede":
        by = event.get("by")
        if not by or by not in nodes:
            bad("bad_supersede", "node_supersede 的 by 必须是本主线已计划的节点")

    mid = event.get("milestone")
    ms = milestone_of(project, mid) if mid else None
    if kind in MILESTONE_KINDS and not ms:
        bad("unknown_milestone", "关键节点未声明：%r" % mid)
    elif mid and not ms:
        bad("unknown_milestone", "关键节点未声明：%r" % mid)
    if kind == "milestone_claim" and ms and line and lid not in (ms.get("lines") or []):
        bad("claim_line", "主线 %s 不负责关键节点 %s" % (lid, mid))

    # handle checks: misuse guard, not forgery-proof (handles come from env)
    if role == "human":
        if not any(_same(handle, h) for h in human_handles(project)):
            bad("not_human", "句柄 %s 不在 project.humans" % handle)
    elif role == "router" and line and not _same(handle, line.get("router")) and not _sub_router_owns(
            nodes, node, handle, include_self=kind == "graph_propose"):
        bad("not_router", "只有主线 %s 的 router（或负责该子树的子 router）能写 %s" % (lid, kind))
    elif role == "coordinator":
        core_ids = []
        if kind in MILESTONE_KINDS and ms:
            core_ids = [ms.get("core")]
        elif line:
            core_ids = [line.get("core")]
        else:
            core_ids = [c.get("id") for c in project.get("cores") or []]
        if not any(_same(handle, (core_of(project, c) or {}).get("coordinator")) for c in core_ids):
            bad("not_coordinator", "句柄 %s 不是对应核心的协调人" % handle)
    elif role == "worker" and node in nodes:
        n = nodes[node]
        ok = _same(handle, n.get("worker")) or (
            actor.get("workspace") and n.get("worktree") and
            (actor["workspace"] == n["worktree"] or actor["workspace"].endswith(n["worktree"]) or
             n["worktree"].endswith(actor["workspace"])))
        if not ok:
            bad("not_worker", "句柄不是节点 %s 派出的 worker" % node)
    if kind in ("milestone_confirm", "milestone_reject") and ms:
        mst = (state.get("milestones") or {}).get(mid) or {}
        if mst.get("status") != "claimed" and kind == "milestone_reject":
            bad("not_claimed", "关键节点 %s 没有待确认的报告" % mid)
    return errs


PUSH_KINDS = ("node_deliver", "node_review")


def _push_field_errors(event: Dict[str, Any]) -> List[str]:
    """deliver/review carry at most one push result (workpush.py): pushed {branch, sha}, push_failed or push_skipped."""
    keys = [k for k in ("pushed", "push_failed", "push_skipped") if k in event]
    if not keys:
        return []
    if event.get("kind") not in PUSH_KINDS:
        return ["%s 只能出现在 node_deliver / node_review 上" % keys[0]]
    if len(keys) > 1:
        return ["pushed / push_failed / push_skipped 只能有一个"]
    v = event[keys[0]]
    if keys[0] == "pushed":
        if not (isinstance(v, dict) and isinstance(v.get("branch"), str) and v["branch"].strip()
                and isinstance(v.get("sha"), str) and re.fullmatch(r"[0-9a-f]{7,64}", v["sha"])):
            return ["pushed 必须是 {branch, sha}"]
        if v["branch"] in ("main", "master"):
            return ["pushed.branch 不能是 %s" % v["branch"]]
        return []
    if not isinstance(v, str) or not v.strip() or len(v) > 500:
        return ["%s 必须是 1–500 字的原因" % keys[0]]
    return []


def same_acceptance_errors(normalized: Dict[str, Any]) -> List[str]:
    """Parallel segments the gate downgraded because siblings share the very same acceptance criteria. That is a
    writing slip, not structure: rejected so the router writes each node its own criteria (or marks the segment
    serial) before anything is committed. 11-06/07: SR and NEWP committed such segments, saw them turned serial,
    and cancelled and re-proposed (4 cancels in 45 min). Overlapping write scopes / inner deps still downgrade."""
    out = []
    for seg in (normalized or {}).get("segments") or []:
        if seg.get("downgraded_from") != "parallel":
            continue
        kids = seg.get("nodes") or []
        for i in range(len(kids)):
            for j in range(i + 1, len(kids)):
                a, b = kids[i], kids[j]
                if a.get("acceptance") and a.get("acceptance") == b.get("acceptance"):
                    out.append("段 %s 标为 parallel，但 %s 与 %s 验收标准完全相同，不能各自验收：给每个节点写只属于它的验收"
                               "（它自己的产物、测试或报告），或者把这段标成 serial" % (seg.get("id"), a.get("id"), b.get("id")))
    return out


def append(pid: str, event: Dict[str, Any]) -> Dict[str, Any]:
    """Validate against the current state and append under an exclusive lock."""
    d = project_dir(pid)
    if not (d / "project.json").is_file():
        raise ProgressError("no_project", "项目未注册：%s" % pid)
    d.mkdir(parents=True, exist_ok=True)
    lock = (d / ".lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        project = load_project(pid)
        events = read_events(pid)
        event = dict(event)
        event.setdefault("v", EVENT_VERSION)
        event.setdefault("id", new_id())
        event.setdefault("at", now_iso())
        event["project"] = pid
        if any(e.get("id") == event["id"] for e in events):
            return {"ok": True, "id": event["id"], "duplicate": True}
        if event.get("kind") == "graph_propose" and isinstance(event.get("proposal"), dict):
            # the parent plan node is the event's node (ownership checks read it)
            if not event.get("node") and event["proposal"].get("parent"):
                event["node"] = event["proposal"]["parent"]
        state = fold(project, events)
        errs = validate(event, project, state)
        if errs:
            if event.get("kind") == "graph_propose" and any(e["code"] == "gate" for e in errs):
                msgs = [e["error"] for e in errs]
                raise ProgressError("gate_rejected", "子图提议未通过闸门：共 %d 条问题" % len(msgs), errors=msgs)
            raise ProgressError(errs[0]["code"], errs[0]["error"], errors=errs)
        warnings = []
        if event.get("kind") == "graph_propose":
            res = _graph.gate(dict(event["proposal"], line=event.get("line"),
                                   parent=event.get("node") or event["proposal"].get("parent")), project, state)
            same = same_acceptance_errors(res["normalized"])
            if same:
                raise ProgressError("gate_rejected", "子图提议未通过闸门：共 %d 条问题" % len(same), errors=same)
            event["proposal"] = res["normalized"]
            event["children"] = res["children"]
            warnings = res["warnings"]
            if warnings:
                event["warnings"] = warnings
        with (d / "events.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        out = {"ok": True, "id": event["id"]}
        if event.get("kind") == "node_plan" and event.get("source") != "import":
            kind_ = event.get("node_kind") if event.get("node_kind") in NODE_KINDS_RUN else infer_node_kind(event.get("node"))
            if kind_ in ("run", "plan") and not (event.get("milestones") or event.get("milestone")):
                out["warnings"] = ["未挂里程碑：节点 %s 没有 --milestones，项目级巡检把它算作没挂到任何完成标准条目的耗时，请补挂" % event.get("node")]
        if event.get("kind") == "graph_propose":
            out.update(children=event["children"], warnings=warnings,
                       segments=[{"id": sg["id"], "mode": sg["mode"], "nodes": [c["id"] for c in sg["nodes"]],
                                  **({"downgraded_from": "parallel"} if sg.get("downgraded_from") else {})}
                                 for sg in event["proposal"]["segments"]])
        return out
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


# ----------------------------------------------------------------------- fold

NODE_KINDS_RUN = _graph.NODE_TYPES  # run, review (legacy), plan, check, decide
_REVIEW_WORDS = ("review", "recheck", "audit")


def infer_node_kind(nid: str) -> str:
    """Default node kind from its id: review-ish words -> "review", else "run"."""
    low = str(nid or "").lower()
    return "review" if any(w in low for w in _REVIEW_WORDS) else "run"


def _node(line_state: Dict[str, Any], nid: str) -> Dict[str, Any]:
    return line_state["nodes"].setdefault(nid, {
        "id": nid, "status": "planned", "deps": [], "history": [], "milestones": [],
    })


def _fold_proposal(ls: Dict[str, Any], ev: Dict[str, Any]) -> None:
    """A committed sub-graph proposal: mark the parent plan node as expanding, create the child nodes."""
    prop = ev.get("proposal") or {}
    at_ev = ev.get("at")
    pid = prop.get("parent")
    rec = {"at": at_ev, "kind": "graph_propose", "outcome": None, "summary": ev.get("summary"),
           "actor": (ev.get("actor") or {}).get("role"), "source": ev.get("source")}
    if pid:
        p = _node(ls, pid)
        p.setdefault("proposals", []).append(ev.get("id"))
        p["history"].append(rec)
        p["updated_at"] = at_ev
        if p["status"] in ("planned", "ready", "blocked"):
            p["status"] = "running"
    for seg in prop.get("segments") or []:
        for c in seg.get("nodes") or []:
            n = _node(ls, c["id"])
            n["history"].append(rec)
            n["updated_at"] = at_ev
            n["summary"] = ev.get("summary") or n.get("summary")
            n["title"] = c.get("title") or c["id"]
            n["deps"] = list(c.get("deps") or [])
            n["require_review"] = list(c.get("require_review") or [])
            n["spec"] = c.get("spec")
            n["route"] = c.get("route")
            n["auto_dispatch"] = bool(c.get("auto_dispatch", bool(c.get("spec") and c.get("route"))))
            n["expected_duration_minutes"] = c.get("expected_minutes") or (c.get("budget") or {}).get("minutes")
            n["kind"] = c.get("kind") if c.get("kind") in NODE_KINDS_RUN else "run"
            n["milestones"] = list(c.get("milestones") or [])
            n["planned_at"] = n.get("planned_at") or at_ev
            n["status_unknown"] = False
            n["parent"] = pid
            n["proposal"] = ev.get("id")
            n["segment"] = seg.get("id")
            n["segment_mode"] = seg.get("mode")
            n["write_scope"] = list(c.get("write_scope") or [])
            n["budget"] = dict(c.get("budget") or {})
            n["acceptance"] = list(c.get("acceptance") or [])
            if n["status"] not in ("running", "delivered", "reviewed"):
                n["status"] = "planned"


def _dep_met(nodes: Dict[str, Any], dep: str, need_review: bool, seen=None) -> bool:
    seen = seen or set()
    if dep in seen:
        return False
    seen.add(dep)
    n = nodes.get(dep)
    if not n:
        return False
    if n["status"] == "superseded" and n.get("by"):
        return _dep_met(nodes, n["by"], need_review, seen)
    if need_review:
        return n["status"] == "reviewed" and n.get("review") == "PASS"
    if n["status"] in ("delivered", "reviewed"):
        return n.get("outcome") in ("PASS", "PARTIAL") and n.get("review") != "FAIL"
    return False


def fold(project: Dict[str, Any], events: List[Dict[str, Any]], at: Optional[str] = None) -> Dict[str, Any]:
    """Fold events into per-line node states, milestone states and a recent feed."""
    lines: Dict[str, Any] = {}
    for ln in project.get("lines") or []:
        lines[ln["id"]] = {"id": ln["id"], "open": ln.get("status", "active") != "closed",
                           "nodes": {}, "last_event_at": None, "nudges": []}
    milestones: Dict[str, Any] = {m["id"]: {"id": m["id"], "status": "open", "claims": []}
                                  for m in project.get("milestones") or []}
    for ev in sorted(events, key=lambda e: e.get("at") or ""):
        kind = ev.get("kind")
        lid = ev.get("line")
        ls = lines.get(lid)
        at_ev = ev.get("at")
        if ls is not None:
            ls["last_event_at"] = at_ev
        if kind == "line_open" and ls is not None:
            ls["open"] = True
        elif kind == "line_close" and ls is not None:
            ls["open"] = False
        elif kind == "nudge" and ls is not None:
            ls["nudges"].append(at_ev)
        if kind == "graph_propose" and ls is not None and isinstance(ev.get("proposal"), dict):
            _fold_proposal(ls, ev)
        elif kind == "worker_release" and ls is not None and ev.get("node") in ls["nodes"]:
            wn = ls["nodes"][ev["node"]]
            wn.setdefault("releases", []).append({"at": at_ev, "released": bool(ev.get("released")),
                                                  "final": bool(ev.get("final")), "result": ev.get("result"),
                                                  "dispatch": (ev.get("refs") or {}).get("dispatch")})
            if ev.get("released"):
                wn["released_at"] = at_ev
        if kind in NODE_KINDS and ls is not None and ev.get("node"):
            n = _node(ls, ev["node"])
            n["history"].append({"at": at_ev, "kind": kind, "outcome": ev.get("outcome"),
                                 "summary": ev.get("summary"), "actor": (ev.get("actor") or {}).get("role"),
                                 "source": ev.get("source")})
            n["updated_at"] = at_ev
            n["summary"] = ev.get("summary") or n.get("summary")
            if ev.get("source") == "import":
                n["imported"] = True
            refs = ev.get("refs") or {}
            for k in ("task", "dispatch", "head", "report", "spec"):
                if refs.get(k):
                    n.setdefault("refs", {})[k] = refs[k]
            if kind == "node_plan":
                n["title"] = ev.get("title") or n.get("title") or ev["node"]
                n["deps"] = list(ev.get("deps") or [])
                n["require_review"] = list(ev.get("require_review") or [])
                n["spec"] = ev.get("spec") or n.get("spec")
                n["route"] = ev.get("route") or n.get("route")
                n["auto_dispatch"] = bool(ev.get("auto_dispatch", bool(ev.get("spec") and ev.get("route"))))
                n["expected_duration_minutes"] = ev.get("expected_duration_minutes")
                n["kind"] = ev.get("node_kind") if ev.get("node_kind") in NODE_KINDS_RUN else infer_node_kind(ev["node"])
                n["milestones"] = list(ev.get("milestones") or ([ev["milestone"]] if ev.get("milestone") else []))
                n["planned_at"] = n.get("planned_at") or at_ev
                n["status_unknown"] = bool(ev.get("status_unknown"))
                if n["status"] not in ("running", "delivered", "reviewed"):
                    n["status"] = "planned"
            elif kind == "auto_dispatch" and ev.get("phase") == "started":
                # patrol's follow-up to its own placeholder: refs only, not a second dispatch
                n["worker"] = refs.get("terminal") or n.get("worker")
                n["worktree"] = refs.get("worktree") or n.get("worktree")
            elif kind in ("node_dispatch", "auto_dispatch"):
                n["status"] = "running"
                n.pop("prereq_missing", None)
                n["dispatched_at"] = at_ev
                n["dispatch_count"] = n.get("dispatch_count", 0) + 1
                n["worker"] = refs.get("terminal") or n.get("worker")
                n["worktree"] = refs.get("worktree") or n.get("worktree")
                act = ev.get("actor") or {}
                n["model"] = (ev.get("route") or {}).get("model") or act.get("model") or n.get("model")
                n["auto"] = kind == "auto_dispatch"
            elif kind == "node_heartbeat":
                n["last_beat"] = at_ev
                if n["status"] in ("planned", "ready", "blocked"):
                    n["status"] = "running"
            for k in ("failures", "candidates", "touched"):
                if kind in ("node_deliver", "node_heartbeat", "node_block") and isinstance(ev.get(k), list):
                    n[k] = [str(x) for x in ev[k] if str(x).strip()][:50]
            if kind == "node_deliver":
                for k in ("failures", "candidates"):
                    if not isinstance(ev.get(k), list):
                        n.pop(k, None)  # a new delivery replaces the old signal
            if kind in PUSH_KINDS and isinstance(ev.get("pushed"), dict):
                n["pushed"] = {"branch": ev["pushed"].get("branch"), "sha": ev["pushed"].get("sha"), "at": at_ev}
                n.pop("push_failed", None)
            elif kind in PUSH_KINDS and isinstance(ev.get("push_failed"), str):
                n["push_failed"] = {"error": ev["push_failed"], "at": at_ev}
            if kind == "node_deliver":
                n["outcome"] = ev.get("outcome")
                n["delivered_at"] = at_ev
                n["status"] = "failed" if ev.get("outcome") == "FAIL" else "delivered"
                n["model"] = (ev.get("actor") or {}).get("model") or n.get("model")
                n["result"] = ev.get("result")
                if ev.get("outcome") == "BLOCKED":   # R2: a blocked delivery leaves the node blocked, not delivered
                    n["status"] = "blocked"
                    n["blocker"] = ev.get("blocker")
                    n["blocked_at"] = at_ev
                    n["blocked_by"] = ev.get("blocked_by")
            elif kind == "node_review":
                n["review"] = ev.get("outcome")
                n["reviewed_at"] = at_ev
                n["status"] = "failed" if ev.get("outcome") == "FAIL" else "reviewed"
            elif kind == "node_block":
                n["status"] = "blocked"
                n["blocker"] = ev.get("blocker")
                n["blocked_at"] = at_ev
                # R1: patrol re-checks these and dispatches once every listed input exists
                n["prereq_missing"] = list(ev.get("prereq_missing") or []) or None
            elif kind == "node_supersede":
                n["status"] = "superseded"
                n["by"] = ev.get("by")
                if ev.get("by"):
                    _node(ls, ev["by"]).setdefault("supersedes", []).append(ev["node"])
            elif kind == "node_cancel":
                n["status"] = "cancelled"
        if kind in MILESTONE_KINDS and ev.get("milestone") in milestones:
            m = milestones[ev["milestone"]]
            rec = {"at": at_ev, "line": lid, "summary": ev.get("summary"),
                   "by": (ev.get("actor") or {}).get("handle"), "refs": ev.get("refs") or {}}
            if kind == "milestone_claim":
                m["claims"].append(rec)
                if m["status"] != "confirmed":
                    m["status"] = "claimed"
                    m["claimed_at"] = at_ev
            elif kind == "milestone_confirm":
                m["status"] = "confirmed"
                m["confirmed_at"] = at_ev
                m["confirm"] = rec
            elif kind == "milestone_reject":
                m["status"] = "open"
                m["rejected"] = rec
    # ready = planned with all deps met
    for ls in lines.values():
        nodes = ls["nodes"]
        for n in nodes.values():
            if n["status"] == "planned" and not n.get("status_unknown"):
                need = set(n.get("require_review") or [])
                if all(_dep_met(nodes, d, d in need) for d in n.get("deps") or []):
                    n["status"] = "ready"
        counts = {s: 0 for s in NODE_STATES}
        for n in nodes.values():
            counts[n["status"]] = counts.get(n["status"], 0) + 1
        ls["counts"] = counts
        chain = 0
        for n in nodes.values():
            if n["status"] == "superseded":
                chain += 1
        ls["rework"] = chain
    feed = sorted((e for e in events if e.get("source") != "import"),
                  key=lambda e: e.get("at") or "", reverse=True)[:80]
    total = len(milestones)
    done = sum(1 for m in milestones.values() if m["status"] == "confirmed")
    claimed = sum(1 for m in milestones.values() if m["status"] == "claimed")
    return {
        "project": project.get("id"),
        "built_at": at or now_iso(),
        "lines": lines,
        "milestones": milestones,
        "progress": {"total": total, "confirmed": done, "claimed": claimed},
        "feed": feed,
        "event_count": len(events),
    }


def load_live(pid: str) -> Optional[Dict[str, Any]]:
    try:
        return json.loads((project_dir(pid) / "live.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_live(pid: str, doc: Dict[str, Any]) -> None:
    d = project_dir(pid)
    if not (d / "project.json").is_file():
        raise ProgressError("no_project", "项目未注册：%s" % pid)
    doc = dict(doc)
    doc.setdefault("generated_at", now_iso())
    tmp = d / "live.json.new"
    tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    tmp.replace(d / "live.json")


SUPERVISOR_KEEP = 48


def supervisor_dir(pid: str) -> pathlib.Path:
    return project_dir(pid) / "supervisor"


def load_supervisor(pid: str) -> Optional[Dict[str, Any]]:
    try:
        doc = json.loads((supervisor_dir(pid) / "latest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def save_supervisor(pid: str, doc: Dict[str, Any], keep: int = SUPERVISOR_KEEP, require_project: bool = True) -> None:
    """latest.json (atomic) plus a dated copy; only the newest `keep` dated copies survive."""
    d = project_dir(pid)
    if require_project and not (d / "project.json").is_file():
        raise ProgressError("no_project", "项目未注册：%s" % pid)
    sd = d / "supervisor"
    sd.mkdir(parents=True, exist_ok=True)
    doc = dict(doc)
    doc.setdefault("generated_at", now_iso())
    text = json.dumps(doc, ensure_ascii=False)
    stamp = re.sub(r"[^0-9TZ]", "", str(doc["generated_at"]))
    for name in ("report-%s.json" % stamp, "latest.json"):
        tmp = sd / (name + ".new")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(sd / name)
    old = sorted(p.name for p in sd.glob("report-*.json"))
    for name in old[:-keep] if keep > 0 else old:
        try:
            (sd / name).unlink()
        except OSError:
            pass
