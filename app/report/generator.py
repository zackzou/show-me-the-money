"""日报生成：汇总某一天的文章 → Markdown + HTML → 落库（按日期覆盖）。

时间窗口约定（见 ``day_window``）：一份日报只覆盖**一个自然日**，默认取整天；
传 ``until`` 则只取到该时刻——用来做「今天实时」这种滚动日报。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from jinja2 import Template
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import session_scope
from app.models import Article, DailyReport
from app.utils.logger import get_logger
from app.utils.text import now_local, split_tags, strip_markdown

log = get_logger(__name__)

# 进日报的状态：processed 是 LLM 处理成功，failed 是 LLM 不可用时的降级摘要。
# 降级文章同样要出现在日报里，否则「LLM 挂了不影响日报产出」就是空话。
STATUS_REPORTABLE = ("processed", "failed")

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
    <li>摘要：{{ item.summary }}{% if item.degraded %}（降级：LLM 不可用，取正文开头）{% endif %}</li>
    <li>标签：{{ item.tags }}</li>
    <li>链接：<a href="{{ item.link }}">{{ item.link }}</a></li>
  </ul>
</section>
{% endfor %}
{% if not items %}<p>本日没有与调研方向相关的文章。</p>{% endif %}
</body></html>
""",
    autoescape=True,
)


def day_window(date_str: str, *, until: datetime | None = None) -> tuple[datetime, datetime]:
    """某一天的时间窗 ``[start, end)``（北京时间 naive）。

    ``until`` 非空时把上界截断到该时刻，用于「今天实时」这种滚动日报。
    用 ``>=`` / ``<`` 比较而不是 ``func.date()``，顺便让 published_at 上的索引能用上。
    """
    start = datetime.strptime(date_str, "%Y-%m-%d")
    end = start + timedelta(days=1)
    return start, (until if until is not None and until < end else end)


def _report_items(session: Session, date_str: str, *, until: datetime | None = None) -> list[dict[str, Any]]:
    start, end = day_window(date_str, until=until)
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
    items: list[dict[str, Any]] = []
    for article in session.execute(statement).scalars():
        source_name = article.source.name if article.source is not None else "未知来源"
        degraded = article.status == "failed"
        items.append(
            {
                "title": article.title,
                "source": source_name,
                "summary": strip_markdown(article.summary) or "（无摘要）",
                "tags": "、".join(split_tags(article.tags)) or "（无标签）",
                "link": article.link,
                "published_at": article.published_at.strftime("%Y-%m-%d %H:%M") if article.published_at else "",
                "degraded": degraded,
            }
        )
    return items


def render_markdown(date_str: str, topic: str, items: list[dict[str, Any]]) -> str:
    lines = [f"# Show Me the Money 日报 · {date_str}", f"> 调研方向：{topic}"]
    if not items:
        lines.append("")
        lines.append("本日没有与调研方向相关的文章。")
        return "\n".join(lines)
    for index, item in enumerate(items, start=1):
        summary = str(item["summary"])
        if item.get("degraded"):
            summary += "（降级：LLM 不可用，取正文开头）"
        lines.extend(
            [
                "",
                f"## {index}. {item['title']}",
                f"- 来源：{item['source']}",
                f"- 摘要：{summary}",
                f"- 标签：{item['tags']}",
                f"- 链接：{item['link']}",
            ]
        )
    return "\n".join(lines)


def generate_daily_report(
    date: str | None = None,
    *,
    until: datetime | None = None,
    session: Session | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """生成指定日期（默认今天）的日报，返回 ``{date, article_count, content_md, content_html}``。

    ``until`` 截断时间窗上界：``None`` 出整天，传当前时刻则出「今天实时」。
    同一天重复调用会覆盖旧结果，所以滚动刷新是安全的。
    """
    date_str = date or now_local().strftime("%Y-%m-%d")
    topic = settings.research_topic if settings is not None else ""

    def _build(session: Session) -> dict[str, Any]:
        items = _report_items(session, date_str, until=until)
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
        degraded_count = sum(1 for item in items if item["degraded"])
        suffix = f"，含 {degraded_count} 条降级摘要" if degraded_count else ""
        log.info("日报已生成：%s（%d 篇%s）", date_str, len(items), suffix)
        return {
            "date": date_str,
            "article_count": len(items),
            "degraded_count": degraded_count,
            "content_md": markdown,
            "content_html": html,
        }

    if session is not None:
        return _build(session)
    with session_scope() as scoped:
        return _build(scoped)
