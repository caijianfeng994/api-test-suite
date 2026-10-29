#!/usr/bin/env python3
"""负向用例必须按代码返回点闭合，禁止猜测 2xx_or_4xx。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from test_default_required_policy import load_module  # noqa: E402


GENERATOR = load_module("generate_full_cases", "generate_full_cases.py")
VALIDATOR = load_module("validate_run", "validate_run.py")
APPLY = load_module("apply_code_return_calibration", "apply_code_return_calibration.py")


def _iface() -> dict:
    return {
        "id": "F4",
        "name": "三方通用订单列表",
        "method": "GET",
        "path": "/growth-order-thirdparty/shadow/general/order/list",
        "query_parameters": [
            {"name": "shopId", "type": "string"},
            {"name": "startTime", "type": "string"},
            {"name": "endTime", "type": "string"},
            {"name": "page", "type": "int64"},
            {"name": "pagesize", "type": "int64"},
        ],
    }


class NegativeCodeReturnTest(unittest.TestCase):
    def test_error_query_only_replaces_resource_id(self) -> None:
        query = GENERATOR.merge_query(
            _iface(),
            None,
            {"fixtures": {"shared": {"page": 1, "limit": 20}}},
            mode="error_not_found",
            policy="negative",
        )

        self.assertEqual(query["shopId"], GENERATOR.NEGATIVE_UNKNOWN_ID)
        self.assertNotIn("startTime", query)
        self.assertNotIn("endTime", query)

    def test_undeclared_negative_expected_has_no_guessed_status(self) -> None:
        expected = GENERATOR.undeclared_error_http_expected(note="未声明")

        self.assertNotIn("status_class", expected)
        self.assertNotIn("not_status", expected)
        self.assertEqual(expected["assertion_policy"], "code_return_required")
        self.assertEqual(expected["calibration"]["source"], "pending_code_return")

    def test_contract_mode_still_rejects_guessed_assertion(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            case = {
                "id": "F4-C04",
                "category": "error",
                "executable": True,
                "expected": {
                    "status_class": "2xx_or_4xx",
                    "not_status": [500, 502, 503, 504],
                    "calibration": {"source": "contract_undeclared_error_http"},
                },
            }
            (run_dir / "test-cases.json").write_text(
                json.dumps({"cases": [case]}), encoding="utf-8"
            )

            errors = VALIDATOR.validate_negative_code_return(
                run_dir, {"run_mode": "contract", "status": "completed"}
            )

        self.assertTrue(any("禁止用 2xx_or_4xx" in error for error in errors))

    def test_placeholder_symbol_cannot_keep_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            (run_dir / "execution" / "requests").mkdir(parents=True)
            (run_dir / "test-cases.json").write_text(
                json.dumps({"cases": [{"id": "F4-C04", "category": "error"}]}),
                encoding="utf-8",
            )
            (run_dir / "execution" / "results.json").write_text(
                json.dumps({"results": [{"id": "F4-C04", "status": "failed"}]}),
                encoding="utf-8",
            )
            (run_dir / "execution" / "requests" / "F4-C04.json").write_text(
                json.dumps({"response_status": 500}), encoding="utf-8"
            )
            (run_dir / "code-validation-evidence.json").write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "case_id": "F4-C04",
                                "actual_status": 500,
                                "code_status": 400,
                                "status_match": False,
                                "format_match": False,
                                "symbols": ["router/router.go GinHandler"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            errors = VALIDATOR.validate_code_failure_calibration(
                run_dir, {"run_mode": "contract"}
            )

        self.assertTrue(any("未落到返回函数" in error for error in errors))

    def test_matching_code_return_becomes_exact_assertion_and_passed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            (run_dir / "execution").mkdir()
            (run_dir / "test-cases.json").write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "id": "F4-C04",
                                "category": "error",
                                "executable": True,
                                "expected": GENERATOR.undeclared_error_http_expected(note="待闭合"),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "execution" / "results.json").write_text(
                json.dumps({"results": [{"id": "F4-C04", "status": "failed", "error": "500"}]}),
                encoding="utf-8",
            )
            (run_dir / "code-validation-evidence.json").write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "case_id": "F4-C04",
                                "actual_status": 500,
                                "code_status": 500,
                                "status_match": True,
                                "format_match": True,
                                "response_format": {"code": 500, "msg": "SQLSTATE 22007"},
                                "classification": "PRODUCT_DEFECT",
                                "symbols": [
                                    "order/controller.go:272-288 ThirdpartGeneralOrderList",
                                    "dao/thirdpart_general_order.go:158-171 GetThirdpartGeneralList",
                                ],
                                "reason": "非法时间进入 created_at 比较，错误被 controller 收成 500",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            closed = APPLY.apply_code_return(run_dir)
            case = json.loads((run_dir / "test-cases.json").read_text())["cases"][0]
            result = json.loads((run_dir / "execution" / "results.json").read_text())["results"][0]
            errors = VALIDATOR.validate_negative_code_return(run_dir, {"status": "completed"})
            calibration_errors = VALIDATOR.validate_code_failure_calibration(
                run_dir, {"run_mode": "contract", "status": "completed"}
            )

        self.assertEqual(closed, ["F4-C04"])
        self.assertEqual(case["expected"]["status"], [500])
        self.assertEqual(case["expected"]["response_format"]["code"], 500)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(errors, [])
        self.assertEqual(calibration_errors, [])


class FixtureBindingLoopTest(unittest.TestCase):
    def test_contract_mode_rejects_missing_binding_without_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            (run_dir / "test-cases.json").write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "id": "F4-C01",
                                "endpoint_id": "F4",
                                "category": "functional",
                                "executable": False,
                                "skip_reason": "缺少真实绑定数据：query.shopId",
                                "regression_blockers": ["required_binding_missing:query.shopId"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "fixtures.json").write_text(
                json.dumps({"pending_requirements": [], "endpoint_bindings": {}}),
                encoding="utf-8",
            )

            errors = VALIDATOR.validate_fixture_partial_binding_leak(
                run_dir, {"run_mode": "contract", "status": "completed"}
            )

        self.assertTrue(any("fixture leak: F4-C01" in error for error in errors))

    def test_generator_records_pending_requirement_for_missing_binding(self) -> None:
        fixtures = {"fixtures": {"shared": {}}, "endpoint_bindings": {}, "pending_requirements": []}
        cases = [
            {
                "id": "F4-C01",
                "endpoint_id": "F4",
                "category": "functional",
                "regression_blockers": ["required_binding_missing:query.shopId"],
            }
        ]

        synced = GENERATOR.sync_pending_requirements(fixtures, cases)

        self.assertEqual(synced["pending_requirements"][0]["field"], "query.shopId")
        self.assertEqual(synced["pending_requirements"][0]["attempts"], [])


def _binding_case(field: str = "query.cid") -> dict:
    return {
        "cases": [
            {
                "id": "F4-C01",
                "endpoint_id": "F4",
                "category": "functional",
                "executable": False,
                "regression_blockers": [f"required_binding_missing:{field}"],
            }
        ]
    }


def _binding_evidence(decision: str, **overrides: object) -> dict:
    item = {
        "endpoint_id": "F4",
        "method": "GET",
        "path": "/traffic-investment/click/wifi",
        "field": "query.cid",
        "query": 'path:"/traffic-investment/click/wifi"',
        "hit_count": 0,
        "request_logged": False,
        "field_present_count": 0,
        "decision": decision,
    }
    item.update(overrides)
    return {
        "window_days": 30,
        "start": "2026-09-22T15:40:00+08:00",
        "end": "2026-10-22T15:40:00+08:00",
        "topic_id": "topic-id",
        "fields": [item],
    }


class BindingLogSupplementTest(unittest.TestCase):
    def _errors(self, cases: dict, evidence: dict | None, fixtures: dict | None = None) -> list[str]:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            (run_dir / "test-cases.json").write_text(json.dumps(cases), encoding="utf-8")
            if fixtures is not None:
                (run_dir / "fixtures.json").write_text(json.dumps(fixtures), encoding="utf-8")
            if evidence is not None:
                target = run_dir / "execution"
                target.mkdir()
                (target / "binding-log-supplement.json").write_text(
                    json.dumps(evidence), encoding="utf-8"
                )
            return VALIDATOR.validate_binding_log_supplement(run_dir, {"status": "completed"})

    def test_missing_binding_without_log_evidence_fails(self) -> None:
        errors = self._errors(_binding_case(), None)
        self.assertTrue(any("binding-log-supplement.json" in error for error in errors))

    def test_no_traffic_allows_missing_binding(self) -> None:
        errors = self._errors(_binding_case(), _binding_evidence("no_traffic"))
        self.assertEqual(errors, [])

    def test_unverified_cannot_close_missing_binding(self) -> None:
        errors = self._errors(
            _binding_case(),
            _binding_evidence("unverified", hit_count=3, query_error="timeout"),
        )
        self.assertTrue(any("不得记 required_binding_missing" in error for error in errors))

    def test_bound_value_cannot_stay_missing(self) -> None:
        errors = self._errors(
            _binding_case(),
            _binding_evidence(
                "bound", hit_count=4, request_logged=True, field_present_count=4
            ),
        )
        self.assertTrue(any("不得保持 required_binding_missing" in error for error in errors))

    def test_log_source_requires_bound_decision(self) -> None:
        errors = self._errors(
            {"cases": []},
            _binding_evidence("no_traffic"),
            {"endpoint_bindings": {"F4": {"sources": {"cid": "log:/click/wifi#query.cid"}, "values": {"cid": "abc"}}}},
        )
        self.assertTrue(any("decision 必须为 bound" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
