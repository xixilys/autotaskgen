#!/usr/bin/env python3
"""Progress board web service: JSON API, server-sent change events and the static board.

  python3 server.py [--port 8790] [--host 127.0.0.1]

Reads $PROGRESS_HOME (default ~/.progress). Standard library only; Python 3.9+.
The frontend uses relative URLs so the service also works behind a path prefix
(for example a proxy that maps /board/* here and strips /board).

Endpoints
  GET /api/projects                    every registered project with top-level progress
  GET /api/p/<id>/state                declaration + folded state + live snapshot + staleness
  GET /api/p/<id>/events?since=&limit= raw events after `since` (ISO time), oldest first
  GET /api/stream                      SSE: `event: change` {project} when a store file changes
  GET /<path>                          static files from web/
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import mimetypes
import os
import pathlib
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import progress_core as core  # noqa: E402

WEB = HERE / "web"
LIVE_STALE_SECONDS = 30 * 60
SUPERVISOR_STALE_SECONDS = 2 * 3600
EVENTS_LIMIT_DEFAULT = 200
EVENTS_LIMIT_MAX = 5000
POLL_SECONDS = 2.0
KEEPALIVE_SECONDS = 15.0


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def live_view(pid: str, now: Optional[_dt.datetime] = None) -> Dict[str, Any]:
    """Live snapshot plus staleness. A stale or missing snapshot is never passed on as current."""
    now = now or _now()
    doc = core.load_live(pid)
    gen = core.parse_iso(doc.get("generated_at")) if isinstance(doc, dict) else None
    if not isinstance(doc, dict) or gen is None:
        return {"live": None, "live_stale": True, "live_at": None, "live_age_s": None,
                "live_reason": "missing" if doc is None else "no_time"}
    age = (now - gen).total_seconds()
    if age > LIVE_STALE_SECONDS:
        return {"live": None, "live_stale": True, "live_at": doc.get("generated_at"),
                "live_age_s": int(age), "live_reason": "stale"}
    return {"live": doc, "live_stale": False, "live_at": doc.get("generated_at"),
            "live_age_s": int(age), "live_reason": None}


def _load(pid: str) -> Dict[str, Any]:
    try:
        return core.load_project(pid)
    except core.ProgressError as exc:
        status = 404 if exc.code == "no_project" else 500
        raise ApiError(status, exc.code, exc.message)


def _check_pid(pid: str) -> None:
    try:
        core.project_dir(pid)
    except core.ProgressError as exc:
        raise ApiError(400, exc.code, exc.message)
    if pid not in core.list_projects():
        raise ApiError(404, "no_project", "项目未注册：%s" % pid)


def project_summary(pid: str) -> Dict[str, Any]:
    doc = core.load_project(pid)
    st = core.fold(doc, core.read_events(pid))
    lv = live_view(pid)
    live = lv["live"]
    workers = None
    if live is not None and isinstance(live.get("workers"), list):
        workers = [{"model": w.get("model"), "line": w.get("line"), "host": w.get("host"),
                    "router": w.get("router"), "core": w.get("core"), "phase": core.worker_phase(w)}
                   for w in live["workers"] if isinstance(w, dict)]
    ms = []
    for m in doc.get("milestones") or []:
        ms.append({"id": m.get("id"), "title": m.get("title"), "core": m.get("core"),
                   "after": list(m.get("after") or []),
                   "status": (st["milestones"].get(m.get("id")) or {}).get("status")})
    last = st["feed"][0].get("at") if st["feed"] else None
    return {
        "id": doc.get("id") or pid, "name": doc.get("name"), "final_goal": doc.get("final_goal"),
        "progress": st["progress"], "milestones": ms,
        "lines": len(doc.get("lines") or []),
        "cores": [{"id": c.get("id"), "name": c.get("name")} for c in doc.get("cores") or []],
        "running_nodes": sum((ln.get("counts") or {}).get("running", 0) for ln in st["lines"].values()),
        "running_workers": None if workers is None else sum(1 for w in workers if w["phase"] == "running"),
        "lingering_workers": None if workers is None else sum(1 for w in workers if w["phase"] == "lingering"),
        "idle_workers": None if workers is None else sum(1 for w in workers if w["phase"] == "idle"),
        "workers": workers,
        "live_at": lv["live_at"], "live_stale": lv["live_stale"],
        "last_event_at": last, "event_count": st["event_count"],
    }


def api_projects() -> Dict[str, Any]:
    out = []
    for pid in core.list_projects():
        try:
            out.append(project_summary(pid))
        except core.ProgressError as exc:
            out.append({"id": pid, "error": exc.as_dict()})
    return {"ok": True, "now": core.now_iso(), "projects": out}


def api_state(pid: str) -> Dict[str, Any]:
    _check_pid(pid)
    doc = _load(pid)
    st = core.fold(doc, core.read_events(pid))
    res = {"ok": True, "now": core.now_iso(), "project": doc, "state": st}
    res.update(live_view(pid))
    res["lead"] = api_lead(pid)   # project lead status (lead/status.json); {"empty": true} until the lead writes one
    return res


def api_events(pid: str, query: Dict[str, List[str]]) -> Dict[str, Any]:
    _check_pid(pid)
    since_raw = (query.get("since") or [None])[-1]
    since = None
    if since_raw:
        since = core.parse_iso(since_raw)
        if since is None:
            raise ApiError(400, "bad_since", "since 不是合法的 ISO 时间：%r" % since_raw)
    limit_raw = (query.get("limit") or [None])[-1]
    limit = EVENTS_LIMIT_DEFAULT
    if limit_raw not in (None, ""):
        try:
            limit = int(limit_raw)
        except ValueError:
            raise ApiError(400, "bad_limit", "limit 必须是整数")
        if limit < 1 or limit > EVENTS_LIMIT_MAX:
            raise ApiError(400, "bad_limit", "limit 须在 1..%d" % EVENTS_LIMIT_MAX)
    evs = sorted(core.read_events(pid), key=lambda e: e.get("at") or "")
    if since is not None:
        evs = [e for e in evs if (core.parse_iso(e.get("at")) or since) > since]
    truncated = len(evs) > limit
    evs = evs[-limit:]
    return {"ok": True, "project": pid, "since": since_raw, "count": len(evs),
            "truncated": truncated, "events": evs}


def api_supervisor(pid: str) -> Dict[str, Any]:
    """Latest supervisor report; stale after 2 h (it runs every 30 min), never shown as current then."""
    _check_pid(pid)
    doc = core.load_supervisor(pid)
    gen = core.parse_iso(doc.get("generated_at")) if doc else None
    if doc is None or gen is None:
        return {"ok": True, "report": None, "stale": True, "reason": "missing"}
    age = (_now() - gen).total_seconds()
    stale = age > SUPERVISOR_STALE_SECONDS
    return {"ok": True, "report": doc, "stale": stale, "age_s": int(age), "reason": "stale" if stale else None}


def api_project_review(pid: str) -> Dict[str, Any]:
    """Newest project-level dossier (progress/project_review/dossier-*.json): verdict hint, per-stage progress, flags,
    pending adjustments from the newest review file if any. Read-only; empty state when nothing was written yet."""
    _check_pid(pid)
    d = core.project_dir(pid) / "project_review"
    names = sorted(p.name for p in d.glob("dossier-*.json")) if d.is_dir() else []
    for name in reversed(names):
        try:
            data = json.loads((d / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        pending: List[Any] = []
        for rn in sorted(p.name for p in d.glob("review-*.json"))[-1:]:
            try:
                rv = json.loads((d / rn).read_text(encoding="utf-8"))
                pending = ((rv.get("merged") or {}).get("pending") or []) if isinstance(rv, dict) else []
            except (OSError, ValueError):
                pass
        md = name[:-5] + ".md"
        return {"ok": True, "empty": False, "generated_at": data.get("generated_at"),
                "verdict_hint": data.get("verdict_hint"), "stage_progress": data.get("stages") or [],
                "flags": data.get("flags") or [], "pending": pending, "file": name, "md_file": md,
                "md_path": str(d / md) if (d / md).is_file() else None}
    return {"ok": True, "empty": True, "reason": "还没有项目级档案", "stage_progress": [], "flags": [], "pending": []}


LEAD_CLOSED = ("done", "closed", "obsolete", "cancelled", "dropped", "resolved")


def _pick(row: Dict[str, Any], *names: str) -> Any:
    for n in names:
        if row.get(n) not in (None, ""):
            return row.get(n)
    return None


LEAD_TO_FIXED = {"human": "你", "user": "你", "autotaskgen": "框架维护", "framework": "框架维护", "lead": "负责人自己"}


def lead_to_label(to: Any, doc: Optional[Dict[str, Any]]) -> str:
    """'coordinator:core1' -> '核心一 后端性能评估 的协调人' (core names from project.json), 'router:FE' -> 'FE 线的 router'."""
    s = str(to or "").strip()
    if not s:
        return "未写收件人"
    if s.lower() in LEAD_TO_FIXED:
        return LEAD_TO_FIXED[s.lower()]
    role, _, name = s.partition(":")
    if not name:
        role, name = "", s
    cores = {c.get("id"): c.get("name") or c.get("id") for c in ((doc or {}).get("cores") or []) if isinstance(c, dict)}
    if role in ("coordinator", "") and name in cores:
        return "%s 的协调人" % cores[name]
    if role == "coordinator":
        return "%s 的协调人" % name
    if role == "router":
        return "%s 线的 router" % name
    if role == "worker":
        return "worker %s" % name
    return s


def api_lead(pid: str) -> Dict[str, Any]:
    """The project lead session's status (progress/<pid>/lead/status.json, schema lead-status/v1, copied by the
    lead at the end of each cycle): verdict, one-line summary, open adjustments. Read-only; empty state if absent."""
    _check_pid(pid)
    f = core.project_dir(pid) / "lead" / "status.json"
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        return {"ok": True, "empty": True, "reason": "项目负责人还没有写状态", "open": []}
    try:
        doc = core.load_project(pid)
    except Exception:  # noqa: BLE001 - labels only; the panel still works without the project doc
        doc = None
    rows = []
    for r in data.get("ledger") or []:
        if not isinstance(r, dict):
            continue
        st = str(_pick(r, "status", "state") or "open")
        if st.lower() in LEAD_CLOSED:
            continue
        to = _pick(r, "to", "recipient", "target")
        rows.append({"id": _pick(r, "id"), "to": to, "to_label": lead_to_label(to, doc), "kind": _pick(r, "kind", "type"),
                     "text": _pick(r, "text", "what", "content", "message", "summary"),
                     "done_when": _pick(r, "done_when", "criteria", "acceptance"),
                     "check_after": _pick(r, "check_after", "check_at", "due", "recheck_at"), "status": st})
    rows.sort(key=lambda x: (str(x.get("check_after") or "9"), str(x.get("id") or "")))  # soonest recheck first
    return {"ok": True, "empty": False, "schema": data.get("schema"), "updated": data.get("updated"),
            "cycle": data.get("cycle"), "verdict": data.get("verdict"), "summary": data.get("summary"),
            "report": data.get("report"), "ledger_counts": data.get("ledger_counts") or {}, "open": rows}


def store_marks() -> Dict[str, Tuple]:
    marks = {}
    for pid in core.list_projects():
        d = core.home() / pid
        sig = []
        for name in ("project.json", "events.jsonl", "live.json", "supervisor/latest.json", "project_review", "lead/status.json"):
            try:
                s = (d / name).stat()
                sig.append((s.st_mtime_ns, s.st_size))
            except OSError:
                sig.append(None)
        marks[pid] = tuple(sig)
    return marks


class Handler(BaseHTTPRequestHandler):
    server_version = "progress-board/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet by default; errors still go to stderr
        if getattr(self.server, "verbose", False):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # ---------------------------------------------------------------- helpers
    def _send(self, status: int, body: bytes, ctype: str, extra: Optional[Dict[str, str]] = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj: Any):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _err(self, status: int, code: str, message: str):
        self._json(status, {"ok": False, "code": code, "error": message})

    # --------------------------------------------------------------- dispatch
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self._route()
        except ApiError as exc:
            self._err(exc.status, exc.code, exc.message)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # never let one request take the server down
            traceback.print_exc()
            try:
                self._err(500, "internal", "%s: %s" % (type(exc).__name__, str(exc)[:200]))
            except Exception:
                pass

    def do_POST(self):
        self._err(405, "method", "只读服务，只接受 GET")

    do_PUT = do_DELETE = do_PATCH = do_POST

    def _route(self):
        raw = self.path
        if not raw.startswith("/") or "\x00" in raw:
            raise ApiError(400, "bad_path", "请求路径不合法")
        parsed = urllib.parse.urlsplit(raw)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        segs = path.split("/")
        if any(s in ("..", ".") for s in segs) or "\\" in path:
            raise ApiError(400, "bad_path", "路径里不能有 .. 或 .")
        if path == "/api/projects":
            return self._json(200, api_projects())
        if path == "/api/stream":
            return self._stream()
        if path.startswith("/api/p/"):
            parts = path[len("/api/p/"):].split("/")
            if len(parts) == 2 and parts[0]:
                if parts[1] == "state":
                    return self._json(200, api_state(parts[0]))
                if parts[1] == "events":
                    return self._json(200, api_events(parts[0], query))
                if parts[1] == "supervisor":
                    return self._json(200, api_supervisor(parts[0]))
                if parts[1] == "project_review":
                    return self._json(200, api_project_review(parts[0]))
                if parts[1] == "lead":
                    return self._json(200, api_lead(parts[0]))
            raise ApiError(404, "no_route", "没有这个接口：%s" % path)
        if path.startswith("/api/") or path == "/api":
            raise ApiError(404, "no_route", "没有这个接口：%s" % path)
        return self._static(path)

    def _static(self, path: str):
        rel = path.lstrip("/") or "index.html"
        root = WEB.resolve()
        target = (root / rel).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise ApiError(400, "bad_path", "路径越界")
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            raise ApiError(404, "not_found", "没有这个文件：%s" % rel)
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        poll = getattr(self.server, "poll_seconds", POLL_SECONDS)
        keep = getattr(self.server, "keepalive_seconds", KEEPALIVE_SECONDS)
        stop = getattr(self.server, "stop_event", None)

        def put(text: str):
            self.wfile.write(text.encode("utf-8"))
            self.wfile.flush()

        try:
            put("retry: 3000\n: hello\n\n")
            put("event: hello\ndata: %s\n\n" % json.dumps({"at": core.now_iso()}))
            prev = store_marks()
            last_send = time.monotonic()
            while not (stop and stop.is_set()):
                time.sleep(poll)
                cur = store_marks()
                for pid in sorted(set(cur) | set(prev)):
                    if cur.get(pid) != prev.get(pid):
                        put("event: change\ndata: %s\n\n" % json.dumps({"project": pid}))
                        last_send = time.monotonic()
                prev = cur
                if time.monotonic() - last_send >= keep:
                    put(": keep-alive %s\n\n" % core.now_iso())
                    last_send = time.monotonic()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, verbose=False):
        super().__init__(addr, Handler)
        self.verbose = verbose
        self.poll_seconds = POLL_SECONDS
        self.keepalive_seconds = KEEPALIVE_SECONDS
        self.stop_event = threading.Event()

    def handle_error(self, request, client_address):
        traceback.print_exc()

    def shutdown(self):
        self.stop_event.set()
        super().shutdown()


def main(argv=None):
    p = argparse.ArgumentParser(description="progress board web service")
    p.add_argument("--port", type=int, default=int(os.environ.get("PROGRESS_PORT", "8790")))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--verbose", action="store_true")
    a = p.parse_args(argv)
    srv = Server((a.host, a.port), verbose=a.verbose)
    sys.stderr.write("progress board on http://%s:%d/ store=%s\n" % (a.host, a.port, core.home()))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop_event.set()
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
