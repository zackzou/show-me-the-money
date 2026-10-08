"""关键词规则管理：屏蔽（block）与特别关注（star）。

页面入口在 ``/keywords``（导航「关键词」），与信源管理同一套交互风格：
输入框 + 添加、列表就地启停/删除、命中计数。规则存在 ``keyword_rules``
表里（见 ``app/fetcher/keywords.py`` 的匹配口径说明），改完**即时生效**：
下一轮抓取 / 正文处理就用新规则，不用重启。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request
from fastapi import Path as PathParam
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_session
from app.fetcher.keywords import KIND_BLOCK, KIND_STAR
from app.models import Article, KeywordRule
from app.utils.logger import get_logger
from app.utils.text import now_local
from app.web.routes import _ctx, templates

log = get_logger(__name__)

keywords_router = APIRouter()

MAX_KEYWORD_CHARS = 100


def _clean_keyword(raw: str | None) -> str:
    return (raw or "").strip()[:MAX_KEYWORD_CHARS]


def _clean_kind(raw: str | None) -> str:
    return KIND_STAR if (raw or "").strip() == KIND_STAR else KIND_BLOCK


def _rows(session: Session) -> dict[str, list[dict[str, Any]]]:
    """按 kind 分组列出规则（命中数倒序，让「拦得最多的」排前面）。"""
    out: dict[str, list[dict[str, Any]]] = {KIND_BLOCK: [], KIND_STAR: []}
    for row in session.execute(
        select(KeywordRule).order_by(KeywordRule.hits.desc(), KeywordRule.id.desc())
    ).scalars():
        out.setdefault(row.kind, []).append(
            {
                "id": row.id,
                "keyword": row.keyword,
                "kind": row.kind,
                "hits": row.hits,
                "enabled": bool(row.enabled),
                "created_at": row.created_at.strftime("%Y-%m-%d %H:%M")
                if row.created_at
                else "",
            }
        )
    return out


def _stats(session: Session) -> dict[str, int]:
    """页面头部的统计卡：规则数 + 命中总量 + 已屏蔽/已关注的文章数。"""
    rules = session.execute(
        select(KeywordRule.kind, func.count(KeywordRule.id)).group_by(KeywordRule.kind)
    ).all()
    by_kind = dict(rules)
    blocked_articles = int(
        session.execute(
            select(func.count(Article.id)).where(Article.deleted_at.isnot(None))
        ).scalar_one()
    )
    starred_articles = int(
        session.execute(
            select(func.count(Article.id)).where(
                Article.starred == 1, Article.deleted_at.is_(None)
            )
        ).scalar_one()
    )
    total_hits = int(
        session.execute(select(func.coalesce(func.sum(KeywordRule.hits), 0))).scalar_one()
    )
    return {
        "block_rules": by_kind.get(KIND_BLOCK, 0),
        "star_rules": by_kind.get(KIND_STAR, 0),
        "blocked_articles": blocked_articles,
        "starred_articles": starred_articles,
        "total_hits": total_hits,
    }


@keywords_router.get("/keywords", response_class=HTMLResponse)
def keywords_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    grouped = _rows(session)
    return templates.TemplateResponse(
        request,
        "keywords.html",
        _ctx(
            request,
            nav="keywords",
            title="关键词",
            block_rules=grouped.get(KIND_BLOCK, []),
            star_rules=grouped.get(KIND_STAR, []),
            stats=_stats(session),
        ),
    )


@keywords_router.post("/keywords", response_class=HTMLResponse)
async def keywords_create(
    request: Request, session: Session = Depends(get_session)
) -> HTMLResponse:
    """新增规则。重复（同关键词同类型）时提示而不是报错。"""
    form = await _form(request)
    keyword = _clean_keyword(form.get("keyword"))
    kind = _clean_kind(form.get("kind"))
    notice: dict[str, str] = {}
    if not keyword:
        notice = {"kind": "warn", "text": "请填写关键词"}
    else:
        existing = session.execute(
            select(KeywordRule).where(
                KeywordRule.keyword == keyword, KeywordRule.kind == kind
            )
        ).scalar_one_or_none()
        if existing is not None:
            label = "屏蔽" if kind == KIND_BLOCK else "关注"
            notice = {"kind": "warn", "text": f"「{keyword}」已经在{label}列表里了"}
        else:
            session.add(KeywordRule(keyword=keyword, kind=kind, created_at=now_local()))
            session.commit()
            label = "屏蔽" if kind == KIND_BLOCK else "特别关注"
            notice = {"kind": "ok", "text": f"已添加{label}：{keyword}（即时生效）"}
            log.info("新增关键词规则：%s %r", kind, keyword)
    grouped = _rows(session)
    return templates.TemplateResponse(
        request,
        "keywords.html",
        _ctx(
            request,
            nav="keywords",
            title="关键词",
            block_rules=grouped.get(KIND_BLOCK, []),
            star_rules=grouped.get(KIND_STAR, []),
            stats=_stats(session),
            notice=notice,
        ),
    )


@keywords_router.post("/keywords/{rule_id}/toggle")
def keywords_toggle(
    rule_id: int = PathParam(ge=1, le=2**63 - 1),
    session: Session = Depends(get_session),
) -> JSONResponse:
    row = session.get(KeywordRule, rule_id)
    if row is None:
        return JSONResponse({"ok": False, "detail": "规则不存在"}, status_code=404)
    row.enabled = 0 if row.enabled else 1
    session.commit()
    return JSONResponse({"ok": True, "enabled": bool(row.enabled)})


@keywords_router.post("/keywords/{rule_id}/delete")
def keywords_delete(
    rule_id: int = PathParam(ge=1, le=2**63 - 1),
    session: Session = Depends(get_session),
) -> JSONResponse:
    row = session.get(KeywordRule, rule_id)
    if row is None:
        return JSONResponse({"ok": False, "detail": "规则不存在"}, status_code=404)
    session.delete(row)
    session.commit()
    log.info("删除关键词规则：%s %r", row.kind, row.keyword)
    return JSONResponse({"ok": True})


async def _form(request: Request) -> dict[str, str]:
    """解析表单（同 sources.py 的写法：不依赖 python-multipart）。"""
    raw = (await request.body()).decode("utf-8", "replace")
    parsed = parse_qs(raw, keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items() if values}
