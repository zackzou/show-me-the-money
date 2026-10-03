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
