"""抓取模块测试：RSS 解析、重试、去重、主流程。"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from app.db import session_scope
from app.fetcher.dedup import is_duplicate
from app.fetcher.pipeline import run_fetch_pipeline
from app.fetcher.rss import FetchError, fetch_feed, parse_feed
from app.models import Article, Source

RSS_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>示例源</title>
  <item>
    <title>AI Agent 融资 10 亿</title>
    <link>https://example.com/a1</link>
    <description><![CDATA[<p>正文<b>加粗</b></p>]]></description>
    <pubDate>Mon, 28 Sep 2026 23:30:00 +0000</pubDate>
  </item>
  <item>
    <title>没有链接的条目</title>
    <description>应被跳过</description>
  </item>
</channel></rss>
"""


def test_parse_feed_extracts_fields():
    items = parse_feed(RSS_SAMPLE, source="https://example.com/feed")
    assert len(items) == 1
    item = items[0]
    assert item["title"] == "AI Agent 融资 10 亿"
    assert item["link"] == "https://example.com/a1"
    assert "正文" in str(item["content"])
    # 存储层统一北京时间：UTC 23:30 → 次日 07:30
    assert item["published_at"] is not None
    assert item["published_at"].strftime("%Y-%m-%d %H:%M") == "2026-09-29 07:30"


def test_fetch_feed_retries_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, content=RSS_SAMPLE.encode())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        items = fetch_feed("https://example.com/feed", retries=1, client=client)
    assert calls["n"] == 2
    assert len(items) == 1


def test_fetch_feed_raises_after_retries():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client, pytest.raises(FetchError):
        fetch_feed("https://example.com/feed", retries=1, client=client)


def test_is_duplicate_by_link_and_similar_title(seeded_db):
    with session_scope() as session:
        session.add(Article(title="AI Agent 融资 10 亿", link="https://example.com/a1", status="pending"))
        session.flush()
        assert is_duplicate(session, "https://example.com/a1", "别的标题") is True
        assert is_duplicate(session, "https://example.com/a2", "AI Agent 融资 10 亿！") is True
        assert is_duplicate(session, "https://example.com/a3", "完全不同的新闻标题") is False


def test_run_fetch_pipeline_inserts_and_dedups(seeded_db):
    def fake_fetcher(url: str, **kwargs):
        return [
            {"title": "AI Agent 融资 10 亿", "link": f"{url}#1", "content": "<p>正文</p>", "published_at": None},
            {"title": "芯片出口新规影响分析", "link": f"{url}#2", "content": "正文", "published_at": None},
        ]

    first = run_fetch_pipeline(fetcher=fake_fetcher)
    assert first["new"] == 2
    assert first["failed"] == 0
    second = run_fetch_pipeline(fetcher=fake_fetcher)
    assert second["new"] == 0
    assert second["duplicated"] == 2

    with session_scope() as session:
        rows = list(session.execute(select(Article)).scalars())
        assert len(rows) == 2
        assert all(row.status == "pending" for row in rows)
        assert rows[0].content == "正文"  # HTML 已被清掉


def test_run_fetch_pipeline_skips_failing_source(seeded_db):
    def broken_fetcher(url: str, **kwargs):
        raise FetchError("挂了")

    stats = run_fetch_pipeline(fetcher=broken_fetcher)
    assert stats["failed"] == 1
    assert stats["details"][0]["status"] == "failed"
    with session_scope() as session:
        assert session.query(Source).count() == 1
        assert session.query(Article).count() == 0
