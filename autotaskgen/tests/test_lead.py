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
        self.write_conf({"terminal": "term_lead-1", "worktree": str(root / "wt"), "utc_offset_h": 0})
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
        left = lead.init(c, {"worktree": "/w", "terminal": ""}, {"goal_doc": "docs/GOAL.md"}, str(tpl))
        self.assertEqual(left, ["keep"])
        text = (pathlib.Path(c.home) / "BOOT.md").read_text(encoding="utf-8")
        self.assertEqual(text, "项目 fresh，目录 %s，命令 `lead glance`，目标 docs/GOAL.md，{keep}\n" % c.home)
        conf = lead.load_json(c.config_path)
        self.assertEqual((conf["worktree"], conf["terminal"], conf["interval_s"]), ("/w", "", 7200))
        self.assertEqual(self.runner.find("init")[0][:3], ["git", "-C", c.home])
        (pathlib.Path(c.home) / "BOOT.md").write_text("mine")
        lead.init(c, {"terminal": "term_x"}, {}, str(tpl))
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


def orca_rules(show=None, idle=True, launch=None, send_ok=True, tail=None):
    def ans(obj):
        return 0, json.dumps(obj), ""
    return [
        (lambda a: a[1:3] == ["terminal", "read"], ans({"ok": True, "result": {"terminal": {"tail": tail or ["\u276f\xa0"]}}})),
        (lambda a: a[1:3] == ["terminal", "show"], ans(show if show is not None else
                                                     {"ok": True, "result": {"terminal": {"connected": True, "agentIdentity": "claude"}}})),
        (lambda a: a[1:3] == ["terminal", "wait"], ans({"ok": True, "result": {"wait": {"satisfied": idle}}})),
        (lambda a: a[1:3] == ["agent", "launch"], ans(launch or {"ok": False})),
        (lambda a: a[1:3] == ["terminal", "send"], ans({"ok": send_ok, "result": {"status": "input_accepted"}})),
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
    def waker(self, now):
        return lead_wake.Waker(self.ctx(), sleep=lambda s: None, now=now)

    def sends(self):
        return [c[c.index("--text") + 1] for c in self.runner.calls if c[1:3] == ["terminal", "send"]]

    def test_idle_terminal_gets_clear_then_prompt(self):
        self.runner.rules = orca_rules()
        os.environ["ORCA_BIN"] = "/fake/orca"
        r = self.waker(T0.timestamp()).run()
        self.assertEqual(r, {"action": "woke", "reason": "timer", "handle": "term_lead-1"})
        texts = self.sends()
        self.assertEqual(texts[0], "/clear")
        self.assertIn("唤醒原因：timer", texts[1])
        self.assertIn(str(self.home / "BOOT.md").replace(os.path.expanduser("~"), "~"), texts[1])
        self.assertIn("`lead cycle start --reason timer`", texts[1])
        self.assertTrue(all(c[0] == "/fake/orca" and c[-1] == "--json" for c in self.runner.calls))
        st = lead.load_json(str(self.home / "wake.json"))
        self.assertEqual((st["last_wake"], st["last_reason"]), (T0.timestamp(), "timer"))
        r = self.waker(T0.timestamp() + 60).run()
        self.assertEqual(r, {"action": "skip", "why": "not_due"})

    def test_inbox_wake_and_custom_prompt(self):
        self.runner.rules = orca_rules()
        self.write_conf({"terminal": "term_lead-1", "wake_prompt": "go {project} {reason} {boot} {lead}",
                         "lead_cmd": "lead --project demo"})
        lead.save_json(str(self.home / "wake.json"), {"last_wake": T0.timestamp() - 700})
        load_plugin().append(str(self.home), {"dir": "in", "text": "hi"})
        r = self.waker(T0.timestamp()).run()
        self.assertEqual(r["reason"], "inbox")
        self.assertTrue(self.sends()[1].startswith("go demo inbox "))
        self.assertTrue(self.sends()[1].endswith(" lead --project demo"))

    def test_running_cycle_and_wake_off_and_busy_skip(self):
        self.runner.rules = orca_rules(idle=False)
        lead.save_json(str(self.home / "cycle.json"), {"state": "running", "started": "2026-01-10T07:30:00Z", "n": 4})
        self.assertEqual(self.waker(T0.timestamp()).run()["why"], "running")
        self.assertEqual(self.runner.calls, [])
        (self.home / "WAKE_OFF").write_text("")
        self.assertEqual(self.waker(T0.timestamp()).run()["why"], "wake_off")
        self.assertEqual(self.waker(T0.timestamp()).run(force=True)["why"], "running")
        os.remove(str(self.home / "WAKE_OFF"))
        lead.save_json(str(self.home / "cycle.json"), {"state": "done", "started": "2026-01-10T07:30:00Z", "n": 4})
        r = self.waker(T0.timestamp()).run()
        self.assertEqual(r["why"], "busy")
        self.assertEqual(self.sends(), [])
        self.assertEqual(lead.load_json(str(self.home / "wake.json"))["busy_since"], T0.timestamp())

    def test_stale_running_cycle_does_not_block(self):
        self.runner.rules = orca_rules()
        lead.save_json(str(self.home / "cycle.json"), {"state": "running", "started": "2026-01-09T20:00:00Z", "n": 4})
        self.assertEqual(self.waker(T0.timestamp()).run()["action"], "woke")

    def test_missing_terminal_is_relaunched_and_saved(self):
        self.runner.rules = orca_rules(show={"ok": False}, launch={"ok": True, "result": {"terminal": {"handle": "term_new-2"}}})
        r = self.waker(T0.timestamp()).run()
        self.assertEqual((r["action"], r["handle"]), ("woke", "term_new-2"))
        launch = self.runner.find("launch")[0]
        self.assertEqual(launch[launch.index("--agent") + 1], "claude")
        self.assertEqual(launch[launch.index("--worktree") + 1], "path:" + str(pathlib.Path(self.tmp.name) / "wt"))
        self.assertEqual(lead.load_json(str(self.home / "config.json"))["terminal"], "term_new-2")
        self.assertEqual(lead.load_json(str(self.home / "config.json"))["utc_offset_h"], 0)
        self.assertNotIn("/clear", self.sends())                         # a fresh session needs no /clear
        out = [m for m in lead.read_jsonl(str(self.home / "inbox.jsonl")) if m["dir"] == "out"]
        self.assertEqual(len(out), 1)                                    # alert attempted (qq unconfigured, logged)
        self.assertIn("term_new-2", out[0]["text"])
        self.assertTrue(out[0]["text"].startswith("[负责人] "))

    def test_launch_failure_alerts_once(self):
        self.runner.rules = orca_rules(show={"ok": False})
        self.assertEqual(self.waker(T0.timestamp()).run()["why"], "launch_failed")
        self.assertEqual(self.waker(T0.timestamp() + 300).run()["why"], "launch_failed")
        out = [m for m in lead.read_jsonl(str(self.home / "inbox.jsonl")) if m["dir"] == "out"]
        self.assertEqual(len(out), 1)

    def test_other_agent_in_terminal_is_refused(self):
        self.runner.rules = orca_rules(show={"ok": True, "result": {"terminal": {"connected": True, "agentIdentity": "codex"}}})
        self.assertEqual(self.waker(T0.timestamp()).run()["why"], "wrong_agent")
        self.assertEqual(self.sends(), [])

    def test_subagent_view_is_skipped_and_alerted_once(self):
        sub = ["\u2500" * 20 + " Daily literature scan 11-07 \u2500", "\u276f\xa0Message @general-purpose\u2026",
               "\u2500" * 40]
        self.runner.rules = orca_rules(tail=sub)
        r = self.waker(T0.timestamp()).run()
        self.assertEqual((r["action"], r["why"]), ("skip", "subagent_view"))
        self.assertEqual(self.sends(), [])                               # neither /clear nor the prompt
        self.assertNotIn("last_wake", lead.load_json(str(self.home / "wake.json")))
        self.assertEqual(self.waker(T0.timestamp() + 300).run()["why"], "subagent_view")
        out = [m for m in lead.read_jsonl(str(self.home / "inbox.jsonl")) if m["dir"] == "out"]
        self.assertEqual(len(out), 1)
        self.assertIn("「Daily literature scan 11-07」", out[0]["text"])
        self.runner.rules = orca_rules()                                 # back on the main session: wakes again
        self.assertEqual(self.waker(T0.timestamp() + 600).run()["action"], "woke")

    def test_parked_on_a_subagent_with_text_in_the_box_is_still_caught(self):
        # 11-07: once "/clear" sat in the box the "Message @" hint was gone; only the titled rule was left
        tail = ["\u2500" * 50 + " Daily literature scan 11-07 \u2500", "\u276f /clear /clear", "\u2500" * 60,
                "  [Opus 5.5] \u2502 dse-rsi"]
        self.assertEqual(lead_wake.prompt_box(tail), ("Daily literature scan 11-07", ""))
        self.runner.rules = orca_rules(tail=tail)
        self.assertEqual(self.waker(T0.timestamp()).run()["why"], "subagent_view")
        self.assertEqual(self.sends(), [])
        out = [m for m in lead.read_jsonl(str(self.home / "inbox.jsonl")) if m["dir"] == "out"]
        self.assertIn("切回主会话", out[0]["text"])

    def test_prompt_box_reads_main_session_states(self):
        rule = "\u2500" * 60
        self.assertEqual(lead_wake.prompt_box(["done", rule, "\u276f", rule, "  [Opus 5.5]"]), (None, ""))
        self.assertEqual(lead_wake.prompt_box([rule, "\u276f Try \"fix lint errors\"", rule]), (None, ""))
        self.assertEqual(lead_wake.prompt_box(["\u276f old prompt in the transcript", "answer", rule,
                                               "\u276f half typed", "  second line", rule, "  footer"]),
                         (None, "half typed second line"))
        self.assertEqual(lead_wake.prompt_box(["no box here"]), (None, ""))

    def test_leftover_input_is_not_glued_to_the_wake(self):
        rule = "\u2500" * 60
        self.runner.rules = orca_rules(tail=[rule, "\u276f \u3010\u9879\u76ee\u8d1f\u8d23\u4eba\u4e00\u8f6e", rule])
        r = self.waker(T0.timestamp()).run()
        self.assertEqual((r["action"], r["why"]), ("skip", "input_not_empty"))
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.waker(T0.timestamp() + 300).run()["why"], "input_not_empty")
        out = [m for m in lead.read_jsonl(str(self.home / "inbox.jsonl")) if m["dir"] == "out"]
        self.assertEqual(len(out), 1)                                    # same text: one alert per interval
        self.assertIn("清空输入框", out[0]["text"])
        self.runner.rules = orca_rules()
        self.assertEqual(self.waker(T0.timestamp() + 600).run()["action"], "woke")

    def test_wake_that_did_not_start_a_cycle_is_reported_within_minutes(self):
        self.runner.rules = orca_rules()
        self.assertEqual(self.waker(T0.timestamp()).run()["action"], "woke")
        self.assertEqual(self.waker(T0.timestamp() + 600).run()["why"], "not_due")
        out = lambda: [m for m in lead.read_jsonl(str(self.home / "inbox.jsonl")) if m["dir"] == "out"]
        self.assertEqual(out(), [])                                      # still inside the 15-minute gap
        self.runner.rules = orca_rules(tail=["\u2500" * 9 + " scan \u2500", "\u276f\xa0Message @x\u2026"])
        self.assertEqual(self.waker(T0.timestamp() + 1200).run()["why"], "not_due")   # not due, still reported
        self.assertEqual(len(out()), 1)
        self.assertIn("20 分钟", out()[0]["text"])
        self.assertIn("子代理「scan」", out()[0]["text"])
        self.waker(T0.timestamp() + 1500).run()
        self.assertEqual(len(out()), 1)                                  # once per wake
        lead.save_json(str(self.home / "cycle.json"), {"state": "done", "started": "2026-01-10T08:01:00Z", "n": 5})
        self.assertEqual(lead.load_json(str(self.home / "wake.json"))["alerted_nostart"], T0.timestamp())

    def test_main_takes_lock_and_runs(self):
        self.runner.rules = orca_rules()
        r = lead_wake.main(["--project", self.PID], runner=self.runner, sleep=lambda s: None)
        self.assertEqual(r["action"], "woke")


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
