"""正文抓取：把文章页的正文提取成纯文本。

为什么需要：RSS 的 ``description`` 大多只有一两句（实测量子位正文中位数 15 字、
InfoQ 7 字），靠它生成的内容就是「标题复读」。要让「页内读完」成立，
必须把原文正文取回来。

做法刻意只用标准库，不引入 readability/lxml 这类重依赖，也不要求任何额外 API key：
先剥掉明显的非正文块，再按「段落密度」挑出正文容器，最后逐段清洗。

挑不出正文时返回空字符串，调用方据此保留原来的短摘要 —— 抓不到不等于失败。
"""

from __future__ import annotations

import re
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

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
_ATTRS_RE = re.compile(r"\s(?:id|class|style|data-[\w-]+|aria-[\w-]+)\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I)
_BLOCK_SPLIT_RE = re.compile(rf"</?(?:{_BLOCK_TAGS})\b[^>]*>", re.I)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_TAGLIKE_RE = re.compile(r"<\s*/?\s*[a-z][\w-]*", re.I)

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
)


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
    out: list[str] = []
    for raw in re.findall(r"<p\b[^>]*>(.*?)</p>", html_text, re.I | re.S):
        text = strip_html(_ATTRS_RE.sub("", raw)).strip()
        if len(text) < _MIN_PARAGRAPH_CHARS or _is_junk(text):
            continue
        out.append(text)
    return out


def extract_article_text(html_text: str, *, min_chars: int = 200) -> str:
    """从文章页 HTML 提取正文纯文本；抓不到就返回空串（调用方保留短摘要兜底）。"""
    if not html_text or _TAGLIKE_RE.search(html_text[:200]) is None:
        return ""
    cleaned = _clean(html_text)

    # 优先用 <article> 里的段落；不够长再退回全文段落
    best: list[str] = []
    for tag in _CONTENT_TAGS:
        for block in re.findall(rf"<{tag}\b[^>]*>(.*?)</{tag}>", cleaned, re.I | re.S):
            paragraphs = _paragraph_tags(block)
            if sum(len(p) for p in paragraphs) > sum(len(p) for p in best):
                best = paragraphs
    if sum(len(p) for p in best) < min_chars:
        whole = _paragraph_tags(cleaned)
        if sum(len(p) for p in whole) > sum(len(p) for p in best):
            best = whole
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
        raw = response.content[:MAX_HTML_BYTES]
        for encoding in ("utf-8", "gb18030", "latin-1"):
            try:
                decoded = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            decoded = raw.decode("utf-8", errors="ignore")
        return extract_article_text(decoded, min_chars=min_chars)
    except (httpx.HTTPError, ValueError):
        return ""
    finally:
        if owns:
            http.close()


def looks_like_chinese(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def backfill_content(
    session: Session,
    *,
    limit: int = 20,
    timeout: float = 12.0,
    min_chars: int = 200,
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
    stats = {"candidates": len(rows), "filled": 0, "short": 0, "failed": 0}
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
                text = fetch_article_text(article.link, timeout=timeout, min_chars=min_chars, client=client)
            except Exception as exc:  # 兜底：抓正文失败绝不能影响抓取/处理
                stats["failed"] += 1
                log.debug("抓正文失败：%s（%r）", article.link[:80], exc)
                continue
            if text:
                article.content_full = text
                stats["filled"] += 1
            else:
                # 标记成空串：查过了确实没有，不要每轮重查
                article.content_full = ""
                stats["short"] += 1
    session.flush()
    if stats["filled"]:
        log.info("抓回正文 %d 篇（候选 %d 篇、无正文 %d 篇）", stats["filled"], stats["candidates"], stats["short"])
    return stats


def count_with_full_text(session: Session) -> int:
    """库里有正文全文的文章数（健康检查用）。"""
    return int(
        session.execute(
            select(func.count(Article.id)).where(Article.content_full.isnot(None), Article.content_full != "")
        ).scalar()
        or 0
    )