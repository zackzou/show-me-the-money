"""文章处理：相关度判断（顺带评分）→ 摘要 → 速览 → 推荐理由 → 标签；LLM 不可用时降级。"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.ai.client import LLMClient, LLMError
from app.ai.prompts import (
    render_brief_prompt,
    render_classify_prompt,
    render_digest_prompt,
    render_reason_prompt,
    render_relevance_prompt,
    render_structure_prompt,
    render_summary_prompt,
    render_tag_prompt,
    render_translate_content_prompt,
    render_translate_digest_zh_prompt,
    render_translate_prompt,
    render_translate_title_zh_prompt,
)
from app.config import PromptsConfig, Settings
from app.db import session_scope
from app.fetcher.content import is_real_body
from app.fetcher.lang import (
    LANG_AUTO,
    LANG_EN,
    LANG_ZH,
    article_is_foreign,
    resolve_lang,
)
from app.models import Article, Source
from app.utils.logger import get_logger
from app.utils.text import (
    has_long_latin_run,
    is_chinese_text,
    looks_english,
    now_local,
    split_tags,
    strip_html,
    strip_markdown,
    truncate,
)

_TAG_SPLIT_RE = re.compile(r"[,，、;；]")

log = get_logger(__name__)

STATUS_PROCESSED = "processed"
STATUS_FAILED = "failed"

_SCORE_RE = re.compile(r"(\d{1,3})")

# 判定词：必须在整段里搜，不能只看开头 8 个字（见 parse_relevance）。
_RELEVANCE_VERDICT_RE = re.compile(
    r"\b(?:yes|no|y|n|true|false|relevant|irrelevant)\b|不相关|是|否|相关"
)
# 这些词表示「不相关」，注意要在匹配到的那个词上判断，而不是整段
_NEGATIVE_VERDICTS = frozenset({"no", "n", "false", "irrelevant", "否", "不相关"})
# 判定词之后的整数候选。要排除三种「长得像评分、其实不是」的数字：
#   · 千分位（``1,200 words`` 里的 200）
#   · 小数（``Confidence: 0.9`` 里的 9）
#   · 四位年份（``2024 coverage`` 里的 2024）
_SCORE_CANDIDATE_RE = re.compile(r"(?<![\d.,])(\d{1,4})(?![\d.,])")
# 判定词之前的老式写法：「85 yes」
_SCORE_LEADING_RE = re.compile(r"\s*(\d{1,3})\s*(?:yes|no|y|n|是|否)\b", re.I)

# 列表页只在分数不低于这个值时强调「值得看」
SCORE_STRONG = 70


def parse_relevance(answer: str) -> tuple[bool, int | None]:
    """解析相关度判断的结果：``yes 85`` → ``(True, 85)``。

    判定词在**整段**里找，不再只看开头 8 个字。原来只看前 8 字，于是模型
    回一句 ``Sure! Here is my assessment: yes 85`` 就会被判成「不相关」——
    而它明明说了 yes、还给了 85 分。那篇文章会被永久写进 relevance=0，
    从此不进日报、不进搜索、不出现在任何列表里（重排队也不会再看它）。

    分数只在**紧跟判定词**时才算数：``yes 85`` 取 85，而
    ``yes — highly relevant. Confidence: 0.9`` 不该取到那个 0.9（显示成
    「AI 评分 0」）、``yes 2024 coverage`` 也不该把 2024 截成 20。

    容忍各种不规范输出：光一个 ``yes``、中文「是的」、分数在前、分数越界等。
    评分是可选的 —— 模型没给就不给，不能因此把一条好内容判掉。
    """
    text = (answer or "").strip()
    lowered = text.casefold()
    match = _RELEVANCE_VERDICT_RE.search(lowered)
    if match is None:
        # 整段没有判定词：退回「开头就是 yes」的老行为，保证纯 "yes"/"是" 仍能用
        return lowered.startswith(("yes", "y", "是", "相关", "true")), _score_leading(text)
    relevant = match.group(0) not in _NEGATIVE_VERDICTS
    if not relevant:
        # 判为不相关就**不要分数**。见函数 docstring：留着 95 分会造出
        # relevance=0 却 score=95 的矛盾行，页面显示「AI 评分 95」，而任何按
        # score 排序或筛选的下游都会把一篇不进日报的文章当成高价值内容。
        return False, None
    score = _score_after(text, match.end())
    if score is None:
        # 判定词后面没有评分，再试老式的「85 yes」（分数写在前面）
        score = _score_leading(text)
    return True, score


def _score_after(text: str, start: int) -> int | None:
    """在判定词之后找评分：``yes 85`` → 85，``Yes, it is relevant. 70`` → 70。

    从判定词后面**往后找第一个像评分的整数**，而不是要求它紧挨着判定词 ——
    模型很爱在 yes 和分数之间插一句解释（``Yes, it is relevant. 70``）。
    """
    for match in _SCORE_CANDIDATE_RE.finditer(text, start):
        value = int(match.group(1))
        if 0 <= value <= 100:
            return value
    return None


def _score_leading(text: str) -> int | None:
    """分数写在判定词前面的老式输出：「85 yes」。"""
    match = _SCORE_LEADING_RE.match(text)
    if not match:
        return None
    value = int(match.group(1))
    return value if 0 <= value <= 100 else None


def _is_affirmative(answer: str) -> bool:
    """只要 yes/no 判定，忽略评分。"""
    return parse_relevance(answer)[0]


def _fallback_body(article: Article) -> str:
    """LLM 不可用时能拿来读的文本：正文全文 → RSS 摘要 → 标题。

    中间那层必须过 ``is_real_body``：Hacker News / Reddit 的 RSS description
    剥掉标签之后只剩 ``Comments``、``submitted by /u/xxx [link] [comments]``
    这类模板套话，直接拿来当摘要，页面上就是一行「推荐理由：Comments」。
    """
    for candidate in (article.content_full, article.content):
        text = strip_html(candidate) or ""
        if is_real_body(text):
            return text
    return article.title or ""


def _fallback_summary(article: Article, chars: int) -> str:
    return truncate(_fallback_body(article), chars)


def _fallback_digest(article: Article, chars: int) -> str:
    """LLM 不可用时的速览：直接取正文开头，好歹让页内能读。"""
    return truncate(_fallback_body(article), chars)


def category_hints(categories: list[Any]) -> str:
    """把分类清单渲染成给模型看的说明（含每类的判定提示）。"""
    return "\n".join(f"- {c.name}：{c.hint}" if c.hint else f"- {c.name}" for c in categories)


def parse_classify(raw: str, valid: set[str]) -> tuple[str | None, list[str]]:
    """解析分类结果：第一行分类名，第二行主题。

    容错：模型多写几行、分类名不在清单里、主题里混进分类名 —— 都不能让整条内容挂掉。
    """
    lines = [line.strip() for line in (raw or "").splitlines() if line.strip()]
    category = None
    topics: list[str] = []
    for line in lines:
        head = line.lstrip(" -•*#").strip()
        if category is None and head in valid:
            category = head
            continue
        for piece in _TAG_SPLIT_RE.split(head):
            name = piece.strip().strip("「」【】")
            if name and name not in valid and len(name) <= 20 and name not in topics:
                topics.append(name)
    return category, topics[:3]


def parse_translate(raw: str) -> tuple[str | None, str | None]:
    """解析中译英结果：第一行英文标题，第二行英文导语。"""
    lines = [line.strip() for line in (raw or "").splitlines() if line.strip()]
    if not lines:
        return None, None
    title_en = lines[0][:500] or None
    digest_en = truncate(" ".join(lines[1:]), 400) or None
    return title_en, digest_en


# 正文重写按「字符预算」切块，不按固定段数。
#
# 早先固定 4 段一译，是为**逐句直译**设计的：段落一一对应，短块不容易被截断。
# 改成「用中文重写成文章」之后这个前提反了 ——
#   · 重写要重新组织段落，4 段看不到上下文，只能各译各的，仍然是译文味；
#   · 调用次数还是长文的最大成本（一篇 28 段 = 7 次调用）。
# 改成 ~2600 字符一块：既给模型足够上下文重组行文，又把调用数降到 1~3 次。
# 分块尺寸直接对准网关的输出上限：实测当前网关单次回复被截在几百字，
# 2600 字符的大块**每次**都要「试 2 次失败 → 对半拆 → 再失败 → 再拆」，
# 烧掉 6~10 次调用才落到写得完的尺寸（文章 520 两轮 264 次就是这么来的）。
# 800 字符的块按 0.35~0.6 的压缩比产出 280~480 字，基本一次写完；
# 首次就成功，递归拆块退化成兜底而不是主路径。
TRANSLATE_CHUNK_CHARS = 800
# 单块至少要有这么多字符才单独成块（否则碎片会被并到上一块）
TRANSLATE_CHUNK_MIN_CHARS = 250
# 单块最多试几次。网关偶尔会把输出截断在半句上（实测停在「…在7」），
# 长度校验会拒掉，先重试一次（多数截断是随机的）。
TRANSLATE_CHUNK_ATTEMPTS = 2
# 写不出来的块最多再对半拆几层（见 _rewrite_chunk）。
# 实测 ag/gemini 网关对部分内容的输出会被截断在几百字：拆到 3 层
# （≈325 字符/块）仍然不够小，长文（文章 520：14.7k 字符）6 块里 5 块
# 全废、整篇判失败。放宽到 6 层（≈40 字符/块），网关的上限再低也压得进去；
# 拆得越深块越小、单次调用越快，总成本反而更低。
MAX_REWRITE_SPLIT_DEPTH = 6
# 整篇翻译的调用预算： hopeless 内容（网关持续截断）会靠递归拆块烧出
# 几百次调用（实测文章 520 两轮共 264 次），把网关和补译批次一起拖死。
# 正常文章 3~6 块、12 次以内完成，120 是 10 倍余量；超限整篇放弃，
# 交给下一轮补译 / 手动「立即重试」。
TRANSLATE_MAX_CALLS = 120
# 补译放弃线：试了这么多次还没成功的内容（网关截断类）不再自动重试 ——
# 每轮一小时的白烧会把整个补译批次拖死，还挤占其他文章的名额。
# 页面上的「立即重试翻译」不受此限制，换模型后手动点一次即可。
I18N_MAX_ATTEMPTS = 6
# 参与翻译的正文长度上限（与抓取时的上限对齐）
MAX_CONTENT_CHARS = 40_000
# 重写后的中文长度下限（占原文的比例）。
# 提示词要求压到原文的 40%~70%，所以 0.25 是「明显没写完」的兜底线；
# 完整重写实测落在 0.35~0.6。压缩成摘要的（0.1 上下）会被这条拦掉。
_TRANSLATE_MIN_RATIO = 0.25
# 中文重写的合理上限：超过原文长度说明模型在扩写/复述，不是编译
_TRANSLATE_MAX_RATIO = 1.3


def translation_is_usable(source: str, translated: str) -> bool:
    """译文到底算不算「整篇都译完了」。

    **整篇级**的校验，分块校验代替不了。分块只看「这一块像不像写完」，
    一篇长文里哪怕只有一小块被翻出来、其余整段丢掉，拼起来的总长仍然可能
    看着像模像样 —— 实测文章 313：英文正文 1249 字，``content_zh`` 只存了
    64 字（翻译了开头一段就收工，比例 0.05），页面照样认为「有中文译文」，
    于是双语模式下正文只剩一个中文段落，英文却是完整六段，看起来像没译完。

    展示层也用这个判断，这样库里已经写坏的老数据同样会被当成「暂无译文」，
    而不是继续把残篇当译文端上去。
    """
    src = (source or "").strip()
    out = (translated or "").strip()
    if not out:
        return False
    if len(src) < _TRANSLATE_MIN_SRC_CHARS:
        # 原文太短，比例没意义（一句英文标题也占不到 0.25）
        return True
    ratio = len(out) / len(src)
    return _TRANSLATE_MIN_RATIO <= ratio <= _TRANSLATE_MAX_RATIO


# 原文短于这个长度就不做比例校验
_TRANSLATE_MIN_SRC_CHARS = 400


def split_paragraphs(text: str) -> list[str]:
    """按空行切段。抓回来的正文本身就是段落结构，直接切即可。"""
    return [block.strip() for block in re.split(r"\n\s*\n", text or "") if block.strip()]


def parse_title_zh(raw: str) -> str | None:
    """解析中译标题：只取第一行，去掉引号和编号。"""
    line = (raw or "").strip().splitlines()[0].strip() if (raw or "").strip() else ""
    line = line.strip("「」『』\"'\u201c\u201d《》").lstrip("#*-— ").strip()
    return line[:500] or None


def translate_title_to_chinese(client: LLMClient, prompts: PromptsConfig, title: str) -> str | None:
    """标题译成中文。已经有汉字或模板为空时返回 ``None``（不浪费调用）。

    这里用「有没有汉字」判断，不能用 ``looks_english``：它要求 40 个字母以上，
    ``The dawn of the age of the exoskeleton`` 这种短标题会被判成「不是英文」，
    中文标题就永远翻不出来。
    """
    if not title or is_chinese_text(title):
        return None
    raw = _optional_llm(client, render_translate_title_zh_prompt(prompts, title), "")
    return parse_title_zh(raw)


def translate_digest_to_chinese(client: LLMClient, prompts: PromptsConfig, digest: str) -> str | None:
    """英文速览（AI 导读）译成中文。中文导读、模板为空、或模型原样吐回英文时返回 ``None``。

    模型偶尔会在译文前面加一行小标题，先去掉；剩下的原样保留 —— 导读本来
    就只有两三句，压缩或合并只会把信息压掉。
    """
    text = (digest or "").strip()
    if not text or is_chinese_text(text):
        return None
    raw = _optional_llm(client, render_translate_digest_zh_prompt(prompts, text), "").strip()
    if not raw:
        return None
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) > 1 and len(lines[0]) <= 20 and lines[0][-1] not in "。！？.!?：:":
        lines = lines[1:]
    result = strip_markdown(" ".join(lines))
    # 拿回一段英文等于没翻，别把它当成功写进库里（写进去就再也不会重试了）
    return None if not result or looks_english(result) else result


def generate_brief_zh(
    client: LLMClient, prompts: PromptsConfig, title: str, digest: str
) -> str | None:
    """写早报推送语：一段通顺的话，不是截断拼凑。

    输入用中文标题 + 中文导读（英文信源调这个函数时先保证两者已是中文，
    调用方负责传对）。失败返回 ``None``，展示层回退旧的截断逻辑。
    """
    text = (digest or "").strip()
    if not text:
        return None
    raw = _optional_llm(client, render_brief_prompt(prompts, title or "", text), "").strip()
    if not raw:
        return None
    result = strip_markdown(" ".join(line.strip() for line in raw.splitlines() if line.strip()))
    # 推送语是中文手机推送：没有汉字等于没写（短英文会绕过 looks_english 的
    # 40 字母门槛，写进库就再也不会重试了）
    if not result or not is_chinese_text(result):
        return None
    # 必须是完整的一句话：以句末标点收尾。
    # 实测踩过：模型偶尔只回半句就停了，直接入库就推成了
    # 「…Only $30 more than the wireless charging ver…」这种半句话。
    if result[-1] not in "。！？.!?":
        return None
    # 也不能夹一大段没翻的英文（读者在手机上看到会以为坏掉了）
    if has_long_latin_run(result):
        return None
    # 超预算就整条作废，**不要截断**。上面刚确认过它是完整一句，
    # truncate(result, 200) 会切在半句上再补一个「…」，把「完整」这个
    # 唯一的保证又弄没了 —— 而且入库后就再也不会重试，读者收到的推送语
    # 就永远停在这个省略号上。
    if len(result) > 200:
        log.info("推送语 %d 字超出 200 字上限，整条不采用（宁可没有也不要半句）", len(result))
        return None
    return result


# 章节最多分这么多节：再多就不是「排版」，而是把文章切碎了
MAX_SECTIONS = 6
# 结构化后的文字量不能比原文少太多，否则模型一定是改写/省略了，直接丢掉
_STRUCTURE_MIN_RATIO = 0.8


def parse_sections(raw: str) -> list[dict[str, str]] | None:
    """解析章节排版结果，返回 ``[{"h_zh","h_en","t"}]``；不合格返回 ``None``。

    期望 JSON：``[{"h_zh": "...", "h_en": "...", "paras": ["...", "..."]}]``。
    同时兼容旧的 ``## 小标题`` 纯文本格式（那时 ``h_en`` 留空，调用方会把
    ``h_zh`` 当英文标题用不到 —— 只在有英文原文时才需要）。

    一次给出两个语言的小标题，是为了让双语视图**节对节对齐**。以前只给一个
    标题，于是英文原文侧没有章节结构（实测 337 篇里只有 3 篇有 body_sections，
    而 body_sections_zh 有 30 篇）—— 双语模式下中文那一半有小标题横幅、英文
    那一半没有，读者看到的就是「有些段落有、有些没有」。
    """
    text = (raw or "").strip()
    if not text:
        return None
    sections = _sections_from_json(text)
    if sections is None:
        sections = _sections_from_markdown(text)
    if not sections:
        return None
    if len(sections) > MAX_SECTIONS + 2:
        return None
    if len(sections) == 1 and not sections[0]["h_zh"]:
        return None  # 等于没分，不占存储
    return sections


def _sections_from_json(text: str) -> list[dict[str, str]] | None:
    body = text
    if body.startswith("```"):
        body = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", body).strip()
    start, end = body.find("["), body.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(body[start:end + 1])
    except ValueError:
        return None
    if not isinstance(parsed, list):
        return None
    out: list[dict[str, str]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        paras = item.get("paras")
        if isinstance(paras, str):
            paras = [paras]
        if not isinstance(paras, list):
            continue
        kept = [strip_markdown(str(p)).strip() for p in paras if str(p).strip()]
        kept = [p for p in kept if p]
        if not kept:
            continue
        out.append({
            "h_zh": str(item.get("h_zh") or "").strip()[:40],
            "h_en": str(item.get("h_en") or "").strip()[:80],
            "t": "\n\n".join(kept),
        })
    return out or None


def _sections_from_markdown(text: str) -> list[dict[str, str]] | None:
    """兼容旧配置/旧输出的 ``## 小标题`` 格式。

    这种格式只有一个标题，双语视图没法用它对齐，所以 ``h_en`` 留空 ——
    调用方看到 ``h_en`` 为空就知道英文侧只能退化成整篇平铺。
    """
    headings: list[str] = []
    buckets: list[list[str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("##"):
            headings.append(line.lstrip("#").strip().strip("：:* ").strip()[:30])
            buckets.append([])
            continue
        clean = strip_markdown(line)
        if clean:
            if not buckets:
                headings.append("")
                buckets.append([])
            buckets[-1].append(clean)
    pairs = [(h, p) for h, p in zip(headings, buckets, strict=True) if p]
    if not pairs:
        return None
    return [{"h_zh": h, "h_en": "", "t": "\n\n".join(p)} for h, p in pairs]


def section_pair(
    sections: list[dict[str, str]], *, chinese: bool
) -> list[dict[str, str]] | None:
    """章节结构落库前的最后一道闸：双语两侧都要有小标题。

    只要有一节的 ``h_en`` 是空的，中英对照就会缺一段结构 —— 与其存一个
    必然不一致的数据，不如整篇退回「原文直排」，页面显示的是一段没有层次
    的正文，但至少两个语言版本是对称的、不会互相错位。
    """
    usable = [s for s in sections if s["t"].strip()]
    if not usable:
        return None
    needs_en = any(not s["h_en"].strip() for s in usable)
    if chinese:
        # 中文原文的文章没有英文侧，只需要中文标题
        return [{"h": s["h_zh"], "t": s["t"]} for s in usable]
    if needs_en:
        return None
    return [{"h": s["h_en"], "t": s["t"]} for s in usable]


# 排版时一次喂给模型的正文上限。
#
# 早先是把**整篇**丢进去让模型照放，实测长文必然被截断：文章 289 原文 20689 字
# / 47 段，模型只回得出 7993 字（占 38.8%），而「丢字超过两成就丢弃」那道闸
# 于是把整篇排版结果扔掉 —— 结果是长文**一个横幅都没有**（实测 337 篇里
# body_sections 只有 3 篇），也正是双语模式「中文有横幅、英文没有」的根源。
#
# 改成按预算分块、逐块排版、结果拼起来。每一块各自校验丢字率，任何一块不合格
# 就整篇放弃（宁可没有横幅，也不能让正文少一段）。
SECTION_BUDGET_CHARS = 2400


def _section_chunks(paragraphs: list[str], budget: int) -> list[list[str]]:
    """把段落按预算切成若干**段落组**（每组仍是段落列表）。

    注意不能直接复用 ``chunk_for_rewrite``：它返回的是「拼好的字符串列表」，
    不是段落列表 —— 拿它的结果再 ``"\n\n".join()`` 会把**每个字符**之间都插上
    换行，模型收到的是彻底错乱的正文。
    """
    groups: list[list[str]] = []
    current: list[str] = []
    size = 0
    for para in paragraphs:
        current.append(para)
        size += len(para) + 2
        if size >= budget:
            groups.append(current)
            current, size = [], 0
    if current:
        groups.append(current)
    return groups or [paragraphs]


def structure_sections(
    client: LLMClient, prompts: PromptsConfig, title: str, body: str
) -> list[dict[str, str]] | None:
    """NYT 责任编辑视角：只分段加**双语**小标题，不改写。

    太短（3 段以内）不值得分；任何一块结构化丢字超两成就整篇放弃。
    """
    paras = split_paragraphs(body)
    if len(paras) <= 3:
        return None
    collected: list[dict[str, str]] = []
    for group in _section_chunks(paras, SECTION_BUDGET_CHARS):
        chunk_body = "\n\n".join(group)
        raw = _optional_llm(
            client, render_structure_prompt(prompts, title, chunk_body), ""
        )
        if not raw.strip():
            return None
        sections = parse_sections(raw)
        if not sections:
            return None
        kept = sum(len(s["t"]) for s in sections)
        total = sum(len(p) for p in group)
        if total <= 0 or kept < total * _STRUCTURE_MIN_RATIO:
            return None
        collected.extend(sections)
    if not collected:
        return None
    # 分块必然产生很多小节（实测一篇 2 万字的长文被切成 8 块、产出 28 节），
    # 而页面上一篇文章挂 28 个横幅没人读得下去。超出的并进最后一节：
    # **正文一个字都不丢**（这是硬要求），只是标题少了几个。
    if len(collected) > MAX_SECTIONS:
        head = collected[: MAX_SECTIONS - 1]
        tail = collected[MAX_SECTIONS - 1:]
        merged = dict(tail[0])
        merged["t"] = "\n\n".join(s["t"] for s in tail if s["t"].strip())
        if not merged["h_zh"] and tail[0].get("h_zh"):
            merged["h_zh"] = tail[0]["h_zh"]
        collected = [*head, merged]
    return collected


def chunk_for_rewrite(paragraphs: list[str], *, budget: int = TRANSLATE_CHUNK_CHARS) -> list[str]:
    """把段落按字符预算分块，尽量在段落边界断开。

    不足 ``TRANSLATE_CHUNK_MIN_CHARS`` 的尾块并到上一块：单独发一个 50 字符的块
    既浪费一次调用，模型也没有上下文可用。
    """
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for para in paragraphs:
        current.append(para)
        size += len(para) + 2
        if size >= budget:
            chunks.append("\n\n".join(current))
            current, size = [], 0
    if current:
        tail = "\n\n".join(current)
        if chunks and size < TRANSLATE_CHUNK_MIN_CHARS:
            chunks[-1] = chunks[-1] + "\n\n" + tail
        else:
            chunks.append(tail)
    return chunks


def _halve_by_sentence(text: str) -> list[str]:
    """没有空行的整段也能对半拆：按句子边界切成大致等长的两半。

    为什么要它：``_rewrite_chunk`` 的自适应拆块原来只按 ``\\n\\n`` 分段，
    遇到单个长段落就 ``len(parts) < 2`` 直接放弃 —— 这一块永远翻不出来，
    整篇跟着判废。按句子对半，递归总能把块压到网关输出上限之内。
    """
    sentences = re.findall(r"[^.!?。！？]+[.!?。！？]+[\"')]?\s*|[^.!?。！？]+$", text)
    if len(sentences) < 2:
        return [text]
    mid = len(text) // 2
    acc = 0
    cut = 1
    for i, piece in enumerate(sentences[:-1]):
        acc += len(piece)
        if acc >= mid:
            cut = i + 1
            break
    left = "".join(sentences[:cut]).strip()
    right = "".join(sentences[cut:]).strip()
    return [left, right] if left and right else [text]


def _rewrite_chunk(
    client: LLMClient, prompts: PromptsConfig, chunk: str, *,
    depth: int = 0, budget: dict[str, int] | None = None,
) -> tuple[str, bool]:
    """把一块重写成中文；返回 ``(译文, 是否可用)``。

    ``budget`` 是跨递归共享的调用预算（``{"left": n}``）：递归拆块遇到
    hopeless 内容时叶子数是指数级的（2^depth），没有预算就会烧出几百次
    调用（实测文章 520 两轮 264 次）。预算耗尽直接判失败，交给上层
    按「整篇或没有」处理。

    会**自适应拆块**：网关对某些块的输出会莫名其妙被截断（实测 183 的第二块
    稳定停在 343 字、半个数字上，重试三次结果一样 —— 不是随机抖动，是这块本身
    触到了上游的输出上限）。单纯重试没用，只有把这块**对半拆开**再试才写得完。

    拆到 ``MAX_REWRITE_SPLIT_DEPTH`` 层还写不出来就放弃（返回空），
    由调用方按「整篇或没有」处理。
    """
    if budget is not None and budget["left"] <= 0:
        return "", False
    translated = ""
    for _ in range(TRANSLATE_CHUNK_ATTEMPTS):
        if budget is not None:
            if budget["left"] <= 0:
                return "", False
            budget["left"] -= 1
        translated = _optional_llm(
            client, render_translate_content_prompt(prompts, chunk), ""
        ).strip()
        ratio = len(translated) / len(chunk) if chunk else 0.0
        # 太短=被截断/压成摘要，太长=在扩写复述；两种都不该写进库
        if translated and _TRANSLATE_MIN_RATIO <= ratio <= _TRANSLATE_MAX_RATIO:
            return translated, True
        translated = ""

    parts = chunk.split("\n\n")
    if depth >= MAX_REWRITE_SPLIT_DEPTH:
        return "", False
    if len(parts) < 2:
        # 整段没有空行（散文/说明文常见）：按句子对半，别在这里卡死
        parts = _halve_by_sentence(chunk)
        if len(parts) < 2:
            return "", False
    middle = len(parts) // 2
    pieces = ["\n\n".join(parts[:middle]), "\n\n".join(parts[middle:])]
    done: list[str] = []
    for piece in pieces:
        text, ok = _rewrite_chunk(client, prompts, piece, depth=depth + 1, budget=budget)
        if not ok:
            return "", False
        done.append(text)
    return "\n\n".join(done), True


def translate_to_chinese(
    client: LLMClient,
    prompts: PromptsConfig,
    content: str,
    *,
    max_chars: int | None = None,
) -> str | None:
    """把英文原文正文整篇译成中文。

    按段分组调用，逐组拼回去；某一组失败只丢那一组，不影响其它段
    （长文很难一次成功，丢掉一段比整篇没有译文好）。
    全程都是英文就原样返回，不浪费调用。
    """
    text = (content or "").strip()
    if not text or not looks_english(text):
        return None
    limit = max_chars or MAX_CONTENT_CHARS
    paragraphs = split_paragraphs(text)[: max(1, limit // 200)]
    if not paragraphs:
        return None
    chunks = chunk_for_rewrite(paragraphs)
    out: list[str] = []
    failed = 0
    budget = {"left": TRANSLATE_MAX_CALLS}
    for chunk in chunks:
        translated, ok = _rewrite_chunk(client, prompts, chunk, budget=budget)
        if ok:
            out.append(translated)
        else:
            failed += 1
        if budget["left"] <= 0:
            log.info("翻译调用预算（%d 次）耗尽，剩余 %d 块放弃", TRANSLATE_MAX_CALLS,
                     len(chunks) - len(out) - failed)
            return None
    if not out:
        return None
    if failed:
        # 全有或全无。早先这里是「丢掉失败的块、保留成功的」，于是文章 259 会出现
        # 只有最后三分之一的中文正文 —— 开头直接断掉，读者看到的是一段残篇，
        # 而页面完全看不出它不完整（实测压缩比 0.09）。
        # 残篇比没有译文更糟：没有译文时页面会明说「原文为英文，暂无中文译文」。
        log.info("正文重写有 %d/%d 块未成功，整篇不采用（宁可显示原文也不要残篇）",
                 failed, len(chunks))
        return None
    joined = "\n\n".join(out)
    # 分块都过了还不够：每块各自「像写完了」，拼起来也可能只覆盖了全文一小半
    # （文章 313 实测 1249 字原文只译出 64 字）。这里再按**整篇**的比例兜一道。
    if not translation_is_usable(text, joined):
        log.info("正文重写总长 %d 对原文 %d 比例过低，整篇不采用",
                 len(joined), len(text))
        return None
    return joined


def split_digest(raw: str) -> tuple[str, str]:
    """把导语结果拆成 ``(中文, 英文)``。

    提示词要求一次输出两行（第一行中文、第二行 ``EN: `` 前缀的英文），
    这样英文信源的「英文」模式下 AI 导读也是英文 —— 以前那里直接顶着
    中文摘要，读者看到的是「英文文章配中文导读」。

    模型不听指令（只回一行、或者把 EN: 写在第一行）时退化成「没有英文行」，
    展示层会如实显示，而不是把中文当英文端出去。
    """
    text = (raw or "").strip()
    if not text:
        return "", ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    en_parts: list[str] = []
    zh_parts: list[str] = []
    for line in lines:
        stripped = re.sub(r"^EN\s*[:：]\s*", "", line, flags=re.I).strip()
        if stripped != line or line.lower().startswith("en"):
            en_parts.append(stripped)
        else:
            zh_parts.append(line)
    return strip_markdown(" ".join(zh_parts)), strip_markdown(" ".join(en_parts))


def _optional_llm(client: LLMClient, prompt: str, fallback: str) -> str:
    """跑一个「锦上添花」的 LLM 步骤；失败就用 fallback，不影响主流程。"""
    if not prompt:
        return fallback
    try:
        return client.chat(prompt)
    except LLMError as exc:
        log.warning("可选步骤失败，已降级（%s）", exc)
        return fallback


def body_for_lang(article: Article) -> str:
    """用来判语言的正文：优先抓回来的全文，没有就退回摘要。

    刻意不看 ``content``（RSS 摘要）：有的源摘要写的是英文标题，混进来会
    把中文正文判成「需要翻译」，于是中文站的文章也被翻一遍。
    """
    return article.content_full or article.content or article.summary or ""


def resolves_native_zh(article: Article, source_lang: str = LANG_AUTO) -> bool:
    """这篇要不要「只留中文、不产出英文版」。

    auto（默认）：按**这篇自己的正文**判定。同一个源里中英文混排很常见，
    按站点一刀切会把中文文章也翻一遍。
    显式 zh：只代表「这个源以中文为主」，**不代表里面每一篇都是中文** ——
    中文站转载英文原文并不少见。按用户的明确要求（英文文章必须出中文版），
    内容实际是英文的仍要走翻译；zh 的语义收窄为「不产出英文版那套字段」。
    显式 en：永远当英文处理。
    """
    forced = resolve_lang(source_lang)
    if forced == LANG_EN:
        return False
    return not article_is_foreign(article.title or "", body_for_lang(article))


def process_article(
    session: Session,
    article: Article,
    client: LLMClient,
    settings: Settings,
    *,
    source_lang: str = LANG_AUTO,
) -> str:
    """处理单篇文章，返回最终 status（processed / failed）。

    每次调用都记一次尝试：上游 LLM 限流会在第一个调用就抛异常，整篇降级成英文，
    必须留下「试过几次、什么时候试的」才能在配额恢复后回来重试（见 ``retry_degraded``）。

    ``source_lang`` 是信源上配的语言（auto/en/zh）。``auto`` 时按**这篇自己的
    正文**判定：中文正文不产出英文版，英文正文必须有中文版。
    """
    article.process_attempts = (article.process_attempts or 0) + 1
    article.process_last_at = now_local()
    topic = settings.research_topic
    # 优先用抓回来的正文做判断，摘要质量直接决定筛选与写作的质量
    body = strip_html(article.content_full) or strip_html(article.content)
    excerpt = truncate(body, 2000)

    try:
        answer = client.chat(render_relevance_prompt(settings.prompts, topic, article.title, excerpt))
        relevant, score = parse_relevance(answer)
        if not relevant:
            article.relevance = 0
            # 判为不相关就**不要记分数**。模型回 "no 95" 时，留下 score=95 会
            # 造出一行 relevance=0 却 score=95 的自相矛盾数据：页面上显示
            # 「AI 评分 95」，而任何只按 score 排序或筛选的下游都会把一篇已经
            # 不进日报的文章当成高价值内容。与其指望每个调用方都记得同时看
            # relevance，不如在写入时就消掉这个矛盾。
            article.score = None
            article.summary = None
            article.digest = None
            article.reason = None
            article.category = None
            article.topics = None
            article.tags = None
            # 不相关的也要存中文标题：详情页直接链接照样可访问，
            # 中文模式顶着英文标题看着像没处理完。标题 1 次调用，
            # 正文/导读不翻（不进日报，省成本），缺译文页面会如实说明。
            if settings.i18n.enabled:
                article.title_zh = translate_title_to_chinese(
                    client, settings.prompts, article.title or ""
                )
                article.digest_zh = ""
            article.status = STATUS_PROCESSED
            return STATUS_PROCESSED

        article.relevance = 1
        article.score = score

        # 「中文原生」要在这里就定下来：速览提示词据此决定要不要顺带产出一行
        # EN 英文。先出速览、再判语言的话，中文文章的英文行既白花 token 又只能
        # 丢掉 —— 用户要的是「中文原文不做英文翻译」。
        native_zh = resolves_native_zh(article, source_lang)

        summary = client.chat(render_summary_prompt(settings.prompts, topic, article.title, excerpt))
        article.summary = truncate(summary, 500)

        # 速览：让读者在页内读完，不用跳原站
        digest = _optional_llm(
            client,
            render_digest_prompt(
                settings.prompts, article.title, summary, excerpt, bilingual=not native_zh
            ),
            _fallback_digest(article, settings.prompts.fallback_digest_chars),
        )
        article.digest, digest_en = split_digest(digest)
        article.digest = truncate(article.digest, 400)
        if digest_en and not native_zh:
            article.digest_en = truncate(digest_en, 400)

        # 推荐理由：回答「为什么要点开这条」
        reason = _optional_llm(
            client,
            render_reason_prompt(settings.prompts, topic, article.title, summary),
            _fallback_digest(article, settings.prompts.fallback_reason_chars),
        )
        article.reason = truncate(reason, 200)

        # 分类 + 主题：一次调用同时产出，失败只丢这两个字段
        if settings.categories:
            raw_classify = _optional_llm(
                client,
                render_classify_prompt(
                    settings.prompts,
                    topic,
                    article.title,
                    summary,
                    category_hints(settings.categories),
                ),
                "",
            )
            category, topics = parse_classify(raw_classify, {c.name for c in settings.categories})
            article.category = category
            article.topics = json.dumps(topics, ensure_ascii=False) if topics else None

        # 中文原生文章**不做英文翻译**。
        # 中文译中文既浪费调用，又把「AI 重写」的质量损耗套到本来不需要改写的
        # 原文上；页面上也不该出现 EN / 双语按钮（见 routes.story 的 langs）。
        #
        # 关键：这里**不能提前 return**。早先的写法在这里
        # `return STATUS_PROCESSED`，于是下面两段（标签、早报推送语）永远轮不到，
        # 中文文章被标成「处理完成」却 tags / brief 全空。改成 if/else 才能让
        # 「不做英文翻译」只关掉翻译那几步，不关掉整篇文章的处理。
        if native_zh:
            article.title_en = None
            article.digest_en = None
            article.title_zh = article.title_zh or article.title
            article.digest_zh = article.digest_zh or ""
            article.content_zh = ""
            if not article.body_sections_zh:
                sections = structure_sections(
                    client, settings.prompts, article.title or "",
                    article.content_full or article.content or "",
                )
                zh_sections = section_pair(sections, chinese=True) if sections else None
                if zh_sections is not None:
                    article.body_sections_zh = json.dumps(zh_sections, ensure_ascii=False)
        else:
            # 中英双语。标题和导语分别判断：英文信源常见「英文标题 + 中文导语」，
            # 整段一起判断会漏掉该翻的导语，也会给纯英文标题翻出一份一模一样的自己。
            if settings.i18n.enabled:
                # 判「要不要翻」只看有没有汉字，与长短无关：
                # 短英文标题会被 looks_english 判成「不是英文」，于是白白翻一次英文→英文
                title_needs = is_chinese_text(article.title)
                # 英文导读的来源，按优先级：
                #   1. 导语那次调用顺带产出的 "EN: " 行 —— 一次调用两种语言，最省；
                #   2. 都没有才单独翻一次中文摘要。
                # **绝不能拿中文摘要直接顶上去**：那样英文模式下的 AI 导读会写着
                # 中文，读者看到的是「英文文章配中文导读」。
                # 早先的写法就是 `digest_en = truncate(summary, 400)`，而 summary
                # 永远是中文 —— 实测 22 篇英文相关报道里 13 篇 digest_en 是空的，
                # 剩下的也是中文。
                article.digest_en = digest_en or None
                if title_needs or article.digest_en is None:
                    raw_en = _optional_llm(
                        client,
                        render_translate_prompt(settings.prompts, article.title, summary),
                        "",
                    )
                    en_title, en_digest = parse_translate(raw_en)
                    if title_needs and en_title:
                        article.title_en = en_title
                    if article.digest_en is None and en_digest:
                        article.digest_en = en_digest
                if not title_needs:
                    # 整篇本来就是英文，标题直接复用，不必再花一次调用
                    article.title_en = article.title
                # 英文信源补一套中文版：标题、速览、正文一个都不能少。
                # 少了任何一样，「中文」模式下就会在标题 / 导读 / 正文其中一处
                # 露出英文，看起来像没处理完。
                if not title_needs:
                    article.title_zh = translate_title_to_chinese(client, settings.prompts, article.title)
                if not is_chinese_text(article.digest or ""):
                    article.digest_zh = translate_digest_to_chinese(
                        client, settings.prompts, article.digest or ""
                    )
                else:
                    # 本来就是中文，标成已处理，补译那一轮就不会再来扫它
                    article.digest_zh = article.digest_zh or ""
                # 英文原文正文整篇译成中文，中文模式下才读得到中文
                body_text = article.content_full or article.content or ""
                foreign = looks_english(body_text)
                sections = structure_sections(client, settings.prompts, article.title or "", body_text)
                # 章节结构一次拿到中英两套小标题，于是 body_sections（英文侧）与
                # body_sections_zh（中文侧）**节数与顺序天然一一对应** —— 双语视图
                # 逐节对照不会错位。以前只存一套，英文侧大面积缺失（337 篇里只有
                # 3 篇有），双语模式下中文那一半有小标题、英文那一半没有。
                en_sections = section_pair(sections, chinese=False) if (sections and foreign) else None
                zh_sections = section_pair(sections, chinese=True) if sections else None
                if en_sections is not None:
                    article.body_sections = json.dumps(en_sections, ensure_ascii=False)
                # ★ 中文侧结构**不能在这里预落库**：section_pair(chinese=True) 的
                #   t 还是英文原文，半中半英的结构会被页面当「中文译文」渲染
                #   （实测 29 篇中招：中文标题 + 英文段落，读者看到的就是
                #   「中文的文章内容是英文」）。t 变成终稿的位置只有两处：
                #   逐节翻译成功后的覆盖写回，和下面 not foreign 分支的直接落库。
                if settings.i18n.translate_content and foreign:
                    if en_sections is not None and zh_sections is not None:
                        # 按节翻：节与节之间天然对齐，中英文对照不会错位；
                        # 某一节失败就整篇回退整翻，不留半中半英
                        translated: list[dict[str, str]] = []
                        failed = False
                        for zh_sec, en_sec in zip(zh_sections, en_sections, strict=True):
                            one = translate_to_chinese(client, settings.prompts, en_sec["t"])
                            if not one:
                                failed = True
                                break
                            translated.append({"h": zh_sec["h"], "t": one})
                        if not failed:
                            article.body_sections_zh = json.dumps(translated, ensure_ascii=False)
                            article.content_zh = "\n\n".join(s["t"] for s in translated)
                        else:
                            article.content_zh = translate_to_chinese(
                                client, settings.prompts, body_text
                            )
                    else:
                        article.content_zh = translate_to_chinese(
                            client, settings.prompts, body_text
                        )
                elif zh_sections is not None and not foreign:
                    # 正文不是英文（或没开翻译）才直接落库：此时 t 本来就是中文终稿。
                    # 英文信源没开翻译时不写 —— 让页面老实显示原文并注明「暂无中文
                    # 译文」，而不是把英文段落标成「中文译文」。
                    article.body_sections_zh = json.dumps(zh_sections, ensure_ascii=False)

        tags = client.chat(render_tag_prompt(settings.prompts, article.title, summary))
        article.tags = ",".join(split_tags(tags)) or None
        # 早报推送语：用中文标题 + 中文导读现写一段，手机上直接读。
        # 失败就留空，展示层回退旧的截断拼凑 —— 推送语是锦上添花，
        # 不能因为它让整篇处理多一次失败点（_optional_llm 本来就吞异常）。
        article.brief_zh = generate_brief_zh(
            client,
            settings.prompts,
            article.title_zh or article.title or "",
            strip_markdown(article.digest_zh or article.digest or ""),
        )
        article.status = STATUS_PROCESSED
        article.degraded_reason = None
        return STATUS_PROCESSED
    except LLMError as exc:
        article.degraded_reason = f"LLM 不可用：{exc}"[:300]
        article.summary = _fallback_summary(article, settings.prompts.fallback_summary_chars)
        if article.digest is None:
            article.digest = _fallback_digest(article, settings.prompts.fallback_digest_chars)
        if article.reason is None:
            article.reason = truncate(article.summary, settings.prompts.fallback_reason_chars)
        article.tags = None
        if article.relevance is None:
            article.relevance = 1  # 相关度未知时先算相关，避免漏掉当天内容
        article.status = STATUS_FAILED
        log.warning("文章处理失败，已降级：%s（%s）", article.title[:60], exc)
        return STATUS_FAILED


def process_pending(
    session: Session,
    client: LLMClient,
    settings: Settings,
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    """批量处理 status=pending 的文章。

    单篇出错只标记这一篇并继续 —— 否则一篇坏数据就能让整批卡在 pending，
    下一个周期又重来一遍。每处理 ``checkpoint_every`` 篇提交一次，
    避免长任务中途失败把已完成的进度一起回滚。
    """
    # id 兜底排序：published_at 真的会撞车（同一分钟入的多篇、或者来源没给
    # 时间的条目共用同一个 now 值到微秒）。没有稳定次序时 limit=60 每次挑中的
    # 集合都可能不一样，靠后的条目会被反复饿死。
    statement = (
        select(Article)
        .where(Article.status == "pending")
        .order_by(Article.published_at.desc(), Article.id.desc())
    )
    if limit is not None:
        statement = statement.limit(limit)
    articles = list(session.execute(statement).scalars())

    # 信源上配的语言（auto/en/zh）。**必须真的传给 process_article**：
    # 以前这个参数只有默认值 auto，用户在信源页选的「中文/英文」在
    # 处理链里完全不生效 —— 页面上写着语言，产出还是按 auto 走。
    langs = dict(session.execute(select(Source.id, Source.lang)).all())

    stats = {"pending": len(articles), "processed": 0, "irrelevant": 0, "failed": 0, "crashed": 0}
    checkpoint = max(1, settings.ai.batch_checkpoint_every)
    for index, article in enumerate(articles, start=1):
        previous_relevance = article.relevance
        try:
            status = process_article(
                session,
                article,
                client,
                settings,
                source_lang=(langs.get(article.source_id) if article.source_id else None) or LANG_AUTO,
            )
        except Exception as exc:  # 兜底：任何意外都不该中断整批
            article.status = STATUS_FAILED
            if article.summary is None:
                article.summary = _fallback_summary(article, settings.prompts.fallback_summary_chars)
            if article.digest is None:
                article.digest = _fallback_digest(article, settings.prompts.fallback_digest_chars)
            if article.reason is None:
                article.reason = truncate(article.summary, settings.prompts.fallback_reason_chars)
            if article.relevance is None:
                article.relevance = 1
            stats["crashed"] += 1
            log.error("处理文章时出现未预期异常，已跳过：%s（%r）", article.title[:60], exc)
            status = STATUS_FAILED
        if status == STATUS_FAILED:
            stats["failed"] += 1
        elif article.relevance == 0 and previous_relevance != 0:
            stats["irrelevant"] += 1
        else:
            stats["processed"] += 1
        if index % checkpoint == 0:
            # 必须 commit 而不是 flush：flush 只是把 SQL 发出去，**写事务还开着**。
            # 下一篇又要等一次慢速 LLM 调用（几十秒），整个这段时间 SQLite 的写锁
            # 一直被占着，抓取任务与网页请求全部 "database is locked"。
            # 每篇的状态互相独立，中途提交不会产生半截数据。
            session.commit()
    session.flush()
    return stats

def backfill_translations(
    session: Session,
    client: LLMClient,
    settings: Settings,
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    """把中英双版本补齐：中文标题、中文速览、中文正文，一个都不能缺。

    为什么需要单独一轮：翻译是可选步骤，失败（限流、网络抖动）时只丢译文，
    文章本身照样处理完 —— 于是 ``title_zh`` / ``digest_zh`` / ``content_zh``
    就一直是空，「中文」模式下又在标题、导读、正文里露出英文，而且再也没人
    回来补。这里定期扫一遍把它们填上。

    五个坑，都是实测踩出来的：

    1. **中文原文要当场把三个字段都标成已处理。** 只写 ``content_zh=""`` 的话，
       ``title_zh`` / ``digest_zh`` 还是 NULL，这篇每轮都会被重新捞回来，白占名额。
    2. **重试次数少的优先。** 一旦上游限流，最新的那几篇会一直失败；
       按发布时间倒序取前 N 篇的话，它们会把名额占满，排在后面的永远轮不到。
    3. **不拿 title_en 当条件。** 处理早期失败的文章压根没轮到写这个字段，
       拿它当条件会让这类文章永远等不到中文标题。
    4. **先补标题与导读，再翻正文。** 正文是长文、一次要翻好几段，单篇成本是
       标题的十几倍；而标题与导读是首页和详情页最显眼的地方。先把便宜的补齐，
       读者先看到全中文，再慢慢补正文。
    5. **整批一个事务 = 写锁被握几个小时。** 每篇的翻译要几分钟，二十篇一批
       就是几个小时的开着写事务 —— 抓取/处理/网页的写入全部 "database is
       locked"（实测 26 次、5 轮抓取全挂、当天首页空了一上午）。所以改成
       **逐篇三段式**：短事务记尝试次数并提交 → 事务外做 LLM 翻译 →
       短事务写回。写锁只盖住毫秒级的写入，翻译期间别人随便写。
    """
    empty = {"candidates": 0, "titles": 0, "digests": 0, "contents": 0, "skipped": 0}
    if not settings.i18n.enabled:
        return dict(empty)
    wants_content = settings.i18n.translate_content
    batch = limit if limit is not None else settings.i18n.backfill_batch_size
    # 短事务：只取候选 id（同一种内容反复失败到上限就放它过 —— 每轮一小时
    # 的白烧会把整个批次和其他文章一起拖死；「立即重试」不受此限）。
    ids = list(
        session.execute(
            select(Article.id)
            .where(
                # 中文标题 / 中文速览缺一个都补；正文译文按开关决定补不补
                Article.digest_zh.is_(None)
                | Article.title_zh.is_(None)
                | (Article.content_zh.is_(None) & wants_content),
                Article.content_full.isnot(None),
                # 不相关的也要存中文标题（详情页直接链接可访问），
                # 但只补标题：导读本来就没有、正文不进日报，省调用。
                or_(
                    Article.relevance == 1,
                    and_(Article.relevance == 0, Article.title_zh.is_(None)),
                ),
                Article.i18n_attempts < I18N_MAX_ATTEMPTS,
                Article.link.notlike("http://localhost%"),
            )
            .order_by(Article.i18n_attempts.asc(), Article.published_at.desc(), Article.id.desc())
            .limit(batch)
        ).scalars()
    )
    stats = {"candidates": len(ids), "titles": 0, "digests": 0, "contents": 0, "skipped": 0}

    for article_id in ids:
        # ── 短事务 A：读字段、记尝试次数，立刻提交（锁几毫秒）─────────
        article = session.get(Article, article_id)
        if article is None:
            continue
        # 与 process_article 用同一个判定（含信源上的显式语言设置），
        # 否则会出现「处理时翻了、补译时又当成中文原文标成已处理」的矛盾状态。
        if resolves_native_zh(article, article.source.lang if article.source else LANG_AUTO):
            # 中文原文不需要译文，三个字段一起标成已处理，别每轮都来扫
            article.content_zh = ""
            article.digest_zh = article.digest_zh or ""
            article.title_zh = article.title_zh or ""
            stats["skipped"] += 1
            session.commit()
            continue
        article.i18n_attempts = (article.i18n_attempts or 0) + 1
        need_title = not article.title_zh
        need_digest = (
            article.relevance != 0
            and not article.digest_zh
            and bool((article.digest or "").strip())
            and not is_chinese_text(article.digest or "")
        )
        body_text = article.content_full or article.content or ""
        need_content = (
            article.relevance == 1
            and wants_content
            and not (article.content_zh or "").strip()
            and bool(body_text.strip())
        )
        session.commit()

        # ── 事务外：LLM 翻译（不持锁，一篇几分钟也没关系）──────────────
        title_zh = (
            translate_title_to_chinese(client, settings.prompts, article.title or "")
            if need_title else None
        )
        digest_zh = (
            translate_digest_to_chinese(client, settings.prompts, article.digest or "")
            if need_digest else None
        )
        content_zh = (
            translate_to_chinese(client, settings.prompts, body_text) if need_content else None
        )

        # ── 短事务 B：写回（先失效缓存重读，别覆盖别的进程刚写入的译文）─
        session.expire(article)
        article = session.get(Article, article_id)
        if article is None:
            continue
        if title_zh and not article.title_zh:
            article.title_zh = title_zh
            stats["titles"] += 1
        if article.relevance == 0:
            # 不相关的只存标题：导读本来就没有（标已处理，别下轮再来），
            # 正文不进日报，不花长文翻译的钱。详情页缺译文会如实说明。
            article.digest_zh = ""
        elif digest_zh and not article.digest_zh:
            article.digest_zh = digest_zh
            stats["digests"] += 1
        elif not article.digest_zh:
            # 没有导读、或导读本来就是中文：标成已处理，别每轮都来扫
            article.digest_zh = ""
        if content_zh and not (article.content_zh or "").strip():
            article.content_zh = content_zh
            stats["contents"] += 1
            # 顺手把章节结构也建起来。
            # 早先只在 process_article（新入库）里排版，补译这条路不排 ——
            # 于是所有「补出来的译文」正文里一个标题都没有，读者看到的是一整片
            # 没有层次的段落，段落横幅/本文目录也就永远不会出现。
            #
            # 关键：**基于英文原文**排版，而不是基于译文。译文是重新组织的，
            # 拿它切出来的分节与原文对不上；而原文那一份的英文小标题正好是
            # 双语视图英文侧要用的。两边由同一节结构派生，所以逐节对齐。
            # （这一步有 2~4 次 LLM 调用，在短事务里做 —— 有界，可接受。）
            if not article.body_sections_zh or not article.body_sections:
                _build_bilingual_sections(client, settings.prompts, article, body_text)
                stats["sections"] = stats.get("sections", 0) + 1
        session.commit()

    if any(stats[key] for key in ("titles", "digests", "contents")):
        log.info(
            "补齐中文版：标题 %d、速览 %d、正文 %d（候选 %d 篇）",
            stats["titles"],
            stats["digests"],
            stats["contents"],
            stats["candidates"],
        )
    return stats


def _build_bilingual_sections(
    client: LLMClient,
    prompts: PromptsConfig,
    article: Article,
    original: str,
) -> bool:
    """基于**英文原文**排一次版，同时写出中英两套章节。返回是否成功。

    为什么必须基于原文而不是译文：译文是重新组织的（实测 45 段压成 15 段），
    拿它切出来的分节与原文对不上。而原文那一次的英文小标题，正好就是双语
    视图英文侧要用的 —— 两侧由同一份节结构派生，逐节一一对应。
    中文原文的文章只需要中文侧（``section_pair(chinese=True)``）。
    """
    body = (original or "").strip()
    if not body:
        body = (article.content_zh or "").strip()
    if not body:
        return False
    sections = structure_sections(client, prompts, article.title or "", body)
    if not sections:
        return False
    chinese_source = not looks_english(body)
    zh_sections = section_pair(sections, chinese=True)
    if zh_sections is None:
        return False
    if chinese_source:
        # 原文就是中文：t 本来就是中文终稿，可以直接落库。
        article.body_sections_zh = json.dumps(zh_sections, ensure_ascii=False)
        article.content_zh = ""          # 本来就是中文，标成已译完
        return True

    # 英文原文：**中文侧的 t 必须来自翻译**。
    # section_pair(chinese=True) 只把标题换成中文，t 仍是英文原文 —— 直接
    # 落库就是「中文小标题 + 英文段落」，页面还当它是中文译文渲染。
    # 1267881 修的是 process_article 那条路径，漏了这里；而
    # backfill_translations / backfill_sections 走的都是这个函数，于是每补一次
    # 排版就把存量文章重新写坏一遍（实测清完 0 篇、下一轮又回到 33 篇）。
    en_sections = section_pair(sections, chinese=False)
    if en_sections is None:
        return False
    # 全部翻成功才落库：缺一节就整篇不写，宁可退回平铺也不要半中半英。
    translated: list[dict[str, str]] = []
    for zh_sec, en_sec in zip(zh_sections, en_sections, strict=True):
        one = translate_to_chinese(client, prompts, en_sec["t"])
        if not one:
            return False
        translated.append({"h": zh_sec["h"], "t": one})
    article.body_sections = json.dumps(en_sections, ensure_ascii=False)
    article.body_sections_zh = json.dumps(translated, ensure_ascii=False)
    return True


def backfill_sections(
    session: Session,
    client: LLMClient,
    settings: Settings,
    *,
    limit: int = 5,
) -> dict[str, Any]:
    """给存量中文正文补章节结构（只排版不翻译，每篇 1 次调用）。

    英文原文的章节在新入库时顺手建，这里只处理「已有中文正文、还没章节」
    的老数据。失败留空，下轮还会再来。
    """
    rows = list(
        session.execute(
            select(Article)
            .where(
                # 缺**任何一侧**的章节都算待补：只补中文侧会让双语模式变成
                # 「中文有小标题、英文没有」，看起来就是有些段落有双语有些没有。
                Article.body_sections_zh.is_(None) | Article.body_sections.is_(None),
                Article.content_zh.isnot(None),
                Article.content_zh != "",
                Article.relevance == 1,
                Article.link.notlike("http://localhost%"),
            )
            .order_by(Article.published_at.desc(), Article.id.desc())
            .limit(limit * 3)
        ).scalars()
    )
    stats = {"candidates": 0, "filled": 0}
    for article in rows:
        if stats["filled"] >= limit:
            break
        stats["candidates"] += 1
        if _build_bilingual_sections(client, settings.prompts, article, article.content_full or ""):
            stats["filled"] += 1
    session.flush()
    if stats["filled"]:
        log.info("补正文章节结构 %d 篇", stats["filled"])
    return stats


def backfill_briefs(
    session: Session,
    client: LLMClient,
    settings: Settings,
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    """给「有中文导读、但还没早报推送语」的文章补一段 fluent 的推送语。

    推送语是展示用的锦上添花：失败就留空，展示层回退截断拼凑，
    所以这里不记 attempts、不重试 —— 下轮还会再来。
    """
    batch = limit if limit is not None else settings.i18n.backfill_batch_size
    rows = list(
        session.execute(
            select(Article)
            .where(
                Article.brief_zh.is_(None),
                Article.relevance == 1,
                Article.link.notlike("http://localhost%"),
            )
            .order_by(Article.published_at.desc(), Article.id.desc())
            .limit(batch * 3)
        ).scalars()
    )
    stats = {"candidates": 0, "filled": 0}
    for article in rows:
        if stats["filled"] >= batch:
            break
        # 中文源的导读在 digest 里（digest_zh 是空串"已处理"标记），英文源的在 digest_zh
        digest = strip_markdown(article.digest_zh or article.digest or "")
        if not digest.strip() or not is_chinese_text(digest):
            continue
        stats["candidates"] += 1
        brief = generate_brief_zh(
            client, settings.prompts, article.title_zh or article.title or "", digest
        )
        if brief:
            article.brief_zh = brief
            stats["filled"] += 1
    session.flush()
    if stats["filled"]:
        log.info("补早报推送语 %d 条", stats["filled"])
    return stats


# 「LLM 不可用 → 整篇降级成英文」的重试策略。
#
# 为什么必须重试：一次 429 就会让 process_article 在第一个调用处抛异常，
# 文章被标成 failed —— 而 process_pending 只捞 pending，于是它再也不会被处理。
# 标题、导读、正文的中文版一个都不会有，页面上就整篇英文。
# 翻译是「锦上添花」的设计在这里变成了「永久缺失」：说了要确保译成中文，
# 实际是发出去就再也不管了。
#
# 上限是为了防止真的坏数据被无限重试；退避是为了别每 2 小时就去撞同一堵墙。
RETRY_MAX_ATTEMPTS = 6
RETRY_BACKOFF_MINUTES = 30


def retry_degraded(
    session: Session,
    *,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    backoff_minutes: int = RETRY_BACKOFF_MINUTES,
    limit: int = 200,
) -> dict[str, Any]:
    """把「因 LLM 不可用而降级」的文章放回 pending，等配额恢复后重新完整处理。

    只捞 ``status=failed`` 且尝试次数还没用完的：这些文章的降级原因是我们自己的
    上游问题，不是内容本身有问题，重新处理一次就能把中文版补齐。
    """
    stats = {"candidates": 0, "requeued": 0, "exhausted": 0}
    deadline = now_local() - timedelta(minutes=backoff_minutes)
    rows = list(
        session.execute(
            select(Article)
            .where(
                Article.status == STATUS_FAILED,
                # 次数上限必须写进 WHERE，不能取回 200 条再在循环里判。
                # 那样的话，攒够 200 条「已用完次数」的失败行之后，LIMIT 会被
                # 它们吃光，真正该重试的（次数还没满）永远排在后面挑不上，
                # requeued 会永久变成 0，而日志上看不出任何异常。
                Article.process_attempts < max_attempts,
            )
            .order_by(Article.published_at.desc(), Article.id.desc())
            .limit(limit)
        ).scalars()
    )
    stats["candidates"] = len(rows)
    exhausted_total = session.execute(
        select(func.count(Article.id)).where(
            Article.status == STATUS_FAILED, Article.process_attempts >= max_attempts
        )
    ).scalar_one()
    stats["exhausted"] = int(exhausted_total)
    for article in rows:
        # 刚试过就退避：上游限流通常不是一秒就恢复的
        if article.process_last_at and article.process_last_at > deadline:
            continue
        article.status = "pending"
        stats["requeued"] += 1
    session.flush()
    if stats["requeued"]:
        log.info(
            "把 %d 篇降级文章放回待处理（候选 %d 篇、已用完重试次数 %d 篇）",
            stats["requeued"],
            stats["candidates"],
            stats["exhausted"],
        )
    return stats
