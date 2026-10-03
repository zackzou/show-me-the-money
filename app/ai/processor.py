"""文章处理：相关度判断（顺带评分）→ 摘要 → 速览 → 推荐理由 → 标签；LLM 不可用时降级。"""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.client import LLMClient, LLMError
from app.ai.prompts import (
    render_classify_prompt,
    render_digest_prompt,
    render_reason_prompt,
    render_relevance_prompt,
    render_summary_prompt,
    render_tag_prompt,
)
from app.config import Settings
from app.models import Article
from app.utils.logger import get_logger
from app.utils.text import split_tags, strip_html, truncate

_TAG_SPLIT_RE = re.compile(r"[,，、;；]")

log = get_logger(__name__)

STATUS_PROCESSED = "processed"
STATUS_FAILED = "failed"

_SCORE_RE = re.compile(r"(\d{1,3})")

# 列表页只在分数不低于这个值时强调「值得看」
SCORE_STRONG = 70


def parse_relevance(answer: str) -> tuple[bool, int | None]:
    """解析相关度判断的结果：``yes 85`` → ``(True, 85)``。

    容忍各种不规范输出：光一个 ``yes``、中文「是的」、分数在前、分数越界等。
    评分是可选的 —— 模型没给就不给，不能因此把一条好内容判掉。
    """
    text = (answer or "").strip()
    head = text.casefold()[:8]
    relevant = head.startswith(("yes", "y")) or head.startswith("是")
    match = _SCORE_RE.search(text)
    score = None
    if match:
        value = int(match.group(1))
        if 0 <= value <= 100:
            score = value
    return relevant, score


def _is_affirmative(answer: str) -> bool:
    """只要 yes/no 判定，忽略评分。"""
    return parse_relevance(answer)[0]


def _fallback_summary(article: Article, chars: int) -> str:
    body = strip_html(article.content) or article.title
    return truncate(body, chars)


def _fallback_digest(article: Article, chars: int) -> str:
    """LLM 不可用时的速览：直接取正文开头，好歹让页内能读。"""
    return truncate(strip_html(article.content_full) or strip_html(article.content) or article.title, chars)


def category_hints(categories: list[Any]) -> str:
    """把分类清单渲染成给模型看的说明（含每类的判定提示）。"""
    return "\n".join(f"- {c.name}：{c.hint}" if c.hint else f"- {c.name}" for c in categories)


def parse_classify(raw: str, valid: set[str]) -> tuple[str | None, list[str]]:
    """解析分类结果：第一行分类名，第二行主题。

    容错：模型多写几行、分类名不在清单里、主题里混进分类名 —— 都不能让整条内容挂掉。
    """
    lines = [line.strip() for line in (raw or "").splitlines() if line.strip()]
    category = None
    topics: list[str] = []
    for line in lines:
        head = line.lstrip(" -•*#").strip()
        if category is None and head in valid:
            category = head
            continue
        for piece in _TAG_SPLIT_RE.split(head):
            name = piece.strip().strip("「」【】")
            if name and name not in valid and len(name) <= 20 and name not in topics:
                topics.append(name)
    return category, topics[:3]


def _optional_llm(client: LLMClient, prompt: str, fallback: str) -> str:
    """跑一个「锦上添花」的 LLM 步骤；失败就用 fallback，不影响主流程。"""
    if not prompt:
        return fallback
    try:
        return client.chat(prompt)
    except LLMError as exc:
        log.warning("可选步骤失败，已降级（%s）", exc)
        return fallback


def process_article(session: Session, article: Article, client: LLMClient, settings: Settings) -> str:
    """处理单篇文章，返回最终 status（processed / failed）。"""
    topic = settings.research_topic
    # 优先用抓回来的正文做判断，摘要质量直接决定筛选与写作的质量
    body = strip_html(article.content_full) or strip_html(article.content)
    excerpt = truncate(body, 2000)

    try:
        answer = client.chat(render_relevance_prompt(settings.prompts, topic, article.title, excerpt))
        relevant, score = parse_relevance(answer)
        if not relevant:
            article.relevance = 0
            article.score = score
            article.summary = None
            article.digest = None
            article.reason = None
            article.category = None
            article.topics = None
            article.tags = None
            article.status = STATUS_PROCESSED
            return STATUS_PROCESSED

        article.relevance = 1
        article.score = score

        summary = client.chat(render_summary_prompt(settings.prompts, topic, article.title, excerpt))
        article.summary = truncate(summary, 500)

        # 速览：让读者在页内读完，不用跳原站
        digest = _optional_llm(
            client,
            render_digest_prompt(settings.prompts, article.title, summary, excerpt),
            _fallback_digest(article, settings.prompts.fallback_digest_chars),
        )
        article.digest = truncate(digest, 400)

        # 推荐理由：回答「为什么要点开这条」
        reason = _optional_llm(
            client,
            render_reason_prompt(settings.prompts, topic, article.title, summary),
            _fallback_digest(article, settings.prompts.fallback_reason_chars),
        )
        article.reason = truncate(reason, 200)

        # 分类 + 主题：一次调用同时产出，失败只丢这两个字段
        if settings.categories:
            raw_classify = _optional_llm(
                client,
                render_classify_prompt(
                    settings.prompts,
                    topic,
                    article.title,
                    summary,
                    category_hints(settings.categories),
                ),
                "",
            )
            category, topics = parse_classify(raw_classify, {c.name for c in settings.categories})
            article.category = category
            article.topics = json.dumps(topics, ensure_ascii=False) if topics else None

        tags = client.chat(render_tag_prompt(settings.prompts, article.title, summary))
        article.tags = ",".join(split_tags(tags)) or None
        article.status = STATUS_PROCESSED
        return STATUS_PROCESSED
    except LLMError as exc:
        article.summary = _fallback_summary(article, settings.prompts.fallback_summary_chars)
        if article.digest is None:
            article.digest = _fallback_digest(article, settings.prompts.fallback_digest_chars)
        if article.reason is None:
            article.reason = truncate(article.summary, settings.prompts.fallback_reason_chars)
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
            if article.digest is None:
                article.digest = _fallback_digest(article, settings.prompts.fallback_digest_chars)
            if article.reason is None:
                article.reason = truncate(article.summary, settings.prompts.fallback_reason_chars)
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