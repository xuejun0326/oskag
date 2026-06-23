"""pipelines/_diff.py — 确定性差异算子 .

0 LLM 调用, 纯算法. 给 compare 流水线提供:
- diff_dirs(a, b): 目录树集合差异 (only_a / only_b / common)
- diff_sets(set_a, set_b): 集合差异 (only_a / only_b / intersection / union)
- jaccard(set_a, set_b): Jaccard 系数 [0, 1]
- simrank(graph_a, graph_b): 图结构相似度 (基于 NetworkX, 0-1)

设计:
- 所有函数永不抛 (空集 / 单元素 / 不可哈希都给确定的兜底值)
- 不依赖 facts.json 的 schema, 全部接 set / list / Path 这种通用类型
- simrank 只算节点级 max-pair, 大图截断到 N 个最高度数节点 (避免 O(n^4) 爆掉)
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from oskag.logging_setup import get_logger

log = get_logger("oskag.pipelines._diff")


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SetDiff:
    """集合差异结果."""
    only_a: set
    only_b: set
    intersection: set
    union: set
    a_count: int
    b_count: int
    intersection_count: int
    jaccard: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "only_a": sorted(self.only_a, key=str),
            "only_b": sorted(self.only_b, key=str),
            "intersection": sorted(self.intersection, key=str),
            "a_count": self.a_count,
            "b_count": self.b_count,
            "intersection_count": self.intersection_count,
            "jaccard": self.jaccard,
        }


@dataclass(frozen=True)
class DirDiff:
    """目录树差异结果."""
    only_a: list[str]
    only_b: list[str]
    common: list[str]
    jaccard: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "only_a": self.only_a,
            "only_b": self.only_b,
            "common": self.common,
            "jaccard": self.jaccard,
        }


# --------------------------------------------------------------------------- #
# 集合差异 / Jaccard
# --------------------------------------------------------------------------- #


def jaccard(set_a: Iterable, set_b: Iterable) -> float:
    """Jaccard 系数 = |A ∩ B| / |A ∪ B|.

    边界:
    - 两边都空 → 1.0 (按 "全空集合彼此相同" 约定, 跟 simrank 自比自一致)
    - 一边空一边非空 → 0.0
    - 不可哈希元素被自动 str() 化
    """
    a = _to_hashable_set(set_a)
    b = _to_hashable_set(set_b)
    if not a and not b:
        return 1.0
    inter = len(a & b)
    union = len(a | b)
    if union == 0:
        return 0.0
    return round(inter / union, 4)


def diff_sets(set_a: Iterable, set_b: Iterable) -> SetDiff:
    """计算两个集合的 only_a / only_b / intersection / union 与 Jaccard."""
    a = _to_hashable_set(set_a)
    b = _to_hashable_set(set_b)
    only_a = a - b
    only_b = b - a
    inter = a & b
    union = a | b
    j = jaccard(a, b)
    return SetDiff(
        only_a=only_a,
        only_b=only_b,
        intersection=inter,
        union=union,
        a_count=len(a),
        b_count=len(b),
        intersection_count=len(inter),
        jaccard=j,
    )


def _to_hashable_set(items: Iterable) -> set:
    """把任意可迭代对象转成 set, 不可哈希元素 str() 化."""
    out: set = set()
    if items is None:
        return out
    for it in items:
        try:
            out.add(it)
        except TypeError:
            out.add(str(it))
    return out


# --------------------------------------------------------------------------- #
# 目录树差异
# --------------------------------------------------------------------------- #


def diff_dirs(a: Path | str | None, b: Path | str | None,
              *, max_depth: int = 2,
              ignore_names: set[str] | None = None) -> DirDiff:
    """对比两个目录的子目录集合差异.

    - 只取相对路径, 限制 max_depth (默认顶层 + 一级子目录)
    - 忽略 .git / target / node_modules / __pycache__ 等 (可定制)
    - 不存在 / 不是目录 → 当作空集

    返回 DirDiff(only_a, only_b, common, jaccard).
    """
    if ignore_names is None:
        ignore_names = {".git", "target", "node_modules", "__pycache__",
                        ".pytest_cache", ".idea", ".vscode", "build", "out"}

    a_paths = _collect_subdirs(a, max_depth, ignore_names)
    b_paths = _collect_subdirs(b, max_depth, ignore_names)

    only_a = sorted(a_paths - b_paths)
    only_b = sorted(b_paths - a_paths)
    common = sorted(a_paths & b_paths)
    j = jaccard(a_paths, b_paths)

    return DirDiff(only_a=only_a, only_b=only_b, common=common, jaccard=j)


def _collect_subdirs(root: Path | str | None, max_depth: int,
                     ignore_names: set[str]) -> set[str]:
    """收集相对路径形式的目录集合 (POSIX 分隔符)."""
    if root is None:
        return set()
    p = Path(root)
    if not p.is_dir():
        return set()

    out: set[str] = set()
    p_resolved = p.resolve()

    def _walk(current: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = list(current.iterdir())
        except (OSError, PermissionError) as e:
            log.warning("dir_walk_failed", path=str(current), error=str(e)[:120])
            return
        for entry in entries:
            if not entry.is_dir():
                continue
            if entry.name in ignore_names or entry.name.startswith("."):
                continue
            try:
                rel = entry.resolve().relative_to(p_resolved).as_posix()
            except ValueError:
                continue
            out.add(rel)
            if depth + 1 <= max_depth:
                _walk(entry, depth + 1)

    _walk(p, 1)
    return out


# --------------------------------------------------------------------------- #
# SimRank (图结构相似度)
# --------------------------------------------------------------------------- #


def simrank(graph_a, graph_b, *,
            top_n: int = 50,
            max_iterations: int = 1000,
            tolerance: float = 1e-2,
            importance_factor: float = 0.85) -> float:
    """两图整体 SimRank 相似度 (初版).

    设计权衡 (重要):
    - SimRank 原本是单图内 "两节点的递归邻居相似度". 跨图对比时, 必须把两图
      合并成一张大图才有意义.
    - 这里采用: 节点 **不加前缀** 合并 (同名节点视为同一节点, 自然桥接两图),
      跑 simrank → 对所有 (only_a, only_b) 节点对取 95 分位.
    - 节点完全不重名 → SimRank 退化为 0 (此时应靠 node_jaccard / edge_jaccard
      给出对比信号, callgraph_score 公式会兜住).
    - 自比自 → graph_a is graph_b 时直接返回 1.0 (避免合并图导致的对称性副作用).

    返回 [0, 1] 浮点, 不抛.

    边界:
    - 两边都空 → 1.0
    - 一边空 → 0.0
    - 不收敛 → 放宽容差再试一次, 再不收敛返回 0.0
    - 任何异常 → 0.0 + warning
    """
    edge_status = _simrank_edge_cases(graph_a, graph_b)
    if edge_status is not None:
        return edge_status

    try:
        return _simrank_compute(
            graph_a, graph_b, top_n, max_iterations, tolerance, importance_factor,
        )
    except Exception as e:
        log.warning("simrank_failed", error=f"{type(e).__name__}: {e!s}"[:160])
        return 0.0


def _simrank_edge_cases(graph_a, graph_b) -> float | None:
    """处理 simrank 的边界情况, 命中返回数值; 否则返回 None 让主路径继续."""
    try:
        import networkx  # noqa: F401
    except ImportError:
        log.warning("simrank_no_networkx")
        return 0.0

    a_empty = (graph_a is None) or (len(graph_a) == 0)
    b_empty = (graph_b is None) or (len(graph_b) == 0)
    if a_empty and b_empty:
        return 1.0
    if a_empty or b_empty:
        return 0.0
    if graph_a is graph_b:
        return 1.0
    return None


def _simrank_compute(graph_a, graph_b, top_n: int, max_iterations: int,
                     tolerance: float, importance_factor: float) -> float:
    """跑实际的 simrank 计算, 调用方负责异常包裹."""
    import networkx as nx

    a_top = _top_nodes_by_degree(graph_a, top_n)
    b_top = _top_nodes_by_degree(graph_b, top_n)
    a_set = set(a_top)
    b_set = set(b_top)
    shared = a_set & b_set

    merged = _build_merged_digraph(nx, graph_a, graph_b, a_set, b_set, a_top, b_top)
    sim = _run_simrank_with_fallback(
        nx, merged, importance_factor, max_iterations, tolerance,
    )
    if sim is None:
        return 0.0

    only_a = a_set - shared
    only_b = b_set - shared
    cross_scores = _collect_cross_pair_scores(sim, only_a, only_b)
    if not cross_scores:
        return 1.0 if shared else 0.0

    cross_scores.sort()
    idx = min(int(len(cross_scores) * 0.95), len(cross_scores) - 1)
    return round(float(cross_scores[idx]), 4)


def _build_merged_digraph(nx, graph_a, graph_b,
                          a_set: set, b_set: set,
                          a_top: list, b_top: list):
    """把两图节点和边合并到同一张 DiGraph, 同名节点自然合并."""
    merged = nx.DiGraph()
    merged.add_nodes_from(a_top)
    merged.add_nodes_from(b_top)
    for u, v in graph_a.edges():
        if u in a_set and v in a_set:
            merged.add_edge(u, v)
    for u, v in graph_b.edges():
        if u in b_set and v in b_set:
            merged.add_edge(u, v)
    return merged


def _collect_cross_pair_scores(sim: dict, only_a: set, only_b: set) -> list[float]:
    """收集所有 (only_a, only_b) 节点对的 simrank 分数."""
    out: list[float] = []
    for u in only_a:
        row = sim.get(u) or {}
        for v in only_b:
            out.append(row.get(v, 0.0))
    return out


def _run_simrank_with_fallback(nx, graph, importance_factor: float,
                                max_iterations: int,
                                tolerance: float):
    """跑 simrank_similarity, 不收敛时放宽容差再来一次."""
    try:
        return nx.simrank_similarity(
            graph,
            importance_factor=importance_factor,
            max_iterations=max_iterations,
            tolerance=tolerance,
        )
    except nx.ExceededMaxIterations:
        # 放宽 10x 容差再试 — 内核调用图常常有大量弱连通分量, 收敛慢但不需要超精确
        try:
            return nx.simrank_similarity(
                graph,
                importance_factor=importance_factor,
                max_iterations=max_iterations,
                tolerance=tolerance * 10,
            )
        except nx.ExceededMaxIterations:
            log.warning("simrank_no_converge", tolerance=tolerance * 10)
            return None


def _top_nodes_by_degree(graph, top_n: int) -> list:
    """返回度数最高的 top_n 个节点 (出度 + 入度)."""
    degs = sorted(
        graph.nodes(),
        key=lambda n: graph.in_degree(n) + graph.out_degree(n),
        reverse=True,
    )
    return list(degs[:top_n])


__all__ = [
    "DirDiff",
    "SetDiff",
    "diff_dirs",
    "diff_sets",
    "jaccard",
    "simrank",
]
