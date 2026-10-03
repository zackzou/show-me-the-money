"""v0.2 新增：RSS 配图抽取与 JSON 存储。"""

from __future__ import annotations

from app.utils.text import extract_images

HTML = """
<p>正文</p>
<img src="/img/a.jpg" width="600">
<img data-src="https://cdn.example.com/b.png">
<img srcset="https://cdn.example.com/c-320.jpg 320w, https://cdn.example.com/c-1280.jpg 1280w">
<img src="https://cdn.example.com/d.jpg">
<img src="https://news.example.com/img/a.jpg">      <!-- 重复 -->
<img src="data:image/gif;base64,R0lGOD">          <!-- 内联 -->
<img src="https://tracker.example.com/pixel.gif">  <!-- 追踪像素 -->
<img src="https://cdn.example.com/placeholder.png">
"""


def test_extract_images_picks_and_dedups():
    urls = extract_images(HTML, base_url="https://news.example.com/post/1")
    assert urls == [
        "https://news.example.com/img/a.jpg",
        "https://cdn.example.com/b.png",
        "https://cdn.example.com/c-1280.jpg",  # srcset 里挑最大的
        "https://cdn.example.com/d.jpg",
    ]


def test_extract_images_handles_empty():
    assert extract_images("") == []
    assert extract_images(None) == []
    assert extract_images("<p>没有配图</p>") == []


def test_extract_images_respects_limit():
    html = "".join(f'<img src="https://x.com/{i}.jpg">' for i in range(20))
    assert len(extract_images(html, limit=3)) == 3


OG_HTML = """
<html><head>
<meta property="og:image" content="https://cdn.example.com/hero-1200.jpg">
<meta name="twitter:image" content="https://cdn.example.com/tw.jpg">
</head><body><img src="/logo.png"><img src="https://cdn.example.com/inline.jpg"></body></html>
"""


def test_extract_og_image_prefers_meta():
    from app.fetcher.images import extract_og_image

    assert extract_og_image(OG_HTML) == "https://cdn.example.com/hero-1200.jpg"
    assert extract_og_image("<html><head></head><body>无图</body></html>") is None


def test_extract_og_image_skips_logo_falls_back_to_first_img():
    from app.fetcher.images import extract_og_image

    html = '<html><body><img src="https://x.com/logo.png"><img src="https://x.com/real.jpg"></body></html>'
    assert extract_og_image(html) == "https://x.com/real.jpg"


def test_fetch_og_image_uses_mock_and_swallows_errors():
    import httpx

    from app.fetcher.images import fetch_og_image

    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='<meta property="og:image" content="https://x.com/a.jpg">')

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with httpx.Client(transport=httpx.MockTransport(ok)) as c:
        assert fetch_og_image("https://x.com/p", client=c) == "https://x.com/a.jpg"
    with httpx.Client(transport=httpx.MockTransport(boom)) as c:
        assert fetch_og_image("https://x.com/p", client=c) is None


def test_backfill_images_marks_empty_so_it_does_not_retry(seeded_db):
    """查过没有图的标记成 []，避免每轮都重查同一篇（省请求，也省对方带宽）。"""
    import json

    import httpx
    from sqlalchemy import select

    from app.db import session_scope
    from app.fetcher.images import backfill_images
    from app.models import Article

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text="<html>没有图</html>")

    with session_scope() as session:
        session.add(Article(title="无图文章", link="https://example.com/nopic", status="processed", relevance=1))

    def fetcher(url, **kwargs):  # noqa: ARG001
        return None

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        import app.fetcher.images as mod

        original = mod.fetch_og_image
        mod.fetch_og_image = lambda url, **kw: original(url, client=client)
        try:
            with session_scope() as session:
                stats = backfill_images(session, limit=10)
        finally:
            mod.fetch_og_image = original

    assert stats["candidates"] == 1
    assert stats["not_found"] == 1
    with session_scope() as session:
        row = session.execute(select(Article)).scalar_one()
        assert json.loads(row.image_urls) == []

    # 第二轮不该再查它
    with session_scope() as session:
        assert backfill_images(session, limit=10)["candidates"] == 0
    assert calls["n"] == 1
    assert fetcher is not None
