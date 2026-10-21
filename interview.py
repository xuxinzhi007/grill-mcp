"""决策树状态机：把访谈长成一棵带纪律的决策树，按 frontier 逐节点收敛。

五格状态（吸收 grilling 的决策树 + wayfinder 的决策地图语义）：
- resolved：已拍板（decision 答了 / fact 查了）
- assumed：显式记录的假设（未证实）
- out_of_scope：明确不做
- pending：依赖已满足、可处理（frontier）
- fog：依赖未满足、还问不清（Not yet specified）

纪律（桶壁规则）：
- 节点挂成树（parent_id 隐含依赖），不是平铺列表；
- next_question 只返回依赖已满足的 open 节点（frontier）；
- fact / decision / assumption 三种 kind 严格分流，各走各的落点；
- decision 节点必须给 recommended（先表态再问）；
- finalize 完备性校验：frontier 未空 / fog 未空一律 done=false。
"""

import json
import time
import uuid
from pathlib import Path
from typing import Any

from factsheet import render_markdown, write_files
from session_store import SessionStore

RESOLVED = "resolved"
ASSUMED = "assumed"
PENDING = "pending"
FOG = "fog"
OUT_OF_SCOPE = "out_of_scope"

KINDS = ("decision", "fact", "assumption")
OUTPUTS = ("", "term", "decision")

_ACTION_BY_KIND = {"decision": "ask_user", "fact": "look_up", "assumption": "assume"}


def _action_for(node: dict) -> str:
    """一个 frontier 节点该由 AI 做的下一步动作。"""
    return _ACTION_BY_KIND[node["kind"]]


def _cue_for(node: dict) -> str:
    """附在输出上的行为指令：推着 AI 做聚焦动作，不废话、不被带偏。"""
    if node["kind"] == "decision":
        return "只把这一句转给用户，不加开场白、不总结、不追问别的"
    if node["kind"] == "fact":
        return "去代码/文档里查证，不要打扰用户，查完用 resolve_fact 记录结果和来源"
    return "显式记录这条假设，用 assume(value, reason)"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _effective_deps(n: dict) -> set[str]:
    """一个节点的有效依赖 = 显式 depends_on ∪ 树父节点 parent_id。"""
    deps = set(n.get("depends_on") or [])
    if n.get("parent_id"):
        deps.add(n["parent_id"])
    return deps


def _normalize_node(raw: Any, i: int) -> dict:
    """把调用方传入的节点规范成统一结构，非法输入直接报错。"""
    if not isinstance(raw, dict):
        raise ValueError(f"第 {i} 个节点必须是对象")
    if not raw.get("question"):
        raise ValueError(f"第 {i} 个节点缺少 question 字段")
    nid = str(raw.get("id") or "").strip()
    if not nid:
        raise ValueError(f"第 {i} 个节点缺少 id 字段")
    kind = str(raw.get("kind") or "decision").strip()
    if kind not in KINDS:
        raise ValueError(f"节点 {nid} 的 kind 非法: {kind}，可用 {KINDS}")
    recommended = str(raw.get("recommended") or "").strip()
    if kind == "decision" and not recommended:
        raise ValueError(f"decision 节点 {nid} 必须给 recommended（先表态再问）")
    output = str(raw.get("output") or "").strip()
    if output not in OUTPUTS:
        raise ValueError(f"节点 {nid} 的 output 非法: {output}，可用 {OUTPUTS}")
    options = raw.get("options") or []
    depends_on = raw.get("depends_on") or []
    if not isinstance(options, list):
        raise ValueError(f"节点 {nid} 的 options 必须是数组")
    if not isinstance(depends_on, list):
        raise ValueError(f"节点 {nid} 的 depends_on 必须是数组")
    return {
        "id": nid,
        "parent_id": str(raw.get("parent_id") or "").strip(),
        "kind": kind,
        "question": str(raw["question"]).strip(),
        "recommended": recommended,
        "why": str(raw.get("why") or "").strip(),
        "options": [str(o) for o in options],
        "depends_on": [str(d) for d in depends_on],
        "output": output,
    }


def _has_cycle(nodes: list[dict]) -> bool:
    """依赖图成环会让 frontier 永远无法收敛，加节点时直接拒绝。"""
    by_id = {n["id"]: n for n in nodes}
    state: dict[str, int] = {}

    def dfs(nid: str) -> bool:
        state[nid] = 1
        for dep in _effective_deps(by_id[nid]):
            if dep not in by_id:
                continue
            if state.get(dep) == 1:
                return True
            if state.get(dep, 0) == 0 and dfs(dep):
                return True
        state[nid] = 2
        return False

    for n in nodes:
        if state.get(n["id"], 0) == 0 and dfs(n["id"]):
            return True
    return False


def _derive_status(session: dict) -> dict[str, str]:
    """按 answers / facts / assumptions / out_of_scope 推导每个节点的五格状态。"""
    by_id = {n["id"]: n for n in session["nodes"]}
    status: dict[str, str] = {}

    def compute(nid: str) -> str:
        if nid in status:
            return status[nid]
        if nid in session["out_of_scope"]:
            status[nid] = OUT_OF_SCOPE
        elif nid in session["assumptions"]:
            status[nid] = ASSUMED
        elif nid in session["answers"] or nid in session["facts"]:
            status[nid] = RESOLVED
        else:
            deps = _effective_deps(by_id[nid])
            blocked = any(compute(d) in (PENDING, FOG) for d in deps)
            status[nid] = FOG if blocked else PENDING
        return status[nid]

    for nid in by_id:
        compute(nid)
    return status


class Interview:
    def __init__(self, store: SessionStore, modes_path: str, output_dir: str = "~/.qoder/grill-mcp/outputs"):
        self._store = store
        self._modes = self._load_modes(modes_path)
        self._output_dir = output_dir

    @staticmethod
    def _load_modes(path: str) -> dict:
        p = Path(path)
        if not p.exists():
            return {"free": {"label": "自由", "description": "全由 AI add_nodes 传入"}}
        return json.loads(p.read_text(encoding="utf-8"))

    def list_modes(self) -> list[dict]:
        return [
            {
                "id": mid,
                "label": m.get("label", mid),
                "description": m.get("description", ""),
                "when_to_use": m.get("when_to_use", ""),
                "flow": m.get("flow", ""),
                "discipline": m.get("discipline", ""),
                "skills": m.get("skills", {}),
            }
            for mid, m in self._modes.items()
        ]

    def create_session(self, requirement: str, mode: str = "requirement") -> dict:
        """开一个空树会话：问题全部由宿主 AI 用 add_nodes 现场长出。"""
        requirement = (requirement or "").strip()
        if not requirement:
            raise ValueError("requirement 不能为空")
        if mode not in self._modes:
            raise ValueError(f"未知 mode: {mode}，可用: {sorted(self._modes)}")
        session = {
            "session_id": uuid.uuid4().hex[:12],
            "requirement": requirement,
            "mode": mode,
            "created_at": _now(),
            "updated_at": _now(),
            "nodes": [],
            "answers": {},
            "facts": {},
            "assumptions": {},
            "out_of_scope": {},
        }
        self._store.save(session)
        return self._public(session)

    def add_nodes(self, session_id: str, nodes: list[Any]) -> dict:
        """宿主 AI 分析产出，往树上长节点。批次内与已有 id 都需唯一，依赖必须能解析。"""
        session = self._load(session_id)
        if not isinstance(nodes, list):
            raise ValueError("nodes 必须是数组")
        batch = [_normalize_node(n, i + 1) for i, n in enumerate(nodes)]
        seen = {n["id"] for n in session["nodes"]}
        for n in batch:
            if n["id"] in seen:
                raise ValueError(f"节点 id 重复: {n['id']}")
            seen.add(n["id"])
        merged = session["nodes"] + batch
        known = {n["id"] for n in merged}
        for n in batch:
            for d in _effective_deps(n):
                if d not in known:
                    raise ValueError(f"节点 {n['id']} 的依赖引用了不存在的 {d}")
        if _has_cycle(merged):
            raise ValueError("节点依赖构成环，frontier 永远无法收敛")
        session["nodes"] = merged
        session["updated_at"] = _now()
        self._store.save(session)
        return self._public(session)

    def next_question(self, session_id: str) -> dict:
        """算 frontier：返回依赖已满足的第一个 open 节点，附 action + cue + brief。"""
        session = self._load(session_id)
        status = _derive_status(session)
        pending = [n for n in session["nodes"] if status[n["id"]] == PENDING]
        fog = [n for n in session["nodes"] if status[n["id"]] == FOG]
        brief = self._brief(session, status)
        if pending:
            nxt = pending[0]
            return {
                "done": False,
                "pending_count": len(pending),
                "fog_count": len(fog),
                "resolved_count": len(session["answers"]) + len(session["facts"]),
                "node": nxt,
                "action": _action_for(nxt),
                "cue": _cue_for(nxt),
                "brief": brief,
            }
        return {
            "done": not fog,
            "pending_count": 0,
            "fog_count": len(fog),
            "resolved_count": len(session["answers"]) + len(session["facts"]),
            "node": None,
            "action": None,
            "cue": None,
            "brief": brief,
        }

    def answer(self, session_id: str, node_id: str, choice: str, note: str = "") -> dict:
        """用户拍板一个 decision 节点。fact / assumption 节点拒绝 answer。"""
        session = self._load(session_id)
        n = self._node_by_id(session, node_id)
        if n["kind"] == "fact":
            raise ValueError(f"{node_id} 是 fact 节点，AI 应自己查证（resolve_fact），不能丢给用户")
        if n["kind"] == "assumption":
            raise ValueError(f"{node_id} 是 assumption 节点，AI 应显式记录假设（assume），不能问用户")
        choice = (choice or "").strip()
        if not choice:
            raise ValueError("choice 不能为空")
        self._clear_other_buckets(session, node_id)
        session["answers"][node_id] = {
            "choice": choice,
            "note": (note or "").strip(),
            "answered_at": _now(),
        }
        session["updated_at"] = _now()
        self._store.save(session)
        return self._public(session)

    def resolve_fact(self, session_id: str, node_id: str, found: str, source: str = "") -> dict:
        """AI 记录一个 fact 节点的查证结果 + 来源。只有 fact 节点能用。"""
        session = self._load(session_id)
        n = self._node_by_id(session, node_id)
        if n["kind"] != "fact":
            raise ValueError(f"{node_id} 不是 fact 节点（kind={n['kind']}），只有 fact 才能 resolve_fact")
        found = (found or "").strip()
        if not found:
            raise ValueError("found 不能为空")
        self._clear_other_buckets(session, node_id)
        session["facts"][node_id] = {
            "found": found,
            "source": (source or "").strip(),
            "resolved_at": _now(),
        }
        session["updated_at"] = _now()
        self._store.save(session)
        return self._public(session)

    def assume(self, session_id: str, node_id: str, value: str, reason: str = "") -> dict:
        """AI 显式记录一条假设。只有 assumption 节点能用。"""
        session = self._load(session_id)
        n = self._node_by_id(session, node_id)
        if n["kind"] != "assumption":
            raise ValueError(f"{node_id} 不是 assumption 节点（kind={n['kind']}），只有 assumption 才能 assume")
        value = (value or "").strip()
        if not value:
            raise ValueError("value 不能为空")
        self._clear_other_buckets(session, node_id)
        session["assumptions"][node_id] = {
            "value": value,
            "reason": (reason or "").strip(),
            "assumed_at": _now(),
        }
        session["updated_at"] = _now()
        self._store.save(session)
        return self._public(session)

    def mark_out_of_scope(self, session_id: str, node_id: str, reason: str = "") -> dict:
        session = self._load(session_id)
        self._node_by_id(session, node_id)
        self._clear_other_buckets(session, node_id)
        session["out_of_scope"][node_id] = {
            "reason": (reason or "").strip(),
            "marked_at": _now(),
        }
        session["updated_at"] = _now()
        self._store.save(session)
        return self._public(session)

    def get_status(self, session_id: str) -> dict:
        return self._public(self._load(session_id))

    def finalize(self, session_id: str, write: bool = False, output_dir: str = "") -> dict:
        """完备性校验 + 收敛成事实清单；write=True 时落盘。"""
        session = self._load(session_id)
        public = self._public(session)
        result = {
            "session_id": session["session_id"],
            "done": public["done"],
            "markdown": render_markdown(session["requirement"], public),
            "facts": public["resolved"],
            "assumptions": public["assumptions"],
            "out_of_scope": public["out_of_scope"],
            "open": public["pending"] + public["fog"],
            "next_skills": self._next_skills(session.get("mode", "free")),
            "brief": public["brief"],
        }
        if write:
            out = output_dir or self._output_dir
            result["written"] = write_files(session["requirement"], session, public, out)
        return result

    def list_sessions(self) -> list[dict]:
        return [self._public(s) for s in self._store.list()]

    def _next_skills(self, mode: str) -> list[str]:
        """finalize 后宿主 AI 应接着调用的 skill（来自模式的 skills.after）。"""
        skills = self._modes.get(mode, {}).get("skills", {})
        after = skills.get("after") or []
        return [str(s) for s in after] if isinstance(after, list) else [str(after)]

    def _load(self, session_id: str) -> dict:
        session = self._store.load(session_id)
        if session is None:
            raise ValueError(f"会话不存在: {session_id}")
        return session

    @staticmethod
    def _node_by_id(session: dict, node_id: str) -> dict:
        for n in session["nodes"]:
            if n["id"] == node_id:
                return n
        raise ValueError(f"节点不存在: {node_id}")

    @staticmethod
    def _clear_other_buckets(session: dict, node_id: str) -> None:
        for key in ("answers", "facts", "assumptions", "out_of_scope"):
            session[key].pop(node_id, None)

    @staticmethod
    def _settled_lines(session: dict) -> list[str]:
        """已拍板摘要：id: 值；假设前缀 ~ 标出「未证实」。"""
        lines = [f"{nid}: {a['choice']}" for nid, a in session["answers"].items()]
        lines += [f"{nid}: {f['found']}" for nid, f in session["facts"].items()]
        lines += [f"{nid}: ~{a['value']}" for nid, a in session["assumptions"].items()]
        return lines

    def _discipline(self, mode: str) -> str:
        d = self._modes.get(mode, {}).get("discipline", "")
        tail = "一次只做一件事，不总结、不追问"
        return f"{d}；{tail}" if d else tail

    def _brief(self, session: dict, status: dict[str, str]) -> dict:
        """现场简报：让 AI 每次调用都能锚定「需求 + 已拍板 + 下一步 + 规则」。"""
        pending = [n for n in session["nodes"] if status[n["id"]] == PENDING]
        fog = [n for n in session["nodes"] if status[n["id"]] == FOG]
        if pending:
            nxt = pending[0]
            next_ = {
                "action": _action_for(nxt),
                "node_id": nxt["id"],
                "question": nxt["question"],
                "recommended": nxt.get("recommended", ""),
                "cue": _cue_for(nxt),
            }
        elif fog:
            first = fog[0]
            next_ = {
                "action": "blocked",
                "node_id": first["id"],
                "question": first["question"],
                "recommended": "",
                "cue": "依赖未满足：先处理它的依赖节点，或 add_nodes 补中间节点",
            }
        else:
            next_ = None
        return {
            "requirement": session["requirement"],
            "mode": session["mode"],
            "settled": self._settled_lines(session),
            "next": next_,
            "rule": self._discipline(session["mode"]),
        }

    def _public(self, session: dict) -> dict:
        status = _derive_status(session)
        buckets = {RESOLVED: [], ASSUMED: [], PENDING: [], FOG: [], OUT_OF_SCOPE: []}
        for n in session["nodes"]:
            st = status[n["id"]]
            nid = n["id"]
            if st == RESOLVED:
                if nid in session["answers"]:
                    a = session["answers"][nid]
                    buckets[RESOLVED].append(
                        {"id": nid, "question": n["question"], "kind": n["kind"],
                         "choice": a["choice"], "note": a["note"]}
                    )
                else:
                    f = session["facts"][nid]
                    buckets[RESOLVED].append(
                        {"id": nid, "question": n["question"], "kind": n["kind"],
                         "choice": f["found"], "source": f["source"]}
                    )
            elif st == ASSUMED:
                a = session["assumptions"][nid]
                buckets[ASSUMED].append(
                    {"id": nid, "question": n["question"], "kind": n["kind"],
                     "value": a["value"], "reason": a["reason"], "confirmed": False}
                )
            elif st == OUT_OF_SCOPE:
                buckets[OUT_OF_SCOPE].append(
                    {"id": nid, "question": n["question"], "kind": n["kind"],
                     "reason": session["out_of_scope"][nid]["reason"]}
                )
            else:
                buckets[st].append(n)
        return {
            "session_id": session["session_id"],
            "requirement": session["requirement"],
            "mode": session["mode"],
            "created_at": session["created_at"],
            "updated_at": session["updated_at"],
            "total": len(session["nodes"]),
            "done": not buckets[PENDING] and not buckets[FOG],
            "resolved": buckets[RESOLVED],
            "assumptions": buckets[ASSUMED],
            "pending": buckets[PENDING],
            "fog": buckets[FOG],
            "out_of_scope": buckets[OUT_OF_SCOPE],
            "brief": self._brief(session, status),
        }
