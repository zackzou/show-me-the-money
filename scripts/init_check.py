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
            max_retries=settings.llm.max_retries,
            extra_headers=settings.llm.extra_headers,
        )
        try:
            reply = client.chat("只回复两个字：可用")
            print(f"✅ LLM 连通：{reply[:40]}")
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
