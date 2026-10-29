#!/usr/bin/env python3
"""把代码返回点证据写回负向用例，并在状态码与响应格式都一致时记为 passed。

证据里的符号必须是 `file.go:start-end Symbol`。路由摘要不能闭合这条链路。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from validate_run import is_real_code_symbol


def _load(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} 必须是 JSON object")
    return payload


def apply_code_return(run_dir: Path) -> list[str]:
    evidence_path = run_dir / "code-validation-evidence.json"
    cases_path = run_dir / "test-cases.json"
    if not evidence_path.is_file() or not cases_path.is_file():
        raise FileNotFoundError("缺少 code-validation-evidence.json 或 test-cases.json")
    evidence = _load(evidence_path)
    cases_payload = _load(cases_path)
    cases = cases_payload.get("cases")
    if not isinstance(cases, list):
        raise ValueError("test-cases.json 缺少 cases")
    by_case = {
        str(case.get("id")): case
        for case in cases
        if isinstance(case, dict) and case.get("id")
    }
    results_path = run_dir / "execution" / "results.json"
    results_payload = _load(results_path) if results_path.is_file() else None
    closed: list[str] = []
    for item in evidence.get("cases") or []:
        if not isinstance(item, dict):
            continue
        case_id = str(item.get("case_id") or "")
        case = by_case.get(case_id)
        if case is None or str(case.get("category") or "") not in {"validation", "error"}:
            continue
        symbols = item.get("symbols")
        symbols_ok = (
            isinstance(symbols, list)
            and bool(symbols)
            and all(is_real_code_symbol(symbol) for symbol in symbols)
        )
        matches = item.get("status_match") is True and item.get("format_match") is True
        if not symbols_ok:
            if matches:
                raise ValueError(f"{case_id} 的代码符号未落到返回函数")
            continue
        if not matches:
            continue
        code_status = item.get("code_status")
        response_format = item.get("response_format")
        if not isinstance(code_status, int) or not isinstance(response_format, dict) or not response_format:
            raise ValueError(f"{case_id} 缺少 code_status 或 response_format")
        if item.get("actual_status") != code_status:
            raise ValueError(f"{case_id} actual_status 与 code_status 不一致")
        case["expected"] = {
            "status": [code_status],
            "status_class": f"{code_status // 100}xx",
            "assertion_policy": "code_return_required",
            "response_format": response_format,
            "code_evidence": {
                "artifact": "code-validation-evidence.json",
                "classification": item.get("classification") or "CODE_RETURN",
                "symbols": symbols,
                "code_status": code_status,
                "response_format": response_format,
            },
            "calibration": {
                "source": "code_return",
                "confidence": "high",
                "note": item.get("reason") or "HTTP 状态和响应格式与代码返回点一致",
            },
        }
        if results_payload is not None:
            for key in ("results", "cases"):
                rows = results_payload.get(key)
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    if isinstance(row, dict) and str(row.get("id") or row.get("case_id")) == case_id:
                        row["status"] = "passed"
                        row["error"] = ""
        closed.append(case_id)
    cases_path.write_text(json.dumps(cases_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if results_payload is not None:
        results_path.write_text(json.dumps(results_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return closed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()
    try:
        closed = apply_code_return(Path(args.run_dir))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps({"closed": closed}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
