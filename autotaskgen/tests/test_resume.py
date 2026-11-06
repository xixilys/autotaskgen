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
import progress_inspect as ins  # noqa: E402
import patrol  # noqa: E402
import snapshot as snap  # noqa: E402
from fakes import FakeRunner  # noqa: E402
from test_patrol import project, plan_ev, live, at, NOW, R, C1  # noqa: E402


def ev(kind, line="L", node=None, minutes_ago=100, role="router", **kw):
    handle = {"coordinator": C1, "patrol": "patrol@mac"}.get(role, R)
    e = {"kind": kind, "line": line, "actor": {"role": role, "handle": handle}, "summary": kw.pop("summary", kind),
         "at": at(minutes_ago)}
    if node:
        e["node"] = node
    e.update(kw)
    return e


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["PROGRESS_HOME"] = self.tmp.name
        os.environ["PROGRESS_CACHE_DIR"] = self.tmp.name + "/cache"
        os.environ.pop("ORCA_TERMINAL_HANDLE", None)
        core.save_project(project())

    def tearDown(self):
        os.environ.pop("PROGRESS_CACHE_DIR", None)
        self.tmp.cleanup()

    def add(self, *evs):
        for e in evs:
            core.append("t", e)

    def pack(self, node=None, max_chars=12000, **kw):
        return ins.build_resume(core.load_project("t"), core.read_events("t"), "L", node, max_chars, now=at(0), **kw)


class Pack(Base):
    def seed(self):
        self.add(plan_ev("L", "a", 300), plan_ev("L", "b", 290, deps=["a"]), plan_ev("L", "c", 280),
                 plan_ev("L", "d", 270))
        self.add(ev("node_dispatch", node="a", minutes_ago=250, refs={"terminal": "term_w1"}),
                 ev("node_deliver", node="a", minutes_ago=200, outcome="PASS", summary="a 做完",
                    refs={"report": "docs/a.md", "head": "abc123"}),
                 ev("node_dispatch", node="c", minutes_ago=150, refs={"terminal": "term_w2"}),
                 ev("node_block", node="c", minutes_ago=100, blocker="human", summary="要用户给授权"),
                 ev("node_supersede", node="d", minutes_ago=90, by="b"))

    def test_sections_and_content(self):
        self.seed()
        self.add(ev("project_update", line=None, role="coordinator", minutes_ago=50, summary="协调人改了方向"),
                 ev("line_open", minutes_ago=400, role="coordinator", summary="开线说明"))
        t = self.pack(followups=[{"id": "fu_1", "to": "router", "text": "把 b 派出去", "done_when": "b 已派", "check_at": at(0)}],
                      lessons=[("memory/L/LESSONS.md", ["规则一", "规则二"])])
        for h in ("最终目标", "未完成节点", "最近裁决与用户拍板", "未落实的跟进", "最近交付", "经验"):
            self.assertIn("## " + h, t)
        self.assertIn("`b`", t)
        self.assertIn("卡点 human：要用户给授权", t)
        self.assertIn("term_w2", t)
        self.assertNotIn("`a`（", t)      # delivered
        self.assertNotIn("`d`（", t)      # superseded
        self.assertIn("协调人改了方向", t)
        self.assertIn("开线说明", t)
        self.assertIn("a 做完", t)
        self.assertIn("docs/a.md", t)
        self.assertIn("abc123", t)
        self.assertIn("把 b 派出去", t)
        self.assertIn("规则二", t)

    def test_missing_lessons_omits_section(self):
        self.seed()
        t = self.pack(lessons=ins.read_lessons("t", "L"))
        self.assertNotIn("## 经验", t)

    def test_lessons_first_40_lines(self):
        d = pathlib.Path(self.tmp.name, "t", "memory", "L")
        d.mkdir(parents=True)
        d.joinpath("LESSONS.md").write_text("\n".join("行%d" % i for i in range(100)), encoding="utf-8")
        pathlib.Path(self.tmp.name, "t", "memory", "GLOBAL.md").write_text("全局规则", encoding="utf-8")
        got = ins.read_lessons("t", "L")
        self.assertEqual(len(got[0][1]), 40)
        self.assertEqual(got[0][1][-1], "行39")
        self.assertEqual(got[1][0], "memory/GLOBAL.md")

    def test_trim_drops_oldest_deliveries_then_rulings(self):
        self.add(plan_ev("L", "a", 900))
        for i in range(30):
            self.add(ev("node_deliver", node="a", minutes_ago=800 - i, outcome="PASS", summary="交付第%02d次" % i + "x" * 80))
            self.add(ev("decision", node="a", role="patrol", minutes_ago=700 - i, summary="裁决第%02d次" % i))
        full = self.pack()
        self.assertIn("交付第29次", full)
        self.assertNotIn("交付第10次", full)   # only the last 10 deliveries
        small = self.pack(max_chars=len(full) - 300)
        self.assertLessEqual(len(small), len(full) - 300)
        self.assertIn("交付第29次", small)      # newest kept
        self.assertNotIn("交付第20次", small)    # oldest dropped first
        self.assertIn("裁决第29次", small)
        self.assertIn("## 未完成节点", small)
        tiny = self.pack(max_chars=1200)
        self.assertLessEqual(len(tiny), 1200)
        self.assertIn("## 最终目标", tiny)

    def test_trim_terminates_when_limit_unreachable(self):
        # regression: a limit below the fixed sections looped forever (11-06 on WSL)
        out = self.pack(max_chars=80)
        self.assertLessEqual(len(out), 80)
        self.assertIn("已截断", out)

    def test_node_first_with_history_and_deps(self):
        self.seed()
        t = self.pack(node="b")
        self.assertLess(t.index("## 重点节点 b"), t.index("## 最终目标"))
        sec = t[t.index("## 重点节点 b"):t.index("## 最终目标")]
        self.assertIn("br@docs/b.md", sec)
        self.assertIn("依赖 a：已交付，结果 PASS，报告 docs/a.md，head abc123", sec)
        sec_c = self.pack(node="c")
        self.assertLess(sec_c.index("node_plan"), sec_c.index("node_dispatch"))
        self.assertLess(sec_c.index("node_dispatch"), sec_c.index("node_block"))
        self.assertIn("term_w2", sec_c)

    def test_cli_exit_zero_and_unknown_line(self):
        self.seed()
        fr = FakeRunner()
        rc, res = ins.run(["--project", "t", "resume", "--line", "L", "--node", "b", "--max-chars", "5000"], runner=fr,
                          store=snap.Store("t", fr))
        self.assertEqual(rc, 0)
        self.assertIn("接续包", res["text"])
        self.assertEqual(fr.calls, [])
        rc, res = ins.run(["--project", "t", "resume", "--line", "ZZ"], runner=fr, store=snap.Store("t", fr))
        self.assertEqual(rc, 1)

    def test_followups_from_supervisor_cache(self):
        snap.save_task_cache({"supervisor:t": {"followups": {
            "fu_a": {"id": "fu_a", "line": "L", "to": "router", "text": "open one"},
            "fu_b": {"id": "fu_b", "line": "L", "to": "router", "text": "closed", "closed_at": at(1)},
            "fu_c": {"id": "fu_c", "line": "Q", "to": "router", "text": "other line"}}}})
        self.assertEqual([f["id"] for f in ins.open_followups("t", "L")], ["fu_a"])


def rlive(handle=R, screen=None, rebound_from=None, flag=None, coord=None):
    d = live()
    d["routers"]["L"] = {"handle": handle, "present": True, "last_output_at": at(1), "screen_flag": flag}
    if screen is not None:
        d["routers"]["L"]["screen"] = screen
    if rebound_from:
        d["routers"]["L"]["rebound_from"] = rebound_from
    if coord is not None:
        d["coordinators"] = {"c1": dict({"handle": C1, "present": True, "core": "c1"}, **coord)}
    return d


class PatrolBase(Base):
    def go(self, doc, minutes_later=0, apply=True, fr=None):
        core.save_live("t", doc)
        fr = fr or FakeRunner()
        out = patrol.run("t", apply=apply, runner=fr, now=NOW + dt.timedelta(minutes=minutes_later),
                         store=snap.Store("t", fr))
        return out, fr

    def resume_sends(self, fr):
        return [c for c in fr.kinds("send") if "[接续]" in " ".join(map(str, c))]

    def mem(self, out):
        return [f for f in out["findings"] if f["code"] == "memory_loss"]


class PatrolFiles(PatrolBase):
    def test_writes_pack_even_dry_run(self):
        self.add(plan_ev("L", "a", 300))
        out, fr = self.go(live(), apply=False)
        for lid in ("L", "Q"):
            p = pathlib.Path(self.tmp.name, "t", "resume", "%s.md" % lid)
            self.assertTrue(p.is_file(), lid)
        self.assertIn("`a`", pathlib.Path(self.tmp.name, "t", "resume", "L.md").read_text(encoding="utf-8"))
        self.assertEqual(fr.kinds("send"), [])
        self.assertFalse(list(pathlib.Path(self.tmp.name, "t", "resume").glob("*.tmp")))
        self.assertEqual(len(out["resume_files"]), 2)


class MemoryLoss(PatrolBase):
    def setUp(self):
        super().setUp()
        self.add(plan_ev("L", "a", 300), ev("node_dispatch", node="a", minutes_ago=290, refs={"terminal": "term_w1"}))

    def test_rebound_nudges_once(self):
        out, fr = self.go(rlive(handle="term_new-9999", rebound_from=R))
        self.assertEqual(len(self.resume_sends(fr)), 1)
        self.assertIn("progress-inspect resume --line L", " ".join(self.resume_sends(fr)[0]))
        self.assertEqual(self.mem(out)[0]["causes"], ["terminal"])
        ev_ = [e for e in core.read_events("t") if e["kind"] == "nudge" and e.get("reason") == "resume"]
        self.assertEqual(len(ev_), 1)
        out2, fr2 = self.go(rlive(handle="term_new-9999", rebound_from=R), minutes_later=10)
        self.assertEqual(self.resume_sends(fr2), [])

    def test_handle_change_between_rounds(self):
        self.go(rlive(), apply=True)
        out, fr = self.go(rlive(handle="term_other-1111"), minutes_later=10)
        self.assertEqual(len(self.resume_sends(fr)), 1)
        self.assertEqual(self.mem(out)[0]["causes"], ["terminal"])

    def test_first_round_no_false_positive(self):
        out, fr = self.go(rlive())
        self.assertEqual(self.mem(out), [])
        self.assertEqual(self.resume_sends(fr), [])

    def test_compact_text_dedupe_per_hour(self):
        self.go(rlive(screen={"compact": False}))
        out, fr = self.go(rlive(screen={"compact": True}), minutes_later=5)
        self.assertEqual(len(self.resume_sends(fr)), 1)
        self.assertEqual(self.mem(out)[0]["causes"], ["compact"])
        # still shown next round: not a new event
        out, fr = self.go(rlive(screen={"compact": True}), minutes_later=10)
        self.assertEqual(self.resume_sends(fr), [])

    def test_gauge_drop_claude_and_codex(self):
        self.go(rlive(screen={"compact": False, "ctx_used": 85}))
        out, fr = self.go(rlive(screen={"compact": False, "ctx_used": 20}), minutes_later=5)
        self.assertEqual(self.mem(out)[0]["causes"], ["gauge"])
        self.assertEqual(len(self.resume_sends(fr)), 1)
        # small change: nothing
        out, fr = self.go(rlive(screen={"compact": False, "ctx_used": 5}), minutes_later=70)
        self.assertEqual(self.mem(out), [])

    def test_no_nudge_without_apply(self):
        self.go(rlive(screen={"compact": False, "ctx_used": 85}))
        out, fr = self.go(rlive(handle="term_new-9999", rebound_from=R, screen={"compact": True}), minutes_later=5,
                          apply=False)
        self.assertEqual(fr.kinds("send"), [])
        self.assertTrue(self.mem(out))
        self.assertEqual([e for e in core.read_events("t") if e.get("reason") == "resume"], [])
        # dry run does not consume the detection: the real run still sends
        out, fr = self.go(rlive(handle="term_new-9999", rebound_from=R, screen={"compact": True}), minutes_later=6)
        self.assertEqual(len(self.resume_sends(fr)), 1)

    def test_waiting_for_input_holds_and_retries(self):
        out, fr = self.go(rlive(handle="term_new-9999", rebound_from=R, flag="waiting_input"))
        self.assertEqual(self.resume_sends(fr), [])
        self.assertTrue(self.mem(out))
        out, fr = self.go(rlive(handle="term_new-9999", rebound_from=R), minutes_later=5)
        self.assertEqual(len(self.resume_sends(fr)), 1)

    def test_coordinator_terminal_checked(self):
        self.go(rlive(coord={"screen": {"compact": False}}))
        out, fr = self.go(rlive(coord={"screen": {"compact": True}}), minutes_later=5)
        sent = self.resume_sends(fr)
        self.assertEqual(len(sent), 1)
        self.assertIn(C1, sent[0])

    def test_worker_continue_mentions_resume_for_new_terminal(self):
        scr = {"flag": "error", "transient": True, "prompt": True, "line": "stream error", "since": at(30)}
        w = [{"host": "wsl", "workspace": "x::/home/u/ws/L-a", "terminal": "term_w9", "line": "L", "node": "a", "screen": scr}]
        out, fr = self.go(dict(live(workers=w)))
        cont = [a for a in out["actions"] if a["type"] == "auto_continue" and a["role"] == "worker"]
        self.assertEqual(len(cont), 1)
        self.assertIn("progress-inspect resume --line L --node a", cont[0]["text"])
        w[0]["terminal"] = "term_w1"   # same terminal as the node's only dispatch
        out, fr = self.go(dict(live(workers=w)), minutes_later=1)
        cont = [a for a in out["actions"] if a["type"] == "auto_continue" and a["role"] == "worker"]
        self.assertEqual(cont[0]["text"], "继续")


class Signals(unittest.TestCase):
    def test_context_signals(self):
        f = snap.context_signals
        self.assertEqual(f(["  Context ███░░░░░░░ 32%"]), {"compact": False, "used": 32})
        self.assertEqual(f(["gpt-6 · 71% context left · ~/x"]), {"compact": False, "used": 29})
        self.assertTrue(f(["✻ Compacting conversation…"])["compact"])
        self.assertTrue(f(["上下文已压缩"])["compact"])
        self.assertEqual(f(["nothing here"]), {"compact": False, "used": None})


class Snippet(unittest.TestCase):
    def test_snippet_identical_and_carries_pointer(self):
        f = HERE.parent.parent / "skills" / "progress-worker" / "SPEC_SNIPPET.md"
        self.assertEqual(f.read_text(encoding="utf-8"), patrol.SPEC_SNIPPET)
        self.assertIn("progress-inspect resume --line {line} --node {node}", patrol.SPEC_SNIPPET)
        self.assertIn("progress-inspect resume --line <线>",
                      (HERE.parent.parent / "skills" / "progress-router" / "SKILL.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
