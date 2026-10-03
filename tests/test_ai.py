"""AI 模块测试：提示词渲染、LLM 客户端、处理与降级。"""

from __future__ import annotations

import httpx
import pytest

from app.ai.client import LLMClient, LLMError
from app.ai.processor import process_article, process_pending
from app.ai.prompts import render_relevance_prompt, render_summary_prompt, render_tag_prompt
from app.config import Settings
from app.db import session_scope

from .conftest import make_article


def _client(handler, **kwargs) -> LLMClient:
    return LLMClient(
        "http://llm.local/v1",
        "test-key",
        "test-model",
        retries=kwargs.pop("retries", 1),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        **kwargs,
    )


def test_prompt_rendering_includes_inputs(settings: Settings):
    prompt = render_summary_prompt(settings.prompts, "AI Agent", "标题A", "正文B")
    assert "AI Agent" in prompt
    assert "标题A" in prompt
    assert "正文B" in prompt
    assert "{title}" not in prompt  # 占位符必须被替换

    long_prompt = render_summary_prompt(settings.prompts, "AI Agent", "标题", "长" * 9000)
    assert len(long_prompt) < 9000  # 正文被截断，避免爆上下文

    assert "标题A" in render_tag_prompt(settings.prompts, "标题A", "摘要")
    assert "AI Agent" in render_relevance_prompt(settings.prompts, "AI Agent", "标题", "摘要")


def test_llm_client_success_and_retry():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, text="server error")
        return httpx.Response(200, json={"choices": [{"message": {"content": "  你好  "}}]})

    client = _client(handler)
    try:
        assert client.chat("hi") == "你好"
        assert calls["n"] == 2
    finally:
        client.close()


def test_llm_client_raises_when_always_failing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="nope")

    client = _client(handler, retries=0)
    try:
        with pytest.raises(LLMError):
            client.chat("hi")
    finally:
        client.close()


def test_llm_client_empty_content_is_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    client = _client(handler, retries=0)
    try:
        with pytest.raises(LLMError):
            client.chat("hi")
    finally:
        client.close()


def test_llm_client_endpoint_normalisation():
    client = LLMClient("http://llm.local/v1/", "k", "m")
    assert client.endpoint == "http://llm.local/v1/chat/completions"
    direct = LLMClient("http://llm.local/v1/chat/completions", "k", "m")
    assert direct.endpoint == "http://llm.local/v1/chat/completions"


def test_process_article_relevant(seeded_db, settings: Settings):
    answers = iter(["yes 88", "这是摘要", "这是速览：两三句导语", "这是推荐理由", "标签一,标签二"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": next(answers)}}]})

    client = _client(handler, retries=0)
    try:
        with session_scope() as session:
            article = make_article(session, title="相关文章", content="正文", status="pending", relevance=None)
            status = process_article(session, article, client, settings)
            assert status == "processed"
            assert article.relevance == 1
            assert article.summary == "这是摘要"
            assert article.digest == "这是速览：两三句导语"
            assert article.reason == "这是推荐理由"
            assert article.score == 88
            assert article.tags == "标签一,标签二"
    finally:
        client.close()


def test_process_article_irrelevant(seeded_db, settings: Settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "no"}}]})

    client = _client(handler, retries=0)
    try:
        with session_scope() as session:
            article = make_article(session, title="无关文章", status="pending", relevance=None)
            assert process_article(session, article, client, settings) == "processed"
            assert article.relevance == 0
            assert article.summary is None
    finally:
        client.close()


def test_process_article_falls_back_on_llm_failure(seeded_db, settings: Settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    client = _client(handler, retries=0)
    try:
        with session_scope() as session:
            article = make_article(
                session,
                title="宕机时的文章",
                content="正文" * 200,
                status="pending",
                relevance=None,
            )
            assert process_article(session, article, client, settings) == "failed"
            assert article.status == "failed"
            assert article.tags is None
            assert len(article.summary) <= settings.prompts.fallback_summary_chars + 1
    finally:
        client.close()


def test_process_pending_counts(seeded_db, settings: Settings):
    answers = iter(["yes 88", "摘要", "速览1", "理由1", "A,B", "no 5", "yes 72", "摘要2", "速览2", "理由2", "C"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": next(answers)}}]})

    client = _client(handler, retries=0)
    try:
        with session_scope() as session:
            make_article(session, title="第一篇", status="pending", relevance=None)
            make_article(session, title="第二篇", status="pending", relevance=None)
            stats = process_pending(session, client, settings)
            assert stats["pending"] == 2
            assert stats["processed"] + stats["irrelevant"] == 2
    finally:
        client.close()


def test_process_pending_survives_unexpected_error(seeded_db, settings: Settings):
    """一篇坏数据不能把整批 pending 卡死 —— 单篇异常要就地标记并继续。"""
    from sqlalchemy import select

    from app.ai.processor import process_pending
    from app.db import session_scope
    from app.models import Article
    from app.utils.text import now_local

    with session_scope() as session:
        session.add(Article(title="会炸的", link="https://example.com/boom", status="pending",
                            published_at=now_local()))
        session.add(Article(title="正常的", link="https://example.com/fine", status="pending",
                            published_at=now_local()))
        session.flush()
        boom_id = session.query(Article).filter(Article.title == "会炸的").one().id

    class ExplodingClient:
        def chat(self, prompt: str) -> str:
            title = "会炸的" if "会炸的" in prompt else "ok"
            if title == "会炸的":
                raise RuntimeError("模拟未预期异常")
            return "yes"

    with session_scope() as session:
        stats = process_pending(session, ExplodingClient(), settings)

    assert stats["crashed"] == 1
    assert stats["processed"] == 1
    with session_scope() as session:
        boom = session.execute(select(Article).where(Article.id == boom_id)).scalar_one()
        assert boom.status == "failed"
        assert boom.summary  # 降级摘要已补上


def test_process_pending_respects_batch_limit(seeded_db, settings: Settings):
    from app.ai.processor import process_pending
    from app.db import session_scope
    from app.models import Article
    from app.utils.text import now_local

    with session_scope() as session:
        for i in range(5):
            session.add(Article(title=f"文章{i}", link=f"https://example.com/b{i}", status="pending",
                                published_at=now_local()))

    class YesClient:
        def chat(self, prompt: str) -> str:
            return "yes"

    with session_scope() as session:
        stats = process_pending(session, YesClient(), settings, limit=2)
    assert stats["pending"] == 2


def test_redact_masks_secrets():
    from app.ai.client import redact

    assert "sk-abcdef123456" not in redact("key=sk-abcdef123456 rest")
    assert "***" in redact("key=sk-abcdef123456 rest")
    assert redact("Authorization: Bearer tok_1234567890") == "Authorization: ***"
    assert redact("正常报错信息") == "正常报错信息"


def test_chat_parses_sse_response():
    """有些网关（实测 9router）即使不声明 stream 也回 SSE，必须能解析。"""
    import json as _json

    import httpx

    from app.ai.client import LLMClient, parse_sse

    chunks = [
        {"choices": [{"delta": {"content": "yes"}}]},
        {"choices": [{"delta": {"content": "，相关"}}]},
    ]
    body = "".join(
        f"event: message\ndata: {_json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks
    ) + "data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        assert _json.loads(request.content)["stream"] is False  # 必须显式声明非流式
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        llm = LLMClient("http://gw/v1", "k", "m", client=client)
        assert llm.chat("判断") == "yes，相关"
    assert parse_sse(body) == "yes，相关"


def test_chat_reads_responses_api_shape():
    """Responses API 风格的 output_text 也能取到。"""
    import httpx

    from app.ai.client import LLMClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output_text": "no"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert LLMClient("http://gw/v1", "k", "m", client=client).chat("判断") == "no"


def test_chat_format_error_is_not_retried():
    """返回 HTML 错误页这类问题重试没有意义，不能白花钱。"""
    import httpx

    from app.ai.client import LLMClient, LLMFormatError

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text="<html>login required</html>", headers={"content-type": "text/html"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client, pytest.raises(LLMFormatError):
        LLMClient("http://gw/v1", "k", "m", client=client, retries=2).chat("判断")
    assert calls["n"] == 1


@pytest.mark.parametrize(
    ("answer", "relevant", "score"),
    [
        ("yes 85", True, 85),
        ("yes", True, None),
        ("是的 92", True, 92),
        ("Yes, it is relevant. 70", True, 70),
        ("no 5", False, 5),
        ("no", False, None),
        ("", False, None),
        ("yes 999", True, None),   # 越界分数直接丢弃，不能影响判定
        ("maybe", False, None),
    ],
)
def test_parse_relevance(answer, relevant, score):
    from app.ai.processor import parse_relevance

    assert parse_relevance(answer) == (relevant, score)


def test_relevance_without_score_still_processes(seeded_db, settings: Settings):
    """模型只给 yes 没给分数时，文章照样要正常进流程（评分是可选的）。"""
    answers = iter(["yes", "摘要", "速览", "理由", "标签"])
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": next(answers)}}]})

    client = _client(handler, retries=0)
    try:
        with session_scope() as session:
            article = make_article(session, title="无分数文章", status="pending", relevance=None)
            assert process_article(session, article, client, settings) == "processed"
            assert article.relevance == 1
            assert article.score is None
            assert article.reason == "理由"
    finally:
        client.close()
