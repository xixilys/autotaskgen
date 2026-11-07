#!/usr/bin/env python3
"""Router workload numbers, computed by script so the supervisor model never has to do arithmetic.

Every number carries its denominator and time window. Sources:
  - progress events (dispatch / deliver / review / plan / supersede / cancel / block);
  - the router's own codex rollout (turn start/end) for how busy the router is, found through the codex
    process that serves the router's terminal (`ps`: `resume <session id>` + ORCA_TERMINAL_HANDLE).

Used by supervisor.py (map section "router 负载", split-router check) and as a command
(`router_load.py --project dse [--hours 24] [--json] [--replay-at <ISO>]`).

Split-router criterion (11-07 router rules R5), decided here, not by the model:
  A. the router was >= 80 % busy for >= 3 consecutive clock hours in the window, and the median wait
     from a worker's delivery to its review is longer than the line's median worker run; or
  B. the line's planned parallelism > 0.8 x mean worker run (min) / router minutes per node.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import glob
import json
import os
import re
import statistics
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

BUSY_HOT = 0.8          # an hour counts as saturated at >= 80 % in a turn
HOT_STREAK_H = 3        # criterion A: this many consecutive saturated hours
SPLIT_FACTOR = 0.8      # criterion B
UNREVIEWED_MIN = 30     # "delivered and not reviewed for more than 30 min"
DISPATCH_KINDS = ("node_dispatch", "auto_dispatch")
END_KINDS = ("node_deliver", "node_block", "node_cancel", "node_supersede")
SESSIONS_DIR = os.path.expanduser(os.environ.get("PROGRESS_CODEX_SESSIONS") or "~/.codex/sessions")


def _t(s: Optional[str]) -> Optional[_dt.datetime]:
    if not s:
        return None
    try:
        v = _dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return v if v.tzinfo else v.replace(tzinfo=_dt.timezone.utc)


def _iso(t: _dt.datetime) -> str:
    return t.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pct(v: List[float], p: float) -> Optional[float]:
    """Nearest-rank percentile on a sorted copy; None when empty."""
    if not v:
        return None
    s = sorted(v)
    return s[min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))]


def _r(x: Optional[float], nd: int = 1) -> Optional[float]:
    return None if x is None else round(x, nd)


# ---------------------------------------------------------------- router rollout (busy share)

def session_of_handle(handle: str, ps_text: Optional[str] = None) -> Optional[str]:
    """codex session id served in the terminal `handle`, from the codex process command line."""
    if not handle:
        return None
    if ps_text is None:
        try:
            ps_text = subprocess.run(["ps", "-axww", "-o", "args="], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return None
    for line in ps_text.splitlines():
        if handle in line and "codex" in line:
            m = re.search(r"\bresume\s+([0-9a-f]{8}-[0-9a-f-]{27,})", line)
            if m:
                return m.group(1)
    return None


def rollout_path(session: str, root: str = SESSIONS_DIR) -> Optional[str]:
    hits = glob.glob(os.path.join(root, "*", "*", "*", "rollout-*%s.jsonl" % session))
    return max(hits, key=os.path.getmtime) if hits else None


def _tail_from(path: str, start: _dt.datetime, chunk: int = 8 << 20) -> List[str]:
    """Lines of `path` from (a little before) the first one stamped >= start; reads from the end, not the
    whole file (router rollouts are 100 MB+)."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        while True:
            pos = max(0, size - chunk)
            f.seek(pos)
            data = f.read()
            lines = data.split(b"\n")
            if pos > 0:
                lines = lines[1:]  # partial first line
            first = None
            for l in lines[:50]:
                m = re.search(rb'"timestamp":"([^"]+)"', l)
                if m:
                    first = _t(m.group(1).decode())
                    break
            if pos == 0 or (first is not None and first < start):
                return [l.decode("utf-8", "replace") for l in lines]
            chunk *= 2


def router_turns(path: str, start: _dt.datetime, now: _dt.datetime) -> Dict[str, Any]:
    """-> {turns: [(a, b)], context_tokens, open_turn, n_turns_total}. A turn still open at the end runs to now."""
    turns: List[Tuple[_dt.datetime, _dt.datetime]] = []
    t0: Optional[_dt.datetime] = None
    ctx = None
    for l in _tail_from(path, start):
        if '"task_started"' not in l and '"task_complete"' not in l and '"turn_aborted"' not in l \
                and '"token_count"' not in l:
            continue
        try:
            d = json.loads(l)
        except ValueError:
            continue
        p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
        ts = _t(d.get("timestamp"))
        if ts is None or ts > now:
            continue
        kind = p.get("type")
        if kind == "task_started":
            t0 = ts
        elif kind in ("task_complete", "turn_aborted") and t0 is not None:
            turns.append((t0, ts))
            t0 = None
        elif kind == "token_count" and isinstance(p.get("info"), dict):
            last = p["info"].get("last_token_usage") or {}
            if last.get("input_tokens"):
                ctx = int(last["input_tokens"])
    if t0 is not None:
        turns.append((t0, now))
    return {"turns": turns, "context_tokens": ctx, "open_turn": t0 is not None}


def busy_profile(turns: List[Tuple[_dt.datetime, _dt.datetime]], start: _dt.datetime,
                 now: _dt.datetime) -> Dict[str, Any]:
    """Busy share over [start, now] and per clock hour; longest run of >= 80 % hours (and the run ending now)."""
    span = (now - start).total_seconds()
    busy = 0.0
    hours: Dict[_dt.datetime, float] = {}
    h = start.replace(minute=0, second=0, microsecond=0)
    while h < now:
        hours[h] = 0.0
        h += _dt.timedelta(hours=1)
    for a, b in turns:
        a, b = max(a, start), min(b, now)
        if b <= a:
            continue
        busy += (b - a).total_seconds()
        c = a.replace(minute=0, second=0, microsecond=0)
        while c < b:
            nxt = c + _dt.timedelta(hours=1)
            hours[c] = hours.get(c, 0.0) + (min(b, nxt) - max(a, c)).total_seconds()
            c = nxt
    # an hour's denominator is the part of it inside the window (first/last hour may be partial)
    share = {}
    for c, s in hours.items():
        den = (min(c + _dt.timedelta(hours=1), now) - max(c, start)).total_seconds()
        share[c] = s / den if den > 0 else 0.0
    full = [c for c in sorted(share) if c >= start and c + _dt.timedelta(hours=1) <= now]
    best = run = 0
    best_end = None
    for c in full:
        run = run + 1 if share[c] >= BUSY_HOT else 0
        if run > best:
            best, best_end = run, c + _dt.timedelta(hours=1)
    cur = 0
    for c in reversed(full):
        if share[c] < BUSY_HOT:
            break
        cur += 1
    return {"busy_h": busy / 3600.0, "span_h": span / 3600.0, "share": busy / span if span > 0 else None,
            "hot_hours": sum(1 for c in full if share[c] >= BUSY_HOT), "full_hours": len(full),
            "hot_streak_max_h": best, "hot_streak_end": _iso(best_end) if best_end else None,
            "hot_streak_now_h": cur}


# ---------------------------------------------------------------- event metrics

def _by_node(events: List[Dict[str, Any]], line: str) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for e in events:
        if e.get("line") == line and e.get("node") and e.get("source") != "import":
            out.setdefault(e["node"], []).append(e)
    for v in out.values():
        v.sort(key=lambda e: e.get("at") or "")
    return out


def _done_at(es: List[Dict[str, Any]]) -> Optional[str]:
    """When a node's output became usable downstream: first PASS deliver or PASS review."""
    for e in es:
        if e.get("kind") in ("node_deliver", "node_review") and e.get("outcome") == "PASS":
            return e.get("at")
    return None


def line_load(line: Dict[str, Any], nodes: Dict[str, Dict[str, Any]], events: List[Dict[str, Any]],
              now: _dt.datetime, hours: float = 24.0, router: Optional[Dict[str, Any]] = None,
              planned_parallel: Optional[int] = None, ready: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """All numbers for one line over [now - hours, now]. `nodes` is the folded state of the line,
    `router` what router_turns() returned (or None when the router's session can't be found)."""
    lid = line["id"]
    start = now - _dt.timedelta(hours=hours)
    S, N = _iso(start), _iso(now)
    byn = _by_node(events, lid)
    inwin = lambda e: S <= (e.get("at") or "") <= N  # noqa: E731

    # worker runs: dispatch -> first end event (or now if still running), clipped to the window for concurrency
    runs: List[float] = []
    inflight_s = 0.0
    dispatched: List[Tuple[str, Dict[str, Any]]] = []
    for nid, es in byn.items():
        for i, e in enumerate(es):
            if e.get("kind") not in DISPATCH_KINDS or (e.get("kind") == "auto_dispatch" and e.get("phase") == "started"):
                continue
            a = _t(e.get("at"))
            end = next((x for x in es[i + 1:] if x.get("kind") in END_KINDS), None)
            b = _t(end.get("at")) if end else (now if (nodes.get(nid) or {}).get("status") == "running" else None)
            if a is None:
                continue
            if inwin(e):
                dispatched.append((nid, e))
                if end is not None and end.get("kind") == "node_deliver":
                    runs.append((b - a).total_seconds() / 60.0)
            if b is not None:
                lo, hi = max(a, start), min(b, now)
                if hi > lo:
                    inflight_s += (hi - lo).total_seconds()
    # review wait: deliver -> next review of the same node (deliveries in the window)
    waits: List[float] = []
    for nid, es in byn.items():
        for i, e in enumerate(es):
            if e.get("kind") == "node_deliver" and inwin(e):
                rv = next((x for x in es[i + 1:] if x.get("kind") == "node_review"), None)
                if rv:
                    waits.append((_t(rv["at"]) - _t(e["at"])).total_seconds() / 60.0)
    # delivered, not reviewed for > 30 min (now)
    unreviewed = []
    for nid, n in nodes.items():
        if n.get("status") == "delivered" and n.get("delivered_at"):
            m = (now - _t(n["delivered_at"])).total_seconds() / 60.0
            if m > UNREVIEWED_MIN:
                unreviewed.append({"node": nid, "min": int(m)})
    unreviewed.sort(key=lambda x: -x["min"])
    # superseded / cancelled share of nodes planned in the window
    planned = {nid for nid, es in byn.items() if any(e.get("kind") == "node_plan" and inwin(e) for e in es)}
    dropped = {nid for nid in planned if any(e.get("kind") in ("node_supersede", "node_cancel") for e in byn[nid])}
    # dispatched before every dependency was done
    early = []
    for nid, e in dispatched:
        deps = (nodes.get(nid) or {}).get("deps") or []
        miss = [d for d in deps if not (_done_at(byn.get(d) or []) or "9") <= e["at"]
                and (nodes.get(d) or {}).get("status") not in ("superseded", "cancelled")]
        if miss and nid not in {x["node"] for x in early}:
            early.append({"node": nid, "missing": miss[:2] + (["…%d" % (len(miss) - 2)] if len(miss) > 2 else []),
                          "at": e["at"]})
    # router actions in the window (denominator for router minutes per node)
    acts = sum(1 for e in events if e.get("line") == lid and inwin(e) and (e.get("actor") or {}).get("role") == "router"
               and e.get("kind") in ("node_dispatch", "node_review"))
    if planned_parallel is None:
        # no declared ceiling: the plan's width now = open nodes none of whose dependencies is still open
        live_ = ("planned", "ready", "running", "delivered")
        planned_parallel = sum(1 for n in nodes.values() if n.get("status") in live_ and not any(
            (nodes.get(d) or {}).get("status") in live_ + ("blocked",) for d in n.get("deps") or [])) or None
    span_h = hours
    out: Dict[str, Any] = {
        "line": lid, "window": {"start": S, "end": N, "hours": hours},
        "review_wait_min": {"n": len(waits), "median": _r(pct(waits, .5)), "p90": _r(pct(waits, .9))},
        "worker_run_min": {"n": len(runs), "median": _r(pct(runs, .5)), "mean": _r(statistics.mean(runs) if runs else None)},
        "avg_concurrent_workers": round(inflight_s / 3600.0 / span_h, 2) if span_h else None,
        "ready_undispatched": ready or {},
        "unreviewed_over_30min": unreviewed,
        "superseded_or_cancelled": {"n": len(dropped), "of_planned": len(planned),
                                    "share": round(len(dropped) / len(planned), 2) if planned else None},
        "dispatched_with_missing_deps": {"n": len(early), "of_dispatched": len(dispatched), "nodes": early[:5]},
        "router_actions": acts, "planned_parallel": planned_parallel,
    }
    if router is None:
        out["router_busy"] = None
    else:
        bp = busy_profile(router["turns"], start, now)
        out["router_busy"] = {**{k: (_r(v, 2) if isinstance(v, float) else v) for k, v in bp.items()},
                              "context_tokens": router.get("context_tokens")}
        out["router_min_per_node"] = _r(bp["busy_h"] * 60.0 / acts) if acts else None
    out["split"] = split_check(out)
    return out


def split_check(m: Dict[str, Any]) -> Dict[str, Any]:
    """Criterion A / B from the module docstring. -> {hit, why: [..], numbers}."""
    why: List[str] = []
    rb = m.get("router_busy") or {}
    wm = (m.get("worker_run_min") or {}).get("median")
    rw = (m.get("review_wait_min") or {}).get("median")
    if rb and rb.get("hot_streak_max_h", 0) >= HOT_STREAK_H and wm is not None and rw is not None and rw > wm:
        why.append("A：router 连续 %d 小时忙于 80%% 以上（截至 %s），审查等待中位 %.0f 分 > worker 中位 %.0f 分"
                   % (rb["hot_streak_max_h"], rb.get("hot_streak_end"), rw, wm))
    mean = (m.get("worker_run_min") or {}).get("mean")
    rmin = m.get("router_min_per_node")
    pp = m.get("planned_parallel")
    if pp and pp >= 2 and mean and rmin:   # one runnable node: a second router would have nothing to take
        cap = SPLIT_FACTOR * mean / rmin
        if pp > cap:
            why.append("B：计划并行 %d > 0.8 × worker 平均 %.0f 分 ÷ router 每节点 %.1f 分 = %.1f" % (pp, mean, rmin, cap))
    return {"hit": bool(why), "why": why}


def independent_groups(nodes: Dict[str, Dict[str, Any]], limit: int = 4) -> List[Dict[str, Any]]:
    """Open nodes split into groups that share no dependency edge (weakly connected components) — the
    sub-goals a sub-router could take. Largest first."""
    open_ = {k: n for k, n in nodes.items() if n.get("status") in ("planned", "ready", "running", "delivered", "blocked")}
    parent = {k: k for k in open_}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for k, n in open_.items():
        for d in n.get("deps") or []:
            if d in open_:
                parent[find(k)] = find(d)
    groups: Dict[str, List[str]] = {}
    for k in open_:
        groups.setdefault(find(k), []).append(k)
    out = []
    for g in sorted(groups.values(), key=lambda g: (-len(g), sorted(g)[0])):
        ms = sorted({m for k in g for m in (open_[k].get("milestones") or [])})
        out.append({"nodes": sorted(g)[:6], "n": len(g), "milestones": ms[:4]})
    return out[:limit]


def render_rows(loads: Dict[str, Dict[str, Any]]) -> List[str]:
    """Map rows: one line per router, plain words, every number with its denominator and window."""
    rows = []
    for lid, m in loads.items():
        w = m["window"]
        rb = m.get("router_busy")
        if rb:
            busy = "忙碌 %.0f%%（%.1f/%.1fh；≥80%% 的整点小时 %d/%d，最长连续 %dh，到现在连续 %dh；上下文 %s token）" % (
                100 * (rb["share"] or 0), rb["busy_h"], rb["span_h"], rb["hot_hours"], rb["full_hours"],
                rb["hot_streak_max_h"], rb["hot_streak_now_h"],
                "%dK" % (rb["context_tokens"] // 1000) if rb.get("context_tokens") else "?")
        else:
            busy = "忙碌 ?（找不到 router 的会话记录）"
        rw, wr = m["review_wait_min"], m["worker_run_min"]
        ru = m.get("ready_undispatched") or {}
        old = ru.get("oldest") or []
        sc, dm = m["superseded_or_cancelled"], m["dispatched_with_missing_deps"]
        un = m["unreviewed_over_30min"]
        parts = [
            busy,
            "审查等待 中位 %s 分 p90 %s 分（%d 次交付）" % (rw["median"], rw["p90"], rw["n"]),
            "worker 每单 中位 %s 分 平均 %s 分（%d 单）" % (wr["median"], wr["mean"], wr["n"]),
            "平均同时在跑 %s 个 worker" % m["avg_concurrent_workers"],
            "就绪未派 %s 个%s" % (ru.get("nodes", 0), ("，最久 %s 等 %s 分" % (old[0]["node"], old[0]["ready_min"])) if old else ""),
            "交付超 30 分没审 %d 单%s" % (len(un), ("：" + "、".join("%s %d分" % (u["node"], u["min"]) for u in un[:3])) if un else ""),
            "被取代或取消 %d/%d（本窗口内排的节点）" % (sc["n"], sc["of_planned"]),
            "派出时前置未齐 %d/%d%s" % (dm["n"], dm["of_dispatched"], ("：" + "、".join(
                "%s 缺 %s" % (x["node"][:48], ",".join(m[:48] for m in x["missing"])) for x in dm["nodes"][:3])) if dm["nodes"] else ""),
            "router 每节点 %s 分（%d 次派/审）" % (m.get("router_min_per_node"), m["router_actions"]),
            "计划并行 %s" % m.get("planned_parallel"),
        ]
        rows.append("- %s [%s→%s，%gh] %s" % (lid, w["start"][5:16], w["end"][5:16], w["hours"], " | ".join(parts)))
        if m["split"]["hit"]:
            rows.append("  ⚠拆 router 判据命中：" + "；".join(m["split"]["why"]))
    return rows or ["（没有开着的线）"]


# ---------------------------------------------------------------- whole project

def project_load(project: Dict[str, Any], state: Dict[str, Any], events: List[Dict[str, Any]],
                 live: Optional[Dict[str, Any]], now: _dt.datetime, hours: float = 24.0,
                 ps_text: Optional[str] = None, sessions_root: str = SESSIONS_DIR,
                 planned_parallel=None) -> Dict[str, Dict[str, Any]]:
    lm = (live or {}).get("metrics") or {}
    out: Dict[str, Dict[str, Any]] = {}
    start = now - _dt.timedelta(hours=hours)
    for ln in project.get("lines") or []:
        lid = ln["id"]
        ls = (state.get("lines") or {}).get(lid) or {}
        if not ls.get("open", True):
            continue
        h = ((((live or {}).get("routers") or {}).get(lid) or {}).get("handle")) or ln.get("router")
        rt = None
        sid = session_of_handle(h, ps_text) if h else None
        path = rollout_path(sid, sessions_root) if sid else None
        if path:
            try:
                rt = router_turns(path, start, now)
            except OSError:
                rt = None
        pp = planned_parallel(project, ln) if callable(planned_parallel) else planned_parallel
        out[lid] = line_load(ln, ls.get("nodes") or {}, events, now, hours, rt, pp,
                             (lm.get(lid) or {}).get("ready_undispatched"))
        out[lid]["router_session"] = sid
    return out


def main(argv=None) -> int:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import progress_core as core
    import snapshot as snap
    import patrol
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project", default=os.environ.get("PROGRESS_PROJECT", "dse"))
    p.add_argument("--hours", type=float, default=24.0)
    p.add_argument("--replay-at", help="compute as of this UTC time (events after it are ignored)")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    store = snap.Store(a.project)
    project, events, live = store.read()
    now = _t(a.replay_at) if a.replay_at else _dt.datetime.now(_dt.timezone.utc)
    if a.replay_at:
        events = [e for e in events if (e.get("at") or "") <= a.replay_at]
    state = core.fold(project, events, at=_iso(now))
    if a.replay_at:
        live = None
    loads = project_load(project, state, events, live, now, a.hours, planned_parallel=patrol.max_parallel)
    print(json.dumps(loads, ensure_ascii=False, indent=1) if a.json else "\n".join(render_rows(loads)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
