"""文本与时间处理：清洗 RSS 正文、截断、标题归一化、标签切分、本地时间。

时间约定：全库统一存**北京时间（naive）**，这样「日报按天分组」跟用户看到的一天一致。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

LOCAL_TZ = timezone(timedelta(hours=8))  # 北京时间

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_NORMALIZE_RE = re.compile(r"[^\w\u4e00-\u9fff]+")
_TAG_SPLIT_RE = re.compile(r"[,，、;；]")


def now_local() -> datetime:
    """当前北京时间（naive，便于直接入库与比较）。"""
    return datetime.now(LOCAL_TZ).replace(tzinfo=None)


def to_local(value: datetime | None) -> datetime | None:
    """带时区的时间 → 北京时间（naive）；已是 naive 的原样返回。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(LOCAL_TZ).replace(tzinfo=None)


def strip_html(text: str | None) -> str:
    """去掉 RSS 正文里的标签与多余空白。"""
    if not text:
        return ""
    plain = _TAG_RE.sub(" ", text)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"')):
        plain = plain.replace(entity, char)
    return _WS_RE.sub(" ", plain).strip()


def truncate(text: str | None, limit: int) -> str:
    """按字符数截断（中文友好），超出加省略号。"""
    if not text:
        return ""
    stripped = text.strip()
    return stripped if len(stripped) <= limit else stripped[:limit].rstrip() + "…"


def normalize_title(title: str | None) -> str:
    """标题归一化：小写、去标点、压缩空白 —— 用于相似度去重。"""
    if not title:
        return ""
    return _WS_RE.sub(" ", _NORMALIZE_RE.sub(" ", title.casefold())).strip()


def split_tags(raw: str | None, limit: int = 5) -> list[str]:
    """把 LLM 返回的标签串切成列表。"""
    if not raw:
        return []
    return [part.strip() for part in _TAG_SPLIT_RE.split(raw) if part.strip()][:limit]
