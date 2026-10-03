"""v0.4 新增：站内搜索与分类过滤。"""

from __future__ import annotations

from app.db import session_scope
from app.models import Article
from app.utils.text import now_local
from app.web.search import SCOPE_FULL, SCOPE_META, escape_like, search_articles


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


def test_search_skips_irrelevant_and_pending(seeded_db):
    """被判为不相关 / 还没处理的不能被搜出来，否则搜索结果会被噪音污染。"""
    _mk("https://x.com/i1", title="噪音关键词", relevance=0)
    _mk("https://x.com/i2", title="关键词待处理", status="pending")

    with session_scope() as session:
        assert search_articles(session, "关键词") == []


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
