"""workpush / push_backfill: local bare repos and a fake guard in temp dirs only (no GitHub, ssh, orca, hermes)."""
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import progress_core as core  # noqa: E402
import push_backfill  # noqa: E402
import workpush  # noqa: E402

R = "term_router-aaaa"
W = "term_worker-dddd"
FAKE_GUARD = """#!%s
import sys
assert sys.argv[1] == "--guard"
data = sys.stdin.buffer.read() if sys.argv[2] == "-" else open(sys.argv[2], "rb").read()
with open(%r, "a") as fh:
    fh.write(data.split(b"\\n", 1)[0].decode() + "\\n")
if b"SALT-SECRET" in data:
    print("HIT salt-value"); sys.exit(1)
if b"HARD-HIT" in data:
    print("HIT hard-content"); sys.exit(1)
if b"UNIT-TOKEN" in data:
    print("HIT unit-token"); sys.exit(1)
if b"GUARD-ERR" in data:
    sys.exit(2)
print("CLEAN")
"""
ENV_KEYS = ("PROGRESS_HOME", "PROGRESS_PUSH", "PROGRESS_PUSH_GUARD", "PROGRESS_PUSH_CACHE", "PROGRESS_PUSH_DSE_CACHE",
            "PROGRESS_PUSH_BACKFILL_STATE", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_AUTHOR_NAME",
            "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")


def project():
    return {"schema": core.SCHEMA_PROJECT, "id": "t", "name": "test",
            "final_goal": {"text": "goal", "done_when": "all"}, "humans": ["term_human-eeee"],
            "cores": [{"id": "c1", "name": "one", "coordinator": "term_coord1-bbbb"}],
            "lines": [{"id": "L", "name": "line", "core": "c1", "router": R}], "milestones": []}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = pathlib.Path(self.tmp.name)
        self.saved = {k: os.environ.get(k) for k in ENV_KEYS}
        self.guard_log = self.t / "guard.log"
        guard = self.t / "guard.py"
        guard.write_text(FAKE_GUARD % (sys.executable, str(self.guard_log)))
        guard.chmod(0o755)
        os.environ.update(PROGRESS_HOME=str(self.t / "store"), PROGRESS_PUSH_GUARD=str(guard),
                          PROGRESS_PUSH_CACHE=str(self.t / "cache"), PROGRESS_PUSH_DSE_CACHE=str(self.t / "dse"),
                          PROGRESS_PUSH_BACKFILL_STATE=str(self.t / "bf.json"),
                          GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                          GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x", GIT_COMMITTER_NAME="t",
                          GIT_COMMITTER_EMAIL="t@x")
        os.environ.pop("PROGRESS_PUSH", None)
        self.origin = str(self.t / "origin.git")
        self.git(str(self.t), "init", "-q", "--bare", "-b", "main", self.origin)
        seed = self.clone("seed")
        self.commit(seed, "README.md", "hello\n")
        self.git(seed, "push", "-q", "origin", "main")

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def git(self, cwd, *args):
        r = subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def clone(self, name, branch=None):
        d = str(self.t / name)
        subprocess.run(["git", "clone", "-q", self.origin, d], check=True, capture_output=True)
        if branch:
            self.git(d, "checkout", "-q", "-b", branch)
        return d

    def commit(self, repo, path, text, msg="c"):
        p = pathlib.Path(repo) / path
        p.parent.mkdir(parents=True, exist_ok=True)
        if text is None:
            self.git(repo, "rm", "-q", path)
        else:
            p.write_text(text)
            self.git(repo, "add", path)
        self.git(repo, "commit", "-q", "-m", msg)
        return self.git(repo, "rev-parse", "HEAD")

    def remote(self, branch):
        out = self.git(str(self.t), "ls-remote", self.origin, "refs/heads/" + branch)
        return out.split()[0] if out else None

    def push(self, repo, **kw):
        return workpush.push_work_branch(repo, **kw)


class Push(Base):
    def test_pushes_branch_and_reports_remote_sha(self):
        w = self.clone("w", "you/evb-a")
        sha = self.commit(w, "a.txt", "work\n")
        res = self.push(w)
        self.assertEqual((res["status"], res["branch"], res["sha"], res["remote"]),
                         ("pushed", "you/evb-a", sha, "origin"), res)
        self.assertEqual(self.remote("you/evb-a"), sha)
        again = self.push(w)
        self.assertEqual((again["status"], again.get("already")), ("pushed", True))

    def test_main_and_master_skipped(self):
        w = self.clone("w")
        before = self.remote("main")
        self.commit(w, "a.txt", "on main\n")
        res = self.push(w)
        self.assertEqual(res["status"], "skipped")
        self.assertIn("main", res["why"])
        self.assertEqual(self.remote("main"), before)

    def test_detached_no_origin_not_repo_skipped(self):
        w = self.clone("w")
        self.git(w, "checkout", "-q", "--detach")
        self.assertIn("detached", self.push(w)["why"])
        lone = str(self.t / "lone")
        self.git(str(self.t), "init", "-q", "-b", "you/x", lone)
        self.commit(lone, "a", "a\n")
        self.assertEqual(self.push(lone)["why"], "no origin remote")
        plain = self.t / "plain"
        plain.mkdir()
        self.assertIn("not a git work tree", self.push(str(plain))["why"])

    def test_progress_push_off_skips(self):
        w = self.clone("w", "you/evb-a")
        self.commit(w, "a.txt", "work\n")
        os.environ["PROGRESS_PUSH"] = "off"
        res = self.push(w)
        self.assertEqual((res["status"], res["why"]), ("skipped", "PROGRESS_PUSH=off"))
        self.assertIsNone(self.remote("you/evb-a"))

    def test_non_ff_rebases_then_pushes(self):
        w = self.clone("w", "you/evb-a")
        self.commit(w, "a.txt", "work\n")
        self.push(w)
        other = self.clone("o")
        self.git(other, "checkout", "-q", "you/evb-a")
        theirs = self.commit(other, "b.txt", "theirs\n")
        self.git(other, "push", "-q", "origin", "you/evb-a")
        mine = self.commit(w, "c.txt", "mine\n")
        res = self.push(w)
        self.assertEqual(res["status"], "pushed", res)
        self.assertTrue(res["rebased"])
        self.assertEqual(res["old_sha"], mine)
        self.assertNotEqual(res["sha"], mine)
        self.assertEqual(self.remote("you/evb-a"), res["sha"])
        self.assertEqual(self.git(w, "merge-base", "--is-ancestor", theirs, res["sha"]), "")

    def test_rebase_conflict_fails_and_leaves_repo_clean(self):
        w = self.clone("w", "you/evb-a")
        self.commit(w, "a.txt", "base\n")
        self.push(w)
        other = self.clone("o")
        self.git(other, "checkout", "-q", "you/evb-a")
        self.commit(other, "a.txt", "theirs\n")
        self.git(other, "push", "-q", "origin", "you/evb-a")
        theirs = self.remote("you/evb-a")
        mine = self.commit(w, "a.txt", "mine\n")
        res = self.push(w)
        self.assertEqual(res["status"], "failed")
        self.assertIn("conflicted", res["error"])
        self.assertEqual(self.git(w, "rev-parse", "HEAD"), mine)
        self.assertEqual(self.git(w, "status", "--porcelain"), "")
        self.assertFalse((pathlib.Path(w) / ".git" / "rebase-merge").exists())
        self.assertEqual(self.remote("you/evb-a"), theirs)

    def test_guard_salt_blocks_nothing_pushed(self):
        w = self.clone("w", "you/evb-a")
        self.commit(w, "cfg/x.txt", "v=SALT-SECRET\n")
        res = self.push(w)
        self.assertEqual(res["status"], "failed")
        self.assertIn("S 类", res["error"])
        self.assertNotIn("SALT-SECRET", json.dumps(res))
        self.assertIsNone(self.remote("you/evb-a"))

    def test_secret_added_then_deleted_still_blocks(self):
        w = self.clone("w", "you/evb-a")
        self.commit(w, "x.txt", "SALT-SECRET\n")
        self.commit(w, "x.txt", None)
        res = self.push(w)
        self.assertEqual(res["status"], "failed", res)
        self.assertIsNone(self.remote("you/evb-a"))

    def test_blob_already_on_origin_not_rescanned(self):
        a = self.clone("a", "you/fe-old")           # the file reached origin long ago (another branch)
        self.commit(a, "raw/runs.tar.gz", "HARD-HIT\n")
        self.git(a, "push", "-q", "origin", "you/fe-old")
        w = self.clone("w")
        self.git(w, "checkout", "-q", "-b", "fe-dev", "origin/you/fe-old")
        self.commit(w, "notes.md", "new work\n")
        res = self.push(w)
        self.assertEqual(res["status"], "pushed", res)
        self.assertEqual(self.remote("fe-dev"), self.git(w, "rev-parse", "HEAD"))
        self.assertNotIn("raw/runs.tar.gz", self.guard_log.read_text().splitlines())  # only the new blob was scanned

    def test_block_names_script_rule_and_sha(self):
        w = self.clone("w", "you/evb-a")
        self.commit(w, "raw/runs.tar.gz", "HARD-HIT\n")
        res = self.push(w)
        self.assertEqual(res["status"], "failed")
        for part in ("H 类（敏感内容） raw/runs.tar.gz", "--guard", "workpush.py 第 3 条规则", "sha256 " + hashlib.sha256(b"HARD-HIT\n").hexdigest(),
                     ".push-allow.json"):
            self.assertIn(part, res["error"])
        self.assertNotIn("HARD-HIT", res["error"])

    def test_every_blocked_file_listed_once_with_paste_safe_labels(self):
        # 11-06 fe-dev: three blocked files surfaced one push at a time, and the raw category name pasted next to
        # the sha into STATE.md tripped the same shape rule again. One push now lists all hits, labels only.
        w = self.clone("w", "you/evb-a")
        self.commit(w, "a/state.md", "v=SALT-SECRET\n")
        self.commit(w, "raw/runs.tar.gz", "HARD-HIT\n")
        res = self.push(w)
        self.assertEqual(res["status"], "failed")
        err = res["error"]
        self.assertIn("拦下 2 个文件", err)
        for part in ("S 类（留出集相关） a/state.md", "H 类（敏感内容） raw/runs.tar.gz",
                     hashlib.sha256(b"v=SALT-SECRET\n").hexdigest()):
            self.assertIn(part, err)
        for raw in ("salt-value", "hard-content", "SALT-SECRET", "HARD-HIT"):
            self.assertNotIn(raw, err)
        self.assertIsNone(self.remote("you/evb-a"))

    def test_allow_list_store_and_origin_main(self):
        h = hashlib.sha256(b"HARD-HIT\n").hexdigest()
        allow = self.t / "allow.json"
        os.environ["PROGRESS_PUSH_ALLOW"] = str(allow)
        try:
            allow.write_text(json.dumps({"allow": [{"path": "raw/runs.tar.gz", "sha256": h, "why": "扫过 665 个成员 0 命中",
                                                    "approved_by": "lead L24"}]}))
            w = self.clone("w", "you/evb-a")
            self.commit(w, "raw/runs.tar.gz", "HARD-HIT\n")
            res = self.push(w)
            self.assertEqual(res["status"], "pushed", res)
            self.assertEqual(res["allowed"][0]["path"], "raw/runs.tar.gz")
            # same path, other content: scanned again and blocked
            self.commit(w, "raw/runs.tar.gz", "HARD-HIT 2\n")
            self.assertEqual(self.push(w)["status"], "failed")
            # entries missing why/approved_by do not count
            allow.write_text(json.dumps({"allow": [{"path": "raw/x.gz", "sha256": h}]}))
            w2 = self.clone("w2", "you/evb-b")
            self.commit(w2, "raw/x.gz", "HARD-HIT\n")
            self.assertEqual(self.push(w2)["status"], "failed")
            allow.unlink()
            # in-repo list on origin main counts; the same list on the pushed branch does not
            w3 = self.clone("w3", "you/evb-c")
            self.commit(w3, ".push-allow.json", json.dumps([{"path": "raw/y.gz", "sha256": h, "why": "w", "approved_by": "me"}]))
            self.commit(w3, "raw/y.gz", "HARD-HIT\n")
            self.assertEqual(self.push(w3)["status"], "failed")
            m = self.clone("m")
            self.commit(m, ".push-allow.json", json.dumps([{"path": "raw/y.gz", "sha256": h, "why": "owner", "approved_by": "8f"}]))
            self.git(m, "push", "-q", "origin", "main")
            self.git(w3, "fetch", "-q", "origin")
            res = self.push(w3)
            self.assertEqual(res["status"], "pushed", res)
            self.assertEqual(res["allowed"][0]["source"], "origin/main:.push-allow.json")
        finally:
            os.environ.pop("PROGRESS_PUSH_ALLOW", None)

    def test_non_secret_category_does_not_block(self):
        w = self.clone("w", "you/evb-a")
        self.commit(w, "x.txt", "UNIT-TOKEN\n")
        self.assertEqual(self.push(w)["status"], "pushed")

    def test_guard_error_and_missing_guard_fail_closed(self):
        w = self.clone("w", "you/evb-a")
        self.commit(w, "x.txt", "GUARD-ERR\n")
        res = self.push(w)
        self.assertEqual(res["status"], "failed")
        self.assertIn("guard error", res["error"])
        w2 = self.clone("w2", "you/evb-b")
        self.commit(w2, "y.txt", "fine\n")
        os.environ["PROGRESS_PUSH_GUARD"] = str(self.t / "nope.sh")
        res = self.push(w2)
        self.assertEqual(res["status"], "failed")
        self.assertIn("guard not found", res["error"])
        self.assertIsNone(self.remote("you/evb-a"))
        self.assertIsNone(self.remote("you/evb-b"))

    def test_named_exclusion_in_tree_and_history(self):
        w = self.clone("w", "you/evb-a")
        self.commit(w, "data/DiTPA_holdout_v1.csv", "x\n")
        res = self.push(w)
        self.assertEqual(res["status"], "failed")
        self.assertIn("named exclusion in the pushed tree", res["error"])
        self.assertNotIn("DiTPA_holdout", res["error"])
        self.commit(w, "data/DiTPA_holdout_v1.csv", None)
        res = self.push(w)
        self.assertIn("named exclusion in the pushed history", res["error"])
        self.assertIsNone(self.remote("you/evb-a"))

    def test_clean_verdicts_cached(self):
        w = self.clone("w", "you/evb-a")
        self.commit(w, "a.txt", "a\n")
        self.push(w)
        n = len(self.guard_log.read_text().splitlines())
        self.commit(w, "b.txt", "b\n")
        self.push(w)
        new = self.guard_log.read_text().splitlines()[n:]
        self.assertEqual(new, ["b.txt"])  # README / a.txt came from the cache

    def test_head_selects_its_worktree(self):
        w = self.clone("w")
        wt = str(self.t / "wt-b")
        self.git(w, "worktree", "add", "-q", "-b", "you/evb-b", wt)
        sha = self.commit(wt, "b.txt", "b\n")
        res = self.push(w, head=sha[:10])
        self.assertEqual((res["status"], res["branch"]), ("pushed", "you/evb-b"), res)
        self.assertEqual(self.remote("you/evb-b"), sha)

    def test_remote_guard_over_fake_ssh(self):
        calls = []

        def runner(cmd, cwd, data, timeout, env):
            if cmd[0] == "ssh":
                calls.append(json.loads(data)["op"])
                return 0, json.dumps(workpush.serve_guard_scan(json.loads(data))).encode(), b""
            return workpush.default_runner(cmd, cwd, data, timeout, env)

        w = self.clone("w", "you/evb-a")
        self.commit(w, "a.txt", "a\n")
        res = self.push(w, runner=runner, guard=str(self.t / "absent.sh"), guard_host="fakehost")
        self.assertEqual(res["status"], "pushed", res)
        self.assertEqual(calls, ["cached", "scan"])
        self.commit(w, "s.txt", "SALT-SECRET\n")
        res = self.push(w, runner=runner, guard=str(self.t / "absent.sh"), guard_host="fakehost")
        self.assertIn("S 类", res["error"])

        def down(cmd, cwd, data, timeout, env):
            if cmd[0] == "ssh":
                return 255, b"", b"ssh: connect refused"
            return workpush.default_runner(cmd, cwd, data, timeout, env)
        self.commit(w, "c.txt", "c\n")
        res = self.push(w, runner=down, guard=str(self.t / "absent.sh"), guard_host="fakehost")
        self.assertEqual(res["status"], "failed")
        self.assertIn("guard host", res["error"])

    def test_event_fields(self):
        self.assertEqual(workpush.event_fields({"status": "pushed", "branch": "b", "sha": "a" * 40}),
                         {"pushed": {"branch": "b", "sha": "a" * 40}})
        self.assertEqual(workpush.event_fields({"status": "failed", "error": "x"}), {"push_failed": "x"})
        self.assertEqual(workpush.event_fields({"status": "skipped", "why": "y"}), {"push_skipped": "y"})


class Cli(Base):
    # plans here are stamped with the real clock, after the R1 cut-off, and have no spec file: the
    # prerequisite check is tested in test_prereq, not here
    _precheck = None

    @classmethod
    def setUpClass(cls):
        cls._precheck = os.environ.get("PROGRESS_PRECHECK")
        os.environ["PROGRESS_PRECHECK"] = "0"

    @classmethod
    def tearDownClass(cls):
        if cls._precheck is None:
            os.environ.pop("PROGRESS_PRECHECK", None)
        else:
            os.environ["PROGRESS_PRECHECK"] = cls._precheck

    def setUp(self):
        super().setUp()
        core.save_project(project())

    def cli(self, cwd, *args, handle=W):
        env = dict(os.environ, ORCA_TERMINAL_HANDLE=handle, PROGRESS_PROJECT="t")
        r = subprocess.run([sys.executable, str(HERE / "progress.py")] + list(args), env=env, cwd=cwd,
                           text=True, capture_output=True)
        return r.returncode, json.loads(r.stdout.strip().splitlines()[-1])

    def node(self):
        d = str(self.t)
        self.assertEqual(self.cli(d, "plan", "--line", "L", "--node", "a", "--summary", "排", handle=R)[0], 0)
        self.cli(d, "dispatch", "--line", "L", "--node", "a", "--terminal", W, "--summary", "派", handle=R)

    def last(self, kind):
        return [e for e in core.read_events("t") if e["kind"] == kind][-1]

    def test_deliver_event_carries_pushed_sha(self):
        self.node()
        w = self.clone("w", "you/evb-a")
        sha = self.commit(w, "a.txt", "a\n")
        code, out = self.cli(w, "deliver", "--line", "L", "--node", "a", "--outcome", "PASS", "--head", sha,
                             "--summary", "交付")
        self.assertEqual((code, out["ok"], out["push"]["status"]), (0, True, "pushed"), out)
        ev = self.last("node_deliver")
        self.assertEqual(ev["pushed"], {"branch": "you/evb-a", "sha": sha})
        self.assertEqual(self.remote("you/evb-a"), sha)
        n = core.fold(core.load_project("t"), core.read_events("t"))["lines"]["L"]["nodes"]["a"]
        self.assertEqual(n["pushed"]["sha"], sha)

    def test_deliver_after_rebase_records_new_head(self):
        self.node()
        w = self.clone("w", "you/evb-a")
        self.commit(w, "a.txt", "a\n")
        self.push(w)
        other = self.clone("o")
        self.git(other, "checkout", "-q", "you/evb-a")
        self.commit(other, "b.txt", "b\n")
        self.git(other, "push", "-q", "origin", "you/evb-a")
        mine = self.commit(w, "c.txt", "c\n")
        code, out = self.cli(w, "deliver", "--line", "L", "--node", "a", "--outcome", "PASS", "--head", mine,
                             "--summary", "交付")
        self.assertEqual(code, 0, out)
        ev = self.last("node_deliver")
        self.assertEqual(ev["refs"]["head"], ev["pushed"]["sha"])
        self.assertNotEqual(ev["refs"]["head"], mine)
        self.assertEqual(self.remote("you/evb-a"), ev["pushed"]["sha"])

    def test_deliver_push_failure_still_written(self):
        self.node()
        w = self.clone("w", "you/evb-a")
        self.commit(w, "x.txt", "SALT-SECRET\n")
        code, out = self.cli(w, "deliver", "--line", "L", "--node", "a", "--outcome", "PASS", "--summary", "交付")
        self.assertEqual((code, out["ok"], out["push"]["status"]), (0, True, "failed"))
        ev = self.last("node_deliver")
        self.assertIn("S 类", ev["push_failed"])
        self.assertNotIn("pushed", ev)
        self.assertIsNone(self.remote("you/evb-a"))

    def test_deliver_on_main_skipped(self):
        self.node()
        w = self.clone("w")
        self.commit(w, "a.txt", "a\n")
        code, out = self.cli(w, "deliver", "--line", "L", "--node", "a", "--outcome", "PASS", "--summary", "交付")
        self.assertEqual(out["push"]["status"], "skipped")
        self.assertIn("main", self.last("node_deliver")["push_skipped"])

    def test_review_pushes_router_branch(self):
        self.node()
        w = self.clone("w", "you/evb-a")
        wsha = self.commit(w, "a.txt", "a\n")
        self.cli(w, "deliver", "--line", "L", "--node", "a", "--outcome", "PASS", "--head", wsha, "--summary", "交付")
        r = self.clone("r", "you/evb-line")
        rsha = self.commit(r, "merged.txt", "accepted\n")
        code, out = self.cli(r, "review", "--line", "L", "--node", "a", "--outcome", "PASS", "--head", wsha,
                             "--summary", "接受", handle=R)
        self.assertEqual((code, out["push"]["status"], out["push"]["branch"]), (0, "pushed", "you/evb-line"))
        ev = self.last("node_review")
        self.assertEqual(ev["pushed"], {"branch": "you/evb-line", "sha": rsha})
        self.assertEqual(ev["refs"]["head"], wsha)
        self.assertEqual(self.remote("you/evb-line"), rsha)

    def test_validate_push_fields(self):
        self.node()
        base = {"kind": "node_deliver", "actor": {"role": "worker", "handle": W}, "summary": "s", "line": "L",
                "node": "a", "outcome": "PASS"}
        for bad in ({"pushed": {"branch": "b"}}, {"pushed": {"branch": "main", "sha": "abcdef0"}},
                    {"push_failed": ""}, {"pushed": {"branch": "b", "sha": "abcdef0"}, "push_failed": "x"}):
            with self.assertRaises(core.ProgressError) as cm:
                core.append("t", dict(base, **bad))
            self.assertIn("bad_push", json.dumps(cm.exception.as_dict()))
        with self.assertRaises(core.ProgressError):
            core.append("t", {"kind": "node_heartbeat", "actor": {"role": "worker", "handle": W}, "summary": "s",
                              "line": "L", "node": "a", "push_skipped": "x"})
        self.assertTrue(core.append("t", dict(base, pushed={"branch": "b", "sha": "abcdef0"}))["ok"])


class Backfill(Base):
    def setUp(self):
        super().setUp()
        self.repo = self.clone("main")
        self.shas = {}
        for br, text in (("you/a-missing", "a\n"), ("you/b-ahead", "b\n"), ("you/c-same", "c\n"),
                         ("you/d-secret", "SALT-SECRET\n"), ("you/e-missing", "e\n")):
            wt = str(self.t / br.split("/")[1])
            self.git(self.repo, "worktree", "add", "-q", "-b", br, wt, "main")
            self.shas[br] = self.commit(wt, br.split("/")[1] + ".txt", text)
            if br in ("you/b-ahead", "you/c-same"):
                self.git(wt, "push", "-q", "origin", br)
        self.shas["you/b-ahead"] = self.commit(str(self.t / "b-ahead"), "b2.txt", "more\n")
        self.git(self.repo, "worktree", "add", "-q", "--detach", str(self.t / "det"), "main")  # detached: not listed

    def test_dry_run_lists_and_respects_batch(self):
        res = push_backfill.run(self.repo, apply=False, batch=1)
        self.assertEqual(sorted(b["branch"] for b in res["candidates"]),
                         ["you/a-missing", "you/d-secret", "you/e-missing"])
        self.assertEqual([b["branch"] for b in res["ahead"]], ["you/b-ahead"])
        self.assertEqual(res["ahead"][0]["ahead_by"], 1)
        self.assertEqual([(b["branch"], "S 类" in b["why"]) for b in res["blocked"]],
                         [("you/d-secret", True)])
        self.assertEqual(res["next_batch"], ["you/a-missing"])
        self.assertEqual(res["counts"], {"branches": 5, "up_to_date": 1, "behind": 0, "missing": 3, "ahead": 1,
                                         "blocked": 1, "next_batch": 1})
        self.assertIsNone(self.remote("you/a-missing"))
        self.assertFalse((self.t / "bf.json").exists())
        self.assertEqual(len(push_backfill.run(self.repo, apply=False, batch=10)["next_batch"]), 3)

    def test_apply_pushes_one_batch_and_resumes(self):
        res = push_backfill.run(self.repo, apply=True, batch=2)
        self.assertEqual(res["pushed"], ["you/a-missing", "you/b-ahead"])
        self.assertEqual(self.remote("you/b-ahead"), self.shas["you/b-ahead"])
        self.assertIsNone(self.remote("you/e-missing"))
        st = json.loads((self.t / "bf.json").read_text())
        self.assertEqual(st["branches"]["you/a-missing"]["status"], "pushed")
        res = push_backfill.run(self.repo, apply=True, batch=2)
        self.assertEqual(res["pushed"], ["you/e-missing"])
        self.assertIsNone(self.remote("you/d-secret"))
        st = json.loads((self.t / "bf.json").read_text())
        self.assertEqual(st["branches"]["you/d-secret"]["status"], "blocked")

    def test_apply_stops_on_index_pack_failure(self):
        def runner(cmd, cwd, data, timeout, env):
            if "push" in cmd:
                return 1, b"", b"error: remote unpack failed: index-pack failed"
            return workpush.default_runner(cmd, cwd, data, timeout, env)
        res = push_backfill.run(self.repo, apply=True, batch=3, runner=runner)
        self.assertEqual(len(res["failed"]), 1)
        self.assertIn("index-pack", res["stopped"])
        self.assertIsNone(self.remote("you/a-missing"))
        st = json.loads((self.t / "bf.json").read_text())
        self.assertEqual(st["branches"]["you/a-missing"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
