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


def render_digest_prompt(
    prompts: PromptsConfig,
    title: str,
    summary: str,
    content: str,
    *,
    bilingual: bool = True,
) -> str:
    """速览提示词。模板为空（老配置）时返回空串，调用方据此跳过这一步。

    ``bilingual=False`` 用于中文原文：模板里那句「再给一行 EN 英文速览」
    会让模型顺手翻一遍，产出的是**没人会看的英文版**（中文原生文章的页面上
    不给 EN 按钮）。这里补一句「只输出中文一行」，省掉整段英文输出。
    """
    if not prompts.digest_prompt.strip():
        return ""
    text = _fill(
        prompts.digest_prompt,
        {
            "title": title,
            "summary": summary or "（无）",
            "content": truncate(content, MAX_ARTICLE_CHARS) or "（无正文）",
        },
    )
    if not bilingual:
        text += "\n\n【本次只输出中文速览一行，不要输出 EN 行，不要输出英文。】"
    return text


def render_reason_prompt(prompts: PromptsConfig, topic: str, title: str, summary: str) -> str:
    """推荐理由。模板为空（老配置）时返回空串，调用方据此跳过。"""
    if not prompts.reason_prompt.strip():
        return ""
    return _fill(prompts.reason_prompt, {"research_topic": topic, "title": title, "summary": summary or "（无）"})


def render_classify_prompt(prompts: PromptsConfig, topic: str, title: str, summary: str, categories: str) -> str:
    """分类 + 主题。模板或分类清单为空时返回空串，调用方据此跳过。"""
    if not prompts.classify_prompt.strip() or not categories.strip():
        return ""
    return _fill(
        prompts.classify_prompt,
        {"categories": categories, "research_topic": topic, "title": title, "summary": summary or "（无）"},
    )


def render_translate_prompt(prompts: PromptsConfig, title: str, summary: str) -> str:
    """中译英。模板为空时返回空串，调用方据此跳过。"""
    if not prompts.translate_prompt.strip():
        return ""
    return _fill(prompts.translate_prompt, {"title": title, "summary": summary or "（无）"})


def render_translate_title_zh_prompt(prompts: PromptsConfig, title: str) -> str:
    """英译中标题。模板为空时返回空串，调用方据此跳过。"""
    if not prompts.translate_title_zh_prompt.strip():
        return ""
    return _fill(prompts.translate_title_zh_prompt, {"title": title})


def render_translate_content_prompt(prompts: PromptsConfig, text: str) -> str:
    """英译中正文。模板为空时返回空串，调用方据此跳过。"""
    if not prompts.translate_content_prompt.strip():
        return ""
    return _fill(prompts.translate_content_prompt, {"text": text})


def render_translate_batch_prompt(prompts: PromptsConfig, chunks: list[str]) -> str:
    """英译中正文（批量）。模板为空时返回空串，调用方走逐块兜底。

    把 N 个块一次交给模型，要求返回 JSON 数组 —— 对齐 AIHOT 的
    ``translate-body``（``{"t": ["…", "…"]}``）。占位符：
      ``{count}``   块数（提示词用它强调「必须恰好 N 项」）
      ``{segments}`` 编号后的块正文
    """
    template = prompts.translate_batch_prompt.strip()
    if not template or not chunks:
        return ""
    segments = "\n\n".join(
        f"【片段 {i + 1}】\n{chunk}" for i, chunk in enumerate(chunks)
    )
    return _fill(template, {"count": str(len(chunks)), "segments": segments})


def render_translate_digest_zh_prompt(prompts: PromptsConfig, text: str) -> str:
    """英译中速览（AI 导读）。模板为空（老配置）时返回空串。"""
    if not prompts.translate_digest_zh_prompt.strip():
        return ""
    return _fill(prompts.translate_digest_zh_prompt, {"text": text})


def render_relevance_prompt(prompts: PromptsConfig, topic: str, title: str, summary: str) -> str:
    return _fill(
        prompts.relevance_prompt,
        {"research_topic": topic, "title": title, "summary": truncate(summary, 500) or "（无摘要）"},
    )


def render_same_story_prompt(prompts: PromptsConfig, title_a: str, title_b: str, shared: str = "") -> str:
    """同题判定：两条报道是不是同一件事。模板为空（老配置）时返回空串。"""
    if not prompts.same_story_prompt.strip():
        return ""
    return _fill(
        prompts.same_story_prompt,
        {"title_a": title_a, "title_b": title_b, "shared_words": shared or "（无）"},
    )


def render_brief_prompt(prompts: PromptsConfig, title: str, digest: str) -> str:
    """早报推送语。模板为空（老配置）时返回空串，调用方回退截断拼凑。"""
    if not prompts.brief_prompt.strip():
        return ""
    return _fill(
        prompts.brief_prompt,
        {"title": title, "digest": digest or "（无导读）"},
    )


def render_structure_prompt(prompts: PromptsConfig, title: str, text: str) -> str:
    """正文章节结构。模板为空（老配置）时返回空串，调用方走原文直排。"""
    if not prompts.structure_prompt.strip():
        return ""
    return _fill(
        prompts.structure_prompt,
        {"title": title, "text": truncate(text, MAX_ARTICLE_CHARS * 2)},
    )
