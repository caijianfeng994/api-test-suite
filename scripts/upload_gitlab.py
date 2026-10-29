#!/usr/bin/env python3
"""Upload api-test-suite cases/scripts to GitLab api/<service>/, merging into existing cases."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from internal_http import open_url  # noqa: E402

PROJECT_PATH = "backsys-testgroup/mstrategysddtestcase"
GIT_SSH = "git@gitlab.yc345.tv:backsys-testgroup/mstrategysddtestcase.git"
GIT_HTTPS = "https://gitlab.yc345.tv/backsys-testgroup/mstrategysddtestcase.git"
WEB_BASE = "https://gitlab.yc345.tv/backsys-testgroup/mstrategysddtestcase"
API_BASE = "https://gitlab.yc345.tv/api/v4"
API_ROOT = "api"
DEFAULT_BRANCH = "main"
TZ = timezone(timedelta(hours=8))

DOC_FILES = [
    "test-cases.json",
    "test-cases.md",
    "traffic-ignore.json",
    "regression-cases.json",
    "regression-cases.md",
    "regression-fixtures.json",
    "regression-promotion.json",
    "backlog-cases.md",
    "test-plan.md",
    "api-test-report.md",
    "code-validation-evidence.json",
    "interface-catalog.md",
    "contract-analysis.md",
    "traceability-matrix.md",
]
SCRIPT_FILES = [
    "package.json",
    "package-lock.json",
    ".env.example",
    "playwright.config.ts",
    "tests/api-cases.spec.ts",
]
CASE_JSON = "test-cases.json"
CASE_MD = "test-cases.md"
REGRESSION_JSON = "regression-cases.json"
REGRESSION_MD = "regression-cases.md"
REGRESSION_FIXTURES = "regression-fixtures.json"


class StagingValidationError(RuntimeError):
    """Raised when merged staging content is inconsistent or sensitive."""


def now_iso() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def load_context(run_dir: Path) -> dict:
    path = run_dir / "run-context.json"
    if not path.is_file():
        raise SystemExit(f"missing {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_service_name(context: dict, override: str | None) -> str:
    if override and override.strip():
        raw = override.strip()
    else:
        raw = context.get("service_name") or context.get("service") or context.get("slug") or ""
        if isinstance(raw, dict):
            raw = raw.get("name") or raw.get("service_name") or ""
        if not raw:
            for item in context.get("input_sources") or []:
                if isinstance(item, dict) and item.get("type") in {"codewiki", "gitlab", "service"}:
                    raw = item.get("reference") or item.get("service_name") or ""
                    if raw:
                        break
        raw = str(raw or "unknown")
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in raw).strip(".-")
    return safe or "unknown"


def remote_dir_for(service: str) -> str:
    return f"{API_ROOT}/{service}"


def list_doc_files(run_dir: Path) -> list[str]:
    return [
        name
        for name in DOC_FILES
        if (run_dir / name).is_file() and (run_dir / name).stat().st_size > 0
    ]


def list_script_files(run_dir: Path) -> list[str]:
    return [name for name in SCRIPT_FILES if (run_dir / name).is_file()]


def write_result(run_dir: Path, payload: dict) -> None:
    (run_dir / "gitlab-upload.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    ctx_path = run_dir / "run-context.json"
    if not ctx_path.is_file():
        return
    ctx = json.loads(ctx_path.read_text(encoding="utf-8"))
    stages = ctx.setdefault("stages", {})
    stages["gitlab_upload"] = "completed" if payload.get("ok") else "blocked"
    artifacts = ctx.setdefault("artifacts", {})
    artifacts["gitlab-upload.json"] = "gitlab-upload.json"
    if payload.get("ok"):
        ctx["gitlab"] = {
            "projectPath": payload.get("projectPath"),
            "branch": payload.get("branch"),
            "directoryPath": payload.get("directoryPath"),
            "serviceName": payload.get("serviceName"),
            "commitId": payload.get("commitId"),
            "treeUrl": payload.get("treeUrl"),
            "updatedInPlace": payload.get("updatedInPlace"),
            "mergeStats": payload.get("mergeStats"),
        }
    ctx_path.write_text(json.dumps(ctx, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=str(cwd), check=check, text=True, capture_output=True)


def gitlab_token() -> str:
    return (os.environ.get("GITLAB_PRIVATE_TOKEN") or os.environ.get("GITLAB_TOKEN") or "").strip()


def https_remote() -> str:
    tok = gitlab_token()
    if tok:
        return (
            "https://oauth2:"
            f"{urllib.parse.quote(tok, safe='')}@gitlab.yc345.tv/"
            "backsys-testgroup/mstrategysddtestcase.git"
        )
    return GIT_HTTPS


def ensure_clone(cache: Path) -> Path:
    cache.parent.mkdir(parents=True, exist_ok=True)
    if (cache / ".git").is_dir():
        run_git(["git", "remote", "set-url", "origin", GIT_SSH], cache, check=False)
        fetched = run_git(["git", "fetch", "--all", "--prune"], cache, check=False)
        if fetched.returncode == 0:
            return cache
        run_git(["git", "remote", "set-url", "origin", https_remote()], cache, check=False)
        fetched = run_git(["git", "fetch", "--all", "--prune"], cache, check=False)
        if fetched.returncode != 0:
            raise RuntimeError(fetched.stderr or fetched.stdout or "git fetch failed")
        return cache
    if cache.exists():
        shutil.rmtree(cache)
    cloned = subprocess.run(["git", "clone", GIT_SSH, str(cache)], text=True, capture_output=True)
    if cloned.returncode != 0:
        cloned = subprocess.run(
            ["git", "clone", https_remote(), str(cache)],
            text=True,
            capture_output=True,
        )
    if cloned.returncode != 0:
        raise RuntimeError(cloned.stderr or cloned.stdout or "git clone failed")
    return cache


def detect_base(repo: Path) -> str:
    refs = run_git(["git", "ls-remote", "--heads", "origin", "main", "cby_spec"], repo, check=False)
    text = refs.stdout or ""
    if "refs/heads/main" in text:
        return "main"
    if "refs/heads/cby_spec" in text:
        return "cby_spec"
    return DEFAULT_BRANCH


def checkout_target_branch(repo: Path, branch: str, base: str) -> None:
    run_git(["git", "fetch", "origin"], repo, check=False)
    if branch == base:
        run_git(["git", "checkout", "-B", branch, f"origin/{base}"], repo)
        run_git(["git", "pull", "--ff-only", "origin", base], repo, check=False)
        return
    remote_has = run_git(["git", "ls-remote", "--heads", "origin", branch], repo, check=False)
    if remote_has.returncode == 0 and branch in (remote_has.stdout or ""):
        run_git(["git", "checkout", "-B", branch, f"origin/{branch}"], repo)
        run_git(["git", "pull", "--rebase", "origin", branch], repo, check=False)
        return
    run_git(["git", "checkout", "-B", branch, f"origin/{base}"], repo)


def case_key(case: dict) -> tuple[str, str]:
    case_id = str(case.get("id") or "").strip()
    if case_id:
        return ("id", case_id)
    method = str(case.get("method") or "").upper()
    path = str(case.get("path") or case.get("target_path") or "").rstrip("/")
    category = str(case.get("category") or "")
    name = str(case.get("name") or "")
    return ("sig", f"{method}|{path}|{category}|{name}")


def load_cases_payload(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return {"cases": data, "case_count": len(data)}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be object or array")
    cases = data.get("cases")
    if not isinstance(cases, list):
        raise ValueError(f"{path} missing cases array")
    return data


def load_ignored_endpoint_ids(run_dir: Path) -> set[str]:
    path = run_dir / "traffic-ignore.json"
    if not path.is_file():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    interfaces = data.get("interfaces") if isinstance(data, dict) else None
    if not isinstance(interfaces, list):
        raise ValueError(f"{path} missing interfaces array")
    return {
        str(item.get("endpoint_id") or "").strip()
        for item in interfaces
        if isinstance(item, dict) and item.get("decision") == "ignore"
    } - {""}


def filter_ignored_endpoints(payload: dict, endpoint_ids: set[str]) -> tuple[dict, int]:
    if not endpoint_ids:
        return payload, 0
    cases = [item for item in payload.get("cases") or [] if isinstance(item, dict)]
    kept = [
        item
        for item in cases
        if str(item.get("endpoint_id") or "").strip() not in endpoint_ids
    ]
    return {**payload, "cases": kept, "case_count": len(kept)}, len(cases) - len(kept)


def merge_case_payloads(existing: dict, incoming: dict) -> tuple[dict, dict]:
    """Merge remote existing cases with incoming; same id/signature -> update, else append."""
    existing_cases = [c for c in (existing.get("cases") or []) if isinstance(c, dict)]
    incoming_cases = [c for c in (incoming.get("cases") or []) if isinstance(c, dict)]

    ordered_keys: list[tuple[str, str]] = []
    by_key: dict[tuple[str, str], dict] = {}
    for case in existing_cases:
        key = case_key(case)
        if key not in by_key:
            ordered_keys.append(key)
        by_key[key] = case

    remote_only = len(by_key)
    added = 0
    updated = 0
    for case in incoming_cases:
        key = case_key(case)
        if key in by_key:
            by_key[key] = case
            updated += 1
        else:
            ordered_keys.append(key)
            by_key[key] = case
            added += 1

    merged_cases = [by_key[key] for key in ordered_keys]
    cats = Counter(str(c.get("category") or "unknown") for c in merged_cases)
    kept_remote_only = max(remote_only - updated, 0)
    # remote_only before incoming: keys that remain unchanged from remote
    incoming_keys = {case_key(c) for c in incoming_cases}
    unchanged_remote = sum(1 for key in ordered_keys if key not in incoming_keys)

    merged = {
        **{k: v for k, v in incoming.items() if k != "cases"},
        "generated_at": now_iso(),
        "merged_from_remote": True,
        "case_count": len(merged_cases),
        "category_counts": dict(cats),
        "coverage_policy": incoming.get("coverage_policy")
        or existing.get("coverage_policy")
        or "full_no_default_smoke",
        "cases": merged_cases,
    }
    stats = {
        "remote_before": len(existing_cases),
        "incoming": len(incoming_cases),
        "added": added,
        "updated": updated,
        "unchanged_remote": unchanged_remote,
        "total": len(merged_cases),
    }
    return merged, stats


def _is_promotable(case: dict) -> bool:
    return (
        case.get("regression_ready") is True
        and case.get("execution_status") == "passed"
        and not case.get("regression_blockers")
    )


def _fixture_keys_for_case(case: dict) -> set[str]:
    endpoint_id = str(case.get("endpoint_id") or "")
    keys: set[str] = set()
    for trace in case.get("fixture_trace") or []:
        if not isinstance(trace, dict):
            continue
        source = str(trace.get("source") or "")
        for prefix in ("regression-fixtures.json#", "fixtures.json#"):
            source = source.removeprefix(prefix)
        if source.startswith("shared."):
            keys.add(source)
        elif source.startswith("endpoint_bindings."):
            tail = source.removeprefix("endpoint_bindings.")
            keys.add(tail if "." in tail else f"{endpoint_id}.{tail}")
        elif source and not source.startswith(
            ("generated:", "yapi.", "yapi:", "proto.", "proto:", "contract.", "contract:", "db:", "api:", "get:")
        ):
            keys.add(source)
        ref = str(trace.get("fixture_ref") or trace.get("ref") or "").strip()
        for prefix in ("regression-fixtures.json#", "fixtures.json#"):
            ref = ref.removeprefix(prefix)
        if ref:
            keys.add(ref)
    return keys


def merge_regression_payloads(existing: dict, incoming: dict) -> tuple[dict, dict]:
    """Merge a regression set without allowing failed or incomplete downgrades."""
    existing_cases = [c for c in (existing.get("cases") or []) if isinstance(c, dict)]
    incoming_cases = [c for c in (incoming.get("cases") or []) if isinstance(c, dict)]
    ordered_keys: list[tuple[str, str]] = []
    by_key: dict[tuple[str, str], dict] = {}
    legacy_by_key: dict[tuple[str, str], dict] = {}
    for item in existing_cases:
        key = case_key(item)
        if _is_promotable(item):
            if key not in by_key:
                ordered_keys.append(key)
            by_key[key] = item
        else:
            legacy_by_key[key] = item

    promoted = 0
    rejected_incoming = 0
    successful_replacements: set[tuple[str, str]] = set()
    incoming_keys: set[tuple[str, str]] = set()
    for item in incoming_cases:
        key = case_key(item)
        incoming_keys.add(key)
        if not _is_promotable(item):
            rejected_incoming += 1
            continue
        if key not in by_key:
            ordered_keys.append(key)
        by_key[key] = item
        successful_replacements.add(key)
        legacy_by_key.pop(key, None)
        promoted += 1

    protected_keys = {
        case_key(item) for item in existing_cases if _is_promotable(item)
    } - successful_replacements
    protected_fixture_keys: set[str] = set()
    for key in protected_keys:
        protected_fixture_keys.update(_fixture_keys_for_case(by_key[key]))

    legacy_cases: list[dict] = []
    for item in legacy_by_key.values():
        blockers = list(item.get("regression_blockers") or [])
        if "legacy_unverified_remote" not in blockers:
            blockers.append("legacy_unverified_remote")
        legacy_cases.append(
            {
                **item,
                "regression_ready": False,
                "execution_status": "legacy_unverified_remote",
                "regression_blockers": blockers,
            }
        )

    merged_cases = [by_key[key] for key in ordered_keys]
    merged = {
        **{k: v for k, v in incoming.items() if k not in {"cases", "case_count"}},
        "generated_at": now_iso(),
        "merged_from_remote": bool(existing_cases),
        "case_count": len(merged_cases),
        "cases": merged_cases,
    }
    return merged, {
        "remote_before": len(existing_cases),
        "incoming": len(incoming_cases),
        "promoted": promoted,
        "protected_remote": len(protected_keys),
        "legacy_quarantined": len(legacy_cases),
        "rejected_incoming": rejected_incoming,
        "unchanged_remote": sum(1 for key in ordered_keys if key not in incoming_keys),
        "total": len(merged_cases),
        "_protected_fixture_keys": sorted(protected_fixture_keys),
        "_legacy_cases": legacy_cases,
    }


def merge_regression_fixtures(
    existing: dict,
    incoming: dict,
    merged_regressions: dict,
    protected_fixture_keys: list[str] | None = None,
) -> dict:
    """Union fixture data so protected remote regression cases remain replayable."""
    remote_fixtures = existing.get("fixtures") or {}
    incoming_fixtures = incoming.get("fixtures") or {}
    if not isinstance(remote_fixtures, dict) or not isinstance(incoming_fixtures, dict):
        raise ValueError("regression-fixtures.json fixtures must be an object")
    fixtures = {**remote_fixtures, **incoming_fixtures}
    for key in protected_fixture_keys or []:
        if key in remote_fixtures:
            fixtures[key] = remote_fixtures[key]
    remote_rejected = existing.get("rejected") or {}
    incoming_rejected = incoming.get("rejected") or {}
    rejected = {
        **(remote_rejected if isinstance(remote_rejected, dict) else {}),
        **(incoming_rejected if isinstance(incoming_rejected, dict) else {}),
    }
    return {
        **{k: v for k, v in incoming.items() if k not in {"fixtures", "fixture_count", "rejected"}},
        "generated_at": now_iso(),
        "merged_from_remote": bool(remote_fixtures),
        "fixture_count": len(fixtures),
        "fixtures": fixtures,
        "rejected": rejected,
        "regression_case_count": len(merged_regressions.get("cases") or []),
    }


def _load_validate_run():
    path = Path(__file__).resolve().parent / "validate_run.py"
    spec = importlib.util.spec_from_file_location("api_test_suite_validate_run", path)
    module = importlib.util.module_from_spec(spec)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load {path}")
    spec.loader.exec_module(module)
    return module


def preflight_run(run_dir: Path) -> list[str]:
    """Run validate_run's complete checks, excluding its post-upload receipt gate."""
    validator = _load_validate_run()
    errors = validator.validate(run_dir)
    upload_only = (
        "missing: gitlab-upload.json",
        "empty: gitlab-upload.json",
        "gitlab-upload.json ok is not true",
        "gitlab-upload.json mock=true",
    )
    remaining = [
        error
        for error in errors
        if not error.startswith(upload_only)
        and error != "run-context status is completed but required artifacts are invalid"
    ]
    if remaining and "run-context status is completed but required artifacts are invalid" in errors:
        remaining.append("run-context status is completed but required artifacts are invalid")
    if (run_dir / REGRESSION_JSON).is_file():
        for relative in SCRIPT_FILES:
            path = run_dir / relative
            if not path.is_file() or path.stat().st_size == 0:
                remaining.append(f"missing independent project file: {relative}")
    return list(dict.fromkeys(remaining))


def validate_staging(staging_dir: Path) -> list[str]:
    """Second-pass consistency and sensitive-data validation on merged content."""
    validator = _load_validate_run()
    errors: list[str] = []
    parsed: dict[str, dict] = {}
    for name, count_key, items_key in (
        (CASE_JSON, "case_count", "cases"),
        (REGRESSION_JSON, "case_count", "cases"),
        (REGRESSION_FIXTURES, "fixture_count", "fixtures"),
    ):
        path = staging_dir / name
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            errors.append(f"{name} invalid JSON: {error}")
            continue
        if not isinstance(data, dict):
            errors.append(f"{name} must be an object")
            continue
        parsed[name] = data
        items = data.get(items_key)
        actual = len(items) if isinstance(items, (list, dict)) else -1
        if data.get(count_key) != actual:
            errors.append(
                f"{name} {count_key} ({data.get(count_key)!r}) != actual ({actual})"
            )
        if name == REGRESSION_JSON and isinstance(items, list):
            seen: set[tuple[str, str]] = set()
            for item in items:
                if not isinstance(item, dict) or not _is_promotable(item):
                    errors.append(
                        f"{name} contains non-promotable case: "
                        f"{item.get('id') if isinstance(item, dict) else '<invalid>'}"
                    )
                    continue
                key = case_key(item)
                if key in seen:
                    errors.append(f"{name} contains duplicate case: {key[1]}")
                seen.add(key)

    regression = parsed.get(REGRESSION_JSON) or {}
    fixture_payload = parsed.get(REGRESSION_FIXTURES) or {}
    fixtures = fixture_payload.get("fixtures") or {}
    if isinstance(fixtures, dict):
        for item in regression.get("cases") or []:
            if not isinstance(item, dict):
                continue
            for trace in item.get("fixture_trace") or []:
                if not isinstance(trace, dict):
                    continue
                source = str(trace.get("source") or "")
                resolved_by_actual_key = bool(_fixture_keys_for_case(item) & set(fixtures))
                if source and not (
                    resolved_by_actual_key
                    or validator._fixture_source_resolves(
                        source, str(item.get("endpoint_id") or ""), fixtures
                    )
                ):
                    errors.append(
                        f"{REGRESSION_JSON} fixture ref unresolved for "
                        f"{item.get('id')}: {source}"
                    )

    for path in [
        *(staging_dir / name for name in DOC_FILES),
        *(staging_dir / name for name in SCRIPT_FILES),
        staging_dir / "README.md",
    ]:
        if not path.is_file():
            continue
        relative = str(path.relative_to(staging_dir))
        try:
            if path.suffix == ".json":
                matches = validator.sensitive_paths(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            else:
                matches = [
                    f"line {number}"
                    for number, line in enumerate(
                        path.read_text(encoding="utf-8").splitlines(), 1
                    )
                    if validator._contains_sensitive_literal(line)
                ]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            errors.append(f"{relative} sensitive scan failed: {error}")
            continue
        errors.extend(f"{relative} contains sensitive value: {match}" for match in matches)
    return list(dict.fromkeys(errors))


def render_cases_markdown(json_path: Path, md_path: Path) -> None:
    renderer = Path(__file__).resolve().parent / "render_test_cases_table.py"
    if not renderer.is_file():
        # minimal fallback
        data = load_cases_payload(json_path)
        cases = data.get("cases") or []
        lines = [
            "# 接口测试用例表",
            "",
            f"- 用例总数：`{len(cases)}`",
            "",
            "## 总览",
            "",
            "| 编号 | Method | Path | 类型 | 用例名称 | 可执行 |",
            "|-|-|-|-|-|-|",
        ]
        for case in cases:
            if not isinstance(case, dict):
                continue
            lines.append(
                f"| {case.get('id','')} | {case.get('method','')} | {case.get('path','')} | "
                f"{case.get('category','')} | {case.get('name','')} | "
                f"{'是' if case.get('executable', True) else '否'} |"
            )
        md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return
    result = subprocess.run(
        [sys.executable, str(renderer), str(json_path), "--output", str(md_path)],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout or "render_test_cases_table failed")


def render_regression_markdown(payload: dict, path: Path) -> None:
    cases = [item for item in (payload.get("cases") or []) if isinstance(item, dict)]
    lines = [
        "# 已晋升业务回归用例",
        "",
        f"- 晋升用例数：{len(cases)}",
        "",
        "| ID | Method | Path | Category | Name |",
        "|-|-|-|-|-|",
    ]
    for item in cases:
        lines.append(
            f"| {item.get('id', '')} | {item.get('method', '')} | "
            f"{item.get('path', '')} | {item.get('category', '')} | "
            f"{item.get('name', '')} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_readme(service: str, context: dict, run_dir: Path, merge_stats: dict | None) -> str:
    stats_lines: list[str] = []
    if merge_stats:
        stats_lines = [
            "",
            "## 最近一次合并",
            "",
            f"- 远程原有：`{merge_stats.get('remote_before', 0)}`",
            f"- 本次新增：`{merge_stats.get('added', 0)}`",
            f"- 本次更新：`{merge_stats.get('updated', 0)}`",
            f"- 远程保留未变：`{merge_stats.get('unchanged_remote', 0)}`",
            f"- 合并后总数：`{merge_stats.get('total', 0)}`",
            f"- protected_remote：`{merge_stats.get('protected_remote', 0)}`",
            f"- promoted：`{merge_stats.get('promoted', 0)}`",
            f"- legacy_quarantined：`{merge_stats.get('legacy_quarantined', 0)}`",
            f"- regression_total：`{merge_stats.get('regression_total', 0)}`",
        ]
    return "\n".join(
        [
            f"# {service} 接口测试",
            "",
            f"- GitLab 服务目录：`api/{service}/`",
            "- suite: `api-test-suite`",
            f"- test_kind: `{context.get('test_kind')}`",
            f"- run_mode: `{context.get('run_mode')}`",
            f"- 最近一次 run_id: `{context.get('run_id')}`",
            f"- 更新时间: {now_iso()}",
            "",
            "## 产物",
            "",
            "- **完整用例（权威）**：`test-cases.md` / `test-cases.json`",
            "- **已验证回归集**：`regression-cases.md` / `regression-cases.json`",
            "- 回归 fixture：`regression-fixtures.json`",
            "- 计划/报告：`test-plan.md`、`api-test-report.md`（若有）",
            "- 独立工程：`package.json`、`package-lock.json`、`.env.example`、"
            "`playwright.config.ts`、`tests/api-cases.spec.ts`",
            "",
            "## 合并策略",
            "",
            "- 同服务再次上传时：**补充/更新**到已有 `test-cases.*`，不丢弃远程已有用例",
            "- 相同 `id`（或 method+path+category+name）用本次内容覆盖；新 id 追加",
            "- `tests/*.spec.ts`：同名覆盖，远程独有脚本保留",
            *stats_lines,
            "",
            "## 本地执行",
            "",
            "```bash",
            "npm ci && npx playwright test",
            "```",
            "",
        ]
    )


def merge_scripts(run_dir: Path, dest: Path, script_files: list[str]) -> list[str]:
    """Overwrite/add incoming scripts; keep remote-only tests/*.ts."""
    uploaded: list[str] = []
    for rel in script_files:
        src = run_dir / rel
        if not src.is_file():
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        uploaded.append(rel)
    return uploaded


def _append_legacy_backlog(path: Path, legacy_cases: list[dict]) -> None:
    if not legacy_cases:
        return
    existing = path.read_text(encoding="utf-8") if path.is_file() else "# 回归 backlog\n"
    lines = [
        existing.rstrip(),
        "",
        "## 远程老格式隔离",
        "",
        "以下用例缺少可验证的 `regression_ready=true` / `execution_status=passed`，"
        "已从回归集移出。请重新执行并完成晋升后再上传：",
        "",
    ]
    for item in legacy_cases:
        lines.append(f"- `{item.get('id') or '<missing-id>'}`：`legacy_unverified_remote`")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_staging_payload(
    run_dir: Path,
    dest: Path,
    doc_files: list[str],
    script_files: list[str],
    context: dict,
    service: str,
) -> tuple[bool, list[str], dict]:
    existed = dest.is_dir()
    dest.mkdir(parents=True, exist_ok=True)
    uploaded: list[str] = []
    merge_stats: dict = {
        "remote_before": 0,
        "incoming": 0,
        "added": 0,
        "updated": 0,
        "unchanged_remote": 0,
        "total": 0,
        "mode": "create",
    }

    incoming_json = run_dir / CASE_JSON
    if not incoming_json.is_file():
        raise SystemExit(f"missing {incoming_json}")

    remote_json = dest / CASE_JSON
    incoming_payload = load_cases_payload(incoming_json)
    ignored_endpoint_ids = load_ignored_endpoint_ids(run_dir)
    incoming_full_keys = {
        case_key(item)
        for item in incoming_payload.get("cases") or []
        if isinstance(item, dict)
    }
    if remote_json.is_file() and remote_json.stat().st_size > 0:
        existing_payload = load_cases_payload(remote_json)
        existing_payload, ignored_remote_cases = filter_ignored_endpoints(
            existing_payload, ignored_endpoint_ids
        )
        merged_payload, merge_stats = merge_case_payloads(existing_payload, incoming_payload)
        merge_stats["ignored_remote_removed"] = ignored_remote_cases
        merge_stats["mode"] = "merge"
    else:
        merged_payload = {
            **incoming_payload,
            "generated_at": now_iso(),
            "merged_from_remote": False,
            "case_count": len(incoming_payload.get("cases") or []),
        }
        cases = [c for c in (merged_payload.get("cases") or []) if isinstance(c, dict)]
        remote_before = 0
        if remote_json.is_file() and remote_json.stat().st_size > 0:
            try:
                remote_before = len(load_cases_payload(remote_json).get("cases") or [])
            except Exception:
                remote_before = 0
        merge_stats = {
            "remote_before": remote_before,
            "incoming": len(cases),
            "added": len(cases),
            "updated": 0,
            "unchanged_remote": 0,
            "total": len(cases),
            "mode": "create",
        }
        merged_payload["category_counts"] = dict(
            Counter(str(c.get("category") or "unknown") for c in cases)
        )

    (dest / CASE_JSON).write_text(
        json.dumps(merged_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    render_cases_markdown(dest / CASE_JSON, dest / CASE_MD)
    uploaded.extend([CASE_JSON, CASE_MD])

    incoming_regression_path = run_dir / REGRESSION_JSON
    remote_regression_path = dest / REGRESSION_JSON
    merged_regressions: dict | None = None
    regression_stats: dict = {
        "promoted": 0,
        "protected_remote": 0,
        "legacy_quarantined": 0,
        "rejected_incoming": 0,
    }
    protected_fixture_keys: list[str] = []
    legacy_cases: list[dict] = []
    if incoming_regression_path.is_file() or remote_regression_path.is_file():
        incoming_regressions = (
            load_cases_payload(incoming_regression_path)
            if incoming_regression_path.is_file()
            else {"cases": [], "case_count": 0}
        )
        existing_regressions = (
            load_cases_payload(remote_regression_path)
            if remote_regression_path.is_file()
            else {"cases": [], "case_count": 0}
        )
        existing_regressions, ignored_remote_regressions = filter_ignored_endpoints(
            existing_regressions, ignored_endpoint_ids
        )
        merged_regressions, regression_stats = merge_regression_payloads(
            existing_regressions, incoming_regressions
        )
        regression_stats["ignored_remote_removed"] = ignored_remote_regressions
        protected_fixture_keys = regression_stats.pop("_protected_fixture_keys", [])
        legacy_cases = regression_stats.pop("_legacy_cases", [])
        remote_regression_path.write_text(
            json.dumps(merged_regressions, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        render_regression_markdown(merged_regressions, dest / REGRESSION_MD)
        uploaded.extend([REGRESSION_JSON, REGRESSION_MD])

    if legacy_cases:
        legacy_by_key = {case_key(item): item for item in legacy_cases}
        rewritten_cases: list[dict] = []
        found_keys: set[tuple[str, str]] = set()
        for item in merged_payload.get("cases") or []:
            key = case_key(item)
            found_keys.add(key)
            if key in legacy_by_key and key not in incoming_full_keys:
                rewritten_cases.append(legacy_by_key[key])
            else:
                rewritten_cases.append(item)
        rewritten_cases.extend(
            item for key, item in legacy_by_key.items() if key not in found_keys
        )
        merged_payload["cases"] = rewritten_cases
        merged_payload["case_count"] = len(merged_payload["cases"])
        merged_payload["category_counts"] = dict(
            Counter(str(item.get("category") or "unknown") for item in merged_payload["cases"])
        )
        (dest / CASE_JSON).write_text(
            json.dumps(merged_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        render_cases_markdown(dest / CASE_JSON, dest / CASE_MD)

    incoming_fixtures_path = run_dir / REGRESSION_FIXTURES
    remote_fixtures_path = dest / REGRESSION_FIXTURES
    if incoming_fixtures_path.is_file() or remote_fixtures_path.is_file():
        incoming_fixtures = (
            json.loads(incoming_fixtures_path.read_text(encoding="utf-8"))
            if incoming_fixtures_path.is_file()
            else {}
        )
        existing_fixtures = (
            json.loads(remote_fixtures_path.read_text(encoding="utf-8"))
            if remote_fixtures_path.is_file()
            else {}
        )
        merged_fixtures = merge_regression_fixtures(
            existing_fixtures,
            incoming_fixtures,
            merged_regressions or {"cases": []},
            protected_fixture_keys if merged_regressions is not None else [],
        )
        remote_fixtures_path.write_text(
            json.dumps(merged_fixtures, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        uploaded.append(REGRESSION_FIXTURES)

    for name in doc_files:
        if name in {
            CASE_JSON,
            CASE_MD,
            REGRESSION_JSON,
            REGRESSION_MD,
            REGRESSION_FIXTURES,
        }:
            continue
        shutil.copy2(run_dir / name, dest / name)
        uploaded.append(name)

    _append_legacy_backlog(dest / "backlog-cases.md", legacy_cases)
    if (dest / "backlog-cases.md").is_file():
        uploaded.append("backlog-cases.md")
    uploaded.extend(merge_scripts(run_dir, dest, script_files))

    merge_stats.update(
        {
            "protected_remote": regression_stats.get("protected_remote", 0),
            "promoted": regression_stats.get("promoted", 0),
            "legacy_quarantined": regression_stats.get("legacy_quarantined", 0),
            "rejected_regression_incoming": regression_stats.get("rejected_incoming", 0),
            "regression_total": regression_stats.get("total", 0),
        }
    )
    readme = build_readme(service, context, run_dir, merge_stats)
    (dest / "README.md").write_text(readme, encoding="utf-8")
    uploaded.append("README.md")
    complete_files = [
        relative
        for relative in [*DOC_FILES, *SCRIPT_FILES, "README.md"]
        if (dest / relative).is_file()
    ]
    return existed, list(dict.fromkeys(complete_files)), merge_stats


def copy_and_merge_payload(
    run_dir: Path,
    dest: Path,
    doc_files: list[str],
    script_files: list[str],
    context: dict,
    service: str,
) -> tuple[bool, list[str], dict]:
    """Build and validate in a temporary directory, then atomically publish."""
    existed = dest.is_dir()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{dest.name}-staging-", dir=dest.parent) as temp:
        staging = Path(temp) / dest.name
        if existed:
            shutil.copytree(dest, staging)
        else:
            staging.mkdir(parents=True)
        _, uploaded, merge_stats = _build_staging_payload(
            run_dir, staging, doc_files, script_files, context, service
        )
        staging_errors = validate_staging(staging)
        if staging_errors:
            raise StagingValidationError("; ".join(staging_errors))

        backup = Path(temp) / f"{dest.name}-backup"
        try:
            if existed:
                os.replace(dest, backup)
            os.replace(staging, dest)
        except Exception:
            if backup.exists() and not dest.exists():
                os.replace(backup, dest)
            raise

    for relative in uploaded:
        source = dest / relative
        if not source.is_file():
            continue
        target = run_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return existed, uploaded, merge_stats


def commit_and_push(repo: Path, dest_rel: str, branch: str, message: str) -> str:
    run_git(["git", "add", "-A", dest_rel], repo)
    status = run_git(["git", "status", "--porcelain", "--", dest_rel], repo)
    if not (status.stdout or "").strip():
        return run_git(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    run_git(["git", "commit", "-m", message], repo)
    sha = run_git(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    pushed = run_git(["git", "push", "-u", "origin", branch], repo, check=False)
    if pushed.returncode != 0:
        raise RuntimeError(pushed.stderr or pushed.stdout or "git push failed")
    return sha


def api_upsert(file_path: str, content: str, branch: str, message: str) -> dict:
    tok = gitlab_token()
    if not tok:
        raise RuntimeError("GITLAB_PRIVATE_TOKEN missing for Files API fallback")
    encoded_project = urllib.parse.quote(PROJECT_PATH, safe="")
    encoded_file = urllib.parse.quote(file_path, safe="")
    url = f"{API_BASE}/projects/{encoded_project}/repository/files/{encoded_file}"
    body = json.dumps(
        {
            "branch": branch,
            "content": content,
            "commit_message": message,
            "encoding": "text",
        }
    ).encode("utf-8")
    headers = {"PRIVATE-TOKEN": tok, "Content-Type": "application/json"}

    def _request(method: str) -> dict:
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        with open_url(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        return _request("POST")
    except urllib.error.HTTPError as error:
        if error.code not in {400, 409}:
            raise RuntimeError(error.read().decode("utf-8", errors="replace")) from error
        return _request("PUT")


def upload_via_api(
    run_dir: Path,
    dest_rel: str,
    files: list[str],
    branch: str,
    message: str,
) -> str:
    last_id = ""
    for rel in files:
        src = run_dir / rel
        if not src.is_file():
            continue
        result = api_upsert(f"{dest_rel}/{rel}", src.read_text(encoding="utf-8"), branch, message)
        last_id = str(result.get("commit_id") or result.get("file_path") or last_id)
    return last_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument(
        "--service-name",
        default="",
        help="GitLab 服务目录名，对应 api/<service>/；默认取 run-context.service_name 或 slug",
    )
    parser.add_argument("--branch", default="", help="目标分支，默认 main")
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--skip-push", action="store_true")
    parser.add_argument(
        "--replace-cases",
        action="store_true",
        help="已禁用；完整用例库强制增量合并",
    )
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    context = load_context(run_dir)
    service = resolve_service_name(context, args.service_name or None)
    docs = list_doc_files(run_dir)
    scripts = list_script_files(run_dir)
    if CASE_MD not in docs and not (run_dir / CASE_JSON).is_file():
        raise SystemExit("missing test-cases.md/json; refuse GitLab upload")

    cache = args.cache_dir or Path.home() / ".cache" / "api-test-suite" / "mstrategysddtestcase"
    dest_rel = remote_dir_for(service)
    message = f"test(api): 合并更新 {service} 接口用例与脚本（run {context.get('run_id')}）"

    payload: dict = {
        "ok": False,
        "generated_at": now_iso(),
        "projectPath": PROJECT_PATH,
        "branch": args.branch or DEFAULT_BRANCH,
        "serviceName": service,
        "directoryPath": dest_rel,
        "files": [],
        "commitId": "",
        "treeUrl": "",
        "updatedInPlace": False,
        "mergeStats": {},
        "mock": False,
        "via": "git",
        "message": "",
    }

    try:
        preflight_errors = preflight_run(run_dir)
    except Exception as error:  # noqa: BLE001
        preflight_errors = [f"validate_run error: {error}"]
    if preflight_errors:
        payload["message"] = "preflight validate_run failed: " + "; ".join(preflight_errors)
        write_result(run_dir, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1
    if args.replace_cases:
        payload["message"] = (
            "preflight policy failed: --replace-cases is disabled; "
            "the complete case library must use incremental merge"
        )
        write_result(run_dir, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1

    staged_ready = False
    try:
        repo = ensure_clone(cache)
        base = detect_base(repo)
        branch = (args.branch or base).strip() or base
        payload["branch"] = branch
        payload["treeUrl"] = f"{WEB_BASE}/-/tree/{branch}/{dest_rel}"

        checkout_target_branch(repo, branch, base)
        dest = repo / dest_rel
        existed, uploaded, merge_stats = copy_and_merge_payload(
            run_dir, dest, docs, scripts, context, service
        )
        staged_ready = True
        payload["updatedInPlace"] = existed
        payload["files"] = uploaded
        payload["mergeStats"] = merge_stats
        payload["message"] = (
            f"mode={merge_stats.get('mode')}: "
            f"+{merge_stats.get('added', 0)} / ~{merge_stats.get('updated', 0)} / "
            f"total={merge_stats.get('total', 0)}"
        )

        if args.skip_push:
            payload["ok"] = True
            payload["mock"] = True
            payload["message"] = f"skip-push: {payload['message']}"
            write_result(run_dir, payload)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        try:
            sha = commit_and_push(repo, dest_rel, branch, message)
            payload["ok"] = True
            payload["commitId"] = sha
            payload["treeUrl"] = f"{WEB_BASE}/-/tree/{branch}/{dest_rel}"
            write_result(run_dir, payload)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        except RuntimeError as push_error:
            if branch == base:
                feature = f"testcase/api-{service}"
                checkout_target_branch(repo, feature, base)
                dest = repo / dest_rel
                existed, uploaded, merge_stats = copy_and_merge_payload(
                    run_dir, dest, docs, scripts, context, service
                )
                staged_ready = True
                payload["branch"] = feature
                payload["updatedInPlace"] = existed
                payload["files"] = uploaded
                payload["mergeStats"] = merge_stats
                sha = commit_and_push(repo, dest_rel, feature, message)
                payload["ok"] = True
                payload["commitId"] = sha
                payload["treeUrl"] = f"{WEB_BASE}/-/tree/{feature}/{dest_rel}"
                payload["message"] = f"main push blocked, used branch {feature}: {push_error}"
                write_result(run_dir, payload)
                print(json.dumps(payload, ensure_ascii=False, indent=2))
                return 0
            raise
    except Exception as error:  # noqa: BLE001
        payload["via"] = "files_api"
        payload["message"] = str(error)
        if isinstance(error, StagingValidationError) or not staged_ready:
            payload["via"] = "git"
            write_result(run_dir, payload)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 1
        try:
            all_files = list(payload.get("files") or [])
            branch = payload["branch"] or DEFAULT_BRANCH
            sha = upload_via_api(dest, dest_rel, all_files, branch, message)
            payload["ok"] = True
            payload["commitId"] = sha
            payload["files"] = all_files
            payload["treeUrl"] = f"{WEB_BASE}/-/tree/{branch}/{dest_rel}"
            payload["message"] = f"git failed, used Files API: {error}"
            write_result(run_dir, payload)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        except Exception as api_error:  # noqa: BLE001
            payload["ok"] = False
            payload["message"] = f"git: {error}; api: {api_error}"
            write_result(run_dir, payload)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
