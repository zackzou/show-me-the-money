"""配图补齐：RSS 没给图时，去文章页抓 og:image。

为什么需要：实测默认信源的 description 多半是纯文本（量子位正文中位数 15 字、
InfoQ 7 字），``<img>`` 抽不到；而这些站点的文章页基本都有 ``og:image``。
不补图，页面就还是只能靠跳原站看图。

约束（都是成本 / 礼貌 / 稳定性考虑）：
- 每轮只处理**有限篇数**，不追着全库跑
- 只给「相关且还没图」的文章补，不给被判为不相关的花流量
- 单篇失败只记日志，不影响其它文章
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Article
from app.utils.logger import get_logger

log = get_logger(__name__)

DEFAULT_UA = "ShowMeTheMoney/0.2 (+https://github.com/zackzou/show-me-the-money)"
MAX_HTML_BYTES = 600_000

# og:image / twitter:image / link[rel=image_src] / 常见的 <img src> 兜底
_META_IMAGE_RE = re.compile(
    r"""<meta[^>]+(?:property|name)\s*=\s*["'](?:og:image(?::secure_url|:url)?|twitter:image(?::src)?)["'][^>]*>""",
    re.I,
)
_LINK_IMAGE_RE = re.compile(
    r"""<link[^>]+rel\s*=\s*["'][^"']*image_src[^"']*["'][^>]*>""", re.I
)
_FIRST_IMG_RE = re.compile(r"""<img[^>]+src\s*=\s*["']([^"']+)["']""", re.I)
_ATTR_RE = re.compile(r"""(\w[\w:-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_BAD_IMAGE_HINTS = ("logo", "avatar", "icon", "placeholder", "blank", "spacer", "qrcode")

# 同一信源里同一张图出现这么多次，就当它是站点通用图
SITE_DEFAULT_THRESHOLD = 3


def _attr(tag: str, name: str) -> str:
    for match in _ATTR_RE.finditer(tag):
        if match.group(1).lower() == name:
            return (match.group(2) or match.group(3) or "").strip()
    return ""


def _looks_like_content_image(url: str) -> bool:
    low = url.lower()
    if any(hint in low for hint in _BAD_IMAGE_HINTS):
        return False
    return low.endswith((".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif")) or "image" in low


def looks_like_site_default(url: str, same_source: dict[str, int], *, threshold: int = SITE_DEFAULT_THRESHOLD) -> bool:
    """同一个图地址在同一信源下反复出现 → 多半是站点通用头图 / 默认头像，不是内容图。

    实测：量子位的 ``og:image`` 永远是 ``qbitai-logo-1.png``，InfoQ 是同一张默认剪影。
    这种图挂上去只会让人觉得「配图和内容没关系」，不如不放。
    """
    return same_source.get(url, 0) >= threshold


def extract_og_image(html_text: str, *, base_url: str = "") -> str | None:
    """从文章页 HTML 里取出首图地址。取不到就返回 ``None``（不猜）。"""
    def _abs(url: str) -> str:
        # 注意方向：URL.join 是「用 base 去解析 relative」，即 base.join(url)
        return str(httpx.URL(base_url).join(url)) if base_url else url

    for tag in _META_IMAGE_RE.findall(html_text) + _LINK_IMAGE_RE.findall(html_text):
        url = _attr(tag, "content") or _attr(tag, "href")
        if url and _looks_like_content_image(url):
            return _abs(url)
    for url in _FIRST_IMG_RE.findall(html_text):
        if _looks_like_content_image(url):
            return _abs(url)
    return None


def fetch_og_image(url: str, *, timeout: float = 10.0, client: httpx.Client | None = None) -> str | None:
    """抓一篇文章的首图地址；任何异常都吞掉（补图是锦上添花）。"""
    owns = client is None
    http = client or httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": DEFAULT_UA},
    )
    try:
        response = http.get(url)
        if response.status_code >= 400:
            return None
        raw = response.content[:MAX_HTML_BYTES]
        found = extract_og_image(raw.decode("utf-8", errors="ignore"), base_url=str(response.url))
        return str(found) if found else None
    except (httpx.HTTPError, ValueError):
        return None
    finally:
        if owns:
            http.close()


def backfill_images(
    session: Session,
    *,
    limit: int = 20,
    timeout: float = 10.0,
    only_today: bool = True,
) -> dict[str, Any]:
    """给「相关但没有配图」的文章补首图，返回统计。"""
    statement = (
        select(Article)
        .where(
            Article.image_urls.is_(None),
            Article.relevance == 1,
            Article.link.notlike("http://localhost%"),
        )
        .order_by(Article.published_at.desc(), Article.id.desc())
        .limit(limit)
    )
    rows = list(session.execute(statement).scalars())
    stats = {"candidates": len(rows), "filled": 0, "not_found": 0, "site_default": 0, "failed": 0}
    if not rows:
        return stats

    # 先数一遍每个信源里各图片地址已出现多少次，用来识别站点通用图
    known: dict[str, dict[str, int]] = {}
    for url, source_id in session.execute(select(Article.image_urls, Article.source_id)):
        if not url or source_id is None:
            continue
        bucket = known.setdefault(str(source_id), {})
        for one in _safe_list(url):
            bucket[one] = bucket.get(one, 0) + 1

    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": DEFAULT_UA},
        limits=httpx.Limits(max_connections=6),
    ) as client:
        for article in rows:
            if article.image_urls is not None:
                continue  # 上一轮已经补上了
            try:
                image = fetch_og_image(article.link, timeout=timeout, client=client)
            except Exception as exc:  # 兜底：补图失败绝不能影响抓取/处理
                stats["failed"] += 1
                log.debug("补图失败：%s（%r）", article.link[:80], exc)
                continue
            same_source = known.get(str(article.source_id), {})
            if image and looks_like_site_default(image, same_source):
                stats["site_default"] += 1
                image = None
            if image:
                article.image_urls = json.dumps([image], ensure_ascii=False)
                same_source[image] = same_source.get(image, 0) + 1
                stats["filled"] += 1
            else:
                # 用空数组标记「查过了没有」，避免每轮都重查同一篇
                article.image_urls = "[]"
                stats["not_found"] += 1
    session.flush()
    if stats["filled"]:
        log.info("补齐配图 %d 篇（候选 %d 篇、无图 %d 篇）", stats["filled"], stats["candidates"], stats["not_found"])
    return stats


def _safe_list(raw: str) -> list[str]:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(x) for x in parsed if isinstance(x, str)]


def count_with_images(session: Session) -> int:
    """库里有配图的文章数（健康检查用）。"""
    return int(
        session.execute(
            select(func.count(Article.id)).where(Article.image_urls.isnot(None), Article.image_urls != "[]")
        ).scalar()
        or 0
    )