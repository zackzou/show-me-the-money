"""LLM 客户端：纯 HTTP，兼容任意 OpenAI 兼容接口（含本地 Ollama 与各类网关）。

网关之间的差异比想象中多，这里做了四件事：
- 显式声明 ``stream=false``，避免上游默认走 SSE；
- 万一上游仍然返回 ``text/event-stream``，也能把 SSE 里的文本拼出来；
- 兼容 Responses API：既认 ``{"output": [{"type": "message", ...}]}`` 这种完整体，
  也认 ``response.output_text.delta`` 这种增量事件；
- 只取 ``type == "message"`` 的条目，不把 reasoning 摘要当成回答。

最后一条是实测踩出来的：只认顶层 ``output_text`` 字符串的写法，碰到
「返回 ``output`` 数组」的网关会解析出空串，于是每个 LLM 调用都抛
``LLMFormatError``，文章整篇停在英文 —— 而测试用的是标准 chat 格式的假网关，
这条路径根本没人走，问题就一直藏着。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx

from app.utils.logger import get_logger

log = get_logger(__name__)

# 兜底脱敏：某些网关会把请求头原样回显在错误体里，别让 API Key 落进日志。
_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{6,}|Bearer\s+[A-Za-z0-9._\-]{6,})")


def redact(text: object, limit: int = 200, secret: str | None = None) -> str:
    """把可能含密钥的片段打码后再截断，用于写日志。

    正则只能认 ``sk-`` / ``Bearer `` 这两种形状，而网关回显密钥的方式远不止
    这两种：把 key 放在 ``x-api-key`` 头、查询串里，或者 GLM 那种
    ``<id>.<secret>`` 形式，都不在覆盖范围内 —— 实测一个不带 ``Bearer ``
    前缀的密钥被原样写进了日志文件，也渲染进了设置页。

    所以除了形状匹配，还把**本次实际发出去的那个密钥**本身替换掉。这是唯一
    能保证不漏的办法：不管上游用什么格式回显，它都会原样包含这个字符串。
    """
    out = str(text)
    if secret and len(secret) >= 8:
        out = out.replace(secret, "***")
    return _SECRET_RE.sub("***", out)[:limit]


class LLMError(RuntimeError):
    """调用大模型失败（超时、HTTP 错误、返回体异常）。"""


class LLMFormatError(LLMError):
    """返回体不是能解析的模型输出。这类问题重试没有意义，直接失败。"""


# Responses API 里真正承载「助手回答」的内容块类型。``reasoning`` 条目的
# summary 也是文本，但那是模型的思维摘要，当回复用等于把草稿当答案。
_RESPONSE_TEXT_BLOCK_TYPES = ("output_text", "text")


def _text_from_response_items(items: Any) -> str:
    """从 Responses API 的 ``output`` 数组里取助手文本（只认 ``message`` 条目）。"""
    if not isinstance(items, list):
        return ""
    parts: list[str] = []
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if isinstance(content, str):
            if content.strip():
                parts.append(content.strip())
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") not in _RESPONSE_TEXT_BLOCK_TYPES:
                continue
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n".join(parts).strip()


def _content_from_payload(data: Any, *, strip: bool = True) -> str:
    """从 OpenAI Chat / Completions / Responses / SSE 分片里取出文本。

    ``strip=False`` 时保留首尾空白 —— SSE 的分片是按 token 切的，英文分片
    常带前导空格，逐片 strip 再拼会拼出 ``HelloWorld``。
    """
    if not isinstance(data, dict):
        return ""
    for choice in data.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        # 非流式在 message.content，流式分片在 delta.content
        content = (choice.get("message") or {}).get("content")
        if content is None:
            content = (choice.get("delta") or {}).get("content")
        if content is None:
            content = choice.get("text")
        if isinstance(content, list):  # 部分网关把内容拆成内容块数组
            content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        if content and str(content).strip():
            return str(content).strip() if strip else str(content)
    # Responses API（非流式）：{"output": [{"type": "message", "content": [...]}]}
    text = _text_from_response_items(data.get("output"))
    if text:
        return text
    # 有些网关把整个 response 对象包一层（``response.completed`` 的 body）
    response = data.get("response")
    if isinstance(response, dict):
        text = _text_from_response_items(response.get("output"))
        if text:
            return text
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip() if strip else output_text
    return ""


def parse_sse(body: str, *, with_usage: bool = False) -> Any:
    """把 SSE 流里的文本增量拼成完整回复。

    兼容两种流：OpenAI Chat 的 ``choices[].delta.content``，以及 Responses API 的
    ``response.output_text.delta``。Responses 的流里**同时**有增量事件和
    ``response.output_item.done`` / ``response.completed`` 携带的整段文本 ——
    两个都收会拼出两倍内容，所以有增量就只用增量，整段只在没有增量时兜底。
    """
    deltas: list[str] = []
    finals: list[str] = []
    usage = {"input": 0, "output": 0, "cached": 0, "cache_write": 0, "reasoning": 0, "total": 0}
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        chunk = line[len("data:") :].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            data = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        event = data.get("type")
        if event == "response.output_text.delta":
            delta = data.get("delta")
            if isinstance(delta, str):
                deltas.append(delta)
            continue
        if event == "response.output_text.done":
            text = data.get("text")
            if isinstance(text, str) and text.strip():
                finals.append(text)
            continue
        if event == "response.output_item.done":
            text = _text_from_response_items([data.get("item")])
            if text:
                finals.append(text)
            continue
        if event == "response.completed":
            response = data.get("response")
            if isinstance(response, dict):
                text = _text_from_response_items(response.get("output"))
                if text:
                    finals.append(text)
                found = parse_usage(response)
                if found["total"]:
                    usage = found
            continue
        if isinstance(event, str) and event.startswith("response."):
            # 其余 Responses 事件（reasoning 摘要增量等）不是回答正文
            continue
        if isinstance(data.get("usage"), dict):
            found = parse_usage(data)
            if found["total"]:
                usage = found
        text = _content_from_payload(data, strip=False)
        if text:
            deltas.append(text)
    result = "".join(deltas) if deltas else "\n".join(finals).strip()
    return (result, usage) if with_usage else result


# ── token 用量：各网关字段名不统一，统一归一 ────────────────────────────────
# OpenAI 风格叫 prompt_tokens/completion_tokens，Gemini 直连风格叫
# input_tokens/output_tokens / cached_content_token_count，
# 还有的把「思考 token」单列。设置页要展示真实消耗，就都得认。
def _int_of(source: Any, *names: str) -> int:
    if not isinstance(source, dict):
        return 0
    for name in names:
        value = source.get(name)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return max(0, int(value))
    return 0


def parse_usage(data: Any) -> dict[str, int]:
    """从返回体里归一出 token 用量。认不出的字段给0，不猜。

    认得的键：``input``（喂进去的）、``output``（模型吐出来的）、
    ``cached``（命中缓存、便宜的那部分）、``reasoning``（思考 token，
    有些网关把它算在 output 之外）、``total``。
    """
    if not isinstance(data, dict):
        return {"input": 0, "output": 0, "cached": 0, "cache_write": 0, "reasoning": 0, "total": 0}
    usage = data.get("usage")
    if not isinstance(usage, dict):
        # 有些网关把 usage 放在 response.completed 事件里
        nested = data.get("response")
        usage = nested.get("usage") if isinstance(nested, dict) else None
    if not isinstance(usage, dict):
        return {"input": 0, "output": 0, "cached": 0, "cache_write": 0, "reasoning": 0, "total": 0}
    input_tokens = _int_of(
        usage, "prompt_tokens", "input_tokens", "inputTokens", "promptTokenCount"
    )
    output_tokens = _int_of(
        usage, "completion_tokens", "output_tokens", "outputTokens", "candidatesTokenCount"
    )
    cached = _int_of(usage.get("prompt_tokens_details"), "cached_tokens") + _int_of(
        usage, "cached_tokens", "cached_content_token_count", "cachedContentTokenCount"
    )
    # 缓存**写入**是另一回事。Anthropic 系的 cache_read / cache_creation 是两个
    # 字段，而 OpenAI 兼容层普遍只映射了 read：只认 read 的写法在 Anthropic
    # 形状的返回体上一个都识别不到，命中率显示成 0，write token 还被当成
    # 普通输入按原价计费（实际 write 比未命中更贵）。
    cache_write = _int_of(
        usage, "cache_creation_input_tokens", "cache_creation", "cacheCreationInputTokens"
    )
    reasoning = _int_of(
        usage.get("completion_tokens_details"), "reasoning_tokens"
    ) + _int_of(usage, "reasoning_tokens", "thoughtsTokenCount")
    total = _int_of(usage, "total_tokens", "totalTokenCount", "totalTokenCount")
    if not total:
        # 思考 token 有些网关算在 output 之外，不加进去总量就对不上账单
        total = input_tokens + output_tokens + max(0, reasoning - output_tokens)
    return {
        "input": input_tokens,
        "output": output_tokens,
        "cached": cached,
        "cache_write": cache_write,
        "reasoning": reasoning,
        "total": total,
    }


class LLMClient:
    """极简 chat 客户端：带超时、重试与多模型 fallback，不依赖 openAI SDK。"""

    def __init__(
        self,
        base: str,
        key: str,
        model: str,
        *,
        timeout: float = 60.0,
        retries: int = 2,
        temperature: float = 0.3,
        extra_headers: dict[str, str] | None = None,
        client: httpx.Client | None = None,
        fallback_models: list[str] | None = None,
        usage_sink: Any | None = None,
    ) -> None:
        self.base = base.rstrip("/")
        self.key = key
        self.model = model
        # 主模型 429/503/401 时按顺序试备选，总尝试次数不变（不额外多花调用）
        self.models = [model] + [m for m in (fallback_models or []) if m and m != model]
        # 用量回调（可选）。设置页要显示真实消耗，就挂在这里而不是在每个
        # 业务调用点各写一遍 —— 漏一处就是用量对不上。
        self.usage_sink = usage_sink
        # 最近一次调用的 token 用量（由 _extract 填），记到账上时带上
        self.last_usage: dict[str, int] = {
            "input": 0, "output": 0, "cached": 0, "cache_write": 0, "reasoning": 0, "total": 0,
        }
        self.timeout = timeout
        self.retries = retries
        self.temperature = temperature
        # 网关自定义头（``LLM_EXTRA_HEADERS``）。中转网关常靠请求头开关行为，
        # 例如 9router 的 ``x-9router-token-saver: off`` —— 不开的话它会往
        # system 里注入一段「回答要尽量简短」的指令，与「完整翻译」直接打架，
        # 译文会随机变成电报体。
        self.extra_headers = dict(extra_headers or {})
        self._client = client
        self._owns_client = client is None

    @property
    def endpoint(self) -> str:
        if self.base.endswith("/chat/completions"):
            return self.base
        return f"{self.base}/chat/completions"

    def _http(self) -> httpx.Client:
        if self._client is None:
            headers = {"Authorization": f"Bearer {self.key}", **self.extra_headers}
            self._client = httpx.Client(timeout=self.timeout, headers=headers)
        return self._client

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def chat(self, prompt: str) -> str:
        """发一次 chat 请求，返回回复文本。

        返回体格式不对（HTML 错误页、SSE、Responses 风格）时抛 ``LLMFormatError``，
        它不会被重试 —— 同一个模型重试也还是同样的错；但会换下一个模型试
        （不同网关路径的格式可能不一样），所有模型都失败才抛出来。
        总尝试次数还是 ``retries + 1``，模型之间轮换，不额外多花调用。
        """
        last_error: Exception | None = None
        attempts = self.retries + 1
        for attempt in range(attempts):
            model = self.models[attempt % len(self.models)]
            if model != self.model and attempt > 0:
                log.info("主模型 %s 不可用，试 %s（第 %d 次）", self.model, model, attempt + 1)
            started = time.monotonic()
            try:
                self.last_usage = {"input": 0, "output": 0, "cached": 0, "cache_write": 0, "reasoning": 0, "total": 0}
                reply = self._chat_once(prompt, model)
                self._record(prompt, model, ok=True, reply=reply,
                             usage=self.last_usage, started=started)
                return reply
            except LLMFormatError as exc:
                self._record(prompt, model, ok=False, error=str(exc), started=started)
                # 单模型时格式错误重试没有意义（同一个错）；有备选才换模型试，
                # 不同网关路径的格式可能不一样
                if len(self.models) == 1:
                    raise
                last_error = exc
                log.warning("LLM 调用失败（%s，第 %d 次）：%s", model, attempt + 1, exc)
                if attempt < attempts - 1:
                    time.sleep(min(2.0**attempt, 4.0))
            except (httpx.HTTPError, ValueError, LLMError) as exc:
                last_error = exc
                self._record(prompt, model, ok=False, error=str(exc), started=started)
                log.warning("LLM 调用失败（%s，第 %d 次）：%s", model, attempt + 1, exc)
                if attempt < attempts - 1:
                    time.sleep(min(2.0**attempt, 4.0))
        raise LLMError(f"调用大模型失败：{last_error}")

    def _record(self, prompt: str, model: str, *, ok: bool,
                reply: str = "", error: str = "", usage: dict[str, int] | None = None,
                started: float | None = None) -> None:
        """把这次调用报给用量回调。回调自己出错绝不能影响主流程。

        ``started`` 是 ``chat()`` 里打的单调时钟。少了它，写进日志的 ms 恒为 0
        —— 一次跑了 9 秒的抓取在设置页上也显示「0ms」，那一列就成了摆设。
        """
        if self.usage_sink is None:
            return
        ms = 0 if started is None else int((time.monotonic() - started) * 1000)
        try:
            self.usage_sink(
                model=model,
                ok=ok,
                ms=ms,
                prompt_chars=len(prompt),
                reply_chars=len(reply),
                usage=usage or {},
                error=error,
            )
        except Exception as exc:  # 兜底：记账失败不能拖垮抓取
            log.debug("用量记录失败：%r", exc)

    def _chat_once(self, prompt: str, model: str) -> str:
        """用指定模型发一次请求。"""
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
            "stream": False,
        }
        response = self._http().post(self.endpoint, json=payload)
        if response.status_code >= 400:
            raise LLMError(f"HTTP {response.status_code}: {redact(response.text, secret=self.key)}")
        return self._extract(response)

    def _extract(self, response: httpx.Response) -> str:
        """从响应里取出文本，兼容 JSON / SSE / Responses API。"""
        content_type = response.headers.get("content-type", "")
        body = response.text
        looks_like_sse = "text/event-stream" in content_type or body.lstrip()[:6] in ("event:", "data: ")
        if looks_like_sse:
            text, sse_usage = parse_sse(body, with_usage=True)
            if text:
                self.last_usage = sse_usage
                return text
            raise LLMFormatError(f"上游返回了 SSE 但解析不出内容（{self.endpoint}）：{redact(body, secret=self.key)}")
        try:
            data = response.json()
        except ValueError as exc:
            raise LLMFormatError(
                f"上游返回的不是 JSON（content-type={content_type or '未知'}，"
                f"{self.endpoint}）：{redact(body, secret=self.key)}"
            ) from exc
        self.last_usage = parse_usage(data)
        text = _content_from_payload(data)
        if not text:
            raise LLMFormatError(f"上游返回体里没有模型回复内容：{redact(body, secret=self.key)}")
        return text
