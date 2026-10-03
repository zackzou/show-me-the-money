"""HTML 页面路由（Jinja2）。

页面口径与日报生成共用 ``day_window`` 与 ``STATUS_REPORTABLE``，否则页面会比日报多/少东西。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import get_session
from app.models import Article, DailyReport, Source
from app.report.generator import STATUS_REPORTABLE, day_window
from app.utils.text import now_local, split_tags, strip_markdown, truncate
from app.web.search import (
    SCOPE_FULL,
    SCOPE_META,
    count_by_category,
    search_articles,
    search_metadata,
)

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

page_router = APIRouter()

PAGE_SIZE = 40
WEEKDAYS = "一二三四五六日"


def _images(raw: str | None) -> list[str]:
    """image_urls 存的是 JSON 数组；老数据/坏数据一律当没有图。"""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in parsed if isinstance(item, str) and item.startswith(("http://", "https://"))]


def _topics_of(article: Article) -> list[str]:
    """topics 存的是 JSON 数组；老数据/坏数据一律当没有。"""
    if not article.topics:
        return []
    try:
        parsed = json.loads(article.topics)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in parsed if isinstance(item, str)]


# 像小标题的段落：短、不以句末标点结尾、不是纯数字
_HEADING_MAX_CHARS = 30
_HEADING_MIN_CHARS = 2
_SENTENCE_END = "。！？；，、：,.!?;:…"


def _body_blocks(raw: str | None) -> list[str]:
    """正文全文按空行切段，供详情页逐段渲染。"""
    if not raw:
        return []
    return [part.strip() for part in raw.split("\n\n") if part.strip()]


def mark_headings(blocks: list[str]) -> list[dict[str, Any]]:
    """把正文里「短句、不以标点结尾」的段落标成小标题。

    这样详情页能像图1 那样给一个本文目录，并把小标题渲染成 ``<h3>``；
    剩下的当普通段落。判不准就当普通段落，不会漏内容。
    """
    marked: list[dict[str, Any]] = []
    for index, text in enumerate(blocks):
        stripped = text.strip()
        is_heading = (
            _HEADING_MIN_CHARS <= len(stripped) <= _HEADING_MAX_CHARS
            and stripped[-1] not in _SENTENCE_END
            and not stripped[0].isdigit()
        )
        marked.append({"i": index, "text": stripped, "heading": is_heading})
    return marked


def table_of_contents(marked: list[dict[str, Any]], *, limit: int = 12) -> list[dict[str, Any]]:
    return [item for item in marked if item["heading"]][:limit]


def _day_statement(date_str: str):
    start, end = day_window(date_str)
    return (
        select(Article, Source.name, Source.url)
        .outerjoin(Source, Article.source_id == Source.id)
        .where(
            Article.relevance == 1,
            Article.status.in_(STATUS_REPORTABLE),
            Article.published_at >= start,
            Article.published_at < end,
        )
        .order_by(Article.published_at.desc(), Article.id.desc())
    )


def _host(url: str | None) -> str:
    if not url:
        return ""
    trimmed = url.split("://", 1)[-1]
    return trimmed.split("/", 1)[0]


def _relative(value: datetime | None, now: datetime) -> str:
    """发布时间的人话版本：刚刚 / N 分钟前 / N 小时前 / N 天前 / 日期。"""
    if value is None:
        return ""
    delta = now - value
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return ""
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前"
    if seconds < 86400 * 7:
        return f"{seconds // 86400} 天前"
    return value.strftime("%Y-%m-%d")


def _card(article: Article, source_name: str | None, source_url: str | None, now: datetime) -> dict[str, Any]:
    published = article.published_at
    return {
        "id": article.id,
        "title": article.title,
        "title_en": article.title_en or "",
        "link": article.link,
        "source": source_name or "未知来源",
        "source_host": _host(source_url),
        "time_hm": published.strftime("%H:%M") if published else "",
        "date_key": published.strftime("%Y-%m-%d") if published else "",
        "time_full": published.strftime("%Y-%m-%d %H:%M") if published else "",
        "relative": _relative(published, now),
        # 速览优先（页内就能读完），没有就退回摘要
        "digest": strip_markdown(article.digest) or strip_markdown(article.summary),
        "digest_en": strip_markdown(article.digest_en),
        "reason": strip_markdown(article.reason),
        "score": article.score,
        "category": article.category or "",
        "topics": _topics_of(article),
        "tags": split_tags(article.tags),
        "images": _images(article.image_urls),
        "degraded": article.status == "failed",
    }


def articles_of_day(session: Session, date_str: str, now: datetime | None = None) -> list[dict[str, Any]]:
    """某一天进日报的全部文章（卡片数据）。非法日期当作「这一天没有内容」。"""
    now = now or now_local()
    try:
        rows = list(session.execute(_day_statement(date_str)))
    except ValueError:
        return []
    return [_card(article, name, url, now) for article, name, url in rows]


def articles_of_day_paged(
    session: Session,
    date_str: str,
    page: int = 1,
    size: int = PAGE_SIZE,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """分页取某一天的文章（首页用，避免一次渲染全部）。"""
    now = now or now_local()
    try:
        statement = _day_statement(date_str).limit(size).offset(max(0, page - 1) * size)
        rows = list(session.execute(statement))
    except ValueError:
        return []
    return [_card(article, name, url, now) for article, name, url in rows]


def _date_label(date_str: str) -> tuple[str, str]:
    try:
        day = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return date_str, ""
    return f"{day.month}月{day.day}日", f"星期{WEEKDAYS[day.weekday()]}"


def group_by_date(cards: list[dict[str, Any]], date_str: str) -> list[dict[str, Any]]:
    """按日期分组，组头显示「10月3日 星期六 · N 条」。单日时只出一个组。"""
    if not cards:
        return []
    label, weekday = _date_label(date_str)
    return [{"date": label, "weekday": weekday, "count": len(cards), "items": cards}]


def group_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """搜索结果可能跨天：按 YYYY-MM-DD 分组，保持时间倒序。"""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for card in cards:
        buckets.setdefault(card["date_key"], []).append(card)
    groups = []
    for date_key in sorted(buckets, reverse=True):
        label, weekday = _date_label(date_key)
        items = buckets[date_key]
        groups.append({"date": label, "weekday": weekday, "count": len(items), "items": items})
    return groups


def filter_by(
    cards: list[dict[str, Any]], *, category: str | None = None, tag: str | None = None
) -> list[dict[str, Any]]:
    """按分类 / 标签过滤卡片。分类与标签都在应用层过滤，语义更准。"""
    result = cards
    if category:
        result = [card for card in result if card["category"] == category]
    if tag:
        needle = tag.strip().casefold()
        result = [card for card in result if any(t.casefold() == needle for t in card["tags"])]
    return result


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


def _settings(request: Request) -> Settings | None:
    return getattr(request.app.state, "settings", None)


def _topics(request: Request) -> list[str]:
    settings = _settings(request)
    return list(getattr(settings, "research_topics", []) or [])


def _categories(request: Request) -> list[dict[str, str]]:
    settings = _settings(request)
    return [{"name": c.name, "hint": c.hint} for c in getattr(settings, "categories", []) or []]


def _ctx(request: Request, **extra: Any) -> dict[str, Any]:
    return {"topics": _topics(request), "categories": _categories(request), **extra}


@page_router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    page: int = Query(1, ge=1),
    cat: str | None = None,
    tag: str | None = None,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """首页：热点资讯时间轴。按时间倒序 + 分页，可用 ?cat= / ?tag= 过滤。"""
    latest = session.execute(select(DailyReport).order_by(DailyReport.date.desc()).limit(1)).scalar_one_or_none()
    date_str = latest.date if latest else now_local().strftime("%Y-%m-%d")
    now = now_local()
    total = count_of_day(session, date_str)
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(page, pages)
    cards = articles_of_day_paged(session, date_str, page, PAGE_SIZE, now)
    cards = filter_by(cards, category=cat, tag=tag)
    return templates.TemplateResponse(
        request,
        "index.html",
        _ctx(
            request,
            report=latest,
            date_str=date_str,
            groups=group_by_date(cards, date_str),
            articles=cards,
            total=total,
            page=page,
            pages=pages,
            active_cat=cat or "",
            active_tag=tag or "",
            title="热点资讯",
        ),
    )


@page_router.get("/search", response_class=HTMLResponse)
def search_page(
    request: Request,
    q: str = "",
    scope: str = SCOPE_META,
    cat: str | None = None,
    tag: str | None = None,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """站内搜索。``scope=全文`` 时连正文一起搜。"""
    keyword = (q or "").strip()
    effective_scope = SCOPE_FULL if scope == SCOPE_FULL else SCOPE_META
    rows = (
        search_articles(session, keyword, scope=effective_scope, category=cat, tag=tag)
        if keyword
        else []
    )
    now = now_local()
    cards = [_card(article, None, None, now) for article in rows]
    # 搜索结果里要显示来源名与域名，所以再查一次 source
    cards = _attach_sources(session, rows, cards, now)
    return templates.TemplateResponse(
        request,
        "search.html",
        _ctx(
            request,
            q=keyword,
            scope=effective_scope,
            groups=group_cards(cards),
            total=len(cards),
            found=len(rows),
            counts=count_by_category(session, keyword, scope=effective_scope) if keyword else {},
            active_cat=cat or "",
            active_tag=tag or "",
            searched_at=now,
            title=f'搜索 "{keyword}"' if keyword else "搜索",
            **search_metadata(),
        ),
    )


def _attach_sources(
    session: Session, rows: list[Article], cards: list[dict[str, Any]], now: datetime
) -> list[dict[str, Any]]:
    """给搜索结果补上来源名与域名（一次查询搞定，不做 N+1）。"""
    if not rows:
        return cards
    ids = {a.source_id for a in rows if a.source_id is not None}
    names: dict[int, tuple[str | None, str | None]] = {}
    if ids:
        names = {
            source_id: (name, url)
            for source_id, name, url in session.execute(
                select(Source.id, Source.name, Source.url).where(Source.id.in_(ids))
            )
        }
    for article, card in zip(rows, cards, strict=True):
        name, url = names.get(article.source_id, (None, None)) if article.source_id is not None else (None, None)
        card["source"] = name or "未知来源"
        card["source_host"] = _host(url)
    return cards


@page_router.get("/saved", response_class=HTMLResponse)
def saved_page(request: Request) -> HTMLResponse:
    """收藏页：收藏存在浏览器 localStorage，这里只提供一个空壳页面。"""
    return templates.TemplateResponse(request, "saved.html", _ctx(request, title="我的收藏"))


@page_router.get("/daily/{date}", response_class=HTMLResponse)
def daily(
    request: Request,
    date: str,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    report = session.execute(select(DailyReport).where(DailyReport.date == date)).scalar_one_or_none()
    now = now_local()
    cards = articles_of_day(session, date, now)
    return templates.TemplateResponse(
        request,
        "daily.html",
        _ctx(
            request,
            report=report,
            articles=cards,
            groups=group_by_date(cards, date),
            date=date,
            title=f"{date} 日报",
        ),
        status_code=200 if report else 404,
    )


@page_router.get("/story/{article_id}", response_class=HTMLResponse)
def story(request: Request, article_id: int, session: Session = Depends(get_session)) -> HTMLResponse:
    """单篇页内预览：左栏来源信息 + 右侧完整正文，读者不用跳原站。"""
    row = session.execute(
        select(Article, Source.name, Source.url)
        .outerjoin(Source, Article.source_id == Source.id)
        .where(Article.id == article_id)
    ).first()
    if row is None:
        return templates.TemplateResponse(
            request, "story.html", _ctx(request, item=None, title="内容不存在"), status_code=404
        )
    article, source_name, source_url = row
    now = now_local()
    item = _card(article, source_name, source_url, now)
    # 同分类 / 同标签的邻居，方便顺着标签继续读
    item["related"] = _related(session, article, now)
    # 正文按段落切开，并标出哪些段落是「小标题」，供页面渲染目录与 <h3>
    marked = mark_headings(_body_blocks(article.content_full))
    item["body_blocks"] = marked
    item["toc"] = table_of_contents(marked)
    item["content_preview"] = truncate(article.content_full or article.content or "", 400)
    return templates.TemplateResponse(request, "story.html", _ctx(request, item=item, title=article.title[:40]))


def _related(session: Session, article: Article, now: datetime, *, limit: int = 6) -> list[dict[str, Any]]:
    """找同类文章：优先同分类，其次同标签。"""
    if not article.category and not article.tags:
        return []
    needle_tags = {t.strip().casefold() for t in (article.tags or "").split(",") if t.strip()}
    rows = list(
        session.execute(
            select(Article, Source.name, Source.url)
            .outerjoin(Source, Article.source_id == Source.id)
            .where(
                Article.id != article.id,
                Article.relevance == 1,
                Article.status.in_(STATUS_REPORTABLE),
            )
            .order_by(Article.published_at.desc(), Article.id.desc())
            .limit(120)
        )
    )
    scored: list[tuple[int, dict[str, Any]]] = []
    for candidate, name, url in rows:
        points = 0
        if article.category and candidate.category == article.category:
            points += 2
        points += len({t.strip().casefold() for t in (candidate.tags or "").split(",")} & needle_tags)
        if points:
            scored.append((points, _card(candidate, name, url, now)))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [card for _, card in scored[:limit]]


@page_router.get("/archive", response_class=HTMLResponse)
def archive(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    """历史日报：按日期倒序列出，点日期展开当天标题（内容按需再拉，不一次性塞满页面）。"""
    reports = list(session.execute(select(DailyReport).order_by(DailyReport.date.desc()).limit(120)).scalars())
    now = now_local()
    rows = []
    peak = max((row.article_count for row in reports), default=0) or 1
    for row in reports:
        label, weekday = _date_label(row.date)
        rows.append(
            {
                "date": row.date,
                "label": label,
                "weekday": weekday,
                "count": row.article_count,
                # 用条形长度直观对比哪天抓得多
                "width": max(6, round(row.article_count / peak * 100)),
                "relative": _relative(
                    datetime.strptime(row.date, "%Y-%m-%d").replace(hour=12), now
                ),
                "href": f"/daily/{row.date}",
            }
        )
    return templates.TemplateResponse(
        request,
        "archive.html",
        _ctx(request, reports=rows, total=len(rows), title="历史日报"),
    )