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


def test_body_image_wins_over_og_image():
    """og:image 在中文站点上极易是站点 logo，所以正文图优先，og:image 只兜底。

    这里 OG_HTML 的正文首图是 logo.png（会被过滤），第一张可用的是 inline.jpg；
    它必须排在 og:image 的 hero-1200.jpg 前面。
    """
    from app.fetcher.images import extract_og_image

    assert extract_og_image(OG_HTML) == "https://cdn.example.com/inline.jpg"
    assert extract_og_image("<html><head></head><body>无图</body></html>") is None


def test_og_image_used_when_body_has_no_usable_image():
    """正文没有可用图时才轮到 og:image。"""
    from app.fetcher.images import extract_og_image

    html = (
        '<html><head><meta property="og:image" content="https://cdn.example.com/hero-1200.jpg">'
        '</head><body><img src="/logo.png"></body></html>'
    )
    assert extract_og_image(html) == "https://cdn.example.com/hero-1200.jpg"


def test_extract_og_image_skips_logo_falls_back_to_first_img():
    from app.fetcher.images import extract_og_image

    html = '<html><body><img src="https://x.com/logo.png"><img src="https://x.com/real.jpg"></body></html>'
    assert extract_og_image(html) == "https://x.com/real.jpg"


def test_fetch_content_image_uses_mock_and_swallows_errors():
    import httpx

    from app.fetcher.images import fetch_content_image

    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='<meta property="og:image" content="https://x.com/a.jpg">')

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with httpx.Client(transport=httpx.MockTransport(ok)) as c:
        assert fetch_content_image("https://x.com/p", client=c) == "https://x.com/a.jpg"
    with httpx.Client(transport=httpx.MockTransport(boom)) as c:
        assert fetch_content_image("https://x.com/p", client=c) is None


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

        original = mod.fetch_content_image
        mod.fetch_content_image = lambda url, **kw: original(url, client=client)
        try:
            with session_scope() as session:
                stats = backfill_images(session, limit=10)
        finally:
            mod.fetch_content_image = original

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


def test_site_default_image_is_rejected(seeded_db):
    """同一张图在同一信源下反复出现 → 视为站点通用图，不挂上去。"""
    import json

    from sqlalchemy import select

    from app.db import session_scope
    from app.fetcher.images import backfill_images
    from app.models import Article, Source
    from app.utils.text import now_local

    common = "https://cdn.example.com/site-logo.png"
    with session_scope() as session:
        source_id = session.query(Source).one().id
        # 先前已经有 3 篇用过同一张图 → 站点通用图
        for i in range(3):
            session.add(Article(title=f"老文章{i}", link=f"https://example.com/old{i}",
                                status="processed", relevance=1, image_urls=json.dumps([common]),
                                source_id=source_id, published_at=now_local()))
        session.add(Article(title="新文章", link="https://example.com/newone", status="processed",
                            relevance=1, source_id=source_id, published_at=now_local()))

    import app.fetcher.images as mod

    original = mod.fetch_content_image
    mod.fetch_content_image = lambda url, **kw: common  # 站点所有文章都返回这张
    try:
        with session_scope() as session:
            stats = backfill_images(session, limit=10)
    finally:
        mod.fetch_content_image = original

    assert stats["site_default"] == 1
    assert stats["filled"] == 0
    with session_scope() as session:
        row = session.execute(select(Article).where(Article.title == "新文章")).scalar_one()
        assert json.loads(row.image_urls) == []  # 标记为「查过、无可用图」


# ── 挑图：下面每一条都是实测踩出来的坑 ──────────────────────────────────────

# 量子位真实文章的头部：og:image 是站点 logo，头图 URL 叫 head.jpg 但 class 写着
# avatar avatar-200，正文里 30 张 CDN 图 URL 既没有扩展名也没有 "image" 字样，
# 而且 alt 文案里含 "signature"（原文提到手写签名）。
QBITAI_HTML = """<html><head>
<meta property="og:image" content="https://www.qbitai.com/wp-content/uploads/imgs/qbitai-logo-1.png">
<meta property="og:image:secure_url" content="https://www.qbitai.com/wp-content/uploads/imgs/qbitai_icon.png">
</head><body>
<img src="/wp-content/uploads/2019/01/qrcode_QbitAI_1.jpg">
<img class="avatar avatar-200" width="200"
     src="http://www.qbitai.com/wp-content/themes/liangziwei/imagesnew/head.jpg">
<img src="https://p3-sign.toutiaoimg.com/tos-cn-i-axegupay5k/aaa?x=1&amp;y=2" alt="访谈现场，作者展示手写 signature">
<img src="https://p3-sign.toutiaoimg.com/tos-cn-i-6w9my0ksvp/bbb">
<img class="attachment-thumbnail size-thumbnail" width="128" src="https://i.qbitai.com/wp-content/uploads/x.jpg">
</body></html>"""


def test_junk_image_filter_ignores_alt_text():
    """alt 是自然语言，里面的 "signature" 不是排除依据（否则真图全被误杀）。"""
    from app.fetcher.images import looks_like_junk_image

    real = "https://p3-sign.toutiaoimg.com/tos-cn-i-axegupay5k/aaa?x=1&y=2"
    assert looks_like_junk_image(url=real, tag='alt="作者展示手写 signature"') is False


def test_junk_image_filter_catches_class_avatar_even_if_url_is_clean():
    """量子位作者头图 URL 叫 head.jpg（不含 avatar），靠 class 才能认出来。"""
    from app.fetcher.images import looks_like_junk_image

    url = "http://www.qbitai.com/wp-content/themes/liangziwei/imagesnew/head.jpg"
    assert looks_like_junk_image(url=url, tag='class="avatar avatar-200" width="200"') is True
    assert looks_like_junk_image(url="https://x.com/a.jpg", tag='class="entry-content"') is False


def test_stem_hints_do_not_kill_real_words():
    """"head-phones.jpg"（耳机图）不能因为含 head 被误杀，只有整个文件名是 head 才排除。"""
    from app.fetcher.images import looks_like_junk_image

    assert looks_like_junk_image(url="https://x.com/head-phones.jpg", tag="") is False
    assert looks_like_junk_image(url="https://x.com/head.jpg", tag="") is True


def test_small_declared_width_is_skipped():
    from app.fetcher.images import looks_like_junk_image

    assert looks_like_junk_image(url="https://x.com/photo.jpg", tag='width="128"') is True
    assert looks_like_junk_image(url="https://x.com/photo.jpg", tag='width="900"') is False


def test_collect_candidates_keeps_extensionless_cdn_and_unescapes():
    """无扩展名的 CDN 图要留下；属性里的 &amp; 必须还原成 & 否则 URL 404。"""
    from app.fetcher.images import collect_image_candidates

    got = collect_image_candidates(QBITAI_HTML, base_url="https://www.qbitai.com/2026/10/500148.html")
    assert "https://p3-sign.toutiaoimg.com/tos-cn-i-axegupay5k/aaa?x=1&y=2" in got
    # 站点 logo / 图标 / 二维码 / 头像 / 缩略图都不该进来
    assert not any("qbitai-logo" in u for u in got)
    assert not any("qbitai_icon" in u for u in got)
    assert not any("qrcode" in u for u in got)
    assert not any("imagesnew/head.jpg" in u for u in got)
    assert not any("i.qbitai.com" in u for u in got)


def test_body_images_rank_before_og_image():
    """og:image 在中文站点上极易是站点 logo，正文大图要排在它前面。"""
    from app.fetcher.images import collect_image_candidates

    got = collect_image_candidates(QBITAI_HTML, base_url="https://www.qbitai.com/")
    assert got[0].startswith("https://p3-sign.toutiaoimg.com/")


def test_parse_image_size_formats():
    from app.fetcher.images import parse_image_size

    def jpeg(w: int, h: int) -> bytes:
        # SOI + APP0 填充段 + SOF0：结构必须是 APP0 在前，否则会先读到别的标记
        return (
            bytes([0xFF, 0xD8])
            + bytes([0xFF, 0xE0]) + (16).to_bytes(2, "big") + b"JFIF\x00" + b"\x00" * 9
            + bytes([0xFF, 0xC0]) + (17).to_bytes(2, "big") + b"\x08"
            + h.to_bytes(2, "big") + w.to_bytes(2, "big") + b"\x03" + b"\x00" * 9
        )

    assert parse_image_size(jpeg(1200, 675)) == (1200, 675)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00" * 4
        + b"IHDR"
        + (640).to_bytes(4, "big")
        + (480).to_bytes(4, "big")
    )
    assert parse_image_size(png) == (640, 480)
    gif = b"GIF89a" + (300).to_bytes(2, "little") + (200).to_bytes(2, "little")
    assert parse_image_size(gif) == (300, 200)
    webp = (
        b"RIFF" + (0).to_bytes(4, "little") + b"WEBPVP8X"
        + (10).to_bytes(4, "little") + b"\x00" * 4
        + (799).to_bytes(3, "little") + (599).to_bytes(3, "little")
    )
    assert parse_image_size(webp) == (800, 600)
    assert parse_image_size(b"not an image at all") is None


def test_pick_image_probes_size_and_skips_small():
    """第一张图太小 → 顺次挑下一张够大的，而不是把第一张直接用上。"""
    import httpx

    from app.fetcher.images import pick_content_image

    sizes = {
        "https://cdn.x.com/small.jpg": (100, 80),
        "https://cdn.x.com/wide.jpg": (1200, 675),
        "https://cdn.x.com/tiny.jpg": (64, 64),
    }

    def png_bytes(w: int, h: int) -> bytes:
        return (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00" * 4
            + b"IHDR"
            + w.to_bytes(4, "big")
            + h.to_bytes(4, "big")
        )

    def handler(request: httpx.Request) -> httpx.Response:
        w, h = sizes[str(request.url)]
        return httpx.Response(206, content=png_bytes(w, h) + b"\x00" * 16)

    html = "<body>" + "".join(f'<img src="{u}">' for u in sizes) + "</body>"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert pick_content_image(html, client=client) == "https://cdn.x.com/wide.jpg"


def test_pick_image_falls_back_to_first_when_size_unknown():
    """探测不出来（比如图片服务器不返回 Range）时，退回第一张候选，总比没图好。"""
    import httpx

    from app.fetcher.images import pick_content_image

    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"<html>"))) as client:
        got = pick_content_image('<img src="https://cdn.x.com/a.jpg">', client=client)
    assert got == "https://cdn.x.com/a.jpg"
