"""Sub-graph proposals, the gate, the scheduler (ceiling / serial / signals / collapse), worker release, metrics."""
import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import graph  # noqa: E402
import progress  # noqa: E402
import progress_core as core  # noqa: E402
from fakes import FakeRunner  # noqa: E402
from test_patrol import NOW, R, ROUTE, Base, at, live, plan_ev, project  # noqa: E402

SUB = "term_subrouter-cccc"


def child(cid, kind="run", deps=None, scope=None, minutes=60, acc=None, spec=True, **kw):
    c = {"id": cid, "title": cid, "kind": kind, "deps": deps or [],
         "budget": {"minutes": minutes}, "acceptance": acc if acc is not None else ["%s 的测试通过" % cid]}
    if scope is not False:
        c["write_scope"] = scope if scope is not None else (["src"] if kind == "plan" else ["src/%s" % cid])
    if kind == "plan":
        c["budget"]["nodes"] = kw.pop("nodes", 4)
    if spec and kind in ("run", "check"):
        c["spec"] = "br@docs/%s.md" % cid
        c["route"] = ROUTE
    c.update(kw)
    return c


def proposal(*segments, parent=None, evidence=("docs/REPORT.md",), line="L"):
    return {"line": line, "parent": parent, "summary": "拆分", "evidence": list(evidence),
            "segments": [{"id": sid, "mode": mode, "nodes": nodes} for sid, mode, nodes in segments]}


def gproject(**line_kw):
    p = project()
    p["lines"][0].update({"write_scope": ["src"], "budget": {"minutes": 900, "nodes": 20}}, **line_kw)
    return p


def prop_ev(prop, actor=R, minutes_ago=200, role="router", **kw):
    ev = {"kind": "graph_propose", "line": prop["line"], "summary": prop["summary"], "proposal": prop,
          "actor": {"role": role, "handle": actor}, "at": at(minutes_ago)}
    if prop.get("parent"):
        ev["node"] = prop["parent"]
    ev.update(kw)
    return ev


class GBase(Base):
    def setUp(self):
        super().setUp()
        core.save_project(gproject())

    def gate(self, prop):
        p = core.load_project("t")
        return graph.gate(prop, p, core.fold(p, core.read_events("t")))


class Gate(GBase):
    def test_valid_proposal_passes(self):
        r = self.gate(proposal(("a", "parallel", [child("x"), child("y")]), ("b", "serial", [child("z", deps=["x", "y"])])))
        self.assertTrue(r["ok"], r["errors"])
        self.assertEqual(r["children"], ["x", "y", "z"])
        self.assertEqual([s["mode"] for s in r["normalized"]["segments"]], ["parallel", "serial"])

    def test_cycle_rejected(self):
        r = self.gate(proposal(("a", "parallel", [child("x", deps=["y"]), child("y", deps=["x"])])))
        self.assertFalse(r["ok"])
        self.assertTrue(any("成环" in e for e in r["errors"]), r["errors"])

    def test_self_dep_and_unknown_dep(self):
        r = self.gate(proposal(("a", "serial", [child("x", deps=["x"]), child("y", deps=["nope"])])))
        self.assertTrue(any("依赖它自己" in e for e in r["errors"]))
        self.assertTrue(any("nope" in e for e in r["errors"]))

    def test_too_many_children(self):
        r = self.gate(proposal(("a", "serial", [child("n%d" % i, minutes=30) for i in range(13)])))
        self.assertTrue(any("超过上限 12" in e for e in r["errors"]), r["errors"])

    def test_depth_limit(self):
        # chain of plan nodes 1..8 deep, then a proposal under the 8th must be rejected
        parent = None
        for d in range(1, 9):
            pid = "p%d" % d
            self.add(prop_ev(proposal(("s", "serial", [child(pid, kind="plan", minutes=60, nodes=18 - 2 * d)]), parent=parent),
                             minutes_ago=300 - d))
            parent = pid
        p = core.load_project("t")
        nodes = core.fold(p, core.read_events("t"))["lines"]["L"]["nodes"]
        self.assertEqual(graph.depth_of(nodes, "p8"), 8)
        r = self.gate(proposal(("s", "serial", [child("leaf", minutes=30)]), parent="p8"))
        self.assertTrue(any("层级过深" in e for e in r["errors"]), r["errors"])

    def test_missing_acceptance_budget_scope_evidence(self):
        bad = child("x", acc=[], scope=False)
        bad.pop("budget")
        r = self.gate(proposal(("a", "serial", [bad]), evidence=()))
        txt = "\n".join(r["errors"])
        for word in ("验收标准", "预算", "写域", "依据"):
            self.assertIn(word, txt)

    def test_bad_kind_and_mode(self):
        r = self.gate(proposal(("a", "sometimes", [child("x", kind="review")])))
        txt = "\n".join(r["errors"])
        self.assertIn("serial 或 parallel", txt)
        self.assertIn("run/plan/check/decide", txt)

    def test_scope_outside_line(self):
        r = self.gate(proposal(("a", "serial", [child("x", scope=["docs/x"])])))
        self.assertTrue(any("超出主线 L 的写域" in e for e in r["errors"]), r["errors"])

    def test_scope_outside_parent_plan(self):
        self.add(prop_ev(proposal(("s", "serial", [child("p1", kind="plan", scope=["src/core"], minutes=120)]))))
        r = self.gate(proposal(("a", "serial", [child("x", scope=["src/other"])]), parent="p1"))
        self.assertTrue(any("超出父节点 p1 的写域" in e for e in r["errors"]), r["errors"])
        r = self.gate(proposal(("a", "serial", [child("x", scope=["src/core/a.py"], minutes=60)]), parent="p1"))
        self.assertTrue(r["ok"], r["errors"])

    def test_parent_must_be_plan(self):
        self.add(plan_ev("L", "legacy"))
        r = self.gate(proposal(("a", "serial", [child("x")]), parent="legacy"))
        self.assertTrue(any("只有 plan" in e for e in r["errors"]), r["errors"])

    def test_budget_against_parent_including_already_handed_out(self):
        self.add(prop_ev(proposal(("s", "serial", [child("p1", kind="plan", minutes=100, nodes=3)]))))
        ok = self.gate(proposal(("a", "parallel", [child("x", minutes=50), child("y", minutes=50)]), parent="p1"))
        self.assertTrue(ok["ok"], ok["errors"])
        self.add(prop_ev(proposal(("a", "parallel", [child("x", minutes=50), child("y", minutes=40)]), parent="p1"),
                         minutes_ago=100))
        r = self.gate(proposal(("b", "serial", [child("z", minutes=30)]), parent="p1"))
        self.assertTrue(any("时间预算超了" in e for e in r["errors"]), r["errors"])
        r = self.gate(proposal(("b", "serial", [child("z", minutes=10), child("w", minutes=0.5)]), parent="p1"))
        self.assertTrue(any("节点数预算超了" in e for e in r["errors"]), r["errors"])

    def test_line_budget_and_subtree_nodes(self):
        r = self.gate(proposal(("s", "serial", [child("p1", kind="plan", minutes=100, nodes=25)])))
        self.assertTrue(any("节点数预算超了" in e for e in r["errors"]), r["errors"])

    def test_no_declared_scope_or_budget_only_warns(self):
        core.save_project(project())
        r = self.gate(proposal(("a", "serial", [child("x")])))
        self.assertTrue(r["ok"], r["errors"])
        self.assertEqual(len(r["warnings"]), 2)

    def test_parallel_overlap_downgraded_to_serial_and_chained(self):
        r = self.gate(proposal(("a", "parallel", [child("x", scope=["src/m"]), child("y", scope=["src/m/f.py"])])))
        self.assertTrue(r["ok"], r["errors"])
        seg = r["normalized"]["segments"][0]
        self.assertEqual((seg["mode"], seg["downgraded_from"]), ("serial", "parallel"))
        self.assertIn("x", seg["nodes"][1]["deps"])
        self.assertTrue(any("已按 serial" in w for w in r["warnings"]))

    def test_parallel_same_acceptance_downgraded(self):
        r = self.gate(proposal(("a", "parallel", [child("x", acc=["全部测试通过"]), child("y", acc=["全部测试通过"])])))
        self.assertEqual(r["normalized"]["segments"][0]["mode"], "serial")

    def test_serial_segment_of_independent_nodes_warns_it_could_be_parallel(self):
        r = self.gate(proposal(("a", "serial", [child("x"), child("y"), child("z")])))
        self.assertTrue(r["ok"], r["errors"])
        self.assertTrue(any("段 a 标为 serial" in w and "可以标 parallel" in w for w in r["warnings"]), r["warnings"])
        for seg in (("a", "serial", [child("x"), child("y", deps=["x"])]),                  # ordered on purpose
                    ("a", "serial", [child("x", scope=["src/m"]), child("y", scope=["src/m/f.py"])]),
                    ("a", "parallel", [child("x", acc=["同一验收"]), child("y", acc=["同一验收"])])):
            r = self.gate(proposal(seg))
            self.assertFalse(any("可以标 parallel" in w for w in r["warnings"]), (seg[0], r["warnings"]))

    def test_serial_order_respects_inner_deps(self):
        r = self.gate(proposal(("a", "serial", [child("x", deps=["y"]), child("y")])))
        nodes = r["normalized"]["segments"][0]["nodes"]
        self.assertEqual([n["id"] for n in nodes], ["y", "x"])

    def test_existing_id_rejected(self):
        self.add(plan_ev("L", "x"))
        r = self.gate(proposal(("a", "serial", [child("x")])))
        self.assertTrue(any("已存在" in e for e in r["errors"]))

    def test_run_size_hint(self):
        r = self.gate(proposal(("a", "serial", [child("x", minutes=10)])))
        self.assertTrue(r["ok"])
        self.assertTrue(any("30 分钟–2 小时" in w for w in r["warnings"]))


class Commit(GBase):
    def test_append_folds_children_with_segment_and_parent(self):
        self.add(prop_ev(proposal(("s", "serial", [child("p1", kind="plan", minutes=200, nodes=5)]))))
        res = core.append("t", prop_ev(proposal(("a", "parallel", [child("x"), child("y")]), parent="p1"), minutes_ago=100))
        self.assertTrue(res["ok"])
        self.assertEqual(res["children"], ["x", "y"])
        ls = core.fold(core.load_project("t"), core.read_events("t"))["lines"]["L"]
        x = ls["nodes"]["x"]
        self.assertEqual((x["parent"], x["segment"], x["segment_mode"], x["kind"], x["status"]),
                         ("p1", "a", "parallel", "run", "ready"))
        self.assertEqual(x["acceptance"], ["x 的测试通过"])
        self.assertTrue(x["auto_dispatch"])
        self.assertEqual(ls["nodes"]["p1"]["status"], "running")  # expanding

    def test_rejected_proposal_writes_nothing_and_lists_errors(self):
        n = len(core.read_events("t"))
        with self.assertRaises(core.ProgressError) as cm:
            core.append("t", prop_ev(proposal(("a", "parallel", [child("x", acc=[]), child("y", deps=["x"], scope=["etc"])]))))
        self.assertEqual(cm.exception.code, "gate_rejected")
        errs = cm.exception.detail["errors"]
        self.assertGreaterEqual(len(errs), 2)
        self.assertTrue(all(isinstance(e, str) for e in errs))
        self.assertEqual(len(core.read_events("t")), n)

    def test_only_line_router_or_owning_subrouter(self):
        with self.assertRaises(core.ProgressError) as cm:
            core.append("t", prop_ev(proposal(("a", "serial", [child("x")])), actor="term_stranger"))
        self.assertEqual(cm.exception.code, "not_router")
        self.add(prop_ev(proposal(("s", "serial", [child("p1", kind="plan", minutes=300, nodes=6)]))),
                 {"kind": "node_dispatch", "line": "L", "node": "p1", "summary": "派子 router", "at": at(150),
                  "actor": {"role": "router", "handle": R}, "refs": {"terminal": SUB}})
        res = core.append("t", prop_ev(proposal(("a", "serial", [child("x")]), parent="p1"), actor=SUB, minutes_ago=100))
        self.assertTrue(res["ok"])
        # the sub-router may also dispatch/review inside its own subtree, not elsewhere
        core.append("t", {"kind": "node_dispatch", "line": "L", "node": "x", "summary": "派", "at": at(90),
                          "actor": {"role": "router", "handle": SUB}})
        self.add(plan_ev("L", "other"))
        with self.assertRaises(core.ProgressError):
            core.append("t", {"kind": "node_dispatch", "line": "L", "node": "other", "summary": "派", "at": at(80),
                              "actor": {"role": "router", "handle": SUB}})

    def test_parent_only_in_proposal_body(self):
        self.add(prop_ev(proposal(("s", "serial", [child("p1", kind="plan", minutes=300, nodes=6)]))),
                 dispatch("p1", 150, terminal=SUB))
        ev = prop_ev(proposal(("a", "serial", [child("x")]), parent="p1"), actor=SUB, minutes_ago=100)
        ev.pop("node")
        self.assertTrue(core.append("t", ev)["ok"])
        self.assertEqual(self.node("L", "x")["parent"], "p1")

    def test_legacy_plan_events_unchanged(self):
        self.add(plan_ev("L", "a"), plan_ev("L", "b", deps=["a"]))
        ls = core.fold(core.load_project("t"), core.read_events("t"))["lines"]["L"]
        self.assertEqual((ls["nodes"]["a"]["status"], ls["nodes"]["b"]["status"]), ("ready", "planned"))
        self.assertNotIn("segment", ls["nodes"]["a"])
        self.assertIsNone(ls["nodes"]["a"].get("parent"))
        # a proposal may depend on legacy nodes
        r = self.gate(proposal(("x", "serial", [child("c", deps=["b"])])))
        self.assertTrue(r["ok"], r["errors"])

    def test_max_parallel_validated(self):
        p = gproject(max_parallel=0)
        self.assertTrue(any("max_parallel" in e for e in core.validate_project(p)))


class Cli(GBase):
    def run_cli(self, *argv, stdin=None):
        buf = io.StringIO()
        old = sys.stdin
        if stdin is not None:
            sys.stdin = io.StringIO(stdin)
        try:
            with contextlib.redirect_stdout(buf):
                rc = progress.main(["--project", "t"] + list(argv))
        finally:
            sys.stdin = old
        return rc, json.loads(buf.getvalue().strip().splitlines()[-1])

    def write(self, doc):
        f = pathlib.Path(self.tmp.name) / "prop.json"
        f.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return str(f)

    def test_propose_ok_and_check_writes_nothing(self):
        os.environ["ORCA_TERMINAL_HANDLE"] = R
        try:
            f = self.write(proposal(("a", "parallel", [child("x"), child("y")])))
            rc, out = self.run_cli("propose", f, "--check")
            self.assertEqual((rc, out["ok"], out.get("checked")), (0, True, True))
            self.assertEqual(core.read_events("t"), [])
            rc, out = self.run_cli("propose", f)
            self.assertEqual((rc, out["ok"], out["children"]), (0, True, ["x", "y"]))
            self.assertEqual(self.events("graph_propose")[0]["proposal"]["segments"][0]["mode"], "parallel")
        finally:
            os.environ.pop("ORCA_TERMINAL_HANDLE", None)

    def test_propose_rejected_chinese_error_list(self):
        f = self.write(proposal(("a", "parallel", [child("x", acc=[]), child("x")]), evidence=()))
        rc, out = self.run_cli("propose", f, "--as", R)
        self.assertEqual(rc, 2)
        self.assertEqual(out["code"], "gate_rejected")
        errs = out["detail"]["errors"]
        self.assertIn("子图提议未通过闸门", out["error"])
        self.assertTrue(any("验收标准" in e for e in errs) and any("依据" in e for e in errs) and any("重复" in e for e in errs))
        self.assertEqual(core.read_events("t"), [])

    def test_parallel_same_acceptance_rejected_before_commit(self):
        f = self.write(proposal(("a", "parallel", [child("x", acc=["全部测试通过"]), child("y", acc=["全部测试通过"])])))
        rc, out = self.run_cli("propose", f, "--check")
        self.assertEqual((rc, out["code"]), (2, "gate_rejected"))
        self.assertTrue(any("验收标准完全相同" in e for e in out["detail"]["errors"]))
        rc, out = self.run_cli("propose", f, "--as", R)
        self.assertEqual((rc, out["code"]), (2, "gate_rejected"))
        self.assertTrue(any("标成 serial" in e for e in out["detail"]["errors"]))
        self.assertEqual(core.read_events("t"), [])
        # overlapping write scopes are structure, not a slip: still committed, downgraded to serial with a warning
        f = self.write(proposal(("a", "parallel", [child("x", scope=["src/s"]), child("y", scope=["src/s"])])))
        rc, out = self.run_cli("propose", f, "--as", R)
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["segments"][0].get("downgraded_from"), "parallel")

    def test_propose_stdin_and_flags_override(self):
        doc = proposal(("a", "serial", [child("x")]))
        doc.pop("line")
        rc, out = self.run_cli("propose", "-", "--line", "L", "--as", R, "--summary", "改写的说明", stdin=json.dumps(doc))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.events("graph_propose")[0]["summary"], "改写的说明")

    def test_deliver_failure_and_touched_flags(self):
        a = progress.parser().parse_args(["deliver", "--line", "L", "--node", "x", "--outcome", "FAIL", "--summary", "s",
                                          "--failure", "test_a", "--failure", "test_b", "--touched", "src/a,src/b"])
        ev = progress.build_event("deliver", a)
        self.assertEqual((ev["failures"], ev["touched"]), (["test_a", "test_b"], ["src/a", "src/b"]))


def deliver(nid, minutes_ago, outcome="PASS", handle="term_new-worker", **kw):
    ev = {"kind": "node_deliver", "line": "L", "node": nid, "outcome": outcome, "summary": "交付",
          "at": at(minutes_ago), "actor": {"role": "worker", "handle": handle}}
    ev.update(kw)
    return ev


def imported(handle="term_new-worker"):
    """actor for a FAIL/PARTIAL delivery: worker/router may only deliver PASS or BLOCKED (R2), history import may not."""
    return {"role": "import", "handle": handle}


def dispatch(nid, minutes_ago, terminal="term_new-worker", task=None, disp=None):
    refs = {"terminal": terminal}
    if task:
        refs["task"] = task
    if disp:
        refs["dispatch"] = disp
    return {"kind": "node_dispatch", "line": "L", "node": nid, "summary": "派", "at": at(minutes_ago),
            "actor": {"role": "router", "handle": R}, "refs": refs}


def finished_live(**tasks):
    lv = live()
    lv["tasks"] = {t: {"state": st, "completed_at": at(5)} for t, st in tasks.items()}
    return lv


class Scheduler(GBase):
    def starts(self, fr):
        return [c[c.index("--task-title") + 1] for c in fr.kinds("worker-start")]

    def test_ceiling_not_target(self):
        core.save_project(gproject(max_parallel=2))
        self.add(prop_ev(proposal(("a", "parallel", [child("n%d" % i) for i in range(5)]))))
        self.put_live(live())
        fr = FakeRunner()
        out = self.run_patrol(fr)
        self.assertEqual(len(fr.kinds("worker-start")), 2)
        f = [x for x in out["findings"] if x["code"] == "parallel_ceiling"]
        self.assertEqual(len(f[0]["deferred"]), 3)
        # idle slots never wake the router: nothing about "fill slots" sent
        for c in fr.kinds("send"):
            self.assertNotIn("可并行", c[c.index("--text") + 1])

    def test_low_disk_holds_auto_dispatch(self):
        self.add(prop_ev(proposal(("a", "parallel", [child("n%d" % i) for i in range(3)]))))
        lv = live()
        disk = {"disks": [{"mount": "/", "avail_gb": 40.0, "used_pct": 95, "level": "low"}], "checked_at": at(1)}
        import snapshot as snap
        lv["resources"] = {h: disk for h in ("wsl", snap.remote_host())}
        self.put_live(lv)
        fr = FakeRunner()
        out = self.run_patrol(fr)
        self.assertEqual(fr.kinds("worker-start"), [])
        f = [x for x in out["findings"] if x["code"] == "dispatch_held_disk"]
        self.assertEqual(len(f[0]["deferred"]), 3)
        lv["resources"] = {h: dict(disk, disks=[dict(disk["disks"][0], avail_gb=80.0)]) for h in lv["resources"]}
        self.put_live(lv)
        fr = FakeRunner()
        self.run_patrol(fr, minutes_later=1)
        self.assertEqual(len(fr.kinds("worker-start")), 3)

    def test_no_default_ceiling(self):
        self.add(prop_ev(proposal(("a", "parallel", [child("n%d" % i) for i in range(5)]))))
        self.put_live(live())
        fr = FakeRunner()
        out = self.run_patrol(fr)
        self.assertEqual(len(fr.kinds("worker-start")), 5)
        self.assertFalse(any(x["code"] == "parallel_ceiling" for x in out["findings"]))

    def test_ceiling_counts_running_workers_and_legacy_lines_alike(self):
        core.save_project(gproject(max_parallel=1))
        self.add(plan_ev("L", "a", auto_dispatch=False), dispatch("a", 30), plan_ev("L", "b"))
        self.put_live(live())
        fr = FakeRunner()
        out = self.run_patrol(fr)
        self.assertEqual(fr.kinds("worker-start"), [])
        self.assertTrue(any(x["code"] == "parallel_ceiling" for x in out["findings"]))

    def test_finished_task_frees_its_slot(self):
        core.save_project(gproject(max_parallel=1))
        self.add(plan_ev("L", "a", auto_dispatch=False), dispatch("a", 30, task="task_a"), plan_ev("L", "b"))
        self.put_live(finished_live(task_a="succeeded"))
        fr = FakeRunner()
        self.run_patrol(fr)
        self.assertEqual(len(fr.kinds("worker-start")), 1)

    def test_serial_segment_one_at_a_time(self):
        # gate chains a serial segment, so only the first is ready; also guard against hand-written deps
        self.add(prop_ev(proposal(("s", "serial", [child("x"), child("y"), child("z")]))))
        self.put_live(live())
        fr = FakeRunner()
        self.run_patrol(fr)
        self.assertEqual(self.starts(fr), ["x"])
        n = self.node("L", "y")
        self.assertEqual((n["status"], n["deps"]), ("planned", ["x"]))

    def test_serial_guard_when_two_ready(self):
        # a serial segment whose nodes are both ready (deps outside the segment): never two at once
        ev = prop_ev(proposal(("s", "serial", [child("x"), child("y")])))
        core.append("t", ev)
        raw = core.read_events("t")
        for seg in raw[0]["proposal"]["segments"]:
            for c in seg["nodes"]:
                c["deps"] = []
        (pathlib.Path(self.tmp.name) / "t" / "events.jsonl").write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in raw), encoding="utf-8")
        self.put_live(live())
        fr = FakeRunner()
        out = self.run_patrol(fr)
        self.assertEqual(len(fr.kinds("worker-start")), 1)
        held = [x for x in out["findings"] if x["code"] == "dispatch_held"]
        self.assertEqual(held[0]["deferred"][0]["why"], "serial")

    def test_global_queue_across_depths_critical_path_first(self):
        core.save_project(gproject(max_parallel=1))
        self.add(prop_ev(proposal(("s", "parallel", [child("p1", kind="plan", scope=["src/deep"], minutes=300, nodes=6),
                                                     child("short", minutes=30)]))))
        self.add(prop_ev(proposal(("a", "serial", [child("deep1", scope=["src/deep/a"], minutes=90),
                                                   child("deep2", scope=["src/deep/b"], minutes=90)]), parent="p1"),
                         minutes_ago=150))
        self.put_live(live())
        fr = FakeRunner()
        self.run_patrol(fr)
        self.assertEqual(self.starts(fr), ["deep1"])  # depth 2, longest chain beats the shallow short node

    def test_plan_and_decide_nodes_not_auto_dispatched(self):
        self.add(prop_ev(proposal(("s", "parallel", [child("p1", kind="plan", minutes=60, route=ROUTE,
                                                           **{"spec": "br@docs/p1.md"}),
                                                     child("d1", kind="decide", scope=False, minutes=30)]))))
        self.put_live(live())
        fr = FakeRunner()
        out = self.run_patrol(fr)
        self.assertEqual(fr.kinds("worker-start"), [])
        self.assertTrue(any(x["code"] == "idle" and "p1" in x["ready"] for x in out["findings"]))

    def test_metrics_in_output(self):
        self.add(plan_ev("L", "a", auto_dispatch=False, minutes_ago=180), plan_ev("L", "b", deps=["a"], auto_dispatch=False))
        self.put_live(live())
        out = self.run_patrol(FakeRunner(), apply=False)
        m = out["metrics"]["L"]
        self.assertEqual(m["ready_undispatched"]["nodes"], 1)
        self.assertAlmostEqual(m["ready_undispatched"]["node_hours"], 3.0, places=1)
        self.assertEqual(m["critical_path"]["nodes"], ["a", "b"])
        self.assertEqual(m["critical_path"]["stall_min"], 180)
        self.assertIsNone(m["review_run"]["ratio"])


class Signals(GBase):
    def setUp(self):
        super().setUp()
        self.add(prop_ev(proposal(("s", "serial", [child("x")]))), dispatch("x", 120))

    def texts(self, fr):
        return [c[c.index("--text") + 1] for c in fr.kinds("send")]

    def test_distinct_failures_wake_router_once(self):
        self.add(deliver("x", 30, outcome="FAIL", failures=["test_a 断言失败", "test_b 超时", "test_a 断言失败"], actor=imported()))
        self.put_live(live())
        fr = FakeRunner()
        out = self.run_patrol(fr)
        f = [x for x in out["findings"] if x["code"] == "parallel_signal"]
        self.assertEqual((f[0]["signal"], f[0]["count"]), ("failures", 2))
        self.assertTrue(any("可并行" in t and "2 个不同的失败" in t for t in self.texts(fr)))
        fr2 = FakeRunner()
        self.run_patrol(fr2, minutes_later=15)
        self.assertFalse(any("可并行" in t for t in self.texts(fr2)))

    def test_single_failure_or_idle_slots_do_not_wake(self):
        self.add(deliver("x", 30, outcome="FAIL", failures=["test_a 断言失败"], actor=imported()))
        self.put_live(live())
        fr = FakeRunner()
        out = self.run_patrol(fr)
        self.assertFalse(any(x["code"] == "parallel_signal" for x in out["findings"]))
        self.assertFalse(any("可并行" in t for t in self.texts(fr)))

    def test_candidates_signal_and_acted_on(self):
        self.add(deliver("x", 30, candidates=["方案 A", "方案 B", "方案 C"]))
        self.put_live(live())
        out = self.run_patrol(FakeRunner(), apply=False)
        self.assertTrue(any(x["code"] == "parallel_signal" and x["signal"] == "candidates" for x in out["findings"]))
        self.add(prop_ev(proposal(("c", "parallel", [child("ca"), child("cb")])), minutes_ago=10))
        out = self.run_patrol(FakeRunner(), apply=False)
        self.assertFalse(any(x["code"] == "parallel_signal" for x in out["findings"]))

    def test_reviewed_delivery_is_not_hinted(self):
        # 11-06: the router reviewed the delivery (and declined to split) before patrol saw it
        self.add(deliver("x", 30, outcome="PARTIAL", candidates=["方案 A", "方案 B"], actor=imported()),
                 {"kind": "node_review", "line": "L", "node": "x", "outcome": "PARTIAL", "summary": "不拆",
                  "at": at(20), "actor": {"role": "router", "handle": "term_router"}})
        self.put_live(live())
        fr = FakeRunner()
        out = self.run_patrol(fr)
        self.assertFalse(any(x["code"] == "parallel_signal" for x in out["findings"]))
        self.assertFalse(any("可并行" in t for t in self.texts(fr)))

    def test_signal_goes_to_owning_subrouter(self):
        self.add(prop_ev(proposal(("p", "serial", [child("p1", kind="plan", minutes=300, nodes=6)])), minutes_ago=110),
                 dispatch("p1", 100, terminal=SUB))
        self.add(prop_ev(proposal(("a", "serial", [child("y")]), parent="p1"), actor=SUB, minutes_ago=90),
                 dispatch("y", 80), deliver("y", 20, outcome="FAIL", failures=["e1", "e2 other"], actor=imported()))
        lv = live()
        lv["router_tree"] = [{"handle": SUB, "kind": "sub", "present": True}]
        self.put_live(lv)
        fr = FakeRunner()
        self.run_patrol(fr)
        sends = [(c[c.index("--terminal") + 1], c[c.index("--text") + 1]) for c in fr.kinds("send")]
        self.assertTrue(any(h == SUB and "可并行" in t for h, t in sends), sends)


class Collapse(GBase):
    def setUp(self):
        super().setUp()
        self.add(prop_ev(proposal(("a", "parallel", [child("x"), child("y"), child("z")]))),
                 dispatch("x", 100), dispatch("y", 100, terminal="term_w2"))

    def test_same_failure_collapse_holds_segment_and_tells_router(self):
        self.add(deliver("x", 30, outcome="FAIL", failures=["ImportError: no module foo 12"], actor=imported()),
                 {"kind": "node_block", "line": "L", "node": "y", "blocker": "env", "summary": "卡",
                  "at": at(20), "actor": {"role": "worker", "handle": "term_w2"},
                  "failures": ["ImportError: no module foo 99"]})
        self.put_live(live())
        fr = FakeRunner()
        out = self.run_patrol(fr)
        c = [x for x in out["findings"] if x["code"] == "collapse"]
        self.assertEqual(c[0]["nodes"], ["x", "y"])
        self.assertEqual(fr.kinds("worker-start"), [])  # z held
        texts = [s[s.index("--text") + 1] for s in fr.kinds("send")]
        self.assertTrue(any("并行塌缩" in t for t in texts))
        fr2 = FakeRunner()
        self.run_patrol(fr2, minutes_later=30)
        self.assertFalse(any("并行塌缩" in s[s.index("--text") + 1] for s in fr2.kinds("send")))

    def test_overlapping_touched_paths(self):
        self.add({"kind": "node_heartbeat", "line": "L", "node": "x", "summary": "在改", "at": at(30),
                  "actor": {"role": "worker", "handle": "term_new-worker"}, "touched": ["src/shared/util.py"]},
                 {"kind": "node_heartbeat", "line": "L", "node": "y", "summary": "在改", "at": at(30),
                  "actor": {"role": "worker", "handle": "term_w2"}, "touched": ["src/shared"]})
        self.put_live(live())
        out = self.run_patrol(FakeRunner(), apply=False)
        self.assertTrue(any(x["code"] == "collapse" and "改了同一处" in x["why"][0] for x in out["findings"]))

    def test_same_screen_error(self):
        w = lambda term, node: {"terminal": term, "line": "L", "node": node,
                                "screen": {"flag": "error", "line": "Error: CUDA OOM at step 3", "since": at(2)}}
        self.put_live(live(workers=[w("term_new-worker", "x"), w("term_w2", "y")]))
        out = self.run_patrol(FakeRunner(), apply=False)
        self.assertTrue(any(x["code"] == "collapse" for x in out["findings"]))

    def test_independent_failures_no_collapse(self):
        self.add(deliver("x", 30, outcome="FAIL", failures=["test_a"], actor=imported()),
                 deliver("y", 30, outcome="FAIL", failures=["test_b"], actor=imported("term_w2")))
        self.put_live(live())
        out = self.run_patrol(FakeRunner(), apply=False)
        self.assertFalse(any(x["code"] == "collapse" for x in out["findings"]))


class Release(GBase):
    def setUp(self):
        super().setUp()
        self.add(prop_ev(proposal(("s", "parallel", [child("x"), child("y")]))),
                 dispatch("x", 100, task="task_x", disp="ctx_x"))

    def releases(self, fr):
        return fr.kinds("worker-release")

    def test_released_after_delivery_and_settled(self):
        self.add(deliver("x", 10))
        self.put_live(finished_live(task_x="succeeded"))
        fr = FakeRunner()
        out = self.run_patrol(fr)
        rel = self.releases(fr)
        self.assertEqual([c[1:] for c in rel], [["orchestration", "worker-release", "--dispatch", "ctx_x", "--json"]])
        ev = self.events("worker_release")
        self.assertEqual((ev[0]["node"], ev[0]["released"], ev[0]["result"]), ("x", True, "released"))
        self.assertTrue(any(r["type"] == "worker_release" for r in out["results"]))
        fr2 = FakeRunner()
        self.run_patrol(fr2, minutes_later=15)
        self.assertEqual(self.releases(fr2), [])  # final: not repeated

    def test_dry_run_does_not_release(self):
        self.add(deliver("x", 10))
        self.put_live(finished_live(task_x="succeeded"))
        fr = FakeRunner()
        out = self.run_patrol(fr, apply=False)
        self.assertEqual(self.releases(fr), [])
        self.assertTrue(any(a["type"] == "worker_release" for a in out["actions"]))
        self.assertEqual(self.events("worker_release"), [])

    def test_not_delivered_or_not_settled(self):
        self.put_live(finished_live(task_x="succeeded"))
        fr = FakeRunner()
        self.run_patrol(fr)
        self.assertEqual(self.releases(fr), [])  # finished in Orca but no deliver
        self.add(deliver("x", 10))
        self.put_live(finished_live(task_x="running"))
        fr = FakeRunner()
        self.run_patrol(fr)
        self.assertEqual(self.releases(fr), [])  # delivered but dispatch not settled

    def test_never_routers_or_plan_nodes(self):
        self.add(deliver("x", 10, handle="term_new-worker"))
        lv = finished_live(task_x="succeeded")
        lv["router_tree"] = [{"handle": "term_new-worker", "kind": "sub", "present": True}]
        self.put_live(lv)
        fr = FakeRunner()
        self.run_patrol(fr)
        self.assertEqual(self.releases(fr), [])
        # a plan node (sub-router) is never released even when delivered
        self.setUp()
        self.add(prop_ev(proposal(("p", "serial", [child("p1", kind="plan", minutes=60, nodes=1, scope=["src/p"])])),
                         minutes_ago=150),
                 dispatch("p1", 100, terminal=SUB, task="task_p", disp="ctx_p"),
                 {"kind": "node_deliver", "line": "L", "node": "p1", "outcome": "PASS", "summary": "子图完成",
                  "at": at(10), "actor": {"role": "router", "handle": R}})
        self.put_live(finished_live(task_p="succeeded"))
        fr = FakeRunner()
        self.run_patrol(fr)
        self.assertEqual(self.releases(fr), [])

    def test_rate_limited_per_round(self):
        core.save_project(gproject(max_parallel=10))
        self.add(prop_ev(proposal(("m", "parallel", [child("r%d" % i) for i in range(5)])), minutes_ago=150))
        tasks = {}
        for i in range(5):
            self.add(dispatch("r%d" % i, 100, task="task_r%d" % i, disp="ctx_r%d" % i), deliver("r%d" % i, 10))
            tasks["task_r%d" % i] = "succeeded"
        self.put_live(finished_live(**tasks))
        fr = FakeRunner()
        out = self.run_patrol(fr)
        self.assertEqual(len(self.releases(fr)), 3)
        held = [x for x in out["findings"] if x["code"] == "release_rate_limited"]
        self.assertEqual([(len(held), held[0]["count"])], [(1, 2)])

    def test_reclaimable_list_filters_already_released(self):
        self.add(deliver("x", 10))
        lv = finished_live(task_x="succeeded")
        lv["reclaimable"] = []          # Orca: nothing left to reclaim (already auto-released)
        self.put_live(lv)
        fr = FakeRunner()
        self.run_patrol(fr)
        self.assertEqual(self.releases(fr), [])
        lv["reclaimable"] = ["ctx_x"]
        self.put_live(lv)
        self.run_patrol(fr, minutes_later=15)
        self.assertEqual(len(self.releases(fr)), 1)

    def test_release_unknown_retried_once_then_stops(self):
        self.add(deliver("x", 10))
        self.put_live(finished_live(task_x="succeeded"))
        bad = {"ok": False, "error": {"code": "release_unknown"}}
        fr = FakeRunner(release=bad, release_rc=1)
        self.run_patrol(fr)
        self.run_patrol(fr, minutes_later=15)
        self.run_patrol(fr, minutes_later=30)
        self.assertEqual(len(self.releases(fr)), patrol_mod().RELEASE_TRIES)
        self.assertEqual([e["released"] for e in self.events("worker_release")], [False, False])

    def test_stale_live_no_release(self):
        self.add(deliver("x", 10))
        lv = finished_live(task_x="succeeded")
        lv["generated_at"] = at(90)
        self.put_live(lv)
        fr = FakeRunner()
        self.run_patrol(fr)
        self.assertEqual(self.releases(fr), [])


def patrol_mod():
    import patrol
    return patrol


class Metrics(unittest.TestCase):
    def test_review_run_ratio_and_stall(self):
        nodes = {
            "a": {"id": "a", "kind": "run", "status": "delivered", "dispatched_at": at(200), "delivered_at": at(100),
                  "history": [{"kind": "node_dispatch", "at": at(200)}], "deps": []},
            "a_check": {"id": "a_check", "kind": "check", "status": "delivered", "dispatched_at": at(90),
                        "delivered_at": at(60), "history": [{"kind": "node_dispatch", "at": at(90)}], "deps": ["a"]},
            "b": {"id": "b", "kind": "run", "status": "running", "dispatched_at": at(30),
                  "history": [{"kind": "node_dispatch", "at": at(30)}], "deps": ["a"]},
        }
        m = graph.line_metrics(nodes, NOW)
        self.assertEqual((m["review_run"]["review_min"], m["review_run"]["run_min"]), (30, 130))
        self.assertTrue(m["review_run"]["over"])
        self.assertEqual(m["critical_path"]["stall_min"], 0)
        self.assertEqual(m["ready_undispatched"]["nodes"], 0)

    def test_expanded_plan_waits_on_children(self):
        nodes = {
            "p": {"id": "p", "kind": "plan", "status": "running", "proposals": ["ev1"], "deps": [], "history": []},
            "c1": {"id": "c1", "kind": "run", "status": "ready", "parent": "p", "proposal": "ev1", "deps": [],
                   "expected_duration_minutes": 60, "history": []},
            "after": {"id": "after", "kind": "run", "status": "planned", "deps": ["p"], "history": [],
                      "expected_duration_minutes": 30},
        }
        self.assertEqual(graph.critical_path(nodes), ["c1", "p", "after"])

    def test_snapshot_carries_metrics(self):
        import snapshot as snap
        p = project()
        st = core.fold(p, [])
        self.assertIn("L", snap.scheduling_metrics(p, st, NOW))
        self.assertEqual(snap.scheduling_metrics(p, None, NOW), {})


if __name__ == "__main__":
    unittest.main()
