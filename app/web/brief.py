"""早报：网页配置页 + 成品存档 + 文本/Markdown 输出接口。

- ``/brief``：配置页（TOP N、时间范围、分节=多张长图、分类/主题/标签/
  关键词/排除词、排序、只看特别关注），右侧实时预览；下方是历史成品；
- ``/brief.txt`` / ``/brief.md``：纯文本 / Markdown 输出；
- ``/api/brief``：JSON 输出（分节 + 条目 + 两种格式文本）；
- ``/api/brief/generate``：POST 立即生成一份并存档（「立即生成」按钮）。

配置存 ``brief_config`` 表，改完即时生效；成品存 ``brief_issues`` 表
（每天一份，早上 06:00 定时生成，配置页可回看每一天）。
内容继承文章处理阶段写好的早报片段（``Article.brief_zh``），拼装零 LLM 调用。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Article, BriefIssue, KeywordRule
from app.report.brief import (
    MAX_DAYS,
    MAX_SECTIONS,
    MAX_TOP_N,
    SORT_CHOICES,
    build_and_save_brief,
    build_brief,
    config_view,
    issue_view,
    load_brief_config,
    save_brief_config,
)
from app.utils.logger import get_logger
from app.utils.text import now_local, split_tags
from app.web.routes import _ctx, templates

log = get_logger(__name__)

brief_router = APIRouter()

# 历史成品在配置页一次列多少天
ISSUE_PAGE_SIZE = 14


def _facet_options(session: Session) -> dict[str, list[str]]:
    """给表单的候选项：分类（库里出现过的）、主题、标签（按出现频次前 30）。"""
    categories = [
        row[0]
        for row in session.execute(
            select(Article.category)
            .where(
                Article.category.isnot(None),
                Article.relevance == 1,
                Article.deleted_at.is_(None),
            )
            .group_by(Article.category)
            .order_by(func.count(Article.id).desc())
        ).all()
        if row[0]
    ]
    topics: set[str] = set()
    tags: dict[str, int] = {}
    for row in session.execute(
        select(Article.topics, Article.tags).where(
            Article.relevance == 1, Article.deleted_at.is_(None)
        )
    ).all():
        raw_topics, raw_tags = row
        if raw_topics:
            import json

            try:
                parsed = json.loads(raw_topics)
                if isinstance(parsed, list):
                    topics.update(str(t).strip() for t in parsed if str(t).strip())
            except (TypeError, ValueError):
                pass
        for tag in split_tags(raw_tags, limit=20):
            tags[tag] = tags.get(tag, 0) + 1
    return {
        "categories": categories,
        "topics": sorted(topics)[:60],
        "tags": [t for t, _ in sorted(tags.items(), key=lambda kv: -kv[1])[:30]],
    }


def _star_keywords(session: Session) -> list[str]:
    return [
        row.keyword
        for row in session.execute(
            select(KeywordRule)
            .where(KeywordRule.kind == "star", KeywordRule.enabled == 1)
            .order_by(KeywordRule.hits.desc())
            .limit(30)
        ).scalars()
    ]


def _issues(session: Session, *, limit: int = ISSUE_PAGE_SIZE) -> list[dict[str, Any]]:
    rows = session.execute(
        select(BriefIssue).order_by(BriefIssue.date.desc()).limit(limit)
    ).scalars()
    return [
        {
            "date": row.date,
            "section_count": row.section_count,
            "article_count": row.article_count,
            "created_at": row.created_at.strftime("%H:%M") if row.created_at else "",
        }
        for row in rows
    ]


def _page_context(request: Request, session: Session, **extra: Any) -> dict[str, Any]:
    row = load_brief_config(session)
    cfg = config_view(row)
    return _ctx(
        request,
        nav="brief",
        title="早报配置",
        cfg=cfg,
        preview=build_brief(session),
        options=_facet_options(session),
        star_keywords=_star_keywords(session),
        max_top_n=MAX_TOP_N,
        max_days=MAX_DAYS,
        max_sections=MAX_SECTIONS,
        sort_choices=SORT_CHOICES,
        issues=_issues(session),
        updated_at=row.updated_at.strftime("%Y-%m-%d %H:%M") if row.updated_at else "",
        **extra,
    )


@brief_router.get("/brief", response_class=HTMLResponse)
def brief_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    return templates.TemplateResponse(request, "brief.html", _page_context(request, session))


@brief_router.post("/brief", response_class=HTMLResponse)
async def brief_save(
    request: Request, session: Session = Depends(get_session)
) -> HTMLResponse:
    """保存配置并回到配置页（表单整页提交，与关键词页同一套交互）。

    多选字段（分类/主题/标签）会以同名多次出现；``parse_qs`` 收成 list，
    单值字段收成单元素 list —— 统一取 list 交给 ``save_brief_config``，
    它两种都认（字符串按逗号拆、列表直接存）。
    """
    raw = (await request.body()).decode("utf-8", "replace")
    parsed = parse_qs(raw, keep_blank_values=True)
    form: dict[str, Any] = {key: (values if len(values) > 1 else values[0])
                            for key, values in parsed.items() if values}
    save_brief_config(session, form)
    session.commit()
    log.info("早报配置已更新：top_n=%s days=%s sections=%s",
             form.get("top_n"), form.get("days"), len(config_view(load_brief_config(session))["sections"]))
    return templates.TemplateResponse(
        request,
        "brief.html",
        _page_context(
            request,
            session,
            notice={"kind": "ok", "text": "已保存 · 右侧预览已按新规则更新"},
        ),
    )


@brief_router.post("/api/brief/generate")
def brief_generate(session: Session = Depends(get_session)) -> JSONResponse:
    """立即生成一份早报并存档（「立即生成」按钮 / 脚本调用）。"""
    data, issue = build_and_save_brief(session)
    session.commit()
    log.info("手动生成早报：%s（%d 节 / %d 条）", issue.date, issue.section_count, issue.article_count)
    return JSONResponse(
        {
            "ok": True,
            "date": issue.date,
            "sections": issue.section_count,
            "total": data["total"],
            "text": data["text"],
        }
    )


@brief_router.get("/brief.txt")
def brief_txt(session: Session = Depends(get_session)) -> PlainTextResponse:
    """纯文本早报（手机推送 / 复制粘贴）。"""
    data = build_brief(session)
    return PlainTextResponse(data["text"], media_type="text/plain; charset=utf-8")


@brief_router.get("/brief.md")
def brief_md(session: Session = Depends(get_session)) -> PlainTextResponse:
    """Markdown 早报（Agent / 富文本场景）。"""
    data = build_brief(session)
    return PlainTextResponse(data["markdown"], media_type="text/markdown; charset=utf-8")


@brief_router.get("/api/brief")
def brief_api(
    date: str | None = None, session: Session = Depends(get_session)
) -> JSONResponse:
    """JSON 早报。

    - 不带 ``date``：实时按当前配置生成（不落库）；
    - ``?date=YYYY-MM-DD``：读当天**成品存档**（配置改过也不影响历史）。
    """
    if date:
        issue = session.execute(
            select(BriefIssue).where(BriefIssue.date == date)
        ).scalar_one_or_none()
        if issue is None:
            return JSONResponse({"ok": False, "detail": f"没有 {date} 的早报成品"}, status_code=404)
        view = issue_view(issue)
        return JSONResponse({"ok": True, "stored": True, **view})
    data = build_brief(session)
    return JSONResponse(
        {
            "generated_at": now_local().strftime("%Y-%m-%d %H:%M:%S"),
            "stored": False,
            "config": data["config"],
            "total": data["total"],
            "sections": data["sections"],
            "rows": data["rows"],
            "text": data["text"],
            "markdown": data["markdown"],
        }
    )


@brief_router.get("/api/brief/issue")
def brief_issue(date: str, session: Session = Depends(get_session)) -> JSONResponse:
    """某天的成品存档（配置页点开历史时用）。"""
    issue = session.execute(
        select(BriefIssue).where(BriefIssue.date == date)
    ).scalar_one_or_none()
    if issue is None:
        return JSONResponse({"ok": False, "detail": f"没有 {date} 的早报成品"}, status_code=404)
    return JSONResponse({"ok": True, **issue_view(issue)})


@brief_router.get("/api/brief/image")
def brief_image(
    section: int = 0,
    date: str | None = None,
    session: Session = Depends(get_session),
) -> Response:
    """早报节点长图（PNG，1080px 宽）。

    ``?section=N`` 取第 N 个节点（0 起）；``?date=`` 读成品存档（不传则实时）。
    Hermes / 微信 iLink 直接把这个 URL 当图片发（一次一条消息）。

    天气节点是**纯文本**（用户要求：早安问候 + 天气 + 穿衣建议直接发文字，
    不占一张图），请求它返回 404 并说明原因 —— 调用方据此只发文字。
    """
    from app.report.longimage import render_section_png

    if date:
        issue = session.execute(
            select(BriefIssue).where(BriefIssue.date == date)
        ).scalar_one_or_none()
        if issue is None:
            return PlainTextResponse(f"没有 {date} 的早报成品", status_code=404)
        view = issue_view(issue)
        sections = view["sections"]
        date_str = date
    else:
        data = build_brief(session)
        sections = data["sections"]
        date_str = now_local().strftime("%Y-%m-%d")
    if not sections:
        return PlainTextResponse("还没有可渲染的节点", status_code=404)
    index = max(0, min(len(sections) - 1, section))
    payload = sections[index]
    if payload.get("type") == "weather":
        return PlainTextResponse(
            f"「{payload.get('name') or '天气'}」是纯文本节点，没有长图；"
            f"直接取 /api/brief 的 text 字段发送",
            status_code=404,
        )
    subtitle = ""
    meta = payload.get("meta") or {}
    if payload.get("type") == "news":
        subtitle = str(meta.get("subtitle") or "")
    png = render_section_png(payload, date_str=date_str, subtitle=subtitle)
    filename = f"brief-{date_str}-{index + 1}.png"
    return Response(
        content=png,
        media_type="image/png",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "no-cache",
        },
    )


@brief_router.get("/api/brief/preview")
def brief_preview(
    top_n: int = 10,
    days: int = 1,
    categories: str = "",
    topics: str = "",
    tags: str = "",
    keywords: str = "",
    exclude: str = "",
    starred_only: int = 0,
    sort: str = "score",
    session: Session = Depends(get_session),
) -> JSONResponse:
    """不落库的试算：调参数时前端即时预览（选 N 条、加过滤词）。"""
    cfg: dict[str, Any] = {
        "top_n": min(MAX_TOP_N, max(1, top_n)),
        "days": min(MAX_DAYS, max(1, days)),
        "categories": [c.strip() for c in categories.split(",") if c.strip()],
        "topics": [t.strip() for t in topics.split(",") if t.strip()],
        "tags": [t.strip() for t in tags.split(",") if t.strip()],
        "keywords": [k.strip() for k in keywords.split(",") if k.strip()],
        "exclude": [e.strip() for e in exclude.split(",") if e.strip()],
        "starred_only": bool(starred_only),
        "sort": sort if sort in SORT_CHOICES else "score",
    }
    from app.report.brief import brief_items, collect_brief_articles, render_text

    articles = collect_brief_articles(session, cfg)
    items = brief_items(articles)
    return JSONResponse({"total": len(items), "items": items, "text": render_text(items)})
