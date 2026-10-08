"""报告页（/reports）：把库里的文章数据变成可读的趋势与分布。

设计取向：
- **纯服务端算数，不引图表库**。条形图 / 环形图 / 热力条都是 CSS 画的
  （宽度百分比、conic-gradient、方块矩阵），跟着页面主题走、深浅色自动适配，
  不加载任何 JS 依赖。数据量小（几十到几千行）时后端 SQL 聚合最快。
- **每个数字都可下钻**。类别 / 来源 / 标签 / 日期各维度都能点进
  ``/reports/breakdown`` 看明细列表（"上帝视角"），明细里再点进文章。
- 时间范围：``days`` 参数（7/14/30/90 天），默认 30。

数据全部实时聚合（不建物化表）：SQLite 对这种「按日期/分类 group by」
在几千行规模上是毫秒级，建汇总表反而带来一致性负担。
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Article, DailyReport, KeywordRule, Source
from app.report.generator import STATUS_REPORTABLE
from app.utils.logger import get_logger
from app.utils.text import now_local, split_tags
from app.web.routes import _ctx, templates

log = get_logger(__name__)

reports_router = APIRouter()

RANGE_CHOICES = (7, 14, 30, 90)


def _range_days(raw: object) -> int:
    """把外部传入的天数归一成合法档位（认不出来就 30）。"""
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        return 30
    return value if value in RANGE_CHOICES else 30


def _visible() -> tuple[Any, ...]:
    return (
        Article.relevance == 1,
        Article.status.in_(STATUS_REPORTABLE),
        Article.duplicate_of.is_(None),
        Article.deleted_at.is_(None),
    )


def collect_report(session: Session, days: int) -> dict[str, Any]:
    """聚合最近 ``days`` 天的数据，供模板渲染。"""
    now = now_local()
    start = now - timedelta(days=days)
    rows = list(
        session.execute(
            select(Article).where(*_visible(), Article.published_at >= start)
        ).scalars()
    )

    # ── 概览 KPI ─────────────────────────────────────────────
    total = len(rows)
    starred = sum(1 for a in rows if a.starred)
    relevant_days = {a.published_at.date() for a in rows if a.published_at}
    # 处理完成率：processed 占全部（失败降级也算可展示，但报告里分开看）
    processed = sum(1 for a in rows if a.status == "processed")
    translated = sum(
        1
        for a in rows
        if (a.content_zh or "").strip() or not _is_foreign(a)
    )
    scores = [a.score for a in rows if a.score is not None]
    avg_score = round(sum(scores) / len(scores), 1) if scores else 0

    # ── 按天序列（趋势图） ───────────────────────────────────
    by_day: dict[str, dict[str, int]] = {}
    for offset in range(days):
        day = (now - timedelta(days=days - 1 - offset)).strftime("%Y-%m-%d")
        by_day[day] = {"total": 0, "starred": 0}
    for a in rows:
        if not a.published_at:
            continue
        key = a.published_at.strftime("%Y-%m-%d")
        if key not in by_day:
            continue
        by_day[key]["total"] += 1
        if a.starred:
            by_day[key]["starred"] += 1
    series: list[dict[str, Any]] = [
        {"date": key, "label": key[5:], "total": val["total"], "starred": val["starred"]}
        for key, val in by_day.items()
    ]
    peak = max((int(point["total"]) for point in series), default=0)
    for point in series:
        # 条形高度百分比：模板直接当 style 宽度/高度用
        point["pct"] = round(int(point["total"]) / peak * 100, 1) if peak else 0

    # ── 分类分布 ─────────────────────────────────────────────
    cat_counter = Counter(a.category for a in rows if a.category)
    categories = [
        {"name": name, "count": count, "pct": round(count / total * 100, 1) if total else 0}
        for name, count in cat_counter.most_common()
    ]

    # ── 来源分布（前 12） ────────────────────────────────────
    source_names: dict[int, str] = {
        int(sid): str(name) for sid, name in session.execute(select(Source.id, Source.name)).all()
    }
    src_counter: Counter[str] = Counter()
    for a in rows:
        src_counter[source_names.get(a.source_id or 0, "未知来源")] += 1
    sources = [
        {"name": name, "count": count, "pct": round(count / total * 100, 1) if total else 0}
        for name, count in src_counter.most_common(12)
    ]

    # ── 标签 Top 20 ──────────────────────────────────────────
    tag_counter: Counter[str] = Counter()
    for a in rows:
        for tag in split_tags(a.tags):
            tag_counter[tag] += 1
    tags = [
        {"name": name, "count": count}
        for name, count in tag_counter.most_common(20)
    ]

    # ── 按小时分布（发布时段热力） ───────────────────────────
    hour_counter = Counter(a.published_at.hour for a in rows if a.published_at)
    peak_hour = max(hour_counter.values(), default=0)
    hours = [
        {
            "hour": hour,
            "count": hour_counter.get(hour, 0),
            "pct": round(hour_counter.get(hour, 0) / peak_hour * 100) if peak_hour else 0,
        }
        for hour in range(24)
    ]

    # ── 评分分布（5 档） ─────────────────────────────────────
    buckets = [("85+", 85, 101), ("70-84", 70, 85), ("55-69", 55, 70), ("40-54", 40, 55), ("<40", 0, 40)]
    score_dist = []
    for label, low, high in buckets:
        count = sum(1 for s in scores if low <= s < high)
        score_dist.append(
            {
                "label": label,
                "count": count,
                "pct": round(count / len(scores) * 100, 1) if scores else 0,
            }
        )

    # ── 关注命中（Top 规则） ─────────────────────────────────
    star_rules = list(
        session.execute(
            select(KeywordRule)
            .where(KeywordRule.kind == "star", KeywordRule.enabled == 1)
            .order_by(KeywordRule.hits.desc())
            .limit(8)
        ).scalars()
    )
    watch = [
        {"keyword": row.keyword, "hits": row.hits} for row in star_rules
    ]

    # ── 最近日报（定稿情况） ─────────────────────────────────
    reports = list(
        session.execute(
            select(DailyReport)
            .where(DailyReport.date >= start.strftime("%Y-%m-%d"))
            .order_by(DailyReport.date.desc())
        ).scalars()
    )
    report_rows = [
        {"date": row.date, "count": row.article_count} for row in reports
    ]

    return {
        "days": days,
        "range_choices": list(RANGE_CHOICES),
        "generated_at": now,
        "kpi": {
            "total": total,
            "starred": starred,
            "days": len(relevant_days),
            "avg_per_day": round(total / days, 1) if days else 0,
            "processed": processed,
            "processed_pct": round(processed / total * 100, 1) if total else 0,
            "translated_pct": round(translated / total * 100, 1) if total else 0,
            "avg_score": avg_score,
        },
        "series": series,
        "categories": categories,
        "sources": sources,
        "tags": tags,
        "hours": hours,
        "score_dist": score_dist,
        "watch": watch,
        "reports": report_rows,
    }


def _is_foreign(article: Article) -> bool:
    from app.utils.text import looks_english

    return looks_english(article.content_full or article.content or "")


@reports_router.get("/reports", response_class=HTMLResponse)
def reports_page(
    request: Request,
    days: str = "30",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """报告页：趋势、分布、关注命中，全部可下钻。"""
    resolved = _range_days(days)
    data = collect_report(session, resolved)
    return templates.TemplateResponse(
        request,
        "reports.html",
        _ctx(request, nav="reports", title="数据报告", **data),
    )


# ── 下钻（breakdown） ────────────────────────────────────────


def _breakdown_rows(session: Session, days: int, dimension: str, value: str) -> list[dict[str, Any]]:
    """某个维度值下的文章明细（按时间倒序）。"""
    now = now_local()
    start = now - timedelta(days=days)
    statement = select(Article).where(*_visible(), Article.published_at >= start)
    rows = list(session.execute(statement).scalars())

    if dimension == "category":
        rows = [a for a in rows if (a.category or "") == value]
    elif dimension == "tag":
        rows = [a for a in rows if value in split_tags(a.tags)]
    elif dimension == "starred":
        rows = [a for a in rows if a.starred]
    elif dimension == "hour":
        rows = [a for a in rows if a.published_at and str(a.published_at.hour) == value]
    elif dimension == "score":
        # 值形如 "85+" / "70-84" / "<40"，与报告页的档位标签一一对应
        if value.endswith("+"):
            low_i, high_i = int(value[:-1]), 101
        elif value.startswith("<"):
            low_i, high_i = 0, int(value[1:])
        else:
            low, _, high = value.partition("-")
            try:
                low_i, high_i = int(low), (int(high) if high else 101)
            except ValueError:
                low_i, high_i = 0, 101
        rows = [a for a in rows if a.score is not None and low_i <= a.score < high_i]
    elif dimension == "source":
        names: dict[int, str] = {
            int(sid): str(name)
            for sid, name in session.execute(select(Source.id, Source.name)).all()
        }
        rows = [a for a in rows if names.get(a.source_id or 0, "未知来源") == value]
    elif dimension == "date":
        rows = [a for a in rows if a.published_at and a.published_at.strftime("%Y-%m-%d") == value]
    # dimension == "all"：不过滤

    return [
        {
            "id": a.id,
            "title": a.title_zh or a.title,
            "source": None,
            "published": a.published_at.strftime("%Y-%m-%d %H:%M") if a.published_at else "",
            "category": a.category or "",
            "score": a.score,
            "starred": bool(a.starred),
            "tags": split_tags(a.tags),
        }
        for a in rows
    ]


@reports_router.get("/reports/breakdown", response_class=HTMLResponse)
def reports_breakdown(
    request: Request,
    dimension: str = "all",
    value: str = "",
    days: str = "30",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """下钻页：从图表点进来的明细列表。"""
    resolved = _range_days(days)
    rows = _breakdown_rows(session, resolved, dimension, value)
    # 补来源名（一次查完，不做 N+1）
    source_ids = {r["id"] for r in rows}
    names: dict[int, str] = {}
    if source_ids:
        for article_id, source_name in session.execute(
            select(Article.id, Source.name)
            .outerjoin(Source, Article.source_id == Source.id)
            .where(Article.id.in_(source_ids))
        ).all():
            names[article_id] = source_name or "未知来源"
    for row in rows:
        row["source"] = names.get(row["id"], "未知来源")

    labels = {
        "category": "分类",
        "tag": "标签",
        "starred": "特别关注",
        "hour": "时段",
        "score": "评分档",
        "source": "来源",
        "date": "日期",
        "all": "全部",
    }
    return templates.TemplateResponse(
        request,
        "breakdown.html",
        _ctx(
            request,
            nav="reports",
            title="明细",
            dimension=dimension,
            dimension_label=labels.get(dimension, dimension),
            value=value,
            days=resolved,
            rows=rows,
            total=len(rows),
        ),
    )
