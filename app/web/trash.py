"""回收站：文章软删除、恢复、批量永久删除。

设计：删除只是把 ``deleted_at`` 打上时间戳（不搬正文/译文/图片），
「恢复」是一次 UPDATE。永久删除（清空 / 批量选中）才真的 ``DELETE`` 行，
并把只被这些行引用的本地图片清掉（与清理任务的孤儿图对账同一套做法）。

展示口径统一走 ``models.visible_article_conditions()``：列表 / 日报 / 搜索 /
RSS 全都过滤 ``deleted_at IS NULL``，所以删掉的文章立刻从所有入口消失，
只在 ``/trash`` 里可见。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi import Path as PathParam
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Article, Source
from app.scheduler import _prune_orphan_images  # 复用「孤儿图对账」逻辑
from app.utils.logger import get_logger
from app.utils.text import now_local
from app.web.routes import _ctx, templates  # 与页面路由共用模板目录

log = get_logger(__name__)

trash_router = APIRouter()

# 路径参数是 SQLite INTEGER（有符号 64 位）：越界值直接 404 而不是 500。
IdParam = int


def _json_ok(**extra: Any) -> JSONResponse:
    return JSONResponse({"ok": True, **extra})


def _json_error(detail: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "detail": detail}, status_code=status)


def _wants_json(request: Request) -> bool:
    """fetch 调用（回收站页的按钮）要 JSON；无 JS 的表单提交走整页重渲染。"""
    return "application/json" in request.headers.get("accept", "") or (
        request.headers.get("x-requested-with") == "fetch"
    )


def _trash_rows(session: Session) -> list[dict[str, Any]]:
    """回收站里的文章（按删除时间倒序）。带来源名与「为什么被删」。"""
    rows = session.execute(
        select(Article, Source.name)
        .outerjoin(Source, Article.source_id == Source.id)
        .where(Article.deleted_at.isnot(None))
        .order_by(Article.deleted_at.desc(), Article.id.desc())
    ).all()
    out: list[dict[str, Any]] = []
    for article, source_name in rows:
        out.append(
            {
                "id": article.id,
                "title": article.title_zh or article.title,
                "source": source_name or "未知来源",
                "link": article.link,
                "deleted_at": article.deleted_at.strftime("%Y-%m-%d %H:%M")
                if article.deleted_at
                else "",
                "published": article.published_at.strftime("%Y-%m-%d %H:%M")
                if article.published_at
                else "",
                "category": article.category or "",
                "reason": _delete_reason(article),
            }
        )
    return out


def _delete_reason(article: Article) -> str:
    """这条为什么在回收站：手动删除 / 命中屏蔽关键词。

    关键词屏蔽走的是同一列 ``deleted_at``，页面上要能区分 —— 否则用户会
    以为「我删过这篇吗？」。屏蔽词命中时标题里通常就带着那个词，展示出来
    用户一看就明白是哪条规则拦的。
    """
    if article.degraded_reason and article.degraded_reason.startswith("关键词屏蔽"):
        return article.degraded_reason
    return "手动删除"


@trash_router.get("/trash", response_class=HTMLResponse)
def trash_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    """回收站页面。"""
    rows = _trash_rows(session)
    return templates.TemplateResponse(
        request,
        "trash.html",
        _ctx(request, nav="trash", title="回收站", rows=rows, total=len(rows)),
    )


@trash_router.post("/api/articles/{article_id}/delete")
def delete_article(
    request: Request,
    article_id: int = PathParam(ge=1, le=2**63 - 1),
    session: Session = Depends(get_session),
) -> JSONResponse:
    """软删除一篇文章（移入回收站）。"""
    article = session.get(Article, article_id)
    if article is None:
        return _json_error("文章不存在", 404)
    if article.deleted_at is not None:
        return _json_ok(already=True)
    article.deleted_at = now_local()
    session.commit()
    log.info("文章 #%d 移入回收站（%s）", article.id, (article.title or "")[:50])
    return _json_ok()


@trash_router.post("/api/articles/{article_id}/restore")
def restore_article(
    request: Request,
    article_id: int = PathParam(ge=1, le=2**63 - 1),
    session: Session = Depends(get_session),
) -> JSONResponse:
    """从回收站恢复一篇文章。"""
    article = session.get(Article, article_id)
    if article is None:
        return _json_error("文章不存在", 404)
    if article.deleted_at is None:
        return _json_ok(already=True)
    article.deleted_at = None
    session.commit()
    log.info("文章 #%d 已从回收站恢复", article.id)
    return _json_ok()


def _purge(session: Session, ids: list[int]) -> int:
    """永久删除指定 id 的文章，返回实际删除条数（不提交，调用方提交）。"""
    if not ids:
        return 0
    result = session.execute(delete(Article).where(Article.id.in_(ids)))
    removed = int(getattr(result, "rowcount", 0) or 0)
    return removed


@trash_router.post("/api/trash/purge")
async def purge_trash(
    request: Request, session: Session = Depends(get_session)
) -> JSONResponse:
    """永久删除：``ids`` 为空（或没传）时清空回收站，否则只删选中的。"""
    form = await _read_form(request)
    raw = form.get("ids", "")
    ids = [int(part) for part in raw.split(",") if part.strip().isdigit()]
    if not ids:
        # 清空：删所有在回收站里的行
        rows = session.execute(
            select(Article.id).where(Article.deleted_at.isnot(None))
        ).scalars()
        ids = list(rows)
    removed = _purge(session, ids)
    session.commit()
    # 图片按内容哈希落盘、从不自动清：永久删除后把没人引用的清掉。
    # ``_prune_orphan_images`` 要 Settings（它自己开 session 对账），
    # 所以从 app.state 取当前配置 —— 和调度器里那轮清理用的是同一份逻辑。
    pruned = _prune_orphan_images_safe(request)
    log.info("回收站永久删除 %d 篇（图片清理 %d 个）", removed, pruned)
    return _json_ok(removed=removed, images=pruned)


def _prune_orphan_images_safe(request: Request) -> int:
    """孤儿图对账。失败不报错（图多留一轮无害）。"""
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        return 0
    try:
        return _prune_orphan_images(settings)
    except Exception as exc:  # pragma: no cover - 对账失败不影响删除结果
        log.warning("孤儿图对账失败：%r", exc)
        return 0


async def _read_form(request: Request) -> dict[str, str]:
    """解析表单或 JSON body（回收站的按钮两种都可能发）。"""
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            data = await request.json()
        except ValueError:
            return {}
        # body 是数组/字符串（如 ["x"]）时 .items() 会 AttributeError 变 500
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items()}
    from urllib.parse import parse_qs

    raw = (await request.body()).decode("utf-8", "replace")
    parsed = parse_qs(raw, keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items() if values}


def trash_counts(session: Session) -> dict[str, int]:
    """回收站统计：总数 + 今天删除的（页面头部展示）。"""
    today = now_local().strftime("%Y-%m-%d")
    total = int(
        session.execute(
            select(func.count(Article.id)).where(Article.deleted_at.isnot(None))
        ).scalar_one()
    )
    today_count = int(
        session.execute(
            select(func.count(Article.id)).where(
                Article.deleted_at.isnot(None),
                Article.deleted_at >= datetime.strptime(today, "%Y-%m-%d"),
            )
        ).scalar_one()
    )
    return {"total": total, "today": today_count}
