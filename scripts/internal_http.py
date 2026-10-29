#!/usr/bin/env python3
"""stdlib HTTP：集群内网地址强制直连，不走系统代理。

`urllib` 默认读取 macOS / 环境变量里的系统代理，`.7to12` 这类只在内网可达的
主机走代理后会拿到 502，必须显式绕过。
"""

from __future__ import annotations

import ipaddress
import os
from typing import Any
from urllib.parse import urlparse
from urllib.request import (
    HTTPSHandler,
    OpenerDirector,
    ProxyHandler,
    Request,
    build_opener,
    urlopen,
)

DEFAULT_DIRECT_SUFFIXES = (
    ".7to12",
    ".backsys",
    ".yc345.tv",
    ".yangcong345.com",
    ".svc.cluster.local",
)
DIRECT_SUFFIX_ENV = "API_TEST_SUITE_DIRECT_SUFFIXES"

_OPENERS: dict[Any, OpenerDirector] = {}


def direct_suffixes() -> tuple[str, ...]:
    extra = tuple(
        item.strip().lower()
        for item in (os.getenv(DIRECT_SUFFIX_ENV) or "").split(",")
        if item.strip()
    )
    return DEFAULT_DIRECT_SUFFIXES + extra


def is_direct_host(host: str) -> bool:
    name = (host or "").strip().lower().rstrip(".")
    if not name:
        return False
    try:
        return ipaddress.ip_address(name).is_private
    except ValueError:
        pass
    return any(
        name == suffix.lstrip(".") or name.endswith(suffix)
        for suffix in direct_suffixes()
    )


def _direct_opener(context: Any) -> OpenerDirector:
    opener = _OPENERS.get(context)
    if opener is None:
        handlers: list[Any] = [ProxyHandler({})]
        if context is not None:
            handlers.append(HTTPSHandler(context=context))
        opener = build_opener(*handlers)
        _OPENERS[context] = opener
    return opener


def open_url(req: Request | str, *, timeout: float, context: Any = None) -> Any:
    """内网主机直连，其余主机保留 urllib 默认（含系统代理）行为。"""
    url = req.full_url if isinstance(req, Request) else req
    if not is_direct_host(urlparse(url).hostname or ""):
        return urlopen(req, timeout=timeout, context=context)
    return _direct_opener(context).open(req, timeout=timeout)
