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
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.cluster import duplicates_of, primary_of
from app.config import Settings
from app.db import get_session
from app.fetcher.content import is_real_body
from app.models import Article, DailyReport, Source
from app.report.generator import STATUS_REPORTABLE, day_window
from app.utils.text import is_chinese_text, now_local, split_tags, strip_markdown, truncate
from app.utils.text import looks_english as is_english
from app.web.search import (
    SCOPE_LABELS,
    SCOPE_META,
    count_by_category,
    normalize_scope,
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


def _body_image_anchors(raw: str | None) -> list[tuple[int, str]]:
    """读 body_images：返回 ``[(接在第几段之后, 地址), ...]``。坏数据一律当没有。"""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    out: list[tuple[int, str]] = []
    for item in parsed if isinstance(parsed, list) else []:
        if not isinstance(item, dict):
            continue
        index, url = item.get("i"), item.get("url")
        if isinstance(index, int) and isinstance(url, str) and url.startswith(("http://", "https://")):
            out.append((index, url))
    return out


def _image_slots(anchors: list[tuple[int, str]], block_count: int) -> list[list[str]]:
    """把锚点摊成按段落序号取用的列表，长度是 ``block_count + 1``。

    第 0 项是正文第一段之前的图（头图），第 n 项是第 n 段之后的图 ——
    模板里 ``slots[loop.index]`` 就能直接取，不用在 Jinja 里算位置。
    """
    slots: list[list[str]] = [[] for _ in range(block_count + 1)]
    for index, url in anchors:
        if 0 <= index <= block_count:
            slots[index].append(url)
    return slots


def _shift_image_slots(slots: list[list[str]], source_blocks: int, target_blocks: int) -> list[list[str]]:
    """把配图位置按比例挪到译文对应的段落上。

    译文是分段翻的，失败的那几段会被丢掉，所以译文段数与原文对不上，
    段号不能直接搬。图片跟着它在全文里的相对位置走，误差只在一两段之内，
    总比所有图堆在开头或结尾好。
    """
    flat = [(index, url) for index, group in enumerate(slots) for url in group]
    if source_blocks <= 0 or target_blocks <= 0:
        return _image_slots([], target_blocks)
    if source_blocks == target_blocks:
        return _image_slots(flat, target_blocks)
    shifted = [(min(target_blocks, round(index / source_blocks * target_blocks)), url) for index, url in flat]
    return _image_slots(shifted, target_blocks)


# 像小标题的段落：短、不以句末标点结尾、不是纯数字
_HEADING_MAX_CHARS = 30
_HEADING_MIN_CHARS = 2
_SENTENCE_END = "。！？；，、：,.!?;:…"


def _body_blocks(raw: str | None) -> list[str]:
    """正文全文按空行切段，供详情页逐段渲染。"""
    if not raw:
        return []
    return [part.strip() for part in raw.split("\n\n") if part.strip()]


def mark_headings(blocks: list[str], *, prefix: str = "sec") -> list[dict[str, Any]]:
    """把正文里「短句、不以标点结尾」的段落标成小标题。

    这样详情页能像图1 那样给一个本文目录，并把小标题渲染成 ``<h3>``；
    剩下的当普通段落。判不准就当普通段落，不会漏内容。

    ``prefix`` 用来给锚点 id 分命名空间：详情页会同时把原文与译文渲染进
    DOM（靠 CSS 按语言隐藏其一），两边都从 0 开始编号就会撞 id，
    目录会跳到另一版的位置去。
    """
    marked: list[dict[str, Any]] = []
    for index, text in enumerate(blocks):
        stripped = text.strip()
        is_heading = (
            _HEADING_MIN_CHARS <= len(stripped) <= _HEADING_MAX_CHARS
            and stripped[-1] not in _SENTENCE_END
            and not stripped[0].isdigit()
        )
        marked.append({"i": index, "id": f"{prefix}-{index}", "text": stripped, "heading": is_heading})
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
            Article.duplicate_of.is_(None),
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


def _reason_of(article: Article, digest_zh: str, digest: str) -> str:
    """推荐理由；和导语一字不差就当没有。

    LLM 不可用时摘要与推荐理由都退到同一段兜底文本，卡片上并排显示两遍一模一样的
    话看起来像坏了。理由本来就是「为什么要点开这条」，复读一遍导语没有意义。
    """
    reason = strip_markdown(article.reason)
    if not reason:
        return ""
    lead = digest_zh.strip() or digest.strip()
    return "" if lead and reason.strip() == lead else reason


def _card(article: Article, source_name: str | None, source_url: str | None, now: datetime) -> dict[str, Any]:
    published = article.published_at
    digest_zh = strip_markdown(article.digest_zh)
    digest = strip_markdown(article.digest_zh or article.digest)
    return {
        "id": article.id,
        # 中文标题优先：英文信源译过来的标题读起来才像中文
        # 中文标题优先：英文信源译过来的标题读起来才像中文
        "title": article.title_zh or article.title,
        "title_zh": article.title_zh or "",
        # title_en 缺失时退回原文：处理早期失败的文章压根没轮到写它，
        # 而模板里没有 en 就没有任何东西可显示 —— 切到英文会是一条空标题
        "title_en": article.title_en or ("" if is_chinese_text(article.title) else article.title),
        "link": article.link,
        "source": source_name or "未知来源",
        "source_host": _host(source_url),
        "time_hm": published.strftime("%H:%M") if published else "",
        "date_key": published.strftime("%Y-%m-%d") if published else "",
        "time_full": published.strftime("%Y-%m-%d %H:%M") if published else "",
        "relative": _relative(published, now),
        # 速览优先（页内就能读完），没有就退回摘要；中文模式读中文版导读
        "digest": digest or strip_markdown(article.summary),
        "digest_zh": digest_zh,
        "digest_en": strip_markdown(article.digest_en or article.digest),
        # 降级时摘要与推荐理由是同一段兜底文本，卡片上并排显示两遍一模一样的话
        "reason": _reason_of(article, digest_zh, digest),
        "score": article.score,
        "category": article.category or "",
        "topics": _topics_of(article),
        "tags": split_tags(article.tags),
        "images": _images(article.image_urls),
        # 「降级」按读者看到的样子判定：有英文原文、却还没中文版。
        # 不按 status 判 —— 重排队期间 status 是 pending，读者看到的还是英文，
        # 这时候把提示收掉等于假装没问题。
        "degraded": bool(article.status != "processed" and is_english(article.digest or article.summary or "")),
        "degraded_reason": article.degraded_reason or "",
        # 中文版还在排队（第几次尝试、什么时候试的）
        "pending_translation": bool(article.status == "failed" and not str(article.digest_zh or "").strip()),
        "attempts": article.process_attempts or 0,
    }


def articles_of_day(session: Session, date_str: str, now: datetime | None = None) -> list[dict[str, Any]]:
    """某一天进日报的全部文章（卡片数据）。非法日期当作「这一天没有内容」。"""
    now = now or now_local()
    try:
        rows = list(session.execute(_day_statement(date_str)))
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


def paginate(cards: list[dict[str, Any]], page: int, size: int = PAGE_SIZE) -> tuple[list[dict[str, Any]], int, int]:
    """先过滤后分页：返回 ``(当前页卡片, 总条数, 总页数)``。

    必须按这个顺序：分类 / 标签是在应用层过滤的（标签是逗号串，用 SQL LIKE
    会把「AI」误配到「AI Agent」）。先按数据库分页再过滤的话，总条数与页数
    算的是**过滤前**的数量 —— 翻到第 3 页可能一条都没有，而页码还显示有 5 页。
    """
    total = len(cards)
    pages = max(1, -(-total // size))
    page = max(1, min(page, pages))
    start = (page - 1) * size
    return cards[start : start + size], total, pages


def _settings(request: Request) -> Settings | None:
    return getattr(request.app.state, "settings", None)


def _topics(request: Request) -> list[str]:
    settings = _settings(request)
    return list(getattr(settings, "research_topics", []) or [])


def _categories(request: Request) -> list[dict[str, str]]:
    settings = _settings(request)
    return [{"name": c.name, "hint": c.hint} for c in getattr(settings, "categories", []) or []]


def _ctx(request: Request, **extra: Any) -> dict[str, Any]:
    return {
        "topics": _topics(request),
        "categories": _categories(request),
        # 导航高亮标记；不传就一个都不亮（base.html 里逐个比 nav 值）
        "nav": "",
        **extra,
    }


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
    # 先过滤再分页：过滤是在应用层做的（见 paginate 的注释），
    # 反过来的话总条数与页数算的是过滤前的数量，翻页会翻出空页。
    cards = filter_by(articles_of_day(session, date_str, now), category=cat, tag=tag)
    cards, total, pages = paginate(cards, page, PAGE_SIZE)
    return templates.TemplateResponse(
        request,
        "index.html",
        _ctx(
            request,
            nav="home",
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
    """站内搜索。``scope=full`` 时连正文一起搜。"""
    keyword = (q or "").strip()
    effective_scope = normalize_scope(scope)
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
            nav="home",
            q=keyword,
            scope=effective_scope,
            scope_label=SCOPE_LABELS.get(effective_scope, ""),
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
    return templates.TemplateResponse(request, "saved.html", _ctx(request, nav="saved", title="我的收藏"))


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
            nav="archive",
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
    # is_real_body 会把 Reddit 那种只剩模板套话的 description 判成「没有正文」，
    # 免得详情页渲染出一个只写着 Comments 的空区块，看着像抓取坏了。
    def _body(*candidates: str | None) -> str:
        return next((c for c in candidates if c and is_real_body(c)), "") or ""

    original = _body(article.content_full, article.content)
    marked = mark_headings(_body_blocks(original), prefix="sec-en")
    item["body_blocks"] = marked
    item["content_preview"] = truncate(original, 400)
    # 正文内联配图：按原站的做法插在段落之间，位置随抓取时一起存下来
    anchors = _body_image_anchors(article.body_images)
    item["shots"] = _image_slots(anchors, len(marked))
    item["has_body_images"] = bool(anchors)
    # 英文原文另有中文版：中文/双语模式读译文，英文模式读原文
    translated = article.content_zh or ""
    item["has_translation"] = bool(translated.strip())
    marked_zh = mark_headings(_body_blocks(translated), prefix="sec-zh") if item["has_translation"] else []
    item["body_blocks_zh"] = marked_zh
    item["content_preview_zh"] = truncate(translated, 400) if item["has_translation"] else ""
    item["shots_zh"] = _shift_image_slots(item["shots"], len(marked), len(marked_zh))
    # 本文目录指向**读者当前看到的那一版**：有译文就指译文的小标题。
    # 译文的段落数可能与原文不同（翻译失败的那几段会被丢掉），
    # 拿原文的下标去点译文的标题会跳错位置甚至跳到不存在的锚点。
    item["toc"] = table_of_contents(marked_zh if item["has_translation"] else marked)
    # 没有译文时要说清楚，避免「中文模式却整页英文」看着像坏了
    item["body_is_foreign"] = bool(original.strip()) and not item["has_translation"] and is_english(original)
    item["body_missing"] = not original.strip()
    # 导读同理：英文原文还没有中文导读时，页面上说清楚，别让「中文」模式看着像坏了
    item["digest_missing_zh"] = (
        is_english(article.digest or "") and not str(article.digest_zh or "").strip()
    )
    # 同题合并：这条是重复稿就指回主条目；是主条目就把别家的同题报道列出来，
    # 合并只是不重复展示，不是把内容删掉
    primary = primary_of(session, article)
    item["duplicate_of_id"] = primary.id if primary else 0
    item["duplicate_of_title"] = (primary.title_zh or primary.title) if primary else ""
    item["duplicate_of_source"] = _source_name(session, primary.source_id) if primary else ""
    item["also_reported"] = [
        {"id": row.id, "source": _source_name(session, row.source_id), "title": row.title_zh or row.title}
        for row in duplicates_of(session, article.id)
    ]
    return templates.TemplateResponse(
        request, "story.html", _ctx(request, nav="home", item=item, title=article.title[:40])
    )


def _source_name(session: Session, source_id: int | None) -> str:
    if source_id is None:
        return "未知来源"
    return session.execute(select(Source.name).where(Source.id == source_id)).scalar() or "未知来源"


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
            Article.duplicate_of.is_(None),
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
        _ctx(request, nav="archive", reports=rows, total=len(rows), title="历史日报"),
    )