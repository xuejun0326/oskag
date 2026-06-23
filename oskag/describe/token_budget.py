"""describe/token_budget.py — 把 facts/repomap/源文件 切成 LLM 可消化的片段.

设计思想: prompt 注入的内容必须给 LLM 足够多上下文, 但又不能塞爆 64K.
这里提供 4 个纯函数, pipeline 用它们组装 prompt:

1. split_repomap_by_module(repomap_text) → dict[domain → text]
   把 repomap 按 `## domain` 头切节段.

2. syscalls_for_module(syscalls_dict, module) → str
   过滤 domain == module 的 item, 序列化成 "name(id)\n..." 文本, 限 1500 字.

3. file_excerpts_for(slice_, *, head_lines, max_total_chars)
   读 ModuleSlice.files 的前 N 行, 按总字符预算累加, 按文件大小降序.

4. boot_excerpt_text(boot_dict) → str
   把 boot facts 序列化成短 JSON, 仅供 boot 模块 prompt 用.

约束:
- 0 LLM 调用
- 不抛异常 (read_file 失败 → 跳过)
- 输出长度严格上限 (硬 cap 字符数, 不是 token; 1 字符 ≈ 0.5 token 中文)
"""
from __future__ import annotations

import json
from pathlib import Path

from oskag.describe._modules import ModuleSlice
from oskag.logging_setup import get_logger
from oskag.tools.fs import read_file

log = get_logger("oskag.describe.budget")


# 默认预算 (改进: 走 pro 模型 1M 上下文, 大幅放开)
DEFAULT_REPOMAP_CHARS = 8000        # 3000 → 8000
DEFAULT_SYSCALLS_CHARS = 4000       # 1500 → 4000
DEFAULT_BOOT_CHARS = 4000           # 2000 → 4000
DEFAULT_FILE_HEAD_LINES = 600       # 200 → 600
DEFAULT_FILES_TOTAL_CHARS = 60000   # 12000 → 60000

_TRUNCATED_MARK = "\n... (truncated)"


# --------------------------------------------------------------------------- #
# repomap 切片
# --------------------------------------------------------------------------- #


def split_repomap_by_module(repomap_text: str) -> dict[str, str]:
    """按 `## domain` 头切 repomap. domain 名转小写."""
    if not repomap_text:
        return {}
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in repomap_text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip().lower()
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    result = {k: "\n".join(lines).strip() for k, lines in sections.items()}
    return {k: v for k, v in result.items() if v}


def repomap_excerpt_for(repomap_sections: dict[str, str], module: str,
                        *, max_chars: int = DEFAULT_REPOMAP_CHARS) -> str:
    """取本模块的 repomap 节段, 截到 max_chars."""
    text = repomap_sections.get(module, "")
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + _TRUNCATED_MARK


# --------------------------------------------------------------------------- #
# syscalls 过滤
# --------------------------------------------------------------------------- #


def syscalls_for_module(syscalls_items: list[dict], module: str,
                        *, max_chars: int = DEFAULT_SYSCALLS_CHARS,
                        max_items: int = 60) -> str:
    """过滤出 domain == module 的 syscall, 序列化为 "name(id) → file:line" 文本."""
    if not syscalls_items:
        return ""
    matched = [it for it in syscalls_items if it.get("domain") == module]
    if not matched:
        return ""

    lines: list[str] = []
    for it in matched[:max_items]:
        name = it.get("name", "?")
        sid = it.get("id")
        sid_text = f"({sid})" if sid is not None else ""
        loc = ""
        f = it.get("file")
        ln = it.get("line")
        if f:
            loc = f" → {f}:{ln}" if ln else f" → {f}"
        lines.append(f"{name}{sid_text}{loc}")

    text = "\n".join(lines)
    if len(matched) > max_items:
        text += f"\n... 还有 {len(matched) - max_items} 项"
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + _TRUNCATED_MARK


# --------------------------------------------------------------------------- #
# boot 摘要
# --------------------------------------------------------------------------- #


def boot_excerpt_text(boot_dict: dict, *, max_chars: int = DEFAULT_BOOT_CHARS) -> str:
    """把 boot facts 关键字段序列化."""
    if not boot_dict:
        return ""
    minimal = {
        "entry_file": boot_dict.get("entry_file"),
        "entry_fn": boot_dict.get("entry_fn"),
        "entry_line": boot_dict.get("entry_line"),
        "boot_style": boot_dict.get("boot_style"),
        "call_chain": boot_dict.get("call_chain", [])[:12],
        "handlers": boot_dict.get("handlers", []),
        "assembly_files": boot_dict.get("assembly_files", [])[:8],
    }
    text = json.dumps(minimal, ensure_ascii=False, indent=2)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + _TRUNCATED_MARK


# --------------------------------------------------------------------------- #
# 文件片段
# --------------------------------------------------------------------------- #


def file_excerpts_for(
    slice_: ModuleSlice,
    repo_root: Path,
    *,
    head_lines: int = DEFAULT_FILE_HEAD_LINES,
    max_total_chars: int = DEFAULT_FILES_TOTAL_CHARS,
) -> list[dict[str, str]]:
    """读 slice 内每个文件的前 head_lines 行, 累计字符不超过 max_total_chars.

    返回 list of {file, lines, content}, file 是相对仓库根的 posix 路径.
    永不抛 (read_file 失败的文件跳过).
    """
    out: list[dict[str, str]] = []
    used = 0
    repo_root = Path(repo_root).resolve()

    for fp in slice_.files:
        if used >= max_total_chars:
            break
        fr = read_file(fp)
        if fr.error or not fr.text:
            continue

        lines = fr.text.splitlines()
        if len(lines) > head_lines:
            content = "\n".join(lines[:head_lines]) + f"\n// ... (file has {len(lines)} lines, showing first {head_lines})"
            line_range = f"1-{head_lines}"
        else:
            content = fr.text
            line_range = f"1-{len(lines)}"

        # 单文件预算: max_total_chars 减去已用
        budget = max_total_chars - used
        if len(content) > budget:
            content = content[:budget] + "\n// ... (truncated by budget)"

        try:
            rel = fp.resolve().relative_to(repo_root).as_posix()
        except ValueError:
            rel = str(fp)

        out.append({"file": rel, "lines": line_range, "content": content})
        used += len(content)

    log.info(
        "file_excerpts_built",
        module=slice_.name,
        files=len(out),
        chars=used,
        budget=max_total_chars,
    )
    return out


__all__ = [
    "DEFAULT_REPOMAP_CHARS",
    "DEFAULT_SYSCALLS_CHARS",
    "DEFAULT_BOOT_CHARS",
    "DEFAULT_FILE_HEAD_LINES",
    "DEFAULT_FILES_TOTAL_CHARS",
    "split_repomap_by_module",
    "repomap_excerpt_for",
    "syscalls_for_module",
    "boot_excerpt_text",
    "file_excerpts_for",
]
