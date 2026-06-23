"""describe/_modules.py — 文档章节切分.

定义 9 个语义模块的中文标题、域归属、以及"给定 profile + repo 路径,
返回该模块涉及的源文件清单"的展开器。

核心契约:
- profile.module_paths 是 已经定下的 source-of-truth (按 repo_name override
  优先, 否则按 family 默认). 我们不在这儿重复维护一份, 只为未知仓库提供 fallback.
- module_files_for() 永不抛异常: 路径不存在/读不到 → 静默跳过, 写 warnings.
- 单个模块的文件总数 cap 在 max_files (默认 20), 按 size 降序,
  让最大的实现文件 (通常是 mod.rs / 主入口) 在前; pipeline 据此截 token budget.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from oskag.facts.profiles import KernelProfile
from oskag.logging_setup import get_logger
from oskag.tools.fs import list_files

log = get_logger("oskag.describe.modules")


# --------------------------------------------------------------------------- #
# 模块定义
# --------------------------------------------------------------------------- #


# 渲染顺序 (与 Markdown 章节顺序一致)
MODULE_ORDER: tuple[str, ...] = (
    "boot",      # 启动流程
    "mm",        # 内存管理
    "task",      # 进程/任务调度
    "fs",        # 文件系统
    "signal",    # 信号
    "ipc",       # 进程间通信
    "net",       # 网络
    "drivers",   # 驱动
    "syscall",   # 系统调用层
)


# 中文标题 + 简介 (供 prompt 与 markdown 章节头共用)
MODULE_TITLES: dict[str, dict[str, str]] = {
    "boot":     {"zh": "启动流程",       "en": "Boot Sequence"},
    "mm":       {"zh": "内存管理",       "en": "Memory Management"},
    "task":     {"zh": "进程与任务调度", "en": "Process and Scheduling"},
    "fs":       {"zh": "文件系统",       "en": "File System"},
    "signal":   {"zh": "信号机制",       "en": "Signal Handling"},
    "ipc":      {"zh": "进程间通信",     "en": "IPC"},
    "net":      {"zh": "网络",           "en": "Networking"},
    "drivers":  {"zh": "驱动框架",       "en": "Drivers"},
    "syscall":  {"zh": "系统调用层",     "en": "Syscall Layer"},
}


# 未知仓库 / module_paths 缺项时的家族级 fallback
_ARCEOS_FALLBACK: dict[str, list[str]] = {
    "boot":    ["arceos/modules/axhal/src/platform", "src/main.rs"],
    "mm":      ["arceos/modules/axmm", "xcore/mm"],
    "task":    ["arceos/modules/axtask", "xcore/task"],
    "fs":      ["arceos/modules/axfs", "xcore/fs"],
    "signal":  ["xcore/signal"],
    "ipc":     [],
    "net":     ["arceos/modules/axnet"],
    "drivers": ["arceos/modules/axdriver"],
    "syscall": ["src/syscall.rs", "xapi", "xcore/syscall"],
}

_RCORE_FALLBACK: dict[str, list[str]] = {
    "boot":    ["os/src/main.rs", "os/src/boot.rs", "os/src/entry.rs"],
    "mm":      ["os/src/mm"],
    "task":    ["os/src/task"],
    "fs":      ["os/src/fs"],
    "signal":  ["os/src/signal"],
    "ipc":     [],
    "net":     ["os/src/net"],
    "drivers": ["os/src/drivers"],
    "syscall": ["os/src/syscall"],
}


# 自由模式 (非内核项目) 识别为"源码模块"的文件后缀
_SOURCE_EXT: frozenset[str] = frozenset({
    ".rs", ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx",
    ".py", ".go", ".java", ".ts", ".tsx", ".js", ".kt", ".scala",
    ".m", ".mm", ".swift", ".zig",
})

# 自由模式扫描时额外跳过的顶层目录名 (在 DEFAULT_IGNORE / ignore_paths 之外)
_FREE_SKIP_DIRS: frozenset[str] = frozenset({
    "test", "tests", "doc", "docs", "example", "examples",
    "third_party", "third-party", "external", "extern",
    "scripts", "tools", "ci", "bench", "benches", "assets",
})


@dataclass
class ModuleSlice:
    """单个模块在某仓库内的 文件 + 元数据 切片."""
    name: str                                    # syscall / mm / ...
    title_zh: str                                # 中文标题
    files: list[Path] = field(default_factory=list)
    total_size: int = 0
    truncated: bool = False                      # 文件数被 cap 时为 True
    paths_searched: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.files

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "title_zh": self.title_zh,
            "files": [str(p) for p in self.files],
            "total_size": self.total_size,
            "truncated": self.truncated,
            "paths_searched": self.paths_searched,
            "warnings": self.warnings,
        }


# --------------------------------------------------------------------------- #
# 路径展开
# --------------------------------------------------------------------------- #


def _resolve_module_paths(profile: KernelProfile, module: str) -> list[str]:
    """优先取 profile.module_paths[module], 缺项 fallback 到家族默认."""
    paths = profile.module_paths.get(module)
    if paths:
        return list(paths)
    if profile.family == "arceos-starry":
        return list(_ARCEOS_FALLBACK.get(module, []))
    if profile.family == "rcore-tutorial":
        return list(_RCORE_FALLBACK.get(module, []))
    return []


def _expand_one_path(repo: Path, rel_path: str,
                     ignore_paths: list[str]) -> list[Path]:
    """单个路径条目展开:

    - 若 repo/rel_path 是文件且 .rs 后缀 → 返回 [它]
    - 若是目录 → list_files(*.rs) 递归收集, 然后剔掉 ignore_paths 命中
    - 若不存在 → 返回 []
    """
    target = repo / rel_path
    if target.is_file():
        if target.suffix == ".rs":
            return [target]
        return []
    if not target.is_dir():
        return []

    files = list_files(target, glob=["*.rs"])
    ignore_set = set(ignore_paths)
    out: list[Path] = []
    for fp in files:
        try:
            rel = fp.resolve().relative_to(repo.resolve()).as_posix()
        except ValueError:
            continue
        parts = rel.split("/")
        if any(p in ignore_set for p in parts):
            continue
        out.append(fp)
    return out


def module_files_for(profile: KernelProfile, module: str, repo: Path | str,
                     *, max_files: int = 20) -> ModuleSlice:
    """展开 profile.module_paths[module] 为实际存在的 .rs 文件清单.

    永不抛异常. 文件总数 > max_files 时按 size 降序保留前 max_files,
    并把 truncated 设为 True.
    """
    repo = Path(repo)
    title = MODULE_TITLES.get(module, {}).get("zh", module)
    slice_ = ModuleSlice(name=module, title_zh=title)

    paths = _resolve_module_paths(profile, module)
    slice_.paths_searched = list(paths)
    if not paths:
        slice_.warnings.append(f"module_paths empty for {module} in {profile.family}")
        return slice_

    seen: set[Path] = set()
    collected: list[tuple[int, Path]] = []  # (size, path) 用于排序
    for p in paths:
        for fp in _expand_one_path(repo, p, profile.ignore_paths):
            if fp in seen:
                continue
            seen.add(fp)
            try:
                size = fp.stat().st_size
            except OSError:
                size = 0
            collected.append((size, fp))

    if not collected:
        slice_.warnings.append(f"no .rs files found under {paths}")
        return slice_

    collected.sort(key=lambda x: -x[0])  # 大文件优先
    if len(collected) > max_files:
        collected = collected[:max_files]
        slice_.truncated = True
        slice_.warnings.append(f"truncated to top-{max_files} largest files")

    slice_.files = [fp for _sz, fp in collected]
    slice_.total_size = sum(sz for sz, _fp in collected)

    log.info(
        "module_slice",
        repo=repo.name,
        module=module,
        files=len(slice_.files),
        size=slice_.total_size,
        truncated=slice_.truncated,
    )
    return slice_


def all_modules_for(profile: KernelProfile, repo: Path | str,
                    *, max_files: int = 20) -> dict[str, ModuleSlice]:
    """对 MODULE_ORDER 中每个模块都展开一次, 返回 {name: ModuleSlice}."""
    repo = Path(repo)
    return {
        m: module_files_for(profile, m, repo, max_files=max_files)
        for m in MODULE_ORDER
    }


# --------------------------------------------------------------------------- #
# 自由模式 (方案 B): 非内核项目的确定性模块发现
# --------------------------------------------------------------------------- #


def _collect_source_files(root: Path, repo: Path,
                          ignore_paths: list[str]) -> list[tuple[int, Path]]:
    """递归收集 root 下的源码文件 (按 _SOURCE_EXT 过滤), 返回 [(size, path)].

    永不抛. 命中 ignore_paths 任一路径段的文件被剔除.
    """
    ignore_set = set(ignore_paths)
    out: list[tuple[int, Path]] = []
    globs = [f"*{ext}" for ext in sorted(_SOURCE_EXT)]
    for fp in list_files(root, glob=globs):
        try:
            rel = fp.resolve().relative_to(repo.resolve()).as_posix()
        except ValueError:
            continue
        if any(p in ignore_set for p in rel.split("/")):
            continue
        try:
            size = fp.stat().st_size
        except OSError:
            size = 0
        out.append((size, fp))
    return out


def _slice_from_collected(name: str, collected: list[tuple[int, Path]],
                          *, max_files: int) -> ModuleSlice:
    """把 [(size, path)] 收敛成一个 ModuleSlice (按 size 降序 + cap)."""
    slice_ = ModuleSlice(name=name, title_zh=name)  # 标题先用英文 key, 待 LLM 命名
    if not collected:
        return slice_
    collected = sorted(collected, key=lambda x: -x[0])
    if len(collected) > max_files:
        collected = collected[:max_files]
        slice_.truncated = True
        slice_.warnings.append(f"truncated to top-{max_files} largest files")
    slice_.files = [fp for _sz, fp in collected]
    slice_.total_size = sum(sz for sz, _fp in collected)
    return slice_


def discover_free_modules(profile: KernelProfile, repo: Path | str,
                          *, max_files: int = 20,
                          max_modules: int = 12) -> dict[str, ModuleSlice]:
    """自由模式: 把仓库顶层目录视为"语义模块"边界, 确定性发现章节.

    规则:
    - 扫顶层 (depth 1) 每个目录; 递归含源码文件 (_SOURCE_EXT) 的目录即一个候选模块.
    - 顶层散落的源码文件归入 "root" 模块.
    - 跳过 ignore_paths + _FREE_SKIP_DIRS (test/docs/scripts 等噪声目录).
    - 候选模块按总 size 降序排, cap max_modules 个 (噪声项被截掉).

    返回 {dir_name: ModuleSlice}, key 是顶层目录名 (英文). 中文标题留 LLM 命名.
    永不抛异常.
    """
    repo = Path(repo).resolve()
    ignore_paths = list(profile.ignore_paths)
    ignore_set = set(ignore_paths) | set(_FREE_SKIP_DIRS)

    candidates: list[ModuleSlice] = []

    # 顶层散落源码文件 → root 模块
    try:
        root_loose = [
            (fp.stat().st_size if fp.is_file() else 0, fp)
            for fp in repo.iterdir()
            if fp.is_file() and fp.suffix in _SOURCE_EXT
        ]
    except OSError:
        root_loose = []
    if root_loose:
        candidates.append(_slice_from_collected("root", root_loose, max_files=max_files))

    # 顶层目录 → 各自一个候选模块
    try:
        top_dirs = sorted(p for p in repo.iterdir() if p.is_dir())
    except OSError:
        top_dirs = []
    for d in top_dirs:
        if d.name in ignore_set:
            continue
        collected = _collect_source_files(d, repo, ignore_paths)
        if not collected:
            continue
        candidates.append(_slice_from_collected(d.name, collected, max_files=max_files))

    # 按总 size 降序, cap max_modules
    candidates.sort(key=lambda s: -s.total_size)
    if len(candidates) > max_modules:
        candidates = candidates[:max_modules]

    result = {s.name: s for s in candidates}
    log.info(
        "free_modules_discovered",
        repo=repo.name,
        modules=list(result.keys()),
        count=len(result),
    )
    return result


__all__ = [
    "MODULE_ORDER",
    "MODULE_TITLES",
    "ModuleSlice",
    "module_files_for",
    "all_modules_for",
    "discover_free_modules",
]
