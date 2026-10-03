"""LLM 客户端：纯 HTTP，兼容任意 OpenAI 兼容接口（含本地 Ollama 与各类网关）。

网关之间的差异比想象中多，这里做了三件事：
- 显式声明 ``stream=false``，避免上游默认走 SSE；
- 万一上游仍然返回 ``text/event-stream``，也能把 SSE 里的文本拼出来；
- 顺带兼容 Responses API 风格的 ``output_text``。
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


def _content_from_payload(data: Any) -> str:
    """从 OpenAI Chat / Completions / Responses / SSE 分片里取出文本。"""
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
            return str(content).strip()
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    return ""


def parse_sse(body: str) -> str:
    """把 SSE 流里的文本增量拼成完整回复。"""
    parts: list[str] = []
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
        text = _content_from_payload(data)
        if text:
            parts.append(text)
    return "".join(parts).strip()


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
        client: httpx.Client | None = None,
    ) -> None:
        self.base = base.rstrip("/")
        self.key = key
        self.model = model
        self.timeout = timeout
        self.retries = retries
        self.temperature = temperature
        self._client = client
        self._owns_client = client is None

    @property
    def endpoint(self) -> str:
        if self.base.endswith("/chat/completions"):
            return self.base
        return f"{self.base}/chat/completions"

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout, headers={"Authorization": f"Bearer {self.key}"})
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
