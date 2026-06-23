"""oskag/agent.py — tool-loop 主循环.

标准 OpenAI function-calling 循环, 不依赖任何外部 agent 框架.
设计要点:

1. **单一职责**: Agent 只编排 LLM ↔ 工具的对话, 不知道具体工具是什么.
   工具列表 (OpenAI tools schema) 与处理函数 (`tool_handlers: dict[str, Callable]`)
   由调用方注入.

2. **每轮发送前剥离 reasoning**: 已在 `DeepSeekClient.chat()` 内部做,
   但 messages 列表是我们自己维护的, 必须显式调 `strip_reasoning()`
   (其实 client.chat() 也会再剥一次, 双保险).

3. **工具异常包装**: tool_handler 抛任何异常 → `{"error": "..."}` 进 tool 回复,
   agent 不崩, 让 LLM 自己看错误信息决定下一步.

4. **tool_calls 解析容错**: arguments 字段是 JSON 字符串, 解析失败时给 LLM
   一个 "invalid_arguments" 错误回复.

5. **终止条件**:
   - LLM 不再请求工具 (`tool_calls=[]`) → 返回最后一次的 content
   - max_turns 用完 → 强制再调一次禁用工具的 LLM 收尾, 拿一次最终输出

6. **token 累加**: 所有轮的 prompt/completion/reasoning tokens 都加进 AgentResult.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from oskag.llm import ChatResult, DeepSeekClient, strip_reasoning
from oskag.logging_setup import get_logger

log = get_logger("oskag.agent")


# 工具调用结果的 schema:
#   { "ok": bool, "content": str, "error"?: str }
# tool 角色消息的 content 字段必须是字符串 (JSON 序列化)


@dataclass
class ToolCallTrace:
    """单次工具调用的 trace, 仅用于日志/调试."""
    turn: int
    name: str
    arguments_preview: str          # 截断后的 args 字符串
    ok: bool
    duration_ms: int
    result_chars: int


@dataclass
class AgentResult:
    """Agent.run() 的最终返回."""
    content: str                                     # 最后一轮 assistant 文字
    finish_reason: str | None = None
    turns: int = 0
    tool_calls_made: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    used_fallback: bool = False
    truncated: bool = False                          # max_turns 触发强制收尾
    traces: list[ToolCallTrace] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)

    def accumulate(self, r: ChatResult) -> None:
        self.prompt_tokens += r.prompt_tokens
        self.completion_tokens += r.completion_tokens
        self.reasoning_tokens += r.reasoning_tokens
        self.total_tokens += r.total_tokens
        self.used_fallback = self.used_fallback or r.used_fallback


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


_ARGS_PREVIEW = 200
_ERROR_PREVIEW = 300


def _parse_tool_arguments(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """tool_call.function.arguments 是 JSON 字符串 (OpenAI 规范).

    返回 (parsed_dict, error_msg). 解析失败 → (None, "invalid json: ...").
    """
    if isinstance(raw, dict):
        return raw, None  # 某些 mock 直接给 dict, 兼容
    if not isinstance(raw, str):
        return None, f"arguments not string: {type(raw).__name__}"
    if not raw.strip():
        return {}, None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"invalid json: {e!s}"[:160]
    if not isinstance(parsed, dict):
        return None, f"arguments not object: {type(parsed).__name__}"
    return parsed, None


def _format_tool_response(payload: dict[str, Any]) -> str:
    """tool 角色消息的 content 必须是字符串. 我们统一用 JSON 序列化."""
    try:
        return json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        return json.dumps({"error": f"serialize failed: {e!s}"[:160]})


def _make_tool_message(tool_call_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": _format_tool_response(payload),
    }


def _minimal_retry_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """为收尾空回复重试构造精简上下文: 首条 system + 首条 user 诉求.

    丢弃所有 assistant/tool 中间轮 (它们占用大量 token 且含 tool_call 引用),
    只保留任务定义本身, 让模型直接产出 JSON. 找不到必要消息返回 None.
    """
    system_msg = next((m for m in messages if m.get("role") == "system"), None)
    user_msg = next((m for m in messages if m.get("role") == "user"), None)
    if user_msg is None:
        return None
    out: list[dict[str, Any]] = []
    if system_msg is not None:
        out.append(system_msg)
    out.append(user_msg)
    return out


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #


class Agent:
    """OpenAI tool-loop 编排器."""

    def __init__(
        self,
        client: DeepSeekClient,
        *,
        tools: list[dict[str, Any]],
        tool_handlers: dict[str, Callable[[dict[str, Any]], Any]],
    ) -> None:
        """
        Args:
            client: 已初始化的 DeepSeekClient (含 settings).
            tools: OpenAI tools schema list (每个有 type=function + function.name + parameters).
            tool_handlers: name → callable. callable 接收 parsed_args dict, 返回任意值
                (会被 json.dumps), 或抛异常 (会被包成 error).
        """
        self.client = client
        self.tools = tools
        self.tool_handlers = tool_handlers
        # name 校验
        declared = {t["function"]["name"] for t in tools if t.get("type") == "function"}
        missing = declared - tool_handlers.keys()
        if missing:
            raise ValueError(f"tool_handlers missing for: {sorted(missing)}")

    # ------------------------------------------------------------------ #
    # tool 分派
    # ------------------------------------------------------------------ #

    def _dispatch_tool(
        self, name: str, arguments_raw: Any, *, turn: int,
    ) -> tuple[dict[str, Any], ToolCallTrace]:
        """调一个工具, 返回 (payload_dict, trace). 永不抛."""
        t0 = time.monotonic()
        args_preview = (str(arguments_raw)[:_ARGS_PREVIEW]).replace("\n", " ")

        if name not in self.tool_handlers:
            payload = {"ok": False, "error": f"unknown tool: {name}"}
            trace = ToolCallTrace(
                turn=turn, name=name, arguments_preview=args_preview,
                ok=False, duration_ms=0, result_chars=len(payload["error"]),
            )
            return payload, trace

        parsed, err = _parse_tool_arguments(arguments_raw)
        if err is not None or parsed is None:
            payload = {"ok": False, "error": err or "invalid arguments"}
            duration = int((time.monotonic() - t0) * 1000)
            trace = ToolCallTrace(
                turn=turn, name=name, arguments_preview=args_preview,
                ok=False, duration_ms=duration,
                result_chars=len(payload["error"]),
            )
            return payload, trace

        try:
            result = self.tool_handlers[name](parsed)
            ok = True
            payload = {"ok": True, "content": result} if not isinstance(result, dict) \
                else {"ok": True, **result}
        except Exception as e:
            ok = False
            payload = {"ok": False, "error": f"{type(e).__name__}: {e!s}"[:_ERROR_PREVIEW]}

        duration = int((time.monotonic() - t0) * 1000)
        try:
            result_chars = len(json.dumps(payload, ensure_ascii=False))
        except (TypeError, ValueError):
            result_chars = 0

        trace = ToolCallTrace(
            turn=turn, name=name, arguments_preview=args_preview,
            ok=ok, duration_ms=duration, result_chars=result_chars,
        )
        return payload, trace

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #

    def run(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str = "flash",
        max_turns: int = 8,
        thinking: str | None = None,
        response_format: dict[str, Any] | None = None,
        temperature: float = 0.0,
    ) -> AgentResult:
        """运行 tool-loop. messages 列表会被原地追加 assistant + tool 消息."""
        if max_turns <= 0:
            raise ValueError("max_turns must be ≥ 1")

        result = AgentResult(content="")

        for turn in range(1, max_turns + 1):
            result.turns = turn
            clean_msgs = strip_reasoning(messages)

            chat = self.client.chat(
                clean_msgs,
                model=model,
                tools=self.tools,
                response_format=response_format,
                thinking=thinking,
                temperature=temperature,
            )
            result.accumulate(chat)
            result.content = chat.content
            result.finish_reason = chat.finish_reason

            # 把 assistant 消息追加进 messages (raw_message 已剥离 reasoning)
            messages.append(chat.raw_message)

            if not chat.tool_calls:
                log.info(
                    "agent_done",
                    turn=turn,
                    finish_reason=chat.finish_reason,
                    tool_calls=result.tool_calls_made,
                )
                return result

            # 处理每个 tool_call, 追加 tool 消息
            for tc in chat.tool_calls:
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                arguments_raw = fn.get("arguments", "")
                tc_id = tc.get("id", f"call_{turn}_{result.tool_calls_made}")

                payload, trace = self._dispatch_tool(
                    name, arguments_raw, turn=turn,
                )
                result.traces.append(trace)
                result.tool_calls_made += 1

                messages.append(_make_tool_message(tc_id, payload))

                log.info(
                    "agent_tool_call",
                    turn=turn,
                    name=name,
                    ok=trace.ok,
                    duration_ms=trace.duration_ms,
                    result_chars=trace.result_chars,
                )

        # max_turns 到了仍在请求工具 → 强制再发一次禁用工具的请求拿收尾文字
        result.truncated = True
        log.warning(
            "agent_max_turns_exhausted",
            max_turns=max_turns,
            tool_calls=result.tool_calls_made,
            note="将禁用 tools 强制 LLM 给出最终回复 (并关掉 thinking 避免 token 被推理吃完)",
        )
        clean_msgs = strip_reasoning(messages)
        final_chat = self.client.chat(
            clean_msgs,
            model=model,
            tools=None,
            response_format=response_format,
            thinking=None,                # 收尾不再 thinking, 上下文已大, 防 token 被吃光
            temperature=temperature,
            max_tokens=16384,             # 显式放大: 收尾要输出完整长 JSON, 防默认上限截断成空
        )
        result.accumulate(final_chat)
        result.content = final_chat.content
        result.finish_reason = final_chat.finish_reason
        messages.append(final_chat.raw_message)

        # 收尾仍返回空 content (上下文过大 / 模型异常) → 再降级重试一次:
        # 砍掉历史只留首条 system + 最后一条 user 诉求, 强制产出 JSON.
        if not (final_chat.content or "").strip():
            log.warning("agent_final_empty_retry", note="收尾 content 为空, 精简上下文重试")
            retry_msgs = _minimal_retry_messages(messages)
            if retry_msgs:
                retry_chat = self.client.chat(
                    retry_msgs,
                    model=model,
                    tools=None,
                    response_format=response_format,
                    thinking=None,
                    temperature=temperature,
                    max_tokens=16384,
                )
                result.accumulate(retry_chat)
                result.content = retry_chat.content
                result.finish_reason = retry_chat.finish_reason
                messages.append(retry_chat.raw_message)
        return result


__all__ = [
    "Agent",
    "AgentResult",
    "ToolCallTrace",
]
