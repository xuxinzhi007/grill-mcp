"""访谈状态机与场景模板的最小回归测试。"""

import json
import tempfile
import unittest
from pathlib import Path

from interview import FOG, Interview, OUT_OF_SCOPE, PENDING, RESOLVED
from session_store import SessionStore
from factsheet import slugify

_REAL_SCENARIOS = str(Path(__file__).parent.parent / "templates" / "scenarios.json")

_TMP = Path(tempfile.mkdtemp())
_SCENARIOS = Path(_TMP) / "scenarios.json"
_SCENARIOS.write_text(
    json.dumps(
        {
            "free": {"label": "自由", "questions": []},
            "medium-feature": {
                "label": "中等功能",
                "questions": [{"id": "a", "question": "本质？"}, {"id": "b", "question": "复用？", "depends_on": ["a"]}],
            },
        },
        ensure_ascii=False,
    ),
    encoding="utf-8",
)


def _make_interview() -> Interview:
    return Interview(SessionStore(str(Path(_TMP) / "test.db")), str(_SCENARIOS))


class TestScenarioTemplate(unittest.TestCase):
    def test_create_from_template(self):
        iv = _make_interview()
        s = iv.create_session("做一个筛选", "medium-feature")
        self.assertEqual(s["total"], 2)
        self.assertEqual(len(s["pending"]), 1)  # b 依赖 a，a 先问
        self.assertEqual(len(s["fog"]), 1)
        self.assertFalse(s["done"])

    def test_unknown_scenario(self):
        iv = _make_interview()
        with self.assertRaises(ValueError):
            iv.create_session("x", "nope")


class TestDependencyOrder(unittest.TestCase):
    def test_fog_graduates_to_pending(self):
        iv = _make_interview()
        s = iv.create_session("x", "medium-feature")
        nxt = iv.next_question(s["session_id"])
        self.assertEqual(nxt["question"]["id"], "a")
        iv.answer(s["session_id"], "a", "筛选")
        s = iv.get_status(s["session_id"])
        self.assertEqual(len(s["fog"]), 0)
        self.assertEqual(len(s["pending"]), 1)
        self.assertEqual(s["pending"][0]["id"], "b")

    def test_done_when_all_resolved(self):
        iv = _make_interview()
        s = iv.create_session("x", "medium-feature")
        iv.answer(s["session_id"], "a", "筛选")
        iv.answer(s["session_id"], "b", "复用现有")
        s = iv.get_status(s["session_id"])
        self.assertTrue(s["done"])
        self.assertEqual(len(s["resolved"]), 2)


class TestCustomDecisionPoints(unittest.TestCase):
    def test_custom_points_override_template(self):
        iv = _make_interview()
        dps = [
            {"id": "why", "question": "为什么做？", "options": ["A", "B"], "recommended": "A", "why": "成本低"},
            {"id": "how", "question": "怎么做？", "depends_on": ["why"]},
        ]
        s = iv.create_session("改造 X", "medium-feature", dps)
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["pending"][0]["recommended"], "A")

    def test_bad_dep_reference_rejected(self):
        iv = _make_interview()
        dps = [{"id": "a", "question": "q", "depends_on": ["ghost"]}]
        with self.assertRaises(ValueError):
            iv.create_session("x", "free", dps)


class TestOutOfScope(unittest.TestCase):
    def test_mark_and_release(self):
        iv = _make_interview()
        s = iv.create_session("x", "free", [{"id": "a", "question": "q"}])
        iv.mark_out_of_scope(s["session_id"], "a", "不做了")
        s = iv.get_status(s["session_id"])
        self.assertEqual(len(s["out_of_scope"]), 1)
        self.assertTrue(s["done"])

    def test_answer_releases_out_of_scope(self):
        iv = _make_interview()
        s = iv.create_session("x", "free", [{"id": "a", "question": "q"}])
        iv.mark_out_of_scope(s["session_id"], "a")
        iv.answer(s["session_id"], "a", "选这个")
        s = iv.get_status(s["session_id"])
        self.assertEqual(len(s["resolved"]), 1)
        self.assertEqual(len(s["out_of_scope"]), 0)


class TestFinalize(unittest.TestCase):
    def test_markdown_follows_factsheet_format(self):
        iv = _make_interview()
        s = iv.create_session("改造 X", "free", [
            {"id": "essence", "question": "本质？"},
            {"id": "additions", "question": "新增？"},
        ])
        iv.answer(s["session_id"], "essence", "一句话收窄")
        iv.answer(s["session_id"], "additions", "一个字段")
        out = iv.finalize(s["session_id"])
        self.assertTrue(out["done"])
        self.assertIn("> 核心结论：一句话收窄", out["markdown"])
        self.assertIn("## 本质", out["markdown"])
        self.assertIn("## 需要新增", out["markdown"])
        self.assertEqual(len(out["facts"]), 2)
        self.assertEqual(len(out["open"]), 0)

    def test_finalize_lists_open_when_not_done(self):
        iv = _make_interview()
        s = iv.create_session("x", "free", [{"id": "a", "question": "q"}])
        out = iv.finalize(s["session_id"])
        self.assertFalse(out["done"])
        self.assertEqual(len(out["open"]), 1)
        self.assertIn("## 待拍板 / 还问不清", out["markdown"])

    def test_write_files(self):
        iv = _make_interview()
        s = iv.create_session("加个字段", "free", [
            {"id": "terms", "question": "术语", "kind": "term"},
            {"id": "decision", "question": "方案", "kind": "decision"},
        ])
        iv.answer(s["session_id"], "terms", "端侧解析")
        iv.answer(s["session_id"], "decision", "方案B")
        out_dir = str(Path(_TMP) / "out")
        out = iv.finalize(s["session_id"], write=True, output_dir=out_dir)
        written = set(out["written"])
        self.assertEqual(len(written), 3)
        self.assertTrue((Path(out_dir) / "加个字段-方案.md").exists())
        ctx = (Path(out_dir) / "CONTEXT.md").read_text(encoding="utf-8")
        self.assertIn("端侧解析", ctx)
        adr_dir = Path(out_dir) / "docs" / "adr"
        adrs = list(adr_dir.glob("*.md"))
        self.assertEqual(len(adrs), 1)
        self.assertIn("方案B", adrs[0].read_text(encoding="utf-8"))

    def test_adr_numbering_increments(self):
        iv = _make_interview()
        out_dir = str(Path(_TMP) / "adr-out")
        for i in range(2):
            s = iv.create_session(f"决策 {i}", "free", [{"id": "d", "question": "方案", "kind": "decision"}])
            iv.answer(s["session_id"], "d", f"方案{i}")
            iv.finalize(s["session_id"], write=True, output_dir=out_dir)
        adrs = sorted(p.name for p in (Path(out_dir) / "docs" / "adr").glob("*.md"))
        self.assertEqual(adrs, ["0001-决策-0.md", "0002-决策-1.md"])

    def test_architecture_template_writes_ctx_and_adr(self):
        iv = Interview(SessionStore(str(Path(_TMP) / "arch.db")), _REAL_SCENARIOS)
        s = iv.create_session("架构体检", "architecture")
        iv.answer(s["session_id"], "terms", "端侧解析")
        iv.answer(s["session_id"], "decision", "方案B")
        out_dir = str(Path(_TMP) / "arch-out")
        out = iv.finalize(s["session_id"], write=True, output_dir=out_dir)
        self.assertIn("端侧解析", (Path(out_dir) / "CONTEXT.md").read_text(encoding="utf-8"))
        adrs = list((Path(out_dir) / "docs" / "adr").glob("*.md"))
        self.assertEqual(len(adrs), 1)
        self.assertIn("方案B", adrs[0].read_text(encoding="utf-8"))


class TestSlugify(unittest.TestCase):
    def test_keeps_cjk_and_strips_punctuation(self):
        self.assertEqual(slugify("给任务加一个筛选！"), "给任务加一个筛选")
        self.assertEqual(slugify("!!!   "), "fact")
        self.assertEqual(slugify("A/B: C"), "a-b-c")


if __name__ == "__main__":
    unittest.main()
