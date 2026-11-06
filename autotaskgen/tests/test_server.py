import datetime as _dt
import http.client
import json
import os
import pathlib
import socket
import sys
import tempfile
import threading
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import progress_core as core  # noqa: E402
import server  # noqa: E402

R = "term_router-aaaa"
C1 = "term_coord1-bbbb"


def project():
    return {
        "schema": core.SCHEMA_PROJECT, "id": "t", "name": "test",
        "final_goal": {"text": "goal", "done_when": "all"},
        "cores": [{"id": "c1", "name": "one", "coordinator": C1}],
        "lines": [{"id": "L", "name": "line", "core": "c1", "router": R}],
        "milestones": [{"id": "M1", "title": "m1", "core": "c1", "lines": ["L"], "after": []},
                       {"id": "M2", "title": "m2", "core": "c1", "lines": [], "after": ["M1"]}],
    }


def iso(minutes_ago):
    t = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def ev(kind, role, handle, at, **kw):
    d = {"kind": kind, "actor": {"role": role, "handle": handle}, "summary": "s", "at": at}
    d.update(kw)
    return d


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("PROGRESS_HOME")
        os.environ["PROGRESS_HOME"] = self.tmp.name
        core.save_project(project())
        core.append("t", ev("node_plan", "router", R, iso(120), line="L", node="a", title="A"))
        core.append("t", ev("node_dispatch", "router", R, iso(100), line="L", node="a",
                            refs={"terminal": "term_w"}))
        core.append("t", ev("milestone_claim", "router", R, iso(50), line="L", milestone="M1"))
        self.srv = server.Server(("127.0.0.1", 0))
        self.srv.poll_seconds = 0.1
        self.srv.keepalive_seconds = 0.3
        self.port = self.srv.server_address[1]
        self.th = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.th.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        if self._old_home is None:
            os.environ.pop("PROGRESS_HOME", None)
        else:
            os.environ["PROGRESS_HOME"] = self._old_home
        self.tmp.cleanup()

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", path)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, r.getheader("Content-Type") or "", body

    def getj(self, path):
        st, ct, body = self.get(path)
        self.assertIn("application/json", ct)
        return st, json.loads(body.decode("utf-8"))

    def raw(self, line):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        s.sendall(("GET %s HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n" % line).encode())
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        s.close()
        return data

    def put_live(self, minutes_ago):
        core.save_live("t", {"schema": "progress-live/v1", "generated_at": iso(minutes_ago),
                             "hosts": {"wsl": "ok", "ib-agent": "unknown"},
                             "routers": {"L": {"present": True}},
                             "workers": [{"host": "wsl", "model": "codex/gpt-6.1-sol", "line": "L", "node": "a", "busy": True},
                                         {"host": "wsl", "model": "x", "line": None, "node": None},
                                         {"host": "wsl", "model": "x", "line": "L", "node": "b", "busy": False,
                                          "lingering": True}]})

    def put_sup(self, minutes_ago):
        core.save_supervisor("t", {"schema": "progress-supervisor/v1", "generated_at": iso(minutes_ago),
                                   "summary": "ok", "lines": [], "delivery": []})

    def test_supervisor_missing(self):
        st, j = self.getj("/api/p/t/supervisor")
        self.assertEqual(st, 200)
        self.assertIsNone(j["report"])
        self.assertTrue(j["stale"])
        self.assertEqual(j["reason"], "missing")

    def test_supervisor_fresh(self):
        self.put_sup(5)
        st, j = self.getj("/api/p/t/supervisor")
        self.assertEqual(j["report"]["summary"], "ok")
        self.assertFalse(j["stale"])
        self.assertIsNone(j["reason"])

    def test_supervisor_old_is_stale(self):
        self.put_sup(150)
        st, j = self.getj("/api/p/t/supervisor")
        self.assertIsNotNone(j["report"])
        self.assertTrue(j["stale"])
        self.assertEqual(j["reason"], "stale")

    def test_project_review_empty(self):
        st, j = self.getj("/api/p/t/project_review")
        self.assertEqual(st, 200)
        self.assertTrue(j["empty"])
        self.assertEqual(j["reason"], "还没有项目级档案")

    def test_project_review_latest(self):
        d = core.project_dir("t") / "project_review"
        d.mkdir(parents=True)
        (d / "dossier-20261106T020000Z.json").write_text(json.dumps({"generated_at": "old", "flags": []}))
        (d / "dossier-20261106T080000Z.json").write_text(json.dumps({
            "generated_at": "2026-11-06T08:00:00Z", "verdict_hint": "warn",
            "stages": [{"stage": "S1", "total": 4, "met": 2, "closed_24h": 1}],
            "flags": [{"level": "warn", "key": "k", "text": "机器空+线空", "to": "coordinator"}]}))
        (d / "dossier-20261106T080000Z.md").write_text("# d")
        st, j = self.getj("/api/p/t/project_review")
        self.assertEqual(st, 200)
        self.assertFalse(j["empty"])
        self.assertEqual(j["generated_at"], "2026-11-06T08:00:00Z")
        self.assertEqual(j["verdict_hint"], "warn")
        self.assertEqual(j["stage_progress"][0]["met"], 2)
        self.assertEqual(j["flags"][0]["text"], "机器空+线空")
        self.assertTrue(j["md_path"].endswith("dossier-20261106T080000Z.md"))

    def test_lead_empty(self):
        st, j = self.getj("/api/p/t/lead")
        self.assertEqual(st, 200)
        self.assertTrue(j["empty"])

    def test_lead_status_open_rows_only(self):
        d = core.project_dir("t") / "lead"
        d.mkdir(parents=True)
        (d / "status.json").write_text(json.dumps({
            "schema": "lead-status/v1", "updated": "2026-11-06T08:00:00Z", "cycle": 3, "verdict": "warn",
            "summary": "FE 等人", "report": "/x/r.md", "ledger_counts": {"open": 1, "done": 1},
            "ledger": [{"id": "ld_1", "to": "core1", "text": "排 B8", "done_when": "B8 交付", "check_after": "2026-11-06T10:00:00Z", "status": "open"},
                       {"id": "ld_0", "to": "core2", "text": "旧", "status": "done"}]}, ensure_ascii=False))
        st, j = self.getj("/api/p/t/lead")
        self.assertEqual(st, 200)
        self.assertFalse(j["empty"])
        _, sj = self.getj("/api/p/t/state")
        self.assertEqual((sj["lead"]["verdict"], [r["id"] for r in sj["lead"]["open"]]), ("warn", ["ld_1"]))
        self.assertEqual((j["verdict"], j["cycle"], j["summary"]), ("warn", 3, "FE 等人"))
        self.assertEqual([r["id"] for r in j["open"]], ["ld_1"])
        self.assertEqual(j["open"][0]["done_when"], "B8 交付")

    def test_lead_rows_plain_labels_and_order(self):
        d = core.project_dir("t") / "lead"
        d.mkdir(parents=True)
        (d / "status.json").write_text(json.dumps({
            "schema": "lead-status/v1", "updated": "2026-11-06T08:00:00Z", "verdict": "at_risk",
            "ledger": [{"id": "L2", "to": "router:FE", "kind": "adjust", "what": "补对照", "check_at": "2026-11-06T12:00:00Z", "status": "open"},
                       {"id": "L1", "to": "autotaskgen", "kind": "tooling", "what": "加字段", "check_at": "2026-11-06T09:00:00Z", "status": "open"},
                       {"id": "L3", "to": "human", "what": "要授权", "status": "escalated"}]}, ensure_ascii=False))
        _, j = self.getj("/api/p/t/lead")
        self.assertEqual([r["id"] for r in j["open"]], ["L1", "L2", "L3"])
        self.assertEqual([r["to_label"] for r in j["open"]], ["框架维护", "FE 线的 router", "你"])
        self.assertEqual((j["open"][0]["text"], j["open"][0]["kind"]), ("加字段", "tooling"))

    def test_lead_to_label_uses_core_names(self):
        doc = {"cores": [{"id": "core1", "name": "核心一 后端"}]}
        self.assertEqual(server.lead_to_label("coordinator:core1", doc), "核心一 后端 的协调人")
        self.assertEqual(server.lead_to_label("coordinator:x", doc), "x 的协调人")
        self.assertEqual(server.lead_to_label("", doc), "未写收件人")

    def test_lead_unknown_project_404(self):
        st, _ = self.getj("/api/p/nope/lead")
        self.assertEqual(st, 404)

    def test_project_review_unknown_project_404(self):
        st, _ = self.getj("/api/p/nope/project_review")
        self.assertEqual(st, 404)

    def test_supervisor_unknown_project_404(self):
        st, _ = self.getj("/api/p/nope/supervisor")
        self.assertEqual(st, 404)

    def test_store_marks_include_supervisor(self):
        before = server.store_marks()["t"]
        self.put_sup(1)
        self.assertNotEqual(before, server.store_marks()["t"])

    # ---------------------------------------------------------------- projects
    def test_projects(self):
        self.put_live(1)
        st, j = self.getj("/api/projects")
        self.assertEqual(st, 200)
        p = j["projects"][0]
        self.assertEqual(p["id"], "t")
        self.assertEqual(p["progress"], {"total": 2, "confirmed": 0, "claimed": 1})
        self.assertEqual(p["lines"], 1)
        # 11-07: only busy workers whose task is not settled count as 在跑; the others are listed apart
        self.assertEqual((p["running_workers"], p["idle_workers"], p["lingering_workers"]), (1, 1, 1))
        self.assertEqual([w["phase"] for w in p["workers"]], ["running", "idle", "lingering"])
        self.assertFalse(p["live_stale"])

    def test_projects_missing_live_is_unknown_not_zero(self):
        st, j = self.getj("/api/projects")
        p = j["projects"][0]
        self.assertIsNone(p["running_workers"])
        self.assertIsNone(p["workers"])
        self.assertTrue(p["live_stale"])

    def test_projects_empty_store(self):
        os.environ["PROGRESS_HOME"] = os.path.join(self.tmp.name, "empty")
        st, j = self.getj("/api/projects")
        self.assertEqual((st, j["projects"]), (200, []))

    # ------------------------------------------------------------------- state
    def test_state_fresh_live(self):
        self.put_live(2)
        st, j = self.getj("/api/p/t/state")
        self.assertEqual(st, 200)
        self.assertEqual(j["project"]["id"], "t")
        self.assertEqual(j["state"]["lines"]["L"]["nodes"]["a"]["status"], "running")
        self.assertEqual(j["state"]["milestones"]["M1"]["status"], "claimed")
        self.assertFalse(j["live_stale"])
        self.assertEqual(len(j["live"]["workers"]), 3)

    def test_state_missing_live(self):
        st, j = self.getj("/api/p/t/state")
        self.assertEqual(st, 200)
        self.assertIsNone(j["live"])
        self.assertTrue(j["live_stale"])
        self.assertIsNone(j["live_at"])
        self.assertEqual(j["live_reason"], "missing")

    def test_state_old_live_is_stale(self):
        self.put_live(31)
        st, j = self.getj("/api/p/t/state")
        self.assertIsNone(j["live"])
        self.assertTrue(j["live_stale"])
        self.assertEqual(j["live_reason"], "stale")
        self.assertIsNotNone(j["live_at"])
        self.assertGreater(j["live_age_s"], 30 * 60)

    def test_state_live_without_time_is_stale(self):
        (pathlib.Path(self.tmp.name) / "t" / "live.json").write_text('{"workers": []}')
        st, j = self.getj("/api/p/t/state")
        self.assertIsNone(j["live"])
        self.assertTrue(j["live_stale"])

    def test_unknown_project_404(self):
        for path in ("/api/p/nope/state", "/api/p/nope/events"):
            st, j = self.getj(path)
            self.assertEqual(st, 404)
            self.assertEqual(j, {"ok": False, "code": "no_project", "error": j["error"]})

    def test_bad_project_id_400(self):
        st, j = self.getj("/api/p/.hidden/state")
        self.assertEqual(st, 400)
        self.assertFalse(j["ok"])

    def test_unknown_api_route_404(self):
        st, j = self.getj("/api/p/t/nothing")
        self.assertEqual(st, 404)
        st, j = self.getj("/api/whatever")
        self.assertEqual(st, 404)

    # ------------------------------------------------------------------ events
    def test_events_all_and_since(self):
        st, j = self.getj("/api/p/t/events")
        self.assertEqual((st, j["count"]), (200, 3))
        kinds = [e["kind"] for e in j["events"]]
        self.assertEqual(kinds, ["node_plan", "node_dispatch", "milestone_claim"])
        st, j = self.getj("/api/p/t/events?since=" + iso(110))
        self.assertEqual([e["kind"] for e in j["events"]], ["node_dispatch", "milestone_claim"])
        st, j = self.getj("/api/p/t/events?since=" + iso(-10))
        self.assertEqual(j["count"], 0)

    def test_events_limit(self):
        st, j = self.getj("/api/p/t/events?limit=1")
        self.assertEqual(j["count"], 1)
        self.assertTrue(j["truncated"])
        self.assertEqual(j["events"][0]["kind"], "milestone_claim")

    def test_events_bad_params_400(self):
        for q in ("since=yesterday", "limit=abc", "limit=0", "limit=999999"):
            st, j = self.getj("/api/p/t/events?" + q)
            self.assertEqual(st, 400, q)
            self.assertFalse(j["ok"])

    # ------------------------------------------------------------------ static
    def test_static_index(self):
        st, ct, body = self.get("/")
        self.assertEqual(st, 200)
        self.assertIn("text/html", ct)
        self.assertIn(b"app.js", body)
        # relative URLs only, so the board works under a path prefix
        self.assertNotIn(b'src="/', body)
        self.assertNotIn(b'href="/', body)
        st, ct, body = self.get("/app.js")
        self.assertEqual(st, 200)
        self.assertNotIn(b'"/api/', body)
        st, ct, _ = self.get("/style.css")
        self.assertIn("text/css", ct)

    def test_static_missing_404(self):
        st, ct, body = self.get("/nope.js")
        self.assertEqual(st, 404)

    def test_path_traversal_refused(self):
        for p in ("/../server.py", "/%2e%2e/server.py", "/web/../../progress_core.py", "/..%2fserver.py"):
            data = self.raw(p)
            head = data.split(b"\r\n", 1)[0]
            self.assertTrue(head.endswith(b"400 Bad Request") or b" 404 " in head, (p, head))
            self.assertNotIn(b"ThreadingHTTPServer", data)
            self.assertNotIn(b"def fold", data)

    def test_post_refused(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("POST", "/api/projects", body="{}")
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 405)

    def test_internal_error_keeps_server(self):
        (pathlib.Path(self.tmp.name) / "t" / "project.json").write_text("{not json")
        st, j = self.getj("/api/p/t/state")
        self.assertEqual(st, 500)
        self.assertFalse(j["ok"])
        st, j = self.getj("/api/projects")
        self.assertEqual(st, 200)
        self.assertIn("error", j["projects"][0])

    # ------------------------------------------------------------------ stream
    def test_stream_change_event(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/api/stream")
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertIn("text/event-stream", r.getheader("Content-Type"))
        buf = b""
        deadline = time.time() + 8
        wrote = False
        while time.time() < deadline and b"event: change" not in buf:
            line = r.fp.readline()
            buf += line
            if b"event: hello" in buf and not wrote:
                time.sleep(0.25)
                self.put_live(0)
                wrote = True
        buf += r.fp.readline()  # the data line that follows
        c.close()
        self.assertIn(b"event: change", buf)
        self.assertIn(b'"project": "t"', buf)

    def test_stream_keepalive(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/api/stream")
        r = c.getresponse()
        buf = b""
        deadline = time.time() + 5
        while time.time() < deadline and b": keep-alive" not in buf:
            buf += r.fp.readline()
        c.close()
        self.assertIn(b": keep-alive", buf)


class LiveView(unittest.TestCase):
    def test_boundary(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["PROGRESS_HOME"] = d
            core.save_project(project())
            now = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)
            core.save_live("t", {"generated_at": (now - _dt.timedelta(minutes=29)).strftime("%Y-%m-%dT%H:%M:%SZ")})
            self.assertFalse(server.live_view("t", now)["live_stale"])
            self.assertTrue(server.live_view("t", now + _dt.timedelta(minutes=2))["live_stale"])
            os.environ.pop("PROGRESS_HOME", None)


if __name__ == "__main__":
    unittest.main()
