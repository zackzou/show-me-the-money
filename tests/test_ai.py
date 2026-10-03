"""AI 模块测试：提示词渲染、LLM 客户端、处理与降级。"""

from __future__ import annotations

import json

import httpx
import pytest

from app.ai.client import LLMClient, LLMError
from app.ai.processor import (
    backfill_translations,
    process_article,
    process_pending,
    translate_title_to_chinese,
    translate_to_chinese,
)
from app.ai.prompts import render_relevance_prompt, render_summary_prompt, render_tag_prompt
from app.config import Settings
from app.db import session_scope
from app.models import Article

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
    answers = iter(
        [
            "yes 88", "这是摘要", "这是速览：两三句导语", "这是推荐理由",
            "模型\nOpenAI, 大模型", "English Title\nEnglish digest here", "标签一,标签二",
        ]
    )

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
            assert article.category == "模型"
            assert article.topics == '["OpenAI", "大模型"]'
            assert article.title_en == "English Title"
            assert article.digest_en == "English digest here"
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
    answers = iter(
        [
            "yes 88", "摘要", "速览1", "理由1", "模型\nOpenAI", "EN Title 1\nEN digest 1", "A,B",
            "no 5",
            "yes 72", "摘要2", "速览2", "理由2", "产品\nAI Agent", "EN Title 2\nEN digest 2", "C",
        ]
    )

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
    answers = iter(["yes", "摘要", "速览", "理由", "行业\n云计算", "EN title\nEN digest", "标签"])
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


@pytest.mark.parametrize(
    ("raw", "category", "topics"),
    [
        ("模型\nOpenAI, 大模型", "模型", ["OpenAI", "大模型"]),
        ("行业\n芯片算力", "行业", ["芯片算力"]),
        ("  - 教程  \n  AI Agent、芯片  ", "教程", ["AI Agent", "芯片"]),
        ("不存在的分类\n随便什么", None, ["不存在的分类", "随便什么"]),
        ("模型", "模型", []),
        ("", None, []),
        ("模型\n同一个\n同一个", "模型", ["同一个"]),
    ],
)
def test_parse_classify(raw, category, topics):
    from app.ai.processor import parse_classify

    assert parse_classify(raw, {"一手", "模型", "产品", "行业", "论文", "教程", "观点"}) == (category, topics)


@pytest.mark.parametrize(
    ("text", "english"),
    [
        ("Meta 开源 Muse 模型代码，计划将这一 AI 嵌入消费设备", False),
        ("Meta open-sources the Muse model code to let anyone embed it in consumer devices", True),
        ("", False),
    ],
)
def test_looks_english(text, english):
    from app.ai.processor import looks_english

    assert looks_english(text) is english


def test_parse_translate():
    from app.ai.processor import parse_translate

    assert parse_translate("English Title\nEnglish digest line") == ("English Title", "English digest line")
    assert parse_translate("Only Title") == ("Only Title", None)
    assert parse_translate("") == (None, None)


def test_english_source_skips_translation(seeded_db, settings: Settings):
    """整篇英文的信源不必再翻一次，省一次调用；英文直接复用原文。"""
    import httpx

    en_summary = (
        "Google will end free access to its Flash and Pro models, "
        "a move that tightens monetization for large language model providers."
    )
    # 纯英文信源：正文与摘要复用原文不翻（没有中译英那一跳），
    # 但标题与导读都要补中文版 → 6 次 + 中文标题 + 中文导读
    answers = iter([
        "yes 80",
        en_summary,
        "English digest",
        "reason",
        "产品\\nAI",
        "苹果收紧隐私设置",
        "谷歌将结束对 Flash 与 Pro 模型的免费访问",
        "标签",
    ])
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": next(answers)}}]})

    client = _client(handler, retries=0)
    try:
        with session_scope() as session:
            article = make_article(
                session,
                title="An English headline about AI agents shipping to production this week",
                status="pending",
                relevance=None,
            )
            assert process_article(session, article, client, settings) == "processed"
            assert article.title_en == article.title, "纯英文标题直接复用原文，不翻"
            assert article.digest_en == en_summary, "纯英文导语直接复用摘要原文"
            # relevance/summary/digest/reason/classify + 中文标题 + 中文导读 + tags
            assert calls["n"] == 8
            assert article.title_zh == "苹果收紧隐私设置"
            # 中文模式下导读读的是中文版，不是那段英文
            assert article.digest_zh == "谷歌将结束对 Flash 与 Pro 模型的免费访问"
    finally:
        client.close()


def test_translate_to_chinese_chunks_and_keeps_order(seeded_db, settings: Settings):
    """长正文按段分组翻译，顺序要保持；某组失败只丢那一组。"""
    body = "\n\n".join(
        f"Paragraph {i} of the English article body, long enough to pass the length gate."
        for i in range(1, 12)
    )
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        text = payload["messages"][0]["content"]
        seen.append(text)
        return httpx.Response(200, json={"choices": [{"message": {"content": "这段是译文。" * 40}}]})

    client = _client(handler, retries=0)
    try:
        out = translate_to_chinese(client, settings.prompts, body)
        assert out, "英文正文应该翻得出中文"
        assert len(seen) >= 2, "11 段不该一次就翻完"
        assert "Paragraph 1" in seen[0]
        assert "Paragraph 11" in seen[-1]
        assert out.count("这段是译文") == len(seen) * 40
        # 译文明显比原文短（模型只回了一截）要判为失败，不写进库
        short = _client(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "太短"}}]}), retries=0)
        try:
            assert translate_to_chinese(short, settings.prompts, body) is None
        finally:
            short.close()
    finally:
        client.close()


def test_translate_to_chinese_skips_chinese_body(seeded_db, settings: Settings):
    """中文原文不翻，也不发请求。"""
    import httpx

    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "不该被调用"}}]})

    client = _client(handler, retries=0)
    try:
        assert translate_to_chinese(client, settings.prompts, "这是一段中文原文，本来就不需要翻译。") is None
        assert translate_to_chinese(client, settings.prompts, "") is None
        assert called["n"] == 0
    finally:
        client.close()


def test_translate_to_chinese_returns_none_when_all_chunks_fail(seeded_db, settings: Settings):
    """全失败时返回 None（调用方据此不写库），不写半截译文。"""
    import httpx

    body = "\n\n".join(f"This is English paragraph number {i} in the article body." for i in range(6))
    client = _client(lambda r: httpx.Response(500, text="boom"), retries=0)
    try:
        assert translate_to_chinese(client, settings.prompts, body) is None
    finally:
        client.close()


def test_translate_chunk_rejected_when_too_short(seeded_db, settings: Settings):
    """译文明显短于原文（多半是模型只回了一截）要判为失败，不写进库。"""
    import httpx

    body = "\n\n".join("English paragraph content that is long enough to pass the gate." for _ in range(4))
    client = _client(
        lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "短"}}]}), retries=0
    )
    try:
        assert translate_to_chinese(client, settings.prompts, body) is None
    finally:
        client.close()


def test_backfill_translations_fills_missing_only(seeded_db, settings: Settings):
    """只补没译文的；中文原文直接标成已处理，不再反复扫。"""
    import httpx

    with session_scope() as session:
        en = make_article(
            session, title="English headline that is definitely long enough to count as English text",
            status="processed", relevance=1,
            content_full="Apple says it is changing its macOS privacy settings for third-party developers.",
        )
        zh = make_article(session, title="中文标题", status="processed", relevance=1, content_full="这是一段中文原文。")
        en.content_zh = None
        zh.content_zh = None
        # 标题与导读的中文版上一轮已经补好了，这一轮只缺正文译文
        en.title_zh = "苹果收紧 macOS 隐私设置"
        en.digest_zh = "苹果将修改 macOS 隐私设置。"
        zh.title_zh = ""
        zh.digest_zh = ""
        en_id, zh_id = en.id, zh.id
        session.commit()

    reply = {"choices": [{"message": {"content": "苹果表示将修改 macOS 的隐私设置。" * 3}}]}
    client = _client(lambda r: httpx.Response(200, json=reply), retries=0)
    try:
        with session_scope() as session:
            stats = backfill_translations(session, client, settings, limit=10)
            assert stats["contents"] == 1
            assert stats["skipped"] == 1
            assert session.get(Article, en_id).content_zh
            # 中文原文标成空串，表示「已确认无需翻译」，下一轮不会再扫它
            assert session.get(Article, zh_id).content_zh == ""
            # 再跑一轮不该重复处理
            assert backfill_translations(session, client, settings, limit=10)["candidates"] == 0
    finally:
        client.close()


def test_parse_title_zh_strips_quotes_and_prefix():
    from app.ai.processor import parse_title_zh

    assert parse_title_zh("苹果收紧 macOS 隐私设置") == "苹果收紧 macOS 隐私设置"
    assert parse_title_zh('"苹果收紧隐私设置"') == "苹果收紧隐私设置"
    assert parse_title_zh("「苹果收紧隐私设置」\n多余的一行") == "苹果收紧隐私设置"
    assert parse_title_zh("## 苹果收紧隐私设置") == "苹果收紧隐私设置"
    assert parse_title_zh("") is None
    assert parse_title_zh("   \n  ") is None


def test_translate_title_skips_chinese_and_no_template(seeded_db, settings: Settings):
    """中文标题不翻；提示词为空时也不发请求。"""
    import httpx

    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "不该调用"}}]})

    client = _client(handler, retries=0)
    try:
        assert translate_title_to_chinese(client, settings.prompts, "中文标题本来就不需要翻译") is None
        empty = settings.model_copy(deep=True)
        empty.prompts.translate_title_zh_prompt = ""
        assert translate_title_to_chinese(client, empty.prompts, "An English headline long enough to pass") is None
        assert called["n"] == 0
    finally:
        client.close()


def test_translate_digest_to_chinese_needs_a_translation(settings: Settings):
    """英文导读要翻；中文导读、英文原样吐回，都不算成功。"""
    import httpx

    from app.ai.processor import translate_digest_to_chinese

    def reply(content: str):
        return _client(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": content}}]}), retries=0)

    assert translate_digest_to_chinese(reply("苹果收紧隐私设置。"), settings.prompts, "苹果收紧隐私设置。") is None
    english = reply("Apple tightens privacy settings for third-party developers.")
    try:
        # 模型把英文原样吐回来 = 没翻，写进库里就再也不会重试了
        assert translate_digest_to_chinese(english, settings.prompts, "Apple tightens privacy settings.") is None
    finally:
        english.close()

    chinese = reply("导读\n苹果表示将修改 macOS 的隐私设置，保护用户数据不被滥用。")
    try:
        out = translate_digest_to_chinese(
            chinese, settings.prompts, "Apple says it is changing its macOS privacy settings."
        )
        # 模型甩的那行小标题要去掉，正文一句都不能少
        assert out == "苹果表示将修改 macOS 的隐私设置，保护用户数据不被滥用。"
    finally:
        chinese.close()


def test_backfill_translations_fills_digest_and_title(seeded_db, settings: Settings):
    """中英双版本要一次补齐：中文标题、中文导读、中文正文缺哪个补哪个。"""
    import httpx

    with session_scope() as session:
        article = make_article(
            session,
            title="English headline that is definitely long enough to count as English text",
            status="processed",
            relevance=1,
            content_full="Apple says it is changing its macOS privacy settings for developers today.",
            digest="Apple says it is changing its macOS privacy settings for developers.",
        )
        article.digest_en = article.digest
        # 正文译文已经有中文版，这一轮只缺标题与导读；
        # title_en 故意留空 —— 处理早期失败的文章根本没轮到写它，
        # 拿它当条件会让这类文章永远等不到中文标题
        article.content_zh = "苹果表示将修改 macOS 的隐私设置。"
        article_id = article.id
        session.commit()

    reply = {"choices": [{"message": {"content": "苹果表示将修改 macOS 的隐私设置，避免第三方滥用。"}}]}
    client = _client(lambda r: httpx.Response(200, json=reply), retries=0)
    try:
        with session_scope() as session:
            stats = backfill_translations(session, client, settings, limit=10)
            assert stats["titles"] == 1
            assert stats["digests"] == 1
            stored = session.get(Article, article_id)
            assert stored.title_zh
            assert "苹果" in stored.digest_zh
            # 再跑一轮不该重复处理
            assert backfill_translations(session, client, settings, limit=10)["candidates"] == 0
    finally:
        client.close()


def test_backfill_translations_rotates_failed_candidates(seeded_db, settings: Settings):
    """上游限流时，最新的那几篇不能把名额占满 —— 重试次数少的优先。"""
    import httpx
    from sqlalchemy import select

    with session_scope() as session:
        for index in range(3):
            article = make_article(
                session,
                title=f"English headline number {index} that is long enough to count as English",
                status="processed",
                relevance=1,
                content_full="Apple says it is changing its macOS privacy settings for developers today.",
            )
            article.title_en = article.title
            # 前两篇已经反复失败过，最后一篇还没试过（发布时间也最新）
            article.i18n_attempts = 5 if index < 2 else 0
        session.commit()

    # 全部翻译失败（限流）
    client = _client(lambda r: httpx.Response(429, text="rate limited"), retries=0)
    try:
        with session_scope() as session:
            before = {a.id: a.i18n_attempts for a in session.execute(select(Article)).scalars()}
            backfill_translations(session, client, settings, limit=1)
            after = {a.id: a.i18n_attempts for a in session.execute(select(Article)).scalars()}
    finally:
        client.close()
    # 名额给了还没试过的那篇（发布时间也最新），而不是又去撞反复失败的两篇
    assert [i for i in after if after[i] > before[i]] == [max(before)]


def test_short_english_title_still_gets_a_chinese_one(settings: Settings):
    """回归：短英文标题也必须翻。

    ``looks_english`` 要求 40 个字母以上，``The dawn of the age of the exoskeleton``
    只有 30 个字母，早先被判成「不是英文」→ 不翻 → 详情页在中文模式下顶着英文标题。
    """
    import httpx

    from app.ai.processor import translate_title_to_chinese

    short = "The dawn of the age of the exoskeleton"
    client = _client(
        lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "外骨骼时代开启"}}]}), retries=0
    )
    try:
        assert translate_title_to_chinese(client, settings.prompts, short) == "外骨骼时代开启"
        # 已经有汉字的标题不浪费调用
        assert translate_title_to_chinese(client, settings.prompts, "外骨骼时代开启") is None
    finally:
        client.close()


def test_fallback_digest_ignores_site_boilerplate(seeded_db, settings: Settings):
    """回归：Hacker News / Reddit 的模板套话不能当摘要。

    HN 的 RSS description 剥掉标签之后只剩「Comments」，直接拿来兜底，
    页面上就是一行「推荐理由：Comments」。
    """
    from app.ai.processor import _fallback_digest, _fallback_summary

    with session_scope() as session:
        article = make_article(
            session,
            title="Scientists invent underwater umbrellas to protect coral reefs",
            link="https://example.com/boiler",
            content="Comments",
            content_full=None,
        )
        article_id = article.id

    with session_scope() as session:
        stored = session.get(Article, article_id)
        assert _fallback_digest(stored, 180) == stored.title
        assert _fallback_summary(stored, 200) == stored.title


def test_fallback_digest_prefers_real_body_over_boilerplate(seeded_db):
    """正文抓回来了就用正文，别退回到只有 Comments 的 RSS 摘要。"""
    from app.ai.processor import _fallback_digest

    with session_scope() as session:
        article = make_article(
            session,
            title="Underwater umbrellas for coral reefs",
            link="https://example.com/boiler2",
            content="Comments",
            content_full=(
                "Researchers tested umbrella-shaped coral shelters across twelve reefs "
                "and measured how much bleaching each one prevented."
            ),
        )
        article_id = article.id

    with session_scope() as session:
        out = _fallback_digest(session.get(Article, article_id), 180)
    assert out.startswith("Researchers tested")


def test_backfill_translations_fills_titles_and_digests_before_bodies(seeded_db, settings: Settings):
    """先补便宜的（标题 + 导读），正文译文放第二轮。

    正文是长文、一次要翻好几段，单篇成本是标题的十几倍；标题与导读才是首页和
    详情页最显眼的地方。限额的时候先保证这些地方是中文的。
    """
    import httpx

    prompts: list[str] = []
    body = "\n\n".join(f"This is English body paragraph number {i} in the article." for i in range(6))

    def handler(request: httpx.Request) -> httpx.Response:
        prompt = request.content.decode("utf-8", "ignore")
        prompts.append(prompt)
        if "把下面这段英文资讯导读翻译成中文" in prompt:
            out = "西雅图山岳救援队开始穿着外骨骼装备进入荒野徒步。"
        elif "科技资讯正文翻译成中文" in prompt:
            out = "这是中文译文的第" + str(len(prompts)) + "段，译完之后的长度足够通过那道长度校验门槛。" * 4
        else:
            out = "外骨骼时代开启"
        return httpx.Response(200, json={"choices": [{"message": {"content": out}}]})

    with session_scope() as session:
        article = make_article(
            session,
            title="The dawn of the age of the exoskeleton",
            status="processed",
            relevance=1,
            content_full=body,
            digest="Mountain rescue crews now hike with powered exoskeletons in the wild.",
        )
        article_id = article.id
        session.commit()

    client = _client(handler, retries=0)
    try:
        with session_scope() as session:
            backfill_translations(session, client, settings, limit=5)
    finally:
        client.close()

    # 标题与导读都在正文译文之前完成
    first_body_call = next(i for i, p in enumerate(prompts) if "科技资讯正文翻译成中文" in p)
    assert all("科技资讯正文翻译成中文" not in p for p in prompts[:first_body_call])
    assert any("英文资讯导读" in p for p in prompts[:first_body_call])
    with session_scope() as session:
        stored = session.get(Article, article_id)
        assert stored.title_zh == "外骨骼时代开启"
        assert "西雅图" in stored.digest_zh
        assert stored.content_zh
