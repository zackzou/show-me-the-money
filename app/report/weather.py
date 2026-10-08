"""天气节点：按城市拉取真实天气（Open-Meteo，免费无 Key），生成 emoji + Markdown 文案。

- 地理编码与天气数据来自 open-meteo.com（无需注册）；
- 文案优先让 AI 润色（穿衣建议、台风提醒等），LLM 不可用时回退到
  模板化 emoji 文案 —— 天气卡永远有内容，不会因为模型故障开天窗；
- 结果按「城市 + 日期 + 小时」缓存到内存：早报一天生成一次，
  同一天内重复预览不重复打外部接口。
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.utils.logger import get_logger
from app.utils.text import now_local

log = get_logger(__name__)

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = 8.0
CACHE_TTL_SECONDS = 3600

# WMO 天气代码 → (emoji, 中文描述)
WMO_MAP: dict[int, tuple[str, str]] = {
    0: ("☀️", "晴"),
    1: ("🌤️", "大致晴朗"),
    2: ("⛅", "多云"),
    3: ("☁️", "阴"),
    45: ("🌫️", "雾"),
    48: ("🌫️", "雾凇"),
    51: ("🌦️", "小毛毛雨"),
    53: ("🌦️", "毛毛雨"),
    55: ("🌧️", "大毛毛雨"),
    56: ("🌧️", "冻毛毛雨"),
    57: ("🌧️", "强冻毛毛雨"),
    61: ("🌦️", "小雨"),
    63: ("🌧️", "中雨"),
    65: ("🌧️", "大雨"),
    66: ("🌧️", "冻雨"),
    67: ("🌧️", "强冻雨"),
    71: ("🌨️", "小雪"),
    73: ("🌨️", "中雪"),
    75: ("❄️", "大雪"),
    77: ("❄️", "雪粒"),
    80: ("🌦️", "阵雨"),
    81: ("🌧️", "强阵雨"),
    82: ("⛈️", "暴雨"),
    85: ("🌨️", "阵雪"),
    86: ("❄️", "强阵雪"),
    95: ("⛈️", "雷暴"),
    96: ("⛈️", "雷暴伴冰雹"),
    99: ("⛈️", "强雷暴伴冰雹"),
}

# 缓存：key = f"{city}:{date}:{hour}" → (timestamp, payload)
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _wmo(code: int | None) -> tuple[str, str]:
    return WMO_MAP.get(int(code or 0), ("🌡️", "未知"))


def _clothing_advice(temp: float | None, rain_prob: float | None) -> str:
    """穿衣提示（按温度区间给一句人话）。"""
    if temp is None:
        return "注意适时增减衣物"
    if temp >= 30:
        base = "炎热，短袖短裤，注意防暑补水"
    elif temp >= 25:
        base = "偏热，短袖或薄衬衫"
    elif temp >= 18:
        base = "舒适，长袖单衣即可"
    elif temp >= 10:
        base = "偏凉，建议加一件外套"
    elif temp >= 0:
        base = "寒冷，羽绒服或厚大衣"
    else:
        base = "严寒，全套保暖装备"
    if rain_prob is not None and rain_prob >= 50:
        base += "，记得带伞 ☔"
    return base


def _typhoon_note(wind_kmh: float | None, code: int | None) -> str:
    """台风/大风提醒。风力与雷暴可能同时成立，两条都给出。"""
    notes: list[str] = []
    if wind_kmh is not None:
        if wind_kmh >= 88:
            notes.append("🌀 风力已达台风级别，务必注意安全，避免外出")
        elif wind_kmh >= 62:
            notes.append("🌪️ 大风天气，注意高空坠物")
        elif wind_kmh >= 39:
            notes.append("💨 风较大，出行注意防风")
    if code in (95, 96, 99):
        notes.append("⚡ 有雷暴天气，尽量避免户外活动")
    return "；".join(notes)


def fetch_city_weather(city: str, *, client: httpx.Client | None = None) -> dict[str, Any] | None:
    """拉一个城市的天气。城市名解析失败或网络故障返回 None。"""
    name = (city or "").strip()
    if not name:
        return None
    # 缓存桶按**北京时间**的小时（本机时区可能是任意值，Docker 里才是 TZ=Asia/Shanghai）
    cache_key = f"{name}:{now_local().strftime('%Y-%m-%d:%H')}"
    cached = _cache.get(cache_key)
    if cached and time.time() - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]
    # 顺手清掉过期条目，防止城市多了以后 _cache 只增不减
    if len(_cache) > 64:
        deadline = time.time() - CACHE_TTL_SECONDS
        for key in [k for k, (ts, _) in _cache.items() if ts < deadline]:
            _cache.pop(key, None)

    http = client or httpx.Client(timeout=TIMEOUT, follow_redirects=True)
    try:
        geo = http.get(GEOCODE_URL, params={
            "name": name, "count": 1, "language": "zh", "format": "json",
        }).json()
        # 上游被代理/网关替换成非对象 JSON（数组、字符串）时 .get 会 AttributeError，
        # 击穿到 /brief 页面 500 —— 这里统一当「取不到」处理。
        if not isinstance(geo, dict):
            log.warning("天气地理编码返回了非对象 JSON（%s）：%r", name, type(geo).__name__)
            return None
        results = geo.get("results") or []
        if not results:
            return None
        place = results[0]
        forecast = http.get(FORECAST_URL, params={
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "current": "temperature_2m,weather_code,wind_speed_10m,precipitation",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
            "timezone": "Asia/Shanghai",
            "forecast_days": 1,
        }).json()
        if not isinstance(forecast, dict):
            log.warning("天气接口返回了非对象 JSON（%s）：%r", name, type(forecast).__name__)
            return None
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
        log.warning("天气获取失败（%s）：%r", name, exc)
        return None
    finally:
        if client is None:
            http.close()

    current = forecast.get("current") or {}
    daily = forecast.get("daily") or {}
    code = current.get("weather_code")
    emoji, desc = _wmo(code)
    temp = current.get("temperature_2m")
    wind = current.get("wind_speed_10m")
    rain_prob = (daily.get("precipitation_probability_max") or [None])[0]
    payload: dict[str, Any] = {
        "city": name,
        "resolved": place.get("name") or name,
        "admin": place.get("admin1") or "",
        "emoji": emoji,
        "desc": desc,
        "temp": temp,
        "temp_min": (daily.get("temperature_2m_min") or [None])[0],
        "temp_max": (daily.get("temperature_2m_max") or [None])[0],
        "rain_prob": rain_prob,
        "wind": wind,
        "clothing": _clothing_advice(temp, rain_prob),
        "typhoon": _typhoon_note(wind, code),
    }
    _cache[cache_key] = (time.time(), payload)
    return payload


# 心情语录池：按日期取一条（同一天内稳定，不会每次刷新都变）。
# 刻意内置而不是调 LLM：早报成稿零调用，语录只是氛围，不值得花一次调用。
QUOTES: list[str] = [
    "每一个清晨，都是世界给你的新起点 ☀️",
    "把今天过好，就是对未来最好的投资 🌱",
    "慢慢来，比较快。稳住节奏，把重要的事做扎实 🧘",
    "行动是治愈焦虑的良药，先迈出第一步 👣",
    "保持好奇，保持锋利，保持温柔 ✨",
    "你不需要很厉害才能开始，但你需要开始才会很厉害 🚀",
    "认真生活的人，运气都不会太差 🍀",
    "今天也要带着笑意出门呀，世界会温柔回应你 🌈",
    "专注眼前，日拱一卒，功不唐捐 🎯",
    "愿你今天的努力，都有回响 🌟",
]


def _quote_of_day() -> str:
    # 用北京时间取当日语录（本机时区可能是任意值）
    return QUOTES[now_local().date().toordinal() % len(QUOTES)]


def _greeting() -> str:
    """早安问候（带当天日期与星期）。"""
    today = now_local().date()
    weekdays = "一二三四五六日"
    return f"🌅 早安！今天是 {today.month} 月 {today.day} 日 星期{weekdays[today.weekday()]}"


def render_weather_markdown(cities: list[str], *, client: httpx.Client | None = None) -> str:
    """把多个城市的天气渲染成「早安问候 + 天气 + 穿衣 + 语录」的晨间文案。

    格式（用户指定）：
        🌅 早安！今天是 X 月 X 日 星期X
        🌤️ **上海** · 多云（15~24°C） · 降水概率 0%
        👕 穿衣推荐：舒适，长袖单衣即可
        （多城市时每城一组）
        ✨ 心情语录
    取不到数据的城市如实标注，不静默丢失。
    """
    if not cities:
        return ""
    lines: list[str] = [_greeting(), ""]
    for city in cities:
        data = fetch_city_weather(city, client=client)
        if data is None:
            lines.append(f"❓ **{city}**：暂时取不到天气数据")
            lines.append("")
            continue
        tmin, tmax = data.get("temp_min"), data.get("temp_max")
        span = ""
        if tmin is not None and tmax is not None:
            span = f"（{round(tmin)}~{round(tmax)}°C）"
        elif data.get("temp") is not None:
            span = f"（{round(data['temp'])}°C）"
        rain = data.get("rain_prob")
        rain_txt = f" · 降水概率 {rain}%" if rain is not None else ""
        lines.append(f"{data['emoji']} **{data['city']}** · {data['desc']}{span}{rain_txt}")
        lines.append(f"👕 穿衣推荐：{data.get('clothing') or '注意适时增减衣物'}")
        typhoon = data.get("typhoon") or ""
        if typhoon:
            lines.append(typhoon)
        lines.append("")
    lines.append(f"✨ {_quote_of_day()}")
    return "\n".join(lines).strip()


def enrich_weather_node(node: dict[str, Any], *, client: httpx.Client | None = None) -> dict[str, Any]:
    """给天气节点补上实时内容（不改原 dict）。

    节点配置：``{"type": "weather", "name": ..., "cities": ["上海", "北京"]}``；
    用户手写的 ``text``（若有）作为追加说明保留在最下面。
    """
    cities = node.get("cities") or []
    if isinstance(cities, str):  # 老配置兼容
        cities = [c.strip() for c in cities.replace("，", ",").split(",") if c.strip()]
    md = render_weather_markdown([str(c) for c in cities], client=client)
    extra = str(node.get("text") or "").strip()
    if md and extra:
        md = f"{md}\n\n{extra}"
    elif not md:
        md = extra
    out = dict(node)
    out["rendered"] = md
    return out


def weather_self_check() -> dict[str, Any]:
    """连通性自检（设置页/状态页可用）：返回能否取到上海天气。"""
    data = fetch_city_weather("上海")
    if data is None:
        return {"ok": False, "detail": "open-meteo 不可达或解析失败"}
    return {"ok": True, "city": data["city"], "desc": data["desc"], "temp": data["temp"]}
