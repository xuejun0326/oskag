"""describe/prompts.py — Jinja2 模板加载与渲染.

模板放在 oskag/prompts/*.j2, 每份模板用 ===SYSTEM=== / ===USER=== 标记分隔,
loader 解析成 [{"role":"system","content":...}, {"role":"user","content":...}].

设计:
- 单一 Environment 实例 (caching), 模块级初始化
- 严格模式: undefined 变量直接报错 (StrictUndefined), 防止 prompt 里悄悄空值
- 自动 trim_blocks + lstrip_blocks, 让 Jinja 控制语句不影响输出空白
- render_messages(template_name, **vars) → list[dict] 直接喂 client.chat()
"""
from __future__ import annotations

import re
from pathlib import Path

from jinja2 import (
    Environment,
    FileSystemLoader,
    StrictUndefined,
    TemplateNotFound,
)

from oskag.logging_setup import get_logger

log = get_logger("oskag.describe.prompts")


_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_SECTION_RE = re.compile(r"^===(SYSTEM|USER|ASSISTANT)===\s*$", re.MULTILINE)


# 单例 Jinja env
_env = Environment(
    loader=FileSystemLoader(str(_PROMPTS_DIR)),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=False,
    autoescape=False,                        # markdown/代码不需要 html escape
)


def _split_sections(text: str) -> list[tuple[str, str]]:
    """按 ===ROLE=== 切分模板渲染产物.

    返回 [(role, content), ...], role ∈ {"system","user","assistant"}.
    没有任何 ===ROLE=== 标记时, 整段当作 user.
    """
    matches = list(_SECTION_RE.finditer(text))
    if not matches:
        body = text.strip()
        return [("user", body)] if body else []

    sections: list[tuple[str, str]] = []
    # ===ROLE=== 之前的内容 (header 注释等) 整段忽略
    for i, m in enumerate(matches):
        role = m.group(1).lower()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[start:end].strip()
        if content:
            sections.append((role, content))
    return sections


def render_messages(template_name: str, **variables) -> list[dict[str, str]]:
    """渲染模板 → OpenAI messages 列表.

    Args:
        template_name: 模板文件名 (如 "describe_module.j2")
        **variables: 注入模板的变量 (StrictUndefined 模式, 缺失会抛)

    Returns:
        list of {"role":..., "content":...}
    """
    try:
        template = _env.get_template(template_name)
    except TemplateNotFound:
        raise FileNotFoundError(
            f"prompt template not found: {template_name} "
            f"(searched in {_PROMPTS_DIR})"
        ) from None

    rendered = template.render(**variables)
    sections = _split_sections(rendered)
    if not sections:
        raise ValueError(f"template {template_name} rendered to empty content")

    messages = [{"role": role, "content": content} for role, content in sections]
    log.info(
        "prompt_rendered",
        template=template_name,
        sections=[role for role, _ in sections],
        total_chars=sum(len(c) for _, c in sections),
    )
    return messages


def list_templates() -> list[str]:
    """列出 prompts/ 目录下的所有 .j2 模板."""
    return sorted(_env.list_templates(extensions=["j2"]))


__all__ = [
    "render_messages",
    "list_templates",
]
