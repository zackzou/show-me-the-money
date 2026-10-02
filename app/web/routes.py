"""HTML 页面路由（Jinja2 + Tailwind CDN）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Article, DailyReport, Source
from app.report.generator import STATUS_REPORTABLE, day_window
from app.utils.text import split_tags

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

page_router = APIRouter()


def articles_of_day(session: Session, date_str: str) -> list[dict[str, Any]]:
    """某一天进日报的文章（页面直接成卡片展示，不必让读者去读 Markdown 源文）。

    口径与日报生成保持一致（含降级摘要的文章），否则页面会比日报多/少东西。
    """
    try:
        start, end = day_window(date_str)
    except ValueError:
        return []  # 非法日期当作「这一天没有内容」，由路由返回 404
    statement = (
        select(Article, Source.name)
        .outerjoin(Source, Article.source_id == Source.id)
        .where(
            Article.relevance == 1,
            Article.status.in_(STATUS_REPORTABLE),
            Article.published_at >= start,
            Article.published_at < end,
        )
        .order_by(Article.published_at.desc(), Article.id.desc())
    )
    items: list[dict[str, Any]] = []
    for article, source_name in session.execute(statement):
        items.append(
            {
                "title": article.title,
                "link": article.link,
                "source": source_name or "未知来源",
                "time": article.published_at.strftime("%m-%d %H:%M") if article.published_at else "",
                "summary": article.summary or "",
                "tags": split_tags(article.tags),
                "degraded": article.status == "failed",
            }
        )
    return items


def _topics(request: Request) -> list[str]:
    return list(getattr(request.app.state.settings, "research_topics", []) or [])


@page_router.get("/", response_class=HTMLResponse)
def index(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    latest = session.execute(select(DailyReport).order_by(DailyReport.date.desc()).limit(1)).scalar_one_or_none()
    articles = articles_of_day(session, latest.date) if latest else []
    return templates.TemplateResponse(
        request,
        "index.html",
        {"report": latest, "articles": articles, "topics": _topics(request), "title": "最新日报"},
    )


@page_router.get("/daily/{date}", response_class=HTMLResponse)
def daily(request: Request, date: str, session: Session = Depends(get_session)) -> HTMLResponse:
    report = session.execute(select(DailyReport).where(DailyReport.date == date)).scalar_one_or_none()
    articles = articles_of_day(session, date)
    return templates.TemplateResponse(
        request,
        "daily.html",
        {
            "report": report,
            "articles": articles,
            "date": date,
            "topics": _topics(request),
            "title": f"{date} 日报",
        },
        status_code=200 if report else 404,
    )


@page_router.get("/archive", response_class=HTMLResponse)
def archive(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    reports = list(
        session.execute(select(DailyReport).order_by(DailyReport.date.desc()).limit(100)).scalars()
    )
    return templates.TemplateResponse(request, "archive.html", {"reports": reports, "title": "历史日报"})
