"""访谈状态机：把一组待拍板点组织成四格状态，按依赖顺序逐题推进。

四格状态（吸收 wayfinder 的决策地图语义）：
- resolved：已拍板（Decisions so far）
- pending：依赖已满足、可问（frontier）
- fog：依赖未满足、还问不清（Not yet specified）
- out_of_scope：明确不做（Out of scope）
"""

import json
import time
import uuid
from pathlib import Path
from typing import Any

from factsheet import render_markdown, write_files
from session_store import SessionStore

RESOLVED = "resolved"
PENDING = "pending"
FOG = "fog"
OUT_OF_SCOPE = "out_of_scope"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _normalize_questions(raw: list[Any]) -> list[dict]:
    """把调用方传入的决策点规范成统一结构，非法输入直接报错。"""
    questions: list[dict] = []
    seen: set[str] = set()
    for i, q in enumerate(raw):
        if not isinstance(q, dict):
            raise ValueError(f"第 {i + 1} 个决策点必须是对象")
        if not q.get("question"):
            raise ValueError(f"第 {i + 1} 个决策点缺少 question 字段")
        qid = str(q.get("id") or f"q{i + 1}")
        if qid in seen:
            raise ValueError(f"决策点 id 重复: {qid}")
        seen.add(qid)
        options = q.get("options") or []
        depends_on = q.get("depends_on") or []
        if not isinstance(options, list):
            raise ValueError(f"决策点 {qid} 的 options 必须是数组")
        if not isinstance(depends_on, list):
            raise ValueError(f"决策点 {qid} 的 depends_on 必须是数组")
        questions.append(
            {
                "id": qid,
                "question": str(q["question"]).strip(),
                "options": [str(o) for o in options],
                "recommended": str(q.get("recommended") or "").strip(),
                "why": str(q.get("why") or "").strip(),
                "depends_on": [str(d) for d in depends_on],
                "kind": str(q.get("kind") or "").strip(),
            }
        )
    ids = {q["id"] for q in questions}
    for q in questions:
        for d in q["depends_on"]:
            if d not in ids:
                raise ValueError(f"决策点 {q['id']} 的 depends_on 引用了不存在的 {d}")
    return questions


def _derive_status(session: dict) -> dict[str, str]:
    """按 answers 与 out_of_scope 推导每个决策点的四格状态。"""
    answers = session["answers"]
    oos = session["out_of_scope"]
    status: dict[str, str] = {}
    for q in session["questions"]:
        qid = q["id"]
        if qid in oos:
            status[qid] = OUT_OF_SCOPE
        elif qid in answers:
            status[qid] = RESOLVED
        elif all(d in answers or d in oos for d in q["depends_on"]):
            status[qid] = PENDING
        else:
            status[qid] = FOG
    return status


def _question_view(q: dict) -> dict:
    return {
        "id": q["id"],
        "question": q["question"],
        "options": q["options"],
        "recommended": q.get("recommended", ""),
        "why": q.get("why", ""),
        "depends_on": q.get("depends_on", []),
    }


class Interview:
    def __init__(self, store: SessionStore, scenarios_path: str, output_dir: str = "~/.qoder/grill-mcp/outputs"):
        self._store = store
        self._scenarios = self._load_scenarios(scenarios_path)
        self._output_dir = output_dir

    @staticmethod
    def _load_scenarios(path: str) -> dict:
        p = Path(path)
        if not p.exists():
            return {"free": {"label": "自由", "description": "不预置问题，全部由调用方传入", "questions": []}}
        return json.loads(p.read_text(encoding="utf-8"))

    def list_scenarios(self) -> list[dict]:
        return [
            {"id": sid, "label": s.get("label", sid), "description": s.get("description", "")}
            for sid, s in self._scenarios.items()
        ]

    def create_session(
        self,
        requirement: str,
        scenario: str = "free",
        decision_points: list[Any] | None = None,
    ) -> dict:
        requirement = (requirement or "").strip()
        if not requirement:
            raise ValueError("requirement 不能为空")
        if scenario not in self._scenarios:
            raise ValueError(f"未知 scenario: {scenario}，可用: {sorted(self._scenarios)}")
        if decision_points:
            questions = _normalize_questions(decision_points)
        else:
            questions = _normalize_questions(self._scenarios[scenario].get("questions", []))
        session = {
            "session_id": uuid.uuid4().hex[:12],
            "requirement": requirement,
            "scenario": scenario,
            "created_at": _now(),
            "updated_at": _now(),
            "questions": questions,
            "answers": {},
            "out_of_scope": {},
        }
        self._store.save(session)
        return self._public(session)

    def next_question(self, session_id: str) -> dict:
        session = self._load(session_id)
        status = _derive_status(session)
        pending = [q for q in session["questions"] if status[q["id"]] == PENDING]
        if not pending:
            return {
                "done": True,
                "remaining": 0,
                "resolved_count": len(session["answers"]),
            }
        return {
            "done": False,
            "remaining": len(pending),
            "resolved_count": len(session["answers"]),
            "question": _question_view(pending[0]),
        }

    def answer(self, session_id: str, question_id: str, choice: str, note: str = "") -> dict:
        session = self._load(session_id)
        self._require_question(session, question_id)
        choice = (choice or "").strip()
        if not choice:
            raise ValueError("choice 不能为空")
        session["answers"][question_id] = {
            "choice": choice,
            "note": (note or "").strip(),
            "answered_at": _now(),
        }
        session["out_of_scope"].pop(question_id, None)
        session["updated_at"] = _now()
        self._store.save(session)
        return self._public(session)

    def mark_out_of_scope(self, session_id: str, question_id: str, reason: str = "") -> dict:
        session = self._load(session_id)
        self._require_question(session, question_id)
        session["out_of_scope"][question_id] = {
            "reason": (reason or "").strip(),
            "marked_at": _now(),
        }
        session["answers"].pop(question_id, None)
        session["updated_at"] = _now()
        self._store.save(session)
        return self._public(session)

    def get_status(self, session_id: str) -> dict:
        return self._public(self._load(session_id))

    def finalize(self, session_id: str, write: bool = False, output_dir: str = "") -> dict:
        """把访谈收敛成事实清单（方案写法格式）；write=True 时落盘。"""
        session = self._load(session_id)
        public = self._public(session)
        result = {
            "session_id": session["session_id"],
            "done": public["done"],
            "markdown": render_markdown(session["requirement"], public),
            "facts": public["resolved"],
            "out_of_scope": public["out_of_scope"],
            "open": public["pending"] + public["fog"],
        }
        if write:
            out = output_dir or self._output_dir
            result["written"] = write_files(session["requirement"], session, public, out)
        return result

    def list_sessions(self) -> list[dict]:
        return [self._public(s) for s in self._store.list()]

    def _load(self, session_id: str) -> dict:
        session = self._store.load(session_id)
        if session is None:
            raise ValueError(f"会话不存在: {session_id}")
        return session

    @staticmethod
    def _require_question(session: dict, question_id: str) -> None:
        if question_id not in {q["id"] for q in session["questions"]}:
            raise ValueError(f"决策点不存在: {question_id}")

    def _public(self, session: dict) -> dict:
        status = _derive_status(session)
        buckets = {RESOLVED: [], PENDING: [], FOG: [], OUT_OF_SCOPE: []}
        for q in session["questions"]:
            st = status[q["id"]]
            if st == RESOLVED:
                a = session["answers"][q["id"]]
                buckets[RESOLVED].append(
                    {"id": q["id"], "question": q["question"], "choice": a["choice"], "note": a["note"]}
                )
            elif st == OUT_OF_SCOPE:
                buckets[OUT_OF_SCOPE].append(
                    {"id": q["id"], "question": q["question"], "reason": session["out_of_scope"][q["id"]]["reason"]}
                )
            else:
                buckets[st].append(_question_view(q))
        return {
            "session_id": session["session_id"],
            "requirement": session["requirement"],
            "scenario": session["scenario"],
            "created_at": session["created_at"],
            "updated_at": session["updated_at"],
            "total": len(session["questions"]),
            "done": not buckets[PENDING] and not buckets[FOG],
            "resolved": buckets[RESOLVED],
            "pending": buckets[PENDING],
            "fog": buckets[FOG],
            "out_of_scope": buckets[OUT_OF_SCOPE],
        }
