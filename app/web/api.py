"""JSON API 路由。"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import Path as PathParam
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import __version__
from app.db import get_session
from app.fetcher.content import count_with_full_text
from app.fetcher.images import count_with_images
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
