"""RSS 输出：把某一天的日报做成可订阅的 RSS 2.0（每篇文章一个条目）。"""

from __future__ import annotations

from datetime import datetime
from email.utils import format_datetime
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Article, DailyReport
from app.report.generator import STATUS_REPORTABLE, day_window
from app.utils.text import LOCAL_TZ, now_local, split_tags, strip_markdown

rss_router = APIRouter()

MAX_DESCRIPTION_CHARS = 600


def _rfc822(value: datetime) -> str:
    return format_datetime(value.replace(tzinfo=LOCAL_TZ))


def _item(title: str, link: str, description: str, pub_date: str) -> str:
    return (
        "<item>"
        f"<title>{escape(title)}</title>"
        f"<link>{escape(link)}</link>"
        f"<guid isPermaLink=\"false\">{escape(link)}</guid>"
        f"<description>{escape(description[:MAX_DESCRIPTION_CHARS])}</description>"
        f"<pubDate>{escape(pub_date)}</pubDate>"
        "</item>"
    )


def _day_items(session: Session, date_str: str) -> list[Article]:
    """按时间倒序取当天进日报的文章（口径与日报一致）。"""
    try:
        start, end = day_window(date_str)
    except ValueError:
        return []
    statement = (
        select(Article)
        .where(
            Article.relevance == 1,
            Article.status.in_(STATUS_REPORTABLE),
            Article.published_at >= start,
            Article.published_at < end,
        )
        .order_by(Article.published_at.desc(), Article.id.desc())
    )
    return list(session.execute(statement).scalars())


@rss_router.get("/rss")
def rss(request: Request, date: str | None = None, session: Session = Depends(get_session)) -> Response:
    """默认输出最新一份日报；``?date=YYYY-MM-DD`` 指定某一天。

    条目粒度是「文章」而不是「整份日报」—— 早先把整份日报塞进一个 item 并截断到
    1000 字符，订阅端实际只能看到开头几行。
    """
    if date:
        statement = select(DailyReport).where(DailyReport.date == date)
    else:
        statement = select(DailyReport).order_by(DailyReport.date.desc())
    report = session.execute(statement.limit(1)).scalar_one_or_none()
    if report is None:
        raise HTTPException(status_code=404, detail="还没有日报")

    base = str(request.base_url).rstrip("/")
    updated = report.created_at or now_local()
    articles = _day_items(session, report.date)

    parts = []
    for article in articles:
        tags = split_tags(article.tags)
        label = f"{article.title}（{'、'.join(tags) or '无标签'}）"
        summary = strip_markdown(article.summary)
        if article.status == "failed":
            summary += "（降级摘要）"
        parts.append(
            _item(
                label,
                article.link,
                summary,
                _rfc822(article.published_at or updated),
            )
        )
    if not parts:  # 当天没有文章时，至少给出日报本身，别让订阅端空着
        parts.append(_item(f"日报 · {report.date}", f"{base}/daily/{report.date}", report.content_md, _rfc822(updated)))

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel>'
        "<title>Show Me the Money 日报</title>"
        f"<link>{escape(base)}</link>"
        "<description>按调研方向自动筛选的行业热点日报</description>"
        f"<lastBuildDate>{escape(_rfc822(updated))}</lastBuildDate>"
        + "".join(parts)
        + "</channel></rss>"
    )
    return Response(content=xml, media_type="application/rss+xml; charset=utf-8")