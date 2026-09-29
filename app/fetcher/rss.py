"""RSS 抓取：拉取、解析、归一化。失败一律抛出，由上层记录日志并跳过。"""

from __future__ import annotations

import time
from calendar import timegm
from datetime import UTC, datetime
from typing import Any

import feedparser
import httpx

from app.utils.logger import get_logger
from app.utils.text import to_local

log = get_logger(__name__)

DEFAULT_USER_AGENT = "ShowMeTheMoney/0.1"


class FetchError(RuntimeError):
    """单个信源抓取失败。"""


def _entry_datetime(entry: Any) -> datetime | None:
    """feedparser 的 published/updated 结构 → 北京时间（naive）。"""
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not parsed:
        return None
    try:
        return to_local(datetime.fromtimestamp(timegm(parsed), tz=UTC))
    except (OverflowError, ValueError):  # pragma: no cover - 极端时间戳
        return None


def parse_feed(raw: bytes | str, *, source: str = "") -> list[dict[str, object]]:
    """解析 RSS/Atom 内容为统一结构（纯函数，便于测试）。"""
    parsed = feedparser.parse(raw)
    items: list[dict[str, object]] = []
    for entry in parsed.entries:
        link = (getattr(entry, "link", "") or "").strip()
        title = (getattr(entry, "title", "") or "").strip()
        if not link or not title:
            continue
        content = ""
        if getattr(entry, "content", None):
            content = str(entry.content[0].get("value", ""))
        if not content:
            content = str(getattr(entry, "summary", "") or "")
        items.append(
            {
                "title": title,
                "link": link,
                "content": content,
                "published_at": _entry_datetime(entry),
                "source": source,
            }
        )
    return items


def fetch_feed(
    url: str,
    timeout: float = 15.0,
    *,
    retries: int = 2,
    user_agent: str = DEFAULT_USER_AGENT,
    client: httpx.Client | None = None,
) -> list[dict[str, object]]:
    """抓取并解析一个 RSS 源（带超时 + 指数退避重试）。

    重试耗尽后抛 ``FetchError``，由 pipeline 记录并跳过该源。
    """
    last_error: Exception | None = None
    own_client = client is None
    http = client or httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": user_agent})
    try:
        for attempt in range(retries + 1):
            try:
                response = http.get(url)
                response.raise_for_status()
                return parse_feed(response.content, source=url)
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                last_error = exc
                log.warning("抓取失败（第 %d 次）：%s —— %s", attempt + 1, url, exc)
                if attempt < retries:
                    time.sleep(min(2.0**attempt, 4.0))
    finally:
        if own_client:
            http.close()
    raise FetchError(f"{url} 抓取失败：{last_error}")
