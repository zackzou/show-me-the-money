"""文本与时间处理：清洗 RSS 正文、抽图、截断、标题归一化、标签切分、本地时间。

时间约定：全库统一存**北京时间（naive）**，这样「日报按天分组」跟用户看到的一天一致。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from html import unescape
from urllib.parse import urljoin

LOCAL_TZ = timezone(timedelta(hours=8))  # 北京时间

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_NORMALIZE_RE = re.compile(r"[^\w\u4e00-\u9fff]+")
_TAG_SPLIT_RE = re.compile(r"[,，、;；]")
_MD_BOLD_RE = re.compile(r"(\*\*|__)")
_MD_LINE_RE = re.compile(r"(?m)^\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s*)")
_IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
_ATTR_RE = re.compile(r"""(\w[\w:-]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")

# 这些图片不是内容：追踪像素、占位图、表情图、1x1 透明图
_IMAGE_NOISE_RE = re.compile(
    r"(pixel|spacer|blank|placeholder|emoji|gravatar|avatar|badge|logo|icon|ads?[-_/]|doubleclick|scorecardresearch)",
    re.I,
)
_IMAGE_EXT_RE = re.compile(r"\.(jpe?g|png|gif|webp|avif)(?:$|[?#])", re.I)


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
    plain = unescape_text(plain)
    return _WS_RE.sub(" ", plain).strip()


def unescape_text(text: str | None) -> str:
    """把 feed 里残留的 HTML 实体（&mdash; &#8217; &amp; 等）还原成字符。"""
    if not text:
        return ""
    return unescape(text)


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


def strip_markdown(text: str | None) -> str:
    """去掉模型偶尔带出来的 Markdown 标记。

    摘要是在网页/卡片里直接展示的纯文本，实测模型经常会写成
    ``**结论：** ……`` 或 ``- 要点``，原样显示就是一屏星号。
    """
    if not text:
        return ""
    plain = _MD_LINE_RE.sub("", text)
    plain = _MD_BOLD_RE.sub("", plain)
    return _WS_RE.sub(" ", plain).strip()


def _attr(tag: str, name: str) -> str:
    """从 `<img ...>` 标签里取属性值。"""
    for match in _ATTR_RE.finditer(tag):
        if match.group(1).lower() == name:
            return unescape(match.group(2) or match.group(3) or match.group(4) or "").strip()
    return ""


def _best_srcset_url(srcset: str) -> str:
    """从 `srcset` 里挑最大的那个候选（按 w 描述子排序）。"""
    best_url, best_weight = "", -1
    for candidate in srcset.split(","):
        parts = candidate.strip().split()
        if not parts:
            continue
        url = parts[0]
        weight = 0
        if len(parts) > 1 and parts[1].lower().endswith("w"):
            try:
                weight = int(parts[1][:-1])
            except ValueError:
                weight = 0
        elif len(parts) > 1 and parts[1].lower().endswith("x"):
            try:
                weight = int(float(parts[1][:-1]) * 1000)
            except ValueError:
                weight = 0
        if weight > best_weight:
            best_url, best_weight = url, weight
    return best_url


def extract_images(html_text: str | None, *, base_url: str = "", limit: int = 6) -> list[str]:
    """从 RSS 正文 HTML 里抽出配图地址（按出现顺序，去重、滤噪）。

    这是「页内直接看图」的数据来源：RSS 的 ``content``/``description`` 里通常已经带了
    ``<img>``，只是以前入库时被 ``strip_html`` 一起丢掉了。
    """
    if not html_text:
        return []
    urls: list[str] = []
    seen: set[str] = set()
    for tag in _IMG_RE.findall(html_text):
        raw = (
            _attr(tag, "src")
            or _attr(tag, "data-src")
            or _attr(tag, "data-original")
            or _attr(tag, "data-lazy-src")
            or _best_srcset_url(_attr(tag, "srcset") or _attr(tag, "data-srcset"))
        )
        if not raw or raw.startswith("data:"):
            continue
        url = urljoin(base_url, raw) if base_url else raw
        if url in seen:
            continue
        if _IMAGE_NOISE_RE.search(url):
            continue
        seen.add(url)
        urls.append(url)
        if len(urls) >= limit:
            break
    return urls
