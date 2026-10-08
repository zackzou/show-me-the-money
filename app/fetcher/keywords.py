"""关键词规则：屏蔽（block）与特别关注（star）。

规则存在 ``keyword_rules`` 表里（网页可改，即时生效），不是配置文件 ——
屏蔽词随热点演化，而且用户要看每条规则的命中计数。

匹配口径：
- **大小写不敏感**，英文词做**词边界**匹配（``muse`` 不该命中 ``museum``，
  这是实测踩出来的：早期用裸 substring，屏蔽 Muse 把博物馆的新闻也拦了）。
- 中文词没有词边界概念，直接 substring。
- 命中范围：屏蔽在入库前看标题+摘要（拦在源头、不花正文抓取的钱）；
  关注与正文级屏蔽在正文抓回来之后再看（标题/摘要/正文全覆盖）。

调用点：
- ``run_fetch_pipeline`` 入库前 ``block_hit`` → 命中即跳过；
- ``process_pending`` / ``backfill_content`` 之后 ``apply_rules`` → 标记关注、
  软删正文命中屏蔽词的文章。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import Article, KeywordRule
from app.utils.logger import get_logger

log = get_logger(__name__)

KIND_BLOCK = "block"
KIND_STAR = "star"

# 拉丁词用词边界；中文/日文等没有词边界，用裸包含。
# (?<![A-Za-z0-9]) / (?![A-Za-z0-9]) 而不是 \b：\b 把 `_` 也算词字符，
# 而 `muse_2` 这种标识符里不该算命中「muse」；同时 `AI` 要能命中 `AI Agent`。
_LATIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+\-]*$")


@dataclass(frozen=True)
class Rule:
    id: int
    keyword: str
    kind: str
    enabled: bool


def _pattern(keyword: str) -> re.Pattern[str]:
    """把关键词编译成匹配正则。拉丁词边界匹配，中文裸包含。"""
    word = keyword.strip()
    if _LATIN_RE.match(word):
        escaped = re.escape(word)
        return re.compile(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", re.IGNORECASE)
    return re.compile(re.escape(word), re.IGNORECASE)


def load_rules(session: Session, *, kind: str | None = None) -> list[Rule]:
    """读启用的规则（可按 kind 过滤）。一次查完，调用方自己缓存。"""
    statement = select(KeywordRule).where(KeywordRule.enabled == 1)
    if kind is not None:
        statement = statement.where(KeywordRule.kind == kind)
    return [
        Rule(id=row.id, keyword=row.keyword, kind=row.kind, enabled=bool(row.enabled))
        for row in session.execute(statement).scalars()
    ]


def matches(text: str | None, rules: list[Rule]) -> Rule | None:
    """第一个命中的规则；没有命中返回 ``None``。

    刻意返回**规则本身**而不是布尔值：命中后要按规则 id 累加计数，
    而且日志里要说清「是哪个词拦的」，布尔值承载不了。
    """
    if not text or not rules:
        return None
    for rule in rules:
        if _pattern(rule.keyword).search(text):
            return rule
    return None


def block_hit(title: str | None, summary: str | None, rules: list[Rule]) -> Rule | None:
    """入库前的屏蔽判定：标题 + 摘要。"""
    return matches(f"{title or ''}\n{summary or ''}", rules)


def _article_text(article: Article) -> str:
    """判定用的全文：标题 + 摘要 + 速览 + 正文（中英都算）。

    中英双语文章两边都要看：屏蔽词「Muse」在英文原标题里，
    但读者看到的是中文译文 —— 只查一侧必然漏。
    """
    parts = [
        article.title,
        article.title_zh,
        article.title_en,
        article.summary,
        article.digest,
        article.digest_zh,
        article.content_full,
        article.content_zh,
    ]
    return "\n".join(part for part in parts if part)


def apply_rules(
    session: Session,
    article: Article,
    *,
    block_rules: list[Rule] | None = None,
    star_rules: list[Rule] | None = None,
) -> str | None:
    """对一篇已入库的文章应用规则；返回 ``"block"`` / ``"star"`` / ``None``。

    正文级屏蔽：命中即软删（进回收站），用户在回收站里能看到「为什么被删」
    （reason 存进回收站页的展示，见 web/routes）。关注：命中即置 ``starred=1``。

    规则命中计数在**调用方提交事务前**累加（这里只改内存里的 row），
    与文章状态同一次提交落库，避免计数和实际状态对不上。
    """
    if article.deleted_at is not None:
        return None  # 已删的不用再判
    text = _article_text(article)
    if block_rules:
        hit = matches(text, block_rules)
        if hit is not None:
            session.execute(
                # 直接 UPDATE 计数：规则行可能没被加载进 session，merge 会多一次查询
                update(KeywordRule)
                .where(KeywordRule.id == hit.id)
                .values(hits=KeywordRule.hits + 1)
            )
            from app.utils.text import now_local

            article.deleted_at = now_local()
            # 留个原因给回收站页展示（``_delete_reason`` 按前缀识别）。
            # 不写的话回收站里所有文章都显示「手动删除」，用户会以为
            # 自己删过 —— 实测反馈过这一点。
            article.degraded_reason = f"关键词屏蔽：{hit.keyword}"[:300]
            log.info("关键词屏蔽：%r 命中，文章 #%d 移入回收站（%s）",
                     hit.keyword, article.id, (article.title or "")[:40])
            return "block"
    if star_rules:
        hit = matches(text, star_rules)
        if hit is not None and not article.starred:
            session.execute(
                update(KeywordRule)
                .where(KeywordRule.id == hit.id)
                .values(hits=KeywordRule.hits + 1)
            )
            article.starred = 1
            log.info("特别关注：%r 命中，文章 #%d 已标记（%s）",
                     hit.keyword, article.id, (article.title or "")[:40])
            return "star"
    return None
