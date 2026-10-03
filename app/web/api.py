"""JSON API 路由。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import __version__
from app.db import get_session
from app.fetcher.content import count_with_full_text
from app.fetcher.images import count_with_images
from app.models import Article, DailyReport, Source
from app.report.generator import STATUS_REPORTABLE, day_window
from app.schemas import ArticleDetailOut, ArticleOut, HealthOut, ReportDetail, ReportOut
from app.utils.text import BRIEF_DIGEST_CHARS as _BRIEF_DIGEST_CHARS
from app.utils.text import brief_digest, now_local

api_router = APIRouter(prefix="/api")


# 早报汇总的下限：低于这个字数在手机上只有两行，推送里显得敷衍
_BRIEF_MIN_CHARS = 70


@api_router.get("/articles", response_model=list[ArticleOut])
def list_articles(date: str | None = None, limit: int = 200, session: Session = Depends(get_session)):
    """按日期（默认**北京时间**今天）列出进日报的文章。"""
    try:
        start, end = day_window(date or now_local().strftime("%Y-%m-%d"))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"日期格式应为 YYYY-MM-DD：{date!r}") from None
    statement = (
        select(Article)
        .where(
            Article.relevance == 1,
            Article.status.in_(STATUS_REPORTABLE),
            Article.published_at >= start,
            Article.published_at < end,
        )
        .order_by(Article.published_at.desc(), Article.id.desc())
        .limit(max(1, min(limit, 1000)))
    )
    return list(session.execute(statement).scalars())


@api_router.get("/articles/{article_id}", response_model=ArticleDetailOut)
def get_article(article_id: int, session: Session = Depends(get_session)):
    """单篇文章详情。

    早报片段浮层点开时才来取 —— 顺带让收藏页能按 id 拿历史文章，
    不必把整页几百篇都拉回来。
    """
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="文章不存在")
    source = session.get(Source, article.source_id) if article.source_id else None
    return ArticleDetailOut(
        **ArticleOut.model_validate(article).model_dump(),
        source_name=source.name if source else "未知来源",
        # 汇总太短撑不起三到五行，就用推荐理由补上（去重，避免复读同一句）
        digest_brief=_digest_with_fallback(article),
        topics_list=_topics_json(article.topics)[:3],
    )


def _topics_json(raw: str | None) -> list[str]:
    """topics 库里存的是 JSON 数组字符串，直接 split 会把引号一起带出来。"""
    try:
        parsed = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return [str(x) for x in parsed if isinstance(x, str)] if isinstance(parsed, list) else []


def _digest_with_fallback(article: Article) -> str:
    """早报汇总：导读优先，太短就补推荐理由，再不够才退回摘要。"""
    brief = brief_digest(article.digest or "")
    if len(brief) >= _BRIEF_MIN_CHARS or not brief:
        return brief
    extra = brief_digest(article.reason or article.summary or "", limit=_BRIEF_DIGEST_CHARS - len(brief))
    # 收尾的标点先剥掉再拼，否则会出现「记录。。Apple」这种双句号
    extra = extra.rstrip("。！？.!? ，,;；")
    if not extra or extra in brief:
        return brief
    # brief 结尾没有句号时才补一个；已经有就别补，否则会拼出「结尾。。推荐」
    joiner = "" if brief[-1] in "。！？.!?" else "。"
    return f"{brief}{joiner}{extra}"[:_BRIEF_DIGEST_CHARS]


@api_router.get("/reports", response_model=list[ReportOut])
def list_reports(limit: int = 100, session: Session = Depends(get_session)):
    statement = select(DailyReport).order_by(DailyReport.date.desc()).limit(max(1, min(limit, 500)))
    return list(session.execute(statement).scalars())


@api_router.get("/reports/{date}", response_model=ReportDetail)
def get_report(date: str, session: Session = Depends(get_session)):
    report = session.execute(select(DailyReport).where(DailyReport.date == date)).scalar_one_or_none()
    if report is None:
        raise HTTPException(status_code=404, detail=f"{date} 没有日报")
    return report


@api_router.get("/health", response_model=HealthOut)
def health(request: Request, session: Session = Depends(get_session)) -> HealthOut:
    settings = getattr(request.app.state, "settings", None)
    return HealthOut(
        status="ok",
        version=__version__,
        database="ok",
        sources=session.query(Source).count(),
        articles=session.query(Article).count(),
        with_images=count_with_images(session),
        with_full_text=count_with_full_text(session),
        reports=session.query(DailyReport).count(),
        research_topics=list(getattr(settings, "research_topics", []) or []),
    )
