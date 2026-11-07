"""R1 (router rules v2, 11-07): a node's spec lists its inputs under 前置条件; dispatch checks them mechanically."""
import datetime as dt
import json
import os
import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import prereq  # noqa: E402
import progress_core as core  # noqa: E402
import test_patrol as tp  # noqa: E402
from fakes import SPEC_HEADER, FakeRunner  # noqa: E402

SPEC = json.dumps(SPEC_HEADER) + """

# 节点 a

## 前置条件
- 上游 b 的交付：`node:b`
- 基线提交 `sha:abc1234`
- 输入表 `file:docs/table.md`，以及 `file:other@data/x.csv`

## 目标
做完。
- 这一条不在前置条件里
"""


def git_runner(present, unreadable=False, remote=None):
    """git cat-file -e answers from `present`; `unreadable` = local repo not readable (launchd); remote: set or None."""
    calls = []

    def run(argv, timeout=30, input_text=None):
        calls.append(argv)
        if argv[0] == "git":
            if unreadable:
                return 128, "", "fatal: not a git repository (or any parent up to mount point /Volumes)"
            return (0, "", "") if argv[-1] in present else (128, "", "fatal: Not a valid object name %s" % argv[-1])
        if argv[0] == "ssh":
            if remote is None:
                return 255, "", "ssh: connect to host wsl: Connection refused"
            obj = argv[-1].split()[-1].strip("'")
            return (0, "", "") if obj in remote else (128, "", "fatal: path does not exist")
        return 1, "", "unexpected"
    run.calls = calls
    return run


def lines(b_status="delivered", b_outcome="PASS", review=None, **extra):
    nodes = {"b": dict({"status": b_status, "outcome": b_outcome, "review": review}, **extra), "a": {"status": "ready"}}
    return {"L": {"nodes": nodes}}


class Parse(unittest.TestCase):
    def test_items_and_tokens(self):
        p = prereq.parse(SPEC)
        self.assertEqual(p["format"], [])
        self.assertEqual([t for it in p["items"] for t in it["tokens"]],
                         [("node", "b"), ("sha", "abc1234"), ("file", "docs/table.md"), ("file", "other@data/x.csv")])

    def test_missing_section_empty_none_and_untokened(self):
        self.assertIn("没有「前置条件」", prereq.parse("# x\n- `node:b`\n")["format"][0])
        self.assertTrue(prereq.parse("## 前置条件\n\n## 目标\n")["format"])
        self.assertEqual(prereq.parse("## 前置条件\n- 无\n"), {"items": [], "format": []})
        p = prereq.parse("### 前置条件\n- `node:b`\n- 上游的表格（没写路径）\n#### 细节\n- x\n## 下一节\n")
        self.assertEqual(len(p["items"]), 3)          # a deeper heading does not end the section
        self.assertIn("第 2 项", p["format"][0])


class Check(unittest.TestCase):
    PRESENT = {"abc1234^{commit}", "br:docs/table.md", "other:data/x.csv"}

    def test_all_present(self):
        r = prereq.check(SPEC, "br", "L", lines(), "/repo", git_runner(self.PRESENT))
        self.assertTrue(r["ok"], r)
        self.assertEqual(prereq.summary(r), "前置条件齐全")

    def test_missing_items_named(self):
        r = prereq.check(SPEC, "br", "L", lines(b_status="running", b_outcome=None), "/repo",
                         git_runner(self.PRESENT - {"other:data/x.csv"}))
        self.assertEqual(r["missing"], ["node:b", "file:other@data/x.csv"])
        self.assertFalse(r["ok"])
        self.assertTrue(prereq.summary(r).startswith("前置条件缺 2 项：node:b"))

    def test_node_rules(self):
        self.assertFalse(prereq.node_met(lines(b_outcome="PARTIAL"), "L", "b"))      # old PARTIAL does not count
        self.assertFalse(prereq.node_met(lines(review="FAIL"), "L", "b"))
        self.assertTrue(prereq.node_met(lines(b_status="reviewed", review="PASS"), "L", "b"))
        sup = {"L": {"nodes": {"b": {"status": "superseded", "by": "b2"},
                               "b2": {"status": "delivered", "outcome": "PASS"}}}}
        self.assertTrue(prereq.node_met(sup, "L", "b"))
        self.assertIsNone(prereq.node_met(sup, "L", "Z/b"))                           # unknown line
        self.assertFalse(prereq.node_met(lines(), "L", "nope"))

    def test_launchd_falls_back_to_remote_and_unknown_when_nobody_answers(self):
        run = git_runner(set(), unreadable=True, remote=self.PRESENT)
        rem = {"host": "wsl", "repo": "/r"}
        self.assertTrue(prereq.check(SPEC, "br", "L", lines(), "/repo", run, rem)["ok"])
        r = prereq.check(SPEC, "br", "L", lines(), "/repo", git_runner(set(), unreadable=True), rem)
        self.assertEqual(r["missing"], [])
        self.assertEqual(r["unknown"], ["sha:abc1234", "file:docs/table.md", "file:other@data/x.csv"])
        # local says missing, remote has it (branch only on the worker host): present
        r = prereq.check(SPEC, "br", "L", lines(), "/repo", git_runner(set(), remote=self.PRESENT), rem)
        self.assertTrue(r["ok"], r)


class PrereqRunner(FakeRunner):
    """FakeRunner whose `git cat-file -e` answers from a set; `git show` returns the spec."""

    def __init__(self, present, **kw):
        super().__init__(spec=SPEC, **kw)
        self.present = present

    def __call__(self, argv, input_text=None, timeout=60):
        if argv[0] == "git" and "cat-file" in argv:
            self.calls.append(argv)
            return (0, "", "") if argv[-1] in self.present else (128, "", "fatal: Not a valid object name")
        return super().__call__(argv, input_text=input_text, timeout=timeout)


NEW = "2026-11-08T10:00:00Z"     # planned after prereq.SINCE


class Patrol(tp.Base):
    """Patrol checks before auto-dispatch, writes node_block dep naming the missing inputs, dispatches once they exist."""

    def setUp(self):
        super().setUp()
        self.now_backup = tp.NOW
        tp.NOW = dt.datetime(2026, 11, 8, 12, 0, 0, tzinfo=dt.timezone.utc)

    def tearDown(self):
        tp.NOW = self.now_backup
        super().tearDown()

    def run_at(self, fr, minutes_later=0):
        import patrol
        import snapshot as snap
        return patrol.run("t", apply=True, runner=fr, now=tp.NOW + dt.timedelta(minutes=minutes_later),
                          store=snap.Store("t", fr))

    def plan_b_and_a(self):
        self.add(tp.plan_ev("L", "b", at=NEW, auto_dispatch=False),
                 tp.plan_ev("L", "a", at=NEW))
        self.put_live(dict(tp.live(), generated_at="2026-11-08T11:58:00Z"))

    def test_missing_input_blocks_then_dispatches_when_it_appears(self):
        self.plan_b_and_a()
        fr = PrereqRunner(Check.PRESENT)
        self.run_at(fr)
        self.assertEqual(fr.kinds("worker-start"), [])
        blk = self.events("node_block")
        self.assertEqual(len(blk), 1)
        self.assertEqual((blk[0]["blocker"], blk[0]["prereq_missing"]), ("dep", ["node:b"]))
        n = self.node("L", "a")
        self.assertEqual((n["status"], n["prereq_missing"]), ("blocked", ["node:b"]))
        sends = [c[c.index("--text") + 1] for c in fr.kinds("send")]
        self.assertTrue(any("代派前核对 L/a" in t and "node:b" in t for t in sends), sends)
        # still missing: nothing new
        self.run_at(fr, 15)
        self.assertEqual(len(self.events("node_block")), 1)
        self.assertEqual(fr.kinds("worker-start"), [])
        # b delivered PASS -> patrol dispatches a
        self.add({"kind": "node_dispatch", "line": "L", "node": "b", "actor": {"role": "router", "handle": tp.R},
                  "summary": "派", "refs": {"terminal": "term_w"}, "at": "2026-11-08T12:20:00Z"},
                 {"kind": "node_deliver", "line": "L", "node": "b", "actor": {"role": "router", "handle": tp.R},
                  "summary": "交", "outcome": "PASS", "at": "2026-11-08T12:25:00Z"})
        self.run_at(fr, 30)
        self.assertEqual(len(fr.kinds("worker-start")), 1)
        self.assertEqual(self.node("L", "a")["status"], "running")
        self.assertNotIn("prereq_missing", self.node("L", "a"))

    def test_spec_without_section_is_listed_not_dispatched(self):
        self.add(tp.plan_ev("L", "a", at=NEW))
        self.put_live(dict(tp.live(), generated_at="2026-11-08T11:58:00Z"))
        fr = PrereqRunner(set())
        fr.spec = json.dumps(SPEC_HEADER) + "\n\n# 没有前置条件\n"
        out = self.run_at(fr)
        self.assertEqual(fr.kinds("worker-start"), [])
        self.assertTrue(any(f.get("code") == "prereq_format" for f in out["findings"]), out["findings"])
        self.assertEqual(self.events("node_block"), [])

    def test_old_node_without_section_still_dispatched(self):
        self.add(tp.plan_ev("L", "a", at="2026-11-07T10:00:00Z"))
        self.put_live(dict(tp.live(), generated_at="2026-11-08T11:58:00Z"))
        fr = PrereqRunner(set())
        fr.spec = json.dumps(SPEC_HEADER) + "\n\n# 旧说明\n"
        self.run_at(fr)
        self.assertEqual(len(fr.kinds("worker-start")), 1)

    def test_manual_node_gets_a_ready_note_instead(self):
        self.add(tp.plan_ev("L", "b", at=NEW, auto_dispatch=False),
                 tp.plan_ev("L", "a", at=NEW, auto_dispatch=False))
        self.put_live(dict(tp.live(), generated_at="2026-11-08T11:58:00Z"))
        self.add({"kind": "node_block", "line": "L", "node": "a", "blocker": "dep", "prereq_missing": ["node:b"],
                  "summary": "前置条件缺 1 项：node:b", "actor": {"role": "router", "handle": tp.R},
                  "at": "2026-11-08T11:00:00Z"})
        fr = PrereqRunner(Check.PRESENT)
        self.run_at(fr)
        self.assertFalse(any("前置条件已经齐了" in c[c.index("--text") + 1] for c in fr.kinds("send")))
        self.add({"kind": "node_dispatch", "line": "L", "node": "b", "actor": {"role": "router", "handle": tp.R},
                  "summary": "派", "at": "2026-11-08T12:20:00Z"},
                 {"kind": "node_deliver", "line": "L", "node": "b", "actor": {"role": "router", "handle": tp.R},
                  "summary": "交", "outcome": "PASS", "at": "2026-11-08T12:25:00Z"})
        self.run_at(fr, 90)
        texts = [c[c.index("--text") + 1] for c in fr.kinds("send")]
        self.assertTrue(any("a 的前置条件已经齐了" in t for t in texts), texts)
        self.assertEqual(fr.kinds("worker-start"), [])
        self.run_at(fr, 105)
        self.assertEqual(sum("a 的前置条件已经齐了" in c[c.index("--text") + 1] for c in fr.kinds("send")), 1)


class Cli(tp.Base):
    """`progress dispatch` refuses a router dispatch whose inputs are missing and records node_block dep instead."""

    def setUp(self):
        super().setUp()
        self.add(tp.plan_ev("L", "b", at=NEW), tp.plan_ev("L", "a", at=NEW))
        import progress
        self.progress = progress
        self.orig = progress._run
        self.present = set(Check.PRESENT)

        def fake_run(argv, timeout=30):
            if argv[:2] == ["git", "rev-parse"]:
                return 0, "/repo\n", ""
            if argv[0] == "git" and "show" in argv:
                return 0, SPEC, ""
            if argv[0] == "git" and "cat-file" in argv:
                return (0, "", "") if argv[-1] in self.present else (128, "", "fatal: Not a valid object name")
            return 255, "", "no ssh in tests"
        progress._run = fake_run
        os.environ["ORCA_TERMINAL_HANDLE"] = tp.R
        os.environ["PROGRESS_PUSH"] = "off"
        os.environ.pop("PROGRESS_REMOTE_REPO", None)

    def tearDown(self):
        self.progress._run = self.orig
        os.environ.pop("ORCA_TERMINAL_HANDLE", None)
        super().tearDown()

    def call(self, *args):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self.progress.main(["--project", "t"] + list(args))
        return rc, json.loads(buf.getvalue().strip().splitlines()[-1])

    def test_dispatch_refused_and_blocked(self):
        rc, out = self.call("dispatch", "--line", "L", "--node", "a", "--summary", "派 a", "--terminal", "term_w")
        self.assertEqual((rc, out["code"], out["dispatch_refused"]), (2, "prereq_missing", True))
        self.assertEqual(self.events("node_dispatch"), [])
        blk = self.events("node_block")
        self.assertEqual((blk[-1]["blocker"], blk[-1]["prereq_missing"], blk[-1]["actor"]["role"]),
                         ("dep", ["node:b"], "router"))

    def test_precheck_dry_run_then_dispatch_when_met(self):
        rc, out = self.call("precheck", "--line", "L", "--node", "a", "--dry-run")
        self.assertEqual((rc, out["precheck"], out["res"]["missing"]), (2, "fail", ["node:b"]))
        self.assertEqual(self.events("node_block"), [])
        self.add({"kind": "node_dispatch", "line": "L", "node": "b", "actor": {"role": "router", "handle": tp.R},
                  "summary": "派"},
                 {"kind": "node_deliver", "line": "L", "node": "b", "actor": {"role": "router", "handle": tp.R},
                  "summary": "交", "outcome": "PASS"})
        rc, out = self.call("precheck", "--line", "L", "--node", "a")
        self.assertEqual((rc, out["precheck"]), (0, "ok"))
        rc, out = self.call("dispatch", "--line", "L", "--node", "a", "--summary", "派 a", "--terminal", "term_w")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.events("node_dispatch")[-1]["prereq"], {"ok": True, "summary": "前置条件齐全"})

    def test_precheck_writes_block(self):
        rc, out = self.call("precheck", "--line", "L", "--node", "a")
        self.assertEqual((rc, out["precheck"]), (2, "missing"))
        self.assertEqual(self.events("node_block")[-1]["prereq_missing"], ["node:b"])


if __name__ == "__main__":
    unittest.main()
