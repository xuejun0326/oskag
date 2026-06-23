"""配置加载.

支持三种 .env 格式 (按优先级):
1. **JSON 配置文件** (JSON, 顶层有 `env` dict) — 用户当前格式;
   抽 `env.ANTHROPIC_AUTH_TOKEN` 当 DeepSeek key,
   `env.ANTHROPIC_MODEL` 经 strip-bracket 当 model.
2. **标准 dotenv** (KEY=VALUE) — 通过 python-dotenv 加载.
3. **进程环境变量** — 始终最高优先级.

绝不在日志或异常中回显 api_key 原文; 暴露到外部时一律走 SecretStr / 掩码.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根 = 本文件父目录的父目录 (oskag/oskag/config.py → oskag/)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 工作目录根 = oskag/ 的父目录 (通常是项目所在目录, 用户 .env 放这里)
_WORKDIR_ROOT = _PROJECT_ROOT.parent


def _candidate_dotenv_paths() -> tuple[Path, ...]:
    """按优先级返回 .env 候选路径. 第一次解析时调用 (不缓存, 让用户 cd 后能命中新路径)."""
    candidates: list[Path] = []
    # 1) 进程当前工作目录 (用户在哪里跑 oskag, 哪里的 .env 优先)
    try:
        candidates.append(Path.cwd() / ".env")
    except OSError:
        pass
    # 2) 项目工作根 (发布场景的标准位置)
    candidates.append(_WORKDIR_ROOT / ".env")
    # 3) oskag 包内 (开发/单元测试场景)
    candidates.append(_PROJECT_ROOT / ".env")
    # 去重保序
    seen: set[Path] = set()
    unique: list[Path] = []
    for c in candidates:
        try:
            r = c.resolve()
        except OSError:
            r = c
        if r not in seen:
            seen.add(r)
            unique.append(c)
    return tuple(unique)

# 移除 model id 上的 Anthropic 计费分桶后缀 (例如 "deepseek-v4-pro[1m]" → "deepseek-v4-pro")
_BRACKET_SUFFIX_RE = re.compile(r"\[.*?\]$")


def _strip_bracket_suffix(s: str) -> str:
    return _BRACKET_SUFFIX_RE.sub("", s).strip()


def _mask_secret(value: str, *, head: int = 6, tail: int = 0) -> str:
    """API key 掩码 — 用于 config-check 等需要展示但不能泄漏的场景."""
    if not value:
        return ""
    if len(value) <= head + tail + 3:
        return "***"
    if tail > 0:
        return f"{value[:head]}***{value[-tail:]}"
    return f"{value[:head]}***"


def _load_json_settings(path: Path) -> dict[str, str] | None:
    """识别 JSON 配置文件 (顶层有 env dict 的 JSON), 返回扁平 KEY=VALUE.

    返回 None 表示该文件不是 JSON 配置文件 (调用方应回退 dotenv 解析).
    """
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        # 文件存在但读不出来 — 这是真实问题, 不静默吞
        import warnings
        warnings.warn(f"读取 .env 文件失败: {path} ({type(e).__name__})", stacklevel=2)
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 这是预期分支 (文件是标准 dotenv 而非 JSON), 不报警
        return None
    if not isinstance(data, dict):
        return None
    env = data.get("env")
    if not isinstance(env, dict):
        return None
    return {str(k): str(v) for k, v in env.items() if v is not None}


def _load_dotenv_pairs(path: Path) -> dict[str, str] | None:
    """标准 KEY=VALUE 格式 .env 解析."""
    if not path.is_file():
        return None
    try:
        from dotenv import dotenv_values
    except ImportError:
        import warnings
        warnings.warn("python-dotenv 未安装, 标准 dotenv 格式不可用", stacklevel=2)
        return None
    try:
        pairs = dotenv_values(str(path))
    except (OSError, UnicodeDecodeError) as e:
        import warnings
        warnings.warn(f"dotenv 解析失败: {path} ({type(e).__name__})", stacklevel=2)
        return None
    return {k: v for k, v in pairs.items() if v is not None}


# oskag 自己关心的 key 集合 — 仅这些会被 .env override (避免误伤 PATH 等)
_OSKAG_RELEVANT_KEYS = frozenset({
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL_FLASH",
    "DEEPSEEK_MODEL_PRO",
    "DEEPSEEK_THINKING",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "OSKAG_LOG_LEVEL",
    "OSKAG_CACHE_DIR",
    "OSKAG_LOG_DIR",
})


def _resolve_dotenv_pairs() -> tuple[Path | None, str, dict[str, str]]:
    """寻找 .env 并解析, 返回 (路径, 来源, 平面键值字典). 不写 os.environ."""
    for p in _candidate_dotenv_paths():
        flat = _load_json_settings(p)
        if flat is not None:
            return p, "json", flat
        flat = _load_dotenv_pairs(p)
        if flat is not None:
            return p, "dotenv", flat
    return None, "none", {}


def _build_alias_map(pairs: dict[str, str]) -> dict[str, str]:
    """把 .env 中的 Anthropic 兼容字段映射到 DEEPSEEK_* 字段.

    当 .env 已显式给出 DEEPSEEK_* 时, 同名映射不覆盖.
    """
    out = dict(pairs)
    if "DEEPSEEK_API_KEY" not in out and "ANTHROPIC_AUTH_TOKEN" in out:
        out["DEEPSEEK_API_KEY"] = out["ANTHROPIC_AUTH_TOKEN"]
    if "DEEPSEEK_MODEL_PRO" not in out and "ANTHROPIC_MODEL" in out:
        out["DEEPSEEK_MODEL_PRO"] = out["ANTHROPIC_MODEL"]
    if "DEEPSEEK_MODEL_FLASH" not in out and "ANTHROPIC_DEFAULT_HAIKU_MODEL" in out:
        out["DEEPSEEK_MODEL_FLASH"] = out["ANTHROPIC_DEFAULT_HAIKU_MODEL"]
    return out


def _apply_env_pairs(pairs: dict[str, str], *, override: bool) -> None:
    """把 .env 解析得到的键值应用到 os.environ.

    override=True: 仅 oskag-relevant 的 key 会强覆盖, 其他用 setdefault.
    override=False: 一律 setdefault (经典 12-factor 语义).
    """
    for k, v in pairs.items():
        if override and k in _OSKAG_RELEVANT_KEYS:
            os.environ[k] = v
        else:
            os.environ.setdefault(k, v)


def load_env_into_os_environ(*, override: bool = True) -> tuple[Path | None, str]:
    """寻找 .env, 把它的键写进 os.environ. (向后兼容入口, 见 load_settings.)

    `override=True` (默认): .env 中存在的键会**覆盖**已存在的进程环境变量.
        仅 oskag-relevant 的 key 受影响, 其他 (PATH 等) 不动.

    `override=False`: 已存在的环境变量优先 (12-factor 经典做法).
    """
    if os.environ.get("OSKAG_ENV_OVERRIDE", "1") == "0":
        override = False
    path, source, pairs = _resolve_dotenv_pairs()
    pairs = _build_alias_map(pairs)
    _apply_env_pairs(pairs, override=override)
    return path, source


class Settings(BaseSettings):
    """oskag 运行时配置.

    字段优先级: 进程环境 > .env (JSON 配置 或 dotenv) > 默认值.

    支持双源 key 名:
    - DeepSeek 原生: DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL_*
    - Anthropic 兼容 (JSON 配置文件): ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL / ANTHROPIC_MODEL
      (DeepSeek 同一 key 在 OpenAI 与 Anthropic 兼容端通用; oskag 内部统一走 OpenAI 兼容端 /v1)
    """

    model_config = SettingsConfigDict(
        env_file=None,  # 不让 pydantic 自己读 .env, 我们已在 load_env_into_os_environ 处理
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- DeepSeek 接入 ----
    api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias="DEEPSEEK_API_KEY",
        description="DeepSeek API key. 也可通过 ANTHROPIC_AUTH_TOKEN 提供 (JSON 配置文件常见).",
    )
    base_url: str = Field(
        default="https://api.deepseek.com/v1",
        validation_alias="DEEPSEEK_BASE_URL",
        description="OpenAI 兼容端点. 显式 /v1; 不要走 /anthropic.",
    )
    model_flash: str = Field(
        default="deepseek-v4-flash",
        validation_alias="DEEPSEEK_MODEL_FLASH",
    )
    model_pro: str = Field(
        default="deepseek-v4-pro",
        validation_alias="DEEPSEEK_MODEL_PRO",
    )
    thinking: str = Field(
        default="enabled",
        validation_alias="DEEPSEEK_THINKING",
        description="thinking 开关: enabled / disabled.",
    )

    # ---- 调试 ----
    log_level: str = Field(default="INFO", validation_alias="OSKAG_LOG_LEVEL")
    cache_dir: Path = Field(default=Path("./cache"), validation_alias="OSKAG_CACHE_DIR")
    log_dir: Path = Field(default=Path("./dev-logs"), validation_alias="OSKAG_LOG_DIR")

    # ---- 元信息 ----
    env_source: str = Field(default="none", description="加载来源: json / dotenv / none")
    env_path: Path | None = Field(default=None, description="实际加载的 .env 路径")

    @field_validator("model_flash", "model_pro", mode="before")
    @classmethod
    def _strip_model_brackets(cls, v: Any) -> Any:
        """去掉 [1m] 等 Anthropic 计费分桶后缀, OpenAI 端不接受."""
        if isinstance(v, str):
            return _strip_bracket_suffix(v)
        return v

    @field_validator("base_url", mode="before")
    @classmethod
    def _normalize_base_url(cls, v: Any) -> Any:
        """如果用户填了 anthropic 端点, 强制改为 OpenAI 端点的 /v1; 显式补 /v1."""
        if not isinstance(v, str):
            return v
        v = v.strip().rstrip("/")
        if "/anthropic" in v:
            v = v.replace("/anthropic", "")
        if not v.endswith("/v1"):
            v = f"{v}/v1"
        return v

    @field_validator("thinking", mode="before")
    @classmethod
    def _normalize_thinking(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip().lower()
        return v

    # ---- 公共 helper ----

    @property
    def api_key_masked(self) -> str:
        """掩码版 api_key, 仅供展示."""
        return _mask_secret(self.api_key.get_secret_value())

    def resolve_model(self, role: str = "flash") -> str:
        """role ∈ {flash, pro, chat (alias→flash), reasoner (alias→pro)}."""
        m = role.lower()
        if m in ("flash", "v4-flash", "deepseek-v4-flash", "chat", "deepseek-chat"):
            return self.model_flash
        if m in ("pro", "v4-pro", "deepseek-v4-pro", "reasoner", "deepseek-reasoner"):
            return self.model_pro
        # 用户直接传一个完整 model id, 也走 strip-bracket 后返回
        return _strip_bracket_suffix(role)


def load_settings() -> Settings:
    """统一入口: 加载 .env, 实例化 Settings.

    优先级 (高 → 低):
    1. 进程环境中已有的 DEEPSEEK_* (用户主动 export 或本进程内已设置)
    2. .env 文件 (JSON 配置文件 或 标准 dotenv)
    3. .env 里的 Anthropic 兼容字段 (ANTHROPIC_AUTH_TOKEN 等) 映射到 DEEPSEEK_*
    4. Settings 默认值

    特殊处理: 父进程 (尤其是 父进程 shell) 注入的 ANTHROPIC_* 不会被
    误识为 DeepSeek 凭据 — 只有当 .env 文件里写了 ANTHROPIC_AUTH_TOKEN 时才映射.
    """
    if os.environ.get("OSKAG_ENV_OVERRIDE", "1") == "0":
        override = False
    else:
        override = True

    path, source, raw = _resolve_dotenv_pairs()
    pairs = _build_alias_map(raw)

    # 把 oskag-relevant 的键写到 os.environ (供 Settings 通过 validation_alias 读取)
    # override=True 时, .env 显式给的覆盖父进程注入的同名键
    _apply_env_pairs(pairs, override=override)

    settings = Settings()
    object.__setattr__(settings, "env_source", source)
    object.__setattr__(settings, "env_path", path)
    return settings


__all__ = [
    "Settings",
    "load_settings",
    "load_env_into_os_environ",
]
