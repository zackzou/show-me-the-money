"""Web / API 测试：JSON 接口、页面路由、RSS 输出。"""

from __future__ import annotations

from urllib.parse import quote

from app.config import Settings
from app.db import session_scope
from app.report.generator import generate_daily_report
from app.utils.text import now_local
from app.web.routes import PAGE_SIZE

from .conftest import make_article

TODAY = now_local().strftime("%Y-%m-%d")


def test_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["research_topics"] == ["AI Agent", "芯片"]
    assert payload["database"] == "ok"
    assert set(payload) >= {"version", "sources", "articles", "reports"}


def test_articles_endpoint(client, seeded_db):
    with session_scope() as session:
        make_article(session, title="接口测试文章", link="https://example.com/api-1")

    response = client.get(f"/api/articles?date={TODAY}")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["title"] == "接口测试文章"
    assert payload[0]["status"] == "processed"

    assert client.get("/api/articles?date=1999-01-01").json() == []


def test_reports_endpoints(client, settings: Settings, seeded_db):
    assert client.get("/api/reports").json() == []
    assert client.get(f"/api/reports/{TODAY}").status_code == 404

    with session_scope() as session:
        make_article(session, title="报告用文章", link="https://example.com/api-2")
    with session_scope() as session:
        generate_daily_report(TODAY, session=session, settings=settings)

    listing = client.get("/api/reports").json()
    assert len(listing) == 1
    assert listing[0]["date"] == TODAY

    detail = client.get(f"/api/reports/{TODAY}")
    assert detail.status_code == 200
    assert "报告用文章" in detail.json()["content_md"]


def test_html_pages(client, settings: Settings, seeded_db):
    home = client.get("/")
    assert home.status_code == 200
    assert "Show Me the Money" in home.text
    assert "这一天还没有筛选出相关报道" in home.text

    assert client.get("/daily/1999-01-01").status_code == 404
    assert client.get("/archive").status_code == 200

    with session_scope() as session:
        make_article(session, title="页面用文章", link="https://example.com/api-3")
    with session_scope() as session:
        generate_daily_report(TODAY, session=session, settings=settings)

    assert "页面用文章" in client.get("/").text
    assert "页面用文章" in client.get(f"/daily/{TODAY}").text
    assert TODAY in client.get("/archive").text


def test_rss_output(client, settings: Settings, seeded_db):
    assert client.get("/rss").status_code == 404

    with session_scope() as session:
        make_article(session, title="RSS 用文章", link="https://example.com/api-4")
    with session_scope() as session:
        generate_daily_report(TODAY, session=session, settings=settings)

    response = client.get("/rss")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/rss+xml")
    assert response.text.startswith("<?xml")
    # 每篇文章一个条目，而不是整份日报塞进一个 item
    assert "RSS 用文章（标签A、标签B）" in response.text
    assert response.text.count("<item>") == 1


def test_unknown_report_date_returns_404(client):
    assert client.get("/api/reports/1999-01-01").status_code == 404


def test_api_and_pages_include_degraded_articles(client, seeded_db):
    """页面、JSON 接口与日报口径一致：降级文章都该出现。"""
    with session_scope() as session:
        make_article(session, title="降级接口文章", link="https://example.com/api-5", status="failed")

    payload = client.get(f"/api/articles?date={TODAY}").json()
    assert [row["title"] for row in payload] == ["降级接口文章"]

    with session_scope() as session:
        generate_daily_report(TODAY, session=session, settings=client.app.state.settings)
    assert "降级接口文章" in client.get(f"/daily/{TODAY}").text


def test_articles_endpoint_defaults_to_beijing_today(client, seeded_db):
    """服务器本地时区不是北京时间时（如容器 UTC），也不能把「今天」算错。"""
    with session_scope() as session:
        make_article(session, title="默认日期文章", link="https://example.com/api-6")

    response = client.get("/api/articles")
    assert response.status_code == 200
    assert [row["title"] for row in response.json()] == ["默认日期文章"]


def test_invalid_date_does_not_return_500(client, seeded_db):
    """外部能直接触发的参数：非法日期必须 4xx，不能 500。"""
    assert client.get("/api/articles?date=2026-13-45").status_code == 400
    assert client.get("/daily/2026-13-45").status_code == 404
    assert client.get("/rss?date=2026-13-45").status_code == 404
    assert client.get("/?page=999").status_code == 200  # 越界页码夹回最后一页


def test_summary_markdown_stripped_in_page_and_rss(client, seeded_db):
    with session_scope() as session:
        make_article(session, title="Markdown 摘要", link="https://example.com/md2",
                     summary="**结论：** 重点在这里")
    with session_scope() as session:
        generate_daily_report(TODAY, session=session, settings=client.app.state.settings)

    assert "**结论：**" not in client.get(f"/daily/{TODAY}").text
    assert "**" not in client.get("/rss").text


def test_story_page_previews_in_page(client, settings: Settings, seeded_db):
    """单篇页：左栏来源信息 + 右栏 AI 导读 + 完整正文，原站链接仍保留。"""
    with session_scope() as session:
        article = make_article(
            session,
            title="页内速览测试",
            link="https://example.com/story-1",
            summary="一句摘要",
            digest="10月3日，某公司发布了 X，带来 Y 的变化。这是导语式速览。",
            reason="披露了具体数字，读者可据此判断影响面。",
            content_full="第一段正文内容。\n\n第二段正文内容。",
            image_urls='["https://cdn.example.com/1.jpg", "https://cdn.example.com/2.jpg"]',
        )
        article_id = article.id

    page = client.get(f"/story/{article_id}")
    assert page.status_code == 200
    assert "页内速览测试" in page.text
    assert "这是导语式速览" in page.text              # AI 导读在页内可见
    assert "第一段正文内容。" in page.text             # 正文全文逐段展示
    assert "第二段正文内容。" in page.text
    assert "AI 导读" in page.text
    assert ">正文<" in page.text.replace(" ", "")
    assert "打开原文 ↗" in page.text                   # 右栏「打开原文」
    assert "data-shot" in page.text                   # 配图点击页内放大


def test_story_page_404_for_missing(client):
    assert client.get("/story/999999").status_code == 404


def test_home_page_paginates_and_has_theme_toggle(client, seeded_db):
    """首页分页 + 深浅色切换 + 空态文案。"""
    with session_scope() as session:
        for i in range(PAGE_SIZE + 5):
            make_article(session, title=f"分页文章{i}", link=f"https://example.com/p{i}")

    # 时间倒序（同秒则按 id 倒序），每页 PAGE_SIZE 条
    first = client.get("/")
    assert first.status_code == 200
    assert f"分页文章{PAGE_SIZE + 4}" in first.text          # 最新一条在首页
    assert "分页文章0" not in first.text                      # 剩 5 条在第二页
    assert "下一页" in first.text
    # 时间轴结构：日期分组头 + 左侧时间列
    assert "day-head" in first.text
    assert "tl-time" in first.text

    second = client.get("/?page=2")
    assert "分页文章0" in second.text
    assert f"分页文章{PAGE_SIZE + 4}" not in second.text

    # 越界页码夹回最后一页，不报错
    assert client.get("/?page=999").status_code == 200
    assert client.get("/?page=0").status_code in (200, 422)


def test_images_only_on_story_page(client, settings: Settings, seeded_db):
    """配图不再抢列表的版面：列表纯文字，配图放详情页正文之后。"""
    with session_scope() as session:
        make_article(session, title="带图文章", link="https://example.com/img",
                     digest="速览内容", reason="值得看", image_urls='["https://cdn.example.com/x.jpg"]')
    with session_scope() as session:
        generate_daily_report(TODAY, session=session, settings=settings)

    home = client.get("/").text
    assert "推荐理由" in home
    assert "值得看" in home
    assert "cdn.example.com/x.jpg" not in home      # 列表页不放图

    story = client.get("/story/1").text
    assert "文章配图" in story
    assert "cdn.example.com/x.jpg" in story


def test_dark_mode_toggle_present(client):
    assert "smtm-theme" in client.get("/").text
    assert 'classList.toggle("dark")' in client.get("/").text


def test_story_images_are_small_grid(client, settings: Settings, seeded_db):
    """详情页配图用小网格，不能再像之前那样一张长图占满整屏。"""
    with session_scope() as session:
        make_article(session, title="长图文章", link="https://example.com/tall",
                     digest="速览", image_urls='["https://cdn.example.com/tall.jpg"]')
    with session_scope() as session:
        generate_daily_report(TODAY, session=session, settings=settings)

    text = client.get("/story/1").text
    assert "shot-grid" in text
    assert "minmax(190px,1fr)" in text


def test_timeline_groups_by_date_and_shows_score(client, settings: Settings, seeded_db):
    """时间轴结构：日期分组头 + 左侧时间 + 相关度评分 + 推荐理由。"""
    with session_scope() as session:
        make_article(session, title="带评分的文章", link="https://example.com/scored",
                     digest="导语内容", reason="披露了关键数字", score=88)
    with session_scope() as session:
        generate_daily_report(TODAY, session=session, settings=settings)

    text = client.get("/").text
    assert "day-head" in text
    assert "条" in text                      # 组头带条数
    assert "相关度" in text
    assert "88" in text
    assert "推荐理由" in text
    assert "披露了关键数字" in text
    assert "score strong" in text            # ≥70 走强调样式


def test_relative_time_formatting(client, settings: Settings, seeded_db):
    from datetime import timedelta

    from app.utils.text import now_local

    with session_scope() as session:
        make_article(session, title="刚刚的", link="https://example.com/just",
                     published_at=now_local() - timedelta(minutes=3))
        make_article(session, title="三小时前的", link="https://example.com/h3",
                     published_at=now_local() - timedelta(hours=3))
    with session_scope() as session:
        generate_daily_report(TODAY, session=session, settings=settings)

    text = client.get("/").text
    assert "分钟前" in text
    assert "小时前" in text


def test_score_hidden_when_absent(client, settings: Settings, seeded_db):
    """没有评分时不该显示「相关度」占位。"""
    with session_scope() as session:
        make_article(session, title="无评分", link="https://example.com/noscore", digest="导语")
    with session_scope() as session:
        generate_daily_report(TODAY, session=session, settings=settings)

    assert "相关度" not in client.get("/").text


def test_timeline_time_not_truncated_and_dot_aligned(client, settings: Settings, seeded_db):
    """时间列必须放得下 HH:MM（曾被截成「10:3」），且圆点要与竖线对齐。"""
    with session_scope() as session:
        make_article(session, title="时间轴对齐", link="https://example.com/tl", digest="导语")
    with session_scope() as session:
        generate_daily_report(TODAY, session=session, settings=settings)

    text = client.get("/").text
    assert "tl-time" in text
    # 时间列右对齐且禁止换行，宽度留给 5 个字符
    assert "white-space:nowrap" in text
    assert "grid-template-columns:92px 1fr" in text


def test_category_tabs_and_search_box(client, seeded_db):
    """顶部要有分类 tab 与搜索框（图1），并且带 / 快捷键。"""
    home = client.get("/").text
    assert 'class="tabs"' in home
    for name in ("一手", "模型", "产品", "行业", "论文", "教程", "观点"):
        assert f">{name}</a>" in home
    assert 'action="/search"' in home
    assert 'placeholder="搜索标题、摘要…"' in home
    assert "s-key" in home          # 搜索框里的 / 提示
    assert 'if (e.key !== "/")' in home or "e.key !== \"/\"" in home  # / 聚焦快捷键


def test_search_page_renders_results(client, settings: Settings, seeded_db):
    with session_scope() as session:
        make_article(session, title="OpenAI 发布新一代模型", link="https://example.com/s1",
                     digest="速览", reason="理由", category="模型", tags="OpenAI")
        make_article(session, title="无关内容", link="https://example.com/s2", digest="别的")

    page = client.get("/search?q=OpenAI")
    assert page.status_code == 200
    assert '搜索"OpenAI"' in page.text
    assert "OpenAI 发布新一代模型" in page.text
    assert "无关内容" not in page.text
    assert "找到" in page.text
    # 分类 tab 与 最新/全文 切换都要在
    assert 'class="tabs"' in page.text
    assert ">最新<" in page.text
    assert ">全文<" in page.text

    assert client.get("/search").status_code == 200
    assert client.get("/search?q=绝对不存在的词").status_code == 200


def test_home_category_filter(client, settings: Settings, seeded_db):
    with session_scope() as session:
        make_article(session, title="模型类文章", link="https://example.com/f1",
                     digest="速览", category="模型")
        make_article(session, title="行业类文章", link="https://example.com/f2",
                     digest="速览", category="行业")

    page = client.get("/?cat=模型")
    assert "模型类文章" in page.text
    assert "行业类文章" not in page.text


def test_story_has_right_rail_with_topic_and_tags(client, settings: Settings, seeded_db):
    """详情页右栏：打开原文 / 推荐理由 / 主题 / 标签，且都可点（图3）。"""
    with session_scope() as session:
        make_article(session, title="右栏测试", link="https://example.com/rail",
                     digest="导语", reason="因为披露了关键数字", category="行业",
                     topics='["OpenAI / ChatGPT", "Agent 智能体"]', tags="行业动态,Agent")
    with session_scope() as session:
        generate_daily_report(TODAY, session=session, settings=settings)

    text = client.get("/story/1").text
    assert 'class="rail"' in text
    assert "打开原文 ↗" in text
    assert "推荐理由" in text
    assert "因为披露了关键数字" in text
    assert "主题" in text
    assert "OpenAI / ChatGPT" in text
    assert "标签" in text
    assert "#Agent" in text
    # 主题跳搜索、标签跳首页过滤，都要能用
    assert "/search?q=OpenAI" in text
    assert "/?tag=Agent" in text
    # 左侧来源栏保留（图4）
    assert "发布时间" in text


def test_card_shows_category_and_clickable_tags(client, settings: Settings, seeded_db):
    """卡片上要有分类 + 可点标签（图2）。"""
    with session_scope() as session:
        make_article(session, title="卡片标签测试", link="https://example.com/card",
                     digest="速览", reason="理由", score=79, category="行业", tags="行业动态,OpenAI")
    with session_scope() as session:
        generate_daily_report(TODAY, session=session, settings=settings)

    text = client.get("/").text
    assert "AI 评分" in text
    assert "79" in text
    # 分类与标签都是可点链接（中文会被 urlencode 成百分号编码）
    assert f"?cat={quote('行业')}" in text
    assert "?tag=OpenAI" in text
    assert "#OpenAI" in text


def test_story_subtitle_is_not_duplicate_of_digest(client, settings: Settings, seeded_db):
    """斜体位置放分类/主题定位，不该把 AI 导读的内容再重复一遍。"""
    with session_scope() as session:
        make_article(session, title="不重复测试", link="https://example.com/dup",
                     digest="这是导语内容，只该出现一次。", category="行业",
                     topics='["云计算"]')
    with session_scope() as session:
        generate_daily_report(TODAY, session=session, settings=settings)

    text = client.get("/story/1").text
    assert text.count("这是导语内容，只该出现一次。") == 1
    assert "行业 · 云计算" in text
