import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import decide  # noqa: E402
import patrol  # noqa: E402
import progress_core as core  # noqa: E402
from fakes import FakeRunner  # noqa: E402
from test_patrol import Base, NOW, R, at, live, plan_ev  # noqa: E402

_spec = importlib.util.spec_from_file_location("progress_qq", HERE.parent / "hermes_plugin" / "progress-qq" / "__init__.py")
qqplug = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qqplug)

SPEC = ('{"route": {"agent": "codex"}}\n\n# 选哪种评估\n问题：评估集用 A 还是 B\n选项：\n'
        '1. 用 A 集（便宜，样本少）\n2. 用 B 集（贵，样本多）\n证据：docs/ev.md\n')
FAKE = ("import os, sys\n"
        "name = sys.argv[1]\n"
        "sys.stdin.read()\n"
        "open(os.environ['FAKE_LOG'], 'a').write(name + '\\n')\n"
        "p = os.environ.get('FAKE_' + name.upper(), '')\n"
        "txt = open(p).read() if p and os.path.exists(p) else 'FAIL'\n"
        "if txt == 'FAIL':\n    sys.exit(1)\n"
        "print(txt)\n")


def verdict(choice=1, needs_human=False, reason="", summary="先用便宜的"):
    return {"choice": choice, "confidence": "medium", "needs_human": needs_human, "human_reason": reason,
            "reasons": ["docs/ev.md 第3行显示样本够"], "would_change_if": "A 集泄漏", "plain_summary": summary}


class DecideBase(Base):
    def setUp(self):
        super().setUp()
        self.fdir = pathlib.Path(self.tmp.name) / "fake"
        self.fdir.mkdir()
        (self.fdir / "fake.py").write_text(FAKE)
        self.log = self.fdir / "log"
        self.saved = {k: os.environ.get(k) for k in ("PROGRESS_DECIDE_OPUS_CMD", "PROGRESS_DECIDE_ASTRA_CMD", "HERMES_BIN",
                                                    "FAKE_LOG", "FAKE_OPUS", "FAKE_ASTRA")}
        py = "/usr/bin/python3 %s" % (self.fdir / "fake.py")
        os.environ["PROGRESS_DECIDE_OPUS_CMD"] = py + " opus"
        os.environ["PROGRESS_DECIDE_ASTRA_CMD"] = py + " astra"
        os.environ["HERMES_BIN"] = "/fake/hermes"
        os.environ["FAKE_LOG"] = str(self.log)
        os.environ.pop("FAKE_OPUS", None)
        os.environ.pop("FAKE_ASTRA", None)
        self.qq_write()
        # a fresh non-patrol event keeps the project-wide stall alert (stall:project) out of these decide tests
        self.add({"kind": "line_open", "line": "L", "actor": {"role": "coordinator", "handle": "term_coord1-bbbb"}, "summary": "keepalive",
                  "at": at(-1000)})

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def qq_write(self, doc=None):
        (core.home() / "qq.json").write_text(json.dumps(doc or {"target": "qqbot:TEST", "owner": "OWNER"}))

    def set_model(self, name, doc):
        p = self.fdir / name
        p.write_text(doc if isinstance(doc, str) else json.dumps(doc))
        os.environ["FAKE_" + name.upper()] = str(p)

    def calls_of(self, name):
        try:
            return self.log.read_text().split().count(name)
        except OSError:
            return 0

    def hermes(self, fr):
        return [c for c in fr.calls if c[0] == "/fake/hermes"]

    def decide_node(self, nid="d1", **kw):
        self.add(plan_ev("L", nid, node_kind="decide", spec="br@docs/%s.md" % nid, **kw))

    def runner(self):
        return FakeRunner(spec=SPEC)

    def inbox(self, *entries):
        with (core.home() / "inbox.jsonl").open("a") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")


class Panel(DecideBase):
    def test_agreed_path(self):
        self.decide_node()
        self.put_live(live())
        self.set_model("opus", verdict(2, summary="B 样本多"))
        self.set_model("astra", verdict(2, summary="B 更稳"))
        fr = self.runner()
        out = self.run_patrol(fr)
        d = self.events("decision")
        self.assertEqual(len(d), 1)
        self.assertEqual((d[0]["card"], d[0]["result"], d[0]["choice"]), ("D1", "agreed", 2))
        self.assertEqual(d[0]["actor"]["role"], "patrol")
        dl = self.events("node_deliver")
        self.assertEqual(len(dl), 1)
        self.assertEqual((dl[0]["outcome"], dl[0]["refs"]["card"]), ("PASS", "D1"))
        self.assertIn("面板一致：选 2", dl[0]["summary"])
        self.assertEqual(self.node("L", "d1")["status"], "delivered")
        sends = self.hermes(fr)
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][:4], ["/fake/hermes", "send", "-t", "qqbot:TEST"])
        text = sends[0][sends[0].index("-q") + 1]
        self.assertTrue(text.startswith("[已执行 D1] L/d1：评估集用 A 还是 B"))
        self.assertIn("结论：选 2 用 B 集", text)
        self.assertIn("B 样本多 / B 更稳", text)
        self.assertIn("回复「D1 推翻」", text)
        self.assertIn("回复「D1 为什么」", text)
        self.assertEqual(self.calls_of("opus"), 1)
        self.assertEqual(self.calls_of("astra"), 1)
        self.assertEqual(decide.load_cards()["D1"]["status"], "agreed")
        self.assertTrue(any(r.get("type") == "decide" and r["result"] == "agreed" for r in out["results"]))

    def test_disagree_blocks_and_sends_card(self):
        self.decide_node()
        self.put_live(live())
        self.set_model("opus", verdict(1))
        self.set_model("astra", verdict(2))
        fr = self.runner()
        self.run_patrol(fr)
        d = self.events("decision")
        self.assertEqual((d[0]["result"], d[0]["choice"]), ("escalated", None))
        self.assertEqual(self.events("node_deliver"), [])
        blk = self.events("node_block")
        self.assertEqual((blk[0]["blocker"], blk[0]["summary"]), ("design", "等用户裁决 D1"))
        self.assertEqual(self.node("L", "d1")["status"], "blocked")
        sends = self.hermes(fr)
        self.assertEqual(len(sends), 2)
        texts = [c[c.index("-q") + 1] for c in sends]
        self.assertTrue(all(len(t) <= 1500 for t in texts))
        self.assertIn("[待你裁决 D1]", texts[0])
        self.assertIn("先说你的直觉选哪个", texts[0])
        self.assertIn("Opus：选 1", texts[1])
        self.assertIn("Astra：选 2", texts[1])
        self.assertIn("分歧点", texts[1])
        self.assertIn("什么证据会改变结论", texts[1])
        self.assertIn("其他工作照常推进", texts[1])
        self.assertIn("D1 选 2", texts[1])
        self.assertEqual(decide.load_cards()["D1"]["status"], "escalated")

    def test_needs_human_escalates_even_if_same_choice(self):
        self.decide_node()
        self.put_live(live())
        self.set_model("opus", verdict(1))
        self.set_model("astra", verdict(1, needs_human=True, reason="对外发布"))
        fr = self.runner()
        self.run_patrol(fr)
        self.assertEqual(self.events("decision")[0]["result"], "escalated")
        self.assertEqual(len(self.events("node_block")), 1)
        self.assertIn("对外发布", self.hermes(fr)[1][self.hermes(fr)[1].index("-q") + 1])

    def test_judge_failure_is_panel_failed(self):
        self.decide_node()
        self.put_live(live())
        self.set_model("opus", verdict(1))
        self.set_model("astra", "FAIL")
        fr = self.runner()
        self.run_patrol(fr)
        self.assertEqual(self.calls_of("astra"), 2)  # tried twice
        self.assertEqual(self.events("decision")[0]["result"], "panel_failed")
        self.assertEqual(self.node("L", "d1")["status"], "blocked")
        self.assertEqual(len(self.hermes(fr)), 2)

    def test_invalid_verdict_counts_as_failure(self):
        self.decide_node()
        self.put_live(live())
        self.set_model("opus", verdict(1))
        self.set_model("astra", verdict(7))  # no such option
        self.run_patrol(self.runner())
        self.assertEqual(self.events("decision")[0]["result"], "panel_failed")

    def test_cli_wrapper_shapes_parse(self):
        v = verdict(1)
        self.assertEqual(decide.extract_json(json.dumps({"structured_output": v}))["choice"], 1)
        self.assertEqual(decide.extract_json(json.dumps({"result": json.dumps(v)}))["choice"], 1)
        self.assertEqual(decide.extract_json(json.dumps(v))["choice"], 1)
        self.assertIsNone(decide.extract_json("nope"))

    def test_per_round_cap(self):
        for nid in ("d1", "d2", "d3"):
            self.decide_node(nid)
        self.put_live(live())
        self.set_model("opus", verdict(1))
        self.set_model("astra", verdict(1))
        fr = self.runner()
        out = self.run_patrol(fr)
        self.assertEqual(len(self.events("decision")), decide.PER_ROUND)
        self.assertEqual([e["card"] for e in self.events("decision")], ["D1", "D2"])
        out2 = self.run_patrol(fr, minutes_later=15)
        self.assertEqual(len(self.events("decision")), 3)
        self.assertEqual(self.events("decision")[-1]["card"], "D3")
        self.assertTrue(out["ok"] and out2["ok"])

    def test_no_rejudge(self):
        self.decide_node()
        self.put_live(live())
        self.set_model("opus", verdict(1))
        self.set_model("astra", verdict(2))
        fr = self.runner()
        self.run_patrol(fr)
        self.run_patrol(fr, minutes_later=15)
        self.run_patrol(fr, minutes_later=400)
        self.assertEqual(self.calls_of("opus"), 1)
        self.assertEqual(len(self.events("decision")), 1)
        # decision cards only: the stale-live monitoring alert of the 400 min round is a separate message
        cards = [c for c in self.hermes(fr) if not c[c.index("-q") + 1].startswith("[需要你看]")]
        self.assertEqual(len(cards), 2)

    def test_qq_missing_no_send_with_finding(self):
        (core.home() / "qq.json").unlink()
        self.decide_node()
        self.put_live(live())
        self.set_model("opus", verdict(1))
        self.set_model("astra", verdict(1))
        fr = self.runner()
        res = self.run_patrol(fr, apply=False)
        self.assertIn("qq_unconfigured", [f["code"] for f in res["findings"]])
        self.assertEqual(self.calls_of("opus"), 0)
        out = self.run_patrol(fr)
        self.assertEqual(self.hermes(fr), [])
        self.assertEqual(self.events("decision")[0]["result"], "agreed")
        self.assertIn("qq_unconfigured", [f["code"] for f in out["findings"]])

    def test_send_failure_is_observed_not_fatal(self):
        self.decide_node()
        self.put_live(live())
        self.set_model("opus", verdict(1))
        self.set_model("astra", verdict(1))
        fr = self.runner()
        fr.send_rc = 1
        self.run_patrol(fr)
        self.assertEqual(self.events("decision")[0]["result"], "agreed")
        self.assertTrue([e for e in self.events("observe") if "QQ 发送失败" in e["summary"]])

    def test_dry_run_runs_nothing(self):
        self.decide_node()
        self.put_live(live())
        self.set_model("opus", verdict(1))
        self.set_model("astra", verdict(1))
        fr = self.runner()
        out = self.run_patrol(fr, apply=False)
        self.assertEqual(self.calls_of("opus") + self.calls_of("astra"), 0)
        self.assertEqual(self.hermes(fr), [])
        self.assertEqual(self.events("decision"), [])
        self.assertEqual([a["type"] for a in out["actions"] if a["type"] == "decide"], ["decide"])

    def test_no_options_falls_back_to_router_nudge(self):
        self.decide_node()
        self.put_live(live())
        fr = FakeRunner(spec="# 没有编号选项\n随便写写\n")
        out = self.run_patrol(fr)
        self.assertEqual(self.calls_of("opus"), 0)
        self.assertIn("decide_unready", [f["code"] for f in out["findings"]])

    def test_patrol_deliver_only_for_decide_nodes_with_card(self):
        self.add(plan_ev("L", "r1"))
        self.decide_node()
        who = {"role": "patrol", "handle": "patrol@mac"}
        with self.assertRaises(core.ProgressError):
            core.append("t", {"kind": "node_deliver", "line": "L", "node": "r1", "actor": who, "outcome": "PASS",
                              "summary": "x", "refs": {"card": "D1"}})
        with self.assertRaises(core.ProgressError):
            core.append("t", {"kind": "node_deliver", "line": "L", "node": "d1", "actor": who, "outcome": "PASS",
                              "summary": "x"})


class Parse(unittest.TestCase):
    def test_grammar(self):
        p = qqplug.parse_reply
        self.assertEqual(p("D1 选 2", []), {"card": "D1", "verb": "pick", "arg": "2"})
        self.assertEqual(p("d12 选2", []), {"card": "D12", "verb": "pick", "arg": "2"})
        self.assertEqual(p("D3\u3000选\u30002", []), {"card": "D3", "verb": "pick", "arg": "2"})
        self.assertEqual(p("Ｄ３　选　２", []), {"card": "D3", "verb": "pick", "arg": "2"})
        self.assertEqual(p("D1 推翻", []), {"card": "D1", "verb": "overrule", "arg": ""})
        self.assertEqual(p("D1 推翻 样本太少", []), {"card": "D1", "verb": "overrule", "arg": "样本太少"})
        self.assertEqual(p("D1 为什么", []), {"card": "D1", "verb": "why", "arg": ""})
        self.assertEqual(p("d1 解释 B 的代价", []), {"card": "D1", "verb": "why", "arg": "B 的代价"})
        self.assertIsNone(p("你好", ["D1"]))
        self.assertIsNone(p("D1 随便", []))
        self.assertIsNone(p("", []))

    def test_bare_pick_needs_exactly_one_open_card(self):
        p = qqplug.parse_reply
        self.assertEqual(p("选 2", ["D4"]), {"card": "D4", "verb": "pick", "arg": "2"})
        self.assertEqual(p("选2", ["D4"])["card"], "D4")
        self.assertIsNone(p("选 2", []))
        self.assertIsNone(p("选 2", ["D1", "D2"]))


class Handler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = os.environ.get("PROGRESS_HOME")
        os.environ["PROGRESS_HOME"] = self.tmp.name
        h = pathlib.Path(self.tmp.name)
        (h / "qq.json").write_text(json.dumps({"target": "qqbot:T", "owner": "OWNER"}))
        (h / "qq_cards.json").write_text(json.dumps({"D1": {"status": "escalated"}, "D2": {"status": "agreed"}}))
        self.popen = mock.patch.object(qqplug.subprocess, "Popen")
        self.kick = self.popen.start()
        self.sent = []

        class Adapter:
            def send(_s, chat, text):
                self.sent.append((chat, text))
        self.gw = types.SimpleNamespace(adapters={"qqbot": Adapter()})

    def tearDown(self):
        self.popen.stop()
        if self.saved is None:
            os.environ.pop("PROGRESS_HOME", None)
        else:
            os.environ["PROGRESS_HOME"] = self.saved
        self.tmp.cleanup()

    def ev(self, text, user="OWNER", platform="qqbot", chat="dm", mid="m1"):
        src = types.SimpleNamespace(platform=platform, chat_type=chat, user_id=user, chat_id="c1")
        return types.SimpleNamespace(text=text, source=src, message_id=mid)

    def lines(self):
        p = pathlib.Path(self.tmp.name) / "inbox.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def test_owner_reply_recorded_acked_kicked(self):
        r = qqplug.handler(self.ev("D1 选 2"), gateway=self.gw)
        self.assertEqual(r, {"action": "skip", "reason": "progress-qq"})
        e = self.lines()
        self.assertEqual((e[0]["card"], e[0]["verb"], e[0]["arg"], e[0]["message_id"]), ("D1", "pick", "2", "m1"))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.kick.call_count, 1)
        self.assertIn("kickstart", self.kick.call_args[0][0])

    def test_dedupe_by_message_id(self):
        qqplug.handler(self.ev("D1 选 2"), gateway=self.gw)
        r = qqplug.handler(self.ev("D1 选 2"), gateway=self.gw)
        self.assertEqual(len(self.lines()), 1)
        self.assertEqual(r["action"], "skip")
        self.assertEqual(self.kick.call_count, 1)

    def test_non_owner_group_and_other_platform_ignored(self):
        self.assertIsNone(qqplug.handler(self.ev("D1 选 2", user="SOMEONE"), gateway=self.gw))
        self.assertIsNone(qqplug.handler(self.ev("D1 选 2", chat="group"), gateway=self.gw))
        self.assertIsNone(qqplug.handler(self.ev("D1 选 2", platform="telegram"), gateway=self.gw))
        self.assertEqual(self.lines(), [])
        self.assertEqual(self.kick.call_count, 0)

    def test_ordinary_text_and_bare_pick(self):
        self.assertIsNone(qqplug.handler(self.ev("今天天气"), gateway=self.gw))
        r = qqplug.handler(self.ev("选 1", mid="m2"), gateway=self.gw)  # exactly one escalated card (D1)
        self.assertEqual(r["action"], "skip")
        self.assertEqual(self.lines()[0]["card"], "D1")

    def test_never_raises(self):
        self.assertIsNone(qqplug.handler(object()))
        r = qqplug.handler(self.ev("D1 为什么", mid="m3"), gateway=None)
        self.assertEqual(r["action"], "skip")


class Inbox(DecideBase):
    def escalate(self):
        self.decide_node()
        self.put_live(live())
        self.set_model("opus", verdict(1))
        self.set_model("astra", verdict(2))
        self.fr = self.runner()
        self.run_patrol(self.fr)
        self.fr.calls.clear()

    def agree(self):
        self.decide_node()
        self.put_live(live())
        self.set_model("opus", verdict(1))
        self.set_model("astra", verdict(1))
        self.fr = self.runner()
        self.run_patrol(self.fr)
        self.fr.calls.clear()

    def entry(self, verb, arg="", mid="m1", card="D1"):
        return {"at": "2026-11-04T12:00:00Z", "card": card, "verb": verb, "arg": arg, "text": "t", "message_id": mid}

    def test_pick_on_escalated_card(self):
        self.escalate()
        self.inbox(self.entry("pick", "2"))
        out = self.run_patrol(self.fr, minutes_later=5)
        d = self.events("decision")[-1]
        self.assertEqual((d["result"], d["choice"]), ("human", 2))
        dl = self.events("node_deliver")[-1]
        self.assertEqual(dl["refs"]["card"], "D1")
        # project.humans lacks qq:OWNER here -> patrol + refs.by, validation untouched
        self.assertEqual((dl["actor"]["role"], dl["refs"]["by"]), ("patrol", "qq:OWNER"))
        self.assertEqual(self.node("L", "d1")["status"], "delivered")
        hr = self.events("human_reply")
        self.assertEqual((hr[0]["refs"]["message_id"], hr[0]["refs"]["outcome"]), ("m1", "applied"))
        sends = self.hermes(self.fr)
        self.assertEqual(len(sends), 1)
        self.assertIn("D1 已生效：选 2", sends[0][sends[0].index("-q") + 1])
        self.assertTrue(out["ok"])

    def test_pick_uses_human_role_when_declared(self):
        p = core.load_project("t")
        p["humans"] = ["qq:OWNER"]
        core.save_project(p)
        self.escalate()
        self.inbox(self.entry("pick", "1"))
        self.run_patrol(self.fr, minutes_later=5)
        self.assertEqual(self.events("node_deliver")[-1]["actor"]["role"], "human")
        self.assertEqual(self.events("decision")[-1]["actor"]["handle"], "qq:OWNER")

    def test_dedupe_applied_once(self):
        self.escalate()
        self.inbox(self.entry("pick", "2"), self.entry("pick", "2"))
        self.run_patrol(self.fr, minutes_later=5)
        self.run_patrol(self.fr, minutes_later=10)
        self.assertEqual(len(self.events("human_reply")), 1)
        self.assertEqual(len([e for e in self.events("decision") if e["result"] == "human"]), 1)
        self.assertEqual(len(self.hermes(self.fr)), 1)

    def test_overrule_agreed_nudges_router(self):
        self.agree()
        self.inbox(self.entry("overrule", "样本太少"))
        self.run_patrol(self.fr, minutes_later=5)
        self.assertEqual(self.events("decision")[-1]["result"], "overruled")
        sends = [c for c in self.fr.calls if "send" in c and c[0] != "/fake/hermes"]
        self.assertEqual(len(sends), 1)
        self.assertIn(R, sends[0])
        text = sends[0][sends[0].index("--text") + 1]
        self.assertIn("[用户推翻 D1] 原结论 选 1", text)
        self.assertIn("git revert", text)
        self.assertIn("样本太少", text)
        self.assertEqual(len(self.hermes(self.fr)), 1)  # QQ ack
        self.assertEqual(self.events("human_reply")[0]["refs"]["verb"], "overrule")

    def test_pick_other_option_on_agreed_card_overrules(self):
        self.agree()
        self.inbox(self.entry("pick", "2"))
        self.run_patrol(self.fr, minutes_later=5)
        d = self.events("decision")[-1]
        self.assertEqual((d["result"], d["choice"]), ("overruled", 2))
        text = [c for c in self.fr.calls if "send" in c and c[0] != "/fake/hermes"][0]
        self.assertIn("选 2", text[text.index("--text") + 1])

    def test_why_runs_one_judge_and_sends(self):
        self.agree()
        self.set_model("opus", {"explanation": "因为 A 集已经够用，B 集太贵。"})
        before = self.calls_of("opus")
        self.inbox(self.entry("why"))
        self.run_patrol(self.fr, minutes_later=5)
        self.assertEqual(self.calls_of("opus") - before, 1)
        self.assertEqual(self.calls_of("astra"), 1)  # unchanged
        sends = self.hermes(self.fr)
        self.assertEqual(len(sends), 1)
        self.assertIn("因为 A 集已经够用", sends[0][sends[0].index("-q") + 1])
        self.assertEqual(self.events("human_reply")[0]["refs"]["outcome"], "explained")
        self.run_patrol(self.fr, minutes_later=10)
        self.assertEqual(self.calls_of("opus") - before, 1)

    def test_why_respects_round_budget(self):
        self.agree()
        self.decide_node("d2")
        self.set_model("opus", {"explanation": "解释"})
        self.set_model("astra", verdict(1))
        self.inbox(self.entry("why", mid="w1"), self.entry("why", mid="w2"), self.entry("why", mid="w3"))
        before = self.calls_of("opus")
        self.run_patrol(self.fr, minutes_later=5)
        self.assertEqual(len(self.events("human_reply")), decide.PER_ROUND)
        self.run_patrol(self.fr, minutes_later=10)
        self.assertGreaterEqual(len(self.events("human_reply")), 3)
        self.assertGreater(self.calls_of("opus"), before)


if __name__ == "__main__":
    unittest.main()


class SpecWorktreeFallback(unittest.TestCase):
    def test_reads_worktree_file_when_git_dir_unreadable(self):
        import tempfile, pathlib as _pl
        import patrol as _p
        with tempfile.TemporaryDirectory() as td:
            _pl.Path(td, "docs").mkdir()
            _pl.Path(td, "docs", "s.md").write_text('{"route": {"agent": "codex"}}\nbody\n', encoding="utf-8")
            runner = lambda argv, **kw: (128, "", "fatal: not a git repository: (null)")
            old = os.environ.get("PROGRESS_REPO_BRANCH")
            os.environ["PROGRESS_REPO_BRANCH"] = "b"
            try:
                got = _p.read_spec("b@docs/s.md", td, runner)
                other = _p.read_spec("other@docs/s.md", td, runner)
                pinned = _p.read_spec("6c30b68cef681674@docs/s.md", td, runner)
            finally:
                if old is None:
                    os.environ.pop("PROGRESS_REPO_BRANCH", None)
                else:
                    os.environ["PROGRESS_REPO_BRANCH"] = old
            self.assertEqual((got.get("source"), got["header"]), ("worktree", {"route": {"agent": "codex"}}))
            self.assertIn("error", other)   # a different branch is never read from the worktree
            self.assertEqual(pinned.get("source"), "worktree")  # frozen spec pinned to a commit sha


class LineRepoFallback(unittest.TestCase):
    def test_line_then_project_then_unique(self):
        import patrol as _p
        lines = [{"id": "A"}, {"id": "B", "repo": "id:r1"}, {"id": "C", "repo": "id:r1"}]
        self.assertEqual(_p.line_repo({"lines": lines}, {"id": "X", "repo": "id:own"}), "id:own")
        self.assertEqual(_p.line_repo({"repo": "id:p", "lines": lines}, lines[0]), "id:p")
        self.assertEqual(_p.line_repo({"lines": lines}, lines[0]), "id:r1")
        two = lines + [{"id": "D", "repo": "id:r2"}]
        self.assertIsNone(_p.line_repo({"lines": two}, lines[0]))  # ambiguous: do not guess


class SpecRemoteFallback(unittest.TestCase):
    def test_branch_only_on_worker_host_is_read_over_ssh(self):
        import patrol as _p
        calls = []

        def runner(argv, **kw):
            calls.append(argv)
            if argv[0] == "ssh":
                return (0, '{"route": {"agent": "codex"}}\nbody\n', "") if "you/fe-dev:docs/s.md" in argv[-1] \
                    else (128, "", "fatal: invalid object name")
            return (128, "", "fatal: invalid object name 'you/fe-dev'")
        old = os.environ.get("PROGRESS_REMOTE_REPO")
        os.environ["PROGRESS_REMOTE_REPO"] = "/home/u/repo"
        try:
            got = _p.read_spec("you/fe-dev@docs/s.md", "/mac/repo", runner)
            miss = _p.read_spec("nope@docs/s.md", "/mac/repo", runner)
        finally:
            if old is None:
                os.environ.pop("PROGRESS_REMOTE_REPO", None)
            else:
                os.environ["PROGRESS_REMOTE_REPO"] = old
        self.assertEqual((got.get("source"), got["header"]), ("remote", {"route": {"agent": "codex"}}))
        self.assertIn("/home/u/repo", calls[1][-1])
        self.assertIn("也读不到", miss["error"])

    def test_no_remote_repo_configured_keeps_old_error(self):
        import patrol as _p
        old = os.environ.pop("PROGRESS_REMOTE_REPO", None)
        try:
            got = _p.read_spec("x@docs/s.md", "/mac/repo", lambda argv, **kw: (128, "", "fatal: bad"))
        finally:
            if old is not None:
                os.environ["PROGRESS_REMOTE_REPO"] = old
        self.assertIn("error", got)
        self.assertNotIn("也读不到", got["error"])
