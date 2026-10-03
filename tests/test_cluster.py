"""跨源重复内容合并（app/ai/cluster.py）。"""

from __future__ import annotations

import httpx
from sqlalchemy import select

from app.ai import cluster
from app.ai.cluster import (
    dedup_title,
    duplicates_of,
    find_duplicate_pairs,
    merge_duplicates,
    parse_same_story,
    primary_of,
)
from app.config import Settings
from app.db import session_scope
from app.models import Article

from .conftest import make_article
from .test_ai import _client

# 同一件事的两种写法（真跨源转载，标题相似度只有 0.5 左右）
APPLE_A = "Apple says it's tightening macOS 'Full Disk Access' controls due to new AI agents"
APPLE_B = "Apple will limit Mac disk access as AI agents 'substantially' increase risk"
# 同站、同题材，但完全不同的两件事（正文词汇天然重叠，标题毫无共同实词）
META_A = "Meta wants your next gadget to be Muse-infused with its new hardware platform"
META_B = "Spotify billionaire's body scan startup has come to America for retail clinics"


def test_find_duplicate_pairs_catches_cross_source_reprints(seeded_db):
    """跨源转载标题措辞差别很大，但实词重合度足以圈出候选。"""
    with session_scope() as session:
        first = make_article(session, title=APPLE_A, link="https://example.com/a")
        second = make_article(session, title=APPLE_B, link="https://example.com/b")
        first_id, second_id = first.id, second.id

    with session_scope() as session:
        rows = list(session.execute(select(Article)).scalars())
        pairs = find_duplicate_pairs(rows)
    assert [(a.id, b.id) for a, b in pairs] == [(first_id, second_id)]


def test_find_duplicate_pairs_ignores_same_topic_different_story(seeded_db):
    """同站同题材的两篇不同文章不能被圈成候选 —— 否则每次都在烧调用。"""
    with session_scope() as session:
        make_article(session, title=META_A, link="https://example.com/c", source_id=1)
        make_article(session, title=META_B, link="https://example.com/d", source_id=1)

    with session_scope() as session:
        assert find_duplicate_pairs(list(session.execute(select(Article)).scalars())) == []


def test_dedup_title_prefers_the_chinese_version(seeded_db):
    """跨语言转载（英文源 + 中文源）要靠中文标题才能对上。"""
    with session_scope() as session:
        article = make_article(
            session, title=APPLE_A, link="https://example.com/e", title_zh="苹果收紧 macOS 全盘访问权限"
        )
        article_id = article.id
    with session_scope() as session:
        assert dedup_title(session.get(Article, article_id)) == "苹果收紧 macOS 全盘访问权限"


def test_parse_same_story_only_accepts_yes():
    assert parse_same_story("yes") is True
    assert parse_same_story("Yes.") is True
    assert parse_same_story("同一件事") is True
    assert parse_same_story("no") is False
    assert parse_same_story("不是") is False
    # 答不出来一律当作「不是」：宁可多留一条，也不要误删内容
    assert parse_same_story("") is False
    assert parse_same_story("这两条讲的是不同的产品") is False


def test_merge_duplicates_keeps_the_richest_and_marks_the_other(seeded_db, settings: Settings):
    """正文最全的那条留作主条目，另一条标记为重复稿。"""
    with session_scope() as session:
        thin = make_article(
            session, title=APPLE_B, link="https://example.com/g", content_full="Apple disk access " * 40
        )
        rich = make_article(
            session,
            title=APPLE_A,
            link="https://example.com/h",
            content_full="Apple disk access limits " * 400,
        )
        thin_id, rich_id = thin.id, rich.id

    client = _client(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "yes"}}]}), retries=0)
    try:
        with session_scope() as session:
            stats = merge_duplicates(session, client, settings, limit=50)
    finally:
        client.close()

    assert stats["merged"] == 1
    with session_scope() as session:
        assert session.get(Article, thin_id).duplicate_of == rich_id
        assert session.get(Article, rich_id).duplicate_of is None
        assert primary_of(session, session.get(Article, thin_id)).id == rich_id
        assert [row.id for row in duplicates_of(session, rich_id)] == [thin_id]


def test_merge_duplicates_keeps_both_when_the_model_says_no(seeded_db, settings: Settings):
    """判成「不是同一件事」就都留着 —— 合并只减重复，不减内容。"""
    with session_scope() as session:
        make_article(session, title=APPLE_A, link="https://example.com/i")
        make_article(session, title=APPLE_B, link="https://example.com/j")

    client = _client(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "no"}}]}), retries=0)
    try:
        with session_scope() as session:
            stats = merge_duplicates(session, client, settings, limit=50)
    finally:
        client.close()

    assert stats["merged"] == 0
    assert stats["kept"] == 1
    with session_scope() as session:
        assert all(a.duplicate_of is None for a in session.execute(select(Article)).scalars())


def test_merge_duplicates_skips_llm_failure(seeded_db, settings: Settings):
    """模型不可用时不能把内容判成重复。"""
    with session_scope() as session:
        make_article(session, title=APPLE_A, link="https://example.com/k")
        make_article(session, title=APPLE_B, link="https://example.com/l")

    client = _client(lambda r: httpx.Response(500, text="boom"), retries=0)
    try:
        with session_scope() as session:
            stats = merge_duplicates(session, client, settings, limit=50)
    finally:
        client.close()

    assert stats["merged"] == 0
    assert stats["kept"] == 1


def test_already_merged_articles_are_not_judged_again(seeded_db, settings: Settings):
    """已经判过重复的不再进候选，避免连成一条链。"""
    with session_scope() as session:
        first = make_article(session, title=APPLE_A, link="https://example.com/m")
        second = make_article(session, title=APPLE_B, link="https://example.com/n")
        third = make_article(session, title="Apple tightens Mac disk access for AI agents", link="https://example.com/o")
        second.duplicate_of = first.id
        third.duplicate_of = first.id

    client = _client(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "yes"}}]}), retries=0)
    try:
        with session_scope() as session:
            stats = merge_duplicates(session, client, settings, limit=50)
    finally:
        client.close()

    assert stats["pairs"] == 0
    assert stats["merged"] == 0

def test_duplicate_detection_reaches_across_several_days(seeded_db):
    """回归：跨 3 天的同一则新闻也要能比到一起。

    抓取按小时轮询，一批文章会在库里躺好几天才轮到处理 —— 窗口卡在 72 小时的话，
    10-03 的 TechCrunch 与 09-30 的 MIT Tech Review 永远比不到一起。
    """
    from datetime import timedelta

    from app.utils.text import now_local

    with session_scope() as session:
        older = make_article(
            session,
            title=APPLE_A,
            link="https://example.com/old",
            published_at=now_local() - timedelta(days=3),
        )
        make_article(
            session,
            title=APPLE_B,
            link="https://example.com/new",
            published_at=now_local() - timedelta(hours=2),
        )
        older_id = older.id

    with session_scope() as session:
        rows = cluster._candidate_articles(session, limit=50)
        assert older_id in {row.id for row in rows}
        ids = {row.id for row in rows}
        assert {older_id, older_id + 1} <= ids
        assert any({a.id, b.id} == {older_id, older_id + 1} for a, b in cluster.find_duplicate_pairs(rows))


def test_duplicate_detection_stops_at_the_window(seeded_db):
    """超出窗口的同名文章不比较 —— 半年前发过同一件事不算今天的重复。"""
    from datetime import timedelta

    from app.utils.text import now_local

    with session_scope() as session:
        make_article(session, title=APPLE_A, link="https://example.com/ancient",
                     published_at=now_local() - timedelta(days=40))
        make_article(session, title=APPLE_B, link="https://example.com/today",
                     published_at=now_local())

    with session_scope() as session:
        rows = cluster._candidate_articles(session, limit=50)
        assert len(rows) == 1  # 40 天前那篇不在窗口内
