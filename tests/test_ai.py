"""AI 模块测试：提示词渲染、LLM 客户端、处理与降级。"""

from __future__ import annotations

import json
from datetime import timedelta

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
from app.utils.text import now_local

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
            "这是推送语",
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
            "推送语1",
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
    """兜底：只给了顶层 ``output_text`` 字符串的网关也能取到。

    注意这条只是**兜底**形状。真实网关（实测 9router 的 wb/hy3）从不这么回，
    它回的是 ``output`` 数组 / ``response.output_text.delta`` —— 见下面几条。
    早期就是因为只有这一条测试，整条 Responses 路径没人走，产品全英文而测试全绿。
    """
    import httpx

    from app.ai.client import LLMClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output_text": "no"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert LLMClient("http://gw/v1", "k", "m", client=client).chat("判断") == "no"


# 下面几条用的是**真实抓包**的形状（9router / wb/hy3），字段一个不少：
# reasoning 条目在前、message 条目在后，且流里既有增量事件又有整段事件。
_RESPONSES_JSON = {
    "id": "resp_cmb-7b452dee",
    "object": "response",
    "status": "completed",
    "error": None,
    "output": [
        {
            "id": "rs_resp_cmb-7b452dee_0",
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "用户要求只输出译文。"}],
        },
        {
            "id": "msg_resp_cmb-7b452dee_0",
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "output_text", "annotations": [], "logprobs": [], "text": "切换提前两年。"}
            ],
        },
    ],
}


def _sse(*events: tuple[str, dict]) -> str:
    return "".join(f"event: {name}\ndata: {json.dumps(body, ensure_ascii=False)}\n\n" for name, body in events)


def _event(name: str, seq: int, **extra: object) -> tuple[str, dict]:
    return name, {"type": name, "sequence_number": seq, **extra}


def _reasoning_item(summary: str = "") -> dict:
    """真实抓包里 reasoning 条目的形状。``summary`` 为空时是真抓包里的 ``[]``。"""
    return {
        "id": "rs_x_0",
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": summary}] if summary else [],
    }


def _message_item(text: str) -> dict:
    """真实抓包里 message 条目的形状 —— 回答就藏在这里的 output_text 块里。"""
    return {
        "id": "msg_x_0",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "annotations": [], "logprobs": [], "text": text}],
    }


def _reasoning_delta(delta: str, seq: int) -> tuple[str, dict]:
    return _event(
        "response.reasoning_summary_text.delta", seq,
        item_id="rs_x_0", output_index=0, summary_index=0, delta=delta,
    )


def _reasoning_done(text: str, seq: int) -> tuple[str, dict]:
    return _event(
        "response.reasoning_summary_text.done", seq,
        item_id="rs_x_0", output_index=0, summary_index=0, text=text,
    )


def _text_delta(delta: str, seq: int) -> tuple[str, dict]:
    return _event(
        "response.output_text.delta", seq,
        item_id="msg_x_0", output_index=0, content_index=0, delta=delta,
    )


def _text_done(text: str, seq: int) -> tuple[str, dict]:
    return _event(
        "response.output_text.done", seq,
        item_id="msg_x_0", output_index=0, content_index=0, text=text,
    )


def _item_done(item: dict, seq: int) -> tuple[str, dict]:
    return _event("response.output_item.done", seq, output_index=0, item=item)


# 真实 SSE 事件序列（关键几帧，顺序与抓包一致）：
# reasoning 增量 → reasoning 整段 → output_text 增量 → output_text 整段 → completed。
_RESPONSES_SSE = _sse(
    _event("response.created", 1, response={"id": "resp_x", "status": "in_progress", "output": []}),
    _event("response.output_item.added", 3, output_index=0, item=_reasoning_item()),
    _reasoning_delta("用户要求只输出", 5),
    _reasoning_delta("译文。", 6),
    _reasoning_done("用户要求只输出译文。", 7),
    _item_done(_reasoning_item("用户要求只输出译文。"), 8),
    _text_delta("切换提前", 389),
    _text_delta("两年。", 390),
    _text_done("切换提前两年。", 391),
    _item_done(_message_item("切换提前两年。"), 393),
    _event("response.completed", 394, response={"id": "resp_x", "output": _RESPONSES_JSON["output"]}),
) + "data: [DONE]\n\n"


def test_responses_non_stream_output_items():
    """回归（本次事故的根因）：``output`` 数组里取 message 条目的文本。

    只认顶层 ``output_text`` 的写法在这里解析出空串 → 每个 LLM 调用都抛
    ``LLMFormatError`` → 文章整篇停在英文。这条测试就是钉住这个根因。
    """
    from app.ai.client import _content_from_payload

    assert _content_from_payload(_RESPONSES_JSON) == "切换提前两年。"
    # 有些网关把 response 对象再包一层
    assert _content_from_payload({"response": _RESPONSES_JSON}) == "切换提前两年。"
    # reasoning 条目是思维摘要，绝不能当回答
    assert _content_from_payload({"output": [_RESPONSES_JSON["output"][0]]}) == ""


def test_responses_stream_uses_deltas_exactly_once():
    """回归：流里既有增量又有整段，两个都收会拼出两倍内容。"""
    from app.ai.client import parse_sse

    text = parse_sse(_RESPONSES_SSE)
    assert text == "切换提前两年。"
    assert text.count("切换提前两年。") == 1
    # 思维摘要不能混进回答
    assert "用户要求只输出译文" not in text


def test_responses_reasoning_only_yields_empty():
    """只有 reasoning 增量、没有 output_text 的流：必须判为「没有内容」。

    抓包里确实出现过这种帧（模型还在想）。把它当回答，等于把草稿当成品，
    而且会把「上游还没答完」伪装成成功。
    """
    from app.ai.client import parse_sse

    body = _sse(
        _reasoning_delta("用户要求", 5),
        _item_done(_reasoning_item("用户要求"), 6),
    )
    assert parse_sse(body) == ""


def test_chat_sse_deltas_keep_leading_space():
    """英文分片按 token 切，前导空格有意义：逐片 strip 会拼出 HelloWorld。"""
    from app.ai.client import parse_sse

    body = _sse(
        ("message", {"choices": [{"delta": {"content": "Hello"}}]}),
        ("message", {"choices": [{"delta": {"content": " world"}}]}),
    )
    assert parse_sse(body) == "Hello world"


def test_sse_done_only_falls_back_to_whole_text():
    """没有增量、只给了整段（output_text.done）时，兜底用整段。"""
    from app.ai.client import parse_sse

    body = _sse(
        ("response.output_text.done", {"type": "response.output_text.done", "text": "只有整段", "sequence_number": 2}),
    )
    assert parse_sse(body) == "只有整段"


def test_llm_extra_headers_reach_the_request():
    """``LLM_EXTRA_HEADERS`` 必须真的发出去。

    9router 靠 ``x-9router-token-saver: off`` 关掉它注入的「回答要简短」指令；
    头没发出去的话，译文会被上游悄悄压成电报体，而本地日志一切正常。
    """
    import httpx

    from app.ai.client import LLMClient

    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    llm = LLMClient(
        "http://gw/v1", "k", "m",
        extra_headers={"x-9router-token-saver": "off"},
        client=httpx.Client(
            transport=httpx.MockTransport(handler),
            headers={"Authorization": "Bearer k", "x-9router-token-saver": "off"},
        ),
    )
    assert llm.chat("判断") == "ok"
    assert seen.get("x-9router-token-saver") == "off"


def test_llm_client_builds_headers_when_owning_the_client():
    """自己建 httpx.Client 时也要带上自定义头（不能只加 Authorization）。"""
    from app.ai.client import LLMClient

    llm = LLMClient("http://gw/v1", "k", "m", extra_headers={"x-9router-token-saver": "off"})
    try:
        headers = llm._http().headers
        assert headers.get("authorization") == "Bearer k"
        assert headers.get("x-9router-token-saver") == "off"
    finally:
        llm.close()


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
        # 判为不相关时分数一律丢弃（见 test_irrelevant_verdict_yields_no_score）
        ("no 5", False, None),
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
    answers = iter(["yes", "摘要", "速览", "理由", "行业\n云计算", "EN title\nEN digest", "标签", "推送语"])
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
        "谷歌收紧模型免费访问，转向商业化变现。",
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
            # relevance/summary/digest/reason/classify + 中文标题 + 中文导读 + tags + 推送语
            assert calls["n"] == 9
            assert article.title_zh == "苹果收紧隐私设置"
            # 中文模式下导读读的是中文版，不是那段英文
            assert article.digest_zh == "谷歌将结束对 Flash 与 Pro 模型的免费访问"
            assert article.brief_zh == "谷歌收紧模型免费访问，转向商业化变现。"
    finally:
        client.close()


def test_translate_to_chinese_chunks_and_keeps_order(seeded_db, settings: Settings):
    """长正文按字符预算分块重写，顺序要保持；某块失败只丢那一块。"""
    body = "\n\n".join(
        f"Paragraph {i} of the English article body, long enough to pass the length gate."
        for i in range(1, 121)
    )
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        text = payload["messages"][0]["content"]
        seen.append(text)
        # 每块按原长度的一半重写，落在 0.25~1.3 的合格区间里
        return httpx.Response(200, json={"choices": [{"message": {"content": "这段是译文。" * 200}}]})

    client = _client(handler, retries=0)
    try:
        out = translate_to_chinese(client, settings.prompts, body)
        assert out, "英文正文应该翻得出中文"
        assert len(seen) >= 2, "长文不该一次就翻完"
        assert "Paragraph 1 " in seen[0]
        assert "Paragraph 120 " in seen[-1]
        assert out.count("这段是译文") == len(seen) * 200
        # 重写得太短（模型只回了一截 / 压成摘要）要判为失败，不写进库
        short = _client(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "太短"}}]}), retries=0)
        try:
            assert translate_to_chinese(short, settings.prompts, body) is None
        finally:
            short.close()
        # 重写得太长（在扩写复述而不是编译）同样不该收
        long_client = _client(
            lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "很长" * 4000}}]}), retries=0
        )
        try:
            assert translate_to_chinese(long_client, settings.prompts, body) is None
        finally:
            long_client.close()
    finally:
        client.close()


def test_chunk_for_rewrite_respects_char_budget():
    """回归：分块从「固定 4 段」改成「字符预算」。

    固定 4 段对重写有两个坏处：模型看不到足够上下文重组行文（还是译文味），
    且长文调用次数翻好几倍。改成约 2600 字符一块。
    """
    from app.ai.processor import TRANSLATE_CHUNK_CHARS, chunk_for_rewrite

    paras = [f"第{i}段。" + "内容" * 200 for i in range(40)]  # 每段约 400 字
    chunks = chunk_for_rewrite(paras)
    assert len(chunks) >= 2, "4000+ 字的长文应该切成多块"
    for chunk in chunks:
        # 允许最后一块偏小（尾块合并规则），但不能超过预算的 1.5 倍
        assert len(chunk) < TRANSLATE_CHUNK_CHARS * 1.5
    # 顺序与内容不能丢
    joined = "\n\n".join(chunks)
    for i in (0, 20, 39):
        assert f"第{i}段。" in joined
    # 短尾块并到上一块，不单独成块
    tail = ["主体段落。" + "内容" * 300, "很短。"]
    merged = chunk_for_rewrite(tail)
    assert len(merged) == 1
    assert "很短。" in merged[0]


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
        if "英文资讯导读改写成中文导语" in prompt:
            out = "西雅图山岳救援队开始穿着外骨骼装备进入荒野徒步。"
        elif "请**用中文重新写成一篇中国读者能顺畅读完的文章**" in prompt:
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
    marker = "用中文重新写成一篇中国读者能顺畅读完的文章"
    first_body_call = next(i for i, p in enumerate(prompts) if marker in p)
    assert all(marker not in p for p in prompts[:first_body_call])
    assert any("英文资讯导读" in p for p in prompts[:first_body_call])
    with session_scope() as session:
        stored = session.get(Article, article_id)
        assert stored.title_zh == "外骨骼时代开启"
        assert "西雅图" in stored.digest_zh
        assert stored.content_zh


def test_retry_degraded_requeues_articles_failed_by_llm(seeded_db):
    """回归：上游限流让整篇降级后，必须能放回 pending 重来一次。

    一次 429 就让 process_article 在第一个调用处抛异常、文章被标成 failed，
    而 process_pending 只捞 pending —— 不放回来，标题 / 导读 / 正文的中文版
    一个都不会有，页面上就整篇英文。
    """
    from app.ai.processor import retry_degraded

    with session_scope() as session:
        article = make_article(
            session,
            title="Capcom is preparing for a future where we create games with AI",
            link="https://example.com/llm-down",
            status="failed",
            relevance=1,
            process_attempts=1,
            process_last_at=now_local() - timedelta(hours=3),
        )
        article_id = article.id

    with session_scope() as session:
        stats = retry_degraded(session)
    assert stats["requeued"] == 1
    with session_scope() as session:
        stored = session.get(Article, article_id)
        # 放回 pending 之后，下一轮 process_pending 就会重新完整处理它
        assert stored.status == "pending"


def test_retry_degraded_backs_off_and_stops_at_the_cap(seeded_db):
    """刚试过的先退避；用完重试次数的不再放回，避免无限重试。"""
    from app.ai.processor import retry_degraded

    with session_scope() as session:
        just_tried = make_article(
            session, title="Just tried", link="https://example.com/just", status="failed",
            relevance=1, process_attempts=1, process_last_at=now_local(),
        )
        exhausted = make_article(
            session, title="Out of attempts", link="https://example.com/out", status="failed",
            relevance=1, process_attempts=99, process_last_at=now_local() - timedelta(days=1),
        )
        just_id, out_id = just_tried.id, exhausted.id

    with session_scope() as session:
        stats = retry_degraded(session)
    assert stats["requeued"] == 0
    assert stats["exhausted"] == 1
    with session_scope() as session:
        assert session.get(Article, just_id).status == "failed"
        assert session.get(Article, out_id).status == "failed"


def test_retry_degraded_leaves_processed_articles_alone(seeded_db):
    """已经处理成功的文章不该被重排队。"""
    from app.ai.processor import retry_degraded

    with session_scope() as session:
        make_article(session, title="已处理", link="https://example.com/done", status="processed")

    with session_scope() as session:
        assert retry_degraded(session)["requeued"] == 0


def test_backfill_translations_marks_missing_digest_as_done(seeded_db, settings: Settings):
    """回归：压根没有导读的文章，digest_zh 要标成已处理，不能每轮都来占名额。

    story/169 的现场：digest 本来就是空，补译轮上来就调翻译、拿回空、
    digest_zh 永远 NULL —— 下一轮又被捞回来，名额全耗在这类文章上。
    """
    import httpx

    with session_scope() as session:
        article = make_article(
            session,
            title="English headline that is definitely long enough to count as English text",
            status="processed",
            relevance=1,
            content_full="Apple says it is changing its macOS privacy settings for developers today.",
            digest=None,
        )
        article.content_zh = "苹果表示将修改 macOS 的隐私设置。"
        article_id = article.id
        session.commit()

    reply = {"choices": [{"message": {"content": "苹果修改隐私设置"}}]}
    client = _client(lambda r: httpx.Response(200, json=reply), retries=0)
    try:
        with session_scope() as session:
            stats = backfill_translations(session, client, settings, limit=10)
            assert stats["titles"] == 1
            stored = session.get(Article, article_id)
            assert stored.title_zh
            assert stored.digest_zh == ""
            # 第二轮不该再捞到它
            assert backfill_translations(session, client, settings, limit=10)["candidates"] == 0
    finally:
        client.close()


def test_process_article_irrelevant_still_archives_chinese_title(seeded_db, settings: Settings):
    """回归：不相关的也要存中文标题（story/169）。

    详情页直接链接照样可访问，中文模式顶着英文标题看着像没处理完。
    正文/导读不翻（不进日报），digest_zh 标空，页面缺译文会如实说明。
    """
    answers = iter(["no 5", "外骨骼时代开启"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": next(answers)}}]})

    client = _client(handler, retries=0)
    try:
        with session_scope() as session:
            article = make_article(
                session,
                title="The dawn of the age of the exoskeleton",
                content="English body",
                status="pending",
                relevance=None,
            )
            assert process_article(session, article, client, settings) == "processed"
            assert article.relevance == 0
            assert article.title_zh == "外骨骼时代开启"
            assert article.digest_zh == ""
            assert article.summary is None
    finally:
        client.close()


def test_backfill_translations_covers_irrelevant_titles_only(seeded_db, settings: Settings):
    """回归：补译轮要捞到不相关但缺中文标题的存量，只补标题，不碰正文。"""
    import httpx

    with session_scope() as session:
        article = make_article(
            session,
            title="English headline that is definitely long enough to count as English text",
            status="processed",
            relevance=0,
            content_full="Apple says it is changing its macOS privacy settings for developers today.",
            digest=None,
        )
        article_id = article.id
        session.commit()

    reply = {"choices": [{"message": {"content": "苹果修改隐私设置"}}]}
    client = _client(lambda r: httpx.Response(200, json=reply), retries=0)
    try:
        with session_scope() as session:
            stats = backfill_translations(session, client, settings, limit=10)
            assert stats["candidates"] == 1
            assert stats["titles"] == 1
            stored = session.get(Article, article_id)
            assert stored.title_zh == "苹果修改隐私设置"
            assert stored.digest_zh == ""
            # 正文不翻（不进日报，省调用）
            assert not stored.content_zh
            assert backfill_translations(session, client, settings, limit=10)["candidates"] == 0
    finally:
        client.close()


def test_translate_chunk_gate_fits_chinese_density(seeded_db, settings: Settings):
    """回归：英译中正常密度只有 0.3~0.45，门槛不能按英文习惯设 0.4。

    story/169 的现场：hy4 的整句译文 ratio 0.35~0.38 被 0.4 门槛误杀，
    而网关压缩的电报体是 0.26 —— 0.3 能把两者分开。
    """
    import httpx

    body = "\n\n".join("English paragraph content that is long enough to pass the gate." for _ in range(4))
    assert len(body) > 200

    def reply_of(prompt: str, percent: int) -> str:
        """按**输入长度**按比例产出译文 —— 真实模型的输出长度跟着输入走，
        固定长度的假译文一遇到「自适应拆块」就会失真。"""
        src = prompt.split("英文原文：", 1)[-1]
        want = max(1, int(len(src) * percent / 100))
        unit = "完整译文内容。"
        return (unit * (want // len(unit) + 1))[:want]

    def make(percent: int):
        def handler(request: httpx.Request) -> httpx.Response:
            prompt = request.content.decode("utf-8", "ignore")
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": reply_of(prompt, percent)}}]},
            )

        return _client(handler, retries=0)

    client = make(40)
    try:
        assert translate_to_chinese(client, settings.prompts, body)
    finally:
        client.close()

    bad = make(12)   # 压成摘要，低于 0.25
    try:
        assert translate_to_chinese(bad, settings.prompts, body) is None
    finally:
        bad.close()

    huge = make(400)  # 扩写复述，高于 1.3
    try:
        assert translate_to_chinese(huge, settings.prompts, body) is None
    finally:
        huge.close()


def test_generate_brief_zh_writes_fluent_sentence(settings: Settings):
    """早报推送语：AI 现写一段通顺的话，不是截断拼凑。"""
    import httpx

    from app.ai.processor import generate_brief_zh

    brief_text = "马斯克确认与台积电洽谈代工合作，目标1TW算力，约为全球总量一半。"
    client = _client(
        lambda r: httpx.Response(200, json={"choices": [{"message": {"content": brief_text}}]}),
        retries=0,
    )
    try:
        out = generate_brief_zh(client, settings.prompts, "标题", "马斯克确认与台积电洽谈代工合作。")
        assert out
        assert "马斯克" in out
        # 空导读不浪费调用
        assert generate_brief_zh(client, settings.prompts, "标题", "") is None
        # 拿回英文等于没写
        en_client = _client(
            lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "Elon confirms talks"}}]}),
            retries=0,
        )
        try:
            assert generate_brief_zh(en_client, settings.prompts, "标题", "英文导读内容足够长才判断") is None
        finally:
            en_client.close()
    finally:
        client.close()


def test_backfill_briefs_skips_without_chinese_digest(seeded_db, settings: Settings):
    """补推送语只找有中文导读的：没导读的不占名额。"""
    import httpx

    from app.ai.processor import backfill_briefs

    with session_scope() as session:
        good = make_article(
            session,
            title="有导读",
            link="https://example.com/brief1",
            status="processed",
            relevance=1,
            digest_zh="马斯克确认与台积电洽谈代工合作，目标1TW算力。",
        )
        nodigest = make_article(
            session,
            title="没导读",
            link="https://example.com/brief2",
            status="processed",
            relevance=1,
        )
        nodigest.digest_zh = None
        # 中文源：导读在 digest 里，digest_zh 只是空串"已处理"标记
        cnsrc = make_article(
            session,
            title="中文源文章标题",
            link="https://example.com/brief3",
            status="processed",
            relevance=1,
            digest="国内大模型发布新版本，推理成本下降一半，开发者可以直接调用。",
        )
        cnsrc.digest_zh = ""
        good_id, no_id, cn_id = good.id, nodigest.id, cnsrc.id
        session.commit()

    client = _client(
        lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "马斯克确认洽谈，目标1TW算力。"}}]}),
        retries=0,
    )
    try:
        with session_scope() as session:
            stats = backfill_briefs(session, client, settings, limit=10)
            assert stats == {"candidates": 2, "filled": 2}
            assert session.get(Article, good_id).brief_zh
            assert session.get(Article, no_id).brief_zh is None
            assert session.get(Article, cn_id).brief_zh
    finally:
        client.close()


def test_parse_sections_reads_headings(settings: Settings):
    from app.ai.processor import parse_sections

    raw = "## 背景\n\n第一段。\n\n第二段。\n\n## 进展\n\n第三段。"
    sections = parse_sections(raw)
    assert sections == [
        {"h": "背景", "t": "第一段。\n\n第二段。"},
        {"h": "进展", "t": "第三段。"},
    ]
    # 单节无标题等于没分
    assert parse_sections("第一段。\n\n第二段。") is None
    assert parse_sections("") is None


def test_structure_sections_skips_short_body(settings: Settings):
    import httpx

    from app.ai.processor import structure_sections

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "## 标题\n\n正文"}}]})

    client = _client(handler, retries=0)
    try:
        assert structure_sections(client, settings.prompts, "标题", "第一段。\n\n第二段。") is None
        assert calls["n"] == 0  # 3 段以内不值得分，不浪费调用
    finally:
        client.close()


def test_structure_sections_drops_rewritten_body(settings: Settings):
    """模型改写/省略正文时整份丢掉，宁可直排也不能排丢内容。"""
    import httpx

    from app.ai.processor import structure_sections

    body = "\n\n".join(f"这是原文第 {i} 段，有足够多的字不会被过滤掉。" for i in range(6))
    client = _client(
        lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "## 缩写\n\n一句话。"}}]}),
        retries=0,
    )
    try:
        assert structure_sections(client, settings.prompts, "标题", body) is None
    finally:
        client.close()


def test_chat_falls_back_to_secondary_model():
    """主模型 429 时换备用模型试，总尝试次数不变。"""
    import httpx

    calls = {"n": 0, "models": []}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        calls["n"] += 1
        body = _json.loads(request.content)
        calls["models"].append(body["model"])
        if body["model"] == "glm/glm-5.3":
            return httpx.Response(429, text="rate limited")
        return httpx.Response(200, json={"choices": [{"message": {"content": "备用模型回答"}}]})

    client = _client(handler, retries=1)
    try:
        client.models = ["glm/glm-5.3", "wb/hy3"]
        assert client.chat("判断") == "备用模型回答"
        assert calls["models"] == ["glm/glm-5.3", "wb/hy3"]
    finally:
        client.close()


def test_chat_format_error_is_not_retried_single_model():
    """单模型时格式错误不重试（已有用例的补充断言见 test_chat_format_error_is_not_retried）。"""
    import httpx

    client = _client(
        lambda r: httpx.Response(200, text="not json", headers={"content-type": "text/plain"}),
        retries=2,
    )
    try:
        assert client.models == ["test-model"]
    finally:
        client.close()


def test_backfill_translations_also_builds_sections(seeded_db, settings: Settings):
    """回归：补译出来的正文必须同时建好章节结构。

    早先只有 process_article（新入库）排版，补译这条路不排 —— 结果所有补出来的
    译文正文里一个标题都没有，段落横幅与本文目录永远不会出现。
    """
    import httpx

    body = "\n\n".join(f"This is English body paragraph number {i} here." for i in range(8))

    def handler(request: httpx.Request) -> httpx.Response:
        prompt = request.content.decode("utf-8", "ignore")
        if "用中文重新写成一篇中国读者能顺畅读完的文章" in prompt:
            # 长度要落在 0.25~1.3 的合格区间，否则会被长度校验拒掉
            out = (
                "## 背景\n\n这是第一段中文内容，长度足够通过长度门槛校验，不会被误判成压缩摘要。\n\n"
                "这是第二段中文内容，同样足够长，读者能看到完整的信息。\n\n"
                "## 进展\n\n这是第三段中文内容，交代了后续的进展与关键数字。\n\n"
                "这是第四段中文内容，收尾说明整体影响与限制条件。"
            )
        elif "纽约时报" in prompt and "责任编辑" in prompt:
            out = (
                "## 背景\n\n这是第一段中文内容，长度足够通过长度门槛校验，不会被误判。\n\n"
                "这是第二段中文内容，同样足够长，读者能看到完整的信息。\n\n"
                "## 进展\n\n这是第三段中文内容，交代了后续的进展与关键数字。\n\n"
                "这是第四段中文内容，收尾说明整体影响与限制条件。"
            )
        elif "英文标题" in prompt:
            out = "外骨骼时代的黎明"
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
            digest="Mountain rescue crews hike with powered exoskeletons in the wild.",
        )
        article_id = article.id

    client = _client(handler, retries=0)
    try:
        with session_scope() as session:
            backfill_translations(session, client, settings, limit=5)
    finally:
        client.close()

    with session_scope() as session:
        stored = session.get(Article, article_id)
        assert stored.content_zh, "正文译文应该补上"
        assert stored.body_sections_zh, "补译时也要建章节结构"
        import json

        sections = json.loads(stored.body_sections_zh)
        assert [s["h"] for s in sections] == ["背景", "进展"]


def test_translate_to_chinese_is_all_or_nothing(seeded_db, settings: Settings):
    """回归：正文重写必须「整篇或没有」，不能只留一半。

    早先是「丢掉失败的块、保留成功的」，实测文章 259 只留下了最后三分之一的中文
    正文（压缩比 0.09）—— 开头直接断掉，而页面完全看不出它不完整。
    残篇比没有译文更糟：没有译文时页面会明说「原文为英文，暂无中文译文」。
    """
    import httpx

    body = "\n\n".join(f"English body paragraph {i} with enough text to matter." for i in range(80))
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        prompt = request.content.decode("utf-8", "ignore")
        src = prompt.split("英文原文：", 1)[-1]
        # 第二块（从 paragraph 40 开始）**永远**写不出来：重试与拆块都救不回来，
        # 模拟上游对这个块有硬性输出上限
        if "paragraph 40 " in src:
            return httpx.Response(200, json={"choices": [{"message": {"content": "摘要。"}}]})
        want = max(1, int(len(src) * 0.4))
        unit = "这是一段足够长的中文译文，"
        return httpx.Response(
            200, json={"choices": [{"message": {"content": (unit * (want // len(unit) + 1))[:want]}}]}
        )

    client = _client(handler, retries=0)
    try:
        assert translate_to_chinese(client, settings.prompts, body) is None
        assert calls["n"] >= 3, "写不出来的块应该重试并拆块试过"
    finally:
        client.close()


def test_translate_chunk_retries_truncated_output(seeded_db, settings: Settings):
    """回归：被网关截断的块要重试，而不是整篇作废。

    实测 183 的第二块输出停在半个数字上（「…在7」），长度校验正确地拒掉了它，
    但早先没有人重试 —— 截断是随机的，那一篇就永远补不上译文。
    """
    import httpx

    body = "\n\n".join(f"English body paragraph {i} long enough to matter here." for i in range(80))
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        prompt = request.content.decode("utf-8", "ignore")
        src = prompt.split("英文原文：", 1)[-1]
        want = max(1, int(len(src) * 0.4))
        unit = "这是一段足够长的中文译文，"
        good = (unit * (want // len(unit) + 1))[:want]
        # 第 2 块第一次被网关截断，重试就好
        text = "截断了" if calls["n"] == 2 else good
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

    client = _client(handler, retries=0)
    try:
        out = translate_to_chinese(client, settings.prompts, body)
        assert out, "截断的块重试后应该能补上整篇"
        assert calls["n"] >= 3, "应该至少重试过一次"
    finally:
        client.close()


def test_whole_article_ratio_gate_rejects_partial_translation(seeded_db, settings: Settings):
    """回归：分块各自合格、拼起来却只覆盖全文一小半，也要整篇作废。

    实测文章 313：英文正文 1249 字，content_zh 只存了 64 字（翻了开头一段
    就收工，比例 0.05），页面照样认为「有中文译文」，双语模式下正文只剩一个
    中文段、英文却是完整六段。分块校验看不出这种残缺，只能按整篇兜一道。
    """
    import httpx

    from app.ai.processor import translation_is_usable

    body = "\n\n".join(
        f"English paragraph {i} with a fair amount of text in it indeed." for i in range(8)
    )
    assert len(body) > 400

    def handler(request: httpx.Request) -> httpx.Response:
        # 每块都只吐一句话：单看这一块比例很合格（甚至偏高），
        # 但整篇加起来远不到 0.25
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "这是一句话。"}}]}
        )

    client = _client(handler, retries=0)
    try:
        assert translate_to_chinese(client, settings.prompts, body) is None
    finally:
        client.close()

    # 判断本身：短译文对长原文 = 不可用
    assert not translation_is_usable(body, "这是一句话。")
    # 完整重写 = 可用（0.4 左右，正好落在要求区间内）
    assert translation_is_usable(body, "这是一段完整的中文译文，" * 17)
    # 扩写复述 = 不可用
    assert not translation_is_usable(body, "这是一段完整的中文译文，" * 200)
    # 原文太短时不看比例，不误杀
    assert translation_is_usable("Short one.", "短译文。")


# ── 判定词与评分：模型爱在 yes 和分数之间插话 ──────────────────────────

@pytest.mark.parametrize(
    ("answer", "relevant", "score"),
    [
        # 模型带了一句寒暄再回答。以前只看开头 8 个字，"Sure! Here is…"
        # 会被判成「不相关」—— 而它明明说了 yes、还给了 85 分。那篇文章会被
        # 永久写进 relevance=0，从此不进日报、不进搜索、任何列表里都看不到。
        ("Sure! Here is my assessment: yes 85", True, 85),
        ("Based on the title, yes, I would rate it 85", True, 85),
        ("**yes** 85", True, 85),
        ("The answer is yes (85)", True, 85),
        # 判定词后面隔着一整句解释才给分
        ("Yes, it is relevant. 70", True, 70),
        # 不该把句子里别的数字当成评分
        ("yes — highly relevant. Confidence: 0.9", True, None),
        ("yes 1,200 words", True, None),
        ("yes 2024 coverage of AI, score 88", True, 88),
        # 老式写法：分数在前
        ("85 yes", True, 85),
    ],
)
def test_relevance_verdict_found_anywhere_in_answer(answer, relevant, score):
    from app.ai.processor import parse_relevance

    assert parse_relevance(answer) == (relevant, score)


@pytest.mark.parametrize("answer", ["no 90", "否 95", "不相关 95"])
def test_irrelevant_verdict_yields_no_score(answer):
    """判定为不相关时**分数一律丢掉**。

    留着一个 95 分会造出自相矛盾的数据行：页面显示「AI 评分 95」，而任何只按
    score 排序或筛选的下游都会把一篇已经不进日报的文章当成高价值内容。与其指望
    每个调用方都记得同时看 relevance，不如在解析这一层就把矛盾消掉。
    """
    from app.ai.processor import parse_relevance

    assert parse_relevance(answer) == (False, None)


def test_irrelevant_article_stores_no_score(seeded_db, settings: Settings):
    """模型说「不相关 95」时，库里不能留下 score=95。"""
    import app.ai.processor as proc

    client = _client(
        lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "不相关 95"}}]}),
        retries=0,
    )
    with session_scope() as session:
        article = Article(
            title="Some unrelated thing", link="https://example.com/unrelated",
            content="x", relevance=None, status="pending", published_at=now_local(),
        )
        session.add(article)
        session.flush()
        try:
            status = proc.process_article(session, article, client, settings)
        finally:
            client.close()

    assert status == "processed"
    assert article.relevance == 0
    assert article.score is None
