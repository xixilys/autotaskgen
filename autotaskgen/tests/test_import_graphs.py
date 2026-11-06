import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import import_graphs as ig  # noqa: E402


class Classify(unittest.TestCase):
    def test_free_text_states(self):
        cases = {
            "": ("planned", None),
            "ready": ("planned", None),
            "dispatched; worker-start ready/input_accepted": ("running", None),
            "delivered_review_PASS_WITH_NOTES": ("reviewed", "PASS"),
            "delivered_pending_independent_review": ("delivered", "UNKNOWN"),
            "reviewed_partial_accepted_UNCLOSED": ("reviewed", "PARTIAL"),
            "failed_partial_repair_created": ("failed", "FAIL"),
            "superseded_candidate": ("superseded", None),
            "cancelled optional premature wait": ("cancelled", None),
            "succeeded": ("delivered", "UNKNOWN"),
        }
        for text, want in cases.items():
            self.assertEqual(ig.classify(text), want, text)

    def test_shapes_and_order(self):
        line = {"id": "L"}
        g = {"nodes": [{"key": "b", "status": "completed", "deps": ["t_a"]},
                       {"key": "a", "task_id": "t_a", "status": "completed_review_pass"}]}
        evs = ig.events_for_line(line, g, "2026-11-04T00:00:00Z")
        plans = [e["node"] for e in evs if e["kind"] == "node_plan"]
        self.assertEqual(plans, ["a", "b"])
        self.assertTrue(all(e["source"] == "import" and not e["auto_dispatch"]
                            for e in evs if e["kind"] == "node_plan"))

    def test_empty_graph(self):
        self.assertEqual(ig.events_for_line({"id": "L"}, {}, "2026-11-04T00:00:00Z"), [])

    def test_superseded_without_successor_is_cancelled(self):
        g = {"nodes": {"x": {"status": "superseded_candidate"}}}
        kinds = [e["kind"] for e in ig.events_for_line({"id": "L"}, g, "2026-11-04T00:00:00Z")]
        self.assertIn("node_cancel", kinds)


if __name__ == "__main__":
    unittest.main()
