"""文章处理：相关度判断 → 摘要 → 标签；LLM 不可用时降级。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.client import LLMClient, LLMError
from app.ai.prompts import render_relevance_prompt, render_summary_prompt, render_tag_prompt
from app.config import Settings
from app.models import Article
from app.utils.logger import get_logger
from app.utils.text import split_tags, strip_html, truncate

log = get_logger(__name__)

STATUS_PROCESSED = "processed"
STATUS_FAILED = "failed"


def _is_affirmative(answer: str) -> bool:
    """把 LLM 的 yes/no 回答归一化（容忍 "Yes."、"是的" 等）。"""
    head = answer.strip().casefold()[:8]
    return head.startswith("yes") or head.startswith("y") or head.startswith("是")


def _fallback_summary(article: Article, chars: int) -> str:
    body = strip_html(article.content) or article.title
    return truncate(body, chars)


def process_article(session: Session, article: Article, client: LLMClient, settings: Settings) -> str:
    """处理单篇文章，返回最终 status（processed / failed）。"""
    topic = settings.research_topic
    excerpt = truncate(strip_html(article.content), 2000)

    try:
        answer = client.chat(render_relevance_prompt(settings.prompts, topic, article.title, excerpt))
        if not _is_affirmative(answer):
            article.relevance = 0
            article.summary = None
            article.tags = None
            article.status = STATUS_PROCESSED
            return STATUS_PROCESSED

        article.relevance = 1
        summary = client.chat(render_summary_prompt(settings.prompts, topic, article.title, excerpt))
        tags = client.chat(render_tag_prompt(settings.prompts, article.title, summary))
        article.summary = truncate(summary, 500)
        article.tags = ",".join(split_tags(tags)) or None
        article.status = STATUS_PROCESSED
        return STATUS_PROCESSED
    except LLMError as exc:
        chars = settings.prompts.fallback_summary_chars
        article.summary = _fallback_summary(article, chars)
        article.tags = None
        if article.relevance is None:
            article.relevance = 1  # 相关度未知时先算相关，避免漏掉当天内容
        article.status = STATUS_FAILED
        log.warning("文章处理失败，已降级：%s（%s）", article.title[:60], exc)
        return STATUS_FAILED


def process_pending(
    session: Session,
    client: LLMClient,
    settings: Settings,
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    """批量处理 status=pending 的文章。"""
    statement = select(Article).where(Article.status == "pending").order_by(Article.published_at.desc())
    if limit is not None:
        statement = statement.limit(limit)
    articles = list(session.execute(statement).scalars())

    stats = {"pending": len(articles), "processed": 0, "irrelevant": 0, "failed": 0}
    for article in articles:
        previous_relevance = article.relevance
        status = process_article(session, article, client, settings)
        if status == STATUS_FAILED:
            stats["failed"] += 1
        elif article.relevance == 0 and previous_relevance != 0:
            stats["irrelevant"] += 1
        else:
            stats["processed"] += 1
    session.flush()
    return stats
