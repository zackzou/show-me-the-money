#!/usr/bin/env python3
"""SMTM MCP server：stdio 型 Model Context Protocol 服务（零依赖）。

把 Show Me the Money 的只读接口包成 MCP 工具，任何支持 MCP 的客户端
（Claude Desktop、Claude Code、Cursor 等）填一段配置就能用：

    {
      "mcpServers": {
        "show-me-the-money": {
          "command": "python3",
          "args": ["/path/to/show-me-the-money/scripts/smtm_mcp.py"],
          "env": { "SMTM_BASE": "http://localhost:8000" }
        }
      }
    }

协议：JSON-RPC 2.0 over stdio，实现 initialize / tools/list / tools/call
三个方法。刻意不引 mcp SDK：本项目「只配两项就能跑」，多一个依赖就多一道
安装门槛；协议本身只是行分隔的 JSON。

环境变量：
- ``SMTM_BASE``：站点地址（默认 http://localhost:8000）
- ``SMTM_TIMEOUT``：HTTP 超时秒数（默认 15）
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

BASE = os.environ.get("SMTM_BASE", "http://localhost:8000").rstrip("/")
TIMEOUT = float(os.environ.get("SMTM_TIMEOUT", "15"))

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "show-me-the-money", "version": "1.0.0"}

TOOLS = [
    {
        "name": "get_brief",
        "description": "早报精选：按用户配置好的规则选出的 TOP N 条行业热点"
                       "（默认 10 条），每条含标题、早报片段、来源、链接。"
                       "适合「今天有什么重要的事」这类问题。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_articles",
        "description": "按日期列出进日报的文章（默认今天）。返回中文标题、导读、"
                       "早报片段与链接。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD，默认今天"},
                "limit": {"type": "integer", "description": "最多几条（默认 20，上限 200）"},
            },
            "required": [],
        },
    },
    {
        "name": "get_article",
        "description": "单篇文章详情：中文标题、导读、早报片段、正文、来源与链接。",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "integer", "description": "文章 id"}},
            "required": ["id"],
        },
    },
    {
        "name": "get_daily_report",
        "description": "某天的日报全文（Markdown）。不传日期则取最新一份。",
        "inputSchema": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "YYYY-MM-DD，可省略"}},
            "required": [],
        },
    },
    {
        "name": "search_articles",
        "description": "站内搜索。scope=meta 搜标题与摘要（默认），scope=full 连正文。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "关键词"},
                "scope": {"type": "string", "enum": ["meta", "full"]},
                "limit": {"type": "integer", "description": "最多几条（默认 20）"},
            },
            "required": ["q"],
        },
    },
]


def _get(path: str, params: dict[str, object] | None = None) -> Any:
    url = BASE + path
    if params:
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def _tool_result(text: str) -> dict[str, object]:
    return {"content": [{"type": "text", "text": text}]}


def _tool_error(message: str) -> dict[str, object]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, object]:
    try:
        if name == "get_brief":
            data = _get("/api/brief")
            return _tool_result(str(data.get("text") or json.dumps(data, ensure_ascii=False)))
        if name == "list_articles":
            rows = _get("/api/articles", {
                "date": arguments.get("date"),
                "limit": min(200, int(arguments.get("limit") or 20)),
            })
            lines = []
            for row in rows:
                title = row.get("title_zh") or row.get("title") or ""
                brief = row.get("digest_zh") or row.get("digest") or ""
                lines.append(f"- [{row.get('id')}] {title}\n  {brief}\n  {row.get('link')}")
            return _tool_result("\n".join(lines) or "（这一天没有内容）")
        if name == "get_article":
            article_id = int(arguments.get("id") or 0)
            if article_id <= 0:
                return _tool_error("id 必须是正整数")
            row = _get(f"/api/articles/{article_id}")
            body = row.get("content") or row.get("content_zh") or ""
            text = (
                f"{row.get('title_zh') or row.get('title')}\n"
                f"来源：{row.get('source_name')} · {row.get('published_at')}\n\n"
                f"{row.get('digest_brief') or ''}\n\n{body}\n\n原文：{row.get('link')}"
            )
            return _tool_result(text)
        if name == "get_daily_report":
            date = arguments.get("date")
            if not date:
                reports = _get("/api/reports", {"limit": 1})
                if not reports:
                    return _tool_error("还没有日报")
                date = reports[0].get("date")
            row = _get(f"/api/reports/{date}")
            return _tool_result(str(row.get("content_md") or ""))
        if name == "search_articles":
            q = str(arguments.get("q") or "").strip()
            if not q:
                return _tool_error("请提供关键词 q")
            rows = _get("/api/search", {
                "q": q,
                "scope": arguments.get("scope") or "meta",
                "limit": min(100, int(arguments.get("limit") or 20)),
            })
            lines = []
            for row in rows:
                title = row.get("title_zh") or row.get("title") or ""
                lines.append(f"- [{row.get('id')}] {title}\n  {row.get('link')}")
            return _tool_result("\n".join(lines) or f"没有找到「{q}」相关文章")
        return _tool_error(f"未知工具：{name}")
    except urllib.error.HTTPError as exc:
        return _tool_error(f"站点返回 {exc.code}：{exc.reason}（{BASE}）")
    except urllib.error.URLError as exc:
        return _tool_error(f"连不上站点 {BASE}：{exc.reason}")
    except (ValueError, TypeError, KeyError) as exc:
        return _tool_error(f"请求参数有问题：{exc}")


def _handle(request: dict[str, object]) -> dict[str, object] | None:
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        }
    if method == "notifications/initialized":
        return None  # 通知，无响应
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = request.get("params") or {}
        if not isinstance(params, dict):
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32602, "message": "params 必须是对象"}}
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            arguments = {}
        return {"jsonrpc": "2.0", "id": request_id, "result": _call_tool(name, arguments)}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if request_id is None:
        return None
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": -32601, "message": f"不支持的方法：{method}"}}


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(request, dict):
            continue
        response = _handle(request)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
