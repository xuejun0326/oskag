"""facts/project_type.py — 从目录结构推断项目类型, 给 LLM 提供分析维度建议.

策略: 扫描目录名/文件名关键词, 匹配特征模式.
不抛异常, 未识别返回 "unknown".
"""
from __future__ import annotations

from pathlib import Path

from oskag.logging_setup import get_logger

log = get_logger("oskag.facts.project_type")


def infer_project_type(repo: Path) -> dict[str, str | list[str]]:
    """从仓库目录结构推断项目类型, 返回 {type, dimensions}.

    type: "compiler" | "database" | "network" | "storage" | "unknown"
    dimensions: 建议的分析维度列表 (给 LLM 参考, 非强制)
    """
    repo = Path(repo).resolve()
    if not repo.is_dir():
        return {"type": "unknown", "dimensions": []}

    # 收集目录名和文件名（递归扫描，限 depth=3 避免大仓库慢）
    dirs = set()
    files = set()
    for depth in range(3):
        for p in repo.rglob("*"):
            try:
                rel = p.relative_to(repo)
                if len(rel.parts) > depth + 1:
                    continue
                if p.is_dir():
                    dirs.add(p.name.lower())
                else:
                    files.add(p.name.lower())
            except (ValueError, OSError):
                continue

    # 特征检测（关键词权重累加）
    compiler_score = 0
    database_score = 0
    network_score = 0
    storage_score = 0

    # Compiler 特征
    compiler_keywords = ["parser", "ast", "ir", "rewriter", "codegen", "lexer", "semantic", "transform"]
    for kw in compiler_keywords:
        if any(kw in d for d in dirs):
            compiler_score += 2
        if any(kw in f for f in files):
            compiler_score += 1

    # Database 特征
    database_keywords = ["sql", "query", "executor", "storage", "transaction", "index", "btree", "catalog"]
    for kw in database_keywords:
        if any(kw in d for d in dirs):
            database_score += 2
        if any(kw in f for f in files):
            database_score += 1

    # Network 特征
    network_keywords = ["socket", "tcp", "udp", "http", "protocol", "packet", "netstack", "ethernet"]
    for kw in network_keywords:
        if any(kw in d for d in dirs):
            network_score += 2
        if any(kw in f for f in files):
            network_score += 1

    # Storage 特征
    storage_keywords = ["filesystem", "vfs", "inode", "block", "extent", "journal", "superblock"]
    for kw in storage_keywords:
        if any(kw in d for d in dirs):
            storage_score += 2
        if any(kw in f for f in files):
            storage_score += 1

    # 取分数最高的类型（阈值 3）
    scores = [
        (compiler_score, "compiler", ["AST 设计", "IR 转换", "pass 流水线", "codegen 策略", "类型系统"]),
        (database_score, "database", ["查询解析", "执行引擎", "存储层", "事务管理", "索引结构"]),
        (network_score, "network", ["协议栈设计", "状态机", "缓冲管理", "并发模型", "错误处理"]),
        (storage_score, "storage", ["文件系统架构", "块分配", "元数据管理", "日志与恢复", "缓存策略"]),
    ]
    scores.sort(reverse=True, key=lambda x: x[0])

    if scores[0][0] >= 3:
        ptype, dimensions = scores[0][1], scores[0][2]
        log.info("project_type_inferred", repo=repo.name, type=ptype, score=scores[0][0])
        return {"type": ptype, "dimensions": dimensions}
    else:
        log.info("project_type_unknown", repo=repo.name, max_score=scores[0][0])
        return {"type": "unknown", "dimensions": []}


__all__ = ["infer_project_type"]
