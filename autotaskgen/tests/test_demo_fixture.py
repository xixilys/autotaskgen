import os
import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import demo_fixture  # noqa: E402
import progress_core as core  # noqa: E402


class DemoFixtureTest(unittest.TestCase):
    """The demo store must exercise every board case the lanes/feed redesign draws."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls._old = os.environ.get("PROGRESS_HOME")
        demo_fixture.main(["--home", cls.tmp.name, "--no-paper"])
        cls.live = core.load_live("demo")
        cls.state = core.fold(core.load_project("demo"), core.read_events("demo"))

    @classmethod
    def tearDownClass(cls):
        if cls._old is None:
            os.environ.pop("PROGRESS_HOME", None)
        else:
            os.environ["PROGRESS_HOME"] = cls._old
        cls.tmp.cleanup()

    def test_line_with_three_workers(self):
        per = {}
        for w in self.live["workers"]:
            if w.get("line"):
                per[w["line"]] = per.get(w["line"], 0) + 1
        self.assertGreaterEqual(max(per.values()), 3)

    def test_router_tree_three_levels_below_line(self):
        tree = {n["handle"]: n for n in self.live["router_tree"]}

        def depth(n):
            d = 0
            while n and n["kind"] == "sub":
                d += 1
                n = tree.get(n["parent"])
            return d
        self.assertGreaterEqual(max(depth(n) for n in tree.values()), 3)

    def test_coordinator_lingering_unattributed_workers(self):
        ws = self.live["workers"]
        coords = {c["coordinator"] for c in core.load_project("demo")["cores"]}
        self.assertGreaterEqual(sum(1 for w in ws if w.get("router") in coords), 2)
        self.assertTrue(any(w.get("lingering") for w in ws))
        self.assertTrue(any(not w.get("line") and not w.get("router") for w in ws))

    def test_unknown_run_and_empty_lane(self):
        acc = self.state["lines"]["ACC"]["nodes"].values()
        self.assertGreaterEqual(sum(1 for n in acc if n.get("status_unknown")), 20)
        self.assertEqual(self.state["lines"]["DB"]["nodes"], {})

    def test_import_burst_in_feed(self):
        burst = [e for e in self.state["feed"] if e["actor"]["role"] == "import"]
        self.assertGreaterEqual(len(burst), 15)


if __name__ == "__main__":
    unittest.main()
