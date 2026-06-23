r"""facts/syscalls.py — 双家族 syscall 抽取 + 6 域分类.

家族路径:
- ArceOS-Starry: rg `Sysno::(\w+)\s*=>` → name 来自 syscalls crate 的枚举名;
  id 用 RISCV64_NAME_TO_ID 反查 (赛题主架构 RISC-V 64).
- rCore-Tutorial: rg `(SYSCALL_\w+)\s*=>` 找 dispatch 站点; 与 `pub const SYSCALL_xxx: usize = N`
  常量声明对齐. 同时与 RISCV64 表交叉校验 (mismatch 写 warnings).

6 域分类: fs / mm / task / signal / ipc / net (其他归 misc).
分类规则: 按 syscall 名前缀/精确名硬编码集合. 不命中归 misc.

输出 SyscallFacts:
- count, items[{name, id, arch_id, file, line, domain, kind}], by_domain,
  arch, backend, warnings.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Literal

from oskag.facts._syscall_tables import (
    LOONGARCH64_NAME_TO_ID,
    RISCV64_NAME_TO_ID,
)
from oskag.facts.profiles import KernelProfile
from oskag.logging_setup import get_logger
from oskag.tools.fs import grep, is_rg_available, read_file

log = get_logger("oskag.facts.syscalls")

Domain = Literal["fs", "mm", "task", "signal", "ipc", "net", "misc"]
Arch = Literal["riscv64", "loongarch64"]


# --------------------------------------------------------------------------- #
# 6-域分类表 (基于 Linux man-pages 大类)
# --------------------------------------------------------------------------- #


_FS_NAMES: frozenset[str] = frozenset({
    "read", "write", "open", "openat", "close", "lseek", "stat", "fstat", "lstat",
    "newfstatat", "fstatat", "statx", "statfs", "fstatfs", "access", "faccessat",
    "faccessat2", "creat", "mkdir", "mkdirat", "unlink", "unlinkat", "link",
    "linkat", "symlink", "symlinkat", "rename", "renameat", "renameat2",
    "readlink", "readlinkat", "chdir", "fchdir", "getcwd", "getdents",
    "getdents64", "dup", "dup2", "dup3", "fcntl", "ioctl", "pipe", "pipe2",
    "splice", "tee", "vmsplice", "sendfile", "sync", "fsync", "fdatasync",
    "syncfs", "truncate", "ftruncate", "fallocate", "copy_file_range",
    "chmod", "fchmod", "fchmodat", "chown", "fchown", "lchown", "fchownat",
    "umask", "mount", "umount", "umount2", "pivot_root", "mknod", "mknodat",
    "utime", "utimes", "futimesat", "utimensat",
    "select", "pselect6", "poll", "ppoll", "epoll_create", "epoll_create1",
    "epoll_ctl", "epoll_wait", "epoll_pwait", "epoll_pwait2",
    "eventfd", "eventfd2", "timerfd_create", "timerfd_settime",
    "timerfd_gettime", "inotify_init", "inotify_init1", "inotify_add_watch",
    "inotify_rm_watch", "fanotify_init", "fanotify_mark", "memfd_create",
    "name_to_handle_at", "open_by_handle_at", "mount_setattr",
    "openat2", "fsmount", "fsopen", "fspick", "fsconfig", "move_mount",
    "open_tree", "close_range", "preadv", "pwritev", "preadv2", "pwritev2",
    "pread64", "pwrite64", "readv", "writev", "flock",
})

_MM_NAMES: frozenset[str] = frozenset({
    "brk", "mmap", "munmap", "mprotect", "msync", "madvise", "mlock", "mlock2",
    "munlock", "mlockall", "munlockall", "mremap", "mincore", "remap_file_pages",
    "membarrier", "userfaultfd", "pkey_alloc", "pkey_free", "pkey_mprotect",
    "set_mempolicy", "get_mempolicy", "mbind", "migrate_pages", "move_pages",
    "process_madvise", "process_mrelease",
})

_TASK_NAMES: frozenset[str] = frozenset({
    "clone", "clone3", "fork", "vfork", "execve", "execveat", "exit",
    "exit_group", "wait4", "waitid", "getpid", "getppid", "gettid",
    "getuid", "geteuid", "getgid", "getegid", "setuid", "setgid", "setpgid",
    "getpgid", "getpgrp", "setsid", "getsid", "setreuid", "setregid",
    "setresuid", "getresuid", "setresgid", "getresgid", "setgroups", "getgroups",
    "set_tid_address", "set_robust_list", "get_robust_list",
    "sched_yield", "sched_setaffinity", "sched_getaffinity", "sched_setparam",
    "sched_getparam", "sched_setscheduler", "sched_getscheduler",
    "sched_get_priority_max", "sched_get_priority_min", "sched_rr_get_interval",
    "sched_setattr", "sched_getattr",
    "prctl", "arch_prctl", "capget", "capset", "personality", "umask",
    "uname", "sethostname", "setdomainname", "ptrace",
    "getrlimit", "setrlimit", "prlimit64", "getpriority", "setpriority",
    "getrandom", "times", "getitimer", "setitimer", "alarm", "pause",
    "rseq", "membarrier", "kcmp", "syslog", "sysinfo", "reboot",
    "init_module", "finit_module", "delete_module",
    "io_uring_setup", "io_uring_enter", "io_uring_register",
    "io_setup", "io_destroy", "io_submit", "io_cancel", "io_getevents",
    "io_pgetevents", "perf_event_open", "bpf", "seccomp", "landlock_create_ruleset",
    "landlock_add_rule", "landlock_restrict_self",
    "clock_gettime", "clock_settime", "clock_getres", "clock_nanosleep",
    "clock_adjtime", "gettimeofday", "settimeofday", "adjtimex", "nanosleep",
    "getrusage", "wait", "waitpid", "yield", "sleep", "set_priority",
})

_SIGNAL_NAMES: frozenset[str] = frozenset({
    "kill", "tkill", "tgkill", "pidfd_send_signal", "pidfd_open", "pidfd_getfd",
    "rt_sigaction", "rt_sigprocmask", "rt_sigreturn", "rt_sigpending",
    "rt_sigtimedwait", "rt_sigqueueinfo", "rt_sigsuspend", "rt_tgsigqueueinfo",
    "sigaltstack", "signalfd", "signalfd4",
})

_IPC_NAMES: frozenset[str] = frozenset({
    "futex", "futex_waitv", "set_robust_list", "get_robust_list",
    "semget", "semop", "semctl", "semtimedop",
    "msgget", "msgsnd", "msgrcv", "msgctl",
    "shmget", "shmat", "shmdt", "shmctl",
    "mq_open", "mq_unlink", "mq_timedsend", "mq_timedreceive",
    "mq_notify", "mq_getsetattr",
    "process_vm_readv", "process_vm_writev",
    "mutex_create", "mutex_lock", "mutex_unlock", "mutex_blocking_lock",
    "condvar_create", "condvar_signal", "condvar_wait",
    "semaphore_create", "semaphore_up", "semaphore_down",
    "mail_read", "mail_write", "enable_deadlock_detect",
})

_NET_NAMES: frozenset[str] = frozenset({
    "socket", "socketpair", "bind", "listen", "accept", "accept4", "connect",
    "send", "sendto", "sendmsg", "sendmmsg", "recv", "recvfrom", "recvmsg",
    "recvmmsg", "shutdown", "getsockname", "getpeername", "getsockopt",
    "setsockopt",
})

def classify_syscall(name: str) -> Domain:
    """syscall 名 → 6 域 (fs/mm/task/signal/ipc/net) 或 misc."""
    if name in _FS_NAMES:
        return "fs"
    if name in _MM_NAMES:
        return "mm"
    if name in _SIGNAL_NAMES:  # signal 优先于 task (rt_sigreturn 之类)
        return "signal"
    if name in _IPC_NAMES:
        return "ipc"
    if name in _NET_NAMES:
        return "net"
    if name in _TASK_NAMES:
        return "task"
    return "misc"


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass
class SyscallEntry:
    name: str
    id: int | None = None              # 仓库自身声明的 id (rCore 路径) 或反查得到 (ArceOS)
    arch_id: int | None = None         # 标准 RISCV64 表对照值
    file: str = ""                     # 相对 repo_root posix 路径
    line: int = 0
    domain: Domain = "misc"
    kind: Literal["sysno_match", "const_decl", "const_match"] = "sysno_match"


@dataclass
class SyscallFacts:
    arch: Arch = "riscv64"
    backend: str = "rg"               # 当前唯一抽取后端
    count: int = 0
    items: list[SyscallEntry] = field(default_factory=list)
    by_domain: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "arch": self.arch,
            "backend": self.backend,
            "count": self.count,
            "items": [asdict(it) for it in self.items],
            "by_domain": dict(self.by_domain),
            "warnings": self.warnings,
        }


# --------------------------------------------------------------------------- #
# 公共: 排除 glob (与 profiles 同步)
# --------------------------------------------------------------------------- #


def _exclude_globs(profile: KernelProfile | None) -> list[str]:
    base = ["*.rs"]
    base.extend(["!arceos/**", "!apps/**", "!vendor/**", "!.arceos/**",
                 "!user/**", "!target/**", "!.git/**", "!patch/**"])
    if profile is not None:
        for ig in profile.ignore_paths:
            base.append(f"!{ig}/**")
    # 去重 (保留第一次出现顺序)
    seen: set[str] = set()
    out: list[str] = []
    for g in base:
        if g not in seen:
            seen.add(g)
            out.append(g)
    return out


def _rel(p: Path, root: Path) -> str:
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(p)


# --------------------------------------------------------------------------- #
# ArceOS-Starry: Sysno::xxx => sys_xxx
# --------------------------------------------------------------------------- #


_SYSNO_RE = re.compile(r"Sysno::(\w+)")


def _names_from_match(m) -> Iterable[str]:
    """从一条 GrepMatch 里抽出所有 'Sysno::xxx' 的 xxx 名."""
    if m.submatches:
        for _s, _e, sub in m.submatches:
            mm = _SYSNO_RE.search(sub)
            if mm:
                yield mm.group(1)
    else:
        yield from _SYSNO_RE.findall(m.line_text)


def _collect_sysno_names(matches: Iterable) -> dict[str, tuple[Path, int]]:
    """从 grep matches 收集 'Sysno::xxx' 名 → (file, line) (首次出现)."""
    seen: dict[str, tuple[Path, int]] = {}
    for m in matches:
        for n in _names_from_match(m):
            if n not in seen:
                seen[n] = (m.file, m.line_no)
    return seen


def _extract_arceos(repo: Path, profile: KernelProfile | None,
                    arch_table: dict[str, int]) -> tuple[list[SyscallEntry], list[str]]:
    warnings: list[str] = []
    matches = grep(
        r"Sysno::\w+\s*=>",
        repo,
        glob=_exclude_globs(profile),
        workspace_root=repo,
        timeout=120,
    )
    if not matches:
        warnings.append("no 'Sysno::xxx =>' matches; ArceOS path empty")
        return [], warnings

    seen = _collect_sysno_names(matches)
    items: list[SyscallEntry] = []
    for name, (fp, line) in sorted(seen.items()):
        sid = arch_table.get(name)
        if sid is None:
            warnings.append(f"sysno_unknown_in_arch_table: {name}")
        items.append(SyscallEntry(
            name=name,
            id=sid,
            arch_id=sid,
            file=_rel(fp, repo),
            line=line,
            domain=classify_syscall(name),
            kind="sysno_match",
        ))
    return items, warnings


# --------------------------------------------------------------------------- #
# rCore-Tutorial: pub const SYSCALL_X: usize = N + SYSCALL_X => sys_x
# --------------------------------------------------------------------------- #


_CONST_DECL_RE = re.compile(
    r"(?:pub\s+)?const\s+(SYSCALL_[A-Z0-9_]+)\s*:\s*usize\s*=\s*(\d+)"
)
_CONST_MATCH_RE = re.compile(r"\b(SYSCALL_[A-Z0-9_]+)\s*=>")


def _const_to_canonical(macro_name: str, sid: int | None,
                         arch_table: dict[str, int]) -> str:
    """SYSCALL_OPENAT → openat. 全部小写, 去前缀.

    SubsToKernel 这类把 'clock_gettime' 写作 'CLOCKGETTIME' (无下划线)
    导致直查 arch_table 不命中 → 用 id 反查作 ground-truth. 找不到就退回 lower(stripped).
    """
    stripped = macro_name[len("SYSCALL_"):] if macro_name.startswith("SYSCALL_") else macro_name
    lower = stripped.lower()
    if lower in arch_table:
        return lower
    # id 反查 (只在 sid 提供时)
    if sid is not None:
        for n, i in arch_table.items():
            if i == sid:
                return n
    return lower


def _collect_const_decls(decl_matches: Iterable) -> dict[str, tuple[int, Path, int]]:
    """SYSCALL_X 常量声明 → (id, file, line) (首次出现)."""
    declared: dict[str, tuple[int, Path, int]] = {}
    for m in decl_matches:
        mm = _CONST_DECL_RE.search(m.line_text)
        if not mm:
            continue
        try:
            sid = int(mm.group(2))
        except ValueError:
            continue
        macro = mm.group(1)
        if macro not in declared:
            declared[macro] = (sid, m.file, m.line_no)
    return declared


def _collect_const_dispatch(dispatch_matches: Iterable) -> dict[str, tuple[Path, int]]:
    """SYSCALL_X dispatch 站点 → (file, line) (首次出现)."""
    dispatched: dict[str, tuple[Path, int]] = {}
    for m in dispatch_matches:
        for macro in _CONST_MATCH_RE.findall(m.line_text):
            if macro not in dispatched:
                dispatched[macro] = (m.file, m.line_no)
    return dispatched


def _build_rcore_entry(
    macro: str,
    repo: Path,
    declared: dict[str, tuple[int, Path, int]],
    dispatched: dict[str, tuple[Path, int]],
    arch_table: dict[str, int],
    warnings: list[str],
) -> SyscallEntry:
    canonical = _const_to_canonical(
        macro,
        declared[macro][0] if macro in declared else None,
        arch_table,
    )
    arch_id = arch_table.get(canonical)
    if macro in declared:
        sid, file_p, line_no = declared[macro]
        kind: Literal["sysno_match", "const_decl", "const_match"] = (
            "const_match" if macro in dispatched else "const_decl"
        )
    else:
        file_p, line_no = dispatched[macro]
        sid = arch_id
        kind = "const_match"
        warnings.append(f"const_dispatch_no_decl: {macro}")
    if arch_id is not None and sid is not None and sid != arch_id:
        warnings.append(f"const_mismatch: {macro} repo={sid} arch={arch_id}")
    return SyscallEntry(
        name=canonical,
        id=sid,
        arch_id=arch_id,
        file=_rel(file_p, repo),
        line=line_no,
        domain=classify_syscall(canonical),
        kind=kind,
    )


def _extract_rcore(
    repo: Path, profile: KernelProfile | None, arch_table: dict[str, int],
) -> tuple[list[SyscallEntry], list[str]]:
    warnings: list[str] = []
    decl_matches = grep(
        r"(?:pub\s+)?const\s+SYSCALL_\w+\s*:\s*usize\s*=\s*\d+",
        repo, glob=_exclude_globs(profile), workspace_root=repo, timeout=120,
    )
    declared = _collect_const_decls(decl_matches)
    dispatch_matches = grep(
        r"\bSYSCALL_\w+\s*=>",
        repo, glob=_exclude_globs(profile), workspace_root=repo, timeout=120,
    )
    dispatched = _collect_const_dispatch(dispatch_matches)

    items = [
        _build_rcore_entry(macro, repo, declared, dispatched, arch_table, warnings)
        for macro in sorted(set(declared) | set(dispatched))
    ]
    if not items:
        warnings.append("no SYSCALL_xxx const or dispatch found")
    return items, warnings


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #


def scan_syscalls(
    repo_path: Path | str,
    profile: KernelProfile | None = None,
    *,
    arch: Arch = "riscv64",
) -> SyscallFacts:
    """对 repo 跑双家族 syscall 抽取. 永不抛异常.

    profile=None 时按 unknown 处理 (两路都跑, 取并集).
    """
    repo = Path(repo_path).resolve()
    facts = SyscallFacts(arch=arch)
    if not repo.is_dir():
        facts.warnings.append(f"not a directory: {repo}")
        return facts
    if not is_rg_available():
        facts.warnings.append("ripgrep not in PATH; syscall extraction skipped")
        return facts

    arch_table = (
        RISCV64_NAME_TO_ID if arch == "riscv64" else LOONGARCH64_NAME_TO_ID
    )

    family = profile.family if profile else "unknown"
    if family == "arceos-starry":
        items, warns = _extract_arceos(repo, profile, arch_table)
    elif family == "rcore-tutorial":
        items, warns = _extract_rcore(repo, profile, arch_table)
    else:
        a_items, a_warns = _extract_arceos(repo, profile, arch_table)
        r_items, r_warns = _extract_rcore(repo, profile, arch_table)
        items = a_items + r_items
        warns = ["family=unknown; tried both paths"] + a_warns + r_warns

    facts.items = items
    facts.count = len(items)
    facts.warnings = warns
    facts.by_domain = dict(Counter(it.domain for it in items))
    log.info(
        "syscalls_scan_done",
        repo=repo.name,
        family=family,
        count=facts.count,
        domains=facts.by_domain,
        warnings=len(facts.warnings),
    )
    return facts


__all__ = [
    "Arch",
    "Domain",
    "SyscallEntry",
    "SyscallFacts",
    "classify_syscall",
    "scan_syscalls",
]
