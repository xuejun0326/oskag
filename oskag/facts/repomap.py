"""facts/repomap.py — 紧凑仓库地图.

输出一段 ≤ 8K token (~32KB) 的文本, 列出每个文件的:
- pub fn / pub async fn 签名 (取 fn 行首)
- pub struct / pub enum / pub trait 名
- impl Block 签名

策略:
- 优先用 tools.ts (tree-sitter) 的 find_functions 拿到精确范围
- ts 不可用时降级到 rg 正则 (pub fn / pub struct / impl / pub trait / pub enum)
- 按 profile.module_paths 给 syscall/task/mm/fs/signal/ipc/boot 分组排序
- 截断: 每文件最多 N 行符号; 总长度软上限 32_000 字符

不抛异常, 失败写 warnings.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from oskag.facts.profiles import KernelProfile
from oskag.logging_setup import get_logger
from oskag.tools.fs import grep, is_rg_available, list_files
from oskag.tools.ts import find_functions, is_ts_available

log = get_logger("oskag.facts.repomap")

DEFAULT_TOTAL_BUDGET_CHARS = 32_000        # ~8K tokens (4 chars/token 经验值)
DEFAULT_PER_FILE_LIMIT = 30                # 每文件最多 N 行符号
DEFAULT_MAX_FILES = 200                    # 全仓最多扫多少文件 (硬上限, 防爆)


@dataclass
class RepoMap:
    text: str = ""
    file_count: int = 0
    symbol_count: int = 0
    char_count: int = 0
    backend: str = "rg"                    # "tree_sitter" / "rg"
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "file_count": self.file_count,
            "symbol_count": self.symbol_count,
            "char_count": self.char_count,
            "backend": self.backend,
            "truncated": self.truncated,
            "warnings": self.warnings,
        }


# --------------------------------------------------------------------------- #
# rg fallback (永远可用作 baseline)
# --------------------------------------------------------------------------- #


# Rust: pub? + 修饰? + fn name (顶层定义)
_FN_RE = re.compile(r"^\s*(?:pub\S*\s)?[\w\s]*\bfn\s+\w+")
# Rust: pub? + (struct|enum|trait|union|type) name
_TYPE_RE = re.compile(r"^\s*(?:pub\S*\s)?(?:struct|enum|trait|union|type)\s+\w+")
_IMPL_RE = re.compile(r"^\s*impl\b")

# C/C++: 函数声明/定义（返回类型 + 函数名 + 参数列表）
_CPP_FN_RE = re.compile(
    r"^\s*(?:static\s+|virtual\s+|inline\s+|extern\s+)*"
    r"[\w:<>*&\s]+\s+[\w:]+\s*\([^)]*\)\s*(?:const\s+)?(?:override\s+)?[{;]"
)
# C/C++: class / struct
_CPP_CLASS_RE = re.compile(r"^\s*(?:class|struct)\s+\w+")
# C/C++: namespace
_CPP_NS_RE = re.compile(r"^\s*namespace\s+\w+")

# Python: def / class
_PY_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+\w+\s*\(")
_PY_CLASS_RE = re.compile(r"^\s*class\s+\w+")

# Go: func
_GO_FN_RE = re.compile(r"^\s*func\s+(?:\(\w+\s+\*?\w+\)\s+)?\w+\s*\(")

# Java: method / class
_JAVA_FN_RE = re.compile(r"^\s*(?:public|private|protected)?\s*(?:static\s+)?[\w<>\[\]]+\s+\w+\s*\(")
_JAVA_CLASS_RE = re.compile(r"^\s*(?:public\s+)?(?:class|interface|enum)\s+\w+")


def _extract_symbols_from_text(text: str, max_lines: int) -> list[str]:
    """从单文件源码抽出符号行 (按出现顺序, 截断到 max_lines).

    支持 Rust / C / C++ / Python / Go / Java.
    """
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        # 跳过注释行（多语言通用：// 或 # 或 /*）
        stripped = line.lstrip()
        if stripped.startswith("//") or stripped.startswith("#") or stripped.startswith("/*"):
            continue

        # 尝试匹配各语言的符号模式
        matched = (
            _FN_RE.match(line) or _TYPE_RE.match(line) or _IMPL_RE.match(line) or
            _CPP_FN_RE.match(line) or _CPP_CLASS_RE.match(line) or _CPP_NS_RE.match(line) or
            _PY_DEF_RE.match(line) or _PY_CLASS_RE.match(line) or
            _GO_FN_RE.match(line) or
            _JAVA_FN_RE.match(line) or _JAVA_CLASS_RE.match(line)
        )

        if matched:
            # 简化签名: 去尾部 { 与多余空格
            sig = line.split("{", 1)[0].rstrip()
            if sig.endswith(","):
                continue
            out.append(sig.strip())
            if len(out) >= max_lines:
                out.append("    ... (truncated)")
                break
    return out


def _file_excludes(profile: KernelProfile | None) -> list[str]:
    """rg --files 的排除 glob. 根据 profile.family 决定源码后缀."""
    # 根据 family 决定源码后缀
    if profile and profile.family == "unknown":
        # 自由模式: 扫描常见系统编程语言
        base = ["*.rs", "*.c", "*.cpp", "*.cc", "*.cxx", "*.h", "*.hpp", "*.hh", "*.py", "*.go", "*.java"]
    else:
        # 内核模式: 只扫 Rust
        base = ["*.rs"]

    base.extend(["!arceos/**", "!apps/**", "!vendor/**", "!.arceos/**",
                 "!user/**", "!target/**", "!.git/**", "!patch/**",
                 "!**/tests/**", "!**/*.test.rs", "!libs/**", "!third_party/**",
                 "!node_modules/**", "!build/**", "!dist/**"])
    if profile is not None:
        for ig in profile.ignore_paths:
            base.append(f"!{ig}/**")
    seen: set[str] = set()
    out: list[str] = []
    for g in base:
        if g not in seen:
            seen.add(g)
            out.append(g)
    return out



# --------------------------------------------------------------------------- #
# 文件优先级 (按 profile.module_paths 分组)
# --------------------------------------------------------------------------- #


_PRIORITY_ORDER: tuple[str, ...] = (
    "syscall", "task", "mm", "fs", "signal", "ipc", "net", "drivers", "boot",
)


def _file_group(rel_path: str, profile: KernelProfile | None) -> tuple[int, str]:
    """根据 profile.module_paths 给文件打分组 (pri_index, group_name).

    pri_index 越小越优先. 不命中任何 module_paths 归 'other'.
    """
    if profile is None or not profile.module_paths:
        return (len(_PRIORITY_ORDER), "other")
    for idx, group in enumerate(_PRIORITY_ORDER):
        prefixes = profile.module_paths.get(group, [])
        for pfx in prefixes:
            pfx_norm = pfx.replace("\\", "/").rstrip("/")
            if rel_path.startswith(pfx_norm + "/") or rel_path == pfx_norm:
                return (idx, group)
    return (len(_PRIORITY_ORDER), "other")


# --------------------------------------------------------------------------- #
# 单文件符号抽取 (优先 ts, fallback rg)
# --------------------------------------------------------------------------- #


def _ts_function_signatures(funcs: list[dict], lines: list[str], max_lines: int) -> list[str]:
    out: list[str] = []
    for f in funcs:
        ln = f.get("start_line", 0)
        if 1 <= ln <= len(lines):
            out.append(lines[ln - 1].split("{", 1)[0].strip())
            if len(out) >= max_lines:
                out.append("    ... (truncated)")
                break
    return out


def _ts_extra_types(lines: list[str], have: int, max_lines: int) -> list[str]:
    extras: list[str] = []
    for line in lines:
        s = line.rstrip()
        if _TYPE_RE.match(s) or _IMPL_RE.match(s):
            extras.append(s.split("{", 1)[0].strip())
            if len(extras) + have >= max_lines:
                break
    return extras


def _symbols_via_ts(path: Path, max_lines: int) -> list[str] | None:
    """tree-sitter 路径: 抽 fn 函数签名 + struct/trait/impl. 失败返回 None."""
    if not is_ts_available():
        return None
    try:
        funcs = find_functions(path)
    except Exception:
        return None
    if not funcs:
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    fn_sigs = _ts_function_signatures(funcs, lines, max_lines)
    extras = _ts_extra_types(lines, len(fn_sigs), max_lines)
    return fn_sigs + extras


def _symbols_via_rg(path: Path, max_lines: int) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return _extract_symbols_from_text(text, max_lines)


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #


def _build_file_priority_list(
    files: list[Path], repo: Path, profile: KernelProfile | None,
) -> list[tuple[tuple[int, str], str, Path]]:
    """收集 (priority, rel_path, abs_path) 三元组并按优先级排序."""
    rels: list[tuple[tuple[int, str], str, Path]] = []
    for fp in files:
        try:
            rel = fp.resolve().relative_to(repo).as_posix()
        except ValueError:
            continue
        rels.append((_file_group(rel, profile), rel, fp))
    rels.sort(key=lambda x: (x[0][0], x[1]))
    return rels


def _extract_file_symbols(fp: Path, backend: str, per_file_limit: int) -> list[str]:
    if backend == "tree_sitter":
        symbols = _symbols_via_ts(fp, per_file_limit)
        if symbols:
            return symbols
    return _symbols_via_rg(fp, per_file_limit)


def scan_repomap(
    repo_path: Path | str,
    profile: KernelProfile | None = None,
    *,
    total_budget: int = DEFAULT_TOTAL_BUDGET_CHARS,
    per_file_limit: int = DEFAULT_PER_FILE_LIMIT,
    max_files: int = DEFAULT_MAX_FILES,
) -> RepoMap:
    """对 repo 跑紧凑 RepoMap. 永不抛异常.

    输出文本结构:
        ## syscall
        path/to/x.rs
            pub fn foo(...)
            pub struct Bar
        ## task
        ...
    """
    repo = Path(repo_path).resolve()
    rm = RepoMap()

    if not repo.is_dir():
        rm.warnings.append(f"not a directory: {repo}")
        return rm
    if not is_rg_available():
        rm.warnings.append("ripgrep not available; cannot enumerate files")
        return rm

    rm.backend = "tree_sitter" if is_ts_available() else "rg"
    files = list_files(repo, glob=_file_excludes(profile))
    if not files:
        rm.warnings.append("no rust files found after filtering")
        return rm

    rels = _build_file_priority_list(files, repo, profile)
    if len(rels) > max_files:
        rels = rels[:max_files]
        rm.truncated = True
        rm.warnings.append(f"file_list truncated to {max_files}")

    out_chunks: list[str] = []
    last_group: str | None = None
    char_used = 0
    sym_total = 0

    for (_prio, group), rel, fp in rels:
        if group != last_group:
            header = f"\n## {group}\n"
            out_chunks.append(header)
            char_used += len(header)
            last_group = group

        symbols = _extract_file_symbols(fp, rm.backend, per_file_limit)
        if not symbols:
            continue

        block = rel + "\n" + "\n".join(f"    {s}" for s in symbols) + "\n"
        if char_used + len(block) > total_budget:
            out_chunks.append(f"\n... (RepoMap budget {total_budget} chars exceeded)\n")
            rm.truncated = True
            break
        out_chunks.append(block)
        char_used += len(block)
        sym_total += len(symbols)
        rm.file_count += 1

    rm.text = "".join(out_chunks).strip() + "\n"
    rm.symbol_count = sym_total
    rm.char_count = len(rm.text)

    log.info(
        "repomap_done",
        repo=repo.name,
        backend=rm.backend,
        files=rm.file_count,
        symbols=rm.symbol_count,
        chars=rm.char_count,
        truncated=rm.truncated,
    )
    return rm


__all__ = [
    "RepoMap",
    "scan_repomap",
    "DEFAULT_TOTAL_BUDGET_CHARS",
    "DEFAULT_PER_FILE_LIMIT",
    "DEFAULT_MAX_FILES",
]
