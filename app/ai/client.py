"""LLM 客户端：纯 HTTP，兼容任意 OpenAI 兼容接口（含本地 Ollama）。"""

from __future__ import annotations

import time

import httpx

from app.utils.logger import get_logger

log = get_logger(__name__)


class LLMError(RuntimeError):
    """调用大模型失败（超时、HTTP 错误、返回体异常）。"""


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
        """发一次 chat 请求，返回首个 choice 的文本内容。"""
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
        }
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = self._http().post(self.endpoint, json=payload)
                if response.status_code >= 400:
                    raise LLMError(f"HTTP {response.status_code}: {response.text[:200]}")
                data = response.json()
                choices = data.get("choices") or []
                if not choices:
                    raise LLMError(f"返回体缺少 choices：{str(data)[:200]}")
                content = (choices[0].get("message") or {}).get("content") or ""
                if not str(content).strip():
                    raise LLMError("返回内容为空")
                return str(content).strip()
            except (httpx.HTTPError, ValueError, LLMError) as exc:
                last_error = exc
                log.warning("LLM 调用失败（第 %d 次）：%s", attempt + 1, exc)
                if attempt < self.retries:
                    time.sleep(min(2.0**attempt, 4.0))
        raise LLMError(f"调用大模型失败：{last_error}")
