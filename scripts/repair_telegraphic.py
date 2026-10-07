#!/usr/bin/env python3
"""清掉被网关「电报体」风格注入压缩过的存量译文/文案。

    python scripts/repair_telegraphic.py           # 只报告
    python scripts/repair_telegraphic.py --apply   # 真改

背景：中转网关（9router 等）会在请求里注入「回答像原始人一样简短」这类
风格指令，把完整的中文输出压成电报体 —— 虚词被系统性丢弃，只留实词堆叠。
实测污染窗口（2026-10-04 起多段）：摘要「OpenAI首日翻车。大模型商业化推视觉
广告砍API等值引怨。」、正文「苹果宣布新AI模型Ferret。全端侧运行。」。

这类污染**长度可能达标**，比例校验看不出来；只有虚词密度能识别
（``app.utils.text.looks_telegraphic``，阈值经线上数据校准：污染 ≤5.6、
正常 ≥7.1）。本脚本把命中的字段清掉，让它们重新进入补译/补写流程：

  · ``title_zh``     → 置空，下一轮 ``backfill_translations`` 重翻
  · ``digest_zh``    → 置空（不是空串，空串表示「已处理」），重新补
  · ``content_zh``   → 置空，重新翻
  · ``summary``      → 置空，下一轮 ``retry_degraded`` / 重新处理时重写
  · ``digest``       → 置空，同上
  · ``reason``       → 置空，同上
  · ``brief_zh``     → 置空，展示层回退截断拼凑
  · ``body_sections_zh`` → 整块置空（里面的 t 是压缩体）

处理过的文章会被标成 ``pending`` 重新进入处理队列（只清字段不重排的话，
``status=processed`` 的文章没有任何路径会回来重写它们）。

判据是逐字段的虚词密度，**中文原文文章不受影响**：它们的摘要/导读本来
就是模型直接写的（不是翻译），但同样会被网关压缩 —— 所以同样要修。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.ai.processor import translation_is_usable  # noqa: E402
from app.config import ConfigError, load_settings  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import Article  # noqa: E402
from app.utils.text import looks_telegraphic  # noqa: E402

# 会被清空重写的字段（模型生成的文案 / 译文）
_TEXT_FIELDS = ("title_zh", "digest_zh", "content_zh", "summary", "digest", "reason", "brief_zh")


def _body_translation_unusable(article: Article) -> bool:
    """正文译文是否存在但不可用（残篇 / 电报体 / 比例离谱）。

    ``looks_telegraphic`` 对短文本有最小汉字数门槛，而「电报体残篇」可能
    只有几十个字（实测 #482/469/468：40~57 字、比例 0.01~0.02）——
    这种要走整篇比例校验才能识别。展示层已经用同一个判断把它们当作
    「暂无译文」，但库里字段非空会让补译轮跳过它们（补译只补 NULL）。
    这里把它们清成 NULL，重新进入补译队列。
    """
    translated = (article.content_zh or "").strip()
    if not translated:
        return False
    original = article.content_full or article.content or ""
    return not translation_is_usable(original, translated)


def _sections_text(raw: str | None) -> str:
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


def _telegraphic_fields(article: Article) -> list[str]:
    """列出这篇里命中电报体（或残篇译文）的字段名，**不改数据**。"""
    hit: list[str] = []
    for field in _TEXT_FIELDS:
        value = getattr(article, field, None)
        if value and looks_telegraphic(value):
            hit.append(field)
    if _sections_text(article.body_sections_zh) and looks_telegraphic(
        _sections_text(article.body_sections_zh)
    ):
        hit.append("body_sections_zh")
    # 正文译文单独按整篇比例校验：短残篇过不了 looks_telegraphic 的最小长度门槛
    if "content_zh" not in hit and _body_translation_unusable(article):
        hit.append("content_zh")
        if article.body_sections_zh:
            hit.append("body_sections_zh")
    return hit


def _already_damaged(article: Article) -> list[str]:
    """找出「已处理、但核心文案字段为空」的文章 —— 曾被清过、等着重写。

    处理完成的文章**一定**有 summary（失败也有回退摘要），所以
    ``status=processed`` 却 ``summary IS NULL`` 只可能是被清理过、
    但还没重新入队的残留。补上这一条，脚本才是幂等的：跑第二遍也能
    把上一遍清过但没入队的文章捞回来。
    """
    if article.status != "processed" or article.relevance != 1:
        return []
    missing = [
        field for field in ("summary", "digest", "reason")
        if getattr(article, field, None) is None
    ]
    return missing


def main() -> int:
    apply = "--apply" in set(sys.argv[1:])
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"❌ 配置有问题：\n{exc}")
        return 2
    init_db(settings.db_file)

    total = 0
    requeued = 0
    fields_hit: dict[str, int] = {}
    with session_scope() as session:
        articles = session.execute(
            select(Article).where(Article.link.notlike("http://localhost%"))
        ).scalars()
        for article in articles:
            cleared = _telegraphic_fields(article)
            damaged = _already_damaged(article)
            if not cleared and not damaged:
                continue
            total += 1
            for field in cleared:
                fields_hit[field] = fields_hit.get(field, 0) + 1
            label = f"命中 {', '.join(cleared)}" if cleared else f"字段已空（{', '.join(damaged)}），补入队"
            print(f"  #{article.id} {label}｜{(article.title or '')[:36]}")
            if not apply:
                continue
            # 只有 --apply 才动数据。清字段 + 放回处理队列：
            # status=processed 的文章没有任何路径会回来重写它们
            #（补译只补「缺字段」，而这里的字段是「有但坏」）。
            for field in cleared:
                if field == "body_sections_zh":
                    article.body_sections_zh = None
                else:
                    setattr(article, field, None)
            article.status = "pending"
            article.i18n_attempts = 0
            requeued += 1
        if apply and total:
            print(f"\n已清理 {total} 篇并放回待处理队列（含补入队 {requeued} 篇）。")
        elif total:
            print(f"\n共 {total} 篇需要处理（**未改动任何数据**）。确认无误后加 --apply 执行。")
        else:
            print("没有命中电报体、也没有待补入队的数据。")
        if total:
            for field, count in sorted(fields_hit.items(), key=lambda kv: -kv[1]):
                print(f"   · {field}: {count}")
    if apply and total:
        print("\n下一轮处理（最多等 2 小时，或重启服务立即触发）会重写这些字段。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
