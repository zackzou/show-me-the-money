"""日报生成：汇总当天相关文章 → Markdown + HTML → 落库（按日期覆盖）。"""

from __future__ import annotations

from typing import Any

from jinja2 import Template
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import session_scope
from app.models import Article, DailyReport
from app.utils.logger import get_logger
from app.utils.text import now_local, split_tags

log = get_logger(__name__)

REPORT_HTML_TEMPLATE = Template(
    """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>SMTM 日报 · {{ date }}</title></head>
<body>
<h1>Show Me the Money 日报 · {{ date }}</h1>
<p>调研方向：{{ topic }}</p>
{% for item in items %}
<section>
  <h2>{{ loop.index }}. {{ item.title }}</h2>
  <ul>
    <li>来源：{{ item.source }}</li>
    <li>摘要：{{ item.summary }}</li>
    <li>标签：{{ item.tags }}</li>
    <li>链接：<a href="{{ item.link }}">{{ item.link }}</a></li>
  </ul>
</section>
{% endfor %}
{% if not items %}<p>本日没有与调研方向相关的文章。</p>{% endif %}
</body></html>
"""
)


def _report_items(session: Session, date_str: str) -> list[dict[str, str]]:
    statement = (
        select(Article)
        .where(
            Article.relevance == 1,
            Article.status == "processed",
            func.date(Article.published_at) == date_str,
        )
        .order_by(Article.published_at.desc(), Article.id.desc())
    )
    items: list[dict[str, str]] = []
    for article in session.execute(statement).scalars():
        source_name = article.source.name if article.source is not None else "未知来源"
        items.append(
            {
                "title": article.title,
                "source": source_name,
                "summary": article.summary or "（无摘要）",
                "tags": "、".join(split_tags(article.tags)) or "（无标签）",
                "link": article.link,
                "published_at": article.published_at.strftime("%Y-%m-%d %H:%M") if article.published_at else "",
            }
        )
    return items


def render_markdown(date_str: str, topic: str, items: list[dict[str, str]]) -> str:
    lines = [f"# Show Me the Money 日报 · {date_str}", f"> 调研方向：{topic}"]
    if not items:
        lines.append("")
        lines.append("本日没有与调研方向相关的文章。")
        return "\n".join(lines)
    for index, item in enumerate(items, start=1):
        lines.extend(
            [
                "",
                f"## {index}. {item['title']}",
                f"- 来源：{item['source']}",
                f"- 摘要：{item['summary']}",
                f"- 标签：{item['tags']}",
                f"- 链接：{item['link']}",
            ]
        )
    return "\n".join(lines)


def generate_daily_report(
    date: str | None = None,
    *,
    session: Session | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """生成指定日期（默认今天）的日报，返回 ``{date, article_count, content_md, content_html}``。"""
    date_str = date or now_local().strftime("%Y-%m-%d")
    topic = settings.research_topic if settings is not None else ""

    def _build(session: Session) -> dict[str, Any]:
        items = _report_items(session, date_str)
        markdown = render_markdown(date_str, topic, items)
        html = REPORT_HTML_TEMPLATE.render(date=date_str, topic=topic, items=items)

        session.execute(delete(DailyReport).where(DailyReport.date == date_str))
        session.add(
            DailyReport(
                date=date_str,
                content_md=markdown,
                content_html=html,
                article_count=len(items),
                created_at=now_local(),
            )
        )
        session.flush()
        log.info("日报已生成：%s（%d 篇）", date_str, len(items))
        return {"date": date_str, "article_count": len(items), "content_md": markdown, "content_html": html}

    if session is not None:
        return _build(session)
    with session_scope() as scoped:
        return _build(scoped)
