"""抓取模块测试：RSS 解析、重试、去重、时间窗过滤、主流程。"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app.db import session_scope
from app.fetcher.dedup import is_duplicate
from app.fetcher.pipeline import run_fetch_pipeline
from app.fetcher.rss import FetchError, fetch_feed, parse_feed
from app.models import Article, Source
from app.utils.text import now_local

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


def test_run_fetch_pipeline_flags_empty_source(seeded_db):
    """抓到 0 条 = 死信源，必须显式暴露出来，而不是安静地当作「今天没更新」。"""

    def empty_fetcher(url: str, **kwargs):
        return []

    stats = run_fetch_pipeline(fetcher=empty_fetcher)
    assert stats["empty_sources"] == 1
    assert stats["details"][0]["status"] == "empty"


def test_run_fetch_pipeline_drops_stale_items(seeded_db):
    """归档型 feed（一次返回整个历史）必须按 max_age_days 挡掉。"""
    now = now_local()

    def archive_fetcher(url: str, **kwargs):
        return [
            {
                "title": "三天前的旧闻",
                "link": "https://example.com/old",
                "content": "正文",
                "published_at": now - timedelta(days=30),
            },
            {
                "title": "昨天的新闻",
                "link": "https://example.com/new",
                "content": "正文",
                "published_at": now - timedelta(days=1),
            },
        ]

    stats = run_fetch_pipeline(fetcher=archive_fetcher, max_age_days=14)

    assert stats["stale"] == 1
    assert stats["new"] == 1
    with session_scope() as session:
        titles = [row.title for row in session.execute(select(Article)).scalars()]
    assert titles == ["昨天的新闻"]


def test_run_fetch_pipeline_caps_items_per_source(seeded_db):
    def many_fetcher(url: str, **kwargs):
        return [
            {"title": f"第 {i} 条", "link": f"https://example.com/{i}", "content": "正文", "published_at": None}
            for i in range(10)
        ]

    stats = run_fetch_pipeline(fetcher=many_fetcher, max_items_per_source=4)

    assert stats["new"] == 4
    assert stats["capped"] == 6


def test_run_fetch_pipeline_min_content_chars(seeded_db):
    def titled_fetcher(url: str, **kwargs):
        return [
            {"title": "只有标题", "link": "https://example.com/t1", "content": "<p>短</p>", "published_at": None},
            {
                "title": "有正文",
                "link": "https://example.com/t2",
                "content": "<p>足够长的正文内容，用来通过长度阈值</p>",
                "published_at": None,
            },
        ]

    stats = run_fetch_pipeline(fetcher=titled_fetcher, min_content_chars=10)

    assert stats["no_content"] == 1
    assert stats["new"] == 1
    with session_scope() as session:
        titles = [row.title for row in session.execute(select(Article)).scalars()]
    assert titles == ["有正文"]


def test_run_fetch_pipeline_survives_link_collision(seeded_db):
    """并发/手动抓取撞上同一 link 时，只丢那一条，不该整批回滚。"""
    with session_scope() as session:
        session.add(Article(title="已存在", link="https://example.com/dup", status="processed"))

    def fetcher(url: str, **kwargs):
        return [
            {"title": "撞车条目", "link": "https://example.com/dup", "content": "正文", "published_at": None},
            {"title": "正常条目", "link": "https://example.com/fine", "content": "正文", "published_at": None},
        ]

    # 故意关掉 link 预检，模拟「预检通过但写入时才发现撞车」的情况
    stats = run_fetch_pipeline(fetcher=fetcher)
    assert stats["new"] == 1
    assert stats["failed"] == 0
    with session_scope() as session:
        titles = {row.title for row in session.execute(select(Article)).scalars()}
    assert titles == {"已存在", "正常条目"}


def test_run_fetch_pipeline_counts_items_without_date(seeded_db):
    def undated_fetcher(url: str, **kwargs):
        return [{"title": "没有日期", "link": "https://example.com/undated", "content": "正文", "published_at": None}]

    stats = run_fetch_pipeline(fetcher=undated_fetcher, max_age_days=14)
    assert stats["no_date"] == 1
    assert stats["new"] == 1


# ── 抓取入口的两道闸：链接协议与未来时间 ──────────────────────────────

def test_javascript_link_is_not_stored():
    """恶意 feed 的 ``javascript:`` link 能在本站源上执行脚本。

    Jinja 会转义引号，但**不会中和 URL 的协议**，所以它会被原样渲染进详情页
    的 href。在入库这一层挡掉：渲染路径有五处，入口只有一个。
    """
    from app.fetcher.pipeline import _safe_link

    assert _safe_link("javascript:alert(document.domain)") == ""
    assert _safe_link("  JavaScript:alert(1)") == ""
    assert _safe_link("data:text/html,<script>alert(1)</script>") == ""
    assert _safe_link("file:///etc/passwd") == ""
    assert _safe_link("vbscript:msgbox(1)") == ""
    # 正常链接照常通过
    assert _safe_link("https://example.com/a") == "https://example.com/a"
    assert _safe_link("http://example.com/a") == "http://example.com/a"
    assert _safe_link("") == ""


def test_future_pubdate_is_clamped_to_now():
    """pubDate 落在未来会被钳到当前时间。

    实测 InfoQ 的 pubDate 把北京时间当成 GMT，文章凭空提前 7.5 小时 ——
    于是一篇 10-04 19:22 抓到的文章被记成 10-05 02:39，进了 10-05 的日报，
    在 10-04 那期里消失、明天又冒出来。库里 6 行 published_at > created_at，
    全是 InfoQ。
    """
    from datetime import timedelta

    from app.fetcher.pipeline import _admit

    now = now_local()
    items = [
        {"link": "https://example.com/future", "title": "Future",
         "content": "<p>" + "body text " * 40 + "</p>",
         "published_at": now + timedelta(hours=8)},
        {"link": "https://example.com/ok", "title": "OK",
         "content": "<p>" + "body text " * 40 + "</p>",
         "published_at": now},
        # 小幅超前属于时钟偏差，应当保留
        {"link": "https://example.com/slightly", "title": "Slight",
         "content": "<p>" + "body text " * 40 + "</p>",
         "published_at": now + timedelta(minutes=2)},
    ]
    kept, dropped = _admit(
        items, max_age_days=30, max_items=10, min_content_chars=10, now=now)
    assert dropped["future"] == 1
    assert len(kept) == 3
    assert kept[0]["published_at"] == now, "未来时间应被钳到 now"
    assert kept[1]["published_at"] == now
    assert kept[2]["published_at"] > now, "几秒的时钟偏差不该被改写"
