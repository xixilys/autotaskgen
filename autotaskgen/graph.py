"""Task-graph model (FRAMEWORK §1/§2): sub-graph proposals, the deterministic gate, scheduling helpers, metrics.

Pure functions over the folded state (progress_core.fold); no IO, no model, standard library only (Python 3.9).

A proposal (the body of a `graph_propose` event, also the file `progress propose` reads):

  {"line": "EVB", "parent": "plan_x" | null, "summary": "白话一句",
   "evidence": ["docs/…/REPORT.md", "ev_…", "<commit>"],
   "segments": [
     {"id": "fix", "mode": "parallel", "nodes": [
       {"id": "fix_a", "title": "…", "kind": "run", "deps": [], "write_scope": ["src/a"],
        "budget": {"minutes": 60}, "acceptance": ["test_a 通过"],
        "spec": "branch@docs/specs/fix_a.md", "route": {"agent": "codex", "model": "…", "on": "wsl"}}]},
     {"id": "merge", "mode": "serial", "nodes": [...]}]}

Gate rules (all deterministic):
- parent is null (line root) or an existing `plan` node of the same line that is not cancelled/superseded;
- depth (root children = 1) <= MAX_DEPTH; <= MAX_CHILDREN children per proposal; new ids unique and unused;
- every child: kind run|plan|check|decide, acceptance criteria, budget.minutes > 0 (plan also budget.nodes >= 1),
  write_scope (run/plan: non-empty) inside the parent's write scope (parent node's, else the line's
  `write_scope` in project.json, else not checked + warning);
- budget: children's minutes and node counts (plan child = 1 + its budget.nodes), plus what the parent already
  handed out, within the parent's budget (parent node's, else line `budget`, else project `defaults.budget`,
  else not checked + warning);
- deps: existing nodes of the line or siblings; acyclic (serial chains included);
- evidence: at least one non-empty reference.
- `parallel` segment whose siblings' write scopes overlap, share identical acceptance criteria, or depend on
  each other: DOWNGRADED to serial (warning, not rejection) — FRAMEWORK §2 "拆不出这两条的就按 serial 走".
- serial segment: nodes are chained in dependency-consistent order (each depends on the previous one), so
  only one of them can be ready at a time.
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

NODE_TYPES = ("run", "review", "plan", "check", "decide")   # review: legacy name, kept for old events
PROPOSE_TYPES = ("run", "plan", "check", "decide")
SEGMENT_MODES = ("serial", "parallel")
MAX_DEPTH = 8
MAX_CHILDREN = 12
NODE_MIN_MINUTES = 30
NODE_MAX_MINUTES = 120
WORKER_TYPES = ("run", "review", "check")      # kinds a worker terminal executes (patrol may dispatch them)
REVIEW_TYPES = ("review", "check")
DEFAULT_WEIGHT = {"run": 120, "review": 30, "check": 30, "plan": 0, "decide": 0}
DONE_STATES = ("delivered", "reviewed", "failed", "superseded", "cancelled")
OPEN_STATES = ("planned", "ready", "running", "blocked")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,79}$")


def _t(text: Optional[str]) -> Optional[_dt.datetime]:
    if not text:
        return None
    try:
        return _dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
    except (TypeError, ValueError):
        try:
            v = _dt.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
            return v if v.tzinfo else v.replace(tzinfo=_dt.timezone.utc)
        except ValueError:
            return None


# ------------------------------------------------------------------ write scopes

def norm_path(p: Any) -> Optional[str]:
    """Repo-relative prefix; "" = whole repo; None = invalid. Trailing /** or /* are dropped."""
    if not isinstance(p, str):
        return None
    s = p.strip().replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    for suf in ("/**", "/*"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    s = re.sub(r"/+", "/", s)
    s = s.rstrip("/") if s != "/" else s
    if s in ("", ".", "*", "**"):
        return ""
    if ".." in s.split("/"):
        return None
    return s


def path_contains(outer: str, inner: str) -> bool:
    return outer == "" or inner == outer or inner.startswith(outer.rstrip("/") + "/")


def paths_overlap(a: str, b: str) -> bool:
    return path_contains(a, b) or path_contains(b, a)


def scope_overlaps(a: Sequence[str], b: Sequence[str]) -> List[Tuple[str, str]]:
    return [(x, y) for x in a for y in b if paths_overlap(x, y)]


# ------------------------------------------------------------------ tree helpers

def depth_of(nodes: Dict[str, Any], nid: Optional[str]) -> int:
    """Root = 0; nodes without a parent (incl. legacy node_plan nodes) = 1."""
    d, seen = 0, set()
    while nid and nid not in seen:
        seen.add(nid)
        d += 1
        nid = (nodes.get(nid) or {}).get("parent")
    return d


def _children_of(nodes: Dict[str, Any], pid: Optional[str]) -> List[Dict[str, Any]]:
    return [n for n in nodes.values() if n.get("proposal") and n.get("parent") == pid]


def _budget_cost(n: Dict[str, Any]) -> Tuple[float, int]:
    b = n.get("budget") or {}
    try:
        mins = float(b.get("minutes") or 0)
    except (TypeError, ValueError):
        mins = 0.0
    extra = 0
    if n.get("kind") == "plan":
        try:
            extra = int(b.get("nodes") or 0)
        except (TypeError, ValueError):
            extra = 0
    return mins, 1 + extra


def _spent(n: Dict[str, Any]) -> bool:
    """Cancelled before it was ever dispatched: its budget goes back to the parent."""
    return not (n.get("status") == "cancelled" and not n.get("dispatched_at"))


def _acceptance(v: Any) -> Optional[List[str]]:
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return None
    out = [" ".join(x.split()) for x in v if isinstance(x, str) and x.strip()]
    return out or None


def _topo(ids: List[str], deps: Dict[str, List[str]]) -> Optional[List[str]]:
    """Stable topological order of `ids` by in-group deps; None on a cycle."""
    left = list(ids)
    done: List[str] = []
    while left:
        for i in left:
            if all(d in done or d not in ids for d in deps.get(i, [])):
                done.append(i)
                left.remove(i)
                break
        else:
            return None
    return done


def _find_cycle(deps: Dict[str, List[str]]) -> Optional[List[str]]:
    color: Dict[str, int] = {}
    stack: List[str] = []

    def visit(u):
        color[u] = 1
        stack.append(u)
        for v in deps.get(u, []):
            if v not in deps:
                continue
            if color.get(v) == 1:
                return stack[stack.index(v):] + [v]
            if not color.get(v):
                c = visit(v)
                if c:
                    return c
        stack.pop()
        color[u] = 2
        return None

    for u in deps:
        if not color.get(u):
            c = visit(u)
            if c:
                return c
    return None


# -------------------------------------------------------------------------- gate

def gate(proposal: Any, project: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    """-> {ok, errors[str], warnings[str], normalized|None, children[ids]}. Errors and warnings are Chinese."""
    errs: List[str] = []
    warns: List[str] = []
    if not isinstance(proposal, dict):
        return {"ok": False, "errors": ["提议必须是一个 JSON 对象"], "warnings": [], "normalized": None, "children": []}
    lid = proposal.get("line")
    line = next((l for l in project.get("lines") or [] if l.get("id") == lid), None)
    if not line:
        errs.append("主线未声明：%r" % lid)
    nodes = ((state.get("lines") or {}).get(lid) or {}).get("nodes") or {}
    summary = proposal.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        errs.append("summary 必填（白话一句，说明这次怎么拆、为什么）")
    evidence = proposal.get("evidence")
    ev_list = [e.strip() for e in evidence if isinstance(e, str) and e.strip()] if isinstance(evidence, list) else []
    if not ev_list:
        errs.append("提议必须引用依据：evidence 至少一条（上游结果、失败记录、报告路径、提交号或文献）")

    pid = proposal.get("parent") or None
    parent = None
    if pid is not None:
        parent = nodes.get(pid)
        if not parent:
            errs.append("父节点 %s 在主线 %s 里不存在" % (pid, lid))
        elif parent.get("kind") != "plan":
            errs.append("父节点 %s 的类型是 %s，只有 plan（router）节点能挂子图" % (pid, parent.get("kind") or "run"))
        elif parent.get("status") in ("cancelled", "superseded"):
            errs.append("父节点 %s 已%s，不能再挂子图" % (pid, "取消" if parent.get("status") == "cancelled" else "被取代"))
    depth = depth_of(nodes, pid) + 1
    if depth > MAX_DEPTH:
        errs.append("层级过深：子节点会在第 %d 层，上限 %d 层" % (depth, MAX_DEPTH))

    segs_in = proposal.get("segments")
    if not isinstance(segs_in, list) or not segs_in:
        errs.append("segments 必填：至少一段，每段标 mode 为 serial 或 parallel")
        segs_in = []

    # parent's write scope and budget
    if parent is not None and parent.get("write_scope") is not None:
        pscope_raw, pscope_from = parent.get("write_scope"), "父节点 %s" % pid
    elif line is not None and line.get("write_scope") is not None:
        pscope_raw, pscope_from = line.get("write_scope"), "主线 %s" % lid
    else:
        pscope_raw, pscope_from = None, None
        warns.append("父节点和主线都没有声明写域（write_scope），本次不查子节点写域是否越界")
    pscope = None
    if pscope_raw is not None:
        pscope = [p for p in (norm_path(x) for x in (pscope_raw if isinstance(pscope_raw, list) else [pscope_raw]))
                  if p is not None]
    if parent is not None and parent.get("budget"):
        pbudget, pbudget_from = parent.get("budget"), "父节点 %s" % pid
    elif parent is None and line is not None and (line.get("budget") or (project.get("defaults") or {}).get("budget")):
        pbudget = line.get("budget") or (project.get("defaults") or {}).get("budget")
        pbudget_from = "主线 %s" % lid
    else:
        pbudget, pbudget_from = None, None
        warns.append("%s没有预算（budget），本次不查子节点预算合计" % ("父节点 %s " % pid if pid else "主线 "))

    seen_ids = set()
    seg_ids = set()
    segments = []
    all_children: List[Dict[str, Any]] = []
    for si, seg in enumerate(segs_in):
        if not isinstance(seg, dict):
            errs.append("第 %d 段不是对象" % (si + 1))
            continue
        sid = seg.get("id") or "s%d" % (si + 1)
        if not isinstance(sid, str) or sid in seg_ids:
            errs.append("段 id 缺失或重复：%r" % sid)
        seg_ids.add(sid)
        mode = seg.get("mode")
        if mode not in SEGMENT_MODES:
            errs.append("段 %s 的 mode 必须是 serial 或 parallel（现在是 %r）" % (sid, mode))
        kids_in = seg.get("nodes")
        if not isinstance(kids_in, list) or not kids_in:
            errs.append("段 %s 没有节点" % sid)
            kids_in = []
        kids = []
        for c in kids_in:
            if not isinstance(c, dict):
                errs.append("段 %s 里有一项不是对象" % sid)
                continue
            cid = c.get("id")
            if not isinstance(cid, str) or not _ID_RE.match(cid):
                errs.append("节点 id 不合法：%r（字母数字开头，只含字母数字 _ . -，≤80 字）" % cid)
                continue
            if cid in seen_ids:
                errs.append("节点 id %s 在本次提议里重复" % cid)
                continue
            seen_ids.add(cid)
            if cid in nodes:
                errs.append("节点 id %s 已存在：返工、改派一律开新节点" % cid)
            kind = c.get("kind")
            if kind not in PROPOSE_TYPES:
                errs.append("节点 %s 的 kind 必须是 run/plan/check/decide（现在是 %r）" % (cid, kind))
            acc = _acceptance(c.get("acceptance"))
            if not acc:
                errs.append("节点 %s 没有验收标准（acceptance）：拆分时就要写清怎样算完成" % cid)
            b = c.get("budget")
            mins = None
            bnodes = None
            if not isinstance(b, dict):
                errs.append("节点 %s 没有预算（budget.minutes）" % cid)
            else:
                try:
                    mins = float(b.get("minutes"))
                    if mins <= 0:
                        raise ValueError
                except (TypeError, ValueError):
                    errs.append("节点 %s 的 budget.minutes 必须是正数" % cid)
                    mins = None
                if kind == "plan":
                    try:
                        bnodes = int(b.get("nodes"))
                        if bnodes < 1:
                            raise ValueError
                    except (TypeError, ValueError):
                        errs.append("plan 节点 %s 要写 budget.nodes（它的子树最多几个节点，≥1）" % cid)
                        bnodes = None
            if kind == "run" and mins is not None and not (NODE_MIN_MINUTES <= mins <= NODE_MAX_MINUTES):
                warns.append("节点 %s 预算 %d 分钟，建议一个节点 30 分钟–2 小时（太碎派发审查开销大，太大难验收）"
                             % (cid, mins))
            ws_raw = c.get("write_scope")
            scope: List[str] = []
            if ws_raw is None:
                if kind in ("run", "plan"):
                    errs.append("%s 节点 %s 必须声明写域 write_scope（会改哪些路径）" % (kind, cid))
            else:
                items = ws_raw if isinstance(ws_raw, list) else [ws_raw]
                for x in items:
                    np_ = norm_path(x)
                    if np_ is None:
                        errs.append("节点 %s 的写域 %r 不合法（不能含 ..）" % (cid, x))
                    else:
                        scope.append(np_)
                if kind in ("run", "plan") and not items:
                    errs.append("%s 节点 %s 的写域为空：要写会改哪些路径" % (kind, cid))
            if pscope is not None:
                for p in scope:
                    if not any(path_contains(o, p) for o in pscope):
                        errs.append("节点 %s 的写域 %s 超出%s 的写域（%s）" % (
                            cid, p or "<整个仓库>", pscope_from, "、".join(x or "<整个仓库>" for x in pscope) or "<只读>"))
            deps = c.get("deps") or []
            if not isinstance(deps, list) or not all(isinstance(d, str) and d for d in deps):
                errs.append("节点 %s 的 deps 必须是节点 id 列表" % cid)
                deps = []
            spec = c.get("spec")
            if spec is not None and (not isinstance(spec, str) or "@" not in spec):
                errs.append("节点 %s 的 spec 要写成 分支@路径" % cid)
            route = c.get("route")
            if route is not None and not isinstance(route, dict):
                errs.append("节点 %s 的 route 必须是对象" % cid)
                route = None
            child = {"id": cid, "title": c.get("title") or cid, "kind": kind, "deps": list(dict.fromkeys(deps)),
                     "write_scope": scope, "budget": {"minutes": mins}, "acceptance": acc or []}
            if bnodes is not None:
                child["budget"]["nodes"] = bnodes
            for k in ("spec", "route", "milestones", "require_review"):
                if c.get(k) is not None:
                    child[k] = c[k] if k != "route" else route
            if c.get("auto_dispatch") is not None:
                child["auto_dispatch"] = bool(c["auto_dispatch"])
            em = c.get("expected_minutes")
            if em is not None:
                try:
                    child["expected_minutes"] = float(em)
                except (TypeError, ValueError):
                    errs.append("节点 %s 的 expected_minutes 必须是数字" % cid)
            kids.append(child)
            all_children.append(child)
        segments.append({"id": sid, "mode": mode, "nodes": kids})

    if len(all_children) > MAX_CHILDREN:
        errs.append("一次分出 %d 个节点，超过上限 %d：先拆成更粗的 plan 节点，再由它们往下拆" % (len(all_children), MAX_CHILDREN))
    if not all_children and segs_in:
        errs.append("提议里没有任何合法节点")

    new_ids = {c["id"] for c in all_children}
    for c in all_children:
        for d in c["deps"]:
            if d == c["id"]:
                errs.append("节点 %s 依赖它自己" % d)
            elif d not in nodes and d not in new_ids:
                errs.append("节点 %s 的依赖 %s 既不是本主线已有节点，也不在本次提议里" % (c["id"], d))

    # parallel segments: independent write scopes and own acceptance, else downgrade to serial
    for seg in segments:
        ids = [c["id"] for c in seg["nodes"]]
        if seg["mode"] == "parallel" and len(ids) >= 2:
            reasons = []
            kids = seg["nodes"]
            for i in range(len(kids)):
                for j in range(i + 1, len(kids)):
                    a, b = kids[i], kids[j]
                    ov = scope_overlaps(a["write_scope"], b["write_scope"])
                    if ov:
                        reasons.append("%s 与 %s 写域重叠（%s / %s）" % (a["id"], b["id"], ov[0][0] or "<整个仓库>",
                                                                   ov[0][1] or "<整个仓库>"))
                    if a["acceptance"] and a["acceptance"] == b["acceptance"]:
                        reasons.append("%s 与 %s 验收标准相同，不能各自验收" % (a["id"], b["id"]))
            for c in kids:
                for d in c["deps"]:
                    if d in ids and d != c["id"]:
                        reasons.append("%s 依赖同段的 %s" % (c["id"], d))
            if reasons:
                seg["mode"] = "serial"
                seg["downgraded_from"] = "parallel"
                seg["downgrade_reasons"] = reasons
                warns.append("段 %s 标为 parallel，但%s；已按 serial 处理（一次只跑一个）" % (seg["id"], "；".join(reasons)))
        if seg["mode"] == "serial" and len(ids) >= 2 and not seg.get("downgraded_from"):
            # 11-07: SR proposed four workloads "按互斥写域并行" under mode=serial, then cancelled all four and
            # re-proposed them as parallel; say so at --check time instead
            kids = seg["nodes"]
            if not any(d in ids for c in kids for d in c["deps"]) and all(c["write_scope"] for c in kids) and \
                    not any(scope_overlaps(a["write_scope"], b["write_scope"]) or
                            (a["acceptance"] and a["acceptance"] == b["acceptance"])
                            for i, a in enumerate(kids) for b in kids[i + 1:]):
                warns.append("段 %s 标为 serial，但 %d 个节点互不依赖、写域不重叠、验收各自独立，可以标 parallel；"
                             "确实要一次只跑一个（例如抢同一台机器的测量窗口），就在 deps 里写明先后" % (seg["id"], len(ids)))
        if seg["mode"] == "serial" and len(ids) >= 2:
            inner = {c["id"]: [d for d in c["deps"] if d in ids] for c in seg["nodes"]}
            order = _topo(ids, inner)
            if order is None:
                continue  # reported by the cycle check below
            by_id = {c["id"]: c for c in seg["nodes"]}
            seg["nodes"] = [by_id[i] for i in order]
            for prev, cur in zip(order, order[1:]):
                if prev not in by_id[cur]["deps"]:
                    by_id[cur]["deps"].append(prev)

    graph = {c["id"]: [d for d in c["deps"] if d in new_ids] for c in all_children}
    cyc = _find_cycle(graph)
    if cyc:
        errs.append("依赖成环：%s" % " → ".join(reversed(cyc)))

    # budget within the parent's (what it already handed out included)
    if pbudget and isinstance(pbudget, dict):
        used_min = used_nodes = 0.0
        for n in _children_of(nodes, pid):
            if _spent(n):
                m, k = _budget_cost(n)
                used_min += m
                used_nodes += k
        new_min = sum(c["budget"]["minutes"] or 0 for c in all_children)
        new_nodes = sum(1 + (c["budget"].get("nodes") or 0 if c["kind"] == "plan" else 0) for c in all_children)
        try:
            cap_min = float(pbudget.get("minutes")) if pbudget.get("minutes") is not None else None
        except (TypeError, ValueError):
            cap_min = None
        try:
            cap_nodes = int(pbudget.get("nodes")) if pbudget.get("nodes") is not None else None
        except (TypeError, ValueError):
            cap_nodes = None
        if cap_min is not None and used_min + new_min > cap_min:
            errs.append("时间预算超了：本次子节点合计 %d 分钟，%s额度 %d 分钟、已分出 %d 分钟，只剩 %d 分钟"
                        % (new_min, pbudget_from, cap_min, used_min, max(0, cap_min - used_min)))
        if cap_nodes is not None and used_nodes + new_nodes > cap_nodes:
            errs.append("节点数预算超了：本次占 %d 个（plan 节点连同它的子树额度），%s额度 %d 个、已分出 %d 个"
                        % (new_nodes, pbudget_from, cap_nodes, used_nodes))

    normalized = None
    if not errs:
        normalized = {"line": lid, "parent": pid, "summary": summary.strip(), "evidence": ev_list,
                      "segments": segments, "depth": depth}
    return {"ok": not errs, "errors": errs, "warnings": warns, "normalized": normalized,
            "children": [c["id"] for c in all_children]}


# -------------------------------------------------------------- scheduling helpers

def node_weight(n: Dict[str, Any], defaults: Optional[Dict[str, Any]] = None) -> float:
    """Expected minutes; an expanded plan node weighs 0 (its children carry the time)."""
    if n.get("kind") == "plan" and n.get("proposals"):
        return 0.0
    for v in (n.get("expected_duration_minutes"), (n.get("budget") or {}).get("minutes")):
        try:
            if v is not None and float(v) > 0:
                return float(v)
        except (TypeError, ValueError):
            pass
    kind = n.get("kind") or "run"
    for src in (defaults or {}, DEFAULT_WEIGHT):
        try:
            v = src.get(kind)
            if v is not None and float(v) >= 0:
                return float(v)
        except (TypeError, ValueError, AttributeError):
            pass
    return 0.0


def _open_preds(nodes: Dict[str, Any], k: str) -> List[str]:
    """Open nodes that must finish before k: its deps, and for a plan node its proposed children."""
    n = nodes[k]
    preds = [d for d in n.get("deps") or [] if d in nodes and d != k]
    preds += [c for c, m in nodes.items() if m.get("parent") == k and m.get("proposal") and c != k]
    return [d for d in dict.fromkeys(preds) if nodes[d].get("status") in OPEN_STATES]


def _downstream(nodes: Dict[str, Any]) -> Dict[str, List[str]]:
    down: Dict[str, List[str]] = {k: [] for k in nodes}
    for k, n in nodes.items():
        if n.get("status") not in OPEN_STATES:
            continue
        for d in _open_preds(nodes, k):
            down[d].append(k)
    return down


def chain_lengths(nodes: Dict[str, Any], defaults: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
    """For each open node: its weight plus the longest open chain that waits on it (cycles broken)."""
    down = _downstream(nodes)
    memo: Dict[str, float] = {}

    def length(k, onstack):
        if k in memo:
            return memo[k]
        onstack.add(k)
        best = 0.0
        for c in down.get(k, []):
            if c not in onstack:
                best = max(best, length(c, onstack))
        onstack.discard(k)
        memo[k] = node_weight(nodes[k], defaults) + best
        return memo[k]

    for k, n in nodes.items():
        if n.get("status") in OPEN_STATES:
            length(k, set())
    return memo


def critical_path(nodes: Dict[str, Any], defaults: Optional[Dict[str, Any]] = None) -> List[str]:
    lens = chain_lengths(nodes, defaults)
    if not lens:
        return []
    down = _downstream(nodes)
    open_ids = set(lens)
    heads = [k for k in lens if not any(d in open_ids for d in _open_preds(nodes, k))] or list(lens)
    cur = max(sorted(heads), key=lambda k: lens[k])
    path, seen = [cur], {cur}
    while True:
        nxt = [c for c in down.get(cur, []) if c not in seen]
        if not nxt:
            return path
        cur = max(sorted(nxt), key=lambda k: lens.get(k, 0))
        path.append(cur)
        seen.add(cur)


def ready_queue(nodes: Dict[str, Any], defaults: Optional[Dict[str, Any]] = None) -> List[str]:
    """Ready nodes, longest downstream chain first (critical path), then oldest plan, then id."""
    lens = chain_lengths(nodes, defaults)
    ready = [k for k, n in nodes.items() if n.get("status") == "ready"]
    return sorted(ready, key=lambda k: (-lens.get(k, 0.0), nodes[k].get("planned_at") or "", k))


def segment_key(n: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """(proposal event id, segment id) for nodes created by a proposal, else None (legacy node_plan nodes)."""
    return (n.get("proposal") or "", n["segment"]) if n.get("segment") else None


def norm_signature(text: Any) -> str:
    """Failure / screen-line signature: lowercase, digits and hex runs folded, whitespace collapsed."""
    s = " ".join(str(text or "").lower().split())
    s = re.sub(r"\b[0-9a-f]{6,}\b", "#", s)
    return re.sub(r"\d+", "#", s)[:160]


def ready_since(nodes: Dict[str, Any], n: Dict[str, Any]) -> Optional[str]:
    """When the node became ready: latest of its plan time and its deps' completion."""
    t = n.get("planned_at") or ""
    need = set(n.get("require_review") or [])
    for d in n.get("deps") or []:
        dn, seen = nodes.get(d), set()
        while dn and dn.get("status") == "superseded" and dn.get("by") and dn.get("by") not in seen:
            seen.add(dn["by"])
            dn = nodes.get(dn["by"])
        if not dn:
            continue
        done = dn.get("reviewed_at") if d in need else (dn.get("delivered_at") or dn.get("reviewed_at"))
        t = max(t, done or "")
    return t or None


def dispatch_imported(n: Dict[str, Any]) -> bool:
    for h in reversed(n.get("history") or []):
        if h.get("kind") in ("node_dispatch", "auto_dispatch"):
            return h.get("source") == "import"
    return False


def ever_dispatched(n: Dict[str, Any]) -> bool:
    return any(h.get("kind") in ("node_dispatch", "auto_dispatch") for h in n.get("history") or [])


def delivered_after_dispatch(n: Dict[str, Any]) -> Optional[str]:
    """`at` of the latest node_deliver written at/after the latest dispatch, else None."""
    since = n.get("dispatched_at") or ""
    best = None
    for h in n.get("history") or []:
        if h.get("kind") == "node_deliver" and (h.get("at") or "") >= since:
            best = max(best or "", h.get("at") or "")
    return best


# ----------------------------------------------------------------------- metrics

REVIEW_RATIO_MAX = 0.2


def line_metrics(nodes: Dict[str, Any], now: _dt.datetime, defaults: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """FRAMEWORK §7 scheduling metrics for one line.

    ready_undispatched: open backlog now — nodes ready and never dispatched (decide nodes excluded: they wait
      for a ruling, not a worker); node_hours = Σ (now − ready_since).
    critical_path: longest open chain by expected minutes; stall_min = 0 when a node on it is running, else
      minutes since anything last happened on it (null when the line has no open nodes).
    review_run: Σ review/check node time over Σ run node time (dispatch → deliver, running ones up to now;
      imported dispatches without real times excluded); ratio null when no run time yet; over 0.2 is too heavy.
    """
    def mins_since(at):
        t = _t(at)
        return None if t is None else max(0.0, (now - t).total_seconds() / 60.0)

    backlog = []
    for k, n in nodes.items():
        if n.get("status") != "ready" or n.get("status_unknown") or n.get("kind") == "decide" or ever_dispatched(n):
            continue
        m = mins_since(ready_since(nodes, n))
        if m is not None:
            backlog.append((m, k))
    backlog.sort(reverse=True)

    cp = critical_path(nodes, defaults)
    stall = None
    if cp:
        if any(nodes[k].get("status") == "running" for k in cp):
            stall = 0.0
        else:
            last = max([h.get("at") or "" for k in cp for h in nodes[k].get("history") or []] +
                       [ready_since(nodes, nodes[cp[0]]) or ""])
            stall = mins_since(last) if last else None

    rev = run = 0.0
    for n in nodes.values():
        if not n.get("dispatched_at") or dispatch_imported(n) or n.get("imported"):
            continue
        start = _t(n.get("dispatched_at"))
        end = _t(n.get("delivered_at")) if n.get("delivered_at") and n["delivered_at"] >= n["dispatched_at"] else (
            now if n.get("status") == "running" else None)
        if start is None or end is None:
            continue
        dur = max(0.0, (end - start).total_seconds() / 60.0)
        if n.get("kind") in REVIEW_TYPES:
            rev += dur
        elif n.get("kind") == "run":
            run += dur
    ratio = round(rev / run, 3) if run > 0 else None
    return {
        "ready_undispatched": {"nodes": len(backlog), "node_hours": round(sum(m for m, _ in backlog) / 60.0, 2),
                               "oldest": [{"node": k, "ready_min": int(m)} for m, k in backlog[:5]]},
        "critical_path": {"nodes": cp, "minutes": round(sum(node_weight(nodes[k], defaults) for k in cp), 1),
                          "stall_min": None if stall is None else int(stall)},
        "review_run": {"review_min": int(rev), "run_min": int(run), "ratio": ratio,
                       "over": bool(ratio is not None and ratio > REVIEW_RATIO_MAX)},
    }
