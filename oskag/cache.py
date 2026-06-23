"""oskag/cache.py — LLM 调用磁盘缓存.

设计:
- key = sha256({model, messages, tools, response_format, thinking, temperature})
- value = ChatResult.__dict__ 的 JSON 序列化 (raw_message 已剥 reasoning, 安全)
- 存路径: cache/llm/<key[:2]>/<key>.json (按前缀分桶, 文件系统友好)
- 默认 disabled=False; cli `--no-cache` 触发 disabled=True 跳过读写
- 不引入 diskcache 依赖, 用 stdlib (hashlib + json + Path)

使用模式 (pipeline 接入):
    cache = LLMCache()
    key = cache.key(model="flash", messages=msgs, tools=tools)
    cached = cache.get(key)
    if cached is None:
        result = client.chat(...)
        cache.set(key, result)
    else:
        result = cached
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, fields, is_dataclass
from pathlib import Path
from typing import Any

from oskag.llm import ChatResult
from oskag.logging_setup import get_logger

log = get_logger("oskag.cache")


_DEFAULT_CACHE_DIR = Path("cache") / "llm"


class LLMCache:
    """LLM 调用结果磁盘缓存."""

    def __init__(self, cache_dir: Path | str = _DEFAULT_CACHE_DIR,
                 *, disabled: bool = False) -> None:
        self.cache_dir = Path(cache_dir)
        self.disabled = disabled
        self.hits = 0
        self.misses = 0
        if not disabled:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # key 计算
    # ------------------------------------------------------------------ #

    @staticmethod
    def key(
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        thinking: str | None = None,
        temperature: float = 0.0,
    ) -> str:
        """构造一个稳定 hash. 顺序敏感的字段 (messages/tools) 保留原顺序."""
        payload = {
            "model": model,
            "messages": messages,
            "tools": tools or [],
            "response_format": response_format or {},
            "thinking": thinking or "",
            "temperature": temperature,
        }
        try:
            blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            blob = repr(payload)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _path_for(self, key: str) -> Path:
        return self.cache_dir / key[:2] / f"{key}.json"

    # ------------------------------------------------------------------ #
    # get / set
    # ------------------------------------------------------------------ #

    def get(self, key: str) -> ChatResult | None:
        if self.disabled:
            return None
        path = self._path_for(key)
        if not path.is_file():
            self.misses += 1
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("cache_read_failed", key=key, error=str(e)[:160])
            self.misses += 1
            return None
        self.hits += 1
        log.info("cache_hit", key=key[:16])
        return _from_json(data)

    def set(self, key: str, value: ChatResult) -> None:
        if self.disabled:
            return
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(
                json.dumps(_to_json(value), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            log.warning("cache_write_failed", key=key, error=str(e)[:160])
            return
        log.info("cache_set", key=key[:16])

    # ------------------------------------------------------------------ #
    # stats
    # ------------------------------------------------------------------ #

    def stats(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses, "disabled": int(self.disabled)}


# --------------------------------------------------------------------------- #
# ChatResult ↔ dict 序列化 (避开 dataclasses.asdict 的边角)
# --------------------------------------------------------------------------- #


def _to_json(result: ChatResult) -> dict[str, Any]:
    """ChatResult → JSON-able dict."""
    if not is_dataclass(result):
        raise TypeError(f"expected ChatResult dataclass, got {type(result).__name__}")
    return asdict(result)


def _from_json(data: dict[str, Any]) -> ChatResult:
    """JSON dict → ChatResult. 容缺字段, 用默认值回填."""
    fnames = {f.name for f in fields(ChatResult)}
    filtered = {k: v for k, v in data.items() if k in fnames}

    # required 字段兜底
    filtered.setdefault("content", "")
    filtered.setdefault("reasoning_content", None)
    filtered.setdefault("tool_calls", [])
    filtered.setdefault("raw_message", {"role": "assistant", "content": filtered.get("content", "")})
    filtered.setdefault("model", "")
    filtered.setdefault("finish_reason", None)
    filtered.setdefault("request_id", None)
    return ChatResult(**filtered)


__all__ = ["LLMCache"]
