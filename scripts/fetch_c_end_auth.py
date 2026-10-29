#!/usr/bin/env python3
"""Fetch C-end Authorization via Shadow login + usercore shadow/token (stdlib).

Mirrors web_cli/tokenUtil.getAppToken without requiring the requests package.
Does not print full tokens unless --print-token is set.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from internal_http import open_url  # noqa: E402

SHADOW_ENV = {
    "test": {
        "host": "shadow-test.yangcong345.com",
        "login_url": "https://shadow-test.yangcong345.com/api/auth/inner/test/login",
    },
    "stage": {
        "host": "shadow-stage.yangcong345.com",
        "login_url": "https://shadow-stage.yangcong345.com/api/auth/inner/test/login",
    },
    "online": {
        "host": "shadow.yangcong345.com",
        "login_url": "https://shadow.yangcong345.com/api/auth/inner/test/login",
    },
}

def _ssl() -> ssl.SSLContext:
    return ssl._create_unverified_context()


def _http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 15,
) -> tuple[int, Any]:
    data = None
    req_headers = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        data = json.dumps(body).encode()
        req_headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=req_headers, method=method)
    try:
        with open_url(request, timeout=timeout, context=_ssl()) as response:
            status = response.getcode()
            raw = response.read().decode(errors="replace")
    except HTTPError as exc:
        status = exc.code
        raw = exc.read().decode(errors="replace")
    except URLError as exc:
        raise RuntimeError(f"请求失败 {url}: {exc}") from exc
    try:
        return status, json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return status, {"_raw": raw[:500]}


def get_shadow_token(*, env: str) -> str:
    existing = (os.environ.get("SHADOW_AUTHORIZATION") or "").strip()
    if existing:
        return existing[7:].strip() if existing.lower().startswith("bearer ") else existing
    if env not in SHADOW_ENV:
        raise ValueError(f"不支持的 env: {env}")
    email = (os.environ.get("SHADOW_EMAIL") or "").strip()
    password = (os.environ.get("SHADOW_PASSWORD") or "").strip()
    if not email or not password:
        raise RuntimeError("缺少 SHADOW_EMAIL/SHADOW_PASSWORD 或 SHADOW_AUTHORIZATION")
    status, payload = _http_json(
        "POST",
        SHADOW_ENV[env]["login_url"],
        body={"email": email, "password": password},
    )
    if status != 200 or not isinstance(payload, dict) or not payload.get("shadowToken"):
        raise RuntimeError(f"Shadow 登录失败：HTTP {status}")
    return str(payload["shadowToken"])


def get_app_authorization(*, user_id: str, env: str = "test", role: str = "student") -> str:
    if not user_id:
        raise ValueError("user_id 不能为空")
    shadow_token = get_shadow_token(env=env)
    host = SHADOW_ENV[env]["host"]
    url = f"https://{host}/api/user/usercore/shadow/token/{user_id}?role={role}"
    status, payload = _http_json(
        "GET",
        url,
        headers={"shadowauthorization": shadow_token},
    )
    if status != 200 or not isinstance(payload, dict) or not payload.get("authorization"):
        raise RuntimeError(f"换用户 token 失败：HTTP {status}")
    token = str(payload["authorization"])
    return token if token.lower().startswith("bearer ") else f"Bearer {token}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch C-end Authorization via Shadow")
    parser.add_argument("--user-id", required=True, help="C 端 userId")
    parser.add_argument("--env", default="test", choices=sorted(SHADOW_ENV))
    parser.add_argument("--role", default="student")
    parser.add_argument(
        "--output",
        help="写入脱敏元数据 JSON（不含完整 token）",
    )
    parser.add_argument(
        "--print-token",
        action="store_true",
        help="仅向 stdout 打印完整 Authorization（供本地 export；勿贴进报告）",
    )
    args = parser.parse_args(argv)

    authorization = get_app_authorization(
        user_id=args.user_id,
        env=args.env,
        role=args.role,
    )
    redacted = {
        "ok": True,
        "userId": args.user_id,
        "env": args.env,
        "role": args.role,
        "authorization_prefix": authorization[:16] + "...",
        "authorization_env": "API_TEST_AUTHORIZATION",
        "headers_shape": {
            "Authorization": "${API_TEST_AUTHORIZATION}",
            "uid": args.user_id,
            "client-version": "7.78.0",
            "Referer": {
                "test": "https://7to12-test.yangcong345.com/",
                "stage": "https://7to12-stage.yangcong345.com/",
                "online": "https://7to12.yangcong345.com/",
            }.get(args.env, "https://7to12-test.yangcong345.com/"),
            "Accept": "application/json",
        },
        "source": "api:fetch_c_end_auth.get_app_authorization",
    }
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(
            json.dumps(redacted, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if args.print_token:
        print(authorization)
    else:
        print(json.dumps(redacted, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
