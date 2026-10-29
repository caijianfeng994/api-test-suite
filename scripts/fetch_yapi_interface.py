#!/usr/bin/env python3
"""通过 yapi-common-mcp 拉取接口详情。Cursor 会话未挂载 YApi MCP 时由 discovery 调用。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from yapi_mcp import YapiMcpClient, unwrap_interface  # noqa: E402

INTERFACE_URL_RE = re.compile(
    r"https?://[^\s]+?/project/(?P<project_id>\d+)/interface/api/(?P<interface_id>\d+)"
    r"|/project/(?P<project_id2>\d+)/interface/api/(?P<interface_id2>\d+)"
)
PROJECT_PAGE_RE = re.compile(r"/project/(?P<project_id>\d+)/interface/api/?(\?.*)?$", re.I)
CAT_PAGE_RE = re.compile(r"/project/\d+/interface/api/cat_(?P<cat_id>\d+)", re.I)


def _ids(link: str) -> tuple[str, str]:
    match = INTERFACE_URL_RE.search(link or "")
    if not match:
        return "", ""
    return (
        match.group("project_id") or match.group("project_id2") or "",
        match.group("interface_id") or match.group("interface_id2") or "",
    )


def _origin(link: str) -> str:
    parsed = urlparse(link)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def _looks_http_error(data: object) -> bool:
    if not isinstance(data, str):
        return False
    return "HTTP 404" in data or "Not Found" in data or "错误" in data[:80]


def fetch_detail(client: YapiMcpClient, *, url: str | None, interface_id: int | None) -> dict:
    arguments: dict = {}
    if url:
        arguments["interfaceURL"] = url
    if interface_id:
        arguments["interfaceID"] = interface_id
    if not arguments:
        raise ValueError("必须提供 --url 或 --interface-id")
    return unwrap_interface(client.call_tool("get_interface_detail", arguments))


def fetch_project(client: YapiMcpClient, *, project_url: str | None, project_id: int | None) -> object:
    arguments: dict = {}
    if project_id:
        arguments["projectId"] = project_id
    if project_url:
        arguments["projectURL"] = project_url
    if not arguments:
        raise ValueError("必须提供 --project-url 或 --project-id")
    return client.call_tool("list_interfaces_by_project", arguments)


def _project_id_from_url(link: str) -> str:
    match = PROJECT_PAGE_RE.search(link or "")
    return match.group("project_id") if match else ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="调用 yapi-common-mcp 拉取 YApi 接口")
    parser.add_argument("--ping", action="store_true", help="只探测 yapi-common-mcp 连通，不拉接口")
    parser.add_argument("--url", help="YApi 接口链接")
    parser.add_argument("--interface-id", type=int, help="YApi 接口 ID")
    parser.add_argument("--cat-url", help="YApi 分类页链接")
    parser.add_argument("--cat-id", type=int, help="YApi 分类 ID")
    parser.add_argument("--project-url", help="YApi 项目页链接，调用 list_interfaces_by_project")
    parser.add_argument("--project-id", type=int, help="YApi 项目 ID，调用 list_interfaces_by_project")
    parser.add_argument("--search", help="按关键词 search_interface")
    parser.add_argument("--output", "-o", help="写入 JSON 文件")
    parser.add_argument("--base-url", help="覆盖 x-yapi-base-url")
    parser.add_argument("--mcp-url", help="覆盖 MCP 地址")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    base_url = args.base_url or (
        _origin(args.url or args.cat_url or args.project_url or "") or None
    )
    client = YapiMcpClient(mcp_url=args.mcp_url, base_url=base_url)
    info = {
        "mcp_url": client.mcp_url,
        "resolve_via": client.resolve_via,
        "base_url": client.base_url,
    }
    try:
        if args.ping:
            initialize = client.initialize()
            payload = {"ok": True, "kind": "ping", **info, "initialize": initialize}
            text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            if args.output:
                Path(args.output).write_text(text, encoding="utf-8")
            sys.stdout.write(text)
            return 0
        if args.url and not args.interface_id:
            if CAT_PAGE_RE.search(args.url) and not args.cat_url:
                args.cat_url = args.url
                args.url = None
            elif PROJECT_PAGE_RE.search(args.url) and not args.project_url and not args.project_id:
                args.project_url = args.url
                args.url = None
        if not (
            args.url
            or args.interface_id
            or args.cat_url
            or args.cat_id
            or args.project_url
            or args.project_id
            or args.search
        ):
            raise ValueError(
                "必须提供 --ping，或 --url / --interface-id / --cat-url / --cat-id "
                "/ --project-url / --project-id / --search"
            )
        if args.project_url or args.project_id:
            data = fetch_project(
                client, project_url=args.project_url, project_id=args.project_id
            )
            data_text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
            looks_error = _looks_http_error(data)
            if looks_error and client.base_url.rstrip("/") == "https://yapi.yc345.tv":
                client.base_url = "https://yapi-test.yc345.tv"
                data = fetch_project(
                    client, project_url=args.project_url, project_id=args.project_id
                )
                info["base_url"] = client.base_url
                info["base_url_fallback"] = True
                data_text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
                looks_error = _looks_http_error(data)
            count = 0
            if isinstance(data, dict):
                count = int(data.get("count") or len(data.get("interfaces") or []) or 0)
            payload = {
                "ok": not looks_error and isinstance(data, dict),
                "kind": "project",
                **info,
                "project_id": str(
                    (data.get("projectId") if isinstance(data, dict) else None)
                    or args.project_id
                    or _project_id_from_url(args.project_url or "")
                    or ""
                ),
                "interface_count": count,
                "mcp_tool": "list_interfaces_by_project",
                "data": data,
            }
            if looks_error:
                payload["error"] = data_text[:300]
        elif args.cat_url or args.cat_id:
            arguments = {}
            if args.cat_url:
                arguments["catURL"] = args.cat_url
            if args.cat_id:
                arguments["catID"] = args.cat_id
            data = client.call_tool("list_interfaces_by_cat", arguments)
            data_text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
            looks_404 = isinstance(data, str) and ("HTTP 404" in data or "Not Found" in data)
            if looks_404 and client.base_url.rstrip("/") == "https://yapi.yc345.tv":
                client.base_url = "https://yapi-test.yc345.tv"
                data = client.call_tool("list_interfaces_by_cat", arguments)
                info["base_url"] = client.base_url
                info["base_url_fallback"] = True
                data_text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
                looks_404 = isinstance(data, str) and ("HTTP 404" in data or "Not Found" in data)
            payload = {"ok": not looks_404, "kind": "category", **info, "data": data}
            if looks_404:
                payload["error"] = data_text[:300]
        elif args.search:
            payload = {
                "ok": True,
                "kind": "search",
                **info,
                "data": client.call_tool(
                    "search_interface",
                    {"rawQuery": args.search, "tokens": [t for t in re.split(r"\s+", args.search) if t]},
                ),
            }
        else:
            try:
                data = fetch_detail(client, url=args.url, interface_id=args.interface_id)
            except Exception:
                # 用户给 yapi.yc345.tv 时 MCP 常需落到 yapi-test 同源 ID
                if client.base_url.rstrip("/") == "https://yapi.yc345.tv":
                    client.base_url = "https://yapi-test.yc345.tv"
                    data = fetch_detail(client, url=args.url, interface_id=args.interface_id)
                    info["base_url"] = client.base_url
                    info["base_url_fallback"] = True
                else:
                    raise
            project_id, interface_id = _ids(args.url or "")
            payload = {
                "ok": True,
                "kind": "interface",
                **info,
                "project_id": str(data.get("project_id") or project_id or ""),
                "interface_id": str(data.get("_id") or data.get("id") or interface_id or args.interface_id or ""),
                "title": data.get("title") or "",
                "method": str(data.get("method") or "").upper(),
                "path": data.get("path") or "",
                "data": data,
            }
            if not payload["method"] or not payload["path"]:
                payload["ok"] = False
                payload["error"] = "MCP 返回缺少 method 或 path"
    except Exception as exc:  # noqa: BLE001 - CLI 出口
        payload = {"ok": False, **info, "error": str(exc)}

    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    return 0 if payload.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
