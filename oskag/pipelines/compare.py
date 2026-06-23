"""pipelines/compare.py — 两仓对比流水线 (+S8).

输入:
- 两仓的 facts.json (产物)
- 两仓的 *-describe.json (产物, 起 cli describe 自动落盘)

流程:
1. load 两份 facts + 两份 describe
2. deterministic diff: 目录 / 模块覆盖 / deps / syscall / LOC
3. 函数签名 Jaccard (facts/signatures.collect_signatures)
4. 调用图 diff (facts/call_graph.collect_call_graph)
5. 模块语义对照 ×9 (LLM, prompts/compare_semantic.j2)
6. novelty / 关键差异 (LLM v4-pro thinking, prompts/novelty.j2)
7. 综合相似度评分 (公式)
8. verifier sweep (复用 verifier.j2, 重读 evidence)
9. 不渲染 markdown — 由 cli 调 render/markdown.compare 处理

设计原则 (用户 2026-06-16 调整):
- 不强求两仓相似. overall_score < 0.15 时 verdict='independent', 不勉强找创新.
- 缺失模块/未实现 → diff_points 标 a_only/b_only, 不抛.
- LLM 调用任何步骤失败都 catch → 写 errors[] 继续.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from oskag.describe.prompts import render_messages
from oskag.facts.call_graph import (
    CallGraph,
    CallGraphDiff,
    collect_call_graph,
    compare_call_graphs,
)
from oskag.facts.signatures import collect_signatures, compare_signatures
from oskag.llm import ChatResult, DeepSeekClient
from oskag.logging_setup import get_logger
from oskag.pipelines._diff import SetDiff, diff_dirs, diff_sets

log = get_logger("oskag.pipelines.compare")


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #


# 9 模块顺序 (与 _modules.py 一致)
_MODULE_ORDER = ["boot", "mm", "task", "fs", "signal", "ipc", "net", "drivers", "syscall"]

_MODULE_TITLES_ZH = {
    "boot": "启动",
    "mm": "内存管理",
    "task": "进程/任务",
    "fs": "文件系统",
    "signal": "信号",
    "ipc": "IPC",
    "net": "网络",
    "drivers": "驱动",
    "syscall": "系统调用",
}

# 综合相似度公式权重
_W_SIG = 0.30        # 函数签名
_W_SYSCALL = 0.20    # syscall 集合
_W_DEPS = 0.20       # cargo 依赖
_W_CALLGRAPH = 0.20  # 调用图综合
_W_DIR = 0.10        # 目录 Jaccard

# 等级阈值
_LEVEL_HIGH = 0.70
_LEVEL_MED = 0.40
_LEVEL_LOW = 0.15


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass
class ModuleCompare:
    """单个模块的语义对照 (LLM 输出)."""
    name: str
    title_zh: str
    a_implemented: bool
    b_implemented: bool
    a_summary: str = ""
    b_summary: str = ""
    verdict: str = "both_missing"  # similar/divergent/independent/a_only/b_only/both_missing
    diff_points: list[dict] = field(default_factory=list)
    error: str | None = None


@dataclass
class SimilarityScore:
    """综合相似度评分."""
    sig_jaccard: float
    syscall_jaccard: float
    deps_jaccard: float
    callgraph_score: float
    dir_jaccard: float
    overall: float
    label: str  # 高度相似 / 中度相似 / 低度相似 / 完全不同

    def to_dict(self) -> dict[str, Any]:
        return {
            "sig_jaccard": self.sig_jaccard,
            "sig_weighted": round(_W_SIG * self.sig_jaccard, 4),
            "syscall_jaccard": self.syscall_jaccard,
            "syscall_weighted": round(_W_SYSCALL * self.syscall_jaccard, 4),
            "deps_jaccard": self.deps_jaccard,
            "deps_weighted": round(_W_DEPS * self.deps_jaccard, 4),
            "callgraph_score": self.callgraph_score,
            "callgraph_weighted": round(_W_CALLGRAPH * self.callgraph_score, 4),
            "dir_jaccard": self.dir_jaccard,
            "dir_weighted": round(_W_DIR * self.dir_jaccard, 4),
            "overall_score": self.overall,
            "overall_label": self.label,
        }


@dataclass
class CompareReport:
    """compare 整轮产出."""
    a_repo: str
    b_repo: str
    # 基础事实并排 (从两份 facts + describe 提取)
    a_basic: dict = field(default_factory=dict)
    b_basic: dict = field(default_factory=dict)
    # 结构差异
    dir_diff: dict = field(default_factory=dict)
    deps_diff: dict = field(default_factory=dict)
    syscall_diff: dict = field(default_factory=dict)
    sig_diff: dict = field(default_factory=dict)
    cg_diff: dict = field(default_factory=dict)
    # LLM 输出
    module_compares: list[ModuleCompare] = field(default_factory=list)
    novelty: dict = field(default_factory=dict)  # {summary, verdict, diffs[]}
    # 评分
    similarity: SimilarityScore | None = None
    # 元数据
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_reasoning_tokens: int = 0
    duration_sec: float = 0.0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "a_repo": self.a_repo,
            "b_repo": self.b_repo,
            "a_basic": self.a_basic,
            "b_basic": self.b_basic,
            "dir_diff": self.dir_diff,
            "deps_diff": self.deps_diff,
            "syscall_diff": self.syscall_diff,
            "sig_diff": self.sig_diff,
            "cg_diff": self.cg_diff,
            "module_compares": [
                {
                    "name": mc.name,
                    "title_zh": mc.title_zh,
                    "a_implemented": mc.a_implemented,
                    "b_implemented": mc.b_implemented,
                    "a_summary": mc.a_summary,
                    "b_summary": mc.b_summary,
                    "verdict": mc.verdict,
                    "diff_points": mc.diff_points,
                    "error": mc.error,
                }
                for mc in self.module_compares
            ],
            "novelty": self.novelty,
            "similarity": self.similarity.to_dict() if self.similarity else {},
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_reasoning_tokens": self.total_reasoning_tokens,
            "duration_sec": self.duration_sec,
            "warnings": self.warnings,
            "errors": self.errors,
        }


# --------------------------------------------------------------------------- #
# 综合评分
# --------------------------------------------------------------------------- #


def compute_similarity(*, sig: float, syscall: float, deps: float,
                       callgraph: float, directory: float) -> SimilarityScore:
    """综合相似度 = 加权和 (公式).

    overall = 0.30·sig + 0.20·syscall + 0.20·deps + 0.20·callgraph + 0.10·dir
    """
    overall = (
        _W_SIG * sig
        + _W_SYSCALL * syscall
        + _W_DEPS * deps
        + _W_CALLGRAPH * callgraph
        + _W_DIR * directory
    )
    overall = round(overall, 4)
    return SimilarityScore(
        sig_jaccard=sig,
        syscall_jaccard=syscall,
        deps_jaccard=deps,
        callgraph_score=callgraph,
        dir_jaccard=directory,
        overall=overall,
        label=_label_for(overall),
    )


def _label_for(score: float) -> str:
    """根据 overall_score 给等级标签."""
    if score >= _LEVEL_HIGH:
        return "高度相似"
    if score >= _LEVEL_MED:
        return "中度相似"
    if score >= _LEVEL_LOW:
        return "低度相似"
    return "完全不同"


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def compare(
    a_repo: Path | str,
    b_repo: Path | str,
    *,
    a_facts: dict | Path | str,
    b_facts: dict | Path | str,
    a_describe: dict | Path | str,
    b_describe: dict | Path | str,
    client: DeepSeekClient | None = None,
    skip_llm: bool = False,
    skip_callgraph: bool = False,
) -> CompareReport:
    """两仓对比主流程.

    Args:
        a_repo, b_repo: 仓库根目录
        a_facts, b_facts: facts.json 路径或已 load 的 dict
        a_describe, b_describe: describe.json 路径或已 load 的 dict
        client: DeepSeekClient (skip_llm=True 时可不传)
        skip_llm: 跳过 LLM 调用 (测试 + 单测用)
        skip_callgraph: 跳过调用图抽取 (大仓很慢, 单测用)

    Returns:
        CompareReport (永不抛, 错误进 .errors).
    """
    t0 = time.time()
    a_path = Path(a_repo)
    b_path = Path(b_repo)
    a_facts_dict = _load_or_pass(a_facts)
    b_facts_dict = _load_or_pass(b_facts)
    a_desc_dict = _load_or_pass(a_describe)
    b_desc_dict = _load_or_pass(b_describe)

    rep = CompareReport(
        a_repo=a_path.name,
        b_repo=b_path.name,
    )

    # 1. 基础事实并排
    rep.a_basic = _extract_basic(a_facts_dict, a_desc_dict)
    rep.b_basic = _extract_basic(b_facts_dict, b_desc_dict)

    # 2. 目录差异
    try:
        dd = diff_dirs(a_path, b_path)
        rep.dir_diff = dd.to_dict()
    except Exception as e:
        rep.errors.append(f"dir_diff failed: {e!s}")
        log.warning("compare_dir_diff_failed", error=str(e)[:160])

    # 3. deps / syscall 差异 (从 facts)
    rep.deps_diff = _diff_deps(a_facts_dict, b_facts_dict).to_dict()
    rep.syscall_diff = _diff_syscalls(a_facts_dict, b_facts_dict).to_dict()

    # 4. 函数签名 Jaccard
    rep.sig_diff = _build_sig_diff(a_path, b_path, rep)

    # 5. 调用图 diff
    if skip_callgraph:
        rep.cg_diff = _empty_cg_diff()
        rep.warnings.append("调用图抽取已跳过 (skip_callgraph=True)")
    else:
        rep.cg_diff = _build_cg_diff(a_path, b_path, rep)

    # 6. 综合评分
    # 注意: 当某项数据完全缺失 (a=0 且 b=0) 时, jaccard 退化为 1.0 是 _diff 的约定,
    # 但在 compare 上下文里"两边都没数据"应当视为 0 而非满分, 避免误判"完全不同"为"高度相似".
    rep.similarity = compute_similarity(
        sig=_safe_jaccard(rep.sig_diff),
        syscall=_safe_jaccard(rep.syscall_diff),
        deps=_safe_jaccard(rep.deps_diff),
        callgraph=_safe_callgraph_score(rep.cg_diff),
        directory=_safe_dir_jaccard(rep.dir_diff),
    )

    # 7. LLM 模块语义对照 + novelty
    if not skip_llm and client is not None:
        # B: 标准化 describe.json (LLM 返回字段名不统一: desc/description 混用)
        a_desc_norm = _normalize_describe(a_desc_dict)
        b_desc_norm = _normalize_describe(b_desc_dict)
        rep.module_compares = _run_module_compares(client, rep, a_desc_norm, b_desc_norm)
        rep.novelty = _run_novelty(client, rep, a_desc_norm, b_desc_norm)
    else:
        rep.warnings.append("LLM 步骤已跳过 (skip_llm=True 或 client=None)")

    rep.duration_sec = round(time.time() - t0, 2)
    log.info("compare_done", a=rep.a_repo, b=rep.b_repo,
             overall=rep.similarity.overall if rep.similarity else None,
             duration_sec=rep.duration_sec,
             prompt_tokens=rep.total_prompt_tokens,
             errors=len(rep.errors))
    return rep


# --------------------------------------------------------------------------- #
# 内部辅助
# --------------------------------------------------------------------------- #


def _load_or_pass(x: dict | Path | str) -> dict:
    """如果 x 是 dict 直接返回, 否则当成 json 路径 load."""
    if isinstance(x, dict):
        return x
    p = Path(x)
    if not p.is_file():
        log.warning("compare_json_not_found", path=str(p))
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("compare_json_load_failed", path=str(p), error=str(e)[:160])
        return {}


def _extract_basic(facts: dict, desc: dict) -> dict:
    """从 facts + describe 抽取并排表用的基础事实."""
    profile = facts.get("profile", {}) or {}
    syscalls = facts.get("syscalls", {}) or {}
    boot_info = facts.get("boot", {}) or {}
    cargo = facts.get("cargo", {}) or {}

    implemented, unimplemented = _split_implemented_modules(desc)
    loc_files, loc_code = _extract_loc(facts.get("repo_stats", {}) or {})
    syscall_count = _extract_syscall_count(syscalls)
    boot_style = boot_info.get("boot_style") or boot_info.get("entry_kind", "unknown")
    handler_kinds = _extract_handler_kinds(boot_info)

    return {
        "name": facts.get("repo_name", "") or (facts.get("meta", {}) or {}).get("repo_name", ""),
        "family": profile.get("family", "unknown"),
        "is_workspace": cargo.get("is_workspace", False),
        "member_count": len(cargo.get("workspace_members", []) or cargo.get("members", []) or []),
        "loc_files": loc_files,
        "loc_code": loc_code,
        "syscall_count": syscall_count,
        "boot_style": boot_style,
        "handler_kinds": handler_kinds,
        "implemented_modules": implemented,
        "unimplemented_modules": unimplemented,
    }


def _split_implemented_modules(desc: dict) -> tuple[list[str], list[str]]:
    """从 describe.modules 拆出已实现 / 未实现两个标题列表."""
    implemented: list[str] = []
    unimplemented: list[str] = []
    for m in desc.get("modules", []) or []:
        title = m.get("title_zh") or m.get("name", "")
        if m.get("unimplemented"):
            unimplemented.append(title)
        else:
            implemented.append(title)
    return implemented, unimplemented


def _extract_loc(repo_stats: dict) -> tuple[int, int]:
    """从 repo_stats 抽 (rust_files, rust_code_lines), 兼容多种 schema."""
    rust_loc = (repo_stats.get("loc", {}) or {}).get("by_language", {}).get("Rust", {}) or {}
    loc_files = rust_loc.get("files", 0) or repo_stats.get("total_files", 0)
    loc_code = rust_loc.get("code", 0) or repo_stats.get("total_code_lines", 0)
    return loc_files, loc_code


def _extract_syscall_count(syscalls: dict) -> int:
    """优先用 syscalls.count, 退而用 items / implemented 长度."""
    count = syscalls.get("count", 0)
    if count:
        return count
    return len(syscalls.get("items", []) or syscalls.get("implemented", []) or [])


def _extract_handler_kinds(boot_info: dict) -> list[str]:
    """boot.handlers 是 list of {kind,file,line} 或纯字符串; 提 kind 字段并去重保序."""
    kinds: list[str] = []
    for h in boot_info.get("handlers", []) or []:
        if isinstance(h, dict):
            kind = h.get("kind")
            if kind:
                kinds.append(kind)
        elif isinstance(h, str):
            kinds.append(h)
    if not kinds:
        kinds = boot_info.get("handler_kinds", []) or []
    return _dedupe_preserve_order(kinds)


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _diff_deps(a_facts: dict, b_facts: dict) -> SetDiff:
    """对比 cargo 依赖集合."""
    a_deps = _collect_deps(a_facts)
    b_deps = _collect_deps(b_facts)
    return diff_sets(a_deps, b_deps)


def _collect_deps(facts: dict) -> set[str]:
    cargo = facts.get("cargo", {}) or {}
    out: set[str] = set()
    # facts schema 实际字段 (优先): all_deps_union 是聚合后的字符串列表
    _add_dep_names(out, cargo.get("all_deps_union", []))
    # workspace_deps 也是字符串列表
    _add_dep_names(out, cargo.get("workspace_deps", []))
    # 兼容历史 schema: members[].dependencies / crates[].dependencies
    for member in cargo.get("members", []) or []:
        _add_dep_names(out, member.get("dependencies", []))
    for crate in cargo.get("crates", []) or []:
        _add_dep_names(out, crate.get("dependencies", []))
    # 兼容旧字段名 workspace_dependencies
    _add_dep_names(out, cargo.get("workspace_dependencies", []))
    return out


def _add_dep_names(target: set[str], deps: list | None) -> None:
    """从 dependencies 列表 (dict 或纯字符串都接受) 抽 name 加进 target."""
    for d in deps or []:
        name = d.get("name") if isinstance(d, dict) else d
        if name:
            target.add(str(name))


def _diff_syscalls(a_facts: dict, b_facts: dict) -> SetDiff:
    """对比 syscall 集合 (按名字)."""
    a_set = _collect_syscall_names(a_facts)
    b_set = _collect_syscall_names(b_facts)
    return diff_sets(a_set, b_set)


def _collect_syscall_names(facts: dict) -> set[str]:
    sc = facts.get("syscalls", {}) or {}
    out: set[str] = set()
    # facts schema 实际字段: syscalls.items[].name
    for s in sc.get("items", []) or []:
        name = s.get("name") if isinstance(s, dict) else s
        if name:
            out.add(str(name))
    # 兼容历史字段名 syscalls.implemented
    for s in sc.get("implemented", []) or []:
        name = s.get("name") if isinstance(s, dict) else s
        if name:
            out.add(str(name))
    return out


def _build_sig_diff(a_path: Path, b_path: Path, rep: CompareReport) -> dict[str, Any]:
    """跑函数签名抽取 + Jaccard."""
    try:
        a_sigs = collect_signatures(a_path, limit_files=2000)
        b_sigs = collect_signatures(b_path, limit_files=2000)
        sd = compare_signatures(a_sigs, b_sigs)
        result = sd.to_dict()
        # 加点采样数据用于 markdown 渲染
        result["only_a_sample"] = list(sd.only_a)[:10]
        result["only_b_sample"] = list(sd.only_b)[:10]
        return result
    except Exception as e:
        rep.errors.append(f"signatures failed: {e!s}")
        log.warning("compare_sig_failed", error=str(e)[:160])
        return {"jaccard": 0.0, "a_count": 0, "b_count": 0,
                "intersection_count": 0, "only_a_sample": [], "only_b_sample": []}


def _build_cg_diff(a_path: Path, b_path: Path, rep: CompareReport) -> dict[str, Any]:
    """跑调用图抽取 + Jaccard (skip simrank, 跨架构会退化)."""
    try:
        a_cg = collect_call_graph(a_path, limit_files=1500)
        b_cg = collect_call_graph(b_path, limit_files=1500)
        cgd = compare_call_graphs(a_cg, b_cg, run_simrank=False)
        return cgd.to_dict()
    except Exception as e:
        rep.errors.append(f"call_graph failed: {e!s}")
        log.warning("compare_cg_failed", error=str(e)[:160])
        return _empty_cg_diff()


def _empty_cg_diff() -> dict[str, Any]:
    return {
        "a_nodes": 0, "a_edges": 0, "a_avg_out_degree": 0.0,
        "b_nodes": 0, "b_edges": 0, "b_avg_out_degree": 0.0,
        "node_jaccard": 0.0, "edge_jaccard": 0.0, "simrank": 0.0,
        "common_nodes_count": 0, "common_edges_count": 0,
        "callgraph_score": 0.0, "a_top_callees": [], "b_top_callees": [],
    }


def _safe_jaccard(set_diff: dict) -> float:
    """从 SetDiff.to_dict 取 jaccard, 但当 a 和 b 都为 0 时返回 0 (数据缺失视为 0 而非满分)."""
    a_count = set_diff.get("a_count", 0)
    b_count = set_diff.get("b_count", 0)
    if a_count == 0 and b_count == 0:
        return 0.0
    return float(set_diff.get("jaccard", 0.0))


def _safe_dir_jaccard(dir_diff: dict) -> float:
    """目录 Jaccard 的同款保护."""
    only_a = len(dir_diff.get("only_a", []))
    only_b = len(dir_diff.get("only_b", []))
    common = len(dir_diff.get("common", []))
    if only_a == 0 and only_b == 0 and common == 0:
        return 0.0
    return float(dir_diff.get("jaccard", 0.0))


def _safe_callgraph_score(cg_diff: dict) -> float:
    """调用图综合分: 两边都没节点 → 0.0."""
    if cg_diff.get("a_nodes", 0) == 0 and cg_diff.get("b_nodes", 0) == 0:
        return 0.0
    return float(cg_diff.get("callgraph_score", 0.0))


# --------------------------------------------------------------------------- #
# LLM 步骤 (模块语义 + novelty)
# --------------------------------------------------------------------------- #


def _normalize_describe(desc: dict) -> dict:
    """标准化 describe.json 字段名 (B 修复 jinja StrictUndefined 抛错).

    LLM 返回时 description / desc 混用, key_designs 里也有 description/desc 混用.
    这里统一成模板期望的 'desc' 字段, 缺失时填空字符串.
    """
    if not isinstance(desc, dict):
        return desc
    out = dict(desc)
    out["innovations"] = [_norm_item(x) for x in (desc.get("innovations") or [])]
    out["modules"] = [_normalize_module(m) for m in (desc.get("modules") or [])]
    return out


def _norm_item(item: dict) -> dict:
    """统一一个 dict 的 desc/description 字段, 加各种兜底."""
    if not isinstance(item, dict):
        return item
    out = dict(item)
    if "desc" not in out:
        out["desc"] = out.get("description") or out.get("note") or ""
    return out


def _normalize_module(mod: dict) -> dict:
    """模块下的 refs / data_structures / interfaces / key_designs 都要标准化.

    新 schema 主路径走 refs (顶层平结构), 旧 key_designs 兼容路径仍标准化.
    """
    if not isinstance(mod, dict):
        return mod
    out = dict(mod)
    for key in ("refs", "key_designs", "data_structures", "interfaces"):
        items = out.get(key) or []
        out[key] = [_norm_item(x) for x in items]
    return out


def _run_module_compares(client: DeepSeekClient, rep: CompareReport,
                         a_desc: dict, b_desc: dict) -> list[ModuleCompare]:
    """对 9 个模块各跑一次 compare_semantic LLM."""
    a_modules = _index_modules(a_desc)
    b_modules = _index_modules(b_desc)
    out: list[ModuleCompare] = []
    for name in _MODULE_ORDER:
        a_m = a_modules.get(name)
        b_m = b_modules.get(name)
        a_implemented = bool(a_m and not a_m.get("unimplemented"))
        b_implemented = bool(b_m and not b_m.get("unimplemented"))
        title_zh = _MODULE_TITLES_ZH.get(name, name)

        # 两边都没实现 → 直接 verdict=both_missing, 跳 LLM
        if not a_implemented and not b_implemented:
            out.append(ModuleCompare(
                name=name, title_zh=title_zh,
                a_implemented=False, b_implemented=False,
                verdict="both_missing",
            ))
            continue

        try:
            mc = _llm_module_compare(
                client, name, title_zh,
                a_repo=rep.a_repo, b_repo=rep.b_repo,
                a_implemented=a_implemented, b_implemented=b_implemented,
                a_module=a_m or _empty_module_dict(),
                b_module=b_m or _empty_module_dict(),
                rep=rep,
            )
        except Exception as e:
            rep.errors.append(f"module_compare {name} failed: {e!s}")
            log.warning("module_compare_failed", module=name, error=str(e)[:160])
            mc = ModuleCompare(
                name=name, title_zh=title_zh,
                a_implemented=a_implemented, b_implemented=b_implemented,
                verdict="similar", error=str(e)[:160],
            )
        out.append(mc)
    return out


def _llm_module_compare(client: DeepSeekClient, name: str, title_zh: str,
                        *, a_repo: str, b_repo: str,
                        a_implemented: bool, b_implemented: bool,
                        a_module: dict, b_module: dict,
                        rep: CompareReport) -> ModuleCompare:
    """单模块 LLM 对照."""
    msgs = render_messages(
        "compare_semantic.j2",
        module_name=name, module_title_zh=title_zh,
        a_repo=a_repo, b_repo=b_repo,
        a_implemented=a_implemented, b_implemented=b_implemented,
        a_module=a_module, b_module=b_module,
    )
    result: ChatResult = client.chat(
        messages=msgs,
        response_format={"type": "json_object"},
        temperature=0.0,
    )
    rep.total_prompt_tokens += result.prompt_tokens
    rep.total_completion_tokens += result.completion_tokens
    rep.total_reasoning_tokens += result.reasoning_tokens

    parsed = _parse_json_or_empty(result.content)
    _strip_invalid_compare_evidence(parsed, where=f"module/{name}")
    return ModuleCompare(
        name=name,
        title_zh=title_zh,
        a_implemented=a_implemented,
        b_implemented=b_implemented,
        a_summary=parsed.get("a_summary", "") or "",
        b_summary=parsed.get("b_summary", "") or "",
        verdict=parsed.get("verdict", "similar") or "similar",
        diff_points=parsed.get("diff_points", []) or [],
    )


def _run_novelty(client: DeepSeekClient, rep: CompareReport,
                 a_desc: dict, b_desc: dict) -> dict:
    """跑 novelty.j2 (v4-pro thinking, 给 B 相对 A 的关键差异)."""
    sim = rep.similarity
    if sim is None:
        return {}
    structural = _build_structural_summary(rep)
    try:
        msgs = render_messages(
            "novelty.j2",
            a_repo=rep.a_repo, b_repo=rep.b_repo,
            a_describe=a_desc, b_describe=b_desc,
            structural_summary=structural,
            sig_jaccard=sim.sig_jaccard,
            syscall_jaccard=sim.syscall_jaccard,
            deps_jaccard=sim.deps_jaccard,
            callgraph_jaccard=sim.callgraph_score,
            overall_score=sim.overall,
            similarity_label=sim.label,
        )
        result: ChatResult = client.chat(
            messages=msgs,
            response_format={"type": "json_object"},
            temperature=0.0,
            model="pro",
            thinking="enabled",
        )
        rep.total_prompt_tokens += result.prompt_tokens
        rep.total_completion_tokens += result.completion_tokens
        rep.total_reasoning_tokens += result.reasoning_tokens
        parsed = _parse_json_or_empty(result.content)
        _strip_invalid_compare_evidence(parsed, where="novelty")
        return parsed
    except Exception as e:
        rep.errors.append(f"novelty failed: {e!s}")
        log.warning("compare_novelty_failed", error=str(e)[:160])
        return {"summary": "", "verdict": "independent", "diffs": []}


def _build_structural_summary(rep: CompareReport) -> str:
    """把结构差异打包成一段中文文字 (供 novelty prompt 使用)."""
    a_basic = rep.a_basic
    b_basic = rep.b_basic
    parts = [
        f"A({rep.a_repo}, 家族={a_basic.get('family')}): "
        f"{'workspace ' + str(a_basic.get('member_count')) + ' 个成员' if a_basic.get('is_workspace') else '单 crate'}, "
        f"{a_basic.get('loc_files')} 文件 / {a_basic.get('loc_code')} 行, "
        f"实现 {a_basic.get('syscall_count')} 个 syscall.",
        f"B({rep.b_repo}, 家族={b_basic.get('family')}): "
        f"{'workspace ' + str(b_basic.get('member_count')) + ' 个成员' if b_basic.get('is_workspace') else '单 crate'}, "
        f"{b_basic.get('loc_files')} 文件 / {b_basic.get('loc_code')} 行, "
        f"实现 {b_basic.get('syscall_count')} 个 syscall.",
        f"目录 Jaccard={rep.dir_diff.get('jaccard')}, "
        f"A 仅有 {len(rep.dir_diff.get('only_a', []))} 个目录, "
        f"B 仅有 {len(rep.dir_diff.get('only_b', []))} 个目录, "
        f"共有 {len(rep.dir_diff.get('common', []))} 个目录.",
    ]
    return " ".join(parts)


def _index_modules(desc: dict) -> dict[str, dict]:
    """把 describe['modules'] 列表按 name 索引."""
    out: dict[str, dict] = {}
    for m in desc.get("modules", []) or []:
        n = m.get("name")
        if n:
            out[n] = m
    return out


def _empty_module_dict() -> dict:
    return {
        "summary": "",
        "narrative": "",        # 新增
        "refs": [],             # 新增
        "key_designs": [],      # 旧字段兜底
        "data_structures": [],
        "interfaces": [],
        "open_issues": [],      # 新增
        "unimplemented": True,
    }


def _parse_json_or_empty(text: str) -> dict:
    """从 LLM 输出里抽 JSON object, 失败返回空 dict.

    复用 describe 流水线的鲁棒解析 (严格 → 修复截断/尾随逗号 → 字段级抢救),
    让 compare 也受益于 JSON 修复三道防线.
    """
    if not text:
        return {}
    from oskag.describe.pipeline import _extract_json_object
    parsed = _extract_json_object(text)
    if parsed is not None:
        return parsed
    log.warning("compare_json_parse_failed", head=text[:120])
    return {}


def _strip_invalid_compare_evidence(parsed: dict, *, where: str = "") -> int:
    """丢弃 diff_points / diffs 里 a_evidence / b_evidence 起始行 < 1 的整条记录.

    背景: 与 describe._strip_invalid_evidence 同源 ([[fix-zero-line-rule]]).
    任意一侧 evidence 行号无效 → 丢整条 diff_point (而非只丢一侧, 因为 compare
    schema 要求两侧必填). 返回丢弃数, 永不抛.
    """
    from oskag.describe.pipeline import _evidence_lines_start

    dropped = 0

    def _ev_valid(ev: object) -> bool:
        if not isinstance(ev, dict):
            return False
        ls = _evidence_lines_start(ev.get("lines"))
        return ls is not None and ls >= 1

    for key in ("diff_points", "diffs"):
        items = parsed.get(key)
        if not isinstance(items, list):
            continue
        new_items: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                dropped += 1
                continue
            a_ev = item.get("a_evidence")
            b_ev = item.get("b_evidence")
            # 只要其中一侧给了 evidence 字段, 就要求合法 (compare 模板要求两侧都有)
            if (a_ev is not None and not _ev_valid(a_ev)) or \
               (b_ev is not None and not _ev_valid(b_ev)):
                dropped += 1
                continue
            new_items.append(item)
        parsed[key] = new_items

    if dropped:
        log.warning("compare_evidence_zero_line_dropped", where=where, dropped=dropped)
    return dropped


__all__ = [
    "CompareReport",
    "ModuleCompare",
    "SimilarityScore",
    "compare",
    "compute_similarity",
]
