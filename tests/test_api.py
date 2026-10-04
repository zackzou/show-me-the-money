"""Web / API 测试：JSON 接口、页面路由、RSS 输出。"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import select

from app.config import Settings
from app.db import session_scope
from app.report.generator import generate_daily_report
from app.utils.text import now_local
from app.web.routes import PAGE_SIZE

from .conftest import make_article


def _today() -> str:
    """今天（测试运行时再算一次）。

    模块级常量会在跨午夜时过期：导入时是 10-03，用例里 ``now_local() - 3 分钟``
    已经是 10-04，文章就落在时间窗外面，页面自然是空的 —— 这个坑只在 23:5x 之后跑测试时出现。
    """
    return now_local().strftime("%Y-%m-%d")



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

    response = client.get(f"/api/articles?date={_today()}")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["title"] == "接口测试文章"
    assert payload[0]["status"] == "processed"

    assert client.get("/api/articles?date=1999-01-01").json() == []


def test_reports_endpoints(client, settings: Settings, seeded_db):
    assert client.get("/api/reports").json() == []
    assert client.get(f"/api/reports/{_today()}").status_code == 404

    with session_scope() as session:
        make_article(session, title="报告用文章", link="https://example.com/api-2")
    with session_scope() as session:
        generate_daily_report(_today(), session=session, settings=settings)

    listing = client.get("/api/reports").json()
    assert len(listing) == 1
    assert listing[0]["date"] == _today()

    detail = client.get(f"/api/reports/{_today()}")
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
        generate_daily_report(_today(), session=session, settings=settings)

    assert "页面用文章" in client.get("/").text
    assert "页面用文章" in client.get(f"/daily/{_today()}").text
    assert _today() in client.get("/archive").text


def test_rss_output(client, settings: Settings, seeded_db):
    assert client.get("/rss").status_code == 404

    with session_scope() as session:
        make_article(session, title="RSS 用文章", link="https://example.com/api-4")
    with session_scope() as session:
        generate_daily_report(_today(), session=session, settings=settings)

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

    payload = client.get(f"/api/articles?date={_today()}").json()
    assert [row["title"] for row in payload] == ["降级接口文章"]

    with session_scope() as session:
        generate_daily_report(_today(), session=session, settings=client.app.state.settings)
    assert "降级接口文章" in client.get(f"/daily/{_today()}").text


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
        generate_daily_report(_today(), session=session, settings=client.app.state.settings)

    assert "**结论：**" not in client.get(f"/daily/{_today()}").text
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
            # 长度要过「是否真的有正文」的阈值（见 is_real_body）
            content_full=(
                "第一段正文内容，这里补足到足够长度以通过正文字数门槛的判定。\n\n"
                "第二段正文内容，同样补足长度，确保整段都会被渲染出来。"
            ),
            image_urls='["https://cdn.example.com/1.jpg", "https://cdn.example.com/2.jpg"]',
        )
        article_id = article.id

    page = client.get(f"/story/{article_id}")
    assert page.status_code == 200
    assert "页内速览测试" in page.text
    assert "这是导语式速览" in page.text              # AI 导读在页内可见
    assert "第一段正文内容，这里补足到足够长度" in page.text   # 正文全文逐段展示
    assert "第二段正文内容，同样补足长度" in page.text
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
        generate_daily_report(_today(), session=session, settings=settings)

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
        generate_daily_report(_today(), session=session, settings=settings)

    text = client.get("/story/1").text
    assert "shot-grid" in text
    assert "minmax(190px,1fr)" in text


def test_timeline_groups_by_date_and_shows_score(client, settings: Settings, seeded_db):
    """时间轴结构：日期分组头 + 左侧时间 + 相关度评分 + 推荐理由。"""
    with session_scope() as session:
        make_article(session, title="带评分的文章", link="https://example.com/scored",
                     digest="导语内容", reason="披露了关键数字", score=88)
    with session_scope() as session:
        generate_daily_report(_today(), session=session, settings=settings)

    text = client.get("/").text
    assert "day-head" in text
    assert "条" in text                      # 组头带条数
    assert "相关度" in text
    assert "88" in text
    assert "推荐理由" in text
    assert "披露了关键数字" in text
    assert "score strong" in text            # ≥70 走强调样式


def test_relative_time_formatting(client, settings: Settings, seeded_db):
    """相对时间文案。

    「小时前」直接验格式化函数，不往库里造：``now - 3 小时`` 在 00:0x 跑的时候
    会落到昨天，那篇文章根本不在今天的时间窗里，页面当然没有「小时前」。
    """
    from datetime import timedelta

    from app.utils.text import now_local
    from app.web.routes import _relative

    now = now_local()
    with session_scope() as session:
        make_article(session, title="刚刚的", link="https://example.com/just",
                     published_at=now - timedelta(minutes=3))
    with session_scope() as session:
        generate_daily_report(_today(), session=session, settings=settings)

    text = client.get("/").text
    assert "分钟前" in text
    assert _relative(now - timedelta(hours=3), now) == "3 小时前"
    assert _relative(now - timedelta(days=2), now) == "2 天前"


def test_score_hidden_when_absent(client, settings: Settings, seeded_db):
    """没有评分时不该显示「相关度」占位。"""
    with session_scope() as session:
        make_article(session, title="无评分", link="https://example.com/noscore", digest="导语")
    with session_scope() as session:
        generate_daily_report(_today(), session=session, settings=settings)

    assert "相关度" not in client.get("/").text


def test_timeline_time_not_truncated_and_dot_aligned(client, settings: Settings, seeded_db):
    """时间列必须放得下 HH:MM（曾被截成「10:3」），且圆点要与竖线对齐。"""
    with session_scope() as session:
        make_article(session, title="时间轴对齐", link="https://example.com/tl", digest="导语")
    with session_scope() as session:
        generate_daily_report(_today(), session=session, settings=settings)

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
        generate_daily_report(_today(), session=session, settings=settings)

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
        generate_daily_report(_today(), session=session, settings=settings)

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
        generate_daily_report(_today(), session=session, settings=settings)

    text = client.get("/story/1").text
    assert text.count("这是导语内容，只该出现一次。") == 1
    assert "行业 · 云计算" in text


def test_card_is_whole_clickable_with_pointer_cursor(client, settings: Settings, seeded_db):
    """整卡可点：卡片是定位容器，标题链接用 ::after 铺满，光标是手型。

    同时确认卡内的标签 / 收藏按钮浮在拉伸链接之上，仍然能单独点。
    """
    with session_scope() as session:
        make_article(session, title="整卡可点测试", link="https://example.com/whole",
                     digest="导语内容", reason="理由", category="行业", tags="OpenAI")
    with session_scope() as session:
        generate_daily_report(_today(), session=session, settings=settings)

    text = client.get("/").text
    # 手型光标
    assert "padding:14px 16px; box-shadow:var(--shadow); cursor:pointer" in text
    # 拉伸链接铺满整卡
    assert ".item .t::after { content:\"\"; position:absolute; inset:0;" in text
    # 标题仍是真链接（可右键/可访问），内容由双语宏包一层
    assert '<a href="/story/1"><div class="t">' in text
    assert '<span class="zh">整卡可点测试</span>' in text
    # 卡内可点元素浮到上面
    assert ".item .cats a, .item .bm, .item .more" in text
    assert "z-index:2" in text


def test_card_body_click_does_not_nest_links(client, settings: Settings, seeded_db):
    """不能出现 a 套 a（非法结构），分类与标签是独立链接。"""
    from html.parser import HTMLParser

    with session_scope() as session:
        make_article(session, title="结构测试", link="https://example.com/struct",
                     digest="导语", category="行业", tags="OpenAI,Agent")
    with session_scope() as session:
        generate_daily_report(_today(), session=session, settings=settings)

    class NestChecker(HTMLParser):
        def __init__(self):
            super().__init__()
            self.depth = 0
            self.max_nest = 0

        def handle_starttag(self, tag, attrs):
            if tag == "a":
                self.depth += 1
                self.max_nest = max(self.max_nest, self.depth)

        def handle_endtag(self, tag):
            if tag == "a":
                self.depth -= 1

    parser = NestChecker()
    parser.feed(client.get("/").text)
    assert parser.max_nest == 1, "出现了 a 套 a"


def test_story_page_uses_wide_container_and_toc(client, settings: Settings, seeded_db):
    """详情页要更宽的容器（不然三栏把正文挤窄），正文小标题要能生成目录。"""
    from app.web.routes import mark_headings, table_of_contents

    marked = mark_headings(
        [
            "内部未发布模型 · RL 训练",  # 像小标题
            "这是一段很长的正文内容，用来占位，所以不会被当成小标题来处理掉。",
            "发生了什么",  # 短句 + 无句末标点 → 小标题
            "2026年5月16日",  # 以数字开头 → 不算
            "小结。",  # 以句号结尾 → 不算
        ]
    )
    assert [item["heading"] for item in marked] == [True, False, True, False, False]
    assert [item["text"] for item in table_of_contents(marked)] == ["内部未发布模型 · RL 训练", "发生了什么"]

    with session_scope() as session:
        article = make_article(
            session,
            title="目录测试",
            link="https://example.com/toc",
            digest="导语",
            content_full="发生了什么\n\n这是第一段正文内容，足够长不会被当成标题。\n\n调查与响应\n\n这是第二段正文内容，也足够长。",
        )
        article_id = article.id

    text = client.get(f"/story/{article_id}").text
    assert "wrap wide" in text          # 用了宽容器
    assert "本文目录" in text
    assert "<h3" in text
    # 目录锚点必须都能落到页面上真实存在的 id。
    # 早期版本目录按原文下标生成、正文 id 又和译文撞号（两边都从 0 编号），
    # 点目录会跳错位置甚至跳到不存在的锚点。这里锁死「锚点集合 ⊆ id 集合」。
    import re

    anchors = set(re.findall(r'href="#([^"]+)"', text))
    ids = set(re.findall(r'id="([^"]+)"', text))
    assert anchors, "详情页应该生成本文目录"
    assert anchors <= ids, f"目录锚点没有对应元素：{anchors - ids}"


def test_story_columns_are_balanced(client, seeded_db):
    """三栏比例：正文列要最宽，左右栏窄一些。"""
    text = client.get("/").text
    assert "grid-template-columns:216px minmax(0,1fr) 300px" in text
    assert ".prose p { margin:0 0 1.2em; font-size:18px; line-height:1.8" in text


def test_story_body_never_renders_empty_paragraphs(client, settings: Settings, seeded_db):
    """回归：body_blocks 传成 dict 列表后，模板若还按字符串取，正文会渲染成一片空段落。

    这里锁死「每个段落要么是 h3、要么是有文字的 p」。
    """
    import re

    with session_scope() as session:
        article = make_article(
            session,
            title="正文渲染",
            link="https://example.com/body",
            digest="导语",
            content_full=(
                "事件概要\n\n这是第一段正文，字符数足够不会被判成标题。\n\n"
                "影响范围\n\n这是第二段正文，同样足够长所以也不会被当成标题。"
            ),
        )
        article_id = article.id

    text = client.get(f"/story/{article_id}").text
    prose = text.split('class="prose')[1].split("</div>")[0]
    assert "<h3" in prose
    for para in re.findall(r"<p>(.*?)</p>", prose, re.S):
        assert para.strip(), "出现了空段落"
    assert "这是第一段正文" in prose
    assert "这是第二段正文" in prose
    # 小标题应该同时出现在目录里，且锚点能落到真实 id 上
    anchors = set(re.findall(r'href="#([^"]+)"', text))
    ids = set(re.findall(r'id="([^"]+)"', text))
    assert anchors, "详情页应该生成本文目录"
    assert anchors <= ids, f"目录锚点没有对应元素：{anchors - ids}"


def test_base_script_tags_are_balanced(client):
    """回归：主 <script> 必须闭合。

    之前 base.html 少了一个 </script>，导致 {% block extra %} 被塞进 script 内部 ——
    浏览器会把后面的内容当脚本文本解析，遇到内层 <script> 直接语法报错，
    结果全站 JS 失效（收藏页永远停在「正在读取…」、收藏按钮与 / 快捷键都不工作）。
    """
    import re

    for path in ("/", "/saved", "/search?q=x"):
        text = client.get(path).text
        opens = len(re.findall(r"<script\b", text))
        closes = len(re.findall(r"</script>", text))
        assert opens == closes, f"{path} 的 script 标签不成对：{opens} 开 / {closes} 闭"
        # extra block 的脚本必须落在主脚本之外
        main_end = text.index("</script>")
        assert 'id="saved-root"' not in text or text.index('fetch("/api/articles') > main_end


def test_language_switcher_present(client):
    """顶栏要有 中文 / EN / 双语 三档，默认中文。"""
    text = client.get("/").text
    assert 'data-lang="zh"' in text
    assert 'data-lang="en"' in text
    assert 'data-lang="both"' in text
    assert 'localStorage.getItem("smtm-lang") || "zh"' in text   # 默认中文
    # 纯 CSS 控制显隐，不来回请求服务器
    assert 'html[data-lang="zh"] .en { display:none; }' in text
    assert 'html[data-lang="en"] .zh { display:none; }' in text
    assert 'html[data-lang="both"] .en' in text


def test_card_shows_english_below_chinese(client, settings: Settings, seeded_db):
    with session_scope() as session:
        make_article(session, title="中文标题", title_en="English Title", link="https://example.com/bi",
                     digest="中文导语", digest_en="English digest", category="行业")
    with session_scope() as session:
        generate_daily_report(_today(), session=session, settings=settings)

    text = client.get("/").text
    assert '<span class="zh">中文标题</span>' in text
    assert '<span class="en">English Title</span>' in text
    assert '<span class="zh">中文导语</span>' in text
    assert '<span class="en">English digest</span>' in text
    # 英文在中文之后（双语模式靠 CSS 排上下）
    assert text.index('<span class="zh">中文标题</span>') < text.index('<span class="en">English Title</span>')
    # 没有英文时不要留空壳
    assert '<span class="en"></span>' not in text


def test_story_shows_bilingual_title_and_digest(client, settings: Settings, seeded_db):
    with session_scope() as session:
        article = make_article(session, title="中文大标题", title_en="Big English Title",
                               link="https://example.com/bi2", digest="中文导语", digest_en="English lead")
        article_id = article.id
    with session_scope() as session:
        generate_daily_report(_today(), session=session, settings=settings)

    text = client.get(f"/story/{article_id}").text
    assert '<h1 class="body-title"><span class="zh">中文大标题</span>' in text
    assert '<span class="en">Big English Title</span>' in text
    assert "English lead" in text


def test_archive_expandable_with_titles(client, settings: Settings, seeded_db):
    """历史日报：点日期展开当天标题，并且是通过接口按需加载。"""
    with session_scope() as session:
        make_article(session, title="归档里的文章", link="https://example.com/ar")
    with session_scope() as session:
        generate_daily_report(_today(), session=session, settings=settings)

    text = client.get("/archive").text
    assert "arc-row" in text
    assert 'aria-expanded="false"' in text
    assert f'data-date="{_today()}"' in text
    assert "arc-panel" in text
    # 展开内容走接口按需拉，不一次性把所有标题塞进 HTML
    assert "/api/articles?date=" in text
    assert "归档里的文章" not in text
    # 视觉要素：条形对比、星期、相对时间
    assert "arc-bar" in text
    assert "星期" in text


def test_saved_page_has_working_script(client):
    """收藏页脚本必须落在主 script 之外（否则整段 JS 失效，页面永远转圈）。"""
    text = client.get("/saved").text
    main_end = text.index("</script>")
    assert text.index('fetch("/api/articles') > main_end
    assert "还没有收藏" in text


def test_lang_control_is_rendered(client):
    """语言控件必须真的输出到页面里。

    回归：曾只加了 CSS 和 JS，却忘了插控件，导致 getElementById("lang") 为 null、
    全局脚本在 addEventListener 处抛错，收藏与收藏页一起失效。
    """
    text = client.get("/").text
    assert '<div class="lang" id="lang"' in text
    for value in ("zh", "en", "both"):
        assert f'data-lang="{value}"' in text
    # 取到元素后才绑定，且绑定前判空
    assert 'if (box) box.addEventListener' in text


def test_global_script_survives_missing_element(client):
    """全局脚本对可选元素判空：控件缺失时不应抛错拖垮收藏等逻辑。"""
    text = client.get("/").text
    assert 'if (themeBtn) themeBtn.onclick' in text
    # 每个 attach 前都有判空保护
    assert 'var lb = document.getElementById("lb")' in text
    main = text[text.index("smtm-saved") - 4000:]
    assert "null.addEventListener" not in main
    assert "null.onclick" not in main


def test_bilingual_macro_skips_identical_english(client, settings: Settings, seeded_db):
    """英文信源的标题/导语本来就是英文，不该在双语模式里显示两遍一样的内容。"""
    with session_scope() as session:
        make_article(session, title="An English only headline about agents", link="https://example.com/en",
                     title_en="An English only headline about agents",
                     digest="An English only digest about agents",
                     digest_en="An English only digest about agents", category="行业")
    with session_scope() as session:
        generate_daily_report(_today(), session=session, settings=settings)

    text = client.get("/").text
    # 中英完全相同时只出一个 span，双语模式不会看到两遍一样的话
    assert text.count('<span class="same">An English only headline about agents</span>') == 1
    assert text.count('<span class="same">An English only digest about agents</span>') == 1
    # 关键：不能只输出 .zh。html[data-lang="en"] 会把 .zh 藏掉，
    # 那样切到英文模式标题与导读会直接变成空白 —— 而页面上没有任何东西顶上。
    assert '<span class="zh">An English only headline about agents</span>' not in text
    assert '<span class="en">An English only headline about agents</span>' not in text
    assert '<span class="zh">An English only digest about agents</span>' not in text
    assert '<span class="en">An English only digest about agents</span>' not in text


# ── 详情页：正文翻译、无正文提示、早报片段 ──────────────────────────────────


def test_story_shows_chinese_translation_in_zh_mode(client, settings: Settings, seeded_db):
    """英文原文整篇译成中文后，中文模式读译文、双语模式两段都在、英文模式读原文。"""
    en_body = (
        "Apple says it is changing its macOS privacy settings to stop third-party app "
        "developers from misusing them to access message histories."
    )
    zh_body = (
        "苹果表示将修改 macOS 的隐私设置，阻止第三方应用滥用这些权限读取信息记录。"
    )
    with session_scope() as session:
        article = make_article(
            session, title="Apple changes permissions", link="https://example.com/tr",
            content_full=en_body, content_zh=zh_body,
        )
        article_id = article.id

    text = client.get(f"/story/{article_id}").text
    # 有译文：译文块与原文块都随语言切换（中文模式只看译文，双语模式上下对照）
    assert '<div class="prose zh">' in text
    assert "苹果表示将修改 macOS" in text
    assert '<div class="prose en">' in text
    assert "stop third-party app developers" in text
    # 英文原文不能是不受语言控制的 orig 块，否则中文模式下两段一起显示
    assert '<div class="prose orig">' not in text
    assert "中文译文" in text
    assert "英文原文" in text


def test_story_marks_untranslated_foreign_body(client, settings: Settings, seeded_db):
    """英文正文没有译文时要说清楚，别让人以为页面坏了。"""
    with session_scope() as session:
        article = make_article(
            session, title="Apple changes permissions", link="https://example.com/tr2",
            content_full=(
                "Apple says it is changing its macOS privacy settings to stop third-party app "
                "developers from misusing them to access message histories."
            ),
        )
        article_id = article.id

    text = client.get(f"/story/{article_id}").text
    assert "原文为英文，暂无中文译文" in text
    assert '<div class="prose orig">' in text
    assert '<div class="prose zh">' not in text


def test_story_explains_when_source_has_no_body(client, settings: Settings, seeded_db):
    """Reddit 这类只有标题的帖子：要明说「原文没有正文」，不能渲染空的正文区块。"""
    with session_scope() as session:
        article = make_article(
            session, title="WTF Google getting rid of free models",
            link="https://example.com/rd",
            content_full="", content="submitted by  /u/someone   [link]   [comments]",
        )
        article_id = article.id

    text = client.get(f"/story/{article_id}").text
    assert "原文没有正文" in text
    assert "内容基本都在标题里" in text
    # 不该把模板套话当正文渲染出来
    assert "[comments]" not in text
    assert ">Comments<" not in text


def test_is_real_body_filters_boilerplate():
    from app.fetcher.content import is_real_body

    real = "Apple says it is changing its macOS privacy settings to stop third-party app developers."
    assert is_real_body(real) is True
    # Reddit 的 RSS description：清掉噪音后只剩用户名，不算正文
    assert is_real_body("submitted by  /u/Ashamed-Principle40   [link]   [comments]") is False
    assert is_real_body("Comments") is False
    assert is_real_body("") is False
    assert is_real_body(None) is False


def test_brief_digest_keeps_three_to_five_lines():
    """早报片段要压到三到五行：按句切，不能硬切断在半句上。"""
    from app.utils.text import brief_digest

    long = "第一句话在这里说清楚了。" * 10   # 120 字，必定超出 108 的上限
    brief = brief_digest(long)
    assert 60 <= len(brief) <= 108
    # 按句收住，所以末句是完整的；不会切在半句上
    assert brief.endswith("。")
    assert brief.count("第一句话在这里说清楚了。") < 10
    # 回归：一个标点都没有的长文本，早先是「硬截断 + 省略号」，
    # 推送到手机上就是半句话（「…Only $30 more than the wireless charging ver…」）。
    # 现在必须断在子句边界并补句号，绝不以省略号收尾。
    no_punct = brief_digest("超长" * 60)
    assert not no_punct.endswith("…")
    assert no_punct.endswith("。")
    assert len(no_punct) <= 108 * 2

    short = "只有一句话的摘要。"
    assert brief_digest(short) == short
    assert brief_digest("") == ""
    assert brief_digest(None) == ""
    assert " " not in brief_digest("第一句。\n\n第二句。")


def test_digest_button_loads_over_api_without_duplicate_text(client, settings: Settings, seeded_db):
    """早报片段按钮不跳转；数据点开时才取，HTML 里不重复塞一遍摘要。"""
    with session_scope() as session:
        article = make_article(
            session, title="早报片段测试", link="https://example.com/dg",
            digest="这是导语内容，只该出现一次。", score=88, category="行业",
        )
        article_id = article.id

    text = client.get(f"/story/{article_id}").text
    assert 'class="digest-btn" id="digest-btn"' in text
    assert f'data-article-id="{article_id}"' in text
    # 浮层是 hidden 的空壳，内容走接口
    assert 'id="dg-mask" hidden' in text
    assert 'id="dg-card"' in text
    assert text.count("这是导语内容，只该出现一次。") == 1
    # 不跳转：不带 href，也不导航
    assert "e.preventDefault()" in text


def test_article_detail_api(client, settings: Settings, seeded_db):
    """单篇详情接口：早报片段与收藏页都靠它按 id 取历史文章。"""
    with session_scope() as session:
        article = make_article(
            session, title="详情接口测试", link="https://example.com/api",
            digest="第一句摘要。第二句摘要也在这里，长度足够。", category="产品", score=77,
        )
        article_id = article.id

    body = client.get(f"/api/articles/{article_id}").json()
    assert body["id"] == article_id
    assert body["category"] == "产品"
    assert body["score"] == 77
    assert body["digest_brief"]
    assert body["source_name"]
    assert client.get("/api/articles/999999").status_code == 404


def test_digest_brief_has_no_double_punctuation():
    """导读结尾已有句号时，补上的理由不能拼出「记录。。Apple」这种双句号。"""
    from app.web.api import _digest_with_fallback

    class FakeArticle:
        digest = "导读第一句已经以句号结尾。第二句也以句号结尾。"
        digest_zh = ""
        reason = "推荐理由的第一句内容，还有第二句。"
        summary = ""
        content = ""

    out = _digest_with_fallback(FakeArticle())
    assert "。。" not in out
    assert "。。" not in out
    assert ".." not in out
    assert out.startswith("导读第一句")
    assert "推荐理由" in out


def test_digest_brief_does_not_repeat_same_text():
    """理由与导读重复时不要硬拼。"""
    from app.web.api import _digest_with_fallback

    class FakeArticle:
        digest = "同一句话在这里出现了两次。"
        digest_zh = ""
        reason = "同一句话在这里出现了两次。"
        summary = ""
        content = ""

    assert _digest_with_fallback(FakeArticle()).count("同一句话") == 1


def test_saved_page_does_not_double_bind_toggle(client):
    """回归：收藏页不能再绑一次 toggle。

    base.html 已经用事件委托统一处理 [data-save]；saved.html 若再绑 onclick，
    一次点击会 toggle 两遍 —— 取消收藏看着像没反应，localStorage 顺序还会乱掉。
    """
    text = client.get("/saved").text
    # 收藏页自己的脚本块 = 最后一个 <script>…</script>
    # （index("</script>") 会命中 head 里那段主题脚本，不能用）
    saved_script = text.rsplit("<script>", 1)[1].split("</script>")[0]
    assert "smtmToggleSave" not in saved_script, "收藏页不应再直接调 toggle"
    # 仍然要处理「取消后刷新列表」
    assert "window.smtmSaved" in saved_script


def test_page_has_exactly_one_h1(client, settings: Settings, seeded_db):
    """一个页面只能有一个 h1。早报浮层里的标题曾经也用 h1，会让读屏多出一个主标题。"""
    import re

    with session_scope() as session:
        make_article(session, title="标题唯一性测试", link="https://example.com/h1",
                     digest="导读内容在这里，足够长以通过字数门槛的判定逻辑。")
    with session_scope() as session:
        generate_daily_report(_today(), session=session, settings=settings)

    for path in ("/", "/story/1"):
        html = client.get(path).text
        # 先整块去掉 script / style，再数 h1 —— 只按 "<script" 截断会把 body 一起切掉
        body = re.sub(r"<script\b.*?</script>", "", html, flags=re.S | re.I)
        body = re.sub(r"<style\b.*?</style>", "", body, flags=re.S | re.I)
        assert len(re.findall(r"<h1\b", body)) == 1, f"{path} 的 h1 不止一个"
    html = client.get("/story/1").text
    assert '<h1 class="body-title">' in html
    assert '<p class="dg-title"' in html, "浮层标题不该用 h1"


def test_article_detail_topics_are_clean_strings():
    """topics 库里是 JSON 字符串，早报卡片要的是去掉引号的列表。"""
    from app.web.api import _topics_json

    assert _topics_json('["AI Agent", "隐私权限"]') == ["AI Agent", "隐私权限"]
    assert _topics_json(None) == []
    assert _topics_json("") == []
    assert _topics_json("不是 JSON") == []
    assert _topics_json('{"a":1}') == []


def test_topics_json_used_in_detail_api(client, settings: Settings, seeded_db):
    with session_scope() as session:
        article = make_article(session, title="主题标签测试", link="https://example.com/tp",
                               digest="导读内容在这里，足够长以通过正文长度门槛的判定。",
                               topics='["AI Agent", "隐私权限"]')
        article_id = article.id
    body = client.get(f"/api/articles/{article_id}").json()
    assert body["topics_list"] == ["AI Agent", "隐私权限"]


def test_story_renders_body_images_between_paragraphs(client, settings: Settings, seeded_db):
    """正文配图要插在段落之间（原站 AIHOT 的做法），不是另开一个配图区块。"""
    with session_scope() as session:
        article = make_article(
            session,
            title="内联配图",
            link="https://example.com/inline-shot",
            digest="导读",
            content_full=(
                "第一段正文，字符数足够不会被判成小标题。\n\n"
                "第二段正文，同样足够长，应该排在第一张图后面。"
            ),
            body_images=json.dumps(
                [{"i": 1, "url": "https://cdn.example.com/one.png"}, {"i": 0, "url": "https://cdn.example.com/top.png"}],
                ensure_ascii=False,
            ),
        )
        article_id = article.id

    text = client.get(f"/story/{article_id}").text
    prose = text.split('class="prose')[1].split("</section>")[0]
    # 头图在第一段之前，正文图在第一段之后
    assert prose.index("top.png") < prose.index("第一段正文")
    assert prose.index("第一段正文") < prose.index("one.png") < prose.index("第二段正文")
    # 正文里已经有配图，就不要再单开「文章配图」区块
    assert "文章配图" not in text


def test_story_keeps_cover_gallery_when_body_has_no_images(client, settings: Settings, seeded_db):
    """只有封面图时仍保留配图区块，否则读者在页内一张图也看不到。"""
    with session_scope() as session:
        article = make_article(
            session,
            title="只有封面",
            link="https://example.com/cover-only",
            digest="导读",
            content_full="这一段正文字符数足够长，应该被当成正文渲染出来。",
            image_urls=json.dumps(["https://cdn.example.com/cover.png"]),
        )
        article_id = article.id

    text = client.get(f"/story/{article_id}").text
    assert "文章配图" in text
    assert "cover.png" in text


def test_story_puts_related_at_the_bottom_without_truncating(client, settings: Settings, seeded_db):
    """相关阅读移到正文末尾（原站的位置），标题给全，不再按 42 字硬砍。"""
    long_title = "标题很长很长很长的英文文章标题用来验证它不会再被截断到四十二个字符而是完整显示出来"
    with session_scope() as session:
        first = make_article(
            session, title="主体文章", link="https://example.com/subject", category="行业动态", digest="导读"
        )
        make_article(session, title=long_title, link="https://example.com/neighbour", category="行业动态")
        first_id = first.id

    text = client.get(f"/story/{first_id}").text
    # 出现在正文区块之后，而不是右栏里
    assert text.index("相关阅读") > text.index("AI 导读")
    # 整条标题都在（老模板是 r.title[:42] 硬砍，读者只能看到半句）
    assert "《" + long_title + "》" in text
    # 右栏不再重复一份
    assert text.count("相关阅读") == 1


def test_english_mode_still_shows_something_when_title_en_missing(client, settings: Settings, seeded_db):
    """回归：只有中文标题、没有英文版时，切到英文不能是一条空标题。

    处理早期失败（限流）的文章压根没轮到写 ``title_en``，而模板里没有 ``en``
    就什么都不显示 —— 英文模式会整条空掉。
    """
    with session_scope() as session:
        article = make_article(
            session,
            title="The dawn of the age of the exoskeleton",
            link="https://example.com/no-title-en",
            title_zh="外骨骼时代开启",
            digest="Mountain rescue crews are now hiking in powered exoskeletons in the Pacific Northwest.",
            digest_zh="山岳救援队开始穿着动力外骨骼进入美西北部荒野。",
            relevance=1,
        )
        article_id = article.id

    text = client.get(f"/story/{article_id}").text
    assert "《外骨骼时代开启》" not in text  # 详情页标题不是书名号
    assert '<span class="zh">外骨骼时代开启</span>' in text
    assert '<span class="en">The dawn of the age of the exoskeleton</span>' in text


def test_duplicate_articles_are_hidden_from_every_listing(client, settings: Settings, seeded_db):
    """合并后的重复稿：列表页 / 日报 / API / 搜索都不再出现，但详情页仍可直达。"""
    with session_scope() as session:
        primary = make_article(
            session,
            title="Apple tightens Mac disk access for AI agents",
            link="https://example.com/p",
            category="行业动态",
        )
        twin = make_article(
            session, title="Apple will limit Mac disk access as AI agents increase risk",
            link="https://example.com/q", category="行业动态",
        )
        twin.duplicate_of = primary.id
        primary_id, twin_id = primary.id, twin.id

    home = client.get("/").text
    assert home.count("Apple will limit Mac disk access") == 0
    assert client.get("/api/articles").text.count("Apple will limit Mac disk access") == 0
    assert client.get("/search?q=Apple").text.count("Apple will limit Mac disk access") == 0
    assert client.get("/rss").text.count("Apple will limit Mac disk access") == 0
    # 详情页仍然可达，并在顶部说明它是重复稿
    twin_page = client.get(f"/story/{twin_id}").text
    assert "这条与另一条是同一则新闻" in twin_page
    assert f"/story/{primary_id}" in twin_page


def test_primary_article_lists_the_other_sources(client, settings: Settings, seeded_db):
    """主条目把别家的同题报道列出来 —— 合并只是不重复展示，不是把内容删掉。"""
    with session_scope() as session:
        primary = make_article(session, title="Apple tightens Mac disk access for AI agents", link="https://example.com/r")
        twin = make_article(
            session, title="Apple will limit Mac disk access as AI agents increase risk", link="https://example.com/s"
        )
        twin.duplicate_of = primary.id
        primary_id, twin_id = primary.id, twin.id

    text = client.get(f"/story/{primary_id}").text
    assert "其他来源也报道了" in text
    assert f"/story/{twin_id}" in text


def test_degraded_card_does_not_repeat_the_digest_as_reason(client, settings: Settings, seeded_db):
    """回归：降级时摘要与推荐理由是同一段兜底文本，卡片上不该并排显示两遍。

    LLM 不可用时两个字段都退到正文开头那一段，看起来像坏了 ——
    而且读者要读两遍一模一样的话。理由本来就是「为什么要点开这条」。
    """
    fallback = "This year, members of Seattle Mountain Rescue have been setting off into the wilds."
    with session_scope() as session:
        make_article(
            session,
            title="The dawn of the age of the exoskeleton",
            link="https://example.com/degraded",
            digest=fallback,
            summary=fallback,
            reason=fallback,
            status="failed",
        )

    text = client.get("/").text
    assert text.count(fallback) == 1
    assert "推荐理由" not in text.split(fallback)[1][:200]


def test_degraded_card_says_the_chinese_version_is_still_coming(client, settings: Settings, seeded_db):
    """降级文章要说明中文版还在生成，而不是默默显示英文让人以为不支持中文。"""
    with session_scope() as session:
        make_article(
            session,
            title="Capcom is preparing for a future where we create games with AI",
            link="https://example.com/capcom",
            digest="During a presentation on the future of RE Engine, Capcom laid out its plans.",
            status="failed",
            process_attempts=3,
        )

    text = client.get("/").text
    assert "中文版还在生成" in text
    assert "已重试 3 次" in text


def test_requeued_article_stays_visible_while_waiting_for_retry(client, settings: Settings, seeded_db):
    """回归：降级文章被放回 pending 去重试期间，不能从首页整条消失。

    上游限流时它会被 retry_degraded 翻成 pending 等重试；如果 pending 不算
    可展示，重试期间这条新闻就从首页和日报里不见了 —— 那比重试本身还糟。
    """
    with session_scope() as session:
        make_article(
            session,
            title="Capcom is preparing for a future where we create games with AI",
            link="https://example.com/requeued",
            digest="During a presentation on the future of RE Engine, Capcom laid out its plans.",
            status="pending",
            relevance=1,
            process_attempts=2,
        )
        # 刚抓回来、还没判过相关的：确实不该出现
        make_article(session, title="刚抓回来的", link="https://example.com/brand-new",
                     status="pending", relevance=None)

    text = client.get("/").text
    assert "Capcom is preparing" in text
    assert "刚抓回来的" not in text
    # 读者看到的是英文，就要告诉他中文版还在生成
    assert "中文版还在生成" in text


# ── 先过滤后分页 ────────────────────────────────────────────────────────────
# 回归：早期是「先按数据库分页、再在应用层按分类/标签过滤」。
# 于是总条数与页数算的是**过滤前**的数量：选了分类之后，翻到第 3 页可能
# 一条都没有，而页码还显示有 5 页；反过来过滤后不足一页也照样出现分页条。


def test_paginate_returns_total_and_pages_of_the_filtered_list():
    from app.web.routes import paginate

    cards = [{"i": i} for i in range(45)]
    first, total, pages = paginate(cards, 1, 40)
    assert (len(first), total, pages) == (40, 45, 2)

    second, total, pages = paginate(cards, 2, 40)
    assert [c["i"] for c in second] == list(range(40, 45))
    assert (total, pages) == (45, 2)


def test_paginate_clamps_out_of_range_page():
    from app.web.routes import paginate

    cards = [{"i": i} for i in range(3)]
    page_cards, total, pages = paginate(cards, 99, 40)
    assert (len(page_cards), total, pages) == (3, 3, 1)

    page_cards, total, pages = paginate(cards, 0, 40)
    assert (len(page_cards), total, pages) == (3, 3, 1)


def test_paginate_empty_list_is_one_page():
    from app.web.routes import paginate

    assert paginate([], 1, 40) == ([], 0, 1)


def test_home_pager_counts_the_filtered_set(client, seeded_db):
    """分类过滤后不足一页，就不该出现分页条；越界页码要夹回该分类的第一页。"""
    with session_scope() as session:
        for i in range(PAGE_SIZE + 5):
            make_article(session, title=f"普通文章{i}", link=f"https://example.com/n{i}", category="行业")
        for i in range(3):
            make_article(session, title=f"模型文章{i}", link=f"https://example.com/m{i}", category="模型")

    page = client.get("/?cat=模型")
    assert page.status_code == 200
    for i in range(3):
        assert f"模型文章{i}" in page.text
    # 过滤后只有 3 条 < PAGE_SIZE，所以是 1 页 → 不该渲染分页条
    assert "下一页" not in page.text, "过滤后不足一页却出现了分页条"

    # 越界页码夹回第 1 页，仍然是这 3 条，不是空白页
    beyond = client.get("/?cat=模型&page=2")
    assert beyond.status_code == 200
    assert "模型文章0" in beyond.text, "越界页码应该夹回最后一页，而不是渲染空白页"


def test_mark_headings_ids_are_namespaced():
    """回归：原文与译文同时在 DOM 里（靠 CSS 按语言隐藏），两边都从 0 编号会撞 id。"""
    from app.web.routes import mark_headings, table_of_contents

    blocks = ["发生了什么", "正文段落内容，足够长所以不会被当成小标题处理掉。"]
    en = mark_headings(blocks, prefix="sec-en")
    zh = mark_headings(blocks, prefix="sec-zh")

    assert [item["id"] for item in en] == ["sec-en-0", "sec-en-1"]
    assert [item["id"] for item in zh] == ["sec-zh-0", "sec-zh-1"]
    assert {item["id"] for item in en}.isdisjoint({item["id"] for item in zh})
    # 目录条目必须带上 id，模板就是拿它拼锚点的
    assert table_of_contents(zh)[0]["id"] == "sec-zh-0"


def test_story_toc_points_at_the_displayed_version(client, settings: Settings, seeded_db):
    """有译文时目录要指向译文的小标题，不能拿原文的下标去点译文。"""
    import re

    with session_scope() as session:
        article = make_article(
            session, title="目录语言测试", link="https://example.com/toc-zh",
            content_full="What happened\n\nThis is the first paragraph and it is long enough to not be a heading.",
            content_zh="发生了什么\n\n这是第一段正文，长度足够所以不会被当成小标题。",
        )
        article_id = article.id

    text = client.get(f"/story/{article_id}").text
    anchors = set(re.findall(r'href="#([^"]+)"', text))
    assert anchors, "有译文时也该生成本文目录"
    assert all(a.startswith("sec-zh-") for a in anchors), f"目录应指向译文锚点，实际是 {anchors}"


def test_section_blocks_prefer_ai_headings():
    """有 AI 章节时小标题用编辑定的，不走启发式猜。"""
    import json

    from app.web.routes import _section_blocks

    raw = json.dumps(
        [{"h": "背景", "t": "第一段。\n\n第二段。"}, {"h": "", "t": "第三段。"}],
        ensure_ascii=False,
    )
    blocks = _section_blocks(raw, prefix="sec-zh")
    assert blocks == [
        {"i": 0, "id": "sec-zh-0", "text": "背景", "heading": True},
        {"i": 1, "id": "sec-zh-1", "text": "第一段。", "heading": False},
        {"i": 2, "id": "sec-zh-2", "text": "第二段。", "heading": False},
        {"i": 3, "id": "sec-zh-3", "text": "第三段。", "heading": False},
    ]
    assert _section_blocks(None, prefix="sec-zh") is None
    assert _section_blocks("坏数据", prefix="sec-zh") is None


def test_image_slots_skip_heading_rows():
    """锚点是纯段落序号：小标题行不占号，图不能后移。"""
    from app.web.routes import _image_slots_for_blocks

    marked = [
        {"i": 0, "id": "s-0", "text": "背景", "heading": True},
        {"i": 1, "id": "s-1", "text": "第一段。", "heading": False},
        {"i": 2, "id": "s-2", "text": "第二段。", "heading": False},
    ]
    slots = _image_slots_for_blocks([(0, "http://x/head.png"), (2, "http://x/mid.png")], marked)
    assert slots[0] == ["http://x/head.png"]
    # 第 2 段之后 → 落在第二个纯段落所在的 block 之后
    assert slots[3] == ["http://x/mid.png"]
    # 无标题时与老行为一致
    plain = [dict(b, heading=False) for b in marked]
    assert _image_slots_for_blocks([(2, "http://x/mid.png")], plain)[2] == ["http://x/mid.png"]


def test_shift_anchors_scales_paragraph_index():
    from app.web.routes import _shift_anchors

    assert _shift_anchors([(2, "u")], 4, 4) == [(2, "u")]
    assert _shift_anchors([(4, "u")], 4, 2) == [(2, "u")]
    assert _shift_anchors([(1, "u")], 0, 3) == []


def test_story_page_renders_ai_sections(client, settings: Settings, seeded_db):
    """有 AI 章节时详情页按节渲染小标题（h3），而不是启发式猜。"""
    import json

    with session_scope() as session:
        article = make_article(
            session,
            title="章节测试",
            link="https://example.com/story-sections",
            summary="一句摘要",
            digest="导读内容足够长，可以通过正文判定门槛的文本。",
            content_full=(
                "第一段正文内容，这里补足到足够长度以通过正文字数门槛的判定。\n\n"
                "第二段正文内容，同样补足长度，确保整段都会被渲染出来。\n\n"
                "第三段正文内容，继续补足长度，让章节结构有意义。"
            ),
            content_zh=(
                "第一段译文内容，这里补足到足够长度以通过正文字数门槛的判定。\n\n"
                "第二段译文内容，同样补足长度，确保整段都会被渲染出来。\n\n"
                "第三段译文内容，继续补足长度，让章节结构有意义。"
            ),
        )
        article.body_sections_zh = json.dumps(
            [
                {"h": "背景", "t": "第一段译文内容，这里补足到足够长度以通过正文字数门槛的判定。"},
                {
                    "h": "",
                    "t": "第二段译文内容，同样补足长度，确保整段都会被渲染出来。\n\n"
                    "第三段译文内容，继续补足长度，让章节结构有意义。",
                },
            ],
            ensure_ascii=False,
        )
        article_id = article.id

    page = client.get(f"/story/{article_id}")
    assert page.status_code == 200
    assert ">背景</h3>" in page.text  # AI 定好的小标题渲染成 h3
    assert "本文目录" in page.text or "toc" in page.text.lower() or "sec-zh-0" in page.text


# ── 信源管理页 ────────────────────────────────────────────────────────────

def test_sources_page_lists_seeded_sources(client, seeded_db):
    page = client.get("/sources")
    assert page.status_code == 200
    assert "信源管理" in page.text
    # conftest 的 seeded_db 建了一个源
    assert "测试源" in page.text
    # 导航里有入口
    assert 'href="/sources"' in page.text


def test_sources_create_probes_before_saving(client, monkeypatch, seeded_db):
    """保存前必须真的试抓一次：抓不通的地址不能进库。"""
    import app.web.sources as sources_mod

    calls = {"n": 0}

    def fake_fetch(url, **kwargs):
        calls["n"] += 1
        assert url == "https://news.example.com/rss"
        return [{"title": "示例条目", "link": "https://news.example.com/1"}]

    monkeypatch.setattr(sources_mod, "fetch_feed", fake_fetch)
    page = client.post(
        "/sources",
        data={"url": "https://news.example.com/rss", "name": "示例站", "lang": "en", "enabled": "1"},
        follow_redirects=False,
    )
    assert page.status_code == 200
    assert calls["n"] == 1
    assert "已添加 示例站" in page.text

    with session_scope() as session:
        from app.models import Source

        added = session.execute(select(Source).where(Source.url == "https://news.example.com/rss")).scalar_one()
        assert added.name == "示例站"
        assert added.enabled == 1


def test_sources_create_rejects_unreachable_url(client, monkeypatch, seeded_db):
    """抓不通就不保存：留着只会让调度器每 2 小时失败一次。"""
    import app.web.sources as sources_mod

    def boom(url, **kwargs):
        raise RuntimeError("Connection refused")

    monkeypatch.setattr(sources_mod, "fetch_feed", boom)
    page = client.post(
        "/sources",
        data={"url": "https://dead.example.com/rss", "name": "死源"},
        follow_redirects=False,
    )
    assert page.status_code == 400
    assert "没能从这个地址取到内容" in page.text
    assert "Connection refused" in page.text

    with session_scope() as session:
        from app.models import Source

        assert session.execute(
            select(Source).where(Source.url == "https://dead.example.com/rss")
        ).scalar_one_or_none() is None


def test_sources_create_rejects_empty_feed(client, monkeypatch, seeded_db):
    """能连上但解析不出条目也要拒：这多半不是 RSS。"""
    import app.web.sources as sources_mod

    monkeypatch.setattr(sources_mod, "fetch_feed", lambda url, **kwargs: [])
    page = client.post(
        "/sources", data={"url": "https://empty.example.com/feed", "name": "空源"}, follow_redirects=False
    )
    assert page.status_code == 400
    assert "没解析出任何条目" in page.text


def test_sources_create_rejects_bad_scheme(client, seeded_db):
    """只接受 http/https：file:// 与 javascript: 都没有意义。"""
    page = client.post("/sources", data={"url": "file:///etc/passwd"}, follow_redirects=False)
    assert page.status_code == 400
    assert "http:// 或 https://" in page.text


def test_sources_create_duplicate_url_is_rejected(client, monkeypatch, seeded_db):
    import app.web.sources as sources_mod

    monkeypatch.setattr(sources_mod, "fetch_feed", lambda url, **kwargs: [{"title": "x"}])
    # 用 seeded_db 里那个源的地址（同一个 url 只允许有一条）
    with session_scope() as session:
        from app.models import Source

        existing_url = session.execute(select(Source.url)).scalar_one()
    page = client.post("/sources", data={"url": existing_url}, follow_redirects=False)
    assert page.status_code == 400
    assert "已经在源列表里" in page.text


def test_sources_create_defaults_name_from_host(client, monkeypatch, seeded_db):
    """显示名留空就从地址取主机名，别逼用户填两遍。"""
    import app.web.sources as sources_mod

    monkeypatch.setattr(sources_mod, "fetch_feed", lambda url, **kwargs: [{"title": "x"}])
    client.post(
        "/sources", data={"url": "https://www.qbitai.com/feed"}, follow_redirects=False
    )
    with session_scope() as session:
        from app.models import Source

        added = session.execute(
            select(Source).where(Source.url == "https://www.qbitai.com/feed")
        ).scalar_one()
        assert added.name == "www.qbitai.com"


def test_sources_toggle_and_rename(client, seeded_db):
    with session_scope() as session:
        from app.models import Source

        source_id = session.execute(select(Source.id)).scalar_one()

    page = client.post(f"/sources/{source_id}/toggle", follow_redirects=False)
    assert page.status_code == 200
    with session_scope() as session:
        from app.models import Source

        assert session.get(Source, source_id).enabled == 0

    page = client.post(f"/sources/{source_id}/rename", data={"name": "改过的名字"}, follow_redirects=False)
    assert page.status_code == 200
    with session_scope() as session:
        from app.models import Source

        assert session.get(Source, source_id).name == "改过的名字"

    # 改名不接受空名
    page = client.post(f"/sources/{source_id}/rename", data={"name": "  "}, follow_redirects=False)
    assert page.status_code == 400


def test_sources_delete_keeps_articles(client, seeded_db):
    """删源不删文章：译文已经生成，历史链接不该因此失效。"""
    with session_scope() as session:
        from app.models import Source

        source_id = session.execute(select(Source.id)).scalar_one()
        make_article(session, title="源被删也要能读", link="https://example.com/keep-after-delete",
                     source_id=source_id)

    page = client.post(f"/sources/{source_id}/delete", follow_redirects=False)
    assert page.status_code == 200
    assert "保留" in page.text

    with session_scope() as session:
        from app.models import Article, Source

        row = session.get(Source, source_id)
        # 软删除：行还在（deleted=1），文章与来源名都留着
        assert row is not None
        assert row.deleted == 1
        assert row.enabled == 0
        assert session.execute(
            select(Article).where(Article.link == "https://example.com/keep-after-delete")
        ).scalar_one() is not None


def test_deleted_source_disappears_from_list_and_fetch(client, seeded_db):
    """删掉的源不再出现在**在用列表**、不再被抓取，但**能撤回**。

    早先只有 restore 路由、页面上没有任何入口：源从列表里消失后既看不到也
    恢复不了，同地址重新添加还会撞上那条隐藏的旧行报「已存在」。
    """
    from app.fetcher.pipeline import _enabled_sources

    with session_scope() as session:
        from app.models import Source

        source_id = session.execute(select(Source.id)).scalar_one()
        fetchable = [s.id for s in _enabled_sources(session)]

    client.post(f"/sources/{source_id}/delete", follow_redirects=False)

    page = client.get("/sources")
    # 在用列表里没有了：删除按钮和启用开关都不该再出现
    assert f"/sources/{source_id}/enable" not in page.text
    assert f"/sources/{source_id}/delete" not in page.text
    # 但「已删除」区里能看见，而且能撤回
    assert f"/sources/{source_id}/restore" in page.text
    with session_scope() as session:
        after = [s.id for s in _enabled_sources(session)]
    assert source_id in fetchable
    assert source_id not in after

    # 撤回后回到在用列表，也重新被抓取；历史文章的来源不受影响
    client.post(f"/sources/{source_id}/restore", follow_redirects=False)
    assert f"/sources/{source_id}/restore" not in client.get("/sources").text
    with session_scope() as session:
        assert source_id in [s.id for s in _enabled_sources(session)]


def test_deleted_source_can_be_restored(client, seeded_db):
    """删错了能找回来。"""
    with session_scope() as session:
        from app.models import Source

        source_id = session.execute(select(Source.id)).scalar_one()
    client.post(f"/sources/{source_id}/delete", follow_redirects=False)
    page = client.post(f"/sources/{source_id}/restore", follow_redirects=False)
    assert page.status_code == 200
    assert "已恢复" in page.text
    with session_scope() as session:
        from app.models import Source

        row = session.get(Source, source_id)
        assert row.deleted == 0
        assert row.enabled == 1


def test_deleted_source_is_not_reactivated_by_seed(settings, seeded_db):
    """seed_sources 不会把用户删掉的源加回来（它按 url 增量，已有即跳过）。"""
    from app.db import seed_sources

    with session_scope() as session:
        from app.models import Source

        source_id = session.execute(select(Source.id)).scalar_one()

    client = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(
        __import__("app.main", fromlist=["create_app"]).create_app(settings, bootstrap=False)
    )
    client.post(f"/sources/{source_id}/delete", follow_redirects=False)

    added = seed_sources(settings.sources)
    with session_scope() as session:
        from app.models import Source

        rows = session.execute(select(Source).where(Source.deleted == 1)).scalars().all()
    assert len(rows) == 1, "删掉的源不该被 seed 又加回来一条新的"
    assert added >= 0


def test_sources_404_for_missing(client):
    assert client.post("/sources/999999/toggle", follow_redirects=False).status_code == 404
    assert client.post("/sources/999999/delete", follow_redirects=False).status_code == 404


def test_sources_probe_reports_status(client, monkeypatch, seeded_db):
    import app.web.sources as sources_mod

    with session_scope() as session:
        from app.models import Source

        source_id = session.execute(select(Source.id)).scalar_one()

    monkeypatch.setattr(sources_mod, "fetch_feed", lambda url, **kwargs: [{"title": "抓到一条"}])
    page = client.post(f"/sources/{source_id}/probe", follow_redirects=False)
    assert "正常，读到 1 条" in page.text

    def boom(url, **kwargs):
        raise RuntimeError("timeout")

    monkeypatch.setattr(sources_mod, "fetch_feed", boom)
    page = client.post(f"/sources/{source_id}/probe", follow_redirects=False)
    assert "timeout" in page.text


def test_sources_page_shows_output_counts(client, seeded_db):
    """管理页要显示这个源已经产出了多少篇，避免误删有用的源。"""
    with session_scope() as session:
        from app.models import Source

        source_id = session.execute(select(Source.id)).scalar_one()
        make_article(session, title="源的第一篇", link="https://example.com/c1", source_id=source_id)
    page = client.get("/sources")
    assert "已产出 1 篇" in page.text


def test_deleting_source_keeps_article_readable(client, seeded_db):
    """回归：删源之后，那 27 篇文章必须还能打开。

    实测踩到：直接 ``session.delete(source)`` 时 SQLAlchemy 把
    ``articles.source_id`` 置成了 NULL（没有 cascade，但关系默认会把
    外键清空），于是文章虽然还在，详情页的「来源」全变成了「未知来源」——
    等于把用户的历史信息抹了。改成先摘关联、再删源。
    """
    with session_scope() as session:
        from app.models import Source

        source_id = session.execute(select(Source.id)).scalar_one()
        make_article(session, title="删源后仍可读", link="https://example.com/keep1", source_id=source_id)

    assert client.post(f"/sources/{source_id}/delete", follow_redirects=False).status_code == 200

    with session_scope() as session:
        from app.models import Article

        row = session.execute(
            select(Article).where(Article.link == "https://example.com/keep1")
        ).scalar_one()
        # source_id 保留：SQLAlchemy 会把它清成 NULL，那就丢了归属信息
        assert row.source_id == source_id


def test_source_name_kept_on_articles_after_delete(client, seeded_db):
    """文章详情页仍能显示来源名（拼 article.source 的关系不能变成 None）。"""
    with session_scope() as session:
        from app.models import Source

        source_id = session.execute(select(Source.id)).scalar_one()
        make_article(session, title="来源名要留住", link="https://example.com/keep2", source_id=source_id)

    client.post(f"/sources/{source_id}/delete", follow_redirects=False)
    with session_scope() as session:
        from app.models import Article, Source

        row = session.execute(
            select(Article).where(Article.link == "https://example.com/keep2")
        ).scalar_one()
        assert row.source is not None
        assert row.source.name == "测试源"


# ── 早报片段：必须是一句完整的中文 ────────────────────────────────────────

def test_brief_never_ends_mid_sentence():
    """回归：早报片段不能以省略号/半句话收尾。

    用户实机看到的是「…伦理声明白。Only $30 more than the wireless charging ver…」——
    硬截断加省略号推到了手机上。宁可超预算也要完整。
    """
    from app.utils.text import brief_digest

    raw = (
        "AirPods 5（AirPods 5）较其无线充版仅贵30美元。功能多。《边缘》（The Verge）链购抽佣。"
        "Only $30 more than the wireless charging version of the AirPods 5, but with many more features. "
        "If you buy something from a link, The Verge may earn a commission."
    )
    out = brief_digest(raw)
    assert not out.endswith("…")
    assert out.endswith(("。", "！", "？", ".", "!"))
    # 断句后仍是完整句子：不能把句子从中间切开
    assert not out.endswith("ver")


def test_brief_rejects_english_as_chinese_newsletter():
    """英文原文不能直接当早报文案用（会半中半英）。"""
    from app.web.api import _digest_with_fallback

    class FakeArticle:
        title = "The AirPods Pro 3 are a fantastic deal at $179"
        title_zh = ""
        brief_zh = ""
        digest = (
            "Only $30 more than the wireless charging version of the AirPods 5, "
            "but with many more features. If you buy something from a link, "
            "The Verge may earn a commission."
        )
        digest_zh = ""
        reason = "AirPods 5 较其无线充版仅贵 30 美元。功能更多。"
        summary = ""

    out = _digest_with_fallback(FakeArticle())
    assert out, "应该给出可读的中文文案，而不是空"
    assert "Only $30 more" not in out, "不能把没翻的英文塞进早报"
    assert ".." not in out
    assert "。。" not in out


def test_brief_falls_back_to_chinese_title():
    """连导读都没有时，用中文标题兜底，并明说译文还没整理好。"""
    from app.web.api import _digest_with_fallback

    class FakeArticle:
        title = "The AirPods Pro 3 are a fantastic deal at $179"
        title_zh = ""
        brief_zh = ""
        digest = ""
        digest_zh = ""
        reason = ""
        summary = ""

    out = _digest_with_fallback(FakeArticle())
    assert "整理中" in out

    class Zh(FakeArticle):
        title_zh = "AirPods Pro 3 只卖 179 美元"

    assert "AirPods Pro 3" in _digest_with_fallback(Zh())


def test_has_long_latin_run_catches_untranslated_sentence():
    """按句判断才能抓住中间夹了 $30 的整句英文（正则数连续字母会漏）。"""
    from app.utils.text import has_long_latin_run

    assert has_long_latin_run("伦理声明白。Only $30 more than the wireless charging version of the AirPods 5.")
    # 专有名词不该误伤
    assert not has_long_latin_run("AirPods 5 较其无线充版仅贵 30 美元。Terafab 是新工厂。")


# ── 模型设置页 ──────────────────────────────────────────────────────────

def test_settings_page_shows_current_config(client, settings: Settings):
    """默认显示当前正在用的配置，而不是空的表单。"""
    page = client.get("/settings")
    assert page.status_code == 200
    assert "模型设置" in page.text
    assert settings.llm.model in page.text
    assert settings.llm.api_base in page.text
    # 默认以掩码显示：明文 key 在 value 里，但输入框是 password + 有眼睛按钮
    assert 'type="password" name="api_key"' in page.text
    assert 'id="k-eye"' in page.text
    assert 'id="k-copy"' in page.text
    assert settings.llm.api_key in page.text  # 供眼睛切换看明文
    assert 'href="/settings"' in page.text


def test_settings_presets_cover_major_providers(client):
    """厂商预设要覆盖市面上主流的 OpenAI 兼容服务。"""
    page = client.get("/settings")
    for name in ("OpenAI", "DeepSeek", "Qwen", "GLM", "Gemini", "Ollama"):
        assert name in page.text
    assert "https://api.openai.com/v1" in page.text
    assert "https://api.deepseek.com/v1" in page.text


def test_settings_saves_and_applies_without_restart(client, settings: Settings, monkeypatch):
    """保存后就地生效：不用重启，下一个任务就用新模型。"""
    import app.web.settings as mod

    monkeypatch.setattr(mod, "_probe", lambda s, base, key, model: {"ok": True, "reply": "ok", "ms": 12})
    page = client.post(
        "/settings",
        data={"api_base": "https://api.deepseek.com/v1", "api_key": "sk-new",
              "model": "deepseek-chat", "fallback_models": "glm-4-flash"},
        follow_redirects=False,
    )
    assert page.status_code == 200
    assert "已保存并生效" in page.text
    # 运行中的 settings 被就地改写
    assert settings.llm.model == "deepseek-chat"
    assert settings.llm.api_base == "https://api.deepseek.com/v1"
    assert settings.llm.api_key == "sk-new"
    assert settings.llm.fallback_models == ["glm-4-flash"]
    # 落盘了，下次启动能读回来
    stored = mod.load_stored(settings)
    assert stored["model"] == "deepseek-chat"
    assert stored["api_key"] == "sk-new"


def test_settings_keeps_existing_key_when_left_blank(client, settings: Settings, monkeypatch):
    """key 留空 = 不改动它（页面上只显示掩码，用户不该被迫重贴）。"""
    import app.web.settings as mod

    original = settings.llm.api_key
    monkeypatch.setattr(mod, "_probe", lambda s, base, key, model: {"ok": True, "reply": "ok", "ms": 1})
    client.post("/settings", data={"api_base": "https://x/v1", "model": "m", "api_key": ""},
                follow_redirects=False)
    assert settings.llm.api_key == original


def test_settings_rejects_bad_config_without_saving(client, settings: Settings, monkeypatch):
    """测试不通过就不保存 —— 否则任务会整片降级成英文而没人发现。"""
    import app.web.settings as mod

    monkeypatch.setattr(
        mod, "_probe", lambda s, base, key, model: {"ok": False, "error": "401 unauthorized", "ms": 30}
    )
    page = client.post(
        "/settings",
        data={"api_base": "https://api.openai.com/v1", "api_key": "sk-bad", "model": "gpt-4o"},
        follow_redirects=False,
    )
    assert page.status_code == 400
    assert "401 unauthorized" in page.text
    assert mod.load_stored(settings).get("api_key") != "sk-bad"
    assert settings.llm.model != "gpt-4o"


def test_settings_rejects_missing_fields(client):
    page = client.post("/settings", data={"api_base": "", "model": ""}, follow_redirects=False)
    assert page.status_code == 400
    assert "请填写" in page.text


def test_settings_test_endpoint_does_not_save(client, settings: Settings, monkeypatch):
    """"仅测试连接"不该写入配置。"""
    import app.web.settings as mod

    monkeypatch.setattr(mod, "_probe", lambda s, base, key, model: {"ok": True, "reply": "ok", "ms": 5})
    page = client.post("/settings/test", data={"api_base": "https://y/v1", "model": "mm"},
                       follow_redirects=False)
    assert page.status_code == 200
    assert "连接正常" in page.text
    assert mod.load_stored(settings).get("model") != "mm"


def test_settings_usage_log_is_recorded(client, settings: Settings, monkeypatch, tmp_path):
    """测试请求会写一条用量记录，设置页能看到。"""
    import app.web.settings as mod

    monkeypatch.setattr(mod, "_probe", lambda s, base, key, model: {"ok": True, "reply": "ok", "ms": 7})
    settings.storage.db_path = str(Path(tmp_path) / "usage" / "smtm.db")
    mod.record_usage(settings, {"kind": "probe", "model": "m", "ok": True, "ms": 7, "total_tokens": 42})
    page = client.get("/settings")
    assert "Token 用量" in page.text
    assert "调用日志" in page.text
    assert "输入" in page.text
    assert "输出" in page.text


def test_settings_stored_config_applies_on_startup(settings: Settings):
    """启动时要套用页面保存过的配置（apply_stored）。"""
    import app.web.settings as mod

    class State:
        pass

    state = State()
    state.settings = settings
    changed = mod.apply_stored(state, {"model": "swapped-model", "api_base": "https://z/v1"})
    assert changed
    assert settings.llm.model == "swapped-model"
    assert settings.llm.api_base == "https://z/v1"
    # 空值不覆盖已有配置
    mod.apply_stored(state, {"model": ""})
    assert settings.llm.model == "swapped-model"
    # 没给任何东西时不算改动
    assert mod.apply_stored(state, {}) is False
def test_settings_key_file_is_owner_only(tmp_path, settings: Settings):
    """key 落盘必须是 0600。手工构造 Settings 太脆，直接改 db_path 让它写到 tmp。"""
    import app.web.settings as mod

    settings.storage.db_path = str(tmp_path / "data" / "smtm.db")
    mod.save_stored(settings, {"api_key": "sk-secret", "model": "m"})
    path = mod._settings_path(settings)
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600, "key 文件不该是全局可读"
    assert mod.load_stored(settings)["api_key"] == "sk-secret"


def test_settings_file_ignored_by_git():
    """data/ 已被 .gitignore 忽略，key 不会误入版本库。"""
    import subprocess

    root = Path(__file__).resolve().parent.parent
    out = subprocess.run(
        ["git", "check-ignore", "-q", "data/llm_settings.json"], cwd=str(root)
    )
    assert out.returncode == 0, "data/llm_settings.json 必须被 .gitignore 忽略"


def test_charged_tokens_dont_undercount_reasoning():
    """回归：思考 token 必须计入消耗。

    实测 ag/gemini 这条链路 `total = prompt + completion`，**不含 thinking**：
    一次改写 thought 用了 2045、output 只有 19，直接展示网关的 total 会少算一半，
    恰恰把最贵的那部分藏起来了。缓存命中则是输入的子集，要扣掉而不是叠加。
    """
    from app.web.settings import _charged

    gemini = {"input_tokens": 4203, "output_tokens": 19, "cached_tokens": 0, "reasoning_tokens": 2045}
    # 4203 + 19 + (2045-19) = 6248
    assert _charged(gemini) == 6248

    # 缓存命中从输入里扣；思考不超过输出时不重复计
    openai = {"input_tokens": 1000, "output_tokens": 500, "cached_tokens": 400, "reasoning_tokens": 200}
    assert _charged(openai) == 1000 - 400 + 500 + 0

    # 缓存不可能超过输入：999 被夹到 10 → 10-10+1 = 1（不能变成负数）
    assert _charged({"input_tokens": 10, "output_tokens": 1, "cached_tokens": 999}) == 1
    # 网关没报任何 token 时不猜
    assert _charged({}) == 0


def test_usage_summary_exposes_charged_and_cache_rate():
    from app.web.settings import usage_summary

    rows = [
        {"at": "2026-10-05 10:00:00", "ok": True, "input_tokens": 1000,
         "output_tokens": 100, "cached_tokens": 250, "reasoning_tokens": 400},
        {"at": "2026-10-05 11:00:00", "ok": False, "error": "429"},
    ]
    total = usage_summary(rows)["totals"]
    assert total["charged_tokens"] == 1000 - 250 + 100 + 300
    assert total["calls"] == 2
    assert total["fail"] == 1
    assert total["cache_hit_rate"] == 25.0
