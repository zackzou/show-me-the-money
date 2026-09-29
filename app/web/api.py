"""JSON API 路由。"""

from __future__ import annotations

from datetime import date as date_type

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import __version__
from app.db import get_session
from app.models import Article, DailyReport, Source
from app.schemas import ArticleOut, HealthOut, ReportDetail, ReportOut

api_router = APIRouter(prefix="/api")


@api_router.get("/articles", response_model=list[ArticleOut])
def list_articles(date: str | None = None, limit: int = 200, session: Session = Depends(get_session)):
    """按日期（默认今天）列出文章。"""
    target = date or date_type.today().isoformat()
    statement = (
        select(Article)
        .where(func.date(Article.published_at) == target)
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
        reports=session.query(DailyReport).count(),
        research_topics=list(getattr(settings, "research_topics", []) or []),
    )
