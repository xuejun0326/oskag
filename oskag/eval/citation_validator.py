"""oskag/eval/citation_validator.py — file:line 引用自动验证 .

目的: 把"反幻觉/精准无误"从设计声明变成可计算的硬指标.

输入: 一份或多份 oskag describe / compare 生成的 markdown 报告 + 仓库根路径映射.
输出: 一个 ValidationReport, 含每条引用的 ✓/⚠/✗ 状态与原因.

引用提取规则 (从模板观察 + LLM 输出习惯):
- 反引号包裹的 `path/to/file.ext:N` 或 `path/to/file.ext:N-M`
- ext ∈ {rs, toml, S, s, c, ld, h, asm, rs.in}
- 路径首字符 [a-zA-Z_/]: 排除 lone 数字 / 空字符串
- N 为正整数, M 可选, M ≥ N

校验规则:
- exists: 文件在磁盘上 (相对仓库根可解析)
- line_range_valid: 1 ≤ N, 且 N ≤ M (若给), 且 M ≤ 文件行数
- 失败时记录 reason_code (Reason 枚举) + reason 可读字符串

不调任何 LLM, 纯静态分析 + 文件 IO.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable

from oskag.logging_setup import get_logger

log = get_logger("oskag.eval.citation")


class Reason(str, Enum):
    """引用校验的失败/警告原因码 ([[fix-zero-line-rule]] 引入)."""

    OK = "ok"
    LINE_START_ZERO = "line_start_zero"          # line_start < 1, LLM 用 :0 占位
    LINE_INVERTED = "line_inverted"              # line_end < line_start
    PATH_ESCAPES_REPO = "path_escapes_repo"      # ../ 越狱
    FILE_NOT_FOUND = "file_not_found"
    FILE_UNREADABLE = "file_unreadable"          # warn, 不算 fail
    LINE_OUT_OF_RANGE = "line_out_of_range"
    # compare 双仓校验时, 失败原因合成 (见 validate_compare_report)
    COMPARE_BOTH_FAIL = "compare_both_fail"


# 反引号引用: `path/to/file.ext:N` 或 `path/to/file.ext:N-M`
# - 路径首字符: 字母 / 下划线 / 斜杠 (不允许纯数字开头, 排除 `__libc__:1` 这种)
# - 路径字符集: 字母数字 / _ / . / / / -
# - 扩展名: 内核常见
_EXTS = ("rs", "toml", "S", "s", "c", "ld", "h", "asm")
_CITATION_RE = re.compile(
    r"`([a-zA-Z_/][a-zA-Z0-9_./\-]*?\.(?:" + "|".join(_EXTS) + r")):(\d+)(?:-(\d+))?`"
)


# --------------------------------------------------------------------------- #
# 数据类
# --------------------------------------------------------------------------- #


@dataclass
class Citation:
    """一条 file:line 引用."""

    raw: str
    file: str
    line_start: int
    line_end: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CitationCheck:
    """一条引用的校验结果."""

    citation: Citation
    status: str  # "ok" | "warn" | "fail"
    reason: str = ""              # 人可读消息 (兼容历史; 含具体数值如 "line_start=0 < 1")
    reason_code: str = "ok"       # 机读枚举 (Reason 值; 默认 "ok")

    def to_dict(self) -> dict:
        return {
            "citation": self.citation.to_dict(),
            "status": self.status,
            "reason": self.reason,
            "reason_code": self.reason_code,
        }


@dataclass
class ValidationReport:
    """一份报告或一组报告的聚合结果."""

    source: str  # markdown 文件名 or "merged"
    repo_root: str  # 校验所用的仓库根
    total: int = 0
    ok: int = 0
    warn: int = 0
    fail: int = 0
    checks: list[CitationCheck] = field(default_factory=list)

    @property
    def precision(self) -> float:
        """ok / total — 反幻觉精确率."""
        return self.ok / self.total if self.total else 0.0

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "repo_root": self.repo_root,
            "total": self.total,
            "ok": self.ok,
            "warn": self.warn,
            "fail": self.fail,
            "precision": round(self.precision, 4),
            "checks": [c.to_dict() for c in self.checks],
        }


# --------------------------------------------------------------------------- #
# 提取
# --------------------------------------------------------------------------- #


def extract_citations(markdown: str) -> list[Citation]:
    """从一份 markdown 文本里抽出所有 file:line 引用.

    重复引用保留 (一篇报告里同一条 evidence 多处引用是合理的).
    """
    out: list[Citation] = []
    for m in _CITATION_RE.finditer(markdown):
        raw = m.group(0)
        file_path = m.group(1)
        line_start = int(m.group(2))
        line_end_str = m.group(3)
        line_end = int(line_end_str) if line_end_str else line_start
        out.append(Citation(raw=raw, file=file_path, line_start=line_start, line_end=line_end))
    return out


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #


def _count_lines(p: Path) -> int:
    """文件行数, 不读全文 (大文件友好)."""
    try:
        with p.open("rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return -1


def validate_citation(c: Citation, repo_root: Path) -> CitationCheck:
    """单条引用校验."""
    if c.line_start < 1:
        return CitationCheck(c, "fail",
                             reason=f"line_start={c.line_start} < 1",
                             reason_code=Reason.LINE_START_ZERO.value)
    if c.line_end < c.line_start:
        return CitationCheck(c, "fail",
                             reason=f"line_inverted (end={c.line_end} < start={c.line_start})",
                             reason_code=Reason.LINE_INVERTED.value)

    target = (repo_root / c.file).resolve()
    # 防 ../ 越狱: target 必须仍然在 repo_root 之下
    try:
        target.relative_to(repo_root.resolve())
    except ValueError:
        return CitationCheck(c, "fail",
                             reason="path_escapes_repo_root",
                             reason_code=Reason.PATH_ESCAPES_REPO.value)

    if not target.is_file():
        return CitationCheck(c, "fail",
                             reason="file_not_found",
                             reason_code=Reason.FILE_NOT_FOUND.value)

    nlines = _count_lines(target)
    if nlines < 0:
        return CitationCheck(c, "warn",
                             reason="file_unreadable",
                             reason_code=Reason.FILE_UNREADABLE.value)
    if c.line_end > nlines:
        return CitationCheck(c, "fail",
                             reason=f"line_out_of_range (end={c.line_end} > file_lines={nlines})",
                             reason_code=Reason.LINE_OUT_OF_RANGE.value)

    return CitationCheck(c, "ok", reason="", reason_code=Reason.OK.value)


def validate_report(
    markdown_path: Path | str,
    repo_root: Path | str,
    *,
    source_label: str | None = None,
) -> ValidationReport:
    """一份 markdown 报告 + 一个仓库根 → ValidationReport."""
    md_path = Path(markdown_path)
    root = Path(repo_root)
    text = md_path.read_text(encoding="utf-8", errors="replace")
    citations = extract_citations(text)
    rep = ValidationReport(
        source=source_label or md_path.name,
        repo_root=str(root),
    )
    for c in citations:
        chk = validate_citation(c, root)
        rep.checks.append(chk)
        rep.total += 1
        if chk.status == "ok":
            rep.ok += 1
        elif chk.status == "warn":
            rep.warn += 1
        else:
            rep.fail += 1
    log.info("citation_validation_done", source=rep.source,
             total=rep.total, ok=rep.ok, fail=rep.fail, warn=rep.warn,
             precision=rep.precision)
    return rep


def validate_compare_report(
    markdown_path: Path | str,
    repo_a_root: Path | str,
    repo_b_root: Path | str,
) -> ValidationReport:
    """compare 报告含两边引用: 同一条引用先在 A 仓查, 再在 B 仓查, 任一命中即 OK.

    这是合理的近似 — compare.md 里的引用没有显式标"来自 A 还是 B",
    但模板要求引用必须取自 evidence, 所以一定能在 A 或 B 里找到.
    """
    md_path = Path(markdown_path)
    root_a = Path(repo_a_root)
    root_b = Path(repo_b_root)
    text = md_path.read_text(encoding="utf-8", errors="replace")
    citations = extract_citations(text)
    rep = ValidationReport(
        source=md_path.name,
        repo_root=f"A={root_a} B={root_b}",
    )
    for c in citations:
        chk_a = validate_citation(c, root_a)
        if chk_a.status == "ok":
            rep.checks.append(chk_a)
            rep.ok += 1
        else:
            chk_b = validate_citation(c, root_b)
            if chk_b.status == "ok":
                rep.checks.append(CitationCheck(
                    c, "ok",
                    reason="matched_in_b",
                    reason_code=Reason.OK.value,
                ))
                rep.ok += 1
            else:
                # 两边都失败: 取 A 的 reason_code, 但若两边码相同则归一化
                merged_code = chk_a.reason_code if chk_a.reason_code == chk_b.reason_code \
                    else Reason.COMPARE_BOTH_FAIL.value
                rep.checks.append(CitationCheck(
                    c, "fail",
                    reason=f"a_fail={chk_a.reason}; b_fail={chk_b.reason}",
                    reason_code=merged_code,
                ))
                rep.fail += 1
        rep.total += 1
    log.info("compare_citation_validation_done",
             source=rep.source, total=rep.total, ok=rep.ok, fail=rep.fail,
             precision=rep.precision)
    return rep


# --------------------------------------------------------------------------- #
# 批量
# --------------------------------------------------------------------------- #


def aggregate(reports: Iterable[ValidationReport]) -> ValidationReport:
    """合并多份 ValidationReport."""
    merged = ValidationReport(source="merged", repo_root="(multi)")
    for r in reports:
        merged.total += r.total
        merged.ok += r.ok
        merged.warn += r.warn
        merged.fail += r.fail
        merged.checks.extend(r.checks)
    return merged
