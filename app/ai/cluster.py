"""同一则新闻的跨源合并：把「多家源报道同一件事」收成一条。

为什么入库时的去重不够用
------------------------
``fetcher/dedup.py`` 只比 link 和**标题相似度**。跨源转载同一件事时标题往往完全不同：

    TechCrunch: Apple says it's tightening macOS 'Full Disk Access' controls…
    The Verge:  Apple will limit Mac disk access as AI agents 'substantially'…
    Ars Technica: Apple changes full-disk access permissions to curb abuse…

三条标题相似度只有 0.2~0.4，全都漏了过去，于是同一天里同一条新闻在首页并排出现三次。

正文重合度不能用来判定
----------------------
实测把同一件事的两条正文做词集合 Jaccard：

    #35 TechCrunch ↔ #53 The Verge      0.62   同一件事
    #31 TechCrunch ↔ #51 The Verge      0.54   同一件事
    #31 TechCrunch ↔ #35 TechCrunch     0.50   两件不同的事（同站同题材，词汇天然重叠）
    #35 TechCrunch ↔ #61 Ars Technica   0.39   同一件事

同一个 0.5 上下既是「同一件事」也是「不同的事」，阈值卡在哪边都会错。标题实词
重合度倒是分得很干净（真重复 0.33~0.67、假重复 0.00~0.08），所以：

**程序只负责圈出候选对（便宜、可解释），「是不是同一件事」交给大模型判。**
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.ai.client import LLMClient, LLMError
from app.ai.prompts import render_same_story_prompt
from app.config import PromptsConfig, Settings
from app.models import Article
from app.utils.logger import get_logger
from app.utils.text import now_local, strip_html

log = get_logger(__name__)

# ── 圈候选对用的阈值 ────────────────────────────────────────────────────────
# 只看标题实词的重合度。实测把这一批文章的两两组合全算了一遍，分得很干净：
#
#   真重复   标题重合 0.33 ~ 0.67   （Apple 磁盘访问 ×3、MIT ×2、Kolibri ×2、Muse ×2…）
#   假重复   标题重合 0.00 ~ 0.08   （同站不同题材，两篇都在聊 AI / model）
#
# 正文词集合的重合度完全不可用：真重复落在 0.23~0.58，假重复落在 0.26~0.49，
# 两段区间叠在一起，阈值卡在哪边都会错。所以正文只用来给候选对排序，不用来判定。
TITLE_OVERLAP_MIN = 0.25
# 至少要有这么多重合实词，否则「AI / model」这种通用词也能凑够比例
TITLE_OVERLAP_MIN_WORDS = 2
# 发布时间相差超过这么久就不算同一件事了。取 7 天而不是一两天：
# 抓取是按小时轮询的，一批文章会在库里躺好几天才轮到处理与合并 ——
# 实测同一则新闻的两个源可以差 3 天（10-03 的 TechCrunch / 09-30 的 MIT Tech Review），
# 窗口卡在 72 小时就永远比不到一起。
PAIR_WINDOW_DAYS = 7
# 正文最多看前这么多字符（只用于给候选对排序）
BODY_SAMPLE_CHARS = 4000
# 一轮最多判多少对：每对一次调用，别把配额打爆
MAX_PAIRS_PER_RUN = 8

_WORD_RE = re.compile(r"[a-z][a-z0-9']{3,}")
_CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]")

# 英文常见词参与比较只会制造噪声（两篇科技报道都满是 the / with / model）
_STOPWORDS = frozenset(
    {
        "about", "above", "after", "again", "against", "also", "among", "because",
        "been", "before", "being", "between", "both", "could", "does", "doing",
        "down", "during", "each", "from", "further", "have", "having", "here",
        "into", "itself", "more", "most", "only", "other", "over", "same",
        "should", "some", "such", "than", "that", "their", "them", "then",
        "there", "these", "they", "this", "those", "through", "under", "until",
        "very", "were", "what", "when", "where", "which", "while", "with",
        "would", "your", "said", "says", "say", "new", "now", "will", "can",
        "may", "might", "via", "using", "used", "use",
    }
)


def _words(text: str | None) -> set[str]:
    """正文里取实词（英文 4 字母以上、中文 2 字以上的片段）。"""
    sample = (text or "")[:BODY_SAMPLE_CHARS]
    latin = {w for w in _WORD_RE.findall(sample.lower()) if w not in _STOPWORDS}
    cjk = _CJK_RE.findall(sample)
    # 中文没有空格可切，用 2-gram 当「词」
    cjk_pairs = {sample[i : i + 2] for i in range(len(sample) - 1)} if cjk else set()
    return latin | cjk_pairs


def _title_words(title: str | None) -> set[str]:
    text = (title or "").lower()
    latin = {w for w in _WORD_RE.findall(text) if w not in _STOPWORDS}
    cjk = _CJK_RE.findall(text)
    cjk_pairs = {text[i : i + 2] for i in range(len(text) - 1)} if cjk else set()
    return latin | cjk_pairs


def dedup_title(article: Article) -> str:
    """拿来比对重复的标题：中文版优先。

    这样「TechCrunch 的英文报道」和「雷锋网的中文报道」讲同一件事时也能对上 ——
    前提是英文那条已经有中文标题（``title_zh``），这也正是补译队列在做的事。
    """
    return article.title_zh or article.title or ""


def _overlap(a: set[str], b: set[str]) -> float:
    """用较小一侧做分母：转载稿常常一长一短，只算 Jaccard 会被长文稀释。"""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _primary_of(a: Article, b: Article) -> Article:
    """同一件事里留哪一条当主条目。

    排序键依次是：**有中文译文 → 正文更长 → 发布更早**。

    译文排在正文长度前面，是因为读者实际看到的就是主条目：按长度选主时，
    一篇 5000 字的英文原文会盖过一篇 100 字但已经译好中文的稿子，结果是
    读者点进来看到整页英文 —— 而被藏起来的那篇本来有译文。这个站的
    不变量就是「中文模式下不至于整页英文」，不能为了正文长度把它破坏掉。
    """
    def rank(article: Article) -> tuple[int, int, float]:
        translated = int(bool((article.content_zh or "").strip()))
        return (
            translated,
            len(article.content_full or ""),
            -(article.published_at.timestamp() if article.published_at else 0.0),
        )

    return a if rank(a) >= rank(b) else b


def find_duplicate_pairs(articles: list[Article], *, limit: int = MAX_PAIRS_PER_RUN) -> list[tuple[Article, Article]]:
    """在候选集合里找「可能是同一件事」的文章对，按可疑度从高到低。

    先用标题实词重合度圈候选（这一刀很准，见上面阈值处的实测数据），
    再用正文重合度给候选对排序 —— 最像的那几对先问模型。
    """
    scored: list[tuple[float, Article, Article]] = []
    for index, left in enumerate(articles):
        for right in articles[index + 1 :]:
            if left.duplicate_of or right.duplicate_of:
                continue
            left_words, right_words = _title_words(dedup_title(left)), _title_words(dedup_title(right))
            shared = left_words & right_words
            title_overlap = _overlap(left_words, right_words)
            if title_overlap < TITLE_OVERLAP_MIN or len(shared) < TITLE_OVERLAP_MIN_WORDS:
                continue
            body_overlap = _overlap(_words(strip_html(left.content_full)), _words(strip_html(right.content_full)))
            scored.append((title_overlap + body_overlap / 10, left, right))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [(a, b) for _, a, b in scored[:limit]]


def parse_same_story(answer: str) -> bool:
    """解析「这两条是不是同一件事」的判定。模型只回答 yes/no。

    答不出来一律当作「不是」—— 宁可多留一条，也不要误删内容。
    """
    text = (answer or "").strip().casefold()
    head = text.lstrip(" -•*#：:")[:8]
    return head.startswith(("yes", "y", "是", "同一"))


def same_story(
    client: LLMClient, prompts: PromptsConfig, left: Article, right: Article
) -> bool:
    """问模型这两条是不是同一件事。模型不可用 / 答不出 → 当作「不是」。"""
    shared = sorted(_title_words(dedup_title(left)) & _title_words(dedup_title(right)))
    raw = ""
    try:
        raw = client.chat(
            render_same_story_prompt(
                prompts, dedup_title(left), dedup_title(right), "、".join(shared[:8])
            )
        )
    except LLMError as exc:
        log.warning("同题判定失败，按「不是同一件事」处理（%s）", exc)
    return parse_same_story(raw)


def _candidate_articles(session: Session, *, limit: int, window_days: int = PAIR_WINDOW_DAYS) -> list[Article]:
    """取最近一段时间内、还没被判过重复的相关文章。"""
    since = now_local().timestamp() - window_days * 86400
    rows = list(
        session.execute(
            select(Article)
            .where(
                Article.relevance == 1,
                Article.status.in_(("processed", "failed")),
                Article.duplicate_of.is_(None),
                Article.link.notlike("http://localhost%"),
                Article.published_at.isnot(None),
            )
            .order_by(Article.published_at.desc(), Article.id.desc())
        ).scalars()
    )
    return [a for a in rows if (a.published_at.timestamp() if a.published_at else 0) >= since][:limit]


def repair_duplicate_links(session: Session) -> int:
    """修掉会让整条报道从站上消失的坏 ``duplicate_of`` 指向，返回修复条数。

    两类坏指向，都会在库里真实出现过：

    1. **指向 relevance != 1 的文章。** 主条目被判为不相关之后，它就成了藏稿的
       黑洞：主页、日报、搜索全都要求 ``relevance == 1``，于是被它藏起来的那条
       （本身 relevance=1、内容完整）在任何列表里都找不到。实测 172/177 就是
       这么个状态。原因在处理器：LLM 出错时先写 ``relevance=1`` 占位避免漏掉当天
       内容，之后重排队真的判成不相关，但 ``duplicate_of`` 从没回头看过。
    2. **指向已经不存在的 id。** 清理任务按 ``created_at`` 删文章，主条目先到期
       被删掉、藏稿还留着，于是藏稿指向虚空，同样谁也看不到它。

    修复方式：把藏稿**提升为独立条目**（``duplicate_of=NULL``）。宁可多一条重复，
    也不能让内容彻底消失 —— 这是 ``parse_same_story``「宁可多留一条」的那条原则
    在存储层的延续。
    """
    existing_ids = select(Article.id)
    bad = session.execute(
        select(Article.id).where(
            Article.duplicate_of.isnot(None),
            or_(
                # 指向已经被清理任务删掉的 id
                Article.duplicate_of.notin_(existing_ids),
                # 指向一篇自己已经不相关的主条目（藏稿黑洞，见 docstring）
                Article.duplicate_of.in_(
                    select(Article.id).where(Article.relevance != 1)
                ),
            ),
        )
    ).scalars().all()
    for article_id in bad:
        session.execute(
            update(Article).where(Article.id == article_id).values(duplicate_of=None)
        )
    if bad:
        log.info("修复了 %d 条指向失效的重复标记（藏稿已提升为独立条目）", len(bad))
    return len(bad)


def merge_duplicates(
    session: Session,
    client: LLMClient,
    settings: Settings,
    *,
    limit: int = 40,
) -> dict[str, Any]:
    """把跨源重复的文章合并成一条，返回统计。

    ``limit`` 是参与比较的文章数（按发布时间倒序），``MAX_PAIRS_PER_RUN``
    限制每轮实际发起的判定调用数。
    """
    repair_duplicate_links(session)
    rows = _candidate_articles(session, limit=limit)
    pairs = find_duplicate_pairs(rows)
    stats = {"candidates": len(rows), "pairs": len(pairs), "merged": 0, "kept": 0}
    for left, right in pairs:
        if not same_story(client, settings.prompts, left, right):
            stats["kept"] += 1
            continue
        primary = _primary_of(left, right)
        loser = right if primary is left else left
        # **任何一侧**已经挂过别人就跳过。只查 primary 是漏的：实测库里已经
        # 出现 53 → 35 → 61 这样的链，而 primary_of() 只解析一跳，于是 53 的
        # 「其他来源也报道了」列表里根本没有自己。链条一旦形成，某一方的
        # 聚合视图就会静默丢掉一个来源。
        if primary.duplicate_of or loser.duplicate_of:
            stats["kept"] += 1
            continue
        loser.duplicate_of = primary.id
        stats["merged"] += 1
        log.info(
            "合并重复：#%d %s ← #%d %s",
            primary.id,
            primary.title[:40],
            loser.id,
            loser.title[:40],
        )
    session.flush()
    return stats


def duplicates_of(session: Session, article_id: int) -> list[Article]:
    """主条目下挂了哪些重复稿（详情页底部列出来，一条内容都不丢）。"""
    return list(
        session.execute(select(Article).where(Article.duplicate_of == article_id)).scalars()
    )


def primary_of(session: Session, article: Article) -> Article | None:
    """重复稿指向的主条目；沿着链一路走到末端。

    只解析一跳是不够的：库里出现过 53 → 35 → 61 这样的链，一跳会让 53 指到
    35（它自己也是藏稿），于是 53 页面上的「另一篇更完整的报道」点进去还是
    一篇藏稿，而 61 的来源列表里又没有 53。走到末端才是一条扁平的结构。
    """
    seen: set[int] = set()
    current = article
    while current.duplicate_of and current.duplicate_of not in seen:
        seen.add(current.duplicate_of)
        nxt = session.get(Article, current.duplicate_of)
        if nxt is None:
            return None
        current = nxt
    return current if current.id != article.id else None