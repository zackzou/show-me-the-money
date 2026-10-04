"""正文抓取：把文章页的正文提取成纯文本。

为什么需要：RSS 的 ``description`` 大多只有一两句（实测量子位正文中位数 15 字、
InfoQ 7 字），靠它生成的内容就是「标题复读」。要让「页内读完」成立，
必须把原文正文取回来。

做法刻意只用标准库，不引入 readability/lxml 这类重依赖，也不要求任何额外 API key：
先剥掉明显的非正文块，再按「段落密度」挑出正文容器，最后逐段清洗。

挑不出正文时返回空字符串，调用方据此保留原来的短摘要 —— 抓不到不等于失败。
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

from app.fetcher.images import looks_like_junk_image, src_of
from app.fetcher.media_store import localize_anchors
from app.models import Article
from app.utils.logger import get_logger
from app.utils.text import strip_html

log = get_logger(__name__)

DEFAULT_UA = "ShowMeTheMoney/0.2 (+https://github.com/zackzou/show-me-the-money)"
MAX_HTML_BYTES = 1_500_000
MAX_CONTENT_CHARS = 40_000

# 先整块删掉：这些标签里的文字一定不是正文
_DROP_BLOCKS = ("script", "style", "noscript", "iframe", "form", "svg", "button", "nav", "aside", "footer", "header")
# 正文候选容器，按优先级
_CONTENT_TAGS = ("article", "main")
# 段落级标签
_BLOCK_TAGS = "p|div|section|li|h1|h2|h3|h4|h5|h6|blockquote|pre|tr|br"
_DROP_RE = re.compile(rf"<({ '|'.join(_DROP_BLOCKS) })\b[^>]*>.*?</\1>", re.I | re.S)
_SELF_CLOSING_DROP_RE = re.compile(rf"<({ '|'.join(_DROP_BLOCKS) })\b[^>]*/?>", re.I)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_P_BLOCK_RE = re.compile(r"<p\b[^>]*>.*?</p>", re.I | re.S)
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.I)
_ATTRS_RE = re.compile(r"\s(?:id|class|style|data-[\w-]+|aria-[\w-]+)\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I)
_BLOCK_SPLIT_RE = re.compile(rf"</?(?:{_BLOCK_TAGS})\b[^>]*>", re.I)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_TAGLIKE_RE = re.compile(r"<\s*/?\s*[a-z][\w-]*", re.I)

# 清掉模板噪音后，正文至少要有这么多字符才算「原文真的有正文」
_MIN_REAL_BODY_CHARS = 30
# 一段正文至少要有这么多字符才值得留（滤掉「阅读全文」「分享到」这类碎片）
_MIN_PARAGRAPH_CHARS = 24
# 导航/版权类短句黑名单
_JUNK_PATTERNS = (
    "阅读全文",
    "查看全文",
    "更多精彩",
    "关注我们",
    "扫码关注",
    "版权声明",
    "转载请注明",
    "点击查看",
    "相关阅读",
    "推荐阅读",
    "Share this",
    "Read more",
    "Subscribe",
    "Sign up",
    "All rights reserved",
    "Cookie",
    # Reddit 的 RSS description 里全是这种模板噪音，清掉后正文才算真的空
    "submitted by",
    "[link]",
    "[comments]",
    "Comments",
    "level 1",
    "ago",
)

# Reddit 这类站点即使抓不到 selftext，RSS description 里也只剩模板套话。
# 清完这些之后还剩下什么，才算「原文真的有正文」。
_BOILERPLATE_ONLY_RE = re.compile(
    r"(?:submitted\s+by|\[link\]|\[comments\]|comments|level\s*\d+|"
    r"/u/[\w-]+|\b\d+\s*(?:mo|yr|day|hour|min)s?\s*ago\b|\bupvote\b|\bpermalink\b|\breport\b)",
    re.I,
)


def is_real_body(text: str | None) -> bool:
    """正文是不是「真的」。清掉模板噪音后连 30 个字都不到，就算没有正文。

    Reddit 的 RSS description 形如
    ``submitted by  /u/xxx   [link]   [comments]`` —— 剥掉标签之后还剩几十个字符，
    不做这一步的话详情页会渲染出一个只写着「Comments」的「正文开头」区块，
    看起来就像抓取坏了。
    """
    body = (text or "").strip()
    if len(body) < _MIN_REAL_BODY_CHARS:
        return False
    # 光看总长度会被「submitted by /u/xxx [link] [comments]」骗过去 ——
    # 得把模板噪音和用户名删掉，剩下的还够长才算真的有正文。
    residue = _BOILERPLATE_ONLY_RE.sub(" ", body).strip()
    return len(residue) >= _MIN_REAL_BODY_CHARS


def _clean(html_text: str) -> str:
    html_text = _COMMENT_RE.sub(" ", html_text)
    html_text = _DROP_RE.sub(" ", html_text)
    html_text = _SELF_CLOSING_DROP_RE.sub(" ", html_text)
    return html_text


def _paragraphs(fragment: str) -> list[str]:
    """把一段 HTML 切成纯文本段落（按块级标签边界切）。"""
    chunks = _BLOCK_SPLIT_RE.split(fragment)
    out: list[str] = []
    for chunk in chunks:
        text = strip_html(_ATTRS_RE.sub("", chunk)).strip()
        if len(text) < _MIN_PARAGRAPH_CHARS:
            continue
        if _is_junk(text):
            continue
        out.append(text)
    return out


def _is_junk(text: str) -> bool:
    low = text.lower()
    return any(junk in text or junk.lower() in low for junk in _JUNK_PATTERNS)


def _paragraph_tags(html_text: str) -> list[str]:
    """抽出文档里所有 ``<p>`` 段落。

    不去挑「正文容器」：正则匹配嵌套 div 会被截断（量子位 51 个 div 全部只拿到 4 段残句），
    而 ``<p>`` 不嵌套，是最稳的正文信号。非正文区域基本不用 ``<p>``。
    """
    return _paragraphs_with_images(html_text, "")[0]


def _paragraphs_with_images(html_text: str, base_url: str) -> tuple[list[str], list[tuple[int, str]]]:
    """抽段落的同时记下正文里图片的位置。

    一次解析拿到两样东西，段落下标与图片位置天然对齐 —— 分两趟扫同一个
    HTML 的话，过滤规则一旦改动，两边的下标就对不上了，图片会插错地方。

    返回的 ``(n, 图片地址)`` 表示「接在前 n 段之后」；``n = 0`` 是正文第一段
    之前（原站把头图放在正文开头的情况）。
    """
    events: list[tuple[int, str, re.Match[str]]] = []
    events.extend((m.start(), "p", m) for m in _P_BLOCK_RE.finditer(html_text))
    events.extend((m.start(), "img", m) for m in _IMG_TAG_RE.finditer(html_text))
    events.sort(key=lambda event: event[0])

    paragraphs: list[str] = []
    images: list[tuple[int, str]] = []
    seen: set[str] = set()
    for _, kind, match in events:
        if kind == "img":
            url = _abs_image_url(match.group(0), base_url)
            if url and url not in seen:
                seen.add(url)
                images.append((len(paragraphs), url))
            continue
        raw = match.group(0)
        text = strip_html(_ATTRS_RE.sub("", raw)).strip()
        # 先记图再判段：``<p><img></p>`` 这种纯图段落自身会被滤掉，
        # 但它里面的图要留下，挂到前一段后面
        if len(text) < _MIN_PARAGRAPH_CHARS or _is_junk(text):
            continue
        paragraphs.append(text)
    return paragraphs, images


def _abs_image_url(tag: str, base_url: str) -> str | None:
    """取 ``<img>`` 的可用地址：过滤图标类资源，并补成绝对地址。"""
    url = html_lib.unescape(src_of(tag))
    if not url or looks_like_junk_image(url=url, tag=tag):
        return None
    try:
        return str(httpx.URL(base_url).join(url)) if base_url else url
    except (httpx.InvalidURL, ValueError):
        return None


def _best_source(cleaned: str, min_chars: int) -> str:
    """挑出最可能装正文的 HTML 片段。

    先看 ``<article>`` / ``<main>``，不够长再退回整篇；返回的是**原始片段**
    而不是段落列表 —— 图片要挂在段落之间，只给段落就丢了位置。
    """
    best = ""
    best_len = 0
    for tag in _CONTENT_TAGS:
        for block in re.findall(rf"<{tag}\b[^>]*>(.*?)</{tag}>", cleaned, re.I | re.S):
            length = sum(len(p) for p in _paragraph_tags(block))
            if length > best_len:
                best, best_len = block, length
    if best_len < min_chars:
        whole = sum(len(p) for p in _paragraph_tags(cleaned))
        if whole > best_len:
            best, best_len = cleaned, whole
    return best


def extract_article_text(html_text: str, *, min_chars: int = 200) -> str:
    """从文章页 HTML 提取正文纯文本；抓不到就返回空串（调用方保留短摘要兜底）。"""
    if not html_text or _TAGLIKE_RE.search(html_text[:200]) is None:
        return ""
    cleaned = _clean(html_text)

    # 优先用 <article> 里的段落；不够长再退回全文段落
    best = _paragraph_tags(_best_source(cleaned, min_chars))
    if not best:
        best = _paragraphs(_pick_container(cleaned))
    if not best:
        return ""

    deduped: list[str] = []
    for text in best:
        if deduped and deduped[-1] == text:  # 模板常把同一段渲染两次
            continue
        deduped.append(text)
    text = "\n\n".join(deduped).strip()
    return text[:MAX_CONTENT_CHARS] if len(text) >= min_chars else ""


# 正文里最多留几张内联配图。再多就不是「读文章」，而是翻相册了
MAX_INLINE_IMAGES = 12


def extract_article_document(
    html_text: str, *, base_url: str = "", min_chars: int = 200
) -> tuple[str, list[dict[str, Any]]]:
    """抓正文 + 正文里配图的位置，返回 ``(正文, [{"i": 段落下标, "url": 地址}])``。

    原站（图1 的 AIHOT、量子位、IT之家）都是把配图**插在正文段落之间**的，
    单独开一个「文章配图」区块既割裂又看不出图配的是哪一段。这里按原站的
    做法把位置一起存下来，详情页照着插回正文。

    挑不出正文时返回 ``("", [])``，与 ``extract_article_text`` 口径一致。
    """
    if not html_text or _TAGLIKE_RE.search(html_text[:200]) is None:
        return "", []
    cleaned = _clean(html_text)
    source = _best_source(cleaned, min_chars)
    if not source:
        return "", []

    paragraphs, images = _paragraphs_with_images(source, base_url)
    if not paragraphs:
        # 没有 <p> 的页面走块级兜底，这时位置信息拿不到，图就整体略过
        return "", []

    deduped: list[str] = []
    dropped_at: list[int] = []  # 被去重掉的段落下标
    for index, text in enumerate(paragraphs):
        if deduped and deduped[-1] == text:  # 模板常把同一段渲染两次
            dropped_at.append(index)
            continue
        deduped.append(text)
    text = "\n\n".join(deduped).strip()
    if len(text) < min_chars:
        return "", []

    anchors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, url in images:
        if len(anchors) >= MAX_INLINE_IMAGES or url in seen:
            continue
        seen.add(url)
        # 锚点说的是「前 n 段之后」，去重掉的段要一并扣掉，否则图片会插到后面去
        shifted = position - sum(1 for index in dropped_at if index < position)
        anchors.append({"i": shifted, "url": url})
    return text[:MAX_CONTENT_CHARS], anchors


def _pick_container(html_text: str) -> str:
    """兜底：没有 ``<p>`` 的页面（少见）才退到按段落密度挑 div。"""
    for tag in _CONTENT_TAGS:
        blocks = re.findall(rf"<{tag}\b[^>]*>(.*?)</{tag}>", html_text, re.I | re.S)
        if blocks:
            best = max(blocks, key=lambda b: len(_paragraphs(b)))
            if _paragraphs(best):
                return best
    divs = re.findall(r"<div\b[^>]*>(.*?)</div>", html_text, re.I | re.S)
    best_div, best_score = "", 0
    for div in divs:
        score = sum(len(p) for p in _paragraphs(div))
        if score > best_score:
            best_div, best_score = div, score
    return best_div or html_text


def _decode(response: httpx.Response) -> str:
    """按 utf-8 / gb18030 / latin-1 依次试解码（国内站点还有 GB2312）。"""
    raw = response.content[:MAX_HTML_BYTES]
    for encoding in ("utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def fetch_article_text(
    url: str,
    *,
    timeout: float = 12.0,
    min_chars: int = 200,
    client: httpx.Client | None = None,
) -> str:
    """抓一篇文章的正文；任何异常都吞掉（抓不到就退回短摘要）。"""
    owns = client is None
    http = client or httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": DEFAULT_UA},
    )
    try:
        response = http.get(url)
        if response.status_code >= 400:
            return ""
        return extract_article_text(_decode(response), min_chars=min_chars)
    except (httpx.HTTPError, ValueError):
        return ""
    finally:
        if owns:
            http.close()


def fetch_article_document(
    url: str,
    *,
    timeout: float = 12.0,
    min_chars: int = 200,
    client: httpx.Client | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """抓正文 + 正文里配图的位置（一次请求拿全，图片地址要按原页 URL 补绝对）。"""
    owns = client is None
    http = client or httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": DEFAULT_UA},
    )
    try:
        response = http.get(url)
        if response.status_code >= 400:
            return "", []
        return extract_article_document(
            _decode(response), base_url=str(response.url), min_chars=min_chars
        )
    except (httpx.HTTPError, ValueError):
        return "", []
    finally:
        if owns:
            http.close()


def looks_like_chinese(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def _store_body_anchors(
    article: Article,
    anchors: list[dict[str, Any]],
    *,
    referer: str = "",
    media_dir: Path | None = None,
    client: httpx.Client | None = None,
) -> str:
    """存内联配图锚点。有 media_dir 就逐张下载本地化（下不到/太小的丢掉）。

    返回 ``saved`` / ``empty`` / ``retry``，三者的区别对调用方很重要：

    - ``saved``  存下了（可能有图，也可能部分成功）；
    - ``empty``  确定没有可用图（本来就没图，或全被尺寸门槛滤掉），
      已写 ``[]`` 标记查过了，调用方不该再为它重排；
    - ``retry``  这轮一张都没下下来且**不是**因为太小 —— 源站在限流/抖动。
      这时**不写库**，原样保留，下轮再来。误写成 ``[]`` 就等于把这些图永久丢了
      （实测量子位长文 12 张图会在源站抖动时整篇清空）。
    """
    if media_dir is not None and anchors:
        probe: dict[str, int] = {}
        localized = localize_anchors(
            anchors, referer=referer, media_dir=media_dir, client=client, stats=probe
        )
        if localized:
            anchors = localized
        elif probe.get("failed", 0) > 0 and not probe.get("too_small", 0):
            return "retry"
        else:
            # 全因太小被丢（徽章/头像/图标）：确定没有可用图
            anchors = []
    article.body_images = json.dumps(anchors, ensure_ascii=False) if anchors else "[]"
    return "saved" if anchors else "empty"


def backfill_content(
    session: Session,
    *,
    limit: int = 20,
    timeout: float = 12.0,
    min_chars: int = 200,
    media_dir: Path | None = None,
) -> dict[str, Any]:
    """给「判为相关但还没正文」的文章抓全文，返回统计。

    抓取放在相关度判断**之前**：速览 / 推荐理由 / 摘要的质量都依赖正文，
    只靠 RSS 那两三句话，写出来必然是标题复读。
    """
    rows = list(
        session.execute(
            select(Article)
            .where(Article.content_full.is_(None), Article.link.notlike("http://localhost%"))
            .order_by(Article.published_at.desc(), Article.id.desc())
            .limit(limit)
        ).scalars()
    )
    stats = {"candidates": len(rows), "filled": 0, "short": 0, "failed": 0, "with_images": 0}
    if not rows:
        return stats

    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": DEFAULT_UA},
        limits=httpx.Limits(max_connections=6),
    ) as client:
        for article in rows:
            try:
                text, images = fetch_article_document(
                    article.link, timeout=timeout, min_chars=min_chars, client=client
                )
            except Exception as exc:  # 兜底：抓正文失败绝不能影响抓取/处理
                stats["failed"] += 1
                log.debug("抓正文失败：%s（%r）", article.link[:80], exc)
                continue
            if text:
                article.content_full = text
                # 正文里的配图连同段落位置一起存，详情页照位置插回正文
                outcome = _store_body_anchors(
                    article, images, referer=article.link or "",
                    media_dir=media_dir, client=client,
                )
                if outcome == "saved":
                    stats["with_images"] += 1
                elif outcome == "retry":
                    # 下载没成功：留 NULL，下轮 backfill_body_images 会再来
                    article.body_images = None
                stats["filled"] += 1
            else:
                # 标记成空串：查过了确实没有，不要每轮重查
                article.content_full = ""
                stats["short"] += 1
            session.commit()
    session.flush()
    if stats["filled"]:
        log.info(
            "抓回正文 %d 篇（候选 %d 篇、无正文 %d 篇、含配图 %d 篇）",
            stats["filled"],
            stats["candidates"],
            stats["short"],
            stats["with_images"],
        )
    return stats


# 「同一个信源里有几篇文章一字不差地以这段开头」才算站点通栏。
# 20 个字是实测出来的下限：InfoQ 那段通栏的第一行只有 28 个字。
_MIN_SHARED_OPENING_CHARS = 20


def strip_shared_openings(
    session: Session, *, min_group: int = 3, limit: int = 300, passes: int = 4
) -> dict[str, Any]:
    """删掉「同一信源里多篇文章共有的开头」—— 站点通栏广告 / 会议宣传。

    抓 InfoQ 时发现的：每一篇文章的开头都是同一段 QCon 大会宣传

        从「构建 AI」到「驾驭 AI」，100+ 实战案例拆解 …
        2026 年 QCon 全球软件开发大会 · 上海站
        将于 10 月 22 日—24 日 举办，聚焦 …

    四篇文章一字不差地顶着这段广告开场，读者以为自己在看重复内容，
    文章真正要讲的第一段也被挤到屏幕外。

    判定不靠关键词（换个会议名就失效），而是**同一个信源里有几篇文章的开头一字不差**：
    真实新闻不会这么巧，站点通栏会。纯本地计算，不用重新抓取。

    不按相关度筛：通栏是站点层面的事，与这条新闻是否相关无关；而筛了的话
    「三篇被判为不相关」就刚好凑不满门槛，广告反而留着。
    """
    rows = list(
        session.execute(
            select(Article)
            .where(Article.content_full.isnot(None), Article.content_full != "")
            .order_by(Article.published_at.desc(), Article.id.desc())
            .limit(limit)
        ).scalars()
    )
    stats = {"candidates": len(rows), "groups": 0, "articles": 0, "paragraphs": 0}
    if not rows:
        return stats
    for _ in range(passes):
        buckets: dict[tuple[int | None, str], list[Article]] = {}
        for article in rows:
            body = article.content_full or ""
            head = body.split("\n\n", 1)[0].strip()
            # 门槛压到 20 个字：InfoQ 的通栏第一段只有「2026 年 QCon 全球软件开发大会大会 ·
            # 上海站」这 28 个字，卡在 40 就正好把它漏掉，第二段通栏也就跟着留下了
            if len(head) >= _MIN_SHARED_OPENING_CHARS:
                buckets.setdefault((article.source_id, head), []).append(article)
        shared = {key: group for key, group in buckets.items() if len(group) >= min_group}
        if not shared:
            break
        stats["groups"] += len(shared)
        for group in shared.values():
            for article in group:
                trimmed = strip_leading_paragraph(article.content_full)
                if trimmed and trimmed != article.content_full:
                    article.content_full = trimmed
                    _shift_body_image_anchors(article)
                    stats["articles"] += 1
                    stats["paragraphs"] += 1
    session.flush()
    if stats["articles"]:
        log.info(
            "删掉站点通栏开头 %d 段（%d 篇、%d 组）", stats["paragraphs"], stats["articles"], stats["groups"]
        )
    return stats


def strip_leading_paragraph(text: str | None) -> str | None:
    """去掉正文的第一段；没有可去的第一段时返回 ``None``（调用方据此判断要不要写回）。"""
    if not text:
        return None
    _, sep, rest = text.partition("\n\n")
    rest = rest.strip()
    return rest or None


def _shift_body_image_anchors(article: Article) -> None:
    """删掉正文第一段后，配图锚点整体上移一段。

    ``body_images`` 存的是「接在第 n 段之后」：删掉下标 0 的段，
    原来接在第 n 段之后的图现在接在第 n-1 段之后。头图（锚点 0，本来就在
    第一段之前）不受影响，钳在 0 不让它变负数。

    坏数据不碰：解析失败就原样保留，展示侧本来就会把它当没有。
    """
    raw = article.body_images
    if not raw:
        return
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return
    if not isinstance(parsed, list):
        return
    shifted = False
    for item in parsed:
        if not isinstance(item, dict):
            continue
        index = item.get("i")
        if isinstance(index, int) and not isinstance(index, bool) and index > 0:
            item["i"] = index - 1
            shifted = True
    if shifted:
        article.body_images = json.dumps(parsed, ensure_ascii=False)


def count_with_full_text(session: Session) -> int:
    """库里有正文全文的文章数（健康检查用）。"""
    return int(
        session.execute(
            select(func.count(Article.id)).where(Article.content_full.isnot(None), Article.content_full != "")
        ).scalar()
        or 0
    )


# 两个分支统计的名字不同：新抓的算「新定位」，已定位的算「已本地化」
_COUNTER_FRESH = {"saved": "filled", "empty": "empty", "retry": "failed"}
_COUNTER_LEGACY = {"saved": "localized", "empty": "empty", "retry": "failed"}


def backfill_body_images(
    session: Session,
    *,
    limit: int = 20,
    timeout: float = 12.0,
    min_chars: int = 200,
    media_dir: Path | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """给「已经有正文、但还没有内联配图位置」的文章补一次位置。

    正文里插图是后来才有的，老数据全都没有 ``body_images``。重新抓一遍文章页
    就能把位置算出来 —— 正文本身已经在库里，不用重写，也不用重新入库。

    旧格式锚点（只有 url、没下载过）也在这里顺手本地化：防盗链裂图、
    GitHub 徽章这类小图都在这一轮被换成/丢掉。
    """
    fresh = list(
        session.execute(
            select(Article)
            .where(
                Article.body_images.is_(None),
                Article.content_full.isnot(None),
                Article.content_full != "",
                Article.relevance == 1,
                Article.link.notlike("http://localhost%"),
            )
            .order_by(Article.published_at.desc(), Article.id.desc())
            .limit(limit)
        ).scalars()
    )
    # 旧格式锚点（只有远端 url）：位置已经算对了，这里只做「下载到本地 + 滤掉小图」。
    # **不限 relevance**：判为不相关的文章详情页照样能直接访问（/story/<id>），
    # 早先这里跟着 fresh 一起筛了 relevance==1，结果这些页面的图一直在引用原站 ——
    # 量子位 CDN 对非本站 Referer 直接 403，读者看到的是一片裂图。
    legacy = (
        list(
            session.execute(
                select(Article)
                .where(
                    Article.body_images.isnot(None),
                    Article.body_images != "[]",
                    Article.body_images.notlike('%"local"%'),
                    Article.link.notlike("http://localhost%"),
                )
                .order_by(Article.published_at.desc(), Article.id.desc())
                .limit(limit)
            ).scalars()
        )
        if media_dir is not None
        else []
    )
    rows = fresh + [a for a in legacy if a not in fresh]
    stats = {"candidates": len(rows), "filled": 0, "empty": 0, "failed": 0, "localized": 0}
    if not rows:
        return stats

    owns = client is None
    http = client or httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": DEFAULT_UA},
        limits=httpx.Limits(max_connections=6),
    )
    try:
        for index, article in enumerate(rows):
            if article.body_images is None:
                try:
                    _, images = fetch_article_document(
                        article.link, timeout=timeout, min_chars=min_chars, client=http
                    )
                except Exception as exc:  # 兜底：补图失败绝不能影响主流程
                    stats["failed"] += 1
                    log.debug("补正文配图失败：%s（%r）", article.link[:80], exc)
                    continue
                # 空数组表示「查过了，正文里确实没有图」，别每轮都重查
                outcome = _store_body_anchors(
                    article, images, referer=article.link or "",
                    media_dir=media_dir, client=http,
                )
                # fresh 分支：新抓到正文并顺带定位了图
                stats[_COUNTER_FRESH[outcome]] += 1
                if outcome == "retry":
                    # 下载没成功：body_images 仍是 NULL，下轮照样会被捞到
                    article.body_images = None
            else:
                # 旧格式：只本地化，不重抓（位置已经是对的）
                try:
                    old = json.loads(article.body_images or "[]")
                except (TypeError, ValueError):
                    old = []
                if not isinstance(old, list):
                    old = []
                outcome = _store_body_anchors(
                    article, old,
                    referer=article.link or "", media_dir=media_dir, client=http,
                )
                # legacy 分支：已定位的图被换成本地文件
                stats[_COUNTER_LEGACY[outcome]] += 1
                if outcome == "retry":
                    # 不写库，原样保留下轮再试（别标 []，那等于把图永久丢了）
                    article.body_images = json.dumps(old, ensure_ascii=False)
            if index % 5 == 4:
                # 单张下载慢：中途提交，别把写锁占到最后
                session.commit()
    finally:
        if owns:
            http.close()
    session.flush()
    if stats["filled"]:
        log.info(
            "补正文内联配图 %d 篇（候选 %d 篇、正文无图 %d 篇）",
            stats["filled"],
            stats["candidates"],
            stats["empty"],
        )
    return stats