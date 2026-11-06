"""Ready nodes must not sit undispatched silently: S1 undispatchable nudge/alert, S2 rejected-dispatch retry,
S3 ready_stale backstop. Fakes only."""
import datetime as dt
import sys
import unittest

import test_patrol as tp
from test_patrol import Base, NOW, R, at, live, plan_ev, project
import patrol  # noqa: E402
import progress_core as core  # noqa: E402
import snapshot as snap  # noqa: E402
from fakes import FakeRunner, wl_row  # noqa: E402

REJECT = "Remote new-top-level requires --name and an explicit --repo"


def running_node(nid="r"):
    return [plan_ev("L", nid, 400, expected_duration_minutes=900),
            {"kind": "node_dispatch", "line": "L", "node": nid, "actor": {"role": "router", "handle": R},
             "summary": "go", "at": at(300)}]


def texts(fr):
    return [c[c.index("--text") + 1] for c in fr.kinds("send")]


class Undispatchable(Base):
    def test_nudge_with_running_nodes_and_dedupe(self):
        self.add(*running_node(), plan_ev("L", "a"))
        self.put_live(live())
        fr = FakeRunner(spec=None)
        out = self.run_patrol(fr)
        sent = texts(fr)
        self.assertEqual(len(sent), 1, sent)
        self.assertIn("1 个就绪节点巡检派不出去：a（spec 读不到）", sent[0])
        self.assertIn("请修 spec/route 或自己派", sent[0])
        self.assertEqual(self.node("L", "r")["status"], "running")
        self.assertTrue(any(f["code"] == "undispatchable" for f in out["findings"]))
        self.run_patrol(fr, minutes_later=15)
        self.assertEqual(len(texts(fr)), 1)           # same node+reason: once
        self.run_patrol(fr, minutes_later=70)
        self.assertEqual(len(texts(fr)), 2)           # one reminder after 1 h
        self.run_patrol(fr, minutes_later=200)
        self.assertEqual(len(texts(fr)), 2)

    def test_route_incomplete_listed(self):
        self.add(*running_node(), plan_ev("L", "a", route={"agent": "codex", "on": "wsl"}))
        self.put_live(live())
        fr = FakeRunner(spec=tp.json.dumps({}) + "\nbody\n")
        self.run_patrol(fr)
        self.assertIn("a（route 缺 model）", texts(fr)[0])

    def test_qq_alert_after_two_rounds_then_resolves(self):
        self.add(*running_node(), plan_ev("L", "a"))
        self.put_live(live())
        bad = FakeRunner(spec=None)
        out1 = self.run_patrol(bad)
        self.assertNotIn("undispatchable:L", out1["alerts_active"])
        out2 = self.run_patrol(bad, minutes_later=15)
        self.assertIn("undispatchable:L", out2["alerts_active"])
        self.assertIn("1 个就绪节点连续派不出去", out2["alerts_active"]["undispatchable:L"])
        self.assertIn("spec 读不到", out2["alerts_active"]["undispatchable:L"])
        good = FakeRunner()
        out3 = self.run_patrol(good, minutes_later=30)
        self.assertEqual(len(good.kinds("worker-start")), 1)
        self.assertNotIn("undispatchable:L", out3["alerts_active"])

    def test_qq_alert_after_30_minutes_without_second_round_count(self):
        self.add(*running_node(), plan_ev("L", "a"))
        self.put_live(live())
        bad = FakeRunner(spec=None)
        self.run_patrol(bad)
        st = patrol.load_patrol_state("t")
        k = "L|a|spec_unreadable"
        st["undispatchable"][k]["rounds"] = 0   # only the clock can trigger now
        patrol.save_patrol_state("t", st)
        out = self.run_patrol(bad, minutes_later=31)
        self.assertIn("undispatchable:L", out["alerts_active"])

    def test_dry_run_does_not_count_rounds(self):
        self.add(*running_node(), plan_ev("L", "a"))
        self.put_live(live())
        bad = FakeRunner(spec=None)
        self.run_patrol(bad, apply=False)
        out = self.run_patrol(bad, minutes_later=5, apply=False)
        self.assertNotIn("undispatchable:L", out["alerts_active"])


class Classify(unittest.TestCase):
    def test_rejected_vs_unknown(self):
        st = {"ok": False, "why": "x"}
        self.assertIn("requires --name", patrol.classify_start(1, "", REJECT, st))
        self.assertTrue(patrol.classify_start(2, "usage: orca orchestration worker-start", "", st))
        self.assertTrue(patrol.classify_start(1, "", "unknown option --foo", st))
        self.assertTrue(patrol.classify_start(1, "", "repo not found", st))
        self.assertIsNone(patrol.classify_start(124, "", REJECT, st))      # timeout
        self.assertIsNone(patrol.classify_start(0, REJECT, "", st))
        self.assertIsNone(patrol.classify_start(1, "", "boom", st))        # unmatched text
        partial = '{"ok": false, "error": "requires x", "result": {"dispatchId": "ctx_1", "state": "failed"}}'
        self.assertIsNone(patrol.classify_start(1, partial, "", st))
        eff = '{"error": "no such thing", "result": {"effects": [{"kind": "terminal", "id": "term_1"}]}}'
        self.assertIsNone(patrol.classify_start(1, eff, "", st))


class Rejected(Base):
    def setUp(self):
        super().setUp()
        self.add(plan_ev("L", "a"))
        self.put_live(live())

    def rej(self, **kw):
        return FakeRunner(start=REJECT, start_rc=1, **kw)

    def test_rejected_blocks_with_retry_note_and_records_cache(self):
        fr = self.rej()
        out = self.run_patrol(fr)
        blk = self.events("node_block")
        self.assertEqual(len(blk), 1)
        self.assertEqual(blk[0]["blocker"], "env")
        self.assertIn("代派被 Orca 拒绝", blk[0]["summary"])
        self.assertIn("巡检会在修正后重试", blk[0]["summary"])
        rec = patrol.load_patrol_state("t")["rejected"]["L:a"]
        self.assertEqual(rec["tries"], 1)
        self.assertIn("requires", rec["last_why"])
        self.assertTrue(rec["argv_hash"])
        self.assertIn("第 1/3 次", texts(fr)[0])

    def test_unknown_failure_is_not_retried(self):
        fr = FakeRunner(start="boom", start_rc=1)
        self.run_patrol(fr)
        self.assertNotIn("代派被 Orca 拒绝", self.events("node_block")[0]["summary"])
        self.assertEqual(patrol.load_patrol_state("t")["rejected"], {})
        self.run_patrol(fr, minutes_later=200)
        self.assertEqual(len(fr.kinds("worker-start")), 1)

    def test_no_retry_when_nothing_changed_within_an_hour(self):
        fr = self.rej()
        self.run_patrol(fr)
        self.run_patrol(fr, minutes_later=15)
        self.run_patrol(fr, minutes_later=59)
        self.assertEqual(len(fr.kinds("worker-start")), 1)

    def test_retry_after_60_minutes_and_success_runs_node(self):
        self.run_patrol(self.rej())
        self.assertEqual(self.node("L", "a")["status"], "blocked")
        ok = FakeRunner()
        self.run_patrol(ok, minutes_later=61)
        self.assertEqual(len(ok.kinds("worker-start")), 1)
        n = self.node("L", "a")
        self.assertEqual((n["status"], n["worker"]), ("running", "term_new-worker"))   # fold: blocked -> running
        self.assertEqual(n["dispatch_count"], 2)
        ids = [e["id"] for e in self.events("auto_dispatch") if not e.get("phase")]
        self.assertEqual(len(set(ids)), 2)                                           # retry placeholder is a new id
        self.assertEqual(patrol.load_patrol_state("t")["rejected"], {})
        self.run_patrol(ok, minutes_later=200)
        self.assertEqual(len(ok.kinds("worker-start")), 1)

    def test_retry_early_when_argv_changed(self):
        self.run_patrol(self.rej())
        p = core.load_project("t")
        p["lines"][0]["repo"] = "repo_evb"
        core.save_project(p)
        fr = FakeRunner()
        self.run_patrol(fr, minutes_later=5)
        self.assertEqual(len(fr.kinds("worker-start")), 1)
        self.assertIn("repo_evb", fr.kinds("worker-start")[0])
        self.assertEqual(self.node("L", "a")["status"], "running")

    def test_retry_early_when_spec_ref_changed(self):
        self.run_patrol(self.rej())
        self.add(plan_ev("L", "a", 1, spec="br@docs/a2.md"))
        fr = FakeRunner()
        self.run_patrol(fr, minutes_later=5)
        self.assertEqual(len(fr.kinds("worker-start")), 1)

    def test_cap_three_tries_then_alert(self):
        fr = self.rej()
        self.run_patrol(fr)
        self.run_patrol(fr, minutes_later=61)
        out = self.run_patrol(fr, minutes_later=130)
        self.assertEqual(len(fr.kinds("worker-start")), 3)
        self.assertIn("已试 3 次", self.events("node_block")[-1]["summary"])
        out = self.run_patrol(fr, minutes_later=400)
        self.assertEqual(len(fr.kinds("worker-start")), 3)
        self.assertIn("dispatch_rejected:L:a", out["alerts_active"])

    def test_existing_task_with_planned_name_blocks_retry(self):
        self.run_patrol(self.rej())
        fr = FakeRunner(wl_rows={"live": [wl_row("ctx_9", "task_9", run="run_x", workspace="repo::/ws/l-a-p11040100")]})
        out = self.run_patrol(fr, minutes_later=61)
        self.assertEqual(fr.kinds("worker-start"), [])
        self.assertTrue(any(f["code"] == "retry_skipped" for f in out["findings"]))
        self.assertEqual(self.node("L", "a")["status"], "blocked")

    def test_live_worker_with_name_blocks_retry(self):
        self.run_patrol(self.rej())
        doc = live(minutes_ago=-60, workers=[{"worktree_name": "l-a-p11040100", "host": "wsl"}])
        self.put_live(doc)
        fr = FakeRunner()
        self.run_patrol(fr, minutes_later=61)
        self.assertEqual(fr.kinds("worker-start"), [])

    def test_unverifiable_worker_list_blocks_retry(self):
        self.run_patrol(self.rej())
        fr = FakeRunner(wl_rc=1)
        self.run_patrol(fr, minutes_later=61)
        self.assertEqual(fr.kinds("worker-start"), [])

    def test_router_touched_block_is_not_retried(self):
        self.run_patrol(self.rej())
        self.add({"kind": "node_block", "line": "L", "node": "a", "blocker": "design", "summary": "router says no",
                  "actor": {"role": "router", "handle": R}, "at": at(-30)})
        fr = FakeRunner()
        self.run_patrol(fr, minutes_later=61)
        self.assertEqual(fr.kinds("worker-start"), [])

    def test_rejected_node_listed_in_undispatchable(self):
        self.add(*running_node())
        fr = self.rej()
        self.run_patrol(fr)
        self.run_patrol(fr, minutes_later=15)
        sent = texts(fr)
        self.assertTrue(any("a（代派被 Orca 拒绝）" in t for t in sent), sent)


class ReadyStale(Base):
    def test_listed_once_with_running_nodes(self):
        self.add(*running_node(), plan_ev("L", "m", 100, auto_dispatch=False))
        self.put_live(live())
        fr = FakeRunner()
        out = self.run_patrol(fr)
        sent = texts(fr)
        self.assertEqual(len(sent), 1, sent)
        self.assertIn("m（就绪 100 分钟未派）", sent[0])
        self.assertTrue(any(f["code"] == "ready_stale" and f["node"] == "m" for f in out["findings"]))
        self.run_patrol(fr, minutes_later=10)
        self.assertEqual(len(texts(fr)), 1)

    def test_young_ready_node_not_listed(self):
        self.add(*running_node(), plan_ev("L", "m", 20, auto_dispatch=False))
        self.put_live(live())
        fr = FakeRunner()
        self.run_patrol(fr)
        self.assertEqual(texts(fr), [])

    def test_not_double_listed_with_undispatchable(self):
        self.add(*running_node(), plan_ev("L", "a", 100))
        self.put_live(live())
        fr = FakeRunner(spec=None)
        out = self.run_patrol(fr)
        sent = texts(fr)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].count("a（"), 1)
        self.assertNotIn("未派", sent[0])
        self.assertFalse(any(f["code"] == "ready_stale" for f in out["findings"]))

    def test_idle_line_gets_one_message_not_two(self):
        self.add(plan_ev("L", "m", 100, auto_dispatch=False))
        self.put_live(live())
        fr = FakeRunner()
        self.run_patrol(fr)
        self.assertEqual(len(texts(fr)), 1)

    def test_held_by_ceiling_not_listed(self):
        p = core.load_project("t")
        p["lines"][0]["max_parallel"] = 1
        core.save_project(p)
        self.add(*running_node(), plan_ev("L", "m", 100))
        self.put_live(live())
        fr = FakeRunner()
        self.run_patrol(fr)
        self.assertEqual(texts(fr), [])


if __name__ == "__main__":
    unittest.main()
