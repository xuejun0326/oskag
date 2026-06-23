"""facts/call_graph.py — 调用图抽取与对比 (轻量版).

设计原则 (用户 2026-06-16 调整):
- 第一版**不画 mermaid 图**, 用数字 + 高频函数名列表把"两个内核调用模式有多接近"讲清楚
- 跨架构两仓 (节点完全不重名) → SimRank 退化为 0, 但 node_jaccard / edge_jaccard 仍有信号
- 调用边的 callee 取调用表达式的"被调函数"字段的源码文本, **不做语义解析** (`x.foo()` 的 callee 是 "foo", 不解析谁的方法)

外部 API:
- collect_call_graph(repo_path) -> CallGraph
- compare_call_graphs(cg_a, cg_b) -> CallGraphDiff
- callgraph_score(cg_diff) -> float (0.5 * node_jaccard + 0.5 * edge_jaccard)
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from oskag.logging_setup import get_logger
from oskag.pipelines._diff import diff_sets, jaccard, simrank
from oskag.tools import ts

log = get_logger("oskag.facts.call_graph")


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass
class CallGraph:
    """单仓的调用图.

    - nodes: 函数名集合 (定义过 + 被调过的所有函数名)
    - edges: (caller, callee) 元组集合
    - call_counts: callee → 被调用次数 (按全仓聚合)
    - graph: networkx.DiGraph 实例 (供 simrank 用); None 表示 networkx 不可用
    """
    repo_name: str
    nodes: set[str] = field(default_factory=set)
    edges: set[tuple[str, str]] = field(default_factory=set)
    call_counts: Counter = field(default_factory=Counter)
    graph: Any = None

    @property
    def n_nodes(self) -> int:
        return len(self.nodes)

    @property
    def n_edges(self) -> int:
        return len(self.edges)

    @property
    def avg_out_degree(self) -> float:
        if not self.nodes:
            return 0.0
        # 按 caller 分组, 平均每个 caller 调用了多少次
        caller_counts: Counter = Counter()
        for caller, _ in self.edges:
            caller_counts[caller] += 1
        if not caller_counts:
            return 0.0
        return round(sum(caller_counts.values()) / len(caller_counts), 2)

    def top_callees(self, n: int = 10) -> list[dict[str, Any]]:
        """返回被调用次数 Top n 的函数 (callee), 形如 [{name, count}]."""
        return [
            {"name": name, "count": count}
            for name, count in self.call_counts.most_common(n)
        ]


@dataclass
class CallGraphDiff:
    """两仓调用图差异."""
    a: CallGraph
    b: CallGraph
    node_jaccard: float
    edge_jaccard: float
    simrank_score: float
    common_nodes_count: int
    common_edges_count: int

    @property
    def callgraph_score(self) -> float:
        """综合分: 0.5·node + 0.5·edge (第一版不含 simrank, 跨架构会退化)."""
        return round(0.5 * self.node_jaccard + 0.5 * self.edge_jaccard, 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "a_nodes": self.a.n_nodes,
            "a_edges": self.a.n_edges,
            "a_avg_out_degree": self.a.avg_out_degree,
            "b_nodes": self.b.n_nodes,
            "b_edges": self.b.n_edges,
            "b_avg_out_degree": self.b.avg_out_degree,
            "node_jaccard": self.node_jaccard,
            "edge_jaccard": self.edge_jaccard,
            "simrank": self.simrank_score,
            "common_nodes_count": self.common_nodes_count,
            "common_edges_count": self.common_edges_count,
            "callgraph_score": self.callgraph_score,
            "a_top_callees": self.a.top_callees(10),
            "b_top_callees": self.b.top_callees(10),
        }


# --------------------------------------------------------------------------- #
# 抽取
# --------------------------------------------------------------------------- #


_SKIP_DIRS = {".git", "target", "node_modules", "__pycache__",
              ".pytest_cache", ".idea", ".vscode", "build", "out",
              "dist", ".cache"}


def collect_call_graph(repo_path: Path | str,
                       *, limit_files: int = 5000) -> CallGraph:
    """遍历仓库下所有 .rs 文件, 抽取调用图.

    边界:
    - tree-sitter 不可用 → 返回空 CallGraph
    - 文件解析失败 → 跳过, 不影响其他文件
    - 仓库不存在 → 返回空 CallGraph
    """
    repo = Path(repo_path)
    cg = CallGraph(repo_name=repo.name)

    if not repo.is_dir():
        log.warning("call_graph_repo_not_dir", path=str(repo))
        return cg

    if not ts.is_ts_available():
        log.warning("call_graph_no_treesitter")
        return cg

    rs_files = _iter_rust_files(repo, limit_files=limit_files)
    n_files = 0
    for rs in rs_files:
        n_files += 1
        try:
            edges = ts.find_call_edges(rs)
            fns = ts.find_functions(rs)
        except Exception as e:
            log.warning("call_graph_parse_failed",
                        path=str(rs.relative_to(repo)),
                        error=str(e)[:120])
            continue

        # 节点 = 该文件定义的所有 fn name
        for fn in fns:
            name = fn.get("name")
            if name:
                cg.nodes.add(name)

        # 边 (caller, callee) 也添加节点 + 计数
        for caller, callee in edges:
            callee_name = _normalize_callee(callee)
            if not callee_name:
                continue
            cg.nodes.add(caller)
            cg.nodes.add(callee_name)
            cg.edges.add((caller, callee_name))
            cg.call_counts[callee_name] += 1

    cg.graph = _build_nx_graph(cg)
    log.info("call_graph_collected", repo=repo.name,
             files=n_files, nodes=cg.n_nodes, edges=cg.n_edges)
    return cg


def _normalize_callee(callee: str) -> str:
    """规整 callee 文本: 取最后一段路径 (`foo::bar::baz` → `baz`),
    去掉调用括号 / 类型参数 (`<T>`).
    """
    if not callee:
        return ""
    s = callee.strip()
    # 去掉 turbofish / 类型参数: `foo::<T>` → `foo`
    if "<" in s:
        s = s.split("<", 1)[0]
    # 取 :: 路径最后一段
    if "::" in s:
        s = s.rsplit("::", 1)[1]
    # 去掉 method call 前缀 (`x.foo` → `foo`)
    if "." in s:
        s = s.rsplit(".", 1)[1]
    # 去掉空白与括号
    s = s.strip().rstrip("(")
    # 排除空 / 仅符号
    if not s or not s[0].isalpha() and s[0] != "_":
        return ""
    return s


def _iter_rust_files(root: Path, *, limit_files: int) -> list[Path]:
    out: list[Path] = []
    for p in root.rglob("*.rs"):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        if not p.is_file():
            continue
        out.append(p)
        if len(out) >= limit_files:
            log.warning("call_graph_file_limit_hit", limit=limit_files)
            break
    return out


def _build_nx_graph(cg: CallGraph):
    """从 nodes/edges 构造 networkx.DiGraph."""
    try:
        import networkx as nx
    except ImportError:
        log.warning("call_graph_no_networkx")
        return None
    g = nx.DiGraph()
    g.add_nodes_from(cg.nodes)
    g.add_edges_from(cg.edges)
    return g


# --------------------------------------------------------------------------- #
# 对比
# --------------------------------------------------------------------------- #


def compare_call_graphs(cg_a: CallGraph, cg_b: CallGraph,
                        *, run_simrank: bool = True) -> CallGraphDiff:
    """对比两个调用图, 返回 CallGraphDiff.

    - run_simrank=False 时跳过 SimRank (跨架构两仓时建议跳, 避免迭代不收敛)
    """
    node_diff = diff_sets(cg_a.nodes, cg_b.nodes)
    edge_diff = diff_sets(cg_a.edges, cg_b.edges)

    sim_score = 0.0
    if run_simrank and cg_a.graph is not None and cg_b.graph is not None:
        sim_score = simrank(cg_a.graph, cg_b.graph)

    return CallGraphDiff(
        a=cg_a,
        b=cg_b,
        node_jaccard=node_diff.jaccard,
        edge_jaccard=edge_diff.jaccard,
        simrank_score=sim_score,
        common_nodes_count=node_diff.intersection_count,
        common_edges_count=edge_diff.intersection_count,
    )


def callgraph_score(node_jacc: float, edge_jacc: float) -> float:
    """综合调用图相似度 = 0.5·node + 0.5·edge.

    第一版不含 SimRank — 跨架构两仓 SimRank 会退化为 0 带噪声, 加 graph kernel 后启用.
    """
    return round(0.5 * node_jacc + 0.5 * edge_jacc, 4)


__all__ = [
    "CallGraph",
    "CallGraphDiff",
    "callgraph_score",
    "collect_call_graph",
    "compare_call_graphs",
]
