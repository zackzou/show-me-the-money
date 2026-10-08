"""站内搜索：标题与摘要（默认）或全文。

两个刻意的取舍：
- **只用 SQL LIKE，不引 FTS5 依赖**。SQLite 的 FTS5 中文分词要靠外部 tokenizer，
  对「先能跑起来」的小项目来说不划算；数据量到几万条再换也不迟。
- **搜索只覆盖已进日报的内容**（``relevance=1``），否则会把被判为不相关的
  噪音也翻出来，搜索结果就失去意义了。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Article, visible_article_conditions
from app.report.generator import STATUS_REPORTABLE

# 搜索框旁边的快捷键提示
SEARCH_HOTKEY = "/"

# scope 是 **URL 参数值**（README 里写的就是 scope=meta|full），不是给人看的中文。
# 早期版本把它写成了中文「标题与摘要」/「全文」，而模板链接里发的是 meta / full，
# 两边永远对不上 —— 于是「全文」这个 tab 从来搜不到正文，高亮也永远停在「最新」。
SCOPE_META = "meta"
SCOPE_FULL = "full"

# 界面上显示的中文名
SCOPE_LABELS = {SCOPE_META: "标题与摘要", SCOPE_FULL: "全文"}

# 一次最多渲染多少条结果。这是渲染预算，不是「命中总数」—— 调用方必须另外
# 查真实总数并如实显示，否则页面上会出现「找到 200 条」而实际命中 247 条、
# 且有 47 条永远翻不到的情况。
SEARCH_RESULT_LIMIT = 200
# 兼容早期版本传进来的中文值，避免老链接直接掉回默认
_LEGACY_SCOPES = {"标题与摘要": SCOPE_META, "全文": SCOPE_FULL}


def normalize_scope(raw: object) -> str:
    """把外部传入的 scope 归一成 ``meta`` / ``full``，认不出来就退回 ``meta``。

    这个函数是「把外部输入变回可信值」的那一层，所以它必须是**全函数**：
    任何输入都要有返回值，不能因为类型不对就抛异常 —— 抛出去就是一个 500，
    而正确的行为是「认不出来就当默认」。
    """
    value = raw.strip() if isinstance(raw, str) else ""
    if value in SCOPE_LABELS:
        return value
    return _LEGACY_SCOPES.get(value, SCOPE_META)


def escape_like(term: str) -> str:
    """转义 LIKE 通配符，用户搜 `%` 时不该变成通配符。"""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _match_conditions(keyword: str, scope: str) -> list[Any]:
    """关键词命中的列条件。

    **必须包含中文译文列**（``title_zh`` / ``digest_zh``，全文再加
    ``content_zh``）。这是一个中文站：读者在页面上看到的就是译文，搜的也是
    译文里出现的词。以前只搜英文列，于是「防范」搜不到那 5 篇正文里写着
    「防范 AI Agent 滥用」的��章 —— 而这些词的英文原文用的是 curb /
    tightening，压根不在库里。译了等于搜不到。
    """
    pattern = f"%{escape_like(keyword)}%"
    columns = [
        Article.title,
        Article.title_zh,
        Article.summary,
        Article.digest,
        Article.digest_zh,
        Article.reason,
        Article.topics,
    ]
    if scope == SCOPE_FULL:
        columns.extend([Article.content_full, Article.content_zh])
    return [col.like(pattern, escape="\\") for col in columns]


def search_articles(
    session: Session,
    term: str,
    *,
    scope: str = SCOPE_META,
    category: str | None = None,
    tag: str | None = None,
    limit: int = SEARCH_RESULT_LIMIT,
) -> list[Article]:
    """按关键词搜索文章。``scope`` 为 ``全文`` 时连正文一起搜。"""
    keyword = (term or "").strip()
    if not keyword:
        return []

    conditions = _match_conditions(keyword, scope)

    statement = (
        select(Article)
        .where(
            Article.relevance == 1,
            Article.status.in_(STATUS_REPORTABLE),
            *visible_article_conditions(),
            or_(*conditions),
        )
        .order_by(Article.published_at.desc(), Article.id.desc())
        .limit(max(1, min(limit, 500)))
    )
    rows = list(session.execute(statement).scalars())

    # 分类与标签在应用层过滤：标签是逗号串，LIKE 会误伤「AI」匹配到「AI Agent」
    if category:
        rows = [row for row in rows if row.category == category]
    if tag:
        needle = tag.strip().casefold()
        rows = [
            row
            for row in rows
            if any(part.strip().casefold() == needle for part in (row.tags or "").split(","))
        ]
    return rows


def count_by_category(session: Session, term: str, *, scope: str = SCOPE_META,
                      category: str | None = None, tag: str | None = None) -> dict[str, int]:
    """当前关键词下各分类的命中数，用来给 tab 上的数字。

    ``category`` / ``tag`` 必须传进来，否则 tab 上的数字与「找到 N 条」对不上：
    早先这里只按关键词统计，于是 ``/search?q=开源&tag=开源`` 头部写「找到 7 条」、
    「全部」tab 却显示 21。数字必须和列表用**同一套过滤条件**。
    """
    keyword = (term or "").strip()
    if not keyword:
        return {}
    conditions = _match_conditions(keyword, scope)
    rows = session.execute(
        select(Article.id, Article.category, Article.tags).where(
            Article.relevance == 1,
            Article.status.in_(STATUS_REPORTABLE),
            *visible_article_conditions(),
            or_(*conditions),
        )
    ).all()
    needle = tag.strip().casefold() if tag else ""
    counts: dict[str, int] = {}
    total = 0
    for _id, row_category, row_tags in rows:
        if category and row_category != category:
            continue
        if needle and not any(
            part.strip().casefold() == needle for part in (row_tags or "").split(",")
        ):
            continue
        total += 1
        if row_category:
            counts[row_category] = counts.get(row_category, 0) + 1
    # ``_all`` 是「全部」tab 上的数字；模板里 tabs() 就是按这个键取的
    counts["_all"] = total
    return counts


def search_metadata() -> dict[str, Any]:
    """给模板用的搜索页元信息。"""
    return {
        "scope_meta": SCOPE_META,
        "scope_full": SCOPE_FULL,
        "scope_labels": SCOPE_LABELS,
        "hotkey": SEARCH_HOTKEY,
    }