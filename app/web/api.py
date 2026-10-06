"""JSON API 路由。"""

from __future__ import annotations

import json
import threading
import time
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi import Path as PathParam
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import __version__
from app.ai.processor import LANG_AUTO, STATUS_PROCESSED, process_article
from app.config import Settings
from app.db import get_session, session_scope
from app.fetcher.content import _store_body_anchors, count_with_full_text, fetch_article_document
from app.fetcher.images import count_with_images
from app.fetcher.media_store import media_dir_for
from app.models import Article, DailyReport, Source
from app.report.generator import STATUS_REPORTABLE, day_window
from app.schemas import ArticleDetailOut, ArticleOut, HealthOut, ReportDetail, ReportOut
from app.utils.text import BRIEF_DIGEST_CHARS as _BRIEF_DIGEST_CHARS
from app.utils.text import brief_digest, chinese_ratio, has_long_latin_run, now_local

api_router = APIRouter(prefix="/api")

# SQLite 的 INTEGER 是有符号 64 位。裸 ``int`` 会照单全收 10**30，然后由驱动抛
# OverflowError 变成 500；不存在的 id 本来就该是 404，所以直接挡在参数声明处。
IdParam = Annotated[int, PathParam(ge=1, le=2**63 - 1)]


# 早报汇总的下限：低于这个字数在手机上只有两行，推送里显得敷衍
_BRIEF_MIN_CHARS = 70


@api_router.get("/articles", response_model=list[ArticleOut])
def list_articles(
    date: str | None = None,
    limit: int = Query(200, ge=0, le=1000),
    session: Session = Depends(get_session),
):
    """按日期（默认**北京时间**今天）列出进日报的文章。"""
    try:
        start, end = day_window(date or now_local().strftime("%Y-%m-%d"))
    except (ValueError, OverflowError):
        # OverflowError 是真实存在的：``?date=9999-12-31`` 能过 datetime.strptime，
        # 但 day_window 里的 ``start + timedelta(days=1)`` 会溢出。原来这里只接
        # ValueError，于是这一种输入直接 500。
        raise HTTPException(status_code=400, detail=f"日期格式应为 YYYY-MM-DD：{date!r}") from None
    statement = (
        select(Article)
        .where(
            Article.relevance == 1,
            Article.status.in_(STATUS_REPORTABLE),
            Article.duplicate_of.is_(None),
            Article.published_at >= start,
            Article.published_at < end,
        )
        .order_by(Article.published_at.desc(), Article.id.desc())
    )
    # limit=0 表示「一条也不要」。早先是 max(1, limit)，于是 ?limit=0 返回 1 条 ——
    # 问「零条」得到一条，比报错更难排查。
    if limit:
        statement = statement.limit(min(limit, 1000))
    return list(session.execute(statement).scalars())


@api_router.get("/articles/{article_id}", response_model=ArticleDetailOut)
def get_article(article_id: IdParam, session: Session = Depends(get_session)):
    """单篇文章详情。

    早报片段浮层点开时才来取 —— 顺带让收藏页能按 id 拿历史文章，
    不必把整页几百篇都拉回来。
    """
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="文章不存在")
    source = session.get(Source, article.source_id) if article.source_id else None
    return ArticleDetailOut(
        **ArticleOut.model_validate(article).model_dump(),
        source_name=source.name if source else "未知来源",
        # 汇总太短撑不起三到五行，就用推荐理由补上（去重，避免复读同一句）
        digest_brief=_digest_with_fallback(article),
        topics_list=_topics_json(article.topics)[:3],
    )


def _topics_json(raw: str | None) -> list[str]:
    """topics 库里存的是 JSON 数组字符串，直接 split 会把引号一起带出来。"""
    try:
        parsed = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return [str(x) for x in parsed if isinstance(x, str)] if isinstance(parsed, list) else []


def _is_chinese_usable(text: str | None) -> bool:
    """能不能当中文早报正文用：有汉字、且没有一长串没翻的英文。"""
    clean = (text or "").strip()
    if not clean:
        return False
    return chinese_ratio(clean) >= 0.4 and not has_long_latin_run(clean)


def _digest_with_fallback(article: Article) -> str:
    """早报片段：一段**完整的中文**话。

    早报是要推到手机上的，所以这里有两条硬要求：

    1. **必须完整**。``brief_digest`` 早先在一整句放不下预算时会硬截断加省略号，
       产出过「…Only $30 more than the wireless charging ver…」这种东西 ——
       半句话推给读者，页面看着却像正常的。
    2. **必须是中文**。英文信源在中文版还没写出来时，``digest`` 里躺的是英文；
       直接拿来用就会出现「伦理声明白。Only $30 more than the wireless…」
       这种半中半英的句子。所以英文原文一律不作为早报文案，
       最多只拿标题兜底，并且明说译文还没准备好。

    取值顺序：AI 写的推送语 → 中文导读 → 中文推荐理由 → 中文标题。
    """
    stored = (getattr(article, "brief_zh", None) or "").strip()
    if _is_chinese_usable(stored):
        return stored

    # 只从「确实是中文」的字段里挑，英文原文不参与
    for candidate in (article.digest_zh, article.digest, article.reason, article.summary):
        if not _is_chinese_usable(candidate):
            continue
        brief = brief_digest(candidate)
        if len(brief) >= _BRIEF_MIN_CHARS:
            return brief
        extra = brief_digest(
            article.reason if candidate is not article.reason else article.summary or "",
            limit=_BRIEF_DIGEST_CHARS,
        )
        if _is_chinese_usable(extra) and extra not in brief:
            # 只去掉**句中的连接标点**，不能连句末的句号一起去掉。
            # 去掉之后 joiner 又因为 brief 已以句号结尾而恒为 ""，拼出来的
            # 段落就永远不以句末标点收尾 —— 正好是用户最反感的那种半句结尾
            #（「…值得持续观察与评估」，后面本来还有内容）。
            extra = extra.rstrip("，、；：,;: ")
            if extra:
                if extra[-1] in "。！？.!?":
                    merged = f"{brief}{extra}"
                else:
                    joiner = "" if brief[-1] in "。！？.!?" else "。"
                    merged = f"{brief}{joiner}{extra}。"
                if len(merged) <= _BRIEF_DIGEST_CHARS + 20:
                    return merged
        if brief:
            return brief

    # 兜底：中文标题（英文源的 title_zh 由补译轮填）。宁可短，也不推英文。
    title = (article.title_zh or article.title or "").strip()
    if chinese_ratio(title) >= 0.4:
        return brief_digest(title)
    return f"（{title}）中文译文还在整理中。" if title else ""


# ── 重新获取：重抓原文 + 按当前配置重跑 AI 处理 ──────────────────────
# 进程内任务表：article_id → {"state": running|ok|failed, "reason": str|None}
# 本应用是单进程 uvicorn，进程内存即可。若改成多 worker 部署，轮询请求可能
# 落在没有这个任务的 worker 上拿到 idle —— 那时要把状态挪到库表或 Redis。
_refetch_jobs: dict[int, dict[str, Any]] = {}
_refetch_lock = threading.Lock()

# 原文换了，这些派生字段就全部作废：旧摘要/译文/章节结构对新正文可能已经
# 错位，宁可空着让 AI 重写，也不能让「新正文配旧导读」留在页面上。
# （summary/digest/reason/score/category/topics 不在列：process_article 会
# 无条件重算并覆盖它们；这里列的是它「有旧值就沿用/跳过」的字段。）
_REFETCH_DERIVED_FIELDS = (
    "title_zh", "title_en", "digest_zh", "digest_en", "content_zh",
    "body_sections", "body_sections_zh", "brief_zh", "tags",
)


def _refetch_set(article_id: int, state: str, reason: str | None = None) -> None:
    with _refetch_lock:
        _refetch_jobs[article_id] = {"state": state, "reason": reason, "done_at": time.time()}


def _run_refetch(article_id: int, settings: Settings | None) -> None:
    """后台任务：重抓原文 → 作废派生字段 → 按当前配置重新跑一遍 AI 处理。

    每一步都收口到 ``_refetch_set``，任何失败都要让轮询端看到原因 ——
    后台任务里没人接异常，吞掉的话前端会永远转圈。
    """
    from app.scheduler import _llm_client  # 延迟导入：scheduler 也会延迟导入 web 层

    if settings is None:
        _refetch_set(article_id, "failed", "设置未加载，无法重建 AI 客户端")
        return
    try:
        with session_scope() as session:
            article = session.get(Article, article_id)
            if article is None:
                _refetch_set(article_id, "failed", "文章不存在")
                return
            link = article.link

        # 1. 重抓原文（正文 + 配图锚点一次拿全；抓不到时返回空串/空表）
        text, images = fetch_article_document(link)
        if not text:
            _refetch_set(article_id, "failed", "原文抓取失败：站点不可达，或页面里提取不出正文")
            return

        # 2. 写回原文并作废派生字段（同一事务，避免出现半新半旧的状态）
        with session_scope() as session:
            article = session.get(Article, article_id)
            if article is None:
                _refetch_set(article_id, "failed", "文章不存在")
                return
            article.content_full = text
            # 旧锚点指的是旧正文的段落位置，正文一换就全部失效 —— 先清成
            # NULL（下轮补图任务还会来），再让 _store_body_anchors 按新正文重算。
            article.body_images = None
            _store_body_anchors(
                article, images, referer=link or "", media_dir=media_dir_for(settings.db_file)
            )
            for field in _REFETCH_DERIVED_FIELDS:
                setattr(article, field, None)

        # 3. 按当前设置页配置现建 LLM 客户端重新处理。source_lang 用 auto：
        # 重抓后的正文语种可能与原来不同，按信源写死的语言走会翻错方向。
        client = _llm_client(settings)
        try:
            with session_scope() as session:
                article = session.get(Article, article_id)
                if article is None:
                    _refetch_set(article_id, "failed", "文章不存在")
                    return
                status = process_article(session, article, client, settings, source_lang=LANG_AUTO)
        finally:
            client.close()

        if status == STATUS_PROCESSED:
            _refetch_set(article_id, "ok")
            return
        with session_scope() as session:
            article = session.get(Article, article_id)
            reason = (article.degraded_reason if article else None) or "AI 处理失败"
        # 处理失败只是这一轮没成：文章已标 failed，调度器会把降级文章放回
        # pending 自动重试；这里如实报告即可。
        _refetch_set(article_id, "failed", reason)
    except Exception as exc:  # 后台任务兜底：状态必须落地，前端才不会永远转圈
        _refetch_set(article_id, "failed", f"重新获取失败：{exc}"[:300])


@api_router.post("/articles/{article_id}/refetch")
def refetch_article(
    article_id: IdParam, background_tasks: BackgroundTasks, request: Request,
    session: Session = Depends(get_session),
) -> dict[str, str]:
    """重新抓取这篇文章的原文并按当前配置重跑 AI 处理（后台执行）。"""
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="文章不存在")
    with _refetch_lock:
        running = _refetch_jobs.get(article_id, {}).get("state") == "running"
        if not running:
            _refetch_jobs[article_id] = {"state": "running", "reason": None, "done_at": time.time()}
    if running:
        # 已经有一份在跑：不重复起任务，让前端接着轮询即可
        return {"state": "running"}
    # 设置用 app.state 里**运行中的那一份**（设置页改完就地生效），不传快照
    background_tasks.add_task(
        _run_refetch, article_id, getattr(request.app.state, "settings", None)
    )
    return {"state": "running"}


@api_router.get("/articles/{article_id}/refetch-status")
def refetch_status(article_id: IdParam) -> dict[str, Any]:
    """重新获取任务的当前状态：idle（没跑过）/ running / ok / failed（带原因）。"""
    with _refetch_lock:
        job = _refetch_jobs.get(article_id)
        if job is None:
            return {"state": "idle", "reason": None}
        return {"state": job["state"], "reason": job.get("reason")}


@api_router.get("/reports", response_model=list[ReportOut])
def list_reports(limit: int = 100, session: Session = Depends(get_session)):
    statement = select(DailyReport).order_by(DailyReport.date.desc()).limit(max(1, min(limit, 500)))
    return list(session.execute(statement).scalars())


@api_router.get("/reports/{date}", response_model=ReportDetail)
def get_report(date: str, session: Session = Depends(get_session)):
    report = session.execute(select(DailyReport).where(DailyReport.date == date)).scalar_one_or_none()
    if report is None:
        raise HTTPException(status_code=404, detail=f"{date} 没有日报")
    return report


@api_router.get("/health", response_model=HealthOut)
def health(request: Request, session: Session = Depends(get_session)) -> HealthOut:
    settings = getattr(request.app.state, "settings", None)
    return HealthOut(
        status="ok",
        version=__version__,
        database="ok",
        sources=session.query(Source).count(),
        articles=session.query(Article).count(),
        with_images=count_with_images(session),
        with_full_text=count_with_full_text(session),
        reports=session.query(DailyReport).count(),
        research_topics=list(getattr(settings, "research_topics", []) or []),
    )
