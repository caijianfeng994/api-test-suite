#!/usr/bin/env python3
"""通过 CodeWiki MCP 拉取服务 HTTP 接口，输出与原 GitNexus catalog 条目相同结构。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from codewiki_mcp import CodewikiMcpClient  # noqa: E402

HTTP_RE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(\/[^\s,`|]+)", re.I)
FIELD_ROW_RE = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*`?([^`|]*)`?\s*\|\s*(.*?)\s*\|$")


def wiki_repo_names(payload: Any) -> list[str]:
    if isinstance(payload, dict):
        repos = payload.get("repos")
        if isinstance(repos, list):
            return [str(item) for item in repos if isinstance(item, str)]
    if isinstance(payload, list):
        names: list[str] = []
        for item in payload:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict) and item.get("name"):
                names.append(str(item["name"]))
        return names
    return []


def match_wiki_repo(service_name: str, repos: list[str]) -> tuple[list[str], list[str]]:
    raw = (service_name or "").strip().strip("/")
    last = raw.split("/")[-1]
    exact = [name for name in repos if name == raw or name == last]
    similar = [name for name in repos if last and last in name and name not in exact][:12]
    return exact, similar


def _parse_http_cell(cell: str) -> list[tuple[str, str]]:
    found = HTTP_RE.findall(cell or "")
    return [(method.upper(), path.rstrip(".,;")) for method, path in found]


def _parse_field_table(lines: list[str], start: int) -> tuple[list[dict[str, Any]], int]:
    fields: list[dict[str, Any]] = []
    index = start
    while index < len(lines):
        line = lines[index].strip()
        # 标题与表格之间常有空行；跳过空白，勿提前结束
        if not line:
            index += 1
            continue
        if not line.startswith("|"):
            break
        if set(line.replace("|", "").replace("-", "").replace(":", "").strip()) == set():
            index += 1
            continue
        match = FIELD_ROW_RE.match(line)
        if match:
            field_name = match.group(1).strip()
            field_type = match.group(2).strip()
            # 只跳过表头；业务字段可以就叫 name
            if field_name.lower() in {"字段", "field"} or field_type.lower() in {"类型", "type"}:
                index += 1
                continue
            fields.append(
                {
                    "name": field_name,
                    "required": False,
                    "type": field_type,
                    "description": match.group(3).replace("`", "").strip(),
                    "example": "",
                }
            )
        index += 1
    return fields, index


def interfaces_from_markdown(
    markdown: str,
    *,
    service_name: str,
    repo: str,
    wiki_id: str,
    page_title: str,
) -> list[dict[str, Any]]:
    lines = (markdown or "").splitlines()
    summaries: list[dict[str, Any]] = []
    in_list = False
    for line in lines:
        if line.strip().startswith("## ") and ("接口清单" in line or "路由清单" in line):
            in_list = True
            continue
        if in_list and line.startswith("## "):
            break
        if in_list and line.startswith("|"):
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if len(cells) < 2 or cells[0] in {"RPC", "HTTP", "---"} or set(cells[0]) <= {"-"}:
                continue
            http_pairs_col0 = _parse_http_cell(cells[0])
            http_pairs_col1 = _parse_http_cell(cells[1]) if len(cells) > 1 else []
            if http_pairs_col0:
                rpc = cells[2].replace("`", "").strip() if len(cells) > 2 else ""
                name = cells[3].replace("`", "").strip() if len(cells) > 3 else rpc
                for method, path in http_pairs_col0:
                    summaries.append(
                        {
                            "rpc": rpc,
                            "method": method,
                            "path": path,
                            "name": name if name and "缺少注释说明" not in name else rpc or path,
                            "response_root_type": "",
                        }
                    )
            elif http_pairs_col1:
                rpc = cells[0].replace("`", "").strip()
                response_root_type = cells[3].replace("`", "").strip() if len(cells) > 3 else ""
                for method, path in http_pairs_col1:
                    summaries.append(
                        {
                            "rpc": rpc,
                            "method": method,
                            "path": path,
                            "name": cells[5].replace("`", "").strip() if len(cells) > 5 else rpc,
                            "response_root_type": response_root_type,
                        }
                    )

    details: dict[str, dict[str, Any]] = {}
    index = 0
    current = ""
    while index < len(lines):
        line = lines[index]
        heading = re.match(r"^###\s+(.+)$", line.strip())
        if heading:
            current = heading.group(1).strip().replace("`", "")
            details.setdefault(current, {"query": [], "body": [], "responses": [], "handler": "", "http": []})
            index += 1
            continue
        if current and (line.strip().startswith("- HTTP：") or line.strip().startswith("- HTTP:")):
            details[current]["http"] = _parse_http_cell(line)
        if current and ("实现入口：" in line or line.strip().startswith("- Handler：") or line.strip().startswith("- Handler:")):
            val = line.split("实现入口：", 1)[-1] if "实现入口：" in line else line.split("Handler：", 1)[-1] if "Handler：" in line else line.split("Handler:", 1)[-1]
            details[current]["handler"] = val.strip().strip("`")
        if current and line.strip() in {"#### 入参字段", "#### 出参字段"}:
            fields, index = _parse_field_table(lines, index + 1)
            key = "query" if "入参" in line else "responses"
            if "入参" in line:
                details[current]["query"] = fields
            else:
                details[current]["responses"] = fields
            continue
        index += 1

    def _lookup_detail(item: dict[str, Any]) -> dict[str, Any]:
        rpc = str(item.get("rpc") or "")
        if rpc and rpc in details:
            return details[rpc]
        method = str(item.get("method") or "")
        path = str(item.get("path") or "")
        http_key = f"{method} {path}".strip()
        if http_key in details:
            return details[http_key]
        for key, detail in details.items():
            for http_method, http_path in detail.get("http") or []:
                if http_method == method and http_path == path:
                    return detail
            # 手写页标题常为 `GET /path`，RPC 名对不上
            if path and (key == path or key.endswith(path) or path in key):
                return detail
        return {}

    interfaces: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in summaries:
        key = (item["method"], item["path"])
        if key in seen:
            continue
        seen.add(key)
        detail = _lookup_detail(item)
        method = item["method"]
        in_fields = detail.get("query") or []
        query_parameters = in_fields if method == "GET" else []
        request_body = None
        if method != "GET" and in_fields:
            request_body = {
                "content_type": "application/json",
                "fields": in_fields,
            }
        out_fields = detail.get("responses") or []
        root_type = str(item.get("response_root_type") or "").strip()
        response_schema = None
        if out_fields or root_type:
            response_schema = {"fields": out_fields, "root_type": root_type}
        interfaces.append(
            {
                "id": "",
                "name": item["name"] or item["rpc"],
                "method": method,
                "path": item["path"],
                "base_url": "",
                "headers": [],
                "path_parameters": [f for f in in_fields if f["name"].startswith(":") or "{" in f["name"]],
                "query_parameters": query_parameters,
                "request_body": request_body,
                "response_root_type": root_type,
                "responses": [
                    {
                        "status": 200,
                        "content_type": "application/json",
                        "schema": response_schema,
                        "example_summary": "",
                    }
                ],
                "auth": {},
                "side_effects": [],
                "source": {
                    "type": "gitnexus",
                    "reference": repo,
                    "service_name": service_name,
                    "repo": repo,
                    "code_evidence": [
                        {
                            "wiki_id": wiki_id,
                            "page_title": page_title,
                            "handler": (detail.get("handler") or item["rpc"]).strip("`"),
                            "file": ((detail.get("handler") or "").split(":")[0]).strip("`"),
                        }
                    ],
                },
            }
        )
    return interfaces


def catalog_from_api_pages(
    pages: Any,
    *,
    service_name: str,
    repo: str,
    path_filter: str | None,
) -> list[dict[str, Any]]:
    if not isinstance(pages, list):
        pages = [pages] if pages else []
    collected: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        markdown = page.get("markdown") or ""
        collected.extend(
            interfaces_from_markdown(
                markdown,
                service_name=service_name,
                repo=repo,
                wiki_id=str(page.get("id") or ""),
                page_title=str(page.get("title") or ""),
            )
        )
    if path_filter:
        needle = path_filter.strip()
        collected = [item for item in collected if item["path"] == needle or needle in item["path"]]
    for index, item in enumerate(collected, start=1):
        item["id"] = f"F{index}"
    return collected


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="从 CodeWiki MCP 拉取接口并输出 GitNexus 形态 catalog")
    parser.add_argument("--service-name", required=True, help="服务名，如 good 或 good-order")
    parser.add_argument("--path", help="可选，过滤到指定 path")
    parser.add_argument("--repo", help="可选，直接指定 CodeWiki repo 名")
    parser.add_argument("--output", "-o", help="写入 JSON")
    parser.add_argument("--mcp-url", help="覆盖 CodeWiki MCP 地址")
    parser.add_argument("--limit", type=int, default=20)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    client = CodewikiMcpClient(mcp_url=args.mcp_url)
    info: dict[str, Any] = {"mcp_url": client.mcp_url, "ok": False}
    try:
        listed = client.call_tool("list_codewiki_repos", {"type": "repo"})
        repos = wiki_repo_names(listed)
        if args.repo:
            if args.repo in repos:
                matched, similar = [args.repo], []
            else:
                matched = []
                similar = [name for name in repos if args.repo in name][:12]
        else:
            matched, similar = match_wiki_repo(args.service_name, repos)
        info["matched_repos"] = matched
        info["similar_repos"] = similar
        if len(matched) != 1:
            info["error"] = "CodeWiki 仓库不唯一或零匹配，停止猜测"
            payload = info
        else:
            repo = matched[0]
            arguments: dict[str, Any] = {"repo": repo, "limit": args.limit}
            if args.path:
                arguments["keyword"] = args.path
            pages = client.call_tool("get_api_reference", arguments)
            interfaces = catalog_from_api_pages(
                pages,
                service_name=args.service_name.split("/")[-1],
                repo=repo,
                path_filter=args.path,
            )
            if not interfaces and args.path:
                pages = client.call_tool("get_api_reference", {"repo": repo, "limit": args.limit})
                interfaces = catalog_from_api_pages(
                    pages,
                    service_name=args.service_name.split("/")[-1],
                    repo=repo,
                    path_filter=args.path,
                )
            payload = {
                **info,
                "ok": bool(interfaces),
                "repo": repo,
                "service_name": args.service_name,
                "interface_count": len(interfaces),
                "interfaces": interfaces,
                "error": None if interfaces else "CodeWiki 未解析到 HTTP 接口",
            }
    except Exception as exc:  # noqa: BLE001
        payload = {**info, "ok": False, "error": str(exc)}

    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    return 0 if payload.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
