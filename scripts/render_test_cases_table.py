#!/usr/bin/env python3
"""Render api-test-suite test-cases.json into a Markdown table view."""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SENSITIVE_KEYS = {
    "authorization",
    "token",
    "access_token",
    "refresh_token",
    "password",
    "phone",
    "mobile",
    "id_card",
    "idcard",
}


def load_cases(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return {"cases": data, "case_count": len(data)}, data
    if not isinstance(data, dict):
        raise ValueError("test-cases.json must be an object or array")
    cases = data.get("cases")
    if not isinstance(cases, list):
        raise ValueError("test-cases.json must contain a cases array")
    return data, cases


def mask_value(key: str, value: Any) -> Any:
    if key.lower() in SENSITIVE_KEYS:
        return "***"
    if isinstance(value, dict):
        return {k: mask_value(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_value(key, item) for item in value]
    return value


PHONE_LOOKALIKE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")


def summarize(value: Any, limit: int = 80) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(value)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    # 截断可能把更长的时间戳切成 11 位，被敏感校验当成手机号。
    return PHONE_LOOKALIKE.sub("***", text)


def cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ").strip()


def expected_status(expected: Any) -> str:
    if not isinstance(expected, dict):
        return summarize(expected)
    for key in ("status", "status_code", "statusCode", "http_status"):
        if key in expected:
            return str(expected[key])
    return summarize(expected)


def path_for_case(case: dict[str, Any]) -> str:
    source = case.get("source_path")
    target = case.get("target_path") or case.get("path")
    if source and target and source != target:
        return f"{source} -> {target}"
    return str(target or case.get("path") or "")


def clause_cell(case: dict[str, Any]) -> str:
    refs = case.get("clause_refs") or []
    if isinstance(refs, list):
        return ",".join(str(item) for item in refs)
    return str(refs)


def render_markdown(meta: dict[str, Any], cases: list[dict[str, Any]]) -> str:
    total = len(cases)
    executable = sum(1 for case in cases if case.get("executable", True))
    skipped = total - executable
    generated_at = meta.get("generated_at") or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    test_kind = meta.get("test_kind") or ""
    run_mode = meta.get("run_mode") or ""

    lines = [
        "# 接口测试用例表",
        "",
        f"- 生成时间：`{generated_at}`",
        f"- test_kind：`{test_kind or '-'}`",
        f"- run_mode：`{run_mode or '-'}`",
        f"- 用例总数：`{total}`",
        f"- 可执行：`{executable}`",
        f"- 不可执行：`{skipped}`",
        f"- proto有：`{sum(1 for case in cases if case.get('source_label') == 'proto有' or case.get('presence') == 'proto_only')}`",
        "",
        "## 总览",
        "",
        "| 编号 | 接口 | Method | Path | 类型 | 用例名称 | 预期状态码 | 可执行 | 跳过原因 | fixture | 条款 |",
        "|-|-|-|-|-|-|-|-|-|-|-|",
    ]

    for case in cases:
        expected = mask_value("expected", case.get("expected") or {})
        fixtures = case.get("fixture_refs") or []
        lines.append(
            "| {id} | {endpoint} | {method} | {path} | {category} | {name} | {status} | {executable} | {skip} | {fixture} | {clauses} |".format(
                id=cell(case.get("id")),
                endpoint=cell(case.get("endpoint_id")),
                method=cell(case.get("method")),
                path=cell(path_for_case(case)),
                category=cell(case.get("category")),
                name=cell(case.get("name")),
                status=cell(expected_status(expected)),
                executable="是" if case.get("executable", True) else "否",
                skip=cell(case.get("skip_reason") or ""),
                fixture=cell(",".join(str(item) for item in fixtures)),
                clauses=cell(clause_cell(case)),
            )
        )

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        grouped[str(case.get("endpoint_id") or "unknown")].append(case)

    lines.extend(["", "## 按接口明细", ""])
    for endpoint_id, endpoint_cases in grouped.items():
        method = endpoint_cases[0].get("method") or ""
        path = path_for_case(endpoint_cases[0])
        lines.extend(
            [
                f"### {endpoint_id} `{method} {path}`",
                "",
                "| 编号 | 用例名称 | 请求参数摘要 | 预期断言摘要 | 比较规则 | 契约来源 | 备注 |",
                "|-|-|-|-|-|-|-|",
            ]
        )
        for case in endpoint_cases:
            request = mask_value("request", case.get("request") or {})
            expected = mask_value("expected", case.get("expected") or {})
            compare_rule = case.get("compare_rule") or case.get("allowed_differences") or ""
            contract_source = case.get("contract_source") or case.get("source_label") or ""
            note = case.get("note") or case.get("skip_reason") or ""
            source_label = str(case.get("source_label") or "")
            if source_label and source_label not in str(note):
                note = f"{source_label}；{note}".rstrip("；") if note else source_label
            lines.append(
                "| {id} | {name} | {request} | {expected} | {compare} | {source} | {note} |".format(
                    id=cell(case.get("id")),
                    name=cell(case.get("name")),
                    request=cell(summarize(request)),
                    expected=cell(summarize(expected)),
                    compare=cell(summarize(compare_rule)),
                    source=cell(contract_source),
                    note=cell(note),
                )
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Render test-cases.json to Markdown tables")
    parser.add_argument("input", type=Path, help="path to test-cases.json")
    parser.add_argument("--output", type=Path, help="path to test-cases.md")
    args = parser.parse_args()

    input_path = args.input.resolve()
    output_path = (args.output or input_path.with_name("test-cases.md")).resolve()
    meta, cases = load_cases(input_path)
    if not cases:
        raise SystemExit("ERROR: cases array is empty")

    markdown = render_markdown(meta, cases)
    output_path.write_text(markdown, encoding="utf-8")
    print(f"OK: wrote {output_path} ({len(cases)} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
