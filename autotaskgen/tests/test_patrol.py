import datetime as dt
import json
import os
import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import progress_core as core  # noqa: E402
import patrol  # noqa: E402
import snapshot as snap  # noqa: E402
from fakes import FakeRunner  # noqa: E402

R = "term_router-aaaa"
C1 = "term_coord1-bbbb"
NOW = dt.datetime(2026, 11, 4, 12, 0, 0, tzinfo=dt.timezone.utc)


def at(minutes_ago):
    return snap.iso(NOW - dt.timedelta(minutes=minutes_ago))


def project(lines=None):
    return {"schema": core.SCHEMA_PROJECT, "id": "t", "name": "test", "final_goal": {"text": "g"},
            "cores": [{"id": "c1", "name": "one", "coordinator": C1}],
            "lines": lines if lines is not None else [
                {"id": "L", "name": "l", "core": "c1", "router": R, "run": "run_x", "patrol": True},
                {"id": "Q", "name": "q", "core": "c1", "router": "term_other-ffff", "patrol": False}],
            "milestones": [{"id": "M1", "title": "m", "core": "c1", "lines": ["L"], "after": []}]}


ROUTE = {"agent": "codex", "model": "codex/gpt-6.1-sol", "effort": "high", "on": "wsl"}


def plan_ev(line, node, minutes_ago=300, router=R, **kw):
    ev = {"kind": "node_plan", "line": line, "node": node, "actor": {"role": "router", "handle": router},
          "summary": "plan", "at": at(minutes_ago), "spec": "br@docs/%s.md" % node, "route": ROUTE}
    ev.update(kw)
    return ev


def live(minutes_ago=2, workers=None, wsl="ok", present=True, flag=None):
    return {"schema": "progress-live/v1", "generated_at": at(minutes_ago),
            "hosts": {"mac": "ok", "wsl": wsl, "ib-agent": "unknown"},
            "routers": {"L": {"handle": R, "present": present, "last_output_at": at(1), "screen_flag": flag}},
            "workers": [] if workers is None and wsl == "ok" else workers, "errors": []}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["PROGRESS_HOME"] = self.tmp.name
        os.environ.pop("ORCA_TERMINAL_HANDLE", None)
        core.save_project(project())

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, *evs):
        for e in evs:
            core.append("t", e)

    def put_live(self, doc):
        core.save_live("t", doc)

    def run_patrol(self, runner, minutes_later=0, apply=True):
        return patrol.run("t", apply=apply, runner=runner, now=NOW + dt.timedelta(minutes=minutes_later),
                          store=snap.Store("t", runner))

    def events(self, kind=None):
        return [e for e in core.read_events("t") if kind is None or e["kind"] == kind]

    def node(self, line, nid):
        return core.fold(core.load_project("t"), core.read_events("t"))["lines"][line]["nodes"][nid]


class Dispatch(Base):
    def test_once_only_across_two_runs(self):
        self.add(plan_ev("L", "a"))
        self.put_live(live())
        fr = FakeRunner()
        out1 = self.run_patrol(fr)
        self.assertEqual(len(fr.kinds("worker-start")), 1)
        start = fr.kinds("worker-start")[0]
        # spec text passed unchanged, route mapped
        spec_sent = start[start.index("--spec") + 1]
        self.assertEqual(spec_sent, patrol.with_snippet(fr.spec, "L", "a"))
        self.assertTrue(spec_sent.startswith(fr.spec.rstrip("\n")))
        self.assertIn("--line L --node a", spec_sent)
        self.assertNotIn("{line}", spec_sent)
        self.assertEqual(start[start.index("--model") + 1], "codex/gpt-6.1-sol")
        self.assertEqual(start[start.index("--worktree") + 1], "new-top-level")
        self.assertEqual(start[start.index("--base-branch") + 1], "br")
        self.assertEqual(start[start.index("--run") + 1], "run_x")
        ad = self.events("auto_dispatch")
        self.assertEqual([e.get("phase") for e in ad], [None, "started"])
        n = self.node("L", "a")
        self.assertEqual((n["status"], n["dispatch_count"], n["worker"]), ("running", 1, "term_new-worker"))
        self.assertTrue(any(r["type"] == "notice" and r["sent"] for r in out1["results"]))
        out2 = self.run_patrol(fr, minutes_later=15)
        self.assertEqual(len(fr.kinds("worker-start")), 1, out2["actions"])

    def test_placeholder_duplicate_stops_concurrent_second_patrol(self):
        self.add(plan_ev("L", "a"))
        self.put_live(live())
        fr = FakeRunner()
        st = snap.Store("t", fr)
        p, e, lv = st.read()
        res = patrol.plan(p, e, lv, NOW, fr)  # both patrols planned before either wrote
        patrol.execute(st, res, NOW, fr)
        done = patrol.execute(st, res, NOW, fr)
        self.assertEqual(len(fr.kinds("worker-start")), 1)
        self.assertEqual(done[0].get("skipped"), "placeholder_exists")

    def test_failure_blocks_and_no_retry(self):
        self.add(plan_ev("L", "a"))
        self.put_live(live())
        fr = FakeRunner(start={"ok": False, "result": {"state": "failed", "failedStage": "setup"}}, start_rc=1)
        out = self.run_patrol(fr)
        blk = self.events("node_block")
        self.assertEqual(len(blk), 1)
        self.assertEqual(blk[0]["blocker"], "env")
        self.assertEqual(self.node("L", "a")["status"], "blocked")
        sends = fr.kinds("send")
        self.assertEqual(len(sends), 1)
        self.assertIn("失败", sends[0][sends[0].index("--text") + 1])
        self.run_patrol(fr, minutes_later=15)
        self.run_patrol(fr, minutes_later=200)
        self.assertEqual(len(fr.kinds("worker-start")), 1)
        self.assertTrue(out["results"])

    def test_malformed_start_output(self):
        self.add(plan_ev("L", "a"))
        self.put_live(live())
        fr = FakeRunner(start="garbage <html>", start_rc=0)
        self.run_patrol(fr)
        blk = self.events("node_block")
        self.assertEqual(len(blk), 1)
        self.assertIn("无法解析", blk[0]["summary"])
        self.run_patrol(fr, minutes_later=15)
        self.assertEqual(len(fr.kinds("worker-start")), 1)

    def test_dependency_not_met(self):
        self.add(plan_ev("L", "a", auto_dispatch=False), plan_ev("L", "b", deps=["a"]))
        self.put_live(live())
        fr = FakeRunner()
        out = self.run_patrol(fr)
        self.assertEqual(fr.kinds("worker-start"), [])
        self.assertEqual(self.node("L", "b")["status"], "planned")
        # a is ready but manual -> router nudged about it, b not mentioned as ready
        nudge = [a for a in out["actions"] if a["type"] == "nudge"]
        self.assertEqual(len(nudge), 1)
        self.assertIn("a", nudge[0]["text"])

    def test_dependency_met_by_partial_delivery(self):
        self.add(plan_ev("L", "a", auto_dispatch=False), plan_ev("L", "b", deps=["a"]),
                 {"kind": "node_deliver", "line": "L", "node": "a", "outcome": "PARTIAL", "summary": "d",
                  "actor": {"role": "router", "handle": R}, "at": at(100)})
        self.put_live(live())
        fr = FakeRunner()
        self.run_patrol(fr)
        self.assertEqual(len(fr.kinds("worker-start")), 1)
        start = fr.kinds("worker-start")[0]
        self.assertIn("br", start)

    def test_unreadable_spec_not_dispatched(self):
        self.add(plan_ev("L", "a"))
        self.put_live(live())
        fr = FakeRunner(spec=None)
        out = self.run_patrol(fr)
        self.assertEqual(fr.kinds("worker-start"), [])
        self.assertTrue(any(f["code"] == "spec_unreadable" for f in out["findings"]))
        self.assertEqual(self.events("auto_dispatch"), [])

    def test_dry_run_writes_nothing(self):
        self.add(plan_ev("L", "a"))
        self.put_live(live())
        fr = FakeRunner()
        out = self.run_patrol(fr, apply=False)
        self.assertEqual(fr.kinds("worker-start"), [])
        self.assertEqual(fr.kinds("send"), [])
        self.assertEqual(self.events("auto_dispatch"), [])
        self.assertEqual([a["type"] for a in out["actions"]], ["auto_dispatch"])
        self.assertTrue(any(str(x).startswith("<spec") for x in out["actions"][0]["argv"]))


class Snippet(unittest.TestCase):
    def test_embedded_matches_file(self):
        f = HERE.parent.parent / "skills" / "progress-worker" / "SPEC_SNIPPET.md"
        self.assertEqual(f.read_text(encoding="utf-8"), patrol.SPEC_SNIPPET)
        self.assertLessEqual(len(patrol.SPEC_SNIPPET.strip().splitlines()), 15)
        self.assertIn(patrol.SNIPPET_MARKER, patrol.SPEC_SNIPPET)

    def test_marker_skips_append(self):
        text = "# spec\n" + patrol.SNIPPET_MARKER + "\nown rules\n"
        self.assertEqual(patrol.with_snippet(text, "L", "a"), text)

    def test_appended_once_with_placeholders_filled(self):
        out = patrol.with_snippet("# spec\n", "EVB", "dev_x")
        self.assertEqual(out.count(patrol.SNIPPET_MARKER), 1)
        self.assertIn("--line EVB --node dev_x", out)
        self.assertEqual(patrol.with_snippet(out, "EVB", "dev_x"), out)


class Undelivered(Base):
    def _setup(self, task_state="succeeded", done_min_ago=30, extra=()):
        self.add(plan_ev("L", "a", auto_dispatch=False),
                 {"kind": "node_dispatch", "line": "L", "node": "a", "summary": "d", "at": at(120),
                  "actor": {"role": "router", "handle": R}, "refs": {"task": "task_1", "dispatch": "ctx_1", "terminal": "w"}},
                 *extra)
        doc = live()
        doc["tasks"] = {"task_1": {"state": task_state, "dispatched_at": at(120),
                                   "completed_at": at(done_min_ago)}}
        self.put_live(doc)

    def test_triggers_and_nudges(self):
        self._setup()
        fr = FakeRunner()
        out = self.run_patrol(fr)
        f = [x for x in out["findings"] if x["code"] == "undelivered"]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["node"], "a")
        sent = fr.kinds("send")
        self.assertEqual(len(sent), 1)
        text = " ".join(sent[0])
        self.assertIn("已结束 30 分钟但没写交付记录，请补 progress deliver", text)

    def test_failed_state_also_counts(self):
        self._setup(task_state="failed")
        out = self.run_patrol(FakeRunner(), apply=False)
        self.assertTrue(any(x["code"] == "undelivered" for x in out["findings"]))

    def test_not_before_20_minutes(self):
        self._setup(done_min_ago=15)
        out = self.run_patrol(FakeRunner(), apply=False)
        self.assertFalse(any(x["code"] == "undelivered" for x in out["findings"]))

    def test_task_still_running(self):
        self._setup(task_state="running", done_min_ago=60)
        out = self.run_patrol(FakeRunner(), apply=False)
        self.assertFalse(any(x["code"] == "undelivered" for x in out["findings"]))

    def test_not_when_delivered(self):
        self._setup(extra=[{"kind": "node_deliver", "line": "L", "node": "a", "outcome": "PASS",
                            "summary": "ok", "at": at(25), "actor": {"role": "worker", "handle": "w"}}])
        out = self.run_patrol(FakeRunner(), apply=False)
        self.assertFalse(any(x["code"] == "undelivered" for x in out["findings"]))

    def test_not_when_blocked(self):
        self._setup(extra=[{"kind": "node_block", "line": "L", "node": "a", "blocker": "env",
                            "summary": "b", "at": at(25), "actor": {"role": "worker", "handle": "w"}}])
        out = self.run_patrol(FakeRunner(), apply=False)
        self.assertFalse(any(x["code"] == "undelivered" for x in out["findings"]))

    def test_rate_limited(self):
        self._setup()
        fr = FakeRunner()
        self.run_patrol(fr)
        self.assertEqual(len(fr.kinds("send")), 1)
        doc = live(minutes_ago=2 - 10)
        doc["tasks"] = {"task_1": {"state": "succeeded", "dispatched_at": at(120), "completed_at": at(30)}}
        self.put_live(doc)
        out = self.run_patrol(fr, minutes_later=10)
        self.assertEqual(len(fr.kinds("send")), 1)
        self.assertTrue(any(f["code"] == "nudge_rate_limited" for f in out["findings"]))

    def test_patrol_off_and_flag_off(self):
        self._setup()
        (pathlib.Path(self.tmp.name) / "PATROL_OFF").write_text("")
        fr = FakeRunner()
        out = self.run_patrol(fr)
        self.assertEqual(fr.kinds("send"), [])
        self.assertTrue(out.get("off"))

    def test_stale_live_no_conclusion(self):
        self._setup()
        doc = live(minutes_ago=60)
        doc["tasks"] = {"task_1": {"state": "succeeded", "completed_at": at(30)}}
        self.put_live(doc)
        out = self.run_patrol(FakeRunner(), apply=False)
        self.assertFalse(any(x["code"] == "undelivered" for x in out["findings"]))


class Nudge(Base):
    def test_rate_limit(self):
        self.add(plan_ev("L", "a", auto_dispatch=False))
        self.put_live(live())
        fr = FakeRunner()
        self.run_patrol(fr)
        self.assertEqual(len(fr.kinds("send")), 1)
        self.assertEqual(len(self.events("nudge")), 1)
        self.put_live(live(minutes_ago=2 - 30))
        out = self.run_patrol(fr, minutes_later=30)
        self.assertEqual(len(fr.kinds("send")), 1)
        self.assertTrue(any(f["code"] == "nudge_rate_limited" for f in out["findings"]))
        self.put_live(live(minutes_ago=2 - 61))
        self.run_patrol(fr, minutes_later=61)
        self.assertEqual(len(fr.kinds("send")), 2)

    def test_quiet_line_nudged_and_absent_router_observed(self):
        self.add(plan_ev("L", "a", auto_dispatch=False),
                 {"kind": "node_cancel", "line": "L", "node": "a", "summary": "c",
                  "actor": {"role": "router", "handle": R}, "at": at(200)})
        self.put_live(live(present=False))
        fr = FakeRunner()
        out = self.run_patrol(fr)
        self.assertEqual(fr.kinds("send"), [])
        self.assertTrue(any(e.get("refs", {}).get("key") == "absent:L" for e in self.events("observe")),
                        out["actions"])

    def test_waiting_input_router_not_messaged(self):
        self.add(plan_ev("L", "a", auto_dispatch=False))
        self.put_live(live(flag="waiting_input"))
        fr = FakeRunner()
        self.run_patrol(fr)
        self.assertEqual(fr.kinds("send"), [])

    def test_unreviewed(self):
        self.add(plan_ev("L", "a", auto_dispatch=False),
                 {"kind": "node_deliver", "line": "L", "node": "a", "outcome": "PASS", "summary": "d",
                  "actor": {"role": "router", "handle": R}, "at": at(180)})
        self.put_live(live())
        out = self.run_patrol(FakeRunner(), apply=False)
        self.assertTrue(any(f["code"] == "unreviewed" for f in out["findings"]))

    def test_milestone_reminder_twice_a_day(self):
        self.add({"kind": "milestone_claim", "line": "L", "milestone": "M1", "summary": "ok",
                  "actor": {"role": "router", "handle": R}, "at": at(300)})
        self.put_live(live())
        fr = FakeRunner()
        for k in range(4):
            self.put_live(live(minutes_ago=2 - 300 * k))
            self.run_patrol(fr, minutes_later=300 * k // 2)
        to_coord = [c for c in fr.kinds("send") if c[c.index("--terminal") + 1] == C1]
        self.assertEqual(len(to_coord), 2)


class Switches(Base):
    def test_patrol_off(self):
        self.add(plan_ev("L", "a"))
        self.put_live(live())
        pathlib.Path(self.tmp.name, "PATROL_OFF").write_text("")
        fr = FakeRunner()
        out = self.run_patrol(fr)
        self.assertTrue(out["off"])
        self.assertEqual(fr.calls, [])
        self.assertEqual(self.events("auto_dispatch"), [])

    def test_patrol_false_line_ignored(self):
        self.add(plan_ev("Q", "a", router="term_other-ffff"))
        self.put_live(live())
        fr = FakeRunner()
        out = self.run_patrol(fr)
        self.assertEqual(fr.kinds("send"), [])  # screens are read for every router now, but nothing is sent
        self.assertEqual([a for a in out["actions"] if a.get("line") == "Q"], [])
        self.assertEqual(out["patrolled"], ["L"])

    def test_empty_project(self):
        doc = project(lines=[])
        doc["milestones"] = []
        core.save_project(doc)
        fr = FakeRunner()
        out = self.run_patrol(fr)
        self.assertEqual((out["actions"], fr.calls), ([], []))
        self.assertEqual([f["code"] for f in out["findings"]], ["qq_unconfigured"])  # no live.json: monitoring alert pending


class Liveness(Base):
    def setUp(self):
        super().setUp()
        self.add(plan_ev("L", "a", expected_duration_minutes=600),
                 {"kind": "node_dispatch", "line": "L", "node": "a", "summary": "go",
                  "actor": {"role": "router", "handle": R}, "at": at(60),
                  "refs": {"worktree": "repo::/home/u/ws/L-a", "terminal": "term_w-1"}})

    def test_missing_live_no_process_conclusion(self):
        out = self.run_patrol(FakeRunner())
        self.assertFalse(any(f["code"] == "process_gone" for f in out["findings"]))
        self.assertEqual(out["live_problem"], "live_missing")
        self.assertTrue(any(e["refs"]["key"] == "live:live_missing" for e in self.events("observe")))
        # observe is deduped on the next run
        self.run_patrol(FakeRunner(), minutes_later=15)
        self.assertEqual(len([e for e in self.events("observe") if e["refs"]["key"] == "live:live_missing"]), 1)

    def test_stale_live_no_process_conclusion(self):
        self.put_live(live(minutes_ago=45))
        out = self.run_patrol(FakeRunner(), apply=False)
        self.assertEqual(out["live_problem"], "live_stale")
        self.assertFalse(any(f["code"] == "process_gone" for f in out["findings"]))

    def test_wsl_unknown_no_process_conclusion(self):
        self.put_live(live(wsl="unknown", workers=None))
        out = self.run_patrol(FakeRunner(), apply=False)
        self.assertEqual(out["live_problem"], "wsl_unknown")
        self.assertFalse(any(f["code"] == "process_gone" for f in out["findings"]))

    def test_imported_running_node_not_called_gone(self):
        self.add(plan_ev("L", "imp", expected_duration_minutes=10),
                 {"kind": "node_dispatch", "line": "L", "node": "imp", "summary": "导入：已派出",
                  "source": "import", "actor": {"role": "import", "handle": "import@progress"}, "at": at(600),
                  "refs": {"worktree": "repo::/home/u/ws/L-imp"}})
        self.put_live(live())
        out = self.run_patrol(FakeRunner(), apply=False)
        self.assertFalse(any(f.get("node") == "imp" for f in out["findings"]), out["findings"])

    def test_fresh_live_process_gone(self):
        self.put_live(live())
        out = self.run_patrol(FakeRunner(), apply=False)
        self.assertTrue(any(f["code"] == "process_gone" for f in out["findings"]))

    def test_fresh_live_worker_present(self):
        self.put_live(live(workers=[{"host": "wsl", "workspace": "repo::/home/u/ws/L-a", "terminal": "term_w-1",
                                     "line": "L", "node": "a"}]))
        out = self.run_patrol(FakeRunner(), apply=False)
        self.assertFalse(any(f["code"] == "process_gone" for f in out["findings"]))

    def test_overtime(self):
        self.add({"kind": "node_plan", "line": "L", "node": "z", "summary": "p", "at": at(500),
                  "actor": {"role": "router", "handle": R}, "expected_duration_minutes": 60},
                 {"kind": "node_dispatch", "line": "L", "node": "z", "summary": "go", "at": at(100),
                  "actor": {"role": "router", "handle": R}})
        self.put_live(live())
        out = self.run_patrol(FakeRunner(), apply=False)
        self.assertTrue(any(f["code"] == "overtime" and f["node"] == "z" for f in out["findings"]))


class Stalled(Base):
    """worker_stalled / overtime: exempt from the hourly router limit, deduped per node."""

    def _setup(self, flag="idle", since_min=15, extra=(), worker=True, **node_kw):
        self.add(plan_ev("L", "a", auto_dispatch=False, expected_duration_minutes=600, **node_kw),
                 {"kind": "node_dispatch", "line": "L", "node": "a", "summary": "d", "at": at(120),
                  "actor": {"role": "router", "handle": R}, "refs": {"task": "task_1", "dispatch": "ctx_1", "terminal": "w"}},
                 *extra)
        self.put(flag, since_min, worker)

    def put(self, flag, since_min, worker=True, minutes_ago=2, routers_flag=None):
        sc = {"flag": flag, "line": "Codex: Bad Request", "checked_at": at(1)}
        if flag:
            sc["since"] = at(since_min)
        w = [{"host": "wsl", "workspace": "repo::/home/u/ws/L-a", "terminal": "w", "line": "L", "node": "a",
              "screen": sc}] if worker else []
        self.put_live(live(minutes_ago=minutes_ago, workers=w, flag=routers_flag))

    def stalled(self, out):
        return [f for f in out["findings"] if f["code"] == "worker_stalled"]

    def test_triggers(self):
        self._setup()
        fr = FakeRunner()
        out = self.run_patrol(fr)
        self.assertEqual(len(self.stalled(out)), 1)
        self.assertEqual(self.stalled(out)[0]["stalled_min"], 15)
        sent = fr.kinds("send")
        self.assertEqual(len(sent), 1)
        text = sent[0][sent[0].index("--text") + 1]
        self.assertIn("a 的 worker 已停（屏幕：Codex: Bad Request），停了约 15 分钟", text)
        self.assertIn("progress block", text)
        self.assertEqual(self.events("nudge")[0]["reason"], "stall")

    def test_each_flag_triggers(self):
        for flag in ("error", "idle", "exited"):
            with self.subTest(flag=flag):
                self.setUp()
                self._setup(flag=flag)
                self.assertEqual(len(self.stalled(self.run_patrol(FakeRunner(), apply=False))), 1)
                self.tearDown()

    def test_under_ten_minutes_does_not_trigger(self):
        self._setup(since_min=9)
        out = self.run_patrol(FakeRunner(), apply=False)
        self.assertEqual(self.stalled(out), [])
        self.assertEqual([a for a in out["actions"] if a["type"] == "alert"], [])

    def test_dedupe_one_per_stall_then_reminder_after_an_hour(self):
        self._setup(since_min=15)
        fr = FakeRunner()
        self.run_patrol(fr)
        self.put("idle", 15 + 20, minutes_ago=2 - 20)
        self.run_patrol(fr, minutes_later=20)
        self.assertEqual(len(fr.kinds("send")), 1)
        self.put("idle", 15 + 61, minutes_ago=2 - 61)
        self.run_patrol(fr, minutes_later=61)
        self.assertEqual(len(fr.kinds("send")), 2)
        self.put("idle", 15 + 130, minutes_ago=2 - 130)
        self.run_patrol(fr, minutes_later=130)
        self.assertEqual(len(fr.kinds("send")), 2)

    def test_bypasses_router_rate_limit(self):
        self._setup()
        self.add({"kind": "nudge", "line": "L", "reason": "nudge", "summary": "earlier", "at": at(2),
                  "actor": {"role": "patrol", "handle": "patrol@mac"}, "refs": {"target": R}})
        fr = FakeRunner()
        out = self.run_patrol(fr)
        self.assertEqual(len(fr.kinds("send")), 1)
        self.assertFalse(any(f["code"] == "nudge_rate_limited" for f in out["findings"]))

    def test_flag_cleared_then_new_stall_nudges_again(self):
        self._setup(since_min=15)
        fr = FakeRunner()
        self.run_patrol(fr)
        self.put(None, 0, minutes_ago=2 - 20)
        out = self.run_patrol(fr, minutes_later=20)
        self.assertEqual(self.stalled(out), [])
        self.assertEqual(len(fr.kinds("send")), 1)
        self.put("error", 12 - 40, minutes_ago=2 - 40)  # new stall began 12 min before the run at +40
        self.run_patrol(fr, minutes_later=40)
        self.assertEqual(len(fr.kinds("send")), 2)

    def test_patrol_off_and_line_flag_off(self):
        self._setup()
        self.add({"kind": "line_open", "line": "L", "actor": {"role": "coordinator", "handle": "term_coord1-bbbb"}, "summary": "keepalive",
                  "at": at(-1000)})   # fresh non-patrol event: no stall:project alert
        (pathlib.Path(self.tmp.name) / "PATROL_OFF").write_text("")
        fr = FakeRunner()
        self.assertTrue(self.run_patrol(fr).get("off"))
        self.assertEqual(fr.kinds("send"), [])
        (pathlib.Path(self.tmp.name) / "PATROL_OFF").unlink()
        core.save_project(project([{"id": "L", "name": "l", "core": "c1", "router": R, "run": "run_x", "patrol": False}]))
        self.assertEqual(self.run_patrol(fr)["findings"], [])
        self.assertEqual(fr.kinds("send"), [])

    def test_stale_live_ignored(self):
        self._setup()
        self.put("idle", 15, minutes_ago=60)
        self.assertEqual(self.stalled(self.run_patrol(FakeRunner(), apply=False)), [])

    def test_waiting_router_not_messaged(self):
        self._setup()
        self.put("idle", 15, routers_flag="waiting_input")
        fr = FakeRunner()
        self.run_patrol(fr)
        self.assertEqual(fr.kinds("send"), [])

    def test_overtime_factor_and_bypass(self):
        self.add(plan_ev("L", "z", auto_dispatch=False, expected_duration_minutes=60),
                 {"kind": "node_dispatch", "line": "L", "node": "z", "summary": "go", "at": at(75),
                  "actor": {"role": "router", "handle": R}},
                 {"kind": "nudge", "line": "L", "reason": "nudge", "summary": "earlier", "at": at(2),
                  "actor": {"role": "patrol", "handle": "patrol@mac"}, "refs": {"target": R}})
        self.put_live(live())
        fr = FakeRunner()
        out = self.run_patrol(fr)  # 75 min > 1.2 x 60 = 72, router was nudged 2 min ago
        self.assertTrue(any(f["code"] == "overtime" for f in out["findings"]))
        self.assertEqual(len(fr.kinds("send")), 1)
        self.run_patrol(fr, minutes_later=10)
        self.assertEqual(len(fr.kinds("send")), 1)  # deduped per node
        self.run_patrol(fr, minutes_later=65)
        self.assertEqual(len(fr.kinds("send")), 2)  # one reminder after 60 min

    def test_not_overtime_under_factor(self):
        self.add(plan_ev("L", "z", auto_dispatch=False, expected_duration_minutes=60),
                 {"kind": "node_dispatch", "line": "L", "node": "z", "summary": "go", "at": at(70),
                  "actor": {"role": "router", "handle": R}})
        self.put_live(live())
        out = self.run_patrol(FakeRunner(), apply=False)
        self.assertFalse(any(f["code"] == "overtime" for f in out["findings"]))


class Kinds(Base):
    def _run(self, nid, minutes_ago, kind=None, exp=None, defaults=None):
        if defaults is not None:
            p = project()
            p["defaults"] = {"expected_minutes": defaults}
            core.save_project(p)
        kw = {}
        if kind:
            kw["node_kind"] = kind
        if exp:
            kw["expected_duration_minutes"] = exp
        self.add(plan_ev("L", nid, auto_dispatch=False, **kw),
                 {"kind": "node_dispatch", "line": "L", "node": nid, "summary": "go", "at": at(minutes_ago),
                  "actor": {"role": "router", "handle": R}})
        self.put_live(live())
        fr = FakeRunner(spec=None)
        out = self.run_patrol(fr)
        return out, fr

    def text(self, fr):
        s = fr.kinds("send")
        return s[0][s[0].index("--text") + 1] if s else ""

    def test_kind_in_fold_explicit_and_inferred(self):
        self.add(plan_ev("L", "dev_x", auto_dispatch=False), plan_ev("L", "dev_x_review", auto_dispatch=False),
                 plan_ev("L", "foo", auto_dispatch=False, node_kind="review"),
                 plan_ev("L", "Recheck_b", auto_dispatch=False), plan_ev("L", "audit1", auto_dispatch=False),
                 plan_ev("L", "bar", auto_dispatch=False, node_kind="run"),
                 plan_ev("L", "dev_review_run", auto_dispatch=False, node_kind="run"))
        got = {k: self.node("L", k)["kind"] for k in ("dev_x", "dev_x_review", "foo", "Recheck_b", "audit1", "bar", "dev_review_run")}
        self.assertEqual(got, {"dev_x": "run", "dev_x_review": "review", "foo": "review", "Recheck_b": "review",
                               "audit1": "review", "bar": "run", "dev_review_run": "run"})

    def test_review_default_30_min_and_text(self):
        out, fr = self._run("dev_x_review", 40)
        f = [x for x in out["findings"] if x["code"] == "overtime"]
        self.assertEqual((f[0]["expected_min"], f[0]["kind"]), (30.0, "review"))
        self.assertIn("审查节点 dev_x_review", self.text(fr))

    def test_run_default_120_min(self):
        out, _ = self._run("dev_x", 100)
        self.assertFalse(any(x["code"] == "overtime" for x in out["findings"]))
        self.setUp()
        out, fr = self._run("dev_x", 150)
        self.assertTrue(any(x["code"] == "overtime" and x["kind"] == "run" for x in out["findings"]))
        self.assertNotIn("审查节点", self.text(fr))

    def test_project_defaults_override_and_explicit_wins(self):
        out, _ = self._run("dev_x", 100, defaults={"run": 60, "review": 10})
        self.assertTrue(any(x["code"] == "overtime" and x["expected_min"] == 60 for x in out["findings"]))
        self.setUp()
        out, _ = self._run("dev_x", 100, exp=600, defaults={"run": 60})
        self.assertFalse(any(x["code"] == "overtime" for x in out["findings"]))

    def test_cli_kind_flag(self):
        import progress
        for argv, want in ((["--kind", "review"], "review"), ([], "run")):
            with self.subTest(argv=argv):
                a = progress.parser().parse_args(["plan", "--line", "L", "--node", "n1", "--summary", "s"] + argv)
                self.assertEqual(progress.build_event("plan", a)["node_kind"], want)
        a = progress.parser().parse_args(["plan", "--line", "L", "--node", "x_review", "--summary", "s"])
        self.assertEqual(progress.build_event("plan", a)["node_kind"], "review")


class RemoteStore(Base):
    def test_unreachable_store_raises(self):
        st = snap.Store("other", FakeRunner())
        self.assertFalse(st.local)
        with self.assertRaises(core.ProgressError) as cm:
            st.read()
        self.assertEqual(cm.exception.code, "store_unreachable")


if __name__ == "__main__":
    unittest.main()


class AutoContinue(Base):
    """Script layer: transient error at the input prompt -> 继续, max 2 tries per stall, then the stalled alert."""

    def _live(self, router_screen=None, worker_screen=None, since=15, flag=None):
        w = []
        if worker_screen is not None:
            sc = {"flag": "error", "line": "stream disconnected", "checked_at": at(1), "since": at(since),
                  "transient": worker_screen.get("transient", True), "prompt": worker_screen.get("prompt", True)}
            w = [{"host": "wsl", "workspace": "repo::/home/u/ws/L-a", "terminal": "term_w1", "line": "L", "node": "a",
                  "screen": sc}]
        d = live(workers=w, flag=flag)
        if router_screen is not None:
            d["routers"]["L"]["screen"] = dict(router_screen, checked_at=at(1))
        d["coordinators"] = {}
        self.put_live(d)

    def sends(self, fr):
        return [c[c.index("--terminal") + 1] for c in fr.kinds("send") if c[c.index("--text") + 1] == "继续"]

    def test_worker_continue_even_when_line_patrol_false(self):
        pr = project()
        pr["lines"][0]["patrol"] = False
        core.save_project(pr)
        self.add(plan_ev("L", "a", auto_dispatch=False))
        self._live(worker_screen={})
        fr = FakeRunner()
        out = self.run_patrol(fr)
        self.assertEqual(self.sends(fr), ["term_w1"])
        send = fr.kinds("send")[0]
        self.assertEqual(send[send.index("--text") + 1], "继续")
        self.assertIn("--enter", send)
        ev = self.events("nudge")[0]
        self.assertEqual(ev["reason"], "auto_continue")
        self.assertEqual([a["type"] for a in out["actions"]], ["auto_continue"])

    def test_router_screen_from_snapshot(self):
        self.add(plan_ev("L", "a", auto_dispatch=False))
        self._live(router_screen={"transient": True, "prompt": True, "line": "rate limit exceeded", "since": at(5)})
        fr = FakeRunner()
        self.run_patrol(fr)
        self.assertEqual(self.sends(fr), [R])

    def test_router_screen_read_when_no_tail_stored(self):
        self.add(plan_ev("L", "a", auto_dispatch=False))
        self._live()
        fr = FakeRunner(terminals=[], screen=["■ {\"error\": {\"message\": \"x\"}}", "", "› "])
        self.run_patrol(fr)
        self.assertIn(R, self.sends(fr))
        self.assertTrue(fr.kinds("read"))

    def test_not_at_prompt_or_not_transient_skipped(self):
        self.add(plan_ev("L", "a", auto_dispatch=False))
        self._live(worker_screen={"prompt": False})
        fr = FakeRunner(screen=["all fine", "› "])
        self.run_patrol(fr)
        self.assertEqual(self.sends(fr), [])
        self._live(worker_screen={"transient": False})
        self.run_patrol(fr)
        self.assertEqual(self.sends(fr), [])

    def test_two_tries_then_stalled_alert(self):
        self.add(plan_ev("L", "a", auto_dispatch=False, expected_duration_minutes=600),
                 {"kind": "node_dispatch", "line": "L", "node": "a", "summary": "d", "at": at(120),
                  "actor": {"role": "router", "handle": R}, "refs": {"task": "t", "dispatch": "ctx_1", "terminal": "term_w1"}})
        self._live(worker_screen={}, since=3)  # under STALL_S: only auto-continue applies at first
        for i in range(2):
            fr = FakeRunner()
            self.run_patrol(fr, minutes_later=i)
            self.assertEqual(self.sends(fr), ["term_w1"])
        self.assertEqual(len([e for e in self.events("nudge") if e["reason"] == "auto_continue"]), 2)
        fr = FakeRunner()
        out = self.run_patrol(fr, minutes_later=3)
        self.assertEqual(self.sends(fr), [])  # no more 继续
        texts = [c[c.index("--text") + 1] for c in fr.kinds("send")]
        self.assertTrue([t for t in texts if "worker 已停" in t])  # the stalled alert, to the router
        self.assertTrue([f for f in out["findings"] if f["code"] == "auto_continue_exhausted"])

    def test_dry_run_sends_nothing(self):
        self.add(plan_ev("L", "a", auto_dispatch=False))
        self._live(worker_screen={})
        fr = FakeRunner()
        out = self.run_patrol(fr, apply=False)
        self.assertEqual(self.sends(fr), [])
        self.assertEqual(out["actions"][0]["type"], "auto_continue")
        self.assertEqual(self.events("nudge"), [])

    def test_patrol_off(self):
        self._live(worker_screen={})
        pathlib.Path(self.tmp.name, "PATROL_OFF").write_text("")
        fr = FakeRunner()
        self.run_patrol(fr)
        self.assertEqual(self.sends(fr), [])
