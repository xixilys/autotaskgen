#!/usr/bin/env python3
"""One-off import: turn each line's existing router graph.json into progress events.

Router graph files have no common format (dict or list nodes, status or state, free-text
values). This maps each node to the fixed states with keyword rules, marks every event
"source": "import", and never invents outcomes: anything it cannot classify stays planned
with the original text kept in the summary.

  python3 import_graphs.py --repo <dse-rsi checkout> --project dse [--apply]

Without --apply it prints the events it would write and a per-line count.
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import progress_core as core  # noqa: E402

STATUS_KEYS = ("status", "state", "outcome", "scientific_status", "review_status")
DATE_RE = re.compile(r"(20\d\d-\d\d-\d\dT\d\d:\d\d:\d\dZ)")


def classify(text: str):
    """Return (status, outcome) for a free-text status. Order matters: first match wins."""
    t = (text or "").lower()
    if not t or t == "none":
        return "planned", None
    if "supersed" in t or "取代" in t:
        return "superseded", None
    if "cancel" in t or "取消" in t:
        return "cancelled", None
    if "running" in t or "dispatched" in t or "in_progress" in t or "input_accepted" in t:
        return "running", None
    if "fail" in t and "review" in t and "pass" not in t:
        return "failed", "FAIL"
    if "failed" in t or t == "fail":
        return "failed", "FAIL"
    if "blocked" in t or "queued_shared" in t or "env" == t:
        return "blocked", None
    outcome = None
    if "partial" in t or "unclosed" in t:
        outcome = "PARTIAL"
    elif "pass" in t or "succeeded" in t or "completed" in t or "accepted" in t or "delivered" in t:
        outcome = "PASS" if ("pass" in t) else None
    elif "unknown" in t:
        outcome = "UNKNOWN"
    reviewed = ("review" in t and ("pass" in t or "finalized" in t or "completed" in t)
                and "pending" not in t and "required" not in t) or "reviewed" in t or "accepted" in t
    if any(w in t for w in ("completed", "succeeded", "delivered", "accepted", "pass", "partial",
                             "unclosed", "proposal", "consumed")):
        if reviewed:
            return "reviewed", outcome or "PASS"
        return "delivered", outcome or "UNKNOWN"
    if t in ("ready", "planned", "prepared") or "ready" in t or "prepared" in t:
        return "planned", None
    if "stopped" in t:
        return "blocked", None
    return "planned", None


def node_rows(graph):
    """Yield (node_id, info dict) from any of the graph shapes seen in router graphs."""
    nodes = graph.get("nodes")
    meta = graph.get("nodes_metadata") or {}
    if isinstance(nodes, dict):
        for k, v in nodes.items():
            info = dict(meta.get(k) or {})
            if isinstance(v, dict):
                info = dict(v, **info)
            elif isinstance(v, str):
                info.setdefault("task_id", v)
            yield k, info
    elif isinstance(nodes, list):
        for v in nodes:
            if isinstance(v, dict):
                k = v.get("key") or v.get("node") or v.get("id") or v.get("task_id")
                if k:
                    yield str(k), v


def status_text(info):
    for k in STATUS_KEYS:
        v = info.get(k)
        if v:
            return str(v)
    return ""


def short(text, n=110):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def events_for_line(line, graph, at):
    rows = list(node_rows(graph))
    known = {k for k, _ in rows}
    task_to_key = {}
    for k, info in rows:
        tid = info.get("task_id") or info.get("task") or info.get("id")
        if isinstance(tid, str):
            task_to_key[tid] = k
    out = []
    base = {"line": line["id"], "source": "import",
            "actor": {"role": "import", "handle": "import@progress"}}
    for idx, (k, info) in enumerate(rows):
        if k.lower() == "root":
            continue
        st_text = status_text(info)
        status, outcome = classify(st_text)
        deps = []
        for d in info.get("deps") or []:
            d = task_to_key.get(d, d)
            if d in known and d != k and d.lower() != "root":
                deps.append(d)
        ev_at = at
        title = info.get("title") or info.get("purpose") or k
        refs = {}
        tid = info.get("task_id") or info.get("task")
        if isinstance(tid, str):
            refs["task"] = tid
        if info.get("dispatch_id"):
            refs["dispatch"] = info["dispatch_id"]
        spec = info.get("spec") or info.get("spec_file")
        out.append(dict(base, kind="node_plan", node=k, at=ev_at, id="ev_imp_%s_%s_p" % (line["id"], k),
                        title=short(title, 80), deps=deps, auto_dispatch=False,
                        status_unknown=not st_text,
                        spec=spec if isinstance(spec, str) else None,
                        summary=short("导入：%s" % title), refs=refs))
        if status in ("running", "delivered", "reviewed", "failed", "blocked"):
            wt = info.get("worktree") or info.get("worktree_id")
            r2 = dict(refs)
            if isinstance(wt, str):
                r2["worktree"] = wt
            if isinstance(info.get("terminal"), str):
                r2["terminal"] = info["terminal"]
            out.append(dict(base, kind="node_dispatch", node=k, at=ev_at, refs=r2,
                            id="ev_imp_%s_%s_d" % (line["id"], k), summary=short("导入：已派出")))
        if status in ("delivered", "reviewed", "failed"):
            out.append(dict(base, kind="node_deliver", node=k, at=ev_at,
                            id="ev_imp_%s_%s_v" % (line["id"], k),
                            outcome=outcome or ("FAIL" if status == "failed" else "UNKNOWN"),
                            summary=short("导入：%s" % (st_text or "已交付"))))
        if status == "reviewed":
            out.append(dict(base, kind="node_review", node=k, at=ev_at,
                            id="ev_imp_%s_%s_r" % (line["id"], k), outcome=outcome or "PASS",
                            summary=short("导入：%s" % st_text)))
        if status == "blocked":
            out.append(dict(base, kind="node_block", node=k, at=ev_at, blocker="other",
                            id="ev_imp_%s_%s_b" % (line["id"], k), summary=short("导入：%s" % st_text)))
        if status == "cancelled":
            out.append(dict(base, kind="node_cancel", node=k, at=ev_at,
                            id="ev_imp_%s_%s_c" % (line["id"], k), summary=short("导入：%s" % st_text)))
    # supersede: only when the graph names the successor
    for k, info in rows:
        if classify(status_text(info))[0] != "superseded":
            continue
        by = info.get("superseded_by") or info.get("superseded_by_source") or info.get("successor")
        by = task_to_key.get(by, by)
        if isinstance(by, str) and by in known and by != k:
            out.append(dict(base, kind="node_supersede", node=k, by=by, at=at,
                            id="ev_imp_%s_%s_s" % (line["id"], k), summary=short("导入：被 %s 取代" % by)))
        else:
            out.append(dict(base, kind="node_cancel", node=k, at=at,
                            id="ev_imp_%s_%s_c" % (line["id"], k),
                            summary=short("导入：已被取代（原图未写后继）")))
    # plans first, dependencies before dependants (validation requires planned deps)
    plans = [e for e in out if e["kind"] == "node_plan"]
    rest = [e for e in out if e["kind"] != "node_plan"]
    done, ordered = set(), []
    pending = list(plans)
    while pending:
        progressed = False
        for e in list(pending):
            if all(d in done for d in e["deps"]):
                ordered.append(e); done.add(e["node"]); pending.remove(e); progressed = True
        if not progressed:  # cycle: drop the unresolved deps rather than invent an order
            e = pending.pop(0)
            e["deps"] = [d for d in e["deps"] if d in done]
            ordered.append(e); done.add(e["node"])
    out = ordered + rest
    for e in out:
        if e.get("spec") is None:
            e.pop("spec", None)
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo", required=True)
    p.add_argument("--project", default="dse")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--at", default=None, help="timestamp for imported events (default now)")
    a = p.parse_args(argv)
    project = core.load_project(a.project)
    at = a.at or core.now_iso()
    report = {}
    rejected = []
    for line in project.get("lines") or []:
        path = pathlib.Path(a.repo) / ("docs/validation/router-%s/graph.json" % line["id"])
        try:
            graph = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            report[line["id"]] = {"error": "no graph: %s" % exc.__class__.__name__}
            continue
        evs = events_for_line(line, graph, at)
        if a.apply:
            for e in evs:
                try:
                    core.append(a.project, e)
                except core.ProgressError as exc:
                    rejected.append({"line": line["id"], "node": e.get("node"), "kind": e["kind"],
                                     "code": exc.code})
        st = collections.Counter()
        for e in evs:
            st[e["kind"]] += 1
        report[line["id"]] = dict(st)
    if a.apply:
        state = core.fold(project, core.read_events(a.project))
        report = {lid: {"counts": {k: v for k, v in ls["counts"].items() if v}}
                  for lid, ls in state["lines"].items()}
    print(json.dumps({"ok": not rejected, "lines": report, "rejected": rejected[:50],
                      "rejected_total": len(rejected)}, ensure_ascii=False, indent=1))
    return 0 if not rejected else 1


if __name__ == "__main__":
    sys.exit(main())
