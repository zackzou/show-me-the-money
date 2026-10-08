"""AIHOT 风格皮肤（可选外观层）的回归测试。

皮肤的两条硬性约束：
1. **默认皮肤零影响** —— 皮肤的全部 CSS 选择器都锁在 html[data-skin="aihot"] 下，
   新壳层元素（.sk-side/.sk-tabbar）默认 display:none；
2. **功能不变** —— 深浅色切换、语言切换、收藏等既有能力不因皮肤而改变。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import session_scope
from app.report.generator import generate_daily_report
from app.utils.text import now_local
from tests.conftest import make_article


def test_skin_css_is_served(client: TestClient):
    response = client.get("/skin/aihot.css")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    assert "immutable" in response.headers.get("cache-control", "")
    # 皮肤的核心：所有覆盖必须锁在 data-skin 属性下
    body = response.text
    assert 'html[data-skin="aihot"]' in body
    # 新壳层元素默认隐藏
    assert ".sk-side, .sk-tabbar { display:none; }" in body


def test_base_links_skin_and_has_shell(client: TestClient):
    text = client.get("/").text
    assert '<link rel="stylesheet" href="/skin/aihot.css">' in text
    # 新壳层：桌面侧栏 + 手机底部标签栏
    assert 'class="sk-side"' in text
    assert 'class="sk-tabbar"' in text
    # 外观切换：顶栏皮肤按钮 + 侧栏三态（深色/跟随系统/浅色）
    assert 'id="skin"' in text
    assert 'id="sk-seg"' in text
    assert 'data-sk-theme="dark"' in text
    assert 'data-sk-theme="system"' in text
    assert 'data-sk-theme="light"' in text
    # 启动脚本在 CSS 落地前恢复皮肤，避免闪一下默认皮肤
    assert 'localStorage.getItem("smtm-skin")' in text


def test_theme_auto_by_time_is_default(client: TestClient):
    """深浅色默认自适应：按电脑时间自动切换（夜里深色、白天浅色），
    手动选择过深色/浅色则记住手动值。"""
    text = client.get("/").text
    assert "autoDark" in text
    assert "new Date().getHours()" in text
    # 自适应：19:00~07:00 深色
    assert "hour < 7 || hour >= 19" in text


def test_icon_buttons_have_instant_tooltips(client: TestClient):
    """图标按钮的悬停文字：原生 title 在内嵌浏览器里不弹（用户反馈），
    改为 data-tip + CSS 伪元素即时显示。"""
    text = client.get("/").text
    assert 'data-tip="切换深浅色"' in text
    assert 'data-tip="切换风格（科技 / 经典）"' in text
    assert 'data-tip="服务状态"' in text
    assert 'content:attr(data-tip)' in text


def test_skin_switch_is_persisted_and_reversible(client: TestClient):
    text = client.get("/").text
    # 科技风格是默认：只存显式的「经典」；写/清两个方向都在
    assert 'localStorage.setItem("smtm-skin", "classic")' in text
    assert 'localStorage.removeItem("smtm-skin")' in text
    # 启动脚本：非 classic 一律走科技风格（未设置 / 旧值 aihot 都落到默认）
    assert 'localStorage.getItem("smtm-skin") !== "classic"' in text
    # 换回经典风格的入口
    assert 'id="sk-back"' in text


def test_default_skin_contract_unchanged(client: TestClient):
    """经典风格的三件套一个都不能少：深浅色、语言、收藏。"""
    text = client.get("/").text
    assert "smtm-theme" in text
    assert 'classList.toggle("dark", dark)' in text
    assert 'localStorage.setItem("smtm-theme"' in text
    assert 'document.documentElement.setAttribute("data-lang", "zh")' in text
    assert "smtm-saved" in text


def test_skin_does_not_add_server_side_theme_state(client: TestClient):
    """皮肤是纯前端偏好：服务端不渲染 data-skin 属性。

    这样测试与爬虫拿到的永远是默认皮肤，也避免「服务器记住某个人的外观」
    这种不该有的状态。
    """
    text = client.get("/").text
    assert "<html" in text
    html_tag = text[text.index("<html"):text.index(">", text.index("<html")) + 1]
    assert "data-skin" not in html_tag


def _today() -> str:
    return now_local().strftime("%Y-%m-%d")


def test_score_hot_class_for_top_scores(client: TestClient, settings: Settings, seeded_db: str):
    """85+ 的评分带 hot 类（AIHOT 皮肤里显示为红色「热」档），
    70+ 仍带 strong —— 默认皮肤的既有样式不受影响。"""
    with session_scope() as session:
        make_article(session, title="高分文章", link="https://example.com/hot-score",
                     digest="导语", score=90)
        make_article(session, title="中分文章", link="https://example.com/mid-score",
                     digest="导语", score=75)
    with session_scope() as session:
        generate_daily_report(_today(), session=session, settings=settings)

    text = client.get("/").text
    assert "score strong hot" in text   # 90 分：两档都在（hot 在后覆盖颜色）
    assert "score strong" in text       # 75 分：只有 strong
    assert "score hot" not in text.replace("score strong hot", "")
