"""统一子进程封装.

设计要点:
- 永不 shell=True (Windows 路径含引号字符 + 防 injection)
- env 强制 PYTHONUTF8=1 + PYTHONIOENCODING=utf-8
- 命令以 list 形式传入, 避免参数转义
- 超时 / 命令不存在 / 非零退出码统一返回 ToolResult, 不抛异常 (调用方检查 .ok)
- 工具不存在用 shutil.which 提前探测 + cache, 避免重复 spawn

借鉴 llm.py 的 _coerce_message_dict 思路: 单一进入点, 屏蔽实现细节.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from oskag.logging_setup import get_logger

log = get_logger("oskag.tools.subprocess")

_DEFAULT_TIMEOUT = 30
# 仅这两个被强制覆盖, 其他 env 全部继承父进程
_FORCE_ENV = {
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
}


@dataclass
class ToolResult:
    """子进程调用结果. 调用方先检查 .ok, 再读 .stdout/.stderr."""

    ok: bool
    returncode: int
    stdout: str
    stderr: str
    cmd: list[str] = field(default_factory=list)
    error_kind: str | None = None  # not_found / timeout / exec_error / nonzero / None
    error_message: str | None = None

    @classmethod
    def error(
        cls,
        kind: str,
        message: str,
        *,
        cmd: list[str] | None = None,
        returncode: int = -1,
        stderr: str = "",
    ) -> "ToolResult":
        return cls(
            ok=False,
            returncode=returncode,
            stdout="",
            stderr=stderr,
            cmd=list(cmd) if cmd else [],
            error_kind=kind,
            error_message=message,
        )


@lru_cache(maxsize=64)
def which(cmd: str) -> str | None:
    """缓存版 shutil.which. 跨 Windows / Unix."""
    return shutil.which(cmd)


def is_available(cmd: str) -> bool:
    """命令是否在 PATH 中存在."""
    return which(cmd) is not None


def run_tool(
    cmd: list[str],
    *,
    cwd: Path | str | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
    extra_env: dict[str, str] | None = None,
    input_text: str | None = None,
    check: bool = False,
) -> ToolResult:
    """执行外部命令, 返回标准化结果.

    cmd[0] 必须是命令名 (相对/绝对路径或 PATH 中的可执行).
    cwd 可省, 默认进程当前目录.
    timeout 默认 30 秒, 命令长跑请显式传更大值.
    extra_env 与父进程 env 合并, 同名键以 extra_env 为准, 但 _FORCE_ENV 永远赢.
    check=True 时, 非零退出码也视为失败 (默认 False, 让调用方自己判断).

    Windows 兼容: 若 which() 解析出 .cmd / .bat / .ps1, 自动包 `cmd /c` 调用.
    npm 全局装的 cli (rg / sg) 实际是 .CMD shim, 直接 spawn 不稳定.

    永不 shell=True. Windows / POSIX 通用.
    """
    if not cmd:
        return ToolResult.error("exec_error", "empty command", cmd=cmd)

    # 1. 提前检查可执行存在
    resolved = which(cmd[0])
    if resolved is None:
        log.warning("tool_not_found", cmd=cmd[0])
        return ToolResult.error("not_found", f"command not found: {cmd[0]}", cmd=cmd)

    # 2. 组装 env: 父进程 env > extra_env > _FORCE_ENV (后者永远覆盖)
    env: dict[str, str] = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    env.update(_FORCE_ENV)

    # 3. Windows .cmd / .bat shim 包 cmd /c
    real_cmd = _wrap_windows_shim(resolved, cmd[1:])

    try:
        proc = subprocess.run(
            real_cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",  # 不让单个解码错误炸整个命令
            timeout=timeout,
            env=env,
            input=input_text,
            check=False,  # 我们自己判断 returncode, 不让 subprocess 抛
        )
    except subprocess.TimeoutExpired as e:
        log.warning("tool_timeout", cmd=cmd[0], timeout=timeout)
        return ToolResult.error(
            "timeout",
            f"timed out after {timeout}s",
            cmd=cmd,
            stderr=str(e),
        )
    except OSError as e:
        log.warning("tool_exec_error", cmd=cmd[0], error=str(e)[:160])
        return ToolResult.error("exec_error", str(e), cmd=cmd)

    # 4. 评估结果
    is_ok = proc.returncode == 0
    if not is_ok and check:
        log.warning(
            "tool_nonzero_exit",
            cmd=cmd[0],
            returncode=proc.returncode,
            stderr_preview=proc.stderr[:240] if proc.stderr else "",
        )
    return ToolResult(
        ok=is_ok,
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        cmd=cmd,
        error_kind=None if is_ok else "nonzero",
        error_message=None if is_ok else f"exit code {proc.returncode}",
    )


def _wrap_windows_shim(resolved: str, args: list[str]) -> list[str]:
    """Windows 下 npm shim (.cmd/.bat) 直接 spawn 不稳定.

    若 resolved 后缀是 .cmd / .bat / .ps1, 包 cmd /c <shim> <args> 调用.
    其他平台 (Linux/macOS) 或 .exe 直接走原命令.

    传给 cmd /c 时, 路径若含特殊字符需要引号; subprocess 走 list 形式时
    Python 自动用 list2cmdline 处理转义, 我们不需要手动 quote.
    """
    if os.name != "nt":
        return [resolved, *args]
    suffix = os.path.splitext(resolved)[1].lower()
    if suffix in {".cmd", ".bat"}:
        # cmd /c <shim> <args>
        return ["cmd", "/c", resolved, *args]
    return [resolved, *args]


def assert_inside(path: Path | str, root: Path | str) -> Path:
    """校验 path 落在 root 之下 (resolve 后比较). 不通过则 raise ValueError.

    防止工具调用越权访问 workspace 之外的路径.
    返回 resolve 后的绝对 Path 供调用方使用.
    """
    p = Path(path).resolve()
    r = Path(root).resolve()
    try:
        # Python 3.9+
        if not p.is_relative_to(r):  # type: ignore[attr-defined]
            raise ValueError(f"path {p} is not inside workspace {r}")
    except AttributeError:
        # 兜底: 旧 Python (我们 3.14, 实际不会走这里)
        try:
            p.relative_to(r)
        except ValueError as e:
            raise ValueError(f"path {p} is not inside workspace {r}") from e
    return p


__all__ = [
    "ToolResult",
    "run_tool",
    "which",
    "is_available",
    "assert_inside",
]
