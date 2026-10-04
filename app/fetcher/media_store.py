"""图片本地落盘：原文的图下载到本地再展示，并按真实尺寸过滤。

为什么需要（三件事都是线上实测翻过车）：

1. **防盗链。** 部分站点（量子位图片 CDN 等）对非本站 Referer 直接 403，
   详情页 `<img src="原文地址">` 经常裂图。下载时带上文章页 Referer，
   展示走自家 `/img/`，不再看原站脸色。

2. **徽章当配图。** GitHub README 的 shields.io 徽章经 camo 转发后，
   URL 变成纯十六进制，关键词过滤认不出来。徽章只有 100×20 像素，
   按真实尺寸卡一下就全掉 —— 这比再加关键词靠谱（下一个徽章站换个域名
   又得补）。

3. **一次下载、两用。** 量尺寸本来就要读文件头（Range），下载整图后
   直接从字节里量，不再多发探测请求。8MB 上限，超了就不要。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import httpx

from app.utils.logger import get_logger

log = get_logger(__name__)

DEFAULT_UA = "ShowMeTheMoney/0.2 (+https://github.com/zackzou/show-me-the-money)"
# 单张图最多下这么多；超出就放弃（原图太大，页内展示也用不上）
MAX_IMAGE_BYTES = 8 * 1024 * 1024
DOWNLOAD_TIMEOUT = 15.0
# 图片文件头里尺寸字段所在的最大偏移（解析失败就当量不出来）
_SIZE_SCAN_LIMIT = 64 * 1024


def _jpeg_size(head: bytes) -> tuple[int, int] | None:
    """从 JPEG 的 SOFn 标记里读宽高。"""
    pos = 2
    end = min(len(head), _SIZE_SCAN_LIMIT)
    while pos + 9 < end:
        if head[pos] != 0xFF:
            pos += 1
            continue
        marker = head[pos + 1]
        if marker in (0xFF, 0x01) or 0xD0 <= marker <= 0xD9:
            pos += 2
            continue
        if pos + 4 > len(head):
            return None
        length = int.from_bytes(head[pos + 2 : pos + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if pos + 9 <= len(head):
                height = int.from_bytes(head[pos + 5 : pos + 7], "big")
                width = int.from_bytes(head[pos + 7 : pos + 9], "big")
                if width and height:
                    return width, height
            return None
        if length < 2:
            return None
        pos += 2 + length
    return None


def _png_size(head: bytes) -> tuple[int, int] | None:
    if len(head) < 24 or head[12:16] != b"IHDR":
        return None
    width = int.from_bytes(head[16:20], "big")
    height = int.from_bytes(head[20:24], "big")
    return (width, height) if width and height else None


def _gif_size(head: bytes) -> tuple[int, int] | None:
    if len(head) < 10 or head[:3] != b"GIF":
        return None
    return int.from_bytes(head[6:8], "little"), int.from_bytes(head[8:10], "little")


def _webp_size(head: bytes) -> tuple[int, int] | None:
    if len(head) < 30 or head[:4] != b"RIFF" or head[8:12] != b"WEBP":
        return None
    chunk = head[12:16]
    if chunk == b"VP8X":
        width = int.from_bytes(head[24:27], "little") + 1
        height = int.from_bytes(head[27:30], "little") + 1
        return width, height
    if chunk == b"VP8 ":
        if head[23:26] != b"\x9d\x01\x2a":
            return None
        width = int.from_bytes(head[26:28], "little") & 0x3FFF
        height = int.from_bytes(head[28:30], "little") & 0x3FFF
        return (width, height) if width and height else None
    if chunk == b"VP8L":
        if head[20] != 0x2F:
            return None
        bits = int.from_bytes(head[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


def parse_image_size(head: bytes) -> tuple[int, int] | None:
    """从图片文件头解析 (宽, 高)。认不出来就返回 ``None``（不猜）。"""
    for parser in (_png_size, _gif_size, _jpeg_size, _webp_size):
        size = parser(head)
        if size:
            return size
    return None

# 正文内联配图的最小尺寸。徽章（约 120×20）、头像、表情会被拦掉；
# 竖版手机截图（300×600）能过。封面首图沿用 images.py 的 400×220，
# 正文里放宽一点 —— 内联图是按原图宽度居中，小一点不碍事。
BODY_MIN_WIDTH = 300
BODY_MIN_HEIGHT = 80

# 落盘文件名只允许这种形状，/img/ 路由按同样规则校验
_SAFE_NAME_RE = re.compile(r"^[a-f0-9]{40}\.(?:jpe?g|png|webp|gif|avif)$")

_MAGIC_EXT = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
)


def _ext_of(data: bytes, url: str) -> str | None:
    """按文件头认扩展名，认不出再看 URL 后缀，都不行就放弃（不猜）。"""
    for magic, ext in _MAGIC_EXT:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    tail = (url or "").split("?")[0].split("#")[0].rsplit("/", 1)[-1].lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"):
        if tail.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    return None


def media_dir_for(db_file: str | Path) -> Path:
    """图片目录：跟数据库放一起（data/img），备份/清理时一份带走。"""
    return Path(db_file).parent / "img"


def is_safe_image_name(name: str) -> bool:
    return bool(_SAFE_NAME_RE.match(name or ""))


def download_image(
    url: str, *, referer: str = "", client: httpx.Client | None = None
) -> bytes | None:
    """下载一张图。带文章页 Referer（过防盗链），只收 image/*，超限截断放弃。"""
    if not url or not url.startswith(("http://", "https://")):
        return None
    owns = client is None
    http = client or httpx.Client(
        timeout=DOWNLOAD_TIMEOUT, follow_redirects=True, headers={"User-Agent": DEFAULT_UA}
    )
    try:
        headers = {"Referer": referer} if referer else {}
        with http.stream("GET", url, headers=headers) as response:
            if response.status_code >= 400:
                return None
            ctype = (response.headers.get("content-type") or "").lower()
            if ctype and "image" not in ctype and "octet-stream" not in ctype:
                return None
            chunks: list[bytes] = []
            total = 0
            for piece in response.iter_bytes(64 * 1024):
                total += len(piece)
                if total > MAX_IMAGE_BYTES:
                    return None
                chunks.append(piece)
            return b"".join(chunks) or None
    except (httpx.HTTPError, ValueError):
        return None
    finally:
        if owns:
            http.close()


def save_image(data: bytes, url: str, media_dir: str | Path) -> str | None:
    """落盘，返回文件名（sha1(url)+扩展名）。已存在直接复用，不重复写。"""
    ext = _ext_of(data, url)
    if not ext:
        return None
    name = hashlib.sha1(url.encode("utf-8")).hexdigest() + ext
    path = Path(media_dir) / name
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return name


def measure(data: bytes) -> tuple[int, int] | None:
    """量真实像素尺寸（复用 images.py 的文件头解析）。"""
    try:
        return parse_image_size(data[: 64 * 1024])
    except (ValueError, IndexError):
        return None


def localize_one(
    url: str,
    *,
    referer: str = "",
    media_dir: str | Path = "",
    client: httpx.Client | None = None,
    min_width: int = BODY_MIN_WIDTH,
    min_height: int = BODY_MIN_HEIGHT,
) -> str | None:
    """下载→量尺寸→落盘，一条龙。返回 `/img/xxx`，太小/失败返回 ``None``。"""
    data = download_image(url, referer=referer, client=client)
    if not data:
        return None
    size = measure(data)
    if not size or size[0] < min_width or size[1] < min_height:
        return None
    name = save_image(data, url, media_dir)
    return f"/img/{name}" if name else None


def localize_anchors(
    anchors: list[dict[str, Any]],
    *,
    referer: str = "",
    media_dir: str | Path = "",
    client: httpx.Client | None = None,
    max_images: int = 12,
    stats: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """把 body_images 锚点逐个本地化：下不到、太小的一律丢掉。

    返回新列表（不改输入），条目 ``{"i": n, "url": 原地址, "local": "/img/.."}``。
    ``local`` 为空的会被调用方丢掉 —— 展示侧只认有 local 的。

    ``stats`` 传入字典时带回计数：``kept`` 留下、``too_small`` 太小永久丢、
    ``failed`` 没下下来（限流/抖动，下轮还值得再试）。调用方靠它区分
    「标空不再来」和「原样保留下轮重试」。
    """
    out: list[dict[str, Any]] = []
    failed = 0
    small = 0
    for item in anchors:
        if len(out) >= max_images:
            break
        if not isinstance(item, dict):
            continue
        index, url = item.get("i"), item.get("url")
        if not isinstance(index, int) or isinstance(index, bool):
            continue
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        if isinstance(item.get("local"), str) and item["local"].startswith("/img/"):
            out.append({"i": index, "url": url, "local": item["local"]})
            continue
        try:
            data = download_image(url, referer=referer, client=client)
        except Exception as exc:  # 单张图失败不影响其它图
            log.debug("图片下载失败：%s（%r）", url[:80], exc)
            failed += 1
            continue
        if not data:
            failed += 1
            continue
        size = measure(data)
        if not size or size[0] < BODY_MIN_WIDTH or size[1] < BODY_MIN_HEIGHT:
            small += 1
            continue
        try:
            name = save_image(data, url, media_dir)
        except OSError as exc:
            log.debug("图片落盘失败：%s（%r）", url[:80], exc)
            failed += 1
            continue
        if name:
            out.append({"i": index, "url": url, "local": f"/img/{name}"})
        else:
            small += 1
    if stats is not None:
        stats["kept"] = len(out)
        stats["too_small"] = small
        stats["failed"] = failed
    return out


def read_media_map(raw: str | None) -> dict[str, str]:
    """读 media_map（远端 URL → 本地 /img/ 路径），坏数据当没有。"""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(k): str(v) for k, v in parsed.items() if str(v).startswith("/img/")}
