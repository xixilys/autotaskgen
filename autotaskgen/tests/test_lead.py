"""Project lead (docs/LEAD.md): ledger, inbox, cycle/status round trip with server.api_lead, wake decisions, lead-qq.

Temp dirs and fake runners only: no Hermes, QQ, ssh, launchd, Orca or model is ever called.
"""
import contextlib
import datetime as dt
import io
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import types
import unittest

HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import lead  # noqa: E402
import lead_wake  # noqa: E402
import progress_core as core  # noqa: E402
import server  # noqa: E402

T0 = dt.datetime(2026, 1, 10, 8, 0, 0, tzinfo=dt.timezone.utc)


def load_plugin():
    spec = importlib.util.spec_from_file_location("lead_qq_plugin", str(HERE / "hermes_plugin" / "lead-qq" / "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, hours):
        self.t += dt.timedelta(hours=hours)


class Runner:
    """Records argv; answers by the first matching (predicate, (rc, out, err)) rule, else (0, '', '')."""

    def __init__(self, rules=None):
        self.calls, self.rules = [], list(rules or [])

    def __call__(self, argv, timeout=60, stdin=None):
        self.calls.append(list(argv))
        for pred, ans in self.rules:
            if pred(argv):
                return ans(argv) if callable(ans) else ans
        return 0, "", ""

    def find(self, *words):
        return [c for c in self.calls if all(w in c for w in words)]


class Base(unittest.TestCase):
    PID = "demo"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.saved = {k: os.environ.get(k) for k in ("LEAD_ROOT", "LEAD_PROJECT", "LEAD_HOME", "PROGRESS_HOME",
                                                     "LEAD_BOARD_HOST", "HERMES_BIN", "ORCA_BIN", "LEAD_QQ_CONFIG",
                                                     "LEAD_WAKE_LABEL", "LEAD_UTC_OFFSET_H")}
        for k in self.saved:
            os.environ.pop(k, None)
        os.environ["LEAD_ROOT"] = str(root / "lead")
        os.environ["PROGRESS_HOME"] = str(root / "progress")
        self.home = root / "lead" / self.PID
        self.home.mkdir(parents=True)
        self.write_conf({"discussion_terminal": "term_lead-1", "worktree": str(root / "wt"), "utc_offset_h": 0})
        self.clock = Clock()
        self.runner = Runner()

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def write_conf(self, conf):
        (self.home / "config.json").write_text(json.dumps(conf), encoding="utf-8")

    def ctx(self):
        return lead.Ctx.load(self.PID, runner=self.runner, clock=self.clock)


class Resolve(Base):
    def test_single_project_is_picked_without_env(self):
        self.assertEqual(lead.resolve_project(), self.PID)
        (pathlib.Path(os.environ["LEAD_ROOT"]) / "other").mkdir()
        (pathlib.Path(os.environ["LEAD_ROOT"]) / "other" / "config.json").write_text("{}")
        with self.assertRaises(SystemExit):
            lead.resolve_project()
        os.environ["LEAD_PROJECT"] = "other"
        self.assertEqual(lead.resolve_project(), "other")

    def test_settings_precedence_env_over_config_over_default(self):
        c = self.ctx()
        self.assertEqual(c.board_host, "")
        self.assertEqual(c.conf["interval_s"], 7200)
        self.write_conf({"board_host": "boardbox", "interval_s": 60})
        c = self.ctx()
        self.assertEqual((c.board_host, c.conf["interval_s"]), ("boardbox", 60))
        os.environ["LEAD_BOARD_HOST"] = "elsewhere"
        self.assertEqual(self.ctx().board_host, "elsewhere")
        self.assertEqual(c.qq_conf, os.path.join(os.environ["PROGRESS_HOME"], "qq.json"))


class Ledger(Base):
    def test_add_check_escalate_and_list(self):
        c = self.ctx()
        r = lead.ledger_add(c, "coordinator:core1", "排 X 节点", "X 出现在 project.json", kind="adjust",
                            why="零并发 80%", check_h=2, baseline=["zero_share=0.8"], sent="req_1")
        self.assertEqual((r["id"], r["status"], r["baseline"], r["check_at"]), ("L1", "open", {"zero_share": "0.8"},
                                                                             "2026-01-10T10:00:00Z"))
        self.assertEqual(lead.ledger_add(c, "human", "问预算", "人回了 QQ")["id"], "L2")
        self.assertEqual([x["id"] for x in lead.ledger_list(c, "due")], [])
        self.clock.advance(3)
        self.assertEqual([x["id"] for x in lead.ledger_list(c, "due")], ["L1"])
        r = lead.ledger_check(c, "L1", "not_done", "没排", recheck_h=4)
        self.assertEqual((r["status"], r["check_at"]), ("open", "2026-01-10T15:00:00Z"))
        r = lead.ledger_check(c, "L1", "cannot_tell", "看板没数据", recheck_h=1)
        self.assertEqual(r["status"], "open")
        r = lead.ledger_check(c, "L1", "not_done", "还是没排")
        self.assertEqual(r["status"], "escalated")
        self.assertEqual([x["id"] for x in lead.ledger_list(c, "open")], ["L1", "L2"])
        self.assertEqual(lead.ledger_check(c, "L2", "done", "回了")["status"], "done")
        self.assertEqual([x["id"] for x in lead.ledger_list(c, "open")], ["L1"])
        self.assertEqual(lead.ledger_check(c, "L1", "obsolete", "人推翻")["status"], "obsolete")
        self.assertEqual(lead.ledger_list(c, "open"), [])
        self.assertEqual(len(lead.ledger_list(c, "all")), 2)
        rows = lead.read_jsonl(c.ledger)
        self.assertEqual([x["verdict"] for x in rows[0]["checks"]], ["not_done", "cannot_tell", "not_done", "obsolete"])

    def test_add_needs_recipient_and_done_when(self):
        c = self.ctx()
        with self.assertRaises(ValueError):
            lead.ledger_add(c, "", "x", "y")
        with self.assertRaises(ValueError):
            lead.ledger_add(c, "human", "x", "")
        with self.assertRaises(KeyError):
            lead.ledger_check(c, "L9", "done")
        with self.assertRaises(ValueError):
            lead.ledger_check(c, "L9", "maybe")

    def test_user_note_and_cli(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self._cli()
        self.assertEqual(out.getvalue().split(), ["L1", "ok", "L1", "->", "done"])
        r = lead.read_jsonl(str(self.home / "ledger.jsonl"))[0]
        self.assertEqual((r["status"], r["user"][0]["text"], r["to"]), ("done", "同意", "router:A"))

    def _cli(self):
        lead.main(["--project", self.PID, "ledger", "add", "--to", "router:A", "--what", "w", "--done-when", "d"],
                  runner=self.runner, clock=self.clock)
        lead.main(["--project", self.PID, "ledger", "user", "L1", "--text", "同意"], runner=self.runner, clock=self.clock)
        lead.main(["--project", self.PID, "ledger", "check", "L1", "--verdict", "done", "--evidence", "e"],
                  runner=self.runner, clock=self.clock)


class Inbox(Base):
    def test_plugin_append_unread_ack(self):
        plug = load_plugin()
        self.assertTrue(plug.append(str(self.home), {"dir": "in", "at": "2026-01-10T08:00:00Z", "text": "为什么慢",
                                                     "ref": None, "message_id": "m1"}))
        self.assertFalse(plug.append(str(self.home), {"dir": "in", "text": "dup", "message_id": "m1"}))
        self.assertTrue(plug.append(str(self.home), {"dir": "in", "text": "L1 推翻", "ref": "L1", "message_id": "m2"}))
        c = self.ctx()
        self.assertEqual([m["id"] for m in lead.unread(lead.read_jsonl(c.inbox))], ["I1", "I2"])
        self.assertTrue(lead.inbox_ack(c, "I1", "回了"))
        self.assertFalse(lead.inbox_ack(c, "I9"))
        rows = lead.read_jsonl(c.inbox)
        self.assertEqual((rows[0]["acked"], rows[0]["handled"]), ("2026-01-10T08:00:00Z", "回了"))
        self.assertEqual([m["id"] for m in lead.unread(rows)], ["I2"])

    def test_script_note_lands_as_unread_once_per_key(self):
        c = self.ctx()
        self.assertEqual(lead.inbox_add(c, "EVB 线 router 是瓶颈", "supervisor", "split:EVB:2026-01-10T08"), "I1")
        self.assertIsNone(lead.inbox_add(c, "again", "supervisor", "split:EVB:2026-01-10T08"))
        rows = lead.read_jsonl(c.inbox)
        self.assertEqual([(r["id"], r["dir"], r["from"]) for r in lead.unread(rows)], [("I1", "in", "supervisor")])

    def test_qq_send_logs_outbound_and_never_leaks_config(self):
        c = self.ctx()
        ok, why = lead.qq_log(c, "hello")
        self.assertEqual((ok, why), (False, "qq_unconfigured"))
        self.assertEqual(self.runner.calls, [])
        pathlib.Path(c.qq_conf).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(c.qq_conf).write_text(json.dumps({"target": "qqbot:fake-target", "owner": "fake-owner"}))
        os.environ["HERMES_BIN"] = "/fake/hermes"
        ok, _ = lead.qq_log(self.ctx(), "x" * 2000)
        self.assertTrue(ok)
        argv = self.runner.calls[-1]
        self.assertEqual(argv[:4], ["/fake/hermes", "send", "-t", "qqbot:fake-target"])
        self.assertLessEqual(len(argv[-1]), 1800)
        rows = lead.read_jsonl(c.inbox)
        self.assertEqual([(r["id"], r["dir"], r["ok"]) for r in rows], [("O1", "out", False), ("O2", "out", True)])
        self.assertNotIn("fake-target", json.dumps(rows))


class Cycle(Base):
    def setUp(self):
        super().setUp()
        pdir = pathlib.Path(os.environ["PROGRESS_HOME"]) / self.PID
        pdir.mkdir(parents=True)
        (pdir / "project.json").write_text(json.dumps({"schema": core.SCHEMA_PROJECT, "id": self.PID}))

    def test_cycle_end_status_round_trips_through_api_lead(self):
        c = self.ctx()
        self.assertEqual(lead.cycle_start(c, "inbox")["n"], 1)
        self.assertEqual(lead.load_json(c.cycle)["state"], "running")
        lead.ledger_add(c, "coordinator:core1", "给空线补活", "线 B 有节点在跑", check_h=2)
        lead.ledger_add(c, "human", "已关的", "x")
        lead.ledger_check(c, "L2", "done", "ok")
        lead.ledger_add(c, "router:C", "第三条", "y")
        lead.ledger_check(c, "L3", "not_done", "a")
        lead.ledger_check(c, "L3", "not_done", "b")
        self.clock.advance(0.5)
        cy, st, ok = lead.cycle_end(c, "at_risk", "B 线空转", "/r/1.md")
        self.assertTrue(ok)
        self.assertEqual((cy["state"], cy["n"], cy["ended"]), ("done", 1, "2026-01-10T08:30:00Z"))
        self.assertEqual(st["schema"], "lead-status/v1")
        self.assertEqual(st["ledger_counts"], {"open": 1, "done": 1, "escalated": 1, "obsolete": 0})
        self.assertEqual([r["id"] for r in st["ledger"]], ["L1", "L3"])
        board = pathlib.Path(os.environ["PROGRESS_HOME"]) / self.PID / "lead" / "status.json"
        self.assertEqual(json.loads(board.read_text()), st)
        j = server.api_lead(self.PID)
        self.assertFalse(j["empty"])
        self.assertEqual((j["schema"], j["cycle"], j["verdict"], j["summary"], j["report"]),
                         ("lead-status/v1", 1, "at_risk", "B 线空转", "/r/1.md"))
        self.assertEqual([(r["id"], r["status"]) for r in j["open"]], [("L1", "open"), ("L3", "escalated")])
        self.assertEqual((j["open"][0]["text"], j["open"][0]["done_when"], j["open"][0]["check_after"], j["open"][0]["to"]),
                         ("给空线补活", "线 B 有节点在跑", "2026-01-10T10:00:00Z", "coordinator:core1"))
        self.assertEqual(j["ledger_counts"]["escalated"], 1)
        self.assertEqual(lead.cycle_start(c)["n"], 2)

    def test_board_host_copy_uses_ssh_and_scp_and_git_commit(self):
        self.write_conf({"board_host": "boardbox", "board_progress_home": "srv/progress"})
        (self.home / ".git").mkdir()
        c = self.ctx()
        lead.cycle_start(c)
        _, _, ok = lead.cycle_end(c, "on_track", "ok", "")
        self.assertTrue(ok)
        self.assertEqual(self.runner.find("ssh")[0][-1], "mkdir -p srv/progress/%s/lead" % self.PID)
        self.assertEqual(self.runner.find("scp")[0][-1], "boardbox:srv/progress/%s/lead/status.json" % self.PID)
        commit = self.runner.find("commit")[0]
        self.assertIn("user.name=lead", commit)
        self.assertFalse((pathlib.Path(os.environ["PROGRESS_HOME"]) / self.PID / "lead").exists())

    def test_failed_board_copy_is_reported(self):
        self.write_conf({"board_host": "boardbox"})
        self.runner.rules.append((lambda a: a[0] == "ssh", (255, "", "no route")))
        c = self.ctx()
        _, _, ok = lead.cycle_end(c, "on_track", "", "")
        self.assertFalse(ok)
        self.assertEqual(self.runner.find("scp"), [])
        self.assertTrue(os.path.exists(c.status))


class Glance(Base):
    def test_glance_reads_dossier_live_ledger_inbox(self):
        pdir = pathlib.Path(os.environ["PROGRESS_HOME"]) / self.PID
        (pdir / "project_review").mkdir(parents=True)
        (pdir / "project_review" / "dossier-20260110T0700Z.json").write_text(json.dumps({
            "generated_at": "2026-01-10T07:00:00Z", "headline": [{"text": "M2 已 30 小时没有确认"}],
            "stages": [{"stage": "S1", "met": 2, "total": 5, "closed_24h": 0, "milestones_confirmed": 1,
                        "milestones": 4, "hours_since_last_closed": 13.0}],
            "critical_path": {"chain": [{"id": "M2", "gap": 3, "total": 4}], "hours_since_confirm": 30},
            "parallel": {"A": {"avg": 0.4, "zero_share": 0.7}}, "flags": [{"level": "warn", "to": "core1", "text": "A 零并发"}]},
            ensure_ascii=False))
        (pdir / "live.json").write_text(json.dumps({"generated_at": "2026-01-10T07:55:00Z",
                                                    "workers": [{"line": "A", "busy": True}, {"line": "A", "busy": True},
                                                                {"line": "B", "busy": False, "lingering": True},
                                                                {"line": "B"}],
                                                    "resources": {"hostx": {"disks": [{"mount": "/", "avail_gb": 90}],
                                                                            "mem_avail_gb": 40}}}))
        c = self.ctx()
        lead.ledger_add(c, "human", "w", "d", check_h=-1)
        load_plugin().append(str(self.home), {"dir": "in", "at": "2026-01-10T07:58:00Z", "text": "进展如何"})
        out = lead.glance_text(c)
        for s in ("M2 已 30 小时没有确认", "S1 条目 2/5", "M2(缺3/4)", "A 0.4/70%", "[warn→core1] A 零并发",
                  "在跑 worker 2 个 {\"A\": 2}", "停着 1 个 {\"B\": 1}", "已结束还开着 1 个 {\"B\": 1}（不算在跑）", "hostx：可用内存 40G，盘 / 90G", "未结 1 条，到期待查 1 条：L1",
                  "未处理 1 条", "[I1 01-10 07:58] 进展如何"):
            self.assertIn(s, out)

    def test_glance_without_any_data_still_prints(self):
        out = lead.glance_text(self.ctx())
        self.assertIn("没有项目档案", out)
        # 11-07: on the Mac with board_host empty the line must say what to set, not just "not found"
        self.assertIn("读不到在跑快照", out)
        self.assertIn("在跑 worker 数未知", out)
        self.assertIn("board_host", out)
        self.assertEqual(self.runner.calls, [])

    def test_glance_board_host_unreachable_names_the_ssh_read(self):
        os.environ["LEAD_BOARD_HOST"] = "boardbox"
        try:
            out = lead.glance_text(self.ctx())
        finally:
            del os.environ["LEAD_BOARD_HOST"]
        self.assertIn("ssh boardbox cat .progress/", out)
        self.assertIn("在跑 worker 数未知", out)


class Init(Base):
    def test_init_renders_template_and_keeps_config(self):
        tpl = pathlib.Path(self.tmp.name) / "BOOT.tpl"
        tpl.write_text("项目 {project}，目录 {state_dir}，命令 `{lead} glance`，目标 {goal_doc}，{keep}\n", encoding="utf-8")
        c = lead.Ctx.load("fresh", runner=self.runner, clock=self.clock)
        left = lead.init(c, {"worktree": "/w", "discussion_terminal": ""}, {"goal_doc": "docs/GOAL.md"}, str(tpl))
        self.assertEqual(left, ["keep"])
        text = (pathlib.Path(c.home) / "BOOT.md").read_text(encoding="utf-8")
        self.assertEqual(text, "项目 fresh，目录 %s，命令 `lead glance`，目标 docs/GOAL.md，{keep}\n" % c.home)
        conf = lead.load_json(c.config_path)
        self.assertEqual((conf["worktree"], conf["discussion_terminal"], conf["interval_s"]), ("/w", "", 7200))
        self.assertEqual(self.runner.find("init")[0][:3], ["git", "-C", c.home])
        (pathlib.Path(c.home) / "BOOT.md").write_text("mine")
        lead.init(c, {"discussion_terminal": "term_x"}, {}, str(tpl))
        self.assertEqual((pathlib.Path(c.home) / "BOOT.md").read_text(), "mine")
        self.assertEqual(lead.load_json(c.config_path)["worktree"], "/w")

    def test_init_warns_when_live_cannot_be_found(self):
        import contextlib, io, types
        tpl = pathlib.Path(self.tmp.name) / "BOOT.tpl"
        tpl.write_text("x\n", encoding="utf-8")
        c = lead.Ctx.load("fresh", runner=self.runner, clock=self.clock)
        args = types.SimpleNamespace(terminal="", worktree="/w", board_host="", set=[], template=str(tpl),
                                     force=False, no_git=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            lead.cmd_init(c, args)
        self.assertIn("WARNING: board_host is empty", buf.getvalue())

    def test_repo_template_has_only_known_placeholders(self):
        text = (HERE.parent / "templates" / "lead" / "BOOT.md").read_text(encoding="utf-8")
        names = set(lead.PLACEHOLDER.findall(text))
        self.assertEqual(names, {"project", "state_dir", "lead", "goal_doc", "holdout_dir", "repo", "branch", "tz",
                                 "kb_dir", "board_url", "framework_contact"})
        for word in ("cycle start", "cycle end", "done_when", "ledger check", "推翻", "HANDOFF.md", "留出集"):
            self.assertIn(word, text.replace("--done-when", "done_when"))


def orca_rules(launch=None, tail=None, close_ok=True):
    """Fake orca: each `agent launch` answers the next handle of `launch` (default term_r1, term_r2, ...)."""
    def ans(obj):
        return 0, json.dumps(obj), ""
    handles = list(launch if launch is not None else ["term_r%d" % i for i in range(1, 10)])

    def do_launch(argv):
        if not handles:
            return ans({"ok": False, "error": {"message": "boom"}})
        return ans({"ok": True, "result": {"outcome": {"kind": "terminal"}, "terminal": {"handle": handles.pop(0)}}})
    return [
        (lambda a: a[1:3] == ["terminal", "read"], ans({"ok": True, "result": {"terminal": {"tail": tail or ["❯\xa0"]}}})),
        (lambda a: a[1:3] == ["terminal", "show"], ans({"ok": True, "result": {"terminal": {"connected": True}}})),
        (lambda a: a[1:3] == ["terminal", "close"], ans({"ok": close_ok})),
        (lambda a: a[1:3] == ["agent", "launch"], do_launch),
    ]


class WakeDecision(unittest.TestCase):
    C = {"interval_s": 7200, "inbox_min_gap_s": 600, "stale_running_s": 10800}

    def test_due_reason(self):
        d = lead_wake.due_reason
        self.assertEqual(d(1000, 900, 0, self.C, force=True, wake_off=True), "manual")
        self.assertIsNone(d(1000, 900, 0, self.C))
        self.assertIsNone(d(1000, 900, 3, self.C))                      # message waiting but gap < 10 min
        self.assertEqual(d(1600, 900, 3, self.C), "inbox")
        self.assertEqual(d(900 + 7200, 900, 0, self.C), "timer")
        self.assertIsNone(d(900 + 7200, 900, 1, self.C, wake_off=True))
        self.assertEqual(d(5, 0, 0, self.C), None)                      # never woken, clock near epoch: not due yet
        self.assertEqual(d(10 ** 9, 0, 0, self.C), "timer")

    def test_running_blocks_until_stale(self):
        started = "2026-01-10T08:00:00Z"
        t = T0.timestamp()
        self.assertEqual(lead_wake.running_blocks(t + 60, {"state": "done", "started": started}, self.C), (False, 0.0))
        self.assertEqual(lead_wake.running_blocks(t + 60, {"state": "running", "started": started}, self.C)[0], True)
        self.assertEqual(lead_wake.running_blocks(t + 10801, {"state": "running", "started": started}, self.C)[0], False)


class Wake(Base):
    DISC = "term_lead-1"

    def waker(self, now):
        return lead_wake.Waker(self.ctx(), sleep=lambda s: None, now=now)

    def at(self, minutes):
        return T0.timestamp() + 60 * minutes

    def calls(self, *words):
        return [c for c in self.runner.calls if c[1:1 + len(words)] == list(words)]

    def out(self):
        return [m for m in lead.read_jsonl(str(self.home / "inbox.jsonl")) if m["dir"] == "out"]

    def wake(self):
        return lead.load_json(str(self.home / "wake.json"), {})

    def start_cycle(self, minutes, state="running"):
        t = lead.dt.datetime.fromtimestamp(self.at(minutes), lead.dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lead.save_json(str(self.home / "cycle.json"), {"state": state, "started": t, "n": 9})

    def tearDown(self):
        # the discussion terminal is never written to, cleared or closed, in any scenario
        for c in self.runner.calls:
            self.assertFalse(c[1:3] in (["terminal", "send"], ["terminal", "close"]) and self.DISC in c, c)
        self.assertEqual(self.calls("terminal", "send"), [])
        super().tearDown()

    def test_force_launches_a_fresh_round_with_the_prompt(self):
        self.runner.rules = orca_rules()
        os.environ["ORCA_BIN"] = "/fake/orca"
        r = self.waker(self.at(0)).run(force=True)
        self.assertEqual(r, {"action": "launched", "reason": "manual", "handle": "term_r1", "attempt": 1,
                             "closed_prev": False})
        (launch,) = self.calls("agent", "launch")
        self.assertEqual(launch[launch.index("--surface") + 1], "terminal")
        self.assertEqual(launch[launch.index("--agent") + 1], "claude")
        self.assertEqual(launch[launch.index("--worktree") + 1], "path:" + str(pathlib.Path(self.tmp.name) / "wt"))
        prompt = launch[launch.index("--prompt") + 1]
        self.assertIn("唤醒原因：manual", prompt)
        self.assertIn("`lead cycle start --reason manual`", prompt)
        self.assertTrue(all(c[0] == "/fake/orca" and c[-1] == "--json" for c in self.runner.calls))
        st = self.wake()
        self.assertEqual((st["round_terminal"], st["round_launched_at"], st["round_attempt"], st["last_reason"]),
                         ("term_r1", self.at(0), 1, "manual"))
        self.assertEqual(self.calls("terminal", "close"), [])

    def test_previous_round_terminal_is_closed_before_the_next(self):
        self.runner.rules = orca_rules()
        self.waker(self.at(0)).run(force=True)
        self.start_cycle(1, "done")
        self.assertEqual(self.waker(self.at(60)).run()["why"], "not_due")
        r = self.waker(self.at(121)).run()
        self.assertEqual((r["action"], r["reason"], r["handle"], r["closed_prev"]), ("launched", "timer", "term_r2", True))
        (close,) = self.calls("terminal", "close")
        self.assertEqual(close[close.index("--terminal") + 1], "term_r1")
        self.assertIn("--tab", close)

    def test_inbox_message_starts_a_round_after_the_gap(self):
        self.runner.rules = orca_rules()
        self.write_conf({"discussion_terminal": self.DISC, "worktree": "/wt", "wake_prompt": "go {project} {reason}"})
        lead.save_json(str(self.home / "wake.json"), {"last_wake": self.at(-11)})
        load_plugin().append(str(self.home), {"dir": "in", "text": "hi"})
        r = self.waker(self.at(0)).run()
        self.assertEqual(r["reason"], "inbox")
        launch = self.calls("agent", "launch")[0]
        self.assertEqual(launch[launch.index("--prompt") + 1], "go demo inbox")

    def test_running_cycle_skips_and_a_stale_one_is_replaced(self):
        self.runner.rules = orca_rules()
        self.waker(self.at(0)).run(force=True)
        self.start_cycle(1)
        self.assertEqual(self.waker(self.at(130)).run()["why"], "running")
        self.assertEqual(len(self.calls("agent", "launch")), 1)
        r = self.waker(self.at(1 + 181)).run()                          # running for 3 h: stale
        self.assertEqual((r["action"], r["closed_prev"]), ("launched", True))
        self.assertEqual(len(self.out()), 1)
        self.assertIn("还没结束", self.out()[0]["text"])

    def test_watchdog_relaunches_once_then_alerts_once(self):
        tail = ["─" * 9 + " scan ─", "❯\xa0Message @x…"]
        self.runner.rules = orca_rules(tail=tail)
        self.waker(self.at(0)).run(force=True)
        self.assertEqual(self.waker(self.at(10)).run()["why"], "not_due")
        r = self.waker(self.at(16)).run()                               # 15 min, no cycle: close and relaunch once
        self.assertEqual((r["action"], r["attempt"], r["handle"], r["closed_prev"]), ("launched", 2, "term_r2", True))
        self.assertEqual(self.out(), [])                                # the first failure does not page the owner
        self.assertEqual(self.waker(self.at(30)).run()["why"], "not_due")
        r = self.waker(self.at(32)).run()                               # the relaunch did not start either
        self.assertEqual(r, {"action": "alerted", "why": "no_cycle_twice"})
        self.assertEqual(len(self.out()), 1)
        self.assertIn("连续两次", self.out()[0]["text"])
        self.assertIn("子代理「scan」", self.out()[0]["text"])
        self.waker(self.at(37)).run()
        self.waker(self.at(60)).run()
        self.assertEqual(len(self.out()), 1)                            # once
        self.assertEqual(len(self.calls("agent", "launch")), 2)
        r = self.waker(self.at(16 + 121)).run()                         # next timer: a fresh first attempt
        self.assertEqual((r["action"], r["attempt"], r["handle"]), ("launched", 1, "term_r3"))

    def test_round_that_starts_its_cycle_is_left_alone(self):
        self.runner.rules = orca_rules()
        self.waker(self.at(0)).run(force=True)
        self.start_cycle(2)
        self.assertEqual(self.waker(self.at(20)).run()["why"], "not_due")
        self.assertEqual(self.waker(self.at(20)).run(force=True)["why"], "running")
        self.start_cycle(2, "done")
        self.assertEqual(self.waker(self.at(40)).run()["why"], "not_due")
        self.assertEqual(self.calls("terminal", "close"), [])

    def test_a_starting_round_blocks_another_launch(self):
        self.runner.rules = orca_rules()
        lead.save_json(str(self.home / "wake.json"), {"last_wake": self.at(-11)})
        load_plugin().append(str(self.home), {"dir": "in", "text": "hi"})
        self.waker(self.at(-130)).run(force=True)
        st = self.wake()
        st.update({"round_launched_at": self.at(-5), "last_wake": self.at(-11)})
        lead.save_json(str(self.home / "wake.json"), st)
        self.assertEqual(self.waker(self.at(0)).run()["why"], "starting")

    def test_wake_off_pauses_everything_but_force(self):
        self.runner.rules = orca_rules()
        (self.home / "WAKE_OFF").write_text("")
        self.assertEqual(self.waker(self.at(0)).run()["why"], "wake_off")
        self.assertEqual(self.waker(self.at(5)).run()["why"], "wake_off")
        self.assertEqual(self.calls("agent", "launch"), [])
        log = (pathlib.Path(os.environ["LEAD_ROOT"]) / "log" / "wake.log").read_text(encoding="utf-8")
        self.assertEqual(log.count("WAKE_OFF present"), 1)              # logged once per pause
        lead.save_json(str(self.home / "wake.json"), {"round_terminal": "term_r0", "round_launched_at": self.at(-30),
                                                      "round_reason": "timer", "last_wake": self.at(-30)})
        self.assertEqual(self.waker(self.at(0)).run()["why"], "wake_off")      # paused: an automatic round is not chased
        self.assertEqual(self.calls("agent", "launch"), [])
        self.waker(self.at(0)).run(force=True)
        r = self.waker(self.at(20)).run()                                      # a hand-started round still is
        self.assertEqual((r["action"], r["attempt"], r["reason"]), ("launched", 2, "manual"))
        self.assertEqual(self.waker(self.at(21)).run()["why"], "wake_off")

    def test_launch_failure_alerts_once(self):
        self.runner.rules = orca_rules(launch=[])
        self.assertEqual(self.waker(self.at(0)).run()["why"], "launch_failed")
        self.assertEqual(self.waker(self.at(5)).run()["why"], "launch_failed")
        self.assertEqual(len(self.out()), 1)
        self.assertNotIn("round_terminal", self.wake())

    def test_discussion_terminal_is_never_closed_even_if_recorded_as_a_round(self):
        self.runner.rules = orca_rules()
        lead.save_json(str(self.home / "wake.json"), {"round_terminal": self.DISC, "round_launched_at": self.at(-200)})
        self.start_cycle(-199, "done")
        r = self.waker(self.at(0)).run()
        self.assertEqual((r["action"], r["closed_prev"]), ("launched", False))
        self.assertEqual(self.calls("terminal", "close"), [])

    def test_prompt_box_reads_main_session_states(self):
        rule = "─" * 60
        self.assertEqual(lead_wake.prompt_box(["done", rule, "❯", rule, "  [Opus 5.5]"]), (None, ""))
        self.assertEqual(lead_wake.prompt_box([rule, '❯ Try "fix lint errors"', rule]), (None, ""))
        self.assertEqual(lead_wake.prompt_box(["❯ old prompt in the transcript", "answer", rule,
                                               "❯ half typed", "  second line", rule, "  footer"]),
                         (None, "half typed second line"))
        self.assertEqual(lead_wake.prompt_box(["─" * 50 + " Daily literature scan 11-07 ─", "❯ /clear /clear",
                                               rule]), ("Daily literature scan 11-07", ""))
        self.assertEqual(lead_wake.prompt_box(["no box here"]), (None, ""))

    def test_main_takes_lock_and_runs(self):
        self.runner.rules = orca_rules()
        r = lead_wake.main(["--project", self.PID, "--force"], runner=self.runner, sleep=lambda s: None)
        self.assertEqual(r["action"], "launched")


class Plugin(Base):
    def test_parse_default_and_custom_prefixes(self):
        p = load_plugin()
        self.assertEqual(p.parse("负责人：为什么 worker 这么少"), {"ref": None, "text": "为什么 worker 这么少"})
        self.assertEqual(p.parse("@负责人 看一下"), {"ref": None, "text": "看一下"})
        self.assertEqual(p.parse("LEAD, status?"), {"ref": None, "text": "status?"})
        self.assertEqual(p.parse("l3 推翻 理由"), {"ref": "L3", "text": "l3 推翻 理由"})
        self.assertEqual(p.parse("Ｌ１２推翻")["ref"], "L12")                 # full width, no space
        for t in ("负责人", "leader 你好", "D3 选 1", "随便聊聊", ""):
            self.assertIsNone(p.parse(t), t)
        self.assertEqual(p.parse("PM: ship it", prefixes=["PM"], ref_prefix="A"), {"ref": None, "text": "ship it"})
        self.assertEqual(p.parse("A7 ok", prefixes=["PM"], ref_prefix="A")["ref"], "A7")
        self.assertIsNone(p.parse("负责人 x", prefixes=["PM"], ref_prefix=""))

    def test_handler_writes_inbox_for_owner_dm_only(self):
        p = load_plugin()
        kicks, acks = [], []
        p._kick = lambda conf: kicks.append(conf["wake_label"])
        qq = pathlib.Path(self.tmp.name) / "qq.json"
        qq.write_text(json.dumps({"owner": "u1", "target": "qqbot:u1"}))
        pathlib.Path(os.environ["LEAD_ROOT"], "lead-qq.json").write_text(json.dumps({
            "owner_conf": str(qq), "wake_label": "org.test.lead-wake", "ack_text": "收到 {id}"}))

        class Adapter:
            def send(self, chat_id, text):
                acks.append((chat_id, text))
        gw = types.SimpleNamespace(adapters={"qqbot": Adapter()})

        def event(text, user="u1", chat="dm", mid="m1"):
            src = types.SimpleNamespace(platform="qqbot", chat_type=chat, user_id=user, chat_id="c1")
            return types.SimpleNamespace(source=src, text=text, message_id=mid)
        self.assertEqual(p.handler(event("负责人 进展？"), gw), {"action": "skip", "reason": "lead-qq"})
        self.assertIsNone(p.handler(event("负责人 x", user="u2", mid="m2"), gw))
        self.assertIsNone(p.handler(event("负责人 x", chat="group", mid="m3"), gw))
        self.assertIsNone(p.handler(event("hello", mid="m4"), gw))
        self.assertEqual(p.handler(event("负责人 进展？"), gw), {"action": "skip", "reason": "lead-qq"})   # duplicate
        rows = lead.read_jsonl(str(self.home / "inbox.jsonl"))
        self.assertEqual([(r["id"], r["dir"], r["text"], r["message_id"]) for r in rows], [("I1", "in", "进展?", "m1")])   # NFKC
        self.assertEqual((kicks, acks), (["org.test.lead-wake"], [("c1", "收到 I1")]))


if __name__ == "__main__":
    unittest.main()
