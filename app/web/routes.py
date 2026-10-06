"""HTML 页面路由（Jinja2）。

页面口径与日报生成共用 ``day_window`` 与 ``STATUS_REPORTABLE``，否则页面会比日报多/少东西。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.cluster import duplicates_of, primary_of
from app.ai.processor import translation_is_usable
from app.config import Settings
from app.db import get_session
from app.fetcher.content import is_real_body
from app.fetcher.media_store import is_safe_image_name, media_dir_for, read_media_map
from app.models import Article, DailyReport, Source
from app.report.generator import STATUS_REPORTABLE, day_window
from app.utils.text import is_chinese_text, now_local, split_tags, strip_markdown, truncate
from app.utils.text import looks_english as is_english
from app.web.search import (
    SCOPE_LABELS,
    SCOPE_META,
    count_by_category,
    normalize_scope,
    search_articles,
    search_metadata,
)

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
log = logging.getLogger(__name__)

page_router = APIRouter()

PAGE_SIZE = 40
# 历史日报一次渲染多少天（分页另算）
ARCHIVE_PAGE_SIZE = 120
WEEKDAYS = "一二三四五六日"

# SQLite 的 INTEGER 是有符号 64 位。路径参数写成裸 ``int`` 时，Pydantic 会照单
# 全收下 10**30 这种值，然后由 SQLite 抛 OverflowError —— 也就是 500。
# 不存在的 id 本来就该是 404，所以直接在参数声明处挡掉超界值。
IdParam = Annotated[int, PathParam(ge=1, le=2**63 - 1)]


@page_router.get("/img/{name}")
def local_image(name: str, request: Request):
    """本地图片：原文图下载落盘后走这里，不再看原站脸色。

    文件名只认 sha1+扩展名（目录穿越直接 404）；文件丢了也 404，
    调用方 `<img>` 有原地址回退……注意模板里拿到的已经是解析后的地址，
    只有 media_map 指过来的才会请求到这里。
    """
    if not is_safe_image_name(name):
        # 不是页面，别谎报成 text/html：<img> 拿到 HTML 会当成坏图处理
        return PlainTextResponse("not found", status_code=404)
    settings = _settings(request)
    media_dir = media_dir_for(settings.db_file) if settings else None
    path = (media_dir / name) if media_dir else None
    if path is None or not path.is_file():
        # 不是页面，别谎报成 text/html：<img> 拿到 HTML 会当成坏图处理
        return PlainTextResponse("not found", status_code=404)
    return FileResponse(
        path,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


def _images(raw: str | None, media_map: str | None = None) -> list[str]:
    """image_urls 存的是 JSON 数组；老数据/坏数据一律当没有图。

    有本地映射就走 `/img/`（原站防盗链经常裂图），文件丢了自动回退原地址。
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    mapping = read_media_map(media_map)
    out = []
    for item in parsed:
        if not isinstance(item, str) or not item.startswith(("http://", "https://")):
            continue
        out.append(mapping.get(item, item))
    return out


def _topics_of(article: Article) -> list[str]:
    """topics 存的是 JSON 数组；老数据/坏数据一律当没有。"""
    if not article.topics:
        return []
    try:
        parsed = json.loads(article.topics)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in parsed if isinstance(item, str)]


def _body_image_anchors(raw: str | None) -> list[tuple[int, str]]:
    """读 body_images：返回 ``[(接在第几段之后, 展示地址), ...]``。

    新格式锚点带 ``local``（本地 /img/ 路径），优先用它；老格式只有 url，
    照样能读（还没轮到下载的那批）。坏数据一律当没有。
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    out: list[tuple[int, str]] = []
    for item in parsed if isinstance(parsed, list) else []:
        if not isinstance(item, dict):
            continue
        index, url = item.get("i"), item.get("url")
        if isinstance(index, int) and isinstance(url, str) and url.startswith(("http://", "https://")):
            local = item.get("local")
            out.append((index, local if isinstance(local, str) and local.startswith("/img/") else url))
    return out


def _image_slots(anchors: list[tuple[int, str]], block_count: int) -> list[list[str]]:
    """把锚点摊成按段落序号取用的列表，长度是 ``block_count + 1``。

    第 0 项是正文第一段之前的图（头图），第 n 项是第 n 段之后的图 ——
    模板里 ``slots[loop.index]`` 就能直接取，不用在 Jinja 里算位置。
    """
    slots: list[list[str]] = [[] for _ in range(block_count + 1)]
    for index, url in anchors:
        if 0 <= index <= block_count:
            slots[index].append(url)
    return slots


def _image_slots_for_blocks(
    anchors: list[tuple[int, str]], marked: list[dict[str, Any]]
) -> list[list[str]]:
    """按 blocks（含小标题行）摊配图：锚点记的是纯段落序号，小标题行不占号。

    以前直接按 blocks 下标摊，AI 章节每节一个小标题，图会系统性后移；
    这里把段号先映射到它所在的 block 位置再摊。模板用法不变。
    """
    slots: list[list[str]] = [[] for _ in range(len(marked) + 1)]
    plain_at = [bi for bi, block in enumerate(marked) if not block.get("heading")]
    for para_index, url in anchors:
        if para_index < 0 or para_index > len(plain_at):
            continue
        if para_index == 0:
            slots[0].append(url)
        else:
            slots[plain_at[para_index - 1] + 1].append(url)
    return slots


def _shift_image_slots(slots: list[list[str]], source_blocks: int, target_blocks: int) -> list[list[str]]:
    """把配图位置按比例挪到译文对应的段落上。

    译文是分段翻的，失败的那几段会被丢掉，所以译文段数与原文对不上，
    段号不能直接搬。图片跟着它在全文里的相对位置走，误差只在一两段之内，
    总比所有图堆在开头或结尾好。
    """
    flat = [(index, url) for index, group in enumerate(slots) for url in group]
    if source_blocks <= 0 or target_blocks <= 0:
        return _image_slots([], target_blocks)
    if source_blocks == target_blocks:
        return _image_slots(flat, target_blocks)
    shifted = [(min(target_blocks, round(index / source_blocks * target_blocks)), url) for index, url in flat]
    return _image_slots(shifted, target_blocks)


def _shift_anchors(
    anchors: list[tuple[int, str]], source_paras: int, target_paras: int
) -> list[tuple[int, str]]:
    """把锚点（纯段落序号）按比例挪到译文对应的段落上。

    与 ``_shift_image_slots`` 同一规则，只是作用在摊开之前 ——
    小标题行不占段号，摊开后的下标已经是 block 位置，不能再按比例缩放。
    """
    if source_paras <= 0 or target_paras <= 0:
        return []
    if source_paras == target_paras:
        return list(anchors)
    return [
        (min(target_paras, round(index / source_paras * target_paras)), url)
        for index, url in anchors
    ]


# 像小标题的段落：短、不以句末标点结尾、不是纯数字
_HEADING_MAX_CHARS = 30
_HEADING_MIN_CHARS = 2
_SENTENCE_END = "。！？；，、：,.!?;:…"


def _body_blocks(raw: str | None) -> list[str]:
    """正文全文按空行切段，供详情页逐段渲染。"""
    if not raw:
        return []
    return [part.strip() for part in raw.split("\n\n") if part.strip()]


def _section_blocks(raw: str | None, *, prefix: str) -> list[dict[str, Any]] | None:
    """读 AI 章节结构：返回显式 ``[{i, id, text, heading}]``，没有返回 ``None``。

    有章节时小标题是 AI 定好的，不再走启发式猜（短句、无标点那种猜法
    会把「马斯克的Terafab」这种短段也标成标题）。返回 ``None`` 时调用方
    回退 mark_headings。
    """
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, list):
        return None
    marked: list[dict[str, Any]] = []
    index = 0
    for section in parsed:
        if not isinstance(section, dict):
            continue
        heading = str(section.get("h") or "").strip()
        if heading:
            marked.append({"i": index, "id": f"{prefix}-{index}", "text": heading, "heading": True})
            index += 1
        for para in str(section.get("t") or "").split("\n\n"):
            text = para.strip()
            if not text:
                continue
            marked.append({"i": index, "id": f"{prefix}-{index}", "text": text, "heading": False})
            index += 1
    return marked or None


def mark_headings(
    blocks: list[str], *, prefix: str = "sec", want_headings: bool = True
) -> list[dict[str, Any]]:
    """把正文里「短句、不以标点结尾」的段落标成小标题。

    这样详情页能像图1 那样给一个本文目录，并把小标题渲染成 ``<h3>``；
    剩下的当普通段落。判不准就当普通段落，不会漏内容。

    ``prefix`` 用来给锚点 id 分命名空间：详情页会同时把原文与译文渲染进
    DOM（靠 CSS 按语言隐藏其一），两边都从 0 开始编号就会撞 id，
    目录会跳到另一版的位置去。

    ``want_headings=False`` 时一律不标小标题 —— 双语结构对不上时要让两个
    语言版本退回同一种朴素排版（见 story 里的 ``_structure_matches``）。
    """
    marked: list[dict[str, Any]] = []
    for index, text in enumerate(blocks):
        stripped = text.strip()
        is_heading = want_headings and (
            _HEADING_MIN_CHARS <= len(stripped) <= _HEADING_MAX_CHARS
            and stripped[-1] not in _SENTENCE_END
            and not stripped[0].isdigit()
        )
        marked.append({"i": index, "id": f"{prefix}-{index}", "text": stripped, "heading": is_heading})
    return marked


def table_of_contents(marked: list[dict[str, Any]], *, limit: int = 12) -> list[dict[str, Any]]:
    return [item for item in marked if item["heading"]][:limit]


def _day_statement(date_str: str):
    start, end = day_window(date_str)
    return (
        select(Article, Source.name, Source.url)
        .outerjoin(Source, Article.source_id == Source.id)
        .where(
            Article.relevance == 1,
            Article.status.in_(STATUS_REPORTABLE),
            Article.duplicate_of.is_(None),
            Article.published_at >= start,
            Article.published_at < end,
        )
        .order_by(Article.published_at.desc(), Article.id.desc())
    )


def counts_by_day(session: Session, dates: list[str]) -> dict[str, int]:
    """一批日期各自有多少篇**当前**可展示的文章。

    日报行上的 ``article_count`` 是 08:00 定稿时的快照，之后补处理完的文章、
    或者把重复项合并掉，真实条数就变了。页面列表是实时查的，于是头部数字
    会和下面的列表对不上（实测 /daily/2026-09-30 写着 2 篇、列了 12 篇）。
    这里用**和列表完全相同的过滤条件**重新数一遍，数字与列表同源。
    """
    counts: dict[str, int] = {}
    for date_str in dates:
        try:
            start, end = day_window(date_str)
        except (ValueError, OverflowError):
            counts[date_str] = 0
            continue
        counts[date_str] = int(
            session.execute(
                select(func.count(Article.id)).where(
                    Article.relevance == 1,
                    Article.status.in_(STATUS_REPORTABLE),
                    Article.duplicate_of.is_(None),
                    Article.published_at >= start,
                    Article.published_at < end,
                )
            ).scalar_one()
        )
    return counts


def _host(url: str | None) -> str:
    if not url:
        return ""
    trimmed = url.split("://", 1)[-1]
    return trimmed.split("/", 1)[0]


def _relative(value: datetime | None, now: datetime) -> str:
    """发布时间的人话版本：刚刚 / N 分钟前 / N 小时前 / N 天前 / 日期。"""
    if value is None:
        return ""
    delta = now - value
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return ""
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前"
    if seconds < 86400 * 7:
        return f"{seconds // 86400} 天前"
    return value.strftime("%Y-%m-%d")


def _reason_of(article: Article, digest_zh: str, digest: str) -> str:
    """推荐理由；和导语一字不差就当没有。

    LLM 不可用时摘要与推荐理由都退到同一段兜底文本，卡片上并排显示两遍一模一样的
    话看起来像坏了。理由本来就是「为什么要点开这条」，复读一遍导语没有意义。
    """
    reason = strip_markdown(article.reason)
    if not reason:
        return ""
    lead = digest_zh.strip() or digest.strip()
    return "" if lead and reason.strip() == lead else reason


def _card(article: Article, source_name: str | None, source_url: str | None, now: datetime) -> dict[str, Any]:
    published = article.published_at
    digest_zh = strip_markdown(article.digest_zh)
    digest = strip_markdown(article.digest_zh or article.digest)
    return {
        "id": article.id,
        # 中文标题优先：英文信源译过来的标题读起来才像中文
        # 中文标题优先：英文信源译过来的标题读起来才像中文
        "title": article.title_zh or article.title,
        "title_zh": article.title_zh or "",
        # title_en 缺失时退回原文：处理早期失败的文章压根没轮到写它，
        # 而模板里没有 en 就没有任何东西可显示 —— 切到英文会是一条空标题
        "title_en": article.title_en or ("" if is_chinese_text(article.title) else article.title),
        "link": article.link,
        "source": source_name or "未知来源",
        "source_host": _host(source_url),
        "time_hm": published.strftime("%H:%M") if published else "",
        "date_key": published.strftime("%Y-%m-%d") if published else "",
        "time_full": published.strftime("%Y-%m-%d %H:%M") if published else "",
        "relative": _relative(published, now),
        # 速览优先（页内就能读完），没有就退回摘要；中文模式读中文版导读
        "digest": digest or strip_markdown(article.summary),
        "digest_zh": digest_zh,
        "digest_en": strip_markdown(article.digest_en or article.digest),
        # 降级时摘要与推荐理由是同一段兜底文本，卡片上并排显示两遍一模一样的话
        "reason": _reason_of(article, digest_zh, digest),
        "score": article.score,
        "category": article.category or "",
        "topics": _topics_of(article),
        "tags": split_tags(article.tags),
        "images": _images(article.image_urls, article.media_map),
        # 「降级」按读者看到的样子判定：有英文原文、却还没中文版。
        # 不按 status 判 —— 重排队期间 status 是 pending，读者看到的还是英文，
        # 这时候把提示收掉等于假装没问题。
        "degraded": bool(article.status != "processed" and is_english(article.digest or article.summary or "")),
        "degraded_reason": article.degraded_reason or "",
        # 中文版还在排队（第几次尝试、什么时候试的）
        "pending_translation": bool(article.status == "failed" and not str(article.digest_zh or "").strip()),
        "attempts": article.process_attempts or 0,
    }


def articles_of_day(session: Session, date_str: str, now: datetime | None = None) -> list[dict[str, Any]]:
    """某一天进日报的全部文章（卡片数据）。非法日期当作「这一天没有内容」。"""
    now = now or now_local()
    try:
        rows = list(session.execute(_day_statement(date_str)))
    except (ValueError, OverflowError):
        # OverflowError 同样要接：/daily/9999-12-31 能过 strptime，但
        # day_window 里的 start + timedelta(days=1) 会溢出成 500。
        return []
    return [_card(article, name, url, now) for article, name, url in rows]


def _day_noon(date_str: str) -> datetime | None:
    """把 ``YYYY-MM-DD`` 锚到当天正午。

    锚在正午而不是当天 00:00，是因为 ``_relative`` 遇到未来时刻会返回空串：
    拿 00:00 去比，当天早上（12:00 之前）每一份日报都会显示不出相对时间。
    日期坏掉就返回 ``None``，调用方跳过 —— ``DailyReport.date`` 只是个
    ``String(10)``，没有约束，手工改坏一行不该让整个 /archive 500。
    """
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").replace(hour=12)
    except (ValueError, OverflowError):
        return None


def _date_label(date_str: str) -> tuple[str, str]:
    try:
        day = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return date_str, ""
    return f"{day.month}月{day.day}日", f"星期{WEEKDAYS[day.weekday()]}"


def group_by_date(cards: list[dict[str, Any]], date_str: str) -> list[dict[str, Any]]:
    """按日期分组，组头显示「10月3日 星期六 · N 条」。单日时只出一个组。"""
    if not cards:
        return []
    label, weekday = _date_label(date_str)
    return [{"date": label, "weekday": weekday, "count": len(cards), "items": cards}]


def group_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """搜索结果可能跨天：按 YYYY-MM-DD 分组，保持时间倒序。"""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for card in cards:
        buckets.setdefault(card["date_key"], []).append(card)
    groups = []
    for date_key in sorted(buckets, reverse=True):
        label, weekday = _date_label(date_key)
        items = buckets[date_key]
        groups.append({"date": label, "weekday": weekday, "count": len(items), "items": items})
    return groups


def filter_by(
    cards: list[dict[str, Any]], *, category: str | None = None, tag: str | None = None
) -> list[dict[str, Any]]:
    """按分类 / 标签过滤卡片。分类与标签都在应用层过滤，语义更准。"""
    result = cards
    if category:
        result = [card for card in result if card["category"] == category]
    if tag:
        needle = tag.strip().casefold()
        result = [card for card in result if any(t.casefold() == needle for t in card["tags"])]
    return result


def paginate(cards: list[dict[str, Any]], page: int, size: int = PAGE_SIZE) -> tuple[list[dict[str, Any]], int, int]:
    """先过滤后分页：返回 ``(当前页卡片, 总条数, 总页数)``。

    必须按这个顺序：分类 / 标签是在应用层过滤的（标签是逗号串，用 SQL LIKE
    会把「AI」误配到「AI Agent」）。先按数据库分页再过滤的话，总条数与页数
    算的是**过滤前**的数量 —— 翻到第 3 页可能一条都没有，而页码还显示有 5 页。
    """
    total = len(cards)
    pages = max(1, -(-total // size))
    page = max(1, min(page, pages))
    start = (page - 1) * size
    return cards[start : start + size], total, pages


def _settings(request: Request) -> Settings | None:
    return getattr(request.app.state, "settings", None)


def _topics(request: Request) -> list[str]:
    settings = _settings(request)
    return list(getattr(settings, "research_topics", []) or [])


def _categories(request: Request) -> list[dict[str, str]]:
    settings = _settings(request)
    return [{"name": c.name, "hint": c.hint} for c in getattr(settings, "categories", []) or []]


# 中文与英文的阅读速度差别很大，用同一个数字估会差出一倍。
# 取值参考常见的中英文阅读速度区间（约 300~500 字/分、200~250 词/分）。
_CJK_PER_MINUTE = 400
_LATIN_PER_MINUTE = 230


def _structure_matches(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> bool:
    """中英两版的分节结构是否一致（逐位看是不是同一类块）。"""
    if not left or not right or len(left) != len(right):
        return False
    return all(bool(a.get("heading")) == bool(b.get("heading")) for a, b in zip(left, right, strict=True))


def _split_sections(blocks: list[dict[str, Any]]) -> list[tuple[int, list[dict[str, Any]]]]:
    """按小标题把块列表切成节，返回 ``[(节的起始块下标, [块, …])]``。

    节 = 一个小标题块 + 它后面的段落块；开头没有标题的块自己算一节。
    """
    sections: list[tuple[int, list[dict[str, Any]]]] = []
    for index, block in enumerate(blocks):
        if block.get("heading") or not sections:
            sections.append((index, []))
        sections[-1][1].append(block)
    return sections


def _pair_sections(
    en_blocks: list[dict[str, Any]],
    en_slots: list[list[dict[str, Any]]],
    zh_blocks: list[dict[str, Any]],
    zh_slots: list[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """把中英两套块按**节**配对，供模板做「中文一节 + 英文原文引用」的交错排版。

    走到这里时两套块的分节结构已由 ``_structure_matches`` 保证一致（或已双双
    退回平铺），所以按标题切开后节数相同、起始下标也对齐；配图槽按同样的
    下标切给各节，图片在节内的相对位置不变。

    脏数据兜底：万一节切不齐，整篇配成一对（视觉退回「中文在上、英文在下」），
    绝不让英文凭空消失 —— 模板拿到空列表会只渲染中文，那比错位更糟。
    """
    if not en_blocks or not zh_blocks:
        return []
    en_sections = _split_sections(en_blocks)
    zh_sections = _split_sections(zh_blocks)
    en_starts = [start for start, _ in en_sections]
    zh_starts = [start for start, _ in zh_sections]
    if len(en_sections) != len(zh_sections) or en_starts != zh_starts:
        return [{
            "zh_blocks": zh_blocks, "zh_slots": list(zh_slots),
            "en_blocks": en_blocks, "en_slots": list(en_slots),
        }]
    en_bounds = en_starts + [len(en_blocks) + 1]
    zh_bounds = zh_starts + [len(zh_blocks) + 1]
    pairs: list[dict[str, Any]] = []
    for index, ((_, en_sec), (_, zh_sec)) in enumerate(zip(en_sections, zh_sections, strict=True)):
        pairs.append({
            "zh_blocks": zh_sec,
            "zh_slots": zh_slots[zh_bounds[index]:zh_bounds[index + 1]],
            "en_blocks": en_sec,
            "en_slots": en_slots[en_bounds[index]:en_bounds[index + 1]],
        })
    return pairs


def reading_stats(text: str) -> dict[str, Any]:
    """正文规模与预计阅读时长。

    中文按「汉字数 ÷ 400 字/分」，英文按「词数 ÷ 230 词/分」—— 混排时分别
    计数再相加。字数用**实际可见字符**（去掉空白），不然排版产生的换行和
    缩进会被算进去，显示出来的「1200 字」可能只有 900 字。
    """
    body = re.sub(r"\s+", " ", text or "").strip()
    if not body:
        return {"chars": 0, "words": 0, "units": 0, "minutes": 0, "label": "正文较短"}
    han = sum(1 for ch in body if "\u4e00" <= ch <= "\u9fff")
    latin_words = len(re.findall(r"[A-Za-z][A-Za-z'\-]*", body))
    units = han + latin_words
    minutes = han / _CJK_PER_MINUTE + latin_words / _LATIN_PER_MINUTE
    rounded = max(1, round(minutes))
    if han and not latin_words:
        size = f"{han:,} 字"
    elif latin_words and not han:
        size = f"{latin_words:,} 词"
    else:
        size = f"{han + latin_words:,} 字 / 词"
    return {
        "chars": han,
        "words": latin_words,
        "units": units,
        "minutes": rounded,
        "label": f"约 {size} · 预计 {rounded} 分钟读完",
    }


def _ctx(request: Request, **extra: Any) -> dict[str, Any]:
    return {
        "topics": _topics(request),
        "categories": _categories(request),
        # 导航高亮标记；不传就一个都不亮（base.html 里逐个比 nav 值）
        "nav": "",
        **extra,
    }


def _homepage_date(session: Session, now: datetime) -> str:
    """首页展示哪一天：最近一份**真有内容**的日报。

    原来直接取 ``max(DailyReport.date)``，于是每天 00:00 之后、第一批文章
    处理完之前，滚动任务先写下一份 0 篇的当日日报，首页就指向它 —— 页面
    全空，而昨天那份已经定稿的日报还躺在归档里。实测日志里
    「日报已生成：2026-10-05（0 篇）」连着出现四次，首页就空了约两小时。

    这里从最新往回找第一份真有文章的日期；一份都没有才退回今天。
    """
    reports = session.execute(
        select(DailyReport.date).order_by(DailyReport.date.desc()).limit(14)
    ).scalars().all()
    counts = counts_by_day(session, list(reports))
    for date_str in reports:
        if counts.get(date_str, 0) > 0:
            return str(date_str)
    return now.strftime("%Y-%m-%d")


@page_router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    page: int = Query(1, ge=1),
    cat: str | None = None,
    tag: str | None = None,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """首页：热点资讯时间轴。按时间倒序 + 分页，可用 ?cat= / ?tag= 过滤。"""
    now = now_local()
    date_str = _homepage_date(session, now)
    latest = session.execute(
        select(DailyReport).where(DailyReport.date == date_str)
    ).scalar_one_or_none()
    # 先过滤再分页：过滤是在应用层做的（见 paginate 的注释），
    # 反过来的话总条数与页数算的是过滤前的数量，翻页会翻出空页。
    cards = filter_by(articles_of_day(session, date_str, now), category=cat, tag=tag)
    cards, total, pages = paginate(cards, page, PAGE_SIZE)
    return templates.TemplateResponse(
        request,
        "index.html",
        _ctx(
            request,
            nav="home",
            report=latest,
            date_str=date_str,
            groups=group_by_date(cards, date_str),
            articles=cards,
            total=total,
            page=page,
            pages=pages,
            active_cat=cat or "",
            active_tag=tag or "",
            title="热点资讯",
        ),
    )


@page_router.get("/search", response_class=HTMLResponse)
def search_page(
    request: Request,
    q: str = "",
    scope: str = SCOPE_META,
    cat: str | None = None,
    tag: str | None = None,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """站内搜索。``scope=full`` 时连正文一起搜。"""
    keyword = (q or "").strip()
    effective_scope = normalize_scope(scope)
    rows = (
        search_articles(session, keyword, scope=effective_scope, category=cat, tag=tag)
        if keyword
        else []
    )
    counts = (
        count_by_category(session, keyword, scope=effective_scope, category=cat, tag=tag)
        if keyword
        else {}
    )
    now = now_local()
    cards = [_card(article, None, None, now) for article in rows]
    # 搜索结果里要显示来源名与域名，所以再查一次 source
    cards = _attach_sources(session, rows, cards, now)
    return templates.TemplateResponse(
        request,
        "search.html",
        _ctx(
            request,
            nav="home",
            q=keyword,
            scope=effective_scope,
            scope_label=SCOPE_LABELS.get(effective_scope, ""),
            groups=group_cards(cards),
            total=len(cards),
            # found 是**真实命中数**，不是这一页渲染了多少条。两者相等就用同一个
            # 数字；不等就两个都显示并说清楚还有多少条没渲染 —— 静默截断会让
            # 「找到 200 条」和 tab 上的 247 并排出现，而那 47 条谁都翻不到。
            found=counts.get("_all", 0) if counts else 0,
            shown=len(rows),
            counts=counts,
            active_cat=cat or "",
            active_tag=tag or "",
            searched_at=now,
            title=f'搜索 "{keyword}"' if keyword else "搜索",
            **search_metadata(),
        ),
    )


def _attach_sources(
    session: Session, rows: list[Article], cards: list[dict[str, Any]], now: datetime
) -> list[dict[str, Any]]:
    """给搜索结果补上来源名与域名（一次查询搞定，不做 N+1）。"""
    if not rows:
        return cards
    ids = {a.source_id for a in rows if a.source_id is not None}
    names: dict[int, tuple[str | None, str | None]] = {}
    if ids:
        names = {
            source_id: (name, url)
            for source_id, name, url in session.execute(
                select(Source.id, Source.name, Source.url).where(Source.id.in_(ids))
            )
        }
    for article, card in zip(rows, cards, strict=True):
        name, url = names.get(article.source_id, (None, None)) if article.source_id is not None else (None, None)
        card["source"] = name or "未知来源"
        card["source_host"] = _host(url)
    return cards


@page_router.get("/saved", response_class=HTMLResponse)
def saved_page(request: Request) -> HTMLResponse:
    """收藏页：收藏存在浏览器 localStorage，这里只提供一个空壳页面。"""
    return templates.TemplateResponse(request, "saved.html", _ctx(request, nav="saved", title="我的收藏"))


@page_router.get("/daily/{date}", response_class=HTMLResponse)
def daily(
    request: Request,
    date: str,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    report = session.execute(select(DailyReport).where(DailyReport.date == date)).scalar_one_or_none()
    now = now_local()
    cards = articles_of_day(session, date, now)
    return templates.TemplateResponse(
        request,
        "daily.html",
        _ctx(
            request,
            nav="archive",
            report=report,
            articles=cards,
            groups=group_by_date(cards, date),
            date=date,
            # 头部显示的篇数必须来自**下面那份列表**，不能读日报里冻结的
            # article_count。日报是 08:00 定稿的快照，之后补处理完的文章、
            # 或者合并掉重复项，列表就会变 —— 于是同一屏上「共 12 篇」下面
            # 摆着 14 张卡片（/daily/2026-09-30 更离谱：写着 2 篇、列了 12 篇）。
            article_count=len(cards),
            title=f"{date} 日报",
        ),
        status_code=200 if report else 404,
    )


@page_router.get("/story/{article_id}", response_class=HTMLResponse)
def story(
    request: Request, article_id: IdParam, session: Session = Depends(get_session)
) -> HTMLResponse:
    """单篇页内预览：左栏来源信息 + 右侧完整正文，读者不用跳原站。"""
    row = session.execute(
        select(Article, Source.name, Source.url)
        .outerjoin(Source, Article.source_id == Source.id)
        .where(Article.id == article_id)
    ).first()
    if row is None:
        return templates.TemplateResponse(
            request, "story.html", _ctx(request, item=None, title="内容不存在"), status_code=404
        )
    article, source_name, source_url = row
    now = now_local()
    item = _card(article, source_name, source_url, now)
    # 同分类 / 同标签的邻居，方便顺着标签继续读
    item["related"] = _related(session, article, now)
    # 正文按段落切开，并标出哪些段落是「小标题」，供页面渲染目录与 <h3>
    # is_real_body 会把 Reddit 那种只剩模板套话的 description 判成「没有正文」，
    # 免得详情页渲染出一个只写着 Comments 的空区块，看着像抓取坏了。
    def _body(*candidates: str | None) -> str:
        return next((c for c in candidates if c and is_real_body(c)), "") or ""

    original = _body(article.content_full, article.content)
    # AI 章节优先：有就是编辑排好的小标题，没有就回退启发式猜
    marked = _section_blocks(article.body_sections, prefix="sec-en") or mark_headings(
        _body_blocks(original), prefix="sec-en"
    )
    item["body_blocks"] = marked
    item["content_preview"] = truncate(original, 400)
    # 正文内联配图：按原站的做法插在段落之间，位置随抓取时一起存下来
    anchors = _body_image_anchors(article.body_images)
    item["shots"] = _image_slots_for_blocks(anchors, marked)
    item["has_body_images"] = bool(anchors)
    # 英文原文另有中文版：中文/双语模式读译文，英文模式读原文。
    # 展示前再校验一次「这算不算整篇译完」：库里可能存着早年写坏的数据
    # （文章 313 正文 1249 字、content_zh 只有 64 字）。不校验的话页面会
    # 认它有译文，双语模式正文只剩一个中文段、英文却是完整六段，
    # 读者看着像没译完。宁可显示原文 + 「暂无中文译文」。
    translated = article.content_zh or ""
    if translated.strip() and not translation_is_usable(original, translated):
        log.info("文章 %s 的译文比例过低（%d/%d），按无译文处理",
                 article.id, len(translated.strip()), len(original))
        translated = ""
    item["has_translation"] = bool(translated.strip())
    marked_zh = (
        (_section_blocks(article.body_sections_zh, prefix="sec-zh")
         or mark_headings(_body_blocks(translated), prefix="sec-zh"))
        if item["has_translation"]
        else []
    )
    item["body_blocks_zh"] = marked_zh
    # 双语模式下两个语言版本的**结构必须对称**。
    #
    # 症状（用户图 2 报的）：中文那一半有小标题横幅，英文那一半没有 ——
    # 读者眼里就成了「有些段落双语、有些只有一种语言」。根因是历史上只给中文
    # 译文存了章节结构（body_sections_zh 有 30 篇，body_sections 只有 3 篇）。
    #
    # 这里做一道展示层的兜底：两边的小标题数量/位置对不上就**一起退回平铺**。
    # 与其显示一个必然错位的组合，不如两个版本都用同一种朴素排版 —— 少了
    # 横幅，但读者看到的是一份对称、可对照的正文。新入库的文章由同一次排版
    # 派生两套结构，永远对称；这条兜底只保护存量数据。
    if item["has_translation"] and not _structure_matches(marked, marked_zh):
        log.info("文章 %s 的中英结构不一致（%d vs %d 段小标题），"
                 "双语视图退回平铺正文", article.id,
                 sum(1 for b in marked if b.get("heading")),
                 sum(1 for b in marked_zh if b.get("heading")))
        marked = mark_headings(_body_blocks(original), prefix="sec-en", want_headings=False)
        marked_zh = mark_headings(_body_blocks(translated), prefix="sec-zh", want_headings=False)
        item["body_blocks"] = marked
        item["body_blocks_zh"] = marked_zh
        item["shots"] = _image_slots_for_blocks(anchors, marked)
        item["toc"] = []
    item["content_preview_zh"] = truncate(translated, 400) if item["has_translation"] else ""
    plain_en = sum(1 for b in marked if not b.get("heading"))
    plain_zh = sum(1 for b in marked_zh if not b.get("heading"))
    item["shots_zh"] = _image_slots_for_blocks(_shift_anchors(anchors, plain_en, plain_zh), marked_zh)
    # 双语正文按**节**交错（用户图 1 的诉求）：中文一节读完，紧跟这一节的英文
    # 原文（引用样式），而不是「整篇中文 → 整篇英文」两大块 —— 后者读者要滚过
    # 全部中文才能对到第一段英文，滚着滚着就以为双语没了。
    # 按「节」而不是按「段」配对：译文是重新写成的中文，段数与原文本来就不对应
    # （实测 45 段压成 15 段），段级交替必然错位；节级配对由上面的结构对称
    # 校验兜底 —— 走到这里两边的节边界一定一致（对称，或已双双退回平铺一节）。
    item["body_pairs"] = _pair_sections(marked, item["shots"], marked_zh, item["shots_zh"])
    # 本文目录指向**读者当前看到的那一版**：有译文就指译文的小标题。
    # 译文的段落数可能与原文不同（翻译失败的那几段会被丢掉），
    # 拿原文的下标去点译文的标题会跳错位置甚至跳到不存在的锚点。
    item["toc"] = table_of_contents(marked_zh if item["has_translation"] else marked)
    # 没有译文时要说清楚，避免「中文模式却整页英文」看着像坏了
    item["body_is_foreign"] = bool(original.strip()) and not item["has_translation"] and is_english(original)
    item["body_missing"] = not original.strip()
    # 阅读时长按「读者默认看到的那一版」算：有译文就算译文（默认中文模式），
    # 没译文才算原文。给英文原文报中文读法的时间是错的。
    item["reading"] = reading_stats(translated if item["has_translation"] else original)
    # 这个页面**实际提供哪几种语言**。中文原生文章不翻译成英文，所以页面上
    # 不该出现 EN / 双语按钮 —— 给一个永远切不出内容的按钮比不给更糟。
    # 这也是判断「要不要做英文版」的唯一依据：有英文原文才需要中文版，
    # 本来就是中文的原文不需要任何翻译。
    item["native_zh"] = bool(original.strip()) and not is_english(original)
    item["langs"] = ["zh"] if item["native_zh"] else ["zh", "en"]
    # 导读同理：英文原文还没有中文导读时，页面上说清楚，别让「中文」模式看着像坏了
    item["digest_missing_zh"] = (
        is_english(article.digest or "") and not str(article.digest_zh or "").strip()
    )
    # 同题合并：这条是重复稿就指回主条目；是主条目就把别家的同题报道列出来，
    # 合并只是不重复展示，不是把内容删掉
    primary = primary_of(session, article)
    item["duplicate_of_id"] = primary.id if primary else 0
    item["duplicate_of_title"] = (primary.title_zh or primary.title) if primary else ""
    item["duplicate_of_source"] = _source_name(session, primary.source_id) if primary else ""
    item["also_reported"] = [
        {"id": row.id, "source": _source_name(session, row.source_id), "title": row.title_zh or row.title}
        for row in duplicates_of(session, article.id)
    ]
    return templates.TemplateResponse(
        request,
        "story.html",
        _ctx(
            request, nav="home", item=item, title=article.title[:40],
            # 中文原生文章不提供英文版，页面上就不该出现语言切换
            langs=item["langs"],
        )
    )


def _source_name(session: Session, source_id: int | None) -> str:
    if source_id is None:
        return "未知来源"
    return session.execute(select(Source.name).where(Source.id == source_id)).scalar() or "未知来源"


def _related(session: Session, article: Article, now: datetime, *, limit: int = 6) -> list[dict[str, Any]]:
    """找同类文章：优先同分类，其次同标签。"""
    if not article.category and not article.tags:
        return []
    needle_tags = {t.strip().casefold() for t in (article.tags or "").split(",") if t.strip()}
    rows = list(
        session.execute(
            select(Article, Source.name, Source.url)
            .outerjoin(Source, Article.source_id == Source.id)
            .where(
                Article.id != article.id,
                Article.relevance == 1,
                Article.status.in_(STATUS_REPORTABLE),
            Article.duplicate_of.is_(None),
            )
            .order_by(Article.published_at.desc(), Article.id.desc())
            .limit(120)
        )
    )
    scored: list[tuple[int, dict[str, Any]]] = []
    for candidate, name, url in rows:
        points = 0
        if article.category and candidate.category == article.category:
            points += 2
        points += len({t.strip().casefold() for t in (candidate.tags or "").split(",")} & needle_tags)
        if points:
            scored.append((points, _card(candidate, name, url, now)))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [card for _, card in scored[:limit]]


@page_router.get("/archive", response_class=HTMLResponse)
def archive(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    """历史日报：按日期倒序列出，点日期展开当天标题（内容按需再拉，不一次性塞满页面）。"""
    # ARCHIVE_PAGE_SIZE 只是「先渲染这么多」，真正的总数另外查。
    # 原来把 120 当成了总数直接印在页面上（「共 120 份」），一旦日报超过 120 份
    # 就会撒谎：更早的日子既翻不到、也没人知道它们存在。
    reports = list(
        session.execute(
            select(DailyReport).order_by(DailyReport.date.desc()).limit(ARCHIVE_PAGE_SIZE)
        ).scalars()
    )
    total_reports = int(
        session.execute(select(func.count(DailyReport.id))).scalar_one() or 0
    )
    now = now_local()
    rows = []
    # 用实时条数（而不是日报快照里的 article_count）画图与标数：点开某一天时
    # 拉的是 /api/articles?date=…，那是实时结果。数字得和它一致。
    live = counts_by_day(session, [row.date for row in reports])
    peak = max(live.values(), default=0) or 1
    for row in reports:
        label, weekday = _date_label(row.date)
        count = live.get(row.date, 0)
        rows.append(
            {
                "date": row.date,
                "label": label,
                "weekday": weekday,
                "count": count,
                # 用条形长度直观对比哪天抓得多
                "width": max(6, round(count / peak * 100)),
                # 存了个坏日期行也不该整页 500：strptime 失败就当没有相对时间
                "relative": _relative(_day_noon(row.date), now),
                "href": f"/daily/{row.date}",
            }
        )
    return templates.TemplateResponse(
        request,
        "archive.html",
        _ctx(
            request, nav="archive", reports=rows, total=total_reports,
            truncated=total_reports > len(rows),
            shown=len(rows),
            title="历史日报",
        ),
    )