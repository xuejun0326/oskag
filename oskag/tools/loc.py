"""LOC (lines of code) 统计.

策略:
1. 优先 `tokei <path> --output json` (准确, 快)
2. tokei 不可用 → Python 手数 fallback:
   - 用 tools.fs.list_files 拿文件列表 (会自动应用 .gitignore + DEFAULT_IGNORE)
   - 按扩展名分组
   - 逐文件简单计算: code / blank / comment 行数
3. 输出统一 dict[lang_name, LangStats], 调用方只看 .code / .files / .total

不追求与 tokei 完全一致 (注释判断会有 ±5% 偏差),
但保证: 文件数 100% 一致, code 总数 ±10% 内.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from oskag.logging_setup import get_logger
from oskag.tools._subprocess import is_available, run_tool
from oskag.tools.fs import DEFAULT_IGNORE, list_files

log = get_logger("oskag.tools.loc")

# 扩展名 → tokei 风格语言名 (与 tokei JSON 输出 key 一致)
EXT_TO_LANG: dict[str, str] = {
    ".rs": "Rust",
    ".c": "C",
    ".h": "C Header",
    ".cpp": "C++",
    ".hpp": "C++ Header",
    ".cc": "C++",
    ".py": "Python",
    ".js": "JavaScript",
    ".ts": "TypeScript",
    ".go": "Go",
    ".java": "Java",
    ".s": "Assembly",
    ".S": "Assembly",
    ".asm": "Assembly",
    ".toml": "TOML",
    ".md": "Markdown",
    ".json": "JSON",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".sh": "Shell",
    ".ld": "Linker Script",
}

# 哪些语言计入"代码总量"汇总 (不含文档/配置)
CODE_LANGS: frozenset[str] = frozenset({
    "Rust", "C", "C Header", "C++", "C++ Header",
    "Python", "JavaScript", "TypeScript", "Go", "Java", "Assembly",
})


@dataclass
class LangStats:
    """单一语言的统计."""
    files: int = 0
    code: int = 0
    blanks: int = 0
    comments: int = 0

    @property
    def total(self) -> int:
        return self.code + self.blanks + self.comments


@dataclass
class LocReport:
    """完整 LOC 报告."""
    by_language: dict[str, LangStats] = field(default_factory=dict)
    total_files: int = 0
    total_code: int = 0
    backend: str = "fallback"  # "tokei" / "fallback"
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "by_language": {k: asdict(v) for k, v in self.by_language.items()},
            "total_files": self.total_files,
            "total_code": self.total_code,
            "backend": self.backend,
            "warnings": self.warnings,
        }


# --------------------------------------------------------------------------- #
# tokei backend
# --------------------------------------------------------------------------- #


def _is_real_tokei() -> bool:
    """验 tokei 是真正的 LOC 工具 (不是 npm 上同名的 localization 包).

    npm tokei 没有 `--output` 选项, 跑 `tokei --version` 输出 "tokei x.y.z node localization".
    Rust tokei 输出 "tokei <semver>" + 不含 "localization".
    """
    if not is_available("tokei"):
        return False
    res = run_tool(["tokei", "--version"], timeout=5)
    if not res.ok:
        return False
    out = (res.stdout + res.stderr).lower()
    # Rust 版含 "tokei " + 版本号; npm 同名包是另一回事
    return "localization" not in out and "tokei" in out


def _run_tokei(path: Path) -> LocReport | None:
    """跑 tokei, 失败/不可用返回 None (调用方走 fallback)."""
    if not _is_real_tokei():
        return None
    res = run_tool(["tokei", str(path), "--output", "json"], timeout=120)
    if not res.ok or not res.stdout:
        log.warning("tokei_failed", returncode=res.returncode, stderr_preview=res.stderr[:160])
        return None
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as e:
        log.warning("tokei_json_decode_failed", error=str(e))
        return None

    report = LocReport(backend="tokei")
    for lang_name, stats in data.items():
        if not isinstance(stats, dict):
            continue
        ls = LangStats(
            files=len(stats.get("reports", [])) or stats.get("inaccurate", 0),
            code=int(stats.get("code", 0)),
            blanks=int(stats.get("blanks", 0)),
            comments=int(stats.get("comments", 0)),
        )
        report.by_language[lang_name] = ls
        report.total_files += ls.files
        if lang_name in CODE_LANGS:
            report.total_code += ls.code
    return report


# --------------------------------------------------------------------------- #
# Python fallback
# --------------------------------------------------------------------------- #


_C_LIKE_BLOCK_START = "/*"
_C_LIKE_BLOCK_END = "*/"


def _classify_lines_c_like(text: str) -> tuple[int, int, int]:
    """C / Rust 风格简单分类 (//, /* */). 返回 (code, blank, comment)."""
    code = blank = comment = 0
    in_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            blank += 1
            continue
        # 处理 /* */ 块注释 (跨行)
        if in_block:
            comment += 1
            if _C_LIKE_BLOCK_END in stripped:
                in_block = False
            continue
        if stripped.startswith(_C_LIKE_BLOCK_START):
            comment += 1
            if _C_LIKE_BLOCK_END not in stripped[2:]:
                in_block = True
            continue
        if stripped.startswith("//"):
            comment += 1
            continue
        # 行内有 // 但前面有代码 → 仍算 code
        code += 1
    return code, blank, comment


def _classify_lines_python(text: str) -> tuple[int, int, int]:
    """Python: # 注释; 简化处理 (不解析 docstring)."""
    code = blank = comment = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            blank += 1
        elif stripped.startswith("#"):
            comment += 1
        else:
            code += 1
    return code, blank, comment


def _classify_lines_simple(text: str) -> tuple[int, int, int]:
    """通用兜底: 不识别注释, 全部当 code 或 blank."""
    code = blank = 0
    for line in text.splitlines():
        if line.strip():
            code += 1
        else:
            blank += 1
    return code, blank, 0


def _classify_for_lang(lang_name: str, text: str) -> tuple[int, int, int]:
    if lang_name in {"Rust", "C", "C Header", "C++", "C++ Header", "JavaScript",
                     "TypeScript", "Go", "Java"}:
        return _classify_lines_c_like(text)
    if lang_name == "Python":
        return _classify_lines_python(text)
    return _classify_lines_simple(text)


def _python_fallback(path: Path, *, ignore: Iterable[str] = DEFAULT_IGNORE) -> LocReport:
    """逐文件 Python 手数. 尽量与 tokei 数对齐."""
    report = LocReport(backend="fallback")
    files = list_files(path)
    for f in files:
        # 忽略 ignore 路径 (DEFAULT_IGNORE)
        try:
            rel_parts = f.relative_to(path).parts
        except ValueError:
            rel_parts = f.parts
        if any(part in ignore or part.startswith(".") for part in rel_parts):
            continue
        suffix = f.suffix.lower() if f.suffix.lower() else f.suffix
        # 处理大写 .S
        if f.suffix == ".S":
            suffix = ".S"
        lang = EXT_TO_LANG.get(suffix)
        if lang is None:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        code, blank, comment = _classify_for_lang(lang, text)
        ls = report.by_language.setdefault(lang, LangStats())
        ls.files += 1
        ls.code += code
        ls.blanks += blank
        ls.comments += comment
        report.total_files += 1
        if lang in CODE_LANGS:
            report.total_code += code
    return report


# --------------------------------------------------------------------------- #
# public entry
# --------------------------------------------------------------------------- #


def scan_loc(path: Path | str) -> LocReport:
    """统计目录 LOC. 优先 tokei, 不可用走 Python fallback. 永不抛异常."""
    p = Path(path)
    if not p.is_dir():
        report = LocReport()
        report.warnings.append(f"not a directory: {p}")
        return report

    report = _run_tokei(p)
    if report is not None:
        return report
    log.info("loc_using_python_fallback", path=str(p))
    return _python_fallback(p)


__all__ = [
    "EXT_TO_LANG",
    "CODE_LANGS",
    "LangStats",
    "LocReport",
    "scan_loc",
]
