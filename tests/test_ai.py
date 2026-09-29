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
    answers = iter(["yes", "这是摘要", "标签一,标签二"])

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
    answers = iter(["yes", "摘要", "A,B", "no", "yes", "摘要2", "C"])

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
