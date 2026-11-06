"""Host resources, router rebind, disk alerts, absent-router escalation, supervisor forwarding (fake runners only)."""
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
from fakes import FakeRunner, df_text, MEM_OK  # noqa: E402
from test_patrol import Base, project, plan_ev, at, NOW, R, C1  # noqa: E402
import test_supervisor as ts  # noqa: E402


def lv(minutes_later=0, disks=None, router_present=True, coord_present=True, host="wsl", rebound=None):
    """A fresh live doc (generated 2 min before the patrol time)."""
    r = {"handle": R, "present": router_present, "last_output_at": at(1), "screen_flag": None}
    if rebound:
        r.update(rebound_from=rebound, rebound_via="run")
    doc = {"schema": "progress-live/v1", "generated_at": at(2 - minutes_later),
           "hosts": {"mac": "ok", "wsl": "ok", "ib-agent": "unknown"},
           "routers": {"L": r},
           "coordinators": {"c1": {"handle": C1, "present": coord_present, "core": "c1", "screen_flag": None}},
           "workers": [], "errors": []}
    if disks is not None:
        doc["resources"] = {host: {"disks": disks, "mem_avail_gb": 8.0, "checked_at": at(2 - minutes_later)}}
    return doc


def disk(gb, level, mount="/"):
    return {"mount": mount, "avail_gb": gb, "used_pct": 90, "level": level}


class Parse(unittest.TestCase):
    def test_df_and_mem(self):
        text = df_text(("/", 12.3, 97), ("/home/x", 80, 50), ("/", 12.3, 97)) + MEM_OK
        res = snap.parse_resources(text, NOW)
        self.assertEqual([d["mount"] for d in res["disks"]], ["/", "/home/x"])  # deduped by mount
        self.assertEqual([d["level"] for d in res["disks"]], ["critical", "ok"])
        self.assertEqual(res["mem_avail_gb"], 16.0)
        self.assertEqual(res["checked_at"], snap.iso(NOW))

    def test_levels(self):
        self.assertEqual(snap.disk_level(snap.DISK_CRIT_GB - 0.1), "critical")
        self.assertEqual(snap.disk_level(snap.DISK_CRIT_GB), "low")
        self.assertEqual(snap.disk_level(snap.DISK_LOW_GB - 0.1), "low")
        self.assertEqual(snap.disk_level(snap.DISK_LOW_GB), "ok")

    def test_garbage(self):
        self.assertIsNone(snap.parse_resources("nope", NOW))

    def test_host_thresholds(self):
        self.assertEqual(snap.disk_level(43.3, "mac"), "ok")       # 11-06: the Mac warns only below 10 G
        self.assertEqual(snap.disk_level(9.9, "mac"), "low")
        self.assertEqual(snap.disk_level(4.9, "mac"), "critical")
        self.assertEqual(snap.disk_level(43.3, "wsl"), "low")
        os.environ["PROGRESS_DISK_GB"] = "wsl=30:10"
        try:
            self.assertEqual(snap.disk_level(43.3, "wsl"), "ok")
            self.assertEqual(snap.disk_level(20, "mac"), "low")     # the env table replaces the defaults
        finally:
            del os.environ["PROGRESS_DISK_GB"]


class Collect(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["PROGRESS_CACHE_DIR"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("PROGRESS_CACHE_DIR", None)
        self.tmp.cleanup()

    def proj(self):
        return {"schema": core.SCHEMA_PROJECT, "id": "t", "name": "t", "final_goal": {"text": "g"},
                "cores": [{"id": "c1", "name": "c", "coordinator": "term_gone-core"}],
                "lines": [{"id": "EVB", "name": "e", "core": "c1", "router": "term_old-1111", "run": "run_evb"}],
                "milestones": []}

    def test_resources_both_hosts_and_summary(self):
        fr = FakeRunner(df={"wsl": df_text(("/", 12, 90), ("/home/x", 12, 90)) + MEM_OK, "mac": df_text(("/System/Volumes/Data", 300, 40))})
        doc = snap.collect(self.proj(), None, fr, host="wsl", now=NOW, screen=False)
        self.assertEqual(sorted(doc["resources"]), ["mac", "wsl"])
        self.assertEqual(doc["resources"]["wsl"]["disks"][0]["level"], "critical")
        s = snap.summary(doc)
        self.assertEqual((s["resources"], s["disk_min_avail_gb"]), (2, 12.0))
        self.assertTrue(any(c[0] == "df" for c in fr.calls))
        self.assertTrue(any(c[0] == "ssh" and "df -Pk" in c[-1] for c in fr.calls))

    def test_failure_goes_to_errors_and_host_left_out(self):
        doc = snap.collect(self.proj(), None, FakeRunner(df_rc=1), host="wsl", now=NOW, screen=False)
        self.assertEqual(doc["resources"], {})
        self.assertTrue(any("磁盘" in e["error"] for e in doc["errors"]))

    def test_rebind_via_run_coordinator(self):
        fr = FakeRunner(terminals=[{"handle": "term_new-2222", "connected": True, "lastOutputAt": 1}],
                        runs=[{"id": "run_evb", "coordinator_handle": "term_new-2222"}], screen=["› "])
        doc = snap.collect(self.proj(), None, fr, host="wsl", now=NOW, screen=True)
        r = doc["routers"]["EVB"]
        self.assertEqual((r["handle"], r["present"], r["rebound_from"], r["rebound_via"]),
                         ("term_new-2222", True, "term_old-1111", "run"))
        self.assertTrue(r["screen_checked"])
        self.assertFalse(doc["coordinators"]["c1"].get("rebound_from"))  # core coordinators never rebind
        self.assertFalse(doc["coordinators"]["c1"]["present"])
        self.assertEqual(snap.summary(doc)["routers_rebound"], ["EVB"])

    def test_no_rebind_when_declared_present_or_new_absent(self):
        fr = FakeRunner(terminals=[{"handle": "term_old-1111", "connected": True}, {"handle": "term_new-2222", "connected": True}],
                        runs=[{"id": "run_evb", "coordinator_handle": "term_new-2222"}])
        r = snap.collect(self.proj(), None, fr, host="wsl", now=NOW, screen=False)["routers"]["EVB"]
        self.assertEqual(r["handle"], "term_old-1111")
        self.assertNotIn("rebound_from", r)
        fr = FakeRunner(terminals=[], runs=[{"id": "run_evb", "coordinator_handle": "term_new-2222"}])
        r = snap.collect(self.proj(), None, fr, host="wsl", now=NOW, screen=False)["routers"]["EVB"]
        self.assertIs(r["present"], False)
        self.assertNotIn("rebound_from", r)


class ResourceAlerts(Base):
    def sends(self, fr):
        return [(c[c.index("--terminal") + 1], c[c.index("--text") + 1]) for c in fr.kinds("send")]

    def go(self, doc, minutes=0):
        self.put_live(doc)
        fr = FakeRunner()
        self.run_patrol(fr, minutes_later=minutes)
        return self.sends(fr)

    def test_low_goes_to_coordinators_only_and_dedupes_six_hours(self):
        d = [disk(30, "low")]
        s = self.go(lv(0, d))
        self.assertEqual([t for t, _ in s], [C1])
        self.assertIn("只剩 30", s[0][1])
        self.assertIn("50 G", s[0][1])
        self.assertTrue(any(f["code"] == "disk_low" for f in patrol.plan(
            project(), core.read_events("t"), lv(0, d), NOW)["findings"]))
        self.assertEqual(self.go(lv(300, d), 300), [])      # 5 h: still silent
        self.assertEqual([t for t, _ in self.go(lv(361, d), 361)], [C1])  # > 6 h: repeats

    def test_critical_goes_to_coordinators_and_routers_repeats_after_two_hours(self):
        d = [disk(10, "critical")]
        s = self.go(lv(0, d))
        self.assertEqual(sorted(t for t, _ in s), sorted([C1, R]))
        self.assertEqual(self.go(lv(100, d), 100), [])
        self.assertEqual(sorted(t for t, _ in self.go(lv(125, d), 125)), sorted([C1, R]))

    def test_absent_router_not_messaged_by_disk_alert_and_ok_is_silent(self):
        s = self.go(lv(0, [disk(10, "critical")], router_present=False))
        self.assertEqual([t for t, _ in s if t == R], [])
        self.assertEqual(self.go(lv(0, [disk(200, "ok")]), 0), [])

    def test_low_then_critical_are_separate_keys(self):
        self.go(lv(0, [disk(30, "low")]))
        s = self.go(lv(10, [disk(10, "critical")]), 10)
        self.assertEqual(sorted(t for t, _ in s), sorted([C1, R]))


class AbsentRouter(Base):
    def setUp(self):
        super().setUp()
        self.add(plan_ev("L", "a", auto_dispatch=False),
                 {"kind": "node_dispatch", "line": "L", "node": "a", "summary": "d", "at": at(60),
                  "actor": {"role": "router", "handle": R}, "refs": {"task": "t", "dispatch": "ctx_1", "terminal": "w"}},
                 plan_ev("L", "b", auto_dispatch=False))

    def test_escalates_to_coordinator_once_then_after_two_hours(self):
        self.put_live(lv(0, router_present=False))
        fr = FakeRunner()
        out = self.run_patrol(fr)
        sends = [(c[c.index("--terminal") + 1], c[c.index("--text") + 1]) for c in fr.kinds("send")]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][0], C1)
        t = sends[0][1]
        for part in ("L", R, "run_x", "1 个节点", "run-use --id run_x"):
            self.assertIn(part, t)
        self.assertTrue(any(f["code"] == "router_absent_escalated" for f in out["findings"]))
        self.assertTrue(self.events("observe") is not None)
        self.put_live(lv(60, router_present=False))
        fr = FakeRunner()
        self.run_patrol(fr, minutes_later=60)
        self.assertEqual(fr.kinds("send"), [])
        self.put_live(lv(125, router_present=False))
        fr = FakeRunner()
        self.run_patrol(fr, minutes_later=125)
        self.assertEqual(len(fr.kinds("send")), 1)

    def test_coordinator_absent_only_observes(self):
        self.put_live(lv(0, router_present=False, coord_present=False))
        fr = FakeRunner()
        out = self.run_patrol(fr)
        self.assertEqual(fr.kinds("send"), [])
        self.assertTrue(any(f["code"] == "coordinator_absent" for f in out["findings"]))
        self.assertTrue(any((e.get("refs") or {}).get("key") == "coordabsent:L" for e in self.events("observe")))

    def test_rebound_observe_and_sends_use_live_handle(self):
        new = "term_new-9999"
        doc = lv(0, rebound=R)
        doc["routers"]["L"]["handle"] = new
        self.add({"kind": "node_dispatch", "line": "L", "node": "b", "summary": "d", "at": at(70),
                  "actor": {"role": "router", "handle": R}, "refs": {"task": "t2", "dispatch": "ctx_2", "terminal": "w2"}})
        self.put_live(doc)
        fr = FakeRunner()
        out = self.run_patrol(fr)
        obs = [e for e in self.events("observe") if (e.get("refs") or {}).get("key") == "rebound:L:%s" % new]
        self.assertEqual(len(obs), 1)
        self.assertIn("project.json", obs[0]["summary"])
        for c in fr.kinds("send"):
            self.assertNotEqual(c[c.index("--terminal") + 1], R)


class Forwarding(ts.Base):
    def sent_all(self, fr):
        return [(c[c.index("--terminal") + 1], c[c.index("--text") + 1]) for c in fr.kinds("send")
                if "[巡检员]" in c[c.index("--text") + 1]]

    def live_absent(self, coord=True):
        doc = lv(0, router_present=False, coord_present=coord)
        core.save_live("t", doc)

    def test_forwarded_to_coordinator(self):
        self.live_absent()
        out, fr = self.go(ts.Fake(), runner=FakeRunner(screen=["x", "› "]))
        s = self.sent_all(fr)
        self.assertEqual(len(s), 1)
        self.assertEqual(s[0][0], C1)
        self.assertIn("[巡检员] L router 不在线，转给你：", s[0][1])
        self.assertIn("先读 worker 屏幕", s[0][1])
        self.assertEqual(core.load_supervisor("t")["delivery"][0]["reason"], "forwarded_to_coordinator")
        # unchanged text is deduped per target
        out, fr = self.go(ts.Fake(), minutes=31, runner=FakeRunner(screen=["x", "› "]))
        self.assertEqual(self.sent_all(fr), [])
        self.assertEqual(core.load_supervisor("t")["delivery"][0]["reason"], "unchanged")

    def test_coordinator_absent_too_not_sent(self):
        self.live_absent(coord=False)
        out, fr = self.go(ts.Fake(), runner=FakeRunner(screen=["x", "› "]))
        self.assertEqual(self.sent_all(fr), [])
        self.assertEqual(core.load_supervisor("t")["delivery"][0]["reason"], "router_absent")

    def test_map_has_resources_and_absent_router(self):
        core.save_live("t", dict(lv(0, [disk(12, "critical")], router_present=False), generated_at=at(2)))
        fake = ts.Fake()
        self.go(fake, runner=FakeRunner(screen=["x", "› "]))
        self.assertIn("主机资源", fake.map_text)
        self.assertIn("剩 12 G", fake.map_text)
        self.assertIn("router 终端不在线", fake.map_text)


if __name__ == "__main__":
    unittest.main()
