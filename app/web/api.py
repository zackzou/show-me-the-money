"""JSON API 路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import __version__
from app.db import get_session
from app.fetcher.content import count_with_full_text
from app.fetcher.images import count_with_images
from app.models import Article, DailyReport, Source
from app.report.generator import STATUS_REPORTABLE, day_window
from app.schemas import ArticleOut, HealthOut, ReportDetail, ReportOut
from app.utils.text import now_local

api_router = APIRouter(prefix="/api")


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
