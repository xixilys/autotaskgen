"""R3 (router rules v2, 11-07): a PASS delivery passes a mechanical gate, then 1-3 Terra max reviewers judge it."""
import datetime as dt
import json
import os
import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import progress_core as core  # noqa: E402
import review_gate as rg  # noqa: E402
import test_patrol as tp  # noqa: E402
from fakes import FakeRunner  # noqa: E402

HEAD = "abc1234def5678"
HEADER = {"route": {"agent": "codex", "model": "codex/gpt-6.1-terra", "effort": "max", "on": "wsl"},
          "acceptance": [{"id": "contract"}, {"id": "boundary"}], "base_head": "base0001",
          "write_scope": ["docs/r/"]}
SPEC = json.dumps(HEADER) + "\n\n# 节点 a\n\n## 前置条件\n- 上游表 `file:docs/in.md`\n\n## 目标\n做完。\n"
REPORT = ("# a\n本单检查 `head=abc1234`。\n| 项 | 结果 |\n|---|---|\n| contract | PASS |\n| boundary | PASS |\n"
          "输入 docs/in.md 已用。\n```text\nPASS: all\n```\n")
NEW_AT = "2026-11-08T11:00:00Z"


class GitFake:
    """Answers the git questions the gate asks."""

    def __init__(self, report=REPORT, ancestors=(), names="", numstat="3\t1\tdocs/r/x.py\n", spec=SPEC, down=False):
        self.report, self.ancestors, self.names, self.numstat, self.spec, self.down = \
            report, set(ancestors), names, numstat, spec, down

    def answer(self, args):
        if self.down:
            return 128, "", "fatal: not a git repository"
        if args[0] == "show":
            ref = args[1]
            if ref == "%s:docs/r/REPORT.md" % HEAD and self.report is not None:
                return 0, self.report, ""
            if ref.endswith(":docs/a.md") or ref.endswith(":docs/spec.md"):
                return 0, self.spec, ""
            return 128, "", "fatal: path does not exist"
        if args[:2] == ["merge-base", "--is-ancestor"]:
            return (0, "", "") if args[2] in self.ancestors else (1, "", "")
        if args[0] == "merge-base":
            return 0, "base0001\n", ""
        if args[:2] == ["diff", "--name-only"]:
            return 0, self.names, ""
        if args[:2] == ["diff", "--numstat"]:
            return 0, self.numstat, ""
        if args[0] == "cat-file":
            return 0, "", ""
        return 128, "", "fatal: unexpected"

    def __call__(self, argv, timeout=30, input_text=None):
        assert argv[0] == "git" and argv[1] == "-C"
        return self.answer(argv[3:])


def gate(fake, spec=SPEC, header=HEADER, head=HEAD, report="docs/r/REPORT.md"):
    return rg.gate(spec, header, head, report, rg.Git("/repo", fake))


class Gate(unittest.TestCase):
    def test_complete_report_passes(self):
        g = gate(GitFake())
        self.assertTrue(g["ok"], g)
        self.assertEqual(g["tested_head"], "abc1234")

    def test_tested_head_may_precede_a_report_only_commit(self):
        rep = REPORT.replace("abc1234", "1111111")
        self.assertTrue(gate(GitFake(report=rep, ancestors={"1111111"}, names="docs/r/REPORT.md\n"))["ok"])
        g = gate(GitFake(report=rep, ancestors={"1111111"}, names="docs/r/REPORT.md\nsrc/core.py\n"))
        self.assertFalse(g["ok"])
        self.assertIn("src/core.py", g["fails"][0])
        g = gate(GitFake(report=rep))
        self.assertIn("不在交付版本的历史里", g["fails"][0])

    def test_each_missing_item_is_named(self):
        g = gate(GitFake(report="# a\n没写版本\n| contract | PASS |\n"))
        text = "；".join(g["fails"])
        for want in ("head=", "boundary", "代码块", "file:docs/in.md"):
            self.assertIn(want, text)
        self.assertIn("不在交付的提交", gate(GitFake(report=None))["fails"][0])
        self.assertIn("--report", gate(GitFake(), report=None)["fails"][0])

    def test_no_acceptance_no_prereq_section_only_head_needed(self):
        g = gate(GitFake(report="head=abc1234 done"), spec="# old\n", header={})
        self.assertTrue(g["ok"], g)

    def test_git_unreachable_is_unknown_not_fail(self):
        g = gate(GitFake(down=True))
        self.assertEqual((g["ok"], g["fails"], g["unknown"]), (False, [], ["报告"]))

    def test_reviewer_count(self):
        self.assertEqual(rg.reviewer_count(HEADER, {}, ["docs/r/x.py"], 20, "docs/r/REPORT.md")[0], 1)
        k, why = rg.reviewer_count(HEADER, {}, ["docs/r/x.py", "tools/shared.py"], 20, "docs/r/REPORT.md")
        self.assertEqual(k, 2)
        self.assertIn("tools/shared.py", why[0])
        self.assertEqual(rg.reviewer_count(HEADER, {"milestones": ["B1"]}, ["tools/s.py"], 5000, None)[0], 3)
        self.assertEqual(rg.reviewer_count({}, {}, ["docs/x.md"], 10, None)[0], 1)   # no scope: docs/ is own
        self.assertEqual(rg.strictest(["PASS", "PARTIAL", "UNKNOWN"]), "PARTIAL")


def ev(kind, node, at, role="router", handle=tp.R, **kw):
    e = {"kind": kind, "line": "L", "node": node, "at": at, "summary": kind, "actor": {"role": role, "handle": handle}}
    e.update(kw)
    return e


class Fold(tp.Base):
    """Reviewers write node_review on the delivered node; the node settles on the strictest verdict."""

    def setUp(self):
        super().setUp()
        self.add(ev("node_plan", "a", "2026-11-08T10:00:00Z", spec="br@docs/a.md", route=tp.ROUTE),
                 ev("node_dispatch", "a", "2026-11-08T10:01:00Z", refs={"terminal": "term_author"}),
                 ev("node_deliver", "a", NEW_AT, outcome="PASS", refs={"head": HEAD, "report": "docs/r/REPORT.md"}))
        for i, w in ((1, "term_rv1"), (2, "term_rv2")):
            rid = "a-review1-%d" % i
            self.add(ev("node_plan", rid, "2026-11-08T11:05:00Z", role="patrol", handle="patrol@mac",
                        node_kind="review", review_of="a", deps=["a"], auto_dispatch=True),
                     ev("auto_dispatch", rid, "2026-11-08T11:06:00Z", role="patrol", handle="patrol@mac"),
                     ev("auto_dispatch", rid, "2026-11-08T11:06:01Z", role="patrol", handle="patrol@mac",
                        phase="started", refs={"terminal": w}))

    def verdict(self, rid, w, outcome, at):
        self.add(ev("node_review", "a", at, role="worker", handle=w, review_node=rid, outcome=outcome,
                    failures=["X_MISSING"] if outcome != "PASS" else None))

    def test_pending_then_strictest(self):
        self.verdict("a-review1-1", "term_rv1", "PASS", "2026-11-08T11:30:00Z")
        n = self.node("L", "a")
        self.assertEqual((n["status"], n.get("review"), n["review_pending"]), ("delivered", None, ["a-review1-2"]))
        self.assertEqual(self.node("L", "a-review1-1")["status"], "reviewed")
        self.verdict("a-review1-2", "term_rv2", "PARTIAL", "2026-11-08T11:40:00Z")
        n = self.node("L", "a")
        self.assertEqual((n["status"], n["review"], n["review_by"]), ("reviewed", "PARTIAL", "auto"))
        self.assertNotIn("review_pending", n)

    def test_only_the_reviewer_may_answer(self):
        with self.assertRaises(core.ProgressError):
            self.verdict("a-review1-1", "term_author", "PASS", "2026-11-08T11:30:00Z")
        with self.assertRaises(core.ProgressError):    # a worker review without --review-node
            self.add(ev("node_review", "a", "2026-11-08T11:31:00Z", role="worker", handle="term_rv1", outcome="PASS"))
        with self.assertRaises(core.ProgressError):    # patrol plans only review nodes
            self.add(ev("node_plan", "z", "2026-11-08T11:32:00Z", role="patrol", handle="patrol@mac"))

    def test_cancelled_reviewer_not_waited_for_and_router_overrides(self):
        self.verdict("a-review1-1", "term_rv1", "FAIL", "2026-11-08T11:30:00Z")
        self.add(ev("node_cancel", "a-review1-2", "2026-11-08T11:35:00Z"))
        self.assertEqual((self.node("L", "a")["status"], self.node("L", "a")["review"]), ("failed", "FAIL"))
        self.add(ev("node_review", "a", "2026-11-08T11:50:00Z", outcome="PASS"))
        self.assertEqual(self.node("L", "a")["review"], "PASS")

    def test_redelivery_starts_over(self):
        self.verdict("a-review1-1", "term_rv1", "PASS", "2026-11-08T11:30:00Z")
        self.add(ev("node_deliver", "a", "2026-11-08T12:00:00Z", outcome="PASS", refs={"head": HEAD}))
        n = self.node("L", "a")
        self.assertEqual((n["status"], n.get("auto_reviews"), n.get("review_pending")), ("delivered", None, None))


class GateRunner(FakeRunner):
    def __init__(self, git, **kw):
        super().__init__(**kw)
        self.git = git

    def __call__(self, argv, input_text=None, timeout=60):
        if argv[0] == "git":
            self.calls.append(argv)
            return self.git(argv, timeout=timeout)
        return super().__call__(argv, input_text=input_text, timeout=timeout)


class Patrol(tp.Base):
    def setUp(self):
        super().setUp()
        self.now_backup = tp.NOW
        tp.NOW = dt.datetime(2026, 11, 8, 12, 0, 0, tzinfo=dt.timezone.utc)
        self.add(ev("node_plan", "a", "2026-11-08T10:00:00Z", spec="br@docs/a.md", route=tp.ROUTE, auto_dispatch=False),
                 ev("node_dispatch", "a", "2026-11-08T10:01:00Z", refs={"terminal": "term_author"}),
                 ev("node_deliver", "a", NEW_AT, outcome="PASS", refs={"head": HEAD, "report": "docs/r/REPORT.md"}))
        self.put_live(dict(tp.live(), generated_at="2026-11-08T11:58:00Z"))

    def tearDown(self):
        tp.NOW = self.now_backup
        super().tearDown()

    def run_at(self, fr, minutes_later=0):
        import patrol
        import snapshot as snap
        return patrol.run("t", apply=True, runner=fr, now=tp.NOW + dt.timedelta(minutes=minutes_later),
                          store=snap.Store("t", fr))

    def sends(self, fr):
        return [c[c.index("--text") + 1] for c in fr.kinds("send")]

    def test_gate_pass_plans_and_dispatches_a_terra_max_reviewer(self):
        fr = GateRunner(GitFake())
        self.run_at(fr)
        plans = [e for e in self.events("node_plan") if e["actor"]["role"] == "patrol"]
        self.assertEqual([p["node"] for p in plans], ["a-review1-1"])
        self.assertEqual((plans[0]["review_of"], plans[0]["review_base"]), ("a", "base0001"))
        starts = fr.kinds("worker-start")
        self.assertEqual(len(starts), 1)
        argv = starts[0]
        spec = argv[argv.index("--spec") + 1]
        self.assertIn("--review-node a-review1-1", spec)
        self.assertIn("git diff base0001 %s" % HEAD, spec)
        self.assertNotIn("progress deliver", spec)            # no worker snippet appended
        self.assertEqual((argv[argv.index("--model") + 1], argv[argv.index("--effort") + 1]),
                         ("codex/gpt-6.1-terra", "max"))
        self.assertTrue(any("过了机械门" in t for t in self.sends(fr)), self.sends(fr))
        self.assertEqual(self.node("L", "a-review1-1")["status"], "running")
        # next round: nothing new, and the delivery is not listed as unreviewed for the router
        out = self.run_at(fr, 15)
        self.assertEqual(len(fr.kinds("worker-start")), 1)
        self.assertFalse([f for f in out["findings"] if f.get("code") == "unreviewed" and f.get("node") == "a"])
        # the reviewer answers -> the router hears the verdict once
        w = self.node("L", "a-review1-1")["worker"]
        self.add(ev("node_review", "a", "2026-11-08T12:20:00Z", role="worker", handle=w, review_node="a-review1-1",
                    outcome="PARTIAL", summary="边界项没有证据", failures=["BOUNDARY_NO_EVIDENCE"]))
        self.run_at(fr, 30)
        got = [t for t in self.sends(fr) if "独立审查结论 PARTIAL" in t]
        self.assertEqual(len(got), 1, self.sends(fr))
        self.assertIn("BOUNDARY_NO_EVIDENCE", got[0])
        self.run_at(fr, 45)
        self.assertEqual(sum("独立审查结论" in t for t in self.sends(fr)), 1)

    def test_shared_code_and_milestone_get_three_reviewers(self):
        self.add(ev("node_plan", "a", "2026-11-08T10:00:00Z", spec="br@docs/a.md", route=tp.ROUTE,
                    auto_dispatch=False, milestones=["M1"]))
        self.add(ev("node_deliver", "a", "2026-11-08T11:10:00Z", outcome="PASS",
                    refs={"head": HEAD, "report": "docs/r/REPORT.md"}))
        fr = GateRunner(GitFake(numstat="900\t10\ttools/shared.py\n"))
        self.run_at(fr)
        self.assertEqual(len([e for e in self.events("node_plan") if e["actor"]["role"] == "patrol"]), 3)
        self.assertEqual(len(fr.kinds("worker-start")), 3)

    def test_gate_fail_tells_the_router_and_plans_nothing(self):
        fr = GateRunner(GitFake(report="# a\n没写版本\n"))
        out = self.run_at(fr)
        self.assertEqual([e for e in self.events("node_plan") if e["actor"]["role"] == "patrol"], [])
        self.assertTrue(any(f.get("code") == "review_gate_fail" for f in out["findings"]))
        self.assertTrue(any("没过机械门" in t and "head=" in t for t in self.sends(fr)), self.sends(fr))
        obs = [e for e in self.events("observe") if e.get("code") == "review_gate"]
        self.assertEqual((len(obs), obs[0]["gate_ok"]), (1, False))
        self.run_at(fr, 15)
        self.assertEqual(len([e for e in self.events("observe") if e.get("code") == "review_gate"]), 1)

    def test_old_delivery_and_git_down_are_left_alone(self):
        fr = GateRunner(GitFake(down=True))
        out = self.run_at(fr)
        self.assertTrue(any(f.get("code") == "review_gate_unchecked" for f in out["findings"]))
        self.assertEqual([e for e in self.events("observe") if e.get("code") == "review_gate"], [])
        orig = rg.SINCE
        rg.SINCE = "2026-11-09T00:00:00Z"
        try:
            out = self.run_at(GateRunner(GitFake()), 15)
        finally:
            rg.SINCE = orig
        self.assertFalse([f for f in out["findings"] if str(f.get("code", "")).startswith("review_")])

    def test_kill_switch(self):
        proj = core.load_project("t")
        proj.setdefault("defaults", {})["auto_review"] = {"enabled": False}
        core.save_project(proj)
        fr = GateRunner(GitFake())
        self.run_at(fr)
        self.assertEqual(fr.kinds("worker-start"), [])


class Cli(tp.Base):
    def test_review_node_writes_as_worker_without_push(self):
        # (events dated before today: the CLI stamps the review with the real clock)
        import contextlib
        import io
        import progress
        self.add(ev("node_plan", "a", "2026-11-06T10:00:00Z"),
                 ev("node_dispatch", "a", "2026-11-06T10:01:00Z"),
                 ev("node_deliver", "a", "2026-11-06T11:00:00Z", outcome="PASS"),
                 ev("node_plan", "a-review1-1", "2026-11-06T11:05:00Z", role="patrol", handle="patrol@mac",
                    node_kind="review", review_of="a", deps=["a"]),
                 ev("auto_dispatch", "a-review1-1", "2026-11-06T11:06:00Z", role="patrol", handle="patrol@mac",
                    refs={"terminal": "term_rv1"}))
        os.environ["ORCA_TERMINAL_HANDLE"] = "term_rv1"
        orig = progress.workpush.push_work_branch
        progress.workpush.push_work_branch = lambda *a, **k: self.fail("a reviewer must not push")
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = progress.main(["--project", "t", "review", "--line", "L", "--node", "a", "--review-node",
                                    "a-review1-1", "--outcome", "PASS", "--summary", "证据齐", "--failure", "NONE"])
        finally:
            progress.workpush.push_work_branch = orig
            os.environ.pop("ORCA_TERMINAL_HANDLE", None)
        self.assertEqual(rc, 0, buf.getvalue())
        e = self.events("node_review")[-1]
        self.assertEqual((e["actor"]["role"], e["review_node"], e["failures"]), ("worker", "a-review1-1", ["NONE"]))
        self.assertEqual(self.node("L", "a")["review"], "PASS")


if __name__ == "__main__":
    unittest.main()
