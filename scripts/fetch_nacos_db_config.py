#!/usr/bin/env python3
"""从测试站 Nacos Open API 解析 PostgreSQL 连接（sensitive / DataId=dict）。

对齐 nacos-vscode-env-sync：login → namespace=sensitive → GET dict。
禁止 Playwright，禁止用集群内 DNS 打 HTTP。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from db_config_parse import (  # noqa: E402
    extract_postgres_block,
    group_spellings,
    parse_config_content,
    parse_postgres_blocks,
    pick_block,
    redact,
)
from db_readonly import uri_from_parts  # noqa: E402
from nacos_open_api import credentials, fetch_dict  # noqa: E402


def load_dotenv(repo_root: Path) -> None:
    env_path = repo_root / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def build_chosen(
    *,
    service_name: str,
    block: dict[str, str],
    fetched: dict[str, Any],
    preferred_fields: dict[str, str] | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    port_raw = block.get("port") or "5432"
    port = int(port_raw)
    uri = uri_from_parts(
        host=block["host"],
        port=port,
        user=block.get("user") or "postgres",
        password=block["password"],
        db=block.get("db") or block.get("dbname") or service_name,
    )
    payload: dict[str, Any] = {
        "ok": True,
        "service_name": service_name,
        "source": "nacos",
        "source_file": (
            f"nacos://{fetched.get('namespace')}/{fetched.get('data_id')}/{fetched.get('group')}"
        ),
        "engine": "postgres",
        "name": block.get("name") or service_name,
        "host": block["host"],
        "port": port,
        "user": block.get("user") or "postgres",
        "password": block["password"],
        "db": block.get("db") or block.get("dbname") or service_name,
        "sslmode": block.get("sslMode") or block.get("sslmode") or "disable",
        "uri": uri,
        "readonly": True,
        "nacos_host": fetched.get("host"),
        "nacos_namespace": fetched.get("namespace"),
        "nacos_namespace_id": fetched.get("tenant"),
        "nacos_data_id": fetched.get("data_id"),
        "nacos_group": fetched.get("group"),
        "nacos_keys_used": sorted((preferred_fields or {}).values()),
        "nacos_attempts": fetched.get("attempts") or [],
    }
    if extra:
        payload.update(extra)
    return payload


def resolve_nacos_postgres(
    *,
    service_name: str,
    groups: list[str] | None = None,
    preferred_fields: dict[str, str] | None = None,
    account: str | None = None,
    password: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    user, passwd = credentials(account, password)
    candidates = group_spellings(*(groups or []), service_name)
    fetched = fetch_dict(groups=candidates, username=user, password=passwd)
    if not fetched.get("ok"):
        payload = {
            "ok": False,
            "error": "Nacos 未拉到 sensitive dict（Group 拼写均空或接口失败）",
            "service_name": service_name,
            "source": "nacos",
            "nacos_attempts": fetched.get("attempts") or [],
            "nacos_namespace": fetched.get("namespace"),
        }
        if extra:
            payload.update(extra)
        return payload
    raw = str(fetched.get("content") or "")
    block = pick_block(parse_postgres_blocks(raw), service_name)
    if not block:
        flat = parse_config_content(raw)
        block = extract_postgres_block(
            flat,
            service_name=service_name,
            preferred_fields=preferred_fields,
        )
    if not block:
        payload = {
            "ok": False,
            "error": "Nacos dict 中未找到可连接的明文 postgres（PG_HOST/PG_DB/PG_PASSWORD）",
            "service_name": service_name,
            "source": "nacos",
            "nacos_group": fetched.get("group"),
            "nacos_attempts": fetched.get("attempts") or [],
            "nacos_key_names": sorted(parse_config_content(raw).keys()),
        }
        if extra:
            payload.update(extra)
        return payload
    return build_chosen(
        service_name=service_name,
        block=block,
        fetched=fetched,
        preferred_fields=preferred_fields,
        extra=extra,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="从测试站 Nacos 读取 PostgreSQL 配置")
    parser.add_argument("--service-name", required=True)
    parser.add_argument("--group", help="覆盖 Nacos Group，默认服务名及拼写变体")
    parser.add_argument("--nacos-account", help="覆盖 NACOS_SD_ACCOUNT，默认 nacos")
    parser.add_argument("--nacos-password", help="覆盖 NACOS_SD_PASSWD")
    parser.add_argument("--output", "-o", help="写入 JSON（含 uri，勿上传 GitLab）")
    parser.add_argument("--redact", action="store_true", help="stdout 脱敏密码")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parents[4]
    load_dotenv(repo_root)
    try:
        groups = [args.group] if args.group else None
        payload = resolve_nacos_postgres(
            service_name=args.service_name,
            groups=groups,
            account=args.nacos_account,
            password=args.nacos_password,
        )
        if args.output:
            Path(args.output).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        printed = redact(payload) if args.redact else payload
        print(json.dumps(printed, ensure_ascii=False, indent=2) + "\n", end="")
        return 0 if payload.get("ok") else 2
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
