"""调度：抓取 / 处理 / 日报 / 清理（APScheduler BackgroundScheduler）。"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.ai.client import LLMClient
from app.ai.cluster import merge_duplicates
from app.ai.processor import (
    backfill_briefs,
    backfill_sections,
    backfill_translations,
    process_pending,
    retry_degraded,
)
from app.config import Settings
from app.db import session_scope
from app.fetcher.content import (
    backfill_body_images,
    backfill_content,
    backfill_tail_boilerplate,
    strip_shared_openings,
)
from app.fetcher.images import backfill_images, localize_missing_covers
from app.fetcher.media_store import media_dir_for
from app.fetcher.pipeline import run_fetch_pipeline
from app.models import Article, DailyReport
from app.report.generator import STATUS_REPORTABLE, day_window, generate_daily_report
from app.utils.logger import get_logger, setup_logging
from app.utils.text import now_local

log = get_logger(__name__)

_scheduler: BackgroundScheduler | None = None


def _usage_sink(settings: Settings):
    """把每次 LLM 调用记到设置页的用量里。

    延迟导入 app.web.settings —— 那是 web 层，反向依赖会让 import 环。
    """
    from app.web.settings import record_llm_call

    def sink(**kwargs) -> None:
        record_llm_call(settings, **kwargs)

    return sink


def _llm_client(settings: Settings) -> LLMClient:
    """按当前配置建一个 LLM 客户端。

    统一在这里建，是为了让 ``extra_headers`` 这类网关参数只写一处 ——
    调度里一共有三个地方要发 LLM 请求，漏掉任何一个，那一路就会静默地
    用不到网关要求的请求头。

    ``settings`` 是**当前运行中的那一份**，不是启动时的快照：设置页改完配置
    会就地改写它，所以这里每次都现读，用户改完不用重启就能生效。
    """
    return LLMClient(
        settings.llm.api_base,
        settings.llm.api_key,
        settings.llm.model,
        timeout=settings.llm.timeout_seconds,
        retries=settings.llm.max_retries,
        temperature=settings.llm.temperature,
        extra_headers=settings.llm.extra_headers,
        fallback_models=settings.llm.fallback_models,
        usage_sink=_usage_sink(settings),
    )


def run_fetch_job(settings: Settings) -> dict[str, Any]:
    """抓取所有启用的信源并入库。"""
    stats = run_fetch_pipeline(
        timeout=settings.fetcher.timeout_seconds,
        retries=settings.fetcher.max_retries,
        user_agent=settings.fetcher.user_agent,
        dedup_window=settings.storage.dedup_recent_window,
        max_age_days=settings.fetcher.max_age_days,
        max_items_per_source=settings.fetcher.max_items_per_source,
        min_content_chars=settings.fetcher.min_content_chars,
    )
    log.info("抓取完成：%s", {k: v for k, v in stats.items() if k != "details"})
    return stats


def run_process_job(settings: Settings) -> dict[str, Any]:
    """处理 status=pending 的文章（相关度 → 摘要 → 标签），随后刷新今天的实时日报。"""
    client = _llm_client(settings)
    # 先把「因 LLM 不可用而降级」的文章放回 pending。上游限流会让整篇停在英文，
    # 而 process_pending 只捞 pending —— 不放回来就永远等不到中文版。
    try:
        with session_scope() as session:
            requeued = retry_degraded(session, max_attempts=settings.ai.retry_max_attempts)
        log.info("降级文章重排队：%s", requeued)
    except Exception as exc:
        log.warning("重排降级文章失败：%s", exc)
    try:
        with session_scope() as session:
            stats = process_pending(session, client, settings, limit=settings.ai.batch_size)
    finally:
        client.close()
    log.info("处理完成：%s", stats)
    # 处理完顺手补图（只给相关文章补，每轮限量）与刷新今日日报；任何一步失败都不影响处理结果。
    try:
        run_media_job(settings)
    except Exception as exc:
        log.warning("补齐配图失败：%s", exc)
    # 合并跨源重复：正文已经抓回来了，这时才有足够信号判断「是不是同一件事」
    try:
        run_merge_job(settings)
    except Exception as exc:
        log.warning("合并重复内容失败：%s", exc)
    # 补译正文已拆成独立的 30 分钟任务（run_translate_backfill_job）：
    # 原来挂在这里尾部，而处理批次最多 60 篇、慢网关下一轮要一个多小时，
    # 补译永远排不到 —— 翻译失败的文章实际重试间隔变成「处理总时长 + 2 小时」。
    # 早报推送语：有中文导读的顺手写一段，手机端直接读（不依赖正文翻译开关）
    try:
        if settings.i18n.enabled:
            translator = _llm_client(settings)
            try:
                with session_scope() as session:
                    log.info("补早报推送语：%s", backfill_briefs(session, translator, settings))
            finally:
                translator.close()
    except Exception as exc:
        log.warning("补早报推送语失败：%s", exc)
    # 正文章节结构：存量中文正文排一遍版，每轮 5 篇（每篇 1 次调用）
    try:
        if settings.i18n.enabled:
            translator = _llm_client(settings)
            try:
                with session_scope() as session:
                    log.info("补正文章节结构：%s", backfill_sections(session, translator, settings))
            finally:
                translator.close()
    except Exception as exc:
        log.warning("补正文章节结构失败：%s", exc)
    try:
        run_today_report_job(settings)
    except Exception as exc:  # 日报失败不该把处理统计一起丢掉
        log.warning("今日日报刷新失败：%s", exc)
    return stats


def run_translate_backfill_job(settings: Settings) -> dict[str, Any]:
    """独立补译任务：把缺中文版（标题/导读/正文）的文章轮转着补齐。

    为什么从 run_process_job 尾部拆出来独立跑：处理批次最多 60 篇，
    英文长文的正文翻译是内联的（慢网关下一篇几分钟），整轮处理动辄
    一个多小时 —— 补译排在轮末就永远轮不到，翻译失败的文章实际重试
    间隔变成「处理总时长 + 2 小时」。独立成 30 分钟一跳的任务后，
    失败半小时内必有一次自动重试；没有候选时零 LLM 调用，纯空转查询。
    """
    if not (settings.i18n.enabled and settings.i18n.translate_content):
        return {"candidates": 0, "titles": 0, "digests": 0, "contents": 0, "skipped": 0}
    translator = _llm_client(settings)
    try:
        with session_scope() as session:
            stats = backfill_translations(session, translator, settings)
            if stats.get("candidates"):
                log.info("补译中文正文（独立任务）：%s", stats)
            return stats
    finally:
        translator.close()


def run_content_job(settings: Settings) -> dict[str, Any]:
    """抓取正文全文 —— 必须跑在处理之前，速览和推荐理由的质量都靠它。"""
    if not settings.content.enabled:
        return {"candidates": 0, "filled": 0, "short": 0, "failed": 0}
    with session_scope() as session:
        return backfill_content(
            session,
            limit=settings.content.batch_size,
            timeout=settings.content.timeout_seconds,
            min_chars=settings.content.min_chars,
            media_dir=media_dir_for(settings.db_file),
        )


def run_media_job(settings: Settings) -> dict[str, Any]:
    """给「相关但没图」的文章补首图（RSS 没带图时抓 og:image）。

    挑中的图同步下载到本地：原站防盗链经常裂图，页内展示走自家 /img/。
    正文里的小图（徽章/头像）按真实尺寸丢掉，不再当配图输出。
    """
    if not settings.media.enabled:
        return {"candidates": 0, "filled": 0, "not_found": 0, "failed": 0}
    media_dir = media_dir_for(settings.db_file)
    # 全程只用**一个** session。嵌套 session_scope() 会另开一条连接去写同一批
    # 行，而外层事务只 flush() 过、还没提交，写锁仍被它攥着 —— 于是内层那条
    # 连接只能干等到 busy_timeout（30 秒）再抛 "database is locked"。
    # 实测日志里 21 次锁失败有 18 次来自这个任务。
    with session_scope() as session:
        stats = backfill_images(
            session,
            limit=settings.media.batch_size,
            timeout=settings.media.timeout_seconds,
            media_dir=media_dir,
        )
        # 老数据的首图也补一份本地（只下还没下过的）
        stats["localized_covers"] = localize_missing_covers(
            session, limit=settings.media.batch_size, media_dir=media_dir
        )["localized"]
        # 站点通栏广告（InfoQ 每篇都顶着同一段大会宣传）不算正文，删掉
        strip_shared_openings(session)
        # 正文末尾的 newsletter 促销/相关链接串：库里 291 篇有 33 篇中招
        strip_tail = backfill_tail_boilerplate(session, limit=settings.media.batch_size)
        # 正文内联配图：老数据都没有位置，顺手补齐，详情页才能像原站那样把图插进正文
        inline = backfill_body_images(
            session,
            limit=settings.media.batch_size,
            timeout=settings.media.timeout_seconds,
            media_dir=media_dir,
        )
    return {"cover": stats, "inline": inline, "tail_stripped": strip_tail}


def run_report_job(settings: Settings) -> dict[str, Any]:
    """生成**昨天**的完整日报。

    08:00 跑的时候昨天已经不会再变化，所以出的是一份定稿；今天那份由
    ``run_today_report_job`` 滚动刷新。
    """
    date_str = (now_local() - timedelta(days=1)).strftime("%Y-%m-%d")
    _warn_if_unprocessed(date_str)
    result = generate_daily_report(date_str, settings=settings)
    log.info("日报定稿：%s（%d 篇）", date_str, result["article_count"])
    return result


def _warn_if_unprocessed(date_str: str) -> None:
    """定稿前提醒：这一天还有没处理完的文章吗？

    处理有每轮条数上限（``ai.batch_size``），冷启动或信源暴增时可能上一轮没跑完；
    这些文章既进不了当天的定稿（时间已过），也不会出现在第二天的日报里 —— 只能靠提醒。
    """
    start, end = day_window(date_str)
    with session_scope() as session:
        pending = session.execute(
            select(func.count(Article.id)).where(
                Article.status == "pending",
                Article.published_at >= start,
                Article.published_at < end,
            )
        ).scalar()
    if pending:
        log.warning(
            "%s 还有 %d 篇文章没处理完，这份日报不包含它们；可调大 config/settings.yaml 的 ai.batch_size",
            date_str,
            pending,
        )


def run_today_report_job(settings: Settings) -> dict[str, Any]:
    """滚动刷新**今天**的日报（只统计到当前时刻）。"""
    now = now_local()
    return generate_daily_report(now.strftime("%Y-%m-%d"), until=now, settings=settings)


def run_backfill_reports(settings: Settings, *, days: int = 7) -> list[str]:
    """补齐最近几天里缺失、且**当天确有内容**的日报，返回补上的日期列表。

    服务停机几天再起来时，只有 08:00 那一次定时任务，中间的日期会永远缺一份日报
    （文章还在库里，只是没人去生成）。启动时补一次就齐了。

    刻意跳过「当天没有任何进日报内容」的日期：刚装好的空库不该在历史列表里
    摆出一串 0 篇的日报。
    """
    today = now_local()
    with session_scope() as session:
        since = (today - timedelta(days=days)).strftime("%Y-%m-%d")
        existing = {
            row[0]: row[1]
            for row in session.execute(
                select(DailyReport.date, DailyReport.article_count).where(
                    DailyReport.date >= since
                )
            ).all()
        }
    filled: list[str] = []
    for offset in range(1, days + 1):
        date_str = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        live = _reportable_count(date_str)
        if live <= 0:
            # 刚装好的空库不该在历史列表里摆出一串 0 篇的日报
            continue
        stored = existing.get(date_str)
        # 已有的那份只在「条数还对得上」时保留。早先这里对已有的日期一律
        # 跳过，于是定稿之后再补处理完的文章、或者把重复项合并掉之后，
        # 存下来的 article_count 与实际能进日报的条数就永久对不上
        # （实测 8 天里有 2 天不一致，其中一天写着 2 篇、实际能列 12 篇）。
        if stored is not None and int(stored) == live:
            continue
        try:
            generate_daily_report(date_str, settings=settings)
        except Exception as exc:  # 补不齐不该影响启动
            log.warning("补齐日报失败：%s（%s）", date_str, exc)
            continue
        filled.append(date_str)
    if filled:
        log.info("已刷新 %d 天的日报：%s", len(filled), "、".join(filled))
    return filled


def _rowcount(result: object) -> int:
    """DML 影响的行数。SQLAlchemy 的类型标注里 Result 没有 rowcount，
    运行时对 UPDATE/DELETE 是有的，所以只能 getattr（与本文件既有写法一致）。"""
    return int(getattr(result, "rowcount", 0) or 0)


def repair_inconsistent_rows(session: Session) -> dict[str, int]:
    """修掉历史遗留的自相矛盾行，返回各类修复条数。

    1. **relevance=0 却留着分数。** 模型回 "no 90" 时会写出 relevance=0 却
       score=90 的行：页面显示「AI 评分 90」，而任何按 score 排序或筛选的下游
       都会把一篇已经不进日报的文章当成高价值内容。
    2. **published_at 晚于 created_at。** 逻辑上不可能 —— 发布时间不可能比入库
       时间还晚。成因是某些源把本地时间当成了 GMT（实测 InfoQ 会让文章凭空
       提前 7.5 小时），于是它被算进了错误的那一天日报。
    """
    stats: dict[str, int] = {}
    cleared = session.execute(
        update(Article)
        .where(Article.relevance == 0, Article.score.isnot(None))
        .values(score=None)
    )
    if _rowcount(cleared):
        stats["score_cleared"] = _rowcount(cleared)

    # 用 created_at 当上限钳回：它至少是「我们确实在那个时刻拿到了这篇文章」
    now = now_local()
    future = session.execute(
        update(Article)
        .where(Article.published_at > Article.created_at, Article.created_at.isnot(None))
        .values(published_at=Article.created_at)
    )
    if _rowcount(future):
        stats["future_dated_clamped"] = _rowcount(future)

    still_future = session.execute(
        update(Article).where(Article.published_at > now).values(published_at=now)
    )
    if _rowcount(still_future):
        stats["future_dated_to_now"] = _rowcount(still_future)

    if stats:
        log.info("修好了历史遗留的矛盾行：%s", "、".join(f"{k}={v}" for k, v in stats.items()))
    return stats


def _reportable_count(date_str: str) -> int:
    """这一天当前有多少篇**能进日报**的文章（与页面列表同一套口径）。"""
    try:
        start, end = day_window(date_str)
    except (ValueError, OverflowError):
        return 0
    with session_scope() as session:
        return int(
            session.execute(
                select(func.count(Article.id)).where(
                    Article.relevance == 1,
                    Article.status.in_(STATUS_REPORTABLE),
                    Article.duplicate_of.is_(None),
                    Article.published_at >= start,
                    Article.published_at < end,
                )
            ).scalar_one()
        )


def run_merge_job(settings: Settings) -> dict[str, Any]:
    """把「多家源报道同一件事」的文章合并成一条。

    入库时的去重只比 link 与标题，跨源转载的标题差别很大（Apple 磁盘访问那条
    被TechCrunch / The Verge / Ars Technica 各写了一遍），只能等处理完、正文
    抓回来之后再判一次。
    """
    if not settings.merge.enabled:
        return {"candidates": 0, "pairs": 0, "merged": 0, "kept": 0}
    client = _llm_client(settings)
    try:
        with session_scope() as session:
            stats = merge_duplicates(session, client, settings, limit=settings.merge.window)
    except Exception as exc:
        log.warning("合并重复内容失败：%s", exc)
        return {"candidates": 0, "pairs": 0, "merged": 0, "kept": 0}
    finally:
        client.close()
    if stats["merged"]:
        log.info("合并重复内容 %d 条（候选 %d 篇、判定 %d 对）", stats["merged"], stats["candidates"], stats["pairs"])
    return stats


def run_cleanup_job(settings: Settings) -> dict[str, int]:
    """删除超过保留期的文章与日报。"""
    cutoff_date = (now_local() - timedelta(days=settings.storage.retention_days)).strftime("%Y-%m-%d")
    with session_scope() as session:
        # 先把历史遗留的矛盾行修好，再谈删除
        repair_inconsistent_rows(session)
        # 先断开指向「即将被删掉的文章」的 duplicate_of，再删。
        # 不做这一步的话：主条目先到期被删掉、藏稿还留着，于是藏稿指向一个
        # 虚空 id —— primary_of() 查不到，它就从主页、日报、搜索里彻底消失，
        # 而且没有任何入口能找回。清理是按 created_at 删的，而合并窗口是
        # 7 天，所以这种跨越保留期边界的主/藏稿组合是必然会出现的。
        session.execute(
            update(Article)
            .where(
                Article.duplicate_of.isnot(None),
                Article.duplicate_of.notin_(select(Article.id)),
            )
            .values(duplicate_of=None)
        )
        # created_at 是 DATETIME，cutoff_date 是 'YYYY-MM-DD' 字符串：直接比
        # 的话 SQLite 会把 '2026-09-05' 当成 '2026-09-05 00:00:00'，
        # 于是当天 00:00 整的那篇也被删掉 —— 实际保留期少了一天。
        # 显式补成当天零点，语义就和「早于这一天」一致了。
        cutoff_dt = datetime.combine(datetime.strptime(cutoff_date, "%Y-%m-%d").date(), time.min)
        articles_result = session.execute(
            delete(Article).where(Article.created_at < cutoff_dt)
        )
        reports_result = session.execute(delete(DailyReport).where(DailyReport.date < cutoff_date))
        removed_articles = int(getattr(articles_result, "rowcount", 0) or 0)
        removed_reports = int(getattr(reports_result, "rowcount", 0) or 0)
        # 图片是按内容哈希落盘、从不清理的：实测一天涨约 11MB，
        # 保留期一过文章没了图还留着，纯浪费磁盘。清掉没人再引用的。
        removed_images = _prune_orphan_images(settings)
    log.info(
        "清理完成：文章 %d 条、日报 %d 份、配图 %d 个（早于 %s）",
        removed_articles, removed_reports, removed_images, cutoff_date,
    )
    return {"articles": removed_articles, "reports": removed_reports, "images": removed_images}


def _prune_orphan_images(settings: Settings) -> int:
    """删掉没有任何文章引用的本地图片，返回删除数量。

    ``save_image`` 用内容哈希命名、只在文件已存在时跳过写入，所以文件名不会
    重复；也不会有人去删它们 —— 于是 ``data/img`` 只增不减（实测 17 小时涨到
    190MB）。这里以数据库里的引用为准做一次对账。
    """
    media_dir = media_dir_for(settings.db_file)
    if not media_dir.is_dir():
        return 0
    referenced: set[str] = set()
    try:
        with session_scope() as session:
            for column in (Article.media_map, Article.image_urls, Article.body_images):
                for raw in session.execute(select(column)).scalars():
                    referenced.update(_image_names(str(raw or "")))
    except Exception as exc:  # 对账失败就不要删，宁可留着
        log.warning("配图对账失败，本次不清理：%s", exc)
        return 0
    removed = 0
    for path in media_dir.iterdir():
        if not path.is_file() or path.name in referenced:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def _image_names(raw: str) -> set[str]:
    """从 media_map / image_urls / body_images 的 JSON 里取出本地文件名。"""
    names: set[str] = set()
    for match in re.finditer(r"([0-9a-f]{40}\.(?:jpe?g|png|webp|gif|avif))", raw):
        names.add(match.group(1))
    return names


def _daily_trigger(hhmm: str) -> CronTrigger:
    hour, _, minute = hhmm.partition(":")
    return CronTrigger(hour=int(hour or 8), minute=int(minute or 0))


# 三个写库任务之间的错峰间隔（秒）。抓完等一会儿再补正文、补完再处理，
# 让每一轮都有时间把事务提交掉，而不是三条连接同时抢 SQLite 的写锁。
CONTENT_JOB_OFFSET_SECONDS = 120
PROCESS_JOB_OFFSET_SECONDS = 300


def _offset_trigger(
    trigger: CronTrigger | IntervalTrigger, seconds: int
) -> CronTrigger | IntervalTrigger:
    """把一个触发器整体推迟若干秒，让几个任务不要挤在同一时刻。"""
    try:
        return trigger + timedelta(seconds=seconds)
    except TypeError:  # pragma: no cover - APScheduler 换了实现时的兜底
        log.warning("触发器不支持偏移，任务可能同时启动")
        return trigger


def _fetch_trigger(settings: Settings) -> CronTrigger | IntervalTrigger:
    """配了 fetch_cron 就按每天固定时刻跑，否则按小时轮询。"""
    if settings.schedule.fetch_cron.strip():
        return CronTrigger.from_crontab(settings.schedule.fetch_cron.strip(), timezone="Asia/Shanghai")
    return IntervalTrigger(seconds=int(settings.schedule.fetch_interval_hours * 3600))


def _process_trigger(settings: Settings) -> CronTrigger | IntervalTrigger:
    if settings.schedule.process_cron.strip():
        return CronTrigger.from_crontab(settings.schedule.process_cron.strip(), timezone="Asia/Shanghai")
    return IntervalTrigger(seconds=int(settings.schedule.fetch_interval_hours * 3600))


def build_scheduler(settings: Settings) -> BackgroundScheduler:
    """按配置装配四个任务（不启动）。"""
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    # 三个任务必须**错开**。IntervalTrigger 是一个具体时刻，把同一个对象传给
    # 多个 add_job 就等于让它们在同一秒起跑（实测日志里三个任务在 15 毫秒内
    # 一起启动），于是抓取在插文章、正文在读同一批行、处理在写同一批行 ——
    # 21 次 "database is locked" 里有一批就是这么来的，日报写入的 3 次重试
    # 也被耗光，当天的滚动日报静默没刷新。
    scheduler.add_job(run_fetch_job, _fetch_trigger(settings), args=[settings], id="fetch_job", replace_existing=True)
    scheduler.add_job(
        run_content_job,
        _offset_trigger(_fetch_trigger(settings), CONTENT_JOB_OFFSET_SECONDS),
        args=[settings],
        id="content_job",
        replace_existing=True,
    )
    scheduler.add_job(
        run_process_job,
        _offset_trigger(_process_trigger(settings), PROCESS_JOB_OFFSET_SECONDS),
        args=[settings],
        id="process_job",
        replace_existing=True,
    )
    # 补译独立高频任务：不等整轮处理（见 run_translate_backfill_job 的说明）。
    # 与 process_job 并发是安全的：两边各自短事务写库，SQLite 写锁最多让
    # 其中一方重试；同一篇文章被两边同时翻的最坏结果是多花一次调用，
    # 落库时后写覆盖先写，内容一致性不受影响。
    scheduler.add_job(
        run_translate_backfill_job,
        IntervalTrigger(seconds=1800),
        args=[settings],
        id="translate_backfill_job",
        replace_existing=True,
    )
    scheduler.add_job(
        run_report_job,
        _daily_trigger(settings.schedule.daily_report_time),
        args=[settings],
        id="report_job",
        replace_existing=True,
    )
    scheduler.add_job(
        run_cleanup_job,
        _daily_trigger(settings.schedule.cleanup_time),
        args=[settings],
        id="cleanup_job",
        replace_existing=True,
    )
    return scheduler


def start_scheduler(settings: Settings, *, log_file: object | None = None) -> BackgroundScheduler:
    global _scheduler
    setup_logging(log_file=log_file)  # type: ignore[arg-type]
    if _scheduler is not None and _scheduler.running:
        return _scheduler
    _scheduler = build_scheduler(settings)
    _scheduler.start()
    fetch_plan = settings.schedule.fetch_cron.strip() or f"每 {settings.schedule.fetch_interval_hours} 小时"
    log.info(
        "调度器已启动：抓取/处理 %s，处理后刷新今日日报；昨天日报定稿 %s；清理 %s",
        fetch_plan,
        settings.schedule.daily_report_time,
        settings.schedule.cleanup_time,
    )
    return _scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("调度器已停止")
    _scheduler = None
