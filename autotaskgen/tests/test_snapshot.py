import contextlib
import datetime as dt
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import progress_core as core  # noqa: E402
import snapshot as snap  # noqa: E402
from fakes import FakeRunner, wl_row, show_result  # noqa: E402

NOW = dt.datetime(2026, 11, 4, 12, 0, 0, tzinfo=dt.timezone.utc)
R = "term_7d3ec177-ea54-c7b4-96ab-786e3c5a0cf3"

PS = """\
  2217   3600 node /home/x/.npm-global/bin/codex --no-daemon -c shell_environment_policy.set.ORCA_WORKSPACE_ID='615b::/home/x/ws/core1-bd-r1' -c shell_environment_policy.set.ORCA_TERMINAL_HANDLE='term_326744e7-93bd' --dangerously-bypass-approvals-and-sandbox -m codex/gpt-6-luna -c model_reasoning_effort=high
  2225   3599 /home/x/.npm-global/lib/node_modules/@openai/codex/vendor/bin/codex --no-daemon -c shell_environment_policy.set.ORCA_WORKSPACE_ID='615b::/home/x/ws/core1-bd-r1' -c shell_environment_policy.set.ORCA_TERMINAL_HANDLE='term_326744e7-93bd' -m codex/gpt-6-luna -c model_reasoning_effort=high
  3000    120 node /home/x/.npm-global/bin/codex -c shell_environment_policy.set.ORCA_WORKSPACE_ID='615b::/home/x/ws/stray' -c shell_environment_policy.set.ORCA_TERMINAL_HANDLE='term_stray-1' -m codex/gpt-6.1-sol -c model_reasoning_effort=xhigh
  3100     10 grep ORCA_WORKSPACE_ID
  4000     50 bash -c sleep 1
"""


def project():
    return {"schema": core.SCHEMA_PROJECT, "id": "t", "name": "t", "final_goal": {"text": "g"},
            "cores": [{"id": "c1", "name": "c", "coordinator": "term_c-1"}],
            "lines": [{"id": "EVB", "name": "e", "core": "c1", "router": "term_7d3ec177", "patrol": True},
                      {"id": "GONE", "name": "g", "core": "c1", "router": "term_dead-0000", "patrol": True},
                      {"id": "NOR", "name": "n", "core": "c1"}],
            "milestones": []}


TERMS = [{"handle": R, "connected": True, "lastOutputAt": 1793780484523, "preview": "› Ask Codex"},
         {"handle": "term_other", "connected": True, "lastOutputAt": 1}]


def state_with_dispatch():
    evs = [{"kind": "node_plan", "line": "EVB", "node": "bd", "summary": "p", "at": "2026-11-04T01:00:00Z",
            "actor": {"role": "router", "handle": R}},
           {"kind": "node_dispatch", "line": "EVB", "node": "bd", "summary": "d", "at": "2026-11-04T02:00:00Z",
            "actor": {"role": "router", "handle": R}, "refs": {"worktree": "core1-bd-r1"}}]
    return core.fold(project(), evs)


class Parse(unittest.TestCase):
    def test_ps_dedupe_and_fields(self):
        ws = snap.parse_ps(PS, NOW)
        self.assertEqual(len(ws), 2)
        w = ws[0]
        self.assertEqual((w["pid"], w["worktree_name"], w["terminal"], w["model"], w["effort"]),
                         (2217, "core1-bd-r1", "term_326744e7-93bd", "codex/gpt-6-luna", "high"))
        self.assertEqual(w["started_at"], "2026-11-04T11:00:00Z")

    def test_ps_empty(self):
        self.assertEqual(snap.parse_ps("", NOW), [])

    def test_attribution(self):
        ws = snap.parse_ps(PS, NOW)
        snap.attribute(ws, state_with_dispatch())
        self.assertEqual((ws[0]["line"], ws[0]["node"]), ("EVB", "bd"))
        self.assertEqual((ws[1]["line"], ws[1]["node"]), (None, None))

    def test_screen_markers(self):
        f = snap.marker_screen_check
        self.assertEqual(f(["■ stream disconnected before completion"], None, NOW), "stream_error")
        self.assertEqual(f(["Do you want to run this command?"], None, NOW), "waiting_input")
        self.assertIsNone(f(["• Worked for 54s", "› Ask Codex to do anything"], None, NOW))
        self.assertEqual(f(["• Working (40m • esc to interrupt)"], "2026-11-04T10:00:00Z", NOW), "stuck")
        old = ["stream disconnected"] + ["ok line %d" % i for i in range(20)]
        self.assertIsNone(f(old, None, NOW))  # scrolled away

    def test_malformed_json(self):
        self.assertIsNone(snap.parse_json_tail("not json\nstill not"))
        self.assertEqual(snap.parse_json_tail('noise\n{"ok": true}'), {"ok": True})


class Collect(unittest.TestCase):
    def test_full(self):
        fr = FakeRunner(terminals=TERMS, ps=PS, screen=["■ stream disconnected"])
        doc = snap.collect(project(), state_with_dispatch(), fr, now=NOW)
        self.assertEqual(doc["hosts"], {"mac": "ok", "wsl": "ok", "ib-agent": "unknown"})
        self.assertEqual(set(doc["routers"]), {"EVB", "GONE"})
        self.assertTrue(doc["routers"]["EVB"]["present"])
        self.assertEqual(doc["routers"]["EVB"]["handle"], R)
        self.assertEqual(doc["routers"]["EVB"]["screen_flag"], "stream_error")
        self.assertFalse(doc["routers"]["GONE"]["present"])
        s = snap.summary(doc)
        self.assertEqual((s["routers_present"], s["workers_found"], s["workers_attributed"]), (1, 2, 1))

    def test_wsl_unreachable_is_unknown_not_zero(self):
        fr = FakeRunner(terminals=TERMS, ps_rc=255)
        doc = snap.collect(project(), None, fr, now=NOW)
        self.assertEqual(doc["hosts"]["wsl"], "unknown")
        self.assertIsNone(doc["workers"])
        self.assertIsNone(snap.summary(doc)["workers_found"])
        self.assertTrue(doc["errors"])

    def test_orca_down_routers_unknown(self):
        fr = FakeRunner(orca_rc=1, ps=PS)
        doc = snap.collect(project(), None, fr, now=NOW)
        self.assertEqual(doc["hosts"]["mac"], "unknown")
        self.assertIsNone(doc["routers"]["EVB"]["present"])
        self.assertEqual(snap.summary(doc)["routers_absent"], [])

    def test_empty_project(self):
        p = project()
        p["lines"] = []
        doc = snap.collect(p, None, FakeRunner(ps=""), now=NOW)
        self.assertEqual((doc["routers"], doc["workers"]), ({}, []))

    def test_external_classifier_only_when_markers_silent(self):
        calls = []

        def judge(tail, last, now):
            calls.append(tail)
            return "stuck"
        fr = FakeRunner(terminals=TERMS, ps="", screen=["Do you want to continue?"])
        doc = snap.collect(project(), None, fr, now=NOW, screen_check=judge)
        self.assertEqual(doc["routers"]["EVB"]["screen_flag"], "waiting_input")
        self.assertEqual(calls, [])
        fr = FakeRunner(terminals=TERMS, ps="", screen=["› Ask Codex"])
        doc = snap.collect(project(), None, fr, now=NOW, screen_check=judge)
        self.assertEqual(doc["routers"]["EVB"]["screen_flag"], "stuck")


def state_with_task(task="task_A", dispatch=None):
    refs = {"task": task}
    if dispatch:
        refs["dispatch"] = dispatch
    evs = [{"kind": "node_plan", "line": "EVB", "node": "imp", "summary": "p", "at": "2026-11-04T01:00:00Z",
            "actor": {"role": "router", "handle": R}},
           {"kind": "node_dispatch", "line": "EVB", "node": "imp", "summary": "d", "at": "2026-11-04T02:00:00Z",
            "actor": {"role": "router", "handle": R}, "refs": refs}]
    return core.fold(project(), evs)


WS = "615b::/home/x/ws/core1-bd-r1"
TERM = "term_326744e7-93bd"
PS_ONE = PS.split("\n")[0] + "\n"


class Orca(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("PROGRESS_CACHE_DIR")
        os.environ["PROGRESS_CACHE_DIR"] = self.tmp.name

    def tearDown(self):
        if self.old is None:
            os.environ.pop("PROGRESS_CACHE_DIR", None)
        else:
            os.environ["PROGRESS_CACHE_DIR"] = self.old
        self.tmp.cleanup()

    def run_collect(self, state, proj=None, **kw):
        fr = FakeRunner(terminals=TERMS, ps=PS_ONE, **kw)
        return snap.collect(proj or project(), state, fr, now=NOW, screen=False), fr

    def attr_case(self, run, proj=None):
        rows = [wl_row("ctx_2", "task_ZZ", "run_9", terminal=TERM, workspace=WS)]
        doc, fr = self.run_collect(state_with_task(), proj=proj, wl_rows={"active": rows}, runs=[run],
                                   shows={"ctx_2": show_result()})
        return doc["workers"][0], fr

    def test_attribution_by_run_id(self):
        p = project()
        p["lines"][1]["run"] = "run_9"
        w, fr = self.attr_case({"id": "run_9", "objective": "whatever"}, p)
        self.assertEqual((w["line"], w["node"], w["attribution"], w["core"]), ("GONE", None, "run", "c1"))
        self.assertTrue(any("run-list" in c and "--full" in c for c in fr.calls))

    def test_attribution_by_run_coordinator_equals_router(self):
        w, _ = self.attr_case({"id": "run_9", "objective": "x", "coordinator_handle": "term_dead-0000-full"})
        self.assertEqual((w["line"], w["attribution"], w["core"]), ("GONE", "run", "c1"))

    def test_run_under_core_coordinator_sets_router_and_core_only(self):
        w, _ = self.attr_case({"id": "run_9", "objective": "x", "coordinator_handle": "term_c-1-full"})
        self.assertEqual((w["line"], w["node"], w["attribution"], w["core"], w["router"]),
                         (None, None, "run", "c1", w["router"]))
        self.assertTrue(w["router"].startswith("term_c-1"))

    def test_attribution_nothing_matches(self):
        w, _ = self.attr_case({"id": "run_9", "objective": "zzz", "coordinator_handle": "term_nobody"})
        self.assertEqual((w["line"], w["node"], w["attribution"], w["core"]), (None, None, None, None))

    def test_run_without_coordinator_field_tolerated(self):
        w, _ = self.attr_case({"id": "run_9"})
        self.assertEqual((w["line"], w["attribution"], w["core"]), (None, None, None))

    def test_core_set_for_terminal_attribution(self):
        doc, _ = self.run_collect(state_with_dispatch())
        self.assertEqual((doc["workers"][0]["line"], doc["workers"][0]["core"]), ("EVB", "c1"))

    def test_run_list_failure_keeps_attribution_unchanged(self):
        rows = [wl_row("ctx_2", "task_ZZ", "run_9", terminal=TERM, workspace=WS)]
        doc, _ = self.run_collect(state_with_task(), runs_rc=1, wl_rows={"active": rows},
                                  shows={"ctx_2": show_result()})
        w = doc["workers"][0]
        self.assertEqual((w["line"], w["attribution"], w["core"]), (None, None, None))

    def test_attribution_by_task(self):
        rows = [wl_row("ctx_1", "task_A", "run_1", terminal=TERM, workspace=WS)]
        doc, _ = self.run_collect(state_with_task(), wl_rows={"retained": rows},
                                  shows={"ctx_1": show_result()})
        w = doc["workers"][0]
        self.assertEqual((w["line"], w["node"], w["attribution"]), ("EVB", "imp", "task"))
        self.assertEqual(w["task"]["id"], "task_A")
        self.assertEqual(w["task"]["dispatched_at"], "2026-11-04T06:08:53Z")

    def test_attribution_terminal_and_worktree_keep_priority(self):
        rows = [wl_row("ctx_1", "task_A", terminal=TERM, workspace=WS)]
        doc, _ = self.run_collect(state_with_dispatch(), wl_rows={"active": rows})
        self.assertEqual(doc["workers"][0]["attribution"], "worktree")
        doc, _ = self.run_collect(state_with_dispatch())
        self.assertEqual(doc["workers"][0]["attribution"], "worktree")
        self.assertIsNone(doc["workers"][0]["task"])

    def test_attribution_by_workspace_when_terminal_unknown(self):
        rows = [wl_row("ctx_1", "task_A", terminal="term_other-9", workspace="615b::/home/x/ws/core1-bd-r1")]
        doc, _ = self.run_collect(state_with_task(), wl_rows={"active": rows}, shows={"ctx_1": show_result()})
        self.assertEqual(doc["workers"][0]["attribution"], "task")

    def test_attribution_by_run_prefix_line_only(self):
        rows = [wl_row("ctx_2", "task_ZZ", "run_7", terminal=TERM, workspace=WS)]
        runs = [{"id": "run_7", "objective": "EVB\uff1a\u4e8b\u4ef6\u7ea7\u57fa\u7ebf"}]
        doc, _ = self.run_collect(state_with_task(), wl_rows={"active": rows}, runs=runs,
                                  shows={"ctx_2": show_result()})
        w = doc["workers"][0]
        self.assertEqual((w["line"], w["node"], w["attribution"]), ("EVB", None, "run"))
        runs = [{"id": "run_7", "objective": "\u6838\u5fc3\u4e00\u534f\u8c03\u4eba\u76f4\u6d3e\uff1a\u4ec0\u4e48"}]
        doc, _ = self.run_collect(state_with_task(), wl_rows={"active": rows}, runs=runs,
                                  shows={"ctx_2": show_result()})
        self.assertEqual((doc["workers"][0]["line"], doc["workers"][0]["attribution"]), (None, None))

    def test_lingering(self):
        done = [wl_row("ctx_1", "task_A", terminal=TERM, workspace=WS, state="succeeded", status="completed")]
        doc, _ = self.run_collect(None, wl_rows={"retained": done},
                                  shows={"ctx_1": show_result("succeeded", "completed", completed="2026-11-04T07:00:00.000Z")})
        w = doc["workers"][0]
        self.assertTrue(w["lingering"])
        self.assertEqual(w["task"]["completed_at"], "2026-11-04T07:00:00Z")
        self.assertEqual(snap.summary(doc)["workers_lingering"], 1)
        ready = [wl_row("ctx_1", "task_B", terminal=TERM, workspace=WS)]
        doc, _ = self.run_collect(None, wl_rows={"retained": ready}, shows={"ctx_1": show_result()})
        self.assertIs(doc["workers"][0]["lingering"], False)

    # ---- worker screens
    def screen_case(self, tail, now=NOW, ps=None, **kw):
        rows = [wl_row("ctx_1", "task_A", terminal=TERM, workspace=WS)]
        reads = {"ctx_1": dict(tail=tail, **kw)}
        fr = FakeRunner(terminals=TERMS, ps=ps or PS_ONE, wl_rows={"active": rows}, shows={"ctx_1": show_result()},
                        reads=reads)
        return snap.collect(project(), state_with_task(), fr, now=now, screen=False), fr

    IDLE_TAIL = ["  \u2514 Read x.py", "\u25a0 {\"error\":{\"message\":\"Codex: Bad Request\"}}", "\u203a Ask Codex to do anything"]
    QUIET_TAIL = ["done reading", "\u203a Ask Codex to do anything", "  ? for shortcuts"]

    def test_screen_error(self):
        doc, fr = self.screen_case(self.IDLE_TAIL)
        sc = doc["workers"][0]["screen"]
        self.assertEqual(sc["flag"], "error")
        self.assertIn("Bad Request", sc["line"])
        self.assertLessEqual(len(sc["line"]), 160)
        self.assertEqual(len(fr.kinds("worker-read")), 1)

    def test_screen_working_is_clean(self):
        doc, _ = self.screen_case(["\u2022 Checking (9m 27s \u2022 esc to interrupt)", "stream error earlier"])
        self.assertIsNone(doc["workers"][0]["screen"]["flag"])

    def test_screen_idle_needs_ten_unchanged_minutes(self):
        doc, _ = self.screen_case(self.QUIET_TAIL)
        self.assertIsNone(doc["workers"][0]["screen"]["flag"])
        doc, _ = self.screen_case(self.QUIET_TAIL, now=NOW + dt.timedelta(minutes=5))
        self.assertIsNone(doc["workers"][0]["screen"]["flag"])
        doc, _ = self.screen_case(self.QUIET_TAIL, now=NOW + dt.timedelta(minutes=11))
        sc = doc["workers"][0]["screen"]
        self.assertEqual(sc["flag"], "idle")
        self.assertEqual(sc["since"], "2026-11-04T12:00:00Z")
        self.assertEqual(sc["line"], "done reading")

    def test_screen_changed_tail_resets_idle(self):
        self.screen_case(self.QUIET_TAIL)
        doc, _ = self.screen_case(self.QUIET_TAIL + ["new output"], now=NOW + dt.timedelta(minutes=11))
        self.assertIsNone(doc["workers"][0]["screen"]["flag"])

    def test_screen_exited(self):
        doc, _ = self.screen_case(self.QUIET_TAIL, liveness="gone", terminal="exited")
        self.assertEqual(doc["workers"][0]["screen"]["flag"], "exited")

    def test_screen_skips_young_and_finished(self):
        young = PS_ONE.replace("   3600 ", "    300 ", 1)
        doc, fr = self.screen_case(self.IDLE_TAIL, ps=young)
        self.assertEqual(fr.kinds("worker-read"), [])
        self.assertNotIn("screen", doc["workers"][0])
        done = [wl_row("ctx_1", "task_A", terminal=TERM, workspace=WS, state="succeeded", status="completed")]
        fr = FakeRunner(terminals=TERMS, ps=PS_ONE, wl_rows={"retained": done},
                        shows={"ctx_1": show_result("succeeded", "completed", completed="2026-11-04T07:00:00Z")},
                        reads={"ctx_1": {"tail": self.IDLE_TAIL}})
        snap.collect(project(), state_with_task(), fr, now=NOW, screen=False)
        self.assertEqual(fr.kinds("worker-read"), [])

    def test_screen_failure_goes_to_errors(self):
        rows = [wl_row("ctx_1", "task_A", terminal=TERM, workspace=WS)]
        fr = FakeRunner(terminals=TERMS, ps=PS_ONE, wl_rows={"active": rows}, shows={"ctx_1": show_result()},
                        read_rc=1)
        doc = snap.collect(project(), state_with_task(), fr, now=NOW, screen=False)
        self.assertTrue(any("worker-read" in e["error"] for e in doc["errors"]))
        self.assertNotIn("screen", doc["workers"][0])

    def test_screen_cap(self):
        ws = [{"task": {"id": "t%d" % i, "dispatch": "d%d" % i, "state": "ready"},
               "started_at": "2026-11-04T10:00:00Z"} for i in range(5)]
        fr = FakeRunner(reads={"d%d" % i: {"tail": ["x"]} for i in range(5)})
        errors = []
        snap.collect_worker_screens(ws, fr, "orca", errors, NOW, cap=2)
        self.assertEqual(len(fr.kinds("worker-read")), 2)
        self.assertTrue(any("上限" in e["error"] for e in errors))

    def test_worker_list_failure_is_unknown(self):
        doc, _ = self.run_collect(state_with_task(), wl_rc=1, runs=[{"id": "run_1"}])
        w = doc["workers"][0]
        self.assertEqual((w["task"], w["lingering"], w["attribution"]), (None, None, None))
        self.assertTrue(any("worker-list" in e["error"] for e in doc["errors"]))
        self.assertEqual(doc["tasks"], {})

    def test_malformed_page_does_not_crash(self):
        doc, _ = self.run_collect(None, wl_rows={"active": "garbage"})
        self.assertTrue(any("worker-list" in e["error"] for e in doc["errors"]))
        self.assertIsNone(doc["workers"][0]["lingering"])

    def test_run_list_failure_recorded(self):
        doc, _ = self.run_collect(None, runs_rc=1)
        self.assertTrue(any("run-list" in e["error"] for e in doc["errors"]))

    def test_worker_show_failure_keeps_state_without_times(self):
        rows = [wl_row("ctx_1", "task_A", terminal=TERM, workspace=WS, state="succeeded", status="completed")]
        doc, _ = self.run_collect(state_with_task(), wl_rows={"retained": rows}, show_rc=1)
        self.assertEqual(doc["tasks"]["task_A"], {"state": "succeeded", "dispatched_at": None, "completed_at": None})
        self.assertTrue(any("worker-show" in e["error"] for e in doc["errors"]))

    def test_paging(self):
        rows = [wl_row("ctx_%d" % i, "task_%d" % i, terminal="term_zz%d" % i) for i in range(5)]
        rows.append(wl_row("ctx_w", "task_A", terminal=TERM, workspace=WS))
        doc, fr = self.run_collect(state_with_task(), wl_rows={"retained": rows}, wl_page=2,
                                   shows={"ctx_w": show_result()})
        self.assertEqual(doc["workers"][0]["attribution"], "task")
        self.assertEqual(len(fr.kinds("worker-list")), 3)
        self.assertTrue(all("--run" in c and "--terminal-state" not in c for c in fr.kinds("worker-list")))

    def test_queries_every_run_with_run_flag(self):
        rows = [wl_row("ctx_1", "task_A", "run_a", terminal=TERM, workspace=WS),
                wl_row("ctx_2", "task_B", "run_b", terminal="term_zz")]
        _, fr = self.run_collect(state_with_task(), wl_rows={"retained": rows},
                                 runs=[{"id": "run_a"}, {"id": "run_b"}, {"id": "run_c"}],
                                 shows={"ctx_1": show_result(), "ctx_2": show_result()})
        got = sorted(c[c.index("--run") + 1] for c in fr.kinds("worker-list"))
        self.assertEqual(got, ["run_a", "run_b", "run_c"])

    def test_index_filter_semantics(self):
        rows = [wl_row("c_rel", "t1", "run_a", state="succeeded", status="completed", tstate="released"),
                wl_row("c_nul", "t2", "run_a", state="succeeded", status="completed", tstate=None),
                wl_row("c_live", "t3", "run_a", state="ready", tstate=None),
                wl_row("c_ret", "t4", "run_a", state="succeeded", status="completed", tstate="retained"),
                wl_row("c_act", "t5", "run_a", state="succeeded", status="completed", tstate="active")]
        fr = FakeRunner(wl_rows={"x": rows}, runs=[{"id": "run_a"}])
        errs = []
        got, complete = snap.fetch_dispatch_index(fr, "orca", errs, ["run_a"])
        self.assertEqual(sorted(r["dispatch"] for r in got), ["c_act", "c_live", "c_ret"])
        self.assertTrue(complete)
        self.assertEqual(errs, [])

    def test_index_dedupes_and_flags_failed_run(self):
        a = wl_row("c1", "t1", "run_a")
        fr = FakeRunner(wl_rows={"x": [a, a]}, runs=[{"id": "run_a"}])
        errs = []
        got, complete = snap.fetch_dispatch_index(fr, "orca", errs, ["run_a"])
        self.assertEqual(len(got), 1)
        fr = FakeRunner(wl_rc=1)
        got, complete = snap.fetch_dispatch_index(fr, "orca", errs, ["run_a"])
        self.assertFalse(complete)
        self.assertTrue(any("run run_a" in e["error"] for e in errs))

    def test_run_list_failure_falls_back_to_unscoped(self):
        rows = [wl_row("ctx_1", "task_A", terminal=TERM, workspace=WS)]
        doc, fr = self.run_collect(state_with_task(), runs_rc=1, wl_rows={"retained": rows},
                                   shows={"ctx_1": show_result()})
        self.assertTrue(all("--run" not in c for c in fr.kinds("worker-list")))
        self.assertTrue(all("--terminal-state" in c for c in fr.kinds("worker-list")))
        self.assertEqual(doc["workers"][0]["task"]["id"], "task_A")
        self.assertFalse(any("绑定" in e["error"] for e in doc["errors"]))

    def test_bound_scope_in_fallback_is_incomplete(self):
        fr = FakeRunner(wl_rows={"retained": [wl_row("c1", "t1")]}, wl_bound=True)
        errs = []
        got, complete = snap.fetch_dispatch_index(fr, "orca", errs, None)
        self.assertFalse(complete)
        self.assertEqual(len(got), 1)
        self.assertEqual(len([e for e in errs if "绑定" in e["error"]]), 1)

    def test_cache_hit_avoids_worker_show(self):
        rows = [wl_row("ctx_1", "task_A", state="succeeded", status="completed")]
        shows = {"ctx_1": show_result("succeeded", "completed", completed="2026-11-04 07:00:00")}
        doc, fr = self.run_collect(state_with_task(), wl_rows={"retained": rows}, shows=shows)
        self.assertEqual(len(fr.kinds("worker-show")), 1)
        self.assertEqual(doc["tasks"]["task_A"]["completed_at"], "2026-11-04T07:00:00Z")
        doc, fr = self.run_collect(state_with_task(), wl_rows={"retained": rows}, shows=shows)
        self.assertEqual(fr.kinds("worker-show"), [])
        self.assertEqual(doc["tasks"]["task_A"]["dispatched_at"], "2026-11-04T06:08:53Z")

    def test_unsettled_is_requeried(self):
        rows = [wl_row("ctx_1", "task_A")]
        _, fr = self.run_collect(state_with_task(), wl_rows={"retained": rows}, shows={"ctx_1": show_result()})
        _, fr = self.run_collect(state_with_task(), wl_rows={"retained": rows}, shows={"ctx_1": show_result()})
        self.assertEqual(len(fr.kinds("worker-show")), 1)

    def test_node_dispatch_ref_used_without_index(self):
        doc, fr = self.run_collect(state_with_task("task_B", dispatch="ctx_b"),
                                   shows={"ctx_b": show_result("failed", "failed", completed="2026-11-04T05:00:00Z")})
        self.assertEqual(doc["tasks"]["task_B"]["state"], "failed")

    def test_task_without_dispatch_gets_no_entry(self):
        doc, fr = self.run_collect(state_with_task("task_nowhere"))
        self.assertNotIn("task_nowhere", doc["tasks"])
        self.assertEqual(fr.kinds("worker-show"), [])

    def test_per_run_cap(self):
        evs = []
        for i in range(5):
            evs.append({"kind": "node_plan", "line": "EVB", "node": "n%d" % i, "summary": "p",
                        "at": "2026-11-04T01:00:00Z", "actor": {"role": "router", "handle": R}})
            evs.append({"kind": "node_dispatch", "line": "EVB", "node": "n%d" % i, "summary": "d",
                        "at": "2026-11-04T02:0%d:00Z" % i, "actor": {"role": "router", "handle": R},
                        "refs": {"task": "task_%d" % i, "dispatch": "ctx_%d" % i}})
        state = core.fold(project(), evs)
        shows = {"ctx_%d" % i: show_result("succeeded", "completed", completed="2026-11-04 07:00:00") for i in range(5)}
        fr = FakeRunner(terminals=TERMS, ps="", shows=shows)
        errors = []
        tasks = snap.collect_tasks(state, [], [], fr, "orca", errors, cap=2)
        self.assertEqual(len(fr.kinds("worker-show")), 2)
        self.assertEqual(len(tasks), 2)
        self.assertTrue(any("上限" in e["error"] for e in errors))
        # next run picks up the rest
        fr = FakeRunner(terminals=TERMS, ps="", shows=shows)
        tasks = snap.collect_tasks(state, [], [], fr, "orca", [], cap=10)
        self.assertEqual(len(fr.kinds("worker-show")), 3)
        self.assertEqual(len(tasks), 5)

    def test_norm_time(self):
        n = snap.norm_time
        self.assertEqual(n("2026-11-04 06:08:53"), "2026-11-04T06:08:53Z")
        self.assertEqual(n("2026-11-04T10:51:00.240Z"), "2026-11-04T10:51:00Z")
        self.assertEqual(n("2026-11-04T10:00:00+08:00"), "2026-11-04T02:00:00Z")
        self.assertIsNone(n(None))
        self.assertIsNone(n("garbage"))

    def test_run_line(self):
        self.assertEqual(snap.run_line("EVB: x", ["EVB"]), "EVB")
        self.assertIsNone(snap.run_line("OTHER\uff1ax", ["EVB"]))
        self.assertIsNone(snap.run_line("no colon", ["EVB"]))
        self.assertIsNone(snap.run_line(None, ["EVB"]))


def tree_project():
    p = project()
    p["lines"][0]["router"] = "term_evb"
    p["lines"][1]["router"] = "term_gone"
    p["cores"][0]["coordinator"] = "term_core"
    return p


def tt(handle, wt=None, **kw):
    d = {"handle": handle, "connected": True, "lastOutputAt": 1793780484523, "worktreeId": wt}
    d.update(kw)
    return d


def row(run, term):
    return wl_row("d_" + term, "t_" + term, run, terminal=term)


def nrow(run, term):
    """A normalized dispatch-index row, as build_router_tree receives them."""
    return {"dispatch": "d_" + term, "task": "t_" + term, "run": run, "state": "ready", "terminal": term,
            "terminal_state": "retained", "workspace": None}


def by_handle(tree):
    return {n["handle"]: n for n in tree}


class RouterTree(unittest.TestCase):
    def build(self, proj=None, runs=None, rows=None, terms=None, wts=None):
        return by_handle(snap.build_router_tree(proj or tree_project(), runs or {}, rows or [], terms or [], wts))

    RUNS = {"rc": {"objective": "core", "coordinator": "term_core"},
            "re": {"objective": "EVB: x", "coordinator": "term_evb"},
            "rs": {"objective": "EVB/sub: do it", "coordinator": "term_alpha"},
            "rss": {"objective": "EVB/sub/leaf: do", "coordinator": "term_beta"}}

    def test_default_edges(self):
        t = self.build()
        self.assertEqual((t["term_core"]["kind"], t["term_core"]["parent"]), ("core", None))
        self.assertEqual((t["term_evb"]["parent"], t["term_evb"]["edge"]), ("term_core", "default"))
        self.assertEqual(t["term_evb"]["line"], "EVB")

    def test_declared_parent_wins(self):
        p = tree_project()
        p["lines"][1]["parent"] = "EVB"
        rows = [nrow("re", "term_gone")]
        t = self.build(p, self.RUNS, rows)
        self.assertEqual((t["term_gone"]["parent"], t["term_gone"]["edge"]), ("term_evb", "declared"))
        p["lines"][1]["parent"] = "c1"
        t = self.build(p, self.RUNS, rows)
        self.assertEqual((t["term_gone"]["parent"], t["term_gone"]["edge"]), ("term_core", "declared"))

    def test_dispatch_edge_and_multilevel(self):
        rows = [nrow("re", "term_alpha"), nrow("rs", "term_beta")]
        t = self.build(runs=self.RUNS, rows=rows)
        self.assertEqual((t["term_alpha"]["parent"], t["term_alpha"]["edge"], t["term_alpha"]["kind"]), ("term_evb", "dispatch", "sub"))
        self.assertEqual((t["term_beta"]["parent"], t["term_beta"]["line"], t["term_beta"]["core"]), ("term_alpha", "EVB", "c1"))
        self.assertEqual(t["term_alpha"]["runs"], ["rs"])

    def test_sub_directly_under_core_has_no_line(self):
        t = self.build(runs=self.RUNS, rows=[nrow("rc", "term_alpha")])
        self.assertEqual((t["term_alpha"]["parent"], t["term_alpha"]["line"], t["term_alpha"]["core"]), ("term_core", None, "c1"))

    def test_worktree_edge_and_priority(self):
        wts = [{"id": "wP", "parentWorktreeId": None}, {"id": "wC", "parentWorktreeId": "wP"}]
        terms = [tt("term_evb", "wP"), tt("term_alpha", "wC"), tt("term_core", "wX")]
        runs = {"rs": self.RUNS["rs"]}
        t = self.build(runs=runs, terms=terms, wts=wts)
        self.assertEqual((t["term_alpha"]["parent"], t["term_alpha"]["edge"]), ("term_evb", "worktree"))
        t = self.build(runs=dict(self.RUNS), rows=[nrow("rc", "term_alpha")], terms=terms, wts=wts)
        self.assertEqual((t["term_alpha"]["parent"], t["term_alpha"]["edge"]), ("term_core", "dispatch"))

    def test_declared_line_follows_its_core_not_worktree(self):
        # project.json (line.core -> that core's coordinator) beats worktree inference (E3)
        wts = [{"id": "wP", "parentWorktreeId": None}, {"id": "wC", "parentWorktreeId": "wP"}]
        terms = [tt("term_evb", "wP"), tt("term_gone", "wC")]
        t = self.build(terms=terms, wts=wts)
        self.assertEqual((t["term_gone"]["parent"], t["term_gone"]["edge"]), ("term_core", "default"))

    def test_line_under_core2_not_core1_despite_worktree(self):
        p = tree_project()
        p["cores"].append({"id": "c2", "name": "two", "coordinator": "term_zeta"})
        p["lines"][1]["core"] = "c2"
        wts = [{"id": "wP", "parentWorktreeId": None}, {"id": "wC", "parentWorktreeId": "wP"}]
        terms = [tt("term_core", "wP"), tt("term_gone", "wC"), tt("term_zeta", "wX")]
        t = self.build(proj=p, terms=terms, wts=wts)
        self.assertEqual(t["term_gone"]["parent"], "term_zeta")

    def test_orca_calls_do_not_inherit_terminal_handle(self):
        import subprocess as sp
        seen = {}

        def fake(argv, **kw):
            seen["env"] = kw.get("env")
            class P: returncode, stdout, stderr = 0, "", ""
            return P()
        old = sp.run
        sp.run = fake
        try:
            with mock.patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": "term_f35f375e"}):
                snap.default_runner(["/x/orca", "orchestration", "worker-start"])
                self.assertNotIn("ORCA_TERMINAL_HANDLE", seen["env"])
                snap.default_runner(["ssh", "wsl", "true"])
                self.assertIsNone(seen["env"])
        finally:
            sp.run = old

    def test_foreign_runs_excluded(self):
        runs = dict(self.RUNS, rf={"objective": "other", "coordinator": "term_foreign"},
                    rf2={"objective": "other2", "coordinator": "term_foreign2"})
        rows = [nrow("rf", "term_foreign2"), nrow("re", "term_alpha")]
        t = self.build(runs=runs, rows=rows)
        self.assertNotIn("term_foreign", t)
        self.assertNotIn("term_foreign2", t)
        self.assertIn("term_alpha", t)

    def test_cycle_guard(self):
        p = tree_project()
        p["lines"][0]["parent"] = "GONE"
        p["lines"][1]["parent"] = "EVB"
        t = self.build(p)
        self.assertTrue(t["term_evb"]["parent"] is None or t["term_gone"]["parent"] is None
                        or "term_core" in (t["term_evb"]["parent"], t["term_gone"]["parent"]))
        runs = {"ra": {"objective": "a", "coordinator": "term_a"}, "rb": {"objective": "b", "coordinator": "term_b"}}
        t = self.build(runs=runs, rows=[nrow("ra", "term_b"), nrow("rb", "term_a")])
        self.assertNotIn("term_a", t)
        self.assertNotIn("term_b", t)

    def test_name_and_presence(self):
        terms = [tt("term_alpha", None, paneTitle="My Sub")]
        t = self.build(runs=self.RUNS, rows=[nrow("re", "term_alpha")], terms=terms)
        self.assertEqual((t["term_alpha"]["name"], t["term_alpha"]["present"]), ("My Sub", True))
        self.assertTrue(t["term_alpha"]["last_output_at"])
        t = self.build(runs=self.RUNS, rows=[nrow("re", "term_alpha")])
        self.assertEqual(t["term_alpha"]["name"], "EVB/sub: do it")

    def test_worker_gets_ancestor_line(self):
        rows = [wl_row("ctx_2", "task_ZZ", "rs", terminal=TERM, workspace=WS), row("re", "term_alpha")]
        fr = FakeRunner(terminals=TERMS, ps=PS_ONE, wl_rows={"active": rows}, shows={"ctx_2": show_result()},
                        runs=[{"id": k, "objective": v["objective"], "coordinator_handle": v["coordinator"]}
                              for k, v in self.RUNS.items()])
        tmp = tempfile.TemporaryDirectory()
        os.environ["PROGRESS_CACHE_DIR"] = tmp.name
        try:
            doc = snap.collect(tree_project(), None, fr, now=NOW, screen=False)
        finally:
            os.environ.pop("PROGRESS_CACHE_DIR", None)
            tmp.cleanup()
        w = doc["workers"][0]
        self.assertEqual((w["router"], w["line"], w["core"], w["attribution"]), ("term_alpha", "EVB", "c1", "run"))
        self.assertEqual(snap.summary(doc)["router_tree"], len(doc["router_tree"]))

    def test_worktree_list_failure(self):
        fr = FakeRunner(terminals=TERMS, ps=PS_ONE, wt_rc=1)
        doc = snap.collect(tree_project(), None, fr, now=NOW, screen=False)
        self.assertTrue(any("worktree list" in e["error"] for e in doc["errors"]))
        self.assertEqual({n["handle"] for n in doc["router_tree"]}, {"term_core", "term_evb", "term_gone"})


class Main(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["PROGRESS_HOME"] = self.tmp.name
        os.environ["PROGRESS_CACHE_DIR"] = self.tmp.name + "/cache"
        core.save_project(project())

    def tearDown(self):
        self.tmp.cleanup()

    def quiet(self, fn, argv, runner):
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(argv, runner=runner)

    def test_writes_local_live(self):
        fr = FakeRunner(terminals=TERMS, ps=PS)
        self.assertEqual(self.quiet(snap.main, ["--project", "t", "--no-screen"], runner=fr), 0)
        doc = core.load_live("t")
        self.assertEqual(doc["schema"], "progress-live/v1")
        self.assertEqual(len(doc["workers"]), 2)

    def test_dry_run_writes_nothing(self):
        fr = FakeRunner(terminals=TERMS, ps=PS)
        self.assertEqual(self.quiet(snap.main, ["--project", "t", "--dry-run", "--no-screen"], runner=fr), 0)
        self.assertIsNone(core.load_live("t"))

    def test_remote_unreachable_store(self):
        fr = FakeRunner(terminals=TERMS, ps=PS)
        self.assertEqual(self.quiet(snap.main, ["--project", "remote-only", "--dry-run"], runner=fr), 2)


if __name__ == "__main__":
    unittest.main()
