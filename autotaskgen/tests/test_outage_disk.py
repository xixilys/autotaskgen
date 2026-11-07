"""Outage / stall alerts, terra backoff, screen signals, disk alert routing, strict worktree cleanup.
Fakes only: no real orca / ssh / hermes / model."""
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
import alerts  # noqa: E402
import patrol  # noqa: E402
import progress_core as core  # noqa: E402
import snapshot as snap  # noqa: E402
import supervisor as sup  # noqa: E402
from fakes import FakeRunner  # noqa: E402
from test_patrol import Base, NOW, R, C1, at, live, plan_ev, project  # noqa: E402
import test_supervisor as ts  # noqa: E402

ROOT = "/home/u/orca/workspaces/dse_harness"
WT = ROOT + "/wt-a"
API504 = "API Error: 504 magpie held this request for 30s without routing it to any provider, try again"


def dispatch(nid, wt=None, minutes_ago=900, terminal="w"):
    refs = {"task": "t_" + nid, "dispatch": "ctx_" + nid, "terminal": terminal}
    if wt:
        refs["worktree"] = "repo::" + wt
    return {"kind": "node_dispatch", "line": "L", "node": nid, "summary": "d", "at": at(minutes_ago),
            "actor": {"role": "router", "handle": R}, "refs": refs}


def deliver(nid, minutes_ago=850):
    return {"kind": "node_deliver", "line": "L", "node": nid, "summary": "ok", "outcome": "PASS", "at": at(minutes_ago),
            "actor": {"role": "worker", "handle": "w"}, "refs": {"head": "abc", "report": "docs/r.md"}}


def review(nid, minutes_ago=800):
    return {"kind": "node_review", "line": "L", "node": nid, "summary": "ok", "outcome": "PASS", "at": at(minutes_ago),
            "actor": {"role": "router", "handle": R}}


def done_node(nid="a", wt=WT, minutes_ago=900):
    """plan + dispatch (into wt) + deliver + review: a node whose worktree may be cleaned."""
    return [plan_ev("L", nid, minutes_ago=minutes_ago + 50, auto_dispatch=False), dispatch(nid, wt, minutes_ago),
            deliver(nid, minutes_ago - 50), review(nid, minutes_ago - 100)]


def with_screens(doc, router_line=None, coord_line=None, workers=()):
    if router_line:
        doc["routers"]["L"]["screen"] = {"transient": True, "prompt": True, "line": router_line, "since": at(5)}
    if coord_line:
        doc["coordinators"] = {"c1": {"handle": C1, "present": True, "core": "c1", "screen_flag": None,
                                      "screen": {"transient": True, "prompt": True, "line": coord_line, "since": at(5)}}}
    doc["workers"] = list(workers)
    return doc


class OBase(Base):
    def setUp(self):
        super().setUp()
        self.saved = {k: os.environ.get(k) for k in ("PROGRESS_CACHE_DIR", "HERMES_BIN", "PROGRESS_WT_ROOT")}
        os.environ["PROGRESS_CACHE_DIR"] = self.tmp.name + "/cache"
        os.environ["HERMES_BIN"] = "/fake/hermes"
        os.environ.pop("PROGRESS_WT_ROOT", None)
        (core.home() / "qq.json").write_text(json.dumps({"target": "qqbot:T", "owner": "O"}))

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def qq(self, fr):
        return [c[c.index("-q") + 1] for c in fr.calls if c[0] == "/fake/hermes"]

    def terminal_sends(self, fr):
        return [(c[c.index("--terminal") + 1], c[c.index("--text") + 1]) for c in fr.kinds("send") if c[0] != "/fake/hermes"]

    def active(self, doc, minutes=0):
        self.put_live(doc)
        return self.run_patrol(FakeRunner(), minutes_later=minutes)["alerts_active"]


class ModelOutage(OBase):
    def test_two_screens_make_outage_one_does_not(self):
        a = self.active(with_screens(live(), router_line=API504))
        self.assertNotIn("outage:model", a)
        a = self.active(with_screens(live(), router_line=API504, coord_line="Error: 502 bad gateway"))
        self.assertIn("outage:model", a)
        self.assertIn("模型网关疑似故障：2 个 agent 屏幕报", a["outage:model"])
        self.assertIn("504", a["outage:model"])
        self.assertIn("terra 连续 0 次调用失败", a["outage:model"])
        self.assertIn("自动继续会在恢复后重试", a["outage:model"])

    def test_workers_count_as_distinct_screens(self):
        w = [{"host": "wsl", "workspace": "repo::/home/u/ws/x", "terminal": "tw%d" % i, "line": "L", "node": "a%d" % i,
              "screen": {"flag": "error", "transient": True, "prompt": False, "line": API504, "since": at(5)}} for i in range(2)]
        self.assertIn("outage:model", self.active(with_screens(live(), workers=w)))
        self.assertNotIn("outage:model", self.active(with_screens(live(), workers=w[:1])))

    def test_terra_two_failed_calls(self):
        cache = snap.load_task_cache()
        cache["supervisor:t"] = {"fail_streak": 1}
        snap.save_task_cache(cache)
        self.assertNotIn("outage:model", self.active(live()))
        cache["supervisor:t"] = {"fail_streak": 2}
        snap.save_task_cache(cache)
        a = self.active(live())
        self.assertIn("terra 连续 2 次调用失败", a["outage:model"])

    def test_sent_then_resolved(self):
        self.put_live(with_screens(live(), router_line=API504, coord_line=API504))
        fr = FakeRunner()
        self.run_patrol(fr)
        self.assertTrue(any("[需要你看] 模型网关疑似故障" in t for t in self.qq(fr)))
        self.put_live(live(minutes_ago=2))
        fr2 = FakeRunner()
        self.run_patrol(fr2, minutes_later=5)
        self.assertTrue(any("[已恢复] 模型网关疑似故障" in t for t in self.qq(fr2)))


class ProjectStall(OBase):
    def setUp(self):
        super().setUp()
        self.add(plan_ev("L", "a", minutes_ago=300, auto_dispatch=False), dispatch("a", minutes_ago=90))
        self.put_live(live())

    def go(self, minutes=0):
        return self.run_patrol(FakeRunner(), minutes_later=minutes)["alerts_active"]

    def test_stall_after_sixty_minutes(self):
        a = self.go()
        self.assertIn("stall:project", a)
        self.assertIn("全部主线已 90 分钟没有 router/worker 事件（最后一条 L ", a["stall:project"])

    def test_recent_event_or_short_gap_no_stall(self):
        self.add(dispatch("a", minutes_ago=30))   # a second dispatch event 30 min ago
        self.assertNotIn("stall:project", self.go())

    def test_patrol_events_do_not_count_and_no_work_no_stall(self):
        self.add({"kind": "observe", "line": "L", "actor": {"role": "patrol", "handle": "patrol@mac"},
                  "summary": "x", "at": at(5), "refs": {"key": "k"}})
        self.assertIn("stall:project", self.go())
        self.add({"kind": "node_cancel", "line": "L", "node": "a", "summary": "no", "at": at(80),
                  "actor": {"role": "router", "handle": R}})
        self.assertNotIn("stall:project", self.go())   # nothing running or ready any more

    def test_resolves(self):
        fr = FakeRunner()
        self.run_patrol(fr)
        self.assertTrue(any("[需要你看] 全部主线" in t for t in self.qq(fr)))
        self.add(dispatch("a", minutes_ago=-4))   # router writes again
        fr2 = FakeRunner()
        self.run_patrol(fr2, minutes_later=5)
        self.assertTrue(any("[已恢复] 全部主线" in t for t in self.qq(fr2)))


class TerraNonBlocking(OBase):
    def test_deterministic_work_happens_before_the_model_call(self):
        self.add(plan_ev("L", "a"))   # ready, auto-dispatchable
        self.put_live(live())
        fr = FakeRunner()
        seen = {}

        def model(prompt, workdir, env, timeout=0):
            seen["worker_start"] = bool(fr.kinds("worker-start"))
            return 124, "", "", "timeout after 600s"
        out = patrol.run("t", apply=True, runner=fr, now=NOW, store=snap.Store("t", fr), supervisor=True,
                         model_call=model)
        self.assertTrue(seen["worker_start"])
        self.assertTrue(out["supervisor"]["ran"])
        self.assertEqual(sup.TIMEOUT_S, 1200)  # terra max (11-07 router rules R5)

    def test_backoff_after_failed_call_then_retry_and_success_clears(self):
        self.put_live(live())
        calls = []

        def run_at(minutes, rc, msg=""):
            fr = FakeRunner()
            good = ts.Fake(msg=msg or ts.good(), rc=rc)

            def model(*a, **k):
                calls.append(minutes)
                return good(*a, **k)
            return patrol.run("t", apply=True, runner=fr, now=NOW + dt.timedelta(minutes=minutes),
                              store=snap.Store("t", fr), supervisor=True, model_call=model)["supervisor"]
        self.assertTrue(run_at(0, 124, msg="x")["ran"])
        self.assertEqual(sup.load_state("t")["fail_streak"], 1)
        info = run_at(35, 0)                          # due by the 30-min clock, but terra failed 35 min ago
        self.assertEqual((info["ran"], info["reason"]), (False, "backoff"))
        self.assertEqual(calls, [0])
        self.assertTrue(run_at(61, 124, msg="x")["ran"])   # 60 min after the failure: tried again, fails again
        self.assertEqual(sup.load_state("t")["fail_streak"], 2)
        self.assertEqual(run_at(95, 0)["reason"], "backoff")
        self.assertTrue(run_at(125, 0)["ran"])
        st = sup.load_state("t")
        self.assertEqual((st["fail_streak"], st.get("last_fail_at")), (0, None))

    def test_force_ignores_backoff(self):
        st = {"last_call_at": snap.iso(NOW), "last_fail_at": snap.iso(NOW), "fail_streak": 1}
        sup.save_state("t", st)
        self.assertTrue(sup.backing_off(st, NOW + dt.timedelta(minutes=59)))
        self.assertFalse(sup.backing_off(st, NOW + dt.timedelta(minutes=60)))


class ScreenSignals(unittest.TestCase):
    def test_magpie_504_is_transient(self):
        sig = snap.screen_signals(["working on it", API504, "", "❯ "])
        self.assertTrue(sig["transient"])
        self.assertTrue(sig["prompt"])
        self.assertIn("504", sig["line"])

    def test_markers(self):
        for line in ("API Error: 500 internal", "request failed with status 502", "HTTP 504 from gateway",
                     "held this request", "stopped without routing it"):
            self.assertTrue(snap.screen_signals([line])["transient"], line)

    def test_numbers_alone_are_not_errors(self):
        self.assertFalse(snap.screen_signals(["wrote 504 rows", "took 502 ms"])["transient"])

    def test_prompts(self):
        self.assertTrue(snap.screen_signals(["done", "❯ hi"])["prompt"])
        self.assertTrue(snap.screen_signals(["done", "› Ask Codex"])["prompt"])
        self.assertFalse(snap.screen_signals(["done", "$ "])["prompt"])
        self.assertFalse(snap.screen_signals([API504, "esc to interrupt"])["transient"])   # still working


def with_disks(doc, mac=None, wsl=None):
    doc["coordinators"] = {"c1": {"handle": C1, "present": True, "core": "c1", "screen_flag": None}}
    doc["resources"] = {}
    for host, gb, mount in (("mac", mac, "/System/Volumes/Data"), ("wsl", wsl, "/")):
        if gb is not None:
            doc["resources"][host] = {"disks": [{"mount": mount, "avail_gb": gb, "used_pct": 90, "level": snap.disk_level(gb, host)}],
                                      "mem_avail_gb": None, "checked_at": at(2)}
    return doc


class DiskRouting(OBase):
    def test_mac_goes_to_qq_only(self):
        self.put_live(with_disks(live(), mac=8))
        fr = FakeRunner()
        out = self.run_patrol(fr)
        self.assertEqual(self.terminal_sends(fr), [])
        key = "disk:mac:/System/Volumes/Data"
        self.assertEqual(out["alerts_active"][key], "mac /System/Volumes/Data 只剩 8 G；这台机器不跑 worker，需要你自己清理")
        self.assertTrue(any("只剩 8 G；这台机器不跑 worker" in t for t in self.qq(fr)))
        self.assertTrue(any(f["code"] == "disk_low" and f["host"] == "mac" for f in out["findings"]))

    def test_mac_43g_is_fine(self):
        self.put_live(with_disks(live(), mac=43.3))  # 11-06 user: the Mac's disk is small by nature, warn below 10 G
        out = self.run_patrol(FakeRunner())
        self.assertFalse([k for k in out["alerts_active"] if k.startswith("disk:")])
        self.assertFalse([f for f in out["findings"] if f.get("host") == "mac"])

    def test_mac_critical_still_no_agent_nudge(self):
        self.put_live(with_disks(live(), mac=4))
        fr = FakeRunner()
        self.run_patrol(fr)
        self.assertEqual(self.terminal_sends(fr), [])

    def test_wsl_nudges_agents_not_qq_disk_key(self):
        self.put_live(with_disks(live(), wsl=10))
        fr = FakeRunner()
        out = self.run_patrol(fr)
        sends = self.terminal_sends(fr)
        self.assertEqual(sorted(t for t, _ in sends), sorted([C1, R]))
        self.assertIn("暂停新的大量写盘任务；清理由巡检负责", sends[0][1])
        self.assertNotIn("不要自行删除", sends[0][1])
        self.assertFalse([k for k in out["alerts_active"] if k.startswith("disk:")])

    def test_host_of_live_workers_is_a_worker_host(self):
        doc = with_disks(live(), mac=8)
        doc["workers"] = [{"host": "mac", "workspace": "r::/x/y", "terminal": "tm", "line": "L", "node": "a"}]
        self.put_live(doc)
        fr = FakeRunner()
        out = self.run_patrol(fr)
        self.assertEqual([t for t, _ in self.terminal_sends(fr)], [C1])
        self.assertFalse([k for k in out["alerts_active"] if k.startswith("disk:")])

    def test_mac_alert_resolves(self):
        self.put_live(with_disks(live(), mac=8))
        self.run_patrol(FakeRunner())
        self.put_live(with_disks(live(), mac=120))
        fr = FakeRunner()
        self.run_patrol(fr, minutes_later=5)
        self.assertTrue(any("[已恢复] mac /System/Volumes/Data 只剩" in t for t in self.qq(fr)))


class DiskFill(OBase):
    def seed(self, pts):
        st = patrol.load_patrol_state("t")
        st["disk_hist"] = {"wsl:/": pts}
        patrol.save_patrol_state("t", st)

    def go(self, gb, apply=True):
        self.put_live(with_disks(live(), wsl=gb))
        fr = FakeRunner()
        return self.run_patrol(fr, apply=apply), fr

    def test_fast_fill_alerts(self):
        self.seed([[at(362), 100.0], [at(122), 85.0]])
        out, fr = self.go(70)
        f = [f for f in out["findings"] if f["code"] == "disk_fill_soon"]
        self.assertEqual(len(f), 1)
        self.assertEqual((f[0]["host"], f[0]["mount"]), ("wsl", "/"))
        self.assertGreater(f[0]["rate_gb_h"], 4)
        self.assertLess(f[0]["hours_left"], 12)
        txt = out["alerts_active"]["disk_fill:wsl:/"]
        self.assertIn("G", txt)
        self.assertIn("小时后降到 15 G 以下", txt)
        self.assertTrue(any("磁盘每小时少约" in t for t in self.qq(fr)))

    def test_slow_fill_and_cleanup_reset_do_not_alert(self):
        self.seed([[at(362), 100.0], [at(122), 98.0]])
        out, _ = self.go(96)
        self.assertNotIn("disk_fill:wsl:/", out["alerts_active"])
        self.seed([[at(362), 100.0], [at(300), 40.0], [at(122), 85.0]])   # cleaned up in between: only the points after the jump count
        out, _ = self.go(84)
        self.assertNotIn("disk_fill:wsl:/", out["alerts_active"])

    def test_short_burst_does_not_project(self):
        # 11-06: ~10 new worktrees in 47 min looked like "4.4 h to 15 G" with 116 G free
        self.seed([[at(63), 139.5], [at(47), 139.5], [at(26), 128.4], [at(16), 120.9]])
        out, _ = self.go(115.8)
        self.assertNotIn("disk_fill:wsl:/", out["alerts_active"])

    def test_history_kept_24h_and_saved_only_with_apply(self):
        self.seed([[at(25 * 60), 100.0], [at(100), 90.0]])
        self.go(89, apply=False)
        self.assertEqual(len(patrol.load_patrol_state("t")["disk_hist"]["wsl:/"]), 2)   # dry run saves nothing
        self.go(89)
        pts = patrol.load_patrol_state("t")["disk_hist"]["wsl:/"]
        self.assertEqual([p[1] for p in pts], [90.0, 89])   # the 25 h old point dropped, the new one added

    def test_mac_gets_no_history(self):
        self.put_live(with_disks(live(), mac=300, wsl=300))
        self.run_patrol(FakeRunner())
        self.assertEqual(list(patrol.load_patrol_state("t")["disk_hist"]), ["wsl:/"])


# ---------------------------------------------------------------- strict worktree cleanup

def rm_calls(fr):
    return [c for c in fr.calls if "worktree" in c and "rm" in c]


class WtBase(OBase):
    def setUp(self):
        super().setUp()
        self.add(*done_node())
        self.put_live(live())

    def go(self, fr=None, apply=True, minutes=0):
        fr = fr or FakeRunner()
        return self.run_patrol(fr, apply=apply, minutes_later=minutes), fr

    def assertKept(self, fr, out=None):
        self.assertEqual(rm_calls(fr), [])
        self.assertEqual([e for e in self.events("observe") if (e.get("refs") or {}).get("key", "").startswith("wt_rm:")], [])


class WtRemove(WtBase):
    def test_clean_candidate_is_removed_with_event_notice_and_size(self):
        out, fr = self.go()
        calls = rm_calls(fr)
        self.assertEqual(len(calls), 1)
        argv = calls[0]
        self.assertEqual(argv[argv.index("--environment") + 1], "wsl")
        self.assertEqual(argv[argv.index("--worktree") + 1], "path:" + WT)
        self.assertIn("--json", argv)
        self.assertNotIn("--force", argv)
        self.assertNotIn("--delete-branch", argv)
        ev = [e for e in self.events("observe") if (e.get("refs") or {}).get("key") == "wt_rm:" + WT]
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["actor"]["role"], "patrol")
        self.assertIn("清理已完成节点的工作区 wt-a（节点 L/a，空闲 ", ev[0]["summary"])
        self.assertIn("释放约 2.4 G", ev[0]["summary"])
        notice = [x for t, x in self.terminal_sends(fr) if t == R]
        self.assertEqual(len(notice), 1)
        self.assertIn("已清理 1 个已完成工作区，分支保留", notice[0])
        rec = [r for r in out["results"] if r["type"] == "worktree_rm"]
        self.assertEqual((rec[0]["removed"], rec[0]["freed_gb"]), (True, 2.4))
        summ = [r for r in out["results"] if r["type"] == "worktree_cleanup"][0]
        self.assertEqual((summ["removed"], summ["freed_gb"], summ["branch_unverified"]), (1, 2.4, []))

    def test_branch_checked_after_removal(self):
        out, fr = self.go(FakeRunner(branch_after=False))
        self.assertEqual(len(rm_calls(fr)), 1)
        summ = [r for r in out["results"] if r["type"] == "worktree_cleanup"][0]
        self.assertEqual(summ["branch_unverified"], [WT])
        self.assertIn("wt_branch_lost", out["alerts_active"])

    def test_branch_deleted_by_orca_is_recreated_at_the_scanned_head(self):
        out, fr = self.go(FakeRunner(branch_after="restored"))
        check = [c for c in fr.calls if c and "@@BR" in c[-1]][0][-1]
        self.assertIn("branch feat-x abc1234", check)          # recreated at the HEAD the scan saw, not at HEAD
        summ = [r for r in out["results"] if r["type"] == "worktree_cleanup"][0]
        self.assertEqual((summ["branch_unverified"], summ["branch_restored"]), ([], [WT]))
        self.assertNotIn("wt_branch_lost", out["alerts_active"])

    def test_dry_run_lists_but_removes_nothing(self):
        out, fr = self.go(apply=False)
        self.assertKept(fr)
        c = out["wt_cleanup"]["candidates"]
        self.assertEqual([(x["path"], x["verdict"]) for x in c], [(WT, "remove")])
        self.assertEqual(c[0]["nodes"], ["L/a"])
        self.assertTrue(c[0]["why"])
        self.assertEqual(self.terminal_sends(fr), [])

    def test_failed_rm_writes_no_event(self):
        out, fr = self.go(FakeRunner(wt_rm_rc=1))
        self.assertEqual(len(rm_calls(fr)), 1)
        self.assertEqual([e for e in self.events("observe") if (e.get("refs") or {}).get("key", "").startswith("wt_rm:")], [])
        rec = [r for r in out["results"] if r["type"] == "worktree_rm"][0]
        self.assertFalse(rec["removed"])
        self.assertEqual(rec["freed_gb"], 0)

    def test_other_ref_contains_head_is_enough_without_branch(self):
        out, fr = self.go(FakeRunner(wt_scan={WT: {"BRANCH": "", "REFS": "refs/remotes/origin/main", "BRANCH_OK": "0"}}))
        self.assertEqual(len(rm_calls(fr)), 1)

    def test_cap_and_oldest_first(self):
        evs = []
        for i in range(25):
            evs += done_node("n%02d" % i, "%s/wt-%02d" % (ROOT, i), minutes_ago=1000 + i * 10)
        self.add(*evs)
        out, fr = self.go()
        calls = rm_calls(fr)
        self.assertEqual(len(calls), patrol.WT_RM_PER_ROUND)
        paths = [c[c.index("--worktree") + 1] for c in calls]
        self.assertEqual(paths[0], "path:%s/wt-24" % ROOT)   # dispatched longest ago
        self.assertNotIn("path:%s/wt-00" % ROOT, paths)       # the newest ones wait for next round
        self.assertIn("已清理 20 个已完成工作区，分支保留", [x for t, x in self.terminal_sends(fr) if t == R][0])


class WtBlocked(WtBase):
    # 1. every node must be terminal
    def test_node_states_that_block(self):
        cases = {"running": [dispatch("b", WT, 900)],
                 "delivered": [dispatch("b", WT, 900), deliver("b", 850)],
                 "blocked": [dispatch("b", WT, 900), {"kind": "node_block", "line": "L", "node": "b", "blocker": "dep",
                                                      "summary": "w", "at": at(850), "actor": {"role": "router", "handle": R}}],
                 "running_again": []}
        for name, evs in cases.items():
            with self.subTest(name):
                self.tmp2 = tempfile.TemporaryDirectory()
                os.environ["PROGRESS_HOME"] = self.tmp2.name
                core.save_project(project())
                self.add(*done_node(), plan_ev("L", "b", minutes_ago=950, auto_dispatch=False), *evs)
                # a second node shares the worktree: a planned node never dispatched to it does not reference it
                if name == "running_again":
                    self.add(dispatch("b", WT, 900))   # dispatched, then nothing: running
                self.put_live(live())
                out, fr = self.go()
                self.assertKept(fr)
                self.tmp2.cleanup()
        os.environ["PROGRESS_HOME"] = self.tmp.name

    def test_unreviewed_delivery_blocks(self):
        self.tmp3 = tempfile.TemporaryDirectory()
        os.environ["PROGRESS_HOME"] = self.tmp3.name
        core.save_project(project())
        self.add(*done_node()[:3])    # delivered, not reviewed
        self.put_live(live())
        out, fr = self.go()
        self.assertKept(fr)
        self.assertEqual(out["wt_cleanup"]["candidates"][0]["verdict"], "skip")
        self.assertIn("节点未收尾", out["wt_cleanup"]["candidates"][0]["why"])
        os.environ["PROGRESS_HOME"] = self.tmp.name
        self.tmp3.cleanup()

    def test_terminal_states_all_allowed(self):
        for i, kind in enumerate(("node_supersede", "node_cancel")):
            nid = "t%d" % i
            self.add(plan_ev("L", nid, minutes_ago=1000, auto_dispatch=False), dispatch(nid, "%s/wt-t%d" % (ROOT, i), 900),
                     dict({"kind": kind, "line": "L", "node": nid, "summary": "x", "at": at(800),
                           "actor": {"role": "router", "handle": R}}, **({"by": "a"} if kind == "node_supersede" else {})))
        out, fr = self.go()
        self.assertGreaterEqual(len(rm_calls(fr)), 2)

    # 2. idle for 12 h (events and files)
    def test_recent_dispatch_blocks(self):
        self.tmp4 = tempfile.TemporaryDirectory()
        os.environ["PROGRESS_HOME"] = self.tmp4.name
        core.save_project(project())
        self.add(*done_node(minutes_ago=600 + 100))   # all events still inside 12 h except the plan
        self.add(dispatch("a", WT, 60))
        self.add(deliver("a", 40), review("a", 30))
        self.put_live(live())
        out, fr = self.go()
        self.assertKept(fr)
        os.environ["PROGRESS_HOME"] = self.tmp.name
        self.tmp4.cleanup()

    def test_any_recent_event_about_the_node_or_path_blocks(self):
        for ev in ({"kind": "observe", "line": "L", "node": "a", "actor": {"role": "patrol", "handle": "patrol@mac"},
                    "summary": "x", "at": at(30), "refs": {"key": "k1"}},
                   {"kind": "observe", "line": "L", "actor": {"role": "patrol", "handle": "patrol@mac"},
                    "summary": "x", "at": at(30), "refs": {"key": "k2", "note": WT}}):
            with self.subTest(ev["refs"]["key"]):
                self.add(ev)
                out, fr = self.go()
                self.assertKept(fr)

    def test_recent_files_block(self):
        out, fr = self.go(FakeRunner(wt_scan={WT: {"NEWER": WT + "/out.log"}}))
        self.assertKept(fr)
        self.assertIn("文件", out["wt_cleanup"]["candidates"][0]["why"])

    # 3. live terminal / router workspace
    def test_live_worker_in_it_blocks(self):
        doc = live(workers=[{"host": "wsl", "workspace": "repo::" + WT, "terminal": "w", "line": "L", "node": "a",
                             "lingering": True}])
        self.put_live(doc)
        out, fr = self.go()
        self.assertKept(fr)
        doc = live(workers=[{"host": "wsl", "workspace": "r::/elsewhere/wt-a", "worktree_name": "wt-a", "terminal": "w"}])
        self.put_live(doc)
        out, fr = self.go()
        self.assertKept(fr)

    def test_router_workspace_blocks(self):
        p = project()
        p["lines"][0]["workspace"] = WT
        core.save_project(p)
        out, fr = self.go()
        self.assertKept(fr)

    def test_router_or_coordinator_live_entry_blocks(self):
        doc = live()
        doc["routers"]["L"]["workspace"] = "repo::" + WT
        self.put_live(doc)
        out, fr = self.go()
        self.assertKept(fr)

    # 4. git state
    def test_dirty_or_untracked_blocks(self):
        out, fr = self.go(FakeRunner(wt_scan={WT: {"DIRTY": "?? notes.txt|"}}))
        self.assertKept(fr)
        self.assertIn("未提交", out["wt_cleanup"]["candidates"][0]["why"])

    def test_ignored_files_block_unless_only_caches(self):
        out, fr = self.go(FakeRunner(wt_scan={WT: {"IGNORED": "out/trace.bin|"}}))
        self.assertKept(fr)
        self.assertIn("忽略", out["wt_cleanup"]["candidates"][0]["why"])
        out, fr = self.go(FakeRunner(wt_scan={WT: {"IGNORED": None}}))   # old scan without the field: keep
        self.assertKept(fr)
        s = patrol.wt_scan_script(["/x/workspaces/a"], dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
        self.assertIn("--ignored=matching", s)
        self.assertIn("__pycache__", s)

    def test_commits_only_on_own_branch_and_branch_unconfirmed_blocks(self):
        out, fr = self.go(FakeRunner(wt_scan={WT: {"BRANCH_OK": "0"}}))
        self.assertKept(fr)
        out, fr = self.go(FakeRunner(wt_scan={WT: {"BRANCH": "", "REFS": "", "BRANCH_OK": "0"}}))   # detached, no other ref
        self.assertKept(fr)

    def test_main_checkout_is_never_removed(self):
        out, fr = self.go(FakeRunner(wt_scan={WT: {"GITDIR": "/home/u/repo/.git"}}))
        self.assertKept(fr)

    def test_broken_or_missing_not_rescanned_within_a_day(self):
        broken = {WT: {"GITRC": "128", "HEAD": "", "GITDIR": "", "COMMON": ""}}
        out, fr = self.go(FakeRunner(wt_scan=broken))
        skip = patrol.load_patrol_state("t")["wt_skip"]
        self.assertEqual(skip[WT]["verdict"], "broken")
        out, fr = self.go(FakeRunner(wt_scan=broken))
        self.assertFalse([c for c in fr.calls if "@@WT" in (c[-1] if c else "")])   # not scanned again
        self.assertEqual(out["wt_cleanup"]["broken"], 1)   # still reported
        self.assertEqual(out["wt_cleanup"]["candidates"][0]["verdict"], "broken")
        pst = patrol.load_patrol_state("t")
        pst["wt_skip"][WT]["at"] = "2026-11-01T00:00:00Z"   # recorded over a day ago: rescanned (now clean), removed
        patrol.save_patrol_state("t", pst)
        out, fr = self.go(FakeRunner())
        self.assertTrue(rm_calls(fr))

    def test_kept_after_scan_waits_six_hours_so_others_get_the_slots(self):
        out, fr = self.go(FakeRunner(wt_scan={WT: {"DIRTY": "?? notes.txt|"}}))
        self.assertEqual(patrol.load_patrol_state("t")["wt_skip"][WT]["verdict"], "held")
        out, fr = self.go(FakeRunner())   # now clean, but within 6 h: not rescanned, kept
        self.assertFalse([c for c in fr.calls if c and "@@WT" in c[-1]])
        self.assertKept(fr)
        self.assertIn("6 小时内不重扫", out["wt_cleanup"]["candidates"][0]["why"])
        self.assertEqual(out["wt_cleanup"]["candidates"][0]["verdict"], "skip")
        pst = patrol.load_patrol_state("t")
        pst["wt_skip"][WT]["at"] = "2026-11-01T00:00:00Z"
        patrol.save_patrol_state("t", pst)
        out, fr = self.go(FakeRunner())
        self.assertTrue(rm_calls(fr))

    def test_moved_away_link_is_skipped_not_broken(self):
        out, fr = self.go(FakeRunner(wt_scan={WT: {"LINK": "/mnt/archive/wt"}}))
        self.assertKept(fr)
        row = out["wt_cleanup"]["candidates"][0]
        self.assertEqual(row["verdict"], "skip")
        self.assertIn("已移走", row["why"])
        self.assertEqual(out["wt_cleanup"]["broken"], 0)
        self.assertNotIn("worktree_broken", [f["code"] for f in out["findings"]])
        self.assertEqual(patrol.load_patrol_state("t")["wt_skip"][WT]["verdict"], "moved")
        s = patrol.wt_scan_script(["/x/workspaces/a"], dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
        self.assertLess(s.index('[ -L "$p" ]'), s.index("git -C"))

    def test_broken_git_is_listed_not_removed(self):
        out, fr = self.go(FakeRunner(wt_scan={WT: {"GITRC": "128", "HEAD": "", "GITDIR": "", "COMMON": ""}}))
        self.assertKept(fr)
        self.assertIn({"code": "worktree_broken", "count": 1}, out["findings"])
        self.assertEqual(out["wt_cleanup"]["candidates"][0]["verdict"], "broken")
        out, fr = self.go(FakeRunner(wt_scan={WT: {"GITRC": "128"}}))   # status failed alone is enough
        self.assertKept(fr)
        self.assertIn({"code": "worktree_broken", "count": 1}, out["findings"])

    # scope guards
    def test_outside_workspaces_dir_or_unsafe_path_ignored(self):
        self.tmp5 = tempfile.TemporaryDirectory()
        os.environ["PROGRESS_HOME"] = self.tmp5.name
        core.save_project(project())
        for i, path in enumerate(("/home/u/other/wt", ROOT + "/x;rm -rf y", "/home/u/orca/workspaces", ROOT + "/../etc")):
            self.add(*done_node("o%d" % i, path))
        self.put_live(live())
        out, fr = self.go()
        self.assertKept(fr)
        self.assertEqual(out["wt_cleanup"]["candidates"], [])
        self.assertEqual([c for c in fr.calls if c[0] == "ssh" and "@@WT" in c[-1]], [])
        os.environ["PROGRESS_HOME"] = self.tmp.name
        self.tmp5.cleanup()

    def test_stale_live_or_unknown_host_means_nothing_happens(self):
        self.put_live(live(minutes_ago=50))
        out, fr = self.go()
        self.assertKept(fr)
        self.assertEqual([c for c in fr.calls if c[0] == "ssh" and "@@WT" in c[-1]], [])
        self.put_live(live(wsl="unknown"))
        out, fr = self.go()
        self.assertKept(fr)

    def test_scan_failure_removes_nothing(self):
        out, fr = self.go(FakeRunner(scan_rc=255))
        self.assertKept(fr)

    def test_one_ssh_script_per_round(self):
        evs = []
        for i in range(5):
            evs += done_node("m%d" % i, "%s/wt-m%d" % (ROOT, i))
        self.add(*evs)
        out, fr = self.go(apply=False)
        self.assertEqual(len([c for c in fr.calls if c[0] == "ssh" and "@@WT" in c[-1]]), 1)

    def test_patrol_off_does_nothing(self):
        (pathlib.Path(self.tmp.name) / "PATROL_OFF").write_text("")
        out, fr = self.go()
        self.assertKept(fr)


if __name__ == "__main__":
    unittest.main()


class WtBareName(unittest.TestCase):
    """11-07: patrol's auto_dispatch records only the worktree name; 47 such worktrees (119 G) were never cleaned."""
    def ev(self, ref, node="n"):
        return {"kind": "auto_dispatch", "line": "L", "node": node, "at": "2026-11-04T08:00:00Z", "refs": {"worktree": ref}}

    def setUp(self):
        self.saved = os.environ.get("PROGRESS_WT_ROOT")

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("PROGRESS_WT_ROOT", None)
        else:
            os.environ["PROGRESS_WT_ROOT"] = self.saved

    def test_bare_name_resolves_under_wt_root(self):
        os.environ["PROGRESS_WT_ROOT"] = ROOT + "/"
        m = patrol.wt_dispatch_map([self.ev("l-n-p11061536"), self.ev("id::%s/abs" % ROOT, "m")])
        self.assertEqual(sorted(m), [ROOT + "/abs", ROOT + "/l-n-p11061536"])
        self.assertEqual(m[ROOT + "/l-n-p11061536"]["nodes"], {("L", "n")})
        self.assertTrue(patrol.wt_root_ok(ROOT + "/l-n-p11061536"))

    def test_bare_name_skipped_without_root_or_when_unsafe(self):
        os.environ.pop("PROGRESS_WT_ROOT", None)
        self.assertEqual(patrol.wt_dispatch_map([self.ev("l-n-p11061536")]), {})
        os.environ["PROGRESS_WT_ROOT"] = ROOT
        for bad in ("..", "../etc", "a/b", ".hidden", "", "x y"):
            self.assertEqual(patrol.wt_dispatch_map([self.ev(bad)]), {}, bad)
