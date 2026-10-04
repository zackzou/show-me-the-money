"""Web / API 测试：JSON 接口、页面路由、RSS 输出。"""

from __future__ import annotations

import json
from urllib.parse import quote

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
    # 一个标点都没有、且超过上限时，才用省略号硬收
    assert brief_digest("超长" * 60).endswith("…")
    assert len(brief_digest("超长" * 60)) == 108

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
