#!/usr/bin/env python3
"""默认必填策略回归测试。"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GENERATOR = load_module("generate_full_cases", "generate_full_cases.py")
VALIDATOR = load_module("validate_run", "validate_run.py")


class DefaultRequiredPolicyTest(unittest.TestCase):
    def test_optional_query_flags_are_ignored(self) -> None:
        iface = {
            "id": "F1",
            "method": "GET",
            "path": "/api/detail",
            "query_parameters": [
                {"name": "id", "required": False, "type": "string"},
                {"name": "locale", "required": "0", "type": "string"},
            ],
        }
        unresolved: list[str] = []

        query = GENERATOR.merge_query(
            iface, None, {}, mode="valid", unresolved=unresolved
        )

        self.assertEqual(query, {})
        self.assertEqual(unresolved, ["query.id", "query.locale"])
        self.assertEqual(GENERATOR.collect_id_fields(iface, None), (["id"], []))

    def test_all_body_properties_are_required(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "strategyId": {"type": "string", "format": "uuid"},
                "name": {"type": "string"},
                "options": {
                    "type": "object",
                    "properties": {"region": {"type": "string"}},
                },
            },
            "required": [],
        }
        unresolved: list[str] = []

        body = GENERATOR.build_body_from_schema(
            schema,
            {},
            mode="valid",
            iface={"id": "F2", "method": "POST", "path": "/api/strategy"},
            binding={"name": "回归策略", "options": {}},
            unresolved=unresolved,
        )

        self.assertEqual(body, {"name": "回归策略", "options": {}})
        self.assertEqual(unresolved, ["body.strategyId", "body.options.region"])

    def test_validator_lists_all_declared_fields(self) -> None:
        iface = {
            "path_parameters": [{"name": "tenantId", "required": False}],
            "query_parameters": [{"name": "locale", "required": False}],
            "request_body": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "strategyId": {},
                        "name": {},
                        "options": {
                            "type": "object",
                            "properties": {"region": {}},
                        },
                    },
                    "required": [],
                }
            },
        }

        self.assertEqual(
            VALIDATOR.all_request_fields(iface),
            [
                ("path_params", "tenantId"),
                ("query", "locale"),
                ("body", "strategyId"),
                ("body", "name"),
                ("body", "options"),
                ("body", "options.region"),
            ],
        )
        self.assertTrue(
            VALIDATOR.request_field_exists(
                {"options": {"region": "cn"}}, "options.region"
            )
        )
        self.assertFalse(
            VALIDATOR.request_field_exists({"options": {}}, "options.region")
        )

    def test_empty_string_is_a_placeholder(self) -> None:
        self.assertEqual(
            VALIDATOR.placeholder_paths({"query": {"id": "  "}}),
            ["query.id"],
        )


if __name__ == "__main__":
    unittest.main()
