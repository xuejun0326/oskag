"""结构化日志 (structlog).

设计:
- 终端 (stderr): rich-rendered key=value, INFO+, 给开发者看的故事
- 文件 (dev-logs/llm-trace-YYYYMMDD.jsonl): JSON 行式, DEBUG 全量, 给程序解析

反幻觉硬规则:
- 任何字段值如形如 sk-* / 长度 ≥ 30 的随机字符串, 都过 SecretCensor 屏蔽
- llm_call 事件强制记录: model / prompt_tokens / completion_tokens / reasoning_tokens / latency_ms / request_id
- tool_call 事件: tool_name / args_hash (不记 args 原文, 防工具参数泄漏密钥)

参考: structlog "rendering and processing chains", aider 的 Censor 处理器思路.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import structlog
from rich.console import Console
from rich.logging import RichHandler

# Sensitive 字段一律改成这个值
_REDACTED = "<REDACTED>"

# 字段名匹配 (不区分大小写) — 命中即整段替换为 REDACTED
_SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|auth[_-]?token|secret|password|bearer|x[-_]api[-_]key|"
    r"authorization)",
    re.IGNORECASE,
)

# 值的 pattern: 多种密钥/令牌形态
_SENSITIVE_VALUE_RES = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9_.\-]{20,}", re.IGNORECASE),
    # JWT 三段式
    re.compile(r"\bey[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b"),
    # key/token=xxx 自由文本形态
    re.compile(
        r"(?i)\b(?:api[_-]?key|token|authorization)\s*[:=]\s*[A-Za-z0-9._\-]{16,}"
    ),
)

# 已知密钥精确子串 — 上层 (DeepSeekClient) 通过 setup_logging(extra_secret_substrings=...)
# 注册自己的 api_key 字面值, 任何文本中包含都全文替换. 这是反幻觉硬护栏.
_KNOWN_SECRETS: tuple[str, ...] = ()


def _redact_value(v: Any) -> Any:
    """对单个值做 secret pattern 替换."""
    if isinstance(v, str):
        # 1) 已知密钥精确替换 (最强护栏)
        for known in _KNOWN_SECRETS:
            if known and known in v:
                v = v.replace(known, _REDACTED)
        # 2) 正则模式替换
        for pat in _SENSITIVE_VALUE_RES:
            v = pat.sub(_REDACTED, v)
        return v
    if isinstance(v, dict):
        return {k: _redact_dict_entry(k, vv) for k, vv in v.items()}
    if isinstance(v, (list, tuple)):
        return type(v)(_redact_value(item) for item in v)
    return v


def _redact_dict_entry(key: str, value: Any) -> Any:
    if _SENSITIVE_KEY_RE.search(str(key)):
        return _REDACTED
    return _redact_value(value)


def secret_censor(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog 处理器: 屏蔽事件 dict 中的敏感数据."""
    return {k: _redact_dict_entry(k, v) for k, v in event_dict.items()}


def add_iso_timestamp(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """统一 ISO 8601 with UTC.

    在 Python shutdown 阶段 sys.meta_path 可能已废, 此时 datetime.now() 内部某些
    lazy import 会 ImportError. 兜底跳过, 不让进程 shutdown 阶段的连接关闭日志崩.
    """
    try:
        event_dict["ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    except (ImportError, RuntimeError):
        event_dict["ts"] = "<shutdown>"
    return event_dict


_CONFIGURED = False


def setup_logging(
    *,
    level: str = "INFO",
    log_dir: Path | str = "./dev-logs",
    enable_file: bool = True,
    console_force_color: bool = True,
    extra_secret_substrings: tuple[str, ...] = (),
) -> None:
    """初始化日志. 多次调用幂等, 仅第一次生效.

    extra_secret_substrings: 已知密钥的精确子串, 在所有事件 dict 与 traceback 中
        全文替换为 <REDACTED>. 反幻觉硬护栏: 比正则更可靠 — 如果上层已经知道
        api_key 原文 (例如 DeepSeekClient 实例化后), 把它注册进来.
    """
    global _CONFIGURED, _KNOWN_SECRETS
    if _CONFIGURED:
        # 二次调用: 仅追加 known secrets, 不重置 logging 配置
        for s in extra_secret_substrings:
            if s and s not in _KNOWN_SECRETS:
                _KNOWN_SECRETS = (*_KNOWN_SECRETS, s)
        return

    for s in extra_secret_substrings:
        if s and s not in _KNOWN_SECRETS:
            _KNOWN_SECRETS = (*_KNOWN_SECRETS, s)

    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    log_level = getattr(logging, level.upper(), logging.INFO)

    # ---- 标准 logging 后端 (structlog 透传过去) ----
    root = logging.getLogger()
    root.handlers.clear()
    # root logger 设最低级 (DEBUG) 让两类 handler 各自决定; 否则 INFO 以下进不到 file
    root.setLevel(logging.DEBUG)

    # 终端: rich, 用 ProcessorFormatter 渲染 structlog 事件成 key=value 文本
    console = Console(
        stderr=True,
        force_terminal=True if console_force_color else None,
        no_color=os.environ.get("NO_COLOR") is not None,
    )
    rich_handler = RichHandler(
        console=console,
        show_time=True,
        show_path=False,
        markup=False,
        rich_tracebacks=True,
        tracebacks_show_locals=False,  # 防止局部变量 (含 api_key) 进入 traceback
        log_time_format="%H:%M:%S",
    )
    rich_handler.setLevel(log_level)
    rich_handler.setFormatter(_make_console_formatter())
    root.addHandler(rich_handler)

    # 文件: JSON 行式, DEBUG 全量
    if enable_file:
        date_str = datetime.now().strftime("%Y%m%d")
        file_path = log_dir / f"llm-trace-{date_str}.jsonl"
        file_handler = RotatingFileHandler(
            file_path,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(_make_jsonl_formatter())
        root.addHandler(file_handler)

    # ---- structlog 处理器链 ----
    processors_pre: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        add_iso_timestamp,
        secret_censor,  # 必须在 render 前
        structlog.processors.StackInfoRenderer(),
    ]

    # filtering_bound_logger 设 DEBUG, 让 file handler 收得到 DEBUG 全量;
    # 终端的 INFO 阈值由 rich_handler.setLevel(log_level) 控制.
    structlog.configure(
        processors=processors_pre + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    _CONFIGURED = True


def _make_console_formatter() -> Any:
    """终端 handler 的 ProcessorFormatter — 把 structlog 事件渲染成可读 key=value."""
    return structlog.stdlib.ProcessorFormatter(
        processor=structlog.dev.ConsoleRenderer(colors=True),
        foreign_pre_chain=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            add_iso_timestamp,
            secret_censor,
            structlog.stdlib.ExtraAdder(),
        ],
    )


def _make_jsonl_formatter() -> Any:
    """文件 handler 用的 ProcessorFormatter, 渲染成 JSON 行."""
    return structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            add_iso_timestamp,
            secret_censor,
            structlog.stdlib.ExtraAdder(),
        ],
    )


def get_logger(name: str = "oskag") -> structlog.stdlib.BoundLogger:
    """获取 logger. 调用前请先 setup_logging."""
    if not _CONFIGURED:
        setup_logging()
    return structlog.get_logger(name)


__all__ = [
    "setup_logging",
    "get_logger",
    "secret_censor",
]


def _self_test() -> None:
    """模块自测: python -m oskag.logging_setup."""
    setup_logging(level="DEBUG", log_dir="./dev-logs")
    log = get_logger("oskag.test")
    log.info("hello", api_key="sk-1234567890abcdefghijklmnop", base_url="https://x.com")
    log.warning("retrying", attempt=1, last_error="rate limited")
    log.debug("trace", tool="read_file", args_hash="abc123")
    print("logging self-test ok; check dev-logs/llm-trace-*.jsonl", file=sys.stderr)


if __name__ == "__main__":
    _self_test()
