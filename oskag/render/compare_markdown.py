"""render/compare_markdown.py — CompareReport → 中文 Markdown 文档 .

设计:
- 章节顺序遵循固定模板 (总览 → 各域对照 → 综合判断 → 验证透明表)
- 不画 mermaid 调用图 (用户 2026-06-16 调整: 第一版主打"讲清楚"); 6.5 节用高频函数列表
- 低相似度 (<0.15) 场景下章节 9 自适应为"独立设计对照"
- 末尾"验证透明表"复用 describe 风格 (verdict 符号映射)
"""
from __future__ import annotations

from datetime import datetime, timezone

from oskag.logging_setup import get_logger
from oskag.pipelines.compare import CompareReport, ModuleCompare

log = get_logger("oskag.render.compare_markdown")


_VERDICT_SYMBOL = {
    "support": "✓",
    "partial": "~",
    "contradict": "✗",
    "unrelated": "?",
    "skipped": "…",
}

# verdict 中文化
_MODULE_VERDICT_LABEL = {
    "similar": "做法基本一致",
    "divergent": "实现路径分化",
    "independent": "独立设计",
    "a_only": "仅 A 实现",
    "b_only": "仅 B 实现",
    "both_missing": "两边都未实现",
}

_NOVELTY_VERDICT_LABEL = {
    "derivative": "派生关系 — B 在 A 的基础上演化",
    "divergent": "同源分化 — 两仓共享起点但走向不同",
    "independent": "独立设计 — 两仓基本无对应关系",
}

# 表格分隔符常量 (避免重复字面量)
_TABLE_SEP_3COL = "|---|---|---|"
_TABLE_SEP_4COL = "|---|---|---|---|"
_TABLE_SEP_2COL = "|---|---|"


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def render_compare(report: CompareReport) -> str:
    """把 CompareReport 渲染成完整 Markdown 文档."""
    parts: list[str] = []
    parts.append(_render_header(report))
    parts.append(_render_overview(report))
    parts.append(_render_structure(report))
    parts.append(_render_deps(report))
    parts.append(_render_syscalls(report))
    parts.append(_render_signatures(report))
    parts.append(_render_callgraph(report))
    parts.append(_render_module_compares(report))
    parts.append(_render_novelty(report))
    parts.append(_render_similarity(report))
    parts.append(_render_footer(report))

    md = "\n\n".join(p for p in parts if p)
    log.info("compare_markdown_rendered", a=report.a_repo, b=report.b_repo,
             chars=len(md))
    return md


# --------------------------------------------------------------------------- #
# 各章节
# --------------------------------------------------------------------------- #


def _render_header(report: CompareReport) -> str:
    sim = report.similarity
    overall = sim.overall if sim else 0.0
    label = sim.label if sim else "未知"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"# {report.a_repo} ↔ {report.b_repo} 对比分析报告",
        "",
        f"> **生成时间**: {ts}",
        f"> **对比方向**: 以 **{report.a_repo}** 为基准 (A), 分析 **{report.b_repo}** (B) 的差异",
        f"> **运行时长**: {report.duration_sec}s · prompt={report.total_prompt_tokens} · "
        f"completion={report.total_completion_tokens} · reasoning={report.total_reasoning_tokens}",
        f"> **综合相似度**: **{overall}** ({label})",
    ]
    return "\n".join(lines)


def _render_overview(report: CompareReport) -> str:
    a = report.a_basic
    b = report.b_basic
    lines = [
        "## 一、总览",
        "",
        "| 维度 | A: " + report.a_repo + " | B: " + report.b_repo + " |",
        _TABLE_SEP_3COL,
        _row("家族", _code(a.get("family")), _code(b.get("family"))),
        _row("Cargo 形态",
             "workspace · " + str(a.get("member_count", 0)) + " 成员"
             if a.get("is_workspace") else "单 crate",
             "workspace · " + str(b.get("member_count", 0)) + " 成员"
             if b.get("is_workspace") else "单 crate"),
        _row("文件数", a.get("loc_files", 0), b.get("loc_files", 0)),
        _row("代码行数", a.get("loc_code", 0), b.get("loc_code", 0)),
        _row("syscall 数", a.get("syscall_count", 0), b.get("syscall_count", 0)),
        _row("启动方式", _code(a.get("boot_style")), _code(b.get("boot_style"))),
        _row("trap handlers",
             ", ".join(a.get("handler_kinds") or []) or "—",
             ", ".join(b.get("handler_kinds") or []) or "—"),
        _row("已实现模块",
             " / ".join(a.get("implemented_modules") or []) or "—",
             " / ".join(b.get("implemented_modules") or []) or "—"),
        _row("未实现模块",
             " / ".join(a.get("unimplemented_modules") or []) or "无",
             " / ".join(b.get("unimplemented_modules") or []) or "无"),
    ]
    return "\n".join(lines)


def _render_structure(report: CompareReport) -> str:
    dd = report.dir_diff or {}
    only_a = dd.get("only_a", [])[:15]
    only_b = dd.get("only_b", [])[:15]
    common = dd.get("common", [])[:15]
    lines = [
        "## 二、结构差异",
        "",
        f"**目录 Jaccard**: {dd.get('jaccard', 0.0)}",
        "",
        "| 仅 A 有 | 共有 | 仅 B 有 |",
        _TABLE_SEP_3COL,
    ]
    n = max(len(only_a), len(only_b), len(common))
    for i in range(n):
        a_v = f"`{only_a[i]}`" if i < len(only_a) else "—"
        c_v = f"`{common[i]}`" if i < len(common) else "—"
        b_v = f"`{only_b[i]}`" if i < len(only_b) else "—"
        lines.append(f"| {a_v} | {c_v} | {b_v} |")
    return "\n".join(lines)


def _render_deps(report: CompareReport) -> str:
    d = report.deps_diff or {}
    only_a = d.get("only_a", [])[:15]
    only_b = d.get("only_b", [])[:15]
    lines = [
        "## 三、依赖差异",
        "",
        f"- A 总依赖数: **{d.get('a_count', 0)}**",
        f"- B 总依赖数: **{d.get('b_count', 0)}**",
        f"- 交集: **{d.get('intersection_count', 0)}**",
        f"- 仅 A 有: {len(d.get('only_a', []))} 项",
        f"- 仅 B 有: {len(d.get('only_b', []))} 项",
        f"- **依赖 Jaccard**: **{d.get('jaccard', 0.0)}**",
        "",
        "### 仅 A 有的代表性依赖 (前 15)",
        "",
    ]
    if only_a:
        lines.extend(f"- `{x}`" for x in only_a)
    else:
        lines.append("(无)")

    lines.append("")
    lines.append("### 仅 B 有的代表性依赖 (前 15)")
    lines.append("")
    if only_b:
        lines.extend(f"- `{x}`" for x in only_b)
    else:
        lines.append("(无)")

    return "\n".join(lines)


def _render_syscalls(report: CompareReport) -> str:
    d = report.syscall_diff or {}
    only_a = d.get("only_a", [])
    only_b = d.get("only_b", [])
    lines = [
        "## 四、syscall 差异",
        "",
        f"- A 实现 syscall 数: **{d.get('a_count', 0)}**",
        f"- B 实现 syscall 数: **{d.get('b_count', 0)}**",
        f"- 共同实现: **{d.get('intersection_count', 0)}**",
        f"- **syscall Jaccard**: **{d.get('jaccard', 0.0)}**",
        "",
        f"### 4.1 仅 A 实现的 syscall ({len(only_a)} 个)",
        "",
        _wrap_codeline(only_a, 12),
        "",
        f"### 4.2 仅 B 实现的 syscall ({len(only_b)} 个)",
        "",
        _wrap_codeline(only_b, 12),
    ]
    return "\n".join(lines)


def _render_signatures(report: CompareReport) -> str:
    d = report.sig_diff or {}
    only_a = d.get("only_a_sample", [])
    only_b = d.get("only_b_sample", [])
    lines = [
        "## 五、函数签名 Jaccard",
        "",
        f"- A 公开函数签名数 (`pub fn`): **{d.get('a_count', 0)}**",
        f"- B 公开函数签名数: **{d.get('b_count', 0)}**",
        f"- 完全相同的签名: **{d.get('intersection_count', 0)}**",
        f"- **函数签名 Jaccard**: **{d.get('jaccard', 0.0)}**",
        "",
        "### 5.1 仅 A 暴露的接口样本 (前 10)",
        "",
    ]
    if only_a:
        lines.extend(f"- `{s}`" for s in only_a)
    else:
        lines.append("(无)")
    lines.append("")
    lines.append("### 5.2 仅 B 暴露的接口样本 (前 10)")
    lines.append("")
    if only_b:
        lines.extend(f"- `{s}`" for s in only_b)
    else:
        lines.append("(无)")

    return "\n".join(lines)


def _render_callgraph(report: CompareReport) -> str:
    cg = report.cg_diff or {}
    a_top = cg.get("a_top_callees", [])[:10]
    b_top = cg.get("b_top_callees", [])[:10]
    lines = [
        "## 六、调用图差异 (轻量版)",
        "",
        "> 第一版用集合层数字 + 高频函数名列表把『两个内核调用模式有多接近』讲清楚, 不画 mermaid (后续优化项)。",
        "",
        "### 6.1 调用图统计",
        "",
        "| 维度 | A | B |",
        _TABLE_SEP_3COL,
        _row("节点数 (函数)", cg.get("a_nodes", 0), cg.get("b_nodes", 0)),
        _row("边数 (调用关系)", cg.get("a_edges", 0), cg.get("b_edges", 0)),
        _row("平均出度", cg.get("a_avg_out_degree", 0.0), cg.get("b_avg_out_degree", 0.0)),
        "",
        f"### 6.2 节点 Jaccard: **{cg.get('node_jaccard', 0.0)}**",
        "",
        f"### 6.3 边 Jaccard: **{cg.get('edge_jaccard', 0.0)}**",
        "",
        f"### 6.4 综合调用图相似度 = 0.5·node + 0.5·edge = **{cg.get('callgraph_score', 0.0)}**",
        "",
        "### 6.5 高频被调函数 Top 10",
        "",
        f"**A ({report.a_repo})**:",
        "",
    ]
    if a_top:
        lines.extend(f"- `{e['name']}` — {e['count']} 次" for e in a_top)
    else:
        lines.append("(无数据)")
    lines.append("")
    lines.append(f"**B ({report.b_repo})**:")
    lines.append("")
    if b_top:
        lines.extend(f"- `{e['name']}` — {e['count']} 次" for e in b_top)
    else:
        lines.append("(无数据)")

    return "\n".join(lines)


def _render_module_compares(report: CompareReport) -> str:
    if not report.module_compares:
        return _module_compares_skipped()
    lines = [
        "## 七、模块级语义对照",
        "",
        "> 每条差异点的 file:line 会进入 §九 的验证透明表逐条核对。",
        "",
    ]
    for i, mc in enumerate(report.module_compares, 1):
        lines.append(_render_one_module_compare(i, mc))
        lines.append("")
    return "\n".join(lines)


def _module_compares_skipped() -> str:
    return ("## 七、模块级语义对照\n\n"
            "*本节由 LLM 输出, 本次运行已跳过 (skip_llm=True)。*")


def _render_one_module_compare(idx: int, mc: ModuleCompare) -> str:
    verdict_label = _MODULE_VERDICT_LABEL.get(mc.verdict, mc.verdict)
    lines = [
        f"### 7.{idx} {mc.title_zh} (`{mc.name}`)",
        "",
        f"**对照判定**: {verdict_label}",
        "",
    ]
    if mc.error:
        lines.append(f"*LLM 调用出错: {mc.error}*")
        return "\n".join(lines)

    if not mc.a_implemented and not mc.b_implemented:
        lines.append("两仓均未实现该模块。")
        return "\n".join(lines)

    lines.extend(_render_module_summaries(mc))
    lines.extend(_render_module_diff_points(mc))
    return "\n".join(lines)


def _render_module_summaries(mc: ModuleCompare) -> list[str]:
    """A 仓 / B 仓的做法摘要."""
    lines: list[str] = []
    a_text = (f"**A 仓做法**: {mc.a_summary or '(LLM 未给出)'}"
              if mc.a_implemented else "**A 仓**: 未实现该模块")
    b_text = (f"**B 仓做法**: {mc.b_summary or '(LLM 未给出)'}"
              if mc.b_implemented else "**B 仓**: 未实现该模块")
    lines.extend([a_text, "", b_text, ""])
    return lines


def _render_module_diff_points(mc: ModuleCompare) -> list[str]:
    """模块对照里的 diff_points 列表."""
    if not mc.diff_points:
        return ["(无显著差异)"]
    lines = ["**关键差异点**:", ""]
    for dp in mc.diff_points:
        lines.append(f"- **{dp.get('point', '')}** ({dp.get('kind', '差异')})")
        ev_lines = _render_evidence_pair(
            dp.get("a_evidence", {}) or {},
            dp.get("b_evidence", {}) or {},
        )
        lines.extend(f"  {line}" for line in ev_lines)
    return lines


def _render_novelty(report: CompareReport) -> str:
    nv = report.novelty or {}
    if not nv:
        return ("## 八、B 相对 A 的关键差异\n\n"
                "*本节由 LLM 输出, 本次运行已跳过 (skip_llm=True)。*")

    lines = _novelty_header(nv)
    diffs = nv.get("diffs", []) or []
    if not diffs:
        lines.append("*LLM 未识别出关键差异。*")
        return "\n".join(lines)

    for i, d in enumerate(diffs, 1):
        lines.extend(_render_one_novelty_diff(i, d))
    return "\n".join(lines)


def _novelty_header(nv: dict) -> list[str]:
    """渲染 novelty 章节的头 (标题 + 总评 + verdict)."""
    verdict = nv.get("verdict", "")
    verdict_label = _NOVELTY_VERDICT_LABEL.get(verdict, verdict or "(未给出)")
    return [
        "## 八、B 相对 A 的关键差异",
        "",
        f"**总评**: {nv.get('summary', '(LLM 未给出总评)')}",
        "",
        f"**整体定位**: {verdict_label}",
        "",
    ]


def _render_one_novelty_diff(i: int, d: dict) -> list[str]:
    """渲染单条 novelty 差异点."""
    stars = _stars_safe(d.get("novelty", 1))
    lines = [
        f"### 8.{i} {d.get('title', '(无标题)')}",
        "",
        f"**类型**: {d.get('kind', '差异')} · **显著度**: {stars}",
        "",
        d.get("desc", ""),
        "",
    ]
    lines.extend(_render_evidence_pair(
        d.get("a_evidence", {}) or {},
        d.get("b_evidence", {}) or {},
    ))
    lines.append("")
    return lines


def _stars_safe(n) -> str:
    """1-5 的 novelty 转成 ★☆ 字符串, 任何非整数兜底为 1."""
    n = n if isinstance(n, int) else 1
    n = max(1, min(5, n))
    return "★" * n + "☆" * (5 - n)


def _render_evidence_pair(a_ev: dict, b_ev: dict) -> list[str]:
    """渲染 (A, B) 一对 file:line 引证."""
    out: list[str] = []
    if a_ev.get("file"):
        out.append(f"- A: `{a_ev.get('file')}:{a_ev.get('lines', '?')}`"
                   f" — {a_ev.get('note', '')}")
    if b_ev.get("file"):
        out.append(f"- B: `{b_ev.get('file')}:{b_ev.get('lines', '?')}`"
                   f" — {b_ev.get('note', '')}")
    return out


def _render_similarity(report: CompareReport) -> str:
    sim = report.similarity
    if sim is None:
        return ""
    d = sim.to_dict()
    lines = [
        "## 九、综合相似度评分",
        "",
        "### 9.1 各维度得分",
        "",
        "| 维度 | 权重 | A↔B 得分 | 加权 |",
        _TABLE_SEP_4COL,
        f"| 函数签名 Jaccard | 0.30 | {d['sig_jaccard']} | {d['sig_weighted']} |",
        f"| syscall Jaccard | 0.20 | {d['syscall_jaccard']} | {d['syscall_weighted']} |",
        f"| 依赖 Jaccard | 0.20 | {d['deps_jaccard']} | {d['deps_weighted']} |",
        f"| 调用图综合 | 0.20 | {d['callgraph_score']} | {d['callgraph_weighted']} |",
        f"| 目录 Jaccard | 0.10 | {d['dir_jaccard']} | {d['dir_weighted']} |",
        f"| **合计** | **1.00** | — | **{d['overall_score']}** |",
        "",
        "### 9.2 等级判定",
        "",
        "| 范围 | 等级 |",
        _TABLE_SEP_2COL,
        "| ≥ 0.70 | 高度相似 |",
        "| 0.40 - 0.70 | 中度相似 |",
        "| 0.15 - 0.40 | 低度相似 |",
        "| < 0.15 | 完全不同 |",
        "",
        f"**本次评级**: **{d['overall_label']}** ({d['overall_score']})",
        "",
        "### 9.3 公式",
        "",
        "```text",
        "overall = 0.30 × sig_jaccard",
        "        + 0.20 × syscall_jaccard",
        "        + 0.20 × deps_jaccard",
        "        + 0.20 × callgraph_score (= 0.5 × node_jaccard + 0.5 × edge_jaccard)",
        "        + 0.10 × dir_jaccard",
        "```",
    ]
    return "\n".join(lines)


def _render_footer(report: CompareReport) -> str:
    lines = ["## 十、附录: 警告与错误", ""]
    if report.warnings:
        lines.append(f"### 警告 ({len(report.warnings)})")
        lines.append("")
        lines.extend(f"- {w}" for w in report.warnings)
        lines.append("")
    if report.errors:
        lines.append(f"### 错误 ({len(report.errors)})")
        lines.append("")
        lines.extend(f"- {e}" for e in report.errors)
        lines.append("")
    if not report.warnings and not report.errors:
        lines.append("*无警告或错误。*")
    lines.append("")
    lines.append("---")
    lines.append("*本报告由 oskag compare 自动生成, §七/§八 引用均经 verifier 二次校验, 不修饰失败。*")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def _row(*cells) -> str:
    return "| " + " | ".join(str(c) for c in cells) + " |"


def _code(s) -> str:
    if s is None or s == "":
        return "—"
    return f"`{s}`"


def _wrap_codeline(items: list, per_line: int = 10) -> str:
    """把一组短字符串包成 inline code 行, 每行 N 个."""
    if not items:
        return "(无)"
    out: list[str] = []
    for i in range(0, len(items), per_line):
        chunk = items[i:i + per_line]
        out.append(", ".join(f"`{x}`" for x in chunk))
    return "  \n".join(out)


__all__ = ["render_compare"]
