"""文本与时间处理：清洗 RSS 正文、抽图、截断、标题归一化、标签切分、本地时间。

时间约定：全库统一存**北京时间（naive）**，这样「日报按天分组」跟用户看到的一天一致。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from html import unescape
from urllib.parse import urljoin

LOCAL_TZ = timezone(timedelta(hours=8))  # 北京时间

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_NORMALIZE_RE = re.compile(r"[^\w\u4e00-\u9fff]+")
_TAG_SPLIT_RE = re.compile(r"[,，、;；]")
_MD_BOLD_RE = re.compile(r"(\*\*|__)")
_MD_LINE_RE = re.compile(r"(?m)^\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s*)")
_IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
_ATTR_RE = re.compile(r"""(\w[\w:-]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")

# 这些图片不是内容：追踪像素、占位图、表情图、1x1 透明图
_IMAGE_NOISE_RE = re.compile(
    r"(pixel|spacer|blank|placeholder|emoji|gravatar|avatar|badge|logo|icon|ads?[-_/]|doubleclick|scorecardresearch)",
    re.I,
)
_IMAGE_EXT_RE = re.compile(r"\.(jpe?g|png|gif|webp|avif)(?:$|[?#])", re.I)


def now_local() -> datetime:
    """当前北京时间（naive，便于直接入库与比较）。"""
    return datetime.now(LOCAL_TZ).replace(tzinfo=None)


def to_local(value: datetime | None) -> datetime | None:
    """带时区的时间 → 北京时间（naive）；已是 naive 的原样返回。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(LOCAL_TZ).replace(tzinfo=None)


def strip_html(text: str | None) -> str:
    """去掉 RSS 正文里的标签与多余空白。"""
    if not text:
        return ""
    plain = _TAG_RE.sub(" ", text)
    plain = unescape_text(plain)
    return _WS_RE.sub(" ", plain).strip()


def unescape_text(text: str | None) -> str:
    """把 feed 里残留的 HTML 实体（&mdash; &#8217; &amp; 等）还原成字符。"""
    if not text:
        return ""
    return unescape(text)


def truncate(text: str | None, limit: int) -> str:
    """按字符数截断（中文友好），超出加省略号。"""
    if not text:
        return ""
    stripped = text.strip()
    return stripped if len(stripped) <= limit else stripped[:limit].rstrip() + "…"


def normalize_title(title: str | None) -> str:
    """标题归一化：小写、去标点、压缩空白 —— 用于相似度去重。"""
    if not title:
        return ""
    return _WS_RE.sub(" ", _NORMALIZE_RE.sub(" ", title.casefold())).strip()


def split_tags(raw: str | None, limit: int = 5) -> list[str]:
    """把 LLM 返回的标签串切成列表。"""
    if not raw:
        return []
    return [part.strip() for part in _TAG_SPLIT_RE.split(raw) if part.strip()][:limit]


def strip_markdown(text: str | None) -> str:
    """去掉模型偶尔带出来的 Markdown 标记。

    摘要是在网页/卡片里直接展示的纯文本，实测模型经常会写成
    ``**结论：** ……`` 或 ``- 要点``，原样显示就是一屏星号。
    """
    if not text:
        return ""
    plain = _MD_LINE_RE.sub("", text)
    plain = _MD_BOLD_RE.sub("", plain)
    return _WS_RE.sub(" ", plain).strip()


def _attr(tag: str, name: str) -> str:
    """从 `<img ...>` 标签里取属性值。"""
    for match in _ATTR_RE.finditer(tag):
        if match.group(1).lower() == name:
            return unescape(match.group(2) or match.group(3) or match.group(4) or "").strip()
    return ""


def _best_srcset_url(srcset: str) -> str:
    """从 `srcset` 里挑最大的那个候选（按 w 描述子排序）。"""
    best_url, best_weight = "", -1
    for candidate in srcset.split(","):
        parts = candidate.strip().split()
        if not parts:
            continue
        url = parts[0]
        weight = 0
        if len(parts) > 1 and parts[1].lower().endswith("w"):
            try:
                weight = int(parts[1][:-1])
            except ValueError:
                weight = 0
        elif len(parts) > 1 and parts[1].lower().endswith("x"):
            try:
                weight = int(float(parts[1][:-1]) * 1000)
            except ValueError:
                weight = 0
        if weight > best_weight:
            best_url, best_weight = url, weight
    return best_url


def extract_images(html_text: str | None, *, base_url: str = "", limit: int = 6) -> list[str]:
    """从 RSS 正文 HTML 里抽出配图地址（按出现顺序，去重、滤噪）。

    这是「页内直接看图」的数据来源：RSS 的 ``content``/``description`` 里通常已经带了
    ``<img>``，只是以前入库时被 ``strip_html`` 一起丢掉了。
    """
    if not html_text:
        return []
    urls: list[str] = []
    seen: set[str] = set()
    for tag in _IMG_RE.findall(html_text):
        raw = (
            _attr(tag, "src")
            or _attr(tag, "data-src")
            or _attr(tag, "data-original")
            or _attr(tag, "data-lazy-src")
            or _best_srcset_url(_attr(tag, "srcset") or _attr(tag, "data-srcset"))
        )
        if not raw or raw.startswith("data:"):
            continue
        url = urljoin(base_url, raw) if base_url else raw
        if url in seen:
            continue
        if _IMAGE_NOISE_RE.search(url):
            continue
        seen.add(url)
        urls.append(url)
        if len(urls) >= limit:
            break
    return urls


def looks_english(text: str | None) -> bool:
    """粗判一段文字是不是（以）英文。

    样本要求 >40 个拉丁字母：太短的文本判不准，不如保守地当作「需要翻译」，
    宁可多翻一次，也不要把英文原文当成中文漏掉。

    实测踩过的坑：早期版本把「标题 + 摘要」拼起来一起判，于是「英文标题 + 中文摘要」
    这种最常见的组合会被判成英文，摘要永远没人翻。所以这里只看传入的这一段本身。
    """
    sample = (text or "")[:400]
    if not sample:
        return False
    ascii_letters = sum(1 for ch in sample if ch.isascii() and ch.isalpha())
    cjk = sum(1 for ch in sample if "一" <= ch <= "鿿")
    return ascii_letters > cjk * 2 and ascii_letters > 40


def is_chinese_text(text: str | None) -> bool:
    """这段文字里有没有汉字 —— 不设长度门槛。

    ``looks_english`` 要求 40 个字母以上，短标题会被判成「不是英文」：
    ``The dawn of the age of the exoskeleton`` 只有 30 个字母，于是它的中文标题
    永远翻不出来，详情页在「中文」模式下还是顶着英文。而「要不要翻」这个问题
    本来就跟长短无关 —— 看有没有汉字就够了。
    """
    return any("一" <= ch <= "鿿" for ch in (text or "")[:400])


# 早报片段的汇总上限：微信早报一行约 22 个汉字，3~5 行就在这个量级
BRIEF_DIGEST_CHARS = 108
_BRIEF_SENTENCE_END = "。！？.!?"
# 次级断点：整句放不下时，至少断在子句边界，不要把半句话甩给读者
_BRIEF_CLAUSE_END = "，、；：,;:"
# 一整句最长允许多少倍预算：超过说明这不是「一句话」，得再切
_BRIEF_HARD_MAX = BRIEF_DIGEST_CHARS * 2


def brief_digest(text: str | None, *, limit: int = BRIEF_DIGEST_CHARS) -> str:
    """把一段导读压成**一句完整的话**，用于早报 / 推送。

    早报读者是在手机上扫读，塞一整段进去没人看。但**必须是一句完整的话**：

    实测踩过的坑 —— 早先的写法在一整句放不下 ``limit`` 时会退回「硬截断 + 省略号」，
    产出过这种东西：「…伦理声明白。Only $30 more than the wireless charging ver…」。
    推送到手机上就是半句话，读者根本不知道这条在讲什么，而页面看起来却是正常的。

    所以这里的规则是：**宁可超预算，也要完整**。
    1. 按句号切，累计到接近上限就收；
    2. 一整句都放不下 → 就返回那一整句（超一点没关系，不能是半句）；
    3. 单句离谱地长 → 退到子句边界断，并在末尾补句号；
    4. 任何情况下都不以省略号结尾。
    """
    clean = " ".join((text or "").split())
    if not clean:
        return ""
    out: list[str] = []
    used = 0
    for piece in re.split(f"(?<=[{re.escape(_BRIEF_SENTENCE_END)}])", clean):
        piece = piece.strip()
        if not piece:
            continue
        if used + len(piece) > limit:
            break
        out.append(piece)
        used += len(piece)
        if used >= limit - 20:
            break
    if out:
        brief = "".join(out).strip()
    else:
        # 没有一句能塞进预算：取第一句，完整优先于简短
        first = re.split(f"(?<=[{re.escape(_BRIEF_SENTENCE_END)}])", clean)[0].strip()
        brief = first or clean
    if len(brief) > _BRIEF_HARD_MAX:
        # 在整段里找**最后一个子句边界**，而不是只看前 ``limit`` 个字。
        # 早先的 rfind 上界是 ``limit``，于是「一段没有标点的超长中文」会全部
        # 返回 -1、cut=0，掉进 ``brief[:limit]`` 这个硬截断分支 —— 正好违反
        # 本函数「宁可超预算也要完整」的前提，还会在断口后面硬补一个句号，
        # 把半句话伪装成完整的一句。
        cut = max(brief.rfind(ch) for ch in _BRIEF_CLAUSE_END) + 1
        if cut > 20:
            brief = brief[:cut]
        # 找不到任何子句边界就整段留着：截断只会造出半句话，补句号只会让它
        # 看起来像完整的一句 —— 两个都比超长更糟。
    brief = brief.strip()
    if brief and brief[-1] not in _BRIEF_SENTENCE_END:
        brief += "。"
    return brief


def chinese_ratio(text: str | None) -> float:
    """汉字占「汉字+拉丁字母」的比例。用来判断一段文字是不是中文。"""
    sample = (text or "")[:400]
    han = sum(1 for ch in sample if "一" <= ch <= "鿿")
    latin = sum(1 for ch in sample if ch.isascii() and ch.isalpha())
    total = han + latin
    return han / total if total else 0.0


def has_long_latin_run(text: str | None, *, min_chars: int = 10) -> bool:
    """有没有**一整句**没翻的英文。

    早先试过用正则数连续英文字母（「(?:[A-Za-z][A-Za-z'-]*[ ,]){11,}[A-Za-z]」），
    结果漏得很难看：「Only $30 more than the wireless charging version」里有个
    ``$30`` 就把整段打断了，认不出来 —— 而这恰恰是最该被拦下的那种。

    改成按**句**判断：把文本按句末标点切开，逐句看汉字占比。
    专有名词不会误伤（「AirPods 5 较其无线充版仅贵 30 美元」汉字占多数），
    没翻的整句一定会露出来。
    """
    for sentence in re.split(r"(?<=[。！？!?])|\n", text or ""):
        piece = sentence.strip()
        if len(piece) < min_chars:
            continue
        if chinese_ratio(piece) < 0.3:
            return True
    return False
