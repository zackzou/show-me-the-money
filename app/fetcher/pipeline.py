"""抓取主流程：拉取所有启用的信源 → 去重 → 入库（status=pending）。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db import session_scope
from app.fetcher.dedup import is_duplicate
from app.fetcher.rss import fetch_feed
from app.models import Article, Source
from app.utils.logger import get_logger
from app.utils.text import now_local, strip_html, truncate, unescape_text

log = get_logger(__name__)

MAX_CONTENT_CHARS = 20_000
Fetcher = Callable[..., list[dict[str, Any]]]


def _enabled_sources(session: Session) -> list[Source]:
    return list(session.execute(select(Source).where(Source.enabled == 1).order_by(Source.id)).scalars())


def run_fetch_pipeline(
    *,
    session_factory: sessionmaker[Session] | None = None,
    fetcher: Fetcher = fetch_feed,
    timeout: float = 15.0,
    retries: int = 2,
    user_agent: str | None = None,
    dedup_window: int = 500,
) -> dict[str, Any]:
    """抓取所有启用的信源并入库。

    返回 ``{fetched, new, duplicated, failed, details}``；单个信源失败只记日志、不影响整体。
    """
    stats: dict[str, Any] = {"fetched": 0, "new": 0, "duplicated": 0, "failed": 0, "details": []}

    def _work(session: Session) -> None:
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

            added = 0
            for item in items:
                stats["fetched"] += 1
                link = str(item.get("link") or "").strip()
                title = str(item.get("title") or "").strip()
                if not link or not title:
                    continue
                if is_duplicate(session, link, title, window=dedup_window):
                    stats["duplicated"] += 1
                    continue
                session.add(
                    Article(
                        source_id=source.id,
                        title=unescape_text(title)[:500],
                        link=link[:1000],
                        content=truncate(strip_html(str(item.get("content") or "")), MAX_CONTENT_CHARS),
                        published_at=item.get("published_at") or now_local(),
                        status="pending",
                        created_at=now_local(),
                    )
                )
                added += 1
                stats["new"] += 1
            session.flush()
            stats["details"].append({"source": source.name, "status": "ok", "items": len(items), "new": added})

    if session_factory is None:
        with session_scope() as session:
            _work(session)
    else:
        with session_factory() as session:
            _work(session)
            session.commit()
    return stats
