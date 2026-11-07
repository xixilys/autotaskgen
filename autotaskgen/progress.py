#!/usr/bin/env python3
"""progress: write and read project progress events.

Examples
  progress plan    --line EVB --node dev_x --title "…" --deps a,b --spec you/evb-dev@docs/…/specs/dev_x.md \
                   --model gpt-6.1-sol --effort high --on wsl --milestone M1 --summary "…"
  progress dispatch --line EVB --node dev_x --dispatch ctx_… --worktree <id> --terminal term_… --summary "…"
  progress deliver --line EVB --node dev_x --outcome PASS [--result negative] --report docs/…/REPORT.md --head <sha> --summary "…"
  progress deliver --line EVB --node dev_x --outcome BLOCKED --blocker dep|env|human|budget|design --blocked-by "还差什么" …
                   (R2 11-07: worker/router deliveries are PASS or BLOCKED only; PARTIAL/FAIL/UNKNOWN are refused)
  progress precheck --line EVB --node dev_x [--dry-run]  R1: check the spec's 前置条件 before worker-start; a missing
                   input is recorded as node_block blocker=dep (patrol dispatches once it exists). `progress dispatch`
                   runs the same check and refuses a dispatch whose inputs are missing.
  progress review  --line EVB --node dev_x --outcome PASS --summary "…"
  deliver / review first push the work branch (cwd, or the worktree holding --head) to origin after the
  fail-closed exclusions (workpush.py); the event gets pushed {branch, sha} | push_failed | push_skipped.
  progress block   --line EVB --node dev_x --blocker env --summary "…"
  progress supersede --line EVB --node dev_x --by dev_x_repair --summary "…"
  progress claim   --line EVB --milestone M1 --summary "…" --report …
  progress confirm --milestone M1 --summary "…"        (core coordinator only)
  progress reject  --milestone M1 --summary "…"
  progress propose FILE [--check]                       router: commit a sub-graph proposal (JSON) after the gate
  progress state   [--line EVB]                         print folded state as JSON
  progress project-init FILE | project-show

Project: --project or $PROGRESS_PROJECT (default dse). Role: --role or inferred from the kind.
Identity: actor handle = $ORCA_TERMINAL_HANDLE, workspace = $ORCA_WORKSPACE_ID (override with --as).
On a machine without the store (no $PROGRESS_HOME/<project>/project.json) the call is forwarded
to $PROGRESS_REMOTE (default ssh host "wsl"); if that fails the event is queued in
~/.progress/outbox.jsonl and sent with the next successful call.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shlex
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import progress_core as core  # noqa: E402
import graph  # noqa: E402
import prereq  # noqa: E402
import workpush  # noqa: E402

DEFAULT_ROLE = {
    "plan": "router", "dispatch": "router", "review": "router", "supersede": "router",
    "cancel": "router", "claim": "router", "deliver": "worker", "heartbeat": "worker",
    "block": "worker", "confirm": "coordinator", "reject": "coordinator",
    "line-open": "coordinator", "line-close": "coordinator",
    "nudge": "patrol", "observe": "patrol", "auto-dispatch": "patrol",
}
KIND = {
    "plan": "node_plan", "dispatch": "node_dispatch", "review": "node_review",
    "supersede": "node_supersede", "cancel": "node_cancel", "claim": "milestone_claim",
    "deliver": "node_deliver", "heartbeat": "node_heartbeat", "block": "node_block",
    "confirm": "milestone_confirm", "reject": "milestone_reject", "line-open": "line_open",
    "line-close": "line_close", "nudge": "nudge", "observe": "observe",
    "auto-dispatch": "auto_dispatch",
}
REMOTE_CLI = os.environ.get("PROGRESS_REMOTE_CLI", "~/opt/progress/progress.py")


def emit(obj, code=0):
    print(json.dumps(obj, ensure_ascii=False))
    return code


def local_store(pid):
    return (core.home() / pid / "project.json").is_file()


def outbox():
    return pathlib.Path("~/.progress/outbox.jsonl").expanduser()


def forward(pid, event):
    """Send one event to the remote store; returns the remote result dict or raises."""
    host = os.environ.get("PROGRESS_REMOTE", "wsl")
    cmd = "python3 %s --project %s append-raw" % (REMOTE_CLI, shlex.quote(pid))
    r = subprocess.run(["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", host, cmd],
                       input=json.dumps(event, ensure_ascii=False), text=True,
                       capture_output=True, timeout=60)
    if r.returncode == 255 or not r.stdout.strip():
        raise ConnectionError(r.stderr.strip()[:300] or "ssh failed")
    return json.loads(r.stdout.strip().splitlines()[-1])


def flush_outbox(pid):
    box = outbox()
    if not box.is_file():
        return 0
    lines = [l for l in box.read_text(encoding="utf-8").splitlines() if l.strip()]
    keep, sent = [], 0
    for raw in lines:
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        try:
            forward(ev.get("project") or pid, ev)
            sent += 1  # rejected events are dropped too: they would be rejected forever
        except (ConnectionError, subprocess.TimeoutExpired, OSError):
            keep.append(raw)
    box.write_text("".join(l + "\n" for l in keep), encoding="utf-8")
    return sent


def submit(pid, event, extra=None):
    """Append (store host) or forward the event; `extra` (e.g. the push result) is merged into the JSON output."""
    out = lambda d, code=0: emit(dict(d, **extra) if extra else d, code)  # noqa: E731
    if local_store(pid):
        try:
            return out(core.append(pid, event))
        except core.ProgressError as exc:
            return out(exc.as_dict(), 2)
    event.setdefault("id", core.new_id())
    event.setdefault("at", core.now_iso())
    event["project"] = pid
    try:
        flush_outbox(pid)
        res = forward(pid, event)
        return out(res, 0 if res.get("ok") else 2)
    except (ConnectionError, subprocess.TimeoutExpired, OSError) as exc:
        box = outbox()
        box.parent.mkdir(parents=True, exist_ok=True)
        with box.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        return out({"ok": True, "queued": True, "id": event["id"], "reason": str(exc)[:200]})


def push_step(cmd, a, pid, ev):
    """deliver: push the worker's branch (cwd, or the worktree holding --head); review: the router's branch (cwd).
    Runs before the event is written; the result goes into the event and the CLI output. Never blocks the event."""
    if cmd not in ("deliver", "review"):
        return None
    guard_host = None if local_store(pid) else os.environ.get(
        "PROGRESS_PUSH_GUARD_HOST", os.environ.get("PROGRESS_REMOTE", "wsl"))
    res = workpush.push_work_branch(os.getcwd(), head=a.head if cmd == "deliver" else None,
                                    guard_host=guard_host)
    ev.update(workpush.event_fields(res))
    refs = ev.get("refs") or {}
    if res.get("status") == "pushed" and res.get("rebased"):
        old = res.get("old_sha") or ""
        given = refs.get("head")
        if (cmd == "deliver" and not given) or (given and old.startswith(given)):
            ev["refs"] = dict(refs, head=res["sha"])  # the rebase rewrote the delivered commit
    return {"push": res}


def build_event(cmd, a):
    actor = {"role": a.role or DEFAULT_ROLE[cmd],
             "handle": a.as_handle or os.environ.get("ORCA_TERMINAL_HANDLE"),
             "workspace": os.environ.get("ORCA_WORKSPACE_ID")}
    if a.model:
        actor["model"] = a.model
    if a.effort:
        actor["effort"] = a.effort
    ev = {"kind": KIND[cmd], "actor": {k: v for k, v in actor.items() if v},
          "summary": a.summary}
    for key in ("line", "node", "milestone", "outcome", "blocker", "by", "title", "spec",
                "source", "result"):
        v = getattr(a, key, None)
        if v:
            ev[key] = v
    if getattr(a, "deps", None) is not None:
        ev["deps"] = [d for d in a.deps.split(",") if d]
    if getattr(a, "require_review", None):
        ev["require_review"] = [d for d in a.require_review.split(",") if d]
    if getattr(a, "expected_minutes", None):
        ev["expected_duration_minutes"] = a.expected_minutes
    if cmd == "dispatch" and (a.model or getattr(a, "agent", None)):
        # R6+R7 (11-07): which worker model ran the node, for the Terra max / DS max comparison
        ev["route"] = {k: v for k, v in (("agent", getattr(a, "agent", None)), ("model", a.model),
                                          ("effort", a.effort), ("on", getattr(a, "on", None))) if v}
    if cmd == "plan":
        ev["node_kind"] = a.kind or core.infer_node_kind(a.node)
        if a.model or a.on:
            ev["route"] = {k: v for k, v in (("agent", a.agent), ("model", a.model),
                                              ("effort", a.effort), ("on", a.on)) if v}
        if a.no_auto:
            ev["auto_dispatch"] = False
        if a.milestones:
            ev["milestones"] = [m for m in a.milestones.split(",") if m]
            ev.pop("milestone", None)
    refs = {}
    for key in ("task", "dispatch", "head", "report", "worktree", "terminal"):
        v = getattr(a, key, None)
        if v:
            refs[key] = v
    if refs:
        ev["refs"] = refs
    if getattr(a, "next_ready", None):
        ev["next_ready"] = [n for n in a.next_ready.split(",") if n]
    for key in ("failures", "candidates"):
        v = getattr(a, key, None)
        if v:
            ev[key] = [x.strip() for x in v if x and x.strip()]
    if getattr(a, "blocked_by", None) and a.blocked_by.strip():
        ev["blocked_by"] = a.blocked_by.strip()
    if getattr(a, "touched", None):
        ev["touched"] = [x.strip() for x in a.touched.split(",") if x.strip()]
    return ev


def build_proposal_event(a, doc):
    """`progress propose FILE`: the file is the proposal (see graph.py); line/parent may come from flags."""
    if not isinstance(doc, dict):
        raise core.ProgressError("bad_input", "提议文件必须是一个 JSON 对象")
    line = a.line or doc.get("line")
    parent = a.node or doc.get("parent")
    actor = {"role": a.role or "router",
             "handle": a.as_handle or os.environ.get("ORCA_TERMINAL_HANDLE"),
             "workspace": os.environ.get("ORCA_WORKSPACE_ID")}
    ev = {"kind": "graph_propose", "actor": {k: v for k, v in actor.items() if v},
          "summary": a.summary or doc.get("summary"), "line": line,
          "proposal": dict(doc, line=line, parent=parent)}
    if parent:
        ev["node"] = parent
    return ev


def check_proposal(pid, ev):
    """--check: run the gate against the local store without writing anything."""
    project = core.load_project(pid)
    state = core.fold(project, core.read_events(pid))
    errs = [e["error"] for e in core.validate(dict(ev, project=pid), project, state)]
    res = graph.gate(ev["proposal"], project, state)
    for e in res["errors"] + (core.same_acceptance_errors(res["normalized"]) if not res["errors"] else []):
        if e not in errs:
            errs.append(e)
    if errs:
        return {"ok": False, "code": "gate_rejected", "error": "子图提议未通过闸门：共 %d 条问题" % len(errs),
                "detail": {"errors": errs}, "warnings": res["warnings"]}
    return {"ok": True, "checked": True, "children": res["children"], "warnings": res["warnings"],
            "segments": [{"id": sg["id"], "mode": sg["mode"], "nodes": [c["id"] for c in sg["nodes"]]}
                         for sg in res["normalized"]["segments"]]}


def _run(argv, timeout=30):
    try:
        r = subprocess.run(argv, text=True, capture_output=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except OSError as exc:
        return 127, "", str(exc)


def _line_states(pid, lines):
    """{line: folded line state} for the given lines, from the local store or the remote one (missing on failure)."""
    out = {}
    if local_store(pid):
        st = core.fold(core.load_project(pid), core.read_events(pid))
        return {l: st["lines"][l] for l in lines if l in st.get("lines", {})}
    host = os.environ.get("PROGRESS_REMOTE", "wsl")
    for l in lines:
        rc, text, _err = _run(["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", host,
                               "python3 %s --project %s state --line %s" % (REMOTE_CLI, shlex.quote(pid), shlex.quote(l))],
                              timeout=60)
        try:
            doc = json.loads(text) if rc == 0 else None
        except ValueError:
            doc = None
        if isinstance(doc, dict) and "nodes" in doc:
            out[l] = doc
    return out


def _spec_text(ref, repo, remote):
    """'branch@path' -> text from the local repo, else the worker host's repo; None when unreadable."""
    if not ref or "@" not in ref:
        return None
    branch, path = ref.split("@", 1)
    if repo:
        rc, out, _ = _run(["git", "-C", repo, "show", "%s:%s" % (branch, path)])
        if rc == 0 and out.strip():
            return out
    if remote:
        rc, out, _ = _run(["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", remote["host"],
                           "git -C %s show %s" % (shlex.quote(remote["repo"]), shlex.quote("%s:%s" % (branch, path)))])
        if rc == 0 and out.strip():
            return out
    return None


def run_precheck(pid, line, node):
    """R1. -> {"applies": bool, "ok": bool, "res": {...}, "summary": str}. Only run/check nodes are checked; nodes
    planned before prereq.SINCE without the section are let through (their specs predate the rule)."""
    states = _line_states(pid, [line])
    ls = states.get(line)
    if ls is None:
        return {"applies": True, "ok": None, "summary": "读不到主线 %s 的状态，前置条件没核对" % line}
    n = (ls.get("nodes") or {}).get(node)
    if not n:
        return {"applies": False, "ok": True, "summary": "节点还没计划"}
    if n.get("kind") not in prereq.CHECKED_KINDS:
        return {"applies": False, "ok": True, "summary": "%s 节点不查前置条件" % n.get("kind")}
    old = (n.get("planned_at") or "") < prereq.SINCE
    rc, top, _ = _run(["git", "rev-parse", "--show-toplevel"])
    repo = top.strip() if rc == 0 and top.strip() else os.environ.get("PROGRESS_REPO")
    remote = None
    if os.environ.get("PROGRESS_REMOTE_REPO"):
        remote = {"host": os.environ.get("PROGRESS_REMOTE", "wsl"), "repo": os.environ["PROGRESS_REMOTE_REPO"]}
    text = _spec_text(n.get("spec"), repo, remote)
    if text is None:
        if old:
            return {"applies": False, "ok": True, "summary": "旧节点没有可读的说明，不查"}
        res = {"format": ["节点没有可读的说明文件（spec %s）" % (n.get("spec") or "未登记")], "missing": [], "unknown": []}
        return {"applies": True, "ok": False, "res": res, "summary": prereq.summary(res)}
    branch = n["spec"].split("@", 1)[0]
    others = sorted({v.split("/", 1)[0] for k, v in prereq.TOKEN_RE.findall(text) if k == "node" and "/" in v} - {line})
    if others:
        states.update(_line_states(pid, others))
    res = prereq.check(text, branch, line, states, repo, _run, remote)
    if old and res["format"]:
        if not res["missing"]:
            return {"applies": False, "ok": True, "res": res, "summary": "旧节点的说明没有可核对的前置条件，不查"}
        res = dict(res, format=[], ok=False)
    return {"applies": True, "ok": res["ok"], "res": res, "summary": prereq.summary(res)}


def prereq_block_event(a, line, node, chk):
    actor = {"role": getattr(a, "role", None) or "router",
             "handle": a.as_handle or os.environ.get("ORCA_TERMINAL_HANDLE"),
             "workspace": os.environ.get("ORCA_WORKSPACE_ID")}
    return {"kind": "node_block", "actor": {k: v for k, v in actor.items() if v}, "line": line, "node": node,
            "blocker": "dep", "summary": chk["summary"], "prereq_missing": chk["res"]["missing"]}


def parser():
    p = argparse.ArgumentParser(prog="progress", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project", default=os.environ.get("PROGRESS_PROJECT", "dse"))
    sub = p.add_subparsers(dest="cmd", required=True)
    for cmd in KIND:
        s = sub.add_parser(cmd)
        s.add_argument("--summary", required=True)
        s.add_argument("--role", choices=sorted(core.ROLES))
        s.add_argument("--as", dest="as_handle")
        s.add_argument("--line")
        s.add_argument("--node")
        s.add_argument("--milestone")
        s.add_argument("--source")
        for key in ("task", "dispatch", "head", "report", "worktree", "terminal"):
            s.add_argument("--" + key)
        s.add_argument("--model")
        s.add_argument("--effort")
        if cmd in ("deliver", "review"):
            s.add_argument("--outcome", required=True, choices=sorted(core.OUTCOMES))
            s.add_argument("--next-ready")
        if cmd == "deliver":
            s.add_argument("--blocked-by", dest="blocked_by",
                           help="with --outcome BLOCKED: what is still missing (budget: the work left)")
            s.add_argument("--blocker", choices=sorted(core.DELIVER_BLOCKERS), help="with --outcome BLOCKED")
            s.add_argument("--result", choices=sorted(core.RESULTS),
                           help="with PASS: the experiment ran but the research result missed the bar")
        if cmd in ("deliver", "block", "heartbeat"):
            s.add_argument("--failure", dest="failures", action="append",
                           help="one distinct failure still unresolved at delivery (test name / error class); repeat for each; omit fixed ones")
            s.add_argument("--candidate", dest="candidates", action="append",
                           help="one independent direction not yet tried that someone else could run in parallel; repeat for each; omit ones already run")
            s.add_argument("--touched", help="comma list of paths this node actually changed")
        if cmd == "block":
            s.add_argument("--blocker", required=True, choices=sorted(core.BLOCKERS))
        if cmd == "supersede":
            s.add_argument("--by", required=True)
        if cmd == "dispatch":
            s.add_argument("--agent", help="worker agent (codex / dsh); with --model/--effort it is recorded as the route")
            s.add_argument("--on")
        if cmd == "plan":
            s.add_argument("--title")
            s.add_argument("--deps", default="")
            s.add_argument("--require-review")
            s.add_argument("--spec", help="branch@path of the node spec")
            s.add_argument("--agent", default="codex")
            s.add_argument("--on")
            s.add_argument("--expected-minutes", type=int)
            s.add_argument("--kind", choices=list(core.NODE_KINDS_RUN),
                           help="run (execute/develop), review/check (read and judge), plan (sub-router), decide; "
                                "default: review if the node id contains review/recheck/audit, else run")
            s.add_argument("--milestones", help="comma list of milestone ids this node serves")
            s.add_argument("--no-auto", action="store_true", help="patrol must not dispatch it")
    s = sub.add_parser("precheck", help="R1: check a node spec's 前置条件; missing inputs -> node_block blocker=dep")
    s.add_argument("--line", required=True)
    s.add_argument("--node", required=True)
    s.add_argument("--as", dest="as_handle")
    s.add_argument("--role", choices=["router", "worker", "patrol"], default="router")
    s.add_argument("--dry-run", action="store_true", help="only print, never write node_block")
    s = sub.add_parser("propose", help="commit a sub-graph proposal (JSON file) after the deterministic gate")
    s.add_argument("file", help="proposal JSON (see autotaskgen/graph.py); '-' reads stdin")
    s.add_argument("--line", help="overrides the file's line")
    s.add_argument("--node", help="parent plan node; overrides the file's parent")
    s.add_argument("--summary", help="overrides the file's summary")
    s.add_argument("--role", choices=["router", "coordinator", "human"])
    s.add_argument("--as", dest="as_handle")
    s.add_argument("--check", action="store_true", help="only run the gate, write nothing (store host only)")
    s = sub.add_parser("state")
    s.add_argument("--line")
    sub.add_parser("project-show")
    s = sub.add_parser("project-init")
    s.add_argument("file")
    sub.add_parser("append-raw", help="read one event JSON from stdin (used by forwarding)")
    s = sub.add_parser("live-put", help="read a live.json document from stdin")
    sub.add_parser("supervisor-put", help="read a supervisor report from stdin (latest.json + dated copy)")
    sub.add_parser("flush")
    s = sub.add_parser("milestones-set", help="coordinator: replace the milestones of one core from a JSON list file")
    s.add_argument("--core", required=True)
    s.add_argument("--summary", default="更新关键节点")
    s.add_argument("--as", dest="as_handle")
    s.add_argument("file", nargs="?", help="JSON list of milestones; omit when piping (milestones-put)")
    s = sub.add_parser("milestones-put", help="internal: read {core, milestones, actor, summary} from stdin")
    s = sub.add_parser("goal-set", help="coordinator/human (or --maintainer with --why): replace final_goal.text/done_when from a JSON file")
    s.add_argument("file", help='JSON {"text": ..., "done_when": ...}')
    s.add_argument("--summary", default="更新最终目标与完成标准")
    s.add_argument("--maintainer", action="store_true", help="framework maintainer writing on someone's recorded request")
    s.add_argument("--why", help="with --maintainer: who asked and where (kept in the event)")
    s.add_argument("--as", dest="as_handle")
    sub.add_parser("goal-put", help="internal: read {final_goal, actor, summary, why} from stdin")
    sub.add_parser("guard-scan", help="internal (guard host): run the publish guard for workpush on stdin JSON")
    return p


def apply_milestones(pid, payload):
    """Replace one core's milestones; only that core's coordinator or a declared human may do it."""
    project = core.load_project(pid)
    cid = payload.get("core")
    c = core.core_of(project, cid)
    if not c:
        raise core.ProgressError("unknown_core", "核心未声明：%r" % cid)
    handle = (payload.get("actor") or {}).get("handle")
    is_coord = core._same(handle, c.get("coordinator"))
    is_human = any(core._same(handle, h) for h in core.human_handles(project))
    if not (is_coord or is_human):
        raise core.ProgressError("not_coordinator", "只有 %s 的协调人能改它的关键节点" % cid)
    ms = payload.get("milestones")
    if not isinstance(ms, list):
        raise core.ProgressError("bad_input", "milestones 必须是列表")
    for m in ms:
        if not isinstance(m, dict) or m.get("core", cid) != cid:
            raise core.ProgressError("wrong_core", "只能写本核心 %s 的关键节点" % cid)
        m["core"] = cid
    keep = [m for m in project.get("milestones") or [] if m.get("core") != cid]
    project["milestones"] = keep + ms
    core.save_project(project)  # validates ids, lines, after
    core.append(pid, {"kind": "project_update", "summary": (payload.get("summary") or "更新关键节点")[:120],
                      "actor": {"role": "coordinator" if is_coord else "human", "handle": handle},
                      "refs": {"core": cid, "milestones": [m.get("id") for m in ms]}})
    return {"ok": True, "core": cid, "milestones": [m.get("id") for m in ms]}


def apply_goal(pid, payload):
    """Replace final_goal.text / done_when. A core coordinator or a declared human may; a framework maintainer may
    when it records who asked (why). Writes a project_update event."""
    project = core.load_project(pid)
    handle = (payload.get("actor") or {}).get("handle")
    is_coord = any(core._same(handle, c.get("coordinator")) for c in project.get("cores") or [])
    is_human = any(core._same(handle, h) for h in core.human_handles(project))
    maint = bool(payload.get("maintainer"))
    if not (is_coord or is_human or maint):
        raise core.ProgressError("not_allowed", "只有协调人、声明的人类或带 --why 的框架维护者能改最终目标")
    why = str(payload.get("why") or "").strip()
    if maint and not (is_coord or is_human) and not why:
        raise core.ProgressError("bad_input", "--maintainer 必须用 --why 写明是谁要求的")
    fg = payload.get("final_goal")
    if not isinstance(fg, dict) or not isinstance(fg.get("text"), str) or not fg.get("text").strip():
        raise core.ProgressError("bad_input", "final_goal.text 不能为空")
    dw = fg.get("done_when")
    if not (isinstance(dw, str) and dw.strip()) and not (isinstance(dw, list) and dw and all(isinstance(x, str) and x.strip() for x in dw)):
        raise core.ProgressError("bad_input", "final_goal.done_when 必须是非空文字或文字列表")
    old = dict(project.get("final_goal") or {})
    project["final_goal"] = dict(old, text=fg["text"].strip(), done_when=dw)
    core.save_project(project)
    role = "coordinator" if is_coord else "human" if is_human else "maintainer"
    refs = {"field": "final_goal", "fields": ["text", "done_when"]}
    if why:
        refs["why"] = why[:300]
    core.append(pid, {"kind": "project_update", "summary": (payload.get("summary") or "更新最终目标与完成标准")[:120],
                      "actor": {"role": role, "handle": handle}, "refs": refs})
    return {"ok": True, "role": role, "final_goal": project["final_goal"]}


def main(argv=None):
    a = parser().parse_args(argv)
    pid = a.project
    try:
        if a.cmd == "append-raw":
            ev = json.loads(sys.stdin.read())
            return emit(core.append(ev.get("project") or pid, ev))
        if a.cmd == "live-put":
            core.save_live(pid, json.loads(sys.stdin.read()))
            return emit({"ok": True})
        if a.cmd == "supervisor-put":
            core.save_supervisor(pid, json.loads(sys.stdin.read()))
            return emit({"ok": True})
        if a.cmd == "flush":
            return emit({"ok": True, "sent": flush_outbox(pid)})
        if a.cmd == "goal-put":
            return emit(apply_goal(pid, json.loads(sys.stdin.read())))
        if a.cmd == "goal-set":
            payload = {"summary": a.summary, "maintainer": a.maintainer, "why": a.why,
                       "final_goal": json.loads(pathlib.Path(a.file).read_text(encoding="utf-8")),
                       "actor": {"handle": a.as_handle or os.environ.get("ORCA_TERMINAL_HANDLE")}}
            if local_store(pid):
                return emit(apply_goal(pid, payload))
            host = os.environ.get("PROGRESS_REMOTE", "wsl")
            r = subprocess.run(["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", host,
                                "python3 %s --project %s goal-put" % (REMOTE_CLI, shlex.quote(pid))],
                               input=json.dumps(payload, ensure_ascii=False), text=True,
                               capture_output=True, timeout=60)
            sys.stdout.write(r.stdout or json.dumps({"ok": False, "code": "remote", "error": r.stderr[:300]}) + "\n")
            return 0 if '"ok": true' in (r.stdout or "") else 2
        if a.cmd == "milestones-put":
            return emit(apply_milestones(pid, json.loads(sys.stdin.read())))
        if a.cmd == "milestones-set":
            payload = {"core": a.core, "summary": a.summary,
                       "milestones": json.loads(pathlib.Path(a.file).read_text(encoding="utf-8")),
                       "actor": {"handle": a.as_handle or os.environ.get("ORCA_TERMINAL_HANDLE")}}
            if local_store(pid):
                return emit(apply_milestones(pid, payload))
            host = os.environ.get("PROGRESS_REMOTE", "wsl")
            r = subprocess.run(["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", host,
                                "python3 %s --project %s milestones-put" % (REMOTE_CLI, shlex.quote(pid))],
                               input=json.dumps(payload, ensure_ascii=False), text=True,
                               capture_output=True, timeout=60)
            sys.stdout.write(r.stdout or json.dumps({"ok": False, "code": "remote", "error": r.stderr[:300]}) + "\n")
            return 0 if '"ok": true' in (r.stdout or "") else 2
        if a.cmd == "project-init" and not local_store(pid) and os.environ.get("PROGRESS_LOCAL") != "1":
            return emit({"ok": False, "code": "not_store_host",
                         "error": "这台机器不是存储主机；在 WSL 上运行，或设 PROGRESS_LOCAL=1 新建本地存储"}, 2)
        if a.cmd == "project-init":
            doc = json.loads(pathlib.Path(a.file).read_text(encoding="utf-8"))
            core.save_project(doc)
            return emit({"ok": True, "project": doc["id"]})
        if a.cmd == "propose":
            raw = sys.stdin.read() if a.file == "-" else pathlib.Path(a.file).read_text(encoding="utf-8")
            ev = build_proposal_event(a, json.loads(raw))
            if a.check:
                if not local_store(pid):
                    return emit({"ok": False, "code": "not_store_host",
                                 "error": "--check 只能在存储主机上跑；直接提交也会先过闸门，不合格不会写入"}, 2)
                res = check_proposal(pid, ev)
                return emit(res, 0 if res["ok"] else 2)
            return submit(pid, ev)
        if a.cmd in ("state", "project-show"):
            if not local_store(pid):
                host = os.environ.get("PROGRESS_REMOTE", "wsl")
                extra = " --line %s" % shlex.quote(a.line) if getattr(a, "line", None) else ""
                r = subprocess.run(["ssh", "-o", "ConnectTimeout=8", host,
                                    "python3 %s --project %s %s%s" % (REMOTE_CLI, shlex.quote(pid), a.cmd, extra)],
                                   text=True, capture_output=True, timeout=60)
                sys.stdout.write(r.stdout)
                return r.returncode
            project = core.load_project(pid)
            if a.cmd == "project-show":
                return emit(project)
            st = core.fold(project, core.read_events(pid))
            if a.line:
                return emit(st["lines"].get(a.line) or {"ok": False, "code": "unknown_line"})
            st.pop("feed", None)
            return emit(st)
        if a.cmd == "guard-scan":
            return emit(workpush.serve_guard_scan(json.loads(sys.stdin.read())))
        if a.cmd == "precheck":
            chk = run_precheck(pid, a.line, a.node)
            missing = bool(chk.get("res") and chk["res"].get("missing") and not chk["res"].get("format"))
            if missing and not a.dry_run:
                submit(pid, prereq_block_event(a, a.line, a.node, chk),
                       {"ok": False, "precheck": "missing", "detail": chk.get("res"),
                        "hint": "已记 node_block dep；缺的输入出现后巡检会自动派（节点要有 spec 和 route），没有 route 的会提醒你派"})
                return 2
            return emit(dict(chk, precheck="ok" if chk["ok"] else "fail"), 0 if chk["ok"] else 2)
        ev = build_event(a.cmd, a)
        if a.cmd == "dispatch" and (a.role or "router") == "router" and os.environ.get("PROGRESS_PRECHECK") != "0":
            chk = run_precheck(pid, a.line, a.node)
            if chk["applies"]:
                ev["prereq"] = {"ok": chk["ok"], "summary": chk["summary"]}
            res = chk.get("res") or {}
            if chk["applies"] and res.get("format"):
                return emit({"ok": False, "code": "prereq_format", "error": chk["summary"] + "。补好说明的「前置条件」一节"
                             "（每项带 `node:…` / `sha:…` / `file:…`）、重新 progress plan 后再派；已经起了 worker 就先停掉它。",
                             "detail": res}, 2)
            if chk["applies"] and res.get("missing"):
                submit(pid, prereq_block_event(a, a.line, a.node, chk),
                       {"ok": False, "dispatch_refused": True, "code": "prereq_missing",
                               "error": chk["summary"] + "。没有记派出，已改记 node_block dep；已经起了 worker 就先停掉它，"
                               "缺的输入出现后巡检会自动派。下次先跑 progress precheck 再起 worker。"})
                return 2
        return submit(pid, ev, push_step(a.cmd, a, pid, ev))
    except core.ProgressError as exc:
        return emit(exc.as_dict(), 2)
    except (ValueError, OSError) as exc:
        return emit({"ok": False, "code": "bad_input", "error": str(exc)[:300]}, 2)


if __name__ == "__main__":
    sys.exit(main())
