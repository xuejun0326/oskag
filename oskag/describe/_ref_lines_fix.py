"""refs[].lines grep 二次定位.

LLM 凭记忆给的 file:line 行号普遍偏 1-12 行 (实测: Undefined-OS mm 模块 20 条 refs
中 8 条偏移). 这个模块用确定性 grep 修复:

1. 取 `ref.snippet` 第一行非空 + 第二行非空作"双行锚"
2. 在 `repo_root / ref.file` 里搜双行锚的真实位置
3. 找到 → 覆盖 `ref.lines` 为 `f"{start}-{start + N - 1}"` (N = snippet 总行数)
4. 找不到 → 保留原值 (LLM 给的可能是真错或文件已变, 留 verifier 处理)

只用 stdlib 的字符串操作, 不调外部命令; 避免跨平台 grep 差异.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)


def _normalize_for_match(line: str) -> str:
    """规范化一行供匹配: rstrip + 去 lstrip 但保留是否空."""
    return line.rstrip("\r\n").rstrip()


def _find_snippet_line(file_text: str, snippet: str) -> int | None:
    """在 file_text 中找 snippet 的起始行号 (1-based). 找不到返回 None.

    策略 (按可靠性递减):
    1. 用 snippet 前两行非空作锚, 在文件里找连续匹配的位置 (容忍 lstrip 差异)
    2. 如果 snippet 只有一行, 退化为单行匹配 (但要去除 lstrip 后整行严格相等, 避免歧义)
    """
    file_lines = file_text.splitlines()
    snippet_lines = [_normalize_for_match(l) for l in snippet.splitlines()]
    snippet_lines = [l for l in snippet_lines if l.strip()]
    if not snippet_lines:
        return None

    if len(snippet_lines) >= 2:
        anchor1 = snippet_lines[0].lstrip()
        anchor2 = snippet_lines[1].lstrip()
        if not anchor1 or not anchor2:
            # 双锚有空行, 退化为单锚
            return _find_single_anchor(file_lines, snippet_lines[0])

        # 双锚: 找 anchor1 出现的所有位置, 挑下一行 lstrip 后等于 anchor2 的
        candidates: list[int] = []
        for i, line in enumerate(file_lines):
            stripped = line.lstrip().rstrip()
            if stripped == anchor1 and i + 1 < len(file_lines):
                next_stripped = file_lines[i + 1].lstrip().rstrip()
                if next_stripped == anchor2:
                    candidates.append(i + 1)  # 1-based

        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            # 多候选: LLM 给的旧 lines 离哪个最近就用哪个 (这里调用方会处理, 我们返回第一个)
            return candidates[0]
        # 双锚无果, 退化为单锚
        return _find_single_anchor(file_lines, snippet_lines[0])

    # 只有一行
    return _find_single_anchor(file_lines, snippet_lines[0])


def _find_single_anchor(file_lines: list[str], anchor: str) -> int | None:
    """单行锚: 严格 lstrip+rstrip 后等于 anchor 的唯一位置."""
    target = anchor.lstrip().rstrip()
    if not target or len(target) < 8:
        # 太短的 anchor (例: `}`) 容易歧义, 不冒险匹配
        return None
    hits: list[int] = []
    for i, line in enumerate(file_lines):
        if line.lstrip().rstrip() == target:
            hits.append(i + 1)
    if len(hits) == 1:
        return hits[0]
    return None


def _parse_lines_field(lines: Any) -> tuple[int, int] | None:
    """解析 LLM 给的 lines 字段 (例: '15-18' / '15' / 15) → (start, end)."""
    if isinstance(lines, int) and lines > 0:
        return (lines, lines)
    if not isinstance(lines, str):
        return None
    s = lines.strip()
    if not s:
        return None
    if "-" in s:
        parts = s.split("-", 1)
        try:
            start = int(parts[0].strip())
            end = int(parts[1].strip()) if parts[1].strip() else start
            if start > 0 and end >= start:
                return (start, end)
        except ValueError:
            return None
    else:
        try:
            n = int(s)
            return (n, n) if n > 0 else None
        except ValueError:
            return None
    return None


def fix_ref_lines(
    refs: list[dict],
    repo_root: Path,
    *,
    module_name: str = "",
) -> dict[str, int]:
    """就地修复 refs[].lines 为 grep 真实行号.

    Args:
        refs: LLM 输出的 refs 列表, 每条形如 {id, file, lines, snippet, why}
        repo_root: 仓库根目录
        module_name: 模块名 (日志用)

    Returns:
        统计 dict: {total, fixed, kept, file_missing, snippet_empty, no_match}
    """
    counts = {
        "total": len(refs),
        "fixed": 0,         # grep 找到, 与原 lines 不同 → 已覆盖
        "kept_correct": 0,  # grep 找到, 与原 lines 一致 → 保留
        "file_missing": 0,  # 文件不存在
        "snippet_empty": 0, # snippet 字段空
        "no_match": 0,      # 文件存在但 grep 没找到 (LLM 可能给错文件或代码已变)
    }
    if not refs:
        return counts

    # 文件内容缓存 (同模块多个 ref 引同一文件常见)
    file_cache: dict[str, str | None] = {}

    for ref in refs:
        if not isinstance(ref, dict):
            continue
        file_rel = (ref.get("file") or "").strip()
        snippet = ref.get("snippet") or ""

        if not file_rel:
            counts["snippet_empty"] += 1
            continue
        if not snippet.strip():
            counts["snippet_empty"] += 1
            continue

        # 读文件 (带缓存)
        if file_rel not in file_cache:
            try:
                target = (repo_root / file_rel).resolve()
                target.relative_to(repo_root.resolve())  # 防越权
                if target.is_file():
                    file_cache[file_rel] = target.read_text(
                        encoding="utf-8", errors="replace"
                    )
                else:
                    file_cache[file_rel] = None
            except (OSError, ValueError):
                file_cache[file_rel] = None

        text = file_cache[file_rel]
        if text is None:
            counts["file_missing"] += 1
            continue

        # grep snippet → 真实起始行
        start = _find_snippet_line(text, snippet)
        if start is None:
            counts["no_match"] += 1
            continue

        # 计算 end (保持 snippet 总行数)
        snippet_line_count = len([l for l in snippet.splitlines() if l.strip()])
        # 用 snippet 行数兜底, 但与原 lines 给的 end-start+1 比较取大者更稳
        end = start + max(snippet_line_count, 1) - 1

        # 比对原 lines, 决定是否真的要覆盖
        old = _parse_lines_field(ref.get("lines"))
        new_lines = f"{start}-{end}" if end > start else str(start)

        if old and old[0] == start:
            # 原起始行就对, 不动 (保留原 end 也行, 但用新 end 更精)
            ref["lines"] = new_lines
            counts["kept_correct"] += 1
        else:
            ref["lines"] = new_lines
            counts["fixed"] += 1

    log.info(
        "ref_lines_fixed",
        module=module_name,
        **counts,
    )
    return counts


__all__ = ["fix_ref_lines"]
