"""HTML 页面路由（Jinja2）。

页面口径与日报生成共用 ``day_window`` 与 ``STATUS_REPORTABLE``，否则页面会比日报多/少东西。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Article, DailyReport, Source
from app.report.generator import STATUS_REPORTABLE, day_window
from app.utils.text import now_local, split_tags, strip_markdown, truncate

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

page_router = APIRouter()

PAGE_SIZE = 20


def _images(raw: str | None) -> list[str]:
    """image_urls 存的是 JSON 数组；老数据/坏数据一律当没有图。"""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in parsed if isinstance(item, str) and item.startswith(("http://", "https://"))]


def _day_statement(date_str: str):
    start, end = day_window(date_str)
    return (
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


def _card(article: Article, source_name: str | None) -> dict[str, Any]:
    return {
        "id": article.id,
        "title": article.title,
        "link": article.link,
        "source": source_name or "未知来源",
        "time": article.published_at.strftime("%m-%d %H:%M") if article.published_at else "",
        # 速览优先（页内就能读完），没有就退回摘要
        "digest": strip_markdown(article.digest) or strip_markdown(article.summary),
        "summary": strip_markdown(article.summary),
        "tags": split_tags(article.tags),
        "images": _images(article.image_urls),
        "degraded": article.status == "failed",
    }


def articles_of_day(session: Session, date_str: str) -> list[dict[str, Any]]:
    """某一天进日报的全部文章（卡片数据）。非法日期当作「这一天没有内容」。"""
    try:
        return [_card(article, name) for article, name in session.execute(_day_statement(date_str))]
    except ValueError:
        return []


def count_of_day(session: Session, date_str: str) -> int:
    try:
        start, end = day_window(date_str)
    except ValueError:
        return 0  # 非法日期当作「这一天没有内容」
    return int(
        session.execute(
            select(func.count(Article.id)).where(
                Article.relevance == 1,
                Article.status.in_(STATUS_REPORTABLE),
                Article.published_at >= start,
                Article.published_at < end,
            )
        ).scalar()
        or 0
    )


def articles_of_day_paged(
    session: Session, date_str: str, page: int = 1, size: int = PAGE_SIZE
) -> list[dict[str, Any]]:
    """分页取某一天的文章（首页用，避免一次渲染全部）。"""
    try:
        statement = _day_statement(date_str).limit(size).offset(max(0, page - 1) * size)
        return [_card(article, name) for article, name in session.execute(statement)]
    except ValueError:
        return []


def _topics(request: Request) -> list[str]:
    return list(getattr(request.app.state.settings, "research_topics", []) or [])


def _ctx(request: Request, **extra: Any) -> dict[str, Any]:
    return {"topics": _topics(request), **extra}


@page_router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    page: int = Query(1, ge=1),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """首页：热点资讯流。按时间倒序 + 分页，不用一次渲染全部。"""
    latest = session.execute(select(DailyReport).order_by(DailyReport.date.desc()).limit(1)).scalar_one_or_none()
    date_str = latest.date if latest else now_local().strftime("%Y-%m-%d")
    total = count_of_day(session, date_str)
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(page, pages)
    return templates.TemplateResponse(
        request,
        "index.html",
        _ctx(
            request,
            report=latest,
            date_str=date_str,
            articles=articles_of_day_paged(session, date_str, page, PAGE_SIZE),
            total=total,
            page=page,
            pages=pages,
            title="热点资讯",
        ),
    )


@page_router.get("/daily/{date}", response_class=HTMLResponse)
def daily(
    request: Request,
    date: str,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    report = session.execute(select(DailyReport).where(DailyReport.date == date)).scalar_one_or_none()
    articles = articles_of_day(session, date)
    return templates.TemplateResponse(
        request,
        "daily.html",
        _ctx(request, report=report, articles=articles, date=date, title=f"{date} 日报"),
        status_code=200 if report else 404,
    )


@page_router.get("/story/{article_id}", response_class=HTMLResponse)
def story(request: Request, article_id: int, session: Session = Depends(get_session)) -> HTMLResponse:
    """单篇页内预览：速览 + 配图 + 正文开头，读者不用跳原站也能读完。"""
    row = session.execute(
        select(Article, Source.name).outerjoin(Source, Article.source_id == Source.id).where(Article.id == article_id)
    ).first()
    if row is None:
        return templates.TemplateResponse(
            request, "story.html", _ctx(request, article=None, title="内容不存在"), status_code=404
        )
    article, source_name = row
    card = _card(article, source_name)
    card["content_text"] = truncate(article.content or article.title, 1500)
    return templates.TemplateResponse(request, "story.html", _ctx(request, article=card, title=article.title[:40]))


@page_router.get("/archive", response_class=HTMLResponse)
def archive(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    reports = list(session.execute(select(DailyReport).order_by(DailyReport.date.desc()).limit(100)).scalars())
    return templates.TemplateResponse(request, "archive.html", _ctx(request, reports=reports, title="历史日报"))