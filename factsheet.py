"""事实清单：把访谈结果渲染成「方案写法」格式并落盘。

渲染（render_markdown）是纯函数，只依据已拍板事实，不掺杂推理；
落盘（write_files）把事实写进三类文件：
- 方案文件：<output_dir>/<slug>-方案.md（方案写法格式）
- 术语表：<output_dir>/CONTEXT.md（追加 kind=term 的已拍板项）
- ADR：<output_dir>/docs/adr/<NNNN>-<slug>.md（kind=decision 的已拍板项）
"""

import re
import time
from pathlib import Path

# 决策点 id → 方案写法章节标题，按输出顺序排列；未命中的进「已拍板事实」。
_SECTIONS: list[tuple[str, str]] = [
    ("essence", "本质"),
    ("scope", "归属"),
    ("additions", "需要新增"),
    ("reuse", "不需要新增（复用现有）"),
    ("api_change", "接口改动"),
    ("flow", "后端流程"),
    ("first_steps", "第一步（frontier）"),
    ("impact", "改动范围与影响面"),
    ("destination", "目的地"),
    ("options", "候选方案"),
    ("decision", "选定方案"),
    ("terms", "术语"),
    ("decisions", "关键决策"),
    ("shallow_modules", "候选 shallow module"),
    ("boundaries", "边界问题"),
]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def slugify(text: str, max_len: int = 48) -> str:
    """把需求压成可用的文件名片段，保留中英文与数字。"""
    s = text.lower()
    s = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", s).strip("-")
    s = s[:max_len].rstrip("-")
    return s or "fact"


def _is_term(q: dict) -> bool:
    return q.get("kind") == "term" or q["id"] == "terms"


def _is_decision(q: dict) -> bool:
    return q.get("kind") == "decision" or q["id"] == "decision"


def render_markdown(requirement: str, public: dict) -> str:
    """按「方案写法」六点格式输出事实清单。"""
    resolved = {r["id"]: r for r in public["resolved"]}
    lines = [f"# 需求：{requirement}", ""]

    conclusion = requirement
    if "essence" in resolved:
        conclusion = resolved["essence"]["choice"]
    lines.append(f"> 核心结论：{conclusion}")

    known = {qid for qid, _ in _SECTIONS}
    for qid, title in _SECTIONS:
        r = resolved.get(qid)
        if r is None:
            continue
        lines.append("")
        lines.append(f"## {title}")
        lines.append(r["choice"] or "（未填）")
        if r.get("note"):
            lines.append(f"备注：{r['note']}")

    unknown = [r for r in public["resolved"] if r["id"] not in known]
    if unknown:
        lines.append("")
        lines.append("## 已拍板事实")
        for r in unknown:
            line = f"- {r['question']} → {r['choice']}"
            if r.get("note"):
                line += f"（备注：{r['note']}）"
            lines.append(line)

    if public["out_of_scope"]:
        lines.append("")
        lines.append("## 明确不做")
        for o in public["out_of_scope"]:
            line = f"- {o['question']}"
            if o.get("reason"):
                line += f"：{o['reason']}"
            lines.append(line)

    open_items = public["pending"] + public["fog"]
    if open_items:
        lines.append("")
        lines.append("## 待拍板 / 还问不清")
        for q in open_items:
            lines.append(f"- {q['question']}")

    return "\n".join(lines) + "\n"


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

    term_lines = [
        f"- {q['question']}：{session['answers'][q['id']]['choice']}"
        for q in session["questions"]
        if _is_term(q) and q["id"] in session["answers"]
    ]
    if term_lines:
        ctx = out / "CONTEXT.md"
        block = [f"## 术语（grill-mcp 追加于 {_now()}）", *term_lines]
        with ctx.open("a", encoding="utf-8") as f:
            f.write("\n" + "\n".join(block) + "\n")
        written.append(str(ctx))

    decisions = [q for q in session["questions"] if _is_decision(q) and q["id"] in session["answers"]]
    if decisions:
        adr_dir = out / "docs" / "adr"
        adr_dir.mkdir(parents=True, exist_ok=True)
        num = _next_adr_number(adr_dir)
        adr = adr_dir / f"{num:04d}-{slug}.md"
        body = [f"# {requirement}"]
        for q in decisions:
            a = session["answers"][q["id"]]
            body.append("")
            body.append(a["choice"])
            if a.get("note"):
                body.append("")
                body.append(a["note"])
        adr.write_text("\n".join(body) + "\n", encoding="utf-8")
        written.append(str(adr))

    return written
