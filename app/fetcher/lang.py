"""信源语言判定：中文站不做翻译，英文站必须有中文版。

为什么要「自适应」
------------------
一个站到底该按中文读还是按英文读，只有两种可靠办法：
看**抓回来的内容**，或者让用户自己填。IP 只能定位服务器，定位不了内容 ——
同一个 .com 域名下既有中文站也有英文站，而 CDN 的 IP 往往在境外。
所以默认走内容判定，用户改成固定语言也随时可以。

三条口径（按优先级）
--------------------
1. 用户显式指定 ``lang="zh"`` / ``lang="en"`` → 听用户的。
2. ``lang="auto"``（默认）→ 看这批文章的实际语言。
3. 单篇文章还会在处理时按正文再判一次（``looks_english``），
   因为同一个站偶尔会发英文稿（少数中文科技站会转载英文原文）。

为什么要区分
------------
中文站的文章**不做任何翻译**：中文译中文是纯浪费，而且会把「AI 重写」的
质量损耗套到本来就不需要改写的原文上。页面上也就不出现 EN / 双语按钮 ——
给一个永远切不出内容的按钮比不给更糟。
英文站则**必须**有中文版（页面默认显示中文），这是整个站的前提。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from app.utils.text import is_chinese_text, looks_english

LANG_AUTO = "auto"
LANG_EN = "en"
LANG_ZH = "zh"

# 判定「这批文章是什么语言」的取样条数：够稳定，又不用把整批都扫一遍。
_SAMPLE = 8


def resolve_lang(configured: str | None) -> str:
    """把用户填的语言值归一成 ``auto`` / ``en`` / ``zh``。"""
    value = (configured or "").strip().lower()
    return value if value in (LANG_AUTO, LANG_EN, LANG_ZH) else LANG_AUTO


def detect_from_texts(texts: Sequence[str]) -> str:
    """按一批正文/标题的实际语言判定信源语言。

    判据是**汉字占比**，不是「有没有汉字」：中文技术站的文章里常夹着大量
    英文专有名词与代码标识符（实测 InfoQ、InfoQ 中文站一篇里能出现几十个
    OpenAI / Kubernetes / GPT-4），用「有没有汉字」会把它们全判成中文站，
    然后这些文章就永远没有中文版了 —— 而它们其实是英文站。

    阈值 0.12：低于它说明汉字只是零星出现（英文正文里引用了几个中文词），
    按英文站处理。
    """
    han = latin = 0
    for text in texts[:_SAMPLE]:
        sample = (text or "")[:2000]
        han += sum(1 for ch in sample if "一" <= ch <= "鿿")
        latin += sum(1 for ch in sample if ch.isascii() and ch.isalpha())
    if han + latin == 0:
        return LANG_EN            # 什么都没有：保守当成需要翻译
    return LANG_ZH if han / (han + latin) >= 0.12 else LANG_EN


def detect_from_feed(items: Sequence[dict]) -> str:
    """从试抓回来的条目判定信源语言（添加信源时那一次）。"""
    texts: list[str] = []
    for item in items:
        title = str(item.get("title") or "")
        body = re.sub(r"<[^>]+>", " ", str(item.get("content") or ""))[:2000]
        texts.append(f"{title}\n{body}")
    return detect_from_texts(texts)


def needs_translation(configured: str | None, texts: Sequence[str]) -> bool:
    """这个信源/这批文章要不要产出中文版。"""
    lang = resolve_lang(configured)
    if lang == LANG_ZH:
        return False
    if lang == LANG_EN:
        return True
    return detect_from_texts(texts) == LANG_EN


def article_is_foreign(title: str, body: str) -> bool:
    """单篇文章是否需要中文版（处理时按正文再判一次）。

    ``auto`` 下信源级的判定可能覆盖不到个例（中文站偶尔发英文稿），
    所以每篇文章仍按自己的正文判断一次。判错的后果不对称：把中文当成英文
    去翻，翻出来还是中文；而把英文当成中文不翻，读者打开就整页英文。
    """
    sample = f"{title}\n{body}"[:1500]
    if is_chinese_text(sample) and not looks_english(sample):
        return False
    return looks_english(sample)
