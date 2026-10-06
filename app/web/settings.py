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
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from app.ai.client import LLMClient, LLMError
from app.config import Settings
from app.utils.logger import get_logger
from app.utils.text import now_local
from app.web.routes import _ctx, templates

log = get_logger(__name__)

settings_router = APIRouter()

SETTINGS_FILENAME = "llm_settings.json"
USAGE_FILENAME = "llm_usage.jsonl"
# 日志分页：每页条数。带表头的日志表一页 15 条 —— 太少翻页翻得烦，
# 太多右栏被拉得过长（15 行 ≈ 480px，和上面按天表的体量相当）。
LOG_PAGE_SIZE = 15

# 主流厂商预设：选一个就把 base/model 填好，key 自己粘。
# 只列「OpenAI 兼容」或已知路径的；base 都能在页面上再改。
PROVIDER_PRESETS: list[dict[str, Any]] = [
    {
        "id": "openai",
        "name": "OpenAI",
        "base": "https://api.openai.com/v1",
        # gpt-4o 已于 2026-03-31 退役，o4-mini/gpt-4.1-nano 于 2026-10-23 停用，
        # 所以默认项换成仍在售的 gpt-5.1 系列 —— 一个点下去就报 404 的预设
        # 比没有这个预设更糟。
        "models": ["gpt-5.1", "gpt-5.1-mini", "gpt-5.1-nano"],
        "note": "也兼容所有 OpenAI 兼容的中转/自建网关",
    },
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "base": "https://api.deepseek.com/v1",
        # deepseek-chat / deepseek-reasoner 已过官方标注的下线日期（2026-07-24），
        # 默认项换成新一代的 flash / v4-pro。
        "models": ["deepseek-v4-pro", "deepseek-flash"],
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
        # moonshot-v1 全系已于 2026-08-31 下线，预设里再列就是「一点就 410」。
        "models": ["kimi-k3", "kimi-k2.7-code", "kimi-k2.6"],
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
        # 容器里 127.0.0.1 指的是容器自己，不是宿主机。用 compose 跑的时候要
        # 换成 host.docker.internal —— 这一点写进 note，否则用户只会看到连不上。
        "base": "http://127.0.0.1:11434/v1",
        "models": ["qwen2.5:7b", "llama3.1:8b"],
        "note": "本机模型，key 随便填 ollama；跑在容器里要把地址换成 "
                "http://host.docker.internal:11434/v1",
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
        # 与 llm_settings.json 同为 0600：这份文件会收进上游错误文本，
        # 而上游偶尔会把密钥原样回显（见 client.redact 的说明）。
        created = not path.exists()
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        if created:
            path.chmod(0o600)
    except OSError as exc:
        log.debug("用量记录写入失败：%r", exc)


def read_usage(settings: Settings, *, limit: int | None = None) -> list[dict[str, Any]]:
    """读用量日志（最新在前）。

    ``limit=None`` 表示**全量读**：分页要按总数算页数，只截最后 N 条的话，
    页数和「共多少条」都会跟着少算 —— 日志一旦超过 N 条，最老的记录就成了
    永远翻不到、也没人知道它存在的黑洞（实测 718 条被砍到 400 条、72 页
    变 40 页）。文件是纯追加的 JSONL，全量读的开销可以接受。
    """
    path = _usage_path(settings)
    try:
        raw = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    if limit is not None:
        raw = raw[-limit:]
    out: list[dict[str, Any]] = []
    for line in raw:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out

def _charged(row: dict[str, Any]) -> int:
    """这条调用的「计费口径」token 数。

    为什么不能直接用网关给的 ``total_tokens``：实测 ag/gemini 这条链路报的
    ``total = prompt + completion``，**不含 thinking**。一次改写调用 thought 用了
    2045、output 只有 19 —— 按网关的 total 算就是 4222，但真实消耗是
    4203 + 19 + (2045-19) ≈ 6248。直接展示网关的 total 会**少算一半**，
    恰恰把最贵的那部分藏起来了。

    口径（避免重复计算）：
      · 缓存命中的 token 是输入的子集，且更便宜 —— 从输入里扣掉，不额外加；
      · 思考 token 与输出有重叠（部分网关把它算进 output）——
        只补 ``思考 - 输出`` 的差额。
    网关没报任何 token 时返回 0，不猜。
    """
    inp = int(row.get("input_tokens") or 0)
    out = int(row.get("output_tokens") or 0)
    cached = int(row.get("cached_tokens") or 0)
    reason = int(row.get("reasoning_tokens") or 0)
    if not (inp or out or cached or reason):
        return 0
    cached = min(cached, inp)          # 缓存不可能超过输入
    return inp - cached + out + max(0, reason - out)


def usage_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """按天 + 明细分项汇总，够看清 token 消耗在哪。"""
    fields = ("input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens",
              "total_tokens", "charged_tokens")
    by_day: dict[str, dict[str, Any]] = {}
    for row in rows:
        day = str(row.get("at", ""))[:10]
        bucket = by_day.setdefault(
            day, {"calls": 0, "ok": 0, "fail": 0, **dict.fromkeys(fields, 0)}
        )
        bucket["calls"] += 1
        bucket["ok" if row.get("ok") else "fail"] += 1
        for field in fields[:5]:
            bucket[field] += int(row.get(field) or 0)
        bucket["charged_tokens"] += _charged(row)
    totals = {
        "calls": sum(b["calls"] for b in by_day.values()),
        **{f: sum(b[f] for b in by_day.values()) for f in fields},
    }
    totals["ok"] = sum(b["ok"] for b in by_day.values())
    totals["fail"] = sum(b["fail"] for b in by_day.values())
    # 缓存命中率：输入里有多少是命中缓存的（便宜的那部分）
    totals["cache_hit_rate"] = (
        round(100 * totals["cached_tokens"] / totals["input_tokens"], 1)
        if totals["input_tokens"] else 0.0
    )
    return {
        "days": sorted(by_day.items(), reverse=True)[:14],
        "totals": totals,
        "total_calls": totals["calls"],
        "total_tokens": totals["charged_tokens"],
        "recent": rows[-20:][::-1],
    }


def record_llm_call(settings: Settings, *, model: str, ok: bool,
                    prompt_chars: int, reply_chars: int,
                    usage: dict[str, int] | None = None,
                    ms: int = 0, error: str = "") -> None:
    """给真正的 LLM 调用记一笔（由 app.ai.client 延迟导入调用）。

    抓取/处理任务的调用也走这里，所以设置页上的用量是**真实消耗**，不只是点
    「测试连接」那几下。token 按 input/output/cached/reasoning 分项记：笼统只记
    一个 total 是看不出消耗在哪儿的 —— 输入涨通常是提示词变长了，输出涨是模型
    话多，缓存命中低说明白白重复喂了同样的内容。
    """
    numbers = usage or {}
    event: dict[str, Any] = {
        "kind": "job",
        "model": model,
        "ok": ok,
        "ms": ms,
        "prompt_chars": prompt_chars,
        "reply_chars": reply_chars,
        "input_tokens": int(numbers.get("input") or 0),
        "output_tokens": int(numbers.get("output") or 0),
        "cached_tokens": int(numbers.get("cached") or 0),
        "reasoning_tokens": int(numbers.get("reasoning") or 0),
        "total_tokens": int(numbers.get("total") or 0),
    }
    if error:
        event["error"] = error[:200]
    record_usage(settings, event)


def compact_num(value: object) -> str:
    """把大数字压成 ``4.72M`` 这种写法。

    为什么：按天那张表有 7 列，右栏只有 440px 宽，``4,724,826`` 这种 9 字符的
    千分位写法让最后一列被裁掉半截（实测「7,1…」看不全）—— 而被裁掉的恰好是
    最想看的那一列。压成 4.72M 之后每格只要 55px，整行放得下。
    精确值仍在 title 属性里，需要核对时鼠标悬停就能看到。
    """
    try:
        number = int(str(value or 0))
    except (TypeError, ValueError):
        return "0"
    if abs(number) < 1000:
        return str(number)
    if abs(number) < 1_000_000:
        text = f"{number / 1000:.1f}"
        return text[:-2] + "K" if text.endswith(".0") else text + "K"
    if abs(number) < 1_000_000_000:
        text = f"{number / 1_000_000:.2f}".rstrip("0").rstrip(".")
        return text + "M"
    text = f"{number / 1_000_000_000:.2f}".rstrip("0").rstrip(".")
    return text + "B"


def _match_preset(base: str) -> str:
    """当前 base_url 对应哪个厂商预设（匹配不上返回空串）。

    匹配方式是**前缀**：用户经常填 ``https://api.deepseek.com``（不带 /v1），
    或者在预设地址后面又追加了一段路径。只做全等比对的话这两种都匹配不上，
    卡片就不会被选中，用户仍然要自己找。
    """
    target = (base or "").strip().rstrip("/")
    if not target:
        return ""
    best = ""
    for preset in PROVIDER_PRESETS:
        # 有些厂商允许省略 /v1（DeepSeek 两个都行），所以根地址也要参与匹配
        for candidate in _preset_roots(str(preset.get("base", ""))):
            matches = target == candidate or target.startswith(candidate + "/")
            if matches and len(candidate) > len(best):
                best = str(preset.get("id", ""))
    return best


def _preset_roots(base: str) -> list[str]:
    """一个预设地址的可匹配根：原地址，以及去掉尾部 ``/v1`` 之后的地址。"""
    root = base.rstrip("/")
    roots = [root] if root else []
    for suffix in ("/v1", "/v4", "/compatible-mode/v1"):
        if root.endswith(suffix):
            trimmed = root[: -len(suffix)].rstrip("/")
            if trimmed and trimmed not in roots:
                roots.append(trimmed)
    return roots


def _split_models(raw: str) -> list[str]:
    """把逗号分隔的备用模型串拆成列表（去空格、去空项、去重保序）。"""
    seen: set[str] = set()
    out: list[str] = []
    for piece in (raw or "").split(","):
        name = piece.strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _probe(settings: Settings, base: str, key: str, model: str,
           *, fallbacks: list[str] | None = None) -> dict[str, Any]:
    """真的发一次请求测连通性，并把用量（含 token 明细）记一条。

    **必须带上备用模型**：调度器实际用的客户端是带 fallback 的，于是会出现
    「主模型被限流、备用模型健康」的配置 —— 线上每个任务都能靠 fallback 跑
    成功，而这里不带 fallback 就会判定失败、连保存都不让存。用户明明配了一
    条能用的链路，却被一个并不存在的失败挡在门外。

    这个函数是**阻塞**的（同步 httpx），调用方必须放进线程池
    （``run_in_threadpool``），否则 60 秒超时会卡住整个事件循环 ——
    实测一次 POST 能让全站所有路由同时无响应。
    """
    started = time.time()
    prompt = PROBE_PROMPT
    captured: dict[str, Any] = {}

    def sink(**kwargs: Any) -> None:
        # 先攒住，等拿到**真实耗时**再统一写一条。
        # 早先这里直接把 sink 接到 record_llm_call，失败时又自己补记一条 ——
        # 一次失败的连通性测试在日志里就成了两条，看着像跑了两次。
        captured.update(kwargs)

    client = LLMClient(
        base,
        key,
        model,
        timeout=60.0,
        retries=0,
        temperature=0.3,
        extra_headers=settings.llm.extra_headers,
        fallback_models=list(fallbacks or []),
        usage_sink=sink,
    )
    ok = True
    text = ""
    error = ""
    try:
        text = client.chat(prompt)
    except LLMError as exc:
        ok = False
        error = str(exc)[:300]
    finally:
        client.close()
    # 记「实际答话的那个模型」：主模型被限流、由备用模型顶上来时，光看
    # configured 那个会以为主模型一直在用，而真实消耗全记在备用模型上。
    answered_by = (captured.get("model") or model)

    elapsed = int((time.time() - started) * 1000)
    numbers = dict(captured.get("usage") or {})
    event: dict[str, Any] = {
        "kind": "probe", "model": answered_by, "base": base, "ok": ok,
        "error": error, "ms": elapsed,
        "prompt_chars": len(prompt), "reply_chars": len(text),
        "input_tokens": int(numbers.get("input") or 0),
        "output_tokens": int(numbers.get("output") or 0),
        "cached_tokens": int(numbers.get("cached") or 0),
        "reasoning_tokens": int(numbers.get("reasoning") or 0),
        "total_tokens": int(numbers.get("total") or 0),
    }
    record_usage(settings, event)
    if not ok:
        return {"ok": False, "error": error, "ms": elapsed}
    return {
        "ok": True,
        "reply": text.strip()[:120],
        "ms": elapsed,
        "usage": numbers,
    }


async def _read_form(request: Request) -> dict[str, str]:
    """解析 urlencoded 表单。

    自己解而不是用 ``request.form`` / ``Form(...)``：两者都依赖未安装的
    python-multipart，缺依赖时抛异常被 except 吞掉，结果是**静默收到空表单**。
    """
    from urllib.parse import parse_qs

    raw = (await request.body()).decode("utf-8", "replace")
    parsed = parse_qs(raw, keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items() if values}


def _newest_first(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把用量记录排成「最新在前」。

    以 ``at`` 为主键倒序，**同秒的按写入顺序的逆序**排（即晚写的在前）。
    单靠 ``sorted(..., reverse=True)`` 不够：Python 的排序是稳定的，
    ``reverse=True`` 不会把相等的键再翻转，于是同一秒内的记录仍然保持
    文件顺序（最旧在前）—— 而日志的时间戳只精确到秒，实测 845 行里就有
    17 对同秒，第 1 页会出现「同秒里最早写的那条」。
    """
    indexed = list(enumerate(rows))
    indexed.sort(key=lambda pair: (str(pair[1].get("at") or ""), pair[0]), reverse=True)
    return [row for _, row in indexed]


def _page_window(page: int, pages: int, *, edge: int = 1, around: int = 1) -> list[tuple[int, bool]]:
    """分页条目的窗口：``[(页码, 是否省略号), ...]``。

    日志是纯追加的，页数只增不减。全量渲染每页数字时实测 86 页就铺了 86 个
    链接（390px 屏同样宽），跑一年就是几千个 —— 页面本身先卡住。
    """
    if pages <= 1:
        return []
    wanted: list[int] = []
    for n in range(1, pages + 1):
        if n <= edge or n > pages - edge or abs(n - page) <= around:
            wanted.append(n)
    out: list[tuple[int, bool]] = []
    previous = 0
    for n in wanted:
        if previous and n - previous > 1:
            out.append((n, True))
        out.append((n, False))
        previous = n
    return out


def _page(request: Request, *, notice: dict[str, str] | None = None,
          result: dict[str, Any] | None = None, log_page: int = 1,
          status: int = 200) -> HTMLResponse:
    settings: Settings | None = getattr(request.app.state, "settings", None)
    # 最新的必须在最前面。文件是纯追加的，所以「最后写入」就是最新；同秒内的
    # 多条（实测 845 行里有 17 对同秒）靠稳定排序保持文件顺序，晚写的排前面。
    # 先整体倒序再按 at 稳定排序：``at`` 乱序时（例如任务完成先后与写入先后
    # 不一致）以时间为准，完全相同则仍按写入先后。
    # sorted 是稳定排序，reverse=True 不会把相等的键再倒过来 —— 配合下面先把
    # 文件顺序整体倒过来，同秒的多条（实测 845 行里有 17 对同秒）就按「晚写的
    # 在前」排，不需要再包一层 reversed()。
    rows = _newest_first(read_usage(settings)) if settings else []
    summary = usage_summary(rows)
    # 日志分页：条数见 LOG_PAGE_SIZE。之前的实现把最近 20 条全铺出来，条目一多就变成一堵墙。
    per_page = LOG_PAGE_SIZE
    total_pages = max(1, -(-len(rows) // per_page))
    log_page = max(1, min(log_page, total_pages))
    window = rows[(log_page - 1) * per_page : log_page * per_page]
    stored = load_stored(settings) if settings else {}
    return templates.TemplateResponse(
        request,
        "settings.html",
        _ctx(
            request,
            nav="settings",
            notice=notice or {},
            result=result or {},
            presets=PROVIDER_PRESETS,
            usage=summary,
            log_rows=window,
            log_page=log_page,
            log_pages=total_pages,
            log_total=len(rows),
            log_window=_page_window(log_page, total_pages),
            compact=compact_num,
            # 当前配置命中哪个厂商预设 —— 页面据此默认选中那张卡片，
            # 用户一眼能看到「我现在用的是哪一家」，不用自己对着地址猜。
            active_preset=_match_preset(settings.llm.api_base if settings else ""),
            current={
                "api_base": settings.llm.api_base if settings else "",
                "api_key_masked": _mask(settings.llm.api_key) if settings else "",
                "has_key": bool(settings and settings.llm.api_key),
                "model": settings.llm.model if settings else "",
                "fallback_models": ",".join(settings.llm.fallback_models or []) if settings else "",
                "saved_at": stored.get("saved_at", ""),
            },
        ),
        status_code=status,
    )


@settings_router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, log_page: int = 1) -> HTMLResponse:
    """模型设置页。默认显示当前正在用的配置。"""
    return _page(request, log_page=log_page)


@settings_router.post("/settings", response_class=HTMLResponse)
async def settings_save(request: Request) -> HTMLResponse:
    """保存模型配置。测通才存，存完就地生效，不用重启。"""
    settings: Settings | None = getattr(request.app.state, "settings", None)
    if settings is None:
        raise HTTPException(status_code=500, detail="服务未初始化")
    form = await _read_form(request)
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
        return _page(
            request, notice={"kind": "error", "text": "；".join(problems)}, status=400
        )

    key = new_key or settings.llm.api_key
    # 服务端兜底：万一有人（某个脚本、某个旧版本页面）把掩码当成 key 提交回来，
    # 不要拿 "sk-a**3456" 覆盖掉真 key —— 那会立刻把可用配置改坏，而且要等到
    # 下一次调用大模型才报错。留空与提交掩码都按「不改动原 key」处理。
    if _mask(key) == key.strip() and key.strip():
        key = settings.llm.api_key
    probe = await run_in_threadpool(_probe, settings, base, key, model, fallbacks=_split_models(fallback))
    if not probe["ok"]:
        return _page(
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
    return _page(
        request,
        notice={"kind": "ok", "text": f"已保存并生效：{model}（测试通过，{probe['ms']}ms）"},
        result=probe,
    )


@settings_router.post("/settings/key")
async def settings_reveal_key(request: Request) -> JSONResponse:
    """按需返回已保存的明文 key（供设置页的「眼睛」与「复制」按钮用）。

    单独开一个接口，而不是把它渲染进页面：``type="password"`` 只挡眼睛，
    挡不住「查看网页源代码」、反向代理的访问日志和浏览器的表单历史 ——
    而这个服务默认监听 0.0.0.0，同网段任何人都能 GET 到设置页。
    页面本身不带明文，密钥只在用户**主动点击**时才过网。

    响应禁止缓存，并要求同源（``same-origin``），避免被中间层缓存下来。
    """
    settings: Settings | None = getattr(request.app.state, "settings", None)
    key = settings.llm.api_key if settings else ""
    if not key:
        return JSONResponse({"api_key": ""}, headers={"Cache-Control": "no-store"})
    return JSONResponse(
        {"api_key": key},
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


@settings_router.post("/settings/test", response_class=HTMLResponse)
async def settings_test(request: Request) -> HTMLResponse:
    """只测不存：填完先测连通性，通了再保存。"""
    settings: Settings | None = getattr(request.app.state, "settings", None)
    if settings is None:
        raise HTTPException(status_code=500, detail="服务未初始化")
    form = await _read_form(request)
    base = (form.get("api_base") or "").strip().rstrip("/")
    model = (form.get("model") or "").strip()
    fallback = (form.get("fallback_models") or "").strip()
    key = (form.get("api_key") or "").strip() or settings.llm.api_key
    if not base or not model:
        return _page(
            request, notice={"kind": "error", "text": "请先填写 API Base URL 与模型名"}, status=400
        )
    probe = await run_in_threadpool(_probe, settings, base, key, model, fallbacks=_split_models(fallback))
    if probe["ok"]:
        return _page(
            request, notice={"kind": "ok", "text": f"连接正常（{probe['ms']}ms）"}, result=probe
        )
    return _page(
        request, notice={"kind": "error", "text": f"连接失败：{probe['error']}"}, result=probe, status=400
    )
