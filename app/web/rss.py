"""RSS 输出：把最新（或指定日期）日报做成可订阅的 RSS 2.0。"""

from __future__ import annotations

from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import DailyReport

rss_router = APIRouter()


def _item(title: str, link: str, description: str, pub_date: str) -> str:
    return (
        "<item>"
        f"<title>{escape(title)}</title>"
        f"<link>{escape(link)}</link>"
        f"<guid isPermaLink=\"false\">{escape(link)}</guid>"
        f"<description>{escape(description[:1000])}</description>"
        f"<pubDate>{escape(pub_date)}</pubDate>"
        "</item>"
    )


@rss_router.get("/rss")
def rss(request: Request, date: str | None = None, session: Session = Depends(get_session)) -> Response:
    statement = select(DailyReport).order_by(DailyReport.date.desc())
    if date:
        statement = select(DailyReport).where(DailyReport.date == date)
    report = session.execute(statement.limit(1)).scalar_one_or_none()
    if report is None:
        raise HTTPException(status_code=404, detail="还没有日报")

    base = str(request.base_url).rstrip("/")
    published = report.created_at.strftime("%a, %d %b %Y %H:%M:%S +0800")
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel>'
        "<title>Show Me the Money 日报</title>"
        f"<link>{escape(base)}</link>"
        "<description>按调研方向自动筛选的行业热点日报</description>"
        f"<lastBuildDate>{escape(published)}</lastBuildDate>"
        + _item(f"日报 · {report.date}", f"{base}/daily/{report.date}", report.content_md, published)
        + "</channel></rss>"
    )
    return Response(content=xml, media_type="application/rss+xml; charset=utf-8")
