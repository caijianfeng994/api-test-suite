#!/usr/bin/env python3
"""stdlib 客户端：连接 CodeWiki MCP（与 yapi_mcp 同一套 streamable HTTP）。"""

from __future__ import annotations

import json
import os
import ssl
import sys
from pathlib import Path
from typing import Any
from urllib.request import Request

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from internal_http import open_url  # noqa: E402

DEFAULT_MCP_URL = "https://codewiki-mcp-inner.yc345.tv/api/mcp"
def env_or(name: str, default: str) -> str:
    return (os.getenv(name) or default).strip()


def _parse_body(raw: str) -> Any:
    text = raw.strip()
    if text.startswith("event:") or "\ndata:" in text:
        for line in text.splitlines():
            if line.startswith("data:"):
                text = line[5:].strip()
                break
    return json.loads(text)


class CodewikiMcpClient:
    def __init__(self, *, mcp_url: str | None = None, token: str | None = None) -> None:
        self.mcp_url = env_or("CODEWIKI_MCP_URL", mcp_url or DEFAULT_MCP_URL)
        raw_token = (token or os.getenv("CODEWIKI_MCP_TOKEN") or "").strip()
        if not raw_token:
            raise RuntimeError("缺少 CODEWIKI_MCP_TOKEN。Skill 不内置 CodeWiki token。")
        self.token = raw_token[7:].strip() if raw_token.lower().startswith("bearer ") else raw_token
        self._session_id: str | None = None
        self._next_id = 1
        self._ssl = ssl._create_unverified_context()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _post(self, payload: dict[str, Any]) -> Any:
        req = Request(
            self.mcp_url,
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        with open_url(req, timeout=60, context=self._ssl) as resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self._session_id = sid
            body = resp.read().decode("utf-8").strip()
        if not body:
            return {}
        data = _parse_body(body)
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"MCP error: {data['error']}")
        return data

    def initialize(self) -> dict[str, Any]:
        result = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "api-test-suite-codewiki-mcp", "version": "1.0"},
                },
            }
        )
        self._next_id += 1
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result.get("result") or {}

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        if not self._session_id:
            self.initialize()
        payload = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )
        self._next_id += 1
        result = payload.get("result") if isinstance(payload, dict) else payload
        if isinstance(result, dict) and isinstance(result.get("content"), list):
            texts: list[str] = []
            for item in result["content"]:
                if isinstance(item, dict) and item.get("type") == "text":
                    texts.append(str(item.get("text") or ""))
                elif isinstance(item, str):
                    texts.append(item)
            joined = "\n".join(texts).strip()
            if joined:
                try:
                    return json.loads(joined)
                except json.JSONDecodeError:
                    return joined
        return result
