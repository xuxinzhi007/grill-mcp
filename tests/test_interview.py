"""决策树状态机与纪律约束的回归测试。"""

import tempfile
import unittest
from pathlib import Path

from factsheet import slugify
from interview import Interview
from session_store import SessionStore

_REAL_MODES = str(Path(__file__).parent.parent / "templates" / "modes.json")

_TMP = Path(tempfile.mkdtemp())


def _make_interview(db_name: str = "test.db") -> Interview:
    return Interview(SessionStore(str(Path(_TMP) / db_name)), _REAL_MODES)


class TestCreateSession(unittest.TestCase):
    def test_empty_tree(self):
        iv = _make_interview()
        s = iv.create_session("做筛选")
        self.assertEqual(s["total"], 0)
        self.assertEqual(s["mode"], "requirement")
        self.assertEqual(len(s["pending"]), 0)

    def test_default_mode_is_requirement(self):
        iv = _make_interview()
        self.assertEqual(iv.create_session("加个筛选")["mode"], "requirement")

    def test_unknown_mode(self):
        iv = _make_interview()
        with self.assertRaises(ValueError):
            iv.create_session("x", "nope")


class TestAddNodes(unittest.TestCase):
    def test_decision_requires_recommended(self):
        iv = _make_interview()
        s = iv.create_session("x", "free")
        with self.assertRaises(ValueError):
            iv.add_nodes(s["session_id"], [{"id": "a", "question": "本质？"}])

    def test_duplicate_id_rejected(self):
        iv = _make_interview()
        s = iv.create_session("x", "free")
        iv.add_nodes(s["session_id"], [{"id": "a", "question": "q", "recommended": "r"}])
        with self.assertRaises(ValueError):
            iv.add_nodes(s["session_id"], [{"id": "a", "question": "q", "recommended": "r"}])

    def test_bad_dep_reference_rejected(self):
        iv = _make_interview()
        s = iv.create_session("x", "free")
        with self.assertRaises(ValueError):
            iv.add_nodes(s["session_id"], [{"id": "a", "question": "q", "depends_on": ["ghost"]}])

    def test_cycle_rejected(self):
        iv = _make_interview()
        s = iv.create_session("x", "free")
        with self.assertRaises(ValueError):
            iv.add_nodes(s["session_id"], [
                {"id": "a", "question": "q", "depends_on": ["b"]},
                {"id": "b", "question": "q", "depends_on": ["a"]},
            ])

    def test_same_batch_dep_allowed(self):
        iv = _make_interview()
        s = iv.create_session("x", "free")
        s = iv.add_nodes(s["session_id"], [
            {"id": "child", "question": "子", "parent_id": "root", "recommended": "r"},
            {"id": "root", "question": "父", "recommended": "r"},
        ])
        self.assertEqual(s["total"], 2)


class TestFrontier(unittest.TestCase):
    def test_parent_blocks_child(self):
        iv = _make_interview()
        s = iv.create_session("x", "free")
        iv.add_nodes(s["session_id"], [
            {"id": "essence", "question": "本质？", "recommended": "改标签"},
            {"id": "api", "question": "接口？", "parent_id": "essence", "recommended": "更新接口"},
        ])
        nxt = iv.next_question(s["session_id"])
        self.assertEqual(nxt["node"]["id"], "essence")
        s = iv.get_status(s["session_id"])
        self.assertEqual({n["id"] for n in s["fog"]}, {"api"})

    def test_fog_graduates_to_pending(self):
        iv = _make_interview()
        s = iv.create_session("x", "free")
        iv.add_nodes(s["session_id"], [
            {"id": "a", "question": "a?", "recommended": "r"},
            {"id": "b", "question": "b?", "depends_on": ["a"], "recommended": "r"},
        ])
        iv.answer(s["session_id"], "a", "选A")
        nxt = iv.next_question(s["session_id"])
        self.assertEqual(nxt["node"]["id"], "b")
        self.assertEqual(nxt["fog_count"], 0)

    def test_fact_gates_root_cause(self):
        iv = _make_interview()
        s = iv.create_session("线上 500", "bug")
        iv.add_nodes(s["session_id"], [
            {"id": "feedback_loop", "kind": "fact", "question": "复现回路？"},
            {"id": "evidence", "kind": "fact", "question": "证据？"},
            {"id": "root_cause", "question": "根因？", "depends_on": ["feedback_loop", "evidence"], "recommended": "缺索引"},
        ])
        s = iv.get_status(s["session_id"])
        self.assertIn("root_cause", {n["id"] for n in s["fog"]})
        iv.resolve_fact(s["session_id"], "feedback_loop", "curl 复现 500")
        s = iv.get_status(s["session_id"])
        self.assertIn("root_cause", {n["id"] for n in s["fog"]})
        iv.resolve_fact(s["session_id"], "evidence", "慢查询日志")
        self.assertIn("root_cause", {n["id"] for n in iv.get_status(s["session_id"])["pending"]})


class TestKindRouting(unittest.TestCase):
    def _session(self, iv):
        s = iv.create_session("x", "free")
        iv.add_nodes(s["session_id"], [
            {"id": "d", "question": "决策", "recommended": "r"},
            {"id": "f", "kind": "fact", "question": "事实"},
            {"id": "a", "kind": "assumption", "question": "假设"},
        ])
        return s

    def test_answer_rejects_fact(self):
        iv = _make_interview()
        s = self._session(iv)
        with self.assertRaises(ValueError):
            iv.answer(s["session_id"], "f", "查到了")

    def test_answer_rejects_assumption(self):
        iv = _make_interview()
        s = self._session(iv)
        with self.assertRaises(ValueError):
            iv.answer(s["session_id"], "a", "我假设")

    def test_resolve_fact_rejects_decision(self):
        iv = _make_interview()
        s = self._session(iv)
        with self.assertRaises(ValueError):
            iv.resolve_fact(s["session_id"], "d", "查到了")

    def test_assume_rejects_decision(self):
        iv = _make_interview()
        s = self._session(iv)
        with self.assertRaises(ValueError):
            iv.assume(s["session_id"], "d", "假设值")


class TestResolveFact(unittest.TestCase):
    def test_fact_records_found_and_source(self):
        iv = _make_interview()
        s = iv.create_session("x", "free")
        iv.add_nodes(s["session_id"], [{"id": "f", "kind": "fact", "question": "接口是哪个？"}])
        iv.resolve_fact(s["session_id"], "f", "POST /api/tag/v1/batch", "grep handler")
        s = iv.get_status(s["session_id"])
        self.assertEqual(len(s["resolved"]), 1)
        self.assertEqual(s["resolved"][0]["choice"], "POST /api/tag/v1/batch")
        self.assertEqual(s["resolved"][0]["source"], "grep handler")


class TestAssume(unittest.TestCase):
    def test_assume_listed_and_unconfirmed(self):
        iv = _make_interview()
        s = iv.create_session("x", "free")
        iv.add_nodes(s["session_id"], [{"id": "perf", "kind": "assumption", "question": "性能假设？"}])
        iv.assume(s["session_id"], "perf", "批量 ≤200 无性能问题", "经验值")
        s = iv.get_status(s["session_id"])
        self.assertEqual(len(s["assumptions"]), 1)
        self.assertFalse(s["assumptions"][0]["confirmed"])
        self.assertTrue(s["done"])

    def test_assume_unlocks_child(self):
        iv = _make_interview()
        s = iv.create_session("x", "free")
        iv.add_nodes(s["session_id"], [
            {"id": "assump", "kind": "assumption", "question": "前置假设"},
            {"id": "nxt", "question": "下一步", "depends_on": ["assump"], "recommended": "r"},
        ])
        iv.assume(s["session_id"], "assump", "假设成立")
        self.assertEqual(iv.next_question(s["session_id"])["node"]["id"], "nxt")


class TestOutOfScope(unittest.TestCase):
    def test_mark_and_release(self):
        iv = _make_interview()
        s = iv.create_session("x", "free")
        iv.add_nodes(s["session_id"], [{"id": "a", "question": "q", "recommended": "r"}])
        iv.mark_out_of_scope(s["session_id"], "a", "不做了")
        s = iv.get_status(s["session_id"])
        self.assertEqual(len(s["out_of_scope"]), 1)
        self.assertTrue(s["done"])

    def test_answer_releases_out_of_scope(self):
        iv = _make_interview()
        s = iv.create_session("x", "free")
        iv.add_nodes(s["session_id"], [{"id": "a", "question": "q", "recommended": "r"}])
        iv.mark_out_of_scope(s["session_id"], "a")
        iv.answer(s["session_id"], "a", "选这个")
        s = iv.get_status(s["session_id"])
        self.assertEqual(len(s["resolved"]), 1)
        self.assertEqual(len(s["out_of_scope"]), 0)


class TestFinalize(unittest.TestCase):
    def test_markdown_follows_factsheet_format(self):
        iv = _make_interview()
        s = iv.create_session("改造 X", "free")
        iv.add_nodes(s["session_id"], [
            {"id": "essence", "question": "本质？", "recommended": "一句话收窄"},
            {"id": "additions", "question": "新增？", "recommended": "一个字段"},
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

    def test_markdown_includes_assumptions(self):
        iv = _make_interview()
        s = iv.create_session("x", "free")
        iv.add_nodes(s["session_id"], [{"id": "p", "kind": "assumption", "question": "性能？"}])
        iv.assume(s["session_id"], "p", "批量无性能问题")
        out = iv.finalize(s["session_id"])
        self.assertIn("## 假设（未证实）", out["markdown"])
        self.assertIn("批量无性能问题", out["markdown"])

    def test_finalize_lists_open_when_not_done(self):
        iv = _make_interview()
        s = iv.create_session("x", "free")
        iv.add_nodes(s["session_id"], [{"id": "a", "question": "q", "recommended": "r"}])
        out = iv.finalize(s["session_id"])
        self.assertFalse(out["done"])
        self.assertEqual(len(out["open"]), 1)
        self.assertIn("## 待拍板 / 还问不清", out["markdown"])

    def test_write_files_term_and_decision(self):
        iv = _make_interview()
        s = iv.create_session("加个字段", "free")
        iv.add_nodes(s["session_id"], [
            {"id": "terms", "question": "术语", "recommended": "端侧解析", "output": "term"},
            {"id": "decision", "question": "方案", "recommended": "方案B", "output": "decision"},
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
        adrs = list((Path(out_dir) / "docs" / "adr").glob("*.md"))
        self.assertEqual(len(adrs), 1)
        self.assertIn("方案B", adrs[0].read_text(encoding="utf-8"))

    def test_adr_numbering_increments(self):
        iv = _make_interview()
        out_dir = str(Path(_TMP) / "adr-out")
        for i in range(2):
            s = iv.create_session(f"决策 {i}", "free")
            iv.add_nodes(s["session_id"], [{"id": "d", "question": "方案", "recommended": "r", "output": "decision"}])
            iv.answer(s["session_id"], "d", f"方案{i}")
            iv.finalize(s["session_id"], write=True, output_dir=out_dir)
        adrs = sorted(p.name for p in (Path(out_dir) / "docs" / "adr").glob("*.md"))
        self.assertEqual(adrs, ["0001-决策-0.md", "0002-决策-1.md"])


class TestModes(unittest.TestCase):
    def test_list_modes_exposes_skills_and_discipline(self):
        iv = _make_interview()
        modes = {m["id"]: m for m in iv.list_modes()}
        self.assertEqual(modes["requirement"]["skills"]["after"], ["to-spec", "to-tickets"])
        self.assertEqual(modes["bug"]["skills"]["analyze"], "diagnosing-bugs")
        self.assertTrue(modes["requirement"]["discipline"])

    def test_finalize_returns_next_skills(self):
        iv = _make_interview()
        s = iv.create_session("加个筛选", "requirement")
        out = iv.finalize(s["session_id"])
        self.assertEqual(out["next_skills"], ["to-spec", "to-tickets"])

    def test_bug_finalize_has_no_downstream_skill(self):
        iv = _make_interview()
        s = iv.create_session("线上报错", "bug")
        out = iv.finalize(s["session_id"])
        self.assertEqual(out["next_skills"], [])


class TestBrief(unittest.TestCase):
    def test_next_question_carries_action_and_cue(self):
        iv = _make_interview()
        s = iv.create_session("加批量改标签", "free")
        iv.add_nodes(s["session_id"], [
            {"id": "essence", "question": "本质？", "recommended": "改标签"},
            {"id": "api", "kind": "fact", "question": "接口？"},
            {"id": "perf", "kind": "assumption", "question": "性能？"},
        ])
        nxt = iv.next_question(s["session_id"])
        self.assertEqual(nxt["node"]["id"], "essence")
        self.assertEqual(nxt["action"], "ask_user")
        self.assertTrue(nxt["cue"])

    def test_brief_contains_requirement_settled_rule(self):
        iv = _make_interview()
        s = iv.create_session("加批量改标签", "requirement")
        iv.add_nodes(s["session_id"], [{"id": "essence", "question": "本质？", "recommended": "改标签"}])
        iv.answer(s["session_id"], "essence", "批量改标签")
        st = iv.get_status(s["session_id"])
        b = st["brief"]
        self.assertEqual(b["requirement"], "加批量改标签")
        self.assertIn("essence: 批量改标签", b["settled"])
        self.assertEqual(b["next"], None)  # 已闭环
        self.assertTrue(b["rule"])

    def test_fact_node_action_is_look_up(self):
        iv = _make_interview()
        s = iv.create_session("x", "free")
        iv.add_nodes(s["session_id"], [{"id": "f", "kind": "fact", "question": "接口？"}])
        nxt = iv.next_question(s["session_id"])
        self.assertEqual(nxt["action"], "look_up")
        self.assertIn("不要打扰用户", nxt["cue"])

    def test_answer_returns_next_frontier_directly(self):
        iv = _make_interview()
        s = iv.create_session("x", "free")
        iv.add_nodes(s["session_id"], [
            {"id": "a", "question": "a?", "recommended": "r"},
            {"id": "b", "question": "b?", "depends_on": ["a"], "recommended": "r"},
        ])
        out = iv.answer(s["session_id"], "a", "选A")
        self.assertEqual(out["brief"]["next"]["node_id"], "b")

    def test_assumption_marked_in_settled(self):
        iv = _make_interview()
        s = iv.create_session("x", "free")
        iv.add_nodes(s["session_id"], [{"id": "p", "kind": "assumption", "question": "性能？"}])
        iv.assume(s["session_id"], "p", "批量无性能问题")
        st = iv.get_status(s["session_id"])
        self.assertIn("p: ~批量无性能问题", st["brief"]["settled"])


class TestSlugify(unittest.TestCase):
    def test_keeps_cjk_and_strips_punctuation(self):
        self.assertEqual(slugify("给任务加一个筛选！"), "给任务加一个筛选")
        self.assertEqual(slugify("!!!   "), "fact")
        self.assertEqual(slugify("A/B: C"), "a-b-c")


if __name__ == "__main__":
    unittest.main()
