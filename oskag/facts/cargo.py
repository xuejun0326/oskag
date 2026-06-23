"""facts/cargo.py — Cargo workspace + 依赖抽取.

策略:
1. 优先 `cargo metadata --no-deps --format-version=1` (准确, 含解析后的版本号 / dep features)
2. cargo 不可用 → toml 库 fallback:
   - 找根 Cargo.toml (没有则探测 os/Cargo.toml 单 crate 风格, e.g. SubsToKernel)
   - 解析 workspace.members + exclude (支持 glob, 如 "modules/*")
   - 逐 member 读 [dependencies] / [dev-dependencies] / [build-dependencies]
   - 顺带读 [target.'cfg(...)'.dependencies]
   - 收集 [workspace.dependencies] 声明 (StarryX 风格)
   - 应用 profile.ignore_paths 过滤 vendor/arceos/apps

输出: CargoFacts dataclass, 含 workspace_members / workspace_deps / per-member deps /
all_deps_union / backend.

不抛异常, 失败写 warnings.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import toml

from oskag.facts.profiles import KernelProfile
from oskag.logging_setup import get_logger
from oskag.tools._subprocess import is_available, run_tool
from oskag.tools.fs import DEFAULT_IGNORE, read_file

log = get_logger("oskag.facts.cargo")


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #


@dataclass
class CargoCrate:
    """单个 member crate 的信息."""

    name: str
    version: str | None = None
    manifest_path: str = ""              # 相对 repo_root 的 posix 路径
    deps: list[str] = field(default_factory=list)
    dev_deps: list[str] = field(default_factory=list)
    build_deps: list[str] = field(default_factory=list)


@dataclass
class CargoFacts:
    """整个仓库 cargo 视角."""

    is_workspace: bool = False
    workspace_members: list[str] = field(default_factory=list)
    workspace_excludes: list[str] = field(default_factory=list)
    workspace_deps: list[str] = field(default_factory=list)   # workspace.dependencies key 集合
    members: list[CargoCrate] = field(default_factory=list)
    all_deps_union: list[str] = field(default_factory=list)
    backend: str = "toml_fallback"        # "cargo_metadata" / "toml_fallback"
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


_DEPS_TABLES = ("dependencies", "dev-dependencies", "build-dependencies")


def _collect_deps_from_section(section: dict | None) -> list[str]:
    """从 [dependencies] / [target.cfg.dependencies] 之类的 dict 里取 key 列表."""
    if not isinstance(section, dict):
        return []
    return sorted(section.keys())


def _collect_target_deps(pkg: dict) -> dict[str, list[str]]:
    """处理 [target.'cfg(...)'.dependencies] 之类的嵌套 target 表.

    返回 {table_name: [dep_names]} 形如 {"dependencies": [...], "dev-dependencies": [...]}.
    """
    out: dict[str, list[str]] = {t: [] for t in _DEPS_TABLES}
    target = pkg.get("target")
    if not isinstance(target, dict):
        return out
    for _cfg, body in target.items():
        if not isinstance(body, dict):
            continue
        for table in _DEPS_TABLES:
            out[table].extend(_collect_deps_from_section(body.get(table)))
    # 去重保序
    for table, names in out.items():
        seen: set[str] = set()
        deduped: list[str] = []
        for n in names:
            if n in seen:
                continue
            seen.add(n)
            deduped.append(n)
        out[table] = deduped
    return out


def _parse_member_manifest(manifest: Path, repo: Path) -> CargoCrate | None:
    """读 member 的 Cargo.toml, 提取 name/version/deps. 失败返回 None."""
    fr = read_file(manifest, max_bytes=200_000)
    if fr.error:
        return None
    try:
        data = toml.loads(fr.text)
    except ValueError as e:
        log.warning("manifest_parse_failed", path=str(manifest), error=str(e)[:160])
        return None

    pkg = data.get("package") if isinstance(data.get("package"), dict) else {}
    name = pkg.get("name") or manifest.parent.name
    version = pkg.get("version")
    if isinstance(version, dict):  # workspace = true 形式
        version = None

    crate = CargoCrate(
        name=str(name),
        version=str(version) if isinstance(version, str) else None,
    )
    try:
        crate.manifest_path = manifest.relative_to(repo).as_posix()
    except ValueError:
        crate.manifest_path = str(manifest)

    crate.deps = _collect_deps_from_section(data.get("dependencies"))
    crate.dev_deps = _collect_deps_from_section(data.get("dev-dependencies"))
    crate.build_deps = _collect_deps_from_section(data.get("build-dependencies"))

    target_deps = _collect_target_deps(data)
    # target.<cfg>.deps 合并到主 deps (按 table)
    crate.deps = sorted(set(crate.deps) | set(target_deps["dependencies"]))
    crate.dev_deps = sorted(set(crate.dev_deps) | set(target_deps["dev-dependencies"]))
    crate.build_deps = sorted(set(crate.build_deps) | set(target_deps["build-dependencies"]))
    return crate


# --------------------------------------------------------------------------- #
# member 路径展开 (含 glob)
# --------------------------------------------------------------------------- #


def _expand_member_glob(repo: Path, pattern: str) -> list[Path]:
    """展开 workspace.members 中的 'modules/*' 一类 glob.

    返回每条解析出的绝对目录路径; 不存在的路径自动跳过.
    """
    if "*" not in pattern and "?" not in pattern:
        candidate = (repo / pattern).resolve()
        return [candidate] if candidate.is_dir() else []
    out: list[Path] = []
    try:
        for p in repo.glob(pattern):
            if p.is_dir():
                out.append(p.resolve())
    except (OSError, ValueError):
        return []
    return sorted(out)


def _is_path_ignored(rel_parts: tuple[str, ...], ignore: set[str]) -> bool:
    """rel_parts 中任一段命中 ignore 即视为忽略."""
    return any(part in ignore or part.startswith(".") for part in rel_parts)


def _discover_members(
    repo: Path, root_data: dict | None, ignore: set[str]
) -> list[Path]:
    """从根 Cargo.toml 找出所有 member Cargo.toml 的路径.

    - 有 workspace.members → 展开 (含 glob)
    - 无 workspace → 把根 Cargo.toml 自己当一个 member
    - 都没有 → 探测 os/Cargo.toml (SubsToKernel 风格)
    """
    members: list[Path] = []
    seen: set[Path] = set()

    def _add(manifest: Path) -> None:
        try:
            rel = manifest.relative_to(repo).parts
        except ValueError:
            return
        if _is_path_ignored(rel, ignore):
            return
        rp = manifest.resolve()
        if rp in seen or not rp.is_file():
            return
        seen.add(rp)
        members.append(rp)

    if root_data is None:
        # SubsToKernel 风格: 没有根 Cargo.toml, 探测常见单 crate 目录
        for guess in ("os", "kernel"):
            cand = repo / guess / "Cargo.toml"
            if cand.is_file():
                _add(cand)
        return members

    ws = root_data.get("workspace")
    if isinstance(ws, dict):
        members_decl = ws.get("members") or []
        for pat in members_decl:
            if not isinstance(pat, str):
                continue
            for member_dir in _expand_member_glob(repo, pat):
                _add(member_dir / "Cargo.toml")
    # workspace 也可以同时是一个 package
    if "package" in root_data:
        _add(repo / "Cargo.toml")
    return members


# --------------------------------------------------------------------------- #
# cargo metadata 路径
# --------------------------------------------------------------------------- #


def _try_cargo_metadata(repo: Path) -> CargoFacts | None:
    """跑 cargo metadata --no-deps. 失败 / 不可用返回 None."""
    if not is_available("cargo"):
        return None
    res = run_tool(
        ["cargo", "metadata", "--no-deps", "--format-version=1"],
        cwd=repo, timeout=120,
    )
    if not res.ok or not res.stdout:
        log.warning("cargo_metadata_failed", returncode=res.returncode,
                    stderr_preview=res.stderr[:160])
        return None
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as e:
        log.warning("cargo_metadata_json_failed", error=str(e))
        return None

    facts = CargoFacts(backend="cargo_metadata")
    facts.workspace_members = [str(m) for m in data.get("workspace_members", [])]
    facts.is_workspace = len(facts.workspace_members) > 1 or bool(data.get("workspace_root"))

    union: set[str] = set()
    for pkg in data.get("packages", []):
        if not isinstance(pkg, dict):
            continue
        crate = CargoCrate(
            name=pkg.get("name", ""),
            version=pkg.get("version"),
            manifest_path=pkg.get("manifest_path", ""),
        )
        for dep in pkg.get("dependencies", []):
            if not isinstance(dep, dict):
                continue
            kind = dep.get("kind") or "normal"
            n = dep.get("name", "")
            if not n:
                continue
            if kind == "dev":
                crate.dev_deps.append(n)
            elif kind == "build":
                crate.build_deps.append(n)
            else:
                crate.deps.append(n)
            union.add(n)
        crate.deps = sorted(set(crate.deps))
        crate.dev_deps = sorted(set(crate.dev_deps))
        crate.build_deps = sorted(set(crate.build_deps))
        facts.members.append(crate)

    facts.all_deps_union = sorted(union)
    return facts


# --------------------------------------------------------------------------- #
# 主入口: scan_cargo
# --------------------------------------------------------------------------- #


def scan_cargo(repo_path: Path | str, profile: KernelProfile | None = None) -> CargoFacts:
    """收集仓库 cargo facts. 优先 cargo metadata, 不可用走 toml fallback.

    profile.ignore_paths 用来过滤 vendor/arceos/apps 等 vendored 子树.
    永不抛异常, 失败写 warnings.
    """
    repo = Path(repo_path).resolve()
    facts = CargoFacts()

    if not repo.is_dir():
        facts.warnings.append(f"not a directory: {repo}")
        return facts

    # 优先 cargo metadata
    cm = _try_cargo_metadata(repo)
    if cm is not None:
        return cm

    # toml fallback
    ignore = set(DEFAULT_IGNORE)
    if profile is not None:
        ignore |= set(profile.ignore_paths)

    root_toml = repo / "Cargo.toml"
    root_data: dict | None = None
    if root_toml.is_file():
        fr = read_file(root_toml, max_bytes=200_000)
        if not fr.error:
            try:
                root_data = toml.loads(fr.text)
            except ValueError as e:
                facts.warnings.append(f"root_cargo_toml_parse_failed: {e!s}"[:240])

    if root_data is not None:
        ws = root_data.get("workspace")
        if isinstance(ws, dict):
            facts.is_workspace = True
            facts.workspace_members = [str(m) for m in ws.get("members", [])]
            facts.workspace_excludes = [str(e) for e in ws.get("exclude", [])]
            ws_deps = ws.get("dependencies")
            facts.workspace_deps = _collect_deps_from_section(ws_deps)

    member_manifests = _discover_members(repo, root_data, ignore)
    union: set[str] = set()
    for m in member_manifests:
        crate = _parse_member_manifest(m, repo)
        if crate is None:
            facts.warnings.append(f"member_parse_failed: {m.relative_to(repo).as_posix()}")
            continue
        facts.members.append(crate)
        union.update(crate.deps)
        union.update(crate.dev_deps)
        union.update(crate.build_deps)
    # workspace.dependencies 也并入 union (可能 member 用 dep.workspace=true 引用)
    union.update(facts.workspace_deps)
    facts.all_deps_union = sorted(union)

    if not facts.members and not facts.workspace_deps:
        facts.warnings.append("no Cargo.toml found in repo (or all filtered by ignore)")

    log.info(
        "cargo_scan_done",
        repo=repo.name,
        backend=facts.backend,
        is_workspace=facts.is_workspace,
        members=len(facts.members),
        deps_union=len(facts.all_deps_union),
        warnings=len(facts.warnings),
    )
    return facts


__all__ = [
    "CargoCrate",
    "CargoFacts",
    "scan_cargo",
]
