#!/usr/bin/env python3
"""grill-mcp：需求澄清访谈引擎（模式路由 + 纪律约束）。

宿主 AI 收到模糊输入后，先按模式路由（需求拆解 / Bug 分析 / 理解架构 / 大型迁移），
把输入长成一棵决策树，MCP 用确定性规则逼着 AI 把需求问透：
list_modes 看模式 → create_session 开空树 → add_nodes 长节点 → next_question 算 frontier →
answer / resolve_fact / assume 逐节点收敛 → finalize 完备性校验并落盘。

传输方式：stdio（默认）或 streamable-http（设 GRILL_MCP_TRANSPORT 环境变量
或写 config.json 的 transport 字段）。
"""

import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from interview import Interview
from session_store import SessionStore

_CONFIG_PATH = Path(__file__).parent / "config.json"
_MODES_PATH = Path(__file__).parent / "templates" / "modes.json"


def _load_config() -> dict:
    cfg = {
        "transport": "stdio",
        "host": "127.0.0.1",
        "port": 8000,
        "token": "",
        "session_db": "~/.qoder/grill-mcp/sessions.db",
        "output_dir": "~/.qoder/grill-mcp/outputs",
        "modes_path": str(_MODES_PATH),
    }
    if _CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(_CONFIG_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    cfg["transport"] = os.environ.get("GRILL_MCP_TRANSPORT", cfg["transport"])
    return cfg


def _call(fn, *args, **kwargs):
    """统一把业务异常转成 {"error": ...}，方便 LLM 读。"""
    try:
        return fn(*args, **kwargs)
    except (ValueError, KeyError) as e:
        return {"error": str(e)}


def main() -> None:
    cfg = _load_config()
    interview = Interview(SessionStore(cfg["session_db"]), cfg["modes_path"], cfg["output_dir"])
    mcp = FastMCP("grill-mcp", host=cfg["host"], port=cfg["port"])

    @mcp.tool()
    def list_modes() -> dict:
        """列出可用模式（需求拆解 / Bug 分析 / 理解架构 / 大型迁移 / 自由），含 when_to_use / skills / discipline 路由提示。"""
        return {"modes": interview.list_modes()}

    @mcp.tool()
    def create_session(requirement: str, mode: str = "requirement") -> dict:
        """开一个空树会话。问题不内置，全部由宿主 AI 用 add_nodes 现场长出。

        Args:
            requirement: 原始输入（需求 / bug 现象 / 架构问题，模糊的也可以）。
            mode: 模式 id，见 list_modes；默认 requirement（需求拆解）。
        """
        return _call(interview.create_session, requirement, mode)

    @mcp.tool()
    def add_nodes(session_id: str, nodes: list) -> dict:
        """宿主 AI 分析产出，往树上长节点。节点格式见下。

        Args:
            session_id: 会话 id。
            nodes: 节点数组。每个节点
                {id, question, kind?, parent_id?, depends_on?, recommended?, why?, options?, output?}
                - kind: decision（默认，用户拍板）/ fact（AI 查证）/ assumption（AI 记假设）。
                - decision 节点 recommended 必填（先表态再问）。
                - output: 落盘路由，term（进 CONTEXT.md）/ decision（写 ADR）/ 空（进方案正文）。
        """
        return _call(interview.add_nodes, session_id, nodes)

    @mcp.tool()
    def next_question(session_id: str) -> dict:
        """算 frontier：返回依赖已满足的第一个 open 节点。

        返回带 action（ask_user/look_up/assume）+ cue（行为指令）+ brief（现场简报：
        需求 + 已拍板摘要 + 下一步 + 规则），让 AI 不用自己记状态。done=true 表示无 open 节点。
        """
        return _call(interview.next_question, session_id)

    @mcp.tool()
    def answer(session_id: str, node_id: str, choice: str, note: str = "") -> dict:
        """用户拍板一个 decision 节点。choice 为选项或自由文本；note 可选补充。

        返回最新状态，brief.next 直接给出下一个 frontier 节点，无需再调 next_question。
        """
        return _call(interview.answer, session_id, node_id, choice, note)

    @mcp.tool()
    def resolve_fact(session_id: str, node_id: str, found: str, source: str = "") -> dict:
        """AI 记录一个 fact 节点的查证结果 + 来源。只有 kind=fact 节点能用。

        返回最新状态，brief.next 直接给出下一个 frontier 节点。
        """
        return _call(interview.resolve_fact, session_id, node_id, found, source)

    @mcp.tool()
    def assume(session_id: str, node_id: str, value: str, reason: str = "") -> dict:
        """AI 显式记录一条假设。只有 kind=assumption 节点能用。

        返回最新状态，brief.next 直接给出下一个 frontier 节点。
        """
        return _call(interview.assume, session_id, node_id, value, reason)

    @mcp.tool()
    def mark_out_of_scope(session_id: str, node_id: str, reason: str = "") -> dict:
        """把某个节点标记为「明确不做」。返回最新状态（含 brief）。"""
        return _call(interview.mark_out_of_scope, session_id, node_id, reason)

    @mcp.tool()
    def get_status(session_id: str) -> dict:
        """查看会话状态：resolved / assumptions / pending / fog / out_of_scope，外加 brief 现场简报。"""
        return _call(interview.get_status, session_id)

    @mcp.tool()
    def finalize(session_id: str, write: bool = False, output_dir: str = "") -> dict:
        """完备性校验 + 收敛成事实清单（方案写法格式），返回 markdown + facts + assumptions + open。

        Args:
            session_id: 会话 id。
            write: 是否落盘。True 时写出方案文件、CONTEXT.md 术语、ADR。
            output_dir: 落盘目录，默认取 config.json 的 output_dir。语义上是目标项目根目录。
        """
        return _call(interview.finalize, session_id, write, output_dir)

    @mcp.tool()
    def list_sessions() -> dict:
        """列出所有会话（运维排查用）。"""
        return {"sessions": interview.list_sessions()}

    if cfg["transport"] == "streamable-http":
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
