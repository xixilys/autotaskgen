"""Close idle worker terminals, QQ alerts, snapshot activity. Fakes only: no real orca / hermes / ssh / QQ."""
import datetime as dt
import json
import os
import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import alerts  # noqa: E402
import patrol  # noqa: E402
import progress_core as core  # noqa: E402
import snapshot as snap  # noqa: E402
from fakes import FakeRunner  # noqa: E402
from test_patrol import Base, NOW, R, at, live, plan_ev  # noqa: E402

PROMPT = ["done", "", "› Ask Codex"]


def dispatch_ev(nid, minutes_ago=200):
    return {"kind": "node_dispatch", "line": "L", "node": nid, "summary": "d", "at": at(minutes_ago),
            "actor": {"role": "router", "handle": R},
            "refs": {"task": "t_" + nid, "dispatch": "ctx_" + nid, "terminal": "term_w_" + nid}}


def deliver_ev(nid, minutes_ago=150):
    return {"kind": "node_deliver", "line": "L", "node": nid, "summary": "ok", "outcome": "PASS", "at": at(minutes_ago),
            "actor": {"role": "worker", "handle": "term_w_" + nid}, "refs": {"head": "abc", "report": "docs/r.md"}}


def worker(nid="a", idle=60, **kw):
    w = {"host": "wsl", "workspace": "repo::/home/u/ws/L-" + nid, "terminal": "term_w_" + nid, "line": "L", "node": nid,
         "lingering": True, "busy": False, "last_output_at": at(idle)}
    w.update(kw)
    return w


class CloseBase(Base):
    def setUp(self):
        super().setUp()
        os.environ["HERMES_BIN"] = "/fake/hermes"
        os.environ["PROGRESS_CACHE_DIR"] = self.tmp.name + "/cache"   # never touch the real task cache
        (core.home() / "qq.json").write_text(json.dumps({"target": "qqbot:T", "owner": "O"}))

    def put(self, workers, reclaimable=None, **extra):
        d = live(workers=workers)
        d["tasks"] = {"t_a": {"state": "succeeded", "completed_at": at(120)}}
        if reclaimable is not None:
            d["reclaimable"] = reclaimable
        d.update(extra)
        self.put_live(d)

    def delivered(self, nid="a"):
        self.add(plan_ev("L", nid, auto_dispatch=False), dispatch_ev(nid), deliver_ev(nid))

    def closes(self, fr):
        return fr.kinds("close")


class CloseSelection(CloseBase):
    def test_idle_delivered_worker_is_closed(self):
        self.delivered()
        self.put([worker()], reclaimable=[])
        fr = FakeRunner(screen=PROMPT)
        out = self.run_patrol(fr)
        self.assertEqual(len(self.closes(fr)), 1)
        argv = self.closes(fr)[0]
        self.assertEqual(argv[argv.index("--environment") + 1], "wsl")
        self.assertEqual(argv[argv.index("--terminal") + 1], "term_w_a")
        ev = self.events("worker_release")[-1]
        self.assertEqual((ev["result"], ev["refs"]["terminal"]), ("closed", "term_w_a"))
        self.assertIn("关闭空闲 worker 终端", ev["summary"])
        self.assertTrue(any(r["type"] == "worker_close" and r["closed"] for r in out["results"]))
        self.assertFalse(fr.kinds("worker-release"))  # not reclaimable: no release attempt

    def test_dry_run_plans_nothing(self):
        self.delivered()
        self.put([worker()], reclaimable=[])
        fr = FakeRunner(screen=PROMPT)
        out = self.run_patrol(fr, apply=False)
        self.assertFalse(self.closes(fr))
        self.assertFalse([a for a in out["actions"] if a["type"] == "worker_close"])

    def test_skips(self):
        self.delivered()
        cases = {
            "busy": worker(busy=True),
            "not lingering": worker(lingering=False),
            "recent output": worker(idle=10),
            "no activity info": worker(last_output_at=None),
            "router handle": worker(terminal=R),
        }
        for name, w in cases.items():
            self.put([w], reclaimable=[])
            fr = FakeRunner(screen=PROMPT)
            self.run_patrol(fr)
            self.assertFalse(self.closes(fr), name)

    def test_node_running_is_skipped(self):
        self.add(plan_ev("L", "a", auto_dispatch=False, expected_duration_minutes=600), dispatch_ev("a"))
        self.put([worker()], reclaimable=[])
        fr = FakeRunner(screen=PROMPT)
        self.run_patrol(fr)
        self.assertFalse(self.closes(fr))

    def test_unmapped_needs_120_minutes(self):
        self.add(plan_ev("L", "z", auto_dispatch=False))
        self.put([worker(line=None, node=None, idle=90)], reclaimable=[])
        fr = FakeRunner(screen=PROMPT)
        self.run_patrol(fr)
        self.assertFalse(self.closes(fr))
        self.put([worker(line=None, node=None, idle=130)], reclaimable=[])
        self.run_patrol(fr)
        self.assertEqual(len(self.closes(fr)), 1)
        self.assertIn("未归属", self.events("worker_release")[-1]["summary"])

    def test_screen_working_or_unreadable_is_skipped(self):
        self.delivered()
        self.put([worker()], reclaimable=[])
        for screen in (["• Working (3m • esc to interrupt)", "› "], ["⠋ thinking"], None):
            fr = FakeRunner(screen=screen)
            self.run_patrol(fr)
            self.assertFalse(self.closes(fr), screen)

    def test_stale_live_closes_nothing(self):
        self.delivered()
        d = live(workers=[worker()], minutes_ago=45)
        self.put_live(d)
        fr = FakeRunner(screen=PROMPT)
        self.run_patrol(fr)
        self.assertFalse(self.closes(fr))

    def test_per_round_cap(self):
        ws = []
        for i in range(8):
            nid = "n%d" % i
            self.delivered(nid)
            ws.append(worker(nid))
        self.put(ws, reclaimable=[])
        fr = FakeRunner(screen=PROMPT)
        out = self.run_patrol(fr)
        self.assertEqual(len(self.closes(fr)), patrol.CLOSE_PER_ROUND)
        self.assertTrue(any(f["code"] == "close_rate_limited" for f in out["findings"]))


class ReleaseThenClose(CloseBase):
    def test_release_first_when_reclaimable_then_no_close(self):
        self.delivered()
        self.put([worker()], reclaimable=["ctx_a"])
        fr = FakeRunner(screen=PROMPT)
        self.run_patrol(fr)
        # release_candidates and close_candidates both see it: one release, no close needed
        self.assertEqual(len(fr.kinds("worker-release")), 1)
        self.assertFalse(self.closes(fr))

    def test_retained_result_falls_back_to_close(self):
        self.delivered()
        self.put([worker()])   # no reclaimable list: close path decides on its own
        fr = FakeRunner(screen=PROMPT, release={"ok": True, "result": {"terminalState": "retained"}})
        self.run_patrol(fr)
        self.assertEqual(len(fr.kinds("worker-release")), 1)
        self.assertEqual(len(self.closes(fr)), 1)
        evs = self.events("worker_release")
        self.assertEqual([e["result"] for e in evs], ["retained", "closed"])

    def test_no_dispatch_goes_straight_to_close(self):
        self.add(plan_ev("L", "a", auto_dispatch=False),
                 {"kind": "node_block", "line": "L", "node": "a", "blocker": "env", "summary": "b", "at": at(150),
                  "actor": {"role": "router", "handle": R}})
        self.put([worker()], reclaimable=[])
        fr = FakeRunner(screen=PROMPT)
        self.run_patrol(fr)
        self.assertFalse(fr.kinds("worker-release"))
        self.assertEqual(len(self.closes(fr)), 1)


class CloseTries(CloseBase):
    def test_two_failures_then_close_failed_alert(self):
        self.delivered()
        self.put([worker()], reclaimable=[])
        fr = FakeRunner(screen=PROMPT, close_rc=1)
        self.run_patrol(fr)
        self.run_patrol(fr, minutes_later=5)
        self.assertEqual(len(self.closes(fr)), 2)
        self.assertEqual([e["result"].split(":")[0] for e in self.events("worker_release")], ["close_failed"] * 2)
        self.put([worker()], reclaimable=[])
        out = self.run_patrol(fr, minutes_later=10)
        self.assertEqual(len(self.closes(fr)), 2)   # capped
        self.assertTrue(any(f["code"] == "close_failed" for f in out["findings"]))
        self.assertIn("close_failed:term_w_a", out["alerts_active"])
        sent = [c[c.index("-q") + 1] for c in fr.calls if c[0] == "/fake/hermes"]
        self.assertTrue(any("关闭失败" in m for m in sent))


class AlertBuilding(CloseBase):
    def test_monitoring_keys(self):
        d = live(workers=[])
        d["hosts"] = {"mac": "ok", "wsl": "unknown", "ib-agent": "unknown"}
        d["errors"] = [{"host": "wsl", "error": "ps 失败或主机不可达"}]
        d["screen_reads"] = {"tried": 4, "failed": 2}
        self.put_live(d)
        out = self.run_patrol(FakeRunner())
        keys = set(out["alerts_active"])
        self.assertIn("monitor:host:wsl", keys)
        self.assertNotIn("monitor:host:ib-agent", keys)
        self.assertIn("monitor:err:wsl:ps 失败或主机不可达", keys)
        self.assertIn("monitor:screen_reads", keys)
        self.assertTrue(any(r.get("sent") for r in out["alerts"]))
        self.assertTrue([e for e in self.events("observe") if (e.get("refs") or {}).get("key", "").startswith("alert:")])

    def test_screen_reads_below_threshold_or_few_tried(self):
        for sr in ({"tried": 2, "failed": 2}, {"tried": 4, "failed": 1}):
            d = live(workers=[])
            d["screen_reads"] = sr
            self.put_live(d)
            out = self.run_patrol(FakeRunner())
            self.assertNotIn("monitor:screen_reads", out["alerts_active"])

    def test_stale_live_key(self):
        self.put_live(live(minutes_ago=50, workers=[]))
        out = self.run_patrol(FakeRunner())
        self.assertIn("monitor:live", out["alerts_active"])

    def test_stuck_after_auto_continue_exhausted(self):
        self.add(plan_ev("L", "a", auto_dispatch=False, expected_duration_minutes=600), dispatch_ev("a"))
        sc = {"flag": "error", "line": "stream disconnected", "checked_at": at(1), "since": at(15),
              "transient": True, "prompt": True}
        w = worker(screen=sc, lingering=False)
        d = live(workers=[w])
        d["coordinators"] = {}
        self.put_live(d)
        fr = FakeRunner()
        for i in range(3):
            out = self.run_patrol(fr, minutes_later=i)
        self.assertIn("stuck:term_w_a", out["alerts_active"])
        self.assertIn("连续 3 次", out["alerts_active"]["stuck:term_w_a"])

    def test_followup_alerts(self):
        st = {"followups": {
            "fu_h": {"id": "fu_h", "line": "L", "to": "human", "text": "请确认端点", "check_at": at(10)},
            "fu_t": {"id": "fu_t", "line": "L", "to": "tooling", "text": "指标算错", "check_at": at(10)},
            "fu_t2": {"id": "fu_t2", "line": "L", "to": "tooling", "tooling_handle": "term_x", "text": "有人管", "check_at": at(10)},
            "fu_r": {"id": "fu_r", "line": "L", "to": "router", "text": "给 router", "check_at": at(10)},
            "fu_future": {"id": "fu_future", "line": "L", "to": "human", "text": "还没到点", "check_at": at(-60)},
            "fu_judged": {"id": "fu_judged", "line": "L", "to": "human", "text": "判过未落实", "check_at": at(-360),
                          "tries": 1, "human": True},
            "fu_done": {"id": "fu_done", "line": "L", "to": "human", "text": "已关", "check_at": at(10), "closed_at": at(5)}}}
        cache = snap.load_task_cache()
        cache["supervisor:t"] = st
        snap.save_task_cache(cache)
        self.put_live(live(workers=[]))
        out = self.run_patrol(FakeRunner())
        fus = {k for k in out["alerts_active"] if k.startswith("fu:")}
        self.assertEqual(fus, {"fu:fu_h", "fu:fu_t", "fu:fu_judged"})  # judged-open stays active (no false 已恢复)
        self.assertIn("请确认端点", out["alerts_active"]["fu:fu_h"])

    def test_human_ask_dropped_once_line_lifts_human_block(self):
        import patrol
        ev = [{"kind": "node_block", "line": "L", "node": "n1", "blocker": "human", "at": "2026-11-06T05:31:00Z",
               "summary": "Jacobi初态grid/RHS/边界实值、精度/终止未定由协调补齐"},
              {"kind": "node_block", "line": "L", "node": "n1", "blocker": "dep", "at": "2026-11-06T06:01:00Z"}]
        self.assertTrue(patrol.human_wait_lifted(ev, "L", "2026-11-06T05:03:00Z"))
        self.assertFalse(patrol.human_wait_lifted(ev, "L", "2026-11-06T06:30:00Z"))  # lifted before the ask
        self.assertFalse(patrol.human_wait_lifted(ev, "M", "2026-11-06T05:03:00Z"))
        still = ev + [{"kind": "node_block", "line": "L", "node": "n2", "blocker": "human", "at": "2026-11-06T06:10:00Z"}]
        self.assertFalse(patrol.human_wait_lifted(still, "L", "2026-11-06T05:03:00Z"))  # another node still waits on the human
        self.assertFalse(patrol.human_wait_lifted(ev[:1], "L", "2026-11-06T05:03:00Z"))  # never lifted
        st = {"followups": {"fu_h": {"id": "fu_h", "line": "L", "to": "human", "check_at": at(-60),
                                     "text": "请提供或确认 Jacobi 64³ 的初始 grid 实值或 input_ref、执行精度",
                                     "human": True, "sent_at": "2026-11-06T05:03:00Z"}}}
        cache = snap.load_task_cache()
        cache["supervisor:t"] = st
        snap.save_task_cache(cache)
        now = core.parse_iso("2026-11-06T07:21:00Z")
        self.assertEqual(patrol.followup_alerts({"id": "t"}, now, ev), {})
        self.assertIn("fu:fu_h", patrol.followup_alerts({"id": "t"}, now, ev[:1]))

    def test_human_ask_kept_when_an_unrelated_human_block_lifts(self):
        import patrol
        ask = "EVB 线：请在 Instabox 恢复 ib-agent-2，或说明目前无法恢复的原因；在机器恢复并通过资源检查前，EVB 无法继续独立面积与功耗校准。"
        ev = [{"kind": "node_block", "line": "EVB", "node": "cg16", "blocker": "human", "at": "2026-11-06T23:17:00Z",
               "summary": "H内部批效果sidecar及固定投影等核心一裁定；已获答复继续独立日志修复与两点估算"},
              {"kind": "node_dispatch", "line": "EVB", "node": "cg16", "at": "2026-11-06T23:18:00Z"}]
        self.assertFalse(patrol.human_wait_lifted(ev, "EVB", "2026-11-06T22:34:45Z", ask))
        self.assertTrue(patrol.human_wait_lifted(ev, "EVB", "2026-11-06T22:34:45Z"))   # without the ask: line level
        self.assertTrue(patrol.lift_matches("FE 线：请带回真实评估器首个回复、明确端点和实际调用授权",
                                            "等待人类带回真实评估器首回复、明确端点和实际调用授权；三项齐备后才启动"))
        self.assertFalse(patrol.lift_matches(ask, "构建前唯一SConscript差异为shared已去旧FabricSliceDevice注册，等确认保留"))

    def test_human_ask_dropped_when_a_coordinator_names_it_settled(self):
        import patrol
        fu = {"id": "fu_d3", "line": "FE", "to": "human", "check_at": at(-60), "human": True, "tries": 1,
              "text": "请决定并提供 F5 的真实评估器调用预算授权", "sent_at": "2026-11-07T01:19:31Z",
              "history": [{"at": "2026-11-07T04:32:12Z", "what": "not_done"}]}
        coord = {"role": "coordinator", "handle": "c1"}
        done = {"kind": "project_update", "at": "2026-11-07T08:03:23Z", "actor": coord,
                "summary": "跟进fu_d3已落实：L46人类批准按需，F5只等B13a"}
        self.assertTrue(patrol.fu_settled([done], fu))
        self.assertFalse(patrol.fu_settled([dict(done, summary="跟进fu_d3未落实，仍等授权")], fu))
        self.assertFalse(patrol.fu_settled([dict(done, actor={"role": "worker"})], fu))
        self.assertFalse(patrol.fu_settled([dict(done, at="2026-11-07T04:00:00Z")], fu))      # before terra's recheck
        self.assertFalse(patrol.fu_settled([dict(done, summary="L46已批，按需")], fu))         # no id: terra decides
        later = dict(fu, history=fu["history"] + [{"at": "2026-11-07T10:32:00Z", "what": "not_done"}])
        self.assertFalse(patrol.fu_settled([done], later))                                   # terra re-arms it
        cache = snap.load_task_cache()
        cache["supervisor:t"] = {"followups": {"fu_d3": fu}}
        snap.save_task_cache(cache)
        now = core.parse_iso("2026-11-07T09:37:00Z")
        self.assertEqual(patrol.followup_alerts({"id": "t"}, now, [done]), {})
        self.assertIn("fu:fu_d3", patrol.followup_alerts({"id": "t"}, now, []))

    def test_dry_run_sends_nothing(self):
        self.put_live(live(minutes_ago=50, workers=[]))
        fr = FakeRunner()
        out = self.run_patrol(fr, apply=False)
        self.assertNotIn("alerts", out)
        self.assertFalse([c for c in fr.calls if c[0] == "/fake/hermes"])


class AlertsSync(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["PROGRESS_HOME"] = self.tmp.name
        os.environ["HERMES_BIN"] = "/fake/hermes"
        os.environ["PROGRESS_CACHE_DIR"] = self.tmp.name + "/cache"
        self.qq = {"target": "qqbot:T", "owner": "O"}
        self.fr = FakeRunner()
        self.calls = lambda: [c[c.index("-q") + 1] for c in self.fr.calls if c[0] == "/fake/hermes"]

    def tearDown(self):
        self.tmp.cleanup()

    def sync(self, active, hours=0.0):
        return alerts.sync(active, NOW + dt.timedelta(hours=hours), self.fr, self.qq)

    def test_fake_runner_handles_hermes(self):
        # FakeRunner has no hermes branch: it answers rc=1 "unexpected"; use a tiny runner for sends
        pass

    def runner(self):
        sent = []

        def run(argv, input_text=None, timeout=60):
            sent.append(argv[argv.index("-q") + 1])
            return 0, "", ""
        run.sent = sent
        return run

    def test_human_request_resolved_wording(self):
        r = self.runner()
        self.fr = r
        self.sync({"fu:fu_1": "FE 线：请给端点"}, 0)
        self.sync({}, 1)
        self.assertEqual(len(r.sent), 1)             # gone: waits FU_GONE_S for a reworded comeback first
        self.sync({}, 2.1)
        self.assertEqual(len(r.sent), 2)
        self.assertTrue(r.sent[-1].startswith("[不用再管] FE 线：请给端点（"))

    def test_reworded_human_request_merges_without_new_send(self):
        r = self.runner()
        self.fr = r
        a = "EVB 线：请在 Instabox 启动 ib-agent-2，供 EVB 继续独立综合校准；不恢复该机器，面积与功耗模型的独立校准无法继续。"
        b = "EVB 线：请在 Instabox 恢复 ib-agent-2，或说明目前无法恢复的原因；在机器恢复并通过资源检查前，EVB 无法继续独立面积与功耗校准。"
        self.sync({"fu:fu_a": a}, 0)
        self.sync({}, 0.5)                          # dropped out: no "[不用再管]" yet
        self.sync({"fu:fu_b": b}, 0.8)               # reworded comeback: same ask, nothing new sent
        self.assertEqual(len(r.sent), 1)
        st = alerts.load()
        self.assertEqual((st["fu:fu_b"]["same_as"], st["fu:fu_b"]["sends"]), ("fu:fu_a", 1))
        self.assertEqual(st["fu:fu_a"]["merged_into"], "fu:fu_b")
        self.sync({"fu:fu_b": b}, 2.1)               # reminder pacing continues from the first send
        self.assertEqual(len(r.sent), 2)
        self.assertIn("已持续 2 小时", r.sent[-1])
        self.sync({"fu:fu_b": b}, 3)
        self.assertEqual(len(r.sent), 2)             # the merged-away key never says "[不用再管]"

    def test_two_live_rewordings_of_one_ask_send_once(self):
        r = self.runner()
        self.fr = r
        a = "EVB 线：请在 Instabox 启动 ib-agent-2，供 EVB 继续独立综合校准；不恢复该机器，面积与功耗模型的独立校准无法继续。"
        b = "EVB 线：请在 Instabox 恢复 ib-agent-2，或说明目前无法恢复的原因；在机器恢复并通过资源检查前，EVB 无法继续独立面积与功耗校准。"
        self.sync({"fu:fu_a": a}, 0)
        self.sync({}, 0.5)
        self.sync({"fu:fu_b": b}, 0.8)               # takes fu_a's record over
        self.sync({"fu:fu_a": a, "fu:fu_b": b}, 1.2)  # 11-07: both live again -> fu_a went out again as new
        self.assertEqual(len(r.sent), 1)
        self.sync({"fu:fu_a": a, "fu:fu_b": b}, 2.1)  # one reminder, paced from the first send
        self.assertEqual(len(r.sent), 2)
        self.assertIn("已持续 2 小时", r.sent[-1])
        self.assertNotIn("\n", r.sent[-1])
        self.sync({"fu:fu_a": a, "fu:fu_b": b}, 2.5)
        self.assertEqual(len(r.sent), 2)

    def test_two_new_rewordings_in_one_round_send_once(self):
        r = self.runner()
        self.fr = r
        self.sync({"fu:fu_a": "EVB 线：请在 Instabox 启动 ib-agent-2，供 EVB 继续独立综合校准",
                   "fu:fu_b": "EVB 线：请在 Instabox 恢复 ib-agent-2，供 EVB 继续独立综合校准"}, 0)
        self.assertEqual(len(r.sent), 1)
        self.sync({"fu:fu_a": "EVB 线：请在 Instabox 启动 ib-agent-2，供 EVB 继续独立综合校准",
                   "fu:fu_b": "EVB 线：请在 Instabox 恢复 ib-agent-2，供 EVB 继续独立综合校准"}, 0.5)
        self.assertEqual(len(r.sent), 1)

    def test_different_human_requests_of_a_line_stay_apart(self):
        r = self.runner()
        self.fr = r
        self.sync({"fu:fu_a": "EVB 线：请在 Instabox 启动 ib-agent-2，供 EVB 继续独立综合校准"}, 0)
        self.sync({"fu:fu_a": "EVB 线：请在 Instabox 启动 ib-agent-2，供 EVB 继续独立综合校准",
                   "fu:fu_c": "EVB 线：请决定是否批准 B8 的第五次单次增量构建；未获批准，四个 CG 实跑不能继续"}, 0.5)
        self.assertEqual(len(r.sent), 2)
        self.assertTrue(r.sent[-1].startswith("[需要你看] EVB 线：请决定"))
        self.sync({"fu:fu_x": "FE 线：请在 Instabox 启动 ib-agent-2，供 EVB 继续独立综合校准"}, 0.6)   # other line
        self.assertEqual(len(r.sent), 3)

    def test_new_reminders_max_and_resolve(self):
        r = self.runner()
        self.fr = r
        self.calls = lambda: r.sent
        a = {"k1": "监控坏了\n细节"}
        rec = self.sync(a, 0)
        self.assertEqual((rec[0]["kind"], rec[0]["sent"]), ("new", True))
        self.assertTrue(r.sent[0].startswith("[需要你看] "))
        self.sync(a, 1)
        self.assertEqual(len(r.sent), 1)             # nothing due before 2 h
        self.sync(a, 2.1)
        self.assertEqual(len(r.sent), 2)
        self.sync(a, 7)
        self.assertEqual(len(r.sent), 2)
        self.sync(a, 8.1)
        self.assertEqual(len(r.sent), 3)
        self.sync(a, 24.1)
        self.assertEqual(len(r.sent), 4)
        self.sync(a, 100)
        self.assertEqual(len(r.sent), 4)             # max 4 sends, silent but kept
        self.assertEqual(alerts.load()["k1"]["sends"], 4)
        rec = self.sync({}, 101)
        self.assertEqual(rec[0]["kind"], "resolved")
        self.assertEqual(r.sent[-1], "[已恢复] 监控坏了")
        self.assertTrue(alerts.load()["k1"]["resolved_at"])
        self.sync({}, 102)
        self.assertEqual(len(r.sent), 5)             # resolved once only
        self.sync(a, 103)                            # active again: starts over
        self.assertEqual(len(r.sent), 6)
        self.assertEqual(alerts.load()["k1"]["sends"], 1)

    def test_merge_into_one_message(self):
        r = self.runner()
        self.fr = r
        rec = self.sync({"a": "甲出问题", "b": "乙出问题", "c": "丙出问题"})
        self.assertEqual(len(r.sent), 1)
        self.assertEqual(len(rec), 3)
        for t in ("甲出问题", "乙出问题", "丙出问题"):
            self.assertIn(t, r.sent[0])
        self.assertLessEqual(len(r.sent[0]), alerts.MAX_CHARS)
        # a resolve and a new one share the next message
        self.sync({"d": "丁出问题"}, 1)
        self.assertEqual(len(r.sent), 2)
        self.assertIn("[已恢复] 甲出问题", r.sent[1])
        self.assertIn("[需要你看] 丁出问题", r.sent[1])

    def test_failed_send_is_retried(self):
        def bad(argv, input_text=None, timeout=60):
            return 1, "", "down"
        self.fr = bad
        rec = self.sync({"k": "x"})
        self.assertFalse(rec[0]["sent"])
        self.assertEqual(alerts.load(), {})
        r = self.runner()
        self.fr = r
        self.sync({"k": "x"}, 0.1)
        self.assertEqual(len(r.sent), 1)

    def test_qq_missing(self):
        self.qq = None
        self.assertEqual(self.sync({"k": "x"}), [{"code": "qq_unconfigured",
                                                  "why": "~/.progress/qq.json 缺失：告警不会发到 QQ"}])
        self.assertEqual(self.sync({}), [])
        self.assertEqual(alerts.load(), {})

    def test_never_raises(self):
        def boom(argv, input_text=None, timeout=60):
            raise RuntimeError("x")
        self.fr = boom
        rec = self.sync({"k": "x"})
        self.assertEqual(rec[0]["code"], "alerts_failed")

    def test_long_texts_fit_in_limit(self):
        r = self.runner()
        self.fr = r
        self.sync({"k%02d" % i: "很长的告警" * 80 for i in range(10)})
        self.assertLessEqual(len(r.sent[0]), alerts.MAX_CHARS)
        self.sync({"k%02d" % i: "很长的告警" * 80 for i in range(10)}, 0.1)
        self.assertEqual(len(r.sent), 2)             # the ones that did not fit go out next round


class SnapshotActivity(unittest.TestCase):
    def test_attach_and_error(self):
        ws = [{"terminal": "term_e31a928e"}, {"terminal": "term_eb26fc9d"}, {"terminal": "term_none"}]
        errors = []
        terms = [{"handle": "term_e31a928e-uuid", "lastOutputAt": 1762430400000, "title": "⠋ Fix the thing"},
                 {"handle": "term_eb26fc9d", "lastOutputAt": 1762430400000, "title": "Codex idle " + "x" * 100}]
        fr = FakeRunner(wsl_terminals=terms)
        snap.collect_worker_activity(ws, fr, "orca", errors)
        self.assertEqual(errors, [])
        self.assertTrue(ws[0]["busy"])
        self.assertEqual(ws[0]["last_output_at"], snap.ms_to_iso(1762430400000))
        self.assertFalse(ws[1]["busy"])
        self.assertEqual(len(ws[1]["title"]), 80)
        self.assertEqual((ws[2]["last_output_at"], ws[2]["title"], ws[2]["busy"]), (None, None, None))
        call = fr.kinds("list")[0]
        self.assertIn("--environment", call)

    def test_failure_is_recorded(self):
        ws = [{"terminal": "term_a"}]
        errors = []
        snap.collect_worker_activity(ws, FakeRunner(wsl_rc=1), "orca", errors)
        self.assertEqual(errors[0]["host"], "wsl")
        self.assertEqual(errors[0]["error"], "orca terminal list --environment wsl 失败")
        self.assertIsNone(ws[0]["busy"])

    def test_collect_counts_screen_reads(self):
        proj = {"schema": core.SCHEMA_PROJECT, "id": "t", "name": "t", "final_goal": {"text": "g"}, "cores": [],
                "lines": [{"id": "L", "name": "l", "core": "c1", "router": R}]}
        terms = [{"handle": R, "connected": True, "lastOutputAt": 1762430400000}]
        fr = FakeRunner(terminals=terms, ps="", screen=None)   # every screen read fails
        doc = snap.collect(proj, None, fr, now=NOW)
        self.assertEqual(doc["screen_reads"], {"tried": 1, "failed": 1})
        fr = FakeRunner(terminals=terms, ps="", screen=["› "])
        doc = snap.collect(proj, None, fr, now=NOW)
        self.assertEqual(doc["screen_reads"], {"tried": 1, "failed": 0})


if __name__ == "__main__":
    unittest.main()
