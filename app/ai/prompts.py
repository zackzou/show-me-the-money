"""提示词渲染：模板来自 config/default_prompts.yaml。

用按键替换而非 str.format，避免正文里的花括号把模板渲染炸掉。
"""

from __future__ import annotations

from app.config import PromptsConfig
from app.utils.text import truncate

MAX_ARTICLE_CHARS = 4000


def _fill(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", value)
    return rendered


def render_summary_prompt(prompts: PromptsConfig, topic: str, title: str, content: str) -> str:
    return _fill(
        prompts.summary_prompt,
        {
            "research_topic": topic,
            "title": title,
            "content": truncate(content, MAX_ARTICLE_CHARS) or "（无正文）",
        },
    )


def render_tag_prompt(prompts: PromptsConfig, title: str, summary: str) -> str:
    return _fill(prompts.tag_prompt, {"title": title, "summary": summary})


def render_relevance_prompt(prompts: PromptsConfig, topic: str, title: str, summary: str) -> str:
    return _fill(
        prompts.relevance_prompt,
        {"research_topic": topic, "title": title, "summary": truncate(summary, 500) or "（无摘要）"},
    )
