#!/usr/bin/env python3
"""启动自检：配置 → 数据库 → 信源 → （可选）LLM 连通性与抓取。

    python scripts/init_check.py            # 只做静态检查
    python scripts/init_check.py --ping     # 额外调一次 LLM
    python scripts/init_check.py --fetch    # 额外跑一次抓取
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ai.client import LLMClient  # noqa: E402
from app.config import ConfigError, load_settings  # noqa: E402
from app.db import init_db, seed_sources, session_scope  # noqa: E402
from app.fetcher.pipeline import run_fetch_pipeline  # noqa: E402
from app.models import Article, DailyReport, Source  # noqa: E402
from app.utils.text import looks_telegraphic  # noqa: E402

# 翻译质量探针用的样本：一段完整的英文资讯，翻成中文应有 0.35~0.6 的压缩比、
# 且虚词密度正常。网关注入「回答简短」类指令时，这段会被压成电报体。
_PROBE_SOURCE = (
    "Apple announced a new AI model called Ferret, which runs entirely on-device "
    "and can process images and text together. The company says it will be "
    "available to developers next month."
)


def _probe_translation_quality(client: LLMClient) -> None:
    """真发一次翻译请求，检查输出是否被网关的风格注入压缩。

    为什么单独做这个探针：``--ping`` 只验「能不能连通」，而网关最常见的
    故障不是不通，是**通但把内容压坏**（9router 的 CAVEMAN 注入把完整翻译
    压成「苹果宣布新AI模型Ferret。全端侧运行。」）。这种故障下所有调用都
    返回 200、长度校验也可能过，只有看输出形态才能发现。
    """
    prompt = (
        "把下面这段英文翻译成中文，只输出译文，不要解释：\n\n" + _PROBE_SOURCE
    )
    try:
        reply = client.chat(prompt, max_tokens=1200)
    except Exception as exc:
        print(f"⚠️ 翻译探针调用失败：{exc}")
        return
    ratio = len(reply) / len(_PROBE_SOURCE)
    if looks_telegraphic(reply):
        print("⚠️ 翻译探针：输出疑似被网关压缩成电报体（虚词密度过低）")
        print(f"   实际输出：{reply[:80]}")
        print("   建议检查网关是否开启了「简短回答 / token saver / caveman」类注入，")
        print("   并确认 LLM_EXTRA_HEADERS 里的关闭开关真的到达了网关。")
    elif ratio < 0.25:
        print(f"⚠️ 翻译探针：输出过短（比例 {ratio:.2f}），疑似被截断或压缩")
        print(f"   实际输出：{reply[:80]}")
    else:
        print(f"✅ 翻译质量探针：比例 {ratio:.2f}，形态正常")


def main() -> int:
    argv = set(sys.argv[1:])
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"❌ 配置有问题：\n{exc}")
        return 2

    print("✅ 配置校验通过")
    print(f"   LLM      : {settings.llm.api_base} / {settings.llm.model}")
    print(f"   调研方向 : {', '.join(settings.research_topics)}")
    print(f"   数据库   : {settings.db_file}")
    print(f"   信源     : {len(settings.sources)} 个（启用 {sum(1 for s in settings.sources if s.enabled)}）")

    init_db(settings.db_file)
    seeded = seed_sources(settings.sources)
    print(f"✅ 数据库就绪（本次导入信源 {seeded} 个）")

    with session_scope() as session:
        print(
            f"   现状：信源 {session.query(Source).count()} · "
            f"文章 {session.query(Article).count()} · 日报 {session.query(DailyReport).count()}"
        )

    if "--ping" in argv:
        client = LLMClient(
            settings.llm.api_base,
            settings.llm.api_key,
            settings.llm.model,
            timeout=settings.llm.timeout_seconds,
            # 参数名是 retries（LLMClient 的构造参数），配置里叫 max_retries ——
            # 照抄配置名会直接 TypeError，`init_check.py --ping` 根本跑不起来
            retries=settings.llm.max_retries,
            extra_headers=settings.llm.extra_headers,
        )
        try:
            reply = client.chat("只回复两个字：可用")
            print(f"✅ LLM 连通：{reply[:40]}")
            _probe_translation_quality(client)
        except Exception as exc:
            print(f"⚠️ LLM 不可用（会走降级摘要）：{exc}")
        finally:
            client.close()

    if "--fetch" in argv:
        stats = run_fetch_pipeline(
            timeout=settings.fetcher.timeout_seconds,
            retries=settings.fetcher.max_retries,
            user_agent=settings.fetcher.user_agent,
            dedup_window=settings.storage.dedup_recent_window,
            max_age_days=settings.fetcher.max_age_days,
            max_items_per_source=settings.fetcher.max_items_per_source,
            min_content_chars=settings.fetcher.min_content_chars,
        )
        print(f"✅ 抓取完成：{ {k: v for k, v in stats.items() if k != 'details'} }")
        for detail in stats["details"]:
            if detail.get("status") == "ok":
                print(
                    f"   · {detail['source']}: 抓到 {detail['items']}，入库 {detail['new']}"
                    f"（过期丢弃 {detail.get('stale', 0)}，超量丢弃 {detail.get('over_cap', 0)}）"
                )
            else:
                print(f"   ⚠️ {detail['source']}: {detail['status']} {detail.get('error', '')}".rstrip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
