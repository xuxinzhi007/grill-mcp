"""事实清单：把访谈结果渲染成「方案写法」格式并落盘。

渲染（render_markdown）是纯函数，只依据已拍板事实，不掺杂推理；
落盘（write_files）把事实写进三类文件：
- 方案文件：<output_dir>/<slug>-方案.md（方案写法格式）
- 术语表：<output_dir>/CONTEXT.md（追加 output=term 的已拍板项）
- ADR：<output_dir>/docs/adr/<NNNN>-<slug>.md（output=decision 的已拍板项）
"""

import re
import time
from pathlib import Path

# 决策点 id → 章节标题，按输出顺序排列；未命中的进「已拍板事实」。
_SECTIONS: list[tuple[str, str]] = [
    # requirement（需求拆解）
    ("essence", "本质"),
    ("scope", "归属"),
    ("additions", "需要新增"),
    ("reuse", "不需要新增（复用现有）"),
    ("api_change", "接口改动"),
    ("flow", "后端流程"),
    ("impact", "改动范围与影响面"),
    # bug（证据驱动，禁止猜测）
    ("symptom", "症状（实际观察）"),
    ("reproduce", "复现步骤"),
    ("feedback_loop", "复现回路"),
    ("minimize", "最小场景"),
    ("evidence", "实际证据"),
    ("ruled_out", "已排除（已验证）"),
    ("root_cause", "根因"),
    ("fix", "修复"),
    ("regression", "回归测试"),
    # migration（决策地图）
    ("destination", "目的地"),
    ("fog", "还问不清（Not yet specified）"),
    ("first_steps", "第一步（frontier）"),
    ("decisions", "关键决策"),
    # architecture（术语与决策）
    ("terms", "术语"),
    ("options", "候选方案"),
    ("decision", "选定方案"),
    ("shallow_modules", "候选 shallow module"),
    ("boundaries", "边界问题"),
]

# 各模式「一句话核心结论」取哪个已拍板答案，按优先级。
_CONCLUSION_IDS = ("essence", "root_cause", "destination", "decision")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def slugify(text: str, max_len: int = 48) -> str:
    """把需求压成可用的文件名片段，保留中英文与数字。"""
    s = text.lower()
    s = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", s).strip("-")
    s = s[:max_len].rstrip("-")
    return s or "fact"


def render_markdown(requirement: str, public: dict) -> str:
    """按「方案写法」六点格式输出事实清单（含假设与未封口项）。"""
    resolved = {r["id"]: r for r in public.get("resolved", [])}
    lines = [f"# 需求：{requirement}", ""]

    conclusion = requirement
    for qid in _CONCLUSION_IDS:
        if qid in resolved:
            conclusion = resolved[qid].get("choice") or conclusion
            break
    lines.append(f"> 核心结论：{conclusion}")

    known = {qid for qid, _ in _SECTIONS}
    for qid, title in _SECTIONS:
        r = resolved.get(qid)
        if r is None:
            continue
        lines.append("")
        lines.append(f"## {title}")
        lines.append(r.get("choice") or "（未填）")
        if r.get("note"):
            lines.append(f"备注：{r['note']}")
        if r.get("source"):
            lines.append(f"来源：{r['source']}")

    unknown = [r for r in public.get("resolved", []) if r["id"] not in known]
    if unknown:
        lines.append("")
        lines.append("## 已拍板事实")
        for r in unknown:
            line = f"- {r['question']} → {r.get('choice') or ''}"
            if r.get("source"):
                line += f"（来源：{r['source']}）"
            if r.get("note"):
                line += f"（备注：{r['note']}）"
            lines.append(line)

    assumptions = public.get("assumptions", [])
    if assumptions:
        lines.append("")
        lines.append("## 假设（未证实）")
        for a in assumptions:
            line = f"- {a['question']} → {a.get('value') or ''}"
            if a.get("reason"):
                line += f"（理由：{a['reason']}）"
            lines.append(line)

    if public.get("out_of_scope"):
        lines.append("")
        lines.append("## 明确不做")
        for o in public["out_of_scope"]:
            line = f"- {o['question']}"
            if o.get("reason"):
                line += f"：{o['reason']}"
            lines.append(line)

    open_items = public.get("pending", []) + public.get("fog", [])
    if open_items:
        lines.append("")
        lines.append("## 待拍板 / 还问不清")
        for q in open_items:
            lines.append(f"- {q['question']}")

    return "\n".join(lines) + "\n"


def _resolved_value(session: dict, node_id: str) -> str:
    """取一个节点已拍板的值：decision→choice、fact→found、assumption→value。"""
    for key, value_key in (("answers", "choice"), ("facts", "found"), ("assumptions", "value")):
        rec = session.get(key, {}).get(node_id)
        if rec:
            return rec.get(value_key) or ""
    return ""


def _next_adr_number(adr_dir: Path) -> int:
    if not adr_dir.exists():
        return 1
    nums = [
        int(m.group(1))
        for f in adr_dir.glob("*.md")
        if (m := re.match(r"(\d+)", f.name))
    ]
    return (max(nums) if nums else 0) + 1


def write_files(requirement: str, session: dict, public: dict, output_dir: str) -> list[str]:
    """把事实清单写进三类文件，返回写出的路径列表。"""
    out = Path(output_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    slug = slugify(requirement)
    written: list[str] = []

    plan = out / f"{slug}-方案.md"
    plan.write_text(render_markdown(requirement, public), encoding="utf-8")
    written.append(str(plan))

    term_nodes = [
        n for n in session["nodes"]
        if n.get("output") == "term" and _resolved_value(session, n["id"])
    ]
    if term_nodes:
        ctx = out / "CONTEXT.md"
        term_lines = [f"- {n['question']}：{_resolved_value(session, n['id'])}" for n in term_nodes]
        block = [f"## 术语（grill-mcp 追加于 {_now()}）", *term_lines]
        with ctx.open("a", encoding="utf-8") as f:
            f.write("\n" + "\n".join(block) + "\n")
        written.append(str(ctx))

    decision_nodes = [
        n for n in session["nodes"]
        if n.get("output") == "decision" and _resolved_value(session, n["id"])
    ]
    if decision_nodes:
        adr_dir = out / "docs" / "adr"
        adr_dir.mkdir(parents=True, exist_ok=True)
        num = _next_adr_number(adr_dir)
        adr = adr_dir / f"{num:04d}-{slug}.md"
        body = [f"# {requirement}"]
        for n in decision_nodes:
            body.append("")
            body.append(f"## {n['question']}")
            body.append(_resolved_value(session, n["id"]))
            rec = session.get("answers", {}).get(n["id"])
            if rec and rec.get("note"):
                body.append("")
                body.append(rec["note"])
        adr.write_text("\n".join(body) + "\n", encoding="utf-8")
        written.append(str(adr))

    return written
