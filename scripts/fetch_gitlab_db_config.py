#!/usr/bin/env python3
"""从 GitLab 业务仓 configs/*.yaml 解析测试环境 PostgreSQL 连接信息。

优先 `backend/<service_name>`，再尝试精确项目路径。
配置文件优先级：local.yaml → development.yaml → test.yaml → default.yaml。
含 `${ENV}` 占位且无明文 host 的条目跳过，除非当前环境已导出对应变量。

GitLab 无明文 postgres 时，回退测试站 Nacos Open API（sensitive / DataId=dict）。
"""

from __future__ import annotations

import argparse
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

from db_config_parse import (  # noqa: E402
    is_concrete,
    parse_postgres_blocks,
    parse_top_level_name,
    pick_block,
    pick_placeholder_block,
    placeholder_fields,
    redact,
)
from db_readonly import uri_from_parts  # noqa: E402
from fetch_nacos_db_config import resolve_nacos_postgres  # noqa: E402
from internal_http import open_url  # noqa: E402

API_BASE = "https://gitlab.yc345.tv/api/v4"
SSL_CTX = ssl._create_unverified_context()
CONFIG_FILES = (
    "configs/local.yaml",
    "configs/development.yaml",
    "configs/test.yaml",
    "configs/default.yaml",
)


def load_dotenv(repo_root: Path) -> None:
    env_path = repo_root / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def gitlab_token() -> str:
    return (os.environ.get("GITLAB_PRIVATE_TOKEN") or os.environ.get("GITLAB_TOKEN") or "").strip()


def gitlab_get(path: str) -> tuple[int, str]:
    token = gitlab_token()
    if not token:
        raise RuntimeError("缺少 GITLAB_PRIVATE_TOKEN / GITLAB_TOKEN")
    url = API_BASE + path
    req = urllib.request.Request(url, headers={"PRIVATE-TOKEN": token})
    try:
        with open_url(req, timeout=30, context=SSL_CTX) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, body


def resolve_project(service_name: str, gitlab_path: str | None) -> dict[str, Any]:
    candidates: list[str] = []
    if gitlab_path:
        candidates.append(gitlab_path.strip().strip("/"))
    candidates.extend(
        [
            f"backend/{service_name}",
            service_name,
        ]
    )
    seen: set[str] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        encoded = urllib.parse.quote(path, safe="")
        status, body = gitlab_get(f"/projects/{encoded}")
        if status == 200:
            data = json.loads(body)
            return {
                "id": data["id"],
                "path_with_namespace": data["path_with_namespace"],
                "default_branch": data.get("default_branch") or "master",
            }
    query = urllib.parse.quote(service_name)
    status, body = gitlab_get(f"/projects?search={query}&simple=true&per_page=50")
    if status != 200:
        raise RuntimeError(f"GitLab 搜索项目失败 HTTP {status}: {body[:300]}")
    matches = []
    for item in json.loads(body):
        ns = item.get("path_with_namespace") or ""
        if ns.endswith(f"/{service_name}") or ns == service_name:
            matches.append(item)
    if len(matches) == 1:
        item = matches[0]
        return {
            "id": item["id"],
            "path_with_namespace": item["path_with_namespace"],
            "default_branch": item.get("default_branch") or "master",
        }
    names = [m.get("path_with_namespace") for m in matches]
    raise RuntimeError(
        f"无法唯一解析 GitLab 项目（service={service_name}）。候选: {names or '无'}。"
        "请传 --gitlab-path，例如 backend/good"
    )


def fetch_file(project_id: int, ref: str, file_path: str) -> str | None:
    encoded = urllib.parse.quote(file_path, safe="")
    status, body = gitlab_get(
        f"/projects/{project_id}/repository/files/{encoded}/raw?ref={urllib.parse.quote(ref)}"
    )
    if status == 200:
        return body
    return None


def chosen_from_block(
    *,
    service_name: str,
    project: dict[str, Any],
    ref: str,
    file_path: str,
    block: dict[str, str],
    attempts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    port_raw = block.get("port") or "5432"
    try:
        port = int(port_raw)
    except ValueError:
        return None
    uri = uri_from_parts(
        host=block["host"],
        port=port,
        user=block.get("user") or "postgres",
        password=block["password"],
        db=block.get("db") or block.get("dbname") or service_name,
    )
    return {
        "ok": True,
        "service_name": service_name,
        "source": "gitlab",
        "gitlab_project": project["path_with_namespace"],
        "gitlab_project_id": project["id"],
        "ref": ref,
        "source_file": file_path,
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
        "attempts": attempts,
    }


def scan_gitlab(
    service_name: str,
    gitlab_path: str | None,
    ref_override: str | None,
) -> dict[str, Any]:
    project = resolve_project(service_name, gitlab_path)
    ref = ref_override or project["default_branch"]
    attempts: list[dict[str, Any]] = []
    yaml_name: str | None = None
    placeholder_block: dict[str, str] | None = None
    for file_path in CONFIG_FILES:
        raw = fetch_file(project["id"], ref, file_path)
        if raw is None:
            attempts.append({"file": file_path, "status": "missing"})
            continue
        if yaml_name is None:
            yaml_name = parse_top_level_name(raw)
        blocks = parse_postgres_blocks(raw)
        block = pick_block(blocks, service_name)
        attempts.append(
            {
                "file": file_path,
                "status": "parsed" if block else "no_concrete_postgres",
                "names": [b.get("name") for b in blocks],
            }
        )
        if not placeholder_block:
            candidate = pick_placeholder_block(blocks, service_name)
            if candidate:
                placeholder_block = candidate
        if block and is_concrete(block):
            chosen = chosen_from_block(
                service_name=service_name,
                project=project,
                ref=ref,
                file_path=file_path,
                block=block,
                attempts=attempts,
            )
            if chosen:
                return chosen
    return {
        "ok": False,
        "error": "GitLab 配置中未找到可连接的明文 postgres（可能仅有 ${PG_*} 占位）",
        "service_name": service_name,
        "source": "gitlab",
        "gitlab_project": project["path_with_namespace"],
        "attempts": attempts,
        "yaml_name": yaml_name,
        "placeholder_fields": placeholder_fields(placeholder_block or {}),
    }


def fallback_nacos(
    *,
    service_name: str,
    gitlab_result: dict[str, Any] | None,
    group: str | None,
    account: str | None,
    password: str | None,
) -> dict[str, Any]:
    groups: list[str] = []
    if group:
        groups.append(group)
    if gitlab_result and gitlab_result.get("yaml_name"):
        groups.append(str(gitlab_result["yaml_name"]))
    preferred = (gitlab_result or {}).get("placeholder_fields") or {}
    extra = {
        "gitlab_attempts": (gitlab_result or {}).get("attempts") or [],
        "gitlab_project": (gitlab_result or {}).get("gitlab_project"),
        "gitlab_error": (gitlab_result or {}).get("error"),
    }
    return resolve_nacos_postgres(
        service_name=service_name,
        groups=groups,
        preferred_fields=preferred or None,
        account=account,
        password=password,
        extra=extra,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="从 GitLab 服务仓读取 PostgreSQL 配置")
    parser.add_argument("--service-name", required=True)
    parser.add_argument("--gitlab-path", help="覆盖项目路径，如 backend/good")
    parser.add_argument("--ref", help="覆盖分支，默认项目 default_branch")
    parser.add_argument("--output", "-o", help="写入 JSON（含 uri，勿上传 GitLab）")
    parser.add_argument("--redact", action="store_true", help="stdout 脱敏密码")
    parser.add_argument(
        "--no-nacos",
        action="store_true",
        help="GitLab 无明文配置时不要回退 Nacos Open API",
    )
    parser.add_argument("--nacos-group", help="覆盖 Nacos Group（默认 yaml name / 服务名）")
    parser.add_argument("--nacos-account", help="覆盖 NACOS_SD_ACCOUNT")
    parser.add_argument("--nacos-password", help="覆盖 NACOS_SD_PASSWD")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parents[4]
    load_dotenv(repo_root)
    gitlab_result: dict[str, Any] | None = None
    try:
        gitlab_result = scan_gitlab(args.service_name, args.gitlab_path, args.ref)
        if gitlab_result.get("ok"):
            payload = gitlab_result
        elif args.no_nacos:
            payload = gitlab_result
        else:
            try:
                payload = fallback_nacos(
                    service_name=args.service_name,
                    gitlab_result=gitlab_result,
                    group=args.nacos_group,
                    account=args.nacos_account,
                    password=args.nacos_password,
                )
            except Exception as nacos_exc:  # noqa: BLE001
                payload = dict(gitlab_result)
                payload["nacos_error"] = str(nacos_exc)
            if not payload.get("ok"):
                payload.setdefault("gitlab_error", gitlab_result.get("error"))
                payload.setdefault("attempts", gitlab_result.get("attempts"))
        if args.output:
            Path(args.output).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        printed = redact(payload) if args.redact else payload
        print(json.dumps(printed, ensure_ascii=False, indent=2) + "\n", end="")
        return 0 if payload.get("ok") else 2
    except Exception as exc:  # noqa: BLE001
        if args.no_nacos:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 1
        try:
            payload = fallback_nacos(
                service_name=args.service_name,
                gitlab_result={"error": str(exc), "attempts": []},
                group=args.nacos_group,
                account=args.nacos_account,
                password=args.nacos_password,
            )
        except Exception as nacos_exc:  # noqa: BLE001
            print(
                json.dumps(
                    {"ok": False, "error": str(exc), "nacos_error": str(nacos_exc)},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 1
        if args.output:
            Path(args.output).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        printed = redact(payload) if args.redact else payload
        print(json.dumps(printed, ensure_ascii=False, indent=2) + "\n", end="")
        return 0 if payload.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
