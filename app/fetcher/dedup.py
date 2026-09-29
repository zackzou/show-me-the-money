"""去重：先按 link 精确匹配，再按标题相似度。"""

from __future__ import annotations

from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Article
from app.utils.text import normalize_title

SIMILARITY_THRESHOLD = 0.9


def is_duplicate(
    session: Session,
    link: str,
    title: str,
    *,
    window: int = 500,
    threshold: float = SIMILARITY_THRESHOLD,
) -> bool:
    """link 命中即为重复；否则与最近 ``window`` 条标题做归一化相似度比较。"""
    normalized_link = (link or "").strip()
    if normalized_link:
        hit = session.execute(select(Article.id).where(Article.link == normalized_link).limit(1)).first()
        if hit is not None:
            return True

    normalized_title = normalize_title(title)
    if not normalized_title:
        return False

    recent_titles = session.execute(
        select(Article.title).order_by(Article.id.desc()).limit(window)
    ).scalars()
    for candidate in recent_titles:
        if normalize_title(candidate) == normalized_title:
            return True
        if SequenceMatcher(None, normalized_title, normalize_title(candidate)).ratio() > threshold:
            return True
    return False
