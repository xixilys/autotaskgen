"""patrol x project review (stage 1 wiring, WSL copy, alert) and the empty-line -> coordinator nudge. Fakes only."""
import datetime as dt
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import patrol  # noqa: E402
import progress_core as core  # noqa: E402
import snapshot as snap  # noqa: E402
from fakes import FakeRunner  # noqa: E402
from test_patrol import Base, C1, R, at, live, plan_ev, project, NOW  # noqa: E402


class FakeReview:
    def __init__(self, result=None, exc=None, delay=0.0):
        self.result, self.exc, self.delay, self.calls = result, exc, delay, []

    def __call__(self, pid, stage=None, now=None, runner=None, **kw):
        self.calls.append({"pid": pid, "stage": stage})
        if self.delay:
            time.sleep(self.delay)
        if self.exc:
            raise self.exc
        return self.result if self.result is not None else {"ran": False, "reason": "not_due"}


def with_stage(stage):
    p = project()
    p["defaults"] = {"project_review": {"stage": stage}}
    return p


class ReviewWiring(Base):
    def setUp(self):
        super().setUp()
        self.add(plan_ev("L", "a", auto_dispatch=False))
        self.put_live(live())

    def go(self, fn, apply=True, runner=None):
        runner = runner or FakeRunner()
        return patrol.run("t", apply=apply, runner=runner, now=NOW, store=snap.Store("t", runner), review_fn=fn), runner

    def test_called_when_due_and_summary_reported(self):
        d = self.tmp.name + "/dossier-1.md"
        pathlib.Path(d).write_text("x")
        pathlib.Path(d.replace(".md", ".json")).write_text("{}")
        fn = FakeReview({"ran": True, "ok": True, "trigger": "slot", "dossier_path": d})
        out, fr = self.go(fn)
        self.assertEqual(len(fn.calls), 1)
        self.assertEqual(fn.calls[0]["stage"], 1)
        pr = out["project_review"]
        self.assertTrue(pr["ran"] and pr["ok"])
        self.assertEqual(pr["path"], d)
        self.assertTrue(pr["sync"]["synced"])
        kinds = [c for c in fr.calls if c and c[0] in ("scp",)]
        self.assertEqual(len(kinds), 1)
        self.assertTrue(kinds[0][-1].startswith("wsl:.progress/t/project_review"))
        self.assertEqual(kinds[0][-3:-1], [d, d.replace(".md", ".json")])

    def test_not_due_reports_not_ran_and_no_copy(self):
        fn = FakeReview({"ran": False, "reason": "not_due"})
        out, fr = self.go(fn)
        self.assertEqual(len(fn.calls), 1)
        self.assertFalse(out["project_review"]["ran"])
        self.assertFalse([c for c in fr.calls if c and c[0] == "scp"])

    def test_failure_does_not_stop_other_actions(self):
        fn = FakeReview(exc=RuntimeError("boom"))
        out, fr = self.go(fn)
        self.assertFalse(out["project_review"]["ok"])
        self.assertIn("boom", out["project_review"]["why"])
        self.assertTrue(out["results"])                       # patrol still executed its actions
        self.assertTrue(fr.kinds("send"))                     # the router nudge went out

    def test_timeout_does_not_block(self):
        fn = FakeReview({"ran": False}, delay=1.0)
        runner = FakeRunner()
        t0 = time.time()
        with mock.patch.object(patrol, "REVIEW_STAGE1_CAP_S", 0.05):
            r = patrol.project_review_round("t", project(), [], True, runner, NOW, fn, cap_s=0.05)
        self.assertLess(time.time() - t0, 0.8)
        self.assertEqual(r["why"], "timeout")
        self.assertFalse(r["ok"])

    def test_dry_run_calls_nothing_writes_nothing(self):
        fn = FakeReview({"ran": True, "ok": True})
        out, fr = self.go(fn, apply=False)
        self.assertEqual(fn.calls, [])
        self.assertEqual(out["project_review"], {"ran": False, "reason": "dry_run"})
        self.assertNotIn("project_review", os.listdir(self.tmp.name + "/t"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "t", "patrol.state.json")))
        self.assertFalse([c for c in fr.calls if c and c[0] == "scp"])

    def test_stage_2_skipped_with_finding(self):
        core.save_project(with_stage(2))
        fn = FakeReview({"ran": True, "ok": True})
        out, _ = self.go(fn)
        self.assertEqual(fn.calls, [])
        self.assertTrue(any(f["code"] == "project_review_stage_unwired" for f in out["findings"]))

    def test_holdout_hit_alerts_without_terms_and_clears(self):
        seen = []

        def sync(active, now, runner, qq):
            seen.append(dict(active))
            return []
        fn = FakeReview({"ran": True, "ok": False, "why": "holdout_hit", "hits": 3})
        with mock.patch.object(patrol.qq_alerts, "sync", sync):
            self.go(fn)
            self.assertIn("project_review:holdout", seen[-1])
            msg = seen[-1]["project_review:holdout"]
            self.assertIn("3", msg)
            self.assertNotIn("terms", msg.lower().replace("留出集名单", ""))
            # still active while the next round reports not-due
            self.go(FakeReview({"ran": False, "reason": "not_due"}))
            self.assertIn("project_review:holdout", seen[-1])
            # cleared after a good run
            self.go(FakeReview({"ran": True, "ok": True, "trigger": "slot"}))
            self.assertNotIn("project_review:holdout", seen[-1])

    def test_sync_failure_is_reported_not_raised(self):
        d = self.tmp.name + "/dossier-2.md"
        pathlib.Path(d).write_text("x")
        calls = []

        def bad(argv, input_text=None, timeout=60):
            calls.append(argv)
            return 255, "", "ssh down"
        r = patrol.sync_review_to_wsl("t", d, bad)
        self.assertFalse(r["synced"])


class IdleToCoordinator(Base):
    def setUp(self):
        super().setUp()
        self.add(plan_ev("L", "a", auto_dispatch=False),
                 {"kind": "node_cancel", "line": "L", "node": "a", "summary": "c",
                  "actor": {"role": "router", "handle": R}, "at": at(200)})

    def lv(self, mem=32, flag=None, coord_flag=None, ago=2):
        d = live(minutes_ago=ago, flag=flag)
        d["coordinators"] = {"c1": {"handle": C1, "present": True, "screen_flag": coord_flag}}
        d["resources"] = {"wsl": {"mem_avail_gb": mem, "disks": []}}
        return d

    def sent(self, fr):
        return [c[c.index("--terminal") + 1] for c in fr.kinds("send")]

    def test_free_machine_goes_to_coordinator_not_router(self):
        self.put_live(self.lv())
        fr = FakeRunner()
        out = self.run_patrol(fr)
        self.assertEqual(self.sent(fr), [C1])
        text = [c for c in fr.kinds("send")][0]
        self.assertTrue(any("线空着、机器也空（可用内存 32 GB）" in str(x) for x in text))
        self.assertTrue(any(f["code"] == "idle_to_coordinator" for f in out["findings"]))
        self.assertEqual(self.events("nudge")[0]["reason"], "idle_coord")

    def test_machine_not_free_goes_to_router(self):
        self.put_live(self.lv(mem=8))
        fr = FakeRunner()
        self.run_patrol(fr)
        self.assertEqual(self.sent(fr), [R])

    def test_ready_node_goes_to_router(self):
        self.add(plan_ev("L", "b", minutes_ago=190, auto_dispatch=False))
        self.put_live(self.lv())
        fr = FakeRunner()
        out = self.run_patrol(fr)
        self.assertNotIn(C1, self.sent(fr))
        self.assertFalse(any(f["code"] == "idle_to_coordinator" for f in out["findings"]))

    def test_rate_limit_holds(self):
        self.put_live(self.lv())
        fr = FakeRunner()
        self.run_patrol(fr)
        self.put_live(self.lv(ago=2 - 30))
        out = self.run_patrol(fr, minutes_later=30)
        self.assertEqual(self.sent(fr), [C1])
        self.assertTrue(any(f["code"] == "idle_coord_rate_limited" for f in out["findings"]))
        self.put_live(self.lv(ago=2 - 61))
        self.run_patrol(fr, minutes_later=61)
        self.assertEqual(self.sent(fr), [C1, C1])

    def test_waiting_coordinator_not_messaged(self):
        self.put_live(self.lv(coord_flag="waiting_input"))
        fr = FakeRunner()
        self.run_patrol(fr)
        self.assertEqual(self.sent(fr), [])

    def test_closed_line_skipped(self):
        self.add({"kind": "line_close", "line": "L", "summary": "done", "actor": {"role": "coordinator", "handle": C1},
                  "at": at(100)})
        self.put_live(self.lv())
        fr = FakeRunner()
        self.run_patrol(fr)
        self.assertEqual(self.sent(fr), [])


if __name__ == "__main__":
    unittest.main()
