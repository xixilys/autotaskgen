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
import snapshot as snap  # noqa: E402
import supervisor as sup  # noqa: E402
import patrol  # noqa: E402
from fakes import FakeRunner  # noqa: E402
from test_patrol import project, plan_ev, live, at, NOW, R  # noqa: E402


def good(status="problem", sugg=("先读 worker 屏幕",)):
    return json.dumps({"summary": "整体在推进", "lines": [
        {"line": "L", "status": status, "progress_note": "a 节点停滞", "suggestions": list(sugg),
         "issues": [{"kind": "stalled", "evidence": "屏幕停在输入框"}] if status != "ok" else []},
        {"line": "Q", "status": "ok", "progress_note": "正常", "issues": [], "suggestions": []}]})


def stream(*cmds):
    return "\n".join(json.dumps({"type": "item.completed", "item": {"id": "i%d" % i, "type": "command_execution",
                                                                     "command": c}}) for i, c in enumerate(cmds))


class Fake:
    """model_call stand-in: records the prompt, can write inspect-log lines like the real tool would."""
    def __init__(self, msg=None, cmds=("progress-inspect tasks --run run_x",), rc=0, calls_logged=2):
        self.msg = good() if msg is None else msg
        self.cmds, self.rc, self.calls_logged = cmds, rc, calls_logged
        self.n = 0
        self.map_text = None

    def __call__(self, prompt, workdir, env, timeout=0):
        self.n += 1
        self.map_text = pathlib.Path(env["PROGRESS_SUPERVISOR_MAP"]).read_text(encoding="utf-8")
        with open(env["PROGRESS_INSPECT_LOG"], "a") as fh:
            for _ in range(self.calls_logged):
                fh.write(json.dumps({"argv": ["tasks"]}) + "\n")
        return self.rc, stream(*self.cmds), self.msg, ""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["PROGRESS_HOME"] = self.tmp.name
        os.environ["PROGRESS_CACHE_DIR"] = self.tmp.name
        os.environ.pop("PROGRESS_SUPERVISOR_CMD", None)
        core.save_project(project())
        core.append("t", plan_ev("L", "a", auto_dispatch=False))
        core.append("t", {"kind": "node_dispatch", "line": "L", "node": "a", "summary": "d", "at": at(60),
                          "actor": {"role": "router", "handle": R}, "refs": {"task": "t", "dispatch": "ctx_1", "terminal": "w"}})
        core.save_live("t", live())

    def tearDown(self):
        os.environ.pop("PROGRESS_CACHE_DIR", None)
        self.tmp.cleanup()

    def go(self, fake, minutes=0, apply=True, force=False, runner=None):
        fr = runner or FakeRunner(screen=["router line", "› "])
        out = patrol.run("t", apply=apply, runner=fr, now=NOW + dt.timedelta(minutes=minutes),
                         store=snap.Store("t", fr), supervisor=True, force_supervisor=force, model_call=fake)
        return out, fr

    def sent(self, fr):
        return [c for c in fr.kinds("send") if "[巡检员" in c[c.index("--text") + 1]]


class Pulse(Base):
    def test_timing(self):
        f = Fake()
        out, _ = self.go(f, 0)
        self.assertTrue(out["supervisor"]["ran"])
        out, _ = self.go(f, 29)
        self.assertFalse(out["supervisor"]["ran"])
        out, _ = self.go(f, 31)
        self.assertTrue(out["supervisor"]["ran"])
        self.assertEqual(f.n, 2)
        out, _ = self.go(f, 32, force=True)
        self.assertTrue(out["supervisor"]["ran"])

    def test_report_saved_and_observe_event(self):
        out, _ = self.go(Fake())
        r = core.load_supervisor("t")
        self.assertEqual(r["schema"], "progress-supervisor/v2")
        self.assertEqual(r["inspect_calls"], 2)
        self.assertEqual(r["lines"][0]["status"], "problem")
        obs = [e for e in core.read_events("t") if e["kind"] == "observe" and e["actor"]["handle"] == "supervisor@mac"]
        self.assertEqual(len(obs), 1)

    def test_map_has_graph_and_last_round_actions(self):
        core.append("t", plan_ev("L", "b", deps=["a"], auto_dispatch=False))
        core.append("t", {"kind": "nudge", "line": "L", "reason": "auto_continue", "summary": "自动继续", "at": at(1),
                          "actor": {"role": "patrol", "handle": "p"}, "refs": {"target": "w", "key": "k"}})
        f = Fake()
        self.go(f)
        m = f.map_text
        self.assertIn("最终目标", m)
        self.assertIn("M1", m)
        self.assertIn("deps=a", m)          # node b depends on a
        self.assertIn("running/run", m)      # node a
        self.assertIn("router line", m)      # router screen tail
        self.assertIn("auto_continue", m)    # last-round script action
        self.assertLessEqual(len(m), sup.MAP_MAX)

    def test_map_capped(self):
        for i in range(400):
            core.append("t", plan_ev("L", "n%d" % i, auto_dispatch=False, title="x" * 100))
        f = Fake()
        self.go(f)
        self.assertLessEqual(len(f.map_text), sup.MAP_MAX)

    def test_previous_report_in_map(self):
        f = Fake()
        self.go(f)
        self.go(f, 31)
        self.assertIn("a 节点停滞", f.map_text)


class Validation(Base):
    def test_invalid_outputs_send_nothing_and_save_nothing(self):
        bads = ["not json", json.dumps({"summary": "x"}),
                json.dumps({"summary": "x", "lines": [{"line": "ZZ", "status": "ok", "progress_note": "", "issues": [], "suggestions": []}]}),
                json.dumps({"summary": "x", "lines": [{"line": "L", "status": "bad", "progress_note": "", "issues": [], "suggestions": []}]}),
                json.dumps({"summary": "x", "lines": [{"line": "L", "status": "problem", "progress_note": "",
                                                        "issues": [{"kind": "weird", "evidence": "e"}], "suggestions": ["s"]}]})]
        for i, b in enumerate(bads):
            out, fr = self.go(Fake(msg=b), minutes=i * 40)
            self.assertFalse(out["supervisor"]["ok"], b)
            self.assertEqual(self.sent(fr), [])
        self.assertIsNone(core.load_supervisor("t"))

    def test_model_failure(self):
        out, fr = self.go(Fake(msg="", rc=1))
        self.assertFalse(out["supervisor"]["ok"])
        self.assertEqual(self.sent(fr), [])

    def test_suggestions_cut_to_three(self):
        out, _ = self.go(Fake(msg=good(sugg=("a", "b", "c", "d"))))
        self.assertEqual(core.load_supervisor("t")["lines"][0]["suggestions"], ["a", "b", "c"])

    def test_schema_file_matches(self):
        self.assertEqual(sup.REPORT_SCHEMA["required"], list(sup.REPORT_SCHEMA["properties"]))  # strict mode: all required
        self.assertEqual(sup.REPORT_SCHEMA["properties"]["lines"]["items"]["properties"]["status"]["enum"],
                         ["ok", "attention", "problem"])


class Delivery(Base):
    def test_dry_run_sends_nothing_but_saves_report(self):
        out, fr = self.go(Fake(), apply=False)
        self.assertEqual(self.sent(fr), [])
        self.assertEqual(core.load_supervisor("t")["delivery"][0]["reason"], "dry_run")

    def test_apply_sends_once_per_router_and_ok_lines_skipped(self):
        out, fr = self.go(Fake())
        s = self.sent(fr)
        self.assertEqual(len(s), 1)
        self.assertEqual(s[0][s[0].index("--terminal") + 1], R)
        self.assertIn("先读 worker 屏幕", s[0][s[0].index("--text") + 1])
        self.assertEqual(core.load_supervisor("t")["delivery"][0]["reason"], "sent")
        self.assertTrue([e for e in core.read_events("t") if e.get("reason") == "supervisor"])

    def test_dedupe_unchanged_and_hourly_limit(self):
        self.go(Fake())
        out, fr = self.go(Fake(), minutes=31)  # same suggestions
        self.assertEqual(self.sent(fr), [])
        self.assertEqual(core.load_supervisor("t")["delivery"][0]["reason"], "unchanged")
        out, fr = self.go(Fake(msg=good(sugg=("换个做法",))), minutes=35, force=True)  # changed, < 1 h
        self.assertEqual(self.sent(fr), [])
        self.assertEqual(core.load_supervisor("t")["delivery"][0]["reason"], "rate_limited")
        out, fr = self.go(Fake(msg=good(sugg=("换个做法",))), minutes=70)  # changed, > 1 h
        self.assertEqual(len(self.sent(fr)), 1)

    def test_router_absent_not_sent(self):
        core.save_live("t", live(present=False))
        out, fr = self.go(Fake(), runner=FakeRunner())
        self.assertEqual(self.sent(fr), [])


class Audit(Base):
    def test_inspect_only_is_clean(self):
        out, fr = self.go(Fake(cmds=("progress-inspect screen term_x", "/bin/zsh -lc 'progress-inspect events --line L'",
                                     "cat /tmp/x | head -5")))
        self.assertNotIn("policy_violation", core.load_supervisor("t"))
        self.assertEqual(len(self.sent(fr)), 1)

    def test_write_like_commands_flagged_and_nothing_sent(self):
        for i, c in enumerate(("git commit -am x", "git -C /w push origin x", "rm -rf /tmp/a", "echo hi > out.txt",
                               "sed -i s/a/b/ f", "orca terminal send --terminal t --text hi", "orca orchestration worker-start x",
                               "cat a | tee b", "mv a b", "git reset --hard", "git checkout main")):
            self.setUp() if i else None
            out, fr = self.go(Fake(cmds=("progress-inspect tasks --run run_x", c)))
            r = core.load_supervisor("t")
            self.assertTrue(r.get("policy_violation"), c)
            self.assertEqual(r["policy_violation"][0]["command"], c)
            self.assertEqual(self.sent(fr), [], c)  # report kept, no messages
            self.assertEqual(r["delivery"][0]["reason"], "policy_violation")
            self.tearDown()
            self.setUp()

    def test_redirect_to_devnull_and_stderr_merge_ok(self):
        self.assertEqual(sup.audit(["progress-inspect screen term_x 2>&1", "ls x 2>/dev/null", "git log -n 3",
                                    "git -C /w show HEAD", "git diff HEAD~1"]), [])


class RealRunModel(Base):
    def test_custom_cmd_env_hook(self):
        script = pathlib.Path(self.tmp.name, "fake.sh")
        script.write_text('#!/bin/sh\ncat >/dev/null\ncat > "$PROGRESS_SUPERVISOR_OUT" <<EOF\n%s\nEOF\n'
                          'echo \'{"item":{"id":"1","command":"progress-inspect tasks --run run_x"}}\'\n' % good("ok", ()))
        script.chmod(0o755)
        os.environ["PROGRESS_SUPERVISOR_CMD"] = str(script)
        try:
            out, fr = self.go(None)
        finally:
            os.environ.pop("PROGRESS_SUPERVISOR_CMD")
        self.assertTrue(out["supervisor"]["ok"])
        self.assertEqual(core.load_supervisor("t")["commands"], 1)

    def test_default_command_shape(self):
        argv = sup.default_cmd("/s.json", "/o.txt")
        self.assertEqual(argv[:3], ["codex", "exec", "-m"])
        self.assertIn("gpt-6.1-terra", argv)
        self.assertIn('model_reasoning_effort="high"', argv)
        self.assertIn("--output-schema", argv)
        self.assertIn("--json", argv)
        self.assertEqual(sup.TIMEOUT_S, 600)


def _ms_project(state_file=None):
    p = project()
    p["final_goal"] = {"text": "目标G", "done_when": "由两位核心协调人在关键节点里写明"}
    p["milestones"] = [{"id": "B4", "title": "b4", "core": "c1", "lines": ["L"], "after": []},
                       {"id": "B5", "title": "b5", "core": "c1", "lines": ["L"], "after": ["B4"]},
                       {"id": "F2", "title": "f2", "core": "c1", "lines": ["Q"], "after": []},
                       {"id": "F5", "title": "f5", "core": "c1", "lines": ["Q"], "after": ["F2"]}]
    if state_file:
        p["lines"][0]["state_file"] = state_file
    return p


class MapSections(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["PROGRESS_HOME"] = self.tmp.name
        os.environ["PROGRESS_CACHE_DIR"] = self.tmp.name
        self.evs = [
            {"kind": "milestone_confirm", "milestone": "B4", "line": "L", "at": at(500), "actor": {"role": "coordinator", "handle": "c"}, "summary": "ok"},
            {"kind": "milestone_confirm", "milestone": "F2", "line": "Q", "at": at(500), "actor": {"role": "coordinator", "handle": "c"}, "summary": "ok"},
            plan_ev("L", "a", milestones=["B5"], auto_dispatch=False),
            plan_ev("L", "free", auto_dispatch=False),
            plan_ev("Q", "w", milestones=["F5"], auto_dispatch=False),
        ]

    def tearDown(self):
        os.environ.pop("PROGRESS_CACHE_DIR", None)
        self.tmp.cleanup()

    def fold(self, evs=None, proj=None):
        proj = proj or _ms_project()
        return proj, core.fold(proj, evs or self.evs, at=snap.iso(NOW))

    def test_frontier(self):
        proj, st = self.fold()
        txt = "\n".join(sup.milestone_frontier(proj, st))
        self.assertIn("B5 [open] b5 | 前置:B4 | 线:L | 前沿", txt)
        self.assertIn("F5 [open] f5 | 前置:F2 | 线:Q | 前沿", txt)
        self.assertNotIn("B4 [confirmed] b4 | 前置:- | 线:L | 前沿", txt)
        self.assertIn("未挂里程碑的活跃节点", txt)
        self.assertIn("L/free", txt)
        self.assertIn("⚠ 完成标准未写成可核对的条件", txt)

    def test_frontier_done_when_list(self):
        proj, st = self.fold()
        proj["final_goal"]["done_when"] = ["D1 真实评估器", "D2 又快又准"]
        txt = "\n".join(sup.milestone_frontier(proj, st))
        self.assertIn("完成标准：D1 真实评估器\nD2 又快又准", txt)
        self.assertNotIn("['D1", txt)
        self.assertNotIn("⚠ 完成标准未写成可核对的条件", txt)

    def test_blocker_table_mislabeled_and_dispatch_failed(self):
        evs = self.evs + [
            {"kind": "node_block", "line": "Q", "node": "w", "blocker": "dep", "at": at(300), "actor": {"role": "router", "handle": R},
             "summary": "等真实端点和调用授权"},
            {"kind": "auto_dispatch", "line": "L", "node": "free", "at": at(60), "actor": {"role": "patrol", "handle": "p"}, "summary": "巡检代派 free"},
            {"kind": "node_block", "line": "L", "node": "free", "blocker": "env", "at": at(59), "actor": {"role": "patrol", "handle": "p"},
             "summary": "代派失败：worker-start 失败 rc=1"},
        ]
        proj, st = self.fold(evs)
        txt = "\n".join(sup.blocker_table(proj, st, NOW))
        self.assertIn("Q/w → F5 | blocker=dep", txt)
        self.assertIn("推断：等人", txt)
        self.assertIn("⚠ blocker 可能标错", txt)
        self.assertIn("5.0h", txt)
        self.assertIn("⚠ 派发失败", txt)

    def test_later_human_step_is_not_a_human_wait(self):
        evs = self.evs + [{"kind": "node_block", "line": "Q", "node": "w", "blocker": "dep", "at": at(300),
                           "actor": {"role": "router", "handle": R},
                           "summary": "按交接先等B8固定后端和真实回复；B8交付后核心二请求人类调用授权再转human"}]
        proj, st = self.fold(evs)
        txt = "\n".join(sup.blocker_table(proj, st, NOW))
        self.assertIn("Q/w → F5 | blocker=dep", txt)
        self.assertNotIn("等人", txt)
        self.assertNotIn("标错", txt)

    def test_dep_wait_that_also_waits_on_the_human_is_both_not_mislabeled(self):
        evs = self.evs + [{"kind": "node_block", "line": "Q", "node": "w", "blocker": "dep", "at": at(300),
                           "actor": {"role": "router", "handle": R},
                           "summary": "B13a交接(EVB经核二)和预算guard待交；人类预算授权(经核二)仍未批；dep/no-auto，零调用"}]
        proj, st = self.fold(evs)
        txt = "\n".join(sup.blocker_table(proj, st, NOW))
        self.assertIn("并存：等依赖，文中也提到人类/授权", txt)
        self.assertNotIn("推断：等人", txt)
        self.assertNotIn("可能标错", txt)
        rows, flagged = sup.hard_metrics(proj, st, evs, {"metrics": {}}, NOW)
        self.assertNotIn("Q:等人", flagged)

    def test_budget_wait_is_not_counted_as_waiting_on_the_human(self):
        evs = self.evs + [{"kind": "node_block", "line": "Q", "node": "w", "blocker": "dep", "at": at(300),
                           "actor": {"role": "router", "handle": R},
                           "summary": "撤回虚假批准，授权未到；B13a端点/固定版本亦待EVB经核二交，保留no-auto零调用"}]
        proj, st = self.fold(evs)
        txt = "\n".join(sup.blocker_table(proj, st, NOW))
        self.assertIn("并存：等依赖", txt)                     # "亦待EVB经核二交" is a handoff wait
        self.assertIn("等预算/调用额度裁定（按 blocker=dep 计", txt)
        self.assertNotIn("推断：等人", txt)
        self.assertNotIn("可能标错", txt)
        rows, flagged = sup.hard_metrics(proj, st, evs, {"metrics": {}}, NOW)
        self.assertNotIn("Q:等人", flagged)
        evs[-1] = dict(evs[-1], blocker="design", summary="等核心一批准追加调用预算")
        txt = "\n".join(sup.blocker_table(*self.fold(evs), NOW))
        self.assertIn("等预算/调用额度裁定（按 blocker=design 计", txt)
        self.assertNotIn("可能标错", txt)
        evs[-1] = dict(evs[-1], blocker="dep", summary="等人类给真实端点")    # a real human ask is still inferred
        self.assertIn("推断：等人", "\n".join(sup.blocker_table(*self.fold(evs), NOW)))

    def test_blocker_human_not_mislabeled(self):
        evs = self.evs + [{"kind": "node_block", "line": "Q", "node": "w", "blocker": "human", "at": at(300),
                           "actor": {"role": "router", "handle": R}, "summary": "等授权"}]
        proj, st = self.fold(evs)
        txt = "\n".join(sup.blocker_table(proj, st, NOW))
        self.assertIn("等人（blocker=human）", txt)
        self.assertNotIn("标错", txt)

    def test_hard_metrics_flags_and_wait_not_stall(self):
        evs = self.evs + [{"kind": "node_block", "line": "Q", "node": "w", "blocker": "human", "at": at(300),
                           "actor": {"role": "router", "handle": R}, "summary": "等授权"}]
        proj, st = self.fold(evs)
        rows, flagged = sup.hard_metrics(proj, st, evs, {"metrics": {"Q": {"critical_path": {"nodes": ["w"], "stall_min": 490},
                                                                         "review_run": {"ratio": 0.355}}}}, NOW)
        q = next(r for r in rows if r.startswith("- Q"))
        self.assertIn("全是等人", q)
        self.assertIn("⚠等人 5.0h", q)
        self.assertIn("⚠审查/执行 0.355（阈 0.2/0.35）", q)
        self.assertIn("Q:等人", flagged)
        self.assertNotIn("Q:关键路径停滞", flagged)

    def test_thresholds_override(self):
        proj = _ms_project()
        proj["defaults"] = {"supervisor_thresholds": {"wait_human_h": [1, 2]}}
        self.assertEqual(sup.thresholds(proj)["wait_human_h"], (1, 2))

    def test_state_digest_prefers_tail(self):
        text = ("# 标题\n\n首段说明。\n\n## 目标\n做X\n\n## 当前\n旧的当前内容\n\n"
                + "## 杂项\n" + "填充\n" * 400 + "\n## 当前立即执行（覆盖上方）\n新的立即执行内容\n")
        d = sup.digest_state(text)
        self.assertIn("新的立即执行内容", d)
        self.assertIn("首段说明", d)
        self.assertLessEqual(len(d), 1800)
        self.assertIn("要交付X", sup.digest_brief("# B\n\n## 要交付\n要交付X\n"))

    def test_line_docs_local_cache_and_missing(self):
        d = pathlib.Path(self.tmp.name, "wt")
        d.mkdir()
        (d / "STATE.md").write_text("# S\n\n首段\n\n## 当前\nnow-text\n", encoding="utf-8")
        (d / "BRIEF.md").write_text("# B\n\n## 目标\nbrief-goal\n", encoding="utf-8")
        proj = _ms_project(state_file="STATE.md")
        proj["repo_path"] = str(d)
        cache = {}
        r = sup.line_docs(proj, proj["lines"][0], FakeRunner(), cache)
        self.assertIn("now-text", r["state"]["digest"])
        self.assertIn("brief-goal", r["brief"]["digest"])
        self.assertEqual(len(r["state"]["sha"]), 8)
        self.assertIn(str(d / "STATE.md"), cache["docs"])
        cache["docs"][str(d / "STATE.md")]["digest"] = "CACHED"
        r = sup.line_docs(proj, proj["lines"][0], FakeRunner(), cache)
        self.assertEqual(r["state"]["digest"], "CACHED")
        (d / "STATE.md").unlink()
        r = sup.line_docs(proj, proj["lines"][0], FakeRunner(), cache)
        self.assertIn("读不到", r["state"]["error"])

    def test_build_map_sections_and_no_cut_of_first_four(self):
        proj = _ms_project()
        evs = list(self.evs)
        for i in range(300):
            evs.append(plan_ev("L", "n%d" % i, auto_dispatch=False, title="x" * 100))
        text = sup.build_map(proj, evs, None, NOW, FakeRunner(screen=["a"] * 5), {}, [], None, max_chars=20000)
        heads = [text.index(h) for h in ("## 1 最终目标", "## 2 卡点", "## 3 硬指标", "## 4 待回查建议", "## 5 各线背景", "## 6 主机资源", "## 7 各条线")]
        self.assertEqual(heads, sorted(heads))
        self.assertLessEqual(len(text), 20000)
        self.assertEqual(sup.MAP_MAX, 60000)

    def test_report_has_map_chars(self):
        core.save_project(project())
        core.append("t", plan_ev("L", "a", auto_dispatch=False))
        core.save_live("t", live())
        fr = FakeRunner(screen=["x"])
        out = patrol.run("t", apply=False, runner=fr, now=NOW, store=snap.Store("t", fr), supervisor=True,
                         force_supervisor=True, model_call=Fake())
        self.assertGreater(core.load_supervisor("t")["map_chars"], 100)



# ---------------------------------------------------------------- v2 report + follow-up ledger (spec §B, §C)

def v2(status="problem", actions=None, followups=(), findings=(), line="L"):
    acts = [{"to": "router", "text": "确认 a 节点恢复", "done_when": "a 出现 node_heartbeat", "check_after_min": 30,
             "verify": {"line": None, "node": None, "event_kind": None}}] if actions is None else actions
    return json.dumps({
        "summary": "s", "critical_path": "cp", "blockers": [{"item": "a", "line": "L", "class": "human", "since_hours": 2.5,
                                                             "who_unblocks": "人类", "evidence": "e"}],
        "lines": [{"line": line, "status": status, "progress_note": "n", "milestone_position": "M1 第二步",
                   "issues": [{"kind": "waiting_human", "evidence": "e"}] if status != "ok" else [], "actions": acts},
                  {"line": "Q", "status": "ok", "progress_note": "ok", "milestone_position": "", "issues": [], "actions": []}],
        "followups": list(followups), "metric_findings": list(findings), "goal_drafts": []})


def act(to="router", text="确认 a 节点恢复", **kw):
    a = {"to": to, "text": text, "done_when": "a 出现 node_heartbeat", "check_after_min": 30,
         "verify": {"line": None, "node": None, "event_kind": None}}
    a.update(kw)
    return a


FID = sup.fu_id("L", "router", "确认 a 节点恢复")
C1H = "term_coord1-bbbb"


class ReportV2(Base):
    def test_v2_passes_and_suggestions_derived(self):
        doc = json.loads(v2(actions=[act(text="x%d" % i) for i in range(5)]))
        clean, why = sup.validate_report(doc, ["L", "Q"])
        self.assertEqual(why, "")
        self.assertEqual(clean["lines"][0]["suggestions"], ["x0", "x1", "x2"])
        self.assertEqual(len(clean["lines"][0]["actions"]), 3)
        self.assertEqual(clean["lines"][0]["milestone_position"], "M1 第二步")
        self.assertTrue(any("超过 3 条" in w for w in clean["warnings"]))
        self.assertEqual(clean["blockers"][0]["class"], "human")

    def test_check_after_clamped_and_bad_to_rejected(self):
        doc = json.loads(v2(actions=[act(check_after_min=5), act(text="b", check_after_min=9999), act(text="c", check_after_min="x")]))
        clean, _ = sup.validate_report(doc, ["L", "Q"])
        self.assertEqual([a["check_after_min"] for a in clean["lines"][0]["actions"]], [30, 360, 60])
        clean, why = sup.validate_report(json.loads(v2(actions=[act(to="boss")])), ["L", "Q"])
        self.assertIsNone(clean)
        self.assertIn("to", why)

    def test_unknown_followup_id_dropped_with_warning(self):
        doc = json.loads(v2(followups=[{"id": "fu_known000", "verdict": "done", "evidence": "e"},
                                       {"id": "fu_bogus000", "verdict": "done", "evidence": "e"}]))
        clean, _ = sup.validate_report(doc, ["L", "Q"], ["fu_known000"])
        self.assertEqual([f["id"] for f in clean["followups"]], ["fu_known000"])
        self.assertTrue(any("fu_bogus000" in w for w in clean["warnings"]))

    def test_missing_metric_verdicts(self):
        doc = json.loads(v2(findings=[{"line": "L", "metric": "审查/执行", "value": "0.3", "verdict": "real", "evidence": "e"}]))
        clean, _ = sup.validate_report(doc, ["L", "Q"], [], ["L:审查/执行", "L:等人", "Q:打回"])
        self.assertEqual(clean["missing_metric_verdicts"], ["L:等人", "Q:打回"])

    def test_other_majority_warns_but_passes(self):
        doc = json.loads(v2())
        doc["lines"][0]["issues"] = [{"kind": "other", "evidence": "a"}, {"kind": "other", "evidence": "b"}, {"kind": "stalled", "evidence": "c"}]
        clean, _ = sup.validate_report(doc, ["L", "Q"])
        self.assertTrue(any("other" in w for w in clean["warnings"]))

    def test_v1_doc_is_migrated_not_crashing(self):
        clean, why = sup.validate_report(json.loads(good()), ["L", "Q"])
        self.assertEqual(why, "")
        self.assertTrue(clean["migrated_from_v1"])
        a = clean["lines"][0]["actions"][0]
        self.assertEqual((a["to"], a["text"], a["check_after_min"]), ("router", "先读 worker 屏幕", 60))
        self.assertEqual(clean["lines"][0]["suggestions"], ["先读 worker 屏幕"])
        clean, why = sup.validate_report({"summary": "x", "lines": [{"line": "L", "status": "ok", "progress_note": "", "issues": []}]}, ["L"])
        self.assertIsNone(clean)  # neither actions nor suggestions: clear error, no exception
        self.assertIn("actions", why)

    def test_pulse_with_v1_model_and_old_saved_v1_report(self):
        # a v1 report saved earlier is only read back as "previous report" in the map: summary + lines
        store = snap.Store("t", FakeRunner())
        store.put_supervisor({"schema": "progress-supervisor/v1", "summary": "旧", "generated_at": at(100),
                              "lines": [{"line": "L", "status": "attention", "progress_note": "旧报告", "issues": [], "suggestions": ["x"]}]})
        f = Fake()  # v1 output
        out, fr = self.go(f, force=True)
        self.assertIn("旧报告", f.map_text)
        self.assertTrue(out["supervisor"]["ok"])
        self.assertEqual(core.load_supervisor("t")["schema"], "progress-supervisor/v2")
        self.assertEqual(len(self.sent(fr)), 1)

    def test_pulse_v2_end_to_end_and_schema_strict(self):
        out, fr = self.go(Fake(msg=v2()))
        r = core.load_supervisor("t")
        self.assertEqual(r["lines"][0]["suggestions"], ["确认 a 节点恢复"])
        self.assertIn("落实标准：a 出现 node_heartbeat", self.sent(fr)[0][self.sent(fr)[0].index("--text") + 1])

        def walk(sc):
            if sc.get("type") == "object":
                self.assertEqual(sc["required"], list(sc["properties"]))
                self.assertFalse(sc["additionalProperties"])
                for v in sc["properties"].values():
                    walk(v)
            elif sc.get("type") == "array":
                walk(sc["items"])
        walk(sup.REPORT_SCHEMA)
        self.assertIn("{map_text}", sup.PROMPT)
        sup.PROMPT.format(map="m", map_text="t")  # no stray braces


class Ledger(Base):
    def setUp(self):
        super().setUp()
        lv = live()
        lv["coordinators"] = {"c1": {"handle": C1H, "present": True}}
        core.save_live("t", lv)

    def led(self):
        return sup.load_state("t").get("followups") or {}

    def test_registered_once_and_not_resent(self):
        out, fr = self.go(Fake(msg=v2()))
        self.assertEqual(len(self.sent(fr)), 1)
        e = self.led()[FID]
        self.assertEqual((e["to"], e["target"], e["tries"]), ("router", R, 0))
        self.assertEqual(e["check_at"], snap.iso(NOW + dt.timedelta(minutes=30)))
        # same advice reworded in whitespace/case: same id, not new, not sent again
        out, fr = self.go(Fake(msg=v2(actions=[act(text="确认  a 节点恢复")])), minutes=95, force=True)
        self.assertEqual(self.sent(fr), [])
        self.assertEqual(len(self.led()), 1)
        self.assertEqual(core.load_supervisor("t")["delivery"][0]["reason"], "unchanged")

    def test_not_sent_means_not_registered(self):
        self.go(Fake(msg=v2()), apply=False)
        self.assertEqual(self.led(), {})
        core.save_live("t", dict(live(), routers={"L": {"handle": R, "present": True}}))
        self.go(Fake(msg=v2()), minutes=40)  # sent now
        self.go(Fake(msg=v2(actions=[act(text="另一条")])), minutes=45, force=True)  # rate limited 1 h
        self.assertEqual(len(self.led()), 1)

    def test_auto_check_closes_on_verify_event(self):
        a = act(verify={"line": "L", "node": "a", "event_kind": "node_heartbeat"})
        self.go(Fake(msg=v2(actions=[a])))
        core.append("t", {"kind": "node_heartbeat", "line": "L", "node": "a", "summary": "hb", "at": snap.iso(NOW + dt.timedelta(minutes=10)),
                          "actor": {"role": "worker", "handle": "w"}, "refs": {}})
        f = Fake(msg=v2(status="ok", actions=[]))
        self.go(f, minutes=31)
        e = self.led()[FID]
        self.assertEqual(e["closed_how"], "auto_done")
        self.assertNotIn("第 1 次回查", f.map_text.split("## 4 待回查建议")[1].split("## 5")[0])
        self.assertIn("已自动确认", f.map_text)

    def test_expires_after_24h(self):
        self.go(Fake(msg=v2()))
        f = Fake(msg=v2(status="ok", actions=[]))
        self.go(f, minutes=25 * 60)
        self.assertEqual(self.led()[FID]["closed_how"], "expired")
        self.assertIn("已过期关闭", f.map_text)

    def test_done_closes(self):
        self.go(Fake(msg=v2()))
        self.go(Fake(msg=v2(status="ok", actions=[], followups=[{"id": FID, "verdict": "done", "evidence": "hb 在 12:30"}])), minutes=61)
        self.assertEqual(self.led()[FID]["closed_how"], "done")

    def test_not_done_resend_then_escalate_then_human_only(self):
        nd = [{"id": FID, "verdict": "not_done", "evidence": "屏幕无动静"}]
        self.go(Fake(msg=v2()))
        # round 2: 1st not_done -> resend to the router (after the 1 h gap)
        out, fr = self.go(Fake(msg=v2(actions=[], followups=nd)), minutes=61)
        s = self.sent(fr)
        self.assertEqual(len(s), 1)
        self.assertEqual(s[0][s[0].index("--terminal") + 1], R)
        self.assertIn("[巡检员·回查]", s[0][s[0].index("--text") + 1])
        self.assertEqual(self.led()[FID]["tries"], 1)
        # round 3: 2nd not_done -> coordinator
        out, fr = self.go(Fake(msg=v2(actions=[], followups=nd)), minutes=92)
        s = self.sent(fr)
        self.assertEqual(len(s), 1)
        self.assertEqual(s[0][s[0].index("--terminal") + 1], C1H)
        txt = s[0][s[0].index("--text") + 1]
        self.assertIn("[巡检员·升级]", txt)
        self.assertIn("a 出现 node_heartbeat", txt)
        self.assertEqual(self.led()[FID]["tries"], 2)
        reasons = [e.get("reason") for e in core.read_events("t") if e["kind"] == "nudge"]
        self.assertIn("supervisor_followup", reasons)
        self.assertIn("supervisor_escalate", reasons)
        # round 4: 3rd not_done -> nothing sent, marked for the human digest
        out, fr = self.go(Fake(msg=v2(actions=[], followups=nd)), minutes=123)
        self.assertEqual(self.sent(fr), [])
        r = core.load_supervisor("t")
        self.assertEqual(self.led()[FID]["tries"], 3)
        self.assertEqual([h["id"] for h in r["human_pending"]], [FID])

    def test_cannot_tell_twice_counts_as_not_done(self):
        ct = [{"id": FID, "verdict": "cannot_tell", "evidence": "?"}]
        self.go(Fake(msg=v2()))
        self.go(Fake(msg=v2(actions=[], followups=ct)), minutes=61)
        e = self.led()[FID]
        self.assertEqual((e["cannot_tell"], e["tries"]), (1, 0))
        out, fr = self.go(Fake(msg=v2(actions=[], followups=ct)), minutes=92)
        self.assertEqual(len(self.sent(fr)), 1)  # treated as not_done #1: resend
        self.assertEqual(self.led()[FID]["tries"], 1)

    def test_escalation_without_coordinator_goes_to_human(self):
        core.save_live("t", live())  # no coordinators
        nd = [{"id": FID, "verdict": "not_done", "evidence": "x"}]
        self.go(Fake(msg=v2()))
        self.go(Fake(msg=v2(actions=[], followups=nd)), minutes=61)
        out, fr = self.go(Fake(msg=v2(actions=[], followups=nd)), minutes=92)
        self.assertEqual(self.sent(fr), [])
        self.assertTrue(self.led()[FID]["human"])

    def test_coordinator_and_tooling_and_human_actions(self):
        os.environ.pop("PROGRESS_TOOLING_HANDLE", None)
        acts = [act("coordinator", "定 B5 方法"), act("tooling", "派发脚本坏了"), act("human", "给真实端点授权")]
        out, fr = self.go(Fake(msg=v2(actions=acts)))
        s = self.sent(fr)
        self.assertEqual([c[c.index("--terminal") + 1] for c in s], [C1H])
        led = self.led()
        self.assertEqual(led[sup.fu_id("L", "coordinator", "定 B5 方法")]["target"], C1H)
        self.assertEqual(led[sup.fu_id("L", "human", "给真实端点授权")]["target"], "human")
        self.assertEqual(led[sup.fu_id("L", "tooling", "派发脚本坏了")]["target"], "observe")
        self.assertTrue([e for e in core.read_events("t") if e["kind"] == "observe" and "tooling" in e["summary"]])
        # a human action is never sent to any router
        self.assertFalse([c for c in s if "真实端点" in c[c.index("--text") + 1]])
        # 2 h cap per coordinator
        out, fr = self.go(Fake(msg=v2(actions=[act("coordinator", "另一件")])), minutes=61)
        self.assertEqual(self.sent(fr), [])
        self.assertEqual(core.load_supervisor("t")["delivery"][0]["reason"], "rate_limited")

    def test_tooling_handle_env(self):
        os.environ["PROGRESS_TOOLING_HANDLE"] = "term_tool-1"
        try:
            out, fr = self.go(Fake(msg=v2(actions=[act("tooling", "指标算错")])))
        finally:
            os.environ.pop("PROGRESS_TOOLING_HANDLE")
        self.assertEqual([c[c.index("--terminal") + 1] for c in self.sent(fr)], ["term_tool-1"])

    def test_pending_list_shown_in_map_and_violation_suppresses_all(self):
        self.go(Fake(msg=v2()))
        f = Fake(msg=v2(status="ok", actions=[]))
        self.go(f, minutes=61)
        sec4 = f.map_text.split("## 4 待回查建议")[1].split("## 5")[0]
        self.assertIn(FID, sec4)
        self.assertIn("第 1 次回查", sec4)
        self.assertIn("确认 a 节点恢复", sec4)
        # violation: verdicts and escalations are not executed, nothing is sent
        nd = [{"id": FID, "verdict": "not_done", "evidence": "x"}]
        out, fr = self.go(Fake(msg=v2(actions=[], followups=nd), cmds=("rm -rf /tmp/a",)), minutes=130)
        self.assertEqual(self.sent(fr), [])
        self.assertEqual(self.led()[FID]["tries"], 0)

    def test_new_action_during_violation_not_registered(self):
        out, fr = self.go(Fake(msg=v2(), cmds=("git push",)))
        self.assertEqual(self.sent(fr), [])
        self.assertEqual(self.led(), {})


if __name__ == "__main__":
    unittest.main()
