#!/usr/bin/env python3
"""Nacos / GitLab postgres 解析单测（无网络）。"""

from __future__ import annotations

import unittest

from db_config_parse import (
    extract_postgres_block,
    group_spellings,
    parse_config_content,
    parse_postgres_blocks,
    parse_top_level_name,
    pick_block,
    pick_placeholder_block,
    placeholder_fields,
)


class ParseYamlTests(unittest.TestCase):
    def test_top_level_name(self) -> None:
        text = "name: good\n\nhttp:\n  port: 80\n"
        self.assertEqual(parse_top_level_name(text), "good")

    def test_placeholder_postgres(self) -> None:
        text = """
postgres:
  good:
    host: ${PG_HOST}
    port: ${PG_PORT}
    user: ${PG_USER}
    password: ${PG_PASSWORD}
    db: ${PG_DB}
"""
        blocks = parse_postgres_blocks(text)
        self.assertEqual(len(blocks), 1)
        self.assertIsNone(pick_block(blocks, "good"))
        block = pick_placeholder_block(blocks, "good")
        assert block is not None
        self.assertEqual(
            placeholder_fields(block),
            {
                "host": "PG_HOST",
                "port": "PG_PORT",
                "user": "PG_USER",
                "password": "PG_PASSWORD",
                "db": "PG_DB",
            },
        )


class NacosDictTests(unittest.TestCase):
    def test_json_flat_keys(self) -> None:
        flat = parse_config_content(
            '{"PG_HOST":"10.0.0.1","PG_PORT":"5432","PG_USER":"postgres",'
            '"PG_PASSWORD":"secret","PG_DB":"good"}'
        )
        block = extract_postgres_block(flat, service_name="good")
        assert block is not None
        self.assertEqual(block["host"], "10.0.0.1")
        self.assertEqual(block["db"], "good")
        self.assertEqual(block["password"], "secret")

    def test_preferred_placeholders(self) -> None:
        flat = parse_config_content("PG_HOST=10.1.1.1\nPG_PASSWORD=pw\nPG_DB=demo\n")
        block = extract_postgres_block(
            flat,
            service_name="demo",
            preferred_fields={"host": "PG_HOST", "password": "PG_PASSWORD", "db": "PG_DB"},
        )
        assert block is not None
        self.assertEqual(block["host"], "10.1.1.1")

    def test_postgres_uri_source(self) -> None:
        flat = parse_config_content(
            "PG_SOURCE=postgresql://tester:secret@10.2.3.4:5544/revenue"
            "?sslmode=require\n"
        )
        block = extract_postgres_block(flat, service_name="revenue")
        assert block is not None
        self.assertEqual(block["host"], "10.2.3.4")
        self.assertEqual(str(block["port"]), "5544")
        self.assertEqual(block["user"], "tester")
        self.assertEqual(block["password"], "secret")
        self.assertEqual(block["db"], "revenue")
        self.assertEqual(block["sslmode"], "require")

    def test_nested_json_postgres(self) -> None:
        text = """
postgres:
  good:
    host: 10.8.8.9
    port: 5411
    user: postgres
    password: hidden
    db: good
"""
        block = pick_block(parse_postgres_blocks(text), "good")
        assert block is not None
        self.assertEqual(block["host"], "10.8.8.9")

    def test_group_spellings(self) -> None:
        names = group_spellings("redeem-code")
        self.assertIn("redeem-code", names)
        self.assertIn("redeem_code", names)
        self.assertIn("redeemcode", names)


if __name__ == "__main__":
    unittest.main()
