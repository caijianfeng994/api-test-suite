#!/usr/bin/env python3
"""通用 PostgreSQL 只读查询。

查询实现复制自 `.claude/skills/traffic-investment/scripts/verify_db.py` 的 `_run_sql`：
优先 LangChain `SQLDatabase.from_uri`，否则 SQLAlchemy；会话强制
`default_transaction_read_only=on`。

本脚本**只允许** SELECT / WITH / SHOW / EXPLAIN，禁止 INSERT / UPDATE / DELETE /
DROP / ALTER / TRUNCATE / CREATE 及任何写入。
不复制 `ensure_traffic_partition.py`（含 CREATE TABLE）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

FORBIDDEN_KEYWORDS = (
    "DELETE",
    "DROP",
    "TRUNCATE",
    "ALTER",
    "CREATE",
    "INSERT",
    "UPDATE",
    "REPLACE",
    "GRANT",
    "REVOKE",
    "CALL",
    "EXECUTE",
    "COPY",
    "VACUUM",
    "REINDEX",
    "CLUSTER",
    "DISCARD",
    "LISTEN",
    "NOTIFY",
    "UNLISTEN",
    "SET ROLE",
    "SET SESSION AUTHORIZATION",
)
ALLOWED_PREFIXES = ("SELECT", "WITH", "SHOW", "EXPLAIN")


def _normalize_sql(sql: str) -> str:
    sql = sql.strip()
    if not sql:
        raise ValueError("SQL 不能为空")
    if sql.count(";") > 1 or (sql.count(";") == 1 and not sql.rstrip().endswith(";")):
        raise ValueError("禁止一次执行多条 SQL 语句")
    if sql.endswith(";"):
        sql = sql[:-1].rstrip()
    return sql


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"--.*?$", " ", sql, flags=re.M)
    return sql


def assert_readonly_sql(sql: str) -> str:
    cleaned = _normalize_sql(sql)
    upper = re.sub(r"\s+", " ", _strip_comments(cleaned)).strip().upper()
    for kw in FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{re.escape(kw)}\b", upper):
            raise ValueError(f"禁止执行包含 {kw} 的 SQL（仅允许只读查询）")
    if not upper.startswith(ALLOWED_PREFIXES):
        raise ValueError("仅允许 SELECT / WITH / SHOW / EXPLAIN")
    return cleaned


def uri_from_parts(
    *,
    host: str,
    port: int | str,
    user: str,
    password: str,
    db: str,
    driver: str = "postgresql+psycopg2",
) -> str:
    user_q = quote_plus(user)
    pass_q = quote_plus(password)
    return f"{driver}://{user_q}:{pass_q}@{host}:{port}/{db}"


def load_uri_from_config(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("uri"):
        return str(data["uri"])
    env_name = data.get("uri_env")
    if env_name and os.environ.get(env_name):
        return os.environ[env_name]
    password = data.get("password") or os.environ.get(data.get("password_env") or "", "")
    if not password:
        raise ValueError(f"{path} 无 uri/password，且环境变量未提供密码")
    return uri_from_parts(
        host=data["host"],
        port=data["port"],
        user=data["user"],
        password=password,
        db=data["db"],
    )


def run_sql(uri: str, sql: str) -> list[dict[str, Any]]:
    """优先 LangChain SQLDatabase；否则 SQLAlchemy。只读。

    与 traffic-investment/scripts/verify_db.py `_run_sql` 相同策略。
    """
    safe_sql = assert_readonly_sql(sql)
    engine_args = {
        "connect_args": {"options": "-c default_transaction_read_only=on"},
    }
    try:
        from langchain_community.utilities import SQLDatabase  # type: ignore

        db = SQLDatabase.from_uri(uri, engine_args=engine_args)
        raw = db.run(safe_sql)
        if isinstance(raw, str):
            return [{"raw": raw}]
        if isinstance(raw, list):
            return [dict(r) if not isinstance(r, dict) else r for r in raw]
        return [{"raw": raw}]
    except ImportError:
        from sqlalchemy import create_engine, text

        eng = create_engine(uri, pool_pre_ping=True, **engine_args)
        with eng.connect() as conn:
            rows = conn.execute(text(safe_sql)).mappings().all()
            return [_serialize(dict(r)) for r in rows]


def _serialize(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        out[key] = value.isoformat() if hasattr(value, "isoformat") else value
        if not isinstance(out[key], (str, int, float, bool, type(None), list, dict)):
            out[key] = str(out[key])
    return out


def cmd_tables(uri: str) -> dict[str, Any]:
    sql = """
    SELECT table_schema, table_name
    FROM information_schema.tables
    WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
      AND table_type = 'BASE TABLE'
    ORDER BY table_schema, table_name
    """
    return {"tables": run_sql(uri, sql)}


def cmd_schema(uri: str, table: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        raise ValueError("table 名仅允许字母数字下划线")
    sql = f"""
    SELECT column_name, data_type, is_nullable, column_default
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = '{table}'
    ORDER BY ordinal_position
    """
    rows = run_sql(uri, sql)
    if not rows or (len(rows) == 1 and rows[0].get("raw") in ("", "[]", "None")):
        raise ValueError(f"未找到表 public.{table}")
    return {"table": table, "columns": rows}


def cmd_query(uri: str, sql: str, limit: int | None) -> dict[str, Any]:
    cleaned = assert_readonly_sql(sql)
    upper = re.sub(r"\s+", " ", _strip_comments(cleaned)).strip().upper()
    if limit and "LIMIT" not in upper:
        cleaned = f"{cleaned} LIMIT {int(limit)}"
    rows = run_sql(uri, cleaned)
    return {"mode": "query", "row_count": len(rows), "rows": rows}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="api-test-suite PostgreSQL 只读查询")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--uri", help="postgresql+psycopg2://...")
    src.add_argument("--config", help="fetch_gitlab_db_config.py / fetch_nacos_db_config.py 产出的 db-config.json")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("tables", help="列出业务表")
    schema = sub.add_parser("schema", help="查看表结构")
    schema.add_argument("--table", required=True)
    query = sub.add_parser("query", help="执行只读 SQL")
    query.add_argument("--sql", required=True)
    query.add_argument("--limit", type=int, default=50)
    parser.add_argument("--output", "-o", help="写入 JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        uri = args.uri or load_uri_from_config(Path(args.config))
        if args.command == "tables":
            payload = cmd_tables(uri)
        elif args.command == "schema":
            payload = cmd_schema(uri, args.table)
        elif args.command == "query":
            payload = cmd_query(uri, args.sql, args.limit)
        else:
            raise ValueError(f"未知命令: {args.command}")
        payload["ok"] = True
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
        print(text, end="")
        if args.output:
            Path(args.output).write_text(text, encoding="utf-8")
        return 0
    except Exception as exc:  # noqa: BLE001
        err = json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
        print(err, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
