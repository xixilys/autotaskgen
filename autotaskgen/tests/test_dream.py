import datetime as dt
import json
import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import dream  # noqa: E402
import progress_core as core  # noqa: E402

R, C1, W1 = "term_router-aaaa", "term_coord1-bbbb", "term_worker1-cccc"
NOW = dt.datetime(2026, 11, 2, 12, 0, 0, tzinfo=dt.timezone.utc)  # a Monday
WT = "/home/u/dse_harness/ws/l-a-p1"
TERM_A = "zq_secretname_17"          # invented holdout names, never real
TERM_B = "invented holdout case phrase 8812"

READ_FAKE = r'''
import json, os, sys
text = sys.stdin.read()
with open(os.environ["DREAM_PROMPT_LOG"], "a") as fh:
    fh.write(text + "\n=====\n")
print(json.dumps({"lessons": [{"rule": "先跑测试再交付", "why": "session s-1 / 2026-11-02", "kind": "rule", "confidence": 0.8}],
                  "state_drift": [{"state_claim": "A 已完成", "reality": "A 仍失败", "evidence": "event 2026-11-02"}],
                  "repeated_failures": [], "memory_loss": []}))
'''

MERGE_FAKE = r'''
import json, os, sys
text = sys.stdin.read()
if os.environ.get("DREAM_MODE") == "bad":
    print("just prose, no heading"); sys.exit(0)
if os.environ.get("DREAM_MODE") == "rc":
    sys.exit(1)
if "GLOBAL.md" in text.split("\n", 1)[0]:
    print("# 全局\n- 共通规则（证据：两条主线；日期：2026-11-02）"); sys.exit(0)
old = text.split("## 当前文件", 1)[1].split("## 候选", 1)[0]
cand = text.split("## 候选", 1)[1].strip()
out = ["# 经验"] + [l for l in old.splitlines() if l.startswith("- ")]
for c in json.loads(cand):
    out.append("- %s（证据：%s；日期：%s）" % (c["rule"], c["why"], c["date"]))
print("\n".join(out))
'''


def project():
    return {"schema": core.SCHEMA_PROJECT, "id": "t", "name": "test", "final_goal": {"text": "g"},
            "cores": [{"id": "c1", "name": "one", "coordinator": C1}],
            "lines": [{"id": "L", "name": "l", "core": "c1", "router": R, "patrol": True},
                      {"id": "M", "name": "m", "core": "c1", "router": "term_other-ffff", "patrol": True}],
            "milestones": [{"id": "M1", "title": "m", "core": "c1", "lines": ["L"], "after": []}]}


def ev(kind, line, node, minutes_ago, role="router", handle=R, **kw):
    t = NOW - dt.timedelta(minutes=minutes_ago)
    e = {"id": "e%s%s%s" % (kind, node, minutes_ago), "project": "t", "kind": kind, "line": line, "node": node,
         "actor": {"role": role, "handle": handle}, "summary": kw.pop("summary", kind + " " + str(node)),
         "at": t.strftime("%Y-%m-%dT%H:%M:%SZ")}
    e.update(kw)
    return e


def jl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    ts = (NOW - dt.timedelta(hours=1)).timestamp()
    os.utime(str(path), (ts, ts))


def codex_rows(cwd, sid="cx-1", extra=()):
    msg = lambda role, text: {"type": "response_item", "payload": {"type": "message", "role": role, "content": [
        {"type": "input_text" if role == "user" else "output_text", "text": text}]}}
    out = lambda code: {"type": "response_item", "payload": {"type": "function_call_output", "output": json.dumps(
        {"output": "boom output" if code else "all fine output", "metadata": {"exit_code": code}})}}
    return [{"type": "session_meta", "payload": {"id": sid, "cwd": cwd}},
            msg("user", "请修复 A"), msg("assistant", "好的，我开始"),
            {"type": "response_item", "payload": {"type": "function_call", "name": "shell", "arguments": json.dumps({"cmd": "pytest " + "x" * 400})}},
            out(0), out(2), {"type": "event_msg", "payload": {"type": "context_compacted"}},
            {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "最终：done"}}] + list(extra)


def claude_rows(cwd, sid="cl-1"):
    return [{"type": "user", "cwd": cwd, "sessionId": sid, "message": {"role": "user", "content": "claude 用户消息"}},
            {"type": "assistant", "cwd": cwd, "sessionId": sid, "message": {"role": "assistant", "content": [
                {"type": "text", "text": "claude 回复"}, {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
            {"type": "user", "cwd": cwd, "sessionId": sid, "message": {"role": "user", "content": [
                {"type": "tool_result", "is_error": True, "content": "claude tool failed"},
                {"type": "tool_result", "is_error": False, "content": "claude tool fine"}]}},
            {"type": "summary", "summary": "compacted summary"}]


class Fake:
    """Fake ssh / rsync / orca. The 'ssh python3 -' extractor really runs, with HOME = the fake WSL home."""

    def __init__(self, wsl_home, idle=True):
        self.wsl_home, self.idle, self.calls, self.sent, self.stdins = wsl_home, idle, [], [], []

    def __call__(self, argv, input_text=None, timeout=60):
        self.calls.append(argv)
        if argv[0] == "ssh":
            if argv[-1] == "python3 -":
                self.stdins.append(input_text)
                p = subprocess.run([sys.executable, "-"], input=input_text, capture_output=True, text=True,
                                   env=dict(os.environ, HOME=str(self.wsl_home)))
                return p.returncode, p.stdout, p.stderr
            return 0, "", ""
        if argv[0] == "rsync":
            return 0, "", ""
        if "terminal" in argv[:3] and "read" in argv[:4]:
            tail = ["output", "❯ "] if self.idle else ["Working (3s • esc to interrupt)", "❯ "]
            return 0, json.dumps({"ok": True, "result": {"terminal": {"tail": tail}}}), ""
        if "terminal" in argv[:3] and "send" in argv[:4]:
            self.sent.append(argv[argv.index("--text") + 1])
            return 0, json.dumps({"ok": True}), ""
        return 1, "", "unexpected"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = pathlib.Path(self.tmp.name)
        self.home, self.wsl = t / "home", t / "wslhome"
        self.home.mkdir(), self.wsl.mkdir()
        self.saved = {k: os.environ.get(k) for k in ("HOME", "PROGRESS_HOME", "PROGRESS_CACHE_DIR", "ORCA_BIN",
                                                    "PROGRESS_DREAM_READ_CMD", "PROGRESS_DREAM_MERGE_CMD",
                                                    "DREAM_PROMPT_LOG", "DREAM_MODE", "PROGRESS_REPO")}
        os.environ["HOME"] = str(self.home)
        os.environ["PROGRESS_HOME"] = str(self.home / ".progress")
        os.environ["PROGRESS_CACHE_DIR"] = str(t / "cache")
        os.environ["ORCA_BIN"] = "/fake/orca"
        os.environ["PROGRESS_REPO"] = str(t / "repo")
        os.environ.pop("DREAM_MODE", None)
        rd, mg = t / "read_fake.py", t / "merge_fake.py"
        rd.write_text(READ_FAKE), mg.write_text(MERGE_FAKE)
        self.prompt_log = t / "prompts.log"
        os.environ["DREAM_PROMPT_LOG"] = str(self.prompt_log)
        os.environ["PROGRESS_DREAM_READ_CMD"] = "%s %s" % (shlex.quote(sys.executable), shlex.quote(str(rd)))
        os.environ["PROGRESS_DREAM_MERGE_CMD"] = "%s %s" % (shlex.quote(sys.executable), shlex.quote(str(mg)))
        core.save_project(project())
        self.events = [
            ev("node_plan", "L", "a", 300), ev("node_dispatch", "L", "a", 280, refs={"terminal": W1, "worktree": WT}),
            ev("node_deliver", "L", "a", 60, outcome="FAIL", failures=["pytest test_a failed"],
               refs={"report": "docs/REPORT_a.md"}, summary="a failed"),
            ev("node_review", "L", "a", 30, outcome="FAIL"), ev("node_review", "L", "a", 20, outcome="PASS")]
        self.write_events(self.events)
        repo = t / "repo" / "docs"
        repo.mkdir(parents=True)
        (repo / "REPORT_a.md").write_text("# report a\n" + "r" * 7000)
        self.fake = Fake(self.wsl)
        self.store = self.home / ".progress" / "t" / "memory"

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def write_events(self, evs):
        p = core.project_dir("t") / "events.jsonl"
        p.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in evs))

    def codex(self, name, cwd, **kw):
        jl(self.home / ".codex" / "sessions" / "2026" / "11" / "02" / (name + ".jsonl"), codex_rows(cwd, name, **kw))

    def run_dream(self, **kw):
        return dream.run("t", runner=self.fake, now=NOW, **kw)

    def tree(self, root):
        return sorted(str(p.relative_to(root)) for p in pathlib.Path(root).rglob("*") if ".git/" not in str(p) + "/")


class Extractor(Base):
    def extract(self, **extra):
        p = {"since": (NOW - dt.timedelta(hours=24)).timestamp(), "scopes": dream.SCOPE_WORDS, "hashes": [], "ns": []}
        p.update(extra)
        return dream._EX["extract"](p)

    def test_codex_and_claude_digest(self):
        self.codex("c1", "/Users/x/proj/dse_harness/wt")
        jl(self.home / ".claude" / "projects" / "-Users-x-dse-harness" / "s.jsonl", claude_rows("/Users/x/dse-harness"))
        res = self.extract()
        by = {s["source"]: s["text"] for s in res["sessions"]}
        c = by["codex"]
        self.assertIn("[user] 请修复 A", c)
        self.assertIn("[assistant] 好的，我开始", c)
        self.assertIn("[tool] shell", c)
        self.assertNotIn("x" * 300, c)                  # tool args capped at 200 chars
        self.assertIn("[err] boom output", c)
        self.assertNotIn("all fine output", c)         # only errors / non-zero exits
        self.assertIn("[compact]", c)
        self.assertIn("[final] 最终：done", c)
        cl = by["claude"]
        self.assertIn("claude 用户消息", cl)
        self.assertIn("[tool] Bash", cl)
        self.assertIn("claude tool failed", cl)
        self.assertNotIn("claude tool fine", cl)
        self.assertIn("[compact] compacted summary", cl)

    def test_scope_filter(self):
        self.codex("in", "/a/autotaskgen/x")
        self.codex("out", "/a/other-project")
        ids = [s["id"] for s in self.extract()["sessions"]]
        self.assertEqual(ids, ["in"])

    def test_window_filter(self):
        self.codex("old", "/a/autotaskgen")
        old = self.home / ".codex" / "sessions" / "2026" / "11" / "02" / "old.jsonl"
        os.utime(str(old), (1, 1))
        self.assertEqual(self.extract()["sessions"], [])

    def test_redaction(self):
        secrets = ["sk-abcdefghijklmnopqrstuvwx", "ghp_abcdefghijklmnopqrstuvwxyz0123", "xoxb-1234567890-abcdef",
                   "Bearer abcDEF123456789xyz", "password=hunter2hunter2", "a" * 20 + "0123456789abcdef0123456789abcdef"]
        msg = {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "keys: " + " ".join(secrets) + " ok-path /home/u/some/long/path/without/digits/x"}]}}
        self.codex("sec", "/a/autotaskgen", extra=[msg])
        text = self.extract()["sessions"][0]["text"]
        for s in ("sk-abcdef", "ghp_abcdef", "xoxb-1234", "abcDEF123456789xyz", "hunter2", "0123456789abcdef0123456789abcdef"):
            self.assertNotIn(s, text)
        self.assertIn("/home/u/some/long/path/without/digits/x", text)

    def test_session_cap_head_tail(self):
        rows = codex_rows("/a/autotaskgen") + [
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": ("w%d " % i) * 800}]}} for i in range(30)]
        jl(self.home / ".codex" / "sessions" / "big.jsonl", rows)
        s = self.extract()["sessions"][0]
        self.assertGreater(s["cut"], 0)
        self.assertLessEqual(len(s["text"]), 40_000 + 60)
        self.assertTrue(s["text"].startswith("[user] 请修复 A"))
        self.assertIn("[final] 最终：done", s["text"])  # tail kept... final is the last line

    def test_line_cap_newest_first_and_cut_recorded(self):
        sessions = [{"id": "s%d" % i, "mtime": i, "text": "t" * 60_000, "host": "mac", "source": "codex"} for i in range(4)]
        b = dream.build_bundle("t", "L", project(), self.events, NOW - dt.timedelta(hours=24), sessions, [], self.fake,
                               dream.Guard())
        self.assertEqual([s["id"] for s in b["sessions"]], ["s3", "s2"])
        self.assertEqual(b["cut_sessions"], ["s1", "s0"])


class Mapping(Base):
    def test_assignment(self):
        state = core.fold(project(), self.events)
        S = lambda sid, cwd, handles=(): {"id": sid, "cwd": cwd, "handles": list(handles)}
        out = dream.assign_sessions([S("a", WT), S("b", "/x", [W1]), S("c", "/x", ["term_unknown-1234"]),
                                     S("d", "/q", [R])], project(), state)
        self.assertEqual([s["id"] for s in out["L"]], ["a", "b", "d"])
        self.assertEqual([s["id"] for s in out["unassigned"]], ["c"])


class Preamble(unittest.TestCase):
    def test_orca_preamble_dropped_handles_kept(self):
        d = tempfile.mkdtemp()
        sess = pathlib.Path(d, ".codex", "sessions", "2026", "11", "05")
        sess.mkdir(parents=True)
        f = sess / "rollout-x.jsonl"
        pre = "You are working inside Orca.\nYour coordinator's terminal handle is: term_2dd2525a-3456\n" + "help " * 900
        msg = pre + "\n=== TASK ===\nfix the cache bug"
        f.write_text(json.dumps({"type": "session_meta", "payload": {"id": "x", "cwd": "/w/autotaskgen"}}) + "\n" +
                     json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user",
                                 "content": [{"type": "input_text", "text": msg}]}}) + "\n")
        old = os.environ.get("HOME")
        os.environ["HOME"] = d
        try:
            out = dream._EX["extract"]({"since": 0, "scopes": ["autotaskgen"], "hashes": [], "ns": [],
                                         "head": 10000, "tail": 30000, "host": "mac"})
        finally:
            os.environ["HOME"] = old
        s = out["sessions"][0]
        self.assertIn("fix the cache bug", s["text"])
        self.assertNotIn("help help", s["text"])
        self.assertIn("term_2dd2525a-3456", s["handles"])


class Reports(unittest.TestCase):
    def test_relative_report_read_from_node_worktree_first(self):
        import supervisor
        from unittest import mock
        seen = []

        def fake_read(path, runner):
            seen.append(path)
            return ("report body", 0.0, "") if path.startswith("/home/u/wt/") else (None, None, "missing")
        with mock.patch.object(supervisor, "_read_file", fake_read):
            text = dream.read_report("docs/validation/X/REPORT.md", {}, {}, None, "/home/u/wt")
        self.assertEqual(text, "report body")
        self.assertEqual(seen, ["/home/u/wt/docs/validation/X/REPORT.md"])


class Holdout(Base):
    def make_holdout(self):
        d = self.home / ".dse_holdout"
        d.mkdir()
        (d / (TERM_A + ".json")).write_text(json.dumps({"case_id": "inv_case_9931", "note": TERM_B}) + "\n" + TERM_B + "\n")

    def leak_check(self):
        blobs = [self.prompt_log.read_text() if self.prompt_log.exists() else ""]
        for p in pathlib.Path(self.store).rglob("*") if self.store.exists() else []:
            if p.is_file() and ".git" not in p.parts:
                blobs.append(p.read_text())
        for s in self.fake.stdins:
            blobs.append(s)
        for b in blobs:
            for term in (TERM_A, TERM_B, "inv_case_9931"):
                self.assertNotIn(term, b)

    def test_session_referencing_path_is_excluded_and_counted(self):
        self.make_holdout()
        self.codex("bad", "/a/autotaskgen", extra=[{"type": "response_item", "payload": {"type": "function_call", "name": "shell",
                                                    "arguments": json.dumps({"cmd": "ls ~/.dse_holdout"})}}])
        self.codex("good", "/a/autotaskgen")
        g = dream.Guard.from_holdout()
        sessions, stats = dream.collect_sessions(NOW - dt.timedelta(hours=24), g, self.fake, "wsl")
        self.assertEqual([s["id"] for s in sessions], ["good"])
        self.assertEqual(stats["excluded_holdout"], 1)

    def test_name_inside_chinese_prose_is_denied_alone(self):
        d = self.home / ".dse_holdout"
        d.mkdir()
        (d / "H.md").write_text("# 冻结集\n世界模型：zork-flux7-3b（视频扩散，时空注意力），一次去噪步，约 2026-11-06 冻结\n")
        g = dream.Guard.from_holdout()
        self.assertTrue(g.hit("we profiled zork-flux7-3b on the board"))
        self.assertTrue(g.hit("改用 zork-flux7-3b。"))
        self.assertFalse(g.hit("ordinary note written 2026-11-06 about the dev set"))
        self.assertFalse(g.hit("一次去噪步的开销"))

    def test_extractor_never_reads_holdout_dir(self):
        self.make_holdout()
        os.chmod(str(self.home / ".dse_holdout"), 0)
        try:
            self.codex("good", "/a/autotaskgen")
            sessions, _ = dream.collect_sessions(NOW - dt.timedelta(hours=24), dream.Guard(), self.fake, "wsl")
            self.assertEqual(len(sessions), 1)
        finally:
            os.chmod(str(self.home / ".dse_holdout"), 0o700)

    def test_denylist_lines_dropped_local_and_wsl_hashed(self):
        self.make_holdout()
        extra = [{"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "look at %s now" % TERM_A}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "see Invented Holdout Case Phrase 8812."}]}}]
        self.codex("loc", "/a/autotaskgen", extra=extra)
        jl(self.wsl / ".codex" / "sessions" / "w.jsonl", codex_rows("/home/u/autotaskgen", "wsl-1", extra=extra))
        g = dream.Guard.from_holdout()
        sessions, stats = dream.collect_sessions(NOW - dt.timedelta(hours=24), g, self.fake, "wsl")
        self.assertEqual({s["host"] for s in sessions}, {"mac", "wsl"})
        for s in sessions:
            self.assertNotIn(TERM_A, s["text"])
            self.assertNotIn("8812", s["text"])
            self.assertIn("[user] 请修复 A", s["text"])
        self.assertEqual(stats["dropped_lines"], 4)
        self.assertTrue(self.fake.stdins)
        for sent in self.fake.stdins:                       # terms never travel in clear
            self.assertNotIn(TERM_A, sent)
            self.assertNotIn("8812", sent)

    def test_term_never_reaches_prompt_store_or_runs(self):
        self.make_holdout()
        extra = [{"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "leak %s" % TERM_A}]}}]
        self.codex("loc", "/a/autotaskgen", extra=extra)
        # also in an event summary and a report: scrubbed at prompt assembly
        evs = list(self.events)
        evs.append(ev("node_heartbeat", "L", "a", 10, summary="touching %s" % TERM_A))
        self.write_events(evs)
        (pathlib.Path(os.environ["PROGRESS_REPO"]) / "docs" / "REPORT_a.md").write_text("# r\nuses %s here\nfine line\n" % TERM_B)
        res = self.run_dream()
        self.assertTrue(res["ok"], res)
        self.assertIn("fine line", self.prompt_log.read_text())
        self.leak_check()


class Pipeline(Base):
    def test_full_run(self):
        self.codex("c1", WT)
        res = self.run_dream()
        self.assertTrue(res["ok"], res)
        self.assertIn("先跑测试再交付", (self.store / "L" / "LESSONS.md").read_text())
        sug = (self.store / "L" / "STATE_SUGGESTIONS.md").read_text()
        self.assertIn("A 仍失败", sug)
        runs = list((self.store / "runs").glob("*.json"))
        self.assertEqual([p.name for p in runs], ["2026-11-02.json"])
        self.assertIn("先跑测试再交付", runs[0].read_text())
        log = subprocess.run(["git", "log", "--oneline"], cwd=str(self.store), capture_output=True, text=True).stdout
        self.assertIn("dream 2026-11-02", log)
        self.assertTrue(any(c[0] == "rsync" and "t/memory/" in c[-1] for c in self.fake.calls))
        prompts = self.prompt_log.read_text()
        self.assertIn("pytest test_a failed", prompts)
        self.assertIn("# report a", prompts)
        self.assertNotIn("r" * 6001, prompts)
        obs = [e for e in core.read_events("t") if e.get("kind") == "observe"]
        self.assertEqual(obs[-1]["refs"]["key"], "dream:2026-11-02")
        self.assertEqual(res["notify"]["sent"], ["L"])
        self.assertIn("[每晚整理] L：新增 1 条经验、1 处 STATE", self.fake.sent[0])
        # STATE.md is never touched
        self.assertFalse(list(self.store.rglob("STATE.md")))

    def test_notify_only_when_screen_idle_then_retry(self):
        self.codex("c1", WT)
        self.fake.idle = False
        res = self.run_dream()
        self.assertEqual((res["notify"]["sent"], res["notify"]["waiting"]), ([], ["L"]))
        self.assertEqual(self.fake.sent, [])
        self.fake.idle = True
        res = self.run_dream(since="2026-11-02T11:00:00Z")      # nothing new; pending message still goes out
        self.assertEqual(self.fake.sent and len(self.fake.sent), 1)

    def test_global_lift_with_two_lines(self):
        self.codex("c1", WT)
        self.write_events(self.events + [ev("node_plan", "M", "b", 40, handle="term_other-ffff")])
        res = self.run_dream()
        self.assertTrue(res["ok"], res)
        self.assertIn("共通规则", (self.store / "GLOBAL.md").read_text())

    def test_validation_fallback_keeps_old_lessons(self):
        self.codex("c1", WT)
        self.assertTrue(self.run_dream()["ok"])
        old = (self.store / "L" / "LESSONS.md").read_text()
        os.environ["DREAM_MODE"] = "bad"
        res = dream.run("t", runner=self.fake, now=NOW + dt.timedelta(days=1), since="2026-11-02T00:00:00Z")
        self.assertEqual((self.store / "L" / "LESSONS.md").read_text(), old)
        self.assertTrue(res["consolidate"]["outcomes"]["L"]["status"].startswith("invalid"))
        os.environ["DREAM_MODE"] = "rc"
        dream.run("t", runner=self.fake, now=NOW + dt.timedelta(days=1), since="2026-11-02T00:00:00Z")
        self.assertEqual((self.store / "L" / "LESSONS.md").read_text(), old)

    def test_head_moved_redo_then_ok(self):
        self.codex("c1", WT)
        moved = []

        def bump(store):
            if not moved:
                moved.append(1)
                (store / "other.txt").write_text("x")
                dream.git(["add", "other.txt"], store)
                dream.git(["commit", "-q", "-m", "someone else"], store)
        res = self.run_dream(before_push=bump)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["consolidate"]["attempts"], 2)
        self.assertTrue((self.store / "other.txt").exists())
        self.assertTrue((self.store / "L" / "LESSONS.md").exists())

    def test_head_moved_twice_gives_up(self):
        self.codex("c1", WT)
        n = [0]

        def bump(store):
            n[0] += 1
            (store / ("o%d.txt" % n[0])).write_text("x")
            dream.git(["add", "-A"], store)
            dream.git(["commit", "-q", "-m", "other %d" % n[0]], store)
        res = self.run_dream(before_push=bump)
        self.assertFalse(res["ok"])
        self.assertEqual(res["consolidate"]["error"], "store_head_moved")
        self.assertFalse((self.store / "L").exists())
        self.assertNotIn("notify", res)

    def test_unreachable_wsl_does_not_stop_run(self):
        self.codex("c1", WT)
        orig = self.fake.__call__

        def broken(argv, input_text=None, timeout=60):
            if argv[0] == "ssh":
                return 255, "", "no route"
            return orig(argv, input_text, timeout)
        res = dream.run("t", runner=broken, now=NOW)
        self.assertTrue(res["ok"], res)
        self.assertIn("error", res["stats"]["hosts"]["wsl"])


class Expiry(unittest.TestCase):
    def test_expired_rules_dropped_after_seven_days(self):
        text = "# t\n- [已过期 2026-10-20] 旧（证据：a；日期：2026-10-01）\n- [已过期 2026-10-30] 新近（证据：a；日期：2026-10-01）\n- 活（证据：a；日期：2026-11-01）"
        out = dream.expire(text, "2026-11-02")
        self.assertNotIn("旧", out)
        self.assertIn("新近", out)
        self.assertIn("活", out)

    def test_undated_marker_is_stamped(self):
        self.assertIn("已过期 2026-11-02", dream.stamp_expired("- [已过期] x（证据：a；日期：2026-11-01）", "2026-11-02"))

    def test_validate(self):
        ok = "# a\n- r（证据：e；日期：2026-11-02）"
        self.assertIsNone(dream.validate_md(ok))
        self.assertEqual(dream.validate_md("# a\n- r（日期：2026-11-02）"), "rule_without_evidence_or_date")
        self.assertTrue(dream.validate_md("# a\n" + "\n".join("- r（证据：e；日期：2026-11-02）" for _ in range(130))).startswith("too_long"))
        self.assertEqual(dream.validate_md("   "), "empty")


class Metrics(Base):
    def test_weekly_table(self):
        evs = [ev("node_plan", "L", "a", 600),
               ev("node_deliver", "L", "a", 200, role="worker", handle=W1, outcome="FAIL", failures=["pytest test_a failed"]),
               ev("node_deliver", "L", "b", 100, role="worker", handle=W1, outcome="FAIL", failures=["pytest test_a failed"]),
               ev("node_review", "L", "a", 90, outcome="FAIL"), ev("node_review", "L", "a", 80, outcome="PASS"),
               ev("nudge", "L", None, 50, role="patrol", handle="patrol@mac", reason="resume", summary="resume"),
               ev("node_plan", "L", "c", 35)]
        md = dream.compute_metrics(project(), evs, {"L": "# l\n- 别再让 pytest test_a failed（证据：e；日期：2026-11-01）"}, "", NOW)
        row = [l for l in md.splitlines() if l.startswith("| L ")][0]
        self.assertEqual([c.strip() for c in row.strip("|").split("|")], ["L", "2", "15", "1", "1/2", "1.00"])

    def test_appended_on_monday_only_once(self):
        self.codex("c1", WT)
        self.assertTrue(self.run_dream()["ok"])
        self.assertIn("## 周报 2026-11-02", (self.store / "METRICS.md").read_text())
        res = self.run_dream()
        self.assertEqual((self.store / "METRICS.md").read_text().count("## 周报 2026-11-02"), 1)
        tue = dream.run("t", runner=self.fake, now=NOW + dt.timedelta(days=1))
        self.assertEqual((self.store / "METRICS.md").read_text().count("## 周报"), 1)


class DryRunAndLock(Base):
    def test_dry_run_writes_nothing_and_calls_no_model(self):
        self.codex("c1", WT)
        before = self.tree(self.home)
        res = self.run_dream(dry_run=True)
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["stats"]["sessions"], 1)
        self.assertEqual(res["stats"]["lines"]["L"]["sessions"], 1)
        self.assertEqual(self.tree(self.home), before)
        self.assertFalse(self.prompt_log.exists())
        self.assertFalse(self.store.exists())
        self.assertEqual(self.fake.sent, [])

    def test_lock_blocks_second_run(self):
        self.codex("c1", WT)
        with dream.run_lock("t") as got:
            self.assertTrue(got)
            res = self.run_dream()
            self.assertEqual(res, {"ok": False, "error": "locked"})
        self.assertTrue(self.run_dream()["ok"])


if __name__ == "__main__":
    unittest.main()


class CompoundIdGuard(unittest.TestCase):
    def test_name_inside_compound_id_is_caught(self):
        import tempfile as _tf
        d = pathlib.Path(_tf.mkdtemp()) / "h"
        d.mkdir()
        (d / "c.txt").write_text("zq_invented_case_9\nfoo-barbaz\n", encoding="utf-8")
        g = dream.Guard.from_holdout(d)
        for s in ("evb-x-zq_invented_case_9", "evb-zq_invented_case_9-1105", "node evb-foo-barbaz-1 done"):
            self.assertTrue(g.hit(s), s)
        for s in ("evb-x-1105", "zq_invented_case_8", "foo-bar"):
            self.assertFalse(g.hit(s), s)
