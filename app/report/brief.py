"""早报：按用户口味从近期文章里精选 TOP N，用文章自带的「早报片段」拼装成稿。

与日报的区别：
- 日报（``app/report/generator.py``）是整天全量快照，08:00 定稿；
- 早报是**精选**：TOP N（默认 10）、按分类/主题/标签/关键词自由定制，
  内容直接继承每篇文章处理阶段写好的 ``Article.brief_zh``（早报片段），
  拼装时**不再调用模型** —— 不花一次 LLM 调用就能出稿。

配置存 ``brief_config`` 单行表（网页可改、即时生效），读取时缺行自动补默认。
输出是纯文本/Markdown，手机推送、复制到微信、交给 Agent 都直接用。
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Article, BriefConfig, BriefIssue, visible_article_conditions
from app.report.generator import STATUS_REPORTABLE
from app.utils.text import (
    BRIEF_DIGEST_CHARS,
    brief_digest,
    chinese_ratio,
    has_long_latin_run,
    now_local,
    strip_markdown,
)

# 早报片段的最低要求（与 api._digest_with_fallback 同一口径）
BRIEF_MIN_CHARS = 70

# TOP N 与时间窗的硬边界（页面输入框也有同样限制，这里兜底）
MAX_TOP_N = 50
MAX_DAYS = 7

# 排序方式
SORT_SCORE = "score"
SORT_TIME = "time"
SORT_CHOICES = (SORT_SCORE, SORT_TIME)


def _json_list(raw: str | None) -> list[str]:
    """读 JSON 数组字段；坏数据一律当空，不让一条坏配置把页面打崩。"""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


def dump_list(values: list[str] | None) -> str | None:
    clean = [str(v).strip() for v in (values or []) if str(v).strip()]
    return json.dumps(clean, ensure_ascii=False) if clean else None


def load_brief_config(session: Session) -> BriefConfig:
    """读配置（单行表，id=1）；没有就创建默认行。"""
    row = session.get(BriefConfig, 1)
    if row is None:
        row = BriefConfig(id=1, top_n=10, days=1, starred_only=0, sort=SORT_SCORE)
        session.add(row)
        session.flush()
    return row


def config_view(row: BriefConfig) -> dict[str, Any]:
    """给模板/接口用的可序列化视图。"""
    return {
        "top_n": int(row.top_n or 10),
        "days": int(row.days or 1),
        "categories": _json_list(row.categories),
        "topics": _json_list(row.topics),
        "tags": _json_list(row.tags),
        "keywords": _json_list(row.keywords),
        "exclude": _json_list(row.exclude),
        "starred_only": bool(row.starred_only),
        "sort": row.sort if row.sort in SORT_CHOICES else SORT_SCORE,
        "sections": sections_view(row),
    }


# 分节（每节 = 一张微信长图）的硬边界
MAX_SECTIONS = 12
SECTION_NAME_CHARS = 20
# 节点类型：news = 从文章流精选；text = 固定文字；weather = 天气卡片
NODE_NEWS = "news"
NODE_TEXT = "text"
NODE_WEATHER = "weather"
NODE_TYPES = (NODE_NEWS, NODE_TEXT, NODE_WEATHER)
TEXT_CHARS = 800


def _clean_node_type(raw: object) -> str:
    value = str(raw or "").strip()
    return value if value in NODE_TYPES else NODE_NEWS


def sections_view(row: BriefConfig) -> list[dict[str, Any]]:
    """读分节配置（= 流水线节点）；坏数据当空（= 用全局筛选出单节）。"""
    if not row.sections:
        return []
    try:
        parsed = json.loads(row.sections)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    out: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:SECTION_NAME_CHARS]
        node_type = _clean_node_type(item.get("type"))
        entry: dict[str, Any] = {
            "type": node_type,
            "name": name or f"第 {len(out) + 1} 节",
            "enabled": bool(item.get("enabled", True)),
        }
        if node_type == NODE_NEWS:
            entry.update(
                {
                    "top_n": _clean_int(item.get("top_n"), default=10, low=1, high=MAX_TOP_N),
                    "categories": _as_str_list(item.get("categories")),
                    "topics": _as_str_list(item.get("topics")),
                    "tags": _as_str_list(item.get("tags")),
                    "keywords": _as_str_list(item.get("keywords")),
                }
            )
        elif node_type == NODE_TEXT:
            entry.update(
                {
                    "title": str(item.get("title") or "").strip()[:80],
                    "text": str(item.get("text") or "").strip()[:TEXT_CHARS],
                }
            )
        else:
            cities = _as_str_list(item.get("cities"))
            if not cities and item.get("city"):  # 老配置（单城市字段）兼容
                cities = _as_str_list(item.get("city"))
            entry.update(
                {
                    "cities": cities,
                    "text": str(item.get("text") or "").strip()[:TEXT_CHARS],
                }
            )
        out.append(entry)
    return out[:MAX_SECTIONS]


def _clean_int(raw: object, *, default: int, low: int, high: int) -> int:
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        return default
    return min(high, max(low, value))


def save_brief_config(session: Session, form: dict[str, Any]) -> BriefConfig:
    """把表单值写进配置行。所有字段都容错，坏值回默认而不是报错。

    语义是「整表覆盖」而不是「部分更新」：表单页每次渲染所有字段，
    提交时**没出现的字段 = 用户清空了它**（复选框全不选就是「不限」）。
    早先按「出现才写」处理，于是取消勾选全部分类保存后旧值还在 ——
    界面上看着已清空、实际过滤还在生效，是个很难发现的坑。

    例外：``sections``（流水线节点）只在字段出现时覆盖 —— 旧版本页面与
    脚本可能不带这个字段，缺席就保留现有节点，而不是把用户的流水线清空。
    """
    row = load_brief_config(session)
    row.top_n = _clean_int(form.get("top_n"), default=10, low=1, high=MAX_TOP_N)
    row.days = _clean_int(form.get("days"), default=1, low=1, high=MAX_DAYS)
    for field in ("categories", "topics", "tags", "keywords", "exclude"):
        value = form.get(field)
        if isinstance(value, list):
            setattr(row, field, dump_list([str(v) for v in value]))
        elif isinstance(value, str):
            # 逗号/顿号/换行分隔都认
            parts = [p.strip() for p in value.replace("，", ",").replace("、", ",").replace("\n", ",").split(",")]
            setattr(row, field, dump_list(parts))
        else:
            # 字段缺席（复选框全不选 / 未提交）= 清空
            setattr(row, field, None)
    row.starred_only = 1 if str(form.get("starred_only", "")).strip() in ("1", "true", "on") else 0
    sort = str(form.get("sort") or "").strip()
    row.sort = sort if sort in SORT_CHOICES else SORT_SCORE
    if "sections" in form:
        row.sections = _clean_sections_json(form.get("sections"))
    row.updated_at = now_local()
    session.flush()
    return row


def _as_str_list(raw: object) -> list[str]:
    """字段值容错：列表照收；字符串按逗号拆（前端旧版本可能提交字符串）。"""
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if isinstance(raw, str):
        return [p.strip() for p in raw.replace("，", ",").replace("、", ",").split(",") if p.strip()]
    return []


def _clean_sections_json(raw: object) -> str | None:
    """流水线节点表单字段（前端序列化成 JSON 字符串）→ 干净的 JSON。

    节点三种类型：
    - ``news``：从文章流精选（top_n + 分类/主题/标签/关键词）；
    - ``text``：固定文字卡（title + text）；
    - ``weather``：天气卡（city + text，内容由用户填写/更新）。
    每个节点带 ``enabled``；坏数据跳过，不让一条坏配置把页面打崩。
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return None
    else:
        parsed = raw
    if not isinstance(parsed, list) or not parsed:
        return None
    clean: list[dict[str, Any]] = []
    for item in parsed[:MAX_SECTIONS]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:SECTION_NAME_CHARS]
        node_type = _clean_node_type(item.get("type"))
        entry: dict[str, Any] = {
            "type": node_type,
            "name": name or f"第 {len(clean) + 1} 节",
            "enabled": bool(item.get("enabled", True)),
        }
        if node_type == NODE_NEWS:
            entry.update(
                {
                    "top_n": _clean_int(item.get("top_n"), default=10, low=1, high=MAX_TOP_N),
                    "categories": _as_str_list(item.get("categories")),
                    "topics": _as_str_list(item.get("topics")),
                    "tags": _as_str_list(item.get("tags")),
                    "keywords": _as_str_list(item.get("keywords")),
                }
            )
        elif node_type == NODE_TEXT:
            entry.update(
                {
                    "title": str(item.get("title") or "").strip()[:80],
                    "text": str(item.get("text") or "").strip()[:TEXT_CHARS],
                }
            )
        else:
            cities = _as_str_list(item.get("cities"))
            if not cities and item.get("city"):  # 老配置（单城市字段）兼容
                cities = _as_str_list(item.get("city"))
            entry.update(
                {
                    "cities": cities,
                    "text": str(item.get("text") or "").strip()[:TEXT_CHARS],
                }
            )
        clean.append(entry)
    return json.dumps(clean, ensure_ascii=False) if clean else None


def _contains(haystack: str, needle: str) -> bool:
    """大小写不敏感包含（英文词用词边界由关键词模块负责；早报这里按用户直觉做包含匹配）。"""
    return needle.casefold() in haystack.casefold()


def article_matches(article: Article, cfg: dict[str, Any]) -> bool:
    """过滤：分类/主题/标签/关键词/排除词。空条件 = 不限。"""
    cats = cfg["categories"]
    if cats and (article.category or "") not in cats:
        return False

    topics = _article_topics(article)
    if cfg["topics"] and not any(t in topics for t in cfg["topics"]):
        return False

    tags = [t.strip() for t in (article.tags or "").split(",") if t.strip()]
    if cfg["tags"] and not any(t in tags for t in cfg["tags"]):
        return False

    haystack = " ".join(
        filter(None, [article.title or "", article.title_zh or "", article.summary or "",
                      article.digest or "", article.digest_zh or "", article.brief_zh or "",
                      article.reason or ""])
    )
    if cfg["keywords"] and not any(_contains(haystack, kw) for kw in cfg["keywords"]):
        return False
    return not (cfg["exclude"] and any(_contains(haystack, kw) for kw in cfg["exclude"]))


def _article_topics(article: Article) -> list[str]:
    return _json_list(article.topics)


def collect_brief_articles(session: Session, cfg: dict[str, Any], *, now: Any = None) -> list[Article]:
    """按配置取文章（已过滤、已排序、已截断到 TOP N）。

    排序：特别关注永远最前；组内按 ``sort``（评分或时间）倒序。
    """
    moment = now or now_local()
    start = moment - timedelta(days=cfg["days"])
    statement = (
        select(Article)
        .where(
            Article.relevance == 1,
            Article.status.in_(STATUS_REPORTABLE),
            *visible_article_conditions(),
            Article.published_at >= start,
            # 上界：抓取层虽然会把未来 pubDate 钳到当前，但手工/脚本写入的
            # 未来时间不该进早报（否则它今天出现、明天又出现）
            Article.published_at <= moment,
        )
        .order_by(Article.published_at.desc(), Article.id.desc())
        .limit(400)
    )
    if cfg["starred_only"]:
        statement = statement.where(Article.starred == 1)
    rows = [a for a in session.execute(statement).scalars() if article_matches(a, cfg)]

    if cfg["sort"] == SORT_TIME:
        rows.sort(key=lambda a: (0 if a.starred else 1,
                                 -(a.published_at.timestamp() if a.published_at else 0)))
    else:
        rows.sort(key=lambda a: (0 if a.starred else 1, -(a.score or 0),
                                 -(a.published_at.timestamp() if a.published_at else 0)))
    return rows[: cfg["top_n"]]


def _section_subtitle(section: dict[str, Any], cfg: dict[str, Any]) -> str:
    """新闻节点的副标题：把筛选条件摊开（长图标题下的小字）。

    用户要求：不限时显示排序方式（评分优先/时间优先）；选了分类/主题/
    标签/关键词就逐项列出 —— 一眼知道这一节收的是什么。
    """
    parts: list[str] = []
    if section.get("categories"):
        parts.append("分类：" + "/".join(section["categories"]))
    if section.get("topics"):
        parts.append("主题：" + "/".join(section["topics"]))
    if section.get("tags"):
        parts.append("标签：#" + " #".join(section["tags"]))
    if section.get("keywords"):
        parts.append("关键词：" + " ".join(section["keywords"]))
    if cfg.get("starred_only"):
        parts.append("只看特别关注")
    sort_label = "评分优先" if cfg.get("sort") != SORT_TIME else "时间优先"
    if not parts:
        parts.append("全部相关新闻 · " + sort_label)
    else:
        parts.append(sort_label)
    return " · ".join(parts)


def collect_brief_sections(
    session: Session, cfg: dict[str, Any], *, now: Any = None
) -> list[dict[str, Any]]:
    """按流水线节点逐节产出；没有节点时回退成「全局筛选的单节」。

    节点三种类型：
    - ``news``：从文章流按该节点的筛选条件精选（``articles``）；
    - ``text``：固定文字卡；
    - ``weather``：按城市拉真实天气（Open-Meteo），渲染成 emoji + Markdown。

    停用（``enabled=False``）的节点**不参与产出**（预览与成稿都不出现），
    但在配置里保留 —— 停用是「暂时不要」，不是删除。
    文章**不跨节去重**：同一篇同时命中两个节点是合理的（不同图不同角度）。
    空新闻节点附带 ``hint``：说明为什么没选到（数据库里没有匹配 / 时间窗太窄），
    页面据此给出明确提示，而不是一片空白（用户反馈）。
    """
    from app.report.weather import enrich_weather_node

    sections = cfg.get("sections") or []
    if not sections:
        entries = brief_items(collect_brief_articles(session, cfg, now=now))
        return [{"name": "", "type": NODE_NEWS, "articles": [], "meta": {},
                 "entries": entries, "hint": _empty_hint(session, cfg, entries, now=now)}]

    out: list[dict[str, Any]] = []
    for section in sections:
        if not section.get("enabled", True):
            continue
        node_type = section.get("type", NODE_NEWS)
        if node_type == NODE_NEWS:
            sub: dict[str, Any] = {
                "top_n": section["top_n"],
                "days": cfg["days"],
                "categories": section["categories"],
                "topics": section["topics"],
                "tags": section["tags"],
                "keywords": section["keywords"],
                "exclude": cfg["exclude"],
                "starred_only": cfg["starred_only"],
                "sort": cfg["sort"],
            }
            articles = collect_brief_articles(session, sub, now=now)
            entries = brief_items(articles)
            out.append({
                "name": section["name"],
                "type": NODE_NEWS,
                "articles": articles,
                "meta": {
                    "top_n": section["top_n"],
                    "subtitle": _section_subtitle(section, cfg),
                },
                "entries": entries,
                "hint": _empty_hint(session, sub, entries, now=now),
            })
        elif node_type == NODE_TEXT:
            out.append({
                "name": section["name"],
                "type": NODE_TEXT,
                "articles": [],
                "meta": {
                    "title": section.get("title", ""),
                    "text": section.get("text", ""),
                },
                "entries": [],
                "hint": "",
            })
        else:
            enriched = enrich_weather_node(section)
            out.append({
                "name": section["name"],
                "type": NODE_WEATHER,
                "articles": [],
                "meta": {
                    "cities": section.get("cities", []),
                    "text": enriched.get("rendered", section.get("text", "")),
                    "raw_text": section.get("text", ""),
                },
                "entries": [],
                "hint": "",
            })
    return out


def _empty_hint(session: Session, cfg: dict[str, Any], entries: list[Any], *, now: Any = None) -> str:
    """空新闻节点的原因说明：帮用户立刻知道该改什么。"""
    if entries:
        return ""
    moment = now or now_local()
    start = moment - timedelta(days=cfg["days"])
    base = session.execute(
        select(func.count(Article.id)).where(
            Article.relevance == 1,
            Article.status.in_(STATUS_REPORTABLE),
            *visible_article_conditions(),
            Article.published_at >= start,
            Article.published_at <= moment,
        )
    ).scalar_one()
    if base == 0:
        return "时间范围内没有任何已处理的文章（数据库里没有匹配内容）——可放宽时间范围或等下一轮抓取处理"
    conditions: list[str] = []
    if cfg.get("categories"):
        conditions.append("分类：" + "/".join(cfg["categories"]))
    if cfg.get("topics"):
        conditions.append("主题：" + "/".join(cfg["topics"]))
    if cfg.get("tags"):
        conditions.append("标签：#" + " #".join(cfg["tags"]))
    if cfg.get("keywords"):
        conditions.append("关键词：" + " ".join(cfg["keywords"]))
    if cfg.get("exclude"):
        conditions.append("排除词：" + " ".join(cfg["exclude"]))
    if cfg.get("starred_only"):
        conditions.append("只看特别关注")
    cond_txt = "、".join(conditions) if conditions else "（无筛选条件）"
    return (
        f"时间范围内有 {base} 篇已处理文章，但没有一篇同时满足本节点条件（{cond_txt}）"
        "——放宽或去掉部分条件即可"
    )


# ── 早报片段（与 /api/articles/{id} 的 digest_brief 同一口径） ──────────


def _is_chinese_usable(text: str | None, *, strict: bool = False) -> bool:
    """能不能当早报文案用。

    ``strict=True``（AI 现写的 ``brief_zh``）：要求整段没有一句没翻的英文 ——
    推送语是成品文案，半中半英就是没写好，宁可重生成。

    ``strict=False``（导读/理由/正文等素材）：只看**整体**是不是以中文为主。
    实测踩过的坑：微软那条导读里「Surface Laptop Ultra」「RTX Spark SoC」
    「Windows 11」把单句拉丁占比拉到 0.25，逐句检查把它判成「没翻」，
    于是早报片段退回成了标题 —— 一段好好的中文导读被丢掉了。
    整段比例 0.3 的宽松线既保住这类稿子，又拦得住真正没翻的英文。
    """
    clean = (text or "").strip()
    if not clean:
        return False
    if strict:
        return chinese_ratio(clean) >= 0.4 and not has_long_latin_run(clean)
    return chinese_ratio(clean) >= 0.3


def _body_excerpt(article: Article, *, limit: int = 400) -> str:
    """正文节选：中文版优先，供「导读撑不起一段话」时取材。"""
    for attr in ("content_zh", "content_full", "content"):
        text = strip_markdown(getattr(article, attr, None) or "")
        if text and _is_chinese_usable(text):
            return text[:limit]
    return ""


def brief_text(article: Article, *, allow_placeholder: bool = False) -> str:
    """早报片段：一段**完整的中文**话。

    取值顺序：
    1. AI 现写的早报片段（``brief_zh``，最优先）；
    2. 中文导读 / 中文推荐理由 / 摘要（宽松中文判定，产品名不误伤）；
    3. **正文节选**——没有导读或导读太短时，从正文里取开头一段话，
       让每条早报都能像其他条目一样读（用户反馈：只有标题没有意义）；
    4. 中文标题兜底。

    英文原文不参与（推到手机上是半中半英就没法读了）。

    ``allow_placeholder=True``（单篇页的早报片段浮层）：全都没有时返回
    「（标题）中文译文还在整理中」让页面有话说；默认 ``False``（早报成稿）：
    返回空串，调用方据此**把这条从早报里剔除** —— 推送里混一条占位符
    还不如少一条（用户反馈：这样的信息根本没有意义）。
    """
    stored = (getattr(article, "brief_zh", None) or "").strip()
    if _is_chinese_usable(stored, strict=True):
        return stored

    for candidate in (article.digest_zh, article.digest, article.reason, article.summary):
        if not _is_chinese_usable(candidate):
            continue
        brief = brief_digest(candidate)
        if len(brief) >= BRIEF_MIN_CHARS:
            return brief
        extra = brief_digest(
            article.reason if candidate is not article.reason else article.summary or "",
            limit=BRIEF_DIGEST_CHARS,
        )
        if _is_chinese_usable(extra) and extra not in brief:
            extra = extra.rstrip("，、；：,;: ")
            if extra:
                if extra[-1] in "。！？.!?":
                    merged = f"{brief}{extra}"
                else:
                    joiner = "" if brief[-1] in "。！？.!?" else "。"
                    merged = f"{brief}{joiner}{extra}。"
                if len(merged) <= BRIEF_DIGEST_CHARS + 20:
                    return merged
        if brief:
            return brief

    # 素材都撑不起一段话：从正文取材，截出完整的前几句
    body = _body_excerpt(article)
    if body:
        brief = brief_digest(body, limit=BRIEF_DIGEST_CHARS * 2)
        if brief and len(brief) >= 30:
            return brief

    title = (article.title_zh or article.title or "").strip()
    if chinese_ratio(title) >= 0.4:
        return brief_digest(title)
    if allow_placeholder:
        return f"（{title}）中文译文还在整理中。" if title else ""
    return ""


# ── 渲染 ────────────────────────────────────────────────────────────────


def brief_items(articles: list[Article]) -> list[dict[str, Any]]:
    """把 ORM 行转成渲染用的条目（标题、片段、来源、链接、时间）。

    没有可读片段的条目**直接剔除**：推送里混一条「（标题）中文译文还在
    整理中」还不如少一条（用户反馈）。剔除发生在 TOP N 截断之后，
    条数可能少于配置值 —— 这是有意的：宁缺毋滥。
    """
    items: list[dict[str, Any]] = []
    for article in articles:
        text = brief_text(article)
        if not text:
            continue
        source_name = article.source.name if article.source is not None else "未知来源"
        items.append(
            {
                "id": article.id,
                "title": (article.title_zh or article.title or "").strip(),
                "brief": text,
                "source": source_name,
                "category": article.category or "",
                "starred": bool(article.starred),
                "score": article.score,
                "link": article.link,
                "published": article.published_at.strftime("%Y-%m-%d %H:%M")
                if article.published_at
                else "",
            }
        )
    return items


def render_text(items: list[dict[str, Any]], *, title: str = "", footer: str = "") -> str:
    """纯文本版：手机推送 / 微信粘贴直接用。"""
    lines: list[str] = []
    if title:
        lines.append(title)
        lines.append("")
    for index, item in enumerate(items, start=1):
        mark = "★ " if item["starred"] else ""
        lines.append(f"{index}. {mark}{item['title']}")
        lines.append(item["brief"])
        meta = item["source"]
        if item["category"]:
            meta = f"{meta} · {item['category']}"
        if item["published"]:
            meta = f"{meta} · {item['published']}"
        lines.append(f"—— {meta}")
        lines.append("")
    if footer:
        lines.append(footer)
    return "\n".join(lines).strip() + "\n"


def render_markdown(items: list[dict[str, Any]], *, title: str = "", footer: str = "") -> str:
    """Markdown 版：交给 Agent / 贴进支持 md 的地方。"""
    lines: list[str] = []
    if title:
        lines.append(f"# {title}")
        lines.append("")
    for index, item in enumerate(items, start=1):
        mark = "★ " if item["starred"] else ""
        lines.append(f"## {index}. {mark}{item['title']}")
        lines.append("")
        lines.append(item["brief"])
        lines.append("")
        meta = f"- 来源：{item['source']}"
        if item["category"]:
            meta += f" · 分类：{item['category']}"
        if item["published"]:
            meta += f" · {item['published']}"
        lines.append(meta)
        lines.append(f"- 原文：{item['link']}")
        lines.append("")
    if footer:
        lines.append(footer)
    return "\n".join(lines).strip() + "\n"


def _plain_md(text: str) -> str:
    """纯文本输出：去掉 ** 加粗标记（emoji 与换行保留）。"""
    return text.replace("**", "")


def _section_plain_lines(section: dict[str, Any]) -> list[str]:
    """把一个节点渲染成纯文本行（news 的条目 / text、weather 的固定内容）。

    固定内容节点的标题只在**与节点名不同**时才单独出一行 —— 节点名已经
    由 ``render_sections_text`` 用【】打出，天气文案自身以「🌅 早安」开头，
    再重复一遍节点名很难看（用户反馈要能直接复制发手机）。
    """
    lines: list[str] = []
    node_type = section.get("type", NODE_NEWS)
    if node_type in (NODE_TEXT, NODE_WEATHER):
        meta = section.get("meta") or {}
        head = meta.get("title") or meta.get("city") or ""
        if head and head != (section.get("name") or ""):
            lines.append(head)
        text = _plain_md(str(meta.get("text") or "").strip())
        if text:
            lines.append(text)
        return lines
    for index, item in enumerate(section["entries"], start=1):
        mark = "★ " if item["starred"] else ""
        lines.append(f"{index}. {mark}{item['title']}")
        lines.append(item["brief"])
        meta_line = item["source"]
        if item["category"]:
            meta_line = f"{meta_line} · {item['category']}"
        if item["published"]:
            meta_line = f"{meta_line} · {item['published']}"
        lines.append(f"—— {meta_line}")
        lines.append("")
    return lines


def section_text(section: dict[str, Any], *, with_name: bool = False) -> str:
    """单节纯文本（Hermes 一条消息发一节用）。

    天气/文字节点就是固定文案（去掉 ** 标记）；新闻节点是编号条目。
    ``with_name=True`` 时带【节点名】抬头（整体成稿用）。
    """
    lines: list[str] = []
    if with_name and section.get("name"):
        lines.append(f"【{section['name']}】")
        lines.append("")
    lines.extend(_section_plain_lines(section))
    return "\n".join(lines).strip()


def render_sections_text(sections: list[dict[str, Any]], *, title: str = "", footer: str = "") -> str:
    """分节纯文本：每节一个小标题 + 各自编号。"""
    lines: list[str] = []
    if title:
        lines.append(title)
        lines.append("")
    multi = len(sections) > 1
    for section in sections:
        if multi and section["name"]:
            lines.append(f"【{section['name']}】")
            lines.append("")
        lines.extend(_section_plain_lines(section))
        lines.append("")
    if footer:
        lines.append(footer)
    return "\n".join(lines).strip() + "\n"


def render_sections_markdown(sections: list[dict[str, Any]], *, title: str = "", footer: str = "") -> str:
    """分节 Markdown：每节一个二级标题，条目为三级标题。"""
    lines: list[str] = []
    if title:
        lines.append(f"# {title}")
        lines.append("")
    multi = len(sections) > 1
    for section in sections:
        if multi and section["name"]:
            lines.append(f"## {section['name']}")
            lines.append("")
        node_type = section.get("type", NODE_NEWS)
        if node_type in (NODE_TEXT, NODE_WEATHER):
            meta = section.get("meta") or {}
            head = meta.get("title") or meta.get("city") or ""
            if head:
                lines.append(f"### {head}")
                lines.append("")
            text = str(meta.get("text") or "").strip()
            if text:
                lines.append(text)
                lines.append("")
            continue
        for index, item in enumerate(section["entries"], start=1):
            mark = "★ " if item["starred"] else ""
            lines.append(f"### {index}. {mark}{item['title']}")
            lines.append("")
            lines.append(item["brief"])
            lines.append("")
            meta_line = f"- 来源：{item['source']}"
            if item["category"]:
                meta_line += f" · 分类：{item['category']}"
            if item["published"]:
                meta_line += f" · {item['published']}"
            lines.append(meta_line)
            lines.append(f"- 原文：{item['link']}")
            lines.append("")
    if footer:
        lines.append(footer)
    return "\n".join(lines).strip() + "\n"


def build_brief(session: Session, *, now: Any = None) -> dict[str, Any]:
    """一步到位：读配置 → 逐节点产出 → 出两种格式 + 分节数据。

    返回结构：
    - ``sections``：``[{"name", "type", "entries", "meta", "total"}]``
      （无分节时是单节，name 为空）；
    - ``rows``：全部条目打平（兼容既有调用方与 JSON 结构）；
    - ``text`` / ``markdown``：分节渲染的成品。
    """
    row = load_brief_config(session)
    cfg = config_view(row)
    raw_sections = collect_brief_sections(session, cfg, now=now)
    multi = len(raw_sections) > 1
    sections = [
        {
            "name": section["name"],
            "type": section["type"],
            "meta": section["meta"],
            "entries": section["entries"],
            "total": len(section["entries"]),
            "hint": section.get("hint", ""),
            # 单节纯文本：Hermes 一条消息发一节（天气节点就发这段，不配图）。
            # 多节时带【节点名】抬头 —— 每节是一条独立消息，没有抬头
            # 收信人不知道这是哪一节。
            "text": section_text(section, with_name=multi),
        }
        for section in raw_sections
    ]
    all_items = [item for section in sections for item in section["entries"]]
    date_str = (now or now_local()).strftime("%Y-%m-%d")
    title = f"早报 · {date_str}"
    footer = "—— Show Me the Money 自动生成"
    return {
        "config": cfg,
        "sections": sections,
        "rows": all_items,
        "title": title,
        "text": render_sections_text(sections, title=title, footer=footer),
        "markdown": render_sections_markdown(sections, title=title, footer=footer),
        "total": len(all_items),
    }


# ── 成品存档（每天一份，配置页可回看） ──────────────────────────────────


def save_brief_issue(session: Session, data: dict[str, Any], *, date_str: str) -> BriefIssue:
    """把一次生成结果存成当天成品（同一天覆盖写）。"""
    payload = json.dumps(
        {
            "title": data["title"],
            "sections": [
                {
                    "name": s["name"],
                    "type": s["type"],
                    "meta": s["meta"],
                    "entries": s["entries"],
                    # 单节文本随存档落库：历史成品的 Hermes 推送也要能一节一发
                    "text": s.get("text") or section_text(s),
                }
                for s in data["sections"]
            ],
        },
        ensure_ascii=False,
    )
    existing = session.execute(
        select(BriefIssue).where(BriefIssue.date == date_str)
    ).scalar_one_or_none()
    if existing is None:
        existing = BriefIssue(date=date_str, content_json=payload, text=data["text"],
                              markdown=data["markdown"])
        session.add(existing)
        try:
            session.flush()
        except IntegrityError:
            # 并发写同一天（06:00 定时任务与「立即生成」撞车）：另一边先插入了，
            # 回滚这一条再走更新路径，而不是把 500 抛给用户。
            session.rollback()
            existing = session.execute(
                select(BriefIssue).where(BriefIssue.date == date_str)
            ).scalar_one_or_none()
            if existing is None:
                raise
    existing.content_json = payload
    existing.text = data["text"]
    existing.markdown = data["markdown"]
    existing.section_count = len(data["sections"])
    existing.article_count = data["total"]
    existing.created_at = now_local()
    session.flush()
    return existing


def build_and_save_brief(session: Session, *, now: Any = None) -> tuple[dict[str, Any], BriefIssue]:
    """生成 + 存档（定时任务与「立即生成」按钮共用）。"""
    moment = now or now_local()
    data = build_brief(session, now=moment)
    issue = save_brief_issue(session, data, date_str=moment.strftime("%Y-%m-%d"))
    return data, issue


def issue_view(issue: BriefIssue) -> dict[str, Any]:
    """成品存档 → 模板/接口用的视图。"""
    try:
        payload = json.loads(issue.content_json)
    except (TypeError, ValueError):
        payload = {"title": f"早报 · {issue.date}", "sections": []}
    sections = payload.get("sections") or []
    multi = len(sections) > 1
    for section in sections:
        # 旧存档没存单节文本（该字段是后加的）：按同一渲染补出来，
        # 保证「早报 2026-10-07」这类历史点播也一节一发。
        if not section.get("text"):
            section["text"] = section_text(section, with_name=multi)
    return {
        "date": issue.date,
        "title": payload.get("title") or f"早报 · {issue.date}",
        "sections": sections,
        "text": issue.text,
        "markdown": issue.markdown,
        "section_count": issue.section_count,
        "article_count": issue.article_count,
        "created_at": issue.created_at.strftime("%Y-%m-%d %H:%M") if issue.created_at else "",
    }
