import contextlib
import datetime as dt
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import progress_core as core  # noqa: E402
import project_review as pr  # noqa: E402
import snapshot as snap  # noqa: E402

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 5, 18, 35, 0, tzinfo=UTC)   # the 11-05 snapshot the spec was written from
C1, C2 = "term_coord1-aaaa", "term_coord2-bbbb"
R = {"EVB": "term_r_evb", "FE": "term_r_fe", "SR": "term_r_sr", "NEWP": "term_r_newp"}


def ago(hours):
    return snap.iso(NOW - dt.timedelta(hours=hours))


def milestone(mid, title, done_when, core_id, lines, after=(), **kw):
    m = {"id": mid, "title": title, "done_when": done_when, "core": core_id, "lines": list(lines), "after": list(after)}
    m.update(kw)
    return m


def make_project(stages=False, extra_defaults=None):
    ms = [
        milestone("B4", "CG 端到端第一个真实结果", "真实 CG 跑完；数值与参考一致", "core1", ["EVB"], [],
                  criteria=[{"id": "B4.1", "text": "CG 跑完", "status": "met", "evidence": "report", "closed_at": ago(30)}]),
        milestone("B5", "MD 和 Qwen 端到端", "MD 完整跑通；Qwen 两层跑通", "core1", ["EVB"], ["B4"]),
        milestone("B6", "单次评估 20 分钟以内", "开发集全部负载各一次完整评估；耗时 20 分钟以内。结果写明分母", "core1", ["EVB"], ["B5"]),
        milestone("B7", "精度达标：周期误差中位 10% 以内", "以参照为真值，周期误差中位 ≤10%；排序一致", "core1", ["EVB"], ["B5"]),
        milestone("B8", "公开接口 v1 上线", "真实后端挂在公共入口后面；CG、MD 各有一次前端真实调用通过 validate", "core1", ["EVB"],
                  ["B4", "B6", "B7"]),
        milestone("B9", "快速评估方法达标", "快速方法在开发集上满足 B7 的准；参照为 RTL 实测", "core1", ["NEWP", "EVB"], ["B5"]),
        milestone("F3", "结构选择手册有样本证据", "证据逐条绑定样本", "core2", ["SR"], []),
        milestone("F5", "接上真实评估器", "整应用请求经 B8 公开接口返回", "core2", ["FE"], ["B8"]),
        milestone("F6", "受控对比实验", "三类模式跑完", "core2", ["SR"], ["F3", "F5"]),
    ]
    if stages:
        for m in ms:
            m["stage"] = "S1" if m["id"].startswith("B") else "S2"
    p = {"schema": core.SCHEMA_PROJECT, "id": "dse", "name": "dse", "final_goal": {"text": "一个新负载自动出好设计", "done_when": "D1 到 D4"},
         "humans": ["term_human"], "cores": [{"id": "core1", "name": "c1", "coordinator": C1}, {"id": "core2", "name": "c2", "coordinator": C2}],
         "lines": [{"id": "EVB", "name": "evb", "core": "core1", "router": R["EVB"]},
                   {"id": "FE", "name": "fe", "core": "core2", "router": R["FE"]},
                   {"id": "SR", "name": "sr", "core": "core2", "router": R["SR"]},
                   {"id": "NEWP", "name": "newp", "core": "core1", "router": R["NEWP"]}],
         "milestones": ms}
    if extra_defaults:
        p["defaults"] = {"project_review": extra_defaults}
    return p


class Ev:
    n = 0

    def __init__(self):
        self.events = []

    def add(self, kind, line, node=None, at=None, role="router", **kw):
        Ev.n += 1
        e = {"id": "e%d" % Ev.n, "project": "dse", "kind": kind, "line": line, "at": at, "summary": kw.pop("summary", kind),
             "actor": {"role": role, "handle": R.get(line, "x")}}
        if node:
            e["node"] = node
        e.update(kw)
        self.events.append(e)

    def node(self, line, nid, start_h, hours, milestones=(), kind="run", outcome="PASS", running=False, title=None):
        """Plan at start-0.1h, dispatch at start_h hours ago, deliver `hours` later (or still running)."""
        self.add("node_plan", line, nid, ago(start_h + 0.1), title=title or nid, node_kind=kind, milestones=list(milestones), deps=[])
        self.add("node_dispatch", line, nid, ago(start_h), refs={"terminal": "w_" + nid})
        if not running:
            self.add("node_deliver", line, nid, ago(start_h - hours), role="worker", outcome=outcome)


def fixture():
    """Shaped like 11-05: EVB busy with heavy review/repair, FE and SR nearly idle, B8 gated by B6/B7 while PASS arrives."""
    ev = Ev()
    ev.add("milestone_confirm", "EVB", at=ago(30), role="coordinator", milestone="B4")
    # EVB: 6 h new work, 3 h review, 2 h repair inside 72 h; B8 related PASS in the last 48 h
    ev.node("EVB", "evb-cg-run-1105", 40, 3, ["B5"])
    ev.node("EVB", "evb-md-run-1105", 36, 3, ["B5"])
    ev.node("EVB", "evb-cg-review-1105", 30, 3, ["B5"], kind="review")
    ev.node("EVB", "evb-md-repair-1105", 20, 2, ["B5"])
    ev.node("EVB", "evb-b8-public-api-1105", 10, 1, ["B8"])
    # NEWP: one running node and 3 PARTIAL deliveries
    ev.node("NEWP", "newp-fast-1", 5, 1, ["B9"], outcome="PARTIAL")
    ev.node("NEWP", "newp-fast-2", 4, 1, ["B9"], outcome="PARTIAL")
    ev.node("NEWP", "newp-fast-3", 3, 1, ["B9"], outcome="PARTIAL")
    ev.node("NEWP", "newp-fast-4", 2, 1, ["B9"], running=True)
    # FE and SR: a single short node each, in 24 h => zero-concurrency share far above 85%
    ev.node("FE", "fe-loop-1105", 12, 1, ["F5"])
    ev.node("SR", "sr-cover-1105", 12, 0.5, ["F3"])
    ev.add("line_close", "FE", at=ago(6), role="coordinator", summary="暂停")
    return ev.events


LIVE = {"resources": {"wsl": {"mem_avail_gb": 40.9, "disks": []}, "mac": {"mem_avail_gb": None, "disks": []}},
        "coordinators": {"core1": {"handle": C1, "present": True}, "core2": {"handle": C2, "present": True}}}


def dossier(project=None, events=None, **kw):
    kw.setdefault("holdout_root", pathlib.Path("/nonexistent-holdout"))
    return pr.build_dossier(project or make_project(), events if events is not None else fixture(), LIVE, NOW, **kw)


class Dossier(unittest.TestCase):
    def test_11_05_numbers(self):
        d = dossier()
        par, text = d["data"]["parallel"], d["text"]
        self.assertGreaterEqual(par["FE"]["zero_share"], 0.85)
        self.assertGreaterEqual(par["SR"]["zero_share"], 0.85)
        self.assertEqual(par["FE"]["n"], 289)   # 24 h at 5 min, both ends
        self.assertGreaterEqual(d["data"]["overhead"]["by_line"]["EVB"], 0.40)
        # B8: gated by B6/B7 although its own node delivered PASS in the last 48 h
        near = {r["milestone"]: r for r in d["data"]["graph_audit"]["blocked_near_done"]}
        self.assertIn("B8", near)
        self.assertEqual(near["B8"]["pass_nodes"], ["EVB/evb-b8-public-api-1105"])
        self.assertEqual(sorted(near["B8"]["unmet_after"]), ["B6", "B7"])
        red = {(r["milestone"], r["prerequisite"]) for r in d["data"]["graph_audit"]["redundant"]}
        self.assertIn(("B8", "B6"), red)
        self.assertIn(("B8", "B7"), red)
        self.assertIn("疑似多余依赖：B8 的前置 B6", text)
        self.assertIn("被图挡住的近完成项：B8", text)
        keys = [f["key"] for f in d["data"]["flags"]]
        self.assertIn("idle_machine_line:FE", keys)
        self.assertIn("idle_machine_line:SR", keys)
        # idle FE/SR wait for B8 (F5) / F5 (F6): attribution names a milestone
        waits = d["data"]["idle"]["waits"]
        self.assertIn("B8", waits)
        self.assertIn("FE", waits["B8"]["lines"])

    def test_numbers_carry_denominators_and_sections(self):
        text = dossier()["text"]
        for n in range(1, 6):
            self.assertIn("\n## %d " % n, text)
        self.assertIn("289", text)                 # sample denominator
        self.assertIn("窗口：最近 72 小时", text)    # investment window
        self.assertIn("pm-1", text)
        self.assertIn("ic-4", text)
        self.assertIn("快速方法占比：跳过", text)    # fast_milestones not configured

    def test_confirmed_milestone_without_criteria_counts_its_split_conditions_met(self):
        p = make_project()
        p["milestones"][0].pop("criteria")          # B4 is confirmed in the fixture (30 h ago)
        d = dossier(project=p)
        b4 = [l for l in d["text"].splitlines() if l.startswith("    · B4.c")]
        self.assertEqual(len(b4), 2)
        self.assertTrue(all("[met]" in l for l in b4))
        self.assertEqual(d["data"]["stages"][0]["met"], 2)
        self.assertEqual(d["data"]["stages"][0]["closed_24h"], 0)   # closed at the confirm time, 30 h ago

    def test_criteria_missing_splits_sentences_unknown(self):
        d = dossier()
        b6 = [l for l in d["text"].splitlines() if l.startswith("    · B6.c")]
        self.assertEqual(len(b6), 3)               # 3 sentences in B6.done_when (。；split)
        self.assertTrue(all("[unknown]" in l for l in b6))
        self.assertIn("机械拆出", d["text"])
        # B4 declares criteria: used as written, met, closing time 30 h ago => not within 24 h
        st1 = d["data"]["stages"][0]
        self.assertEqual(st1["met"], 1)
        self.assertEqual(st1["closed_24h"], 0)

    def test_stage_undeclared_then_declared(self):
        self.assertIn("阶段未声明", dossier()["text"])
        self.assertEqual([r["stage"] for r in dossier()["data"]["stages"]], ["全部"])
        d = dossier(make_project(stages=True))
        self.assertNotIn("阶段未声明", d["text"])
        self.assertEqual([r["stage"] for r in d["data"]["stages"]], ["S1", "S2"])

    def test_fast_share_uses_config(self):
        p = make_project(stages=True, extra_defaults={"fast_milestones": ["B9"]})
        d = dossier(p)
        self.assertIn("快速方法（B9）占 S1 总投入", d["text"])
        # B9 holds 4 of ~18 S1 node-hours: well under 30% => flag
        self.assertIn("fast_share", [f["key"] for f in d["data"]["flags"]])

    def test_leak_ratio_from_injected_git(self):
        calls = []

        def git(line, since):
            calls.append((line, since))
            if line == "EVB":
                return [("src/md_peptide_case.py", 60), ("src/edit_utils.py", 40), ("src/core.py", 20)]
            return None
        d = dossier(git=git)
        by = {r["line"]: r for r in d["data"]["leak"]}
        self.assertEqual(by["EVB"]["total"], 120)
        self.assertEqual(by["EVB"]["workload"], 60)       # "edit" must not count as "dit"
        self.assertIn("leak:EVB", [f["key"] for f in d["data"]["flags"]])
        self.assertIn("skipped", by["FE"])
        self.assertEqual(calls[0][0], "EVB")

    def test_workload_terms_from_config(self):
        p = make_project(extra_defaults={"workload_terms": ["edit"]})
        d = dossier(p, git=lambda line, since: [("src/edit_utils.py", 10), ("src/core.py", 10)] if line == "EVB" else None)
        self.assertEqual({r["line"]: r for r in d["data"]["leak"]}["EVB"]["workload"], 10)

    def test_partial_pileup_needs_no_closed_criterion(self):
        d = dossier()
        self.assertEqual(d["data"]["partial"]["NEWP"], {"partial": 3, "total": 3})
        self.assertIn("partial:NEWP", [f["key"] for f in d["data"]["flags"]])

    def test_events_after_now_are_ignored(self):
        evs = fixture()
        late = dict(evs[1], id="late", at=snap.iso(NOW + dt.timedelta(hours=1)), kind="node_deliver", node="evb-cg-run-1105")
        a = dossier(events=evs)["data"]["invest"]["total_h"]
        b = dossier(events=evs + [late])["data"]["invest"]["total_h"]
        self.assertEqual(a, b)


class LeadContract(unittest.TestCase):
    """The project lead session (11-06) reads these dossier fields every cycle; renaming one breaks it.
    Change them only after telling the lead (docs/PROJECT_REVIEW.md)."""

    def test_overhead_flag_skips_lines_not_in_project(self):
        # 11-06: an old line imported from 11-04 records (not in project.json) raised a 50% overhead flag nobody owns
        base = dossier()
        self.assertIn("overhead:EVB", {f["key"] for f in base["data"]["flags"]})
        proj = make_project()
        proj["lines"] = [ln for ln in proj["lines"] if ln["id"] != "EVB"]
        d = dossier(project=proj)
        self.assertNotIn("overhead:EVB", {f["key"] for f in d["data"]["flags"]})
        self.assertGreaterEqual(d["data"]["overhead"]["by_line"]["EVB"], 0.40)   # still shown
        self.assertIn("不在项目线里，不计旗标", d["text"])

    def test_overhead_has_24h_per_line_window(self):
        d = dossier()
        o = d["data"]["overhead_24h"]
        self.assertEqual(o["window_h"], 24)
        self.assertTrue(o["by_line"])
        for v in o["by_line"].values():
            self.assertLessEqual({"ratio", "review_h", "repair_h", "total_h"}, set(v))
            self.assertLessEqual(v["review_h"] + v["repair_h"], v["total_h"] + 1e-9)
        self.assertEqual(d["data"]["overhead"]["window_h"], 72)
        self.assertIn("窗口 24 h", d["text"])

    def test_done_when_list_is_one_bullet_per_condition(self):
        p = make_project()
        p["final_goal"]["done_when"] = ["D1 真实评估器", "D4 新负载泛化"]
        txt = dossier(project=p)["text"]
        self.assertIn("- D1 真实评估器", txt)
        self.assertIn("- D4 新负载泛化", txt)
        self.assertNotIn("['D1", txt)

    def test_lead_facing_fields_are_stable(self):
        d = dossier()["data"]
        for k, typ in (("headline", list), ("stages", list), ("parallel", dict), ("idle", dict),
                       ("graph_audit", dict), ("flags", list)):
            self.assertIsInstance(d.get(k), typ, k)
        for h in d["headline"]:
            self.assertLessEqual({"ids", "text"}, set(h))
        for st in d["stages"]:
            self.assertLessEqual({"stage", "total", "met", "unknown", "unmet", "closed_24h", "milestones"}, set(st))
        self.assertIn("全项目", d["parallel"])
        for v in d["parallel"].values():
            self.assertLessEqual({"avg", "n", "peak", "zero", "zero_share"}, set(v))
        self.assertLessEqual({"nowork", "waits"}, set(d["idle"]))
        self.assertLessEqual({"blocked_near_done", "redundant", "top_waits"}, set(d["graph_audit"]))
        for f in d["flags"]:
            self.assertLessEqual({"key", "level", "text", "to"}, set(f))


class Holdout(unittest.TestCase):
    TERM = "zq_secretname_17"      # invented, never real

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name) / "holdout"
        self.root.mkdir()
        (self.root / "cases.txt").write_text("%s\nsome other invented case 8812 zz\n" % self.TERM, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def project_with_term(self):
        p = make_project()
        p["milestones"][1]["title"] = "MD 端到端 %s 变体" % self.TERM
        return p

    def events_with_term(self):
        evs = fixture()
        evs[15] = dict(evs[15], node=evs[15]["node"] + "-" + self.TERM)   # a name buried in a compound node id
        return evs

    def test_clean_dossier_passes_with_active_guard(self):
        d = pr.build_dossier(make_project(), fixture(), LIVE, NOW, holdout_root=self.root)
        self.assertNotIn(self.TERM, d["text"])

    def test_dossier_with_holdout_term_is_refused(self):
        with self.assertRaises(pr.HoldoutHit) as cm:
            pr.build_dossier(make_project(), self.events_with_term(), LIVE, NOW, holdout_root=self.root)
        self.assertGreaterEqual(cm.exception.count, 1)
        self.assertNotIn(self.TERM, str(cm.exception))

    def test_term_already_in_project_json_is_shown_with_an_overlap_count(self):
        d = pr.build_dossier(self.project_with_term(), fixture(), LIVE, NOW, holdout_root=self.root)
        self.assertEqual(d["data"]["holdout_public_overlap"], 1)
        self.assertIn("留出集名单里有 1 个词也出现在 project.json", d["text"])
        # still refused when the same term comes from somewhere other than the declaration
        with self.assertRaises(pr.HoldoutHit):
            pr.build_dossier(make_project(), self.events_with_term(), LIVE, NOW, holdout_root=self.root)

    def test_run_refuses_writes_nothing_and_never_prints_the_term(self):
        home = pathlib.Path(self.tmp.name) / "home"
        (home / "dse").mkdir(parents=True)
        os.environ["PROGRESS_HOME"] = str(home)
        os.environ["PROGRESS_CACHE_DIR"] = str(home / "cache")

        class Store:
            def read(_s):
                return make_project(), self.events_with_term(), LIVE
        err, out = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
                res = pr.run("dse", stage=3, force=True, now=NOW, store=Store(), holdout_root=self.root,
                             fn_a=lambda *a, **k: self.fail("model must not be called"),
                             send_qq_fn=lambda t: self.fail("nothing may be sent"))
        finally:
            os.environ.pop("PROGRESS_HOME")
            os.environ.pop("PROGRESS_CACHE_DIR")
        self.assertFalse(res["ok"])
        self.assertEqual(res["why"], "holdout_hit")
        self.assertFalse((home / "dse" / "project_review").exists())
        self.assertNotIn(self.TERM, err.getvalue() + out.getvalue() + json.dumps(res, ensure_ascii=False, default=str))
        self.assertRegex(err.getvalue(), r"（\d+ 行）")     # a count was logged


class Merge(unittest.TestCase):
    def adj(self, what="把 B8 对 B6 和 B7 的前置依赖去掉，B8 只依赖 B4", typ="restructure_milestone", to="coordinator:core1", **kw):
        d = {"type": typ, "to": to, "what": what, "why": "B8 有 PASS 交付但被 B6/B7 挡住", "expected_effect": "关键路径缩短 48 小时",
             "cost": "B8 的质量门移到 B11", "done_when": "project.json 里 B8.after 只剩 B4", "check_after_h": 12}
        d.update(kw)
        return d

    def rep(self, *adjs):
        return {"verdict": "at_risk", "summary": "s", "adjustments": list(adjs), "followups": []}

    def deliver(self, merged, st=None, live=LIVE):
        sent, qq = [], []
        st = st if st is not None else {}
        data = {"headline": [{"text": "B8 被 B6/B7 挡住，PASS 交付 1 个（最近 48 小时）", "ids": ["B8"]}], "flags": []}
        rows = pr.deliver(make_project(), merged, data, live, st, NOW,
                          lambda h, t: (sent.append((h, t)) or (True, "")), lambda t: (qq.append(t) or (True, "")))
        return rows, sent, qq, st

    def test_agreed_is_sent_with_numbers_and_qq(self):
        a = self.adj()
        b = self.adj(what="去掉 B8 对 B6、B7 的依赖（只保留 B4）")
        merged = pr.merge(self.rep(a), self.rep(b), [], NOW)
        self.assertEqual(len(merged["send"]), 1)
        self.assertEqual(merged["send"][0]["proposed_by"], ["A", "B"])
        self.assertEqual(merged["pending"], [])
        rows, sent, qq, st = self.deliver(merged)
        self.assertEqual(sent[0][0], C1)
        self.assertIn("B8 被 B6/B7 挡住，PASS 交付 1 个", sent[0][1])      # dossier numbers ride along
        self.assertIn("12 小时后回查", sent[0][1])
        self.assertEqual(len(qq), 1)
        self.assertIn("已发出 1 条调整", qq[0])
        self.assertEqual(len(st["followups"]), 1)

    def test_single_goes_pending_and_is_sent_when_proposed_again(self):
        merged = pr.merge(self.rep(self.adj()), self.rep(), [], NOW)
        self.assertEqual(merged["send"], [])
        self.assertEqual(len(merged["pending"]), 1)
        rows, sent, qq, _st = self.deliver(merged)
        self.assertEqual(sent, [])
        # next round: only the OTHER model proposes it again, in different words
        later = NOW + dt.timedelta(hours=6)
        again = self.adj(what="去掉 B8 对 B6 B7 的前置依赖，只留 B4")
        merged2 = pr.merge(self.rep(), self.rep(again), merged["pending"], later)
        self.assertEqual(len(merged2["send"]), 1)
        self.assertTrue(merged2["send"][0]["from_pending"])
        self.assertEqual(merged2["pending"], [])
        _rows, sent2, qq2, _ = self.deliver(merged2)
        self.assertEqual(len(sent2), 1)
        self.assertEqual(len(qq2), 1)

    def test_unrelated_single_proposals_stay_pending(self):
        other = self.adj(what="给 FE 补一个独立的 SMAC 对比任务", typ="add_work", to="coordinator:core2")
        merged = pr.merge(self.rep(self.adj()), self.rep(other), [], NOW)
        self.assertEqual(merged["send"], [])
        self.assertEqual(len(merged["pending"]), 2)

    def test_pending_expires(self):
        merged = pr.merge(self.rep(self.adj()), self.rep(), [], NOW)
        later = NOW + dt.timedelta(hours=pr.PENDING_TTL_H + 1)
        merged2 = pr.merge(self.rep(), self.rep(self.adj(what="完全不同的事情 增加 FE 线的基线对比", typ="add_work")), merged["pending"], later)
        self.assertEqual([p["type"] for p in merged2["pending"]], ["add_work"])

    def test_tier3_goes_to_human_only_even_when_agreed(self):
        a = self.adj(what="追加预算：给 core1 增加一台机器的算力", typ="reallocate", to="coordinator:core1")
        b = self.adj(what="追加预算，给 core1 再加一台机器", typ="reallocate", to="coordinator:core1")
        merged = pr.merge(self.rep(a), self.rep(b), [], NOW)
        self.assertEqual(merged["send"], [])
        self.assertEqual(len(merged["human"]), 1)
        rows, sent, qq, st = self.deliver(merged)
        self.assertEqual(sent, [])
        self.assertEqual(len(qq), 1)
        self.assertIn("待你决定", qq[0])
        # to=human and goal_draft are tier 3 whatever the text says
        m2 = pr.merge(self.rep(self.adj(to="human", typ="calibrate")), self.rep(self.adj(typ="goal_draft")), [], NOW)
        self.assertEqual(m2["send"], [])
        self.assertEqual(len(m2["human"]), 2)

    def test_open_ledger_entry_blocks_resend(self):
        a, b = self.adj(), self.adj(what="去掉 B8 对 B6、B7 的依赖（只保留 B4）")
        st = {}
        pr.register(st, a, C1, NOW)
        merged = pr.merge(self.rep(a), self.rep(b), [], NOW + dt.timedelta(hours=1), ledger=st["followups"])
        self.assertEqual(merged["send"], [])
        self.assertEqual(len(merged["dropped"]), 1)

    def test_absent_coordinator_requeues(self):
        merged = pr.merge(self.rep(self.adj()), self.rep(self.adj()), [], NOW)
        rows, sent, qq, st = self.deliver(merged, live={"coordinators": {}})
        self.assertEqual(sent, [])
        self.assertEqual(rows[0]["reason"], "coordinator_absent")
        self.assertEqual(len(merged["requeue"]), 1)
        self.assertEqual(st.get("followups") or {}, {})

    def test_validate_review(self):
        good = {"verdict": "at_risk", "summary": "x",
                "stage_progress": [{"stage": "S1", "met": 1, "total": 5, "closed_24h": 0}],
                "checklists": {"pm": [{"id": "pm-1", "result": "FAIL", "evidence": "24h 关闭 0"}], "codesign": [], "ic": []},
                "adjustments": [self.adj(check_after_h=1)] * 8,
                "followups": [{"id": "fu_a", "verdict": "done", "evidence": "e"}, {"id": "fu_zz", "verdict": "done", "evidence": "e"}]}
        clean, why = pr.validate_review(good, ["fu_a"])
        self.assertEqual(why, "")
        self.assertEqual(len(clean["adjustments"]), pr.MAX_ADJ)
        self.assertEqual(clean["adjustments"][0]["check_after_h"], 6)
        self.assertEqual([f["id"] for f in clean["followups"]], ["fu_a"])
        bad = json.loads(json.dumps(good))
        bad["checklists"]["pm"][0]["id"] = "pm-99"
        self.assertIsNone(pr.validate_review(bad, [])[0])
        self.assertIsNone(pr.validate_review({"verdict": "ok"}, [])[0])


class Ledger(unittest.TestCase):
    def adj(self, h):
        return {"type": "reallocate", "to": "coordinator:core2", "what": "给 FE 补活", "why": "w", "done_when": "有新节点", "check_after_h": h}

    def test_hours_and_clamp(self):
        st = {}
        e = pr.register(st, self.adj(1), C2, NOW)
        self.assertEqual(e["check_after_h"], 6)
        self.assertEqual(e["check_at"], snap.iso(NOW + dt.timedelta(hours=6)))
        st2 = {}
        e2 = pr.register(st2, self.adj(500), C2, NOW)
        self.assertEqual(e2["check_after_h"], 48)
        self.assertEqual(e2["check_at"], snap.iso(NOW + dt.timedelta(hours=48)))
        st3 = {}
        self.assertEqual(pr.register(st3, self.adj(24), C2, NOW)["check_after_h"], 24)
        # not due before the hours pass; due after
        self.assertEqual(core_pending(st, NOW + dt.timedelta(hours=5)), [])
        self.assertEqual(len(core_pending(st, NOW + dt.timedelta(hours=6))), 1)

    def test_second_not_done_escalates_to_human(self):
        st = {}
        e = pr.register(st, self.adj(12), C2, NOW)
        t1 = NOW + dt.timedelta(hours=12)
        r1 = pr.apply_verdicts(st, [{"id": e["id"], "verdict": "not_done", "evidence": "没有新节点"}], t1)
        self.assertEqual([x["id"] for x in r1["resend"]], [e["id"]])
        self.assertEqual(r1["human"], [])
        self.assertEqual(e["tries"], 1)
        self.assertEqual(e["check_at"], snap.iso(t1 + dt.timedelta(hours=12)))
        t2 = t1 + dt.timedelta(hours=12)
        r2 = pr.apply_verdicts(st, [{"id": e["id"], "verdict": "not_done", "evidence": "仍然没有"}], t2)
        self.assertEqual(r2["resend"], [])
        self.assertEqual([x["id"] for x in r2["human"]], [e["id"]])
        self.assertTrue(e["human"])
        self.assertEqual(e["tries"], 2)
        # the human digest carries it
        sent, qq = [], []
        pr.deliver(make_project(), {"send": [], "human": []}, {"headline": [], "flags": []}, LIVE, st, t2,
                   lambda h, t: sent.append(t) or (True, ""), lambda t: qq.append(t) or (True, ""), escalated=r2["human"])
        self.assertEqual(sent, [])
        self.assertIn("两次没落实", qq[0])

    def test_done_closes_and_cannot_tell_twice_counts(self):
        st = {}
        e = pr.register(st, self.adj(12), C2, NOW)
        pr.apply_verdicts(st, [{"id": e["id"], "verdict": "cannot_tell", "evidence": ""}], NOW)
        self.assertFalse(e.get("closed_at"))
        self.assertEqual(e["tries"], 0)
        r = pr.apply_verdicts(st, [{"id": e["id"], "verdict": "cannot_tell", "evidence": ""}], NOW)
        self.assertEqual(len(r["resend"]), 1)
        pr.apply_verdicts(st, [{"id": e["id"], "verdict": "done", "evidence": "node x"}], NOW)
        self.assertEqual(e["closed_how"], "done")

    def test_merge_verdicts(self):
        a = {"followups": [{"id": "x", "verdict": "done", "evidence": "a"}, {"id": "y", "verdict": "done", "evidence": "a"},
                           {"id": "z", "verdict": "not_done", "evidence": "a"}]}
        b = {"followups": [{"id": "x", "verdict": "done", "evidence": "b"}, {"id": "y", "verdict": "not_done", "evidence": "b"},
                           {"id": "z", "verdict": "cannot_tell", "evidence": "b"}]}
        out = {v["id"]: v["verdict"] for v in pr.merge_verdicts(a, b)}
        self.assertEqual(out, {"x": "done", "y": "cannot_tell", "z": "not_done"})


def core_pending(st, now):
    import supervisor
    return supervisor.pending_followups(st, now)


class Due(unittest.TestCase):
    def test_slots_in_beijing_time(self):
        # 2026-11-05T18:35Z = 11-06 02:35 BJ: the 02:00 slot has begun
        self.assertEqual(pr.slot_start(NOW), dt.datetime(2026, 11, 5, 18, 0, tzinfo=UTC))
        d, why = pr.due({"last_run_at": snap.iso(NOW - dt.timedelta(hours=1))}, NOW)
        self.assertTrue(d)      # last run 17:35Z, before the 18:00Z slot
        d, why = pr.due({"last_run_at": snap.iso(NOW - dt.timedelta(minutes=10))}, NOW)
        self.assertFalse(d)
        self.assertTrue(pr.due({}, NOW)[0])

    def test_triggers(self):
        st = {"last_run_at": snap.iso(NOW - dt.timedelta(minutes=10))}
        conf = [{"kind": "milestone_confirm", "at": snap.iso(NOW - dt.timedelta(minutes=5))}]
        self.assertEqual(pr.due(st, NOW, None, conf), (True, "milestone_confirm"))
        old = [{"kind": "milestone_confirm", "at": snap.iso(NOW - dt.timedelta(hours=1))}]
        self.assertFalse(pr.due(st, NOW, None, old)[0])
        p = make_project()
        st2 = dict(st, project_hash=pr.project_hash(p))
        self.assertFalse(pr.due(st2, NOW, p)[0])
        p["milestones"][0]["title"] = "changed"
        self.assertEqual(pr.due(st2, NOW, p), (True, "project_changed"))
        rep = {"generated_at": "t1", "lines": [{"line": "EVB", "status": "problem", "issues": [{"kind": "stalled"}]}]}
        rep2 = dict(rep, generated_at="t2")
        self.assertEqual(pr.due(st, NOW, None, [], [rep, rep2]), (True, "supervisor_repeat"))
        self.assertFalse(pr.due(dict(st, last_trigger_report="t2"), NOW, None, [], [rep, rep2])[0])
        other = {"generated_at": "t2", "lines": [{"line": "FE", "status": "problem", "issues": [{"kind": "stalled"}]}]}
        self.assertFalse(pr.due(st, NOW, None, [], [rep, other])[0])


class RunStages(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        (self.home / "dse").mkdir()
        self.env = {k: os.environ.get(k) for k in ("PROGRESS_HOME", "PROGRESS_CACHE_DIR")}
        os.environ["PROGRESS_HOME"] = str(self.home)
        os.environ["PROGRESS_CACHE_DIR"] = str(self.home / "cache")
        proj = make_project(stages=True)
        self.proj = proj

        class Store:
            def read(_s):
                return proj, fixture(), LIVE
        self.store = Store()
        self.model_calls = []

    def tearDown(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def report(self, what):
        return {"verdict": "at_risk", "summary": "s", "stage_progress": [],
                "checklists": {"pm": [], "codesign": [], "ic": []},
                "adjustments": [{"type": "restructure_milestone", "to": "coordinator:core1", "what": what, "why": "w",
                                 "expected_effect": "e", "cost": "c", "done_when": "d", "check_after_h": 12}],
                "followups": []}

    def fn(self, name, what):
        def call(prompt, cwd, schema, timeout):
            self.model_calls.append((name, prompt))
            return self.report(what)
        return call

    def go(self, stage, **kw):
        sent, qq = [], []
        res = pr.run("dse", stage=stage, force=True, now=NOW, store=self.store, holdout_root=pathlib.Path("/nonexistent"),
                     fn_a=self.fn("a", "把 B8 对 B6 和 B7 的前置依赖去掉，B8 只依赖 B4"),
                     fn_b=self.fn("b", "去掉 B8 对 B6、B7 的依赖（只保留 B4）"),
                     send_coord=lambda h, t: sent.append((h, t)) or (True, ""), send_qq_fn=lambda t: qq.append(t) or (True, ""), **kw)
        return res, sent, qq

    def test_stage1_dossier_only(self):
        res, sent, qq = self.go(1)
        self.assertTrue(res["ok"])
        self.assertEqual(self.model_calls, [])
        d = self.home / "dse" / "project_review"
        self.assertEqual(len(list(d.glob("dossier-*.md"))), 1)
        self.assertEqual(len(list(d.glob("dossier-*.json"))), 1)
        self.assertEqual(list(d.glob("review-*.json")), [])
        self.assertEqual(sent + qq, [])

    def test_dry_run_writes_nothing(self):
        res, sent, qq = self.go(3, dry_run=True)
        self.assertTrue(res["dry_run"])
        self.assertFalse((self.home / "dse" / "project_review").exists())
        self.assertEqual(self.model_calls, [])

    def test_stage2_one_model_nothing_sent(self):
        res, sent, qq = self.go(2)
        self.assertEqual([c[0] for c in self.model_calls], ["a"])
        self.assertEqual(sent + qq, [])
        self.assertEqual(len(list((self.home / "dse" / "project_review").glob("review-*.json"))), 1)
        self.assertIn("项目档案", self.model_calls[0][1])
        self.assertNotIn("{dossier}", self.model_calls[0][1])
        self.assertNotIn("另一家模型", self.model_calls[0][1])

    def test_stage3_two_models_merge_and_send(self):
        res, sent, qq = self.go(3)
        self.assertEqual(sorted(c[0] for c in self.model_calls), ["a", "b"])
        prompt_b = [p for n, p in self.model_calls if n == "b"][0]
        self.assertIn("另一家模型也在独立评审", prompt_b)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], C1)
        self.assertEqual(len(qq), 1)
        st = pr.load_state("dse")
        self.assertEqual(len(st["followups"]), 1)
        doc = json.loads(next((self.home / "dse" / "project_review").glob("review-*.json")).read_text(encoding="utf-8"))
        self.assertEqual(doc["schema"], pr.SCHEMA_ID)

    def test_not_due_skips(self):
        self.go(1)
        res = pr.run("dse", stage=1, now=NOW + dt.timedelta(minutes=5), store=self.store, holdout_root=pathlib.Path("/nonexistent"))
        self.assertFalse(res["ran"])

    def test_env_command_override(self):
        os.environ["PROGRESS_REVIEW_A_CMD"] = "cat"
        try:
            self.assertIsNone(pr.call_a("not json", None, pr.SCHEMA, 5))
        finally:
            os.environ.pop("PROGRESS_REVIEW_A_CMD")


class CoreValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("PROGRESS_HOME")
        os.environ["PROGRESS_HOME"] = self.tmp.name

    def tearDown(self):
        if self.old is None:
            os.environ.pop("PROGRESS_HOME", None)
        else:
            os.environ["PROGRESS_HOME"] = self.old
        self.tmp.cleanup()

    def test_criteria_and_stage_accepted(self):
        p = make_project(stages=True)
        self.assertEqual(core.validate_project(p), [])
        p["milestones"][0]["criteria"] = [{"id": "a", "text": "t", "status": "unmet", "evidence": ""}]
        self.assertEqual(core.validate_project(p), [])

    def test_missing_criteria_is_fine(self):
        p = make_project()
        for m in p["milestones"]:
            m.pop("criteria", None)
        self.assertEqual(core.validate_project(p), [])

    def test_malformed_criteria_rejected_with_clear_error(self):
        for bad, needle in [
            ("x", "必须是数组"),
            ([{"id": "a", "text": "t", "status": "done"}], "status 只能是 met/unknown/unmet"),
            ([{"id": "a", "text": "", "status": "met"}], "缺 text"),
            ([{"text": "t", "status": "met"}], "缺 id"),
            ([{"id": "a", "text": "t", "status": "met"}, {"id": "a", "text": "u", "status": "met"}], "id 重复"),
            (["str"], "必须是对象"),
            ([{"id": "a", "text": "t", "status": "met", "evidence": 5}], "evidence"),
            ([{"id": "a", "text": "t", "status": "met", "closed_at": "yesterday"}], "closed_at"),
        ]:
            p = make_project()
            p["milestones"][0]["criteria"] = bad
            errs = core.validate_project(p)
            self.assertTrue(any(needle in e for e in errs), (bad, errs))
        p = make_project()
        p["milestones"][0]["stage"] = "phase1"
        self.assertTrue(any("stage" in e for e in core.validate_project(p)))
        with self.assertRaises(core.ProgressError):
            p["milestones"][0]["criteria"] = [{"id": "a", "text": "t", "status": "bogus"}]
            core.save_project(p)

    def test_untagged_run_plan_warns(self):
        core.save_project(make_project())
        ev = {"kind": "node_plan", "line": "EVB", "node": "evb-x-1", "title": "x", "summary": "s",
              "actor": {"role": "router", "handle": R["EVB"]}}
        res = core.append("dse", dict(ev))
        self.assertTrue(res["ok"])
        self.assertTrue(any("未挂里程碑" in w for w in res["warnings"]))
        tagged = core.append("dse", dict(ev, node="evb-x-2", milestones=["B5"]))
        self.assertNotIn("warnings", tagged)
        review = core.append("dse", dict(ev, node="evb-x-3", node_kind="review"))
        self.assertNotIn("warnings", review)
        imported = core.append("dse", dict(ev, node="evb-x-4", source="import", actor={"role": "import", "handle": "import@progress"}))
        self.assertNotIn("warnings", imported)


if __name__ == "__main__":
    unittest.main()
