"""日报模块测试：生成、覆盖、时间窗口、降级文章、空数据、清理。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.config import ConfigError, Settings, load_settings
from app.db import session_scope
from app.models import DailyReport
from app.report.generator import day_window, generate_daily_report, render_markdown
from app.scheduler import (
    build_scheduler,
    run_backfill_reports,
    run_cleanup_job,
    run_report_job,
    run_today_report_job,
)
from app.utils.text import now_local
from tests.conftest import CONFIG_DIR, ENV, make_article

BAD_TIME_DIR = CONFIG_DIR.parent / "tests" / "fixtures" / "bad_schedule"

NOW = now_local()
TODAY = NOW.strftime("%Y-%m-%d")
YESTERDAY = (NOW - timedelta(days=1)).strftime("%Y-%m-%d")


def _at(days: int = 0, hour: int = 12):
    """某个自然日的第 N 天 + 指定小时（北京时间）。"""
    return (NOW - timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)


def _seed(session, **kwargs):
    return make_article(session, **kwargs)


def test_generate_daily_report_filters_and_renders(seeded_db, settings: Settings):
    with session_scope() as session:
        _seed(session, title="相关的第一条", link="https://example.com/1", summary="摘要一", tags="AI,芯片")
        _seed(session, title="相关的第二条", link="https://example.com/2", summary="摘要二", tags="开源")
        _seed(session, title="不相关的", link="https://example.com/3", relevance=0)
        _seed(session, title="昨天的", link="https://example.com/4", published_at=now_local() - timedelta(days=1))
        _seed(session, title="还没处理的", link="https://example.com/5", status="pending")

    with session_scope() as session:
        result = generate_daily_report(TODAY, session=session, settings=settings)

    assert result["article_count"] == 2
    assert "相关的第一条" in result["content_md"]
    assert "调研方向：AI Agent, 芯片" in result["content_md"]
    assert "不相关的" not in result["content_md"]
    assert "<html" in result["content_html"]

    with session_scope() as session:
        rows = list(session.execute(select(DailyReport)).scalars())
        assert len(rows) == 1
        assert rows[0].date == TODAY


def test_generate_daily_report_overwrites_same_date(seeded_db, settings: Settings):
    with session_scope() as session:
        _seed(session, title="第一版", link="https://example.com/10")
    with session_scope() as session:
        generate_daily_report(TODAY, session=session, settings=settings)

    with session_scope() as session:
        _seed(session, title="第二版", link="https://example.com/11")
    with session_scope() as session:
        second = generate_daily_report(TODAY, session=session, settings=settings)

    assert second["article_count"] == 2
    with session_scope() as session:
        rows = list(session.execute(select(DailyReport)).scalars())
        assert len(rows) == 1  # 同一天只留一份
        assert "第二版" in rows[0].content_md


def test_generate_daily_report_empty_day(seeded_db, settings: Settings):
    with session_scope() as session:
        result = generate_daily_report("2000-01-01", session=session, settings=settings)
    assert result["article_count"] == 0
    assert "没有与调研方向相关" in result["content_md"]


def test_run_report_job_finalizes_whole_previous_day(seeded_db, settings: Settings):
    """08:00 的日报出的是「昨天」，必须覆盖昨天 00:00 到 23:59 的全部内容。

    回归用例：早先的实现按「生成当天 + 只取到生成时刻」筛，当天下午和晚上的文章
    永远不属于任何一份日报。
    """
    with session_scope() as session:
        _seed(session, title="昨天凌晨的", link="https://example.com/21", published_at=_at(days=1, hour=0))
        _seed(session, title="昨天中午的", link="https://example.com/22", published_at=_at(days=1, hour=12))
        _seed(session, title="昨天深夜的", link="https://example.com/23", published_at=_at(days=1, hour=23))

    result = run_report_job(settings)

    assert result["date"] == YESTERDAY
    assert result["article_count"] == 3
    assert "昨天深夜的" in result["content_md"]
    assert "凌晨的" in result["content_md"]


def test_run_today_report_job_is_rolling_to_now(seeded_db, settings: Settings):
    """今天这份只统计到当前时刻，之后发布的还不算数。"""
    with session_scope() as session:
        _seed(session, title="今天早些时候", link="https://example.com/24", published_at=now_local())
        _seed(
            session,
            title="今天稍晚才发布",
            link="https://example.com/25",
            published_at=now_local() + timedelta(hours=3),
        )

    result = run_today_report_job(settings)

    assert result["date"] == TODAY
    assert result["article_count"] == 1
    assert "今天早些时候" in result["content_md"]
    assert "今天稍晚才发布" not in result["content_md"]


def test_day_window_covers_full_day_and_respects_until():
    start, end = day_window("2026-09-29")
    assert start.strftime("%Y-%m-%d %H:%M") == "2026-09-29 00:00"
    assert end.strftime("%Y-%m-%d %H:%M") == "2026-09-30 00:00"

    from datetime import datetime

    cut = datetime(2026, 9, 29, 8, 0)
    start2, end2 = day_window("2026-09-29", until=cut)
    assert (start2, end2) == (start, cut)


def test_report_includes_degraded_articles(seeded_db, settings: Settings):
    """LLM 不可用时的降级文章也必须进日报，否则「不影响日报产出」是空话。"""
    with session_scope() as session:
        _seed(session, title="降级文章", link="https://example.com/26", summary="正文开头前 200 字", status="failed")
        _seed(session, title="未处理文章", link="https://example.com/27", status="pending")

    with session_scope() as session:
        result = generate_daily_report(TODAY, session=session, settings=settings)

    assert result["article_count"] == 1
    assert result["degraded_count"] == 1
    assert "降级文章" in result["content_md"]
    assert "未处理文章" not in result["content_md"]
    assert "降级：LLM 不可用" in result["content_md"]
    assert "降级：LLM 不可用" in result["content_html"]


def test_render_markdown_shape():
    markdown = render_markdown(
        "2026-09-29",
        "AI Agent",
        [{"title": "T", "source": "S", "summary": "SU", "tags": "TG", "link": "L", "published_at": ""}],
    )
    assert markdown.startswith("# Show Me the Money 日报 · 2026-09-29")
    assert "> 调研方向：AI Agent" in markdown
    assert "## 1. T" in markdown
    assert "- 链接：L" in markdown


def test_run_report_job_uses_global_session(seeded_db, settings: Settings):
    """定时任务走全局 session，不需要外部再传。"""
    with session_scope() as session:
        _seed(session, title="定时任务生成的文章", link="https://example.com/20", published_at=_at(days=1))
    result = run_report_job(settings)
    assert result["article_count"] == 1


def test_cleanup_removes_expired_rows(seeded_db, settings: Settings):
    old = now_local() - timedelta(days=settings.storage.retention_days + 5)
    with session_scope() as session:
        _seed(session, title="过期的", link="https://example.com/30", published_at=old, created_at=old)
        _seed(session, title="新鲜的", link="https://example.com/31")
        session.add(
            DailyReport(date="2000-01-01", content_md="旧", content_html="<p>旧</p>", article_count=1, created_at=old)
        )

    stats = run_cleanup_job(settings)
    assert stats["articles"] == 1
    assert stats["reports"] == 1

    with session_scope() as session:
        assert session.query(DailyReport).count() == 0


def test_scheduler_defaults_to_interval_triggers(settings: Settings):
    jobs = {job.id: type(job.trigger).__name__ for job in build_scheduler(settings).get_jobs()}
    assert jobs == {
        "fetch_job": "IntervalTrigger",
        "process_job": "IntervalTrigger",
        "report_job": "CronTrigger",
        "cleanup_job": "CronTrigger",
    }


def test_scheduler_switches_to_daily_cron_when_configured(settings: Settings):
    settings.schedule.fetch_cron = "0 7 * * *"
    settings.schedule.process_cron = "30 7 * * *"
    scheduler = build_scheduler(settings)
    jobs = {job.id: type(job.trigger).__name__ for job in scheduler.get_jobs()}
    assert jobs["fetch_job"] == "CronTrigger"
    assert jobs["process_job"] == "CronTrigger"
    # 抓取每天 07:00、处理 07:30，日报仍在 08:00
    assert "hour='7', minute='0'" in str(scheduler.get_job("fetch_job").trigger)
    assert "hour='7', minute='30'" in str(scheduler.get_job("process_job").trigger)
    assert "hour='8', minute='0'" in str(scheduler.get_job("report_job").trigger)


def test_backfill_reports_fills_missing_days(seeded_db, settings: Settings):
    """停机几天后重启：中间缺失的日报要在启动时补齐。"""
    with session_scope() as session:
        for offset in (1, 3):
            _seed(session, title=f"{offset} 天前的", link=f"https://example.com/bf{offset}",
                  published_at=_at(days=offset))

    missing = run_backfill_reports(settings, days=7)

    assert (NOW - timedelta(days=1)).strftime("%Y-%m-%d") in missing
    assert (NOW - timedelta(days=3)).strftime("%Y-%m-%d") in missing
    with session_scope() as session:
        dates = {row.date for row in session.execute(select(DailyReport)).scalars()}
    assert (NOW - timedelta(days=1)).strftime("%Y-%m-%d") in dates
    assert (NOW - timedelta(days=3)).strftime("%Y-%m-%d") in dates


def test_backfill_reports_is_idempotent(seeded_db, settings: Settings):
    """已经有日报的日期不会被重复生成。"""
    with session_scope() as session:
        _seed(session, title="已有", link="https://example.com/bf-1", published_at=_at(days=1))
    with session_scope() as session:
        generate_daily_report(YESTERDAY, session=session, settings=settings)

    missing = run_backfill_reports(settings, days=7)

    assert YESTERDAY not in missing
    with session_scope() as session:
        yesterday_rows = session.query(DailyReport).filter(DailyReport.date == YESTERDAY).count()
    assert yesterday_rows == 1


def test_settings_reject_bad_schedule_time():
    """调度时间写成「早上八点」这种自然语言，要报出人话而不是 int() 崩溃。"""
    with pytest.raises(ConfigError, match="daily_report_time"):
        load_settings(env=ENV, config_dir=BAD_TIME_DIR)


def test_report_job_warns_on_unprocessed_articles(seeded_db, settings: Settings, caplog):
    """定稿时若前一天还有 pending，必须在日志里提醒（否则这些文章会被静默丢掉）。"""
    import logging

    with session_scope() as session:
        _seed(session, title="昨天没处理完的", link="https://example.com/w1",
              published_at=_at(days=1), status="pending")

    with caplog.at_level(logging.WARNING):
        result = run_report_job(settings)

    assert result["article_count"] == 0  # pending 不进日报
    assert any("batch_size" in record.message for record in caplog.records)


def test_backfill_skips_days_without_articles(seeded_db, settings: Settings):
    """空库不该被补出一串 0 篇日报（那会让历史列表很难看）。"""
    with session_scope() as session:
        _seed(session, title="唯一有内容的一天", link="https://example.com/only", published_at=_at(days=2))

    filled = run_backfill_reports(settings, days=7)

    assert filled == [(NOW - timedelta(days=2)).strftime("%Y-%m-%d")]
    with session_scope() as session:
        assert session.query(DailyReport).count() == 1
