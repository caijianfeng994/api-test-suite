#!/usr/bin/env python3
"""解析 GitLab yaml / Nacos dict 中的 PostgreSQL 连接字段。"""

from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

PLACEHOLDER_RE = re.compile(r"^\$\{([A-Z0-9_]+)\}$")

FLAT_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "host": ("PG_HOST", "POSTGRES_HOST", "DB_HOST"),
    "port": ("PG_PORT", "POSTGRES_PORT", "DB_PORT"),
    "user": ("PG_USER", "PG_USERNAME", "PG_ACCOUNT", "POSTGRES_USER", "DB_USER"),
    "password": ("PG_PASSWORD", "PG_PASSWD", "POSTGRES_PASSWORD", "DB_PASSWORD"),
    "db": ("PG_DB", "PG_DATABASE", "PG_DBNAME", "POSTGRES_DB", "DB_NAME", "DB_DATABASE"),
}


def expand_env(value: str) -> str:
    match = PLACEHOLDER_RE.match(value.strip())
    if not match:
        return value
    return os.environ.get(match.group(1), value)


def parse_top_level_name(text: str) -> str | None:
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent != 0:
            continue
        if re.match(r"^name\s*:", raw):
            value = raw.split(":", 1)[1].strip().strip('"').strip("'")
            return value or None
    return None


def parse_postgres_blocks(text: str) -> list[dict[str, str]]:
    lines = text.splitlines()
    blocks: list[dict[str, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.match(r"^\s*postgres:\s*$", line):
            base_indent = len(line) - len(line.lstrip(" "))
            i += 1
            current: dict[str, str] = {}
            child_indent: int | None = None
            while i < len(lines):
                raw = lines[i]
                if not raw.strip() or raw.lstrip().startswith("#"):
                    i += 1
                    continue
                indent = len(raw) - len(raw.lstrip(" "))
                if indent <= base_indent:
                    break
                stripped = raw.strip()
                if stripped.endswith(":") and stripped.count(":") == 1:
                    if current:
                        blocks.append(current)
                        current = {}
                    current = {"name": stripped[:-1].strip()}
                    child_indent = None
                    i += 1
                    continue
                if ":" in stripped:
                    key, value = stripped.split(":", 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if child_indent is None:
                        child_indent = indent
                    current[key] = expand_env(value)
                i += 1
            if current:
                blocks.append(current)
            continue
        i += 1
    return blocks


def is_concrete(block: dict[str, str]) -> bool:
    host = block.get("host") or ""
    dbname = block.get("db") or block.get("dbname") or ""
    password = block.get("password") or ""
    if not host or PLACEHOLDER_RE.match(host):
        return False
    if not dbname or PLACEHOLDER_RE.match(dbname):
        return False
    if not password or PLACEHOLDER_RE.match(password):
        return False
    return True


def pick_block(blocks: list[dict[str, str]], service_name: str) -> dict[str, str] | None:
    named = [b for b in blocks if b.get("name") == service_name and is_concrete(b)]
    if named:
        return named[0]
    concrete = [b for b in blocks if is_concrete(b)]
    return concrete[0] if concrete else None


def pick_placeholder_block(blocks: list[dict[str, str]], service_name: str) -> dict[str, str] | None:
    named = [b for b in blocks if b.get("name") == service_name]
    if named:
        return named[0]
    return blocks[0] if blocks else None


def placeholder_key(value: str) -> str | None:
    match = PLACEHOLDER_RE.match((value or "").strip())
    return match.group(1) if match else None


def placeholder_fields(block: dict[str, str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for field in ("host", "port", "user", "password", "db", "dbname"):
        key = placeholder_key(block.get(field, ""))
        if key:
            mapping["db" if field == "dbname" else field] = key
    return mapping


def group_spellings(*names: str) -> list[str]:
    seen: list[str] = []

    def add(name: str) -> None:
        value = (name or "").strip()
        if value and value not in seen:
            seen.append(value)

    for name in names:
        add(name)
        add(name.replace("-", "_"))
        add(name.replace("_", "-"))
        add(name.replace("-", "").replace("_", ""))
    return seen


def parse_config_content(text: str) -> dict[str, str]:
    """把 Nacos 正文收成扁平 key→string。JSON / properties / yaml 单层均可。"""
    stripped = (text or "").strip()
    if not stripped:
        return {}
    if stripped[0] in "{[":
        try:
            loaded = json.loads(stripped)
        except json.JSONDecodeError:
            loaded = None
        if isinstance(loaded, dict):
            return _flatten_mapping(loaded)
    flat: dict[str, str] = {}
    for raw in stripped.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if "=" in line and ":" not in line.split("=", 1)[0]:
            key, value = line.split("=", 1)
        elif ":" in line:
            key, value = line.split(":", 1)
        else:
            continue
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            flat[key] = value
    return flat


def _flatten_mapping(obj: Any, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_s = str(key)
            path = f"{prefix}.{key_s}" if prefix else key_s
            if isinstance(value, dict):
                out.update(_flatten_mapping(value, path))
            elif isinstance(value, list):
                continue
            elif value is None:
                continue
            else:
                text = str(value)
                out[path] = text
                out[key_s] = text
    return out


def lookup_field(flat: dict[str, str], *names: str) -> str:
    for name in names:
        if name in flat and str(flat[name]).strip():
            return str(flat[name]).strip()
        lower = name.lower()
        for key, value in flat.items():
            if key.lower() == lower and str(value).strip():
                return str(value).strip()
    return ""


def extract_postgres_block(
    flat: dict[str, str],
    *,
    service_name: str,
    preferred_fields: dict[str, str] | None = None,
) -> dict[str, str] | None:
    source = lookup_field(flat, "PG_SOURCE", "POSTGRES_SOURCE", "DATABASE_URL")
    if source:
        parsed = urlsplit(source)
        if parsed.scheme in {"postgres", "postgresql"} and parsed.hostname:
            query = parse_qs(parsed.query)
            block = {
                "name": service_name,
                "host": parsed.hostname,
                "port": str(parsed.port or 5432),
                "user": unquote(parsed.username or "postgres"),
                "password": unquote(parsed.password or ""),
                "db": unquote(parsed.path.lstrip("/")),
                "sslmode": (query.get("sslmode") or ["disable"])[0],
            }
            if is_concrete(block):
                return block

    block: dict[str, str] = {"name": service_name}
    preferred = preferred_fields or {}
    for field, aliases in FLAT_FIELD_ALIASES.items():
        names = []
        if preferred.get(field):
            names.append(preferred[field])
        names.extend(aliases)
        value = lookup_field(flat, *names)
        if value:
            block[field] = value
    if is_concrete(block):
        return block
    return None


def redact(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    if out.get("password"):
        out["password"] = "***"
    if out.get("uri"):
        out["uri_redacted"] = re.sub(r":[^:@/]+@", ":***@", str(out["uri"]), count=1)
        del out["uri"]
    return out
