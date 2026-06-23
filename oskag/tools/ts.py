"""tree-sitter 封装: 解析器缓存 + 高层查询.

设计要点:
- get_parser/get_language 用 lru_cache, 重复调用零开销
- tree-sitter 不可用时 (网络下载 parser 失败 / cp 版本无 wheel),
  全局 _TS_AVAILABLE=False, 调用方查 is_ts_available() 决定走 rg 正则 fallback
- 高层 helper 函数返回简单 dict, 不暴露 tree-sitter 内部对象给调用方
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from oskag.logging_setup import get_logger
from oskag.tools.fs import read_file

log = get_logger("oskag.tools.ts")

# 全局可用性标记 — 模块导入时探测一次
_TS_AVAILABLE: bool | None = None


def is_ts_available() -> bool:
    """tree-sitter-language-pack 是否可用. 第一次调用时探测, 后续走 cache."""
    global _TS_AVAILABLE
    if _TS_AVAILABLE is not None:
        return _TS_AVAILABLE
    try:
        from tree_sitter_language_pack import get_parser  # noqa: F401
        # 探测 rust + c parser 真的能加载 (有些版本会动态下载, 网络不通即失败)
        _ = get_parser("rust")
        _ = get_parser("c")
        _TS_AVAILABLE = True
    except Exception as e:  # noqa: BLE001
        log.warning("ts_unavailable", error_type=type(e).__name__, error=str(e)[:160])
        _TS_AVAILABLE = False
    return _TS_AVAILABLE


@lru_cache(maxsize=8)
def _get_parser(lang: str) -> Any:
    """获取 tree-sitter Parser. 失败时抛 RuntimeError (调用方应先 is_ts_available)."""
    if not is_ts_available():
        raise RuntimeError("tree-sitter-language-pack not available")
    from tree_sitter_language_pack import get_parser
    return get_parser(lang)


@lru_cache(maxsize=8)
def _get_language(lang: str) -> Any:
    if not is_ts_available():
        raise RuntimeError("tree-sitter-language-pack not available")
    from tree_sitter_language_pack import get_language
    return get_language(lang)


# --------------------------------------------------------------------------- #
# 通用解析与查询
# --------------------------------------------------------------------------- #


@dataclass
class ParsedFile:
    """parse_file 返回的简单结果."""
    path: Path
    lang: str
    tree: Any  # tree_sitter.Tree (避免 import 失败时模块加载崩)
    source_bytes: bytes
    error: str | None = None


def detect_lang(path: Path | str) -> str | None:
    """从扩展名猜语言. 返回 None 表示不支持."""
    suffix = Path(path).suffix.lower()
    return {
        ".rs": "rust",
        ".c": "c",
        ".h": "c",
    }.get(suffix)


def parse_file(path: Path | str, *, lang: str | None = None) -> ParsedFile | None:
    """解析单个源文件. 失败 (含 ts 不可用 / 文件读不出来) 返回 None."""
    p = Path(path)
    if not is_ts_available():
        return None
    real_lang = lang or detect_lang(p)
    if not real_lang:
        return None
    fr = read_file(p)
    if fr.error:
        log.warning("ts_read_failed", path=str(p), error=fr.error[:160])
        return None
    src = fr.text.encode("utf-8", errors="replace")
    try:
        parser = _get_parser(real_lang)
        tree = parser.parse(src)
    except Exception as e:  # noqa: BLE001
        log.warning(
            "ts_parse_failed",
            path=str(p),
            lang=real_lang,
            error_type=type(e).__name__,
            error=str(e)[:160],
        )
        return None
    return ParsedFile(path=p, lang=real_lang, tree=tree, source_bytes=src)


def run_query(parsed: ParsedFile, sexp: str) -> list[dict[str, Any]]:
    """对 parsed.tree 跑 S-expression query, 返回 list[{name, node, text, start, end}].

    name 是 query 里 @name 捕获标识; text 是节点对应的源码片段 (utf-8 解码).
    失败时返回 [].

    兼容 tree-sitter Python 0.21- (Language.query + Query.captures) 与
    0.25+ (Query() 构造 + QueryCursor.captures) 两套 API.
    """
    if parsed is None or not is_ts_available():
        return []
    try:
        lang = _get_language(parsed.lang)
        # 新 API: from tree_sitter import Query, QueryCursor; Query(lang, sexp)
        try:
            from tree_sitter import Query, QueryCursor  # type: ignore[attr-defined]
            query = Query(lang, sexp)
            cursor = QueryCursor(query)
        except (ImportError, AttributeError):
            # 旧 API
            query = lang.query(sexp)
            cursor = None
    except Exception as e:  # noqa: BLE001
        log.warning(
            "ts_query_compile_failed",
            lang=parsed.lang,
            error_type=type(e).__name__,
            error=str(e)[:240],
        )
        return []

    out: list[dict[str, Any]] = []
    try:
        if cursor is not None:
            captures = cursor.captures(parsed.tree.root_node)
        else:
            captures = query.captures(parsed.tree.root_node)
    except Exception as e:  # noqa: BLE001
        log.warning("ts_query_run_failed", error=str(e)[:160])
        return []

    if isinstance(captures, dict):
        # 新版 API: {name: [Node, ...]}
        for name, nodes in captures.items():
            for node in nodes:
                out.append(_node_record(name, node, parsed.source_bytes))
    else:
        # 旧版 API: [(Node, name), ...]
        for node, name in captures:
            out.append(_node_record(name, node, parsed.source_bytes))
    return out


def _node_record(name: str, node: Any, source: bytes) -> dict[str, Any]:
    try:
        text = source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
    except (UnicodeDecodeError, AttributeError):
        text = ""
    return {
        "name": name,
        "text": text,
        "start_line": node.start_point[0] + 1,  # 1-indexed
        "end_line": node.end_point[0] + 1,
        "start_byte": node.start_byte,
        "end_byte": node.end_byte,
    }


# --------------------------------------------------------------------------- #
# 高层封装 (供 facts/ 模块使用)
# --------------------------------------------------------------------------- #

# Rust: 找所有 fn 定义 (含 pub fn / pub(crate) fn / unsafe fn / async fn / impl 内的 fn)
_RUST_QUERY_FUNCTIONS = """
(function_item
    name: (identifier) @fn_name) @fn_def
"""

# Rust: 函数体内的所有调用站点 (call_expression 的 function 字段)
_RUST_QUERY_CALLS = """
(call_expression
    function: (_) @callee) @call_site
"""

# Rust: struct 定义
_RUST_QUERY_STRUCTS = """
(struct_item
    name: (type_identifier) @struct_name) @struct_def
"""


def find_functions(path: Path | str) -> list[dict[str, Any]]:
    """提取 Rust 文件中所有 fn 定义. 返回 [{name, start_line, end_line, signature_line}].

    ts 不可用或解析失败时返回 []. 调用方应有 fallback (rg 正则).
    """
    parsed = parse_file(path)
    if parsed is None:
        return []
    rows = run_query(parsed, _RUST_QUERY_FUNCTIONS)
    by_def: dict[tuple[int, int], dict[str, Any]] = {}
    for r in rows:
        if r["name"] == "fn_def":
            key = (r["start_byte"], r["end_byte"])
            by_def.setdefault(key, {}).update({
                "start_line": r["start_line"],
                "end_line": r["end_line"],
                "start_byte": r["start_byte"],
                "end_byte": r["end_byte"],
            })
        elif r["name"] == "fn_name":
            # 找到包含该 fn_name 的 fn_def
            for key, info in by_def.items():
                if key[0] <= r["start_byte"] and r["end_byte"] <= key[1]:
                    info["name"] = r["text"]
                    break
            else:
                # 还没看到对应的 fn_def (顺序不保证), 暂存待后处理
                by_def.setdefault((r["start_byte"], r["end_byte"]), {})["name"] = r["text"]
    out: list[dict[str, Any]] = []
    src = parsed.source_bytes
    for info in by_def.values():
        if "name" not in info or "start_line" not in info:
            continue
        # 截取签名行 (从 start_byte 到第一个 '{' 或行尾)
        try:
            seg_end = src.index(b"{", info["start_byte"]) if b"{" in src[info["start_byte"]:info["end_byte"]] else info["end_byte"]
            sig = src[info["start_byte"]:seg_end].decode("utf-8", errors="replace").strip()
            sig = " ".join(sig.split())  # 多行规整成单行
        except ValueError:
            sig = info["name"]
        out.append({
            "name": info["name"],
            "start_line": info["start_line"],
            "end_line": info.get("end_line", info["start_line"]),
            "signature": sig[:200],
        })
    out.sort(key=lambda x: x["start_line"])
    return out


def find_call_edges(path: Path | str) -> list[tuple[str, str]]:
    """提取 Rust 文件中所有 (caller_fn, callee_name) 调用边.

    callee 取调用表达式的"被调函数"字段的源码文本 (可能是 ident, path, field 等).
    用于 调用图; 暂时只用于 boot.py 找前 3 层调用链.
    """
    parsed = parse_file(path)
    if parsed is None:
        return []
    fns = find_functions(path)
    if not fns:
        return []

    # 按 start_byte 排序, 二分查找包含某 byte 的 fn
    src = parsed.source_bytes
    fn_ranges: list[tuple[int, int, str]] = []
    for fn in fns:
        # 用 ts 查询的 byte 范围比 line 准
        # 这里简化: find_functions 没暴露 byte, 重新解析
        pass
    # 重新跑一次 query 拿 byte 范围
    rows = run_query(parsed, _RUST_QUERY_FUNCTIONS)
    by_def: dict[tuple[int, int], str] = {}
    name_pending: dict[tuple[int, int], str] = {}
    defs: list[tuple[int, int]] = []
    for r in rows:
        if r["name"] == "fn_def":
            defs.append((r["start_byte"], r["end_byte"]))
            by_def[(r["start_byte"], r["end_byte"])] = ""
        elif r["name"] == "fn_name":
            name_pending[(r["start_byte"], r["end_byte"])] = r["text"]
    for (ns, ne), nm in name_pending.items():
        for ds, de in defs:
            if ds <= ns and ne <= de:
                by_def[(ds, de)] = nm
                break

    def caller_at(byte: int) -> str | None:
        for (ds, de), nm in by_def.items():
            if ds <= byte < de:
                return nm or None
        return None

    # 跑 call query
    call_rows = run_query(parsed, _RUST_QUERY_CALLS)
    calls: list[tuple[str, str]] = []
    for r in call_rows:
        if r["name"] != "callee":
            continue
        callee = r["text"].strip()
        if not callee:
            continue
        # 截短: 只取最后一个段 (a::b::c → c) 也保留全路径
        caller = caller_at(r["start_byte"])
        if caller is None:
            continue  # 顶层调用不计入边
        calls.append((caller, callee))
    return calls


__all__ = [
    "ParsedFile",
    "is_ts_available",
    "detect_lang",
    "parse_file",
    "run_query",
    "find_functions",
    "find_call_edges",
]
