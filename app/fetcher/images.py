"""配图挑选：从文章页里挑**一张**最合适的内容配图。

踩过的坑（都是实测踩出来的，不是假想）：

1. **只认 URL 后缀会漏掉绝大多数真图。**
   量子位正文里的图片全是 ``p3-sign.toutiaoimg.com/tos-cn-i-axegupay5k/<hash>`` 这种
   CDN 地址 —— 没有扩展名、路径里也没有 "image" 字样。原来按后缀白名单筛，
   结果 30 张真图一张都没留下。

2. **只按 URL 关键词过滤会漏掉作者头像。**
   量子位的头图是 ``.../imagesnew/head.jpg``，URL 里没有 logo/avatar/icon，
   但它的 ``class`` 是 ``avatar avatar-200``。原来只看 URL，于是把作者头像
   当成了文章配图挂上去。

3. **``og:image`` 常常是站点 logo，不是内容图。**
   量子位所有文章的 ``og:image`` 都是 ``qbitai-logo-1.png``。所以正文大图
   要**优先于** og:image，而不是反过来。

4. **声明尺寸不可信，真图常常没写 width。**
   量子位正文那 30 张图一个 width 都没有。只能真的去量。

所以这里的做法是：先按 class / URL / 声明尺寸粗筛，再对剩下的候选
**真实探测像素尺寸**，取第一张够大（宽度与高度都达到阈值）的。
探测只发 Range 请求读文件头，不下载整张图。
"""

from __future__ import annotations

import html as html_lib
import json
import re
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.fetcher.media_store import (
    download_image,
    measure,
    parse_image_size,
    read_media_map,
    save_image,
)
from app.models import Article
from app.utils.logger import get_logger

log = get_logger(__name__)

DEFAULT_UA = "ShowMeTheMoney/0.2 (+https://github.com/zackzou/show-me-the-money)"
MAX_HTML_BYTES = 600_000
# 探测图片尺寸时只读文件头，够解析 JPEG/PNG/WebP 的尺寸字段
PROBE_BYTES = 64 * 1024
PROBE_TIMEOUT = 8.0
# 一次最多探测几张候选图，避免在图片多的页面上发几十个请求
MAX_PROBES = 5

# 排除用的关键词，分两级。
#
# 为什么分级：早期版本对整个 URL / class / alt 做子串匹配，结果把量子位正文里
# 30 张真图全部误杀 —— 那些图的 alt 文案里出现了 "signature"（原文是访谈里
# 提到的手写签名图），而 alt 是自然语言，里面的词不能当排除依据。
#
# STRICT：这些词出现在 URL / class 的任何一段里都说明是图标类资源，
#         不会出现在正经内容图上。
# STEM：  这些词是常见英文单词（head / share / author…），只有当**整个文件名**
#         就是这个词时才排除，否则 "head-phones.jpg"（耳机图）会被误杀。
_STRICT_HINTS = frozenset(
    {
        "logo", "favicon", "qrcode", "qr", "gravatar", "spacer", "placeholder",
        "sprite", "blank", "pixel", "icon", "avatar", "emoji", "spinner",
        "loader", "watermark", "badge", "button", "ads", "advert",
    }
)
_STEM_HINTS = frozenset(
    {"head", "header", "banner", "bg", "background", "share", "shared", "social",
     "author", "profile", "signature", "arrow", "default", "generic", "logo"}
)
_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")

# 认定为「内容图」的最小像素尺寸。低于这个基本是图标、头像、缩略图。
MIN_WIDTH_PX = 400
MIN_HEIGHT_PX = 220
# <img> 上声明的宽度低于这个值直接放弃，不必浪费探测请求
MIN_DECLARED_WIDTH = 300

# 明显是数据 URI / 追踪像素
_DATA_URI_RE = re.compile(r"^data:", re.I)
_META_IMAGE_RE = re.compile(
    r"""<meta[^>]+(?:property|name)\s*=\s*["'](?:og:image(?::secure_url|:url)?|twitter:image(?::src)?)["'][^>]*>""",
    re.I,
)
_LINK_IMAGE_RE = re.compile(r"""<link[^>]+rel\s*=\s*["'][^"']*image_src[^"']*["'][^>]*>""", re.I)
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.I)
_SRC_ATTR_RE = re.compile(
    r"""\b(?:src|data-src|data-original|data-lazy-src|data-echo)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""",
    re.I,
)
_ATTR_RE = re.compile(r"""(\w[\w:-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_EXT_RE = re.compile(r"\.(?:jpe?g|png|webp|avif|gif)(?:$|[?#])", re.I)

# 同一信源里同一张图出现这么多次，就当它是站点通用图
SITE_DEFAULT_THRESHOLD = 3


def _attr(tag: str, name: str) -> str:
    for match in _ATTR_RE.finditer(tag):
        if match.group(1).lower() == name:
            return (match.group(2) or match.group(3) or "").strip()
    return ""


def src_of(tag: str) -> str:
    for match in _SRC_ATTR_RE.finditer(tag):
        url = (match.group(1) or match.group(2) or match.group(3) or "").strip()
        if url:
            return url
    return ""


def _tokens(*values: str) -> set[str]:
    """把 URL / class / id 切成小写 token 集合，并顺带记下去掉尾部数字的变体。

    为什么要变体：``class="avatar avatar-200"`` 里的 ``avatar200`` 也要能命中。
    """
    out: set[str] = set()
    for value in values:
        for piece in _TOKEN_SPLIT_RE.split((value or "").lower()):
            if not piece:
                continue
            out.add(piece)
            trimmed = piece.rstrip("0123456789")
            if trimmed:
                out.add(trimmed)
    return out


def _stem_of(url: str) -> str:
    """取 URL 末段的文件名主干（去掉扩展名），用于 EXACT 匹配。"""
    tail = (url or "").split("?")[0].split("#")[0].rstrip("/").rsplit("/", 1)[-1]
    return tail.rsplit(".", 1)[0].lower() if "." in tail else tail.lower()


def looks_like_junk_image(*, url: str, tag: str = "") -> bool:
    """判断是不是图标、头像、占位图、站点通用图。

    看 URL、``<img>`` 的 class 与 id，**不看 alt** —— alt 是自然语言描述，
    里面出现 "signature"、"share" 这类词是内容的一部分，不能拿来当排除依据
    （这个误伤在量子位正文 30 张真图上翻过车）。
    """
    if not url or _DATA_URI_RE.match(url):
        return True
    marks = _tokens(url, _attr(tag, "class"), _attr(tag, "id"))
    if marks & _STRICT_HINTS:
        return True
    if _stem_of(url) in _STEM_HINTS:
        return True
    if tag:
        # <img> 声明的宽度太小 → 头像 / 缩略图，不必再花探测请求
        for key in ("width", "data-width"):
            raw = _attr(tag, key)
            if raw.isdigit() and int(raw) < MIN_DECLARED_WIDTH:
                return True
    return False


def looks_like_site_default(url: str, same_source: dict[str, int], *, threshold: int = SITE_DEFAULT_THRESHOLD) -> bool:
    """同一个图地址在同一信源下反复出现 → 多半是站点通用头图 / 默认头像。

    实测：量子位的 ``og:image`` 永远是 ``qbitai-logo-1.png``，InfoQ 是同一张默认剪影。
    这种图挂上去只会让人觉得「配图和内容没关系」，不如不放。
    """
    return same_source.get(url, 0) >= threshold


# ── 像素尺寸探测：只读文件头，不下载整张图 ─────────────────────────────────


def probe_image_size(
    url: str, *, client: httpx.Client | None = None, timeout: float = PROBE_TIMEOUT
) -> tuple[int, int] | None:
    """用 Range 请求读图片文件头，量出真实像素尺寸。任何异常都吞掉。"""
    owns = client is None
    http = client or httpx.Client(
        timeout=timeout, follow_redirects=True, headers={"User-Agent": DEFAULT_UA}
    )
    try:
        response = http.get(url, headers={"Range": f"bytes=0-{PROBE_BYTES - 1}"})
        if response.status_code >= 400:
            return None
        return parse_image_size(response.content[:PROBE_BYTES])
    except (httpx.HTTPError, ValueError):
        return None
    finally:
        if owns:
            http.close()


# ── 从 HTML 里挑图 ─────────────────────────────────────────────────────────


def collect_image_candidates(html_text: str, *, base_url: str = "") -> list[str]:
    """按出现顺序收集候选图：正文 <img> 在前，og:image 在后（兜底）。

    正文图优先是因为 og:image 在中文站点上非常容易是站点 logo。
    """
    def _abs(url: str) -> str:
        # 注意方向：URL.join 是「用 base 去解析 relative」，即 base.join(url)
        return str(httpx.URL(base_url).join(url)) if base_url else url

    candidates: list[str] = []
    for tag in _IMG_TAG_RE.findall(html_text):
        # 属性值里的 &amp; 必须还原成 &，否则拿到的 URL 直接 404
        url = html_lib.unescape(src_of(tag))
        if not url or looks_like_junk_image(url=url, tag=tag):
            continue
        candidates.append(_abs(url))
    for tag in _META_IMAGE_RE.findall(html_text) + _LINK_IMAGE_RE.findall(html_text):
        url = html_lib.unescape(_attr(tag, "content") or _attr(tag, "href"))
        if not url or looks_like_junk_image(url=url):
            continue
        candidates.append(_abs(url))
    # 去重但保持顺序
    return list(dict.fromkeys(candidates))


def pick_content_image(
    html_text: str,
    *,
    base_url: str = "",
    client: httpx.Client | None = None,
    max_probes: int = MAX_PROBES,
    min_width: int = MIN_WIDTH_PX,
    min_height: int = MIN_HEIGHT_PX,
) -> str | None:
    """挑一张最合适的内容配图：量过尺寸、够大、正文里靠前的第一张。

    量不到尺寸的候选（比如返回了 HTML 错误页）会被跳过；全都量不到就返回
    第一张候选，总比完全没图好。
    """
    candidates = collect_image_candidates(html_text, base_url=base_url)
    if not candidates:
        return None
    first = candidates[0]
    owns = client is None
    http = client or httpx.Client(
        timeout=PROBE_TIMEOUT, follow_redirects=True, headers={"User-Agent": DEFAULT_UA}
    )
    try:
        for url in candidates[:max_probes]:
            size = probe_image_size(url, client=http)
            if size and size[0] >= min_width and size[1] >= min_height:
                return url
    finally:
        if owns:
            http.close()
    return first


def extract_og_image(html_text: str, *, base_url: str = "") -> str | None:
    """兼容旧调用：只按 HTML 静态判断取第一张图（不探测尺寸）。"""
    found = collect_image_candidates(html_text, base_url=base_url)
    return found[0] if found else None


def fetch_content_image(
    url: str, *, timeout: float = PROBE_TIMEOUT, client: httpx.Client | None = None
) -> str | None:
    """抓一篇文章的配图；任何异常都吞掉（补图是锦上添花）。"""
    owns = client is None
    http = client or httpx.Client(
        timeout=timeout, follow_redirects=True, headers={"User-Agent": DEFAULT_UA}
    )
    try:
        response = http.get(url)
        if response.status_code >= 400:
            return None
        raw = response.content[:MAX_HTML_BYTES]
        return pick_content_image(raw.decode("utf-8", errors="ignore"), base_url=str(response.url), client=http)
    except (httpx.HTTPError, ValueError):
        return None
    finally:
        if owns:
            http.close()


def backfill_images(
    session: Session,
    *,
    limit: int = 20,
    timeout: float = PROBE_TIMEOUT,
    only_today: bool = True,
    media_dir: Path | None = None,
) -> dict[str, Any]:
    """给「相关但没有配图」的文章补配图，返回统计。

    有 media_dir 时顺手把挑中的首图下载到本地（防盗链），映射记进
    ``media_map``；展示层优先读本地，文件丢了就回退原地址。
    """
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
        for index, article in enumerate(rows):
            if article.image_urls is not None:
                continue  # 上一轮已经补上了
            try:
                image = fetch_content_image(article.link, timeout=timeout, client=client)
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
                if media_dir is not None:
                    _localize_cover(article, image, referer=article.link or "",
                                    media_dir=media_dir, client=client)
            else:
                # 用空数组标记「查过了没有」，避免每轮都重查同一篇
                article.image_urls = "[]"
                stats["not_found"] += 1
            if index % 10 == 9:
                # 下载慢，一轮几十张图要跑好几分钟：中途提交，别把写锁占到最后，
                # 否则网页请求与其它任务全程 "database is locked"
                session.commit()
    session.flush()
    if stats["filled"]:
        log.info("补齐配图 %d 篇（候选 %d 篇、无图 %d 篇）", stats["filled"], stats["candidates"], stats["not_found"])
    return stats


def _localize_cover(
    article: Article,
    url: str,
    *,
    referer: str = "",
    media_dir: Path | None = None,
    client: httpx.Client | None = None,
) -> None:
    """把首图下载到本地，映射记进 ``media_map``。失败就留空，展示回退原地址。"""
    if media_dir is None:
        return
    mapping = read_media_map(article.media_map)
    if url in mapping:
        return
    try:
        data = download_image(url, referer=referer, client=client)
    except Exception as exc:
        log.debug("封面下载失败：%s（%r）", url[:80], exc)
        return
    if not data:
        return
    size = measure(data)
    if size and (size[0] < MIN_WIDTH_PX or size[1] < MIN_HEIGHT_PX):
        return
    try:
        name = save_image(data, url, media_dir)
    except OSError as exc:
        log.debug("封面落盘失败：%s（%r）", url[:80], exc)
        return
    if name:
        mapping[url] = f"/img/{name}"
        article.media_map = json.dumps(mapping, ensure_ascii=False)


def localize_missing_covers(
    session: Session,
    *,
    limit: int = 50,
    timeout: float = PROBE_TIMEOUT,
    media_dir: Path | None = None,
) -> dict[str, int]:
    """给「有首图地址、但还没本地文件」的老数据补下载。"""
    if media_dir is None:
        return {"candidates": 0, "localized": 0}
    rows = list(
        session.execute(
            select(Article)
            .where(
                Article.image_urls.isnot(None),
                Article.image_urls != "[]",
                Article.link.notlike("http://localhost%"),
            )
            .order_by(Article.published_at.desc(), Article.id.desc())
            .limit(limit * 3)
        ).scalars()
    )
    stats = {"candidates": 0, "localized": 0}
    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": DEFAULT_UA},
        limits=httpx.Limits(max_connections=6),
    ) as client:
        for index, article in enumerate(rows):
            try:
                urls = json.loads(article.image_urls or "[]")
            except (TypeError, ValueError):
                continue
            first = next((u for u in urls if isinstance(u, str) and u.startswith("http")), None)
            if not first or first in read_media_map(article.media_map):
                continue
            stats["candidates"] += 1
            if stats["localized"] >= limit:
                continue
            before = article.media_map
            _localize_cover(article, first, referer=article.link or "",
                            media_dir=media_dir, client=client)
            if article.media_map != before:
                stats["localized"] += 1
            if index % 10 == 9:
                session.commit()
    session.flush()
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