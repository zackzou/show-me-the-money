"""图片本地化：下载落盘、尺寸过滤、/img/ 路由。"""

from __future__ import annotations

import struct
import zlib

import httpx

from app.fetcher import media_store
from app.fetcher.media_store import (
    BODY_MIN_HEIGHT,
    BODY_MIN_WIDTH,
    is_safe_image_name,
    localize_anchors,
    measure,
    media_dir_for,
    read_media_map,
    save_image,
)


def _png(width: int, height: int) -> bytes:
    """现场拼一张最小 PNG（只要 IHDR 能被量出尺寸）。"""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr
    raw += struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
    return raw


def _client(data: bytes) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=data, headers={"content-type": "image/png"}
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_measure_reads_png_size():
    assert measure(_png(600, 400)) == (600, 400)
    assert measure(b"not an image") is None


def test_save_image_reuses_same_file(tmp_path):
    data = _png(600, 400)
    first = save_image(data, "https://example.com/a.png", tmp_path)
    second = save_image(data, "https://example.com/a.png", tmp_path)
    assert first == second
    assert (tmp_path / first).exists()
    # 不同 URL 即使内容一样也是两个文件（文件名按 URL 哈希）
    other = save_image(data, "https://example.com/b.png", tmp_path)
    assert other != first


def test_is_safe_image_name():
    assert is_safe_image_name("a" * 40 + ".png")
    assert not is_safe_image_name("../../etc/passwd")
    assert not is_safe_image_name("a" * 40 + ".exe")
    assert not is_safe_image_name("")


def test_media_dir_lives_next_to_db(tmp_path):
    assert media_dir_for(tmp_path / "data" / "smtm.db").name == "img"


def test_localize_anchors_drops_badges_and_keeps_photos(tmp_path):
    """GitHub 徽章（120×20）丢掉，真图（600×400）留下并给 local。"""
    badge = _png(120, 20)
    photo = _png(600, 400)

    def handler(request: httpx.Request) -> httpx.Response:
        data = badge if "badge" in str(request.url) else photo
        return httpx.Response(200, content=data, headers={"content-type": "image/png"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    anchors = [
        {"i": 0, "url": "https://camo.example.com/abc123badge"},
        {"i": 3, "url": "https://example.com/photo.png"},
        {"i": 5, "url": "not-a-url"},
    ]
    out = localize_anchors(anchors, referer="https://example.com/p", media_dir=tmp_path, client=client)
    assert len(out) == 1
    assert out[0]["i"] == 3
    assert out[0]["local"].startswith("/img/")
    assert (tmp_path / out[0]["local"].split("/")[-1]).exists()


def test_localize_anchors_keeps_existing_local(tmp_path):
    anchors = [{"i": 2, "url": "https://example.com/x.png", "local": "/img/" + "b" * 40 + ".png"}]
    out = localize_anchors(anchors, media_dir=tmp_path, client=_client(b""))
    assert out == anchors  # 有 local 的不再下载


def test_read_media_map_tolerates_garbage():
    assert read_media_map(None) == {}
    assert read_media_map("not json") == {}
    assert read_media_map('{"a": "http://x"}') == {}  # 非 /img/ 值不要
    assert read_media_map('{"http://x/a.png": "/img/' + "c" * 40 + '.png"}') == {
        "http://x/a.png": "/img/" + "c" * 40 + ".png"
    }


def test_routes_prefer_local_images():
    from app.web.routes import _body_image_anchors, _images

    raw = (
        '[{"i": 1, "url": "http://x/a.png", "local": "/img/' + "d" * 40 + '.png"},'
        ' {"i": 2, "url": "http://x/b.png"}]'
    )
    assert _body_image_anchors(raw) == [(1, "/img/" + "d" * 40 + ".png"), (2, "http://x/b.png")]
    # 老格式照样能读
    assert _body_image_anchors('[{"i": 0, "url": "http://x/c.png"}]') == [(0, "http://x/c.png")]
    assert _images('["http://x/a.png"]', '{"http://x/a.png": "/img/' + "e" * 40 + '.png"}') == [
        "/img/" + "e" * 40 + ".png"
    ]
    assert _images('["http://x/a.png"]', None) == ["http://x/a.png"]


def test_img_route_rejects_traversal(client):
    response = client.get("/img/../../etc/passwd")
    assert response.status_code in (404, 422)
    response = client.get("/img/" + "f" * 40 + ".png")
    assert response.status_code == 404  # 名字合法但文件不存在


def test_body_min_threshold_kills_badges():
    # 徽章约 120×20：两个维度都过不了；竖版截图 300×600 能过
    assert BODY_MIN_WIDTH > 120
    assert BODY_MIN_HEIGHT > 20
    assert BODY_MIN_WIDTH <= 300
    assert BODY_MIN_HEIGHT <= 600
    assert media_store.BODY_MIN_WIDTH == BODY_MIN_WIDTH
