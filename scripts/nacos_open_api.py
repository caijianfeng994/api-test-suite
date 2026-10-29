#!/usr/bin/env python3
"""Nacos Open API 只读客户端。

地址、命名空间和 DataId 只从环境变量读取，Skill 不内置。
禁止使用集群内 DNS，禁止 Playwright / 浏览器。accessToken 只留在内存，不得写入产物。
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from internal_http import open_url  # noqa: E402

SSL_CTX = ssl._create_unverified_context()


def _required_env(name: str, explicit: str | None = None) -> str:
    value = (explicit or os.environ.get(name) or "").strip()
    if not value:
        raise ValueError(f"缺少环境变量 {name}。Skill 不内置数据库地址或配置项名称。")
    return value


def api_host() -> str:
    raw = _required_env("NACOS_OPEN_API_HOST").rstrip("/")
    if "svc.cluster.local" in raw:
        raise ValueError("Open API 禁止使用集群内 DNS，请设置对外可达的 NACOS_OPEN_API_HOST")
    return raw


def namespace_name(explicit: str | None = None) -> str:
    return _required_env("NACOS_NAMESPACE_NAME", explicit)


def config_data_id(explicit: str | None = None) -> str:
    return _required_env("NACOS_DATA_ID", explicit)


def credentials(
    account: str | None = None,
    password: str | None = None,
) -> tuple[str, str]:
    user = (
        (account or "").strip()
        or (os.environ.get("NACOS_SD_ACCOUNT") or "").strip()
        or (os.environ.get("NACOS_USERNAME") or "").strip()
        or (os.environ.get("NACOS_ACCOUNT") or "").strip()
    )
    if not user:
        raise ValueError(
            "缺少 Nacos 账号。请设置 NACOS_SD_ACCOUNT（或 NACOS_USERNAME），"
            "或传 --nacos-account。禁止写入默认账号。"
        )
    passwd = (
        (password or "").strip()
        or (os.environ.get("NACOS_SD_PASSWD") or "").strip()
        or (os.environ.get("NACOS_PASSWORD") or "").strip()
        or (os.environ.get("NACOS_PASSWD") or "").strip()
    )
    if not passwd:
        raise ValueError(
            "缺少 Nacos 密码。请设置 NACOS_SD_PASSWD（或 NACOS_PASSWORD），"
            "或传 --nacos-password。禁止写入默认密码。"
        )
    return user, passwd


def _request(
    method: str,
    url: str,
    *,
    data: dict[str, str] | None = None,
    token: str | None = None,
    timeout: int = 30,
) -> tuple[int, str]:
    headers = {"Accept": "application/json, text/plain, */*"}
    body = None
    if method == "POST":
        encoded = urllib.parse.urlencode(data or {}).encode("utf-8")
        body = encoded
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif data:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(data)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with open_url(req, timeout=timeout, context=SSL_CTX) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def login(username: str, password: str) -> str:
    if not password:
        raise ValueError("NACOS_SD_PASSWD 为空，禁止 login")
    url = f"{api_host()}/nacos/v1/auth/login"
    status, body = _request("POST", url, data={"username": username, "password": password})
    if status != 200:
        raise RuntimeError(f"Nacos login 失败 HTTP {status}")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Nacos login 返回非 JSON") from exc
    token = payload.get("accessToken") or payload.get("data", {}).get("accessToken")
    if not token:
        raise RuntimeError("Nacos login 未返回 accessToken")
    return str(token)


def resolve_namespace_id(token: str, show_name: str | None = None) -> tuple[str, str]:
    show_name = namespace_name(show_name)
    url = f"{api_host()}/nacos/v1/console/namespaces"
    status, body = _request("GET", url, data={"accessToken": token}, token=token)
    if status != 200:
        raise RuntimeError(f"Nacos namespaces 失败 HTTP {status}")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Nacos namespaces 返回非 JSON") from exc
    items = payload.get("data") or payload.get("namespaces") or []
    if isinstance(payload, list):
        items = payload
    for item in items:
        if not isinstance(item, dict):
            continue
        name = (
            item.get("namespaceShowName")
            or item.get("namespaceName")
            or item.get("showName")
            or ""
        )
        if str(name) == show_name:
            ns_id = item.get("namespace") or item.get("namespaceId") or ""
            if not ns_id:
                raise RuntimeError(f"命名空间 {show_name} 缺少 namespaceId")
            return str(ns_id), show_name
    raise RuntimeError(f"未找到展示名为 {show_name} 的 namespace，禁止使用 public 空 tenant")


def get_config(
    token: str,
    *,
    group: str,
    tenant: str,
    data_id: str | None = None,
) -> str:
    data_id = config_data_id(data_id)
    url = f"{api_host()}/nacos/v1/cs/configs"
    status, body = _request(
        "GET",
        url,
        data={
            "accessToken": token,
            "dataId": data_id,
            "group": group,
            "tenant": tenant,
        },
        token=token,
    )
    if status == 404:
        return ""
    if status != 200:
        raise RuntimeError(f"Nacos configs 失败 HTTP {status} group={group}")
    return body.strip()


def fetch_dict(
    *,
    groups: list[str],
    username: str,
    password: str,
    data_id: str | None = None,
    namespace: str | None = None,
) -> dict[str, Any]:
    data_id = config_data_id(data_id)
    namespace = namespace_name(namespace)
    token = login(username, password)
    tenant, ns_name = resolve_namespace_id(token, namespace)
    attempts: list[dict[str, Any]] = []
    chosen: dict[str, Any] | None = None
    for group in groups:
        try:
            raw = get_config(token, group=group, tenant=tenant, data_id=data_id)
        except RuntimeError as exc:
            attempts.append({"group": group, "status": "error", "error": str(exc)})
            continue
        if not raw:
            attempts.append({"group": group, "status": "empty"})
            continue
        attempts.append({"group": group, "status": "ok", "bytes": len(raw.encode("utf-8"))})
        chosen = {
            "content": raw,
            "group": group,
            "tenant": tenant,
            "namespace": ns_name,
            "data_id": data_id,
            "host": api_host(),
        }
        break
    del token
    if not chosen:
        return {"ok": False, "attempts": attempts, "tenant": tenant, "namespace": ns_name}
    chosen["ok"] = True
    chosen["attempts"] = attempts
    return chosen
