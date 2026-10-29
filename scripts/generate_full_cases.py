#!/usr/bin/env python3
"""Expand interface-catalog (+ optional YApi raw) into full API test cases (no default smoke).

成功路径（functional / list / side_effect）只接受真实可追溯的取值：
`endpoint_bindings` → `shared` → 契约 example/default/enum。解析不到必填字段时
**不写假值**，用例标 `executable=false` 并记 `required_binding_missing:<位置>.<字段>`，
因此永远不会用 `test` / `placeholder` / 零 UUID 冒充业务数据。
负向用例（validation / error）允许按契约构造非法值，但必须带
`fixture_policy=negative_case` 与 `negative_construction` 构造依据。auth 用例只移除鉴权条件，
业务参数仍按成功路径绑定真实值。
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone, timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any


def _load_sibling_module(name: str) -> Any:
    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise SystemExit(f"cannot load {name} from {path}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_regression_promotion() -> Any:
    """复用晋升内核的占位/敏感判定，避免生成器与判定端规则分叉。"""
    return _load_sibling_module("regression_promotion")


PROMOTION = _load_regression_promotion()
contains_placeholder = PROMOTION.contains_placeholder
SENSITIVE_KEYS = PROMOTION.SENSITIVE_KEYS
resolve_write_authorization = _load_sibling_module("write_authorization").resolve_write_authorization
should_generate_auth = _load_sibling_module("app_usage").should_generate_auth


TZ = timezone(timedelta(hours=8))
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
LIST_HINTS = ("list", "page", "search", "batch/list")
LIMIT_NAMES = ("limit", "pagesize", "page_size", "pagesize", "pageSize", "size", "pageNo", "page")

# 成功路径类别：必填取值必须真实可追溯（与 regression_promotion 的判定一致）
SUCCESS_PATH_CATEGORIES = {"functional", "list", "side_effect"}
# 负向类别：允许按契约构造非法值，须标 fixture_policy=negative_case
NEGATIVE_CATEGORIES = {"validation", "auth", "error"}

# 分页结构参数名：仅用于识别「哪些字段按分页语义处理」。
# 注意：识别出来不等于有默认值——分页值同样只能来自 endpoint binding / shared.limit|page /
# YApi example|default|enum，无来源即 unresolved（见 resolve_query_value / merge_query）。
LIMIT_KEYS = {"limit", "pagesize", "page_size", "size"}
PAGE_KEYS = {"page", "pageno", "page_no"}

# 负向构造用的确定性非法值：不得落在 regression_promotion.PLACEHOLDER_VALUES 里
NEGATIVE_UNKNOWN_ID = "00000000-0000-0000-0000-ffffffffdead"
NEGATIVE_UNKNOWN_STRING = "regression-negative-unknown"
NEGATIVE_UNKNOWN_NUMBER = -987654321

# 生成期固定 blocker：用例尚未执行，任何用例都不能在生成阶段就算晋升
NOT_EXECUTED_BLOCKER = "execution_not_passed"
NOT_EXECUTABLE_BLOCKER = "case_not_executable"

# expand_interface 期间注入；make_case 读取以补 Header uid
_ACTIVE_FIXTURES: dict = {}


def now_iso() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def load_json(path: Path | None) -> dict | list | None:
    if not path or not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def yapi_index(yapi_raw: dict | None) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    if not isinstance(yapi_raw, dict):
        return out
    items = yapi_raw.get("interfaces")
    if not isinstance(items, list):
        data = yapi_raw.get("data")
        items = data.get("interfaces") if isinstance(data, dict) else None
    for item in items or []:
        if not isinstance(item, dict):
            continue
        method = str(item.get("method") or "").upper()
        path = str(item.get("path") or "")
        query_path = item.get("query_path") if isinstance(item.get("query_path"), dict) else {}
        normalized = str(query_path.get("path") or "")
        if method and path:
            out[(method, path)] = item
        # YApi 有时把服务前缀重复写进 path，真实路径在 query_path.path。
        if method and normalized and (method, normalized) not in out:
            out[(method, normalized)] = item
    return out


def parse_schema(raw: Any) -> dict | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def is_required_flag(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def is_query_iface(iface: dict, yapi: dict | None) -> bool:
    """POST/GET that fetch lists/search should not be treated as mutating writes."""
    if is_list_iface(iface, yapi):
        return True
    name = str(iface.get("name") or "")
    path = str(iface.get("path") or "").lower()
    if any(k in name for k in ("获取", "查询", "列表", "统计", "配置")):
        if str(iface.get("method") or "").upper() in {"GET", "POST"}:
            return "list" in path or "get" in path or "query" in path or "search" in path or "统计" in name or "配置" in name
    return False


def is_mutating_write(iface: dict, yapi: dict | None) -> bool:
    method = str(iface.get("method") or "GET").upper()
    if method not in WRITE_METHODS:
        return False
    if method == "GET":
        return False
    path = str(iface.get("path") or "").lower()
    name = str(iface.get("name") or "")
    if any(h in path for h in ("/list", "list/", "/search")) or path.endswith("list"):
        return False
    if any(k in name for k in ("获取列表", "获取详情", "查询", "统计")):
        return False
    return True


def collect_id_fields(iface: dict, yapi: dict | None) -> tuple[list[str], list[str]]:
    """Return all resource ID names as required; optional list is always empty."""
    required: list[str] = []

    def consider(name: str) -> None:
        n = str(name or "")
        if not n:
            return
        if "id" not in n.lower() and n.lower() not in {"uuid", "key"}:
            return
        required.append(n)

    for p in iface.get("path_parameters") or []:
        if isinstance(p, dict):
            consider(str(p.get("name") or ""))
    for p in iface.get("query_parameters") or []:
        if isinstance(p, dict):
            consider(str(p.get("name") or ""))
    if yapi:
        for q in yapi.get("req_query") or []:
            if isinstance(q, dict):
                consider(str(q.get("name") or ""))
        for q in yapi.get("req_params") or []:
            if isinstance(q, dict):
                consider(str(q.get("name") or ""))
        schema = parse_schema(yapi.get("req_body_other"))
        if isinstance(schema, dict):
            for key in (schema.get("properties") or {}):
                consider(str(key))
    # de-dup preserve order
    def uniq(items: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for x in items:
            if x in seen:
                continue
            seen.add(x)
            out.append(x)
        return out

    return uniq(required), []


def has_declared_required_fields(iface: dict, yapi: dict | None, schema: dict | None) -> bool:
    if any(
        isinstance(p, dict) and p.get("name")
        for p in list(iface.get("query_parameters") or [])
        + list(iface.get("path_parameters") or [])
    ):
        return True
    if yapi:
        if any(
            isinstance(q, dict) and q.get("name")
            for q in list(yapi.get("req_query") or [])
            + list(yapi.get("req_params") or [])
        ):
            return True
        schema = schema or parse_schema(yapi.get("req_body_other"))
    if isinstance(schema, dict) and bool(schema.get("properties")):
        return True
    return False


def ensure_limit(
    query: dict[str, Any],
    iface: dict,
    yapi: dict | None,
    fixtures: dict,
    *,
    value: int | None = None,
) -> dict[str, Any]:
    """兼容旧调用点，但绝不注入无来源分页值。"""
    del iface, yapi, fixtures, value
    return dict(query)


def is_list_iface(iface: dict, yapi: dict | None) -> bool:
    path = str(iface.get("path") or "").lower()
    name = str(iface.get("name") or "").lower()
    if any(h in path for h in LIST_HINTS) or "list" in name or "分页" in name:
        return True
    queries = list(iface.get("query_parameters") or [])
    if yapi:
        for q in yapi.get("req_query") or []:
            queries.append({"name": q.get("name")})
    for q in queries:
        n = str((q or {}).get("name") or "")
        if n.lower() in {x.lower() for x in LIMIT_NAMES} or n in ("pageNo", "pageSize", "page"):
            return True
    return False


def is_resource_identifier(name: str, schema: dict | None = None) -> bool:
    """ID/UUID/资源定位字段不能把 0/false 当作成功数据。"""
    low = str(name or "").strip().lower()
    schema = schema if isinstance(schema, dict) else {}
    fmt = str(schema.get("format") or "").lower()
    return (
        fmt == "uuid"
        or low in {"id", "ids", "uuid", "key"}
        or low.endswith(("id", "ids", "_id", "_ids", "-id", "-ids", "uuid"))
    )


def _candidate_value(raw: Any) -> tuple[Any, bool]:
    """兼容纯值与 ``{value, usage}``；明确 negative_only 时拒绝成功路径。"""
    if isinstance(raw, dict) and "value" in raw:
        usage = str(raw.get("usage") or "").strip().lower()
        return raw.get("value"), usage != "negative_only"
    return raw, True


def _is_code_unused(raw: Any) -> bool:
    """处理函数未读取的声明字段：成功路径省略，不记缺绑定。"""
    return isinstance(raw, dict) and str(raw.get("usage") or "").strip().lower() == "code_unused"


def is_real_value(value: Any) -> bool:
    """非空、且不含未替换占位（`test` / `placeholder` / 零 UUID / `{xxx}`）才算真实值。"""
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return not contains_placeholder(value)


def missing_nested_fields(schema: dict, value: Any, prefix: str) -> list[str]:
    """检查对象值是否包含 schema 声明的全部嵌套字段。"""
    if str(schema.get("type") or "").lower() != "object":
        return []
    props = schema.get("properties") or {}
    if not isinstance(props, dict) or not props:
        return []
    if not isinstance(value, dict):
        return [prefix]
    missing: list[str] = []
    for name, child_schema in props.items():
        child_path = f"{prefix}.{name}"
        if name not in value:
            missing.append(child_path)
            continue
        if isinstance(child_schema, dict):
            missing.extend(
                missing_nested_fields(child_schema, value[name], child_path)
            )
    return missing


def resolve_value(
    name: str,
    schema: dict | None,
    binding: dict | None,
    shared: dict | None,
) -> tuple[Any | None, str | None]:
    """按 `endpoint_bindings → shared → 契约示例` 顺序解析真实取值。

    解析不到时返回 `(None, None)`；**绝不**回退到 `test` / `placeholder` / 零 UUID。
    契约示例包含 `example` / `default` / `enum[0]`，三者都是
    `reference/regression-case-semantics.md` 认可的「契约允许的有效值」。
    """
    schema = schema if isinstance(schema, dict) else {}
    binding = binding if isinstance(binding, dict) else {}
    shared = shared if isinstance(shared, dict) else {}

    if name in binding:
        value, allowed = _candidate_value(binding[name])
        if allowed and is_real_value(value):
            return value, f"endpoint_bindings.{name}"
    if name in shared:
        value, allowed = _candidate_value(shared[name])
        if allowed and is_real_value(value):
            return value, f"shared.{name}"
    if is_real_value(schema.get("example")):
        return schema["example"], "yapi.example"
    default = schema.get("default")
    invalid_resource_default = is_resource_identifier(name, schema) and (
        default is None
        or default is False
        or default == ""
        or default == 0
        or default == []
        or default == {}
    )
    if not invalid_resource_default and is_real_value(default):
        return schema["default"], "yapi.default"
    enum = schema.get("enum")
    if isinstance(enum, (list, tuple)):
        for candidate in enum:
            if is_real_value(candidate):
                return candidate, "yapi.enum"
    return None, None


def resolve_query_value(
    name: str,
    schema: dict,
    binding: dict,
    shared: dict,
) -> tuple[Any | None, str | None]:
    """分页兼容 shared.limit/page 别名，其余仍按 resolve_value 顺序解析。"""
    value, source = resolve_value(name, {}, binding, shared)
    if source:
        return value, source
    low = name.lower()
    alias = "limit" if low in LIMIT_KEYS else "page" if low in PAGE_KEYS else None
    if alias and alias != name:
        value, source = resolve_value(alias, {}, {}, shared)
        if source:
            return value, source
    return resolve_value(name, schema, {}, {})


def _is_non_target_constrained_field(name: str) -> bool:
    """时间、日期、状态、枚举不是资源 ID 负向目标，禁止填非法哨兵。"""
    token = (name or "").lower()
    markers = ("time", "date", "status", "reason", "phone", "enum")
    return any(marker in token for marker in markers)


def negative_sample_value(prop_name: str, prop_schema: dict | None) -> Any:
    """负向构造用的确定性非法/不存在值（不是占位，含义是「刻意不合法」）。"""
    prop_schema = prop_schema if isinstance(prop_schema, dict) else {}
    name = str(prop_name or "").lower()
    typ = str(prop_schema.get("type") or "").lower()
    fmt = str(prop_schema.get("format") or "").lower()
    if typ in {"integer", "number"} or "int" in fmt:
        return NEGATIVE_UNKNOWN_NUMBER
    if typ == "boolean":
        return False
    if typ == "array":
        return []
    if typ == "object":
        return {}
    if "id" in name or "uuid" in fmt or name in {"uuid", "key"}:
        return NEGATIVE_UNKNOWN_ID
    if _is_non_target_constrained_field(name):
        return None
    return NEGATIVE_UNKNOWN_STRING


def _values_with_metadata(
    values: dict | None,
    metadata: dict | None,
    usage: dict | None,
) -> dict:
    """把分离式 fixture 元数据合并成 resolve_value 可识别的包装值。"""
    out = dict(values or {})
    metadata = metadata if isinstance(metadata, dict) else {}
    usage = usage if isinstance(usage, dict) else {}
    for name, raw in list(out.items()):
        if isinstance(raw, dict) and "value" in raw:
            continue
        meta = metadata.get(name)
        meta = meta if isinstance(meta, dict) else {}
        declared_usage = usage.get(name, meta.get("usage"))
        if declared_usage:
            out[name] = {
                "value": raw,
                "usage": declared_usage,
                **({"source": meta["source"]} if meta.get("source") else {}),
            }
    return out


def shared_fixtures(fixtures: dict) -> dict:
    fixture_root = fixtures.get("fixtures") or {}
    values = fixture_root.get("shared") or {}
    metadata = (
        fixture_root.get("shared_metadata")
        or (fixtures.get("fixture_metadata") or {}).get("shared")
        or {}
    )
    usage = fixture_root.get("shared_usage") or {}
    return _values_with_metadata(values, metadata, usage)


def build_body_from_schema(
    schema: dict | None,
    fixtures: dict,
    *,
    mode: str,
    iface: dict | None = None,
    binding: dict | None = None,
    unresolved: list[str] | None = None,
    policy: str = "success",
) -> Any:
    """按 schema 构造 body。

    `policy="success"`：所有声明字段只用 `resolve_value` 的有效取值，解析不到时把
    `body.<field>` 记入 `unresolved` 并**跳过该键**（不写假值）。
    `policy="negative"`：解析不到时用 `negative_sample_value` 的显式非法值。
    """
    shared = shared_fixtures(fixtures)
    binding = binding or {}
    if not schema or schema.get("type") != "object":
        if mode == "invalid_type":
            return {"__invalid__": [1, 2, 3]}
        return {}
    props = schema.get("properties") or {}
    required = [str(key) for key in props]
    body: dict[str, Any] = {}
    if mode == "empty":
        return {}
    if mode == "missing_required":
        if not required:
            return {}
        skip = required[0]
        for key, prop in props.items():
            if key == skip:
                continue
            value, _ = resolve_value(str(key), prop if isinstance(prop, dict) else {}, binding, shared)
            if value is not None:
                body[key] = value
        return body
    if mode == "invalid_type":
        for key, prop in props.items():
            typ = str((prop or {}).get("type") or "string")
            if typ in {"string", ""}:
                body[key] = 12345
            elif typ in {"integer", "number"}:
                body[key] = "not-a-number"
            else:
                body[key] = negative_sample_value(str(key), prop if isinstance(prop, dict) else {})
        if not body:
            body = {"id": {"nested": True}}
        return body
    # valid: all declared request properties are required by suite policy
    keys = required
    for key in keys:
        prop = props.get(key) or {}
        prop = prop if isinstance(prop, dict) else {}
        if _is_code_unused(binding.get(str(key))):
            continue
        value, _ = resolve_value(str(key), prop, binding, shared)
        if value is not None:
            body[key] = value
            if policy == "success" and unresolved is not None:
                unresolved.extend(missing_nested_fields(prop, value, f"body.{key}"))
            continue
        if policy == "negative":
            continue
        if key in required and unresolved is not None:
            unresolved.append(f"body.{key}")
        # 未解析字段直接省略并由 unresolved 阻断，禁止塞假值
    return body


def body_fixture_trace(
    schema: dict | None,
    fixtures: dict,
    body: Any,
    binding: dict | None,
    iface: dict | None = None,
) -> list[dict]:
    """给已写入 body 的字段回填 `{ref, source, required}` 溯源。"""
    if not isinstance(body, dict) or not isinstance(schema, dict):
        return []
    shared = shared_fixtures(fixtures)
    props = schema.get("properties") or {}
    trace: list[dict] = []
    for key in body:
        prop = props.get(key) if isinstance(props, dict) else None
        resolved, source = resolve_value(
            str(key), prop if isinstance(prop, dict) else {}, binding, shared
        )
        if not source or resolved != body[key]:
            continue
        trace.append(
            {
                "ref": f"body.{key}",
                "source": source,
                "required": True,
                "value": body[key],
            }
        )
    return trace


def endpoint_binding(iface: dict, fixtures: dict) -> dict:
    entry = (fixtures.get("endpoint_bindings") or {}).get(iface.get("id")) or {}
    return _values_with_metadata(
        entry.get("values"),
        entry.get("metadata") or entry.get("fixture_metadata"),
        entry.get("usage"),
    )


def param_schema(param: dict | None) -> dict:
    """把 catalog / YApi 参数条目折叠成 `resolve_value` 能用的 schema 片段。"""
    param = param if isinstance(param, dict) else {}
    out: dict[str, Any] = {}
    for key in ("example", "default", "enum", "type", "format", "minimum", "maximum"):
        if param.get(key) not in (None, ""):
            out[key] = param[key]
    return out


def collect_query_params(iface: dict, yapi: dict | None) -> list[dict]:
    params: list[dict] = list(iface.get("query_parameters") or [])
    if yapi:
        for q in yapi.get("req_query") or []:
            if not isinstance(q, dict):
                continue
            params.append(
                {
                    "name": q.get("name"),
                    "required": is_required_flag(q.get("required")),
                    "example": q.get("example"),
                    "default": q.get("default"),
                    "enum": q.get("enum"),
                    "type": q.get("type"),
                    "format": q.get("format"),
                    "minimum": q.get("minimum"),
                    "maximum": q.get("maximum"),
                    "description": q.get("desc") or q.get("description") or "",
                }
            )
    return params


BOUNDARY_SUFFIXES = {
    "min": ("Min", "_min", "Minimum", "_minimum"),
    "max": ("Max", "_max", "Maximum", "_maximum"),
}


def resolve_pagination_boundary(
    iface: dict,
    yapi: dict | None,
    fixtures: dict,
    kind: str,
) -> tuple[str, Any, str] | None:
    """解析分页下/上界，返回 `(参数名, 取值, 来源)`。

    只接受可追溯来源：`endpoint_bindings.<字段>Max`、`shared.<字段>Max`、契约
    `minimum` / `maximum`。契约与 fixture 都没给时返回 None——**不得**自造 0 / 99999。
    """
    binding = endpoint_binding(iface, fixtures)
    shared = shared_fixtures(fixtures)
    schema_key = "minimum" if kind == "min" else "maximum"
    for param in collect_query_params(iface, yapi):
        name = str((param or {}).get("name") or "")
        if not name or name.lower() not in LIMIT_KEYS:
            continue
        for suffix in BOUNDARY_SUFFIXES[kind]:
            key = f"{name}{suffix}"
            for store, label in ((binding, "endpoint_bindings"), (shared, "shared")):
                value, allowed = _candidate_value(store.get(key))
                if allowed and value is not None and is_real_value(value):
                    return name, value, f"{label}.{key}"
        declared = param_schema(param).get(schema_key)
        if declared is not None and is_real_value(declared):
            return name, declared, f"yapi.{schema_key}"
    return None


def merge_query(
    iface: dict,
    yapi: dict | None,
    fixtures: dict,
    *,
    mode: str,
    unresolved: list[str] | None = None,
    policy: str = "success",
) -> dict[str, Any]:
    """组装 query。成功路径任一声明参数解析不到时记 unresolved 并省略。"""
    shared = shared_fixtures(fixtures)
    binding = endpoint_binding(iface, fixtures)
    params = collect_query_params(iface, yapi)
    seen: set[str] = set()
    query: dict[str, Any] = {}
    for p in params:
        name = str((p or {}).get("name") or "")
        if not name or name in seen:
            continue
        seen.add(name)
        low = name.lower()
        required = True
        if mode in {"none", "auth_only"}:
            continue
        if mode == "error_not_found" and "id" in low:
            query[name] = NEGATIVE_UNKNOWN_ID
            continue
        if _is_code_unused(binding.get(name)):
            continue
        value, _ = resolve_query_value(name, param_schema(p), binding, shared)
        if value is not None:
            query[name] = value
            continue
        if policy == "negative":
            continue
        if unresolved is not None:
            unresolved.append(f"query.{name}")
    return query


def query_fixture_trace(
    iface: dict,
    yapi: dict | None,
    fixtures: dict,
    query: dict,
) -> list[dict]:
    shared = shared_fixtures(fixtures)
    binding = endpoint_binding(iface, fixtures)
    schemas = {str((p or {}).get("name") or ""): p for p in collect_query_params(iface, yapi)}
    trace: list[dict] = []
    for name in query:
        resolved, source = resolve_query_value(
            str(name), param_schema(schemas.get(str(name))), binding, shared
        )
        if not source or resolved != query[name]:
            continue
        trace.append(
            {
                "ref": f"query.{name}",
                "source": source,
                "required": True,
                "value": query[name],
            }
        )
    return trace


def resolve_path_params(
    iface: dict,
    yapi: dict | None,
    fixtures: dict,
    *,
    unresolved: list[str] | None = None,
    policy: str = "success",
) -> dict[str, Any]:
    """解析 path 参数；成功路径解析不到必填项时记 `path.<name>`。"""
    shared = shared_fixtures(fixtures)
    binding = endpoint_binding(iface, fixtures)
    params: list[dict] = [p for p in (iface.get("path_parameters") or []) if isinstance(p, dict)]
    if yapi:
        params.extend(p for p in (yapi.get("req_params") or []) if isinstance(p, dict))
    out: dict[str, Any] = {}
    for p in params:
        name = str(p.get("name") or "")
        if not name or name in out:
            continue
        required = True
        value, _ = resolve_value(name, param_schema(p), binding, shared)
        if value is not None:
            out[name] = value
            continue
        if policy == "negative":
            continue
        if required and unresolved is not None:
            unresolved.append(f"path.{name}")
    return out


def path_fixture_trace(
    iface: dict,
    yapi: dict | None,
    fixtures: dict,
    path_params: dict,
) -> list[dict]:
    shared = shared_fixtures(fixtures)
    binding = endpoint_binding(iface, fixtures)
    params: list[dict] = [
        p for p in (iface.get("path_parameters") or []) if isinstance(p, dict)
    ]
    if yapi:
        params.extend(p for p in (yapi.get("req_params") or []) if isinstance(p, dict))
    schemas = {str(p.get("name") or ""): p for p in params}
    trace: list[dict] = []
    for name, value in path_params.items():
        schema = param_schema(schemas.get(str(name)))
        resolved, source = resolve_value(str(name), schema, binding, shared)
        if not source or resolved != value:
            continue
        trace.append(
            {
                "ref": f"path.{name}",
                "source": source,
                "required": True,
                "value": value,
            }
        )
    return trace


def clause_refs(eid: str) -> list[str]:
    return [f"{eid}-CL-001", f"{eid}-CL-002"]


def _catalog_response_meta(iface: dict) -> tuple[list[dict], str]:
    fields: list[dict] = []
    root_type = str(iface.get("response_root_type") or "").strip()
    for resp in iface.get("responses") or []:
        if not isinstance(resp, dict):
            continue
        schema = resp.get("schema")
        if not isinstance(schema, dict):
            continue
        if not root_type:
            root_type = str(schema.get("root_type") or "").strip()
        raw_fields = schema.get("fields")
        if isinstance(raw_fields, list):
            fields = [f for f in raw_fields if isinstance(f, dict) and f.get("name")]
            if fields:
                break
    return fields, root_type


def _proto_json_type(type_text: str) -> str:
    text = str(type_text or "").strip().lower()
    if text.startswith("[]") or text.startswith("repeated ") or text.startswith("repeated<"):
        return "array"
    if text.startswith("map") or "map<" in text:
        return "object"
    if text in {"string", "bytes"}:
        return "string"
    if text in {"bool", "boolean"}:
        return "boolean"
    if text in {"int", "int32", "int64", "uint32", "uint64", "sint32", "sint64", "fixed32", "fixed64"}:
        return "integer"
    if text in {"float", "double", "number"}:
        return "number"
    if text in {"object", "array", "string", "number", "integer", "boolean"}:
        return text
    return "object"


def _is_array_root_type(root_type: str) -> bool:
    text = str(root_type or "").strip()
    return text.startswith("[]") or text.lower().startswith("repeated ")


def _is_dynamic_schema_marker(name: str) -> bool:
    """CodeWiki 用 `{*}` 表示动态 map，不是响应里的真实字段名。"""
    token = (name or "").strip()
    return token in {"{*}", "*", "{*}[]"} or token.startswith("{*")


def schema_from_code_fields(fields: list[dict], root_type: str = "") -> dict | None:
    """把 CodeWiki 出参表转成可断言的 JSON schema。"""
    if not fields and not root_type:
        return None
    if _is_array_root_type(root_type):
        item_props: dict[str, Any] = {}
        for field in fields:
            name = str(field.get("name") or "")
            # 根数组元素只收顶层字段：name / publishers；跳过 publishers[].id
            if not name or _is_dynamic_schema_marker(name) or "." in name or "[" in name:
                continue
            item_props[name] = {"type": _proto_json_type(str(field.get("type") or ""))}
        out: dict[str, Any] = {
            "type": "array",
            "required_fields": [],
            "source": "code_wiki",
            "items": {"type": "object", "properties": item_props} if item_props else {"type": "object"},
        }
        if root_type:
            out["root_type"] = root_type
        return out

    properties: dict[str, Any] = {}
    required_fields: list[str] = []
    nested: dict[str, dict[str, Any]] = {}
    for field in fields:
        name = str(field.get("name") or "")
        if not name or _is_dynamic_schema_marker(name):
            continue
        json_type = _proto_json_type(str(field.get("type") or ""))
        if "[]." in name:
            parent, child = name.split("[].", 1)
            parent = parent.strip()
            child = child.split("[", 1)[0].split(".", 1)[0].strip()
            if not parent or not child:
                continue
            bucket = nested.setdefault(parent, {})
            bucket[child] = {"type": json_type}
            properties.setdefault(parent, {"type": "array", "items": {"type": "object", "properties": {}}})
            continue
        if "." in name or "[" in name:
            continue
        properties[name] = {"type": json_type}
        if field.get("required") is True:
            required_fields.append(name)

    for parent, child_props in nested.items():
        node = properties.setdefault(parent, {"type": "array", "items": {"type": "object", "properties": {}}})
        if node.get("type") != "array":
            node["type"] = "array"
        items = node.setdefault("items", {"type": "object", "properties": {}})
        if not isinstance(items, dict):
            items = {"type": "object", "properties": {}}
            node["items"] = items
        props = items.setdefault("properties", {})
        if isinstance(props, dict):
            props.update(child_props)

    if not properties and not root_type:
        return None
    if not required_fields and properties:
        # 代码出参表通常不标 required；取顶层已声明键前 5 个作存在性校验
        required_fields = [str(k) for k in list(properties.keys())[:5]]
    out = {
        "type": "object",
        "required_fields": required_fields,
        "source": "code_wiki",
        "properties": properties,
    }
    if root_type:
        out["root_type"] = root_type
    return out


def _yapi_only_response_schema(yapi: dict | None) -> dict:
    raw_body = None
    if yapi:
        raw_body = yapi.get("res_body") or yapi.get("res_body_other")
    schema = parse_schema(raw_body) if raw_body is not None else None

    if not isinstance(schema, dict):
        return {
            "type": "object",
            "required_fields": [],
            "source": "undeclared",
            "note": "无 YApi/代码响应体，禁止猜测字段；仅校验 HTTP/业务包（若响应自带 code/ok）",
        }

    resp_type = schema.get("type")
    if resp_type not in {"object", "array", "string", "number", "integer", "boolean"}:
        resp_type = "object" if (schema.get("properties") or schema.get("required")) else (
            "array" if schema.get("items") is not None else "object"
        )

    required_fields: list[str] = []
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    declared_required = [str(x) for x in (schema.get("required") or []) if x]
    if declared_required:
        required_fields = declared_required
    elif props:
        for key in ("code", "message", "msg", "data", "list", "total", "ok", "reason"):
            if key in props:
                required_fields.append(key)
        if not required_fields:
            required_fields = [str(k) for k in list(props.keys())[:5]]

    out: dict[str, Any] = {
        "type": resp_type,
        "required_fields": required_fields,
        "source": "yapi_res_body",
    }
    if resp_type == "array" and isinstance(schema.get("items"), dict):
        out["items"] = schema["items"]
    if props:
        out["properties"] = props
    if not required_fields and not props:
        out["note"] = "契约未声明响应字段（如 EmptyResp/暂无数据），禁止补猜 code；仅校验响应类型"
    return out


def _normalize_array_property(node: dict) -> dict:
    """YApi 常把 array 写成同时带 properties；断言时以 type=array 为准。"""
    if not isinstance(node, dict):
        return node
    if node.get("type") == "array" or node.get("items") is not None:
        items = node.get("items")
        if not isinstance(items, dict):
            props = node.get("properties") if isinstance(node.get("properties"), dict) else None
            items = {"type": "object", "properties": props} if props else {"type": "object"}
        cleaned = {"type": "array", "items": items}
        if isinstance(node.get("required"), list):
            # required 属于 items 时保留在 items
            if isinstance(cleaned["items"], dict) and "required" not in cleaned["items"]:
                cleaned["items"] = {**cleaned["items"], "required": node["required"]}
        return cleaned
    return node


def _calibrate_yapi_with_code(yapi_schema: dict, code_schema: dict) -> dict:
    """以代码为准合并 YApi；记录冲突。"""
    overrides: list[dict[str, str]] = []
    merged = deepcopy(code_schema)
    merged["source"] = "code_calibrated"

    if yapi_schema.get("type") and yapi_schema.get("type") != code_schema.get("type"):
        overrides.append(
            {
                "path": "$",
                "yapi": str(yapi_schema.get("type")),
                "code": str(code_schema.get("type")),
                "reason": "root_type",
            }
        )

    code_props = code_schema.get("properties") if isinstance(code_schema.get("properties"), dict) else {}
    yapi_props = yapi_schema.get("properties") if isinstance(yapi_schema.get("properties"), dict) else {}
    out_props = deepcopy(code_props)
    for key, yapi_node in yapi_props.items():
        if key in out_props:
            code_node = out_props[key]
            if isinstance(code_node, dict) and isinstance(yapi_node, dict):
                if code_node.get("type") == "array":
                    out_props[key] = _normalize_array_property({**yapi_node, **code_node, "type": "array"})
                    if yapi_node.get("type") not in {None, "array"}:
                        overrides.append(
                            {
                                "path": key,
                                "yapi": str(yapi_node.get("type")),
                                "code": "array",
                                "reason": "field_type",
                            }
                        )
                elif key not in code_props:
                    out_props[key] = yapi_node
        else:
            # YApi 多出来的键：若与代码字段仅大小写不同，丢掉 YApi 键
            lower_map = {str(k).lower(): k for k in code_props}
            code_key = lower_map.get(str(key).lower())
            if code_key and code_key != key:
                overrides.append(
                    {
                        "path": key,
                        "yapi": key,
                        "code": code_key,
                        "reason": "field_name_case",
                    }
                )
            elif key not in out_props:
                out_props[key] = _normalize_array_property(yapi_node) if isinstance(yapi_node, dict) else yapi_node

    # items.required 字段名按代码 nested 字段校准
    if merged.get("type") == "object" and out_props:
        for key, node in list(out_props.items()):
            if not isinstance(node, dict) or node.get("type") != "array":
                continue
            items = node.get("items")
            if not isinstance(items, dict):
                continue
            code_items = (code_props.get(key) or {}).get("items") if isinstance(code_props.get(key), dict) else {}
            code_item_props = (
                code_items.get("properties") if isinstance(code_items, dict) else {}
            )
            if not isinstance(code_item_props, dict) or not code_item_props:
                continue
            required = [str(x) for x in (items.get("required") or []) if x]
            if not required:
                yapi_items = (yapi_props.get(key) or {}).get("items") if isinstance(yapi_props.get(key), dict) else {}
                required = [str(x) for x in ((yapi_items or {}).get("required") or []) if x]
            lower_map = {str(k).lower(): k for k in code_item_props}
            calibrated_required: list[str] = []
            for field in required:
                code_field = lower_map.get(field.lower())
                if code_field and code_field != field:
                    overrides.append(
                        {
                            "path": f"{key}[].{field}",
                            "yapi": field,
                            "code": code_field,
                            "reason": "field_name_case",
                        }
                    )
                    calibrated_required.append(code_field)
                elif code_field:
                    calibrated_required.append(code_field)
                # 代码没有的 required 不强制
            item_props = items.get("properties") if isinstance(items.get("properties"), dict) else {}
            merged_item_props = deepcopy(code_item_props)
            for field_name, field_schema in item_props.items():
                code_field = lower_map.get(str(field_name).lower())
                if code_field and code_field != field_name:
                    overrides.append(
                        {
                            "path": f"{key}[].{field_name}",
                            "yapi": field_name,
                            "code": code_field,
                            "reason": "field_name_case",
                        }
                    )
                elif code_field is None:
                    merged_item_props[field_name] = field_schema
            out_props[key] = {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": merged_item_props,
                    "required": calibrated_required,
                },
            }

    if out_props:
        merged["properties"] = out_props
        if not merged.get("required_fields"):
            merged["required_fields"] = [str(k) for k in list(out_props.keys())[:5]]
    if overrides:
        merged["code_overrides"] = overrides
    else:
        # 无冲突时仍可标 code_wiki（调用方也可保留）
        merged["source"] = "code_calibrated" if yapi_schema.get("source") == "yapi_res_body" else merged["source"]
    return merged


def infer_response_schema(yapi: dict | None, iface: dict) -> dict:
    """成功响应 schema：代码出参优先，YApi 仅补充；禁止无字段时猜 code/data。

    规则见 reference/code-response-schema.md。
    """
    code_fields, root_type = _catalog_response_meta(iface if isinstance(iface, dict) else {})
    code_schema = schema_from_code_fields(code_fields, root_type)
    yapi_schema = _yapi_only_response_schema(yapi)

    if code_schema is not None:
        if yapi_schema.get("source") == "yapi_res_body":
            return _calibrate_yapi_with_code(yapi_schema, code_schema)
        return code_schema

    # 仅有根类型 []Xxx、无字段表
    if _is_array_root_type(root_type):
        return {
            "type": "array",
            "required_fields": [],
            "source": "code_wiki",
            "root_type": root_type,
            "items": {"type": "object"},
            "note": "代码出参为根数组；禁止默认 object",
        }

    return yapi_schema


def is_state_change_iface(iface: dict) -> bool:
    path = str(iface.get("path") or "").lower()
    name = str(iface.get("name") or "")
    hints = (
        "shelve", "unshelve", "enable", "disable", "status", "state", "publish", "offline",
        "上架", "下架", "启用", "停用", "禁用", "状态", "审核",
    )
    return any(h in path for h in hints) or any(h in name for h in hints)


def guess_detail_path(iface: dict) -> str | None:
    path = str(iface.get("path") or "")
    method = str(iface.get("method") or "").upper()
    if not path:
        return None
    # create/update style → drop trailing action, use GET detail/list sibling
    if method in WRITE_METHODS:
        for suffix in ("/create", "/update", "/edit", "/add", "/save", "/shelve", "/unshelve", "/enable", "/disable"):
            if path.lower().endswith(suffix):
                base = path[: -len(suffix)]
                return base or path
        if path.rstrip("/").endswith("list"):
            return path
        # POST /api/sku → GET /api/sku or /api/sku/detail
        return path
    return path


def inject_test_marker(payload: Any, marker: str, schema: dict | None) -> Any:
    """把唯一标识写进**契约已声明**的可读字段；没有这类字段时不注入。

    禁止 `setdefault("note", marker)`：那会往请求里塞契约外的键，既可能被服务拒绝，
    也会让「请求里出现非契约取值」的门禁失去意义。此时标识仍保留在 case.test_marker。
    """
    if not isinstance(payload, dict):
        return payload
    properties = (
        schema.get("properties")
        if isinstance(schema, dict) and isinstance(schema.get("properties"), dict)
        else {}
    )
    out = dict(payload)
    for key in ("name", "title", "note", "remark", "desc", "description", "goodName", "skuName"):
        match = next((k for k in out if k.lower() == key.lower()), None)
        if match is None or match not in properties:
            continue
        existing = out.get(match)
        if existing is None or isinstance(existing, str):
            out[match] = f"{marker}-{(existing or 'case')}"[:120]
            return out
    return out


def success_when_from_schema(schema: dict | None) -> dict[str, Any]:
    """仅当响应契约用 const/enum 写明成功值时才生成 success_when；禁止默认 ok=true / code∈{0,200}。"""
    if not isinstance(schema, dict):
        return {}
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    out: dict[str, Any] = {}
    ok_prop = props.get("ok")
    if isinstance(ok_prop, dict):
        if ok_prop.get("const") is True:
            out["ok_is"] = True
        elif ok_prop.get("const") is False:
            out["ok_is"] = False
        else:
            enum_vals = ok_prop.get("enum")
            if isinstance(enum_vals, list) and len(enum_vals) == 1 and isinstance(enum_vals[0], bool):
                out["ok_is"] = enum_vals[0]
    for key in ("code", "errcode", "errCode"):
        code_prop = props.get(key)
        if not isinstance(code_prop, dict):
            continue
        if isinstance(code_prop.get("const"), (int, float)) and not isinstance(
            code_prop.get("const"), bool
        ):
            out["code_in"] = [int(code_prop["const"])]
            break
        enum_vals = code_prop.get("enum")
        if isinstance(enum_vals, list):
            codes = [
                int(x)
                for x in enum_vals
                if isinstance(x, (int, float)) and not isinstance(x, bool)
            ]
            if codes:
                out["code_in"] = codes
                break
    return out


def dual_track_success_expected(*, schema: dict, calibration_note: str = "") -> dict:
    success_when = success_when_from_schema(schema)
    value_note = (
        f"契约声明成功值 success_when={success_when}"
        if success_when
        else "契约未声明 code/ok 成功取值：只校验 schema 类型与已声明字段，禁止默认 ok=true 或 code∈{0,200}"
    )
    return {
        "http": {
            "status": [200, 400],
            "not_status": [500, 502, 503, 504],
            "accept_structured_4xx": True,
        },
        "business": {
            "envelope": "auto",
            "outcome": "success_or_structured_4xx",
            "success_when": success_when,
            "code_path": ["code", "errcode", "errCode"],
            "msg_path": ["message", "msg", "reason", "errmsg"],
        },
        "schema": schema,
        "outcome": "success_or_structured_4xx",
        "calibration": {
            "source": schema.get("source") or "undeclared",
            "confidence": "high" if success_when else "medium",
            "note": calibration_note
            or (
                "HTTP 200 按 schema 校验，或 HTTP 400 且 JSON 含 code/message/reason（结构化业务拒绝）均通过；"
                f"仅 5xx/UNKNOWN 失败。{value_note}"
            ),
        },
    }


def undeclared_error_http_expected(*, note: str) -> dict:
    """契约未声明负向 HTTP 码时不写状态断言，等代码返回点闭合后再写精确状态。"""
    return {
        "assertion_policy": "code_return_required",
        "note": note,
        "calibration": {
            "source": "pending_code_return",
            "confidence": "none",
            "note": (
                "契约未声明该负向分支的 HTTP 状态。禁止写入 status_class、not_status 或“禁止 5xx”。"
                "执行后必须按代码返回点写入 expected.status、response_format 与 code_evidence。"
            ),
        },
    }


def success_path_expected(yapi: dict | None, iface: dict, *, note: str = "") -> dict:
    """list/side_effect/functional 共用：schema 只来自契约，不写无 schema 的 status_class 壳子。"""
    return dual_track_success_expected(
        schema=infer_response_schema(yapi, iface),
        calibration_note=note,
    )


def build_functional_extras(
    *,
    iface: dict,
    yapi: dict | None,
    fixtures: dict,
    case_id: str,
    mutating: bool,
    listish: bool,
    body: Any,
    query: dict,
    request_schema: dict | None = None,
    resolved_trace: list[dict] | None = None,
) -> dict:
    run_token = str((fixtures.get("run_id") or fixtures.get("fixtures", {}).get("shared", {}).get("run_id") or "local"))
    # 前缀刻意避开 `test` 字样：请求体里出现 `test` 会与占位门禁语义混淆
    marker = f"api-regression-{run_token}-{case_id}".replace(" ", "")[:80]
    fixture_trace = list(resolved_trace or [])

    resp_schema = infer_response_schema(yapi, iface)
    expected = dual_track_success_expected(schema=resp_schema)

    # unique marker on write bodies
    marked_body = (
        inject_test_marker(body, marker, request_schema)
        if isinstance(body, dict)
        else body
    )
    if isinstance(body, dict) and isinstance(marked_body, dict):
        for key, value in marked_body.items():
            if body.get(key) == value:
                continue
            marker_item = {
                "ref": f"body.{key}",
                "source": "generated:test_marker",
                "required": True,
                "value": value,
            }
            fixture_trace = [
                item for item in fixture_trace if item.get("ref") != marker_item["ref"]
            ]
            fixture_trace.append(marker_item)
    fixture_refs = [
        str(item.get("ref")) for item in fixture_trace if item.get("ref")
    ]
    no_input_read = not mutating and not query and body in (None, {})
    marked_query = dict(query)

    detail_path = guess_detail_path(iface)
    if mutating:
        verify_read = {
            "mode": "query_after_write",
            "method": "GET",
            "path": detail_path or iface.get("path"),
            "id_from": ["response.data.id", "response.id", "response.data", "request.body.id", "fixture.shared"],
            "assert_fields": ["id", "name", "status"] if not listish else ["total", "list", "data"],
            "required": True,
        }
        cleanup = {
            "enabled": True,
            "method": "DELETE",
            "path": detail_path or iface.get("path"),
            "id_from": ["response.data.id", "response.id", "request.body.id"],
            "reason": None,
        }
    else:
        verify_read = {
            "mode": "self",
            "method": str(iface.get("method") or "GET").upper(),
            "path": iface.get("path"),
            "id_from": [],
            "assert_fields": list(resp_schema.get("required_fields") or []),
            "required": True,
        }
        cleanup = {"enabled": False, "method": None, "path": None, "id_from": [], "reason": "read_only"}

    if is_state_change_iface(iface) and mutating:
        state_check = {
            "required": True,
            "field": "status",
            "before": {"from_observe": True},
            "after": {"must_change": True},
            "observe": {
                "method": "GET",
                "path": detail_path or iface.get("path"),
                "id_from": ["request.body.id", "fixture.shared", "response.id"],
            },
        }
        state_policy = "required"
    else:
        state_check = None
        state_policy = "not_applicable"

    return {
        "test_marker": marker,
        "independent": True,
        "depends_on": [],
        "fixture_refs": fixture_refs,
        "fixture_trace": fixture_trace,
        "fixture_policy": "none_ok" if no_input_read else "required",
        "expected": expected,
        "verify_read": verify_read,
        "state_check": state_check,
        "state_policy": state_policy,
        "cleanup": cleanup,
        "request_body": marked_body,
        "request_query": marked_query,
    }


def resolve_context_uid(fixtures: dict | None) -> str | None:
    """网关/服务常见从 Header `uid` 取登录用户（见 go-revenue redeemCode 等）。

    取值只接受 fixtures 中可追溯的 userId/uid，禁止编造。
    """
    shared = shared_fixtures(fixtures or {})
    for key in ("uid", "userId", "user_id"):
        value, allowed = _candidate_value(shared.get(key))
        if allowed and is_real_value(value):
            return str(value)
    return None


def apply_context_uid_header(
    headers: dict[str, Any],
    *,
    category: str,
    fixtures: dict | None,
    drop_identity: bool = False,
) -> dict[str, Any]:
    """业务身份 Header `uid`：默认补 shared.uid；刻意去身份时不补。

    go-revenue 等从 Header 读用户，与 Authorization 鉴权头无关。
    validation/error 负向若只省略 query/body，仍应带真实 uid，否则服务先报「用户ID不能为空」盖住契约缺参行为。
    """
    out = dict(headers or {})
    if drop_identity:
        out.pop("uid", None)
        return out
    uid = resolve_context_uid(fixtures if fixtures is not None else _ACTIVE_FIXTURES)
    if uid:
        out["uid"] = uid
    return out


def make_case(
    *,
    eid: str,
    seq: int,
    iface: dict,
    name: str,
    category: str,
    query: dict,
    body: Any,
    expected: dict,
    executable: bool,
    skip_reason: str | None,
    fixtures_refs: list[str],
    extras: dict | None = None,
    unresolved: list[str] | None = None,
    path_params: dict | None = None,
    negative_construction: dict | None = None,
    fixtures: dict | None = None,
    drop_identity: bool = False,
) -> dict:
    yapi = iface.get("yapi") or {}
    contract_source = f"yapi:{yapi.get('id')}" if yapi.get("id") else "proto"
    headers = {"Accept": "application/json"}
    method = str(iface.get("method") or "GET").upper()
    if body is not None and method != "GET":
        headers["Content-Type"] = "application/json"
    headers = apply_context_uid_header(
        headers,
        category=category,
        fixtures=fixtures,
        drop_identity=drop_identity,
    )

    missing = list(dict.fromkeys(unresolved or []))
    blockers = [NOT_EXECUTED_BLOCKER]
    blockers.extend(f"required_binding_missing:{item}" for item in missing)
    if missing and category in SUCCESS_PATH_CATEGORIES | {"auth"}:
        # 成功路径及 auth 的业务参数缺真实绑定：既不可执行，也不可晋升
        executable = False
        skip_reason = skip_reason or "缺少真实绑定数据：" + "、".join(missing)
    if not executable:
        blockers.append(NOT_EXECUTABLE_BLOCKER)

    request: dict[str, Any] = {
        "query": query,
        "body": body if method != "GET" else None,
        "headers": headers,
    }
    if path_params:
        request["path_params"] = path_params

    case = {
        "id": f"{eid}-C{seq:02d}",
        "endpoint_id": eid,
        "name": name,
        "category": category,
        "method": method,
        "path": iface.get("path"),
        "base_url": iface.get("base_url"),
        "source_label": iface.get("source_label") or "YApi+proto",
        "presence": iface.get("presence"),
        "request": request,
        "expected": expected,
        "fixture_refs": fixtures_refs,
        "clause_refs": clause_refs(eid),
        "contract_source": contract_source,
        "executable": executable,
        "skip_reason": skip_reason,
        "independent": True,
        "depends_on": [],
        # 生成阶段一律未晋升；晋升由 regression_promotion 在执行 + 证据齐全后判定
        "regression_ready": False,
        "regression_blockers": blockers,
    }
    if extras:
        # functional closed-loop fields; extras.expected overrides plain expected
        for key in (
            "test_marker",
            "independent",
            "depends_on",
            "fixture_refs",
            "fixture_trace",
            "fixture_policy",
            "expected",
            "verify_read",
            "state_check",
            "state_policy",
            "cleanup",
        ):
            if key in extras and extras[key] is not None:
                case[key] = extras[key]
        if extras.get("request_body") is not None and method != "GET":
            case["request"]["body"] = extras["request_body"]
        if extras.get("request_query") is not None:
            case["request"]["query"] = extras["request_query"]
    if category in NEGATIVE_CATEGORIES:
        # 负向用例可以带非法值，但必须自报家门，否则晋升内核无法区分「刻意非法」与「占位」
        case["fixture_policy"] = "negative_case"
        case["negative_construction"] = negative_construction or {
            "strategy": "contract_negative",
            "basis": "按契约声明构造非法/不存在取值，不使用占位数据",
        }
    return case


def normalize_case_fixture_trace(
    case: dict,
    iface: dict,
    yapi: dict | None,
    fixtures: dict,
    schema: dict | None,
) -> None:
    """只保留请求实际使用字段的 trace，并给所有有来源字段补字段级 trace。"""
    request = case.get("request") or {}
    binding = endpoint_binding(iface, fixtures)
    computed = query_fixture_trace(
        iface, yapi, fixtures, request.get("query") or {}
    )
    computed.extend(
        body_fixture_trace(
            schema, fixtures, request.get("body"), binding, iface
        )
    )
    computed.extend(
        path_fixture_trace(
            iface, yapi, fixtures, request.get("path_params") or {}
        )
    )

    by_ref = {str(item["ref"]): item for item in computed if item.get("ref")}

    # test_marker、分页边界等取值不经过 resolve_value，重算不出来；
    # 只在 trace 记录的值与请求里实际发出的值一致时保留，避免留下过期 trace。
    actual: dict[str, Any] = {}
    for prefix, section in (
        ("query", request.get("query")),
        ("body", request.get("body")),
        ("path", request.get("path_params")),
    ):
        if isinstance(section, dict):
            for field, value in section.items():
                actual[f"{prefix}.{field}"] = value
    for item in case.get("fixture_trace") or []:
        ref = str(item.get("ref") or "")
        if not ref or ref in by_ref:
            continue
        if ref in actual and actual[ref] == item.get("value"):
            by_ref[ref] = item

    case["fixture_trace"] = list(by_ref.values())
    case["fixture_refs"] = list(by_ref)
    if case.get("category") in NEGATIVE_CATEGORIES:
        case["fixture_policy"] = "negative_case"
    elif not by_ref and str(case.get("method") or "").upper() == "GET" and not (
        request.get("query") or request.get("path_params") or request.get("body")
    ):
        case["fixture_policy"] = "none_ok"
    else:
        case["fixture_policy"] = "required"


def expand_interface(
    iface: dict,
    yapi: dict | None,
    fixtures: dict,
    *,
    write_authorized: bool = False,
) -> list[dict]:
    global _ACTIVE_FIXTURES
    _ACTIVE_FIXTURES = fixtures if isinstance(fixtures, dict) else {}
    eid = str(iface.get("id") or "F?")
    method = str(iface.get("method") or "GET").upper()
    display = str(iface.get("name") or iface.get("path") or eid)
    schema = parse_schema((yapi or {}).get("req_body_other")) if yapi else None
    if schema is None and isinstance(iface.get("request_body"), dict):
        schema = iface.get("request_body")
    listish = is_list_iface(iface, yapi)
    mutating = is_mutating_write(iface, yapi)
    required_ids, optional_ids = collect_id_fields(iface, yapi)
    has_required = has_declared_required_fields(iface, yapi, schema)
    cases: list[dict] = []
    seq = 1

    # 1) functional — closed-loop (dual-track + fixture + verify_read + cleanup + independent)
    binding = endpoint_binding(iface, fixtures)
    unresolved: list[str] = []
    q = merge_query(
        iface,
        yapi,
        fixtures,
        mode="list_normal" if listish else "valid",
        unresolved=unresolved,
    )
    path_params = resolve_path_params(iface, yapi, fixtures, unresolved=unresolved)
    body = None
    if mutating:
        body = build_body_from_schema(
            schema, fixtures, mode="valid", iface=iface, binding=binding, unresolved=unresolved
        )
        if body is None:
            body = {}
    elif method in WRITE_METHODS and not listish:
        body = build_body_from_schema(
            schema, fixtures, mode="valid", iface=iface, binding=binding, unresolved=unresolved
        )
    if listish and method in WRITE_METHODS:
        body = (
            build_body_from_schema(
                schema, fixtures, mode="valid", iface=iface, binding=binding, unresolved=unresolved
            )
            if schema
            else None
        )
    resolved_trace = query_fixture_trace(iface, yapi, fixtures, q)
    resolved_trace.extend(body_fixture_trace(schema, fixtures, body, binding, iface))
    resolved_trace.extend(path_fixture_trace(iface, yapi, fixtures, path_params))
    case_id = f"{eid}-C{seq:02d}"
    extras = build_functional_extras(
        iface=iface,
        yapi=yapi,
        fixtures=fixtures,
        case_id=case_id,
        mutating=mutating,
        listish=listish,
        body=body if method != "GET" else None,
        query=q,
        request_schema=schema,
        resolved_trace=resolved_trace,
    )
    cases.append(
        make_case(
            eid=eid,
            seq=seq,
            iface=iface,
            name=f"正常功能-{display}",
            category="functional",
            query=extras.get("request_query") or q,
            body=extras.get("request_body") if method != "GET" else None,
            expected=extras["expected"],
            executable=True,
            skip_reason=None,
            fixtures_refs=extras.get("fixture_refs") or [],
            extras=extras,
            unresolved=unresolved,
            path_params=path_params,
        )
    )
    seq += 1

    # 2) validation — every declared request field is default-required
    if mutating and isinstance(schema, dict) and schema.get("properties"):
        cases.append(
            make_case(
                eid=eid,
                seq=seq,
                iface=iface,
                name=f"校验-缺少必填/空 Body-{display}",
                category="validation",
                query=merge_query(iface, yapi, fixtures, mode="valid", policy="negative"),
                body=build_body_from_schema(
                    schema, fixtures, mode="empty", binding=binding, policy="negative"
                ),
                expected=undeclared_error_http_expected(
                    note="全部声明字段默认必填；契约未声明缺 Body 的 HTTP 码，禁止猜测必须 4xx"
                ),
                executable=True,
                skip_reason=None,
                fixtures_refs=[],
                path_params=resolve_path_params(iface, yapi, fixtures, policy="negative"),
                negative_construction={
                    "strategy": "omit_required_body",
                    "basis": f"请求 schema 声明字段 {sorted(str(x) for x in (schema or {}).get('properties') or {})} 全部默认必填；本用例刻意发送空 Body",
                },
            )
        )
        seq += 1
    if mutating and isinstance(schema, dict) and (schema.get("properties") or schema.get("required")):
        cases.append(
            make_case(
                eid=eid,
                seq=seq,
                iface=iface,
                name=f"校验-字段类型错误-{display}",
                category="validation",
                query=merge_query(iface, yapi, fixtures, mode="valid", policy="negative"),
                body=build_body_from_schema(
                    schema, fixtures, mode="invalid_type", binding=binding, policy="negative"
                ),
                expected=undeclared_error_http_expected(
                    note="契约未声明类型错误的 HTTP 码；允许 2xx 或 4xx，禁止 5xx"
                ),
                executable=True,
                skip_reason=None,
                fixtures_refs=[],
                path_params=resolve_path_params(iface, yapi, fixtures, policy="negative"),
                negative_construction={
                    "strategy": "invalid_field_type",
                    "basis": "按 schema 声明的字段类型反向取值（string→数字、number→字符串）",
                },
            )
        )
        seq += 1
    if (
        mutating
        and has_required
        and not (isinstance(schema, dict) and schema.get("properties"))
    ):
        bad_q = merge_query(iface, yapi, fixtures, mode="valid", policy="negative")
        bad_path = resolve_path_params(iface, yapi, fixtures, policy="negative")
        omitted = None
        if bad_q:
            key = next(iter(bad_q))
            omitted = f"query.{key}"
            bad_q.pop(key, None)
        elif bad_path:
            key = next(iter(bad_path))
            omitted = f"path.{key}"
            bad_path.pop(key, None)
        cases.append(
            make_case(
                eid=eid,
                seq=seq,
                iface=iface,
                name=f"校验-缺必填参数-{display}",
                category="validation",
                query=bad_q,
                body=None,
                expected=undeclared_error_http_expected(
                    note="全部声明字段默认必填；契约未声明缺参 HTTP 码，禁止猜测必须 4xx"
                ),
                executable=True,
                skip_reason=None,
                fixtures_refs=[],
                path_params=bad_path,
                negative_construction={
                    "strategy": "omit_default_required_param",
                    "basis": f"忽略 required/optional 标记，刻意移除默认必填字段 {omitted}",
                },
            )
        )
        seq += 1
    if mutating and not has_required:
        cases.append(
            make_case(
                eid=eid,
                seq=seq,
                iface=iface,
                name=f"校验-无请求字段探测-{display}",
                category="validation",
                query=merge_query(iface, yapi, fixtures, mode="valid", policy="negative"),
                body={},
                expected=undeclared_error_http_expected(
                    note="接口未声明任何请求字段，仅探测空请求；禁止强求 4xx 或必须 200"
                ),
                executable=True,
                skip_reason=None,
                fixtures_refs=[],
                path_params=resolve_path_params(iface, yapi, fixtures, policy="negative"),
                negative_construction={
                    "strategy": "empty_body_probe",
                    "basis": "接口没有可执行默认必填策略的请求字段，空请求只作探测",
                },
            )
        )
        seq += 1
    if not mutating:
        bad_q = merge_query(iface, yapi, fixtures, mode="valid", policy="negative")
        bad_path = resolve_path_params(iface, yapi, fixtures, policy="negative")
        if has_required:
            omitted = None
            if bad_q:
                key = next(iter(bad_q))
                omitted = f"query.{key}"
                bad_q.pop(key, None)
            elif bad_path:
                key = next(iter(bad_path))
                omitted = f"path.{key}"
                bad_path.pop(key, None)
            val_name = f"校验-缺必填参数-{display}"
            val_expected = undeclared_error_http_expected(
                note="全部声明字段默认必填；契约未声明缺参 HTTP 码，禁止猜测必须 4xx"
            )
            val_basis = {
                "strategy": "omit_required_query",
                "basis": f"忽略 required/optional 标记，刻意移除默认必填字段 {omitted}",
            }
        elif listish:
            val_name = f"校验-分页契约探测-{display}"
            val_expected = undeclared_error_http_expected(
                note="仅使用有来源的分页值；契约未声明非法分页 HTTP 码，禁止硬编码边界或强求 4xx"
            )
            val_basis = {
                "strategy": "pagination_contract_probe",
                "basis": "契约存在分页字段；沿用真实 fixture 或契约值探测响应",
            }
        else:
            val_name = f"校验-无请求参数-{display}"
            # 无参接口的空请求 ≈ 成功路径调用：用契约 schema，禁止臆造「必须 HTTP 200」
            val_expected = success_path_expected(
                yapi,
                iface,
                note="接口未声明请求参数；按成功路径契约 schema 断言，禁止臆造必须 200",
            )
            val_basis = {
                "strategy": "no_request_params",
                "basis": "接口无请求参数，不适用缺参校验；空请求按成功路径契约校验",
            }
        cases.append(
            make_case(
                eid=eid,
                seq=seq,
                iface=iface,
                name=val_name,
                category="validation",
                query=bad_q,
                body=None,
                expected=val_expected,
                executable=True,
                skip_reason=None,
                fixtures_refs=[],
                path_params=bad_path,
                negative_construction=val_basis,
            )
        )
        seq += 1

    # 3) auth
    if should_generate_auth(iface):
        auth_meta = iface.get("auth") or {}
        auth_required = bool(auth_meta.get("required"))
        functional_case = cases[0]
        functional_request = functional_case.get("request") or {}
        auth_query = deepcopy(functional_request.get("query") or {})
        auth_body = deepcopy(functional_request.get("body"))
        auth_path = deepcopy(functional_request.get("path_params") or {})
        auth_trace = deepcopy(functional_case.get("fixture_trace") or [])
        auth_refs = deepcopy(functional_case.get("fixture_refs") or [])
        auth_unresolved = [
            str(blocker).removeprefix("required_binding_missing:")
            for blocker in functional_case.get("regression_blockers") or []
            if str(blocker).startswith("required_binding_missing:")
        ]
        cases.append(
            make_case(
                eid=eid,
                seq=seq,
                iface=iface,
                name=(f"鉴权-无鉴权头应拒绝-{display}" if auth_required else f"鉴权探测-契约未声明强制鉴权-{display}"),
                category="auth",
                query=auth_query,
                body=auth_body,
                expected=(
                    {"status_class": "4xx", "prefer_status": [401, 403]}
                    if auth_required
                    else {
                        "status_class": "2xx_or_4xx",
                        "not_status": [500, 502, 503, 504],
                        "note": "契约未声明强制鉴权；200 不算失败，禁止当鉴权门禁",
                    }
                ),
                executable=True,
                skip_reason=None,
                fixtures_refs=auth_refs,
                unresolved=auth_unresolved,
                path_params=auth_path,
                negative_construction={
                    "strategy": "drop_auth_header",
                    "basis": (
                        "契约声明强制鉴权，移除鉴权头应被拒绝"
                        if auth_required
                        else "契约未声明强制鉴权，仅做无鉴权头探测；业务入参仍使用真实 fixture"
                    ),
                },
            )
        )
        cases[-1]["fixture_trace"] = auth_trace
        # 强制鉴权：去掉身份头；未声明强制鉴权：保留 Header uid（业务身份，非 Authorization）
        auth_headers = {"Accept": "application/json"}
        if mutating:
            auth_headers["Content-Type"] = "application/json"
        cases[-1]["request"]["headers"] = apply_context_uid_header(
            auth_headers,
            category="auth",
            fixtures=fixtures,
            drop_identity=bool(auth_required),
        )
        seq += 1

    # 4) error — only when contract can derive a branch
    dead = NEGATIVE_UNKNOWN_ID
    if required_ids:
        err_q = merge_query(iface, yapi, fixtures, mode="error_not_found", policy="negative")
        err_path = resolve_path_params(iface, yapi, fixtures, policy="negative")
        path_names = {
            str(p.get("name") or "")
            for p in (iface.get("path_parameters") or [])
            if isinstance(p, dict)
        }
        for key in required_ids:
            if key in path_names:
                err_path[key] = dead
        err_body = None
        if mutating:
            err_body = (
                build_body_from_schema(
                    schema, fixtures, mode="valid", binding=binding, policy="negative"
                )
                or {}
            )
            if isinstance(err_body, dict):
                properties = set((schema or {}).get("properties") or {})
                for key in required_ids:
                    if key in err_body or key in properties:
                        err_body[key] = dead
        cases.append(
            make_case(
                eid=eid,
                seq=seq,
                iface=iface,
                name=f"错误契约-默认必填资源标识不存在-{display}",
                category="error",
                query=err_q,
                body=err_body if mutating else None,
                expected=undeclared_error_http_expected(
                    note="只替换资源 ID；其余字段保持类型合法或省略。HTTP 状态等代码返回点闭合后再断言"
                ),
                executable=True,
                skip_reason=None,
                fixtures_refs=[],
                path_params=err_path,
                negative_construction={
                    "strategy": "unknown_required_resource_id",
                    "basis": f"资源标识 {sorted(required_ids)} 按默认必填处理；替换为确定不存在的 {dead}",
                },
            )
        )
        seq += 1
    elif optional_ids and listish:
        err_q = merge_query(iface, yapi, fixtures, mode="error_not_found", policy="negative")
        cases.append(
            make_case(
                eid=eid,
                seq=seq,
                iface=iface,
                name=f"错误契约-可选过滤未知标识应空列表-{display}",
                category="error",
                query=err_q,
                body=None,
                expected={
                    **success_path_expected(
                        yapi,
                        iface,
                        note="YApi required=0；未知过滤 ID 按成功路径 schema 断言，并期望空集合语义",
                    ),
                    "empty_collection": True,
                },
                executable=True,
                skip_reason=None,
                fixtures_refs=[],
                path_params=resolve_path_params(iface, yapi, fixtures, policy="negative"),
                negative_construction={
                    "strategy": "unknown_optional_filter_id",
                    "basis": f"契约可选过滤标识 {sorted(optional_ids)} required=0；未知值应得空列表而非臆造 4xx",
                },
            )
        )
        seq += 1
    # else: no resource identifier — do not emit fake 4xx not-found

    # 5) side_effect for mutating writes only
    if mutating:
        destructive = any(
            x in str(iface.get("path") or "").lower()
            for x in ("delete", "remove", "disable", "unshelve", "abolish", "recall")
        ) or any(
            x in display for x in ("删除", "停用", "下架", "废弃", "撤回")
        )
        executable = write_authorized or not destructive
        skip = None if executable else "破坏性写操作：测试环境未记录显式写授权"
        side_unresolved: list[str] = []
        side_query = merge_query(iface, yapi, fixtures, mode="valid", unresolved=side_unresolved)
        side_path = resolve_path_params(iface, yapi, fixtures, unresolved=side_unresolved)
        side_body = build_body_from_schema(
            schema,
            fixtures,
            mode="valid",
            iface=iface,
            binding=binding,
            unresolved=side_unresolved,
        )
        side_trace = query_fixture_trace(iface, yapi, fixtures, side_query)
        side_trace.extend(body_fixture_trace(schema, fixtures, side_body, binding, iface))
        side_trace.extend(path_fixture_trace(iface, yapi, fixtures, side_path))
        side_refs = [str(item["ref"]) for item in side_trace if item.get("ref")]
        cases.append(
            make_case(
                eid=eid,
                seq=seq,
                iface=iface,
                name=f"副作用与清理-{display}",
                category="side_effect",
                query=side_query,
                body=side_body,
                expected={
                    **success_path_expected(
                        yapi,
                        iface,
                        note="写副作用按契约 schema 断言；禁止只用 status_class=2xx 且无 schema",
                    ),
                    "cleanup": "required" if not destructive else "blocked",
                },
                executable=executable,
                skip_reason=skip,
                fixtures_refs=side_refs,
                unresolved=side_unresolved,
                path_params=side_path,
            )
        )
        cases[-1]["fixture_trace"] = side_trace
        cases[-1]["fixture_policy"] = "required"
        cases[-1]["write_authorization"] = {
            "authorized": write_authorized,
            "destructive": destructive,
        }
        seq += 1

    # 6) list cases — 分页值必须有来源；边界值只在契约/fixture 给出时才测
    # list 属成功路径类别：必填业务参数必须是真实绑定，分页值也必须可追溯
    if listish:
        boundaries = {
            kind: resolve_pagination_boundary(iface, yapi, fixtures, kind)
            for kind in ("min", "max")
        }
        uncovered = [kind for kind, found in boundaries.items() if not found]
        coverage = (
            {
                "mode": "contract_probe",
                "reason": "契约与 fixture 未提供分页边界值，仅按有来源的分页值探测契约行为",
                "uncovered": uncovered,
            }
            if len(uncovered) == 2
            else {
                "mode": "sourced_boundary",
                "reason": "分页边界值来自契约 minimum/maximum 或 fixture",
                "uncovered": uncovered,
            }
        )
        variants: list[tuple[str, str, tuple[str, Any, str] | None]] = [
            ("显式limit/pageSize正常分页", "请求必须携带有来源的 limit 或 pageSize", None)
        ]
        for kind, label in (("min", "下界"), ("max", "上界")):
            found = boundaries[kind]
            if not found:
                continue
            bname, bvalue, bsource = found
            variants.append(
                (
                    f"分页{label}边界-{bname}={bvalue}",
                    f"边界值来自 {bsource}，不是硬编码",
                    found,
                )
            )
        for suffix, note, boundary in variants:
            list_unresolved: list[str] = []
            list_query = merge_query(
                iface, yapi, fixtures, mode="list_normal", unresolved=list_unresolved
            )
            if boundary:
                list_query[boundary[0]] = boundary[1]
            list_path = resolve_path_params(iface, yapi, fixtures, unresolved=list_unresolved)
            list_body = (
                None
                if method == "GET" or not schema
                else build_body_from_schema(
                    schema,
                    fixtures,
                    mode="valid",
                    iface=iface,
                    binding=binding,
                    unresolved=list_unresolved,
                )
            )
            expected = success_path_expected(yapi, iface, note=note)
            list_trace = query_fixture_trace(iface, yapi, fixtures, list_query)
            if boundary:
                list_trace = [
                    item for item in list_trace if item.get("ref") != f"query.{boundary[0]}"
                ]
                list_trace.append(
                    {
                        "ref": f"query.{boundary[0]}",
                        "source": boundary[2],
                        "value": boundary[1],
                        "required": False,
                    }
                )
            list_trace.extend(
                body_fixture_trace(schema, fixtures, list_body, binding, iface)
            )
            list_trace.extend(path_fixture_trace(iface, yapi, fixtures, list_path))
            list_refs = [str(item["ref"]) for item in list_trace if item.get("ref")]
            cases.append(
                make_case(
                    eid=eid,
                    seq=seq,
                    iface=iface,
                    name=f"列表-{suffix}-{display}",
                    category="list",
                    query=list_query,
                    body=list_body,
                    expected=expected,
                    executable=True,
                    skip_reason=None,
                    fixtures_refs=list_refs,
                    unresolved=list_unresolved,
                    path_params=list_path,
                )
            )
            cases[-1]["fixture_trace"] = list_trace
            cases[-1]["fixture_policy"] = "required"
            cases[-1]["pagination_coverage"] = dict(coverage)
            seq += 1

    for case in cases:
        normalize_case_fixture_trace(case, iface, yapi, fixtures, schema)
    return cases


# ops 有时会存「刻意非法」的 fixture（如 invalid_id）；导出时必须标出来，
# 否则下游会把它当成可用于成功路径的真实业务数据
NEGATIVE_FIXTURE_HINT = re.compile(
    r"invalid|not_?found|non_?existent|nonexist|dead|deleted|expired|illegal",
    re.I,
)


def env_reference(key: str) -> str:
    """敏感 fixture 的唯一放行形式：环境变量引用。"""
    slug = re.sub(r"[^0-9A-Za-z]+", "_", str(key)).strip("_").upper()
    return f"${{API_TEST_{slug}}}" if slug else "${API_TEST_SECRET}"


def fixture_entry(
    key: str,
    value: Any,
    source: str,
    verified_at: Any,
    usage: str | None = None,
) -> dict:
    """敏感键只写环境变量引用；非敏感键写真实值 + 来源 + 核验时间。"""
    usage = str(usage or "").strip().lower() or (
        "negative_only"
        if NEGATIVE_FIXTURE_HINT.search(str(key))
        or NEGATIVE_FIXTURE_HINT.search(str(source))
        else "success_path"
    )
    if SENSITIVE_KEYS.search(str(key)):
        ref = str(value).strip() if isinstance(value, str) and value.strip().startswith("${") else env_reference(key)
        return {
            "value_ref": ref,
            "sensitive": True,
            "usage": usage,
            "source": source,
            "verified_at": verified_at,
        }
    return {
        "value": value,
        "sensitive": False,
        "usage": usage,
        "source": source,
        "verified_at": verified_at,
    }


def sync_pending_requirements(fixtures: dict, cases: list[dict]) -> dict:
    """成功路径缺绑定必须出现在 pending_requirements，禁止 pending 为空却标跳过。"""
    needed: dict[tuple[str, str], list[str]] = {}
    for case in cases:
        if not isinstance(case, dict):
            continue
        if case.get("category") not in SUCCESS_PATH_CATEGORIES | {"auth"}:
            continue
        endpoint_id = str(case.get("endpoint_id") or "")
        case_id = str(case.get("id") or "")
        for blocker in case.get("regression_blockers") or []:
            text = str(blocker)
            if not text.startswith("required_binding_missing:"):
                continue
            field = text.split(":", 1)[1].strip()
            if not field:
                continue
            needed.setdefault((endpoint_id, field), [])
            if case_id and case_id not in needed[(endpoint_id, field)]:
                needed[(endpoint_id, field)].append(case_id)

    existing = fixtures.get("pending_requirements") or []
    kept: list[dict] = []
    seen: set[tuple[str, str]] = set()
    if isinstance(existing, list):
        for item in existing:
            if not isinstance(item, dict):
                continue
            key = (str(item.get("endpoint_id") or ""), str(item.get("field") or ""))
            if key not in needed or key in seen:
                continue
            item = dict(item)
            case_ids = [str(x) for x in (item.get("case_ids") or []) if str(x)]
            for case_id in needed[key]:
                if case_id not in case_ids:
                    case_ids.append(case_id)
            item["case_ids"] = case_ids
            item["reason"] = item.get("reason") or "required_binding_missing"
            if not isinstance(item.get("attempts"), list):
                item["attempts"] = []
            kept.append(item)
            seen.add(key)
    for key, case_ids in needed.items():
        if key in seen:
            continue
        kept.append(
            {
                "endpoint_id": key[0],
                "field": key[1],
                "case_ids": case_ids,
                "reason": "required_binding_missing",
                "attempts": [],
            }
        )
    fixtures["pending_requirements"] = kept
    return fixtures


def build_regression_fixtures(catalog: dict, fixtures: dict) -> dict:
    """把 ops 产出的 fixtures.json 收敛成可上传的回归 fixture 清单。

    只收录真实值：占位取值（`test` / `placeholder` / 零 UUID / `{xxx}`）进
    `rejected`，敏感取值只保留 `${ENV}` 引用，绝不复制明文。
    """
    shared = shared_fixtures(fixtures)
    shared_sources = fixtures.get("sources") or (fixtures.get("fixtures") or {}).get("sources") or {}
    shared_verified = fixtures.get("verified_at")

    out: dict[str, dict] = {}
    rejected: dict[str, dict] = {}

    def add(key: str, name: str, value: Any, source: str, verified_at: Any) -> None:
        declared_usage = None
        if isinstance(value, dict) and "value" in value:
            declared_usage = value.get("usage")
            source = str(value.get("source") or source)
            value = value.get("value")
        if value is None or (isinstance(value, str) and not value.strip()):
            rejected[key] = {"reason": "empty_value", "source": source}
            return
        if contains_placeholder(value):
            rejected[key] = {"reason": "placeholder_value", "source": source}
            return
        out[key] = fixture_entry(
            name, value, source, verified_at, usage=declared_usage
        )

    for name, value in shared.items():
        add(
            f"shared.{name}",
            str(name),
            value,
            str(shared_sources.get(name) or f"fixtures.json#shared.{name}"),
            shared_verified,
        )

    for eid, entry in (fixtures.get("endpoint_bindings") or {}).items():
        if not isinstance(entry, dict):
            continue
        values = endpoint_binding({"id": eid}, fixtures)
        sources = entry.get("sources") or {}
        verified_at = entry.get("verified_at", shared_verified)
        for name, value in values.items():
            add(
                f"{eid}.{name}",
                str(name),
                value,
                str(sources.get(name) or f"fixtures.json#endpoint_bindings.{eid}.{name}"),
                verified_at,
            )

    return {
        "schema_version": "1.0",
        "service": catalog.get("service_name") or catalog.get("service") or "unknown",
        "generated_at": now_iso(),
        "fixture_count": len(out),
        "fixtures": out,
        "rejected": rejected,
    }


def write_plan(path: Path, catalog: dict, cases: list[dict]) -> None:
    by_ep: dict[str, list[dict]] = {}
    for c in cases:
        by_ep.setdefault(str(c["endpoint_id"]), []).append(c)
    counts = Counter(c.get("category") for c in cases)
    lines = [
        f"# {catalog.get('service_name') or 'service'} 测试计划",
        "",
        f"- Base URL：`{(catalog.get('base_url') or '').rstrip('/')}/`",
        f"- 接口数：{len(catalog.get('interfaces') or [])}",
        f"- 用例总数：{len(cases)}",
        f"- 策略：**回归向覆盖**（全部请求字段默认必填；禁止无资源 ID 套「不存在→4xx」），**不含默认冒烟**",
        f"- 类别统计：{dict(counts)}",
        f"- 可执行：{sum(1 for c in cases if c.get('executable'))}；不可执行：{sum(1 for c in cases if not c.get('executable'))}",
        "",
        "## 按接口",
        "",
    ]
    for iface in catalog.get("interfaces") or []:
        eid = iface.get("id")
        lines.append(
            f"### {eid} {iface.get('method')} `{iface.get('path')}` ({iface.get('source_label')})"
        )
        ep_cases = by_ep.get(str(eid), [])
        for c in ep_cases:
            exe = "可执行" if c.get("executable") else f"不可执行:{c.get('skip_reason')}"
            lines.append(
                f"- `{c['id']}` [{c.get('category')}] {c.get('name')} — {exe}"
            )
        if not should_generate_auth(iface):
            # evidence_ref 缺失时回退到确定性引用，证据文件本身仍由 validator 校验
            evidence_ref = str(
                (iface.get("app_usage") or {}).get("evidence_ref") or ""
            ).strip() or f"app-usage-evidence.json#{eid}"
            lines.append(
                f"- `UNCOVERED` [auth] 已确认非 APP 调用接口；证据见 `{evidence_ref}`"
            )
        if not any(c.get("category") == "error" for c in ep_cases):
            lines.append("- `UNCOVERED` [error] 无资源标识，禁止机械套「资源不存在→4xx」")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--yapi-raw", type=Path, default=None)
    parser.add_argument("--fixtures", type=Path, default=None)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--write-plan", action="store_true")
    parser.add_argument("--write-cases", action="store_true")
    parser.add_argument("--allow-smoke", action="store_true", help="reserved; this script never emits smoke")
    parser.add_argument(
        "--no-fixtures",
        action="store_true",
        help="skip writing <run_dir>/regression-fixtures.json",
    )
    args = parser.parse_args()

    catalog = load_json(args.catalog)
    if not isinstance(catalog, dict):
        raise SystemExit("catalog must be object")
    yapi_raw = load_json(args.yapi_raw)
    fixtures = load_json(args.fixtures) or {"fixtures": {"shared": {}}, "endpoint_bindings": {}}
    if not isinstance(fixtures, dict):
        fixtures = {"fixtures": {"shared": {}}, "endpoint_bindings": {}}

    run_context = load_json(args.run_dir / "run-context.json")
    if not isinstance(run_context, dict):
        run_context = {}
    authorization = resolve_write_authorization(run_context, os.environ)
    write_authorized = bool(authorization.allowed)

    idx = yapi_index(yapi_raw if isinstance(yapi_raw, dict) else None)
    all_cases: list[dict] = []
    for iface in catalog.get("interfaces") or []:
        if not isinstance(iface, dict):
            continue
        key = (str(iface.get("method") or "").upper(), str(iface.get("path") or ""))
        all_cases.extend(
            expand_interface(
                iface,
                idx.get(key),
                fixtures,
                write_authorized=write_authorized,
            )
        )

    # hard gate: no smoke
    if any(c.get("category") == "smoke" for c in all_cases) and not args.allow_smoke:
        raise SystemExit("internal error: smoke cases generated without allow")

    args.run_dir.mkdir(parents=True, exist_ok=True)
    if args.write_plan:
        write_plan(args.run_dir / "test-plan.md", catalog, all_cases)

    fixtures = sync_pending_requirements(fixtures, all_cases)
    if args.write_cases:
        fixture_path = args.fixtures or (args.run_dir / "fixtures.json")
        fixture_path.write_text(
            json.dumps(fixtures, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    regression_fixtures = build_regression_fixtures(catalog, fixtures)
    if not args.no_fixtures:
        (args.run_dir / "regression-fixtures.json").write_text(
            json.dumps(regression_fixtures, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    payload = {
        "schema_version": "1.0",
        "generated_at": now_iso(),
        "test_kind": "functional",
        "run_mode": "full",
        "case_count": len(all_cases),
        "category_counts": dict(Counter(c.get("category") for c in all_cases)),
        "coverage_policy": "full_no_default_smoke",
        "cases": all_cases,
    }
    if args.write_cases:
        (args.run_dir / "test-cases.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    print(
        json.dumps(
            {
                "ok": True,
                "interfaces": len(catalog.get("interfaces") or []),
                "case_count": len(all_cases),
                "category_counts": payload["category_counts"],
                "executable": sum(1 for c in all_cases if c.get("executable")),
                "blocked_by_missing_binding": sum(
                    1
                    for c in all_cases
                    if any(
                        str(b).startswith("required_binding_missing")
                        for b in (c.get("regression_blockers") or [])
                    )
                ),
                "regression_fixtures": regression_fixtures["fixture_count"],
                "rejected_fixtures": len(regression_fixtures["rejected"]),
                "wrote_plan": bool(args.write_plan),
                "wrote_cases": bool(args.write_cases),
                "wrote_fixtures": not args.no_fixtures,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
