#!/usr/bin/env python3
"""清掉「中文原文却被翻成英文」的存量数据。

    python scripts/cleanup_native_zh.py           # 只报告，不改
    python scripts/cleanup_native_zh.py --apply   # 真改

为什么需要它：中文原生文章不做英文翻译是这一版才有的规则（判据是正文里
汉字占多数）。规则之前入库的文章，``title_en`` / ``digest_en`` 与英文侧的
``body_sections`` 都还在。页面上已经按正文判定隐藏了 EN / 双语按钮，但库里
留着这些字段，早报推送、RSS 输出这些**不读 langs 的地方**仍可能用上它们，
于是中文文章在手机上顶着英文标题。

判定与 ``routes.story`` 保持同一口径（``is_english`` 看正文），并且**跳过**
信源上显式配了 ``lang=en`` 的文章 —— 用户明确指定英文源的，尊重设置。

只清「多出来」的中文文章的英文字段，不删任何中文内容，也不碰英文原文的文章。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.config import ConfigError, load_settings  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.fetcher.lang import LANG_EN, resolve_lang  # noqa: E402
from app.models import Article, Source  # noqa: E402
from app.utils.text import looks_english  # noqa: E402


def _drop_english_side(article: Article) -> list[str]:
    """把一篇文章里多出来的英文字段清空，返回被改动的字段名。"""
    changed: list[str] = []
    if (article.title_en or "").strip():
        article.title_en = None
        changed.append("title_en")
    if (article.digest_en or "").strip():
        article.digest_en = None
        changed.append("digest_en")
    if article.body_sections:
        article.body_sections = None
        changed.append("body_sections")
    # content_zh 对中文原文来说应该是空串（表示「不需要译」）。中文正文里
    # 混着产品名、术语、代码时 is_chinese_text 会判成 False（40 字母门槛），
    # 那种译文其实是「中英混排」，留着比清掉更合适，所以只清纯外语的。
    if article.content_zh and looks_english(article.content_zh):
        article.content_zh = ""
        changed.append("content_zh")
    # 中文标题/导读缺了就用原文顶上，别让「已处理」的文章在中文模式下露英文
    if not (article.title_zh or "").strip():
        article.title_zh = article.title
        changed.append("title_zh")
    return changed


def main() -> int:
    apply = "--apply" in set(sys.argv[1:])
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"❌ 配置有问题：\n{exc}")
        return 2
    init_db(settings.db_file)
    total = 0
    with session_scope() as session:
        langs = dict(session.execute(select(Source.id, Source.lang)).all())
        candidates = session.execute(
            select(Article).where(
                Article.content_full.isnot(None),
                Article.relevance == 1,
                Article.link.notlike("http://localhost%"),
            )
        ).scalars()
        for article in candidates:
            body = article.content_full or ""
            if not body.strip() or looks_english(body):
                continue
            # 正文太短的判不准，留给展示层处理，不在这里动
            if len(body) < 400:
                continue
            forced = resolve_lang((langs.get(article.source_id) if article.source_id else None) or "auto")
            if forced == LANG_EN:
                print(f"  跳过 #{article.id}（信源显式设为英文）{article.title[:36]}")
                continue
            fields = _drop_english_side(article)
            if fields:
                total += 1
                print(f"  #{article.id} 清掉 {', '.join(fields)}｜{article.title[:36]}")
    if total and apply:
        session.commit()
        print(f"\n已清理 {total} 篇。加上 --apply 才会真的写入。")
    elif total:
        print(f"\n共 {total} 篇需要清理。确认无误后加 --apply 执行。")
    else:
        print("没有需要清理的中文原文英译数据。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())