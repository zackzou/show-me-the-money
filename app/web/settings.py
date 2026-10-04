"""模型设置：运行时改写 LLM 配置 + 厂商预设 + 连通性测试 + 简单用量记录。

为什么要这个页面：``api_base`` / ``api_key`` / ``model`` 原来只能靠环境变量或
``.env`` 改，改完要重启容器。网关换模型、换 key 是一件经常发生的事，
为它重启服务不划算。

存哪：``data/llm_settings.json``（跟着数据库目录走，备份时一起带走），
**不进版本库**。生效方式是就地改写 ``app.state.settings.llm`` ——
调度器每次建客户端都从 ``settings`` 读，所以改完下一个任务就用新的，
不用重启。

安全：key 只显示掩码，留空表示「不改动原来的 key」；页面永远不把明文 key
渲染回 HTML。
"""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.ai.client import LLMClient, LLMError
from app.config import Settings
from app.utils.logger import get_logger
from app.utils.text import now_local
from app.web.routes import _ctx, templates

log = get_logger(__name__)

settings_router = APIRouter()

SETTINGS_FILENAME = "llm_settings.json"
USAGE_FILENAME = "llm_usage.jsonl"

# 主流厂商预设：选一个就把 base/model 填好，key 自己粘。
# 只列「OpenAI 兼容」或已知路径的；base 都能在页面上再改。
PROVIDER_PRESETS: list[dict[str, Any]] = [
    {
        "id": "openai",
        "name": "OpenAI",
        "base": "https://api.openai.com/v1",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "o4-mini"],
        "note": "也兼容所有 OpenAI 兼容的中转/自建网关",
    },
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "base": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "note": "",
    },
    {
        "id": "qwen",
        "name": "阿里云百炼（Qwen）",
        "base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-max", "qwen-plus", "qwen-turbo", "qwen-long"],
        "note": "兼容模式，OpenAI 格式",
    },
    {
        "id": "glm",
        "name": "智谱 GLM",
        "base": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4-plus", "glm-4-air", "glm-4-flash", "glm-4v-plus"],
        "note": "",
    },
    {
        "id": "moonshot",
        "name": "Moonshot（Kimi）",
        "base": "https://api.moonshot.cn/v1",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        "note": "",
    },
    {
        "id": "siliconflow",
        "name": "硅基流动 SiliconFlow",
        "base": "https://api.siliconflow.cn/v1",
        "models": ["Qwen/Qwen3-235B-A22B", "deepseek-ai/DeepSeek-V3"],
        "note": "一个 key 打通很多开源模型",
    },
    {
        "id": "openrouter",
        "name": "OpenRouter",
        "base": "https://openrouter.ai/api/v1",
        "models": ["openai/gpt-4o", "google/gemini-2.5-pro", "anthropic/claude-sonnet-4"],
        "note": "",
    },
    {
        "id": "gemini",
        "name": "Google Gemini",
        "base": "https://generativelanguage.googleapis.com/v1beta/openai",
        "models": ["gemini-2.5-pro", "gemini-2.5-flash"],
        "note": "官方 OpenAI 兼容入口",
    },
    {
        "id": "ollama",
        "name": "Ollama（本机）",
        "base": "http://127.0.0.1:11434/v1",
        "models": ["qwen2.5:7b", "llama3.1:8b"],
        "note": "本机模型，key 随便填 ollama",
    },
]

PROBE_PROMPT = "用一句话说明什么是外骨骼。"


# ── 配置文件读写 ─────────────────────────────────────────────────────────


def _settings_path(settings: Settings) -> Path:
    return Path(settings.db_file).parent / SETTINGS_FILENAME


def _usage_path(settings: Settings) -> Path:
    return Path(settings.db_file).parent / USAGE_FILENAME


def _mask(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= 10:
        return "*" * len(value)
    return f"{value[:4]}{'*' * 6}{value[-4:]}"


def load_stored(settings: Settings) -> dict[str, str]:
    """读已保存的设置。文件坏掉就当没存过（不能让一个坏文件卡住启动）。"""
    path = _settings_path(settings)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, (str, int, float))}


def save_stored(settings: Settings, data: dict[str, Any]) -> None:
    path = _settings_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    # key 是凭据，别让别的本机用户读到
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def apply_stored(app_state: Any, stored: dict[str, str]) -> bool:
    """把已保存的设置套到运行中的 settings 上。返回是否真的改了。"""
    settings = getattr(app_state, "settings", None)
    if settings is None or not stored:
        return False
    llm = settings.llm
    changed = False
    for field in ("api_base", "api_key", "model"):
        value = (stored.get(field) or "").strip()
        if value and getattr(llm, field) != value:
            setattr(llm, field, value)
            changed = True
    fallback = stored.get("fallback_models") or ""
    if isinstance(fallback, str):
        models = [m.strip() for m in fallback.split(",") if m.strip()]
        if models != list(llm.fallback_models or []):
            llm.fallback_models = models
            changed = True
    if changed:
        log.info("已应用页面保存的模型设置：model=%s base=%s", llm.model, llm.api_base)
    return changed


def record_usage(settings: Settings, event: dict[str, Any]) -> None:
    """追加一条用量记录（JSONL，一行一条）。

    只是「简单版」：够看清今天调了多少次、成功多少、花了多少 token。
    不做成数据库表 —— 写入要抢锁，而抓取/处理任务正持有事务时会被 database is
    locked 拒掉；追加文件不会。

    这个函数被 :mod:`app.ai.client` 反向调用（延迟导入，避免循环依赖），
    所以只在这里 import app 包、不在模块顶层引入 client。
    """
    row = {"at": now_local().strftime("%Y-%m-%d %H:%M:%S"), **event}
    try:
        path = _usage_path(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        log.debug("用量记录写入失败：%r", exc)


def read_usage(settings: Settings, *, limit: int = 400) -> list[dict[str, Any]]:
    path = _usage_path(settings)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def usage_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """按天 + 最近一小时汇总，够看趋势就够。"""
    by_day: dict[str, dict[str, int]] = {}
    for row in rows:
        day = str(row.get("at", ""))[:10]
        bucket = by_day.setdefault(day, {"calls": 0, "ok": 0, "fail": 0, "tokens": 0})
        bucket["calls"] += 1
        if row.get("ok"):
            bucket["ok"] += 1
        else:
            bucket["fail"] += 1
        bucket["tokens"] += int(row.get("total_tokens") or 0)
    recent = rows[-20:][::-1]
    return {
        "days": sorted(by_day.items(), reverse=True)[:14],
        "total_calls": sum(b["calls"] for b in by_day.values()),
        "total_tokens": sum(b["tokens"] for b in by_day.values()),
        "recent": recent,
    }


# ── 页面 ───────────────────────────────────────────────────────────────


def _ctx_for_settings(request: Request, *, notice: dict[str, str] | None = None,
                      result: dict[str, Any] | None = None, status: int = 200) -> HTMLResponse:
    settings: Settings | None = getattr(request.app.state, "settings", None)
    rows = read_usage(settings) if settings else []
    return templates.TemplateResponse(
        request,
        "settings.html",
        _ctx(
            request,
            nav="settings",
            notice=notice or {},
            result=result or {},
            presets=PROVIDER_PRESETS,
            usage=usage_summary(rows),
            current={
                "api_base": settings.llm.api_base if settings else "",
                "api_key_masked": _mask(settings.llm.api_key) if settings else "",
                "has_key": bool(settings and settings.llm.api_key),
                "model": settings.llm.model if settings else "",
                "fallback_models": ",".join(settings.llm.fallback_models or []) if settings else "",
            },
        ),
        status_code=status,
    )


@settings_router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    """模型设置页。默认显示当前正在用的配置。"""
    return _ctx_for_settings(request)


def _probe(settings: Settings, base: str, key: str, model: str) -> dict[str, Any]:
    """真的发一次请求测连通性，并把用量记一条。"""
    started = time.time()
    event: dict[str, Any] = {"model": model, "base": base, "kind": "probe"}
    client = LLMClient(
        base,
        key,
        model,
        timeout=30.0,
        retries=0,
        temperature=0.3,
        extra_headers=settings.llm.extra_headers,
    )
    try:
        text = client.chat(PROBE_PROMPT)
    except LLMError as exc:
        event.update({"ok": False, "error": str(exc)[:300], "ms": int((time.time() - started) * 1000)})
        record_usage(settings, event)
        return {"ok": False, "error": str(exc)[:300], "ms": event["ms"]}
    finally:
        client.close()
    event.update({"ok": True, "ms": int((time.time() - started) * 1000), "reply": text.strip()[:120]})
    record_usage(settings, event)
    return {"ok": True, "reply": text.strip()[:120], "ms": event["ms"]}


@settings_router.post("/settings", response_class=HTMLResponse)
async def settings_save(request: Request) -> HTMLResponse:
    """保存模型配置。就地生效，下一个任务就用新的，不用重启。"""
    settings: Settings | None = getattr(request.app.state, "settings", None)
    if settings is None:
        raise HTTPException(status_code=500, detail="服务未初始化")
    from urllib.parse import parse_qs

    raw = (await request.body()).decode("utf-8", "replace")
    form = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items() if v}

    base = (form.get("api_base") or "").strip().rstrip("/")
    model = (form.get("model") or "").strip()
    new_key = (form.get("api_key") or "").strip()
    fallback = (form.get("fallback_models") or "").strip()

    problems = []
    if not base:
        problems.append("请填写 API Base URL")
    elif not base.startswith(("http://", "https://")):
        problems.append("API Base URL 必须以 http:// 或 https:// 开头")
    if not model:
        problems.append("请填写模型名")
    if problems:
        return _ctx_for_settings(
            request, notice={"kind": "error", "text": "；".join(problems)}, status=400
        )

    # key 留空 = 沿用当前的（页面上只显示掩码，不回显明文）
    key = new_key or settings.llm.api_key
    probe = _probe(settings, base, key, model)
    if not probe["ok"]:
        return _ctx_for_settings(
            request,
            notice={"kind": "error", "text": f"测试没通过，没有保存：{probe['error']}"},
            result=probe,
            status=400,
        )

    stored = {
        "api_base": base,
        "api_key": key,
        "model": model,
        "fallback_models": fallback,
        "saved_at": now_local().strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_stored(settings, stored)
    apply_stored(request.app.state, stored)
    return _ctx_for_settings(
        request,
        notice={"kind": "ok", "text": f"已保存并生效：{model}（测试通过，{probe['ms']}ms）"},
        result=probe,
    )


@settings_router.post("/settings/test", response_class=HTMLResponse)
async def settings_test(request: Request) -> HTMLResponse:
    """只测不存：填完先点「测试连接」，通了再保存。"""
    settings: Settings | None = getattr(request.app.state, "settings", None)
    if settings is None:
        raise HTTPException(status_code=500, detail="服务未初始化")
    from urllib.parse import parse_qs

    raw = (await request.body()).decode("utf-8", "replace")
    form = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items() if v}
    base = (form.get("api_base") or "").strip().rstrip("/")
    model = (form.get("model") or "").strip()
    key = (form.get("api_key") or "").strip() or settings.llm.api_key
    if not base or not model:
        return _ctx_for_settings(
            request, notice={"kind": "error", "text": "请先填写 API Base URL 与模型名"}, status=400
        )
    probe = _probe(settings, base, key, model)
    if probe["ok"]:
        return _ctx_for_settings(
            request,
            notice={"kind": "ok", "text": f"连接正常（{probe['ms']}ms）"},
            result=probe,
        )
    return _ctx_for_settings(
        request, notice={"kind": "error", "text": f"连接失败：{probe['error']}"}, result=probe, status=400
    )

def record_llm_call(settings: Settings, *, model: str, ok: bool,
                    prompt_chars: int, reply_chars: int,
                    ms: int = 0, total_tokens: int | None = None,
                    error: str = "") -> None:
    """给真正的 LLM 调用记一笔（由 app.ai.client 延迟导入调用）。

    抓取/处理任务的调用也走这里，所以设置页上的用量是**真实消耗**，
    不只是点「测试连接」那几下。
    """
    event: dict[str, Any] = {
        "kind": "job",
        "model": model,
        "ok": ok,
        "ms": ms,
        "prompt_chars": prompt_chars,
        "reply_chars": reply_chars,
    }
    if total_tokens is not None:
        event["total_tokens"] = total_tokens
    if error:
        event["error"] = error[:200]
    record_usage(settings, event)
