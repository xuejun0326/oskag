"""facts/scan.py — 仓库扫描统筹.

范围: repo_stats (LOC / dir_tree / rust-toolchain / Makefile targets / git commits).
~S10 会在 scan() 顶层 dict 里填充 cargo / syscalls / boot / repomap.

设计要点:
- 永不抛异常: 子项失败写入 warnings/errors, 永不让一个失败拖垮整轮 scan
- 每个子收集器单独可调 (供未来 cli 子命令灵活组合)
- profile 必传给 scan_repo_stats, 用其 ignore_paths 过滤 dir_tree
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from oskag import __version__
from oskag.facts.boot import scan_boot
from oskag.facts.cargo import scan_cargo
from oskag.facts.profiles import KernelProfile, detect_family
from oskag.facts.project_type import infer_project_type
from oskag.facts.repomap import scan_repomap
from oskag.facts.syscalls import scan_syscalls
from oskag.logging_setup import get_logger
from oskag.tools._subprocess import is_available, run_tool
from oskag.tools.fs import DEFAULT_IGNORE, list_dir, read_file
from oskag.tools.loc import scan_loc

log = get_logger("oskag.facts.scan")

SCHEMA_VERSION = 1


# --------------------------------------------------------------------------- #
# RepoStats dataclass
# --------------------------------------------------------------------------- #


@dataclass
class RepoStats:
    """输出: 仓库基本统计.

    所有字段在 facts.json 的 "repo_stats" 节点下.
    """

    loc: dict = field(default_factory=dict)              # LocReport.to_dict()
    dir_tree: list[dict] = field(default_factory=list)
    rust_toolchain: dict | None = None                   # {channel, components, ...}
    makefile_targets: list[str] = field(default_factory=list)
    git_commits: int | None = None
    git_head_short: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Makefile target 提取
# --------------------------------------------------------------------------- #


# 行首 (非缩进) 的 target 名, 后跟 ":" 但不是 ":=" / "::=".
# 排除 .PHONY / .DEFAULT 等以 "." 开头的隐式 target (用业务逻辑过滤)
_MAKEFILE_TARGET_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_\-]*)\s*:(?!=)")


def _extract_targets_from_text(text: str, seen: set[str], targets: list[str]) -> None:
    """从 Makefile 文本累加 target 到 targets (顺序保留, seen 去重)."""
    for line in text.splitlines():
        if not line or line[0] in (" ", "\t", "#"):
            continue
        m = _MAKEFILE_TARGET_RE.match(line)
        if m is None:
            continue
        t = m.group(1)
        if t in seen or t.startswith("."):
            continue
        seen.add(t)
        targets.append(t)


def _read_makefile_targets(repo: Path) -> list[str]:
    """扫顶层 Makefile / makefile / GNUmakefile, 返回 target 名 (按出现顺序去重).

    跳过缩进行 (recipe), 跳注释, 跳 := 赋值, 跳 . 开头.
    """
    targets: list[str] = []
    seen: set[str] = set()
    for fname in ("Makefile", "makefile", "GNUmakefile"):
        mf = repo / fname
        if not mf.is_file():
            continue
        fr = read_file(mf, max_bytes=200_000)
        if fr.error:
            continue
        _extract_targets_from_text(fr.text, seen, targets)
    return targets


# --------------------------------------------------------------------------- #
# rust-toolchain 解析
# --------------------------------------------------------------------------- #


def _read_rust_toolchain(repo: Path) -> tuple[dict | None, str | None]:
    """读 rust-toolchain.toml 或旧式 rust-toolchain (单行 channel).

    返回 (toolchain_dict_or_None, error_message_or_None).
    """
    toml_path = repo / "rust-toolchain.toml"
    if toml_path.is_file():
        fr = read_file(toml_path, max_bytes=64_000)
        if fr.error:
            return None, fr.error
        try:
            import toml
            data = toml.loads(fr.text)
        except ValueError as e:
            return {"raw": fr.text}, f"toml_parse_failed: {e}"
        # 标准 [toolchain] 节; 也兼容直接顶层放字段的旧格式
        return data.get("toolchain", data), None

    plain = repo / "rust-toolchain"
    if plain.is_file():
        fr = read_file(plain, max_bytes=4_000)
        if fr.error:
            return None, fr.error
        return {"channel": fr.text.strip()}, None

    return None, None


# --------------------------------------------------------------------------- #
# git 统计
# --------------------------------------------------------------------------- #


def _git_stats(repo: Path) -> tuple[int | None, str | None, list[str]]:
    """git rev-list --count HEAD + git rev-parse --short HEAD.

    返回 (commits, head_short, warnings_list). 无 .git / 无 git 命令时返回 (None, None, [...]).
    """
    warnings: list[str] = []
    if not (repo / ".git").exists():
        return None, None, warnings
    if not is_available("git"):
        warnings.append("git not in PATH; skipped commit count")
        return None, None, warnings

    res = run_tool(["git", "rev-list", "--count", "HEAD"], cwd=repo, timeout=15)
    commits: int | None = None
    if res.ok and res.stdout.strip().isdigit():
        commits = int(res.stdout.strip())
    elif not res.ok:
        warnings.append(f"git rev-list failed: {res.stderr[:160].strip()}")

    res2 = run_tool(["git", "rev-parse", "--short", "HEAD"], cwd=repo, timeout=15)
    head_short: str | None = None
    if res2.ok and res2.stdout.strip():
        head_short = res2.stdout.strip()

    return commits, head_short, warnings

# --------------------------------------------------------------------------- #
# scan_repo_stats — 主入口
# --------------------------------------------------------------------------- #


def scan_repo_stats(
    repo_path: Path | str,
    profile: KernelProfile | None = None,
    *,
    dir_tree_depth: int = 2,
) -> RepoStats:
    """收集仓库基本统计. 不抛异常, 失败写 warnings.

    profile=None 时使用 DEFAULT_IGNORE; 否则用 profile.ignore_paths.
    """
    repo = Path(repo_path).resolve()
    stats = RepoStats()

    if not repo.is_dir():
        stats.warnings.append(f"not a directory: {repo}")
        return stats

    # 1) LOC
    try:
        loc_report = scan_loc(repo)
        stats.loc = loc_report.to_dict()
    except Exception as e:  # 防御: scan_loc 已 noexcept, 但底层第三方仍可能抛
        stats.warnings.append(f"loc_failed: {e!s}"[:240])
        log.warning("loc_failed", error=str(e)[:160])

    # 2) dir_tree (应用 profile.ignore_paths)
    ignore = (
        set(profile.ignore_paths) | set(DEFAULT_IGNORE) if profile else set(DEFAULT_IGNORE)
    )
    try:
        stats.dir_tree = list_dir(repo, depth=dir_tree_depth, ignore=ignore)
    except Exception as e:
        stats.warnings.append(f"dir_tree_failed: {e!s}"[:240])

    # 3) rust-toolchain
    try:
        rt, rt_err = _read_rust_toolchain(repo)
        stats.rust_toolchain = rt
        if rt_err:
            stats.warnings.append(f"rust_toolchain: {rt_err}")
    except Exception as e:
        stats.warnings.append(f"rust_toolchain_failed: {e!s}"[:240])

    # 4) Makefile targets
    try:
        stats.makefile_targets = _read_makefile_targets(repo)
    except Exception as e:
        stats.warnings.append(f"makefile_failed: {e!s}"[:240])

    # 5) git
    try:
        commits, head_short, git_warns = _git_stats(repo)
        stats.git_commits = commits
        stats.git_head_short = head_short
        stats.warnings.extend(git_warns)
    except Exception as e:
        stats.warnings.append(f"git_failed: {e!s}"[:240])

    log.info(
        "repo_stats_done",
        repo=repo.name,
        loc_files=stats.loc.get("total_files"),
        loc_code=stats.loc.get("total_code"),
        dir_entries=len(stats.dir_tree),
        targets=len(stats.makefile_targets),
        commits=stats.git_commits,
        warnings=len(stats.warnings),
    )
    return stats


# --------------------------------------------------------------------------- #
# scan — 完整 facts.json 入口 (S7-S10 会扩展子节点)
# --------------------------------------------------------------------------- #


def scan(repo_path: Path | str, *, out: Path | str | None = None) -> dict:
    """对一个 repo 跑全套确定性事实抽取, 返回 facts dict (并可选写出 JSON).

    阶段: 已填 meta + profile + repo_stats. cargo/syscalls/boot/repomap 占位.
    """
    repo = Path(repo_path).resolve()
    errors: list[str] = []
    warnings: list[str] = []

    try:
        profile = detect_family(repo)
    except Exception as e:
        errors.append(f"profile_failed: {e!s}"[:240])
        log.warning("profile_failed", error=str(e)[:160])
        profile = KernelProfile(repo_root=repo, repo_name=repo.name)

    stats = scan_repo_stats(repo, profile)
    warnings.extend(stats.warnings)

    try:
        cargo_facts = scan_cargo(repo, profile)
        cargo_dict = cargo_facts.to_dict()
        warnings.extend(cargo_facts.warnings)
    except Exception as e:
        errors.append(f"cargo_failed: {e!s}"[:240])
        log.warning("cargo_failed", error=str(e)[:160])
        cargo_dict = {}

    try:
        syscall_facts = scan_syscalls(repo, profile)
        syscalls_dict = syscall_facts.to_dict()
        warnings.extend(w for w in syscall_facts.warnings if not w.startswith("sysno_unknown_in_arch_table"))
    except Exception as e:
        errors.append(f"syscalls_failed: {e!s}"[:240])
        log.warning("syscalls_failed", error=str(e)[:160])
        syscalls_dict = {}

    try:
        repomap_facts = scan_repomap(repo, profile)
        repomap_text = repomap_facts.text
        warnings.extend(repomap_facts.warnings)
    except Exception as e:
        errors.append(f"repomap_failed: {e!s}"[:240])
        log.warning("repomap_failed", error=str(e)[:160])
        repomap_text = ""

    try:
        boot_facts = scan_boot(repo, profile)
        boot_dict = boot_facts.to_dict()
        warnings.extend(boot_facts.warnings)
    except Exception as e:
        errors.append(f"boot_failed: {e!s}"[:240])
        log.warning("boot_failed", error=str(e)[:160])
        boot_dict = {}

    # 推断项目类型 (自由模式下给 LLM 分析维度提示)
    try:
        project_type_dict = infer_project_type(repo)
    except Exception as e:
        log.warning("project_type_failed", error=str(e)[:160])
        project_type_dict = {"type": "unknown", "dimensions": []}

    facts = {
        "meta": {
            "schema_version": SCHEMA_VERSION,
            "scanned_at": datetime.now(timezone.utc).isoformat(),
            "oskag_version": __version__,
            "repo_name": repo.name,
            "repo_root": str(repo),
            "family": profile.family,
            "errors": errors,
            "warnings": warnings,
        },
        "profile": profile.to_dict(),
        "repo_stats": stats.to_dict(),
        "cargo": cargo_dict,
        "syscalls": syscalls_dict,
        "boot": boot_dict,
        "repomap": repomap_text,
        "project_type": project_type_dict,
    }

    if out is not None:
        out_path = Path(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(facts, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info("facts_written", path=str(out_path), bytes=out_path.stat().st_size)

    return facts


__all__ = [
    "SCHEMA_VERSION",
    "RepoStats",
    "scan_repo_stats",
    "scan",
]
