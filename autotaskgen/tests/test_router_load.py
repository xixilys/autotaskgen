import datetime as dt
import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import router_load as rl  # noqa: E402

NOW = dt.datetime(2026, 11, 7, 12, 0, 0, tzinfo=dt.timezone.utc)


def iso(minutes_ago):
    return (NOW - dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def ev(kind, node, minutes_ago, line="L", role="router", **kw):
    e = {"kind": kind, "line": line, "node": node, "at": iso(minutes_ago), "actor": {"role": role}}
    e.update(kw)
    return e


def rollout(path, turns, ctx=210000):
    """turns: [(start_min_ago, end_min_ago or None)]"""
    rows = [{"timestamp": iso(2000), "type": "session_meta", "payload": {"cwd": "/x"}}]
    for a, b in turns:
        rows.append({"timestamp": iso(a), "type": "event_msg", "payload": {"type": "task_started"}})
        rows.append({"timestamp": iso(a - 1), "type": "event_msg", "payload": {
            "type": "token_count", "info": {"last_token_usage": {"input_tokens": ctx}}}})
        if b is not None:
            rows.append({"timestamp": iso(b), "type": "event_msg", "payload": {"type": "task_complete"}})
    pathlib.Path(path).write_text("\n".join(json.dumps(r) for r in rows) + "\n")


class Busy(unittest.TestCase):
    def test_hot_streak_and_share(self):
        # busy the whole of the last 4 h except 10 min, idle before
        turns = [(NOW - dt.timedelta(hours=4), NOW - dt.timedelta(minutes=10))]
        bp = rl.busy_profile(turns, NOW - dt.timedelta(hours=24), NOW)
        self.assertAlmostEqual(bp["busy_h"], 4 - 10 / 60, places=2)
        self.assertEqual(bp["full_hours"], 24)
        self.assertEqual(bp["hot_streak_max_h"], 4)       # 11:00-12:00 is 50/60 = 83 %
        self.assertEqual(bp["hot_streak_now_h"], 4)

    def test_rollout_tail_and_open_turn(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "rollout-2026-11-07T00-00-00-0000aaaa-1111-2222-3333-444455556666.jsonl")
            rollout(p, [(300, 240), (100, None)])
            r = rl.router_turns(p, NOW - dt.timedelta(hours=24), NOW)
            self.assertEqual(len(r["turns"]), 2)
            self.assertTrue(r["open_turn"])                 # the open turn runs to now
            self.assertEqual(r["turns"][-1][1], NOW)
            self.assertEqual(r["context_tokens"], 210000)
            # small chunk: the reader goes back far enough to see the window start
            self.assertEqual(len(rl._tail_from(p, NOW - dt.timedelta(hours=24), chunk=64)) >= 5, True)

    def test_session_of_handle_from_ps(self):
        ps = ("/opt/x/codex resume 0000aaaa-84e5-7280-9f1e-f99cb4ec5aa7 -C /w -c "
              "shell_environment_policy.set.ORCA_TERMINAL_HANDLE='term_abc' hi\n"
              "/opt/x/codex resume 0000bbbb-0000-7280-9f1e-f99cb4ec5aa7 -c X='term_other'\n")
        self.assertEqual(rl.session_of_handle("term_abc", ps), "0000aaaa-84e5-7280-9f1e-f99cb4ec5aa7")
        self.assertIsNone(rl.session_of_handle("term_zzz", ps))


class Line(unittest.TestCase):
    def nodes(self):
        return {"a": {"status": "reviewed"}, "b": {"status": "delivered", "delivered_at": iso(45), "deps": ["a"]},
                "c": {"status": "running", "deps": ["a"]}, "d": {"status": "ready", "deps": []},
                "e": {"status": "superseded"}}

    def events(self):
        return [ev("node_plan", "a", 600), ev("node_plan", "b", 590), ev("node_plan", "e", 580),
                ev("node_dispatch", "a", 500), ev("node_deliver", "a", 480, role="worker", outcome="PASS"),
                ev("node_review", "a", 450, outcome="PASS"),
                ev("node_dispatch", "b", 470),            # before a was done: a's PASS came at 480 min ago? no: 480 > 470
                ev("node_deliver", "b", 45, role="worker", outcome="PASS"),
                ev("node_dispatch", "c", 520),            # c dispatched before a was delivered -> missing dep
                ev("node_supersede", "e", 300)]

    def test_numbers_carry_denominators(self):
        m = rl.line_load({"id": "L"}, self.nodes(), self.events(), NOW, 24.0, None, None, {"nodes": 1})
        self.assertEqual(m["review_wait_min"], {"n": 1, "median": 30.0, "p90": 30.0})
        self.assertEqual(m["worker_run_min"]["n"], 2)                     # a: 20 min, b: 425 min
        self.assertEqual(m["unreviewed_over_30min"], [{"node": "b", "min": 45}])
        self.assertEqual(m["superseded_or_cancelled"], {"n": 1, "of_planned": 3, "share": 0.33})
        self.assertEqual(m["dispatched_with_missing_deps"]["n"], 1)
        self.assertEqual(m["dispatched_with_missing_deps"]["nodes"][0]["node"], "c")
        self.assertIsNone(m["router_busy"])
        self.assertEqual(m["planned_parallel"], 3)   # b, c, d have no open dependency
        rows = rl.render_rows({"L": m})
        self.assertIn("找不到 router 的会话记录", rows[0])
        self.assertIn("被取代或取消 1/3", rows[0])

    def test_split_a_hits_on_hot_router_and_slow_reviews(self):
        m = {"router_busy": {"hot_streak_max_h": 4, "hot_streak_end": "x"},
             "worker_run_min": {"median": 14.0, "mean": 20.0}, "review_wait_min": {"median": 16.0},
             "router_min_per_node": 5.0, "planned_parallel": 1}
        s = rl.split_check(m)
        self.assertTrue(s["hit"])
        self.assertTrue(s["why"][0].startswith("A："))
        m["review_wait_min"]["median"] = 10.0
        self.assertFalse(rl.split_check(m)["hit"])

    def test_split_b_hits_on_plan_wider_than_router_can_feed(self):
        m = {"router_busy": {"hot_streak_max_h": 0}, "worker_run_min": {"median": 14.0, "mean": 23.0},
             "review_wait_min": {"median": 5.0}, "router_min_per_node": 6.7, "planned_parallel": 5}
        s = rl.split_check(m)          # 0.8 * 23 / 6.7 = 2.7 < 5
        self.assertTrue(s["hit"])
        self.assertIn("B：", s["why"][0])
        m["planned_parallel"] = 2
        self.assertFalse(rl.split_check(m)["hit"])
        m.update(planned_parallel=1, router_min_per_node=30.0)   # 1 > 0.6, but nothing to split off
        self.assertFalse(rl.split_check(m)["hit"])

    def test_independent_groups(self):
        nodes = {"a": {"status": "ready", "milestones": ["M1"]}, "b": {"status": "planned", "deps": ["a"]},
                 "c": {"status": "ready", "milestones": ["M2"]}, "d": {"status": "reviewed"}}
        gs = rl.independent_groups(nodes)
        self.assertEqual([g["nodes"] for g in gs], [["a", "b"], ["c"]])
        self.assertEqual(gs[0]["milestones"], ["M1"])

    def test_project_load_finds_router_rollout(self):
        with tempfile.TemporaryDirectory() as d:
            sid = "0000aaaa-1111-2222-3333-444455556666"
            day = os.path.join(d, "2026", "11", "07")
            os.makedirs(day)
            rollout(os.path.join(day, "rollout-2026-11-07T00-00-00-%s.jsonl" % sid), [(240, 5)])
            ps = "/x/codex resume %s -c H='term_r'\n" % sid
            project = {"lines": [{"id": "L", "router": "term_r"}]}
            state = {"lines": {"L": {"nodes": self.nodes()}}}
            out = rl.project_load(project, state, self.events(), None, NOW, 24.0, ps_text=ps, sessions_root=d)
            self.assertEqual(out["L"]["router_session"], sid)
            self.assertAlmostEqual(out["L"]["router_busy"]["busy_h"], 3.92, places=2)
            self.assertEqual(out["L"]["router_busy"]["context_tokens"], 210000)


if __name__ == "__main__":
    unittest.main()


class SupervisorWiring(unittest.TestCase):
    """The supervisor puts the numbers in its map, calls terra at max, and on a split hit writes once to the
    router and once to the lead's inbox (12 h gap)."""

    def setUp(self):
        import test_supervisor as ts
        self.ts = ts
        self.base = ts.Base("run")
        self.base.setUp()
        self.calls = os.path.join(self.base.tmp.name, "lead_calls")
        script = os.path.join(self.base.tmp.name, "lead")
        pathlib.Path(script).write_text("#!/bin/sh\necho \"$@\" >> %s\necho I9\n" % self.calls)
        os.chmod(script, 0o755)
        os.environ["PROGRESS_LEAD_CMD"] = script
        import supervisor as sup
        self.sup = sup
        self.orig = sup.router_load.project_load
        hit = {"hit": True, "why": ["A：router 连续 4 小时忙于 80% 以上"]}

        def fake_load(project, state, events, live, now, hours, **kw):
            m = rl.line_load({"id": "L"}, {}, [], now, hours, None, 3, {})
            m["split"] = hit
            return {"L": m}
        sup.router_load.project_load = fake_load

    def tearDown(self):
        self.sup.router_load.project_load = self.orig
        os.environ.pop("PROGRESS_LEAD_CMD", None)
        self.base.tearDown()

    def test_map_section_split_notes_and_gap(self):
        fake = self.ts.Fake()
        out, fr = self.base.go(fake, force=True)
        self.assertIn("## 3b router 负载", fake.map_text)
        self.assertIn("⚠拆 router 判据命中", fake.map_text)
        sends = [c for c in fr.kinds("send") if "拆 router 建议" in c[c.index("--text") + 1]]
        self.assertEqual(len(sends), 1)
        lead = pathlib.Path(self.calls).read_text().splitlines()
        self.assertEqual(len(lead), 1)
        self.assertIn("inbox add --from supervisor", lead[0])
        rep = self.sup.core.load_supervisor("t")
        self.assertEqual(rep["effort"], "max")
        self.assertEqual(rep["router_split"][0]["router"], "sent")
        # next forced round 40 min later: nothing new to router or lead
        out, fr = self.base.go(self.ts.Fake(), minutes=40, force=True)
        self.assertFalse([c for c in fr.kinds("send") if "拆 router 建议" in c[c.index("--text") + 1]])
        self.assertEqual(len(pathlib.Path(self.calls).read_text().splitlines()), 1)

    def test_terra_action_saying_split_replaces_the_script_note(self):
        msg = json.dumps({"summary": "s", "lines": [
            {"line": "L", "status": "attention", "progress_note": "n", "issues": [{"kind": "other", "evidence": "x"}],
             "actions": [{"to": "router", "text": "建议拆出子 router：a 组", "done_when": "出现子 router",
                          "verify": {"line": None, "node": None, "event_kind": None}, "check_after_min": 60}]},
            {"line": "Q", "status": "ok", "progress_note": "n", "issues": [], "actions": []}]})
        out, fr = self.base.go(self.ts.Fake(msg=msg), force=True)
        self.assertFalse([c for c in fr.kinds("send") if "拆 router 建议" in c[c.index("--text") + 1]])
        rep = self.sup.core.load_supervisor("t")
        self.assertEqual(rep["router_split"][0]["router"], "via_terra_action")
        self.assertEqual(len(pathlib.Path(self.calls).read_text().splitlines()), 1)


class DefaultRoute(unittest.TestCase):
    """11-07 R6 trial: patrol's default route for daily development is Terra max; explicit choices are kept."""

    def test_defaults(self):
        import patrol
        r = patrol.resolve_route({"route": {"agent": "codex", "model": "gpt-6.1-terra", "on": "wsl"}}, {})
        self.assertEqual(r["effort"], "max")
        r = patrol.resolve_route({"route": {"agent": "codex", "on": "wsl"}}, {})
        self.assertNotIn("model", r)            # no model: still listed as route incomplete, not guessed
        r = patrol.resolve_route({"route": {"agent": "codex", "model": "codex/gpt-6.1-terra", "on": "wsl"}}, {})
        self.assertEqual(r["effort"], "max")
        r = patrol.resolve_route({"route": {"model": "gpt-6.1-terra", "effort": "high"}}, {})
        self.assertEqual(r["effort"], "high")
        r = patrol.resolve_route({"route": {"agent": "codex", "model": "codex/gpt-6.1-sol", "on": "wsl"}}, {})
        self.assertNotIn("effort", r)
        r = patrol.resolve_route({"route": {"agent": "dsh", "on": "wsl"}}, {})
        self.assertNotIn("model", r)


class Alternate(unittest.TestCase):
    """R6+R7: dev nodes planned on Terra alternate Terra max / DS max per line; rework -> other model -> Sol high."""

    def setUp(self):
        import patrol
        self.p = patrol
        self.terra = {"agent": "codex", "model": "codex/gpt-6.1-terra", "effort": "high", "on": "wsl"}

    def disp(self, node, mins, route, deliver=True):
        out = [ev("auto_dispatch", node, mins, role="patrol", route=route)]
        if deliver:
            out.append(ev("node_deliver", node, mins - 10, role="worker", outcome="PASS"))
        return out

    def test_ds_off_everything_terra_max(self):
        r, why = self.p.dev_route({}, [], {}, "L", "a", {"kind": "run"}, self.terra)
        self.assertEqual((r["agent"], r["model"], r["effort"], why), ("codex", "codex/gpt-6.1-terra", "max", "terra_max_ds_off"))

    def test_alternates_when_ds_on(self):
        proj = {"defaults": {"ds_worker": {"enabled": True, "on": "wsl"}}}
        evs = self.disp("x", 100, {"agent": "codex", "model": "gpt-6.1-terra", "effort": "max"})
        r, why = self.p.dev_route(proj, evs, {"x": {"kind": "run"}}, "L", "b", {"kind": "run"}, self.terra)
        self.assertEqual((r, why), ({"agent": "dsh", "on": "wsl"}, "alternate_ds"))
        evs += self.disp("y", 50, {"agent": "dsh", "on": "wsl"})
        r, why = self.p.dev_route(proj, evs, {"x": {}, "y": {}}, "L", "c", {"kind": "run"}, self.terra)
        self.assertEqual((r["model"], r["effort"], why), ("codex/gpt-6.1-terra", "max", "alternate_terra"))

    def test_rework_other_model_then_sol(self):
        proj = {"defaults": {"ds_worker": {"enabled": True}}}
        evs = self.disp("a", 100, {"agent": "codex", "model": "gpt-6.1-terra", "effort": "max"})
        r, why = self.p.dev_route(proj, evs, {}, "L", "a-repair", {"kind": "run"}, self.terra)
        self.assertEqual((r["agent"], why), ("dsh", "rework_other_model"))
        evs += self.disp("a-repair", 50, {"agent": "dsh", "on": "wsl"})
        r, why = self.p.dev_route(proj, evs, {}, "L", "a-repair-2", {"kind": "run"}, self.terra)
        self.assertEqual((r["model"], r["effort"], why), ("gpt-6.1-sol", "high", "rework_sol_high"))
        # DS off: a Terra failure goes straight to Sol high
        r, why = self.p.dev_route({}, evs[:2], {}, "L", "a-repair", {"kind": "run"}, self.terra)
        self.assertEqual(why, "rework_sol_high")

    def test_review_and_explicit_sol_untouched(self):
        sol = {"agent": "codex", "model": "gpt-6.1-sol", "effort": "high", "on": "wsl"}
        self.assertEqual(self.p.dev_route({}, [], {}, "L", "a", {"kind": "run"}, sol), (sol, None))
        self.assertEqual(self.p.dev_route({}, [], {}, "L", "a-review", {"kind": "review"}, self.terra), (self.terra, None))


class DispatchRoute(unittest.TestCase):
    def test_dispatch_event_carries_route(self):
        import progress as cli
        a = cli.parser().parse_args(["--project", "t", "dispatch", "--line", "L", "--node", "a", "--summary", "s",
                                     "--agent", "codex", "--model", "gpt-6.1-terra", "--effort", "max", "--on", "wsl"])
        ev = cli.build_event("dispatch", a)
        self.assertEqual(ev["route"], {"agent": "codex", "model": "gpt-6.1-terra", "effort": "max", "on": "wsl"})
        a = cli.parser().parse_args(["--project", "t", "dispatch", "--line", "L", "--node", "a", "--summary", "s"])
        self.assertNotIn("route", cli.build_event("dispatch", a))
