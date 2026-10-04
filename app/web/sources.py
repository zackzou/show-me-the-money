"""信源管理：增删改查 + 试抓，把「我要盯哪些网站」这件小事交给用户自己。

为什么单独开一个页面：信源原本只写在 ``config/default_sources.yaml`` 里，
要加一个网站得改文件重启服务。现在源本来就是从 ``sources`` 表读的
（``_enabled_sources``），只差一个写入口 —— 这里补上，并顺手做两件事：

1. **保存前先试抓**：加一个写错的 URL 会让调度器每 2 小时失败一次，
   用户却什么反馈都收不到。加源时先真的拉一次，抓不通就直接拒绝并把原因显示出来。
2. **删除只删源、不删文章**：文章已经处理过、译文也已经在库里，
   把历史一并删掉等于让读者点不开的链接。源删掉后新文章不再进来，
   老文章照常能读。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_session
from app.fetcher.pipeline import fetch_feed
from app.models import Article, Source
from app.utils.logger import get_logger
from app.utils.text import now_local
from app.web.routes import _ctx, templates  # 与页面路由共用同一套模板目录

log = get_logger(__name__)

sources_router = APIRouter()

# 只接受这几个协议：RSS 就是网络地址，file:// / javascript: 没有意义
ALLOWED_SCHEMES = ("http://", "https://")
# 试抓时最多看几条 —— 够判断「这个源通不通」，又不会真去抓几百条
PROBE_ITEMS = 3
MAX_URL_LEN = 500
MAX_NAME_LEN = 200


def _clean_url(raw: str) -> str:
    """校验并规范化 URL。非法就抛 400，带上原因。"""
    url = (raw or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="请填写 RSS 地址")
    if len(url) > MAX_URL_LEN:
        raise HTTPException(status_code=400, detail=f"地址过长（{len(url)} > {MAX_URL_LEN} 字符）")
    if not url.startswith(ALLOWED_SCHEMES):
        raise HTTPException(
            status_code=400, detail="地址必须以 http:// 或 https:// 开头（只支持 RSS/Atom 订阅源）"
        )
    return url


def _clean_name(raw: str, url: str) -> str:
    """站点名可留空：留空就从地址里取主机名，别逼用户填两遍。"""
    name = (raw or "").strip()
    if not name:
        name = url.split("//", 1)[-1].split("/", 1)[0]
    if len(name) > MAX_NAME_LEN:
        raise HTTPException(status_code=400, detail=f"名称过长（{len(name)} > {MAX_NAME_LEN} 字符）")
    return name


def _probe(url: str, *, timeout: float = 12.0) -> dict[str, Any]:
    """真的拉一次，确认这个源通、且确实解析得出条目。

    返回 ``{"ok": bool, "count": int, "error": str, "sample": str}``。
    故意不做静默失败：加源时用户最需要看到的是「为什么不通」。
    """
    try:
        items = fetch_feed(url, timeout=timeout)
    except Exception as exc:
        return {"ok": False, "count": 0, "error": str(exc)[:200], "sample": ""}
    sample = ""
    for item in items[:PROBE_ITEMS]:
        # 条目未必是 dict（fetch_feed 解析出来的东西类型不保证），
        # 直接 item.get 会在第三方 feed 上抛 AttributeError，
        # 变成「加源失败」而真实原因只是这一条格式怪
        title = item.get("title") if isinstance(item, dict) else ""
        title = (str(title) if title else "").strip()
        if title:
            sample = title[:80]
            break
    return {
        "ok": bool(items),
        "count": len(items),
        "error": "" if items else "能连上但没解析出任何条目（可能不是 RSS/Atom，或需要登录）",
        "sample": sample,
    }


def _source_stats(session: Session, source_id: int | None) -> dict[str, Any]:
    """这个源已经产出了多少篇文章（管理页要显示，免得误删有用的源）。"""
    if source_id is None:
        return {"total": 0, "latest": None}
    total = int(
        session.execute(
            select(func.count(Article.id)).where(Article.source_id == source_id)
        ).scalar()
        or 0
    )
    latest = session.execute(
        select(Article.published_at)
        .where(Article.source_id == source_id)
        .order_by(Article.published_at.desc())
        .limit(1)
    ).scalar()
    return {"total": total, "latest": latest.strftime("%Y-%m-%d %H:%M") if latest else None}


def _rows(session: Session) -> list[dict[str, Any]]:
    # 软删除的源不出现在列表里（见 models.Source.deleted）
    sources = list(
        session.execute(select(Source).where(Source.deleted == 0).order_by(Source.id)).scalars()
    )
    return [_row(session, row) for row in sources]


def _deleted_rows(session: Session) -> list[dict[str, Any]]:
    """已软删除的源。

    之前只有 ``/sources/{id}/restore`` 这个路由，页面上**没有任何入口**能点它 ——
    删除后源就从列表里消失了，用户既看不到也恢复不了；再手动添加同一个 URL 还会
    撞上那条隐藏的旧行、报「已存在」。删掉的源必须能看见、能撤回。
    """
    # 只按 id 倒序：表里没有 deleted_at（Source 只有 deleted 这个软删标记），
    # 为了显示一个删除日期去加列、给存量库补迁移，不值当。
    rows = list(
        session.execute(
            select(Source).where(Source.deleted != 0).order_by(Source.id.desc())
        ).scalars()
    )
    return [_row(session, row) for row in rows]


def _row(session: Session, row: Source) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "url": row.url,
        "type": row.type,
        "lang": row.lang,
        "enabled": bool(row.enabled),
        "created": row.created_at.strftime("%Y-%m-%d") if row.created_at else "",
        **_source_stats(session, row.id),
    }


def _render(request: Request, session: Session, *, notice: dict[str, str] | None = None,
            status: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "sources.html",
        _ctx(
            request,
            nav="sources",
            sources=_rows(session),
            deleted_sources=_deleted_rows(session),
            notice=notice or {},
        ),
        status_code=status,
    )


@sources_router.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    """信源管理页：看现状、加源、改名/启停、删源。"""
    return _render(request, session)


@sources_router.post("/sources", response_class=HTMLResponse)
async def sources_create(
    request: Request,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """新增信源：先试抓，通了才存。抓不通就把原因原样显示回去。"""
    form = await _form(request)
    name = _clean_name(form.get("name", ""), form.get("url", ""))
    url = _clean_url(form.get("url", ""))
    lang = (form.get("lang") or "en").strip()[:10] or "en"
    enabled = 1 if form.get("enabled") else 0

    existing = session.execute(select(Source).where(Source.url == url)).scalar_one_or_none()
    if existing is not None:
        return _render(
            request, session,
            notice={"kind": "warn", "text": f"这个地址已经在源列表里了（{existing.name}）"},
            status=400,
        )

    probe = _probe(url)
    if not probe["ok"]:
        # 不存：留着只会让调度器每 2 小时失败一次
        return _render(
            request, session,
            notice={
                "kind": "error",
                "text": f"没能从这个地址取到内容：{probe['error']}（未保存）",
            },
            status=400,
        )

    session.add(Source(name=name, url=url, type="rss", lang=lang, enabled=enabled, created_at=now_local()))
    session.commit()
    detail = f"，读到 {probe['count']} 条" + (f"，例如「{probe['sample']}」" if probe["sample"] else "")
    return _render(request, session, notice={"kind": "ok", "text": f"已添加 {name}{detail}"})


@sources_router.post("/sources/{source_id}/toggle", response_class=HTMLResponse)
async def sources_toggle(
    request: Request, source_id: int, session: Session = Depends(get_session)
) -> HTMLResponse:
    """启用/停用。停用只是不再抓新文章，已入库的文章照常可读。"""
    row = session.get(Source, source_id)
    if row is None or row.deleted:
        raise HTTPException(status_code=404, detail="信源不存在")
    row.enabled = 0 if row.enabled else 1
    session.commit()
    verb = "已启用" if row.enabled else "已停用"
    return _render(request, session, notice={"kind": "ok", "text": f"{verb}：{row.name}"})


@sources_router.post("/sources/{source_id}/rename", response_class=HTMLResponse)
async def sources_rename(
    request: Request, source_id: int, session: Session = Depends(get_session)
) -> HTMLResponse:
    """改显示名。URL 不给改 —— 改地址等于换一个新源，历史文章还挂在旧源上。"""
    row = session.get(Source, source_id)
    if row is None or row.deleted:
        raise HTTPException(status_code=404, detail="信源不存在")
    name = (await _form(request)).get("name", "").strip()
    if not name:
        return _render(
            request, session, notice={"kind": "warn", "text": "名称不能为空"}, status=400
        )
    if len(name) > MAX_NAME_LEN:
        return _render(
            request, session, notice={"kind": "warn", "text": "名称过长"}, status=400
        )
    old = row.name
    row.name = name
    session.commit()
    return _render(request, session, notice={"kind": "ok", "text": f"已改名：{old} → {name}"})


@sources_router.post("/sources/{source_id}/delete", response_class=HTMLResponse)  # noqa: E501
async def sources_delete(
    request: Request, source_id: int, session: Session = Depends(get_session)
) -> HTMLResponse:
    """删除信源：**软删除**。

    置 ``deleted=1`` 而不是真的删行 —— 真删会把 ``articles.source_id``
    连带置成 NULL（SQLAlchemy 关系的默认行为），那 27 篇历史文章的
    「来源」就全变成了「未知来源」，等于把用户已有的信息悄悄抹掉。
    软删除后：列表里不再出现、后台不再抓新文章，已入库文章照常可读、来源名还在。
    彻底删行可以在设置里做。
    """
    row = session.get(Source, source_id)
    if row is None or row.deleted:
        raise HTTPException(status_code=404, detail="信源不存在")
    stats = _source_stats(session, source_id)
    name = row.name
    row.deleted = 1
    row.enabled = 0
    session.commit()
    extra = f"，已入库的 {stats['total']} 篇文章与来源名保留" if stats["total"] else ""
    return _render(request, session, notice={"kind": "ok", "text": f"已删除 {name}{extra}"})


@sources_router.post("/sources/{source_id}/restore", response_class=HTMLResponse)
async def sources_restore(
    request: Request, source_id: int, session: Session = Depends(get_session)
) -> HTMLResponse:
    """恢复一个被删掉的源（删错了还能找回来）。"""
    row = session.get(Source, source_id)
    if row is None or not row.deleted:
        raise HTTPException(status_code=404, detail="信源不存在")
    row.deleted = 0
    row.enabled = 1
    session.commit()
    return _render(request, session, notice={"kind": "ok", "text": f"已恢复：{row.name}"})


@sources_router.post("/sources/{source_id}/probe", response_class=HTMLResponse)
async def sources_probe(
    request: Request, source_id: int, session: Session = Depends(get_session)
) -> HTMLResponse:
    """手动试抓一个已存在的源：用于排查「最近没抓到东西」。"""
    row = session.get(Source, source_id)
    if row is None or row.deleted:
        raise HTTPException(status_code=404, detail="信源不存在")
    probe = _probe(row.url)
    if probe["ok"]:
        text = f"{row.name}：正常，读到 {probe['count']} 条" + (
            f"，例如「{probe['sample']}」" if probe["sample"] else ""
        )
        return _render(request, session, notice={"kind": "ok", "text": text})
    return _render(request, session, notice={"kind": "error", "text": f"{row.name}：{probe['error']}"})


async def _form(request: Request) -> dict[str, str]:
    """解析 ``application/x-www-form-urlencoded`` 表单。

    **不走 ``request.form`` / ``Form(...)`` 参数**：两者都依赖 python-multipart，
    而本项目没有这个依赖（所有表单都没有文件上传，不需要它）。
    早先这里写的是 `request.form` 加 try/except 兜底 —— 缺依赖时它抛异常，
    except 返回**空字典**，于是不管提交什么都读不到 ``url``，
    一律报「请填写 RSS 地址」，报错文案与真实原因完全对不上。

    ``request.body()`` 是协程，所以处理 POST 的路由必须是 ``async def``。
    """
    raw = (await request.body()).decode("utf-8", "replace")
    parsed = parse_qs(raw, keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items() if values}