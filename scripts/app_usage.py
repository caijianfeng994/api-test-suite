"""APP 调用三态解析与证据校验内核。"""

from __future__ import annotations

import json
from pathlib import Path

VALID_APP_USAGE_STATUSES = {
    "called_by_app",
    "not_called_by_app",
    "unknown",
}

REQUIRED_RUNTIME_LIST_FIELDS = (
    "hostnames",
    "app_from_values",
    "client_types",
    "client_versions",
    "uids",
)


def normalize_app_usage_status(iface: dict) -> str:
    app_usage = iface.get("app_usage")
    if not isinstance(app_usage, dict):
        return "unknown"
    status = str(app_usage.get("status") or "").strip()
    return status if status in VALID_APP_USAGE_STATUSES else "unknown"


def should_generate_auth(iface: dict) -> bool:
    return normalize_app_usage_status(iface) != "not_called_by_app"


def _endpoint_error(endpoint_id: str, message: str) -> str:
    return f"endpoint {endpoint_id} app usage {message}"


def _is_meaningful_code_evidence(entry: object) -> bool:
    if isinstance(entry, dict):
        return any(str(value or "").strip() for value in entry.values())
    return bool(str(entry or "").strip())


def _has_code_evidence(value: object) -> bool:
    if not isinstance(value, list):
        return False
    return any(_is_meaningful_code_evidence(entry) for entry in value)


def _parse_count(value: object) -> int | None:
    """计数字段只接受真正的整数，bool 与字符串都视为不可用。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _unique_nonempty_repos(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    for item in value:
        repo = str(item or "").strip()
        if not repo or repo in seen:
            continue
        seen.add(repo)
        ordered.append(repo)
    return ordered


def _code_evidence_kinds(code_evidence: object) -> list[dict]:
    if not isinstance(code_evidence, list):
        return []
    return [entry for entry in code_evidence if isinstance(entry, dict)]


def validate_app_usage_evidence(run_dir: Path, catalog: dict) -> list[str]:
    """只校验 catalog 中声明 not_called_by_app 的接口。"""
    catalog_interfaces = catalog.get("interfaces") if isinstance(catalog, dict) else None
    if not isinstance(catalog_interfaces, list):
        catalog_interfaces = []

    targets = [
        iface
        for iface in catalog_interfaces
        if isinstance(iface, dict) and normalize_app_usage_status(iface) == "not_called_by_app"
    ]
    if not targets:
        return []

    errors: list[str] = []
    evidence_path = Path(run_dir) / "app-usage-evidence.json"
    if not evidence_path.is_file():
        for iface in targets:
            endpoint_id = str(iface.get("id") or "").strip() or "<unknown>"
            errors.append(_endpoint_error(endpoint_id, "evidence file missing"))
        return errors

    try:
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        for iface in targets:
            endpoint_id = str(iface.get("id") or "").strip() or "<unknown>"
            errors.append(_endpoint_error(endpoint_id, f"evidence file invalid: {exc}"))
        return errors

    if not isinstance(payload, dict):
        for iface in targets:
            endpoint_id = str(iface.get("id") or "").strip() or "<unknown>"
            errors.append(_endpoint_error(endpoint_id, "evidence root must be an object"))
        return errors

    evidence_interfaces = payload.get("interfaces")
    if not isinstance(evidence_interfaces, list):
        for iface in targets:
            endpoint_id = str(iface.get("id") or "").strip() or "<unknown>"
            errors.append(_endpoint_error(endpoint_id, "evidence interfaces must be a list"))
        return errors

    by_endpoint: dict[str, list[dict]] = {}
    for item in evidence_interfaces:
        if not isinstance(item, dict):
            continue
        endpoint_id = str(item.get("endpoint_id") or "").strip()
        if not endpoint_id:
            continue
        by_endpoint.setdefault(endpoint_id, []).append(item)

    for iface in targets:
        endpoint_id = str(iface.get("id") or "").strip() or "<unknown>"
        matches = by_endpoint.get(endpoint_id, [])
        if not matches:
            errors.append(_endpoint_error(endpoint_id, "evidence entry missing"))
            continue
        if len(matches) > 1:
            errors.append(_endpoint_error(endpoint_id, "evidence entry not unique"))
            continue

        entry = matches[0]
        entry_status = str(entry.get("status") or "").strip()
        catalog_status = normalize_app_usage_status(iface)
        if entry_status != catalog_status:
            errors.append(_endpoint_error(endpoint_id, "status conflicts with evidence"))

        if entry.get("coverage_complete") is not True:
            errors.append(_endpoint_error(endpoint_id, "coverage_complete must be true"))

        covered_repos = _unique_nonempty_repos(entry.get("covered_app_repos"))
        if len(covered_repos) < 2:
            errors.append(
                _endpoint_error(
                    endpoint_id,
                    "covered_app_repos must contain at least 2 unique non-empty repos",
                )
            )

        code_evidence = entry.get("code_evidence")
        if not _has_code_evidence(code_evidence):
            errors.append(_endpoint_error(endpoint_id, "code evidence missing"))

        evidence_dicts = _code_evidence_kinds(code_evidence)
        if any(str(item.get("kind") or "").strip() == "app_repo_hit" for item in evidence_dicts):
            errors.append(
                _endpoint_error(endpoint_id, "app_repo_hit conflicts with not_called_by_app")
            )

        miss_repos = {
            str(item.get("repo") or "").strip()
            for item in evidence_dicts
            if str(item.get("kind") or "").strip() == "app_repo_miss"
            and str(item.get("repo") or "").strip()
        }
        for repo in covered_repos:
            if repo not in miss_repos:
                errors.append(
                    _endpoint_error(endpoint_id, f"app_repo_miss missing for covered repo: {repo}")
                )

        runtime_evidence = entry.get("runtime_evidence")
        if not isinstance(runtime_evidence, dict):
            errors.append(_endpoint_error(endpoint_id, "runtime evidence missing"))
            continue

        time_range = runtime_evidence.get("time_range")
        if not isinstance(time_range, dict):
            errors.append(_endpoint_error(endpoint_id, "time_range missing"))
        else:
            start = str(time_range.get("start") or "").strip()
            end = str(time_range.get("end") or "").strip()
            if not start or not end:
                errors.append(_endpoint_error(endpoint_id, "time_range start/end must be non-empty"))

        matched_requests = _parse_count(runtime_evidence.get("matched_requests"))
        if matched_requests is None or matched_requests <= 0:
            errors.append(_endpoint_error(endpoint_id, "matched_requests must be greater than zero"))

        app_identified_requests = _parse_count(runtime_evidence.get("app_identified_requests"))
        if app_identified_requests != 0:
            errors.append(_endpoint_error(endpoint_id, "app_identified_requests must equal zero"))

        for field in REQUIRED_RUNTIME_LIST_FIELDS:
            if not isinstance(runtime_evidence.get(field), list):
                errors.append(
                    _endpoint_error(
                        endpoint_id, f"runtime evidence missing list field: {field}"
                    )
                )

    return errors
