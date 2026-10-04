"""v0.4 新增：站内搜索与分类过滤。"""

from __future__ import annotations

from app.db import session_scope
from app.models import Article
from app.utils.text import now_local
from app.web.search import (
    SCOPE_FULL,
    SCOPE_LABELS,
    SCOPE_META,
    count_by_category,
    escape_like,
    normalize_scope,
    search_articles,
    search_metadata,
)


def _mk(link: str, **kwargs):
    payload = {
        "title": "标题",
        "link": link,
        "content": "rss 正文",
        "published_at": now_local(),
        "summary": "摘要",
        "digest": "速览",
        "reason": "推荐理由",
        "tags": "AI,芯片",
        "relevance": 1,
        "status": "processed",
    }
    payload.update(kwargs)
    article = Article(**payload)
    with session_scope() as session:
        session.add(article)
        session.flush()
        return article.id


def test_search_matches_title_summary_digest_reason(seeded_db):
    _mk("https://x.com/1", title="OpenAI 发布新模型")
    _mk("https://x.com/2", title="别家新闻", summary="提到了 OpenAI")
    _mk("https://x.com/3", title="第三篇", digest="速览里有 openai")
    _mk("https://x.com/4", title="第四篇", reason="理由里提 openai")
    _mk("https://x.com/5", title="完全无关")

    with session_scope() as session:
        hits = search_articles(session, "openai", scope=SCOPE_META)
    titles = sorted(a.title for a in hits)
    assert titles == ["OpenAI 发布新模型", "别家新闻", "第三篇", "第四篇"]
    # 大小写不敏感
    with session_scope() as session:
        assert len(search_articles(session, "OPENAI")) == 4


def test_search_full_scope_includes_body(seeded_db):
    _mk("https://x.com/b1", title="标题甲", content_full="正文里才有这个词")
    _mk("https://x.com/b2", title="标题乙", content_full="无关内容")

    with session_scope() as session:
        assert len(search_articles(session, "正文里", scope=SCOPE_META)) == 0
        assert len(search_articles(session, "正文里", scope=SCOPE_FULL)) == 1


def test_search_skips_unjudged_articles(seeded_db):
    """被判为不相关 / 还没判过相关的不能被搜出来，否则搜索结果会被噪音污染。

    判别标准是 ``relevance`` 而不是 status：降级文章会被放回 pending 去重试
    （上游限流恢复后重新处理），它 relevance 已经是 1，重试期间就该能被搜到。
    """
    _mk("https://x.com/i1", title="噪音关键词", relevance=0)
    _mk("https://x.com/i2", title="关键词还没判过", status="pending", relevance=None)
    _mk("https://x.com/i3", title="关键词等重试", status="pending", relevance=1)

    with session_scope() as session:
        hits = search_articles(session, "关键词")
    assert [h.title for h in hits] == ["关键词等重试"]


def test_search_escapes_like_wildcards(seeded_db):
    """用户搜 % 时不该变成通配符匹配全部。"""
    _mk("https://x.com/w1", title="正常标题")
    _mk("https://x.com/w2", title="含 100% 的标题")

    with session_scope() as session:
        hits = search_articles(session, "%")
    assert len(hits) == 1
    assert escape_like("100%_x") == "100\\%\\_x"


def test_search_empty_term_returns_nothing(seeded_db):
    _mk("https://x.com/e1", title="任意")
    with session_scope() as session:
        assert search_articles(session, "") == []
        assert search_articles(session, "   ") == []


def test_search_filters_by_category_and_tag(seeded_db):
    _mk("https://x.com/c1", title="分类甲", category="模型", tags="芯片")
    _mk("https://x.com/c2", title="分类乙", category="行业", tags="融资")
    _mk("https://x.com/c3", title="分类丙", category="模型", tags="AI Agent,芯片")

    with session_scope() as session:
        assert len(search_articles(session, "分类", category="模型")) == 2
        assert len(search_articles(session, "分类", category="行业")) == 1
        # 标签要精确匹配：搜 AI 不该命中 "AI Agent"
        assert len(search_articles(session, "分类", tag="AI")) == 0
        assert len(search_articles(session, "分类", tag="AI Agent")) == 1


# ── scope 归一化 ────────────────────────────────────────────────────────────
# 回归：早期 SCOPE_META/SCOPE_FULL 被写成中文「标题与摘要」/「全文」，
# 而模板与 README 发的是 scope=meta|full，两边永远对不上 ——
# 于是「全文」这个 tab 从来没搜到过正文，高亮也永远停在默认那一档。


def test_scope_constants_are_url_values_not_labels():
    assert SCOPE_META == "meta"
    assert SCOPE_FULL == "full"
    assert SCOPE_LABELS[SCOPE_FULL] == "全文"


def test_normalize_scope_accepts_url_values():
    assert normalize_scope("meta") == SCOPE_META
    assert normalize_scope("full") == SCOPE_FULL
    assert normalize_scope(" full ") == SCOPE_FULL


def test_normalize_scope_accepts_legacy_chinese():
    """老链接里带的是中文值，不能直接掉回默认。"""
    assert normalize_scope("全文") == SCOPE_FULL
    assert normalize_scope("标题与摘要") == SCOPE_META


def test_normalize_scope_falls_back_to_meta():
    for raw in (None, "", "   ", "bogus", "FULL", 123):
        assert normalize_scope(raw) == SCOPE_META


def test_full_scope_actually_searches_the_body(seeded_db):
    """全文范围要能搜到只出现在正文里的词。"""
    _mk("https://x.com/f1", title="标题甲", content="rss 正文", digest="速览", reason="理由",
        content_full="只有正文里才有的关键词：钍基熔盐堆")
    with session_scope() as session:
        assert search_articles(session, "钍基熔盐堆", scope=SCOPE_FULL), "全文范围应该搜到正文"
        assert search_articles(session, "钍基熔盐堆", scope=SCOPE_META) == [], "摘要范围不该搜到正文"


def test_count_by_category_exposes_all_total(seeded_db):
    """「全部」tab 的数字取 counts['_all']，模板就是这么读的。"""
    _mk("https://x.com/a1", title="甲一", category="模型")
    _mk("https://x.com/a2", title="甲二", category="模型")
    _mk("https://x.com/a3", title="甲三", category="行业")
    with session_scope() as session:
        counts = count_by_category(session, "甲", scope=SCOPE_META)
    assert counts["_all"] == 3
    assert counts["模型"] == 2


def test_search_metadata_exposes_labels_for_the_template(seeded_db):
    meta = search_metadata()
    assert meta["scope_labels"][SCOPE_FULL] == "全文"
    assert meta["scope_meta"] == SCOPE_META
