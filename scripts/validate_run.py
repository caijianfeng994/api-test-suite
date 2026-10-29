#!/usr/bin/env python3
"""Validate api-test-suite run artifacts using only Python stdlib."""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any


def _load_sibling_module(name: str) -> Any:
    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise ImportError(f"unable to load sibling module: {name}")
    loaded = module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


try:
    resolve_write_authorization = _load_sibling_module(
        "write_authorization"
    ).resolve_write_authorization
    WRITE_AUTHORIZATION_LOAD_ERROR = ""
except Exception as error:  # 加载失败不静默放行，留给校验阶段报告
    resolve_write_authorization = None
    WRITE_AUTHORIZATION_LOAD_ERROR = type(error).__name__

try:
    _app_usage = _load_sibling_module("app_usage")
    validate_app_usage_evidence = _app_usage.validate_app_usage_evidence
    should_generate_auth = _app_usage.should_generate_auth
    APP_USAGE_LOAD_ERROR = ""
except Exception as error:  # 加载失败不静默放行，留给校验阶段报告
    validate_app_usage_evidence = None
    should_generate_auth = None
    APP_USAGE_LOAD_ERROR = type(error).__name__

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# 授权型跳过的英文表达：必须明确指向写操作/破坏性写的授权，
# 不能匹配 `missing Authorization header` 这类请求头缺失。
WRITE_AUTH_SKIP_PATTERNS = (
    re.compile(r"(?i)write[_\- ]operations?[_\- ]not[_\- ]authorized"),
    re.compile(r"(?i)\bwrite\s+operations?\b[^.\n]{0,40}\bauthoriz"),
    re.compile(r"(?i)\bdestructive\b[^.\n]{0,40}\bauthoriz"),
)

# 授权型跳过的中文精确短语
WRITE_AUTH_SKIP_PHRASES = (
    "写接口未获得",
    "破坏性写操作未授权",
    "破坏性写操作：测试环境未记录显式写授权",
)


COMMON_FULL = [
    "run-context.json",
    "interface-catalog.json",
    "interface-catalog.md",
    "fixtures.json",
    "test-plan.md",
    "test-cases.json",
    "test-cases.md",
    "execution/results.json",
    "api-test-report.md",
]

CONTRACT_ARTIFACTS = [
    "contract-clauses.json",
    "contract-analysis.md",
    "traceability-matrix.md",
]

REGRESSION_ARTIFACTS = [
    "regression-fixtures.json",
    "regression-promotion.json",
    "regression-cases.json",
    "regression-cases.md",
    "backlog-cases.md",
]

PLACEHOLDER_VALUES = {
    "test",
    "placeholder",
    "api-test-placeholder",
    "00000000-0000-0000-0000-000000000000",
}
INLINE_PLACEHOLDER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_.\-]*\}")
ENV_REFERENCE = re.compile(r"\$\{[^{}]+\}")
SENSITIVE_KEYS = re.compile(
    r"authorization|access[_-]?token|token|cookie|password|passwd|secret|phone|mobile|"
    r"idcard|id_card|database_uri|database_url|private[_-]?key",
    re.I,
)
PLAINTEXT_SENSITIVE_TEXT = re.compile(
    r"(?i)\b(?:authorization|access[_-]?token|token|cookie|password|passwd|secret|"
    r"phone|mobile|idcard|id_card|database_uri|database_url|private[_-]?key)\b"
    r"\s*[:=]\s*[\"'`]?(?:bearer\s+)?(?!\$\{)(?!process\.env\b)(?!deno\.env\b)"
    r"(?!os\.environ\b)(?!getenv\s*\()[^\s\"'`,;}\]]+"
)
BEARER_VALUE = re.compile(r"(?i)\bbearer\s+(?!\$\{)[A-Za-z0-9._~+/=-]{6,}")
PHONE_VALUE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
ID_CARD_VALUE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
DATABASE_URI_VALUE = re.compile(r"(?i)\b(?:mysql|postgres(?:ql)?|mongodb(?:\+srv)?|redis)://[^/\s:@]+:[^@\s]+@")

# 敏感 fixture 条目的描述字段：本身不承载取值，只有 `value` 之类才算明文
FIXTURE_METADATA_KEYS = {
    "value_ref",
    "sensitive",
    "source",
    "usage",
    "verified_at",
    "name",
    "ref",
    "required",
    "reason",
    "masked",
    "env",
    "env_var",
}

# 写授权对象：键名含 authorization，但整棵子树是门禁元数据，不是密钥明文
WRITE_AUTHORIZATION_META_KEYS = {
    "authorization",
    "write_authorization",
    "writeauthorization",
}
WRITE_AUTHORIZATION_FIELDS = {
    "allowed",
    "authorized",
    "authorized_at",
    "authorized_by",
    "context_sha256",
    "destructive",
    "environment",
    "legacy_environment_required",
    "reason",
    "scope",
    "source",
}

# fixture_trace 允许为空的策略
FIXTURE_OPTIONAL_POLICIES = {"none_ok", "negative_case"}

# 静态契约来源：不需要在 regression-fixtures.json 里有条目
STATIC_TRACE_SOURCES = (
    "generated:",
    "yapi.",
    "yapi:",
    "proto.",
    "proto:",
    "contract.",
    "contract:",
)

# 请求证据的约定路径与安全 case ID
EVIDENCE_DIR = "execution/requests"
SAFE_CASE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
EVIDENCE_ID_KEYS = ("case_id", "id", "caseId", "case")


def placeholder_paths(value: object, path: str = "") -> list[str]:
    """返回空值、固定占位值和未替换模板所在路径。"""
    matches: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            matches.extend(placeholder_paths(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            matches.extend(placeholder_paths(item, f"{path}[{index}]"))
    elif isinstance(value, str):
        stripped = value.strip()
        without_env = ENV_REFERENCE.sub("", stripped)
        if (
            not stripped
            or stripped.lower() in PLACEHOLDER_VALUES
            or INLINE_PLACEHOLDER.search(without_env)
        ):
            matches.append(path)
    return matches


def all_request_fields(iface: dict) -> list[tuple[str, str]]:
    """返回全部 schema 请求字段；required/optional 标记不参与筛选。"""
    fields: list[tuple[str, str]] = []
    for location, params in (
        ("path_params", iface.get("path_parameters") or []),
        ("query", iface.get("query_parameters") or []),
    ):
        for param in params:
            if isinstance(param, dict) and param.get("name"):
                fields.append((location, str(param["name"])))
    body = iface.get("request_body") or {}
    if isinstance(body, dict) and isinstance(body.get("schema"), dict):
        body = body["schema"]
    if isinstance(body, dict):
        def add_body_fields(schema: dict, prefix: str = "") -> None:
            for name, child in (schema.get("properties") or {}).items():
                path = f"{prefix}.{name}" if prefix else str(name)
                fields.append(("body", path))
                if isinstance(child, dict):
                    add_body_fields(child, path)

        add_body_fields(body)
    return list(dict.fromkeys(fields))


def request_field_exists(container: object, field_path: str) -> bool:
    current = container
    for part in field_path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]
    return True


def sensitive_paths(value: object, path: str = "") -> list[str]:
    """返回 JSON 中明文敏感值路径；`${ENV}` 引用与 `value_ref` 描述对象允许。"""
    return sorted(set(_scan_sensitive(value, path, False)))


def _scan_sensitive(value: object, path: str, sensitive: bool) -> list[str]:
    """递归扫描。敏感键下的容器继续下钻，只有真正的明文标量才算泄漏。"""
    matches: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            key_text = str(key)
            key_lower = key_text.lower()
            # 仅放行形状明确的写授权门禁元数据；Header Authorization 明文仍须拦截。
            if _is_write_authorization_metadata(key_lower, item):
                matches.extend(_scan_sensitive(item, child, False))
                continue
            child_sensitive = bool(SENSITIVE_KEYS.search(key_text)) or (
                sensitive and key_lower not in FIXTURE_METADATA_KEYS
            )
            matches.extend(_scan_sensitive(item, child, child_sensitive))
        return matches
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            matches.extend(_scan_sensitive(item, f"{path}[{index}]", sensitive))
        return matches
    if sensitive and _has_plaintext_value(value):
        # expected.schema 里的 properties.phone.title/type 等只是契约注解，不是密钥明文；
        # 仅当注解字符串本身像手机号/Bearer/连接串时才报。
        if ".expected.schema" in path:
            if _contains_sensitive_literal(str(value)):
                matches.append(path)
        elif (
            path == "blocker_summary"
            or path.startswith("blocker_summary.")
            or ".blocker_summary." in path
        ) and isinstance(value, (int, float)) and not isinstance(value, bool):
            # blocker_summary 的计数（如 sensitive:query.token → 2）不是密钥明文
            pass
        elif _phone_like_key(path):
            # isPhoneLimit / blocker_summary.sensitive:body.isPhoneLimit 等业务键名含 phone，
            # 仅当值本身像手机号时才算泄漏；布尔/计数/占位不算。
            if PHONE_VALUE.search(str(value)):
                matches.append(path)
        else:
            matches.append(path)
    elif isinstance(value, str) and _contains_sensitive_literal(value):
        matches.append(path)
    return matches


def _is_write_authorization_metadata(key: str, value: object) -> bool:
    if key not in WRITE_AUTHORIZATION_META_KEYS or not isinstance(value, dict):
        return False
    return bool(value) and set(map(str, value)).issubset(WRITE_AUTHORIZATION_FIELDS)


def _phone_like_key(path: str) -> bool:
    """路径叶键是否因 phone/mobile 子串被标为敏感（而非 authorization/token 等）。"""
    leaf = path.rsplit(".", 1)[-1]
    if not re.search(r"phone|mobile", leaf, re.I):
        return False
    # 叶键同时匹配其它敏感词时，仍按通用敏感键处理
    other = re.compile(
        r"authorization|access[_-]?token|token|cookie|password|passwd|secret|"
        r"idcard|id_card|database_uri|database_url|private[_-]?key",
        re.I,
    )
    return not other.search(leaf)


def _is_env_reference(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"\$\{[^{}]+\}", value.strip()))


def _has_plaintext_value(value: object) -> bool:
    if value is None or value == "":
        return False
    if _is_env_reference(value):
        return False
    if isinstance(value, str) and re.fullmatch(
        r"(?:process\.env\.[A-Za-z_][A-Za-z0-9_]*|"
        r"os\.environ\[[\"'][A-Za-z_][A-Za-z0-9_]*[\"']\]|"
        r"(?:os\.)?getenv\([\"'][A-Za-z_][A-Za-z0-9_]*[\"']\))",
        value.strip(),
        re.I,
    ):
        return False
    return bool(value) if isinstance(value, (dict, list, tuple, set, str)) else True


def _contains_sensitive_literal(text: str) -> bool:
    without_env = ENV_REFERENCE.sub("", text)
    return any(
        pattern.search(without_env)
        for pattern in (PLAINTEXT_SENSITIVE_TEXT, BEARER_VALUE, PHONE_VALUE, ID_CARD_VALUE, DATABASE_URI_VALUE)
    )


def _json_cases(payload: object) -> list[dict]:
    raw = payload.get("cases") if isinstance(payload, dict) else payload
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _fixture_source_resolves(source: str, endpoint_id: str, fixtures: dict) -> bool:
    """source 必须是静态契约来源，或严格解析到 regression-fixtures.json 的条目。"""
    if source.startswith(STATIC_TRACE_SOURCES):
        return True

    for prefix in ("regression-fixtures.json#", "fixtures.json#"):
        if source.startswith(prefix):
            source = source.removeprefix(prefix)
            break

    if source.startswith("shared."):
        return source in fixtures
    if source.startswith("endpoint_bindings."):
        tail = source.removeprefix("endpoint_bindings.")
        if endpoint_id and f"{endpoint_id}.{tail}" in fixtures:
            return True
        # `endpoint_bindings.<interface>.<field>` 形式：接口段已写在 source 里
        return "." in tail and tail in fixtures
    return False


def _evidence_errors(case_id: str, promotion: dict, run_dir: Path) -> list[str]:
    """独立核验请求证据：路径在 run 内、文件存在、可解析且 case ID 一致。"""
    evidence = promotion.get("evidence") if isinstance(promotion.get("evidence"), dict) else {}
    raw = evidence.get("evidence_path")
    if not evidence.get("request_evidence") or not isinstance(raw, str) or not raw.strip():
        return [f"regression {case_id} request evidence missing"]

    relative = raw.strip().replace("\\", "/")
    if relative.startswith("/") or re.match(r"^[A-Za-z]:", relative) or ".." in relative.split("/"):
        return [f"regression {case_id} evidence path escapes run dir: {raw}"]

    path = run_dir / relative
    try:
        path.resolve().relative_to(run_dir.resolve())
    except (OSError, ValueError):
        return [f"regression {case_id} evidence path escapes run dir: {raw}"]

    errors: list[str] = []
    if SAFE_CASE_ID.match(case_id):
        expected = f"{EVIDENCE_DIR}/{case_id}.json"
        if relative != expected:
            errors.append(f"regression {case_id} evidence path is not {expected}: {raw}")

    if not path.is_file():
        errors.append(f"regression {case_id} evidence file missing: {relative}")
        return errors

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        errors.append(f"regression {case_id} evidence file invalid JSON: {relative}: {error}")
        return errors
    if not isinstance(payload, dict):
        errors.append(f"regression {case_id} evidence is not a JSON object: {relative}")
        return errors

    for key in EVIDENCE_ID_KEYS:
        found = payload.get(key)
        if isinstance(found, str) and found.strip():
            if found.strip() != case_id:
                errors.append(
                    f"regression {case_id} evidence case id mismatch: {found.strip()}"
                )
            break
    return errors


def _sensitive_scan_files(run_dir: Path) -> list[Path]:
    """列出回归产物及 GitLab 白名单内将上传的 Markdown、脚本和配置。"""
    paths = [
        run_dir / "regression-fixtures.json",
        run_dir / "regression-cases.json",
        run_dir / "regression-promotion.json",
        run_dir / "test-cases.json",
        *sorted(run_dir.glob("*.md")),
        run_dir / "playwright.config.ts",
    ]
    tests_dir = run_dir / "tests"
    if tests_dir.is_dir():
        paths.extend(sorted(tests_dir.rglob("*.ts")))
    return list(dict.fromkeys(path for path in paths if path.is_file()))


def validate_regression_artifacts(run_dir: Path) -> list[str]:
    """校验晋升集可重放性，并在上传前阻断明文敏感信息。"""
    errors: list[str] = []
    fixtures_path = run_dir / "regression-fixtures.json"
    promotion_path = run_dir / "regression-promotion.json"
    cases_path = run_dir / "regression-cases.json"
    for relative in REGRESSION_ARTIFACTS:
        path = run_dir / relative
        if not path.is_file():
            errors.append(f"missing: {relative}")
        elif path.stat().st_size == 0:
            errors.append(f"empty: {relative}")

    try:
        fixtures_payload = load_json(fixtures_path) if fixtures_path.is_file() else {}
        promotion_payload = load_json(promotion_path) if promotion_path.is_file() else {}
        cases_payload = load_json(cases_path) if cases_path.is_file() else {}
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return [f"regression artifact JSON invalid: {error}"]

    fixtures = fixtures_payload.get("fixtures") or {}
    fixtures = fixtures if isinstance(fixtures, dict) else {}
    promotion_cases = {
        str(item.get("id") or ""): item
        for item in _json_cases(promotion_payload)
        if item.get("id")
    }
    regression_cases = {
        str(item.get("id") or ""): item
        for item in _json_cases(cases_payload)
        if item.get("id")
    }
    source_cases: dict[str, list[dict]] = {}
    source_path = run_dir / "test-cases.json"
    source_available = source_path.is_file()
    if source_available:
        try:
            for item in _json_cases(load_json(source_path)):
                source_cases.setdefault(str(item.get("id") or ""), []).append(item)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            errors.append(f"invalid JSON: test-cases.json: {error}")
            source_available = False

    promoted_ids = sorted(
        {case_id for case_id, item in promotion_cases.items() if item.get("regression_ready") is True}
        | set(regression_cases)
    )

    for case_id in promoted_ids:
        promotion = promotion_cases.get(case_id)
        case = regression_cases.get(case_id)
        if promotion is None:
            errors.append(f"regression {case_id} missing from regression-promotion.json")
        elif promotion.get("regression_ready") is not True or promotion.get("regression_blockers"):
            errors.append(f"regression {case_id} is not regression_ready")
        if case is None:
            errors.append(f"regression {case_id} missing from regression-cases.json")

        statuses = {
            str((item or {}).get("execution_status") or "")
            for item in (promotion, case)
            if item is not None
        }
        if statuses != {"passed"}:
            errors.append(f"regression {case_id} execution status is not passed")

        if promotion is not None:
            errors.extend(_evidence_errors(case_id, promotion, run_dir))

        if source_available:
            found = source_cases.get(case_id) or []
            if not found:
                errors.append(f"regression {case_id} missing from test-cases.json")
            elif len(found) > 1:
                errors.append(f"regression {case_id} duplicated in test-cases.json")
            else:
                stored = found[0]
                if (
                    stored.get("execution_status") != "passed"
                    or stored.get("regression_ready") is not True
                    or stored.get("regression_blockers")
                ):
                    errors.append(f"regression {case_id} status differs in test-cases.json")
        else:
            errors.append("missing test-cases.json; cannot reconcile promoted cases")

        if case is None:
            continue
        if case.get("regression_ready") is not True or case.get("regression_blockers"):
            errors.append(f"regression {case_id} is not regression_ready")

        for path in placeholder_paths(case.get("request")):
            errors.append(f"regression {case_id} contains placeholder: {path}")
        for path in sensitive_paths(case.get("request")):
            errors.append(f"regression {case_id} contains sensitive value: {path}")

        trace = case.get("fixture_trace")
        policy = str(case.get("fixture_policy") or "").strip()
        if isinstance(trace, list) and not trace:
            if policy not in FIXTURE_OPTIONAL_POLICIES:
                errors.append(f"regression {case_id} fixture_trace missing")
            continue
        if not isinstance(trace, list):
            errors.append(f"regression {case_id} fixture_trace missing")
            continue
        for index, item in enumerate(trace):
            if not isinstance(item, dict):
                errors.append(f"regression {case_id} fixture_trace[{index}] invalid")
                continue
            source = str(item.get("source") or "").strip()
            if not source:
                errors.append(f"regression {case_id} fixture_trace source missing: {index}")
            elif not _fixture_source_resolves(
                source, str(case.get("endpoint_id") or ""), fixtures
            ):
                errors.append(
                    f"regression {case_id} fixture_trace ref unresolved: "
                    f"{item.get('ref') or '<missing-ref>'} ({source})"
                )

    for path in _sensitive_scan_files(run_dir):
        relative = str(path.relative_to(run_dir))
        try:
            if path.suffix == ".json":
                payload = json.loads(path.read_text(encoding="utf-8"))
                matches = sensitive_paths(payload)
            else:
                matches = [
                    f"line {number}"
                    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
                    if _contains_sensitive_literal(line)
                ]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            errors.append(f"{relative} sensitive scan failed: {error}")
            continue
        for match in matches:
            errors.append(f"{relative} contains sensitive value: {match}")
    return list(dict.fromkeys(errors))

CONTRACT_MODE = [
    "run-context.json",
    "interface-catalog.json",
    "interface-catalog.md",
    *CONTRACT_ARTIFACTS,
    "test-plan.md",
    "test-cases.json",
    "test-cases.md",
]


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as file:
        value = json.load(file)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def required_artifacts(context: dict) -> list[str]:
    kind = context.get("test_kind")
    mode = context.get("run_mode")

    if mode == "contract":
        return list(CONTRACT_MODE)

    if kind == "functional":
        if mode == "plan":
            return [
                "run-context.json",
                "interface-catalog.json",
                "interface-catalog.md",
                *CONTRACT_ARTIFACTS,
                "fixtures.json",
                "test-plan.md",
            ]
        if mode == "generate":
            return [
                "run-context.json",
                "interface-catalog.json",
                "interface-catalog.md",
                *CONTRACT_ARTIFACTS,
                "fixtures.json",
                "test-plan.md",
                "test-cases.json",
                "test-cases.md",
            ]
        required = list(COMMON_FULL)
        if mode == "full":
            required.extend(REGRESSION_ARTIFACTS)
        return required

    if kind == "refactor":
        if mode == "baseline":
            return [*COMMON_FULL, "baseline-manifest.json", *REGRESSION_ARTIFACTS]
        if mode == "replay":
            return [
                "run-context.json",
                "test-cases.json",
                "test-cases.md",
                "execution/results.json",
                "compare/comparison.json",
                "api-test-report.md",
                *REGRESSION_ARTIFACTS,
            ]

    if kind == "migration":
        if mode == "plan":
            return [
                "run-context.json",
                "interface-catalog.json",
                "interface-catalog.md",
                *CONTRACT_ARTIFACTS,
                "fixtures.json",
                "test-plan.md",
            ]
        if mode == "generate":
            return [
                "run-context.json",
                "interface-catalog.json",
                "interface-catalog.md",
                *CONTRACT_ARTIFACTS,
                "fixtures.json",
                "test-plan.md",
                "test-cases.json",
                "test-cases.md",
            ]
        required = [*COMMON_FULL, "compare/comparison.json"]
        if mode in {"full", "baseline", "replay"}:
            required.extend(REGRESSION_ARTIFACTS)
        return required

    raise ValueError(f"unsupported test_kind/run_mode: {kind}/{mode}")


def requires_scripts(context: dict) -> bool:
    mode = context.get("run_mode")
    if mode in {"plan", "contract"}:
        return False
    if mode == "generate":
        return True
    if mode == "replay":
        return False
    return mode in {"full", "baseline", "execute"} or mode is None


def requires_gitlab_upload(context: dict, run_dir: Path) -> bool:
    mode = context.get("run_mode")
    stages = context.get("stages") or {}
    if stages.get("gitlab_upload") == "not_applicable":
        return False
    if mode == "plan" and not (run_dir / "test-cases.md").is_file():
        return False
    return (run_dir / "test-cases.md").is_file() or mode in {
        "generate",
        "full",
        "contract",
        "baseline",
        "replay",
        "execute",
    }


def requires_contract(context: dict) -> bool:
    mode = context.get("run_mode")
    if mode in {"execute", "replay"}:
        return False
    stages = context.get("stages") or {}
    if stages.get("contract") == "not_applicable":
        return False
    # contract / plan / generate / full / baseline 默认需要契约产物
    return True


def _safe_case_id(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text if text else ""


def _is_authorization_skip_reason(text: object) -> bool:
    """识别授权型跳过；不把参数缺失、依赖缺失、请求头缺失等误判为授权问题。"""
    if not isinstance(text, str):
        return False
    reason = text.strip()
    if not reason:
        return False
    if any(phrase in reason for phrase in WRITE_AUTH_SKIP_PHRASES):
        return True
    return any(pattern.search(reason) for pattern in WRITE_AUTH_SKIP_PATTERNS)


def _has_binding_blocker(case: dict) -> bool:
    blockers = case.get("regression_blockers")
    if isinstance(blockers, list):
        for item in blockers:
            if str(item).startswith("required_binding_missing:"):
                return True
    skip_reason = case.get("skip_reason")
    if isinstance(skip_reason, str) and "required_binding_missing:" in skip_reason:
        return True
    return False


def _is_destructive_write_case(case: dict) -> bool:
    """显式 `destructive` 布尔值优先；缺字段的旧产物才回退到 category。"""
    auth_meta = case.get("write_authorization")
    if isinstance(auth_meta, dict) and isinstance(auth_meta.get("destructive"), bool):
        return auth_meta["destructive"]
    return str(case.get("category") or "").strip().lower() == "side_effect"


def _result_skip_text(result: dict) -> str:
    for key in ("reason", "skip_reason"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _missing_binding_fields(case: dict) -> list[str]:
    fields: list[str] = []
    blockers = case.get("regression_blockers")
    if isinstance(blockers, list):
        for item in blockers:
            text = str(item)
            if text.startswith("required_binding_missing:"):
                fields.append(text.split(":", 1)[1].strip())
    skip_reason = case.get("skip_reason")
    if isinstance(skip_reason, str) and "缺少真实绑定数据" in skip_reason:
        # e.g. 缺少真实绑定数据：body.category、body.workerName
        tail = skip_reason.split("：", 1)[-1] if "：" in skip_reason else skip_reason
        for part in re.split(r"[、,，\s]+", tail):
            part = part.strip().rstrip("。.")
            if part.startswith(("body.", "query.", "path.")):
                fields.append(part)
    # dedupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for field in fields:
        if field and field not in seen:
            seen.add(field)
            out.append(field)
    return out


_TOOL_CREATION_ACTIONS = {
    "create_api",
    "fixture_script",
    "ops_ass_create",
    "admin_create",
}
_WEB_CLI_DOC_ONLY_ACTIONS = {
    "shadow_path_lookup",
    "menu_index_lookup",
    "skill_lookup",
    "doc_lookup",
}
_WEB_CLI_DOC_ONLY_PHRASES = (
    "菜单索引",
    "未索引",
    "shadow-path 无",
    "没有对应菜单",
    "无法打开后台",
    "skill 文档",
    "菜单文档",
)


def _attempt_blob(attempt: dict) -> str:
    return json.dumps(attempt, ensure_ascii=False)


def _tool_attempt_executed(attempt: dict, field: str) -> tuple[bool, str]:
    """现有数据取不到时，造数步必须实际调用创建 API、脚本或后台创建动作。"""
    action = str(attempt.get("action") or "").strip().lower()
    result = str(attempt.get("result") or attempt.get("detail") or "").strip()
    if action not in _TOOL_CREATION_ACTIONS:
        return False, "tool 未实际造数；catalog 扫描、查询接口和无专用脚本声明均不能代替创建"
    target = str(
        attempt.get("target")
        or attempt.get("endpoint")
        or attempt.get("script")
        or attempt.get("opened_url")
        or ""
    ).strip()
    if not target:
        return False, "tool 未记录造数目标（创建接口、脚本或后台页面）"
    status = str(attempt.get("status") or "").strip().lower()
    if status not in {"failed", "blocked"}:
        return False, "pending 字段的造数尝试须记录 failed/blocked；创建成功应回填而非继续 pending"
    if len(result) < 8:
        return False, "tool 未记录实际造数结果"
    return True, ""


def _db_attempt_executed(attempt: dict, field: str) -> tuple[bool, str]:
    """查库步必须留下针对该字段的只读 SELECT，不能只用「已查相关表」。"""
    sql = str(attempt.get("sql") or attempt.get("query") or "").strip()
    if not re.search(r"\bselect\b", sql, re.IGNORECASE):
        return False, "db 未记录针对该字段的 SELECT"
    leaf = field.split(".")[-1]
    if leaf.lower() not in _attempt_blob(attempt).lower():
        return False, f"db SQL/结果未包含字段 {leaf}"
    return True, ""


def _web_cli_evidence_file(run_dir: Path, evidence: str) -> tuple[bool, str]:
    rel = evidence.replace("\\", "/").lstrip("./")
    if rel.startswith("/"):
        return False, "web_cli evidence 必须是 run_dir 内相对路径"
    if not rel.startswith("execution/web-cli/"):
        return False, "web_cli evidence 必须位于 execution/web-cli/"
    path = (run_dir / rel).resolve()
    root = (run_dir / "execution" / "web-cli").resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return False, "web_cli evidence 超出 execution/web-cli/"
    if not path.is_file():
        return False, f"缺少后台取数证据文件 {rel}"
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text.strip()) < 20 or "http" not in text.lower():
        return False, "web_cli 证据文件没有打开页面或抓到的后台接口"
    return True, ""


def _web_cli_attempt_executed(attempt: dict, run_dir: Path) -> tuple[bool, str]:
    """后台取数必须真正打开 Shadow / onion-backstage 或抓到后台接口。菜单文档检索不算。"""
    action = str(attempt.get("action") or "").strip().lower()
    blob = _attempt_blob(attempt)
    if action in _WEB_CLI_DOC_ONLY_ACTIONS or any(phrase in blob for phrase in _WEB_CLI_DOC_ONLY_PHRASES):
        return False, "web_cli 只检索了 shadow-path 菜单文档，没有打开后台"
    evidence = str(attempt.get("evidence") or attempt.get("artifact") or "").strip()
    if not evidence:
        return False, "web_cli 缺少 execution/web-cli/ 证据文件"
    ok, reason = _web_cli_evidence_file(run_dir, evidence)
    if not ok:
        return False, reason
    opened = str(attempt.get("opened_url") or attempt.get("url") or "")
    captured = str(attempt.get("captured_api") or "")
    if not opened.startswith("http") and not captured.startswith("http"):
        return False, "web_cli 未记录 opened_url 或 captured_api"
    return True, ""


def _attempts_cover_field(attempts: list, field: str, run_dir: Path) -> tuple[bool, str]:
    tool_ok = db_ok = web_ok = False
    reasons: list[str] = []
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        step = str(attempt.get("step") or "").strip().lower()
        if step == "tool" and not tool_ok:
            tool_ok, reason = _tool_attempt_executed(attempt, field)
            if not tool_ok:
                reasons.append(reason)
        elif step in {"db", "database"} and not db_ok:
            db_ok, reason = _db_attempt_executed(attempt, field)
            if not db_ok:
                reasons.append(reason)
        elif step == "web_cli" and not web_ok:
            web_ok, reason = _web_cli_attempt_executed(attempt, run_dir)
            if not web_ok:
                reasons.append(reason)
    if tool_ok and db_ok and web_ok:
        return True, ""
    missing = []
    if not tool_ok:
        missing.append("tool")
    if not db_ok:
        missing.append("db")
    if not web_ok:
        missing.append("web_cli")
    detail = "；".join(dict.fromkeys(reasons))
    return False, f"三步证据不完整（{', '.join(missing)}）" + (f"：{detail}" if detail else "")


def _fixture_attempts_cover_field(
    fixtures_payload: dict,
    endpoint_id: str,
    field: str,
    run_dir: Path,
) -> tuple[bool, str]:
    """缺口字段必须有可复核的造数、查库 SQL、后台打开证据。步骤名本身不算证据。"""
    pending = fixtures_payload.get("pending_requirements") or []
    if isinstance(pending, list):
        for item in pending:
            if not isinstance(item, dict):
                continue
            item_field = str(item.get("field") or item.get("name") or "").strip()
            item_ep = str(item.get("endpoint_id") or item.get("endpoint") or "").strip()
            if item_field and item_field != field and not field.endswith(item_field):
                continue
            if item_ep and endpoint_id and item_ep != endpoint_id:
                continue
            attempts = item.get("attempts") or item.get("steps") or []
            if isinstance(attempts, list) and attempts:
                return _attempts_cover_field(attempts, field, run_dir)
            return False, "pending_requirements 没有 attempts"
    steps = fixtures_payload.get("steps") or []
    if isinstance(steps, list):
        matched = [
            step
            for step in steps
            if isinstance(step, dict) and field.split(".")[-1] in _attempt_blob(step)
        ]
        if matched:
            return _attempts_cover_field(matched, field, run_dir)
    return False, "fixtures.json 没有该字段的三步证据"


def validate_fixture_partial_binding_leak(run_dir: Path, context: dict) -> list[str]:
    """缺绑定必须有 pending 和三步证据。contract / execute 模式不豁免。"""
    errors: list[str] = []
    fixtures_path = run_dir / "fixtures.json"
    cases_path = run_dir / "test-cases.json"
    if not cases_path.is_file():
        return errors
    if not fixtures_path.is_file():
        try:
            cases_payload = json.loads(cases_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            return [f"fixture partial-binding check failed: {error}"]
        cases = cases_payload.get("cases") if isinstance(cases_payload, dict) else cases_payload
        if isinstance(cases, list) and any(_missing_binding_fields(case) for case in cases if isinstance(case, dict)):
            errors.append(
                "fixture leak: 用例存在 required_binding_missing，但缺少 fixtures.json，"
                "不得改 run_mode=contract 绕过造数"
            )
        return errors
    try:
        fixtures_payload = json.loads(fixtures_path.read_text(encoding="utf-8"))
        cases_payload = json.loads(cases_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return [f"fixture partial-binding check failed: {error}"]
    cases = cases_payload.get("cases") if isinstance(cases_payload, dict) else cases_payload
    if not isinstance(cases, list) or not isinstance(fixtures_payload, dict):
        return errors
    for case in cases:
        if not isinstance(case, dict):
            continue
        if case.get("executable") is not False and not _has_binding_blocker(case):
            continue
        fields = _missing_binding_fields(case)
        if not fields:
            continue
        endpoint_id = str(case.get("endpoint_id") or "")
        case_id = str(case.get("id") or "")
        for field in fields:
            covered, reason = _fixture_attempts_cover_field(
                fixtures_payload, endpoint_id, field, run_dir
            )
            if not covered:
                errors.append(
                    f"fixture leak: {case_id} required_binding_missing:{field} "
                    f"without executable three-step evidence ({reason})"
                )
    return errors


def validate_code_unused_sources(run_dir: Path, context: dict) -> list[str]:
    """code_unused 必须带 code:<file>#<symbol>，禁止用它绕过缺绑定。"""
    if context.get("run_mode") == "contract":
        return []
    fixtures_path = run_dir / "fixtures.json"
    if not fixtures_path.is_file():
        return []
    try:
        payload = json.loads(fixtures_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return [f"code unused check failed: {error}"]
    bindings = payload.get("endpoint_bindings") if isinstance(payload, dict) else None
    if not isinstance(bindings, dict):
        return []
    errors: list[str] = []
    for endpoint_id, entry in bindings.items():
        if not isinstance(entry, dict):
            continue
        usage = entry.get("usage") or {}
        sources = entry.get("sources") or {}
        if not isinstance(usage, dict):
            continue
        for field, declared in usage.items():
            if str(declared or "").strip().lower() != "code_unused":
                continue
            source = str((sources or {}).get(field) or "")
            if not source.startswith("code:") or len(source) < 13:
                errors.append(f"code unused: {endpoint_id}.{field} 缺少 code:<file>#<symbol> 证据")
    return errors


def _offline_scan_key(method: str, path: str) -> str:
    return f"{(method or '').upper()} {(path or '').rstrip('/')}"


def _offline_scan_lookup(by_path: dict, method: str, path: str) -> dict | None:
    """匹配 scan.by_path：允许 key 为 METHOD path，或仅 path。"""
    if not isinstance(by_path, dict):
        return None
    full = _offline_scan_key(method, path)
    raw_path = (path or "").rstrip("/")
    for key, value in by_path.items():
        key_s = str(key).strip()
        if key_s == full or key_s.rstrip("/") == raw_path:
            return value if isinstance(value, dict) else {"status": str(value)}
        # METHOD 与 path 之间多空格 / path 前缀差异：后缀匹配
        if key_s.upper().startswith((method or "").upper() + " ") and key_s.rstrip("/").endswith(
            raw_path
        ):
            return value if isinstance(value, dict) else {"status": str(value)}
    return None


_UNIMPLEMENTED_RE = re.compile(r"not implemented|unimplemented", re.IGNORECASE)


def _normalize_http_path(path: str) -> str:
    text = str(path or "").strip()
    if "://" in text:
        text = text.split("://", 1)[1]
        text = text.split("/", 1)[1] if "/" in text else text
        text = "/" + text
    text = text.split("?", 1)[0].rstrip("/")
    return text or "/"


def _result_is_unimplemented(result: dict, request_payload: dict | None) -> bool:
    if result.get("response_status") == 501:
        return True
    parts = [str(result.get("error") or "")]
    if isinstance(request_payload, dict):
        response = request_payload.get("response")
        if response is not None:
            parts.append(json.dumps(response, ensure_ascii=False))
    return bool(_UNIMPLEMENTED_RE.search("\n".join(parts)))


def _traffic_window_error(payload: dict) -> str:
    if payload.get("window_days") != 30:
        return "window_days 必须为 30"
    try:
        start = datetime.fromisoformat(str(payload.get("start") or ""))
        end = datetime.fromisoformat(str(payload.get("end") or ""))
    except ValueError:
        return "start/end 不是合法时间"
    seconds = (end - start).total_seconds()
    if seconds < 29 * 86400 or seconds > 31 * 86400:
        return "日志时间窗不是近 30 天"
    return ""


def _catalog_code_response(iface: dict) -> tuple[bool, str, set[str]]:
    """返回 (has_code_response, root_type, top_level_and_item_field_names)。"""
    root_type = str(iface.get("response_root_type") or "").strip()
    names: set[str] = set()
    has_fields = False
    for resp in iface.get("responses") or []:
        if not isinstance(resp, dict):
            continue
        schema = resp.get("schema")
        if not isinstance(schema, dict):
            continue
        if not root_type:
            root_type = str(schema.get("root_type") or "").strip()
        fields = schema.get("fields")
        if isinstance(fields, list) and fields:
            has_fields = True
            for field in fields:
                if not isinstance(field, dict):
                    continue
                name = str(field.get("name") or "")
                if not name:
                    continue
                if "[]." in name:
                    parent, child = name.split("[].", 1)
                    names.add(parent.strip())
                    names.add(child.split(".", 1)[0].split("[", 1)[0].strip())
                elif "." not in name and "[" not in name:
                    names.add(name)
    return (has_fields or bool(root_type), root_type, {n for n in names if n})


def _saved_response_status(run_dir: Path, case_id: str) -> int | None:
    path = run_dir / "execution" / "requests" / f"{case_id}.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    status = payload.get("response_status")
    if isinstance(status, int):
        return status
    if isinstance(status, str) and status.isdigit():
        return int(status)
    return None


_REAL_CODE_SYMBOL = re.compile(r"\.go:\d+(?:-\d+)?\s+[A-Za-z_]\w*$")
_GUESSED_NEGATIVE_SOURCES = {"contract_undeclared_error_http"}
_NEGATIVE_ASSERTION_CATEGORIES = {"validation", "error"}


def is_real_code_symbol(symbol: object) -> bool:
    """代码核对必须落到带行号的返回函数，路由摘要不算证据。"""
    text = str(symbol or "").strip()
    if not text or "GinHandler" in text:
        return False
    return _REAL_CODE_SYMBOL.search(text) is not None


def _exact_expected_statuses(expected: dict) -> list[int]:
    http = expected.get("http") if isinstance(expected.get("http"), dict) else {}
    raw = http.get("status", expected.get("status"))
    if isinstance(raw, int):
        return [raw]
    if isinstance(raw, list):
        statuses: list[int] = []
        for item in raw:
            if isinstance(item, int):
                statuses.append(item)
            elif isinstance(item, str) and item.isdigit():
                statuses.append(int(item))
        return statuses
    return []


def _forbids_5xx(expected: dict) -> bool:
    http = expected.get("http") if isinstance(expected.get("http"), dict) else {}
    classes = {str(expected.get("status_class") or ""), str(http.get("status_class") or "")}
    if "2xx_or_4xx" in classes:
        return True
    not_status = list(expected.get("not_status") or []) + list(http.get("not_status") or [])
    for item in not_status:
        if isinstance(item, int) and item >= 500:
            return True
        if isinstance(item, str) and item.isdigit() and int(item) >= 500:
            return True
    return False


def _result_rows(results_payload: dict) -> list:
    results = results_payload.get("results")
    if isinstance(results, list):
        return results
    cases = results_payload.get("cases")
    return cases if isinstance(cases, list) else []


def validate_negative_code_return(run_dir: Path, context: dict) -> list[str]:
    """validation/error 只能断言代码返回点，禁止用 2xx_or_4xx 或禁止 5xx 猜测。"""
    cases_path = run_dir / "test-cases.json"
    if not cases_path.is_file():
        return []
    try:
        cases_payload = json.loads(cases_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return [f"negative code return check failed: {error}"]
    cases = cases_payload.get("cases") if isinstance(cases_payload, dict) else None
    if not isinstance(cases, list):
        return []
    completed = context.get("status") == "completed"
    errors: list[str] = []
    for case in cases:
        if not isinstance(case, dict):
            continue
        if str(case.get("category") or "") not in _NEGATIVE_ASSERTION_CATEGORIES:
            continue
        case_id = str(case.get("id") or "")
        expected = case.get("expected") if isinstance(case.get("expected"), dict) else {}
        calibration = expected.get("calibration") if isinstance(expected.get("calibration"), dict) else {}
        guessed = _forbids_5xx(expected) or calibration.get("source") in _GUESSED_NEGATIVE_SOURCES
        if guessed:
            errors.append(
                f"negative code return: {case_id} 禁止用 2xx_or_4xx 或 not_status=5xx 猜测断言，"
                "必须按代码返回点写入精确 status"
            )
            continue
        statuses = _exact_expected_statuses(expected)
        pending = (
            expected.get("assertion_policy") == "code_return_required"
            and calibration.get("source") == "pending_code_return"
            and not statuses
        )
        executed = (run_dir / "execution" / "requests" / f"{case_id}.json").is_file()
        if pending:
            if case.get("executable") is not False and (completed or executed):
                errors.append(
                    f"negative code return: {case_id} 已执行或运行已完成，仍未按代码返回点闭合断言"
                )
            continue
        if not statuses:
            errors.append(f"negative code return: {case_id} 缺少代码返回的精确 status")
            continue
        evidence = expected.get("code_evidence") if isinstance(expected.get("code_evidence"), dict) else {}
        symbols = evidence.get("symbols")
        if not isinstance(symbols, list) or not symbols or not all(is_real_code_symbol(item) for item in symbols):
            errors.append(
                f"negative code return: {case_id} 精确状态缺少落到返回函数的代码符号"
            )
            continue
        code_status = evidence.get("code_status")
        if code_status not in statuses:
            errors.append(
                f"negative code return: {case_id} code_status 必须等于 expected.status"
            )
        response_format = evidence.get("response_format", expected.get("response_format"))
        if not isinstance(response_format, dict) or not response_format:
            errors.append(f"negative code return: {case_id} 缺少代码返回的 response_format")
    return errors


def validate_code_failure_calibration(run_dir: Path, context: dict) -> list[str]:
    """5xx 失败须用代码返回点核对；两者都一致时不得继续记 failed。contract 模式不豁免。"""
    del context
    results_path = run_dir / "execution" / "results.json"
    if not results_path.is_file():
        return []
    try:
        results_payload = json.loads(results_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return [f"code failure calibration check failed: {error}"]
    if not isinstance(results_payload, dict):
        return []
    results = _result_rows(results_payload)
    if not results:
        return []
    failed_5xx: list[tuple[str, int]] = []
    statuses = {
        str(item.get("id") or item.get("case_id") or ""): item.get("status")
        for item in results
        if isinstance(item, dict)
    }
    for item in results:
        if not isinstance(item, dict) or item.get("status") != "failed":
            continue
        case_id = str(item.get("id") or item.get("case_id") or "")
        status = _saved_response_status(run_dir, case_id)
        if case_id and status is not None and status >= 500:
            failed_5xx.append((case_id, status))
    evidence_path = run_dir / "code-validation-evidence.json"
    if not failed_5xx and not evidence_path.is_file():
        return []
    if failed_5xx and not evidence_path.is_file():
        ids = ", ".join(case_id for case_id, _status in failed_5xx[:8])
        return [
            "code failure calibration: 5xx 失败须按代码核对状态码和响应格式，"
            f"缺少 code-validation-evidence.json（{ids}）"
        ]
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return [f"code failure calibration check failed: {error}"]
    cases = evidence.get("cases") if isinstance(evidence, dict) else None
    by_id = {}
    if isinstance(cases, list):
        for case in cases:
            if isinstance(case, dict) and case.get("case_id"):
                by_id[str(case.get("case_id"))] = case
    current_ids: set[str] | None = None
    cases_path = run_dir / "test-cases.json"
    if cases_path.is_file():
        try:
            cases_payload = json.loads(cases_path.read_text(encoding="utf-8"))
            listed = cases_payload.get("cases") if isinstance(cases_payload, dict) else None
            if isinstance(listed, list):
                current_ids = {
                    str(item.get("id"))
                    for item in listed
                    if isinstance(item, dict) and item.get("id")
                }
        except (OSError, ValueError, json.JSONDecodeError):
            current_ids = None
    errors: list[str] = []
    for case_id, actual in failed_5xx:
        case = by_id.get(case_id)
        if not isinstance(case, dict):
            errors.append(f"code failure calibration: {case_id} 缺少代码核对证据")
            continue
        symbols = case.get("symbols")
        if not isinstance(symbols, list) or not symbols or not all(is_real_code_symbol(item) for item in symbols):
            errors.append(
                f"code failure calibration: {case_id} 符号未落到返回函数，不得用路由摘要保持 failed"
            )
            continue
        if case.get("status_match") is None or case.get("format_match") is None:
            errors.append(
                f"code failure calibration: {case_id} 须写明 status_match 与 format_match"
            )
            continue
        code_status = case.get("code_status", case.get("expected_status"))
        if case.get("actual_status") != actual:
            errors.append(
                f"code failure calibration: {case_id} actual_status 须等于实测 {actual}"
            )
            continue
        status_match = case.get("status_match") is True and code_status == actual
        format_match = case.get("format_match") is True and isinstance(case.get("response_format"), dict)
        if status_match and format_match and statuses.get(case_id) == "failed":
            errors.append(
                f"code failure calibration: {case_id} 状态码和响应格式都与代码一致，应记 passed"
            )
    for case_id, case in by_id.items():
        if current_ids is not None and case_id not in current_ids:
            continue
        symbols = case.get("symbols")
        if not isinstance(symbols, list) or not all(is_real_code_symbol(item) for item in symbols):
            errors.append(
                f"code failure calibration: {case_id} 证据符号未落到返回函数"
            )
        if case.get("status_match") is True and case.get("format_match") is True:
            if statuses.get(case_id) not in {None, "passed"}:
                errors.append(
                    f"code failure calibration: {case_id} 状态码和响应格式都与代码一致，应记 passed"
                )
    return errors


def validate_code_response_schema(run_dir: Path, context: dict) -> list[str]:
    """catalog 已有代码出参时，成功路径 schema 必须代码校准。"""
    if context.get("run_mode") == "contract":
        return []
    catalog_path = run_dir / "interface-catalog.json"
    cases_path = run_dir / "test-cases.json"
    if not catalog_path.is_file() or not cases_path.is_file():
        return []
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        cases_payload = json.loads(cases_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return [f"code response schema check failed: {error}"]
    interfaces = catalog.get("interfaces") if isinstance(catalog, dict) else None
    cases = cases_payload.get("cases") if isinstance(cases_payload, dict) else None
    if not isinstance(interfaces, list) or not isinstance(cases, list):
        return []
    by_id: dict[str, dict] = {}
    for iface in interfaces:
        if isinstance(iface, dict) and iface.get("id"):
            by_id[str(iface["id"])] = iface
    errors: list[str] = []
    for case in cases:
        if not isinstance(case, dict):
            continue
        if case.get("category") not in {"functional", "list", "side_effect"}:
            continue
        if case.get("executable") is False:
            continue
        endpoint_id = str(case.get("endpoint_id") or "")
        iface = by_id.get(endpoint_id)
        if not iface:
            continue
        has_code, root_type, code_names = _catalog_code_response(iface)
        if not has_code:
            continue
        schema = ((case.get("expected") or {}) if isinstance(case.get("expected"), dict) else {}).get("schema")
        if not isinstance(schema, dict):
            errors.append(f"code response schema: {case.get('id')} 缺少 expected.schema，catalog 已有代码出参")
            continue
        source = str(schema.get("source") or "")
        if source not in {"code_wiki", "code_calibrated", "code_proto"}:
            errors.append(
                f"code response schema: {case.get('id')} source={source or 'missing'}，"
                "catalog 已有代码出参时必须为 code_wiki/code_calibrated/code_proto"
            )
        if root_type.startswith("[]") or root_type.lower().startswith("repeated "):
            if schema.get("type") != "array":
                errors.append(
                    f"code response schema: {case.get('id')} 代码出参为 {root_type}，"
                    f"schema.type 必须是 array，当前为 {schema.get('type')}"
                )
        # 字段名大小写：YApi VideoId vs 代码 videoId
        required = [str(x) for x in (schema.get("required_fields") or []) if x]
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        declared = set(required) | set(str(k) for k in props.keys())
        for prop in props.values():
            if not isinstance(prop, dict):
                continue
            items = prop.get("items")
            if isinstance(items, dict):
                declared.update(str(x) for x in (items.get("required") or []) if x)
                item_props = items.get("properties")
                if isinstance(item_props, dict):
                    declared.update(str(k) for k in item_props.keys())
        lower_code = {name.lower(): name for name in code_names}
        for name in declared:
            code_name = lower_code.get(name.lower())
            if code_name and code_name != name:
                errors.append(
                    f"code response schema: {case.get('id')} 字段 {name} 与代码 {code_name} 大小写不一致，"
                    "须以代码为准"
                )
    return errors


def _normalize_api_path(path: str) -> str:
    text = str(path or "").strip()
    if "://" in text:
        text = text.split("://", 1)[1]
        text = "/" + text.split("/", 1)[1] if "/" in text else text
    return text.split("?", 1)[0].rstrip("/") or "/"


def _id_source(case: dict, bindings: dict) -> str:
    """主键来源。fixture_trace 里的 endpoint_bindings.<字段> 要落到绑定上的 api:/db: 来源。"""
    endpoint_id = str(case.get("endpoint_id") or "")
    item = bindings.get(endpoint_id) if isinstance(bindings, dict) else None
    sources = item.get("sources") if isinstance(item, dict) else None

    def resolve(source: str) -> str:
        if source.startswith("endpoint_bindings.") and isinstance(sources, dict):
            field = source.removeprefix("endpoint_bindings.")
            bound = str(sources.get(field) or "").strip()
            if bound:
                return bound
        return source

    for trace in case.get("fixture_trace") or []:
        if not isinstance(trace, dict):
            continue
        ref = str(trace.get("ref") or "")
        leaf = ref.split(".")[-1]
        if leaf == "id":
            return resolve(str(trace.get("source") or ""))
    if isinstance(sources, dict) and sources.get("id"):
        return str(sources.get("id"))
    return ""


def _case_sends_resource_id(case: dict) -> bool:
    request = case.get("request") if isinstance(case.get("request"), dict) else {}
    body = request.get("body")
    query = request.get("query")
    if isinstance(body, dict) and body.get("id") not in (None, ""):
        return True
    if isinstance(query, dict) and query.get("id") not in (None, ""):
        return True
    return "{id}" in str(case.get("path") or "")


def _db_source_names_column(source: str) -> bool:
    # db:<service>.<table>.<column>
    body = source.split(":", 1)[1] if ":" in source else ""
    parts = [part for part in body.split(".") if part]
    return len(parts) >= 3


def _seed_attempt_ok(attempts: list, step: str) -> bool:
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        if str(attempt.get("step") or "").strip().lower() != step:
            continue
        status = str(attempt.get("status") or "").strip().lower()
        target = str(attempt.get("target") or "").strip()
        result = str(attempt.get("result") or "").strip()
        if status in {"failed", "blocked"} and target and len(result) >= 4:
            return True
    return False


def validate_resource_seed_order(run_dir: Path, context: dict) -> list[str]:
    """删除/更新主键须先创建再获取；不满足才允许带表列的查库来源。"""
    if context.get("run_mode") == "contract":
        return []
    catalog_path = run_dir / "interface-catalog.json"
    cases_path = run_dir / "test-cases.json"
    if not catalog_path.is_file() or not cases_path.is_file():
        return []
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        cases_payload = json.loads(cases_path.read_text(encoding="utf-8"))
        fixtures_path = run_dir / "fixtures.json"
        fixtures = json.loads(fixtures_path.read_text(encoding="utf-8")) if fixtures_path.is_file() else {}
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return [f"resource seed check failed: {error}"]
    interfaces = catalog.get("interfaces") if isinstance(catalog, dict) else None
    cases = cases_payload.get("cases") if isinstance(cases_payload, dict) else None
    if not isinstance(interfaces, list) or not isinstance(cases, list):
        return []
    methods_by_path: dict[str, set[str]] = {}
    for iface in interfaces:
        if not isinstance(iface, dict):
            continue
        path = _normalize_api_path(str(iface.get("path") or ""))
        method = str(iface.get("method") or "").upper()
        if path and method:
            methods_by_path.setdefault(path, set()).add(method)
    bindings = fixtures.get("endpoint_bindings") if isinstance(fixtures, dict) else {}
    seed_attempts = fixtures.get("resource_seed_attempts") if isinstance(fixtures, dict) else {}
    if not isinstance(bindings, dict):
        bindings = {}
    if not isinstance(seed_attempts, dict):
        seed_attempts = {}
    errors: list[str] = []
    for case in cases:
        if not isinstance(case, dict):
            continue
        if case.get("executable") is False or case.get("fixture_policy") == "negative_case":
            continue
        if str(case.get("method") or "").upper() not in {"PUT", "PATCH", "DELETE"}:
            continue
        if case.get("category") not in {"functional", "side_effect", "auth"}:
            continue
        if not _case_sends_resource_id(case):
            continue
        path = _normalize_api_path(str(case.get("path") or ""))
        siblings = methods_by_path.get(path, set())
        needs_create = "POST" in siblings
        needs_get = "GET" in siblings
        if not needs_create and not needs_get:
            continue
        source = _id_source(case, bindings).strip()
        if source.startswith("api:"):
            continue
        endpoint_id = str(case.get("endpoint_id") or "")
        attempts = seed_attempts.get(endpoint_id) if isinstance(seed_attempts.get(endpoint_id), list) else []
        if not source.startswith("db:"):
            errors.append(
                f"resource seed: {case.get('id')} 主键须来自同 path 创建/获取接口（api:），"
                f"当前来源 {source or '缺失'}"
            )
            continue
        missing = []
        if needs_create and not _seed_attempt_ok(attempts, "create"):
            missing.append("创建接口")
        if needs_get and not _seed_attempt_ok(attempts, "get"):
            missing.append("获取接口")
        if missing:
            errors.append(
                f"resource seed: {case.get('id')} 同 path 已有{'/'.join(missing)}，"
                "须先造数并取主键；不满足时才查库，并在 resource_seed_attempts 记录 failed/blocked"
            )
        elif not _db_source_names_column(source):
            errors.append(
                f"resource seed: {case.get('id')} 查库来源必须是 db:<service>.<table>.<column>，当前为 {source}"
            )
    return errors


def validate_unimplemented_traffic(run_dir: Path, context: dict) -> list[str]:
    """501 / not implemented 必须有近 30 天访问日志；0 调用只能 ignored。"""
    if context.get("run_mode") == "contract":
        return []
    results_path = run_dir / "execution" / "results.json"
    if not results_path.is_file():
        return []
    try:
        results_payload = json.loads(results_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return [f"unimplemented traffic check failed: {error}"]
    results = results_payload.get("results") if isinstance(results_payload, dict) else None
    if not isinstance(results, list):
        return []

    grouped: dict[str, list[dict]] = {}
    for result in results:
        if not isinstance(result, dict):
            continue
        case_id = str(result.get("case_id") or "")
        request_payload = None
        request_path = run_dir / "execution" / "requests" / f"{case_id}.json"
        if request_path.is_file():
            try:
                request_payload = json.loads(request_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                request_payload = None
        if not _result_is_unimplemented(result, request_payload if isinstance(request_payload, dict) else None):
            continue
        raw_path = ""
        if isinstance(request_payload, dict):
            raw_path = str(request_payload.get("path") or "")
            request = request_payload.get("request")
            if isinstance(request, dict) and request.get("url"):
                raw_path = str(request.get("url"))
        grouped.setdefault(_normalize_http_path(raw_path), []).append(result)
    if not grouped:
        return []

    evidence_path = run_dir / "execution" / "unimplemented-traffic.json"
    if not evidence_path.is_file():
        return [
            "unimplemented traffic: HTTP 501/not implemented 缺少近 30 天访问日志证据 "
            "execution/unimplemented-traffic.json"
        ]
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return [f"unimplemented traffic evidence invalid: {error}"]
    if not isinstance(evidence, dict):
        return ["unimplemented traffic evidence must be an object"]
    errors: list[str] = []
    window_error = _traffic_window_error(evidence)
    if window_error:
        errors.append(f"unimplemented traffic: {window_error}")
    interfaces = evidence.get("interfaces")
    by_path: dict[str, dict] = {}
    if isinstance(interfaces, list):
        for item in interfaces:
            if isinstance(item, dict):
                by_path[_normalize_http_path(str(item.get("path") or ""))] = item
    for path, endpoint_results in grouped.items():
        item = by_path.get(path)
        case_ids = ",".join(str(result.get("case_id") or "") for result in endpoint_results)
        if item is None:
            errors.append(f"unimplemented traffic: {path} ({case_ids}) 没有 30 天日志记录")
            continue
        hit_count = item.get("hit_count")
        decision = str(item.get("decision") or "")
        if item.get("query_error"):
            if decision != "unverified" or any(result.get("status") != "failed" for result in endpoint_results):
                errors.append(f"unimplemented traffic: {path} 日志查询失败，必须 decision=unverified 且用例保持 failed")
            continue
        if not isinstance(hit_count, int) or isinstance(hit_count, bool) or hit_count < 0:
            errors.append(f"unimplemented traffic: {path} hit_count 必须是非负整数")
            continue
        statuses = {str(result.get("status") or "") for result in endpoint_results}
        if hit_count == 0:
            if decision != "ignore" or statuses != {"ignored"}:
                errors.append(
                    f"unimplemented traffic: {path} 近 30 天调用数为 0，必须忽略整条接口，不得保留 failed"
                )
        elif decision != "fail" or "failed" not in statuses:
            errors.append(f"unimplemented traffic: {path} 近 30 天有 {hit_count} 次调用，必须保留 failed")
    return errors


def _response_status(result: dict, request_payload: dict | None) -> int | None:
    sources: list[dict] = [result]
    if isinstance(request_payload, dict):
        sources.append(request_payload)
        response = request_payload.get("response")
        if isinstance(response, dict):
            sources.append(response)
    for source in sources:
        value = source.get("response_status", source.get("status"))
        if isinstance(value, int) and not isinstance(value, bool) and value >= 100:
            return value
    return None


def _zero_traffic_targets(results: list[dict], run_dir: Path) -> dict[str, list[dict]]:
    """HTTP 5xx 且不属于 501/未实现门禁的失败，必须再核 path 流量。"""
    grouped: dict[str, list[dict]] = {}
    for result in results:
        if not isinstance(result, dict) or result.get("status") not in {"failed", "passed"}:
            continue
        case_id = str(result.get("case_id") or "")
        request_payload = None
        request_path = run_dir / "execution" / "requests" / f"{case_id}.json"
        if request_path.is_file():
            try:
                request_payload = json.loads(request_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                request_payload = None
        payload = request_payload if isinstance(request_payload, dict) else None
        if _result_is_unimplemented(result, payload):
            continue
        if (_response_status(result, payload) or 0) < 500:
            continue
        raw_path = ""
        if payload is not None:
            raw_path = str(payload.get("path") or "")
            request = payload.get("request")
            if isinstance(request, dict) and request.get("url"):
                raw_path = str(request.get("url"))
        grouped.setdefault(_normalize_http_path(raw_path), []).append(result)
    return grouped


def _path_traffic_index(run_dir: Path) -> tuple[dict[str, dict], str]:
    evidence_path = run_dir / "execution" / "path-traffic.json"
    if not evidence_path.is_file():
        return {}, ""
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return {}, f"path traffic evidence invalid: {error}"
    if not isinstance(evidence, dict):
        return {}, "path traffic evidence must be an object"
    window_error = _traffic_window_error(evidence)
    if window_error:
        return {}, f"path traffic: {window_error}"
    by_path: dict[str, dict] = {}
    interfaces = evidence.get("interfaces")
    if isinstance(interfaces, list):
        for item in interfaces:
            if isinstance(item, dict):
                by_path[_normalize_http_path(str(item.get("path") or ""))] = item
    return by_path, ""


def validate_zero_traffic_failures(run_dir: Path, context: dict) -> list[str]:
    """参数已绑定的 HTTP 5xx 也要核近 30 天 path 调用数；0 调用不得保留 failed。"""
    if context.get("run_mode") == "contract":
        return []
    results_path = run_dir / "execution" / "results.json"
    if not results_path.is_file():
        return []
    try:
        results_payload = json.loads(results_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return [f"path traffic check failed: {error}"]
    results = results_payload.get("results") if isinstance(results_payload, dict) else None
    if not isinstance(results, list):
        return []
    grouped = _zero_traffic_targets(results, run_dir)
    if not grouped:
        return []
    by_path, evidence_error = _path_traffic_index(run_dir)
    if evidence_error:
        return [evidence_error]
    if not by_path and not (run_dir / "execution" / "path-traffic.json").is_file():
        paths = ", ".join(sorted(grouped))
        return [
            "path traffic: HTTP 5xx 缺少近 30 天 path 调用证据 "
            f"execution/path-traffic.json ({paths})"
        ]
    errors: list[str] = []
    results_by_path: dict[str, list[dict]] = {}
    for result in results:
        if not isinstance(result, dict):
            continue
        case_id = str(result.get("case_id") or "")
        request_path = run_dir / "execution" / "requests" / f"{case_id}.json"
        raw_path = ""
        if request_path.is_file():
            try:
                payload = json.loads(request_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                raw_path = str(payload.get("path") or "")
                request = payload.get("request")
                if isinstance(request, dict) and request.get("url"):
                    raw_path = str(request.get("url"))
        results_by_path.setdefault(_normalize_http_path(raw_path), []).append(result)
    for path, endpoint_results in grouped.items():
        item = by_path.get(path)
        case_ids = ",".join(str(result.get("case_id") or "") for result in endpoint_results)
        if item is None:
            errors.append(f"path traffic: {path} ({case_ids}) 没有 30 天 path 调用记录")
            continue
        hit_count = item.get("hit_count")
        decision = str(item.get("decision") or "")
        if item.get("query_error") or decision == "unverified":
            if decision != "unverified" or any(result.get("status") != "failed" for result in endpoint_results):
                errors.append(f"path traffic: {path} 日志查询失败，必须 decision=unverified 且用例保持 failed")
            continue
        if not isinstance(hit_count, int) or isinstance(hit_count, bool) or hit_count < 0:
            errors.append(f"path traffic: {path} hit_count 必须是非负整数")
            continue
        if hit_count == 0:
            statuses = {str(result.get("status") or "") for result in results_by_path.get(path, [])}
            if decision != "ignore" or statuses != {"ignored"}:
                errors.append(
                    f"path traffic: {path} 近 30 天调用数为 0，必须忽略整条接口，不得保留 failed"
                )
        elif decision != "keep" or any(
            result.get("status") not in {"failed", "passed"} for result in endpoint_results
        ):
            errors.append(f"path traffic: {path} 近 30 天有 {hit_count} 次调用，HTTP 5xx 必须保留在结果中")
    return errors


_BINDING_LOG_DECISIONS = {"bound", "absent", "no_traffic", "sensitive_withheld", "unverified"}
_SENSITIVE_BINDING_FIELD = re.compile(
    r"phone|mobile|idcard|id_card|token|authorization|cookie|password|passwd|secret|"
    r"^conv$|^attribution$",
    re.IGNORECASE,
)


def _binding_field_is_sensitive(field: str) -> bool:
    leaf = str(field or "").rsplit(".", 1)[-1]
    return bool(_SENSITIVE_BINDING_FIELD.search(leaf))


def _log_absent_request_fields(run_dir: Path) -> set[tuple[str, str, str]]:
    """近 30 天有请求、字段始终为空时，允许按线上请求省略该字段。"""
    path = run_dir / "execution" / "binding-log-supplement.json"
    if not path.is_file():
        return set()
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return set()
    if not isinstance(evidence, dict):
        return set()
    found: set[tuple[str, str, str]] = set()
    for item in evidence.get("fields") or []:
        if not isinstance(item, dict) or str(item.get("decision") or "") != "absent":
            continue
        if item.get("request_logged") is not True:
            continue
        hit_count = item.get("hit_count")
        present = item.get("field_present_count")
        if not isinstance(hit_count, int) or isinstance(hit_count, bool) or hit_count <= 0:
            continue
        if present != 0:
            continue
        field = str(item.get("field") or "")
        location, _, name = field.partition(".")
        endpoint_id = str(item.get("endpoint_id") or "")
        if endpoint_id and name and location in {"query", "body", "path_params"}:
            found.add((endpoint_id, location, name))
    return found


def _binding_log_index(evidence: dict) -> dict[tuple[str, str], dict]:
    indexed: dict[tuple[str, str], dict] = {}
    fields = evidence.get("fields")
    if not isinstance(fields, list):
        return indexed
    for item in fields:
        if not isinstance(item, dict):
            continue
        endpoint_id = str(item.get("endpoint_id") or "").strip()
        field = str(item.get("field") or "").strip()
        if endpoint_id and field:
            indexed[(endpoint_id, field)] = item
    return indexed


def _binding_log_item_errors(item: dict, label: str) -> list[str]:
    errors: list[str] = []
    decision = str(item.get("decision") or "")
    if decision not in _BINDING_LOG_DECISIONS:
        return [f"binding log: {label} decision 必须是 {', '.join(sorted(_BINDING_LOG_DECISIONS))}"]
    if item.get("query_error") or decision == "unverified":
        return [f"binding log: {label} 日志未核验，不得记 required_binding_missing"]
    hit_count = item.get("hit_count")
    if not isinstance(hit_count, int) or isinstance(hit_count, bool) or hit_count < 0:
        return [f"binding log: {label} hit_count 必须是非负整数"]
    request_logged = item.get("request_logged")
    present = item.get("field_present_count")
    if decision == "no_traffic":
        if hit_count != 0:
            errors.append(f"binding log: {label} decision=no_traffic 时 hit_count 必须为 0")
        return errors
    if request_logged is not True:
        return [f"binding log: {label} 日志没有请求内容，不得当成字段未出现"]
    if not isinstance(present, int) or isinstance(present, bool) or present < 0:
        return [f"binding log: {label} field_present_count 必须是非负整数"]
    if decision == "absent":
        if hit_count <= 0 or present != 0:
            errors.append(f"binding log: {label} decision=absent 需要有调用且字段出现次数为 0")
    elif decision == "sensitive_withheld":
        field = str(item.get("field") or "")
        if not _binding_field_is_sensitive(field):
            errors.append(f"binding log: {label} 非敏感字段不得用 sensitive_withheld 跳过回填")
        if item.get("value"):
            errors.append(f"binding log: {label} 敏感字段不得把线上原值写入证据")
        if present <= 0:
            errors.append(f"binding log: {label} decision=sensitive_withheld 需要字段在日志中出现")
    elif decision == "bound" and present <= 0:
        errors.append(f"binding log: {label} decision=bound 需要字段在日志中出现")
    return errors


def validate_binding_log_supplement(run_dir: Path, context: dict) -> list[str]:
    """缺绑定必须先有近 30 天线上日志补数证据；查到的非敏感值不得继续缺绑定。"""
    del context
    cases_path = run_dir / "test-cases.json"
    if not cases_path.is_file():
        return []
    try:
        cases_payload = json.loads(cases_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return [f"binding log check failed: {error}"]
    cases = cases_payload.get("cases") if isinstance(cases_payload, dict) else cases_payload
    if not isinstance(cases, list):
        return []

    missing: list[tuple[str, str, str]] = []
    for case in cases:
        if not isinstance(case, dict):
            continue
        endpoint_id = str(case.get("endpoint_id") or "")
        case_id = str(case.get("id") or "")
        for field in _missing_binding_fields(case):
            missing.append((case_id, endpoint_id, field))

    log_sources: list[tuple[str, str]] = []
    fixtures_path = run_dir / "fixtures.json"
    if fixtures_path.is_file():
        try:
            fixtures_payload = json.loads(fixtures_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            return [f"binding log check failed: {error}"]
        bindings = fixtures_payload.get("endpoint_bindings") if isinstance(fixtures_payload, dict) else None
        if isinstance(bindings, dict):
            for endpoint_id, entry in bindings.items():
                sources = entry.get("sources") if isinstance(entry, dict) else None
                if not isinstance(sources, dict):
                    continue
                for name, source in sources.items():
                    if str(source or "").startswith("log:"):
                        log_sources.append((str(endpoint_id), str(name)))
    if not missing and not log_sources:
        return []

    evidence_path = run_dir / "execution" / "binding-log-supplement.json"
    if not evidence_path.is_file():
        return [
            "binding log: 缺绑定或 log: 来源缺少近 30 天线上日志补数证据 "
            "execution/binding-log-supplement.json"
        ]
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return [f"binding log evidence invalid: {error}"]
    if not isinstance(evidence, dict):
        return ["binding log evidence must be an object"]
    errors: list[str] = []
    window_error = _traffic_window_error(evidence)
    if window_error:
        errors.append(f"binding log: {window_error}")
    indexed = _binding_log_index(evidence)
    seen: set[tuple[str, str]] = set()
    for case_id, endpoint_id, field in missing:
        key = (endpoint_id, field)
        label = f"{case_id} {endpoint_id} {field}".strip()
        item = indexed.get(key)
        if item is None:
            errors.append(f"binding log: {label} 没有 30 天日志补数记录")
            continue
        if key in seen:
            continue
        seen.add(key)
        if str(item.get("decision") or "") == "bound":
            errors.append(f"binding log: {label} 日志已有请求值，不得保持 required_binding_missing")
            continue
        errors.extend(_binding_log_item_errors(item, label))
    for endpoint_id, name in log_sources:
        matched = [
            item
            for (item_endpoint, field), item in indexed.items()
            if item_endpoint == endpoint_id and (field == name or field.endswith("." + name))
        ]
        label = f"{endpoint_id}.{name}"
        if not matched or str(matched[0].get("decision") or "") != "bound":
            errors.append(f"binding log: {label} 来源是 log:，证据 decision 必须为 bound")
    return errors


def validate_code_offline_scan(run_dir: Path, context: dict) -> list[str]:
    """校验 code-offline-filter.json 的 scan.by_path 覆盖（见 reference/code-offline-filter.md）。"""
    errors: list[str] = []
    if context.get("run_mode") in {"execute", "replay", "contract"}:
        return errors
    offline_path = run_dir / "code-offline-filter.json"
    yapi_filter_path = run_dir / "yapi-proto-filter.json"
    catalog_path = run_dir / "interface-catalog.json"
    if not offline_path.is_file():
        # 旧 run 可能只有 yapi-proto-filter；有 code_offline 但无独立 scan 时提示一次
        if yapi_filter_path.is_file():
            try:
                yapi_payload = json.loads(yapi_filter_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                return errors
            filtered = yapi_payload.get("filtered") if isinstance(yapi_payload, dict) else None
            has_offline = isinstance(filtered, list) and any(
                isinstance(item, dict) and item.get("reason") == "code_offline" for item in filtered
            )
            scan = yapi_payload.get("scan") if isinstance(yapi_payload, dict) else None
            if has_offline and not (isinstance(scan, dict) and scan.get("by_path")):
                errors.append(
                    "code_offline entries exist in yapi-proto-filter.json but "
                    "code-offline-filter.json / scan.by_path is missing "
                    "(see reference/code-offline-filter.md)"
                )
        return errors
    if not catalog_path.is_file():
        return errors
    try:
        offline_payload = json.loads(offline_path.read_text(encoding="utf-8"))
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return [f"code-offline-filter check failed: {error}"]
    if not isinstance(offline_payload, dict):
        return ["code-offline-filter.json must be an object"]
    scan = offline_payload.get("scan")
    if not isinstance(scan, dict):
        errors.append("code-offline-filter.json missing scan object")
        return errors
    if scan.get("complete") is not True:
        errors.append("code-offline-filter.json scan.complete must be true")
    by_path = scan.get("by_path")
    if not isinstance(by_path, dict) or not by_path:
        errors.append("code-offline-filter.json scan.by_path must be a non-empty object")
        return errors

    interfaces = catalog.get("interfaces") if isinstance(catalog, dict) else None
    if isinstance(interfaces, list):
        for iface in interfaces:
            if not isinstance(iface, dict):
                continue
            method = str(iface.get("method") or "")
            path = str(iface.get("path") or "")
            if not method or not path:
                continue
            entry = _offline_scan_lookup(by_path, method, path)
            if entry is None:
                errors.append(
                    f"code-offline scan missing catalog path: {_offline_scan_key(method, path)}"
                )
                continue
            status = str(entry.get("status") or "").lower()
            if status == "unknown":
                errors.append(
                    f"code-offline scan remains unknown after required fallback: "
                    f"{_offline_scan_key(method, path)}"
                )
            if status == "offline":
                errors.append(
                    f"catalog still contains code_offline path: {_offline_scan_key(method, path)}"
                )

    filtered = offline_payload.get("filtered")
    if isinstance(filtered, list):
        for item in filtered:
            if not isinstance(item, dict) or item.get("reason") != "code_offline":
                continue
            method = str(item.get("method") or "")
            path = str(item.get("path") or "")
            entry = _offline_scan_lookup(by_path, method, path)
            if entry is None:
                errors.append(
                    f"code-offline scan missing filtered path: {_offline_scan_key(method, path)}"
                )
            elif str(entry.get("status") or "").lower() != "offline":
                errors.append(
                    f"code-offline filtered path scan.status must be offline: "
                    f"{_offline_scan_key(method, path)}"
                )
    return errors


def validate_write_authorization_artifacts(
    run_dir: Path,
    context: dict,
) -> list[str]:
    """已授权运行中：禁止授权型跳过，禁止无绑定阻塞的破坏性写仍不可执行。

    授权解析不可用时 fail closed：报告可观测错误（只含异常类型）并停止本任务检查。
    """
    errors: list[str] = []
    resolver = resolve_write_authorization
    if resolver is None:
        reason = WRITE_AUTHORIZATION_LOAD_ERROR or "ImportError"
        return [f"write authorization resolution failed: {reason}"]
    try:
        allowed = bool(resolver(context, os.environ).allowed)
    except Exception as error:
        return [f"write authorization resolution failed: {type(error).__name__}"]
    if not allowed:
        return errors

    cases_path = run_dir / "test-cases.json"
    results_path = run_dir / "execution/results.json"
    cases_by_id: dict[str, dict] = {}
    if cases_path.is_file():
        try:
            payload = json.loads(cases_path.read_text(encoding="utf-8"))
            for item in _json_cases(payload):
                case_id = _safe_case_id(item.get("id"))
                if case_id:
                    cases_by_id[case_id] = item
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            pass

    if results_path.is_file():
        try:
            results_payload = json.loads(results_path.read_text(encoding="utf-8"))
            raw_results = (
                results_payload.get("cases")
                if isinstance(results_payload, dict)
                else results_payload
            )
            results = raw_results if isinstance(raw_results, list) else []
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            results = []
        for result in results:
            if not isinstance(result, dict):
                continue
            if str(result.get("status") or "").strip().lower() != "skipped":
                continue
            case_id = _safe_case_id(result.get("id"))
            if not case_id:
                continue
            case = cases_by_id.get(case_id) or {}
            method = str(case.get("method") or result.get("method") or "").upper()
            if method not in WRITE_METHODS:
                continue
            skip_text = _result_skip_text(result)
            if not skip_text and isinstance(case, dict):
                skip_text = case.get("skip_reason")
            if _is_authorization_skip_reason(skip_text):
                errors.append(
                    "authorized write case still skipped for missing authorization: "
                    f"{case_id}"
                )

    for case_id, case in cases_by_id.items():
        method = str(case.get("method") or "").upper()
        if method not in WRITE_METHODS:
            continue
        if case.get("executable") is not False:
            continue
        if not _is_destructive_write_case(case):
            continue
        if _has_binding_blocker(case):
            continue
        errors.append(
            "authorized destructive case remains non-executable without binding blocker: "
            f"{case_id}"
        )
    return errors


def validate(run_dir: Path) -> list[str]:
    errors: list[str] = []
    context_path = run_dir / "run-context.json"
    if not context_path.is_file():
        return ["missing run-context.json"]

    try:
        context = load_json(context_path)
        required = required_artifacts(context)
        if requires_contract(context):
            for relative in CONTRACT_ARTIFACTS:
                if relative not in required:
                    required.append(relative)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return [str(error)]

    for relative in required:
        path = run_dir / relative
        if not path.is_file():
            errors.append(f"missing: {relative}")
        elif path.stat().st_size == 0:
            errors.append(f"empty: {relative}")

    if requires_scripts(context):
        tests_dir = run_dir / "tests"
        if not tests_dir.is_dir() or not any(tests_dir.glob("*.spec.ts")):
            errors.append("missing: tests/*.spec.ts")

    if requires_gitlab_upload(context, run_dir):
        upload_path = run_dir / "gitlab-upload.json"
        if not upload_path.is_file() or upload_path.stat().st_size == 0:
            errors.append("missing: gitlab-upload.json")
        else:
            try:
                upload = json.loads(upload_path.read_text(encoding="utf-8"))
                if not isinstance(upload, dict) or not upload.get("ok"):
                    errors.append("gitlab-upload.json ok is not true; remote delivery failed")
                if upload.get("mock") is True:
                    errors.append("gitlab-upload.json mock=true; must push real remote")
            except json.JSONDecodeError as error:
                errors.append(f"invalid JSON: gitlab-upload.json: {error}")

    for relative in (
        "interface-catalog.json",
        "fixtures.json",
        "test-cases.json",
        "contract-clauses.json",
    ):
        path = run_dir / relative
        if path.is_file():
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                errors.append(f"invalid JSON: {relative}: {error}")

    cases_json = run_dir / "test-cases.json"
    cases_md = run_dir / "test-cases.md"
    if cases_json.is_file() and cases_md.is_file():
        try:
            payload = json.loads(cases_json.read_text(encoding="utf-8"))
            cases = payload.get("cases") if isinstance(payload, dict) else payload
            if isinstance(cases, list):
                lines = cases_md.read_text(encoding="utf-8").splitlines()
                in_overview = False
                overview_case_rows = 0
                for line in lines:
                    if line.strip() == "## 总览":
                        in_overview = True
                        continue
                    if in_overview and line.startswith("## "):
                        break
                    if (
                        in_overview
                        and line.startswith("| ")
                        and not line.startswith("|-")
                        and "编号" not in line
                        and re.match(r"^\|\s*[A-Za-z0-9._-]+\s*\|", line)
                    ):
                        overview_case_rows += 1
                if overview_case_rows != len(cases):
                    errors.append(
                        f"test-cases.md overview rows ({overview_case_rows}) != cases ({len(cases)})"
                    )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            errors.append(f"test-cases table check failed: {error}")

    filter_path = run_dir / "yapi-proto-filter.json"
    cases_json = run_dir / "test-cases.json"
    if filter_path.is_file() and cases_json.is_file():
        try:
            filtered_payload = json.loads(filter_path.read_text(encoding="utf-8"))
            dropped = {
                (str(item.get("method") or "").upper(), str(item.get("path") or "").rstrip("/"))
                for item in (filtered_payload.get("filtered") or [])
                if isinstance(item, dict)
                and item.get("reason") in {"not_in_proto", "code_offline"}
            }
            cases_payload = json.loads(cases_json.read_text(encoding="utf-8"))
            cases = cases_payload.get("cases") if isinstance(cases_payload, dict) else cases_payload
            catalog_path = run_dir / "interface-catalog.json"
            catalog_keys: set[tuple[str, str]] = set()
            if catalog_path.is_file():
                try:
                    catalog_payload = json.loads(catalog_path.read_text(encoding="utf-8"))
                    for iface in catalog_payload.get("interfaces") or []:
                        if isinstance(iface, dict):
                            catalog_keys.add(
                                (
                                    str(iface.get("method") or "").upper(),
                                    str(iface.get("path") or "").rstrip("/"),
                                )
                            )
                except (OSError, ValueError, json.JSONDecodeError):
                    catalog_keys = set()
            if isinstance(cases, list):
                for case in cases:
                    if not isinstance(case, dict):
                        continue
                    key = (
                        str(case.get("method") or "").upper(),
                        str(case.get("path") or "").rstrip("/"),
                    )
                    # 远程合并保留的历史 code_offline 用例：不在本 run catalog 内则不报错
                    if catalog_keys and key not in catalog_keys:
                        continue
                    if key in dropped:
                        reason = next(
                            (
                                str(item.get("reason"))
                                for item in (filtered_payload.get("filtered") or [])
                                if isinstance(item, dict)
                                and str(item.get("method") or "").upper() == key[0]
                                and str(item.get("path") or "").rstrip("/") == key[1]
                                and item.get("reason") in {"not_in_proto", "code_offline"}
                            ),
                            "filtered",
                        )
                        errors.append(
                            f"test case {case.get('id')} uses path filtered as {reason}: {key[0]} {key[1]}"
                        )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            errors.append(f"yapi-proto-filter check failed: {error}")

    if context.get("run_mode") in {"full", "baseline", "replay"}:
        errors.extend(validate_regression_artifacts(run_dir))

    errors.extend(validate_write_authorization_artifacts(run_dir, context))
    errors.extend(validate_fixture_partial_binding_leak(run_dir, context))
    errors.extend(validate_code_unused_sources(run_dir, context))
    errors.extend(validate_binding_log_supplement(run_dir, context))
    errors.extend(validate_resource_seed_order(run_dir, context))
    errors.extend(validate_code_offline_scan(run_dir, context))
    errors.extend(validate_unimplemented_traffic(run_dir, context))
    errors.extend(validate_zero_traffic_failures(run_dir, context))
    errors.extend(validate_code_response_schema(run_dir, context))
    errors.extend(validate_negative_code_return(run_dir, context))
    errors.extend(validate_code_failure_calibration(run_dir, context))

    # Full coverage gate: forbid smoke-only / per-endpoint single smoke as default delivery
    catalog_path = run_dir / "interface-catalog.json"
    if cases_json.is_file() and catalog_path.is_file() and context.get("run_mode") not in {"execute", "replay"}:
        try:
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            cases_payload = json.loads(cases_json.read_text(encoding="utf-8"))
            cases = cases_payload.get("cases") if isinstance(cases_payload, dict) else None
            interfaces = catalog.get("interfaces") if isinstance(catalog, dict) else None
            if isinstance(cases, list) and isinstance(interfaces, list) and interfaces:
                by_ep: dict[str, list[dict]] = {}
                for case in cases:
                    if isinstance(case, dict):
                        by_ep.setdefault(str(case.get("endpoint_id") or ""), []).append(case)
                smoke_only = bool(cases) and all(
                    isinstance(c, dict) and c.get("category") == "smoke" for c in cases
                )
                if smoke_only:
                    errors.append(
                        "test-cases are smoke-only; default delivery must be full coverage "
                        "(functional/validation/error[/side_effect][/list]; "
                        "auth per app_usage tri-state, omitted only for complete "
                        "not_called_by_app), not smoke"
                    )
                if len(cases) == len(interfaces) and smoke_only:
                    errors.append("one-case-per-interface smoke plan is not allowed")
                if validate_app_usage_evidence is None or should_generate_auth is None:
                    reason = APP_USAGE_LOAD_ERROR or "ImportError"
                    errors.append(f"app usage validation failed: {reason}")
                else:
                    errors.extend(validate_app_usage_evidence(run_dir, catalog))
                write_methods = {"POST", "PUT", "PATCH", "DELETE"}

                def _id_fields(iface: dict) -> tuple[bool, bool]:
                    required_id = False
                    for p in list(iface.get("path_parameters") or []) + list(iface.get("query_parameters") or []):
                        if not isinstance(p, dict):
                            continue
                        name = str(p.get("name") or "").lower()
                        if "id" not in name:
                            continue
                        required_id = True
                    body = iface.get("request_body")
                    if isinstance(body, dict):
                        if isinstance(body.get("schema"), dict):
                            body = body["schema"]
                        for key in body.get("properties") or {}:
                            if "id" in str(key).lower():
                                required_id = True
                    return required_id, False

                def _semantic_id_fields(iface: dict) -> list[tuple[str, str]]:
                    method = str(iface.get("method") or "").upper()
                    text = f"{iface.get('path') or ''} {iface.get('name') or ''}".lower()
                    listish = (
                        any(h in text for h in ("list", "page", "search", "列表", "分页"))
                        or any(
                            str((p or {}).get("name") or "").lower()
                            in {"limit", "pagesize", "page_size", "page", "pageno", "size"}
                            for p in iface.get("query_parameters") or []
                            if isinstance(p, dict)
                        )
                    )
                    hints = (
                        "detail", "info", "edit", "update", "delete", "remove",
                        "status", "state", "详情", "编辑", "更新", "删除",
                        "状态", "上下架", "启用", "停用",
                    )
                    if listish or not (
                        method in write_methods or any(hint in text for hint in hints)
                    ):
                        return []
                    fields: list[tuple[str, str]] = []
                    for location, params in (
                        ("path_params", iface.get("path_parameters") or []),
                        ("query", iface.get("query_parameters") or []),
                    ):
                        for param in params:
                            if not isinstance(param, dict):
                                continue
                            name = str(param.get("name") or "")
                            schema = param.get("schema") or {}
                            if "id" in name.lower() or str(schema.get("format") or "").lower() == "uuid":
                                fields.append((location, name))
                    body = iface.get("request_body") or {}
                    if isinstance(body.get("schema"), dict):
                        body = body["schema"]
                    for name, schema in (body.get("properties") or {}).items():
                        if "id" in str(name).lower() or str((schema or {}).get("format") or "").lower() == "uuid":
                            fields.append(("body", str(name)))
                    return list(dict.fromkeys(fields))

                for iface in interfaces:
                    if not isinstance(iface, dict):
                        continue
                    eid = str(iface.get("id") or "")
                    ep_cases = by_ep.get(eid) or []
                    cats = {c.get("category") for c in ep_cases if isinstance(c, dict)}
                    # 每个接口新建 required_base，避免前一接口 .add("auth") 污染后一接口
                    required_base = {"functional", "validation"}
                    if should_generate_auth is None or should_generate_auth(iface):
                        required_base.add("auth")
                    missing = required_base - cats
                    if missing:
                        errors.append(
                            f"endpoint {eid} missing required categories: {sorted(missing)}"
                        )
                    method = str(iface.get("method") or "").upper()
                    path = str(iface.get("path") or "").lower()
                    name = str(iface.get("name") or "").lower()
                    listish = any(h in path for h in ("list", "page", "search")) or "list" in name or "分页" in name
                    mutating = method in write_methods and not listish and "获取列表" not in name and "查询" not in name
                    if mutating and "side_effect" not in cats:
                        errors.append(f"endpoint {eid} mutating write missing side_effect category")
                    if listish and "list" not in cats:
                        errors.append(f"endpoint {eid} list-like path missing list category")
                    required_id, optional_id = _id_fields(iface)
                    fake_not_found = [
                        c
                        for c in ep_cases
                        if isinstance(c, dict)
                        and c.get("category") == "error"
                        and "资源不存在" in str(c.get("name") or "")
                        and (c.get("expected") or {}).get("status_class") == "4xx"
                    ]
                    if fake_not_found and not required_id:
                        errors.append(
                            f"endpoint {eid} has not-found error expecting 4xx but catalog has no required resource id"
                        )
                    if listish:
                        for c in ep_cases:
                            if not isinstance(c, dict) or c.get("category") != "list":
                                continue
                            cname = str(c.get("name") or "")
                            request = c.get("request") or {}
                            containers: list[dict] = []
                            query = request.get("query") or {}
                            body = request.get("body") or {}
                            if isinstance(query, dict):
                                containers.append(query)
                            if isinstance(body, dict):
                                containers.append(body)
                            limit_keys = [
                                k
                                for container in containers
                                for k in container
                                if str(k).lower() in {"limit", "pagesize", "page_size", "size"}
                            ]
                            if ("limit" in cname.lower() or "pagesize" in cname.lower()) and not limit_keys:
                                errors.append(
                                    f"case {c.get('id')} list limit case missing limit/pageSize in request query/body"
                                )

                # Real fixture pre-execution gate: functional/auth business IDs must
                # come from a verified DB row or an authorized prerequisite API.
                fixtures_path = run_dir / "fixtures.json"
                fixtures_payload = (
                    load_json(fixtures_path)
                    if fixtures_path.is_file() and context.get("run_mode") != "contract"
                    else {}
                )
                endpoint_bindings = fixtures_payload.get("endpoint_bindings") or {}
                interface_by_id = {
                    str(iface.get("id") or ""): iface
                    for iface in interfaces
                    if isinstance(iface, dict)
                }
                real_data_cases = (
                    cases
                    if fixtures_payload and context.get("run_mode") != "contract"
                    else []
                )
                log_absent_fields = _log_absent_request_fields(run_dir)
                for case in real_data_cases:
                    if (
                        not isinstance(case, dict)
                        or case.get("category") not in {"functional", "auth"}
                        or case.get("executable") is False
                    ):
                        continue
                    cid = str(case.get("id") or "")
                    request = case.get("request") or {}
                    business_request = {
                        "query": request.get("query") or {},
                        "body": request.get("body"),
                        "path_params": request.get("path_params") or {},
                    }
                    for path in placeholder_paths(business_request):
                        errors.append(
                            f"{case.get('category')} {cid} request contains empty/placeholder value: {path}"
                        )
                    blockers = case.get("regression_blockers") or []
                    if any(str(item).startswith("required_binding_missing:") for item in blockers):
                        errors.append(
                            f"{case.get('category')} {cid} is executable with required_binding_missing blocker"
                        )

                    iface = interface_by_id.get(str(case.get("endpoint_id") or "")) or {}
                    binding = endpoint_bindings.get(str(case.get("endpoint_id") or "")) or {}
                    unused = binding.get("usage") if isinstance(binding.get("usage"), dict) else {}
                    unused_sources = binding.get("sources") if isinstance(binding.get("sources"), dict) else {}
                    endpoint_id = str(case.get("endpoint_id") or "")
                    for location, name in all_request_fields(iface):
                        if (endpoint_id, location, name) in log_absent_fields:
                            continue
                        if (
                            str(unused.get(name) or "").strip().lower() == "code_unused"
                            and str(unused_sources.get(name) or "").startswith("code:")
                        ):
                            continue
                        container = request.get(location)
                        if not request_field_exists(container, name):
                            errors.append(
                                f"{case.get('category')} {cid} missing default-required field: "
                                f"{location}.{name}"
                            )
                    values = binding.get("values") or {}
                    sources = binding.get("sources") or {}
                    for location, name in _semantic_id_fields(iface):
                        if (endpoint_id, location, name) in log_absent_fields:
                            continue
                        if (
                            str(unused.get(name) or "").strip().lower() == "code_unused"
                            and str(unused_sources.get(name) or "").startswith("code:")
                        ):
                            continue
                        container = request.get(location)
                        value = container.get(name) if isinstance(container, dict) else None
                        if value is None or (isinstance(value, str) and not value.strip()):
                            errors.append(
                                f"{case.get('category')} {cid} missing semantic real binding: {location}.{name}"
                            )
                            continue
                        source = str(sources.get(name) or "")
                        if values.get(name) != value or not source.startswith(
                            ("db:", "api:", "get:", "user_provided", "log:")
                        ):
                            errors.append(
                                f"{case.get('category')} {cid} semantic binding "
                                f"{location}.{name} lacks verified endpoint source"
                            )

                # functional closed-loop gate (6 rules)
                for case in cases:
                    if not isinstance(case, dict) or case.get("category") != "functional":
                        continue
                    if case.get("executable") is False:
                        continue
                    cid = case.get("id")
                    # 1) fixture traceability
                    refs = case.get("fixture_refs")
                    policy = case.get("fixture_policy") or ""
                    if refs is None:
                        errors.append(f"functional {cid} missing fixture_refs")
                    elif refs == [] and policy not in {"none_ok", "optional"}:
                        errors.append(
                            f"functional {cid} empty fixture_refs without fixture_policy=none_ok"
                        )
                    if refs and not case.get("fixture_trace"):
                        errors.append(
                            f"functional {cid} has fixture_refs but missing fixture_trace"
                        )
                    # 2) dual-track + schema
                    expected = case.get("expected") or {}
                    http = expected.get("http") if isinstance(expected, dict) else None
                    business = expected.get("business") if isinstance(expected, dict) else None
                    schema = expected.get("schema") if isinstance(expected, dict) else None
                    if not isinstance(http, dict) or not (
                        http.get("status") or http.get("status_class")
                    ):
                        errors.append(
                            f"functional {cid} expected.http missing status/status_class"
                        )
                    if not isinstance(business, dict):
                        cal = (
                            (expected.get("calibration") or {})
                            if isinstance(expected, dict)
                            else {}
                        )
                        if (business is None) and str(cal.get("note") or "").find(
                            "http_only"
                        ) < 0:
                            errors.append(
                                f"functional {cid} expected.business missing (dual-track required)"
                            )
                    # schema 允许：required_fields / properties / 明确 type（含 EmptyResp 空 object）
                    # / source=undeclared。禁止因「无字段」再逼生成器猜 code。
                    supported_types = {
                        "object",
                        "array",
                        "string",
                        "number",
                        "integer",
                        "boolean",
                    }
                    if not isinstance(schema, dict) or not (
                        schema.get("required_fields")
                        or schema.get("properties")
                        or schema.get("type") in supported_types
                        or schema.get("source") == "undeclared"
                        or schema.get("empty_response") is True
                    ):
                        errors.append(
                            f"functional {cid} expected.schema missing "
                            "(need required_fields/properties/type/undeclared)"
                        )
                    # 3) verify_read
                    verify = case.get("verify_read")
                    if not isinstance(verify, dict) or not verify.get("mode"):
                        errors.append(f"functional {cid} missing verify_read")
                    else:
                        method = str(case.get("method") or "").upper()
                        path = str(case.get("path") or "").lower()
                        name = str(case.get("name") or "")
                        query_like = method == "GET" or any(
                            h in path
                            for h in (
                                "/list",
                                "list/",
                                "/search",
                                "/query",
                                "by-",
                                "best-",
                                "deduct/history",
                                "deduct-source",
                                "pay-detail",
                            )
                        ) or any(
                            k in name
                            for k in ("获取", "查询", "列表", "统计", "批量查询", "返回")
                        )
                        mutating = (
                            method in write_methods
                            and not query_like
                            and not any(h in path for h in ("list", "page", "search"))
                        )
                        if mutating and verify.get("mode") == "self":
                            errors.append(
                                f"functional {cid} mutating write must use verify_read after write, not mode=self"
                            )
                        if mutating and not verify.get("id_from"):
                            errors.append(
                                f"functional {cid} verify_read.id_from required for writes"
                            )
                    # 4) state_check policy
                    state_policy = case.get("state_policy")
                    state_check = case.get("state_check")
                    if state_policy not in {"required", "not_applicable", None}:
                        errors.append(
                            f"functional {cid} invalid state_policy={state_policy}"
                        )
                    path_name = f"{case.get('path') or ''}{case.get('name') or ''}".lower()
                    method = str(case.get("method") or "").upper()
                    # 状态查询（GET/list/byorder）不是状态变更写接口
                    status_query = method == "GET" or any(
                        h in path_name
                        for h in ("/list", "byorder", "by-order", "status/by", "的状态")
                    )
                    state_hints = (
                        "shelve",
                        "unshelve",
                        "enable",
                        "disable",
                        "上架",
                        "下架",
                        "启用",
                        "停用",
                    )
                    # 写方法路径含 /status 才视为状态变更；单独「状态」中文查询名不触发
                    write_status = method in write_methods and (
                        "/status" in path_name or any(h in path_name for h in state_hints)
                    )
                    if write_status and not status_query:
                        if state_policy != "required" or not isinstance(state_check, dict):
                            errors.append(
                                f"functional {cid} state-changing API requires state_policy=required and state_check"
                            )
                    elif state_policy is None and state_check is None:
                        errors.append(
                            f"functional {cid} missing state_policy=not_applicable (or state_check)"
                        )
                    # 5) test_marker + cleanup
                    if not case.get("test_marker"):
                        errors.append(f"functional {cid} missing test_marker")
                    cleanup = case.get("cleanup")
                    if not isinstance(cleanup, dict) or "enabled" not in cleanup:
                        errors.append(f"functional {cid} missing cleanup.enabled")
                    elif cleanup.get("enabled") and not cleanup.get("id_from"):
                        errors.append(
                            f"functional {cid} cleanup.enabled requires id_from"
                        )
                    # 6) independent / no order dependency
                    if case.get("independent") is not True:
                        errors.append(f"functional {cid} independent must be true")
                    deps = case.get("depends_on")
                    if deps not in ([], None):
                        errors.append(
                            f"functional {cid} depends_on must be [] (got {deps})"
                        )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            errors.append(f"coverage gate check failed: {error}")

    errors = list(dict.fromkeys(errors))
    if context.get("status") == "completed" and errors:
        errors.append("run-context status is completed but required artifacts are invalid")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()

    errors = validate(args.run_dir.resolve())
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("OK: api-test-suite run artifacts are complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
