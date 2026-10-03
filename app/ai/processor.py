"""文章处理：相关度判断 → 摘要 → 标签；LLM 不可用时降级。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.client import LLMClient, LLMError
from app.ai.prompts import (
    render_digest_prompt,
    render_relevance_prompt,
    render_summary_prompt,
    render_tag_prompt,
)
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


def _fallback_digest(article: Article, chars: int) -> str:
    """LLM 不可用时的速览：直接取正文开头，好歹让页内能读。"""
    return truncate(strip_html(article.content) or article.title, chars)


def process_article(session: Session, article: Article, client: LLMClient, settings: Settings) -> str:
    """处理单篇文章，返回最终 status（processed / failed）。"""
    topic = settings.research_topic
    excerpt = truncate(strip_html(article.content), 2000)

    try:
        answer = client.chat(render_relevance_prompt(settings.prompts, topic, article.title, excerpt))
        if not _is_affirmative(answer):
            article.relevance = 0
            article.summary = None
            article.digest = None
            article.tags = None
            article.status = STATUS_PROCESSED
            return STATUS_PROCESSED

        article.relevance = 1
        summary = client.chat(render_summary_prompt(settings.prompts, topic, article.title, excerpt))
        article.summary = truncate(summary, 500)

        # 速览：让读者在页内读完，不用跳原站。生成失败不影响主流程。
        digest_prompt = render_digest_prompt(settings.prompts, article.title, summary, excerpt)
        if digest_prompt:
            try:
                article.digest = truncate(client.chat(digest_prompt), 400)
            except LLMError as exc:
                article.digest = _fallback_digest(article, settings.prompts.fallback_digest_chars)
                log.warning("速览生成失败，已降级：%s（%s）", article.title[:60], exc)

        tags = client.chat(render_tag_prompt(settings.prompts, article.title, summary))
        article.tags = ",".join(split_tags(tags)) or None
        article.status = STATUS_PROCESSED
        return STATUS_PROCESSED
    except LLMError as exc:
        chars = settings.prompts.fallback_summary_chars
        article.summary = _fallback_summary(article, chars)
        if article.digest is None:
            article.digest = _fallback_digest(article, settings.prompts.fallback_digest_chars)
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
    """批量处理 status=pending 的文章。

    单篇出错只标记这一篇并继续 —— 否则一篇坏数据就能让整批卡在 pending，
    下一个周期又重来一遍。每处理 ``checkpoint_every`` 篇提交一次，
    避免长任务中途失败把已完成的进度一起回滚。
    """
    statement = select(Article).where(Article.status == "pending").order_by(Article.published_at.desc())
    if limit is not None:
        statement = statement.limit(limit)
    articles = list(session.execute(statement).scalars())

    stats = {"pending": len(articles), "processed": 0, "irrelevant": 0, "failed": 0, "crashed": 0}
    checkpoint = max(1, settings.ai.batch_checkpoint_every)
    for index, article in enumerate(articles, start=1):
        previous_relevance = article.relevance
        try:
            status = process_article(session, article, client, settings)
        except Exception as exc:  # 兜底：任何意外都不该中断整批
            article.status = STATUS_FAILED
            if article.summary is None:
                article.summary = _fallback_summary(article, settings.prompts.fallback_summary_chars)
            if article.relevance is None:
                article.relevance = 1
            stats["crashed"] += 1
            log.error("处理文章时出现未预期异常，已跳过：%s（%r）", article.title[:60], exc)
            status = STATUS_FAILED
        if status == STATUS_FAILED:
            stats["failed"] += 1
        elif article.relevance == 0 and previous_relevance != 0:
            stats["irrelevant"] += 1
        else:
            stats["processed"] += 1
        if index % checkpoint == 0:
            session.flush()
    session.flush()
    return stats
