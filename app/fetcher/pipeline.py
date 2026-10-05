"""抓取主流程：拉取所有启用的信源 → 去重 → 入库（status=pending）。"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db import session_scope
from app.fetcher.dedup import is_duplicate
from app.fetcher.rss import fetch_feed
from app.models import Article, Source
from app.utils.logger import get_logger
from app.utils.text import extract_images, now_local, strip_html, truncate, unescape_text

log = get_logger(__name__)

MAX_CONTENT_CHARS = 20_000

# pubDate 最多允许比「现在」晚多久。留一点余量是为了容忍源与本机几秒的时钟
# 偏差，以及 RSS 里常见的「整点时间」；超出这个量就不是偏差而是错的。
FUTURE_TOLERANCE = timedelta(hours=6)
Fetcher = Callable[..., list[dict[str, Any]]]


def _store_images(urls: list[str]) -> str | None:
    """图片地址以 JSON 数组存进 image_urls。"""
    return json.dumps(urls, ensure_ascii=False) if urls else None


def _enabled_sources(session: Session) -> list[Source]:
    """启用中的源。``deleted=1`` 的源不参与抓取（软删除，见 models.Source）。"""
    return list(
        session.execute(
            select(Source)
            .where(Source.enabled == 1, Source.deleted == 0)
            .order_by(Source.id)
        ).scalars()
    )


def _safe_link(raw: str) -> str:
    """只接受 http/https 链接；其它协议一律当成没有链接。

    Jinja 会把引号转义，但**不会中和 URL 的协议**。所以一个 ``javascript:``
    的 link 会被原样渲染进详情页的 ``href`` —— 恶意或被攻陷的 feed 因此能在
    本站自己的源上执行脚本，而本站能读到设置页。在入库这一层挡掉，比在
    渲染时补救可靠：渲染路径有五处，入口只有一个。
    """
    link = (raw or "").strip()
    if not link:
        return ""
    scheme = link.split(":", 1)[0].casefold() if ":" in link else ""
    if scheme not in ("http", "https"):
        return ""
    return link


def _admit(
    items: list[dict[str, Any]],
    *,
    max_age_days: int,
    max_items: int,
    min_content_chars: int,
    now: datetime,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """筛掉不该入库的条目，并返回 (保留的条目, 各类丢弃计数)。

    三道闸门：
    - ``max_age_days``：太旧的直接丢（全量归档型 feed 的救命开关）
    - ``max_items``：单源单次条数上限，成本闸门
    - ``min_content_chars``：只有标题没有正文的条目，摘要必然是标题复读
    """
    cutoff = now - timedelta(days=max_age_days) if max_age_days > 0 else None
    kept: list[dict[str, Any]] = []
    dropped = {"stale": 0, "over_cap": 0, "no_content": 0, "no_date": 0, "future": 0}
    for item in items:
        published = item.get("published_at")
        if published is not None and published > now + FUTURE_TOLERANCE:
            # pubDate 落在未来：要么源把本地时间当成了 GMT（实测 InfoQ 的
            # CST/GMT 混用会让文章凭空提前 7.5 小时），要么时钟坏了。
            # 照单全收就会算进「未来那一天」的日报 —— 而那一天还没发生，
            # 于是这篇文章在当期日报里消失、明天又冒出来。
            # 钳到「现在」，宁可同一天偏早，也不要落到错误的一天。
            log.info("pubDate 在未来（%s > %s），已钳到当前时间", published, now)
            item["published_at"] = now
            published = now
            dropped["future"] += 1
        if published is None:
            # 没有 pubDate 就没法判断新旧，只能收下；单独计数，方便发现「无日期源」。
            dropped["no_date"] += 1
        if cutoff is not None and published is not None and published < cutoff:
            dropped["stale"] += 1
            continue
        content = strip_html(str(item.get("content") or ""))
        if min_content_chars > 0 and len(content) < min_content_chars:
            dropped["no_content"] += 1
            continue
        if len(kept) >= max_items > 0:
            dropped["over_cap"] += 1
            continue
        kept.append(item)
    return kept, dropped


def run_fetch_pipeline(
    *,
    session_factory: sessionmaker[Session] | None = None,
    fetcher: Fetcher = fetch_feed,
    timeout: float = 15.0,
    retries: int = 2,
    user_agent: str | None = None,
    dedup_window: int = 500,
    max_age_days: int = 14,
    max_items_per_source: int = 60,
    min_content_chars: int = 0,
) -> dict[str, Any]:
    """抓取所有启用的信源并入库。

    返回 ``{fetched, new, duplicated, stale, capped, no_content, failed, empty_sources, details}``；
    单个信源失败只记日志、不影响整体；抓到 0 条的源会被标成 ``empty`` 以便发现死源。
    """
    stats: dict[str, Any] = {
        "fetched": 0,
        "new": 0,
        "duplicated": 0,
        "stale": 0,
        "capped": 0,
        "no_content": 0,
        "no_date": 0,
        "future": 0,
        "bad_link": 0,
        "failed": 0,
        "empty_sources": 0,
        "details": [],
    }

    def _work(session: Session) -> None:
        now = now_local()
        for source in _enabled_sources(session):
            kwargs: dict[str, Any] = {"timeout": timeout, "retries": retries}
            if user_agent:
                kwargs["user_agent"] = user_agent
            try:
                items = fetcher(source.url, **kwargs)
            except Exception as exc:  # 单源失败不阻断整体
                stats["failed"] += 1
                stats["details"].append({"source": source.name, "status": "failed", "error": str(exc)[:200]})
                log.warning("信源抓取失败，跳过：%s（%s）", source.name, exc)
                continue

            stats["fetched"] += len(items)
            if not items:
                stats["empty_sources"] += 1
                stats["details"].append({"source": source.name, "status": "empty", "items": 0, "new": 0})
                log.warning("信源一条内容都没抓到（地址可能已失效）：%s <%s>", source.name, source.url)
                continue

            admitted, dropped = _admit(
                items,
                max_age_days=max_age_days,
                max_items=max_items_per_source,
                min_content_chars=min_content_chars,
                now=now,
            )
            stats["stale"] += dropped["stale"]
            stats["capped"] += dropped["over_cap"]
            stats["no_content"] += dropped["no_content"]
            stats["no_date"] += dropped["no_date"]
            stats["future"] += dropped["future"]

            added = 0
            for item in admitted:
                link = _safe_link(str(item.get("link") or ""))
                title = str(item.get("title") or "").strip()
                if not link or not title:
                    stats["bad_link"] += 1
                    continue
                if is_duplicate(session, link, title, window=dedup_window):
                    stats["duplicated"] += 1
                    continue
                # 用 savepoint 兜住唯一约束冲突：link 撞车（定时任务和手动抓取同时插同一条）
                # 只该丢这一条，不该把整批回滚掉。
                try:
                    with session.begin_nested():
                        raw_html = str(item.get("content") or "")
                        session.add(
                            Article(
                                source_id=source.id,
                                title=unescape_text(title)[:500],
                                link=link[:1000],
                                content=truncate(strip_html(raw_html), MAX_CONTENT_CHARS),
                                published_at=item.get("published_at") or now,
                                # 页内预览用的配图：RSS 正文里通常已经带了 <img>，
                                # 以前入库时被 strip_html 一起丢掉了
                                image_urls=_store_images(extract_images(raw_html, base_url=link)),
                                status="pending",
                                created_at=now,
                            )
                        )
                        session.flush()
                except IntegrityError:
                    stats["duplicated"] += 1
                    log.debug("link 撞车已跳过：%s", link[:120])
                    continue
                added += 1
                stats["new"] += 1
            session.flush()
            stats["details"].append(
                {
                    "source": source.name,
                    "status": "ok",
                    "items": len(items),
                    "admitted": len(admitted),
                    "new": added,
                    **dropped,
                }
            )

    if session_factory is None:
        with session_scope() as session:
            _work(session)
    else:
        with session_factory() as session:
            _work(session)
            session.commit()
    return stats
