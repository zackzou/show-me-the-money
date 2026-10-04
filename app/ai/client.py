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


def redact(text: object, limit: int = 200) -> str:
    """把可能含密钥的片段打码后再截断，用于写日志。"""
    return _SECRET_RE.sub("***", str(text))[:limit]


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


def parse_sse(body: str) -> str:
    """把 SSE 流里的文本增量拼成完整回复。

    兼容两种流：OpenAI Chat 的 ``choices[].delta.content``，以及 Responses API 的
    ``response.output_text.delta``。Responses 的流里**同时**有增量事件和
    ``response.output_item.done`` / ``response.completed`` 携带的整段文本 ——
    两个都收会拼出两倍内容，所以有增量就只用增量，整段只在没有增量时兜底。
    """
    deltas: list[str] = []
    finals: list[str] = []
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
            text = _text_from_response_items(response.get("output") if isinstance(response, dict) else None)
            if text:
                finals.append(text)
            continue
        if isinstance(event, str) and event.startswith("response."):
            # 其余 Responses 事件（reasoning 摘要增量等）不是回答正文
            continue
        text = _content_from_payload(data, strip=False)
        if text:
            deltas.append(text)
    return ("".join(deltas) if deltas else "\n".join(finals)).strip()


class LLMClient:
    """极简 chat 客户端：带超时与重试，不依赖 openai SDK。"""

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
    ) -> None:
        self.base = base.rstrip("/")
        self.key = key
        self.model = model
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
        它不会被重试 —— 重试也还是同样的错，白花钱。
        """
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
            "stream": False,
        }
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = self._http().post(self.endpoint, json=payload)
                if response.status_code >= 400:
                    raise LLMError(f"HTTP {response.status_code}: {redact(response.text)}")
                return self._extract(response)
            except LLMFormatError:
                raise
            except (httpx.HTTPError, ValueError, LLMError) as exc:
                last_error = exc
                log.warning("LLM 调用失败（第 %d 次）：%s", attempt + 1, exc)
                if attempt < self.retries:
                    time.sleep(min(2.0**attempt, 4.0))
        raise LLMError(f"调用大模型失败：{last_error}")

    def _extract(self, response: httpx.Response) -> str:
        """从响应里取出文本，兼容 JSON / SSE / Responses API。"""
        content_type = response.headers.get("content-type", "")
        body = response.text
        looks_like_sse = "text/event-stream" in content_type or body.lstrip()[:6] in ("event:", "data: ")
        if looks_like_sse:
            text = parse_sse(body)
            if text:
                return text
            raise LLMFormatError(f"上游返回了 SSE 但解析不出内容（{self.endpoint}）：{redact(body)}")
        try:
            data = response.json()
        except ValueError as exc:
            raise LLMFormatError(
                f"上游返回的不是 JSON（content-type={content_type or '未知'}，{self.endpoint}）：{redact(body)}"
            ) from exc
        text = _content_from_payload(data)
        if not text:
            raise LLMFormatError(f"上游返回体里没有模型回复内容：{redact(body)}")
        return text
