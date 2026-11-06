#!/usr/bin/env python3
"""progress-inspect: read-only look-up tool for the supervisor model (and humans).

  progress-inspect screen <terminal handle | dispatch id>
  progress-inspect events --line X [--node Y] [--since ISO]
  progress-inspect node --line X --node Y
  progress-inspect file <path>              only under PROGRESS_REPO or a live worker worktree root; <= 200 KB
  progress-inspect git-log <worktree> [-n N]
  progress-inspect tasks --run R
  progress-inspect resume --line L [--node N] [--max-chars 12000]   接续包：从事件记录确定性生成（不调模型），markdown

Every call is appended as one JSON line to $PROGRESS_INSPECT_LOG (when set). Anything else is rejected.
Project: $PROGRESS_PROJECT (default dse). Nothing here writes to Orca, git or the progress store.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import pathlib
import posixpath
import shlex
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import progress_core as core  # noqa: E402
import snapshot as snap  # noqa: E402

FILE_CAP = 200 * 1024
EVENT_CAP = 60
COMMANDS = ("screen", "events", "node", "file", "git-log", "tasks", "resume")
RESUME_MAX = 12000
LESSON_LINES = 40
DONE_STATES = ("delivered", "reviewed", "superseded", "cancelled")
STATE_ZH = {"planned": "已排", "ready": "就绪", "running": "在跑", "dispatched": "已派", "blocked": "卡住",
            "failed": "失败", "delivered": "已交付", "reviewed": "已审", "superseded": "已取代", "cancelled": "已取消"}


class Reject(Exception):
    pass


def _repo() -> str:
    return os.environ.get("PROGRESS_REPO", os.path.expanduser("~/project/repo"))


def _is_remote(path: str) -> bool:
    return path.startswith("/home/")


def worktree_roots(live: Optional[Dict[str, Any]]) -> List[str]:
    """PROGRESS_REPO plus the worktree path of every live worker ("id::/path" workspaces)."""
    roots = [_repo()]
    for w in (live or {}).get("workers") or []:
        ws = w.get("workspace")
        if isinstance(ws, str) and "::" in ws:
            roots.append(ws.split("::", 1)[1])
    return [posixpath.normpath(r) for r in roots if r.startswith("/")]


def check_path(path: str, live: Optional[Dict[str, Any]]) -> str:
    """-> normalized absolute path inside an allowed root, else Reject (no .., no relative, no NUL)."""
    if not path or "\0" in path or not path.startswith("/"):
        raise Reject("路径必须是绝对路径")
    if ".." in path.split("/"):
        raise Reject("路径含 ..，拒绝")
    norm = posixpath.normpath(path)
    for r in worktree_roots(live):
        if norm == r or norm.startswith(r.rstrip("/") + "/"):
            return norm
    raise Reject("路径不在允许范围（PROGRESS_REPO 或在跑 worker 的 worktree）：%s" % path)


# ------------------------------------------------------------------ resume pack (接续包)

def _t(text: Any, n: int = 160) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _refs_text(refs: Any) -> str:
    if not isinstance(refs, dict) or not refs:
        return ""
    return "，".join("%s=%s" % (k, _t(v, 80)) for k, v in sorted(refs.items()) if v)


def read_lessons(pid: str, line: str) -> List[Tuple[str, List[str]]]:
    """[(file label, first 40 lines)] of memory/<line>/LESSONS.md and memory/GLOBAL.md; absent files are skipped."""
    out = []
    base = core.home() / pid / "memory"
    for label, path in (("memory/%s/LESSONS.md" % line, base / line / "LESSONS.md"), ("memory/GLOBAL.md", base / "GLOBAL.md")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[:LESSON_LINES]
        except (OSError, ValueError):
            continue
        if any(l.strip() for l in lines):
            out.append((label, lines))
    return out


def open_followups(pid: str, line: str) -> List[Dict[str, Any]]:
    """Open supervisor follow-ups of this line (the same ledger alerts/patrol read: task cache `supervisor:<pid>`)."""
    try:
        rows = list(((snap.load_task_cache().get("supervisor:%s" % pid) or {}).get("followups") or {}).values())
    except Exception:  # noqa: BLE001
        return []
    return sorted((e for e in rows if isinstance(e, dict) and not e.get("closed_at") and e.get("line") == line),
                  key=lambda e: e.get("created_at") or "")


def build_resume(project: Dict[str, Any], events: List[Dict[str, Any]], line: str, node: Optional[str] = None,
                 max_chars: int = RESUME_MAX, now: Optional[str] = None,
                 lessons: Optional[List[Tuple[str, List[str]]]] = None,
                 followups: Optional[List[Dict[str, Any]]] = None) -> str:
    """Plain-Chinese markdown resume pack for one line (optionally one node first). Pure: no I/O, no model."""
    now = now or core.now_iso()
    state = core.fold(project, events, at=now)
    ls = state["lines"].get(line) or {}
    nodes = ls.get("nodes") or {}
    decl = core.line_of(project, line) or {}
    rows = sorted((e for e in events if e.get("line") == line), key=lambda e: e.get("at") or "")
    secs: List[Tuple[str, List[str]]] = []

    if node:
        n = nodes.get(node)
        items = []
        if n is None:
            items.append("没有节点 %s" % node)
        else:
            items.append("状态：%s；任务：%s" % (STATE_ZH.get(n.get("status"), n.get("status")), _t(n.get("title") or node)))
            items.append("spec：%s" % (n.get("spec") or "无"))
            if n.get("worker"):
                items.append("worker：%s" % n["worker"])
            for d in n.get("deps") or []:
                dn = nodes.get(d) or {}
                rf = dn.get("refs") or {}
                items.append("依赖 %s：%s%s%s" % (d, STATE_ZH.get(dn.get("status"), dn.get("status") or "未知"),
                                                 "，结果 %s" % dn["outcome"] if dn.get("outcome") else "",
                                                 "，报告 %s，head %s" % (rf.get("report") or "-", rf.get("head") or "-")
                                                 if dn.get("delivered_at") else ""))
            items.append("全部事件（按时间）：")
            for e in rows:
                if e.get("node") == node:
                    items.append("  - %s %s%s %s%s" % (e.get("at"), e.get("kind"),
                                                       "/" + e["outcome"] if e.get("outcome") else "", _t(e.get("summary"), 200),
                                                       "（%s）" % _refs_text(e.get("refs")) if _refs_text(e.get("refs")) else ""))
        secs.append(("重点节点 %s" % node, items))

    goal = project.get("final_goal")
    goal_text = goal.get("text") if isinstance(goal, dict) else goal
    items = [_t(goal_text or "（未写）", 600)]
    for m in project.get("milestones") or []:
        if line in (m.get("lines") or []):
            items.append("- 关键节点 %s %s：%s" % (m["id"], _t(m.get("title"), 80),
                                                 (state["milestones"].get(m["id"]) or {}).get("status", "open")))
    secs.append(("最终目标与本线关键节点", items))

    items = []
    for nid, n in sorted(nodes.items()):
        if n.get("status") in DONE_STATES:
            continue
        s = "- `%s`（%s）%s" % (nid, STATE_ZH.get(n.get("status"), n.get("status")), _t(n.get("title") or nid, 100))
        if n.get("status") == "blocked":
            s += " | 卡点 %s：%s" % (n.get("blocker") or "?", _t(n.get("summary"), 160))
        s += " | worker %s | 最近事件 %s" % (n.get("worker") or "-", n.get("updated_at") or "-")
        items.append(s)
    secs.append(("未完成节点", items or ["无"]))

    rul = []
    for e in rows:
        if e.get("kind") in ("decision", "human_reply", "line_open"):
            rul.append("- %s %s%s：%s" % (e.get("at"), e["kind"], "/" + str(e["result"]) if e.get("result") else "",
                                         _t(e.get("summary"), 200)))
    for e in sorted((e for e in events if e.get("kind") == "project_update" and e.get("line") in (None, line)),
                    key=lambda e: e.get("at") or ""):
        rul.append("- %s project_update：%s" % (e.get("at"), _t(e.get("summary"), 200)))
    rul.sort(key=lambda s: s[2:22])
    secs.append(("最近裁决与用户拍板", rul[-10:] or ["无"]))

    fus = ["- [%s] 发给 %s：%s | 落实标准：%s | 到期 %s" % (f.get("id"), f.get("to") or "?", _t(f.get("text"), 200),
                                                      _t(f.get("done_when"), 100) or "-", f.get("check_at") or "-")
           for f in (followups or [])]
    secs.append(("未落实的跟进", fus or ["无"]))

    dels = []
    for e in rows:
        if e.get("kind") == "node_deliver":
            rf = e.get("refs") or {}
            dels.append("- %s %s %s：%s | 报告 %s | head %s" % (e.get("at"), e.get("node"), e.get("outcome") or "?",
                                                              _t(e.get("summary"), 160), rf.get("report") or "-", rf.get("head") or "-"))
    secs.append(("最近交付", dels[-10:] or ["无"]))

    if lessons:
        items = []
        for label, ll in lessons:
            items.append("（%s）" % label)
            items.extend(ll)
        secs.append(("经验", items))

    def render() -> str:
        out = ["# 接续包：%s（%s）" % (decl.get("name") or line, line), "生成于 %s。只读事件记录生成，不含模型判断。" % now]
        for title, its in secs:
            out += ["", "## " + title] + its
        return "\n".join(out) + "\n"

    text = render()
    for victim in ("最近交付", "最近裁决与用户拍板"):
        items = next(i for t, i in secs if t == victim)
        cut = "无（已按长度裁掉）"
        while len(text) > max_chars and items and items[0] not in ("无", cut):
            items.pop(0)
            if not items:
                items.append(cut)
            text = render()
    if len(text) > max_chars:
        text = text[: max(0, max_chars - 12)] + "\n…（已截断）\n"
    return text


class Inspect:
    def __init__(self, pid: str, runner=snap.default_runner, store: Optional[snap.Store] = None):
        self.pid = pid
        self.runner = runner
        self.store = store or snap.Store(pid, runner)
        self._data = None

    def data(self):
        if self._data is None:
            self._data = self.store.read()
        return self._data

    # -- subcommands
    def screen(self, target: str) -> Dict[str, Any]:
        _p, _e, live = self.data()
        if target.startswith("term_"):
            tail = snap.read_terminal_tail(self.runner, target, live, 60)
        elif target.startswith("ctx_"):
            tail = snap.read_worker_tail(self.runner, target, 60)
        else:
            raise Reject("screen 只接受 term_… 终端句柄或 ctx_… dispatch id")
        if tail is None:
            return {"ok": False, "error": "读不到屏幕"}
        return {"ok": True, "target": target, "tail": [l.rstrip() for l in tail if l.strip()][-60:]}

    def events(self, line: str, node: Optional[str], since: Optional[str]) -> Dict[str, Any]:
        _p, events, _l = self.data()
        if since and core.parse_iso(since) is None:
            raise Reject("--since 不是 ISO 时间")
        rows = [e for e in events if e.get("line") == line and (not node or e.get("node") == node)
                and (not since or (e.get("at") or "") >= since)]
        rows.sort(key=lambda e: e.get("at") or "")
        out = [{k: e.get(k) for k in ("at", "kind", "node", "outcome", "reason", "summary", "refs") if e.get(k) is not None}
               | {"actor": (e.get("actor") or {}).get("role")} for e in rows[-EVENT_CAP:]]
        return {"ok": True, "line": line, "total": len(rows), "events": out}

    def node(self, line: str, nid: str) -> Dict[str, Any]:
        project, events, _l = self.data()
        state = core.fold(project, events, at=snap.iso(snap.utcnow()))
        n = ((state["lines"].get(line) or {}).get("nodes") or {}).get(nid)
        if n is None:
            return {"ok": False, "error": "没有这个节点"}
        keep = {k: n.get(k) for k in ("id", "title", "status", "kind", "deps", "outcome", "review", "blocker", "refs",
                                       "dispatched_at", "delivered_at", "reviewed_at", "expected_duration_minutes",
                                       "worker", "worktree", "model", "summary", "spec", "parent", "segment", "segment_mode",
                                       "write_scope", "budget", "acceptance", "failures", "candidates", "touched")
                if n.get(k) is not None}
        keep["history"] = (n.get("history") or [])[-8:]
        out = {"ok": True, "line": line, "node": keep}
        if n.get("spec"):
            import patrol  # local import: patrol imports this module's siblings only lazily
            spec = patrol.read_spec(n["spec"], patrol.repo_path(project, core.line_of(project, line) or {}), self.runner)
            out["spec_header"] = spec.get("header") if not spec.get("error") else {"error": spec["error"]}
        return out

    def resume(self, line: str, node: Optional[str], max_chars: int) -> Dict[str, Any]:
        project, events, _l = self.data()
        if core.line_of(project, line) is None:
            return {"ok": False, "error": "没有这条主线：%s" % line}
        text = build_resume(project, events, line, node, max_chars, now=snap.iso(snap.utcnow()),
                            lessons=read_lessons(self.pid, line), followups=open_followups(self.pid, line))
        return {"ok": True, "line": line, "text": text}

    def file(self, path: str) -> Dict[str, Any]:
        _p, _e, live = self.data()
        norm = check_path(path, live)
        if _is_remote(norm):
            roots = [r for r in worktree_roots(live) if _is_remote(r)]
            cases = "".join("%s) ok=1;; " % ("%s|%s/*" % (shlex.quote(r), shlex.quote(r.rstrip("/")))) for r in roots)
            cmd = ("p=$(realpath -e -- %s) || exit 4; ok=0; case \"$p\" in %s*) ;; esac; "
                   "[ \"$ok\" = 1 ] || exit 3; [ -f \"$p\" ] || exit 5; head -c %d -- \"$p\"" % (shlex.quote(norm), cases, FILE_CAP + 1))
            rc, out, err = self.runner(snap.ssh_argv(snap.remote_host(), cmd), timeout=60)
            if rc == 3:
                raise Reject("路径解析后越出允许范围")
            if rc != 0:
                return {"ok": False, "error": "读不到文件 rc=%s %s" % (rc, (err or "")[:100])}
            text = out
        else:
            real = os.path.realpath(norm)
            if not any(real == os.path.realpath(r) or real.startswith(os.path.realpath(r).rstrip("/") + "/")
                       for r in worktree_roots(live)):
                raise Reject("路径解析后越出允许范围")
            try:
                with open(real, "rb") as fh:
                    text = fh.read(FILE_CAP + 1).decode("utf-8", "replace")
            except OSError as exc:
                return {"ok": False, "error": "读不到文件：%s" % exc.strerror}
        trunc = len(text.encode("utf-8", "replace")) > FILE_CAP
        return {"ok": True, "path": norm, "truncated": trunc, "text": text[:FILE_CAP]}

    def git_log(self, worktree: str, n: int) -> Dict[str, Any]:
        _p, _e, live = self.data()
        norm = check_path(worktree, live)
        n = max(1, min(int(n), 50))
        fmt = "%h %cI %an %s"
        if _is_remote(norm):
            rc, out, err = self.runner(snap.ssh_argv(snap.remote_host(), "git -C %s log -n %d --format=%s"
                                                     % (shlex.quote(norm), n, shlex.quote(fmt))), timeout=60)
        else:
            rc, out, err = self.runner(["git", "-C", norm, "log", "-n", str(n), "--format=" + fmt], timeout=60)
        if rc != 0:
            return {"ok": False, "error": (err or "git log 失败")[:200]}
        return {"ok": True, "worktree": norm, "commits": out.strip().splitlines()}

    def tasks(self, run: str) -> Dict[str, Any]:
        if not run.startswith("run_") or not run[4:].isalnum():
            raise Reject("--run 必须是 run_… 形式")
        rc, out, err = self.runner([snap.orca_bin(), "orchestration", "worker-list", "--full", "--run", run, "--json"],
                                   timeout=60)
        doc = snap.parse_json_tail(out) if rc == 0 else None
        res = doc.get("result") if isinstance(doc, dict) else None
        rows = res.get("workers") if isinstance(res, dict) else None
        if not isinstance(rows, list):
            return {"ok": False, "error": "worker-list 失败 rc=%s %s" % (rc, (err or "")[:100])}
        keep = ("dispatchId", "taskId", "workerState", "dispatchStatus", "agentTerminalHandle", "terminalState")
        return {"ok": True, "run": run, "tasks": [{k: r.get(k) for k in keep if r.get(k) is not None} for r in rows[:80]]}


def log_call(argv: List[str], ok: bool, note: str = "") -> None:
    path = os.environ.get("PROGRESS_INSPECT_LOG")
    if not path:
        return
    rec = {"at": core.now_iso(), "argv": argv, "ok": ok}
    if note:
        rec["note"] = note[:200]
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="progress-inspect", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project", default=os.environ.get("PROGRESS_PROJECT", "dse"))
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("screen").add_argument("target")
    e = sub.add_parser("events")
    e.add_argument("--line", required=True)
    e.add_argument("--node")
    e.add_argument("--since")
    n = sub.add_parser("node")
    n.add_argument("--line", required=True)
    n.add_argument("--node", required=True)
    sub.add_parser("file").add_argument("path")
    g = sub.add_parser("git-log")
    g.add_argument("worktree")
    g.add_argument("-n", type=int, default=10)
    sub.add_parser("tasks").add_argument("--run", required=True)
    r = sub.add_parser("resume")
    r.add_argument("--line", required=True)
    r.add_argument("--node")
    r.add_argument("--max-chars", type=int, default=RESUME_MAX)
    return p


def run(argv: List[str], runner=snap.default_runner, store: Optional[snap.Store] = None) -> Tuple[int, Dict[str, Any]]:
    parser = build_parser()
    try:
        import io
        import contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(err):
            a = parser.parse_args(argv)
    except SystemExit:
        log_call(argv, False, "rejected: usage")
        return 2, {"ok": False, "error": "不在白名单内的用法；可用：%s" % ", ".join(COMMANDS)}
    if a.cmd not in COMMANDS:
        log_call(argv, False, "rejected: no command")
        return 2, {"ok": False, "error": "需要子命令：%s" % ", ".join(COMMANDS)}
    try:
        ins = Inspect(a.project, runner, store)
        if a.cmd == "screen":
            res = ins.screen(a.target)
        elif a.cmd == "events":
            res = ins.events(a.line, a.node, a.since)
        elif a.cmd == "node":
            res = ins.node(a.line, a.node)
        elif a.cmd == "file":
            res = ins.file(a.path)
        elif a.cmd == "git-log":
            res = ins.git_log(a.worktree, a.n)
        elif a.cmd == "resume":
            res = ins.resume(a.line, a.node, max(500, a.max_chars))
        else:
            res = ins.tasks(a.run)
    except Reject as exc:
        log_call(argv, False, "rejected: %s" % exc)
        return 2, {"ok": False, "error": str(exc)}
    except core.ProgressError as exc:
        log_call(argv, False, "error: %s" % exc)
        return 1, exc.as_dict()
    log_call(argv, bool(res.get("ok")))
    return (0 if res.get("ok") else 1), res


def main(argv=None) -> int:
    rc, res = run(list(sys.argv[1:] if argv is None else argv))
    if rc == 0 and isinstance(res.get("text"), str) and "resume" in (sys.argv[1:] if argv is None else argv):
        print(res["text"])
        return rc
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return rc


if __name__ == "__main__":
    sys.exit(main())
