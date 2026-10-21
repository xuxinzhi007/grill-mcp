#!/usr/bin/env python3
"""grill-mcp：需求澄清访谈引擎。

宿主 AI 收到模糊需求后，用它把需求整理成不可含糊的事实：
create_session 开访谈 → next_question 取题 → answer 记录 → get_status 看四格状态。

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
_SCENARIOS_PATH = Path(__file__).parent / "templates" / "scenarios.json"


def _load_config() -> dict:
    cfg = {
        "transport": "stdio",
        "host": "127.0.0.1",
        "port": 8000,
        "token": "",
        "session_db": "~/.qoder/grill-mcp/sessions.db",
        "output_dir": "~/.qoder/grill-mcp/outputs",
        "scenarios_path": str(_SCENARIOS_PATH),
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
    interview = Interview(SessionStore(cfg["session_db"]), cfg["scenarios_path"], cfg["output_dir"])
    mcp = FastMCP("grill-mcp", host=cfg["host"], port=cfg["port"])

    @mcp.tool()
    def list_scenarios() -> dict:
        """列出可用的需求场景，每类场景绑定一组默认必问清单。"""
        return {"scenarios": interview.list_scenarios()}

    @mcp.tool()
    def create_session(requirement: str, scenario: str = "free", decision_points: list | None = None) -> dict:
        """开一个访谈会话。

        Args:
            requirement: 原始需求描述（模糊的也可以）。
            scenario: 场景 id，见 list_scenarios；默认 free（不预置问题）。
            decision_points: 可选。宿主 AI 分析后提炼的待拍板点，元素格式
                {id, question, options?, recommended?, why?, depends_on?}，
                传入则优先于场景模板。
        """
        return _call(interview.create_session, requirement, scenario, decision_points)

    @mcp.tool()
    def next_question(session_id: str) -> dict:
        """取下一个待确认的问题（按依赖顺序）。done=true 表示访谈闭环。"""
        return _call(interview.next_question, session_id)

    @mcp.tool()
    def answer(session_id: str, question_id: str, choice: str, note: str = "") -> dict:
        """记录一个回答。choice 为选项或自由文本；note 可选补充。返回最新状态。"""
        return _call(interview.answer, session_id, question_id, choice, note)

    @mcp.tool()
    def mark_out_of_scope(session_id: str, question_id: str, reason: str = "") -> dict:
        """把某个问题标记为「明确不做」。返回最新状态。"""
        return _call(interview.mark_out_of_scope, session_id, question_id, reason)

    @mcp.tool()
    def get_status(session_id: str) -> dict:
        """查看会话四格状态：resolved / pending / fog / out_of_scope。"""
        return _call(interview.get_status, session_id)

    @mcp.tool()
    def list_sessions() -> dict:
        """列出所有会话（运维排查用）。"""
        return {"sessions": interview.list_sessions()}

    @mcp.tool()
    def finalize(session_id: str, write: bool = False, output_dir: str = "") -> dict:
        """把会话收敛成事实清单（方案写法格式），返回 markdown + 结构化 facts。

        Args:
            session_id: 会话 id。
            write: 是否落盘。True 时写出方案文件、CONTEXT.md 术语、ADR。
            output_dir: 落盘目录，默认取 config.json 的 output_dir。语义上是目标项目根目录。
        """
        return _call(interview.finalize, session_id, write, output_dir)

    if cfg["transport"] == "streamable-http":
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
