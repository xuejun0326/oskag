"""家族探针 + KernelProfile.

判定 4 仓属于哪个家族, 输出 KernelProfile 供后续 facts 模块使用.

家族分类:
- arceos-starry: 顶层 Cargo.toml workspace exclude arceos; 用 Sysno 枚举 + linkme;
                 register_trap_handler(SYSCALL) 是核心标识.
- rcore-tutorial: 单 crate 或 workspace=[os, user]; SYSCALL_xxx: usize = N 整数常量;
                  集中式 match dispatch.
- unknown: 兜底, 给出最低限度 profile.

探针顺序 (确定性, 短路):
1. 读根 Cargo.toml workspace.members + exclude → 判 ArceOS-Starry
2. 找 register_trap_handler 用法 → 二次确认 ArceOS
3. 找 os/src/syscall/mod.rs + SYSCALL_GETCWD: usize 常量 → rCore-Tutorial
4. 都没命中 → unknown
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

import toml

from oskag.logging_setup import get_logger
from oskag.tools.fs import grep, is_rg_available, read_file

log = get_logger("oskag.facts.profiles")

Family = Literal["arceos-starry", "rcore-tutorial", "unknown"]
SyscallStyle = Literal["sysno", "const", "unknown"]
BootStyle = Literal["axhal", "polyhal", "global_asm", "unknown"]


# --------------------------------------------------------------------------- #
# KernelProfile dataclass
# --------------------------------------------------------------------------- #


@dataclass
class KernelProfile:
    """对一个仓库的"家族画像", 后续 facts 模块据此决定走哪条解析路径."""

    family: Family = "unknown"
    repo_root: Path = field(default_factory=Path)
    repo_name: str = ""

    # cargo 视角
    is_workspace: bool = False
    workspace_members: list[str] = field(default_factory=list)
    workspace_excludes: list[str] = field(default_factory=list)

    # syscall 视角
    syscall_style: SyscallStyle = "unknown"
    syscall_files: list[str] = field(default_factory=list)  # 相对 repo_root 的路径

    # boot 视角
    boot_style: BootStyle = "unknown"

    # 模块路径 (相对 repo_root, 供 描述生成时取材)
    module_paths: dict[str, list[str]] = field(default_factory=dict)

    # 忽略路径 (相对 repo_root) — 在 LOC / dir_tree / repomap 阶段过滤
    ignore_paths: list[str] = field(default_factory=list)

    # 探针置信度 (用于 facts.json meta)
    detection_signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["repo_root"] = str(self.repo_root)
        return d


# --------------------------------------------------------------------------- #
# 默认忽略路径 (按家族扩展)
# --------------------------------------------------------------------------- #


_BASE_IGNORE: tuple[str, ...] = (
    ".git", ".github", "target", "node_modules", "__pycache__",
    "dist", "build", ".venv", "venv", ".cargo", ".rustup",
    ".vscode", ".idea", ".devcontainer",
)

_ARCEOS_IGNORE: tuple[str, ...] = (
    "arceos", "apps", "vendor", "patch", ".arceos",
)

_RCORE_IGNORE: tuple[str, ...] = (
    "user", "vendor", "bootloader", "patch",
    # rCore 系常带的子工具
    "fat32-fuse", "lwext4_rust",
)


# --------------------------------------------------------------------------- #
# 仓库特异化 override (已确认的 4 仓 module 路径)
# --------------------------------------------------------------------------- #


_REPO_OVERRIDES: dict[str, dict] = {
    # StarryX: 顶层 src/ + xapi/src/{域}/ + xcore/src/{域}/ + xmodules/*
    "OSKernel2025-StarryX-3037": {
        "module_paths": {
            "syscall": ["src/syscall.rs", "xapi/src"],
            "task":    ["xapi/src/task", "xcore/src/task", "xmodules/xprocess"],
            "mm":      ["xapi/src/mm", "xcore/src/mm", "xmodules/xvma"],
            "fs":      ["xapi/src/fs", "xcore/src/fs"],
            "signal":  ["xapi/src/sys", "xmodules/xsignal"],
            "ipc":     ["xapi/src/ipc", "xcore/src/ipc", "xmodules/xcache"],
            "net":     ["xapi/src/net", "xcore/src/net"],
            "boot":    ["src/main.rs", "src/mm.rs", "arceos/modules/axhal/src/platform"],
        },
        "ignore_paths": _ARCEOS_IGNORE,
    },
    # Undefined-OS: api/src/imp + api/src/core + modules/vfs + core/src/{task.rs,mm.rs}
    "T202510003995291-2331": {
        "module_paths": {
            "syscall": ["src/syscall.rs", "api/src/imp"],
            "task":    ["api/src/imp/task", "process/src", "core/src/task.rs", "core/src/process.rs"],
            "mm":      ["api/src/imp/mm", "core/src/mm.rs"],
            "fs":      ["api/src/core/fs", "modules/vfs/src"],
            "signal":  ["api/src/imp/task/signal.rs"],
            "ipc":     ["core/src/shared_memory.rs"],
            "net":     ["api/src/imp/net"],
            "boot":    ["src/main.rs", "core/src/entry.rs", "arceos/modules/axhal/src/platform"],
        },
        "ignore_paths": _ARCEOS_IGNORE,
    },
    # SubsToKernel: 单 crate, 全在 os/src/
    "T202510008995695-2720": {
        "module_paths": {
            "syscall": ["os/src/syscall"],
            "task": ["os/src/task"],
            "mm": ["os/src/mm"],
            "fs": ["os/src/fs"],
            "signal": ["os/src/signal"],
            "ipc": [],
            "boot": ["os/src/boot.rs", "os/src/main.rs"],
            "drivers": ["os/src/drivers"],
            "net": ["os/src/net"],
        },
        "ignore_paths": _RCORE_IGNORE,
    },
    # Nonix: workspace os + user, polyhal 抽象
    "nonix": {
        "module_paths": {
            "syscall": ["os/src/syscall"],
            "task": ["os/src/task"],
            "mm": ["os/src/mm"],
            "fs": ["os/src/fs"],
            "signal": ["os/src/signal"],
            "ipc": [],
            "boot": ["os/src/main.rs"],
            "drivers": ["os/src/drivers"],
            "net": ["os/src/net"],
        },
        "ignore_paths": _RCORE_IGNORE,
    },
}


# --------------------------------------------------------------------------- #
# 探针 helpers
# --------------------------------------------------------------------------- #


def _read_root_cargo(repo: Path) -> dict | None:
    """读根 Cargo.toml. 不存在/解析失败返回 None."""
    cargo_toml = repo / "Cargo.toml"
    fr = read_file(cargo_toml)
    if fr.error:
        return None
    try:
        return toml.loads(fr.text)
    except ValueError as e:
        log.warning("cargo_toml_parse_failed", path=str(cargo_toml), error=str(e)[:160])
        return None


# 探针阶段的 rg 排除项. 探针已经知道存在 ArceOS 系仓库会 vendor 完整 arceos 副本,
# 也会 vendor 一些 rcore 第三方 crate. 我们要排除这些干扰, 只看主仓 syscall 模式.
_PROBE_GLOB_EXCLUDES = (
    "!arceos/**",
    "!vendor/**",
    "!.arceos/**",
    "!apps/**",
    "!user/**",
    "!target/**",
    "!.git/**",
    "!syscall_trace/**",  # Undefined-OS 的 trace 模块, 用 rcore 风格但不是主 syscall
)


def _probe_globs(extra: list[str] | None = None) -> list[str]:
    """组合主 glob (*.rs) + 探针 ignore."""
    base = ["*.rs"] if extra is None else list(extra)
    return base + list(_PROBE_GLOB_EXCLUDES)


def _has_register_trap_handler(repo: Path) -> bool:
    """grep register_trap_handler. 是 ArceOS 系强标识.

    timeout 90s 兜底 (4仓 ~10MB+ Rust 代码, 30s 不够).
    """
    if not is_rg_available():
        return False
    matches = grep(
        r"register_trap_handler",
        repo,
        glob=_probe_globs(),
        workspace_root=repo,
        timeout=90,
    )
    return len(matches) > 0


def _has_syscall_const_table(repo: Path) -> tuple[bool, list[Path]]:
    """grep 'pub const SYSCALL_xxx: usize' 风格. 是 rCore 系强标识.

    返回 (是否命中, syscall mod 文件列表).
    """
    if not is_rg_available():
        return False, []
    matches = grep(
        r"pub const SYSCALL_\w+:\s*usize",
        repo,
        glob=_probe_globs(),
        workspace_root=repo,
        timeout=90,
    )
    if not matches:
        return False, []
    files = sorted({m.file.resolve() for m in matches})
    return True, files


def _find_sysno_match_files(repo: Path) -> list[Path]:
    """grep 'Sysno::xxx =>' 找 ArceOS dispatch 表所在文件."""
    if not is_rg_available():
        return []
    matches = grep(
        r"Sysno::\w+\s*=>",
        repo,
        glob=_probe_globs(),
        workspace_root=repo,
        timeout=90,
    )
    return sorted({m.file.resolve() for m in matches})


def _detect_boot_style(repo: Path, family: Family) -> BootStyle:
    """识别启动风格."""
    if not is_rg_available():
        return "unknown"
    # polyhal: define_entry!
    if grep(r"define_entry!", repo, glob=["*.rs"], workspace_root=repo, timeout=20):
        return "polyhal"
    # ArceOS axhal: register_trap_handler 已在前面查过, 可以推断
    if family == "arceos-starry":
        return "axhal"
    # 找 global_asm + _start
    if grep(r"\.globl\s+_start", repo, glob=["*.rs", "*.S", "*.s"],
            workspace_root=repo, timeout=20):
        return "global_asm"
    return "unknown"


# --------------------------------------------------------------------------- #
# 主探针
# --------------------------------------------------------------------------- #


def _apply_workspace_meta(profile: KernelProfile, cargo: dict | None) -> None:
    """从根 Cargo.toml 解析 workspace.members/exclude, 写入 profile."""
    if cargo is None:
        return
    ws = cargo.get("workspace")
    if not isinstance(ws, dict):
        return
    profile.is_workspace = True
    profile.workspace_members = [str(m) for m in ws.get("members", [])]
    profile.workspace_excludes = [str(e) for e in ws.get("exclude", [])]
    profile.detection_signals.append(
        f"workspace.members={profile.workspace_members[:5]}"
    )


def _classify_family(
    profile: KernelProfile,
    repo: Path,
    *,
    has_rth: bool,
    sysno_files: list[Path],
    has_const: bool,
    const_files: list[Path],
) -> None:
    """综合 4 个探针信号决定 family + syscall_style + syscall_files.

    优先 ArceOS (强信号 register_trap_handler 或 Sysno match):
    若同时命中 const, ArceOS 仍胜出 (Undefined-OS syscall_trace/ 子模块写了
    rCore 风格常量, 易误判).
    """
    is_arceos = has_rth or bool(sysno_files)
    if is_arceos:
        profile.family = "arceos-starry"
        profile.syscall_style = "sysno"
        profile.syscall_files = [
            str(f.relative_to(repo).as_posix()) for f in sysno_files
        ]
    elif has_const and const_files:
        profile.family = "rcore-tutorial"
        profile.syscall_style = "const"
        profile.syscall_files = [
            str(f.relative_to(repo).as_posix()) for f in const_files
        ]
    else:
        profile.family = "unknown"


def _apply_paths(profile: KernelProfile) -> None:
    """根据 repo 名应用 _REPO_OVERRIDES, 否则按 family 套默认 ignore."""
    override = _REPO_OVERRIDES.get(profile.repo_name)
    if override:
        profile.module_paths = dict(override.get("module_paths", {}))
        profile.ignore_paths = list(override.get("ignore_paths", _BASE_IGNORE))
    elif profile.family == "arceos-starry":
        profile.ignore_paths = list(_ARCEOS_IGNORE)
    elif profile.family == "rcore-tutorial":
        profile.ignore_paths = list(_RCORE_IGNORE)
    else:
        profile.ignore_paths = []
    # 合并 BASE_IGNORE
    profile.ignore_paths = sorted(set(profile.ignore_paths) | set(_BASE_IGNORE))


def detect_family(repo_path: Path | str) -> KernelProfile:
    """对 repo 跑探针, 返回 KernelProfile (永不抛异常, 失败时 family=unknown)."""
    repo = Path(repo_path).resolve()
    profile = KernelProfile(repo_root=repo, repo_name=repo.name)

    if not repo.is_dir():
        profile.detection_signals.append(f"repo_not_dir:{repo}")
        return profile

    # 1. workspace meta
    _apply_workspace_meta(profile, _read_root_cargo(repo))

    # 2. 三类探针
    has_rth = _has_register_trap_handler(repo)
    if has_rth:
        profile.detection_signals.append("register_trap_handler:found")
    sysno_files = _find_sysno_match_files(repo)
    if sysno_files:
        profile.detection_signals.append(f"sysno_match_in:{len(sysno_files)}_files")
    has_const, const_files = _has_syscall_const_table(repo)
    if has_const:
        profile.detection_signals.append(f"syscall_const_in:{len(const_files)}_files")

    # 3. 综合判定 family + syscall
    _classify_family(
        profile, repo,
        has_rth=has_rth,
        sysno_files=sysno_files,
        has_const=has_const,
        const_files=const_files,
    )

    # 4. boot 风格
    profile.boot_style = _detect_boot_style(repo, profile.family)
    profile.detection_signals.append(f"boot_style:{profile.boot_style}")

    # 5. 路径 (overrides / family 默认)
    _apply_paths(profile)

    log.info(
        "profile_detected",
        repo=profile.repo_name,
        family=profile.family,
        syscall_style=profile.syscall_style,
        boot_style=profile.boot_style,
        signals=profile.detection_signals,
    )
    return profile


__all__ = [
    "Family",
    "SyscallStyle",
    "BootStyle",
    "KernelProfile",
    "detect_family",
]
