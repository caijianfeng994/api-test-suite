#!/usr/bin/env python3
"""按 GitLab proto HTTP 路径过滤 YApi，并保留仅存在于 proto 的接口。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
PATH_RE = re.compile(r"^/")


def normalize_path(path: str) -> str:
    raw = (path or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw if "://" in raw else f"http://local{raw if raw.startswith('/') else '/' + raw}")
    value = parsed.path or ""
    value = re.sub(r"/+", "/", value)
    if len(value) > 1:
        value = value.rstrip("/")
    return value or ""


def service_slug(service_name: str) -> str:
    return (service_name or "").strip().strip("/").split("/")[-1]


def path_aliases(path: str, service_name: str) -> set[str]:
    normalized = normalize_path(path)
    if not normalized:
        return set()
    aliases = {normalized}
    slug = service_slug(service_name)
    parts = [part for part in normalized.strip("/").split("/") if part]
    if slug and parts and parts[0] == slug:
        rest = "/" + "/".join(parts[1:])
        if rest != "/":
            aliases.add(normalize_path(rest))
    if slug and not normalized.startswith(f"/{slug}/") and normalized != f"/{slug}":
        aliases.add(normalize_path(f"/{slug}{normalized}"))
    return {item for item in aliases if item}


def paths_match(yapi_path: str, proto_path: str, service_name: str) -> bool:
    left = path_aliases(yapi_path, service_name)
    right = path_aliases(proto_path, service_name)
    if left & right:
        return True
    slug = service_slug(service_name)
    for a in left:
        for b in right:
            longer, shorter = (a, b) if len(a) >= len(b) else (b, a)
            if not shorter or not longer.endswith(shorter):
                continue
            prefix = longer[: len(longer) - len(shorter)].rstrip("/")
            if prefix in {"", f"/{slug}"}:
                return True
    return False


def endpoint_key(method: str, path: str) -> tuple[str, str]:
    return method.upper(), normalize_path(path)


def _as_endpoint(obj: dict[str, Any]) -> dict[str, Any] | None:
    method = str(obj.get("method") or "").upper()
    path = obj.get("path") or obj.get("url") or ""
    if method not in HTTP_METHODS or not isinstance(path, str):
        return None
    normalized = normalize_path(path)
    if not PATH_RE.match(normalized):
        return None
    name = str(obj.get("name") or obj.get("title") or obj.get("rpc") or normalized)
    ident = str(obj.get("id") or obj.get("_id") or obj.get("interface_id") or "")
    return {
        "id": ident,
        "name": name,
        "method": method,
        "path": normalized,
        "raw_path": path,
        "source_item": {k: obj[k] for k in obj if k != "source_item"},
    }


def iter_endpoints(payload: Any, *, seen: set[tuple[str, str]] | None = None) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen = seen if seen is not None else set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            item = _as_endpoint(node)
            if item:
                key = endpoint_key(item["method"], item["path"])
                if key not in seen:
                    seen.add(key)
                    found.append(item)
            for value in node.values():
                if value is node.get("source_item"):
                    continue
                walk(value)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(payload)
    return found


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def match_yapi_to_proto(
    yapi_items: list[dict[str, Any]],
    proto_items: list[dict[str, Any]],
    *,
    service_name: str,
) -> dict[str, Any]:
    kept: list[dict[str, Any]] = []
    dropped_yapi: list[dict[str, Any]] = []
    proto_used: set[tuple[str, str]] = set()

    for yapi in yapi_items:
        matched: dict[str, Any] | None = None
        for proto in proto_items:
            if yapi["method"] != proto["method"]:
                continue
            if paths_match(yapi["path"], proto["path"], service_name):
                matched = proto
                proto_used.add(endpoint_key(proto["method"], proto["path"]))
                break
        if matched is None:
            dropped_yapi.append(
                {
                    "method": yapi["method"],
                    "path": yapi["path"],
                    "name": yapi["name"],
                    "yapi_id": yapi["id"],
                    "reason": "not_in_proto",
                    "source": "yapi",
                }
            )
            continue
        merged = dict(matched.get("source_item") or matched)
        merged["method"] = yapi["method"]
        merged["path"] = matched["path"] or yapi["path"]
        merged["name"] = yapi["name"] or merged.get("name") or yapi["path"]
        sources = list(merged.get("sources") or [])
        yapi_source = {
            "type": "yapi",
            "reference": yapi["id"],
            "path": yapi["path"],
        }
        if yapi_source not in sources:
            sources.append(yapi_source)
        merged["sources"] = sources
        merged["yapi"] = {"id": yapi["id"], "path": yapi["path"], "name": yapi["name"]}
        merged["proto_match"] = True
        merged["yapi_documented"] = True
        merged["presence"] = "yapi_and_proto"
        merged["source_label"] = "YApi+proto"
        proto_source = merged.get("source") if isinstance(merged.get("source"), dict) else {}
        if proto_source:
            merged["source"] = proto_source
        else:
            merged["source"] = {"type": "protobuf", "service_name": service_name}
        kept.append(merged)

    proto_only: list[dict[str, Any]] = []
    for proto in proto_items:
        key = endpoint_key(proto["method"], proto["path"])
        if key in proto_used:
            continue
        item = dict(proto.get("source_item") or proto)
        item["method"] = proto["method"]
        item["path"] = proto["path"]
        item["name"] = proto["name"] or proto["path"]
        item["proto_match"] = True
        item["yapi_documented"] = False
        item["presence"] = "proto_only"
        item["source_label"] = "proto有"
        item["yapi"] = None
        sources = list(item.get("sources") or [])
        proto_only_source = {"type": "protobuf", "reference": "proto_only", "path": proto["path"]}
        if proto_only_source not in sources:
            sources.append(proto_only_source)
        item["sources"] = sources
        if not isinstance(item.get("source"), dict):
            item["source"] = {"type": "protobuf", "service_name": service_name}
        proto_only.append(item)
        kept.append(item)

    for index, item in enumerate(kept, start=1):
        item["id"] = f"F{index}"

    return {
        "ok": True,
        "service_name": service_name,
        "yapi_count": len(yapi_items),
        "proto_count": len(proto_items),
        "kept_count": len(kept),
        "kept_yapi_and_proto_count": len(kept) - len(proto_only),
        "kept_proto_only_count": len(proto_only),
        "filtered_count": len(dropped_yapi),
        "interfaces": kept,
        "filtered": dropped_yapi,
    }


def self_test() -> None:
    assert normalize_path("/api/v2/good/") == "/api/v2/good"
    assert paths_match("/good/api/v2/good", "/api/v2/good", "good")
    assert paths_match("/api/v2/good", "/good/api/v2/good", "backend/good")
    assert not paths_match("/api/v2/good", "/api/v3/skuGood", "good")
    yapi = [{"method": "GET", "path": "/good/api/sku", "name": "列表", "id": "1"}]
    proto = [{"method": "GET", "path": "/api/sku", "name": "ListSKU", "id": "F1"}]
    result = match_yapi_to_proto(iter_endpoints(yapi), iter_endpoints(proto), service_name="good")
    assert result["kept_count"] == 1
    assert result["filtered_count"] == 0
    assert result["interfaces"][0]["presence"] == "yapi_and_proto"
    miss = match_yapi_to_proto(
        iter_endpoints([{"method": "GET", "path": "/legacy", "name": "旧"}]),
        iter_endpoints(proto),
        service_name="good",
    )
    assert miss["filtered"][0]["reason"] == "not_in_proto"
    assert miss["kept_proto_only_count"] == 1
    assert miss["interfaces"][0]["presence"] == "proto_only"
    assert miss["interfaces"][0]["source_label"] == "proto有"
    assert miss["interfaces"][0]["yapi_documented"] is False
    extra_proto = match_yapi_to_proto(
        iter_endpoints(yapi),
        iter_endpoints(proto + [{"method": "POST", "path": "/api/sku/auto", "name": "AutoCreateSKU"}]),
        service_name="good",
    )
    assert extra_proto["kept_count"] == 2
    assert extra_proto["kept_proto_only_count"] == 1
    labels = {item["path"]: item["source_label"] for item in extra_proto["interfaces"]}
    assert labels["/api/sku"] == "YApi+proto"
    assert labels["/api/sku/auto"] == "proto有"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="YApi 与 proto HTTP 路径对齐：过滤仅 YApi 有的接口，保留交集与仅 proto 有")
    parser.add_argument("--yapi", required=False, help="fetch_yapi_interface.py 输出 JSON")
    parser.add_argument("--proto", required=False, help="fetch_codewiki_interfaces.py 输出 JSON（来自 api/*/*.proto）")
    parser.add_argument("--service-name", default="", help="服务名，用于去掉/补上网关前缀")
    parser.add_argument("--output", "-o", help="写入过滤结果 JSON")
    parser.add_argument("--self-test", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.self_test:
        self_test()
        sys.stdout.write("OK: filter_yapi_by_proto self-test\n")
        return 0
    if not args.yapi or not args.proto:
        raise SystemExit("必须提供 --yapi 与 --proto，或使用 --self-test")
    yapi_payload = load_json(Path(args.yapi))
    proto_payload = load_json(Path(args.proto))
    yapi_items = iter_endpoints(yapi_payload)
    proto_items = iter_endpoints(proto_payload)
    if isinstance(proto_payload, dict) and proto_payload.get("interfaces"):
        proto_items = iter_endpoints(proto_payload["interfaces"])
    result = match_yapi_to_proto(yapi_items, proto_items, service_name=args.service_name)
    result["yapi_file"] = str(args.yapi)
    result["proto_file"] = str(args.proto)
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    return 0 if result["kept_count"] or result["yapi_count"] == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
