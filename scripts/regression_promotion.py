#!/usr/bin/env python3
"""回归晋升判定内核与执行后产物生成。

晋升口径（产品约定，2026-09-22 起）：
普通 `passed` 用例进入回归集，并保留完整请求数据（query/body/path_params 取值）。
不再因 fixture 追溯、负向期望精确度、schema/业务码启发式等因素把「已通过」挡在回归集外。
代码核对后状态码和响应格式都一致的 5xx，与普通 `passed` 一样进入回归集。
未通过（failed/skipped/not_run）进入 backlog。

`build_promotion_artifacts(run_dir)` 关联 `test-cases.json`、`execution/results.json`、
`execution/requests/<case-id>.json`，回写全量用例的执行与晋升状态，并落盘
`regression-promotion.json`、`regression-cases.json` / `.md`、`backlog-cases.md`。
敏感字段在写入回归集前转为 `${API_TEST_*}` 引用，禁止明文上传。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

PLACEHOLDER_VALUES = {
    "test",
    "placeholder",
    "api-test-placeholder",
    "00000000-0000-0000-0000-000000000000",
}

SENSITIVE_KEYS = re.compile(
    r"authorization|token|cookie|password|phone|mobile|idcard|database_uri",
    re.I,
)

# fixture_trace 可为空的策略
FIXTURE_OPTIONAL_POLICIES = {"none_ok", "negative_case"}

# 成功路径类别：需 2xx + 业务成功码 + schema
SUCCESS_PATH_CATEGORIES = {"functional", "list", "side_effect"}

# 负向用例类别：期望必须精确
NEGATIVE_CATEGORIES = {"validation", "auth", "error"}

# 契约未声明 success_when.code_in 时：不猜测默认成功码（禁止默认 {0,200}）
DEFAULT_SUCCESS_CODES: set[int] = set()


# schema.required_fields 里的注释项，不参与校验
COMMENT_FIELD = re.compile(r"^\s*/\*.*\*/\s*$", re.S)

# 可接受的负向状态类：形如 4xx / 5xx；`2xx_or_4xx`、`non_5xx` 等模糊表达不算精确
STATUS_CLASS = re.compile(r"^([1-5])xx$", re.I)

# 环境变量引用（敏感值唯一放行形式，也不算未替换占位）
ENV_REFERENCE = re.compile(r"\$\{[^{}]+\}")

# 字符串内部未替换的占位，如 `/users/{id}`
INLINE_PLACEHOLDER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_.\-]*\}")

# 用于区分「键不存在」与「键存在但值为 null」
_MISSING = object()


def contains_placeholder(value: object, path: str = "") -> list[str]:
    """返回所有取值仍是占位符的字段路径（按遍历顺序）。"""
    matches: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            matches.extend(contains_placeholder(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            matches.extend(contains_placeholder(item, f"{path}[{index}]"))
    elif isinstance(value, str):
        stripped = value.strip()
        normalized = stripped.lower()
        # `${ENV}` 是环境变量引用，不算未替换占位
        without_env = ENV_REFERENCE.sub("", stripped)
        if normalized in PLACEHOLDER_VALUES or INLINE_PLACEHOLDER.search(without_env):
            matches.append(path)
    return matches


def find_sensitive_values(value: object, path: str = "") -> list[str]:
    """返回敏感字段中直接写了明文（非 `${ENV}` 引用）的字段路径。"""
    matches: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            if SENSITIVE_KEYS.search(str(key)):
                if _is_plaintext_secret(item):
                    matches.append(child)
                    # 该子树已整体判为明文，无需再逐层重复上报
                    continue
                if _is_env_reference(item):
                    continue
            matches.extend(find_sensitive_values(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            matches.extend(find_sensitive_values(item, f"{path}[{index}]"))
    return sorted(set(matches))


def _is_env_reference(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"\$\{[^{}]+\}", value.strip()))


def _is_plaintext_secret(value: object) -> bool:
    """敏感键下任何非空且非 `${ENV}` 引用的值都算明文。"""
    if value is None:
        return False
    if _is_env_reference(value):
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def evaluate_case(case: dict, result: dict | None, evidence: dict | None) -> dict:
    """判定单条用例能否晋升。

    新口径：普通 `execution_status=passed` → `regression_ready=true`。
    其它诊断项（缺证据、fixture、期望精度等）仅写入 `promotion_notes`，不挡晋升。
    代码核对后状态码和响应格式都一致的 5xx 同样晋升。
    """
    case = case or {}
    notes: list[str] = []
    status = (result or {}).get("status", "not_run")

    if status != "passed":
        return {
            "execution_status": status,
            "regression_ready": False,
            "regression_blockers": ["execution_not_passed"],
            "promotion_notes": notes,
        }

    request = case.get("request")
    notes.extend(f"placeholder:{p}" for p in contains_placeholder(request))
    notes.extend(f"sensitive:{p}" for p in find_sensitive_values(request))

    if (
        not _has_traceable_fixture(case.get("fixture_trace"))
        and case.get("fixture_policy") not in FIXTURE_OPTIONAL_POLICIES
    ):
        notes.append("fixture_trace_missing")

    if not evidence:
        notes.append("request_evidence_missing")

    category = str(case.get("category") or "").strip().lower()
    if category in SUCCESS_PATH_CATEGORIES:
        notes.extend(_success_path_blockers(case, evidence))
    elif category in NEGATIVE_CATEGORIES:
        if evidence:
            notes.extend(_negative_blockers(case, evidence))
    elif category:
        notes.append("unsupported_category")

    return {
        "execution_status": "passed",
        "regression_ready": True,
        "regression_blockers": [],
        "promotion_notes": _dedupe(notes),
    }


def redact_sensitive_request(request: object) -> object:
    """把请求里的敏感明文改成 ${API_TEST_*}，保留非敏感参数取值。"""
    if not isinstance(request, dict):
        return request
    out = deepcopy(request)

    def walk(node: object, key_hint: str = "") -> object:
        if isinstance(node, dict):
            result: dict = {}
            for key, value in node.items():
                child_hint = str(key)
                if SENSITIVE_KEYS.search(str(key)):
                    if isinstance(value, str) and re.fullmatch(r"\$\{[^{}]+\}", value.strip()):
                        result[key] = value
                    else:
                        slug = re.sub(r"[^0-9A-Za-z]+", "_", str(key)).strip("_").upper() or "SECRET"
                        result[key] = f"${{API_TEST_{slug}}}"
                    continue
                result[key] = walk(value, child_hint)
            return result
        if isinstance(node, list):
            return [walk(item, key_hint) for item in node]
        return node

    return walk(out)



def _has_traceable_fixture(trace: object) -> bool:
    """fixture_trace 每项必须是 dict 且带非空 source，否则视为不可追溯。"""
    if not isinstance(trace, (list, tuple)) or not trace:
        return False
    for item in trace:
        if not isinstance(item, dict):
            return False
        if not str(item.get("source") or "").strip():
            return False
    return True


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _response(evidence: dict | None) -> dict:
    if not isinstance(evidence, dict):
        return {}
    response = evidence.get("response")
    return response if isinstance(response, dict) else {}


def _is_structured_business_reject(status: object, body: object) -> bool:
    if status != 400 or not isinstance(body, dict):
        return False
    return any(key in body for key in ("code", "reason", "message", "msg"))


def _success_path_blockers(case: dict, evidence: dict | None) -> list[str]:
    """functional/list/side_effect：证据须 2xx（或结构化 400）+ 契约声明的业务成功条件 + schema。"""
    blockers: list[str] = []
    response = _response(evidence)
    status = response.get("status")
    body = response.get("body") if response else _MISSING

    if evidence:
        if _is_structured_business_reject(status, body if body is not _MISSING else None):
            # 结构化 400：与执行器一致，不要求业务成功码，也不因成功体 schema 卡晋升
            return blockers
        if not isinstance(status, int) or isinstance(status, bool) or not 200 <= status <= 299:
            blockers.append("functional_not_business_success")
        elif not _business_success(case, body):
            blockers.append("functional_not_business_success")

    if _schema_mismatch(case, body):
        blockers.append("functional_schema_mismatch")
    return blockers


def _business_success(case: dict, body: object) -> bool:
    """独立复核业务成功：仅当契约显式声明 code_in / ok_is 时才校验取值。"""
    if not isinstance(body, dict):
        # 直出非对象（如列表、文本）：2xx 即视作业务成功
        return True

    expected = case.get("expected") if isinstance(case.get("expected"), dict) else {}
    business = expected.get("business") if isinstance(expected.get("business"), dict) else {}
    success_when = (
        business.get("success_when") if isinstance(business.get("success_when"), dict) else {}
    )

    code_paths = business.get("code_path") or ["code"]
    if isinstance(code_paths, str):
        code_paths = [code_paths]
    code = _numeric_code(_lookup(body, code_paths))
    if code is not None:
        allowed = success_when.get("code_in")
        allowed_codes = {
            int(item)
            for item in (allowed if isinstance(allowed, (list, tuple, set)) else [])
            if _numeric_code(item) is not None
        }
        # 未声明 code_in：不因出现 code 字段而用默认 {0,200} 拦截
        if allowed_codes and code not in allowed_codes:
            return False

    ok = body.get("ok")
    if isinstance(ok, bool):
        ok_is = success_when.get("ok_is")
        # 未声明 ok_is：不默认要求 ok===true
        if isinstance(ok_is, bool) and ok is not ok_is:
            return False

    return True


def _schema_mismatch(case: dict, body: object) -> bool:
    """按最小 JSON Schema 子集校验响应体；声明缺失或类型/必填字段不符时为 True。"""
    expected = case.get("expected") if isinstance(case.get("expected"), dict) else {}
    schema = expected.get("schema") if isinstance(expected.get("schema"), dict) else None
    if not schema:
        return True

    required = schema.get("required_fields")
    fields = [
        field.strip()
        for field in (required if isinstance(required, (list, tuple)) else [])
        if isinstance(field, str) and field.strip() and not COMMENT_FIELD.match(field)
    ]
    properties = schema.get("properties")
    has_properties = isinstance(properties, dict) and bool(properties)
    has_supported_type = schema.get("type") in {
        "object",
        "array",
        "string",
        "number",
        "integer",
        "boolean",
    }
    if not fields and not has_properties and not has_supported_type:
        return True

    if body is _MISSING:
        return True
    if any(_lookup_raw(body, field) is _MISSING for field in fields):
        return True
    return not _matches_schema(body, schema)


def _matches_schema(value: object, schema: object) -> bool:
    """校验 object/array/标量类型、required 与实际出现 property 的递归类型。"""
    if not isinstance(schema, dict):
        return True

    schema_type = schema.get("type")
    properties = schema.get("properties")
    # properties 本身即意味着对象；尤其 properties-only 不能放过数组/字符串响应。
    if (schema_type == "object" or isinstance(properties, dict)) and not isinstance(value, dict):
        return False
    if schema_type == "array" and not isinstance(value, list):
        return False
    if schema_type == "string" and not isinstance(value, str):
        return False
    if schema_type == "number" and (
        isinstance(value, bool) or not isinstance(value, (int, float))
    ):
        return False
    if schema_type == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
        return False
    if schema_type == "boolean" and not isinstance(value, bool):
        return False

    if isinstance(value, dict):
        required = schema.get("required")
        if isinstance(required, (list, tuple)) and any(
            isinstance(field, str) and field not in value for field in required
        ):
            return False
        if isinstance(properties, dict):
            for field, child_schema in properties.items():
                # properties-only 不把字段变成 required；仅校验实际出现的字段。
                if field in value and not _matches_schema(value[field], child_schema):
                    return False

    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        return all(_matches_schema(item, schema["items"]) for item in value)
    return True


def _negative_blockers(case: dict, evidence: dict | None) -> list[str]:
    """validation/auth/error：期望必须精确，且实际状态命中期望。"""
    response = _response(evidence)
    status = response.get("status")

    expected = case.get("expected") if isinstance(case.get("expected"), dict) else {}
    http = expected.get("http") if isinstance(expected.get("http"), dict) else {}

    exact = _status_list(http.get("status")) or _status_list(expected.get("status"))
    if exact:
        if isinstance(status, int) and not isinstance(status, bool) and status in exact:
            return []
        return ["negative_status_mismatch"]

    status_class = http.get("status_class") or expected.get("status_class")
    matched = STATUS_CLASS.match(str(status_class or ""))
    if matched:
        if (
            isinstance(status, int)
            and not isinstance(status, bool)
            and status // 100 == int(matched.group(1))
        ):
            return []
        return ["negative_status_mismatch"]

    return ["negative_expectation_not_exact"]


def _status_list(value: object) -> set[int]:
    if isinstance(value, bool):
        return set()
    if isinstance(value, int):
        return {value}
    if isinstance(value, (list, tuple, set)):
        codes = {int(item) for item in value if _numeric_code(item) is not None}
        return codes
    return set()


def _numeric_code(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value.strip())
    return None


def _lookup(body: dict, paths: list[str]) -> object | None:
    """按点号路径在响应体里取值，返回第一个存在且非 None 的结果。"""
    for path in paths:
        value = _lookup_raw(body, path)
        if value is not _MISSING and value is not None:
            return value
    return None


def _lookup_raw(body: dict, path: object) -> object:
    """按点号路径取值；键不存在返回 `_MISSING`，键存在但值为 null 返回 None。"""
    if not isinstance(path, str) or not path.strip():
        return _MISSING
    current: object = body
    for part in path.strip().split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return _MISSING
    return current


# ---------------------------------------------------------------------------
# 执行后产物：regression-promotion.json / regression-cases.* / backlog-cases.md
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "1.0"

CASES_REL = "test-cases.json"
RESULTS_REL = "execution/results.json"

# 每次执行后重新判定的 blocker（内核 + 产物层）：回写时按本轮实测覆盖。
# 不在此表、也不匹配前缀的 blocker 一律视为生成期结论，原样保留，绝不丢弃。
REEVALUATED_BLOCKERS = {
    "execution_not_passed",
    "request_evidence_missing",
    "request_evidence_invalid",
    "fixture_trace_missing",
    "unsupported_category",
    "functional_not_business_success",
    "functional_schema_mismatch",
    "negative_status_mismatch",
    "negative_expectation_not_exact",
}
REEVALUATED_PREFIXES = ("placeholder:", "sensitive:")

# 结果条目里 case ID / 状态 / 响应的字段别名（历史 run 的 executor 写法不统一）
RESULT_ID_KEYS = ("case_id", "id", "caseId", "case")
STATUS_KEYS = ("status", "state", "result")
RESPONSE_STATUS_KEYS = ("response_status", "http_status", "status_code", "statusCode")
RESPONSE_BODY_KEYS = ("response_body", "body", "responseBody")
ERROR_KEYS = ("error", "errors", "network_error", "failure", "message")

# results.json 的三种容器写法：顶层 list / `<key>` 为 list / `<key>` 为 id→entry 映射
RESULT_COLLECTION_KEYS = ("cases", "results", "tests")
# 摘要写法：`failed` / `skipped` / `passed` 直接给 ID 列表
SUMMARY_STATUS_KEYS = ("failed", "skipped", "passed")

EXECUTION_STATES = ("passed", "failed", "skipped", "not_run")

# 同一 case 出现多条结果时按「最差状态」合并，后写的 passed 不得覆盖先写的 failed
STATUS_SEVERITY = {"passed": 0, "not_run": 1, "skipped": 2, "failed": 3}
UNKNOWN_STATUS_SEVERITY = 4

SUMMARY_LIMIT = 200


class PromotionInputError(FileNotFoundError):
    """晋升输入缺失或损坏。

    继承 `FileNotFoundError` 以兼容「缺少必备产物」的调用方；输入损坏
    （JSON 解析失败、结构不可识别、ID 缺失或重复）同样走这个异常，
    因为损坏绝不能被降级成「文件不存在 → 全部 not_run」。
    """


def build_promotion_artifacts(run_dir: str | Path) -> dict:
    """关联执行证据、回写用例状态并生成回归集与 backlog，返回计数摘要。"""
    run_path = Path(run_dir)
    warnings: list[dict] = []

    cases_payload, cases = _load_test_cases(run_path)
    results = _load_results(run_path, warnings)
    known_ids = {_case_id(case) for case in cases}
    for case_id in results:
        if case_id not in known_ids:
            _warn(
                warnings,
                "result_id_not_in_test_cases",
                f"结果中的 {case_id} 在 test-cases.json 中不存在，已忽略",
                RESULTS_REL,
            )

    generated_at = _now()
    entries: list[dict] = []
    for case in cases:
        case_id = _case_id(case)
        result = results.get(case_id)
        evidence, evidence_invalid, evidence_rel = _load_evidence(
            run_path, case_id, result, warnings
        )
        promotion = evaluate_case(case, result, evidence)
        notes = list(promotion.get("promotion_notes") or [])
        if evidence_invalid:
            notes.append("request_evidence_invalid")

        if (
            promotion["execution_status"] == "passed"
            and promotion["regression_ready"] is True
        ):
            # 普通已通过用例直接晋升；代码证据确认的 5xx 产品缺陷在下一分支阻断。
            blockers: list[str] = []
            ready = True
            # 回归集保留请求取值；敏感键改为 ${ENV}
            if isinstance(case.get("request"), dict):
                case["request"] = redact_sensitive_request(case["request"])
        elif promotion["execution_status"] == "passed":
            blockers = _merge_blockers(
                list(promotion["regression_blockers"]),
                case.get("regression_blockers"),
            )
            ready = False
        else:
            blockers = _merge_blockers(
                list(promotion["regression_blockers"]) + notes,
                case.get("regression_blockers"),
            )
            ready = False

        case["execution_status"] = promotion["execution_status"]
        case["regression_ready"] = ready
        case["regression_blockers"] = blockers
        case["promotion_evaluated_at"] = generated_at
        if notes:
            case["promotion_notes"] = _dedupe(notes)

        entries.append(
            {
                "id": case_id,
                "name": case.get("name"),
                "endpoint_id": case.get("endpoint_id"),
                "category": case.get("category"),
                "method": _method(case, evidence),
                "path": _request_path(case),
                "execution_status": promotion["execution_status"],
                "regression_ready": ready,
                "regression_blockers": blockers,
                "promotion_notes": _dedupe(notes),
                "request_summary": _request_summary(case, evidence),
                "evidence": _evidence_digest(evidence, result, evidence_rel, evidence_invalid),
            }
        )

    promoted = [entry for entry in entries if entry["regression_ready"]]
    backlog = [entry for entry in entries if not entry["regression_ready"]]
    summary = {"total": len(entries), "promoted": len(promoted), "backlog": len(backlog)}

    _write_json(run_path / CASES_REL, cases_payload)

    service = _detect_service(run_path)
    promoted_ids = {entry["id"] for entry in promoted}

    _write_json(
        run_path / "regression-promotion.json",
        {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at,
            "run_dir": run_path.name,
            "service": service,
            "summary": summary,
            "execution": _execution_counts(entries),
            "blocker_summary": _blocker_counts(entries),
            "input_warnings": warnings,
            "cases": entries,
        },
    )
    _write_json(
        run_path / "regression-cases.json",
        {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at,
            "service": service,
            "source_run": run_path.name,
            "fixtures_ref": (
                "regression-fixtures.json"
                if (run_path / "regression-fixtures.json").exists()
                else None
            ),
            "case_count": len(promoted),
            "cases": [deepcopy(case) for case in cases if _case_id(case) in promoted_ids],
        },
    )
    _write_text(run_path / "regression-cases.md", _render_regression_md(promoted, generated_at))
    _write_text(
        run_path / "backlog-cases.md", _render_backlog_md(backlog, entries, warnings, generated_at)
    )

    return summary


# --- 输入加载 ---------------------------------------------------------------


def _read_json(path: Path, label: str) -> object:
    """读取 JSON；损坏一律抛带路径的 PromotionInputError，不得静默当成缺失。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PromotionInputError(f"{label} 解析失败（{path}）：{exc}") from exc


def _load_test_cases(run_path: Path) -> tuple[object, list[dict]]:
    path = run_path / CASES_REL
    if not path.exists():
        raise PromotionInputError(f"缺少 {CASES_REL}：{path}")

    payload = _read_json(path, CASES_REL)
    if isinstance(payload, dict):
        raw = payload.get("cases")
    elif isinstance(payload, list):
        raw = payload
    else:
        raise PromotionInputError(f"{CASES_REL} 结构不可识别（{path}）")
    if not isinstance(raw, list):
        raise PromotionInputError(f"{CASES_REL} 缺少 cases 列表（{path}）")

    cases: list[dict] = []
    seen: set[str] = set()
    for index, case in enumerate(raw, start=1):
        if not isinstance(case, dict):
            raise PromotionInputError(f"{CASES_REL} 第 {index} 条用例不是对象（{path}）")
        case_id = _case_id(case)
        if not case_id:
            raise PromotionInputError(f"{CASES_REL} 第 {index} 条用例缺少 ID（{path}）")
        if case_id in seen:
            raise PromotionInputError(f"{CASES_REL} 用例 ID 重复：{case_id}（{path}）")
        seen.add(case_id)
        cases.append(case)
    return payload, cases


def _case_id(case: dict) -> str:
    for key in RESULT_ID_KEYS:
        value = case.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _warn(warnings: list[dict], code: str, detail: str, location: str) -> None:
    warnings.append({"code": code, "detail": detail, "path": location})


def _load_results(run_path: Path, warnings: list[dict]) -> dict[str, dict]:
    """结果文件不存在 → 全部 not_run；存在但损坏/不可识别 → 报错。"""
    path = run_path / "execution" / "results.json"
    if not path.exists():
        return {}
    payload = _read_json(path, RESULTS_REL)
    return _parse_results(payload, path, warnings)


def _parse_results(payload: object, path: Path, warnings: list[dict]) -> dict[str, dict]:
    if payload is None:
        return {}
    if isinstance(payload, list):
        return _results_from_sequence(payload, warnings)
    if isinstance(payload, dict):
        for key in RESULT_COLLECTION_KEYS:
            if key not in payload:
                continue
            value = payload[key]
            if isinstance(value, list):
                return _results_from_sequence(value, warnings)
            if isinstance(value, dict):
                return _results_from_mapping(value, warnings)
            _warn(
                warnings,
                "result_collection_not_iterable",
                f"`{key}` 既不是列表也不是映射，已忽略",
                RESULTS_REL,
            )
        if any(key in payload for key in SUMMARY_STATUS_KEYS):
            return _results_from_summary(payload, warnings)
        if not payload:
            return {}
    raise PromotionInputError(f"{RESULTS_REL} 结构不可识别，无法关联用例（{path}）")


def _results_from_sequence(items: list, warnings: list[dict]) -> dict[str, dict]:
    mapping: dict[str, dict] = {}
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            _warn(warnings, "result_entry_not_object", f"第 {index} 条结果不是对象", RESULTS_REL)
            continue
        case_id = _case_id(item)
        if not case_id:
            _warn(
                warnings,
                "result_entry_missing_id",
                f"第 {index} 条结果缺少 case ID，无法关联用例",
                RESULTS_REL,
            )
            continue
        _merge_result(mapping, case_id, _normalize_result(item), warnings)
    return mapping


def _results_from_mapping(payload: dict, warnings: list[dict]) -> dict[str, dict]:
    mapping: dict[str, dict] = {}
    for key, item in payload.items():
        case_id = str(key).strip()
        if not case_id:
            _warn(warnings, "result_entry_missing_id", "结果映射存在空 case ID", RESULTS_REL)
            continue
        if isinstance(item, str):
            entry: dict = {"status": item}
        elif isinstance(item, dict):
            entry = dict(item)
        else:
            _warn(
                warnings,
                "result_entry_not_object",
                f"{case_id} 的结果既不是对象也不是状态字符串",
                RESULTS_REL,
            )
            continue
        entry["case_id"] = case_id
        _merge_result(mapping, case_id, _normalize_result(entry), warnings)
    return mapping


def _results_from_summary(payload: dict, warnings: list[dict]) -> dict[str, dict]:
    """`failed`/`skipped`/`passed` 为 ID 列表的摘要写法；只有计数时无法映射，记 warning。"""
    mapping: dict[str, dict] = {}
    for status_key in SUMMARY_STATUS_KEYS:
        if status_key not in payload:
            continue
        value = payload[status_key]
        if isinstance(value, list):
            for index, item in enumerate(value, start=1):
                if isinstance(item, str):
                    case_id, entry = item.strip(), {}
                elif isinstance(item, dict):
                    case_id, entry = _case_id(item), dict(item)
                else:
                    _warn(
                        warnings,
                        "result_entry_not_object",
                        f"`{status_key}` 第 {index} 项不是 ID 也不是对象",
                        RESULTS_REL,
                    )
                    continue
                if not case_id:
                    _warn(
                        warnings,
                        "result_entry_missing_id",
                        f"`{status_key}` 第 {index} 项缺少 case ID",
                        RESULTS_REL,
                    )
                    continue
                entry["case_id"] = case_id
                entry.setdefault("status", status_key)
                _merge_result(mapping, case_id, _normalize_result(entry), warnings)
        elif value in (None, ""):
            continue
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            if value:
                _warn(
                    warnings,
                    "execution_count_not_mappable",
                    f"`{status_key}={value:g}` 只有计数、没有用例 ID，相关用例保守按 not_run 处理",
                    RESULTS_REL,
                )
        else:
            _warn(
                warnings,
                "result_collection_not_iterable",
                f"`{status_key}` 既不是 ID 列表也不是计数",
                RESULTS_REL,
            )
    return mapping


def _normalize_result(entry: dict) -> dict:
    normalized = dict(entry)
    normalized["status"] = _normalize_status(entry)
    for key in RESPONSE_STATUS_KEYS:
        if key in normalized:
            normalized[key] = _coerce_status_code(normalized[key])
    return normalized


def _normalize_status(entry: dict) -> str:
    for key in STATUS_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return "not_run"


def _merge_result(
    mapping: dict[str, dict], case_id: str, entry: dict, warnings: list[dict]
) -> None:
    """同 ID 多条结果按最差状态合并：failed > skipped > not_run > passed。"""
    existing = mapping.get(case_id)
    if existing is None:
        mapping[case_id] = entry
        return
    _warn(
        warnings,
        "duplicate_result_id",
        f"{case_id} 存在多条结果（{existing['status']} / {entry['status']}），按最差状态合并",
        RESULTS_REL,
    )
    if _severity(entry["status"]) > _severity(existing["status"]):
        mapping[case_id] = entry


def _severity(status: object) -> int:
    return STATUS_SEVERITY.get(str(status), UNKNOWN_STATUS_SEVERITY)


def _coerce_status_code(value: object) -> object:
    """`"200"` / `200.0` 这类写法归一为 int；其余原样返回交给内核判定。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"\s*\d{1,3}\s*", value):
        return int(value.strip())
    return value


def _load_evidence(
    run_path: Path, case_id: str, result: dict | None, warnings: list[dict]
) -> tuple[dict | None, bool, str | None]:
    """返回 `(证据, 是否损坏, 相对路径)`；只有证据文件存在才算有请求证据。"""
    if not case_id:
        return None, False, None
    rel = f"execution/requests/{case_id}.json"
    path = run_path / "execution" / "requests" / f"{case_id}.json"
    if not path.exists():
        return None, False, None
    raw = _read_json(path, rel)
    if not isinstance(raw, dict):
        _warn(warnings, "evidence_not_object", f"{case_id} 的请求证据不是对象，不能用于晋升", rel)
        return None, True, rel
    return _normalize_evidence(raw, result), False, rel


def _normalize_evidence(raw: dict, result: dict | None) -> dict:
    """把各代 executor 的证据写法统一成 `{"response": {"status", "body"}}`。"""
    evidence = dict(raw)
    nested = raw.get("response") if isinstance(raw.get("response"), dict) else {}

    response: dict = {}
    status = nested.get("status") if "status" in nested else _first_present(raw, RESPONSE_STATUS_KEYS)
    if status is _MISSING and isinstance(result, dict):
        status = _first_present(result, RESPONSE_STATUS_KEYS)
    if status is not _MISSING:
        response["status"] = _coerce_status_code(status)

    if "body" in nested:
        body = nested.get("body")
    else:
        body = _first_present(raw, RESPONSE_BODY_KEYS)
        if body is _MISSING and isinstance(result, dict):
            body = _first_present(result, RESPONSE_BODY_KEYS)
    if body is not _MISSING:
        response["body"] = _maybe_json(body)

    evidence["response"] = response
    return evidence


def _first_present(source: dict, keys: tuple[str, ...]) -> object:
    for key in keys:
        if key in source:
            return source[key]
    return _MISSING


def _maybe_json(value: object) -> object:
    """响应体常被 executor 存成 JSON 字符串，解析失败时保留原文。"""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "{[":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _detect_service(run_path: Path) -> str | None:
    """服务名只是产物可读性信息，读不到就留空，不阻断晋升。"""
    for name in ("interface-catalog.json", "run-context.json"):
        path = run_path / name
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            continue
        if isinstance(payload, dict):
            for key in ("service_name", "service", "target_service"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None


# --- blocker 合并 -----------------------------------------------------------


def _merge_blockers(fresh: list[str], existing: object) -> list[str]:
    """本轮重算的 blocker + 生成期遗留的结构性 blocker（后者一条都不能丢）。"""
    persistent = [
        item
        for item in (existing if isinstance(existing, (list, tuple)) else [])
        if isinstance(item, str) and item.strip() and not _is_reevaluated(item)
    ]
    return _dedupe(list(fresh) + persistent)


def _is_reevaluated(blocker: str) -> bool:
    return blocker in REEVALUATED_BLOCKERS or blocker.startswith(REEVALUATED_PREFIXES)


# --- 摘要（只写形状，不写取值） --------------------------------------------


def _method(case: dict, evidence: dict | None) -> str:
    method = case.get("method") or (evidence or {}).get("method") or ""
    return str(method).upper()


def _request_path(case: dict) -> str:
    """Markdown 只保留 URL path，丢弃 host、query 与 fragment。"""
    raw = str(case.get("path") or case.get("url") or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    return parsed.path or "/"


def _request_summary(case: dict, evidence: dict | None) -> str:
    """一行请求摘要：只有 method、path 与参数**键名**，绝不序列化任何取值或 Header。"""
    request = case.get("request") if isinstance(case.get("request"), dict) else {}
    parts = [f"{_method(case, evidence)} {_request_path(case)}".strip()]
    for key, label in (("query", "query"), ("path_params", "path"), ("body", "body")):
        shape = _shape(request.get(key))
        if shape:
            parts.append(f"{label}={shape}")

    text = " ".join(part for part in parts if part).strip() or "(无请求)"
    if len(text) > SUMMARY_LIMIT:
        text = text[:SUMMARY_LIMIT] + "…"
    return text


def _shape(value: object) -> str:
    """描述参数形状：dict 给键名，list 给长度，标量只说明存在。"""
    if isinstance(value, dict):
        keys = [str(key) for key in value]
        return "[" + ", ".join(keys) + "]" if keys else ""
    if isinstance(value, list):
        return f"[list:{len(value)}]" if value else ""
    if value is None:
        return ""
    return "[scalar]"


def _evidence_digest(
    evidence: dict | None, result: dict | None, rel: str | None, invalid: bool
) -> dict:
    """只写安全元数据：证据路径、HTTP 状态、耗时、是否有错误；不复制响应体与错误原文。"""
    status = _response(evidence).get("status")
    return {
        "evidence_path": rel,
        "request_evidence": bool(evidence),
        "evidence_invalid": invalid,
        "http_status": status if isinstance(status, int) and not isinstance(status, bool) else None,
        "duration_ms": _duration(evidence, result),
        "has_error": _has_error(evidence, result),
    }


def _duration(evidence: dict | None, result: dict | None) -> float | int | None:
    for source in (evidence or {}, result or {}):
        for key in ("duration_ms", "duration"):
            value = source.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value
    return None


def _has_error(evidence: dict | None, result: dict | None) -> bool:
    for source in (result or {}, evidence or {}):
        for key in ERROR_KEYS:
            if source.get(key):
                return True
    return False


# --- 统计 -------------------------------------------------------------------


def _execution_counts(entries: list[dict]) -> dict[str, int]:
    counts = {state: 0 for state in EXECUTION_STATES}
    for entry in entries:
        status = entry["execution_status"]
        counts[status] = counts.get(status, 0) + 1
    return counts


def _blocker_counts(entries: list[dict]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for entry in entries:
        counter.update(entry["regression_blockers"])
    return dict(counter.most_common())


# --- Markdown 渲染 ----------------------------------------------------------


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ").strip() or "-"


def _table(headers: list[str], rows: list[list[object]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["-"] * len(headers)) + "|"]
    lines.extend("| " + " | ".join(_cell(cell) for cell in row) + " |" for row in rows)
    return lines


def _render_regression_md(promoted: list[dict], generated_at: str) -> str:
    lines = [
        "# 已验证回归用例集",
        "",
        f"- 生成时间：{generated_at}",
        f"- 晋升用例数：{len(promoted)}",
        "- 晋升口径：普通 `passed` 进入回归集并保留请求参数取值；代码核对后状态码和响应格式一致的 5xx 同样进入回归集",
        "- 本表列出请求形状；完整取值见 `regression-cases.json`",
        "- 机器可读版本：`regression-cases.json`；完整判定明细：`regression-promotion.json`",
        "",
    ]
    if not promoted:
        lines.append("本轮没有用例达到晋升标准，全部用例见 `backlog-cases.md`。")
        return "\n".join(lines) + "\n"

    lines.extend(
        _table(
            ["用例 ID", "名称", "类别", "方法", "路径", "执行状态", "请求形状"],
            [
                [
                    entry["id"],
                    entry["name"],
                    entry["category"],
                    entry["method"],
                    entry["path"],
                    entry["execution_status"],
                    entry["request_summary"],
                ]
                for entry in promoted
            ],
        )
    )
    lines.append("")
    return "\n".join(lines) + "\n"


def _render_backlog_md(
    backlog: list[dict], entries: list[dict], warnings: list[dict], generated_at: str
) -> str:
    lines = [
        "# 待补回归用例（backlog）",
        "",
        f"- 生成时间：{generated_at}",
        f"- 待补用例数：{len(backlog)} / 总用例数 {len(entries)}",
        "- backlog 含未通过用例（failed / skipped / not_run）",
        "- 本表只列请求形状（参数键名），不展示任何请求取值或 Header",
        "",
    ]
    if backlog:
        lines.extend(
            _table(
                ["用例 ID", "名称", "类别", "请求形状", "执行状态", "HTTP 状态", "Blocker"],
                [
                    [
                        entry["id"],
                        entry["name"],
                        entry["category"],
                        entry["request_summary"],
                        entry["execution_status"],
                        entry["evidence"]["http_status"],
                        "、".join(entry["regression_blockers"]) or "-",
                    ]
                    for entry in backlog
                ],
            )
        )
        counts = _blocker_counts(backlog)
        if counts:
            lines.extend(["", "## Blocker 分布", ""])
            lines.extend(
                _table(["Blocker", "用例数"], [[name, count] for name, count in counts.items()])
            )
    else:
        lines.append("本轮全部用例均已晋升，没有待补用例。")

    if warnings:
        lines.extend(["", "## 输入告警", ""])
        lines.extend(
            _table(
                ["代码", "位置", "说明"],
                [[item["code"], item["path"], item["detail"]] for item in warnings],
            )
        )
    lines.append("")
    return "\n".join(lines) + "\n"


# --- 落盘与 CLI -------------------------------------------------------------


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行后生成回归集与 backlog")
    parser.add_argument("--run-dir", default=".", help="run 目录（含 test-cases.json 与 execution/）")
    args = parser.parse_args(argv)
    try:
        summary = build_promotion_artifacts(args.run_dir)
    except PromotionInputError as exc:
        print(f"晋升输入错误：{exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
