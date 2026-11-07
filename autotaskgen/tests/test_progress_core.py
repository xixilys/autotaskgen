import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import progress_core as core  # noqa: E402

R = "term_router-aaaa"
C1 = "term_coord1-bbbb"
C2 = "term_coord2-cccc"
W = "term_worker-dddd"


def project():
    return {
        "schema": core.SCHEMA_PROJECT, "id": "t", "name": "test",
        "final_goal": {"text": "goal", "done_when": "all"},
        "humans": ["term_human-eeee"],
        "cores": [{"id": "c1", "name": "one", "coordinator": C1},
                  {"id": "c2", "name": "two", "coordinator": C2}],
        "lines": [{"id": "L", "name": "line", "core": "c1", "router": R, "patrol": True}],
        "milestones": [{"id": "M1", "title": "m1", "core": "c1", "lines": ["L"], "after": []},
                       {"id": "M2", "title": "m2", "core": "c1", "lines": [], "after": ["M1"]}],
    }


def ev(kind, role, handle, **kw):
    d = {"kind": kind, "actor": {"role": role, "handle": handle}, "summary": kw.pop("summary", "s")}
    d.update(kw)
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["PROGRESS_HOME"] = self.tmp.name
        core.save_project(project())

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, e):
        return core.append("t", e)

    def rejects(self, e, code):
        with self.assertRaises(core.ProgressError) as cm:
            self.add(e)
        self.assertEqual(cm.exception.code, code, cm.exception.detail)

    def state(self):
        return core.fold(core.load_project("t"), core.read_events("t"))


class Declaration(Base):
    def test_bad_project_rejected(self):
        doc = project()
        doc["lines"][0]["core"] = "nope"
        doc["milestones"][1]["after"] = ["MX"]
        with self.assertRaises(core.ProgressError) as cm:
            core.save_project(doc)
        self.assertEqual(len(cm.exception.detail["errors"]), 2)

    def test_empty_project_rejected(self):
        with self.assertRaises(core.ProgressError):
            core.save_project({})

    def test_unregistered(self):
        with self.assertRaises(core.ProgressError) as cm:
            core.append("nope", ev("node_plan", "router", R, line="L", node="a"))
        self.assertEqual(cm.exception.code, "no_project")

    def test_bad_project_id(self):
        with self.assertRaises(core.ProgressError):
            core.project_dir("../x")


class Validation(Base):
    def test_unknown_kind(self):
        self.rejects(ev("node_explode", "router", R, line="L", node="a"), "bad_kind")

    def test_unknown_line(self):
        self.rejects(ev("node_plan", "router", R, line="X", node="a"), "unknown_line")

    def test_node_needs_plan(self):
        self.rejects(ev("node_deliver", "worker", W, line="L", node="a", outcome="PASS"), "unknown_node")

    def test_wrong_router(self):
        self.rejects(ev("node_plan", "router", "term_other", line="L", node="a"), "not_router")

    def test_role_kind(self):
        self.rejects(ev("milestone_confirm", "router", R, milestone="M1"), "role_kind")

    def test_summary_required_and_bounded(self):
        self.rejects(ev("node_plan", "router", R, line="L", node="a", summary=""), "no_summary")
        self.rejects(ev("node_plan", "router", R, line="L", node="a", summary="x" * 121), "long_summary")

    def test_outcome_enum(self):
        self.add(ev("node_plan", "router", R, line="L", node="a"))
        self.rejects(ev("node_deliver", "worker", W, line="L", node="a", outcome="GREEN"), "bad_outcome")
        self.rejects(ev("node_deliver", "worker", W, line="L", node="a"), "no_outcome")

    def test_unknown_dep(self):
        self.rejects(ev("node_plan", "router", R, line="L", node="b", deps=["zz"]), "unknown_dep")

    def test_worker_identity(self):
        self.add(ev("node_plan", "router", R, line="L", node="a"))
        self.add(ev("node_dispatch", "router", R, line="L", node="a", refs={"terminal": W}))
        self.rejects(ev("node_deliver", "worker", "term_stranger", line="L", node="a", outcome="PASS"),
                     "not_worker")
        self.add(ev("node_deliver", "worker", W, line="L", node="a", outcome="PASS"))

    def test_coordinator_must_own_core(self):
        self.add(ev("milestone_claim", "router", R, line="L", milestone="M1"))
        self.rejects(ev("milestone_confirm", "coordinator", C2, milestone="M1"), "not_coordinator")
        self.add(ev("milestone_confirm", "coordinator", C1, milestone="M1"))

    def test_claim_from_unrelated_line(self):
        self.rejects(ev("milestone_claim", "router", R, line="L", milestone="M2"), "claim_line")

    def test_unknown_milestone(self):
        self.rejects(ev("milestone_claim", "router", R, line="L", milestone="M9"), "unknown_milestone")

    def test_handle_prefix_matches(self):
        self.add(ev("node_plan", "router", "term_router", line="L", node="a"))

    def test_human_must_be_listed(self):
        self.rejects(ev("node_plan", "human", "term_nobody", line="L", node="a"), "not_human")
        self.add(ev("node_plan", "human", "term_human-eeee", line="L", node="a"))

    def test_duplicate_id_idempotent(self):
        e = ev("node_plan", "router", R, line="L", node="a", id="ev_fixed")
        self.add(e)
        self.assertTrue(self.add(e).get("duplicate"))
        self.assertEqual(len(core.read_events("t")), 1)


class DeliverOutcomes(Base):
    """R2 (11-07): worker/router deliver only PASS (optionally result negative) or BLOCKED with blocker + blocked_by."""

    def setup_node(self):
        self.add(ev("node_plan", "router", R, line="L", node="a"))
        self.add(ev("node_dispatch", "router", R, line="L", node="a", refs={"terminal": W}))

    def deliver(self, outcome, role="worker", handle=W, **kw):
        return ev("node_deliver", role, handle, line="L", node="a", outcome=outcome, **kw)

    def last(self):
        return core.read_events("t")[-1]

    def test_worker_partial_fail_unknown_rejected(self):
        self.setup_node()
        for oc in ("PARTIAL", "FAIL", "UNKNOWN"):
            self.rejects(self.deliver(oc), "deliver_outcome")

    def test_router_partial_rejected(self):
        self.setup_node()
        self.rejects(self.deliver("PARTIAL", role="router", handle=R), "deliver_outcome")

    def test_import_partial_accepted(self):
        self.setup_node()
        self.add(self.deliver("PARTIAL", role="import", handle="import"))

    def test_pass_accepted(self):
        self.setup_node()
        self.add(self.deliver("PASS"))

    def test_pass_negative_result_folded(self):
        self.setup_node()
        self.add(self.deliver("PASS", result="negative"))
        self.assertEqual(self.last()["result"], "negative")
        self.assertEqual(self.state()["lines"]["L"]["nodes"]["a"]["result"], "negative")

    def test_result_negative_on_blocked_rejected(self):
        self.setup_node()
        self.rejects(self.deliver("BLOCKED", blocker="budget", blocked_by="剩 3 项", result="negative"), "bad_result")

    def test_blocked_needs_blocker_and_blocked_by(self):
        self.setup_node()
        self.rejects(self.deliver("BLOCKED", blocked_by="缺上游 b 的产物"), "deliver_blocker")
        self.rejects(self.deliver("BLOCKED", blocker="dep"), "deliver_blocker")
        self.rejects(self.deliver("BLOCKED", blocker="dep", blocked_by="  "), "deliver_blocker")
        self.rejects(self.deliver("BLOCKED", blocker="bogus", blocked_by="x"), "deliver_blocker")

    def test_blocked_budget_folds_to_blocked(self):
        self.setup_node()
        self.add(self.deliver("BLOCKED", blocker="budget", blocked_by="剩 3 项"))
        self.assertEqual(self.last()["blocked_by"], "剩 3 项")
        n = self.state()["lines"]["L"]["nodes"]["a"]
        self.assertEqual((n["status"], n["blocker"], n["blocked_by"]), ("blocked", "budget", "剩 3 项"))

    def test_review_partial_accepted_blocked_rejected(self):
        self.setup_node()
        self.add(self.deliver("PASS"))
        self.add(ev("node_review", "router", R, line="L", node="a", outcome="PARTIAL"))
        self.rejects(ev("node_review", "router", R, line="L", node="a", outcome="BLOCKED"), "bad_outcome")

    def test_cli_blocked_and_result_flags(self):
        import progress
        a = progress.parser().parse_args(["deliver", "--line", "L", "--node", "a", "--outcome", "BLOCKED",
                                          "--blocker", "budget", "--blocked-by", "剩 3 项", "--summary", "s"])
        e = progress.build_event("deliver", a)
        self.assertEqual((e["blocker"], e["blocked_by"]), ("budget", "剩 3 项"))
        a = progress.parser().parse_args(["deliver", "--line", "L", "--node", "a", "--outcome", "PASS",
                                          "--result", "negative", "--summary", "s"])
        e = progress.build_event("deliver", a)
        self.assertEqual(e["result"], "negative")
        self.assertNotIn("blocked_by", e)


class Fold(Base):
    def test_ready_and_dependency_rules(self):
        self.add(ev("node_plan", "router", R, line="L", node="a"))
        self.add(ev("node_plan", "router", R, line="L", node="b", deps=["a"]))
        self.add(ev("node_plan", "router", R, line="L", node="c", deps=["a"], require_review=["a"]))
        n = self.state()["lines"]["L"]["nodes"]
        self.assertEqual((n["a"]["status"], n["b"]["status"], n["c"]["status"]), ("ready", "planned", "planned"))
        self.add(ev("node_dispatch", "router", R, line="L", node="a", refs={"terminal": W}))
        self.add(ev("node_deliver", "import", "import", line="L", node="a", outcome="PARTIAL"))
        n = self.state()["lines"]["L"]["nodes"]
        self.assertEqual((n["b"]["status"], n["c"]["status"]), ("ready", "planned"))
        self.add(ev("node_review", "router", R, line="L", node="a", outcome="PASS"))
        self.assertEqual(self.state()["lines"]["L"]["nodes"]["c"]["status"], "ready")

    def test_fail_and_unknown_do_not_unblock(self):
        self.add(ev("node_plan", "router", R, line="L", node="a"))
        self.add(ev("node_plan", "router", R, line="L", node="b", deps=["a"]))
        self.add(ev("node_dispatch", "router", R, line="L", node="a", refs={"terminal": W}))
        self.add(ev("node_deliver", "import", "import", line="L", node="a", outcome="UNKNOWN"))
        self.assertEqual(self.state()["lines"]["L"]["nodes"]["b"]["status"], "planned")
        self.add(ev("node_deliver", "import", "import", line="L", node="a", outcome="FAIL"))
        self.assertEqual(self.state()["lines"]["L"]["nodes"]["a"]["status"], "failed")

    def test_supersede_hands_dependency_over(self):
        self.add(ev("node_plan", "router", R, line="L", node="a"))
        self.add(ev("node_plan", "router", R, line="L", node="a2"))
        self.add(ev("node_plan", "router", R, line="L", node="b", deps=["a"]))
        self.add(ev("node_supersede", "router", R, line="L", node="a", by="a2"))
        self.add(ev("node_dispatch", "router", R, line="L", node="a2", refs={"terminal": W}))
        self.add(ev("node_deliver", "worker", W, line="L", node="a2", outcome="PASS"))
        st = self.state()["lines"]["L"]
        self.assertEqual(st["nodes"]["b"]["status"], "ready")
        self.assertEqual(st["rework"], 1)

    def test_supersede_target_must_exist(self):
        self.add(ev("node_plan", "router", R, line="L", node="a"))
        self.rejects(ev("node_supersede", "router", R, line="L", node="a", by="ghost"), "bad_supersede")

    def test_milestone_claim_confirm_reject(self):
        self.add(ev("milestone_claim", "router", R, line="L", milestone="M1"))
        st = self.state()
        self.assertEqual(st["milestones"]["M1"]["status"], "claimed")
        self.assertEqual(st["progress"], {"total": 2, "confirmed": 0, "claimed": 1})
        self.add(ev("milestone_reject", "coordinator", C1, milestone="M1", summary="evidence thin"))
        self.assertEqual(self.state()["milestones"]["M1"]["status"], "open")
        self.rejects(ev("milestone_reject", "coordinator", C1, milestone="M1"), "not_claimed")
        self.add(ev("milestone_claim", "router", R, line="L", milestone="M1"))
        self.add(ev("milestone_confirm", "coordinator", C1, milestone="M1"))
        self.assertEqual(self.state()["progress"]["confirmed"], 1)

    def test_empty_project_folds(self):
        st = self.state()
        self.assertEqual(st["event_count"], 0)
        self.assertEqual(st["lines"]["L"]["counts"]["ready"], 0)

    def test_torn_line_ignored(self):
        self.add(ev("node_plan", "router", R, line="L", node="a"))
        with (core.project_dir("t") / "events.jsonl").open("a") as fh:
            fh.write('{"kind": "node_pl')
        self.assertEqual(len(core.read_events("t")), 1)

    def test_auto_dispatch_default(self):
        self.add(ev("node_plan", "router", R, line="L", node="a", spec="b@p", route={"model": "m"}))
        self.add(ev("node_plan", "router", R, line="L", node="b", spec="b@p", route={"model": "m"},
                    auto_dispatch=False))
        self.add(ev("node_plan", "router", R, line="L", node="c"))
        n = self.state()["lines"]["L"]["nodes"]
        self.assertEqual((n["a"]["auto_dispatch"], n["b"]["auto_dispatch"], n["c"]["auto_dispatch"]),
                         (True, False, False))


class Cli(Base):
    # plans here are stamped with the real clock, after the R1 cut-off, and have no spec file: the
    # prerequisite check is tested in test_prereq, not here
    _precheck = None

    @classmethod
    def setUpClass(cls):
        cls._precheck = os.environ.get("PROGRESS_PRECHECK")
        os.environ["PROGRESS_PRECHECK"] = "0"

    @classmethod
    def tearDownClass(cls):
        if cls._precheck is None:
            os.environ.pop("PROGRESS_PRECHECK", None)
        else:
            os.environ["PROGRESS_PRECHECK"] = cls._precheck

    def run_cli(self, *args, handle=R, stdin=None):
        env = dict(os.environ, ORCA_TERMINAL_HANDLE=handle, PROGRESS_PROJECT="t", PROGRESS_PUSH="off")
        r = subprocess.run([sys.executable, str(HERE / "progress.py")] + list(args), env=env,
                           text=True, capture_output=True, input=stdin)
        return r.returncode, json.loads(r.stdout.strip().splitlines()[-1])

    def test_plan_deliver_roundtrip(self):
        code, out = self.run_cli("plan", "--line", "L", "--node", "a", "--summary", "排一个节点",
                                 "--spec", "br@p.md", "--model", "m", "--on", "wsl", "--milestones", "M1")
        self.assertEqual((code, out["ok"]), (0, True))
        self.run_cli("dispatch", "--line", "L", "--node", "a", "--terminal", W, "--summary", "派出")
        code, out = self.run_cli("deliver", "--line", "L", "--node", "a", "--outcome", "PASS",
                                 "--summary", "交付", handle=W)
        self.assertEqual(code, 0, out)
        code, st = self.run_cli("state", "--line", "L")
        self.assertEqual(st["nodes"]["a"]["status"], "delivered")
        self.assertEqual(st["nodes"]["a"]["milestones"], ["M1"])

    def test_structured_error(self):
        code, out = self.run_cli("deliver", "--line", "L", "--node", "nope", "--outcome", "PASS",
                                 "--summary", "x", handle=W)
        self.assertEqual(code, 2)
        self.assertEqual(out["code"], "unknown_node")

    def test_milestones_set_by_coordinator_only(self):
        f = pathlib.Path(self.tmp.name) / "ms.json"
        f.write_text(json.dumps([{"id": "N1", "title": "n1", "lines": ["L"], "after": []}]))
        code, out = self.run_cli("milestones-set", "--core", "c1", str(f), handle=C2)
        self.assertEqual((code, out["code"]), (2, "not_coordinator"))
        f.write_text(json.dumps([{"id": "N1", "title": "n1", "core": "c2", "lines": [], "after": []}]))
        code, out = self.run_cli("milestones-set", "--core", "c1", str(f), handle=C1)
        self.assertEqual(out["code"], "wrong_core")
        f.write_text(json.dumps([{"id": "N1", "title": "n1", "lines": ["L"], "after": ["M9"]}]))
        code, out = self.run_cli("milestones-set", "--core", "c1", str(f), handle=C1)
        self.assertEqual(out["code"], "bad_project")
        f.write_text(json.dumps([{"id": "N1", "title": "n1", "lines": ["L"], "after": []}]))
        code, out = self.run_cli("milestones-set", "--core", "c1", str(f), handle=C1)
        self.assertEqual((code, out["milestones"]), (0, ["N1"]))
        ids = [m["id"] for m in core.load_project("t")["milestones"]]
        self.assertEqual(ids, ["N1"])

    def test_goal_set_roles_and_event(self):
        f = pathlib.Path(self.tmp.name) / "goal.json"
        f.write_text(json.dumps({"text": "新目标", "done_when": ["D1 甲", "D2 乙"]}, ensure_ascii=False))
        code, out = self.run_cli("goal-set", str(f), handle=W)
        self.assertEqual((code, out["code"]), (2, "not_allowed"))
        code, out = self.run_cli("goal-set", str(f), "--maintainer", handle=W)
        self.assertEqual((code, out["code"]), (2, "bad_input"))      # maintainer must say who asked
        code, out = self.run_cli("goal-set", str(f), "--maintainer", "--why", "项目负责人 11-06 14:45 要求", handle=W)
        self.assertEqual((code, out["role"]), (0, "maintainer"))
        fg = core.load_project("t")["final_goal"]
        self.assertEqual((fg["text"], fg["done_when"]), ("新目标", ["D1 甲", "D2 乙"]))
        ev = [e for e in core.read_events("t") if e["kind"] == "project_update"][-1]
        self.assertEqual(ev["actor"]["role"], "maintainer")
        self.assertIn("项目负责人", ev["refs"]["why"])
        f.write_text(json.dumps({"text": "x", "done_when": ""}))
        code, out = self.run_cli("goal-set", str(f), handle=C1)
        self.assertEqual((code, out["code"]), (2, "bad_input"))
        f.write_text(json.dumps({"text": "协调人改", "done_when": "写明"}, ensure_ascii=False))
        code, out = self.run_cli("goal-set", str(f), handle=C1)
        self.assertEqual((code, out["role"]), (0, "coordinator"))

    def test_maintainer_role_cannot_touch_nodes(self):
        with self.assertRaises(core.ProgressError):
            core.append("t", {"kind": "node_plan", "line": "L", "node": "zz", "summary": "x",
                              "actor": {"role": "maintainer", "handle": W}})

    def test_forward_puts_global_flag_before_subcommand(self):
        import progress as cli
        seen = {}
        def fake_run(argv, **kw):
            seen["cmd"] = argv[-1]
            class R: returncode = 0; stdout = '{"ok": true}'; stderr = ""
            return R()
        orig = cli.subprocess.run
        cli.subprocess.run = fake_run
        try:
            cli.forward("t", {"kind": "node_plan"})
        finally:
            cli.subprocess.run = orig
        words = seen["cmd"].split()
        self.assertLess(words.index("--project"), words.index("append-raw"), seen["cmd"])
        # and the real parser accepts that order
        cli.parser().parse_args(words[2:])

    def test_bad_raw_json(self):
        code, out = self.run_cli("append-raw", stdin="{not json")
        self.assertEqual((code, out["code"]), (2, "bad_input"))


if __name__ == "__main__":
    unittest.main()


class HumanBlocker(unittest.TestCase):
    def test_human_is_a_blocker(self):
        self.assertIn("human", core.BLOCKERS)
