"""describe/pipeline.py — 描述流水线主编排.

总流程:
1. load_facts(repo) → facts.json (产物)
2. 切模块 → 每模块独立跑 describe_module() (agent tool-loop, 可主动 read_file)
3. synthesize() 汇总 → 总览 + 创新点
4. call_graph() → mermaid graph TD
5. verifier_sweep() → 每条 evidence 重读真实文件让 LLM 判 verdict
6. 返回 DescribeReport (供 render/markdown.py 渲染)

设计:
- 永不抛 (一个模块失败 → 该模块 unimplemented + warning, 其他继续)
- token / latency 累加, 整体可观测
- 0 真实 LLM 在单元测试 (用 fake client)
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from oskag.agent import Agent, AgentResult
from oskag.describe._modules import (
    MODULE_ORDER,
    MODULE_TITLES,
    ModuleSlice,
    discover_free_modules,
    module_files_for,
)
from oskag.describe.prompts import render_messages
from oskag.describe.token_budget import (
    DEFAULT_FILE_HEAD_LINES,
    DEFAULT_FILES_TOTAL_CHARS,
    boot_excerpt_text,
    file_excerpts_for,
    repomap_excerpt_for,
    split_repomap_by_module,
    syscalls_for_module,
)
from oskag.facts.profiles import KernelProfile, detect_family
from oskag.llm import DeepSeekClient
from oskag.logging_setup import get_logger
from oskag.tools.fs import read_file

log = get_logger("oskag.describe.pipeline")


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass
class ModuleReport:
    """一个模块的描述结果 (LLM 输出 + 元数据).

    重构: 从扁平 key_designs 列表改成叙述式 narrative + refs 索引.
    保留 data_structures / interfaces 作辅助索引.
    """
    name: str
    title_zh: str
    summary: str = ""
    narrative: str = ""                                              # 1500-2500 字章节叙述
    refs: list[dict] = field(default_factory=list)                   # [{id, file, lines, snippet, why}]
    data_structures: list[dict] = field(default_factory=list)        # [{name, file, line, ref_id, desc}]
    interfaces: list[dict] = field(default_factory=list)             # [{fn, file, line, ref_id, desc}]
    open_issues: list[str] = field(default_factory=list)             # 该模块的不足/疑问
    # 旧字段 (保留兼容 verifier / compare; 新流程不再填)
    key_designs: list[dict] = field(default_factory=list)
    unimplemented: bool = False
    # 元数据
    files_used: list[str] = field(default_factory=list)
    tool_calls: int = 0
    turns: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "title_zh": self.title_zh,
            "summary": self.summary,
            "narrative": self.narrative,
            "refs": self.refs,
            "data_structures": self.data_structures,
            "interfaces": self.interfaces,
            "open_issues": self.open_issues,
            "key_designs": self.key_designs,
            "unimplemented": self.unimplemented,
            "files_used": self.files_used,
            "tool_calls": self.tool_calls,
            "turns": self.turns,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "error": self.error,
        }


@dataclass
class VerifierVerdict:
    """单条 evidence 的校验结果."""
    module: str
    claim: str
    file: str
    lines: str
    verdict: str = "unrelated"   # support|partial|contradict|unrelated|skipped
    reason: str = ""
    suggested_lines: str = ""
    error: str | None = None


@dataclass
class DescribeReport:
    """整轮 describe 的产出, 喂给 render/markdown.py."""
    repo_name: str
    family: str
    facts: dict
    modules: list[ModuleReport] = field(default_factory=list)
    overview: str = ""
    synthesis: str = ""                                       # 新增: 综合评价 800-1500 字
    innovations: list[dict] = field(default_factory=list)     # 兜底字段, 新 prompt 不再返
    syscall_coverage_comment: str = ""
    rating: dict = field(default_factory=dict)
    call_graph_mermaid: str = ""
    verifier_verdicts: list[VerifierVerdict] = field(default_factory=list)
    # 元数据
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_reasoning_tokens: int = 0
    duration_sec: float = 0.0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """序列化为 dict, 供 compare 流水线读取."""
        return {
            "repo_name": self.repo_name,
            "family": self.family,
            "modules": [m.to_dict() for m in self.modules],
            "overview": self.overview,
            "synthesis": self.synthesis,
            "innovations": self.innovations,
            "syscall_coverage_comment": self.syscall_coverage_comment,
            "rating": self.rating,
            "call_graph_mermaid": self.call_graph_mermaid,
            "verifier_verdicts": [
                {
                    "module": v.module,
                    "claim": v.claim,
                    "file": v.file,
                    "lines": v.lines,
                    "verdict": v.verdict,
                    "reason": v.reason,
                    "suggested_lines": v.suggested_lines,
                    "error": v.error,
                }
                for v in self.verifier_verdicts
            ],
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_reasoning_tokens": self.total_reasoning_tokens,
            "duration_sec": self.duration_sec,
            "warnings": self.warnings,
            "errors": self.errors,
            # facts 不进 json (太大, 且 compare 流水线会另外加载 facts.json)
        }


# --------------------------------------------------------------------------- #
# 工具集 (供 describe_module 的 agent tool-loop 使用)
# --------------------------------------------------------------------------- #


_READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "读取仓库内某文件指定行范围. 注意 file 必须在 file_excerpts 列出的范围内或同模块目录下.",
        "parameters": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "相对仓库根的 posix 路径"},
                "start_line": {"type": "integer", "description": "起始行号 (1-based, 含)"},
                "end_line": {"type": "integer", "description": "结束行号 (1-based, 含)"},
            },
            "required": ["file"],
        },
    },
}


def _make_read_file_handler(repo_root: Path, allowed_files: set[str]):
    """构造 read_file 处理器, 限制只能读 allowed_files 内的文件 (反幻觉)."""
    repo_root = repo_root.resolve()

    def handler(args: dict) -> dict:
        rel = args.get("file", "").strip()
        if not rel:
            return {"ok": False, "error": "file 参数为空"}
        if rel not in allowed_files:
            return {
                "ok": False,
                "error": f"文件 {rel} 不在允许列表内. 只能读 file_excerpts 中已列出的文件.",
            }

        target = (repo_root / rel).resolve()
        try:
            target.relative_to(repo_root)
        except ValueError:
            return {"ok": False, "error": "路径越权"}

        fr = read_file(target)
        if fr.error:
            return {"ok": False, "error": fr.error}

        lines = fr.text.splitlines()
        # ([[fix-zero-line-rule]] H1) start_line < 1 不静默改 1, 让 LLM 收到明确错误
        # 旧实现 start = max(1, int(args.get("start_line", 1))) 是隐藏失败 (违反 R6),
        # LLM 给 :0 后会拿到看似合法的片段, 然后把 :0-0 写进 evidence.
        try:
            start = int(args.get("start_line", 1))
        except (TypeError, ValueError):
            return {"ok": False, "error": "start_line 必须是整数"}
        if start < 1:
            return {"ok": False, "error": f"start_line({start}) 必须 ≥ 1; 不要用 0 占位"}
        try:
            end = int(args.get("end_line", min(start + 200, len(lines))))
        except (TypeError, ValueError):
            return {"ok": False, "error": "end_line 必须是整数"}
        end = min(len(lines), end)
        if end < start:
            return {"ok": False, "error": f"end_line({end}) < start_line({start})"}

        snippet = "\n".join(lines[start - 1:end])
        # 单次 read 上限 6000 字符
        if len(snippet) > 6000:
            snippet = snippet[:6000] + "\n... (truncated by tool)"
        return {"file": rel, "lines": f"{start}-{end}", "content": snippet}

    return handler


# --------------------------------------------------------------------------- #
# JSON 解析容错
# --------------------------------------------------------------------------- #


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL)


def _extract_json_object(text: str) -> dict | None:
    """从 LLM 输出中尽力提取一个 JSON object.

    优先按 ``` 代码块剥; 失败再按第一个 { 到最后一个 } 暴力截取;
    再失败尝试修复常见 JSON 错误 (尾随逗号 / 截断未闭合).
    全部失败返回 None.
    """
    if not text:
        return None
    candidates: list[str] = []
    fence = _JSON_FENCE_RE.search(text)
    if fence:
        candidates.append(fence.group(1).strip())
    # 暴力截取
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last > first:
        candidates.append(text[first:last + 1])
    candidates.append(text.strip())

    # 第一轮: 严格解析
    for c in candidates:
        try:
            parsed = json.loads(c)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue

    # 第二轮: 修复后再解析 (尾随逗号 / 截断的 JSON 补全闭合)
    for c in candidates:
        repaired = _repair_json(c)
        if repaired is None:
            continue
        try:
            parsed = json.loads(repaired)
            if isinstance(parsed, dict):
                log.info("json_repaired", original_len=len(c), repaired_len=len(repaired))
                return parsed
        except json.JSONDecodeError:
            continue

    # 第三轮: 字段级抢救 (snippet 里有未转义字符导致结构无法修复时,
    # 至少抠出 narrative/summary/module, 走正常渲染路径而非降级)
    salvaged = _salvage_fields(text)
    if salvaged:
        log.info("json_field_salvaged", fields=list(salvaged.keys()))
        return salvaged
    return None


# 匹配 JSON 字符串字段值: "key": "....." (正确处理 \" 转义)
def _string_field_re(key: str) -> re.Pattern[str]:
    return re.compile(r'"' + key + r'"\s*:\s*"((?:[^"\\]|\\.)*)"', re.DOTALL)


_SALVAGE_KEYS = ("module", "summary", "narrative")


def _salvage_fields(text: str) -> dict | None:
    """从无法解析/修复的 JSON 里用正则抠出关键字符串字段.

    只抢救 _SALVAGE_KEYS 中的字符串字段 (narrative 是核心, 缺它才需要降级).
    抢救到 narrative 才返回 (其余字段尽力而为); refs 等结构化字段放弃 (置空).
    返回 None 表示连 narrative 都没抠到.
    """
    if not text:
        return None
    out: dict = {}
    for key in _SALVAGE_KEYS:
        m = _string_field_re(key).search(text)
        if not m:
            continue
        # 用 json.loads 还原转义 (\n \" \\ 等)
        try:
            out[key] = json.loads('"' + m.group(1) + '"')
        except json.JSONDecodeError:
            out[key] = m.group(1)  # 还原失败就用原文
    if "narrative" not in out or not out["narrative"]:
        return None
    # 补齐结构字段为空, 让下游渲染不报错
    out.setdefault("refs", [])
    out.setdefault("data_structures", [])
    out.setdefault("interfaces", [])
    out.setdefault("open_issues", [])
    return out


def _repair_json(text: str) -> str | None:
    """尽力修复被截断或含小错误的 JSON object.

    处理两类常见错误:
    1. 尾随逗号: ``{"a":1,}`` / ``[1,2,]``
    2. 截断: LLM 写到一半 stop, 留下未闭合的 string / array / object.
       策略: 找到最后一个"完整键值对结束"的位置 (一个 , 或闭合符之后),
       在那里截断, 去尾随逗号, 补齐剩余闭合符号.

    返回修复后的字符串 (可能仍解析失败), 无法修复返回 None.
    """
    if not text:
        return None
    start = text.find("{")
    if start == -1:
        return None
    s = text[start:]

    state = _scan_json(s)
    # 情况 1: 字符串闭合且括号平衡 — 只需去尾随逗号
    if not state["in_str"] and not state["depth"]:
        return _strip_trailing_commas(s)

    # 情况 2: 截断 — 在最后一个完整值断点处截断后补齐
    cut = state["last_value_end"]
    if cut <= 0:
        return None
    truncated = _strip_trailing_commas(s[:cut + 1])
    return truncated + _compute_closers(truncated)


def _scan_json(s: str) -> dict:
    """单次扫描 JSON 文本, 返回 {in_str, depth, last_value_end}.

    last_value_end: 最后一个"值结束"位置的索引 — 仅在遇到 , 或闭合符 } ] 时更新,
    保证截断点之后一定是合法的"键值对边界"(不会停在裸 key 后).
    """
    in_str = False
    escape = False
    depth = 0
    last_value_end = -1
    for i, ch in enumerate(s):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            last_value_end = i
        elif ch == ",":
            last_value_end = i - 1  # 逗号前一个字符是值的结尾
    return {"in_str": in_str, "depth": depth, "last_value_end": last_value_end}


def _strip_trailing_commas(s: str) -> str:
    """去掉 ``,}`` / ``,]`` 中的尾随逗号 (简化处理, 字符串内的极少见)."""
    return re.sub(r",(\s*[}\]])", r"\1", s)


def _compute_closers(s: str) -> str:
    """扫描 s, 返回需要补齐的闭合符序列 (顺序正确)."""
    stack: list[str] = []
    in_str = False
    escape = False
    for ch in s:
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]" and stack:
            stack.pop()
    return "".join(reversed(stack))


# --------------------------------------------------------------------------- #
# 反幻觉: 行号合法性过滤 ([[fix-zero-line-rule]])
# --------------------------------------------------------------------------- #


_LINES_RANGE_RE = re.compile(r"^\s*(\d+)\s*[-~]\s*(\d+)\s*$")
_LINES_SINGLE_RE = re.compile(r"^\s*(\d+)\s*$")


def _evidence_lines_start(lines_spec: object) -> int | None:
    """解析 evidence.lines 字段, 返回起始行号 int.

    - 接受 "65-100" / "65~100" / "65" / 整数 65
    - 解析失败返回 None (调用方按"无效"处理)
    """
    if isinstance(lines_spec, int):
        return lines_spec
    if not isinstance(lines_spec, str):
        return None
    m = _LINES_RANGE_RE.match(lines_spec) or _LINES_SINGLE_RE.match(lines_spec)
    if not m:
        return None
    try:
        return int(m.group(1))
    except (ValueError, IndexError):
        return None


def _is_line_valid(value: object) -> bool:
    """判断 evidence.lines / data_struct.line / interface.line 字段是否合法 (起始行 ≥ 1)."""
    ls = _evidence_lines_start(value)
    return ls is not None and ls >= 1


def _filter_by_line_field(items: object, line_key: str) -> tuple[list[dict], int]:
    """过滤 items 中 line_key 字段不合法的元素, 返回 (新列表, 丢弃数)."""
    new_items: list[dict] = []
    dropped = 0
    for it in items or []:
        if not isinstance(it, dict) or not _is_line_valid(it.get(line_key)):
            dropped += 1
            continue
        new_items.append(it)
    return new_items, dropped


def _filter_key_design(kd: object) -> tuple[dict | None, int]:
    """过滤一条 key_design 的 evidence 列表; evidence 全空则整条丢弃.

    返回 (新 key_design 或 None, 丢弃的 evidence 数).

    改造后, key_designs 已不再是新 prompt 主输出 (走 narrative + refs[]).
    这个函数保留, 仅作为兜底: 如果 LLM 不按新 schema 而仍返回 key_designs,
    我们至少不让无效行号通过.
    """
    if not isinstance(kd, dict):
        return None, 0
    evidences = kd.get("evidence") or []
    valid_evs = [
        ev for ev in evidences
        if isinstance(ev, dict) and _is_line_valid(ev.get("lines"))
    ]
    dropped_evs = len(evidences) - len(valid_evs)
    if not valid_evs:
        return None, dropped_evs
    kd["evidence"] = valid_evs
    return kd, dropped_evs


def _strip_invalid_evidence(parsed: dict, *, module: str = "") -> dict[str, int]:
    """就地丢弃 line_start < 1 的 refs / data_structure / interface (+ 旧 key_designs 兜底).

    背景: LLM 拿不到具体行号时会用 :0-0 占位 ([[fix-zero-line-rule]] 根因),
    citation_validator 后续会判 fail. 这里在 parse 阶段先静默清理, 不重发 LLM
    (重发 1 次成本翻倍且不一定能修, 不如让 LLM 下次跑被 prompt 拦下来).

    schema: refs[] 是顶层平结构, 每条带 `lines` 字段. data_structures /
    interfaces 仍是平结构带 `line` 字段. key_designs[] 不再使用, 但保留兜底
    清理路径以防 LLM 偶发回退到旧 schema.

    返回丢弃计数 dict 供 log; 永不抛.
    """
    counts = {
        "refs_dropped": 0,
        "evidence_dropped": 0,        # 旧 schema 兜底
        "key_design_dropped": 0,      # 旧 schema 兜底
        "data_struct_dropped": 0,
        "interface_dropped": 0,
    }

    # 1. refs (新 schema 主路径): 顶层平结构, 走通用过滤器, key=lines
    if "refs" in parsed:
        parsed["refs"], counts["refs_dropped"] = _filter_by_line_field(
            parsed.get("refs"), "lines"
        )

    # 2. key_designs (旧 schema 兜底): 嵌套 evidence, 单独处理
    if "key_designs" in parsed:
        new_kds: list[dict] = []
        for kd in parsed.get("key_designs") or []:
            cleaned, dropped_evs = _filter_key_design(kd)
            counts["evidence_dropped"] += dropped_evs
            if cleaned is None:
                counts["key_design_dropped"] += 1
            else:
                new_kds.append(cleaned)
        parsed["key_designs"] = new_kds

    # 3/4. data_structures + interfaces: 平结构, 走通用过滤器, key=line
    if "data_structures" in parsed:
        parsed["data_structures"], counts["data_struct_dropped"] = _filter_by_line_field(
            parsed.get("data_structures"), "line"
        )
    if "interfaces" in parsed:
        parsed["interfaces"], counts["interface_dropped"] = _filter_by_line_field(
            parsed.get("interfaces"), "line"
        )

    if any(counts.values()):
        log.warning("evidence_zero_line_dropped", module=module, **counts)
    return counts


# --------------------------------------------------------------------------- #
# 单模块描述 — helpers
# --------------------------------------------------------------------------- #


def _degraded_module_narrative(
    *,
    client: DeepSeekClient,
    module: str,
    title: str,
    repomap_excerpt: str,
    file_excerpts: list[dict],
    model: str,
) -> str:
    """降级兜底: 当主路径 JSON 解析失败时, 再跑一次 LLM 直接出纯 Markdown.

    设计要点:
    - thinking=disabled (上下文已大, 关思考避免 token 被推理吃光)
    - 不要 response_format=json (允许自由 Markdown)
    - 不允许 tool 调用 (一次性)
    - prompt 极短: 只让 LLM 写一段 ≈800 字 Markdown 分析, 不要 narrative+refs 双结构

    返回: Markdown 字符串 (1-2K 字), 失败抛异常由调用方 catch.
    """
    # 简短 file 上下文: 只取前 3 个文件的头 80 行
    file_blob_parts: list[str] = []
    for ex in file_excerpts[:3]:
        head = "\n".join(ex.get("content", "").splitlines()[:80])
        file_blob_parts.append(f"### `{ex['file']}`\n```\n{head}\n```")
    file_blob = "\n\n".join(file_blob_parts)

    sys_msg = (
        "你是一名 OS 内核代码评审专家. 用户上一次让你按严格 JSON schema 写"
        f"{title}模块的分析, 但 JSON 解析失败. 现在改用最简模式: 直接给一段 800-1200 字的"
        "中文 Markdown 分析, 不要任何 JSON 结构. 内容包括 (各段 1-2 自然段):\n"
        "1. 模块在仓库中的定位 (做什么 / 在哪些文件)\n"
        "2. 一两个核心抽象或设计取舍\n"
        "3. 代码中能看到的具体不足或 TODO (≥2 条)\n"
        "4. 跨文件协同 (如果能看到)\n"
        "**硬要求**: 行号引用必须以 `path:line` 形式给出, 不要瞎编. 如果不确定行号宁可不给.\n"
        "**降级声明**: 段首加一行 `> ⚠ 此章为 JSON 解析失败后的降级兜底分析, 引证较少`."
    )
    user_msg = (
        f"模块: {title} ({module})\n\n"
        f"## 仓库 repomap (与本模块相关段)\n\n```\n{repomap_excerpt[:3000]}\n```\n\n"
        f"## 模块内主要文件 (头 80 行)\n\n{file_blob[:8000]}\n\n"
        "请直接写 800-1200 字 Markdown 分析."
    )
    result = client.chat(
        [
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": user_msg},
        ],
        model=model,
        thinking="disabled",
        temperature=0.0,
        max_tokens=4096,
    )
    return (result.content or "").strip()


def _detect_baseline_module(
    repo: Path, profile: KernelProfile, module: str
) -> dict[str, Any] | None:
    """baseline 探针: 检测仓库是否在 ignore_paths 下沿用了基线模块.

    例: Undefined-OS 把 `arceos/` 加入 ignore_paths, 但 `arceos/modules/axdriver/`
    实际上是仓库选择"沿用基线驱动框架". `module_files_for` 把这些文件 ignore 掉
    后会判 unimplemented; 这里二次探针, 命中时返回 metadata.

    返回 dict (命中) 或 None (未命中).
    """
    family = (profile.family or "").lower()
    if not family.startswith("arceos"):
        return None  # 当前只支持 arceos 家族, rcore 暂无内置基线概念

    arceos_module_map = {
        "drivers": "arceos/modules/axdriver",
        "fs":      "arceos/modules/axfs",
        "net":     "arceos/modules/axnet",
        "signal":  "arceos/modules/axsignal",
        "task":    "arceos/modules/axtask",
        "mm":      "arceos/modules/axmm",
        "boot":    "arceos/modules/axhal",
    }
    baseline_path = arceos_module_map.get(module)
    if not baseline_path:
        return None

    target = repo / baseline_path
    if not target.is_dir():
        return None

    rs_files = list(target.rglob("*.rs"))
    if not rs_files:
        return None

    sample_files = sorted(
        str(p.relative_to(repo)).replace("\\", "/")
        for p in rs_files[:5]
    )
    return {
        "family_label": "ArceOS",
        "baseline_path": baseline_path,
        "file_count": len(rs_files),
        "sample_files": sample_files,
    }


def _try_fill_from_baseline(
    *,
    report: ModuleReport,
    repo: Path,
    profile: KernelProfile,
    module: str,
    title: str,
) -> bool:
    """探针命中时填充 report 的 summary + narrative, 返回 True.

    未命中返回 False, 调用方按原逻辑判 unimplemented.
    """
    baseline_hit = _detect_baseline_module(repo, profile, module)
    if not baseline_hit:
        return False

    family_label = baseline_hit["family_label"]
    baseline_path = baseline_hit["baseline_path"]
    file_count = baseline_hit["file_count"]
    sample_files = baseline_hit["sample_files"]

    report.unimplemented = False
    report.summary = (
        f"本仓库未在自家代码中重写 {title}, 而是直接沿用基线 "
        f"`{family_label}` 在 `{baseline_path}` 下的实现 "
        f"({file_count} 个 .rs 文件). 详见基线项目, 本仓未做实质性修改."
    )
    report.narrative = (
        "## 基线沿用说明\n\n"
        f"扫描 `profile.module_paths[{module}]` 在本仓内未匹配到自家代码, "
        f"但在被 `ignore_paths` 排除的 `{baseline_path}` 下存在 "
        f"{file_count} 个 .rs 文件 (例: "
        f"{', '.join(sample_files[:3])}). "
        f"这表明该仓选择**直接沿用 {family_label} 基线**, "
        "而不是从零重写本模块. 这是合理的工程取舍 — "
        "基线已稳定的模块没必要重新发明轮子.\n\n"
        "**评价**: 此处不能简单计为'未实现'. 本仓的工作重心在其他模块. "
        "若需评估完整度, 应看基线项目本身."
    )
    log.info(
        "module_baseline_used", module=module, repo=repo.name,
        baseline_path=baseline_path, file_count=file_count,
    )
    return True





def _try_degraded_fallback(
    *,
    report: ModuleReport,
    client: DeepSeekClient,
    module: str,
    title: str,
    repomap_excerpt: str,
    file_excerpts: list[dict],
    model: str,
    content_preview: str,
) -> None:
    """主路径 JSON 解析失败时调用. 写日志 → 跑降级 → 把结果塞 narrative.

    永不抛异常, 失败时只 log.
    """
    report.error = "json_parse_failed"
    log.warning(
        "module_json_parse_failed", module=module,
        content_preview=content_preview,
    )
    try:
        fallback = _degraded_module_narrative(
            client=client, module=module, title=title,
            repomap_excerpt=repomap_excerpt, file_excerpts=file_excerpts,
            model=model,
        )
    except Exception as e:
        log.warning(
            "module_degraded_fallback_failed",
            module=module, error=f"{type(e).__name__}: {e!s}"[:160],
        )
        return

    if fallback:
        report.narrative = fallback
        report.error = "json_parse_failed_with_fallback"
        log.info(
            "module_degraded_fallback_ok",
            module=module, narrative_chars=len(fallback),
        )


def describe_module(
    module: str,
    facts: dict,
    profile: KernelProfile,
    repo: Path,
    client: DeepSeekClient,
    *,
    repomap_sections: dict[str, str] | None = None,
    module_slice: ModuleSlice | None = None,
    model: str = "pro",
    max_turns: int = 10,
    files_total_chars: int = DEFAULT_FILES_TOTAL_CHARS,
) -> ModuleReport:
    """跑一个模块的 describe (含 agent tool-loop). 永不抛.

    module_slice: 可选预构建的文件切片 (自由模式下由 discover_free_modules 提供).
                  若为 None, 调用 module_files_for 按固定内核模块路径查找.
    """
    title = MODULE_TITLES.get(module, {}).get("zh", module)
    report = ModuleReport(name=module, title_zh=title)

    # 1. 模块文件切片
    if module_slice is not None:
        slice_ = module_slice
    else:
        slice_ = module_files_for(profile, module, repo)
    if slice_.is_empty:
        # 二次校验 — 是否仅是仓库选择"沿用基线" (axdriver / axfs / ...)?
        if _try_fill_from_baseline(report=report, repo=repo, profile=profile,
                                   module=module, title=title):
            return report
        report.unimplemented = True
        report.summary = f"本仓库未发现 {title} 模块的实现。"
        log.info("module_unimplemented", module=module, repo=repo.name)
        return report

    # 2. 预算切片
    if repomap_sections is None:
        repomap_sections = split_repomap_by_module(facts.get("repomap", ""))
    repomap_excerpt = repomap_excerpt_for(repomap_sections, module)
    syscalls_excerpt = syscalls_for_module(
        facts.get("syscalls", {}).get("items", []), module
    )
    boot_excerpt = boot_excerpt_text(facts.get("boot", {})) if module == "boot" else ""

    # 自由模式下放宽文件预算 (没有 syscall/boot 上下文, 需要更多代码)
    if profile.family == "unknown":
        file_head_lines = 800
        file_total_chars = min(80000, files_total_chars * 2)
    else:
        file_head_lines = DEFAULT_FILE_HEAD_LINES
        file_total_chars = files_total_chars

    file_excerpts = file_excerpts_for(
        slice_, repo, head_lines=file_head_lines, max_total_chars=file_total_chars
    )
    report.files_used = [ex["file"] for ex in file_excerpts]

    # 3. 渲染 prompt
    try:
        messages = render_messages(
            "describe_module.j2",
            module_name=module,
            module_title_zh=title,
            family=profile.family,
            repo_name=profile.repo_name,
            file_excerpts=file_excerpts,
            repomap_excerpt=repomap_excerpt,
            syscalls_excerpt=syscalls_excerpt,
            boot_excerpt=boot_excerpt,
            project_type=facts.get("project_type", {"type": "unknown", "dimensions": []}),
        )
    except Exception as e:
        report.error = f"prompt_render_failed: {e!s}"[:160]
        log.warning("module_prompt_failed", module=module, error=report.error)
        return report

    # 4. 跑 agent (允许它再 read_file 已读文件之外的更多片段)
    allowed_files = {ex["file"] for ex in file_excerpts}
    tools = [_READ_FILE_TOOL]
    handlers = {"read_file": _make_read_file_handler(repo, allowed_files)}
    agent = Agent(client, tools=tools, tool_handlers=handlers)

    try:
        agent_result: AgentResult = agent.run(
            list(messages),
            model=model,
            max_turns=max_turns,
            thinking="enabled",  # pro 模型默认开 thinking, flash 会被 client 自动忽略
            response_format={"type": "json_object"},
            temperature=0.0,
        )
    except Exception as e:
        report.error = f"agent_failed: {e!s}"[:200]
        log.warning("module_agent_failed", module=module, error=report.error)
        return report

    report.turns = agent_result.turns
    report.tool_calls = agent_result.tool_calls_made
    report.prompt_tokens = agent_result.prompt_tokens
    report.completion_tokens = agent_result.completion_tokens

    # 5. 解析 JSON
    parsed = _extract_json_object(agent_result.content)
    if not parsed:
        # 降级兜底 — 见 _try_degraded_fallback. 失败也不抛, 让上层继续.
        _try_degraded_fallback(
            report=report, client=client, module=module, title=title,
            repomap_excerpt=repomap_excerpt, file_excerpts=file_excerpts,
            model=model, content_preview=agent_result.content[:200],
        )
        return report

    # 5b. 反幻觉: 丢弃 line < 1 的 refs / data_struct / interface ([[fix-zero-line-rule]])
    _strip_invalid_evidence(parsed, module=module)

    # 5c. refs[].lines grep 二次定位 (LLM 凭记忆给的行号偏 1-12 行,
    # 这里用 snippet 在源码里 grep 真实起始行覆盖. 见 _ref_lines_fix.py)
    _refs = parsed.get("refs") or []
    if _refs:
        from oskag.describe._ref_lines_fix import fix_ref_lines
        fix_ref_lines(_refs, repo, module_name=module)

    # 新 schema: narrative + refs + open_issues
    report.summary = parsed.get("summary", "")
    report.narrative = parsed.get("narrative", "") or ""
    report.refs = parsed.get("refs", []) or []
    report.open_issues = parsed.get("open_issues", []) or []
    report.data_structures = parsed.get("data_structures", []) or []
    report.interfaces = parsed.get("interfaces", []) or []
    report.unimplemented = bool(parsed.get("unimplemented", False))
    # 旧字段兜底: 如果 LLM 偶发返回 key_designs 也保留, 但新流程不依赖
    report.key_designs = parsed.get("key_designs", []) or []

    log.info(
        "module_done",
        module=module,
        turns=report.turns,
        tool_calls=report.tool_calls,
        narrative_chars=len(report.narrative),
        refs=len(report.refs),
        open_issues=len(report.open_issues),
        prompt_tokens=report.prompt_tokens,
    )
    return report


# --------------------------------------------------------------------------- #
# 综合 (synthesize)
# --------------------------------------------------------------------------- #


_FAMILY_BASELINE: dict[str, str] = {
    "arceos-starry": (
        "ArceOS 标准提供 axhal/axtask/axmm/axfs/axnet/axdriver 模块化 crate; "
        "syscall 通过 Sysno 枚举 + register_trap_handler 分发; "
        "boot 由 axhal::platform 处理硬件初始化."
    ),
    "rcore-tutorial": (
        "rCore-Tutorial 为单 crate 教学内核, 标准实现 SV39 三级页表 + 简单 buddy 分配, "
        "用 const SYSCALL_X 宏分发 syscall, boot 由 entry.S → rust_main 接力, "
        "通常无独立 IPC/网络模块."
    ),
    "unknown": "无标准基线参考, 直接客观描述实现.",
}


def synthesize(
    profile: KernelProfile,
    facts: dict,
    modules: list[ModuleReport],
    client: DeepSeekClient,
    *,
    model: str = "pro",
    thinking: str | None = "enabled",
) -> dict:
    """跑 synthesize 模板, 返回 {overview, innovations, ...}. 永不抛."""
    basic_facts = {
        "syscall_count": facts.get("syscalls", {}).get("count", 0),
        "syscall_by_domain": facts.get("syscalls", {}).get("by_domain", {}),
        "loc_files": facts.get("repo_stats", {}).get("loc", {}).get("total_files"),
        "loc_code": facts.get("repo_stats", {}).get("loc", {}).get("total_code"),
        "is_workspace": facts.get("cargo", {}).get("is_workspace"),
        "members": [m.get("name") for m in facts.get("cargo", {}).get("members", [])],
        "trap_handlers": [h.get("kind") for h in facts.get("boot", {}).get("handlers", [])],
        "boot_style": facts.get("boot", {}).get("boot_style"),
    }
    family_baseline = _FAMILY_BASELINE.get(profile.family, _FAMILY_BASELINE["unknown"])

    module_dicts = [
        {
            "name": m.name,
            "title_zh": m.title_zh,
            "summary": m.summary,
            "narrative": m.narrative,                         # 新增
            "refs": m.refs,                                    # 新增
            "data_structures": m.data_structures,              # 新增
            "interfaces": m.interfaces,                        # 新增
            "open_issues": m.open_issues,                      # 新增
            "key_designs": m.key_designs,                      # 旧字段, 兜底
            "unimplemented": m.unimplemented,
        }
        for m in modules
    ]

    try:
        messages = render_messages(
            "synthesize.j2",
            repo_name=profile.repo_name,
            family=profile.family,
            basic_facts=json.dumps(basic_facts, ensure_ascii=False, indent=2),
            family_baseline=family_baseline,
            module_results=module_dicts,
        )
    except Exception as e:
        log.warning("synthesize_render_failed", error=str(e)[:160])
        return {}

    try:
        chat = client.chat(
            messages,
            model=model,
            thinking=thinking,
            response_format={"type": "json_object"},
            temperature=0.0,
        )
    except Exception as e:
        log.warning("synthesize_call_failed", error=str(e)[:160])
        return {}

    parsed = _extract_json_object(chat.content)
    if not parsed:
        log.warning("synthesize_parse_failed", preview=chat.content[:200])
        return {
            "_tokens": {"prompt": chat.prompt_tokens, "completion": chat.completion_tokens,
                        "reasoning": chat.reasoning_tokens},
        }

    parsed["_tokens"] = {
        "prompt": chat.prompt_tokens,
        "completion": chat.completion_tokens,
        "reasoning": chat.reasoning_tokens,
    }
    return parsed


# --------------------------------------------------------------------------- #
# 自由模式: 章节命名 (name_free_modules)
# --------------------------------------------------------------------------- #


def name_free_modules(
    repo_name: str,
    discovered: dict[str, ModuleSlice],
    client: DeepSeekClient,
    *,
    model: str = "flash",
) -> dict[str, dict]:
    """自由模式: 用 LLM 把确定性发现的候选目录映射成中文章节标题+简介+排序.

    返回 {key: {"title_zh": ..., "summary": ..., "order": int}, ...}.
    失败时返回空 dict (外层降级用英文 key 当标题). 永不抛.
    """
    if not discovered:
        return {}

    candidates = []
    for key, slice_ in discovered.items():
        sample = [str(f.name) for f in slice_.files[:5]] if slice_.files else []
        candidates.append({
            "key": key,
            "file_count": len(slice_.files),
            "sample_files": sample,
        })

    try:
        messages = render_messages(
            "free_modules.j2",
            repo_name=repo_name,
            candidates=candidates,
        )
    except Exception as e:
        log.warning("free_modules_prompt_failed", error=str(e)[:120])
        return {}

    try:
        chat = client.chat(messages, model=model, temperature=0.0)
    except Exception as e:
        log.warning("free_modules_call_failed", error=str(e)[:160])
        return {}

    parsed = _extract_json_object(chat.content)
    if not parsed or "modules" not in parsed:
        log.warning("free_modules_parse_failed", preview=chat.content[:200])
        return {}

    # 转成 {key: {title_zh, summary, order}}
    result = {}
    for item in parsed.get("modules", []):
        key = item.get("key")
        if key and key in discovered:
            result[key] = {
                "title_zh": item.get("title_zh", key),
                "summary": item.get("summary", ""),
                "order": item.get("order", 99),
            }

    log.info(
        "free_modules_named",
        repo=repo_name,
        count=len(result),
        prompt_tokens=chat.prompt_tokens,
        completion_tokens=chat.completion_tokens,
    )
    return result


# --------------------------------------------------------------------------- #
# 调用图 (call_graph)
# --------------------------------------------------------------------------- #


_MERMAID_FENCE_RE = re.compile(r"```mermaid\s*(.+?)```", re.DOTALL)


def call_graph(
    profile: KernelProfile,
    facts: dict,
    client: DeepSeekClient,
    *,
    model: str = "pro",
) -> tuple[str, dict]:
    """返回 (mermaid 文本, _tokens dict). 失败返回空 mermaid."""
    boot = facts.get("boot", {}) or {}
    if not boot.get("entry_file"):
        return "", {}

    try:
        messages = render_messages(
            "call_graph.j2",
            repo_name=profile.repo_name,
            entry_file=boot.get("entry_file") or "",
            entry_fn=boot.get("entry_fn") or "main",
            entry_line=boot.get("entry_line") or 0,
            call_chain=(boot.get("call_chain") or [])[:12],
            handlers=boot.get("handlers") or [],
        )
    except Exception as e:
        log.warning("call_graph_render_failed", error=str(e)[:160])
        return "", {}

    try:
        chat = client.chat(messages, model=model, temperature=0.0)
    except Exception as e:
        log.warning("call_graph_call_failed", error=str(e)[:160])
        return "", {}

    tokens = {
        "prompt": chat.prompt_tokens, "completion": chat.completion_tokens,
        "reasoning": chat.reasoning_tokens,
    }
    fence = _MERMAID_FENCE_RE.search(chat.content)
    if fence:
        return f"```mermaid\n{fence.group(1).strip()}\n```", tokens
    # LLM 没用 fence — 回退原样
    return chat.content.strip(), tokens


# --------------------------------------------------------------------------- #
# verifier 扫描
# --------------------------------------------------------------------------- #


def _read_file_lines(repo_root: Path, file_rel: str, lines_spec: str) -> str | None:
    """根据 "x-y" 行号规格从真实文件读片段. 失败返回 None."""
    target = (repo_root / file_rel).resolve()
    try:
        target.relative_to(repo_root.resolve())
    except ValueError:
        return None
    fr = read_file(target)
    if fr.error or not fr.text:
        return None
    lines = fr.text.splitlines()

    m = re.match(r"^\s*(\d+)\s*[-~]\s*(\d+)\s*$", lines_spec or "")
    if m:
        start, end = int(m.group(1)), int(m.group(2))
    else:
        m1 = re.match(r"^\s*(\d+)\s*$", lines_spec or "")
        if not m1:
            return None
        start = int(m1.group(1))
        end = start
    # ([[fix-zero-line-rule]]) start=0 不静默改成 1, 直接判失败让 verifier 拿到 file_or_lines_invalid
    if start < 1:
        return None
    end = min(len(lines), max(start, end))
    return "\n".join(lines[start - 1:end])


def _collect_module_evidences(m: ModuleReport) -> list[tuple[str, str, str]]:
    """从 ModuleReport 抽 verifier 要校验的 (claim, file, lines) 列表.

    主路径走 m.refs[] (新 schema). 若 refs 为空, 兜底走 m.key_designs (旧 schema).
    """
    out: list[tuple[str, str, str]] = []

    # 新 schema 主路径
    for ref in m.refs:
        if not isinstance(ref, dict):
            continue
        claim = (ref.get("why") or "").strip()
        out.append((claim, ref.get("file") or "", str(ref.get("lines", ""))))

    if out:
        return out

    # 旧 schema 兜底
    for kd in m.key_designs:
        if not isinstance(kd, dict):
            continue
        point = kd.get("point", "")
        for ev in kd.get("evidence", []) or []:
            if not isinstance(ev, dict):
                continue
            out.append((point, ev.get("file", ""), str(ev.get("lines", ""))))
    return out


def _run_single_verifier(
    v: VerifierVerdict,
    snippet: str,
    client: DeepSeekClient,
    model: str,
) -> None:
    """跑一次 verifier LLM 调用, 就地填 v.verdict / v.reason / v.suggested_lines / v.error."""
    try:
        msgs = render_messages(
            "verifier.j2",
            claim=v.claim,
            file=v.file,
            lines=v.lines,
            actual_snippet=snippet[:2000],
        )
        chat = client.chat(
            msgs, model=model,
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        parsed = _extract_json_object(chat.content) or {}
        v.verdict = parsed.get("verdict", "unrelated")
        v.reason = parsed.get("reason", "")
        v.suggested_lines = parsed.get("suggested_lines", "")
    except Exception as e:
        v.verdict = "skipped"
        v.error = f"{type(e).__name__}: {e!s}"[:160]


def verifier_sweep(
    modules: list[ModuleReport],
    repo: Path,
    client: DeepSeekClient,
    *,
    model: str = "pro",
    max_evidence: int = 30,
) -> list[VerifierVerdict]:
    """遍历每条 ref / evidence 让 LLM 判 verdict. 限 max_evidence 条以控成本.

    schema: 主路径走 m.refs[] (顶层平结构, 每条 {id, file, lines, why, snippet}).
    旧 m.key_designs 兜底, 仅在 m.refs 为空且 LLM 回退到旧 schema 时启用.
    """
    verdicts: list[VerifierVerdict] = []
    repo_root = repo.resolve()
    seen = 0

    for m in modules:
        if seen >= max_evidence:
            break

        for claim, file_, lines in _collect_module_evidences(m):
            if seen >= max_evidence:
                break
            seen += 1
            v = VerifierVerdict(
                module=m.name, claim=claim,
                file=file_, lines=lines,
            )

            snippet = _read_file_lines(repo_root, v.file, v.lines)
            if snippet is None:
                v.verdict = "unrelated"
                v.reason = "无法读取引用文件"
                v.error = "file_or_lines_invalid"
                verdicts.append(v)
                continue

            _run_single_verifier(v, snippet, client, model)
            verdicts.append(v)

    log.info("verifier_sweep_done",
             total=len(verdicts),
             support=sum(1 for v in verdicts if v.verdict == "support"))
    return verdicts


# --------------------------------------------------------------------------- #
# 顶层
# --------------------------------------------------------------------------- #


def load_facts(facts_path: Path) -> dict:
    """读 产出的 facts.json. 失败抛 FileNotFoundError / ValueError."""
    facts_path = Path(facts_path)
    if not facts_path.is_file():
        raise FileNotFoundError(f"facts.json not found: {facts_path} (先跑 oskag facts <repo>)")
    try:
        return json.loads(facts_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"facts.json 解析失败: {e!s}") from e


def _resolve_target_modules(
    profile: KernelProfile,
    repo: Path,
    client: DeepSeekClient,
    modules_to_run: list[str] | None,
) -> tuple[list[str], dict[str, dict], dict[str, ModuleSlice], bool]:
    """决定要分析哪些模块 + 各自标题. 返回 (target_modules, module_meta, discovered, is_free_mode).

    - family == "unknown" → 自由模式: 确定性发现顶层目录 + LLM 命名
    - 否则 → 固定内核模块 (MODULE_ORDER)
    """
    is_free_mode = profile.family == "unknown"
    module_meta: dict[str, dict] = {}
    discovered: dict[str, ModuleSlice] = {}

    if not is_free_mode:
        target_modules = modules_to_run or list(MODULE_ORDER)
        for m in MODULE_ORDER:
            module_meta[m] = {"title_zh": MODULE_TITLES.get(m, {}).get("zh", m), "order": 0}
        return target_modules, module_meta, discovered, is_free_mode

    # 自由模式
    discovered = discover_free_modules(profile, repo, max_files=20, max_modules=12)
    if not discovered:
        log.warning("free_mode_no_modules", repo=profile.repo_name)
        return [], module_meta, discovered, is_free_mode

    module_meta = name_free_modules(profile.repo_name, discovered, client, model="flash")
    sorted_keys = sorted(
        discovered.keys(),
        key=lambda k: module_meta.get(k, {}).get("order", 999),
    )
    target_modules = modules_to_run or sorted_keys
    return target_modules, module_meta, discovered, is_free_mode


def _run_one_module(
    m: str,
    facts: dict,
    profile: KernelProfile,
    repo: Path,
    client: DeepSeekClient,
    *,
    repomap_sections: dict[str, str],
    module_meta: dict[str, dict],
    discovered: dict[str, ModuleSlice],
    is_free_mode: bool,
    max_turns: int,
) -> ModuleReport:
    """跑单个模块的 describe_module, 处理自由模式的 slice 注入 + 标题覆盖. 永不抛."""
    title_zh = module_meta.get(m, {}).get("title_zh") or MODULE_TITLES.get(m, {}).get("zh", m)
    pre_slice = discovered.get(m) if is_free_mode else None
    # 自由模式下保证至少 8 轮 (无 syscall/boot 上下文, LLM 需更多轮 read_file 跨文件探索)
    effective_max_turns = max(max_turns, 8) if is_free_mode else max_turns

    try:
        mr = describe_module(
            m, facts, profile, repo, client,
            repomap_sections=repomap_sections,
            module_slice=pre_slice,
            max_turns=effective_max_turns,
        )
        if is_free_mode and module_meta.get(m):
            mr.title_zh = module_meta[m]["title_zh"]
        return mr
    except Exception as e:
        return ModuleReport(
            name=m, title_zh=title_zh,
            error=f"{type(e).__name__}: {e!s}"[:200],
        )


def describe(
    repo: Path | str,
    *,
    facts_path: Path | None = None,
    client: DeepSeekClient,
    modules_to_run: list[str] | None = None,
    deep_synthesize: bool = False,
    max_turns: int = 10,
    max_verifier: int = 30,
) -> DescribeReport:
    """主入口: 给定一个仓库, 产出 DescribeReport. 永不抛."""
    repo = Path(repo).resolve()
    t0 = time.monotonic()

    facts_path = facts_path or (Path("reports") / f"{repo.name}-facts.json")
    facts = load_facts(facts_path)
    profile = detect_family(repo)

    report = DescribeReport(
        repo_name=profile.repo_name,
        family=profile.family,
        facts=facts,
    )

    repomap_sections = split_repomap_by_module(facts.get("repomap", ""))

    # 决定模块列表 (自由模式 vs 固定内核模块)
    target_modules, module_meta, discovered, is_free_mode = _resolve_target_modules(
        profile, repo, client, modules_to_run
    )

    # 1. 各模块描述
    for m in target_modules:
        if not is_free_mode and m not in MODULE_ORDER:
            report.warnings.append(f"unknown_module: {m}")
            continue

        mr = _run_one_module(
            m, facts, profile, repo, client,
            repomap_sections=repomap_sections,
            module_meta=module_meta,
            discovered=discovered,
            is_free_mode=is_free_mode,
            max_turns=max_turns,
        )
        if mr.error:
            report.errors.append(f"module_{m}: {mr.error}")
        report.modules.append(mr)
        report.total_prompt_tokens += mr.prompt_tokens
        report.total_completion_tokens += mr.completion_tokens

    # 2. synthesize (pro + thinking, 不再受 deep_synthesize 限制 — 整套都升级)
    syn = synthesize(
        profile, facts, report.modules, client,
        model="pro",
        thinking="enabled",
    )
    report.overview = syn.get("overview", "")
    report.synthesis = syn.get("synthesis", "") or ""                # 新增
    report.innovations = syn.get("innovations", []) or []            # 兜底, 新 prompt 不再返
    report.syscall_coverage_comment = syn.get("syscall_coverage_comment", "")
    report.rating = syn.get("rating", {}) or {}
    syn_tokens = syn.get("_tokens", {})
    report.total_prompt_tokens += syn_tokens.get("prompt", 0)
    report.total_completion_tokens += syn_tokens.get("completion", 0)
    report.total_reasoning_tokens += syn_tokens.get("reasoning", 0)

    # 3. call_graph
    mermaid, cg_tokens = call_graph(profile, facts, client)
    report.call_graph_mermaid = mermaid
    report.total_prompt_tokens += cg_tokens.get("prompt", 0)
    report.total_completion_tokens += cg_tokens.get("completion", 0)

    # 4. verifier sweep
    report.verifier_verdicts = verifier_sweep(
        report.modules, repo, client, max_evidence=max_verifier,
    )

    report.duration_sec = round(time.monotonic() - t0, 2)
    log.info(
        "describe_done",
        repo=repo.name,
        modules=len(report.modules),
        prompt_tokens=report.total_prompt_tokens,
        completion_tokens=report.total_completion_tokens,
        duration_sec=report.duration_sec,
        verifier_total=len(report.verifier_verdicts),
    )
    return report


__all__ = [
    "DescribeReport",
    "ModuleReport",
    "VerifierVerdict",
    "describe",
    "describe_module",
    "synthesize",
    "call_graph",
    "verifier_sweep",
    "load_facts",
    "_extract_json_object",
    "_strip_invalid_evidence",
    "_evidence_lines_start",
    "_make_read_file_handler",
]
