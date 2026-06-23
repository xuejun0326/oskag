"""render/markdown.py — DescribeReport → 中文 Markdown 文档 (重构: 叙述式排版).

新版排版:
- 顶部目录 (TOC), 每章可锚点跳转
- 章节顺序: 一、总览 → 二、综合评价 (synthesis) → 三..N、各模块 → 验证透明表 → footer
- 每个模块: TL;DR 引用框 → narrative (含 [ref:N] 上标超链接) → 关键数据结构 → 主要接口 → 引用索引 → 开放问题
- narrative 中 `[ref:N]` 自动渲染成 `<sup>[N](#mod-{name}-ref-{N})</sup>` 上标超链接
- 每模块尾部 "### 引用索引" 集中渲染 refs[] (锚点 + snippet 代码块), 让正文叙述干净
- innovations 沦为 fallback (synthesis 优先用)
- verifier verdict 符号 + 通过率统计保留
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from oskag.describe._modules import MODULE_ORDER, MODULE_TITLES
from oskag.describe.pipeline import DescribeReport, ModuleReport, VerifierVerdict
from oskag.logging_setup import get_logger

log = get_logger("oskag.render.markdown")


_VERDICT_SYMBOL = {
    "support": "✅",
    "partial": "🟡",
    "contradict": "❌",
    "unrelated": "❓",
    "skipped": "⏭",
}

_VERDICT_LABEL = {
    "support": "支持",
    "partial": "部分支持",
    "contradict": "反驳",
    "unrelated": "无关",
    "skipped": "跳过",
}

# narrative 里 LLM 写的 [ref:N] 标记, 渲染时替换为上标超链接
_REF_INLINE_RE = re.compile(r"\[ref:(\d+)\]")


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def render_describe(report: DescribeReport) -> str:
    """把 DescribeReport 渲染成完整 Markdown 文档.

    章节顺序:
        header → TOC → 一、总览 → 二、综合评价 → 三..N、各模块 → 验证透明表 → footer

    章节间用 `---` 分隔线强切割, 提升视觉层次 (排版约定).
    """
    SEP = "\n---\n"
    parts: list[str] = []
    parts.append(_render_header(report))

    # 计算章节编号 + TOC
    chapters = _build_chapter_index(report)
    parts.append(_render_toc(chapters, report))

    # 一、总览
    parts.append(_render_overview(report, chapters["overview"]))

    # 二、综合评价 (新章节, 走 synthesis; 若没 synthesis 则 fallback 旧 innovations)
    parts.append(_render_synthesis_or_innovations(report, chapters["synthesis"]))

    # 三..N、各模块
    by_name = {m.name: m for m in report.modules}
    for module_name, ch_label in chapters["modules"]:
        if module_name not in by_name:
            continue
        parts.append(_render_module(by_name[module_name], report, ch_label))

    # 验证透明表
    parts.append(_render_verifier_table(report.verifier_verdicts, chapters["verifier"]))

    parts.append(_render_footer(report))

    md = SEP.join(p for p in parts if p)
    log.info("markdown_rendered", repo=report.repo_name, chars=len(md),
             modules=len(report.modules), verdicts=len(report.verifier_verdicts))
    return md


# --------------------------------------------------------------------------- #
# 章节索引 + TOC
# --------------------------------------------------------------------------- #


def _chapter_label(idx: int) -> str:
    """1 → '一', 11 → '十一', 12 → '十二', ..."""
    digits = "〇一二三四五六七八九"
    if idx < 1:
        return str(idx)
    if idx < 10:
        return digits[idx]
    if idx == 10:
        return "十"
    if idx < 20:
        return "十" + digits[idx - 10]
    if idx < 100:
        tens, ones = divmod(idx, 10)
        out = digits[tens] + "十"
        if ones:
            out += digits[ones]
        return out
    return str(idx)


def _build_chapter_index(report: DescribeReport) -> dict:
    """根据实际包含的模块, 算出每个章节的中文编号 + 模块章节列表."""
    idx = {
        "overview": _chapter_label(1),     # 一、总览
        "synthesis": _chapter_label(2),    # 二、综合评价
        "modules": [],                     # [(module_name, chapter_label), ...]
        "verifier": "",                    # 末尾验证表
    }
    cur = 3
    # 直接遍历 report.modules (已按 describe() 里的顺序排好, 内核/自由模式通用)
    for m in report.modules:
        idx["modules"].append((m.name, _chapter_label(cur)))
        cur += 1
    idx["verifier"] = _chapter_label(cur)
    return idx


_MODULE_EMOJI = {
    "boot":    "🚀",
    "mm":      "🧠",
    "task":    "🧵",
    "fs":      "📁",
    "signal":  "📡",
    "ipc":     "🔄",
    "net":     "🌐",
    "drivers": "🔌",
    "syscall": "⚙️",
}


def _render_toc(chapters: dict, report: DescribeReport) -> str:
    """目录: 列出所有章节锚点. emoji 让目录视觉锚点更明显, 易扫读."""
    lines = ["## 目录", ""]
    lines.append(f"- 📖 {chapters['overview']}、总览")
    lines.append(f"- 🎯 {chapters['synthesis']}、综合评价")

    by_name = {m.name: m for m in report.modules}
    for module_name, ch_label in chapters["modules"]:
        m = by_name.get(module_name)
        if not m:
            continue
        title_zh = m.title_zh or module_name  # 用 ModuleReport 里的实际标题
        emoji = _MODULE_EMOJI.get(module_name, "📄")
        lines.append(f"- {emoji} {ch_label}、{title_zh}")
    lines.append(f"- 🔍 {chapters['verifier']}、验证透明表")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# header / footer
# --------------------------------------------------------------------------- #


def _render_header(report: DescribeReport) -> str:
    facts = report.facts or {}
    syscalls = facts.get("syscalls", {}) or {}
    repo_stats = facts.get("repo_stats", {}) or {}
    loc = repo_stats.get("loc", {}) or {}
    cargo = facts.get("cargo", {}) or {}

    scanned = facts.get("meta", {}).get("scanned_at") or \
              datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rendered_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    members_str = ""
    if cargo.get("is_workspace"):
        names = [m.get("name", "?") for m in cargo.get("members", []) or []]
        if names:
            members_str = ", ".join(names[:8])

    # 表格 (用表格而非纵向列表)
    rows = [
        ("📅 报告生成", rendered_at),
        ("📦 源码扫描", f"{scanned} (facts.json)"),
        ("🏷 内核家族", f"`{report.family}`"),
    ]
    if members_str:
        rows.append(("📚 Workspace", f"`{members_str}`"))
    rows.extend([
        ("📊 代码量", f"**{loc.get('total_files', '?')}** 文件 · **{loc.get('total_code', '?')}** 行"),
        ("🔌 syscall", f"**{syscalls.get('count', '?')}** 项"),
        ("⏱ 运行时长", f"**{report.duration_sec:.1f}s** · prompt={report.total_prompt_tokens:,} · completion={report.total_completion_tokens:,} · reasoning={report.total_reasoning_tokens:,}"),
    ])
    if report.errors:
        rows.append(("⚠ 错误", f"**{len(report.errors)}** 处 (见末尾)"))

    table_lines = [
        "| 项 | 值 |",
        "| --- | --- |",
    ]
    for k, v in rows:
        table_lines.append(f"| {k} | {v} |")

    return (
        f"# {report.repo_name} · 内核代码分析报告\n\n"
        + "\n".join(table_lines)
    )


def _render_footer(report: DescribeReport) -> str:
    bits = []
    if report.warnings:
        bits.append("## ⚠ 警告\n\n" + "\n".join(f"- {w}" for w in report.warnings))
    if report.errors:
        bits.append("## ❌ 错误\n\n" + "\n".join(f"- {e}" for e in report.errors))
    bits.append(
        "<sub>📌 _本报告由 [oskag](https://github.com/) describe 自动生成, "
        "所有引用经 verifier 二次校验。_</sub>"
    )
    return "\n\n".join(bits)


# --------------------------------------------------------------------------- #
# 总览 / 综合评价
# --------------------------------------------------------------------------- #


def _render_rating_table(rating: dict) -> str:
    """评分表 (用表格而非纵向列表)."""
    if not rating:
        return ""
    rows = []
    for key, label in (("completeness", "完整度"),
                       ("innovation", "创新性"),
                       ("code_quality", "代码质量")):
        v = rating.get(key)
        if v is None:
            continue
        reason = (rating.get(f"{key}_reason") or "").strip() or "—"
        rows.append((label, _stars(v), reason))
    if not rows:
        return ""
    out = "\n\n### 📊 评分\n\n| 维度 | 评级 | 评分理由 |\n| --- | --- | --- |\n"
    for label, stars, reason in rows:
        out += f"| **{label}** | {stars} | {reason} |\n"
    return out


def _render_overview(report: DescribeReport, chapter_no: str) -> str:
    body = report.overview.strip() if report.overview else "_(synthesize 阶段未返回总览)_"
    out = f"## 📖 {chapter_no}、总览\n\n{body}"
    out += _render_rating_table(report.rating or {})
    if report.syscall_coverage_comment:
        out += (
            "\n\n> 🔌 **syscall 覆盖**\n>\n> "
            + report.syscall_coverage_comment.replace("\n", "\n> ")
        )
    return out


def _stars(n) -> str:
    """rating 1-3 → ★ 数, 容错: 非法值返回 '?'."""
    try:
        n_int = int(n)
    except (TypeError, ValueError):
        return "?"
    n_int = max(0, min(3, n_int))
    return "★" * n_int + "☆" * (3 - n_int)


def _render_synthesis_or_innovations(report: DescribeReport, chapter_no: str) -> str:
    """渲染综合评价章节. 优先用 synthesis (新), 没有则 fallback innovations (旧)."""
    if report.synthesis and report.synthesis.strip():
        return f"## 🎯 {chapter_no}、综合评价\n\n{report.synthesis.strip()}"

    # fallback: 旧 innovations 列表
    if report.innovations:
        out = [f"## 🎯 {chapter_no}、综合评价"]
        out.append("\n_（注: 本仓库 synthesize 阶段未返回 synthesis 叙述, 退化展示为创新点列表）_")
        for i, inn in enumerate(report.innovations, 1):
            title = inn.get("title", "(无标题)")
            desc = inn.get("desc", "")
            out.append(f"\n### ✨ {i}. {title}\n\n{desc}")
            ev = inn.get("evidence") or []
            if ev:
                out.append("\n**证据**:\n")
                for e in ev:
                    out.append(f"- `{e.get('file', '?')}:{e.get('lines', '?')}`")
        return "\n".join(out).rstrip()

    return f"## 🎯 {chapter_no}、综合评价\n\n_(synthesize 阶段未返回综合评价)_"


# --------------------------------------------------------------------------- #
# 模块章节
# --------------------------------------------------------------------------- #


def _render_module(m: ModuleReport, report: DescribeReport,
                   chapter_no: str) -> str:
    title = m.title_zh or MODULE_TITLES.get(m.name, {}).get("zh", m.name)
    emoji = _MODULE_EMOJI.get(m.name, "📄")
    head = f"## {emoji} {chapter_no}、{title}"

    if m.unimplemented:
        return f"{head}\n\n> 📭 **未实现** · 本仓库未发现该模块的实现代码。"

    # 主路径解析失败 + 降级兜底成功 → 仍渲染 narrative, 顶部加警示标记
    if m.error and m.narrative:
        return (
            f"{head}\n\n"
            f"> ⚠ **降级模式** · 主流程 JSON 解析失败 (`{m.error}`)\n>\n"
            f"> 以下为兜底分析, refs / 数据结构 / 接口表均为空。\n\n"
            f"{m.narrative}"
        )

    if m.error:
        return f"{head}\n\n> ❌ **该模块描述失败**: `{m.error}`"

    parts = [head]

    # TL;DR 引用框 (callout 分级)
    if m.summary:
        # 把多行 summary 处理成 callout 多行格式
        summary_lines = m.summary.strip().split("\n")
        callout = "\n>\n> ".join(summary_lines)
        parts.append(f"> 💡 **TL;DR**\n>\n> {callout}")

    # boot 章节追加 mermaid call_graph
    if m.name == "boot" and report.call_graph_mermaid:
        parts.append("### 🚦 启动调用图\n\n" + report.call_graph_mermaid)

    # 主体 narrative (含 [ref:N] 上标超链接)
    if m.narrative:
        parts.append(_render_module_narrative(m.narrative, m.name))

    # 关键数据结构 / 主要接口
    parts.extend(_render_data_structures(m.data_structures, m.name))
    parts.extend(_render_interfaces(m.interfaces, m.name))

    # 引用索引 (锚点 + snippet)
    refs_block = _render_refs_index(m.refs, m.name)
    if refs_block:
        parts.append(refs_block)

    # 开放问题
    open_block = _render_open_issues(m.open_issues)
    if open_block:
        parts.append(open_block)

    return "\n\n".join(parts).rstrip()


def _render_module_narrative(narrative: str, module_name: str) -> str:
    """把 narrative 文本渲染成 markdown.

    [ref:N] 自动替换为 <sup>[N](#mod-{module_name}-ref-{N})</sup> 上标超链接.
    其余内容原样保留 (LLM 在 narrative 里可以用 ### 子标题、列表等).
    """
    def replace(match: re.Match) -> str:
        n = match.group(1)
        anchor = f"mod-{module_name}-ref-{n}"
        return f"<sup>[{n}](#{anchor})</sup>"

    return _REF_INLINE_RE.sub(replace, narrative.strip())


def _render_refs_index(refs: list[dict], module_name: str) -> str:
    """模块尾部的"引用索引"子节. 每条:

    <a id="mod-mm-ref-1"></a>
    > **🔖 [1]** `core/src/mm.rs:21-32` — copy_from_kernel 的 cfg gate

    ```rust
    <snippet>
    ```

    排版约定:
    - 标题用 callout 引用框 (#3) 让每条 ref 视觉上独立
    - 路径 + 行号用 `code` 包裹 (#5)
    """
    if not refs:
        return ""

    lines = ["### 🔖 引用索引", ""]
    for r in refs:
        if not isinstance(r, dict):
            continue
        rid = r.get("id", "?")
        anchor = f"mod-{module_name}-ref-{rid}"
        file_ = r.get("file", "?")
        loc = r.get("lines", "?")
        why = (r.get("why") or "").strip()
        snippet = (r.get("snippet") or "").strip()

        lines.append(f'<a id="{anchor}"></a>')
        line_head = f"**[{rid}]** `{file_}:{loc}`"
        if why:
            line_head += f" — {why}"
        lines.append(line_head)
        if snippet:
            lines.append("")
            lines.append("```rust")
            lines.append(snippet)
            lines.append("```")
        lines.append("")
    return "\n".join(lines).rstrip()


def _render_open_issues(open_issues: list[str]) -> str:
    """模块尾部的"开放问题"子节. 每条用 ⚠ 图标作"chip"."""
    if not open_issues:
        return ""
    lines = ["### ⚠ 开放问题", ""]
    for issue in open_issues:
        text = str(issue).strip() if not isinstance(issue, str) else issue.strip()
        if text:
            lines.append(f"- {text}")
    return "\n".join(lines)


def _render_data_structures(data_structures: list[dict], module_name: str = "") -> list[str]:
    """关键数据结构表格. 表格视觉密度比纵向列表高 (排版约定)."""
    if not data_structures:
        return []
    rows = ["### 🧩 关键数据结构", ""]
    rows.append("| 名称 | 位置 | 引用 | 职责 |")
    rows.append("| --- | --- | --- | --- |")
    for ds in data_structures:
        name = ds.get("name", "?")
        file_ = ds.get("file", "?")
        line = ds.get("line", "?")
        desc = (ds.get("desc", "") or "").replace("|", "\\|").replace("\n", " ")
        ref_id = ds.get("ref_id")
        ref_link = f"[{ref_id}](#mod-{module_name}-ref-{ref_id})" if ref_id and module_name else "—"
        rows.append(f"| `{name}` | `{file_}:{line}` | {ref_link} | {desc} |")
    return ["\n".join(rows)]


def _render_interfaces(interfaces: list[dict], module_name: str = "") -> list[str]:
    """主要接口表格."""
    if not interfaces:
        return []
    rows = ["### 🔧 主要接口", ""]
    rows.append("| 函数 | 位置 | 引用 | 用途 |")
    rows.append("| --- | --- | --- | --- |")
    for it in interfaces:
        fn = it.get("fn", "?")
        file_ = it.get("file", "?")
        line = it.get("line", "?")
        desc = (it.get("desc", "") or "").replace("|", "\\|").replace("\n", " ")
        ref_id = it.get("ref_id")
        ref_link = f"[{ref_id}](#mod-{module_name}-ref-{ref_id})" if ref_id and module_name else "—"
        rows.append(f"| `{fn}` | `{file_}:{line}` | {ref_link} | {desc} |")
    return ["\n".join(rows)]


# --------------------------------------------------------------------------- #
# 验证透明表
# --------------------------------------------------------------------------- #


def _render_verifier_table(verdicts: list[VerifierVerdict],
                           chapter_no: str) -> str:
    if not verdicts:
        return f"## 🔍 {chapter_no}、验证透明表\n\n_(无 evidence 被校验)_"

    counts: dict[str, int] = {}
    for v in verdicts:
        counts[v.verdict] = counts.get(v.verdict, 0) + 1

    # 顶部统计 (排版约定: 表格汇总, 比一行 emoji 紧凑更易扫)
    summary_rows = [
        "| 状态 | 数量 | 占比 |",
        "| --- | --- | --- |",
    ]
    total = len(verdicts)
    for verdict_name in ("support", "partial", "contradict", "unrelated", "skipped"):
        if verdict_name not in counts:
            continue
        n = counts[verdict_name]
        sym = _VERDICT_SYMBOL[verdict_name]
        label = _VERDICT_LABEL[verdict_name]
        summary_rows.append(f"| {sym} {label} | {n} | {n/total:.0%} |")

    pass_rate = (counts.get("support", 0) + counts.get("partial", 0)) / total
    strict_pass = counts.get("support", 0) / total

    out = [
        f"## 🔍 {chapter_no}、验证透明表",
        "",
        f"对 LLM 输出的 **{total}** 条引证 evidence 进行二次重读校验。",
        "",
        "\n".join(summary_rows),
        "",
        f"> 📈 **通过率**: 严格 (仅 ✅ support) **{strict_pass:.0%}** · "
        f"宽松 (含 🟡 partial) **{pass_rate:.0%}**",
        "",
        "### 详细校验明细",
        "",
        "| # | 模块 | 论断 | 引证 | verdict | 说明 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for i, v in enumerate(verdicts, 1):
        sym = _VERDICT_SYMBOL.get(v.verdict, "?")
        label = _VERDICT_LABEL.get(v.verdict, v.verdict)
        loc_str = f"`{v.file}:{v.lines}`"
        claim = (v.claim or "").replace("|", "\\|").replace("\n", " ")[:50]
        reason = (v.reason or v.error or "").replace("|", "\\|").replace("\n", " ")[:80]
        out.append(f"| {i} | {v.module} | {claim} | {loc_str} | {sym} {label} | {reason} |")

    return "\n".join(out)


__all__ = ["render_describe"]
