"""文章处理：相关度判断（顺带评分）→ 摘要 → 速览 → 推荐理由 → 标签；LLM 不可用时降级。"""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.client import LLMClient, LLMError
from app.ai.prompts import (
    render_classify_prompt,
    render_digest_prompt,
    render_reason_prompt,
    render_relevance_prompt,
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
from app.utils.text import is_chinese_text, looks_english, split_tags, strip_html, strip_markdown, truncate

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


# 正文翻译按段翻：一次翻太多容易被截断，也浪费 token
TRANSLATE_CHUNK_PARAGRAPHS = 4
# 参与翻译的正文长度上限（与抓取时的上限对齐）
MAX_CONTENT_CHARS = 40_000
# 译文明显比原文短一半以上，就认为这一段没翻成功
_TRANSLATE_MIN_RATIO = 0.4


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
    out: list[str] = []
    failed = 0
    for start in range(0, len(paragraphs), TRANSLATE_CHUNK_PARAGRAPHS):
        chunk = "\n\n".join(paragraphs[start : start + TRANSLATE_CHUNK_PARAGRAPHS])
        translated = _optional_llm(
            client, render_translate_content_prompt(prompts, chunk), ""
        ).strip()
        if not translated or len(translated) < len(chunk) * _TRANSLATE_MIN_RATIO:
            failed += 1
            continue
        out.append(translated)
    if not out:
        return None
    result = "\n\n".join(out)
    if failed:
        log.info("正文翻译有 %d/%d 段未成功，保留已译部分", failed, len(paragraphs))
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
    """处理单篇文章，返回最终 status（processed / failed）。"""
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
            if settings.i18n.translate_content:
                article.content_zh = translate_to_chinese(
                    client, settings.prompts, article.content_full or article.content or ""
                )

        tags = client.chat(render_tag_prompt(settings.prompts, article.title, summary))
        article.tags = ",".join(split_tags(tags)) or None
        article.status = STATUS_PROCESSED
        return STATUS_PROCESSED
    except LLMError as exc:
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
            session.flush()
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
                Article.relevance == 1,
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
        if not article.digest_zh:
            if not is_chinese_text(article.digest or ""):
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
