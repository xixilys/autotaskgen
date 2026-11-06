#!/usr/bin/env python3
"""Project-level review (docs/PROJECT_REVIEW_SPEC.md, docs/PROJECT_REVIEW.md).

Not the node-level supervisor: it asks whether the whole project is moving toward its done-when criteria, whether
effort is spent in the right place, whether work is meaningless, and whether the dependency graph slows us down.

- build_dossier(): pure function over (project, events, live, now); every number carries its denominator and window;
  the model never does arithmetic. The text goes through the holdout denylist (dream.Guard) before anything is
  written or sent: a hit refuses the whole run and only a count is logged.
- rollout stage (project.defaults.project_review.stage, default 1): 1 = dossier only; 2 = + one model (Opus), report
  saved, nothing sent; 3 = two independent models + merge + send (coordinators, QQ notice, human digest).
- state lives in the Mac task cache under "project_review:<pid>" (follow-up ledger, pending proposals, criteria
  closing times); outputs under ~/.progress/<pid>/project_review/.
- PROGRESS_REVIEW_A_CMD / PROGRESS_REVIEW_B_CMD replace the two model commands (tests): prompt on stdin, stdout = JSON.

CLI: project_review.py --project dse [--stage N] [--force] [--dry-run] [--print]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import difflib
import hashlib
import json
import os
import pathlib
import re
import shlex
import sys
import tempfile
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import decide  # noqa: E402
import dream  # noqa: E402
import progress_core as core  # noqa: E402
import snapshot as snap  # noqa: E402
import supervisor as sup  # noqa: E402

SCHEMA_ID = "progress-project-review/v1"
STATE_PREFIX = "project_review:"
SAMPLE_MIN = 5
CONC_WINDOW_H = 24
INVEST_WINDOW_H_DEFAULT = 72
OVERHEAD_SHORT_H = 24        # second, per-line overhead window (shown next to the invest window)
NEAR_DONE_H = 48
KEEP_FILES = 40
MODEL_TIMEOUT_S = 20 * 60
SLOT_HOURS_BJ = (2, 8, 14, 20)
CHECK_H_MIN, CHECK_H_MAX = 6, 48
MAX_ADJ = 6
PENDING_TTL_H = 72
SIM_RATIO = 0.55
DEFAULT_EST_H = 24.0

# thresholds (spec §2)
FAST_SHARE_MIN = 0.30
OVERHEAD_WARN, OVERHEAD_PROBLEM = 0.35, 0.50
UNATTACHED_WARN = 0.25
ZERO_CONC_WARN = 0.50
FREE_MEM_GB = 16.0
LEAK_WARN = 0.40
PARTIAL_WARN = 0.50
PARTIAL_MIN_DELIVERIES = 3     # deviation from the spec: a ratio over fewer than 3 deliveries is noise
DECISION_DELAY_H = 6.0

DEFAULT_WORKLOAD_TERMS = ["md", "qwen", "cg", "lammps", "rhodo", "jacobi", "pagerank", "cholesky", "vit", "dit", "olmoe"]
DEFAULT_LEAK_LINES = ["EVB", "FE", "SR"]
DEFAULT_TRUTH_MILESTONES = ["B7", "B9"]
EXT_BASELINES = ["Timeloop", "Accelergy", "LLMCompass", "MAESTRO", "SMAC"]

REPAIR_RE = re.compile(r"repair|recheck|resume|fix|修|复核", re.I)
SPEED_ACC_RE = re.compile(r"速度|精度|精确|准确|快速|周期误差|误差|质量门|分钟以内|校准|排序|accuracy|speed|gated", re.I)
IFACE_RE = re.compile(r"接口|入口|流程|上线|调用|\bAPI\b|interface|validate", re.I)
TRUTH_RE = re.compile(r"真值|参照|RTL|实测|锚点|外部工具|公开", re.I)
TIER3_RE = re.compile(r"最终目标|改目标|调整目标|修改完成标准|改完成标准|预算|对外|留出集|holdout|算力追加|追加算力", re.I)

TYPES = ("restructure_milestone", "reallocate", "downgrade", "stop", "add_work", "unblock", "calibrate", "goal_draft")
TO_VALUES = ("coordinator:core1", "coordinator:core2", "human", "tooling")
RESULTS = ("PASS", "FAIL", "UNSURE")
VERDICTS = ("done", "not_done", "obsolete", "cannot_tell")
VERDICTS_ENUM = list(VERDICTS)
SUMMARY_OK = ("on_track", "at_risk", "off_track")

CHECKLIST_TEXT: Dict[str, List[Tuple[str, str]]] = {
    "pm": [
        ("pm-1", "最近 24 小时有没有关闭完成标准条目？没有的话，主因是什么？"),
        ("pm-2", "投入是否压在关键路径和创新点（S1 的快速方法）上？"),
        ("pm-3", "开销是否超标？哪类审查或返修可以砍？"),
        ("pm-4", "有没有被依赖图挡住的近完成项、疑似多余依赖？"),
        ("pm-5", "有没有线空着、机器也空着？给出不依赖别人、挂在条目上的候选活（每线 1–3 个）。"),
        ("pm-6", "有没有持续 24 小时以上、产出全是 PARTIAL、没关任何条目的分支？给出继续或停掉的判断。"),
    ],
    "codesign": [
        ("cd-1", "精度真值是不是循环的（快模型对慢模型，而慢模型本身没有校准）？有没有 RTL 或实测锚点？"),
        ("cd-2", "有没有在性能模拟里做和时序无关的功能级精确工作（逐位数值、完整功能执行）？这些工作能不能改成「功能轨迹 + 时序模拟」？"),
        ("cd-3", "评估规模是否和用途相称？做排序和找瓶颈时，有没有用代表区段或抽样？"),
        ("cd-4", "有没有外部工具和搜索基线（Timeloop/Accelergy、LLMCompass、MAESTRO、SMAC/随机搜索）？"),
        ("cd-5", "Agent 的能力有没有用真实大模型测过（对比搜索基线的样本效率）？"),
    ],
    "ic": [
        ("ic-1", "面积、功耗模型有没有负责人、来源（ASAP7 部件库、综合校准点）、误差数？"),
        ("ic-2", "负载接入是「写数据」还是「写代码」？负载专用代码占比的趋势如何？"),
        ("ic-3", "映射和编译是否有通用表示（算子图、循环嵌套）？新负载能不能不写代码进入评估器？"),
        ("ic-4", "物理流程（综合、STA）的结论有没有标清条件（工具版本、约束、是否布线）？"),
    ],
}
CHECK_IDS = {cid for items in CHECKLIST_TEXT.values() for cid, _t in items}
CHECK_TITLES = {"pm": "A 科研项目管理", "codesign": "B 软硬件协同", "ic": "C IC 自动化"}


def _obj(props: Dict[str, Any]) -> Dict[str, Any]:
    # strict schema (codex --output-schema): every property required, no extras, optional = nullable
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


_S = {"type": "string"}
_CHECK_ITEM = _obj({"id": _S, "result": {"type": "string", "enum": list(RESULTS)}, "evidence": _S})
SCHEMA = _obj({
    "verdict": {"type": "string", "enum": list(SUMMARY_OK)},
    "summary": _S,
    "stage_progress": {"type": "array", "items": _obj({
        "stage": _S, "met": {"type": "integer"}, "total": {"type": "integer"}, "closed_24h": {"type": "integer"}})},
    "checklists": _obj({"pm": {"type": "array", "items": _CHECK_ITEM},
                        "codesign": {"type": "array", "items": _CHECK_ITEM},
                        "ic": {"type": "array", "items": _CHECK_ITEM}}),
    "adjustments": {"type": "array", "items": _obj({
        "type": {"type": "string", "enum": list(TYPES)},
        "to": {"type": "string", "enum": list(TO_VALUES)},
        "what": _S, "why": _S, "expected_effect": _S, "cost": _S, "done_when": _S,
        "check_after_h": {"type": "integer"}})},
    "followups": {"type": "array", "items": _obj({
        "id": _S, "verdict": {"type": "string", "enum": VERDICTS_ENUM}, "evidence": _S})},
})

PROMPT = """你是 dse 项目的项目级评审。你不干活、不派活、不发消息，只读档案、按三份清单逐项判断，再提出项目级调整，由脚本发给协调人或用户。{family_note}

项目要做到：给一个新的科学计算或神经网络负载，只给应用描述、不写专门代码，Agent 在真实评估器上自动迭代出好设计；评估器要又快又准（这是创新点）。完成标准是 D1–D4，阶段是 S1 快准评估器 → S2 开发集真实闭环 → S3 新负载泛化，都在档案第 1 段。

下面是脚本生成的项目档案（副本在 {dossier_path}）。档案里的数都由脚本算出，你不要自己重算或编数。需要核实时用 progress-inspect（file / events / node / git-log）抽查，并引用你看到的原文。

{dossier}

----（档案结束）----

一、先回答三个问题，写进 summary
1. 过去 24 小时，项目离完成标准近了多少（按关闭的条目算，不按交付数）？
2. 现在最长、最不确定的那段链是哪段？在等什么？
3. 有哪件正在花大量时间的事，即使做成了，也不会让任何完成标准条目更近？

二、逐项判三份清单
A 科研项目管理（pm-1 至 pm-6）、B 软硬件协同（cd-1 至 cd-5）、C IC 自动化（ic-1 至 ic-4），条目见档案第 3 段。每项给 PASS、FAIL 或 UNSURE，附证据（档案里的数，或"路径:行"）。没把握判 UNSURE，不要为凑结论判 PASS。

三、提出项目级调整（最多 6 条，按对关键路径的影响排序）
- 只提会改变速度或方向的事：改里程碑结构（拆分、去掉多余依赖、合并）、在线之间挪投入、停掉或降级某个分支、给空着的线补不依赖别人的活、补校准或基线。不提节点级的小事（那是节点级巡检的事）。
- 每条都要写：改什么、为什么（引用档案的数）、预期效果（能缩短哪条链、关掉哪些条目）、代价、落实标准、几小时后回查。
- 收件人：技术和组织取舍给对应的协调人（core1 管 B 线、EVB、NEWP；core2 管 F 线、FE、SR）；只有目标、完成标准、预算或算力追加、对外动作、读留出集，才给 human；巡检或看板工具的问题给 tooling。
- 依赖图审计里标出的"疑似多余依赖"和"被图挡住的近完成项"，必须逐个表态：保留（写理由）还是改。
- "机器空 + 线空"的线，必须给出 1–3 个不依赖别人、挂在完成标准条目上的候选活。

四、回查
档案第 5 段的每条待回查调整，给 done、not_done、obsolete 或 cannot_tell，附证据。看落实标准写的事实有没有出现，不要因为对方回了消息就判 done。

五、边界
不谈留出集，也不猜它的内容。不把"还没做完"当问题。不重复上一轮已发、还没到回查时间的调整。

只输出符合 schema 的 JSON。"""
FAMILY_NOTE = "另一家模型也在独立评审同一份档案；你的判断会和它比对，不要迎合常见结论，有分歧就说清楚依据。"


def schema_for(project: Dict[str, Any]) -> Dict[str, Any]:
    """SCHEMA with `to` restricted to this project's cores (the spec enum names core1/core2)."""
    cores = ["coordinator:%s" % c.get("id") for c in project.get("cores") or [] if c.get("id")]
    sch = json.loads(json.dumps(SCHEMA))
    if cores:
        sch["properties"]["adjustments"]["items"]["properties"]["to"]["enum"] = cores + ["human", "tooling"]
    return sch


# ---------------------------------------------------------------- small helpers

class HoldoutHit(Exception):
    """The dossier text matched the holdout denylist. Carries only the number of matching lines."""

    def __init__(self, count: int):
        super().__init__("holdout hit: %d line(s)" % count)
        self.count = count


def cfg(project: Dict[str, Any]) -> Dict[str, Any]:
    c = ((project.get("defaults") or {}).get("project_review"))
    return c if isinstance(c, dict) else {}


def _t(at: Optional[str]) -> Optional[_dt.datetime]:
    return core.parse_iso(at)


def _hours_between(a: Optional[_dt.datetime], b: Optional[_dt.datetime]) -> Optional[float]:
    return None if a is None or b is None else (b - a).total_seconds() / 3600.0


def _h(x: Optional[float]) -> str:
    return "?" if x is None else ("%.1f" % x)


def _ratio(n: float, d: float) -> str:
    return "%d%%（%s/%s）" % (round(100.0 * n / d), _num(n), _num(d)) if d else "n/a（分母 0）"


def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else "%.1f" % x


def _words(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _norm(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", (text or "").lower())


def _id_in(text: str, ident: str) -> bool:
    return re.search(r"(?<![A-Za-z0-9])%s(?![0-9])" % re.escape(ident), text or "", re.I) is not None


def _tokens(path: str) -> List[str]:
    return [t for t in re.split(r"[^a-z0-9]+", path.lower()) if t]


def term_hit(path: str, terms: List[str]) -> bool:
    """A workload name is a whole token of the path; names of 4+ letters also match as a token prefix
    ("rhodo" in rhodopsin_ref.py), short ones (md, cg, vit, dit) only exactly, so "edit" never counts as "dit"."""
    toks = _tokens(path)
    for term in terms:
        term = term.lower()
        for tk in toks:
            if tk == term or (len(term) >= 4 and tk.startswith(term)):
                return True
            # camel/glued forms such as "cg64" or "md_peptide": split letters from digits
            if re.fullmatch(r"%s\d+" % re.escape(term), tk):
                return True
    return False


# ---------------------------------------------------------------- criteria and stages

def split_done_when(mid: str, done_when: str) -> List[Dict[str, Any]]:
    parts = [p.strip() for p in re.split(r"[。；;]", done_when or "")]
    out = []
    for p in parts:
        if len(p) >= 2:
            out.append({"id": "%s.c%d" % (mid, len(out) + 1), "text": p, "status": "unknown", "evidence": "",
                        "mechanical": True})
    return out


def criteria_of(m: Dict[str, Any], prior: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    prior = prior or {}
    raw = m.get("criteria")
    if isinstance(raw, list) and raw:
        out = []
        for c in raw:
            if not isinstance(c, dict) or not c.get("id"):
                continue
            row = {"id": str(c["id"]), "text": str(c.get("text") or ""),
                   "status": c.get("status") if c.get("status") in core.CRITERION_STATUS else "unknown",
                   "evidence": str(c.get("evidence") or ""), "mechanical": False}
            if row["status"] == "met":
                row["closed_at"] = c.get("closed_at") or (prior.get(row["id"]) or {}).get("closed_at")
            out.append(row)
        return out
    return split_done_when(str(m.get("id")), str(m.get("done_when") or ""))


def update_criteria_ledger(prior: Optional[Dict[str, Any]], crits: List[Dict[str, Any]], now: _dt.datetime) -> Dict[str, Any]:
    """criterion id -> {status, closed_at}: closing time is the first round that saw it met."""
    prior = prior or {}
    out: Dict[str, Any] = {}
    for c in crits:
        if c["status"] == "met":
            old = prior.get(c["id"]) or {}
            out[c["id"]] = {"status": "met", "closed_at": c.get("closed_at") or old.get("closed_at") or snap.iso(now)}
    return out


def stage_map(project: Dict[str, Any]) -> Tuple[Dict[str, str], bool]:
    """milestone id -> stage. When no milestone declares one: every milestone lands in one bucket and the flag is False."""
    ms = [m for m in project.get("milestones") or [] if isinstance(m, dict) and m.get("id")]
    declared = any(m.get("stage") for m in ms)
    return {m["id"]: (m.get("stage") or ("S?" if declared else "全部")) for m in ms}, declared


# ---------------------------------------------------------------- node intervals (dispatch -> deliver)

def node_intervals(events: List[Dict[str, Any]], state: Dict[str, Any], now: _dt.datetime) -> List[Dict[str, Any]]:
    """One row per dispatch: start, end (a running node ends at `now`), plus the node's kind/title/milestones."""
    open_: Dict[Tuple[str, str], _dt.datetime] = {}
    rows: List[Dict[str, Any]] = []

    def meta(lid: str, nid: str) -> Dict[str, Any]:
        n = ((state.get("lines") or {}).get(lid) or {}).get("nodes", {}).get(nid) or {}
        return n

    def close(key, at):
        s = open_.pop(key, None)
        if s is not None and at >= s:
            rows.append({"line": key[0], "node": key[1], "start": s, "end": at})

    for ev in sorted(events, key=lambda e: e.get("at") or ""):
        at = _t(ev.get("at"))
        lid, nid, kind = ev.get("line"), ev.get("node"), ev.get("kind")
        if at is None or at > now or not lid or not nid:
            continue
        key = (lid, nid)
        if kind == "node_dispatch" or (kind == "auto_dispatch" and ev.get("phase") != "started"):
            close(key, at)
            open_[key] = at
        elif kind in ("node_deliver", "node_block", "node_cancel", "node_supersede"):
            close(key, at)
        elif kind == "worker_release" and ev.get("released"):
            close(key, at)
    for key, s in list(open_.items()):
        n = meta(*key)
        end = now if n.get("status") == "running" else (_t(n.get("updated_at")) or s)
        end = min(max(end, s), now)
        rows.append({"line": key[0], "node": key[1], "start": s, "end": end})
    out = []
    for r in rows:
        n = meta(r["line"], r["node"])
        kind = n.get("kind") or core.infer_node_kind(r["node"])
        if kind == "decide":
            continue
        title = str(n.get("title") or r["node"])
        if REPAIR_RE.search("%s %s" % (r["node"], title)):
            cat = "repair"
        elif kind in ("review", "check"):
            cat = "review"
        else:
            cat = "new"
        out.append(dict(r, kind=kind, title=title, cat=cat, milestones=list(n.get("milestones") or [])))
    return out


def _clip_hours(r: Dict[str, Any], lo: _dt.datetime, hi: _dt.datetime) -> float:
    s, e = max(r["start"], lo), min(r["end"], hi)
    return max(0.0, (e - s).total_seconds() / 3600.0)


# ---------------------------------------------------------------- dossier sections

def _critical_path(mls: List[Dict[str, Any]], confirmed: set, est: Dict[str, float]) -> List[str]:
    by = {m["id"]: m for m in mls}
    memo: Dict[str, Tuple[float, List[str]]] = {}

    def best(mid: str, seen: Tuple[str, ...]) -> Tuple[float, List[str]]:
        if mid in memo:
            return memo[mid]
        if mid in seen:
            return 0.0, []
        top: Tuple[float, List[str]] = (0.0, [])
        for a in by[mid].get("after") or []:
            if a in by and a not in confirmed:
                cand = best(a, seen + (mid,))
                if cand[0] > top[0] or (cand[0] == top[0] and len(cand[1]) > len(top[1])):
                    top = cand
        res = (top[0] + est[mid], top[1] + [mid])
        memo[mid] = res
        return res

    cands = [best(m["id"], ()) for m in mls if m["id"] not in confirmed]
    if not cands:
        return []
    cands.sort(key=lambda c: (-c[0], -len(c[1]), c[1]))
    return cands[0][1]


def _idle_attribution(project: Dict[str, Any], state: Dict[str, Any], zero_times: Dict[str, List[float]]) -> Dict[str, Any]:
    mls = [m for m in project.get("milestones") or [] if isinstance(m, dict) and m.get("id")]
    conf_at: Dict[str, float] = {}
    for mid, ms in (state.get("milestones") or {}).items():
        t = _t(ms.get("confirmed_at")) if ms.get("status") == "confirmed" else None
        if t:
            conf_at[mid] = t.timestamp()
    memo: Dict[Tuple[str, frozenset], Tuple[str, List[str]]] = {}
    waits: Dict[str, Dict[str, Any]] = {}
    nowork: Dict[str, Dict[str, Any]] = {}
    step_h = SAMPLE_MIN / 60.0

    def who(lid: str, confirmed: frozenset) -> Tuple[str, List[str]]:
        key = (lid, confirmed)
        if key in memo:
            return memo[key]
        un = [m for m in mls if lid in (m.get("lines") or []) and m["id"] not in confirmed]
        if not un:
            res = ("none", [])
        else:
            front = [m["id"] for m in un if all(a in confirmed for a in m.get("after") or [])]
            if front:
                res = ("nowork", sorted(front))
            else:
                bl = sorted({a for m in un for a in m.get("after") or [] if a not in confirmed})
                ready = [a for a in bl if all(x in confirmed for x in (next((m for m in mls if m["id"] == a), {}).get("after") or []))]
                res = ("wait", ready or bl)
        memo[key] = res
        return res

    for lid, times in zero_times.items():
        for t in times:
            confirmed = frozenset(mid for mid, c in conf_at.items() if c <= t)
            kind, ids = who(lid, confirmed)
            if kind == "wait":
                for mid in ids:
                    w = waits.setdefault(mid, {"line_hours": 0.0, "lines": set()})
                    w["line_hours"] += step_h / len(ids)
                    w["lines"].add(lid)
            elif kind == "nowork":
                key = "+".join(ids)
                w = nowork.setdefault(key, {"line_hours": 0.0, "lines": set()})
                w["line_hours"] += step_h
                w["lines"].add(lid)
    return {"waits": waits, "nowork": nowork}


def _git_leak(project: Dict[str, Any], c: Dict[str, Any], git: Optional[Callable], since: str) -> List[Dict[str, Any]]:
    terms = c.get("workload_terms") if isinstance(c.get("workload_terms"), list) else DEFAULT_WORKLOAD_TERMS
    lines = c.get("leak_lines") if isinstance(c.get("leak_lines"), list) else DEFAULT_LEAK_LINES
    out = []
    for lid in lines:
        if core.line_of(project, lid) is None:
            continue
        if git is None:
            out.append({"line": lid, "skipped": "未提供 git 读取（config git_paths 未设）"})
            continue
        try:
            files = git(lid, since)
        except Exception as exc:  # noqa: BLE001 - a broken repo must not sink the dossier
            out.append({"line": lid, "skipped": "git 读取失败：%s" % str(exc)[:80]})
            continue
        if files is None:
            out.append({"line": lid, "skipped": "该线未配置 git 路径"})
            continue
        total = sum(int(n) for _p, n in files)
        spec = sum(int(n) for p, n in files if term_hit(p, terms))
        out.append({"line": lid, "total": total, "workload": spec, "files": len(files),
                    "share": (spec / total) if total else None})
    return out


def build_dossier(project: Dict[str, Any], events: List[Dict[str, Any]], live: Optional[Dict[str, Any]],
                  now: _dt.datetime, runner=None, git: Optional[Callable] = None,
                  state: Optional[Dict[str, Any]] = None, sup_state: Optional[Dict[str, Any]] = None,
                  holdout_root: Optional[pathlib.Path] = None, guard: Optional[Any] = None) -> Dict[str, Any]:
    """-> {"text": markdown, "data": dict}. Raises HoldoutHit when the text matches the holdout denylist.
    `git(line_id, since_iso)` -> [(path, changed_lines)] | None (injected; `runner` is only for default_git)."""
    state_in = state or {}
    c = cfg(project)
    now_s = snap.iso(now)
    events = [e for e in events if (e.get("at") or "") <= now_s]
    st = core.fold(project, events, at=now_s)
    mls = [m for m in project.get("milestones") or [] if isinstance(m, dict) and m.get("id")]
    by_m = {m["id"]: m for m in mls}
    smap, declared = stage_map(project)
    stages = sorted(set(smap.values()))
    ms_state = st.get("milestones") or {}
    confirmed = {mid for mid, s in ms_state.items() if s.get("status") == "confirmed"}
    prior = state_in.get("criteria_ledger") or {}
    since24 = now - _dt.timedelta(hours=24)
    flags: List[Dict[str, Any]] = []
    headline: List[Dict[str, Any]] = []
    data: Dict[str, Any] = {"generated_at": now_s, "stage_declared": declared}

    def flag(level: str, key: str, text: str, to: str = "coordinator", ids: Optional[List[str]] = None) -> None:
        flags.append({"level": level, "key": key, "text": text, "to": to})
        headline.append({"text": text, "ids": ids or []})

    # ---- 1. goal and criteria
    crits: Dict[str, List[Dict[str, Any]]] = {m["id"]: criteria_of(m, prior) for m in mls}
    # a confirmed milestone without coordinator-written criteria: its mechanically split conditions count as met
    # (the coordinator's confirm is the check of done_when), closed at the confirm time
    for mid in confirmed:
        at_c = (ms_state.get(mid) or {}).get("confirmed_at")
        for x in crits.get(mid) or []:
            if x.get("mechanical"):
                x.update(status="met", evidence="里程碑 %s 已确认（%s）" % (mid, at_c or "时间不明"), closed_at=at_c)
    all_crits = [(m["id"], x) for m in mls for x in crits[m["id"]]]
    new_ledger = update_criteria_ledger(prior, [x for _m, x in all_crits], now)
    for _m, x in all_crits:
        if x["status"] == "met" and not x.get("closed_at"):
            x["closed_at"] = (new_ledger.get(x["id"]) or {}).get("closed_at")
    fg = project.get("final_goal") or {}
    dw = fg.get("done_when")
    L1: List[str] = ["## 1 目标与完成标准", "", "最终目标：%s" % _words(str(fg.get("text") or ""))]
    if isinstance(dw, list) and dw:
        L1 += ["完成标准（final_goal.done_when，全部满足才算完成）："] + ["- %s" % _words(str(x)) for x in dw] + [""]
    else:
        L1 += ["完成标准（final_goal.done_when）：%s" % _words(str(dw or "（未写）")), ""]
    if not declared:
        L1.append("⚠ 阶段未声明：没有任何里程碑带 stage 字段，下面只分一个桶「%s」；请协调人给里程碑补 stage（S1/S2/S3）。" % "全部")
    mech = sum(1 for _m, x in all_crits if x.get("mechanical"))
    if mech:
        L1.append("说明：%d/%d 条完成标准条目是按句号/分号从 done_when 机械拆出的：已确认里程碑的记 met（以确认为准），"
                  "其余记 unknown；协调人在 project.json 里程碑写 criteria 之后才有逐条的 met/unmet。" % (mech, len(all_crits)))
    L1.append("进度只按条目算，不按节点交付数算。")
    L1.append("")
    L1.append("阶段条目进度（总数 = 该阶段所有里程碑的条目；窗口：关闭数看最近 24 小时，截至 %s）：" % now_s)
    stage_rows = []
    last_closed_any: Optional[_dt.datetime] = None
    for sg in stages:
        ids = [mid for mid in smap if smap[mid] == sg]
        cs = [x for mid in ids for x in crits[mid]]
        met = [x for x in cs if x["status"] == "met"]
        unm = sum(1 for x in cs if x["status"] == "unmet")
        unk = sum(1 for x in cs if x["status"] == "unknown")
        closed = [x for x in met if _t(x.get("closed_at")) and _t(x["closed_at"]) > since24]
        times = [_t(x.get("closed_at")) for x in met if _t(x.get("closed_at"))]
        last = max(times) if times else None
        if last and (last_closed_any is None or last > last_closed_any):
            last_closed_any = last
        conf = sum(1 for mid in ids if mid in confirmed)
        row = {"stage": sg, "total": len(cs), "met": len(met), "unmet": unm, "unknown": unk,
               "closed_24h": len(closed), "milestones": len(ids), "milestones_confirmed": conf,
               "hours_since_last_closed": _hours_between(last, now)}
        stage_rows.append(row)
        L1.append("- %s：条目 %d 条，已满足 %d，未满足 %d，未知 %d；最近 24h 关闭 %d；距上次关闭条目 %s；里程碑已确认 %d/%d" % (
            sg, row["total"], row["met"], unm, unk, row["closed_24h"],
            (_h(row["hours_since_last_closed"]) + " 小时") if last else "n/a（没有可查的关闭时间）", conf, len(ids)))
    data["stages"] = stage_rows
    tot_met = sum(r["met"] for r in stage_rows)
    tot_all = sum(r["total"] for r in stage_rows)
    closed_24 = sum(r["closed_24h"] for r in stage_rows)
    headline.append({"text": "完成标准条目 %d/%d 已满足，最近 24h 关闭 %d（分母：全部里程碑条目；截至 %s）" % (tot_met, tot_all, closed_24, now_s),
                     "ids": []})
    L1.append("")
    L1.append("里程碑与条目（id [状态] 阶段｜前置｜线｜条目）：")
    for m in mls:
        cs = crits[m["id"]]
        L1.append("- %s [%s] %s｜%s｜前置:%s｜线:%s｜条目 %d 条（met %d）" % (
            m["id"], (ms_state.get(m["id"]) or {}).get("status"), smap.get(m["id"]), _words(str(m.get("title")))[:60],
            ",".join(m.get("after") or []) or "-", ",".join(m.get("lines") or []) or "-",
            len(cs), sum(1 for x in cs if x["status"] == "met")))
        for x in cs:
            L1.append("    · %s [%s] %s%s" % (x["id"], x["status"], _words(x["text"])[:200],
                                              ("（证据：%s）" % _words(x["evidence"])[:100]) if x.get("evidence") else ""))

    # ---- 2. speed and critical path
    est = {m["id"]: float(m["est_hours"]) if isinstance(m.get("est_hours"), (int, float)) and not isinstance(m.get("est_hours"), bool)
           else float(c.get("default_est_h") or DEFAULT_EST_H) for m in mls}
    declared_est = sum(1 for m in mls if isinstance(m.get("est_hours"), (int, float)))
    last_conf = max([_t(s.get("confirmed_at")) for s in ms_state.values() if s.get("confirmed_at")] or [None],
                    key=lambda t: t.timestamp() if t else 0)
    L2 = ["## 2 速度与关键路径", ""]
    since_conf = _hours_between(last_conf, now)
    since_crit = _hours_between(last_closed_any, now)
    L2.append("- 距上次里程碑确认：%s 小时（%s）" % (_h(since_conf), snap.iso(last_conf) if last_conf else "从未确认"))
    L2.append("- 距上次关闭完成标准条目：%s" % ((_h(since_crit) + " 小时") if last_closed_any else "n/a（没有可查的关闭时间）"))
    chain = _critical_path(mls, confirmed, est)
    L2.append("- 关键路径（最长未确认前置链，工期用里程碑 est_hours；%d/%d 个里程碑声明了 est_hours，其余按默认 %s 小时）：" % (
        declared_est, len(mls), _num(float(c.get("default_est_h") or DEFAULT_EST_H))))
    total_est = 0.0
    cp_rows = []
    for mid in chain:
        cs = crits[mid]
        gap = sum(1 for x in cs if x["status"] != "met")
        total_est += est[mid]
        unconf_after = [a for a in by_m[mid].get("after") or [] if a not in confirmed]
        cp_rows.append({"id": mid, "status": (ms_state.get(mid) or {}).get("status"), "gap": gap, "total": len(cs), "est_h": est[mid]})
        L2.append("    %s [%s] 条目缺口 %d/%d｜估计 %s h｜未确认前置:%s" % (
            mid, (ms_state.get(mid) or {}).get("status"), gap, len(cs), _num(est[mid]), ",".join(unconf_after) or "-"))
    L2.append("    合计估计 %s 小时" % _num(total_est) if chain else "    （没有未确认的里程碑）")
    data["critical_path"] = {"chain": cp_rows, "est_total_h": total_est, "hours_since_confirm": since_conf,
                             "hours_since_criterion": since_crit}
    if chain:
        headline.append({"text": "关键路径 %s，合计估计 %s 小时" % (" → ".join(chain), _num(total_est)), "ids": chain})

    # ---- 3. checklists
    L3 = ["## 3 三份检查清单（逐项判 PASS / FAIL / UNSURE，写证据）", ""]
    for key in ("pm", "codesign", "ic"):
        L3.append("%s：" % CHECK_TITLES[key])
        for cid, txt in CHECKLIST_TEXT[key]:
            L3.append("- %s %s" % (cid, txt))
        L3.append("")

    # ---- 4. investment, parallel, graph audit, direction, decision delay
    L4 = ["## 4 投入、并行、依赖图、方向、决策延迟", ""]
    win_h = float(c.get("invest_window_h") or INVEST_WINDOW_H_DEFAULT)
    lo_i = now - _dt.timedelta(hours=win_h)
    ivs = node_intervals(events, st, now)
    L4.append("### 4.1 投入分配（窗口：最近 %s 小时内的节点耗时，派出到交付，在跑的算到现在；decide 节点不算）" % _num(win_h))
    hrs_by_line: Dict[str, float] = {}
    cat_by_line: Dict[str, Dict[str, float]] = {}
    by_ms: Dict[str, float] = {}
    by_stage: Dict[str, float] = {}
    cat_tot = {"new": 0.0, "repair": 0.0, "review": 0.0}
    unatt = 0.0
    for r in ivs:
        h = _clip_hours(r, lo_i, now)
        if h <= 0:
            continue
        hrs_by_line[r["line"]] = hrs_by_line.get(r["line"], 0.0) + h
        cl = cat_by_line.setdefault(r["line"], {"new": 0.0, "repair": 0.0, "review": 0.0})
        cl[r["cat"]] += h
        cat_tot[r["cat"]] += h
        ms_ids = [m for m in r["milestones"] if m in by_m]
        if not ms_ids:
            unatt += h
            by_ms["（未挂里程碑）"] = by_ms.get("（未挂里程碑）", 0.0) + h
            by_stage["（未挂）"] = by_stage.get("（未挂）", 0.0) + h
        for mid in ms_ids:
            by_ms[mid] = by_ms.get(mid, 0.0) + h / len(ms_ids)
            by_stage[smap[mid]] = by_stage.get(smap[mid], 0.0) + h / len(ms_ids)
    total_h = sum(hrs_by_line.values())
    L4.append("- 总耗时 %s 节点小时；新工作 %s、返修 %s、审查 %s" % (
        _num(round(total_h, 1)), _num(round(cat_tot["new"], 1)), _num(round(cat_tot["repair"], 1)), _num(round(cat_tot["review"], 1))))
    L4.append("- 按线：" + "；".join("%s %s h" % (k, _num(round(v, 1))) for k, v in sorted(hrs_by_line.items())))
    L4.append("- 按阶段：" + "；".join("%s %s h" % (k, _num(round(v, 1))) for k, v in sorted(by_stage.items())))
    L4.append("- 按里程碑：" + "；".join("%s %s h" % (k, _num(round(v, 1))) for k, v in sorted(by_ms.items(), key=lambda kv: -kv[1])[:12]))
    data["invest"] = {"window_h": win_h, "total_h": total_h, "cat": cat_tot, "by_line": hrs_by_line,
                      "by_stage": by_stage, "by_milestone": by_ms, "unattached_h": unatt}

    # Lines the project no longer declares (e.g. records imported from an earlier setup, 11-06 FULLREQ/ACC) are
    # shown but never flagged: nobody can act on their overhead.
    declared = {ln["id"] for ln in project.get("lines") or [] if isinstance(ln, dict) and ln.get("id")}

    def overhead(label: str, n_rep: float, n_rev: float, tot: float, ids: List[str], flaggable: bool = True) -> None:
        ov = (n_rep + n_rev) / tot if tot else 0.0
        mark = "" if flaggable else "（不在项目线里，不计旗标）"
        if not flaggable:
            pass
        elif tot and ov > OVERHEAD_PROBLEM:
            mark = " problem"
            flag("problem", "overhead:%s" % label, "%s 开销 %s 超过 %d%%" % (label, _ratio(n_rep + n_rev, tot), OVERHEAD_PROBLEM * 100), ids=ids)
        elif tot and ov > OVERHEAD_WARN:
            mark = " ⚠"
            flag("warn", "overhead:%s" % label, "%s 开销 %s 超过 %d%%" % (label, _ratio(n_rep + n_rev, tot), OVERHEAD_WARN * 100), ids=ids)
        L4.append("- 开销（审查+返修）÷总耗时 · %s：%s（审查 %s h，返修 %s h；窗口 %s h）%s" % (
            label, _ratio(n_rep + n_rev, tot), _num(round(n_rev, 1)), _num(round(n_rep, 1)), _num(win_h), mark))

    overhead("全项目", cat_tot["repair"], cat_tot["review"], total_h, [])
    for lid, cl in sorted(cat_by_line.items()):
        overhead(lid, cl["repair"], cl["review"], hrs_by_line[lid], [lid], flaggable=not declared or lid in declared)
    data["overhead"] = {"all": ((cat_tot["repair"] + cat_tot["review"]) / total_h) if total_h else None,
                        "by_line": {k: (v["repair"] + v["review"]) / hrs_by_line[k] for k, v in cat_by_line.items() if hrs_by_line.get(k)},
                        "window_h": win_h}
    # same ratio over the last OVERHEAD_SHORT_H hours, per line (the lead's done_when criteria use 24 h; 11-06).
    # Shown only, no flags: the flags above stay on the long window.
    lo_s = now - _dt.timedelta(hours=OVERHEAD_SHORT_H)
    sh: Dict[str, Dict[str, float]] = {}
    for r in ivs:
        h = _clip_hours(r, lo_s, now)
        if h <= 0:
            continue
        cl = sh.setdefault(r["line"], {"new": 0.0, "repair": 0.0, "review": 0.0, "total": 0.0})
        cl[r["cat"]] += h
        cl["total"] += h
    s_tot = sum(v["total"] for v in sh.values())
    s_ov = sum(v["repair"] + v["review"] for v in sh.values())
    data["overhead_24h"] = {
        "window_h": OVERHEAD_SHORT_H, "all": (s_ov / s_tot) if s_tot else None, "total_h": s_tot,
        "by_line": {k: {"ratio": ((v["repair"] + v["review"]) / v["total"]) if v["total"] else None,
                        "review_h": v["review"], "repair_h": v["repair"], "total_h": v["total"]} for k, v in sorted(sh.items())}}
    L4.append("- 开销（审查+返修）÷总耗时 · 窗口 %s h · 全项目 %s；按线：%s" % (
        _num(OVERHEAD_SHORT_H), _ratio(s_ov, s_tot) if s_tot else "无耗时",
        "；".join("%s %s（审查 %s h，返修 %s h，共 %s h）" % (k, _ratio(v["repair"] + v["review"], v["total"]), _num(round(v["review"], 1)),
                                                     _num(round(v["repair"], 1)), _num(round(v["total"], 1))) for k, v in sorted(sh.items())) or "无"))
    if total_h:
        mark = ""
        if unatt / total_h > UNATTACHED_WARN:
            mark = " ⚠"
            flag("warn", "unattached", "未挂到任何里程碑的节点耗时占 %s，超过 %d%%（可能是没意义的工作）" % (
                _ratio(unatt, total_h), UNATTACHED_WARN * 100))
        L4.append("- 未挂任何里程碑的节点耗时：%s%s" % (_ratio(unatt, total_h), mark))
    fast = c.get("fast_milestones")
    if isinstance(fast, list) and fast:
        s1 = c.get("fast_stage") or ("S1" if declared else "全部")
        s1_tot = by_stage.get(s1, 0.0)
        fast_h = sum(by_ms.get(mid, 0.0) for mid in fast if smap.get(mid) == s1)
        mark = ""
        if s1_tot and fast_h / s1_tot < FAST_SHARE_MIN:
            mark = " ⚠"
            flag("warn", "fast_share", "%s 里快速方法（%s）的投入只占 %s，不到 %d%%" % (
                s1, ",".join(fast), _ratio(fast_h, s1_tot), FAST_SHARE_MIN * 100), ids=list(fast))
        L4.append("- 快速方法（%s）占 %s 总投入：%s%s" % (",".join(fast), s1, _ratio(fast_h, s1_tot), mark))
    else:
        L4.append("- 快速方法占比：跳过（project.defaults.project_review.fast_milestones 未配置）")
    L4.append("")

    # 4.2 parallel efficiency
    L4.append("### 4.2 并行效率（窗口：最近 24 小时，每 %d 分钟采样一次）" % SAMPLE_MIN)
    t0 = since24.timestamp()
    nsamp = int(CONC_WINDOW_H * 60 / SAMPLE_MIN)
    samples = [t0 + k * SAMPLE_MIN * 60 for k in range(nsamp + 1) if t0 + k * SAMPLE_MIN * 60 <= now.timestamp()]
    # every declared line counts, paused (line_close) ones too: an idle paused line next to an idle machine is the finding
    open_lines = [ln["id"] for ln in project.get("lines") or [] if isinstance(ln, dict) and ln.get("id")]
    paused = {lid for lid in open_lines if not ((st.get("lines") or {}).get(lid) or {}).get("open", True)}
    iv_by_line: Dict[str, List[Tuple[float, float]]] = {}
    for r in ivs:
        iv_by_line.setdefault(r["line"], []).append((r["start"].timestamp(), r["end"].timestamp()))
    conc: Dict[str, List[int]] = {}
    for lid in open_lines:
        conc[lid] = [sum(1 for s, e in iv_by_line.get(lid, []) if s <= t < e) for t in samples]
    totals = [sum(conc[l][i] for l in open_lines) for i in range(len(samples))]
    par_rows: Dict[str, Any] = {}
    n = len(samples)

    def par(label: str, series: List[int]) -> Dict[str, Any]:
        zero = sum(1 for v in series if v == 0)
        return {"avg": (sum(series) / n) if n else None, "peak": max(series) if series else 0,
                "zero_share": (zero / n) if n else None, "zero": zero, "n": n}

    for lid in open_lines:
        par_rows[lid] = par(lid, conc[lid])
    par_rows["全项目"] = par("全项目", totals)
    mems = {h: v.get("mem_avail_gb") for h, v in ((live or {}).get("resources") or {}).items() if isinstance(v, dict)}
    mem_vals = [v for v in mems.values() if isinstance(v, (int, float))]
    mem_max = max(mem_vals) if mem_vals else None
    for label, r in par_rows.items():
        L4.append("- %s%s：平均并发 %s，峰值 %d，零并发时间占比 %s" % (
            label, "（线现在是暂停状态 line_close）" if label in paused else "",
            "n/a" if r["avg"] is None else "%.2f" % r["avg"], r["peak"], _ratio(r["zero"], r["n"])))
    data["parallel"] = par_rows
    L4.append("- 机器余量（live.resources）：" + ("；".join("%s 可用内存 %s GB" % (h, "n/a" if v is None else v) for h, v in sorted(mems.items())) or "live 里没有 resources")
              + "；负载：live 里没有该字段")
    for lid in open_lines:
        r = par_rows[lid]
        if r["zero_share"] is not None and r["zero_share"] > ZERO_CONC_WARN:
            core_id = (core.line_of(project, lid) or {}).get("core")
            if mem_max is not None and mem_max > FREE_MEM_GB:
                L4.append("  ⚠ 机器空 + 线空：%s 零并发 %s，机器可用内存最高 %s GB（>%s GB）；收件人：协调人 %s（router 没法自己造活）" % (
                    lid, _ratio(r["zero"], r["n"]), mem_max, _num(FREE_MEM_GB), core_id))
                flag("warn", "idle_machine_line:%s" % lid, "机器空+线空：%s 零并发 %s，可用内存 %s GB" % (
                    lid, _ratio(r["zero"], r["n"]), mem_max), to="coordinator:%s" % core_id, ids=[lid])
            else:
                L4.append("  %s 零并发 %s 超过 %d%%，但机器余量%s，不判机器空" % (
                    lid, _ratio(r["zero"], r["n"]), ZERO_CONC_WARN * 100, "未知" if mem_max is None else "只有 %s GB" % mem_max))
    zero_times = {lid: [t for t, v in zip(samples, conc[lid]) if v == 0] for lid in open_lines}
    attr = _idle_attribution(project, st, zero_times)
    L4.append("- 空闲归因（线零并发时它在等哪个里程碑；单位：线·小时，多个等待对象平分）：")
    waits = sorted(attr["waits"].items(), key=lambda kv: (-kv[1]["line_hours"], kv[0]))
    for mid, w in waits[:8]:
        L4.append("    等 %s：%s 线·小时，涉及线 %s" % (mid, _num(round(w["line_hours"], 1)), ",".join(sorted(w["lines"]))))
    for key, w in sorted(attr["nowork"].items(), key=lambda kv: -kv[1]["line_hours"])[:5]:
        L4.append("    前沿 %s 有活但没人在跑：%s 线·小时，线 %s" % (key, _num(round(w["line_hours"], 1)), ",".join(sorted(w["lines"]))))
    if not waits and not attr["nowork"]:
        L4.append("    （无）")
    data["idle"] = {"waits": {k: {"line_hours": v["line_hours"], "lines": sorted(v["lines"])} for k, v in attr["waits"].items()},
                    "nowork": {k: {"line_hours": v["line_hours"], "lines": sorted(v["lines"])} for k, v in attr["nowork"].items()}}
    for lid in open_lines:
        r = par_rows[lid]
        headline.append({"text": "%s 零并发 %s（最近 24h，每 5 分钟采样）" % (lid, _ratio(r["zero"], r["n"])), "ids": [lid]})
    L4.append("")

    # 4.3 graph audit
    L4.append("### 4.3 依赖图审计")
    texts = {m["id"]: " ".join([str(m.get("title") or ""), str(m.get("done_when") or "")] + [x["text"] for x in crits[m["id"]]]) for m in mls}
    redundant = []
    for m in mls:
        if m["id"] in confirmed:
            continue
        mt = texts[m["id"]]
        if not IFACE_RE.search(mt) or SPEED_ACC_RE.search(mt):
            continue
        for p in m.get("after") or []:
            if p not in texts or not SPEED_ACC_RE.search(texts[p]) or _id_in(mt, p):
                continue
            redundant.append({"milestone": m["id"], "prerequisite": p})
            L4.append("- ⚠ 疑似多余依赖：%s 的前置 %s 是速度/精度类（%s），而 %s 的完成标准和条目只涉及接口/流程，没提到 %s 交付的东西——交模型判断保留还是改" % (
                m["id"], p, _words(str(by_m[p].get("title")))[:30], m["id"], p))
            flag("warn", "redundant_dep:%s<-%s" % (m["id"], p), "%s 疑似多余依赖 %s" % (m["id"], p), ids=[m["id"], p])
    if not redundant:
        L4.append("- 疑似多余依赖：无命中（规则：前置是速度/精度类、本节点只涉及接口/流程、条目没提到前置的东西）")
    near_lo = now - _dt.timedelta(hours=NEAR_DONE_H)
    blocked_near = []
    for m in mls:
        mid = m["id"]
        unmet_after = [a for a in m.get("after") or [] if a not in confirmed]
        if mid in confirmed or not unmet_after:
            continue
        kw = [mid] + [str(k) for k in (m.get("keywords") or [])]
        rel = set()
        for lid, ls in (st.get("lines") or {}).items():
            for nid, nd in (ls.get("nodes") or {}).items():
                if mid in (nd.get("milestones") or []) or any(_id_in("%s %s" % (nid, nd.get("title") or ""), k) for k in kw):
                    rel.add((lid, nid))
        passed = []
        for ev in events:
            at = _t(ev.get("at"))
            if at and near_lo < at <= now and ev.get("outcome") == "PASS" and ev.get("kind") in ("node_deliver", "node_review") \
                    and (ev.get("line"), ev.get("node")) in rel:
                passed.append("%s/%s" % (ev.get("line"), ev.get("node")))
        if passed:
            passed = sorted(set(passed))
            blocked_near.append({"milestone": mid, "unmet_after": unmet_after, "pass_nodes": passed})
            L4.append("- ⚠ 被图挡住的近完成项：%s 相关节点最近 %d 小时有 %d 个 PASS 交付（%s），但前置 %s 未确认，不能申报" % (
                mid, NEAR_DONE_H, len(passed), "、".join(passed[:4]), ",".join(unmet_after)))
            flag("warn", "blocked_near_done:%s" % mid, "%s 有 %d 个 PASS 交付（最近 %d 小时）却被前置 %s 挡住" % (
                mid, len(passed), NEAR_DONE_H, ",".join(unmet_after)), ids=[mid] + unmet_after)
    if not blocked_near:
        L4.append("- 被图挡住的近完成项：无命中（最近 %d 小时内相关节点没有 PASS 交付，或前置已满足）" % NEAR_DONE_H)
    top3 = waits[:3]
    L4.append("- 下游空等前 3（让最多线时空等的里程碑，见 4.2）：" + ("；".join(
        "%s（%s 线·小时，%d 条线）" % (mid, _num(round(w["line_hours"], 1)), len(w["lines"])) for mid, w in top3) or "无"))
    data["graph_audit"] = {"redundant": redundant, "blocked_near_done": blocked_near,
                           "top_waits": [{"milestone": mid, "line_hours": w["line_hours"], "lines": sorted(w["lines"])} for mid, w in top3]}
    L4.append("")

    # 4.4 direction
    L4.append("### 4.4 方向风险（脚本只给证据，判断交模型）")
    leak = _git_leak(project, c, git, snap.iso(since24))
    terms = c.get("workload_terms") if isinstance(c.get("workload_terms"), list) else DEFAULT_WORKLOAD_TERMS
    L4.append("- 通用性泄漏：最近 24 小时开发分支改动中，路径含负载名（%s）的行数占比（分母：该线 24h 改动总行数）：" % "、".join(terms))
    for r in leak:
        if r.get("skipped"):
            L4.append("    %s：跳过（%s）" % (r["line"], r["skipped"]))
            continue
        mark = ""
        if r["share"] is not None and r["share"] > LEAK_WARN:
            mark = " ⚠（会影响 B10 和 D4）"
            flag("warn", "leak:%s" % r["line"], "%s 负载专用代码占比 %s，超过 %d%%" % (
                r["line"], _ratio(r["workload"], r["total"]), LEAK_WARN * 100), ids=[r["line"]])
        L4.append("    %s：%s，涉及 %d 个文件%s" % (r["line"], _ratio(r["workload"], r["total"]), r["files"], mark))
    data["leak"] = leak
    L4.append("- PARTIAL 堆积（每条线最近 24 小时 PARTIAL ÷ 全部交付；同期没有关闭任何条目才标 ⚠；交付少于 %d 个不判）：" % PARTIAL_MIN_DELIVERIES)
    partial = {}
    for lid in open_lines:
        dl = [e for e in events if e.get("line") == lid and e.get("kind") == "node_deliver" and e.get("source") != "import"
              and _t(e.get("at")) and since24 < _t(e["at"]) <= now]
        pn = sum(1 for e in dl if e.get("outcome") == "PARTIAL")
        partial[lid] = {"partial": pn, "total": len(dl)}
        mark = ""
        if len(dl) >= PARTIAL_MIN_DELIVERIES and pn / len(dl) > PARTIAL_WARN and closed_24 == 0:
            mark = " ⚠（同期关闭条目 0）"
            flag("warn", "partial:%s" % lid, "%s 最近 24h PARTIAL %s 且没关任何条目" % (lid, _ratio(pn, len(dl))), ids=[lid])
        L4.append("    %s：%s%s" % (lid, _ratio(pn, len(dl)), mark))
    data["partial"] = partial
    tm = c.get("truth_milestones") if isinstance(c.get("truth_milestones"), list) else DEFAULT_TRUTH_MILESTONES
    L4.append("- 真值来源（%s 的参照字段原文；有没有 RTL/实测/外部工具锚点由模型判断）：" % ",".join(tm))
    for mid in tm:
        if mid not in by_m:
            L4.append("    %s：里程碑不存在，跳过" % mid)
            continue
        sents = [s for s in re.split(r"[。；;]", texts[mid]) if TRUTH_RE.search(s)]
        L4.append("    %s：%s" % (mid, "｜".join(_words(s)[:160] for s in sents) or "（done_when/条目里没有出现 真值/参照/RTL/实测/锚点 字样）"))
    blob_all = [(str(e.get("summary") or ""), e.get("at") or "") for e in events]
    proj_blob = " ".join(texts.values())
    L4.append("- 外部基线（在事件摘要和里程碑文字里出现的次数；事件窗口：全部 %d 条 / 最近 24h）：" % len(events))
    ext = {}
    for name in EXT_BASELINES:
        rx = re.compile(re.escape(name), re.I)
        n_all = sum(1 for s, _a in blob_all if rx.search(s))
        n_24 = sum(1 for s, a in blob_all if rx.search(s) and a > snap.iso(since24))
        n_ms = len(rx.findall(proj_blob))
        ext[name] = {"events_all": n_all, "events_24h": n_24, "milestone_text": n_ms}
        L4.append("    %s：事件 %d（24h %d），里程碑文字 %d" % (name, n_all, n_24, n_ms))
    data["external_baselines"] = ext
    L4.append("")

    # 4.5 decision delay
    L4.append("### 4.5 决策延迟（超过 %s 小时标 ⚠）" % _num(DECISION_DELAY_H))
    delays: List[Dict[str, Any]] = []
    for lid, ls in (st.get("lines") or {}).items():
        for nid, nd in (ls.get("nodes") or {}).items():
            if nd.get("kind") != "decide":
                continue
            s = _t(nd.get("planned_at"))
            e = _t(nd.get("delivered_at")) if nd.get("delivered_at") else None
            if s is None or (e is not None and e < near_lo - _dt.timedelta(hours=24)):
                continue
            delays.append({"what": "decide 节点 %s/%s" % (lid, nid), "hours": _hours_between(s, e or now), "open": e is None})
    for e_ in (sup_state or {}).get("followups", {}).values() if sup_state else []:
        if e_.get("to") == "coordinator" and _t(e_.get("sent_at")):
            end = _t(e_.get("closed_at"))
            delays.append({"what": "巡检建议→协调人 %s：%s" % (e_.get("line"), _words(str(e_.get("text")))[:50]),
                           "hours": _hours_between(_t(e_["sent_at"]), end or now), "open": end is None})
    ordered = sorted(events, key=lambda e: e.get("at") or "")
    for i, e_ in enumerate(ordered):
        if "交协调人" in str(e_.get("summary") or "") and _t(e_.get("at")) and _t(e_["at"]) > since24 - _dt.timedelta(hours=24):
            nxt = next((x for x in ordered[i + 1:] if x.get("line") == e_.get("line")
                        and ((x.get("actor") or {}).get("role") == "coordinator" or x.get("kind") in ("decision", "human_reply"))), None)
            end = _t(nxt.get("at")) if nxt else None
            delays.append({"what": "跨线问题 %s：%s" % (e_.get("line"), _words(str(e_.get("summary")))[:50]),
                           "hours": _hours_between(_t(e_["at"]), end or now), "open": end is None})
    for d in sorted(delays, key=lambda x: -(x["hours"] or 0))[:10]:
        late = (d["hours"] or 0) > DECISION_DELAY_H
        L4.append("- %s：%s 小时%s%s" % (d["what"], _h(d["hours"]), "（仍未定论）" if d["open"] else "（已定论）", " ⚠" if late else ""))
        if late and d["open"]:
            flag("warn", "decision_delay", "%s 已等 %s 小时仍未定论" % (d["what"], _h(d["hours"])), ids=[])
    if not delays:
        L4.append("- （窗口内没有 decide 节点、给协调人的建议或跨线问题）")
    data["decision_delay"] = delays
    L4.append("")

    # ---- 5. pending follow-ups and proposals
    L5 = ["## 5 待回查的调整（上一轮发出、到点的请给 verdict；未到点的不要重复提）", ""]
    led = state_in.get("followups") or {}
    due_ids = []
    rows5 = []
    for e in sorted(led.values(), key=lambda x: x.get("created_at") or ""):
        if e.get("closed_at"):
            continue
        is_due = (_t(e.get("check_at")) or now) <= now
        if is_due:
            due_ids.append(e["id"])
        rows5.append("- [%s] 发给 %s 于 %s｜%s｜内容：%s｜落实标准：%s" % (
            e["id"], e.get("to"), e.get("sent_at"), "到点，请给 verdict（第 %d 次回查）" % (e.get("tries", 0) + 1) if is_due else "未到点（%s），不要重复提" % e.get("check_at"),
            _words(e.get("text") or "")[:200], _words(e.get("done_when") or "（未写）")[:200]))
    L5 += rows5 or ["（暂无）"]
    pend = state_in.get("pending") or []
    L5.append("")
    L5.append("待议（只有一家模型提过的调整；这一轮若你也认为该做，就再提一次，再提才会发出）：")
    L5 += ["- [%s] %s → %s：%s" % (p.get("id"), p.get("type"), p.get("to"), _words(p.get("what") or "")[:200]) for p in pend] or ["（暂无）"]
    data["followups_due"] = due_ids
    data["flags"] = flags
    data["headline"] = headline
    data["criteria_ledger"] = new_ledger
    data["verdict_hint"] = "problem" if any(f["level"] == "problem" for f in flags) else ("warn" if flags else "ok")

    head = ["# dse 项目档案（脚本生成，%s）" % now_s, "",
            "所有数由脚本算出，每个数写明分母和时间窗。要点：" + ("；".join(h["text"] for h in headline[:6]) if headline else "无"), ""]
    text = "\n".join(head + L1 + [""] + L2 + [""] + L3 + L4 + L5) + "\n"
    g0 = guard or load_guard(holdout_root)
    g1, exempt = public_guard(g0, project)
    if exempt:
        data["holdout_public_overlap"] = exempt
        text += ("\n⚠ 留出集名单里有 %d 个词也出现在 project.json 的公开声明里（只给个数，不列词）。"
                 "公开声明里的原文照常显示；请人核对这是否意味着开发集和留出集有重叠。\n" % exempt)
    check_holdout(text + json.dumps(data, ensure_ascii=False), holdout_root, g1)
    return {"text": text, "data": data}


# ---------------------------------------------------------------- holdout

def load_guard(root: Optional[pathlib.Path] = None) -> Any:
    return dream.Guard.from_holdout(root)


def _strings(node: Any) -> List[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [x for v in node.values() for x in _strings(v)]
    if isinstance(node, list):
        return [x for v in node for x in _strings(v)]
    return []


def public_guard(guard: Any, project: Dict[str, Any]) -> Tuple[Any, int]:
    """The denylist minus grams that already occur in the project declaration (project.json is read by every agent, so
    repeating its text leaks nothing new). -> (guard, number of exempted denylist entries). The count is reported in the
    dossier so a holdout name that slipped into project.json stays visible; the terms themselves are never shown."""
    hashes = set(getattr(guard, "hashes", None) or [])
    ns = list(getattr(guard, "ns", None) or [])
    if not hashes or not ns:
        return guard, 0
    ex = dream._EX
    seen = set()
    for text in _strings(project):
        t = ex["toks"](text)
        for n in ns:
            for i in range(len(t) - n + 1):
                h = ex["hash_gram"](t[i:i + n])
                if h in hashes:
                    seen.add(h)
    if not seen:
        return guard, 0
    return dream.Guard(hashes=hashes - seen, ns=ns), len(seen)


def check_holdout(text: str, root: Optional[pathlib.Path] = None, guard: Optional[Any] = None) -> Any:
    """Raise HoldoutHit (count of matching lines only, never the matched terms) when `text` hits the denylist."""
    g = guard or load_guard(root)
    if g.hit(text):
        n = sum(1 for ln in text.splitlines() if g.hit(ln))
        raise HoldoutHit(max(1, n))
    return g


# ---------------------------------------------------------------- git (default reader)

def default_git(paths: Dict[str, str], runner) -> Callable[[str, str], Optional[List[Tuple[str, int]]]]:
    """`git log --since --numstat` per configured path -> [(file, added+deleted)]; None when the line has no path."""
    def read(line_id: str, since_iso: str) -> Optional[List[Tuple[str, int]]]:
        path = (paths or {}).get(line_id)
        if not path:
            return None
        rc, out, err = runner(["git", "-C", os.path.expanduser(path), "log", "--since=" + since_iso, "--numstat",
                               "--format=", "--no-merges"], timeout=60)
        if rc != 0:
            raise RuntimeError("git rc=%s" % rc)
        acc: Dict[str, int] = {}
        for ln in out.splitlines():
            parts = ln.split("\t")
            if len(parts) == 3:
                a = int(parts[0]) if parts[0].isdigit() else 0
                d = int(parts[1]) if parts[1].isdigit() else 0
                acc[parts[2]] = acc.get(parts[2], 0) + a + d
        return sorted(acc.items())
    return read


# ---------------------------------------------------------------- report validation and merge

def _str(v: Any, n: int) -> str:
    return v.strip()[:n] if isinstance(v, str) else ""


def clamp_h(v: Any, default: int = 24) -> int:
    try:
        return max(CHECK_H_MIN, min(CHECK_H_MAX, int(v)))
    except (TypeError, ValueError):
        return default


def validate_review(doc: Any, pending_ids: Optional[List[str]] = None) -> Tuple[Optional[Dict[str, Any]], str]:
    """-> (clean doc, "") or (None, why). Bad structure rejects; over-long adjustments are trimmed to 6; verdicts on ids
    that are not due this round are dropped quietly; check_after_h is clamped to [6, 48]."""
    if not isinstance(doc, dict):
        return None, "不是 JSON 对象"
    if doc.get("verdict") not in SUMMARY_OK:
        return None, "verdict 必须是 %s" % "/".join(SUMMARY_OK)
    if not isinstance(doc.get("summary"), str) or not doc["summary"].strip():
        return None, "summary 缺失"
    sp = []
    for r in doc.get("stage_progress") or []:
        if not isinstance(r, dict) or not isinstance(r.get("stage"), str):
            return None, "stage_progress 条目不合法"
        sp.append({"stage": r["stage"], "met": int(r.get("met") or 0), "total": int(r.get("total") or 0),
                   "closed_24h": int(r.get("closed_24h") or 0)})
    cl_in = doc.get("checklists")
    if not isinstance(cl_in, dict):
        return None, "checklists 缺失"
    cl: Dict[str, List[Dict[str, str]]] = {}
    for key in ("pm", "codesign", "ic"):
        rows = cl_in.get(key)
        if not isinstance(rows, list):
            return None, "checklists.%s 不是数组" % key
        cl[key] = []
        for r in rows:
            if not isinstance(r, dict) or r.get("id") not in CHECK_IDS or r.get("result") not in RESULTS \
                    or not isinstance(r.get("evidence"), str):
                return None, "checklists.%s 有不合法条目：%r" % (key, (r or {}).get("id") if isinstance(r, dict) else r)
            cl[key].append({"id": r["id"], "result": r["result"], "evidence": r["evidence"].strip()[:500]})
    adj_in = doc.get("adjustments")
    if not isinstance(adj_in, list):
        return None, "adjustments 不是数组"
    adjs, warns = [], []
    for a in adj_in:
        if not isinstance(a, dict) or a.get("type") not in TYPES:
            return None, "adjustment.type 必须是 %s" % "/".join(TYPES)
        to = a.get("to")
        if not (isinstance(to, str) and (to in ("human", "tooling") or re.fullmatch(r"coordinator:\w+", to))):
            return None, "adjustment.to 不合法：%r" % to
        if not isinstance(a.get("what"), str) or not a["what"].strip():
            return None, "adjustment.what 缺失"
        adjs.append({"type": a["type"], "to": to, "what": a["what"].strip()[:600], "why": _str(a.get("why"), 800),
                     "expected_effect": _str(a.get("expected_effect"), 400), "cost": _str(a.get("cost"), 300),
                     "done_when": _str(a.get("done_when"), 400), "check_after_h": clamp_h(a.get("check_after_h"))})
    if len(adjs) > MAX_ADJ:
        warns.append("adjustments 超过 %d 条，只留前 %d 条" % (MAX_ADJ, MAX_ADJ))
        adjs = adjs[:MAX_ADJ]
    fu = []
    for v in doc.get("followups") or []:
        if not isinstance(v, dict) or v.get("verdict") not in VERDICTS or not isinstance(v.get("id"), str):
            return None, "followups 条目不合法"
        if pending_ids is not None and v["id"] not in pending_ids:
            continue
        fu.append({"id": v["id"], "verdict": v["verdict"], "evidence": _str(v.get("evidence"), 400)})
    out = {"verdict": doc["verdict"], "summary": doc["summary"].strip()[:1500], "stage_progress": sp,
           "checklists": cl, "adjustments": adjs, "followups": fu}
    if warns:
        out["warnings"] = warns
    return out, ""


def is_tier3(adj: Dict[str, Any]) -> bool:
    """FRAMEWORK §5 tier 3: goal / budget / external / holdout, to=human, or a goal draft. Never sent to a coordinator."""
    return adj.get("to") == "human" or adj.get("type") == "goal_draft" or \
        TIER3_RE.search("%s %s" % (adj.get("what") or "", adj.get("done_when") or "")) is not None


_IDS_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{1,5}\d{0,3}(?![A-Za-z0-9])")


def _ids_of(adj: Dict[str, Any], known: Optional[List[str]]) -> set:
    text = "%s %s" % (adj.get("what") or "", adj.get("done_when") or "")
    if known:
        return {k for k in known if _id_in(text, k)}
    return {m for m in _IDS_RE.findall(text) if len(m) >= 2}


def similar(a: Dict[str, Any], b: Dict[str, Any], known: Optional[List[str]] = None) -> bool:
    """Same type and either normalized-text similarity >= 0.55 or same `to` plus a shared milestone/line id."""
    if a.get("type") != b.get("type"):
        return False
    if difflib.SequenceMatcher(None, _norm(a.get("what") or ""), _norm(b.get("what") or "")).ratio() >= SIM_RATIO:
        return True
    return a.get("to") == b.get("to") and bool(_ids_of(a, known) & _ids_of(b, known))


def adj_id(adj: Dict[str, Any]) -> str:
    return sup.fu_id("project", adj["to"], adj["what"])


def merge(rep_a: Optional[Dict[str, Any]], rep_b: Optional[Dict[str, Any]], pending_store: Optional[List[Dict[str, Any]]],
          now: Optional[_dt.datetime] = None, known_ids: Optional[List[str]] = None,
          ledger: Optional[Dict[str, Any]] = None) -> Dict[str, List[Dict[str, Any]]]:
    """Spec §4. -> {"send": [...], "pending": [...new pending store...], "human": [...]}.
    agreed by both -> send; proposed by one -> pending, and sent when either model proposes it again in a later
    round; tier-3 or to=human -> human digest only (agreed or not); anything matching an open ledger entry is dropped
    (already sent, not yet due for re-check)."""
    now = now or core.parse_iso(snap.iso(snap.utcnow()))
    ledger = ledger or {}
    open_led = [e for e in ledger.values() if not e.get("closed_at")]
    items: List[Tuple[str, Dict[str, Any]]] = []
    for who, rep in (("A", rep_a), ("B", rep_b)):
        for adj in (rep or {}).get("adjustments") or []:
            items.append((who, adj))
    send: List[Dict[str, Any]] = []
    human: List[Dict[str, Any]] = []
    singles: List[Tuple[str, Dict[str, Any]]] = []
    used: set = set()
    now_s = snap.iso(now)

    def row(adj: Dict[str, Any], by: List[str], **extra: Any) -> Dict[str, Any]:
        r = dict(adj)
        r.update(id=adj_id(adj), proposed_by=by, **extra)
        return r

    def dup_of_open(adj: Dict[str, Any]) -> bool:
        return any(e.get("to") == adj["to"] and similar(adj, {"type": adj["type"], "to": adj["to"], "what": e.get("text") or ""}, known_ids)
                   or e.get("id") == adj_id(adj) for e in open_led)

    for i, (wi, ai) in enumerate(items):
        if i in used:
            continue
        used.add(i)
        mate = None
        for j, (wj, aj) in enumerate(items):
            if j in used or wj == wi:
                continue
            if similar(ai, aj, known_ids):
                mate = j
                break
        if mate is not None:
            used.add(mate)
            by = sorted({wi, items[mate][0]})
            if is_tier3(ai) or is_tier3(items[mate][1]):
                human.append(row(ai if is_tier3(ai) else items[mate][1], by, agreed=True))
            else:
                send.append(row(ai, by, agreed=True))
        elif is_tier3(ai):
            human.append(row(ai, [wi], agreed=False))
        else:
            singles.append((wi, ai))

    pending: List[Dict[str, Any]] = []
    old = [p for p in (pending_store or [])
           if ((_t(p.get("first_at")) and (now - _t(p["first_at"])).total_seconds() / 3600.0 <= PENDING_TTL_H) or not _t(p.get("first_at")))]
    consumed: set = set()
    for who, adj in singles:
        hit = next((k for k, p in enumerate(old) if k not in consumed and similar(adj, p, known_ids)), None)
        if hit is not None:
            consumed.add(hit)
            prev_by = old[hit].get("proposed_by") or []
            send.append(row(adj, sorted(set(prev_by) | {who}), agreed=False, from_pending=True))
        else:
            pending.append(row(adj, [who], first_at=now_s))
    for k, p in enumerate(old):
        if k in consumed:
            continue
        # an agreed pair this round also settles a pending entry
        if any(similar(s, p, known_ids) for s in send if not s.get("from_pending")):
            continue
        pending.append(p)
    # drop what is already out (open ledger entry) or duplicated inside the same round
    kept: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    for s in send:
        if dup_of_open(s) or any(similar(s, k, known_ids) and s.get("to") == k.get("to") for k in kept):
            dropped.append(s)
        else:
            kept.append(s)
    pending = [p for p in pending if not dup_of_open(p)]
    return {"send": kept, "pending": pending, "human": human, "dropped": dropped}


def merge_verdicts(rep_a: Optional[Dict[str, Any]], rep_b: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One verdict per follow-up id: both agree -> that; one says cannot_tell -> the other; disagree -> cannot_tell."""
    va = {v["id"]: v for v in (rep_a or {}).get("followups") or []}
    vb = {v["id"]: v for v in (rep_b or {}).get("followups") or []}
    out = []
    for fid in list(va) + [k for k in vb if k not in va]:
        a, b = va.get(fid), vb.get(fid)
        if a and b:
            if a["verdict"] == b["verdict"]:
                pick = a
            elif a["verdict"] == "cannot_tell":
                pick = b
            elif b["verdict"] == "cannot_tell":
                pick = a
            else:
                pick = dict(a, verdict="cannot_tell", evidence="两家不一致：%s / %s" % (a["verdict"], b["verdict"]))
        else:
            pick = a or b
        out.append(pick)
    return out


# ---------------------------------------------------------------- follow-up ledger (spec §C semantics, hours)

def register(st: Dict[str, Any], adj: Dict[str, Any], target: str, now: _dt.datetime, human: bool = False) -> Dict[str, Any]:
    h = clamp_h(adj.get("check_after_h"))
    fid = adj_id(adj)
    e = {"id": fid, "line": "project", "to": adj["to"], "target": target, "type": adj.get("type"), "text": adj["what"],
         "why": adj.get("why") or "", "done_when": adj.get("done_when") or "", "check_after_h": h,
         "check_after_min": h * 60, "created_at": snap.iso(now), "sent_at": snap.iso(now),
         "check_at": snap.iso(now + _dt.timedelta(hours=h)), "tries": 0, "cannot_tell": 0, "human": human,
         "history": [{"at": snap.iso(now), "what": "registered"}]}
    st.setdefault("followups", {})[fid] = e
    return e


def apply_verdicts(st: Dict[str, Any], verdicts: List[Dict[str, Any]], now: _dt.datetime) -> Dict[str, List[Dict[str, Any]]]:
    """done/obsolete close; cannot_tell twice counts as not_done; first not_done -> resend once (tries=1);
    second not_done -> escalate to the human (tries=2, human=True). -> {"resend": [entry], "human": [entry]}."""
    led = st.setdefault("followups", {})
    out: Dict[str, List[Dict[str, Any]]] = {"resend": [], "human": []}
    for v in verdicts:
        e = led.get(v["id"])
        if not e or e.get("closed_at"):
            continue
        verdict, ev = v["verdict"], v.get("evidence") or ""
        e.setdefault("history", []).append({"at": snap.iso(now), "what": verdict, "evidence": ev[:300]})
        if verdict in ("done", "obsolete"):
            sup._close(e, now, verdict, ev)
            continue
        h = clamp_h(e.get("check_after_h"))
        if verdict == "cannot_tell":
            e["cannot_tell"] = e.get("cannot_tell", 0) + 1
            if e["cannot_tell"] < 2:
                e["check_at"] = snap.iso(now)
                continue
        e["cannot_tell"] = 0
        e["evidence_last"] = ev[:300]
        tries = e.get("tries", 0)
        if e.get("to") == "human" or e.get("human"):
            e["tries"] = tries + 1
            e["check_at"] = snap.iso(now + _dt.timedelta(hours=h))
            continue
        if tries == 0:
            e["tries"] = 1
            e["check_at"] = snap.iso(now + _dt.timedelta(hours=h))
            out["resend"].append(e)
        else:
            e.update(tries=2, human=True, check_at=snap.iso(now + _dt.timedelta(hours=h)))
            e["history"].append({"at": snap.iso(now), "what": "escalate_to_human"})
            out["human"].append(e)
    return out


def prune_ledger(st: Dict[str, Any], now: _dt.datetime) -> None:
    led = st.setdefault("followups", {})
    for fid in list(led):
        e = led[fid]
        if e.get("closed_at") and (sup._hrs(now, e["closed_at"]) or 0) > 24 * 7:
            del led[fid]


# ---------------------------------------------------------------- state

def _skey(pid: str) -> str:
    return STATE_PREFIX + pid


def load_state(pid: str) -> Dict[str, Any]:
    return snap.load_task_cache().get(_skey(pid)) or {}


def save_state(pid: str, st: Dict[str, Any]) -> None:
    cache = snap.load_task_cache()
    cache[_skey(pid)] = st
    snap.save_task_cache(cache)


def project_hash(project: Dict[str, Any]) -> str:
    return hashlib.sha1(json.dumps(project, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def slot_start(now: _dt.datetime) -> _dt.datetime:
    """The latest 02/08/14/20 o'clock Beijing time (UTC+8) that is <= now, as UTC."""
    bj = now.astimezone(_dt.timezone(_dt.timedelta(hours=8)))
    cands = [bj.replace(hour=h, minute=0, second=0, microsecond=0) for h in SLOT_HOURS_BJ]
    cands += [c - _dt.timedelta(days=1) for c in cands]
    return max(c for c in cands if c <= bj).astimezone(_dt.timezone.utc)


def repeated_problem(reports: List[Dict[str, Any]]) -> bool:
    """The node-level supervisor flagged the same (line, issue kind) as `problem` in its last 2 reports."""
    if len(reports) < 2:
        return False

    def keys(rep: Dict[str, Any]) -> set:
        return {(l.get("line"), i.get("kind")) for l in rep.get("lines") or [] if l.get("status") == "problem"
                for i in l.get("issues") or []}
    return bool(keys(reports[-1]) & keys(reports[-2]))


def due(st: Dict[str, Any], now: _dt.datetime, project: Optional[Dict[str, Any]] = None,
        events: Optional[List[Dict[str, Any]]] = None, sup_reports: Optional[List[Dict[str, Any]]] = None) -> Tuple[bool, str]:
    """-> (due, why). Every 6 h at 02/08/14/20 Beijing, or: a milestone confirmed/rejected since the last run,
    project.json changed, the supervisor ledger flagged the same problem in 2 consecutive rounds."""
    last = _t(st.get("last_run_at"))
    if last is None:
        return True, "first_run"
    if last < slot_start(now):
        return True, "slot"
    for e in events or []:
        if e.get("kind") in ("milestone_confirm", "milestone_reject") and (e.get("at") or "") > st.get("last_run_at", ""):
            return True, "milestone_%s" % e["kind"].split("_")[1]
    if project is not None and st.get("project_hash") and st["project_hash"] != project_hash(project):
        return True, "project_changed"
    if sup_reports and repeated_problem(sup_reports) and \
            (st.get("last_trigger_report") or "") != str((sup_reports[-1] or {}).get("generated_at")):
        return True, "supervisor_repeat"
    return False, "not_due"


# ---------------------------------------------------------------- models

def _call(env: str, default: Callable, prompt: str, cwd: Optional[str], schema: Dict[str, Any],
          timeout: int) -> Optional[Dict[str, Any]]:
    custom = os.environ.get(env)
    if custom:
        rc, out, _err = decide._run(shlex.split(custom), prompt, cwd, timeout)
        return decide.extract_json(out) if rc == 0 else None
    return default(prompt, cwd, schema, timeout)


def call_a(prompt: str, cwd: Optional[str], schema: Dict[str, Any], timeout: int = MODEL_TIMEOUT_S) -> Optional[Dict[str, Any]]:
    return _call("PROGRESS_REVIEW_A_CMD", decide.call_opus, prompt, cwd, schema, timeout)


def call_b(prompt: str, cwd: Optional[str], schema: Dict[str, Any], timeout: int = MODEL_TIMEOUT_S) -> Optional[Dict[str, Any]]:
    return _call("PROGRESS_REVIEW_B_CMD", decide.call_astra, prompt, cwd, schema, timeout)


def build_prompt(dossier_text: str, dossier_path: str, second: bool = False) -> str:
    return PROMPT.replace("{family_note}", FAMILY_NOTE if second else "").replace("{dossier_path}", dossier_path) \
        .replace("{dossier}", dossier_text)


def run_models(prompt_a: str, prompt_b: Optional[str], cwd: Optional[str], schema: Dict[str, Any],
               fn_a: Callable = call_a, fn_b: Callable = call_b,
               timeout: int = MODEL_TIMEOUT_S) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Two independent calls in parallel, 20 min wall-clock in total."""
    res: Dict[str, Optional[Dict[str, Any]]] = {"a": None, "b": None}

    def go(key: str, fn: Callable, prompt: str) -> None:
        try:
            res[key] = fn(prompt, cwd, schema, timeout)
        except Exception:  # noqa: BLE001 - a model failure is a missing report, never a crash
            res[key] = None

    threads = [threading.Thread(target=go, args=("a", fn_a, prompt_a), daemon=True)]
    if prompt_b is not None:
        threads.append(threading.Thread(target=go, args=("b", fn_b, prompt_b), daemon=True))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout + 30)
    return res["a"], res["b"]


# ---------------------------------------------------------------- delivery

def cite(adj: Dict[str, Any], data: Dict[str, Any]) -> List[str]:
    """Dossier numbers for the adjustment: headline rows that mention an id/line the adjustment names, else the first
    flags. The model's own `why` is kept separately."""
    text = "%s %s %s" % (adj.get("what") or "", adj.get("why") or "", adj.get("to") or "")
    hit = [h["text"] for h in data.get("headline") or [] if any(_id_in(text, i) for i in h.get("ids") or [])]
    if not hit:
        hit = [f["text"] for f in (data.get("flags") or [])[:3]]
    return list(dict.fromkeys(hit))[:5]


def adj_message(adj: Dict[str, Any], data: Dict[str, Any], prefix: str = "[项目级巡检·调整]") -> str:
    nums = cite(adj, data)
    return _fit("%s %s（类型 %s）\n依据：%s\n档案数：%s\n预期效果：%s\n代价：%s\n落实标准：%s\n%d 小时后回查" % (
        prefix, adj["what"], adj["type"], adj.get("why") or "-", "；".join(nums) or "-", adj.get("expected_effect") or "-",
        adj.get("cost") or "-", adj.get("done_when") or "-", clamp_h(adj.get("check_after_h"))), 1500)


def _fit(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n - 1] + "…"


def coordinator_handle(project: Dict[str, Any], live: Optional[Dict[str, Any]], to: str) -> Optional[str]:
    cid = to.split(":", 1)[1] if ":" in to else None
    info = ((live or {}).get("coordinators") or {}).get(str(cid)) or {}
    return info.get("handle") if info.get("present") is True and info.get("handle") else None


def deliver(project: Dict[str, Any], merged: Dict[str, List[Dict[str, Any]]], data: Dict[str, Any],
            live: Optional[Dict[str, Any]], st: Dict[str, Any], now: _dt.datetime,
            send_coord: Callable[[str, str], Tuple[bool, str]], send_qq_fn: Callable[[str], Tuple[bool, str]],
            resend: Optional[List[Dict[str, Any]]] = None, escalated: Optional[List[Dict[str, Any]]] = None,
            guard: Optional[Any] = None) -> List[Dict[str, Any]]:
    """Send agreed adjustments to their coordinator (registered in the ledger on success), re-send first-time
    not_done ones, and push one QQ message: what went out, plus the human digest (tier-3 items and ledger entries that
    failed twice). Texts that match the holdout denylist are dropped. -> delivery rows."""
    rows: List[Dict[str, Any]] = []
    sent_adj: List[Dict[str, Any]] = []

    def ok_text(t: str) -> bool:
        return not (guard is not None and guard.hit(t))

    for adj in merged.get("send") or []:
        if adj["to"] == "tooling":
            merged.setdefault("human", []).append(dict(adj, note="tooling 没有接收终端，转人类摘要"))
            rows.append({"id": adj["id"], "to": adj["to"], "sent": False, "reason": "tooling_to_human"})
            continue
        h = coordinator_handle(project, live, adj["to"])
        if not h:
            merged.setdefault("requeue", []).append(adj)
            rows.append({"id": adj["id"], "to": adj["to"], "sent": False, "reason": "coordinator_absent"})
            continue
        text = adj_message(adj, data)
        if not ok_text(text):
            rows.append({"id": adj["id"], "to": adj["to"], "sent": False, "reason": "holdout_dropped"})
            continue
        ok, why = send_coord(h, text)
        rows.append({"id": adj["id"], "to": adj["to"], "target": h, "sent": ok, "reason": "sent" if ok else "failed",
                     **({} if ok else {"error": why})})
        if ok:
            register(st, adj, h, now)
            sent_adj.append(adj)
        else:
            merged.setdefault("requeue", []).append(adj)
    for e in resend or []:
        h = e.get("target")
        if not h:
            continue
        text = _fit("[项目级巡检·回查] 上次的调整没看到落实：%s；落实标准：%s；证据：%s" % (
            e["text"], e.get("done_when") or "（未写）", e.get("evidence_last") or "-"), 1500)
        if ok_text(text):
            ok, why = send_coord(h, text)
            rows.append({"id": e["id"], "to": e["to"], "target": h, "sent": ok, "reason": "resend" if ok else "failed"})
    human_items = list(merged.get("human") or [])
    digest = []
    for a in human_items:
        digest.append("- [%s] %s：%s（依据：%s）" % (a["type"], a["to"], a["what"][:160], (a.get("why") or "-")[:160]))
        if ok_text(a["what"]) and a["id"] not in (st.get("followups") or {}):
            register(st, a, "human", now, human=True)
    for e in escalated or []:
        digest.append("- [两次没落实] 发给 %s：%s；证据：%s" % (e["to"], e["text"][:160], (e.get("evidence_last") or "-")[:160]))
    if sent_adj:
        notice = "[项目级巡检·已发出 %d 条调整]\n%s" % (len(sent_adj), "\n".join(
            "%d) 给 %s：%s（依据：%s）" % (i + 1, a["to"], a["what"][:140], (a.get("why") or "-")[:140]) for i, a in enumerate(sent_adj)))
        if ok_text(notice):
            ok, why = send_qq_fn(_fit(notice, 1500))
            rows.append({"to": "qq", "sent": ok, "reason": "notice" if ok else "failed"})
    if digest:
        text = "[项目级巡检·待你决定]\n" + "\n".join(digest)
        if ok_text(text):
            ok, why = send_qq_fn(_fit(text, 1500))
            rows.append({"to": "qq", "sent": ok, "reason": "human_digest" if ok else "failed"})
    return rows


# ---------------------------------------------------------------- files

def review_dir(pid: str) -> pathlib.Path:
    return core.project_dir(pid) / "project_review"


def _stamp(now: _dt.datetime) -> str:
    return re.sub(r"[^0-9TZ]", "", snap.iso(now))


def prune_files(d: pathlib.Path, keep: int = KEEP_FILES) -> None:
    for pat in ("dossier-*.md", "dossier-*.json", "review-*.json"):
        names = sorted(p.name for p in d.glob(pat))
        for name in names[:-keep] if keep > 0 else names:
            try:
                (d / name).unlink()
            except OSError:
                pass


def _write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".new")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def save_dossier(pid: str, dossier: Dict[str, Any], now: _dt.datetime) -> pathlib.Path:
    d = review_dir(pid)
    stamp = _stamp(now)
    _write(d / ("dossier-%s.md" % stamp), dossier["text"])
    _write(d / ("dossier-%s.json" % stamp), json.dumps(dossier["data"], ensure_ascii=False))
    return d / ("dossier-%s.md" % stamp)


def save_review(pid: str, doc: Dict[str, Any], now: _dt.datetime) -> pathlib.Path:
    d = review_dir(pid)
    p = d / ("review-%s.json" % _stamp(now))
    _write(p, json.dumps(doc, ensure_ascii=False))
    prune_files(d)
    return p


# ---------------------------------------------------------------- entry

def run(pid: str, stage: Optional[int] = None, force: bool = False, dry_run: bool = False,
        now: Optional[_dt.datetime] = None, runner=snap.default_runner, store: Optional[Any] = None,
        git: Optional[Callable] = None, holdout_root: Optional[pathlib.Path] = None,
        fn_a: Callable = call_a, fn_b: Callable = call_b,
        send_coord: Optional[Callable[[str, str], Tuple[bool, str]]] = None,
        send_qq_fn: Optional[Callable[[str], Tuple[bool, str]]] = None,
        sup_reports: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Stage 1 dossier only, 2 + one model (nothing sent), 3 two models + merge + send. dry_run builds the dossier and
    stops: no files, no state, no model, no message. Returns {"ran", "ok", ...}; never raises for model trouble."""
    now = now or snap.utcnow()
    store = store or snap.Store(pid, runner)
    project, events, live = store.read()
    c = cfg(project)
    stage = int(stage or c.get("stage") or 1)
    st = load_state(pid)
    is_due, why = (True, "forced") if force else due(st, now, project, events, sup_reports)
    if not is_due:
        return {"ran": False, "reason": why, "last_run_at": st.get("last_run_at")}
    if git is None and isinstance(c.get("git_paths"), dict):
        git = default_git(c["git_paths"], runner)
    guard = load_guard(holdout_root)
    sup_state = snap.load_task_cache().get("supervisor:" + pid)
    try:
        dossier = build_dossier(project, events, live, now, runner, git, state=st, sup_state=sup_state, guard=guard)
    except HoldoutHit as exc:
        print("project_review: 档案命中留出集名单（%d 行），已拒绝生成，未写任何文件" % exc.count, file=sys.stderr)
        return {"ran": True, "ok": False, "why": "holdout_hit", "hits": exc.count}
    info: Dict[str, Any] = {"ran": True, "ok": True, "stage": stage, "trigger": why, "dossier": dossier,
                            "guard_active": bool(getattr(guard, "active", False))}
    if dry_run:
        info["dry_run"] = True
        return info
    path = save_dossier(pid, dossier, now)
    info["dossier_path"] = str(path)
    st["last_run_at"] = snap.iso(now)
    st["project_hash"] = project_hash(project)
    st["criteria_ledger"] = dossier["data"]["criteria_ledger"]
    if sup_reports:
        st["last_trigger_report"] = str((sup_reports[-1] or {}).get("generated_at"))
    prune_ledger(st, now)
    save_state(pid, st)   # recorded before the models run: a hung call is not retried every round
    prune_files(review_dir(pid))
    if stage <= 1:
        return info
    schema = schema_for(project)
    cwd = str(review_dir(pid))
    due_ids = dossier["data"]["followups_due"]
    prompt_a = build_prompt(dossier["text"], str(path))
    prompt_b = build_prompt(dossier["text"], str(path), second=True) if stage >= 3 else None
    raw_a, raw_b = run_models(prompt_a, prompt_b, cwd, schema, fn_a, fn_b)
    rep_a, why_a = validate_review(raw_a, due_ids) if raw_a is not None else (None, "模型没有返回")
    rep_b, why_b = validate_review(raw_b, due_ids) if raw_b is not None else (None, "模型没有返回")
    out_doc: Dict[str, Any] = {"schema": SCHEMA_ID, "generated_at": snap.iso(now), "stage": stage,
                               "dossier": path.name, "a": rep_a, "b": rep_b if stage >= 3 else None,
                               "errors": {k: v for k, v in (("a", why_a if rep_a is None else ""),
                                                            ("b", why_b if stage >= 3 and rep_b is None else "")) if v}}
    if rep_a is None and rep_b is None:
        info.update(ok=False, why="没有可用的评审报告：%s" % (why_a or why_b))
        save_review(pid, out_doc, now)
        return info
    if stage == 2:
        info.update(report=rep_a, review_path=str(save_review(pid, out_doc, now)))
        return info
    # stage 3: merge, verdicts on the ledger, send
    known = [m["id"] for m in project.get("milestones") or []] + [l["id"] for l in project.get("lines") or []]
    merged = merge(rep_a, rep_b, st.get("pending"), now, known, st.get("followups"))
    acts = apply_verdicts(st, merge_verdicts(rep_a, rep_b), now)
    if send_coord is None:
        def send_coord(h: str, text: str) -> Tuple[bool, str]:  # type: ignore[misc]
            ok, why_, _m = snap.terminal_send(runner, h, text[:1500], snap.env_for_terminal(h, live))
            return ok, why_
    if send_qq_fn is None:
        def send_qq_fn(text: str) -> Tuple[bool, str]:  # type: ignore[misc]
            return decide.send_qq(runner, decide.load_qq(), text)
    rows = deliver(project, merged, dossier["data"], live, st, now, send_coord, send_qq_fn,
                   resend=acts["resend"], escalated=acts["human"], guard=public_guard(guard, project)[0])
    st["pending"] = merged["pending"] + [dict(a, first_at=snap.iso(now)) for a in merged.get("requeue", [])]
    st["last_verdict"] = (rep_a or rep_b or {}).get("verdict")
    save_state(pid, st)
    out_doc.update(merged={k: merged.get(k) for k in ("send", "pending", "human", "dropped", "requeue")}, delivery=rows,
                   ledger_open=sum(1 for e in st.get("followups", {}).values() if not e.get("closed_at")))
    info.update(report=rep_a or rep_b, merged=merged, delivery=rows, review_path=str(save_review(pid, out_doc, now)))
    return info


def main(argv=None, runner=snap.default_runner, now: Optional[_dt.datetime] = None) -> int:
    ap = argparse.ArgumentParser(description="project-level review (docs/PROJECT_REVIEW.md)")
    ap.add_argument("--project", required=True)
    ap.add_argument("--stage", type=int, choices=(1, 2, 3), help="rollout stage (default: project.defaults.project_review.stage, else 1)")
    ap.add_argument("--force", action="store_true", help="run even when not due")
    ap.add_argument("--dry-run", action="store_true", help="build the dossier only; write and send nothing")
    ap.add_argument("--print", dest="print_", action="store_true", help="print the dossier text")
    a = ap.parse_args(argv)
    try:
        res = run(a.project, a.stage, a.force, a.dry_run, now=now, runner=runner)
    except core.ProgressError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False))
        return 2
    if a.print_ and res.get("dossier"):
        print(res["dossier"]["text"])
    summary = {k: v for k, v in res.items() if k not in ("dossier", "report", "merged")}
    print(json.dumps(summary, ensure_ascii=False, default=str))
    return 0 if res.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
