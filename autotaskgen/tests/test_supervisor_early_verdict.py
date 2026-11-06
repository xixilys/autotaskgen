import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import json  # noqa: E402
import supervisor  # noqa: E402
from test_supervisor import v2  # noqa: E402


class EarlyVerdict(unittest.TestCase):
    def doc(self, fus):
        d = json.loads(v2())
        d["followups"] = fus
        return d

    def run_v(self, fus):
        clean, why = supervisor.validate_report(self.doc(fus), ["L", "Q"], ["fu_due"], [], ["fu_due", "fu_early"])
        self.assertIsNotNone(clean, why)
        return clean

    def test_early_done_with_evidence_kept_quietly(self):
        c = self.run_v([{"id": "fu_early", "verdict": "done", "evidence": "event 17:30 node_review PASS"}])
        self.assertEqual([f["id"] for f in c["followups"]], ["fu_early"])
        self.assertFalse(any("丢弃" in w for w in c.get("warnings") or []))

    def test_early_not_done_dropped_without_warning(self):
        c = self.run_v([{"id": "fu_early", "verdict": "not_done", "evidence": "x"},
                        {"id": "fu_early", "verdict": "done", "evidence": ""}])
        self.assertEqual(c["followups"], [])
        self.assertFalse(any("丢弃" in w for w in c.get("warnings") or []))

    def test_unknown_id_still_warns(self):
        c = self.run_v([{"id": "fu_nope", "verdict": "done", "evidence": "x"}])
        self.assertTrue(any("fu_nope" in w for w in c.get("warnings") or []))


class RewordedFollowup(unittest.TestCase):
    A = "请带回真实评估器首个回复、明确端点和实际调用授权；缺任一项，FE 无法启动真实两轮评估闭环。"
    B = "请带回真实评估器首个回复、明确端点和实际调用授权；缺任一项，FE 无法启动真实两轮闭环。"

    def led(self, **kw):
        e = {"id": "fu_old", "line": "FE", "to": "human", "text": self.A}
        e.update(kw)
        return {"fu_old": e}

    def test_near_copy_maps_to_the_open_entry(self):
        self.assertEqual(supervisor.same_open(self.led(), "FE", "human", self.B), "fu_old")

    def test_other_line_recipient_closed_or_different_text_do_not(self):
        self.assertIsNone(supervisor.same_open(self.led(), "SR", "human", self.B))
        self.assertIsNone(supervisor.same_open(self.led(), "FE", "router", self.B))
        self.assertIsNone(supervisor.same_open(self.led(closed_at="2026-11-06T00:00:00Z"), "FE", "human", self.B))
        self.assertIsNone(supervisor.same_open(self.led(), "FE", "human", "请提供外部 typed 结构对照及原始证据。"))


if __name__ == "__main__":
    unittest.main()


class ClosedByHandlerShown(unittest.TestCase):
    def test_tooling_close_listed_once(self):
        import datetime as dt
        now = dt.datetime(2026, 11, 6, 3, 0, tzinfo=dt.timezone.utc)
        e = {"id": "fu_x", "line": "L", "to": "tooling", "text": "查 Bad Request", "created_at": "2026-11-06T01:00:00Z",
             "closed_at": "2026-11-06T02:50:00Z", "closed_how": "done", "closed_evidence": "代理短时故障"}
        out = supervisor.render_followups({"fu_x": e}, now)
        self.assertTrue(any("已确认落实" in x and "代理短时故障" in x for x in out))
        e["shown"] = True
        self.assertEqual(supervisor.render_followups({"fu_x": e}, now), ["（暂无）"])
