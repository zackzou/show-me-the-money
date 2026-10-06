#!/usr/bin/env python3
"""清掉「中文侧章节里其实是英文原文」的半截译文。

    python scripts/repair_half_translated.py           # 只报告
    python scripts/repair_half_translated.py --apply   # 真改

背景：``section_pair(chinese=True)`` 返回的小节里 ``t`` 是**英文原文**。曾经
的代码在逐节翻译之前就把它写进 ``body_sections_zh``，于是任何一节翻译失败、
或走到整篇回退分支时，这份「中文小标题 + 英文段落」的结构就留在库里。页面把
``body_sections_zh`` 当作中文译文渲染，读者看到的是「中文标题、正文英文」。
写入路径现在两处都修好了（``process_article`` 见 1267881，调度器走的
``_build_bilingual_sections`` 见后续提交），这个脚本只负责修**已经写坏的数据**。

为什么直接置空而不是就地翻译：``content_zh`` 在这些文章里是完好的整篇中文
译文，置空 ``body_sections_zh`` 之后中文视图退回它 —— 内容正确，只是暂时没有
小标题；同时 ``backfill_sections`` 的条件是「任一侧章节为空」，于是这些文章会
被下一轮补排版重新生成中英两套对齐结构。不在这里重新调 LLM 翻译，避免为了
修数据再花一遍长文翻译的钱。

判据刻意保守：英文原文 + 中文侧章节里几乎一个汉字都没有（英文词 > 20）。
真正中英混排的译文（产品名、术语）不会被误判。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.config import ConfigError, load_settings  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import Article  # noqa: E402

_HAN = re.compile(r"[\u4e00-\u9fff]")
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]*")


def _section_text(raw: str | None) -> str:
    """把 body_sections_zh 的正文拼起来；结构不合法就返回空串。"""
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if not isinstance(data, list):
        return ""
    return " ".join(str(sec.get("t", "")) for sec in data if isinstance(sec, dict))


def _is_foreign(body: str) -> bool:
    han = len(_HAN.findall(body))
    return len(_LATIN_WORD.findall(body)) > han


def _is_half_translated(body: str, sections_zh: str | None) -> bool:
    text = _section_text(sections_zh)
    if not text.strip():
        return False
    han = len(_HAN.findall(text))
    latin = len(_LATIN_WORD.findall(text))
    return latin > 20 and latin > han


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
        articles = session.execute(
            select(Article).where(
                Article.body_sections_zh.isnot(None),
                Article.content_full.isnot(None),
                Article.relevance == 1,
                Article.link.notlike("http://localhost%"),
            )
        ).scalars()
        for article in articles:
            body = article.content_full or ""
            if not _is_foreign(body):
                continue
            if not _is_half_translated(body, article.body_sections_zh):
                continue
            total += 1
            has_zh = bool((article.content_zh or "").strip())
            print(
                f"  #{article.id} 置空 body_sections_zh"
                f"（content_zh {'有，中文视图退回整篇译文' if has_zh else '也为空，中文视图会说明缺译文'}）"
                f"｜{article.title[:30]}"
            )
            article.body_sections_zh = None
        if total and apply:
            session.commit()
            print(f"\n已清理 {total} 篇，下一轮 backfill_sections 会重新排版。")
        elif total:
            print(f"\n共 {total} 篇需要清理。确认无误后加 --apply 执行。")
    if not total:
        print("没有半截译文的中文章节数据。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
