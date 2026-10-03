"""v0.3 新增：正文全文抽取。"""

from __future__ import annotations

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

    original = mod.fetch_article_text
    mocked = httpx.Client(transport=httpx.MockTransport(handler))
    mod.fetch_article_text = lambda url, **kw: original(url, min_chars=50, client=mocked)
    try:
        with session_scope() as session:
            stats = backfill_content(session, limit=5)
    finally:
        mod.fetch_article_text = original

    assert stats["candidates"] == 1
    assert stats["short"] == 1
    with session_scope() as session:
        # 空串表示「查过，确实没有正文」，不再重复请求
        assert session.execute(select(Article)).scalar_one().content_full == ""

    with session_scope() as session:
        assert backfill_content(session, limit=5)["candidates"] == 0  # 不重复请求
    assert calls["n"] == 1
