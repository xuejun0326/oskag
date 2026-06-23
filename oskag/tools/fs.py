"""文件系统工具: read_file / list_dir / grep (通过 rg).

设计要点:
- 所有路径必须落在 workspace_root 之下 (assert_inside)
- read_file 默认 max_bytes=2MB, 超出截断并标记 truncated
- grep 用 ripgrep --json 输出, 解析为标准 GrepMatch 列表
- 跳过常见 ignore 路径 (target, node_modules, .git, vendor, ...)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from oskag.logging_setup import get_logger
from oskag.tools._subprocess import ToolResult, assert_inside, is_available, run_tool

log = get_logger("oskag.tools.fs")

DEFAULT_MAX_BYTES = 2_000_000  # 2MB

# 默认忽略路径 (工具调用 / 扫描时统一适用)
DEFAULT_IGNORE: frozenset[str] = frozenset({
    ".git",
    ".github",
    "target",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".pytest-tmp",
    ".vscode",
    ".idea",
    "dist",
    "build",
    ".venv",
    "venv",
    "vendor",  # 在 facts 阶段会单独处理 vendor
    ".cargo",
    ".rustup",
})


@dataclass
class FileRead:
    """read_file 的结果."""

    path: Path
    text: str
    truncated: bool = False
    size_bytes: int = 0
    error: str | None = None


@dataclass
class GrepMatch:
    """rg --json 输出的一条 match 标准化结果."""

    file: Path
    line_no: int
    column_start: int
    column_end: int
    line_text: str
    submatches: list[tuple[int, int, str]] = field(default_factory=list)


def read_file(
    path: Path | str,
    *,
    workspace_root: Path | str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    encoding: str = "utf-8",
) -> FileRead:
    """读文本文件. 不存在/权限错误时返回带 error 的 FileRead, 不抛异常.

    workspace_root 不为 None 时检查路径越权.
    超过 max_bytes 时只读前 max_bytes, truncated=True.
    """
    p = Path(path)
    if workspace_root is not None:
        try:
            p = assert_inside(p, workspace_root)
        except ValueError as e:
            return FileRead(path=p, text="", error=str(e))

    if not p.exists():
        return FileRead(path=p, text="", error=f"not found: {p}")
    if not p.is_file():
        return FileRead(path=p, text="", error=f"not a file: {p}")

    try:
        size = p.stat().st_size
    except OSError as e:
        return FileRead(path=p, text="", error=str(e))

    truncated = size > max_bytes
    try:
        with p.open("rb") as f:
            raw = f.read(max_bytes)
        text = raw.decode(encoding, errors="replace")
    except OSError as e:
        return FileRead(path=p, text="", size_bytes=size, error=str(e))

    return FileRead(path=p, text=text, truncated=truncated, size_bytes=size)


def list_dir(
    path: Path | str,
    *,
    depth: int = 1,
    ignore: Iterable[str] = DEFAULT_IGNORE,
    workspace_root: Path | str | None = None,
) -> list[dict]:
    """列举目录, 限制深度, 跳过 ignore 名.

    返回 list[dict], 每个 dict 含 {name, type:'dir'|'file', size, rel_path}.
    rel_path 相对 path 本身.
    depth=1 表示只列直接子项; depth=2 进一层; ...
    """
    base = Path(path)
    if workspace_root is not None:
        base = assert_inside(base, workspace_root)
    if not base.is_dir():
        return []

    ignore_set = set(ignore)
    out: list[dict] = []

    def _walk(cur: Path, cur_depth: int) -> None:
        if cur_depth > depth:
            return
        try:
            entries = sorted(cur.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        except OSError:
            return
        for entry in entries:
            if entry.name in ignore_set or entry.name.startswith("."):
                # . 开头的也跳 (.git/.github/.venv 等)
                if entry.name not in {"."}:
                    continue
            rel = entry.relative_to(base)
            kind = "dir" if entry.is_dir() else "file"
            try:
                size = entry.stat().st_size if entry.is_file() else 0
            except OSError:
                size = 0
            out.append({
                "name": entry.name,
                "type": kind,
                "size": size,
                "rel_path": str(rel).replace("\\", "/"),
                "depth": cur_depth,
            })
            if entry.is_dir() and cur_depth < depth:
                _walk(entry, cur_depth + 1)

    _walk(base, 1)
    return out


def grep(
    pattern: str,
    path: Path | str,
    *,
    glob: str | list[str] | None = None,
    case_sensitive: bool = True,
    fixed_string: bool = False,
    multiline: bool = False,
    max_count: int | None = None,
    workspace_root: Path | str | None = None,
    timeout: int = 60,
) -> list[GrepMatch]:
    """调用 rg --json 搜索并解析结果. rg 不可用时返回 [] (调用方自检 is_rg_available()).

    glob: 单个 pattern 或多个; e.g. "*.rs" 或 ["*.rs", "*.toml"].
    """
    target = Path(path)
    if workspace_root is not None:
        target = assert_inside(target, workspace_root)
    if not is_available("rg"):
        log.warning("grep_skipped_rg_missing", pattern=pattern[:80])
        return []

    cmd: list[str] = ["rg", "--json", "--no-config"]
    if not case_sensitive:
        cmd.append("-i")
    if fixed_string:
        cmd.append("-F")
    if multiline:
        cmd.append("-U")
    if max_count is not None:
        cmd.extend(["-m", str(max_count)])
    if glob:
        if isinstance(glob, str):
            cmd.extend(["-g", glob])
        else:
            for g in glob:
                cmd.extend(["-g", g])
    cmd.extend(["-e", pattern, str(target)])

    res = run_tool(cmd, timeout=timeout)
    # rg 退出码: 0=有匹配, 1=无匹配, 2=错误
    if res.error_kind == "not_found":
        return []
    if res.returncode == 2:
        log.warning(
            "grep_rg_error",
            pattern=pattern[:80],
            stderr_preview=res.stderr[:240],
        )
        return []
    if not res.stdout:
        return []

    return list(_parse_rg_json(res.stdout))


def _parse_rg_json(stdout: str) -> Iterator[GrepMatch]:
    """解析 rg --json 输出 (每行一个 JSON 事件)."""
    for raw_line in stdout.splitlines():
        if not raw_line:
            continue
        try:
            evt = json.loads(raw_line)
        except json.JSONDecodeError:
            # 单行格式不对就跳过, 不让一条坏数据拖垮整个调用
            continue
        if evt.get("type") != "match":
            continue
        try:
            data = evt["data"]
            file_path = Path(data["path"]["text"])
            line_no = int(data["line_number"])
            line_text = data["lines"]["text"].rstrip("\n")
            submatches_raw = data.get("submatches", [])
        except (KeyError, TypeError, ValueError):
            continue

        submatches: list[tuple[int, int, str]] = []
        col_start = 0
        col_end = 0
        for sm in submatches_raw:
            try:
                start = int(sm["start"])
                end = int(sm["end"])
                text = sm["match"]["text"]
                submatches.append((start, end, text))
                if not col_start:
                    col_start = start
                col_end = end
            except (KeyError, TypeError, ValueError):
                continue

        yield GrepMatch(
            file=file_path,
            line_no=line_no,
            column_start=col_start,
            column_end=col_end,
            line_text=line_text,
            submatches=submatches,
        )


def is_rg_available() -> bool:
    """rg 是否在 PATH 中. 用于 facts/ 模块判断要不要走 fallback."""
    return is_available("rg")


def list_files(
    path: Path | str,
    *,
    glob: str | list[str] | None = None,
    workspace_root: Path | str | None = None,
    timeout: int = 30,
) -> list[Path]:
    """列出符合 glob 的文件路径, 应用 rg 的 .gitignore 规则. rg 不可用时 fallback Path.rglob."""
    target = Path(path)
    if workspace_root is not None:
        target = assert_inside(target, workspace_root)

    if is_available("rg"):
        cmd: list[str] = ["rg", "--files", "--no-config"]
        if glob:
            if isinstance(glob, str):
                cmd.extend(["-g", glob])
            else:
                for g in glob:
                    cmd.extend(["-g", g])
        cmd.append(str(target))
        res = run_tool(cmd, timeout=timeout)
        if res.ok or res.returncode == 1:
            return [Path(line) for line in res.stdout.splitlines() if line.strip()]

    # fallback: pathlib.rglob, 应用 DEFAULT_IGNORE
    out: list[Path] = []
    patterns = [glob] if isinstance(glob, str) else list(glob) if glob else ["*"]
    ignore_set = set(DEFAULT_IGNORE)

    def _has_ignored_part(p: Path) -> bool:
        return any(part in ignore_set or part.startswith(".") for part in p.parts)

    for pat in patterns:
        for p in target.rglob(pat):
            if p.is_file() and not _has_ignored_part(p.relative_to(target)):
                out.append(p)
    return out


__all__ = [
    "FileRead",
    "GrepMatch",
    "DEFAULT_IGNORE",
    "DEFAULT_MAX_BYTES",
    "read_file",
    "list_dir",
    "list_files",
    "grep",
    "is_rg_available",
]
