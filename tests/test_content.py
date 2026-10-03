"""v0.3 新增：正文全文抽取。"""

from __future__ import annotations

import json

import httpx

from app.fetcher.content import extract_article_text, fetch_article_text

PAGE = """
<html><head><title>标题</title><style>.x{color:red}</style><script>var a=1;</script></head>
<body>
  <nav><a href="/">首页</a><a href="/about">关于</a></nav>
  <div class="wrap">
    <p>这是一段足够长的正文内容，用来验证抽取器能不能把它取出来。</p>
    <p>这是第二段正文内容，同样足够长，应该被保留下来作为第二段。</p>
    <ul><li>列表项一的内容也足够长，应当被保留。</li><li>列表项二的内容也足够长。</li></ul>
    <p>短</p>
    <p>阅读全文</p>
  </div>
  <footer>版权所有</footer>
</body></html>
"""

NESTED = """
<html><body><div class="a"><div class="b"><div class="c">
  <p>嵌套 div 里的正文第一段，字符数足够多可以被保留下来。</p>
  <p>嵌套 div 里的正文第二段，字符数同样足够多也该被保留下来。</p>
</div></div></div></body></html>
"""


def test_extract_article_text_returns_paragraphs():
    # 夹具正文只有几十字，所以把阈值调低；真实页面动辄两三千字
    text = extract_article_text(PAGE, min_chars=50)
    assert "足够长的正文内容，用来验证抽取器" in text
    assert "第二段正文内容" in text
    assert "\n\n" in text  # 段落被空行分隔
    # 非正文与碎片要滤掉
    assert "var a=1" not in text
    assert "color:red" not in text
    assert "首页" not in text
    assert "阅读全文" not in text
    assert "版权所有" not in text


def test_extract_handles_nested_divs():
    """正则挑容器会被嵌套 div 截断，所以正文抽取必须直接扫全文档的 <p>。"""
    text = extract_article_text(NESTED, min_chars=50)
    assert "嵌套 div 里的正文第一段" in text
    assert "嵌套 div 里的正文第二段" in text


def test_extract_returns_empty_for_non_article():
    assert extract_article_text("") == ""
    assert extract_article_text("just plain text, no html at all") == ""
    assert extract_article_text("<html><body><p>太短</p></body></html>") == ""
    # 达不到 min_chars 也算抓不到（宁可退回短摘要，也不要给一段残缺正文）
    assert extract_article_text(PAGE, min_chars=500) == ""


def test_fetch_article_text_uses_mock_and_swallows_errors():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, text=PAGE)
        raise httpx.ConnectError("down")

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        assert "正文内容" in fetch_article_text("https://x.com/p", min_chars=50, client=c)
        assert fetch_article_text("https://x.com/p", client=c) == ""  # 异常吞掉

    def not_found(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(not_found)) as c:
        assert fetch_article_text("https://x.com/p", client=c) == ""


def test_backfill_content_marks_empty_so_it_does_not_retry(seeded_db):
    from sqlalchemy import select

    from app.db import session_scope
    from app.fetcher.content import backfill_content
    from app.models import Article

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text="<html><body><p>没有正文</p></body></html>")

    with session_scope() as session:
        session.add(Article(title="无正文文章", link="https://example.com/nobody", status="pending"))

    import app.fetcher.content as mod

    original = mod.fetch_article_document
    mocked = httpx.Client(transport=httpx.MockTransport(handler))
    mod.fetch_article_document = lambda url, **kw: original(url, min_chars=50, client=mocked)
    try:
        with session_scope() as session:
            stats = backfill_content(session, limit=5)
    finally:
        mod.fetch_article_document = original

    assert stats["candidates"] == 1
    assert stats["short"] == 1
    with session_scope() as session:
        # 空串表示「查过，确实没有正文」，不再重复请求
        assert session.execute(select(Article)).scalar_one().content_full == ""

    with session_scope() as session:
        assert backfill_content(session, limit=5)["candidates"] == 0  # 不重复请求
    assert calls["n"] == 1


# 正文内联配图：原站（图1 的 AIHOT）就是把图夹在段落之间，所以位置必须跟着一起抓
INLINE = """
<html><body><article>
  <p>第一段正文，内容足够长，可以通过抽取器的长度门槛。</p>
  <p><img src="/img/lead.jpg" width="900" height="474"></p>
  <p>第二段正文，同样足够长，应该出现在第一张图之后。</p>
  <p><img src="https://cdn.example.com/chart.png"></p>
  <p>第三段正文，这一段也足够长，用来验证第二张图出现的位置。</p>
  <img src="/static/logo.png" class="logo">
  <img src="/img/lead.jpg">
</article></body></html>
"""


def test_extract_article_document_keeps_inline_image_positions():
    from app.fetcher.content import extract_article_document

    text, images = extract_article_document(
        INLINE, base_url="https://news.example.com/a/1.html", min_chars=20
    )
    blocks = text.split("\n\n")
    assert len(blocks) == 3
    # 图 1 夹在第 1 段后，图 2 夹在第 2 段后；相对地址补成绝对地址
    assert images == [
        {"i": 1, "url": "https://news.example.com/img/lead.jpg"},
        {"i": 2, "url": "https://cdn.example.com/chart.png"},
    ]
    assert blocks[0].startswith("第一段正文")
    assert blocks[2].startswith("第三段正文")


def test_extract_article_document_drops_icons_and_repeats():
    from app.fetcher.content import extract_article_document

    _, images = extract_article_document(INLINE, base_url="https://news.example.com/", min_chars=20)
    urls = [item["url"] for item in images]
    assert not any("logo" in url for url in urls)  # 站点 logo 不进正文
    assert len(urls) == len(set(urls))  # 同一张图重复出现只留一次


def test_extract_article_document_without_images_matches_text():
    from app.fetcher.content import extract_article_document, extract_article_text

    text, images = extract_article_document(PAGE, min_chars=50)
    assert images == []
    assert text == extract_article_text(PAGE, min_chars=50)


def test_backfill_content_stores_body_images(seeded_db):
    import app.fetcher.content as mod
    from app.db import session_scope
    from app.fetcher.content import backfill_content
    from app.models import Article
    from tests.conftest import make_article

    with session_scope() as session:
        article = make_article(session, link="https://example.com/inline", content_full=None)
        article_id = article.id

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=INLINE)

    original = mod.fetch_article_document
    mocked = httpx.Client(transport=httpx.MockTransport(handler))
    mod.fetch_article_document = lambda url, **kw: original(url, min_chars=20, client=mocked)
    try:
        with session_scope() as session:
            stats = backfill_content(session, limit=5)
    finally:
        mod.fetch_article_document = original

    assert stats["with_images"] == 1
    with session_scope() as session:
        stored = json.loads(session.get(Article, article_id).body_images)
    assert stored[0]["url"].endswith("/img/lead.jpg")


def test_backfill_body_images_fills_positions_only(seeded_db):
    """老数据补内联配图：只写位置，已经在库里的正文不重写。"""
    import app.fetcher.content as mod
    from app.db import session_scope
    from app.fetcher.content import backfill_body_images
    from app.models import Article
    from tests.conftest import make_article

    original_text = "这是一段已经在库里的正文，字符数足够多，不应该被重新抓取覆盖掉。"
    with session_scope() as session:
        article = make_article(session, link="https://example.com/old", content_full=original_text)
        article_id = article.id

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=INLINE)

    original = mod.fetch_article_document
    mocked = httpx.Client(transport=httpx.MockTransport(handler))
    mod.fetch_article_document = lambda url, **kw: original(url, min_chars=20, client=mocked)
    try:
        with session_scope() as session:
            stats = backfill_body_images(session, limit=5)
    finally:
        mod.fetch_article_document = original

    assert stats["filled"] == 1
    with session_scope() as session:
        stored = session.get(Article, article_id)
        assert stored.content_full == original_text
        assert json.loads(stored.body_images)[0]["url"].endswith("/img/lead.jpg")
        # 标记成数组（含空数组）之后就不再重复请求
        with session_scope() as session:
            assert backfill_body_images(session, limit=5)["candidates"] == 0
