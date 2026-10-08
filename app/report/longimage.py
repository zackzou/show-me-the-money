"""长图服务端渲染：把早报节点画成微信可发的 PNG（1080px 宽）。

为什么放服务端而不是只用前端 Canvas：
- Hermes / 微信 iLink 推送需要**可直接下载的图片 URL**（发图不能靠浏览器截图）；
- 服务端渲染结果稳定（同一份内容在任何设备/时区画出来一致），
  也方便测试（像素级断言文字不出卡片）。

字体：容器里没有中文字体，Dockerfile 装了 fonts-noto-cjk；
开发机回退到 macOS 的苹方/黑体。找不到任何 CJK 字体时降级为
Pillow 默认字体（英文可读、中文会变方框）并在响应头/日志里明示。
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from app.utils.logger import get_logger

log = get_logger(__name__)

W = 1080
PAD = 64
INNER_W = W - PAD * 2 - 64  # 卡片内文字可用宽度（左右各留 32 内边距）

FONT_CANDIDATES = (
    # Docker（fonts-noto-cjk）
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
)
BOLD_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
)
# 彩色 emoji 字体：PingFang/Noto CJK 都没有 emoji 字形，混排的 emoji
# 会画成方框（实测天气卡里的 ☀️/👕 全变豆腐块）。单独找一份 emoji 字体，
# 遇到 emoji 码点用 embedded_color 绘制。
EMOJI_CANDIDATES = (
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/noto/NotoColorEmoji.ttf",
    "/System/Library/Fonts/Apple Color Emoji.ttc",
)


def _find_font(candidates: tuple[str, ...]) -> str | None:
    for path in candidates:
        if Path(path).is_file():
            return path
    return None


_font_path_cache: dict[str, str | None] = {}


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    key = "bold" if bold else "regular"
    if key not in _font_path_cache:
        _font_path_cache[key] = _find_font(BOLD_CANDIDATES if bold else FONT_CANDIDATES)
    path = _font_path_cache[key]
    if path is None:
        log.warning("找不到中文字体，长图将用默认字体（中文可能显示为方框）")
        return ImageFont.load_default()
    try:
        return ImageFont.truetype(path, size)
    except OSError:  # pragma: no cover - 字体文件损坏
        return ImageFont.load_default()


_emoji_font_cache: dict[int, tuple[Any, int] | None] = {}


def _emoji_font(size: int) -> tuple[Any, int] | None:
    """emoji 字体 + 实际加载字号（不可用时 None，调用方回退普通字体）。

    彩色 emoji 字体基本都是位图字体，只能在固定字号加载：
    - Apple Color Emoji：20/40/48/64/96/160 可用（30/109/128 抛 OSError）
    - NotoColorEmoji（Docker）：只有一个 109px 的原生字面
    按请求字号加载失败时退到候选里最接近的字号；实际字号一起返回，
    位图远大于目标时由 _draw_text 缩到目标大小（否则 109px 的 emoji
    会压住上下好几行文字）。
    """
    if size in _emoji_font_cache:
        return _emoji_font_cache[size]
    path = _find_font(EMOJI_CANDIDATES)
    result: tuple[Any, int] | None = None
    if path is not None:
        for attempt in (size, 40, 48, 64, 96, 20, 160, 109):
            try:
                result = (ImageFont.truetype(path, attempt), attempt)
                break
            except OSError:
                continue
    _emoji_font_cache[size] = result
    return result


_emoji_tile_cache: dict[tuple[str, int, int], Image.Image | None] = {}


def _emoji_tile(ch: str, emoji_font: Any, native: int, target: int) -> Image.Image | None:
    """把单个 emoji 画成 target 像素级的透明小图（字形缺失时 None）。

    位图字体没有目标字号时先画到原生字面大小的画布，裁掉空白再等比
    缩小 —— 直接 draw.text 到正文里会画出 109px 的巨大 emoji。
    """
    key = (ch, native, target)
    if key in _emoji_tile_cache:
        return _emoji_tile_cache[key]
    canvas = Image.new("RGBA", (native * 2, native * 2), (0, 0, 0, 0))
    ImageDraw.Draw(canvas).text((0, 0), ch, font=emoji_font, embedded_color=True)
    bbox = canvas.getbbox()
    if bbox is None:
        _emoji_tile_cache[key] = None
        return None
    glyph = canvas.crop(bbox)
    px = max(1, round(target * 4 / 3))
    scale = px / max(glyph.width, glyph.height)
    tile = glyph.resize(
        (max(1, round(glyph.width * scale)), max(1, round(glyph.height * scale))),
        Image.Resampling.LANCZOS,
    )
    _emoji_tile_cache[key] = tile
    return tile


def _is_emoji(ch: str) -> bool:
    code = ord(ch)
    return (
        0x1F300 <= code <= 0x1FAFF
        or 0x2600 <= code <= 0x27BF
        or 0x1F000 <= code <= 0x1F2FF
        or code in (0x2764, 0x2B50, 0x2705, 0x274C, 0x2728, 0x2600, 0x26C5)
        or 0xFE00 <= code <= 0xFE0F
        or 0x1F1E6 <= code <= 0x1F1FF
    )


def _is_variation_selector(ch: str) -> bool:
    """变体选择符（U+FE00-FE0F）：属于前一个 emoji，不单独画。"""
    return 0xFE00 <= ord(ch) <= 0xFE0F


def _draw_text(
    image: Image.Image,
    xy: tuple[int, int],
    text: str,
    font: Any,
    fill: str,
) -> None:
    """绘制一行文本，emoji 用彩色字体逐段画。

    普通字体没有 emoji 字形，直接画是方框；把 emoji 切出来渲染成
    透明小图再贴回（位图字体不支持任意字号，109px 直接画会压住
    上下好几行）。其余部分保持原字体，字宽由普通字体量，保证与
    _wrap 的换行计算一致。变体选择符（如 ☀️ 里的 FE0F）跟随前一个
    emoji，单独画只会是方框。
    """
    draw = ImageDraw.Draw(image)
    emoji = _emoji_font(getattr(font, "size", 30))
    if emoji is None:
        draw.text(xy, text, font=font, fill=fill)
        return
    emoji_font, native = emoji
    size = getattr(font, "size", 30)
    x = float(xy[0])
    y = float(xy[1])
    segment = ""
    for ch in text:
        if _is_variation_selector(ch):
            continue
        if _is_emoji(ch):
            if segment:
                draw.text((x, y), segment, font=font, fill=fill)
                x += draw.textlength(segment, font=font)
                segment = ""
            tile = _emoji_tile(ch, emoji_font, native, size)
            if tile is None:
                draw.text((x, y), ch, font=font, fill=fill)
                x += draw.textlength(ch, font=font)
                continue
            # 垂直居中于正文行盒，右留 3px 间隔
            image.paste(tile, (round(x), round(y + (size - tile.height) / 2)), tile)
            x += tile.width + 3
        else:
            segment += ch
    if segment:
        draw.text((x, y), segment, font=font, fill=fill)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: Any, max_width: int) -> list[str]:
    """断行：CJK 逐字、拉丁按词（与前端 Canvas 的 wrapText 同规则）。"""
    lines: list[str] = []
    for para in str(text or "").split("\n"):
        tokens = _tokenize(para)
        line = ""
        for token in tokens:
            test = line + token
            if draw.textlength(test, font=font) > max_width and line:
                lines.append(line.rstrip())
                line = token.lstrip() if token.strip() else ""
            else:
                line = test
        if line.strip():
            lines.append(line.rstrip())
        elif not lines:
            lines.append("")
    return lines


def _tokenize(para: str) -> list[str]:
    """把一段话切成「逐字（CJK/标点）」与「整词（拉丁）」的 token 流。"""
    tokens: list[str] = []
    buf = ""
    for ch in para:
        if _is_cjk(ch) or ch == " ":
            if buf:
                tokens.append(buf)
                buf = ""
            tokens.append(ch)
        else:
            buf += ch
    if buf:
        tokens.append(buf)
    return tokens


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3000 <= code <= 0x303F
        or 0xFF00 <= code <= 0xFFEF
    )


def render_section_png(
    section: dict[str, Any],
    *,
    date_str: str,
    subtitle: str = "",
) -> bytes:
    """渲染一个节点为 PNG 字节。

    ``section``：``{"name", "type", "meta", "entries"}``（与 build_brief 一致）。
    """
    f_title = _font(40, bold=True)
    f_sub = _font(26)
    f_item_t = _font(36, bold=True)
    f_body = _font(30)
    f_meta = _font(24)

    # ── 第一遍：量高度 ──────────────────────────────────────────
    probe = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(probe)

    blocks: list[dict[str, Any]] = []
    y = 170
    if section.get("name"):
        lines = _wrap(draw, section["name"], f_title, INNER_W)
        blocks.append({"kind": "section", "lines": lines, "h": 52 * len(lines) + 46})
        y += 52 * len(lines) + 46
    if subtitle and section.get("type") == "news":
        lines = _wrap(draw, subtitle, f_sub, INNER_W)
        blocks.append({"kind": "subtitle", "lines": lines, "h": 38 * len(lines) + 18})
        y += 38 * len(lines) + 18

    node_type = section.get("type") or "news"
    entries = section.get("entries") or []
    if node_type != "news":
        meta = section.get("meta") or {}
        plain = str(meta.get("text") or "").replace("**", "")
        lines = _wrap(draw, plain, f_body, INNER_W)
        h = 46 * len(lines) + 70
        blocks.append({"kind": "fixed", "lines": lines, "h": h})
        y += h
    elif not entries:
        lines = _wrap(draw, "（本节暂无内容）", f_body, INNER_W)
        h = 46 * len(lines) + 70
        blocks.append({"kind": "fixed", "lines": lines, "h": h})
        y += h
    else:
        for item in entries:
            t_lines = _wrap(draw, ("★ " if item.get("starred") else "") + item.get("title", ""),
                            f_item_t, INNER_W)
            b_lines = _wrap(draw, item.get("brief", ""), f_body, INNER_W)
            meta_txt = item.get("source", "")
            if item.get("category"):
                meta_txt += " · " + item["category"]
            if item.get("published"):
                meta_txt += " · " + item["published"]
            m_lines = _wrap(draw, meta_txt, f_meta, INNER_W)
            h = 56 * len(t_lines) + 46 * len(b_lines) + 40 * len(m_lines) + 90
            blocks.append({"kind": "item", "t": t_lines, "b": b_lines, "m": m_lines, "h": h})
            y += h
    y += 120

    # ── 第二遍：绘制 ────────────────────────────────────────────
    img = Image.new("RGB", (W, max(600, y)), "#f6f7f9")
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, W, 140], fill="#176b75")
    draw.text((PAD, 28), "早报", font=f_title, fill="#ffffff")
    draw.text((PAD, 84), date_str, font=f_sub, fill="#dfe9ea")
    brand = "Show Me the Money"
    brand_w = draw.textlength(brand, font=f_sub)
    draw.text((W - PAD - brand_w, 74), brand, font=f_sub, fill="#eaf4f5")

    cy = 200
    for block in blocks:
        kind = block["kind"]
        if kind == "section":
            draw.rectangle([PAD, cy - 4, PAD + 8, cy + 52 * len(block["lines"]) + 6],
                           fill="#176b75")
            for i, line in enumerate(block["lines"]):
                _draw_text(img, (PAD + 30, cy + 6 + i * 52), line, f_title, "#202a30")
            cy += block["h"]
        elif kind == "subtitle":
            for i, line in enumerate(block["lines"]):
                _draw_text(img, (PAD + 30, cy + 4 + i * 38), line, f_sub, "#89979d")
            cy += block["h"]
        elif kind == "fixed":
            card_h = block["h"] - 24
            draw.rectangle([PAD, cy, PAD + W - PAD * 2, cy + card_h], fill="#ffffff",
                           outline="#dfe4e1", width=2)
            ty = cy + 30
            for line in block["lines"]:
                _draw_text(img, (PAD + 32, ty), line, f_body, "#59656b")
                ty += 46
            cy += block["h"]
        else:
            card_h = block["h"] - 24
            draw.rectangle([PAD, cy, PAD + W - PAD * 2, cy + card_h], fill="#ffffff",
                           outline="#dfe4e1", width=2)
            ty = cy + 26
            for line in block["t"]:
                _draw_text(img, (PAD + 32, ty), line, f_item_t, "#202a30")
                ty += 56
            for line in block["b"]:
                _draw_text(img, (PAD + 32, ty), line, f_body, "#59656b")
                ty += 46
            for line in block["m"]:
                _draw_text(img, (PAD + 32, ty), line, f_meta, "#89979d")
                ty += 40
            cy += block["h"]

    footer = "—— Show Me the Money 自动生成"
    draw.text((PAD, img.height - 56), footer, font=f_meta, fill="#89979d")

    buffer = io.BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def font_available() -> bool:
    """容器里有没有可用的中文字体（状态页/自检用）。"""
    return _find_font(FONT_CANDIDATES) is not None
