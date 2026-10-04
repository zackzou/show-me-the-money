"""文章处理：相关度判断（顺带评分）→ 摘要 → 速览 → 推荐理由 → 标签；LLM 不可用时降级。"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, or_, select
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
from app.fetcher.content import is_real_body
from app.models import Article
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

# 列表页只在分数不低于这个值时强调「值得看」
SCORE_STRONG = 70


def parse_relevance(answer: str) -> tuple[bool, int | None]:
    """解析相关度判断的结果：``yes 85`` → ``(True, 85)``。

    容忍各种不规范输出：光一个 ``yes``、中文「是的」、分数在前、分数越界等。
    评分是可选的 —— 模型没给就不给，不能因此把一条好内容判掉。
    """
    text = (answer or "").strip()
    head = text.casefold()[:8]
    relevant = head.startswith(("yes", "y")) or head.startswith("是")
    match = _SCORE_RE.search(text)
    score = None
    if match:
        value = int(match.group(1))
        if 0 <= value <= 100:
            score = value
    return relevant, score


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
TRANSLATE_CHUNK_CHARS = 2600
# 单块至少要有这么多字符才单独成块（否则碎片会被并到上一块）
TRANSLATE_CHUNK_MIN_CHARS = 400
# 参与翻译的正文长度上限（与抓取时的上限对齐）
MAX_CONTENT_CHARS = 40_000
# 重写后的中文长度下限（占原文的比例）。
# 提示词要求压到原文的 40%~70%，所以 0.25 是「明显没写完」的兜底线；
# 完整重写实测落在 0.35~0.6。压缩成摘要的（0.1 上下）会被这条拦掉。
_TRANSLATE_MIN_RATIO = 0.25
# 中文重写的合理上限：超过原文长度说明模型在扩写/复述，不是编译
_TRANSLATE_MAX_RATIO = 1.3


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
    return truncate(result, 200) or None


# 章节最多分这么多节：再多就不是「排版」，而是把文章切碎了
MAX_SECTIONS = 6
# 结构化后的文字量不能比原文少太多，否则模型一定是改写/省略了，直接丢掉
_STRUCTURE_MIN_RATIO = 0.8


def parse_sections(raw: str) -> list[dict[str, str]] | None:
    """解析章节排版结果：``## 小标题`` 开头新节，其余是该节段落。

    段数太少（单节无标题）、节数超限、文字量对不上的一律返回 ``None``，
    调用方回退原文直排 —— 排版是锦上添花，不能把正文排丢了。
    """
    lines = [line.strip() for line in (raw or "").splitlines()]
    headings: list[str] = []
    buckets: list[list[str]] = []
    for line in lines:
        if not line:
            continue
        if line.startswith("##"):
            heading = line.lstrip("#").strip().strip("：:* ").strip()[:30]
            headings.append(heading)
            buckets.append([])
            continue
        clean = strip_markdown(line)
        if not clean:
            continue
        if not buckets:
            headings.append("")
            buckets.append([])
        buckets[-1].append(clean)
    # 去掉空节
    pairs = [(h, p) for h, p in zip(headings, buckets, strict=True) if p]
    if not pairs or len(pairs) > MAX_SECTIONS + 2:
        return None
    if len(pairs) == 1 and not pairs[0][0]:
        return None  # 等于没分，不占存储
    return [{"h": h, "t": "\n\n".join(p)} for h, p in pairs]


def structure_sections(
    client: LLMClient, prompts: PromptsConfig, title: str, body: str
) -> list[dict[str, str]] | None:
    """NYT 责任编辑视角：只分段加小标题，不改写。

    太短（3 段以内）不值得分；结构化丢字超两成直接丢掉。失败返回 ``None``。
    """
    paras = split_paragraphs(body)
    if len(paras) <= 3:
        return None
    raw = _optional_llm(client, render_structure_prompt(prompts, title, body), "")
    if not raw.strip():
        return None
    sections = parse_sections(raw)
    if not sections:
        return None
    kept = sum(len(s["t"]) for s in sections)
    total = sum(len(p) for p in paras)
    if total <= 0 or kept < total * _STRUCTURE_MIN_RATIO:
        return None
    return sections[:MAX_SECTIONS]


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
    for chunk in chunks:
        translated = _optional_llm(
            client, render_translate_content_prompt(prompts, chunk), ""
        ).strip()
        ratio = len(translated) / len(chunk) if chunk else 0.0
        # 太短=没写完，太长=在扩写复述；两种都不该写进库
        if not translated or ratio < _TRANSLATE_MIN_RATIO or ratio > _TRANSLATE_MAX_RATIO:
            failed += 1
            continue
        out.append(translated)
    if not out:
        return None
    result = "\n\n".join(out)
    if failed:
        log.info("正文重写有 %d/%d 块未成功，保留已写部分", failed, len(chunks))
    return result


def _optional_llm(client: LLMClient, prompt: str, fallback: str) -> str:
    """跑一个「锦上添花」的 LLM 步骤；失败就用 fallback，不影响主流程。"""
    if not prompt:
        return fallback
    try:
        return client.chat(prompt)
    except LLMError as exc:
        log.warning("可选步骤失败，已降级（%s）", exc)
        return fallback


def process_article(session: Session, article: Article, client: LLMClient, settings: Settings) -> str:
    """处理单篇文章，返回最终 status（processed / failed）。

    每次调用都记一次尝试：上游 LLM 限流会在第一个调用就抛异常，整篇降级成英文，
    必须留下「试过几次、什么时候试的」才能在配额恢复后回来重试（见 ``retry_degraded``）。
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
            article.score = score
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

        summary = client.chat(render_summary_prompt(settings.prompts, topic, article.title, excerpt))
        article.summary = truncate(summary, 500)

        # 速览：让读者在页内读完，不用跳原站
        digest = _optional_llm(
            client,
            render_digest_prompt(settings.prompts, article.title, summary, excerpt),
            _fallback_digest(article, settings.prompts.fallback_digest_chars),
        )
        article.digest = truncate(digest, 400)

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

        # 中英双语。标题和导语分别判断：英文信源常见「英文标题 + 中文导语」，
        # 整段一起判断会漏掉该翻的导语，也会给纯英文标题翻出一份一模一样的自己。
        if settings.i18n.enabled:
            # 判「要不要翻」只看有没有汉字，与长短无关：
            # 短英文标题会被 looks_english 判成「不是英文」，于是白白翻一次英文→英文
            title_needs = is_chinese_text(article.title)
            digest_needs = is_chinese_text(summary)
            if not title_needs and not digest_needs:
                # 整篇本来就是英文，直接复用，不必再花一次调用
                article.title_en = article.title
                article.digest_en = truncate(summary, 400)
            else:
                raw_en = _optional_llm(
                    client,
                    render_translate_prompt(settings.prompts, article.title, summary),
                    "",
                )
                en_title, en_digest = parse_translate(raw_en)
                article.title_en = en_title if title_needs else article.title
                article.digest_en = en_digest if digest_needs else truncate(summary, 400)
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
            sections = structure_sections(client, settings.prompts, article.title or "", body_text)
            if sections is not None:
                article.body_sections = json.dumps(sections, ensure_ascii=False)
            if settings.i18n.translate_content:
                if sections is not None and looks_english(body_text):
                    # 按节翻：节与节之间天然对齐，中英文对照不会错位；
                    # 某一节失败就整篇回退整翻，不留半中半英
                    zh_sections: list[dict[str, str]] = []
                    failed = False
                    for section in sections:
                        one = translate_to_chinese(client, settings.prompts, section["t"])
                        if not one:
                            failed = True
                            break
                        zh_sections.append({"h": section["h"], "t": one})
                    if not failed:
                        article.body_sections_zh = json.dumps(zh_sections, ensure_ascii=False)
                        article.content_zh = "\n\n".join(s["t"] for s in zh_sections)
                    else:
                        article.content_zh = translate_to_chinese(
                            client, settings.prompts, body_text
                        )
                elif sections is not None:
                    # 中文原文：章节直接复用，不花翻译调用
                    article.body_sections_zh = article.body_sections
                    article.content_zh = ""
                else:
                    article.content_zh = translate_to_chinese(
                        client, settings.prompts, body_text
                    )

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
    statement = select(Article).where(Article.status == "pending").order_by(Article.published_at.desc())
    if limit is not None:
        statement = statement.limit(limit)
    articles = list(session.execute(statement).scalars())

    stats = {"pending": len(articles), "processed": 0, "irrelevant": 0, "failed": 0, "crashed": 0}
    checkpoint = max(1, settings.ai.batch_checkpoint_every)
    for index, article in enumerate(articles, start=1):
        previous_relevance = article.relevance
        try:
            status = process_article(session, article, client, settings)
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

    四个坑，都是实测踩出来的：

    1. **中文原文要当场把三个字段都标成已处理。** 只写 ``content_zh=""`` 的话，
       ``title_zh`` / ``digest_zh`` 还是 NULL，这篇每轮都会被重新捞回来，白占名额。
    2. **重试次数少的优先。** 一旦上游限流，最新的那几篇会一直失败；
       按发布时间倒序取前 N 篇的话，它们会把名额占满，排在后面的永远轮不到。
    3. **不拿 title_en 当条件。** 处理早期失败的文章压根没轮到写这个字段，
       拿它当条件会让这类文章永远等不到中文标题。
    4. **先补标题与导读，再翻正文。** 正文是长文、一次要翻好几段，单篇成本是
       标题的十几倍；而标题与导读是首页和详情页最显眼的地方。先把便宜的补齐，
       读者先看到全中文，再慢慢补正文。
    """
    if not settings.i18n.enabled:
        return {"candidates": 0, "titles": 0, "digests": 0, "contents": 0, "skipped": 0}
    wants_content = settings.i18n.translate_content
    batch = limit if limit is not None else settings.i18n.backfill_batch_size
    rows = list(
        session.execute(
            select(Article)
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
                Article.link.notlike("http://localhost%"),
            )
            .order_by(Article.i18n_attempts.asc(), Article.published_at.desc(), Article.id.desc())
            .limit(batch)
        ).scalars()
    )
    stats = {"candidates": len(rows), "titles": 0, "digests": 0, "contents": 0, "skipped": 0}
    pending_content: list[tuple[Article, str]] = []
    for article in rows:
        source = article.content_full or ""
        if not looks_english(source):
            # 中文原文不需要译文，三个字段一起标成已处理，别每轮都来扫
            article.content_zh = ""
            article.digest_zh = article.digest_zh or ""
            article.title_zh = article.title_zh or ""
            stats["skipped"] += 1
            continue
        article.i18n_attempts = (article.i18n_attempts or 0) + 1
        if not article.title_zh:
            title_zh = translate_title_to_chinese(client, settings.prompts, article.title or "")
            if title_zh:
                article.title_zh = title_zh
                stats["titles"] += 1
        if article.relevance == 0:
            # 不相关的只存标题：导读本来就没有（标已处理，别下轮再来），
            # 正文不进日报，不花长文翻译的钱。详情页缺译文会如实说明。
            article.digest_zh = ""
            continue
        if not article.digest_zh:
            if not (article.digest or "").strip():
                # 压根没有导读：没有东西可翻，标成已处理，别每轮都来占名额。
                # 否则 digest_zh 永远是 NULL，这篇每轮都被捞回来白跑一趟。
                article.digest_zh = ""
            elif not is_chinese_text(article.digest or ""):
                digest_zh = translate_digest_to_chinese(client, settings.prompts, article.digest or "")
                if digest_zh:
                    article.digest_zh = digest_zh
                    stats["digests"] += 1
            else:
                # 没有导读、或导读本来就是中文：标成已处理，别每轮都来扫
                article.digest_zh = ""
        if wants_content and not article.content_zh:
            pending_content.append((article, source))

    # 正文译文单独一轮：标题与导读都已经补上了，这里只翻长文
    for article, source in pending_content:
        translated = translate_to_chinese(client, settings.prompts, source)
        if translated:
            article.content_zh = translated
            stats["contents"] += 1
            # 顺手把章节结构也建起来。
            # 早先只在 process_article（新入库）里排版，补译这条路不排 ——
            # 于是所有「补出来的译文」正文里一个标题都没有，读者看到的是一整片
            # 没有层次的段落，段落横幅/本文目录也就永远不会出现。
            if not article.body_sections_zh:
                sections = structure_sections(
                    client, settings.prompts, article.title_zh or article.title or "", translated
                )
                if sections:
                    article.body_sections_zh = json.dumps(sections, ensure_ascii=False)
                    stats["sections"] = stats.get("sections", 0) + 1
    session.flush()
    if any(stats[key] for key in ("titles", "digests", "contents")):
        log.info(
            "补齐中文版：标题 %d、速览 %d、正文 %d（候选 %d 篇）",
            stats["titles"],
            stats["digests"],
            stats["contents"],
            stats["candidates"],
        )
    return stats


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
                Article.body_sections_zh.is_(None),
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
        sections = structure_sections(
            client, settings.prompts, article.title_zh or article.title or "",
            article.content_zh or "",
        )
        if sections:
            article.body_sections_zh = json.dumps(sections, ensure_ascii=False)
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
            .where(Article.status == STATUS_FAILED)
            .order_by(Article.published_at.desc(), Article.id.desc())
            .limit(limit)
        ).scalars()
    )
    stats["candidates"] = len(rows)
    for article in rows:
        if (article.process_attempts or 0) >= max_attempts:
            stats["exhausted"] += 1
            continue
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
