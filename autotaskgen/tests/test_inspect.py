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
import snapshot as snap  # noqa: E402
from fakes import FakeRunner, wl_row  # noqa: E402
from test_patrol import project, plan_ev, live, at  # noqa: E402


class InspectBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self.tmp.name, "repo")
        self.repo.mkdir()
        (self.repo / "a.txt").write_text("hello")
        (self.repo / "big.txt").write_text("x" * (300 * 1024))
        outside = pathlib.Path(self.tmp.name, "outside.txt")
        outside.write_text("secret")
        os.symlink(str(outside), str(self.repo / "link.txt"))
        os.environ["PROGRESS_HOME"] = self.tmp.name
        os.environ["PROGRESS_REPO"] = str(self.repo)
        os.environ["PROGRESS_PROJECT"] = "t"
        self.log = str(pathlib.Path(self.tmp.name, "inspect.log"))
        os.environ["PROGRESS_INSPECT_LOG"] = self.log
        core.save_project(project())
        core.append("t", plan_ev("L", "a"))
        d = live(workers=[{"host": "wsl", "workspace": "id::/home/u/ws/L-a", "terminal": "term_w1", "line": "L", "node": "a"}])
        core.save_live("t", d)

    def tearDown(self):
        for k in ("PROGRESS_REPO", "PROGRESS_PROJECT", "PROGRESS_INSPECT_LOG"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def call(self, *argv, runner=None):
        fr = runner or FakeRunner()
        rc, res = ins.run(list(argv), runner=fr, store=snap.Store("t", fr))
        return rc, res, fr

    def logged(self):
        return [json.loads(l) for l in pathlib.Path(self.log).read_text().splitlines()]


class Whitelist(InspectBase):
    def test_unknown_commands_rejected(self):
        for argv in (["rm", "-rf", "/"], ["send", "x"], ["bash"], [], ["file"], ["events"]):
            rc, res, fr = self.call(*argv)
            self.assertEqual(rc, 2, argv)
            self.assertFalse(res["ok"])
            self.assertEqual(fr.calls, [])
        self.assertTrue(all(not r["ok"] for r in self.logged()))

    def test_screen_only_handles(self):
        rc, res, _ = self.call("screen", "/etc/passwd")
        self.assertEqual(rc, 2)
        rc, res, fr = self.call("screen", "term_x", runner=FakeRunner(screen=["hi", "› "]))
        self.assertEqual(rc, 0)
        self.assertEqual(res["tail"], ["hi", "›"])

    def test_screen_dispatch(self):
        fr = FakeRunner(reads={"ctx_1": {"tail": ["working"]}})
        rc, res, _ = self.call("screen", "ctx_1", runner=fr)
        self.assertEqual(res["tail"], ["working"])

    def test_events_node_tasks(self):
        rc, res, _ = self.call("events", "--line", "L", "--node", "a")
        self.assertEqual(res["total"], 1)
        rc, res, _ = self.call("events", "--line", "L", "--since", "2999-01-01T00:00:00Z")
        self.assertEqual(res["total"], 0)
        rc, res, _ = self.call("events", "--line", "L", "--since", "garbage")
        self.assertEqual(rc, 2)
        rc, res, _ = self.call("node", "--line", "L", "--node", "a")
        self.assertEqual(res["node"]["id"], "a")
        self.assertIn("spec_header", res)
        rc, res, _ = self.call("tasks", "--run", "run_x; rm")
        self.assertEqual(rc, 2)
        fr = FakeRunner(wl_rows={"ready": [wl_row("ctx_1", "task_1", run="run_x")]})
        rc, res, _ = self.call("tasks", "--run", "run_x", runner=fr)
        self.assertEqual(res["tasks"][0]["dispatchId"], "ctx_1")


class Paths(InspectBase):
    def test_read_inside_repo(self):
        rc, res, _ = self.call("file", str(self.repo / "a.txt"))
        self.assertEqual((rc, res["text"]), (0, "hello"))

    def test_cap_200kb(self):
        rc, res, _ = self.call("file", str(self.repo / "big.txt"))
        self.assertTrue(res["truncated"])
        self.assertEqual(len(res["text"]), 200 * 1024)

    def test_escapes_rejected(self):
        for p in (str(self.repo / ".." / "outside.txt"), "/etc/passwd", "relative.txt", str(self.repo) + "-evil/a",
                  str(self.repo / "link.txt"), "/home/other/x", "/home/u/ws/L-a/../../secret"):
            rc, res, fr = self.call("file", p)
            self.assertEqual(rc, 2, p)
            self.assertFalse(res["ok"])
            self.assertEqual(fr.kinds("ssh"), [], p)

    def test_wsl_worktree_goes_over_ssh(self):
        fr = FakeRunner()
        rc, res, _ = self.call("file", "/home/u/ws/L-a/src/x.py", runner=fr)
        self.assertEqual(fr.calls[-1][0], "ssh")
        self.assertIn("realpath", fr.calls[-1][-1])

    def test_git_log_scope(self):
        rc, res, _ = self.call("git-log", "/etc")
        self.assertEqual(rc, 2)
        fr = FakeRunner(spec="abc 2026 me msg\n")
        rc, res, _ = self.call("git-log", str(self.repo), "-n", "3", runner=fr)
        self.assertEqual(res["commits"], ["abc 2026 me msg"])
        self.assertEqual(fr.calls[-1][:3], ["git", "-C", str(self.repo)])


class Logging(InspectBase):
    def test_every_call_logged(self):
        self.call("tasks", "--run", "run_x", runner=FakeRunner(wl_rows={"ready": []}))
        self.call("file", "/etc/passwd")
        self.call("bogus")
        recs = self.logged()
        self.assertEqual(len(recs), 3)
        self.assertEqual(recs[0]["argv"][0], "tasks")
        self.assertIn("rejected", recs[1]["note"])

    def test_no_log_env_no_file(self):
        os.environ.pop("PROGRESS_INSPECT_LOG")
        self.call("bogus")
        self.assertFalse(pathlib.Path(self.log).exists())


if __name__ == "__main__":
    unittest.main()
