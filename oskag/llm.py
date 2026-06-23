"""DeepSeek LLM 客户端封装.

设计要点:

1. **同一 key 通用**: DeepSeek 一个 API key 在 OpenAI 兼容端 (/v1) 与 Anthropic
   兼容端 (/anthropic) 都能用. oskag 走 OpenAI 端, base_url 显式 /v1.

2. **model id 必须 strip [1m]**: Anthropic 端的 "deepseek-v4-pro[1m]" 后缀是
   计费分桶, OpenAI 端不接受.

3. **thinking 接入**: extra_body={"thinking": {"type": "enabled"}}.
   响应字段 message.reasoning_content (与 content 同级).

4. **strip_reasoning**: 上一轮 assistant 的 reasoning_content 不能回传 messages,
   一旦携带下一轮请求会 400. 每次发请求前必须剥离.

5. **重试**: 白名单 {429, APIConnectionError, APITimeoutError, 500, 503} →
   指数退避 (initial=1.0, factor=2, jitter±0.3, cap=60s, max=5).
   黑名单 {401, 402, 400, 422, 404, 409} → 立即抛.
   最后一次失败前不再 sleep, 避免浪费 16s.

6. **model fallback**: 收到 NotFoundError (model id 不识别) 时, 自动 fallback
   到 chat 别名重试一次, 提示用户更新配置.

7. **usage 字段** 用 getattr 兜底: prompt_tokens / completion_tokens /
   reasoning_tokens (在 completion_tokens_details 下) / cached_tokens.

8. **structured output** 走 json_object + system prompt 含 "json" 字面量.
   DeepSeek V4 不支持 json_schema strict.
"""
from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from typing import Any

from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    InternalServerError,
    NotFoundError,
    OpenAI,
    PermissionDeniedError,
    RateLimitError,
    UnprocessableEntityError,
)

from oskag.config import Settings
from oskag.logging_setup import get_logger, setup_logging

log = get_logger("oskag.llm")

_BRACKET_SUFFIX_RE = re.compile(r"\[.*?\]$")

# 重试白名单
_RETRYABLE_EXC: tuple[type[Exception], ...] = (
    RateLimitError,
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
)
# 黑名单 (立即抛)
_FATAL_EXC: tuple[type[Exception], ...] = (
    AuthenticationError,
    PermissionDeniedError,
    BadRequestError,
    NotFoundError,
    UnprocessableEntityError,
    ConflictError,
)

_DEFAULT_MAX_RETRIES = 5
_BACKOFF_INITIAL = 1.0
_BACKOFF_FACTOR = 2.0
_BACKOFF_CAP = 60.0
_BACKOFF_JITTER = 0.3


@dataclass
class ChatResult:
    """一次 chat completions 调用的标准化结果."""

    content: str
    """LLM 主回复文本 (剥离 reasoning_content 后的部分)."""

    reasoning_content: str | None
    """思维链原文; 仅 thinking=enabled 且模型返回时非空."""

    tool_calls: list[dict[str, Any]]
    """function calling: 标准化为 dict 列表."""

    raw_message: dict[str, Any]
    """原始 message dict (供后续构造 messages 入参用; 已剥离 reasoning_content)."""

    model: str
    finish_reason: str | None
    request_id: str | None

    # token usage
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    used_fallback: bool = False
    from_cache: bool = False
    """True 表示这次结果命中了磁盘 LLMCache, 没有真调 API."""
    """是否用了 model fallback (NotFoundError → chat alias)."""

    extras: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _strip_bracket(model: str) -> str:
    return _BRACKET_SUFFIX_RE.sub("", model).strip()


def _coerce_message_dict(m: Any) -> dict[str, Any]:
    """把任意消息形态转 dict; 不支持的类型显式抛 TypeError."""
    if isinstance(m, dict):
        return dict(m)
    if hasattr(m, "model_dump"):
        return m.model_dump(exclude_none=True)  # type: ignore[no-any-return]
    if hasattr(m, "__dict__"):
        return {k: v for k, v in vars(m).items() if not k.startswith("_")}
    raise TypeError(f"unsupported message type: {type(m).__name__}")


def strip_reasoning(messages: list[Any]) -> list[dict[str, Any]]:
    """剥离上一轮 assistant 消息中的 reasoning_content.

    DeepSeek OpenAI 兼容端硬规则: 携带 reasoning_content 的 assistant message
    再发请求会 400. 必须在每次 chat.completions.create 之前调用.

    返回新 list, 原 list 不变.
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        d = _coerce_message_dict(m)
        if d.get("role") == "assistant":
            d.pop("reasoning_content", None)
            d.pop("reasoning", None)
        out.append(d)
    return out


def _extract_usage(usage: Any) -> dict[str, int]:
    """从 response.usage 提取标准 token 字段, 不存在的字段默认 0."""

    def _g(obj: Any, key: str, default: int = 0) -> int:
        v = getattr(obj, key, None)
        if v is None and isinstance(obj, dict):
            v = obj.get(key)
        return int(v) if v is not None else default

    if usage is None:
        return {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "reasoning_tokens": 0,
            "cached_tokens": 0,
            "total_tokens": 0,
        }

    prompt = _g(usage, "prompt_tokens")
    completion = _g(usage, "completion_tokens")
    total = _g(usage, "total_tokens")
    cached = _g(usage, "prompt_cache_hit_tokens", 0)

    # reasoning_tokens 嵌套在 completion_tokens_details 里 (OpenAI 风格)
    reasoning = 0
    details = getattr(usage, "completion_tokens_details", None)
    if details is not None:
        reasoning = _g(details, "reasoning_tokens", 0)

    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "reasoning_tokens": reasoning,
        "cached_tokens": cached,
        "total_tokens": total,
    }


def _normalize_tool_calls(tool_calls: Any) -> list[dict[str, Any]]:
    """统一 tool_calls 为 list[dict] 形态."""
    if not tool_calls:
        return []
    out: list[dict[str, Any]] = []
    for tc in tool_calls:
        if hasattr(tc, "model_dump"):
            out.append(tc.model_dump())
        elif isinstance(tc, dict):
            out.append(dict(tc))
    return out


def _backoff_delay(
    attempt: int,
    *,
    initial: float = _BACKOFF_INITIAL,
    factor: float = _BACKOFF_FACTOR,
    cap: float = _BACKOFF_CAP,
    jitter: float = _BACKOFF_JITTER,
) -> float:
    base = min(cap, initial * (factor ** attempt))
    return max(0.0, base + random.uniform(-jitter, jitter) * base)


def _build_chat_kwargs(
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    tool_choice: str | dict[str, Any] | None,
    response_format: dict[str, Any] | None,
    thinking: str,
    temperature: float,
    max_tokens: int | None,
    seed: int | None,
) -> dict[str, Any]:
    """组装 client.chat.completions.create 的 kwargs."""
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
    if response_format:
        kwargs["response_format"] = response_format
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    elif thinking and thinking != "disabled":
        # thinking + reasoning_effort=max 模式下, 默认 max_tokens 容易让 narrative
        # 1500-2500 字 + 多条 refs[snippet] 写到一半被截. 显式给 32768 留足空间.
        # (deepseek-v4-pro 单次 completion 上限 ≥ 32K, 服务端会 clip 不会拒)
        kwargs["max_tokens"] = 32768
    if seed is not None:
        kwargs["seed"] = seed
    if thinking and thinking != "disabled":
        # thinking 开启时自动加 reasoning_effort=max (deepseek 官方文档:
        # 思考模式下默认 effort=high, 我们这里强制 max 来最大化分析深度).
        # 用 OpenAI 兼容格式 (deepseek 同时支持 OAI / Anthropic 两种字段).
        kwargs["extra_body"] = {
            "thinking": {"type": thinking},
            "reasoning_effort": "max",
        }
    return kwargs


# --------------------------------------------------------------------------- #
# DeepSeek client
# --------------------------------------------------------------------------- #


class DeepSeekClient:
    """oskag 的 DeepSeek 调用入口."""

    def __init__(self, settings: Settings, *, timeout: float = 60.0,
                 cache: "LLMCache | None | bool" = True):
        """构造客户端.

        cache 参数 (接入):
            - True (默认): 启用默认磁盘缓存 (cache/llm/...)
            - False: 显式禁用 (cli --no-cache 用)
            - LLMCache 实例: 注入自定义 cache (单测 / 自定义路径)
            - None: 同 False
        """
        self.settings = settings
        api_key = settings.api_key.get_secret_value()
        if not api_key:
            raise ValueError(
                "DEEPSEEK_API_KEY 未配置. "
                "请在 .env 或 JSON 配置文件 (env.ANTHROPIC_AUTH_TOKEN) 中提供."
            )
        # 反幻觉硬护栏: 把已知 api_key 字面值注册为 substring redaction
        # 任何意外 (异常 / SDK 内部日志 / repr) 中含此串都全文替换
        setup_logging(extra_secret_substrings=(api_key,))
        self.client = OpenAI(
            api_key=api_key,
            base_url=settings.base_url,
            timeout=timeout,
        )
        self.timeout = timeout
        self._cache = self._resolve_cache(cache)

    @staticmethod
    def _resolve_cache(cache: "LLMCache | None | bool") -> "LLMCache | None":
        """把 True/False/None/LLMCache 统一成 LLMCache 实例或 None."""
        # 局部 import 避免循环 (cache.py 反向 import ChatResult)
        from oskag.cache import LLMCache
        if cache is True:
            return LLMCache()
        if cache is False or cache is None:
            return None
        if isinstance(cache, LLMCache):
            return cache
        raise TypeError(f"cache 参数类型不支持: {type(cache).__name__}")

    # ------------------------------------------------------------------ #
    # core: chat (低复杂度版)
    # ------------------------------------------------------------------ #

    def chat(
        self,
        messages: list[Any],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
        thinking: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        seed: int | None = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> ChatResult:
        """单次 chat completion, 内置剥离 reasoning + 重试 + model fallback + 磁盘缓存.

        thinking ∈ {"enabled", "disabled", None}; None 用 settings.thinking.

        缓存 : 同样的 (model, messages, tools, response_format, thinking, temperature)
        命中后直接返回 ChatResult(from_cache=True), 不调 API.
        """
        # model 入参可能是: 角色别名 (flash/pro/chat/reasoner) 或完整 model id;
        # resolve_model 处理双语义并 strip [1m] 后缀
        if model is None:
            chosen_model = self.settings.resolve_model("flash")
        else:
            chosen_model = self.settings.resolve_model(model)
        chosen_model = _strip_bracket(chosen_model)
        chosen_thinking = thinking if thinking is not None else self.settings.thinking
        clean_messages = strip_reasoning(messages)

        # cache 查询 (key 计入 messages/tools/response_format/thinking/temperature)
        cache_key = self._cache_key_or_none(
            chosen_model, clean_messages, tools,
            response_format, chosen_thinking, temperature,
        )
        if cache_key is not None:
            hit = self._cache.get(cache_key)  # type: ignore[union-attr]
            if hit is not None:
                hit.from_cache = True
                return hit

        result = self._call_with_fallback(
            primary_model=chosen_model,
            messages=clean_messages,
            tools=tools,
            tool_choice=tool_choice,
            response_format=response_format,
            thinking=chosen_thinking,
            temperature=temperature,
            max_tokens=max_tokens,
            seed=seed,
            max_retries=max_retries,
        )

        if cache_key is not None:
            self._cache.set(cache_key, result)  # type: ignore[union-attr]
        return result

    def _cache_key_or_none(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        response_format: dict[str, Any] | None,
        thinking: str | None,
        temperature: float,
    ) -> str | None:
        """有 cache 时算 key, 否则返回 None (避免无 cache 时也算 hash 浪费 cpu)."""
        if self._cache is None:
            return None
        return self._cache.key(
            model=model,
            messages=messages,
            tools=tools,
            response_format=response_format,
            thinking=thinking,
            temperature=temperature,
        )

    # ------------------------------------------------------------------ #
    # internal: model fallback wrapper
    # ------------------------------------------------------------------ #

    def _call_with_fallback(
        self,
        *,
        primary_model: str,
        max_retries: int,
        **shared: Any,
    ) -> ChatResult:
        """先用 primary_model 调用. 若 NotFoundError, fallback 到 v4-flash 再试一次."""
        try:
            return self._call_with_retry(primary_model, max_retries=max_retries, **shared)
        except NotFoundError as e:
            fb = self.settings.resolve_model("flash")
            if fb == primary_model:
                raise
            log.warning(
                "llm_model_fallback",
                primary_model=primary_model,
                fallback_model=fb,
                error=str(e)[:160],
                hint="primary 模型 ID 无效, 已自动切到 flash 别名重试. 请检查配置.",
            )
            result = self._call_with_retry(fb, max_retries=max_retries, **shared)
            result.used_fallback = True
            return result

    def _call_with_retry(
        self,
        model: str,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: str | dict[str, Any] | None,
        response_format: dict[str, Any] | None,
        thinking: str,
        temperature: float,
        max_tokens: int | None,
        seed: int | None,
        max_retries: int,
    ) -> ChatResult:
        """指数退避重试主循环.

        语义: 最多尝试 max_retries 次, 最后一次失败前不再 sleep.
        总等待时间 = sum(_backoff_delay(0..max_retries-2)).
        """
        kwargs = _build_chat_kwargs(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            response_format=response_format,
            thinking=thinking,
            temperature=temperature,
            max_tokens=max_tokens,
            seed=seed,
        )
        last_exc: Exception | None = None
        for attempt in range(max_retries):
            try:
                t0 = time.perf_counter()
                resp = self.client.chat.completions.create(**kwargs)
                latency_ms = int((time.perf_counter() - t0) * 1000)
                return self._normalize_response(resp, model, latency_ms)
            except _FATAL_EXC:
                log.error("llm_call_fatal", model=model, attempt=attempt + 1)
                raise
            except _RETRYABLE_EXC as e:
                last_exc = e
                self._handle_retry_delay(model, attempt, max_retries, e)
            except APIError as e:
                last_exc = e
                self._handle_retry_delay(model, attempt, max_retries, e, unknown=True)

        # 跑完 max_retries 仍失败
        log.error(
            "llm_call_exhausted",
            model=model,
            max_retries=max_retries,
            last_error_type=type(last_exc).__name__ if last_exc else None,
        )
        if last_exc:
            raise last_exc
        raise RuntimeError("LLM call exhausted retries with no captured exception")

    def _handle_retry_delay(
        self,
        model: str,
        attempt: int,
        max_retries: int,
        exc: Exception,
        *,
        unknown: bool = False,
    ) -> None:
        """打日志 + sleep. 最后一次尝试不 sleep."""
        is_last = attempt + 1 >= max_retries
        event = "llm_call_unknown_apierror_retry" if unknown else "llm_call_retry"
        if is_last:
            log.warning(
                event + "_final",
                model=model,
                attempt=attempt + 1,
                max_retries=max_retries,
                error_type=type(exc).__name__,
            )
            return
        delay = _backoff_delay(attempt)
        log.warning(
            event,
            model=model,
            attempt=attempt + 1,
            max_retries=max_retries,
            error_type=type(exc).__name__,
            delay_sec=round(delay, 2),
        )
        time.sleep(delay)

    # ------------------------------------------------------------------ #
    # response normalization
    # ------------------------------------------------------------------ #

    def _normalize_response(self, resp: Any, model: str, latency_ms: int) -> ChatResult:
        """把 OpenAI SDK 响应对象转成 ChatResult."""
        choice = resp.choices[0]
        msg = choice.message

        content = getattr(msg, "content", None) or ""
        reasoning_content = getattr(msg, "reasoning_content", None)
        if reasoning_content is None:
            reasoning_obj = getattr(msg, "reasoning", None)
            if reasoning_obj is not None:
                reasoning_content = getattr(reasoning_obj, "text", None)

        tool_calls = _normalize_tool_calls(getattr(msg, "tool_calls", None))

        try:
            raw = msg.model_dump(exclude_none=True)
        except AttributeError:
            raw = {"role": "assistant", "content": content}
        # DeepSeek thinking 多轮规则 (2026-06 已更新):
        # reasoning_content 必须回传，不能剥离；剥离会导致 400 invalid_request_error
        # 旧规则 (已废弃): raw.pop("reasoning_content", None); raw.pop("reasoning", None)
        raw.pop("reasoning", None)   # "reasoning" 字段仍不需要 (只保留 reasoning_content)

        usage_dict = _extract_usage(getattr(resp, "usage", None))
        request_id = getattr(resp, "id", None) or getattr(resp, "_request_id", None)

        result = ChatResult(
            content=content,
            reasoning_content=reasoning_content,
            tool_calls=tool_calls,
            raw_message=raw,
            model=getattr(resp, "model", model) or model,
            finish_reason=getattr(choice, "finish_reason", None),
            request_id=request_id,
            latency_ms=latency_ms,
            **usage_dict,
        )

        log.info(
            "llm_call",
            model=result.model,
            request_id=result.request_id,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            reasoning_tokens=result.reasoning_tokens,
            cached_tokens=result.cached_tokens,
            latency_ms=result.latency_ms,
            finish_reason=result.finish_reason,
            has_tool_calls=bool(result.tool_calls),
        )
        return result

    # ------------------------------------------------------------------ #
    # convenience
    # ------------------------------------------------------------------ #

    def ping(self, *, model: str | None = None, max_tokens: int = 1) -> ChatResult:
        """1-token 轻量调用, 验证连通."""
        return self.chat(
            messages=[{"role": "user", "content": "ok"}],
            model=model,
            thinking="disabled",
            max_tokens=max_tokens,
        )


__all__ = [
    "DeepSeekClient",
    "ChatResult",
    "strip_reasoning",
]
