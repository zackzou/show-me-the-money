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

import time
from typing import Annotated, Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.db import get_session
from app.fetcher.guard import blocked_reason
from app.fetcher.lang import LANG_AUTO, detect_from_feed
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

# SQLite 的 INTEGER 是有符号 64 位；超界 id 传进去会由驱动抛 OverflowError
# 变成 500。不存在的源本来就该是 404，所以在参数声明处就挡掉。
IdParam = Annotated[int, PathParam(ge=1, le=2**63 - 1)]

# 一页几个信源。10 个正好一屏能看完一眼扫完，又不会让页面拉得太长。
PAGE_SIZE = 10
# tab 取值。默认只看「启用中」—— 停用的信源不该占着首页的注意力，
# 但它们必须随时找得回来，所以做成 tab 而不是藏进二级页面。
TAB_ACTIVE = "on"
TAB_DISABLED = "off"
TABS = (TAB_ACTIVE, TAB_DISABLED)
TAB_LABELS = {TAB_ACTIVE: "启用中", TAB_DISABLED: "已停用"}
# 语言：默认「自适应」，按**抓到的内容**判断，不靠 IP 也不靠用户填。
# 取值定义见 app.fetcher.lang（判定逻辑也在那里）。
LANGS = (LANG_AUTO, "en", "zh")
LANG_LABELS = {
    LANG_AUTO: "自适应",
    "en": "英文（自动译成中文）",
    "zh": "中文（直接用原文）",
}


def _clean_tab(raw: str | None) -> str:
    return raw if raw in TABS else TAB_ACTIVE


def _clean_lang(raw: str | None) -> str:
    return raw if raw in LANGS else LANG_AUTO


def _clean_page(raw: str | None) -> int:
    try:
        value = int(raw or "")
    except (TypeError, ValueError):
        return 1
    return value if value >= 1 else 1


def _lang_hint(lang: str, detected: str = "") -> str:
    """加完源后说清楚它被怎么处理，别让用户猜。"""
    if lang == "zh":
        return "；按你的设置当中文源处理：直接用原文，不翻译"
    if lang == "en":
        return "；按你的设置当英文源处理：会自动生成中文版"
    if detected == "zh":
        return "；自动识别为中文站：直接用原文，不翻译"
    if detected == "en":
        return "；自动识别为英文站：会自动生成中文版，页面默认显示中文"
    return ""


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
    # 纵深防御：拒绝云厂商元数据接口一类的本机/链路本地地址（见 guard.py
    # 的说明 —— 为什么不连内网一起拦）。
    reason = blocked_reason(url)
    if reason:
        raise HTTPException(status_code=400, detail=reason)
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

    返回 ``{"ok", "count", "error", "sample", "lang"}``。
    故意不做静默失败：加源时用户最需要看到的是「为什么不通」。

    ``lang`` 是按这批条目实际内容判出来的语言，加源时直接告诉用户「识别成
    中文站/英文站」，省得加完还要自己去列表里核对。判据见 app.fetcher.lang。
    """
    try:
        items = fetch_feed(url, timeout=timeout)
    except Exception as exc:
        return {"ok": False, "count": 0, "error": str(exc)[:200], "sample": "",
                "lang": ""}
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
        "lang": detect_from_feed(items) if items else "",
        "error": "" if items else "能连上但没解析出任何条目（可能不是 RSS/Atom，或需要登录）",
        "sample": sample,
    }


def _all_source_stats(session: Session) -> dict[int, dict[str, Any]]:
    """一次查出所有信源的篇数与最新一篇时间。

    原来每个源单独查 2 次（篇数 + 最新），信源页就是 ``2N+2`` 条 SQL；
    而 ``articles.source_id`` 是外键却没有索引，SQLite 也不会自动建外键索引，
    于是每次都是全表扫描（3 万篇时每个源 12ms，9 个源 108ms）。现在压成
    一次 ``GROUP BY``，配合 ix_articles_source_id 走索引。
    """
    rows = session.execute(
        select(
            Article.source_id,
            func.count(Article.id),
            func.max(Article.published_at),
        )
        .where(Article.source_id.isnot(None))
        .group_by(Article.source_id)
    ).all()
    out: dict[int, dict[str, Any]] = {}
    for source_id, total, latest in rows:
        if source_id is None:      # WHERE 已排除，这里只是让类型收敛
            continue
        out[source_id] = {
            "total": int(total or 0),
            "latest": latest.strftime("%Y-%m-%d %H:%M") if latest else None,
        }
    return out


def _source_stats(session: Session, source_id: int | None) -> dict[str, Any]:
    """这个源已经产出了多少篇文章（管理页要显示，免得误删有用的源）。"""
    if source_id is None:
        return {"total": 0, "latest": None}
    return _all_source_stats(session).get(source_id, {"total": 0, "latest": None})


def _rows(session: Session, stats: dict[int, dict[str, Any]] | None = None
          ) -> list[dict[str, Any]]:
    # 软删除的源不出现在列表里（见 models.Source.deleted）
    sources = list(
        session.execute(select(Source).where(Source.deleted == 0).order_by(Source.id)).scalars()
    )
    return _rows_from(sources, stats if stats is not None else _all_source_stats(session))


def _deleted_rows(session: Session, stats: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
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
    return _rows_from(rows, stats)


def _rows_from(sources: list[Source], stats: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """组装信源行；篇数统计由调用方查一次后传进来（见 _all_source_stats）。"""
    return [_row(row, stats) for row in sources]


def _row(row: Source, stats: dict[int, dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "url": row.url,
        "type": row.type,
        "lang": row.lang,
        "enabled": bool(row.enabled),
        "created": row.created_at.strftime("%Y-%m-%d") if row.created_at else "",
        # 停用原因展示：存量旧行没有记录，如实说「历史停用」而不是编一个。
        "disabled_reason": row.disabled_reason or ("历史停用（原因未记录）" if not row.enabled else ""),
        "disabled_at": row.disabled_at.strftime("%Y-%m-%d %H:%M") if row.disabled_at else "",
        **stats.get(row.id, {"total": 0, "latest": None}),
    }


def _paged(session: Session, tab: str, page: int) -> tuple[list[dict[str, Any]], int, int]:
    """按 tab 取一页信源，返回 ``(这一页的行, 总条数, 总页数)``。"""
    stats = _all_source_stats(session)
    column = Source.enabled == 1 if tab == TAB_ACTIVE else Source.enabled != 1
    total = int(
        session.execute(
            select(func.count(Source.id)).where(Source.deleted == 0, column)
        ).scalar_one()
        or 0
    )
    pages = max(1, -(-total // PAGE_SIZE))
    page = max(1, min(page, pages))
    rows = list(
        session.execute(
            select(Source)
            .where(Source.deleted == 0, column)
            .order_by(Source.id.desc())
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        ).scalars()
    )
    return [_row(row, stats) for row in rows], total, pages


def _list_ctx(session: Session, tab: str, page: int) -> dict[str, Any]:
    """列表区需要的全部上下文。整页渲染与局部刷新共用它 —— 两个入口给同一份
    数据的两种包装，才不会出现「刷新后和刷新前长得不一样」。"""
    rows, total, pages = _paged(session, tab, page)
    stats = _all_source_stats(session)
    return {
        "sources": rows,
        "tab": tab,
        "page": page,
        "pages": pages,
        "total": total,
        "page_size": PAGE_SIZE,
        "tabs": [{"key": key, "label": TAB_LABELS[key],
                  "n": _count_tab(session, key)} for key in TABS],
        "deleted_sources": _deleted_rows(session, stats),
        "lang_labels": LANG_LABELS,
        "page_window": _page_window(page, pages),
    }


def _page_window(page: int, pages: int) -> list[tuple[int, bool]]:
    """分页窗口：``[(页码, 是否省略号), ...]``。"""
    if pages <= 1:
        return []
    out: list[tuple[int, bool]] = []
    previous = 0
    for n in range(1, pages + 1):
        if not (n <= 1 or n > pages - 1 or abs(n - page) <= 1):
            continue
        if previous and n - previous > 1:
            out.append((n, True))
        out.append((n, False))
        previous = n
    return out


def _count_tab(session: Session, tab: str) -> int:
    column = Source.enabled == 1 if tab == TAB_ACTIVE else Source.enabled != 1
    return int(
        session.execute(
            select(func.count(Source.id)).where(Source.deleted == 0, column)
        ).scalar_one()
        or 0
    )


def _list_fragment(request: Request, session: Session, tab: str, page: int) -> str:
    """只渲染「列表 + 分页 + 回收站」这一块，供前端局部替换。"""
    return templates.get_template("_sources_list.html").render(
        **_list_ctx(session, tab, page)
    )


def _wants_json(request: Request) -> bool:
    """前端 fetch 带这个头；普通表单提交不带，所以两条路都能走。"""
    return bool(request.headers.get("x-smtt-partial"))


def _respond(
    request: Request,
    session: Session,
    tab: str,
    page: int,
    *,
    notice: dict[str, str] | None = None,
    status: int = 200,
) -> Any:
    """统一出口：fetch 要 JSON + 列表片段，普通提交要整页。

    没有 JS 时（直接点表单按钮）走整页重渲染，功能完整；有了 JS 就只换列表，
    改名/启停/删除不会把整页刷掉 —— 用户抱怨的正是这个。
    """
    notice = notice or {}
    if _wants_json(request):
        return JSONResponse(
            {
                "ok": status < 400,
                "notice": notice,
                "tab": tab,
                "page": page,
                "list_html": _list_fragment(request, session, tab, page),
            },
            status_code=status,
        )
    return _render(request, session, tab=tab, page=page, notice=notice, status=status)


def _render(
    request: Request,
    session: Session,
    *,
    tab: str = TAB_ACTIVE,
    page: int = 1,
    notice: dict[str, str] | None = None,
    status: int = 200,
) -> HTMLResponse:
    # 篇数统计整页只查一次：活跃列表与已删除列表共用同一份统计，
    # 否则这一页会重复跑同一条 GROUP BY。
    return templates.TemplateResponse(
        request,
        "sources.html",
        _ctx(
            request,
            nav="sources",
            notice=notice or {},
            **_list_ctx(session, tab, page),
        ),
        status_code=status,
    )


@sources_router.get("/sources", response_class=HTMLResponse)
def sources_page(
    request: Request,
    tab: str | None = None,
    page: int = Query(1, ge=1),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """信源管理页：看现状、加源、改名/启停、删源。默认只看启用中的。"""
    return _render(request, session, tab=_clean_tab(tab), page=page)


@sources_router.get("/sources/list", response_class=HTMLResponse)
def sources_list_fragment(
    request: Request,
    tab: str | None = None,
    page: int = Query(1, ge=1),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """只要列表区那一块。前端换 tab / 翻页时用它，不必重排整页。"""
    return HTMLResponse(_list_fragment(request, session, _clean_tab(tab), page))


@sources_router.post("/sources", response_class=HTMLResponse)
async def sources_create(
    request: Request,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """新增信源：先试抓，通了才存。抓不通就把原因原样显示回去。"""
    form = await _form(request)
    name = _clean_name(form.get("name", ""), form.get("url", ""))
    url = _clean_url(form.get("url", ""))
    # 默认「自适应」：按抓到的内容判断这个站是中文还是英文，而不是让用户猜
    lang = _clean_lang(form.get("lang"))
    enabled = 1 if form.get("enabled") else 0
    tab = _clean_tab(form.get("tab"))
    page = _clean_page(form.get("page"))

    existing = session.execute(select(Source).where(Source.url == url)).scalar_one_or_none()
    if existing is not None:
        return _respond(
            request, session, tab, page,
            notice={"kind": "warn", "text": f"这个地址已经在源列表里了（{existing.name}）"},
            status=400,
        )

    probe = await run_in_threadpool(_probe, url)
    if not probe["ok"]:
        # 不存：留着只会让调度器每 2 小时失败一次
        return _respond(
            request, session, tab, page,
            notice={
                "kind": "error",
                "text": f"没能从这个地址取到内容：{probe['error']}（未保存）",
            },
            status=400,
        )

    session.add(Source(
        name=name, url=url, type="rss", lang=lang, enabled=enabled, created_at=now_local(),
        # 添加时就没勾「启用」的，同样记录原因，别让「已停用」列表里出现来历不明的行
        disabled_reason="添加时未启用" if not enabled else None,
        disabled_at=now_local() if not enabled else None,
    ))
    session.commit()
    detail = f"，读到 {probe['count']} 条" + (f"，例如「{probe['sample']}」" if probe["sample"] else "")
    if enabled:
        detail += _lang_hint(lang, probe.get("lang") or "")
    # 新加的源排在最前（按 id 倒序），跳到第 1 页才能立刻看到它
    return _respond(
        request, session, tab, 1,
        notice={"kind": "ok", "text": f"已添加 {name}{detail}"},
    )


@sources_router.post("/sources/{source_id}/toggle", response_class=HTMLResponse)
async def sources_toggle(
    request: Request, source_id: IdParam, session: Session = Depends(get_session)
) -> HTMLResponse:
    """启用/停用。停用只是不再抓新文章，已入库的文章照常可读。"""
    row = session.get(Source, source_id)
    if row is None or row.deleted:
        raise HTTPException(status_code=404, detail="信源不存在")
    form = await _form(request)
    tab = _clean_tab(form.get("tab"))
    page = _clean_page(form.get("page"))
    row.enabled = 0 if row.enabled else 1
    # 停用要留痕：什么时候、为什么。启用时一并清掉，不留过期信息。
    if not row.enabled:
        row.disabled_reason = "手动停用"
        row.disabled_at = now_local()
    else:
        row.disabled_reason = None
        row.disabled_at = None
    session.commit()
    verb = "已启用" if row.enabled else "已停用"
    # 启停会改变它属于哪个 tab：停用之后它从「启用中」消失。停在当前页，
    # 夹带一个提示说明它去哪儿了，而不是让列表无声地少一行。
    if tab == TAB_ACTIVE and not row.enabled:
        notice = {"kind": "ok", "text": f"已停用：{row.name}（可在「已停用」里找回）"}
    elif tab == TAB_DISABLED and row.enabled:
        notice = {"kind": "ok", "text": f"已启用：{row.name}"}
    else:
        notice = {"kind": "ok", "text": f"{verb}：{row.name}"}
    return _respond(request, session, tab, page, notice=notice)


@sources_router.post("/sources/{source_id}/rename", response_class=HTMLResponse)
async def sources_rename(
    request: Request, source_id: IdParam, session: Session = Depends(get_session)
) -> HTMLResponse:
    """改显示名。URL 不给改 —— 改地址等于换一个新源，历史文章还挂在旧源上。"""
    row = session.get(Source, source_id)
    if row is None or row.deleted:
        raise HTTPException(status_code=404, detail="信源不存在")
    form = await _form(request)
    name = form.get("name", "").strip()
    tab = _clean_tab(form.get("tab"))
    page = _clean_page(form.get("page"))
    if not name:
        return _respond(
            request, session, tab, page,
            notice={"kind": "warn", "text": "名称不能为空"}, status=400,
        )
    if len(name) > MAX_NAME_LEN:
        return _respond(
            request, session, tab, page,
            notice={"kind": "warn", "text": "名称过长"}, status=400,
        )
    old = row.name
    row.name = name
    session.commit()
    return _respond(
        request, session, tab, page,
        notice={"kind": "ok", "text": f"已改名：{old} → {name}"},
    )


@sources_router.post("/sources/{source_id}/delete", response_class=HTMLResponse)  # noqa: E501
async def sources_delete(
    request: Request, source_id: IdParam, session: Session = Depends(get_session)
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
    form = await _form(request)
    tab = _clean_tab(form.get("tab"))
    page = _clean_page(form.get("page"))
    name = row.name
    row.deleted = 1
    row.enabled = 0
    session.commit()
    extra = f"，已入库的 {stats['total']} 篇文章与来源名保留" if stats["total"] else ""
    return _respond(
        request, session, tab, page,
        notice={"kind": "ok", "text": f"已删除 {name}{extra}（可在下方回收站撤回）"},
    )


@sources_router.post("/sources/{source_id}/restore", response_class=HTMLResponse)
async def sources_restore(
    request: Request, source_id: IdParam, session: Session = Depends(get_session)
) -> HTMLResponse:
    """恢复一个被删掉的源（删错了还能找回来）。"""
    row = session.get(Source, source_id)
    if row is None or not row.deleted:
        raise HTTPException(status_code=404, detail="信源不存在")
    form = await _form(request)
    tab = _clean_tab(form.get("tab"))
    page = _clean_page(form.get("page"))
    row.deleted = 0
    row.enabled = 1
    session.commit()
    return _respond(
        request, session, tab, page,
        notice={"kind": "ok", "text": f"已恢复：{row.name}（已启用）"},
    )


@sources_router.post("/sources/{source_id}/probe", response_class=HTMLResponse)
async def sources_probe(
    request: Request, source_id: IdParam, session: Session = Depends(get_session)
) -> HTMLResponse:
    """手动试抓一个已存在的源：用于排查「最近没抓到东西」。

    JSON 路径带一份结构化的 ``probe`` 详情（耗时/条数/样例/识别语言/错误），
    前端拿它渲染进度条与结果面板；无 JS 的表单路径仍走整页 notice。
    """
    row = session.get(Source, source_id)
    if row is None or row.deleted:
        raise HTTPException(status_code=404, detail="信源不存在")
    form = await _form(request)
    tab = _clean_tab(form.get("tab"))
    page = _clean_page(form.get("page"))
    started = time.time()
    probe = await run_in_threadpool(_probe, row.url)
    probe["ms"] = int((time.time() - started) * 1000)
    if _wants_json(request):
        if probe["ok"]:
            text = f"{row.name}：正常，读到 {probe['count']} 条"
        else:
            text = f"{row.name}：{probe['error']}"
        return JSONResponse(
            {
                "ok": probe["ok"],
                "notice": {"kind": "ok" if probe["ok"] else "error", "text": text},
                "probe": probe,
            },
            status_code=200 if probe["ok"] else 400,
        )
    if probe["ok"]:
        text = f"{row.name}：正常，读到 {probe['count']} 条" + (
            f"，例如「{probe['sample']}」" if probe["sample"] else ""
        )
        return _respond(request, session, tab, page,
                        notice={"kind": "ok", "text": text})
    return _respond(request, session, tab, page,
                    notice={"kind": "error", "text": f"{row.name}：{probe['error']}"},
                    status=400)


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