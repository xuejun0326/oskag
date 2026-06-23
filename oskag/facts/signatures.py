"""facts/signatures.py — 函数签名提取与对比 .

把一个 Rust 仓库的所有 `pub fn` (含 `pub(crate)`, 不含私有 `fn`) 签名
抽出来, 标准化成"指纹字符串", 再算两仓签名集合的 Jaccard.

标准化策略 (重要 — 决定 Jaccard 是否准):
1. 只保留 `pub` / `pub(crate)` / `pub(super)` 等可见修饰的函数 (内部私有实现不算)
2. 提取: name + 参数类型列表 + 返回类型, 丢掉:
   - 生命周期 ('a, 'static)
   - where 子句
   - 默认参数 / 默认值
   - self 参数 (& mut self / self 都规整成 SELF)
   - 注释 / doc string
3. 多余空白合并成单个空格, 然后所有空白删掉, 得到稳定指纹

输出例:
  `sys_open(*const u8,i32,i32) -> isize`

外部 API:
- collect_signatures(repo_path) -> set[str]
- normalize_signature(raw) -> str
- compare_signatures(set_a, set_b) -> SetDiff (复用 _diff.py)
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from oskag.logging_setup import get_logger
from oskag.pipelines._diff import SetDiff, diff_sets
from oskag.tools import ts

log = get_logger("oskag.facts.signatures")


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

# pub 前缀正则 (匹配 pub / pub(crate) / pub(super) / pub(in path::to::mod))
_PUB_PREFIX_RE = re.compile(r"^\s*pub(\([^)]*\))?\s+")

# 完整签名头部正则: `[pub] [unsafe] [async] [const] fn NAME[generics]( PARAMS ) [-> RET]`
_FN_HEADER_RE = re.compile(
    r"""
    ^\s*
    (?P<pub>pub(\([^)]*\))?\s+)?
    (?:(?:unsafe|async|const|extern(\s+"[^"]+")?)\s+)*
    fn\s+
    (?P<name>[A-Za-z_][A-Za-z0-9_]*)
    (?P<generics><[^>]*>)?
    \s*\(
    (?P<params>.*?)
    \)
    \s*
    (?:->\s*(?P<ret>[^{]*?))?
    \s*
    (?:where\b[^{]*)?
    \s*$
    """,
    re.VERBOSE | re.DOTALL,
)

# 生命周期参数 ('a, 'static): 出现在类型里时全部抹掉
_LIFETIME_RE = re.compile(r"'[a-zA-Z_][a-zA-Z0-9_]*\b\s*[,+]?\s*")

# 多余空白
_WHITESPACE_RE = re.compile(r"\s+")


# --------------------------------------------------------------------------- #
# 标准化
# --------------------------------------------------------------------------- #


def normalize_signature(raw: str) -> str | None:
    """把一段原始函数签名 (`pub fn foo<'a>(x: &'a str) -> Result<u32, Error>`)
    规整成一个稳定指纹, 例如 `foo(&str) -> Result<u32, Error>`.

    返回 None 表示:
    - 不是 pub fn (内部实现, 不进对比集合)
    - 解析失败 (语法太复杂, 跳过)
    """
    if not raw or "fn " not in raw:
        return None
    if not _PUB_PREFIX_RE.match(raw):
        return None

    text = _strip_lifetime_and_collapse(raw)
    m = _FN_HEADER_RE.match(text)
    if m is None:
        return None

    name = m.group("name")
    params = _normalize_params(m.group("params") or "")
    ret = _normalize_type(m.group("ret") or "()")

    return f"{name}({params}) -> {ret}"


def _strip_lifetime_and_collapse(s: str) -> str:
    """去生命周期 + 折叠空白."""
    s = _LIFETIME_RE.sub("", s)
    s = _WHITESPACE_RE.sub(" ", s)
    return s.strip()


def _normalize_params(params: str) -> str:
    """对参数列表做标准化, 返回 'TYPE1,TYPE2,...' 形式."""
    if not params.strip():
        return ""
    parts = _split_top_level_commas(params)
    out: list[str] = []
    for p in parts:
        norm = _normalize_one_param(p)
        if norm:
            out.append(norm)
    return ",".join(out)


def _normalize_one_param(p: str) -> str:
    """单个参数: 把 `name: type = default` 规整为 `type`. self/&self/&mut self → SELF."""
    p = p.strip()
    if not p:
        return ""
    # self 系列
    if p in ("self", "&self", "&mut self") or p.endswith(" self") or p.endswith("&self"):
        return "SELF"
    # `mut foo: Type` / `foo: Type` / `_: Type` — 取冒号后的类型
    if ":" in p:
        type_part = p.split(":", 1)[1].strip()
        # 去掉 `= default` 部分
        if "=" in type_part:
            type_part = type_part.split("=", 1)[0].strip()
        return _normalize_type(type_part)
    # 没冒号 (例如 macro 展开后的奇怪形式), 整体当类型
    return _normalize_type(p)


def _normalize_type(t: str) -> str:
    """类型标准化: 移除 'a 等, 折叠空白, 去尾随逗号/分号."""
    t = _strip_lifetime_and_collapse(t)
    t = t.rstrip(",;").strip()
    # 折叠 `& mut Foo` → `&mut Foo`, `& Foo` → `&Foo`
    t = re.sub(r"&\s+mut\s+", "&mut ", t)
    t = re.sub(r"&\s+", "&", t)
    # 类型内部空格也压缩
    t = re.sub(r"\s*,\s*", ",", t)
    t = re.sub(r"\s*<\s*", "<", t)
    t = re.sub(r"\s*>\s*", ">", t)
    return t.replace(" ", "")


def _split_top_level_commas(s: str) -> list[str]:
    """按顶层逗号分割, 不切到嵌套的 < > /  / [ ] 内部."""
    out: list[str] = []
    buf: list[str] = []
    depth_lt = 0
    depth_paren = 0
    depth_brack = 0
    for ch in s:
        if ch == "<":
            depth_lt += 1
        elif ch == ">":
            depth_lt = max(0, depth_lt - 1)
        elif ch == "(":
            depth_paren += 1
        elif ch == ")":
            depth_paren = max(0, depth_paren - 1)
        elif ch == "[":
            depth_brack += 1
        elif ch == "]":
            depth_brack = max(0, depth_brack - 1)
        if ch == "," and depth_lt == 0 and depth_paren == 0 and depth_brack == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


# --------------------------------------------------------------------------- #
# 仓库扫描
# --------------------------------------------------------------------------- #


def collect_signatures(repo_path: Path | str,
                       *, limit_files: int = 5000) -> set[str]:
    """遍历仓库下所有 .rs 文件, 抽取所有 pub fn 签名, 返回标准化指纹集合.

    边界:
    - tree-sitter 不可用 → 返回空集 + warning (调用方应回退到 rg 正则但本函数不做)
    - 文件解析失败 → 跳过, 不影响其他文件
    - target/.git/node_modules 等目录自动跳过

    返回: set[str] 形如 {"sys_open(*const u8,i32,i32) -> isize", ...}
    """
    repo = Path(repo_path)
    if not repo.is_dir():
        log.warning("signatures_repo_not_dir", path=str(repo))
        return set()

    if not ts.is_ts_available():
        log.warning("signatures_no_treesitter")
        return set()

    rs_files = _iter_rust_files(repo, limit_files=limit_files)
    seen: set[str] = set()
    n_files = 0
    n_fn = 0
    for rs in rs_files:
        n_files += 1
        try:
            fns = ts.find_functions(rs)
        except Exception as e:
            log.warning("signatures_parse_failed",
                        path=str(rs.relative_to(repo)),
                        error=str(e)[:120])
            continue
        for fn in fns:
            sig_raw = fn.get("signature") or ""
            n_fn += 1
            norm = normalize_signature(sig_raw)
            if norm:
                seen.add(norm)

    log.info("signatures_collected", repo=repo.name,
             files=n_files, total_fn=n_fn, pub_fn=len(seen))
    return seen


def _iter_rust_files(root: Path, *, limit_files: int) -> Iterable[Path]:
    """递归找 .rs 文件, 跳过常见无效目录."""
    skip_dirs = {".git", "target", "node_modules", "__pycache__",
                 ".pytest_cache", ".idea", ".vscode", "build", "out",
                 "dist", ".cache"}
    out: list[Path] = []
    for p in root.rglob("*.rs"):
        if any(part in skip_dirs for part in p.parts):
            continue
        if not p.is_file():
            continue
        out.append(p)
        if len(out) >= limit_files:
            log.warning("signatures_file_limit_hit", limit=limit_files)
            break
    return out


# --------------------------------------------------------------------------- #
# 对比
# --------------------------------------------------------------------------- #


def compare_signatures(set_a: set[str] | Iterable[str],
                       set_b: set[str] | Iterable[str]) -> SetDiff:
    """对比两仓签名集合, 返回 SetDiff (含 Jaccard)."""
    return diff_sets(set_a, set_b)


__all__ = [
    "collect_signatures",
    "compare_signatures",
    "normalize_signature",
]
