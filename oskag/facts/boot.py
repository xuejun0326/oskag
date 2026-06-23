"""facts/boot.py — 启动入口与前 3 层调用链.

输出 BootFacts:
- entry_file: 入口主文件 (src/main.rs 或 os/src/main.rs)
- entry_fn: 入口函数 (main / rust_main)
- entry_line: 入口行号
- handlers: ArceOS 系 #[register_trap_handler(KIND)] fn 列表
- assembly_files: 启动汇编 (.S/.asm) 列表
- call_chain: 入口函数体里的前 N 个直接调用 (粗粒度, 用 rg + 简单正则)
- boot_style: 与 profile.boot_style 同步, 这里再确认一次

所有抽取容错: 找不到入口写 warnings, 不抛异常.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from oskag.facts.profiles import KernelProfile
from oskag.logging_setup import get_logger
from oskag.tools.fs import grep, is_rg_available, list_files, read_file

log = get_logger("oskag.facts.boot")

DEFAULT_CALL_CHAIN_LIMIT = 12      # 入口函数前 N 个调用
DEFAULT_ASM_FILE_LIMIT = 8         # 最多列 N 个汇编文件


@dataclass
class TrapHandler:
    """ArceOS 注册的 trap 处理函数."""
    kind: str                       # SYSCALL / PAGE_FAULT / IRQ ...
    fn_name: str
    file: str
    line: int


@dataclass
class BootFacts:
    boot_style: str = "unknown"
    entry_file: str = ""
    entry_fn: str = ""
    entry_line: int = 0
    call_chain: list[str] = field(default_factory=list)
    handlers: list[TrapHandler] = field(default_factory=list)
    assembly_files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# --------------------------------------------------------------------------- #
# 候选入口文件 (按家族优先)
# --------------------------------------------------------------------------- #


_ARCEOS_CANDIDATES: tuple[str, ...] = (
    "src/main.rs",
)
_RCORE_CANDIDATES: tuple[str, ...] = (
    "os/src/main.rs",
    "os/src/lib.rs",
    "kernel/src/main.rs",
    "src/main.rs",
)


def _entry_candidates(profile: KernelProfile | None) -> list[str]:
    family = profile.family if profile else "unknown"
    if family == "arceos-starry":
        return list(_ARCEOS_CANDIDATES)
    if family == "rcore-tutorial":
        return list(_RCORE_CANDIDATES)
    # unknown: 都试
    return list(_ARCEOS_CANDIDATES) + list(_RCORE_CANDIDATES)


# --------------------------------------------------------------------------- #
# 入口函数定位 + 调用链抽取
# --------------------------------------------------------------------------- #


_ENTRY_FN_RE = re.compile(
    r"^\s*(?:pub\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+(rust_main|main)\b"
)
_BRACE_OPEN_RE = re.compile(r"\{")
_BRACE_CLOSE_RE = re.compile(r"\}")
# 函数体内调用: identifier( 或 path::identifier(
_CALL_RE = re.compile(r"\b([A-Za-z_][\w:]*)\s*\(")
# 噪音 ident (rust 关键字 / 常见 macro 内置)
_CALL_BLACKLIST: frozenset[str] = frozenset({
    "if", "while", "for", "match", "loop", "return", "let", "fn", "as", "mut",
    "ref", "Some", "None", "Ok", "Err", "Box", "Vec", "String", "Option",
    "Result", "core", "alloc", "self", "Self", "super",
    "println", "print", "info", "warn", "debug", "error", "trace",
    "log", "dbg", "panic", "format", "write", "writeln", "include_str",
    "include_bytes", "concat", "stringify", "assert", "assert_eq", "assert_ne",
    "todo", "unimplemented", "matches",
})


def _find_entry_in_file(
    repo: Path, rel_path: str,
) -> tuple[int, str] | None:
    """在指定文件找 fn main / fn rust_main; 返回 (行号, fn_name) 或 None."""
    fp = repo / rel_path
    if not fp.is_file():
        return None
    fr = read_file(fp, max_bytes=500_000)
    if fr.error:
        return None
    for idx, line in enumerate(fr.text.splitlines(), 1):
        m = _ENTRY_FN_RE.match(line)
        if m:
            return idx, m.group(1)
    return None


def _read_function_body(fp: Path, start_line: int) -> str:
    """从 start_line 起读到匹配的右 { 为止. 简单括号计数, 忽略字符串内的 {}.

    返回函数体文本 (不含函数签名行). 解析失败返回空串.
    """
    fr = read_file(fp, max_bytes=500_000)
    if fr.error:
        return ""
    lines = fr.text.splitlines()
    if start_line < 1 or start_line > len(lines):
        return ""
    depth = 0
    body_lines: list[str] = []
    started = False
    for line in lines[start_line - 1:]:
        opens = len(_BRACE_OPEN_RE.findall(line))
        closes = len(_BRACE_CLOSE_RE.findall(line))
        if not started:
            if opens > 0:
                started = True
                depth = opens - closes
            # 签名行不计入 body
        else:
            depth += opens - closes
            body_lines.append(line)
            if depth <= 0:
                break
    return "\n".join(body_lines)


def _is_call_noise(ident: str) -> bool:
    """是否应当过滤掉这个 identifier (关键字 / 全大写常量 / 黑名单)."""
    if ident.endswith("!"):
        return True
    if ident.isupper() and len(ident) > 1:
        return True
    short = ident.split("::")[-1]
    return short in _CALL_BLACKLIST


def _extract_calls_from_body(body: str, limit: int) -> list[str]:
    """从函数体抽前 limit 个直接调用 (按出现顺序去重)."""
    seen: set[str] = set()
    out: list[str] = []
    for line in body.splitlines():
        if line.lstrip().startswith("//"):
            continue
        for m in _CALL_RE.finditer(line):
            ident = m.group(1)
            if _is_call_noise(ident) or ident in seen:
                continue
            seen.add(ident)
            out.append(ident)
            if len(out) >= limit:
                return out
    return out


# --------------------------------------------------------------------------- #
# trap handlers (ArceOS): #[register_trap_handler(KIND)] fn name(...)
# --------------------------------------------------------------------------- #


_TRAP_HEADER_RE = re.compile(r"#\[register_trap_handler\((\w+)\)\]")
_FN_AFTER_TRAP_RE = re.compile(
    r"^\s*(?:pub\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+(\w+)"
)


def _fn_name_after_trap(file_path: Path, header_line: int) -> str:
    """读 file_path, 从 header_line+1 起最多 3 行内找 fn name."""
    fr = read_file(file_path, max_bytes=500_000)
    if fr.error:
        return ""
    lines = fr.text.splitlines()
    for offset in range(1, 4):
        idx = header_line + offset - 1
        if idx >= len(lines):
            return ""
        mm = _FN_AFTER_TRAP_RE.match(lines[idx])
        if mm:
            return mm.group(1)
    return ""


def _build_handler_excludes(profile: KernelProfile | None) -> list[str]:
    excludes = ["*.rs", "!arceos/**", "!apps/**", "!vendor/**", "!.arceos/**",
                "!user/**", "!target/**", "!.git/**", "!patch/**"]
    if profile is not None:
        for ig in profile.ignore_paths:
            excludes.append(f"!{ig}/**")
    return excludes


def _find_trap_handlers(
    repo: Path, profile: KernelProfile | None,
) -> list[TrapHandler]:
    """rg #[register_trap_handler(X)], 取下一行的 fn name 作 handler."""
    if not is_rg_available():
        return []
    matches = grep(
        r"#\[register_trap_handler\(\w+\)\]",
        repo, glob=_build_handler_excludes(profile),
        workspace_root=repo, timeout=60,
    )
    out: list[TrapHandler] = []
    for m in matches:
        kh = _TRAP_HEADER_RE.search(m.line_text)
        if not kh:
            continue
        fn_name = _fn_name_after_trap(m.file, m.line_no)
        try:
            rel = m.file.resolve().relative_to(repo.resolve()).as_posix()
        except ValueError:
            rel = str(m.file)
        out.append(TrapHandler(
            kind=kh.group(1), fn_name=fn_name, file=rel, line=m.line_no,
        ))
    return out


# --------------------------------------------------------------------------- #
# 启动汇编文件
# --------------------------------------------------------------------------- #


def _find_assembly_files(
    repo: Path, profile: KernelProfile | None, limit: int,
) -> list[str]:
    """收集仓内 .S / .asm 文件 (相对路径), 应用 ignore_paths 过滤."""
    if not is_rg_available():
        return []
    s_files = list_files(repo, glob=["*.S"])
    asm_files = list_files(repo, glob=["*.asm"])
    files = sorted(set(s_files) | set(asm_files))

    ignore_set: set[str] = set()
    if profile is not None:
        ignore_set |= set(profile.ignore_paths)
    ignore_set |= {"arceos", "vendor", "target", "patch", ".git", "user", "apps"}

    out: list[str] = []
    for fp in files:
        try:
            rel = fp.resolve().relative_to(repo.resolve()).as_posix()
        except ValueError:
            continue
        parts = rel.split("/")
        if any(p in ignore_set for p in parts):
            continue
        out.append(rel)
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #


def scan_boot(
    repo_path: Path | str,
    profile: KernelProfile | None = None,
    *,
    call_chain_limit: int = DEFAULT_CALL_CHAIN_LIMIT,
    asm_file_limit: int = DEFAULT_ASM_FILE_LIMIT,
) -> BootFacts:
    """对 repo 跑启动入口探测. 永不抛异常."""
    repo = Path(repo_path).resolve()
    facts = BootFacts()
    if not repo.is_dir():
        facts.warnings.append(f"not a directory: {repo}")
        return facts

    facts.boot_style = profile.boot_style if profile else "unknown"

    # 找入口函数
    for cand in _entry_candidates(profile):
        result = _find_entry_in_file(repo, cand)
        if result is not None:
            facts.entry_line, facts.entry_fn = result
            facts.entry_file = cand
            break
    if not facts.entry_fn:
        facts.warnings.append("entry function not found in any candidate path")
    else:
        # 提取调用链
        body = _read_function_body(repo / facts.entry_file, facts.entry_line)
        if body:
            facts.call_chain = _extract_calls_from_body(body, call_chain_limit)
        else:
            facts.warnings.append("entry function body could not be parsed")

    # ArceOS trap handlers (rcore 路径会拿到空 list, 不报警)
    if facts.boot_style == "axhal" or (profile and profile.family == "arceos-starry"):
        facts.handlers = _find_trap_handlers(repo, profile)
        if not facts.handlers:
            facts.warnings.append("no #[register_trap_handler] found in arceos repo")

    # 汇编文件
    facts.assembly_files = _find_assembly_files(repo, profile, asm_file_limit)

    log.info(
        "boot_scan_done",
        repo=repo.name,
        entry_file=facts.entry_file,
        entry_fn=facts.entry_fn,
        call_chain_n=len(facts.call_chain),
        handlers=len(facts.handlers),
        asm_files=len(facts.assembly_files),
        warnings=len(facts.warnings),
    )
    return facts


__all__ = [
    "TrapHandler",
    "BootFacts",
    "scan_boot",
]
