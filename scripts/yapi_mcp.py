#!/usr/bin/env python3
"""stdlib 客户端：连接 yapi-common-mcp，系统 DNS 无法解析 .7to12 时走集群 DNS。"""

from __future__ import annotations

import json
import os
import socket
import ssl
import struct
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse
from urllib.request import Request

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from internal_http import open_url  # noqa: E402

SSL_CTX = ssl._create_unverified_context()

DEFAULT_MCP_URL = "http://yapi-common-mcp.7to12/mcp"
DEFAULT_BASE_URL = "https://yapi-test.yc345.tv"
DEFAULT_CLUSTER_DNS = "10.8.8.89"


def env_or(name: str, default: str) -> str:
    return (os.getenv(name) or default).strip()


def _dns_a_query(hostname: str, server: str, timeout: float = 2.0) -> str | None:
    labels = hostname.strip(".").encode("ascii").split(b".")
    question = b"".join(bytes([len(part)]) + part for part in labels) + b"\x00"
    payload = struct.pack("!HHHHHH", 0x4321, 0x0100, 1, 0, 0, 0) + question + struct.pack("!HH", 1, 1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(payload, (server, 53))
        data, _ = sock.recvfrom(512)
    except OSError:
        return None
    finally:
        sock.close()
    if len(data) < 12:
        return None
    _, flags, qcount, acount, nscount, arcount = struct.unpack("!HHHHHH", data[:12])
    if flags & 0x000F:
        return None
    offset = 12

    def skip_name(pos: int) -> int:
        while pos < len(data):
            length = data[pos]
            if length == 0:
                return pos + 1
            if length & 0xC0:
                return pos + 2
            pos += 1 + length
        return pos

    for _ in range(qcount):
        offset = skip_name(offset) + 4
    for _ in range(acount + nscount + arcount):
        if offset + 10 > len(data):
            return None
        offset = skip_name(offset)
        rtype, _, _, rdlength = struct.unpack("!HHIH", data[offset : offset + 10])
        offset += 10
        rdata = data[offset : offset + rdlength]
        offset += rdlength
        if rtype == 1 and rdlength == 4:
            return socket.inet_ntoa(rdata)
    return None


def resolve_hostname(hostname: str, cluster_dns: str | None = None) -> tuple[str, str]:
    """返回 (用于连接的 host, 解析说明)。优先系统 DNS，失败则查集群 DNS。"""
    try:
        socket.getaddrinfo(hostname, None, socket.AF_INET)
        return hostname, "system-dns"
    except OSError:
        pass
    dns = (cluster_dns or env_or("YAPI_CLUSTER_DNS", DEFAULT_CLUSTER_DNS)).strip()
    ip = _dns_a_query(hostname, dns)
    if not ip:
        raise RuntimeError(
            f"无法解析 {hostname}：系统 DNS 失败，集群 DNS {dns} 也无 A 记录。"
            "可设置 YAPI_COMMON_MCP_URL 为可连通地址，或设置 YAPI_CLUSTER_DNS。"
        )
    return ip, f"cluster-dns:{dns}->{ip}"


def resolve_mcp_url(mcp_url: str | None = None) -> tuple[str, str]:
    url = env_or("YAPI_COMMON_MCP_URL", mcp_url or DEFAULT_MCP_URL)
    parsed = urlparse(url)
    if not parsed.hostname:
        raise ValueError(f"无效 MCP URL: {url}")
    host, how = resolve_hostname(parsed.hostname)
    if host == parsed.hostname:
        return url, how
    netloc = host
    if parsed.port:
        netloc = f"{host}:{parsed.port}"
    elif parsed.username:
        netloc = parsed.netloc.replace(parsed.hostname, host, 1)
    return urlunparse(parsed._replace(netloc=netloc)), how


def _parse_body(raw: str) -> Any:
    text = raw.strip()
    if text.startswith("event:") or "\ndata:" in text:
        for line in text.splitlines():
            if line.startswith("data:"):
                text = line[5:].strip()
                break
    return json.loads(text)


class YapiMcpClient:
    def __init__(
        self,
        *,
        mcp_url: str | None = None,
        base_url: str | None = None,
        token: str | None = None,
    ) -> None:
        self.base_url = env_or("YAPI_BASE_URL", base_url or DEFAULT_BASE_URL).rstrip("/")
        self.token = (token or os.getenv("YAPI_GLOBAL_TOKEN") or "").strip()
        if not self.token:
            raise RuntimeError("缺少 YAPI_GLOBAL_TOKEN。Skill 不内置 YApi token。")
        self.mcp_url, self.resolve_via = resolve_mcp_url(mcp_url)
        self._session_id: str | None = None
        self._next_id = 1

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "x-yapi-base-url": self.base_url,
            "x-yapi-global-token": self.token,
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
        with open_url(req, timeout=30, context=SSL_CTX) as resp:
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
                    "clientInfo": {"name": "api-test-suite-yapi-mcp", "version": "1.0"},
                },
            }
        )
        self._next_id += 1
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result.get("result") or {}

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if not self._session_id:
            self.initialize()
        payload = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
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


def unwrap_interface(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict) and isinstance(payload.get("rawData"), dict):
        return payload["rawData"]
    if isinstance(payload, dict):
        return payload
    raise RuntimeError(f"无法解析 YApi MCP 返回: {type(payload).__name__}")
