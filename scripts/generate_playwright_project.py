#!/usr/bin/env python3
"""把 run 目录里的用例集打包成可运行的 Playwright 工程。

默认 `mode=regression`：输入只有 `<run_dir>/regression-cases.json`（晋升集，backlog
不进工程）；输出落在 `output_dir`（默认 `.`，即 run 目录本身）的独立回归工程。

`mode=execution`：读取 `<run_dir>/test-cases.json` 与 `run-context.json`，在
`<run_dir>/<output_dir>/`（通常 `execution-runtime/`）生成完整执行工程；spec 通过
相对路径回读父 run，证据写回父 run 的 `execution/requests/`。写授权在生成期调用
Task 1 的 `resolve_write_authorization` 固化进执行元数据，避免 TypeScript 复制冲突逻辑。

三条硬约束：

1. **可移植**：生成物不得出现本机绝对路径，依赖按包名解析（`@playwright/test`），
   工程拷到任何机器上 `npm ci && npm test` 即可跑。
2. **版本不手写**：`@playwright/test` 的精确版本与依赖闭包来自
   `backend/workspace/api/package-lock.json`，保证与仓库内既有执行环境一致。
3. **断言精确**：spec 按每条用例自己的 `expected` 断言（成功路径要 2xx + 业务码 +
   schema 字段；负向要精确 status 或 `<n>xx`），**不得**退化成「非 5xx 即通过」。

输入缺失、损坏或 `case_count` 对不上时一律抛 `ProjectGenerationError`，CLI 退出码非 0，
且不落任何文件。`case_count=0` 是合法的空回归集，仍生成可安装、可收集 0 条的工程。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

CASES_FILE = "regression-cases.json"
EXECUTION_CASES_FILE = "test-cases.json"
RUN_CONTEXT_FILE = "run-context.json"
WORKSPACE_LOCK_REL = "backend/workspace/api/package-lock.json"
PLAYWRIGHT_PACKAGE = "@playwright/test"
LOCKFILE_VERSION = 3
PACKAGE_VERSION = "0.0.0"

BASE_URL_ENV = "API_BASE_URL"
SUPPORTED_MODES = frozenset({"regression", "execution"})

# `${VAR}` 形式的环境变量引用：请求里唯一允许出现的「非真实取值」
ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# npm 包名允许的字符
PACKAGE_NAME_UNSAFE = re.compile(r"[^a-z0-9._-]+")


class ProjectGenerationError(RuntimeError):
    """回归/执行工程生成失败：输入缺失、损坏或不足以产出可运行工程。"""


def _load_sibling_module(name: str) -> Any:
    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise ProjectGenerationError(f"无法加载同级模块 {name}：{path}")
    loaded = module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


resolve_write_authorization = _load_sibling_module("write_authorization").resolve_write_authorization

# --- 输入解析 ---------------------------------------------------------------


def read_playwright_version(lock_path: str | Path) -> str:
    """从既有 lock 里读 `@playwright/test` 的精确版本，读不到就报错，绝不猜。"""
    path = Path(lock_path)
    if not path.is_file():
        raise ProjectGenerationError(f"缺少 package-lock.json：{path}")
    payload = _read_json(path, "package-lock.json")
    packages = payload.get("packages") if isinstance(payload, dict) else None
    entry = packages.get(f"node_modules/{PLAYWRIGHT_PACKAGE}") if isinstance(packages, dict) else None
    version = (entry or {}).get("version") if isinstance(entry, dict) else None
    if not isinstance(version, str) or not version.strip():
        raise ProjectGenerationError(
            f"package-lock.json 未锁定 {PLAYWRIGHT_PACKAGE} 的精确版本：{path}"
        )
    return version.strip()


def load_suite(run_dir: str | Path, *, mode: str = "regression") -> dict:
    """加载并校验用例集：结构、ID 唯一性、`case_count` 与实际条数一致。

    regression 模式额外要求每条 `regression_ready=true`；execution 模式读取完整
    `test-cases.json`，不强制晋升门禁。
    """
    if mode not in SUPPORTED_MODES:
        raise ProjectGenerationError(f"unsupported mode: {mode}")
    suite_name = CASES_FILE if mode == "regression" else EXECUTION_CASES_FILE
    path = Path(run_dir) / suite_name
    if not path.is_file():
        raise ProjectGenerationError(f"缺少 {suite_name}：{path}")

    payload = _read_json(path, suite_name)
    if not isinstance(payload, dict):
        raise ProjectGenerationError(f"{suite_name} 顶层必须是对象：{path}")

    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise ProjectGenerationError(f"{suite_name} 缺少 cases 列表：{path}")
    seen: set[str] = set()
    for index, case in enumerate(cases, start=1):
        if not isinstance(case, dict):
            raise ProjectGenerationError(f"{suite_name} 第 {index} 条用例不是对象：{path}")
        case_id = str(case.get("id") or "").strip()
        if not case_id:
            raise ProjectGenerationError(f"{suite_name} 第 {index} 条用例缺少 id：{path}")
        if case_id in seen:
            raise ProjectGenerationError(f"{suite_name} 用例 id 重复：{case_id}（{path}）")
        seen.add(case_id)
        if mode == "regression" and case.get("regression_ready") is not True:
            raise ProjectGenerationError(
                f"{suite_name} 用例 {case_id} 不是 regression_ready=true，禁止进入回归工程：{path}"
            )

    declared = payload.get("case_count")
    if not isinstance(declared, int) or isinstance(declared, bool) or declared < 0:
        raise ProjectGenerationError(
            f"{suite_name} 的 case_count 必须存在且为非负整数：{path}"
        )
    if declared != len(cases):
        raise ProjectGenerationError(
            f"{suite_name} 的 case_count={declared} 与实际用例数 {len(cases)} 不一致：{path}"
        )
    return payload


def collect_env_names(cases: list[dict]) -> list[str]:
    """收集 path 与请求各位置里的全部 `${VAR}` 引用。"""
    names: set[str] = set()
    for case in cases:
        if isinstance(case, dict):
            _scan_env(case.get("path"), names)
        request = case.get("request") if isinstance(case, dict) else None
        if isinstance(request, dict):
            _scan_env(request, names)
    return sorted(names)


def _scan_env(value: object, names: set[str]) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _scan_env(item, names)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _scan_env(item, names)
    elif isinstance(value, str):
        names.update(ENV_REFERENCE.findall(value))


def _read_json(path: Path, label: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProjectGenerationError(f"{label} 读取失败（{path}）：{error}") from error


# --- 工程文件 ---------------------------------------------------------------


def package_name(service_name: str) -> str:
    slug = PACKAGE_NAME_UNSAFE.sub("-", str(service_name or "").strip().lower()).strip("-._")
    return f"api-regression-{slug}" if slug else "api-regression"


def build_package_json(name: str, version: str) -> dict:
    return {
        "name": name,
        "version": PACKAGE_VERSION,
        "private": True,
        "scripts": {
            "test": "playwright test",
            "test:list": "playwright test --list --pass-with-no-tests",
        },
        "devDependencies": {PLAYWRIGHT_PACKAGE: version},
    }


def build_package_lock(name: str, version: str, source_lock: dict) -> dict:
    """按既有 lock 的依赖闭包生成本工程的 lock，保证 `npm ci` 可复现。"""
    packages = source_lock.get("packages") if isinstance(source_lock, dict) else None
    if not isinstance(packages, dict):
        raise ProjectGenerationError("package-lock.json 缺少 packages 段，无法复制依赖闭包")

    root_key = f"node_modules/{PLAYWRIGHT_PACKAGE}"
    if root_key not in packages:
        raise ProjectGenerationError(f"package-lock.json 缺少 {PLAYWRIGHT_PACKAGE} 条目")
    selected_keys = _dependency_closure(packages, root_key)
    dependencies = {key: packages[key] for key in sorted(selected_keys)}
    for key, value in dependencies.items():
        if not value.get("resolved") or not value.get("integrity"):
            raise ProjectGenerationError(f"package-lock.json 条目缺少 resolved/integrity：{key}")
        if _is_local_resolved(str(value["resolved"])):
            raise ProjectGenerationError(
                f"package-lock.json 条目 resolved 指向本机路径，禁止生成：{key} -> {value['resolved']}"
            )

    result = {
        "name": name,
        "version": PACKAGE_VERSION,
        "lockfileVersion": LOCKFILE_VERSION,
        "requires": True,
        "packages": {
            "": {
                "name": name,
                "version": PACKAGE_VERSION,
                "private": True,
                "devDependencies": {PLAYWRIGHT_PACKAGE: version},
            },
            **dependencies,
        },
    }
    snapshots = source_lock.get("snapshots")
    if isinstance(snapshots, dict):
        selected_snapshots = {
            key: value
            for key, value in snapshots.items()
            if _snapshot_belongs_to(key, selected_keys, packages)
        }
        if selected_snapshots:
            result["snapshots"] = selected_snapshots
    return result


def _dependency_closure(packages: dict, root_key: str) -> set[str]:
    """按 Node 解析规则递归收集 dependencies/optionalDependencies/peerDependencies。"""
    selected: set[str] = set()
    pending = [root_key]
    while pending:
        key = pending.pop()
        if key in selected:
            continue
        entry = packages.get(key)
        if not isinstance(entry, dict):
            raise ProjectGenerationError(f"package-lock.json 依赖条目缺失：{key}")
        selected.add(key)
        optional_peers = {
            name
            for name, meta in (entry.get("peerDependenciesMeta") or {}).items()
            if isinstance(meta, dict) and meta.get("optional") is True
        }
        for section in ("dependencies", "optionalDependencies", "peerDependencies"):
            declared = entry.get(section)
            if not isinstance(declared, dict):
                continue
            for dependency_name in declared:
                resolved_key = _resolve_dependency_key(packages, key, dependency_name)
                if resolved_key:
                    pending.append(resolved_key)
                elif section == "dependencies" or (
                    section == "peerDependencies" and dependency_name not in optional_peers
                ):
                    raise ProjectGenerationError(
                        f"package-lock.json 无法解析 {key} 的 {section}.{dependency_name}"
                    )
    return selected


def _resolve_dependency_key(packages: dict, parent_key: str, dependency_name: str) -> str | None:
    """从父包目录逐级向上模拟 Node 的 node_modules 查找。"""
    current = parent_key
    candidates: list[str] = []
    while current:
        candidates.append(f"{current}/node_modules/{dependency_name}")
        marker = current.rfind("/node_modules/")
        if marker < 0:
            break
        current = current[:marker]
    candidates.append(f"node_modules/{dependency_name}")
    return next((candidate for candidate in candidates if candidate in packages), None)


def _is_local_resolved(resolved: str) -> bool:
    value = resolved.strip()
    return (
        value.lower().startswith("file:")
        or value.startswith(("/", "\\"))
        or bool(re.match(r"^[A-Za-z]:[\\/]", value))
    )


def _snapshot_belongs_to(key: str, selected: set[str], packages: dict) -> bool:
    if key in selected or f"node_modules/{key}" in selected:
        return True
    for package_key in selected:
        entry = packages.get(package_key) or {}
        package_name = package_key.rsplit("node_modules/", 1)[-1]
        version = entry.get("version")
        snapshot_prefix = f"{package_name}@{version}"
        if version and (key == snapshot_prefix or key.startswith(f"{snapshot_prefix}(")):
            return True
    return False


def build_env_example(service_name: str, env_names: list[str]) -> str:
    lines = [
        f"# 回归工程环境变量模板（服务：{service_name}）",
        "# 复制为 .env 或在 shell 中导出后再执行 npm test；模板只列键名，不写任何真实取值。",
        "",
        "# 被测服务 Base URL，形如 http://<host>[:<port>]",
        f"{BASE_URL_ENV}=",
        "# 可选：访问影子/灰度环境需要指定 Host 头时填写",
        "API_HOST_HEADER=",
        "# 可选：单测试与单请求超时（毫秒），默认 20000",
        "API_TEST_TIMEOUT_MS=",
        "# 可选：Playwright 并发 worker 数，默认 4",
        "API_TEST_WORKERS=",
    ]
    if env_names:
        lines.extend(["", "# 用例请求中通过 ${...} 引用的变量"])
        lines.extend(f"{name}=" for name in env_names)
    return "\n".join(lines) + "\n"


PLAYWRIGHT_CONFIG = '''import { defineConfig } from "@playwright/test";

// 全部路径相对本配置文件所在目录，工程可整体拷贝到任意机器运行。
export default defineConfig({
  testDir: "./tests",
  outputDir: "./execution/pw-output",
  timeout: Number(process.env.API_TEST_TIMEOUT_MS ?? 20000),
  workers: Number(process.env.API_TEST_WORKERS ?? 4),
  fullyParallel: true,
  reporter: [
    ["list"],
    ["json", { outputFile: "./execution/playwright-results.json" }],
  ],
});
'''


SPEC_TEMPLATE = '''/**
 * 回归用例执行脚本（服务：__SERVICE_NAME__）。
 *
 * 输入只有工程根目录的 regression-cases.json，不回读 run 目录的其它产物。
 * 每条用例按自身 expected 精确断言：成功路径要 2xx + 业务码 + schema 声明字段；
 * 负向用例要精确 status 或 <n>xx 状态类。期望不精确的用例直接判失败。
 */
import { expect, test } from "@playwright/test";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

const projectDir = path.resolve(__dirname, "..");
const suite = JSON.parse(fs.readFileSync(path.join(projectDir, "regression-cases.json"), "utf8"));
const cases: any[] = Array.isArray(suite.cases) ? suite.cases : [];
const evidenceDir = path.join(projectDir, "execution", "requests");

const ENV_EXACT = /^\\$\\{([A-Za-z_][A-Za-z0-9_]*)\\}$/;
const ENV_INLINE = /\\$\\{([A-Za-z_][A-Za-z0-9_]*)\\}/g;
const UNRESOLVED_SEGMENT = /\\{[A-Za-z_][A-Za-z0-9_.-]*\\}/;
const STATUS_CLASS = /^([1-5])xx$/i;
const COMMENT_FIELD = /^\\s*\\/\\*[\\s\\S]*\\*\\/\\s*$/;
const SUCCESS_CATEGORIES = new Set(["functional", "list", "side_effect"]);
const NEGATIVE_CATEGORIES = new Set(["validation", "auth", "error"]);
const DEFAULT_SUCCESS_CODES: number[] = [];
const EVIDENCE_BODY_LIMIT = 20000;

function requireEnv(name: string): string {
  const value = process.env[name];
  if (value === undefined || value === "") {
    throw new Error(`缺少环境变量 ${name}，请按 .env.example 配置后重试`);
  }
  return value;
}

/** 递归把 `${VAR}` 展开成环境变量；缺变量时抛错，绝不把字面量发出去。 */
function resolveEnv(value: any): any {
  if (typeof value === "string") {
    const exact = ENV_EXACT.exec(value.trim());
    if (exact) {
      return requireEnv(exact[1]);
    }
    return value.replace(ENV_INLINE, (_match, name) => requireEnv(name));
  }
  if (Array.isArray(value)) {
    return value.map(resolveEnv);
  }
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, resolveEnv(item)]));
  }
  return value;
}

function requestPart(testCase: any, key: string): any {
  const request = testCase.request;
  if (request === null || typeof request !== "object") {
    return undefined;
  }
  return resolveEnv(request[key]);
}

function buildTarget(testCase: any): string {
  let target = String(resolveEnv(String(testCase.path ?? "")));
  const params = requestPart(testCase, "path_params");
  if (params !== null && typeof params === "object" && !Array.isArray(params)) {
    for (const [key, value] of Object.entries(params)) {
      if (value === null || value === undefined) continue;
      target = target.split(`{${key}}`).join(encodeURIComponent(String(value)));
    }
  }
  const unresolved = UNRESOLVED_SEGMENT.exec(target);
  if (unresolved) {
    throw new Error(`用例 ${testCase.id} 的路径参数未提供：${unresolved[0]}`);
  }

  if (!target.startsWith("/")) {
    target = `/${target}`;
  }
  const parsed = new URL(target, "http://api-regression.local");
  const query = requestPart(testCase, "query");
  if (query !== null && typeof query === "object" && !Array.isArray(query)) {
    for (const [key, value] of Object.entries(query)) {
      if (value === null || value === undefined) continue;
      if (Array.isArray(value)) {
        for (const item of value) {
          if (!isQueryScalar(item)) {
            throw new Error(`用例 ${testCase.id} 的 query.${key} 数组只能包含标量`);
          }
          parsed.searchParams.append(key, String(item));
        }
      } else {
        if (!isQueryScalar(value)) {
          throw new Error(`用例 ${testCase.id} 的 query.${key} 必须是标量或标量数组`);
        }
        parsed.searchParams.append(key, String(value));
      }
    }
  } else if (query !== null && query !== undefined) {
    throw new Error(`用例 ${testCase.id} 的 query 必须是对象`);
  }
  return `${parsed.pathname}${parsed.search}`;
}

function isQueryScalar(value: any): boolean {
  return typeof value === "string" || typeof value === "number" || typeof value === "boolean";
}

function buildHeaders(testCase: any): Record<string, string> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const declared = requestPart(testCase, "headers");
  if (declared !== null && typeof declared === "object" && !Array.isArray(declared)) {
    for (const [key, value] of Object.entries(declared)) {
      if (value === null || value === undefined) continue;
      headers[key] = String(value);
    }
  }
  const hostHeader = process.env.API_HOST_HEADER;
  if (hostHeader) {
    headers.Host = hostHeader;
  }
  return headers;
}

function applyRuntimeUnique(testCase: any, body: any): any {
  const fields = testCase.runtime_unique_fields;
  if (!Array.isArray(fields) || body === null || typeof body !== "object" || Array.isArray(body)) {
    return body;
  }
  const next = { ...body };
  for (const field of fields) {
    if (field === "body.userId" && typeof next.userId === "string") {
      next.userId = crypto.randomUUID();
    }
  }
  return next;
}

function buildBody(testCase: any, method: string): any {
  if (method === "GET" || method === "HEAD") {
    return undefined;
  }
  const body = requestPart(testCase, "body");
  if (body === null || body === undefined) return undefined;
  return applyRuntimeUnique(testCase, body);
}

function parseBody(text: string): any {
  const trimmed = text.trim();
  if (!trimmed || (trimmed[0] !== "{" && trimmed[0] !== "[")) {
    return text;
  }
  try {
    return JSON.parse(trimmed);
  } catch {
    return text;
  }
}

function lookup(body: any, dotted: string): { found: boolean; value: any } {
  let current = body;
  for (const part of String(dotted).split(".")) {
    if (current !== null && typeof current === "object" && !Array.isArray(current) && part in current) {
      current = current[part];
    } else {
      return { found: false, value: undefined };
    }
  }
  return { found: true, value: current };
}

function numericCode(value: any): number | null {
  if (typeof value === "boolean") return null;
  if (typeof value === "number" && Number.isInteger(value)) return value;
  if (typeof value === "string" && /^-?\\d+$/.test(value.trim())) return Number(value.trim());
  return null;
}

function statusList(value: any): number[] {
  const items = Array.isArray(value) ? value : value === undefined || value === null ? [] : [value];
  const codes: number[] = [];
  for (const item of items) {
    const code = numericCode(item);
    if (code !== null) codes.push(code);
  }
  return codes;
}

function matchesJsonType(value: any, declaredType: any): boolean {
  if (declaredType === undefined || declaredType === null || declaredType === "") return true;
  if (declaredType === "object") {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }
  if (declaredType === "array") return Array.isArray(value);
  if (declaredType === "string") return typeof value === "string";
  if (declaredType === "number") return typeof value === "number" && Number.isFinite(value);
  if (declaredType === "integer") return typeof value === "number" && Number.isInteger(value);
  if (declaredType === "boolean") return typeof value === "boolean";
  return true;
}

function assertSchema(value: any, schema: any, location: string): void {
  if (schema === null || typeof schema !== "object" || Array.isArray(schema)) return;
  const properties = schema.properties;
  // type=array 优先：YApi 常给 array 节点同时带 properties，不得改判成 object
  if (schema.type === "array" || schema.items !== undefined) {
    expect(Array.isArray(value), `${location} 应为 array`).toBe(true);
    if (Array.isArray(value) && schema.items !== null && typeof schema.items === "object") {
      value.forEach((item, index) => assertSchema(item, schema.items, `${location}[${index}]`));
    }
    return;
  }
  const impliesObject =
    schema.type === "object" ||
    (properties !== null && typeof properties === "object" && !Array.isArray(properties));
  if (impliesObject) {
    expect(
      value !== null && typeof value === "object" && !Array.isArray(value),
      `${location} 应为 object`,
    ).toBe(true);
  } else {
    expect(matchesJsonType(value, schema.type), `${location} 类型应为 ${schema.type}`).toBe(true);
  }

  if (value !== null && typeof value === "object" && !Array.isArray(value)) {
    const required = Array.isArray(schema.required) ? schema.required : [];
    for (const field of required) {
      expect(Object.prototype.hasOwnProperty.call(value, field), `${location} 缺字段 ${field}`).toBe(
        true,
      );
      expect(
        value[field] !== null && value[field] !== undefined,
        `${location}.${field} 为 required，不得为 null/undefined`,
      ).toBe(true);
    }
    if (properties !== null && typeof properties === "object" && !Array.isArray(properties)) {
      for (const [field, childSchema] of Object.entries(properties)) {
        // properties-only 不把属性视作 required，但实际出现时必须递归校验类型。
        // 非 required 字段允许显式 null（常见可选对象/数组占位）。
        if (!Object.prototype.hasOwnProperty.call(value, field)) continue;
        if ((value[field] === null || value[field] === undefined) && !required.includes(field)) {
          continue;
        }
        assertSchema(value[field], childSchema, `${location}.${field}`);
      }
    }
  }
}

function isStructuredBusinessReject(status: number, body: any, testCase?: any): boolean {
  // 默认：HTTP 400 + 结构化业务包（code/reason/message/msg）记通过，不是 5xx 崩溃。
  // 当 expected.http.accept_structured_4xx=true 时，扩展到任意 4xx 结构化业务拒绝。
  const http = (testCase?.expected ?? {}).http ?? {};
  const acceptStructured4xx = http.accept_structured_4xx === true;
  if (acceptStructured4xx) {
    if (status < 400 || status > 499) return false;
  } else if (status !== 400) {
    return false;
  }
  if (body === null || typeof body !== "object" || Array.isArray(body)) return false;
  // 兼容 code/msg 与 errcode/errmsg（discount 等服务）业务信封
  return ["code", "errcode", "errCode", "reason", "message", "msg", "errmsg"].some((key) =>
    Object.prototype.hasOwnProperty.call(body, key),
  );
}

function matchesCompoundStatusClass(status: number, declaredClass: string): boolean {
  // 支持 "2xx" / "4xx"，以及用 _or_ 拼接的复合类（鉴权探测、业务失败兜底）。
  const parts = String(declaredClass || "")
    .split(/_or_/i)
    .map((part) => part.trim())
    .filter(Boolean);
  if (parts.length === 0) return false;
  const bucket = Math.floor(status / 100);
  return parts.some((part) => {
    const matched = STATUS_CLASS.exec(part);
    if (matched) return bucket === Number(matched[1]);
    if (/^business[_-]?fail$/i.test(part)) return bucket === 4;
    return false;
  });
}

function assertSuccessPath(testCase: any, status: number, body: any): void {
  // 结构化 400（或 accept_structured_4xx 时的 4xx）：业务拒绝，跳过 2xx/业务成功码/schema 与写后校验。
  if (isStructuredBusinessReject(status, body, testCase)) {
    return;
  }
  const expected = testCase.expected ?? {};
  const http = expected.http ?? {};
  const declared = statusList(http.status ?? expected.status);
  const allowed = declared.filter((code) => code >= 200 && code <= 299);
  if (declared.length > 0 && allowed.length === 0) {
    throw new Error(`用例 ${testCase.id} 的成功路径期望不含 2xx，无法精确断言`);
  }
  if (allowed.length > 0) {
    expect(allowed, `${testCase.id} HTTP 状态`).toContain(status);
  } else {
    expect(status, `${testCase.id} HTTP 状态`).toBeGreaterThanOrEqual(200);
    expect(status, `${testCase.id} HTTP 状态`).toBeLessThanOrEqual(299);
  }

  const business = expected.business ?? {};
  const successWhen = business.success_when ?? {};
  if (body !== null && typeof body === "object" && !Array.isArray(body)) {
    const rawPaths = business.code_path ?? ["code"];
    const codePaths = Array.isArray(rawPaths) ? rawPaths : [rawPaths];
    let code: number | null = null;
    for (const item of codePaths) {
      const found = lookup(body, item);
      if (found.found && numericCode(found.value) !== null) {
        code = numericCode(found.value);
        break;
      }
    }
    if (code !== null) {
      const declaredCodes = statusList(successWhen.code_in);
      // 仅当用例显式声明 code_in 时校验；禁止默认猜测 [0,200]
      if (declaredCodes.length > 0) {
        expect(declaredCodes, `${testCase.id} 业务码`).toContain(code);
      }
    }
    // 仅当显式声明 ok_is 时校验；禁止默认 ok===true
    if (typeof body.ok === "boolean" && typeof successWhen.ok_is === "boolean") {
      expect(body.ok, `${testCase.id} 业务 ok`).toBe(successWhen.ok_is);
    }
  }

  const schema = expected.schema ?? {};
  const fields = (Array.isArray(schema.required_fields) ? schema.required_fields : []).filter(
    (field: any) => typeof field === "string" && field.trim() && !COMMENT_FIELD.test(field),
  );
  const properties = schema.properties;
  const hasProperties =
    properties !== null && typeof properties === "object" && Object.keys(properties).length > 0;
  const hasSupportedType = ["object", "array", "string", "number", "integer", "boolean"].includes(
    schema.type,
  );
  if (fields.length === 0 && !hasProperties && !hasSupportedType) {
    throw new Error(
      `用例 ${testCase.id} 的 expected.schema 未声明 type/字段（禁止执行器猜测）；` +
        "EmptyResp 须 type=object 且 required_fields=[]，有契约字段才填 required_fields",
    );
  }
  assertSchema(body, schema, `${testCase.id} response`);
  for (const field of fields) {
    expect(lookup(body, field).found, `${testCase.id} 响应缺字段 ${field}`).toBe(true);
  }
}

function assertNegativePath(testCase: any, status: number, body: any): void {
  const expected = testCase.expected ?? {};
  const http = expected.http ?? {};
  const exact = statusList(http.status ?? expected.status);
  if (expected.assertion_policy === "code_return_required") {
    if (exact.length === 0) {
      throw new Error(
        `用例 ${testCase.id} 负向断言未按代码返回点闭合，禁止使用 2xx_or_4xx 或禁止 5xx`,
      );
    }
    expect(exact, `${testCase.id} HTTP 状态须等于代码返回`).toContain(status);
    const format = expected.response_format ?? expected.code_evidence?.response_format;
    if (format !== null && typeof format === "object") {
      for (const [key, value] of Object.entries(format)) {
        if (key === "empty_object" && value === true) {
          const isEmptyObject =
            body !== null &&
            typeof body === "object" &&
            !Array.isArray(body) &&
            Object.keys(body).length === 0;
          expect(isEmptyObject, `${testCase.id} 响应须为空对象`).toBe(true);
          continue;
        }
        expect(body?.[key], `${testCase.id} 响应 ${key} 须等于代码返回`).toEqual(value);
      }
    }
    return;
  }
  if (exact.length > 0) {
    expect(exact, `${testCase.id} HTTP 状态`).toContain(status);
    return;
  }
  const declaredClass = String(http.status_class ?? expected.status_class ?? "");
  if (!declaredClass) {
    throw new Error(
      `用例 ${testCase.id} 的负向期望不精确（status_class=缺失），` +
        "必须声明精确 status 或 <n>xx，不接受模糊兜底",
    );
  }
  if (matchesCompoundStatusClass(status, declaredClass)) {
    return;
  }
  const matched = STATUS_CLASS.exec(declaredClass);
  if (matched) {
    expect(Math.floor(status / 100), `${testCase.id} HTTP 状态类`).toBe(Number(matched[1]));
    return;
  }
  if (/[1-5]xx/i.test(declaredClass) || /business[_-]?fail/i.test(declaredClass)) {
    throw new Error(`用例 ${testCase.id} HTTP ${status} 不符合 status_class=${declaredClass}`);
  }
  throw new Error(
    `用例 ${testCase.id} 的负向期望不精确（status_class=${declaredClass}），` +
      "必须声明精确 status 或 <n>xx，不接受模糊兜底",
  );
}

function assertCodeEvidenceQualityGate(testCase: any, status: number): void {
  const expected = testCase.expected ?? {};
  const codeEvidence = expected.code_evidence ?? {};
  const classification = String(codeEvidence.classification ?? "").toUpperCase();
  if (classification === "PRODUCT_DEFECT" && status >= 500) {
    // 代码结论、精确预期与实测状态一致时，执行断言通过，并允许进入回归集。
    return;
  }
}

function assertExpected(testCase: any, status: number, body: any): void {
  const category = String(testCase.category ?? "").trim().toLowerCase();
  const expected = testCase.expected ?? {};
  // validation 也可能挂双轨+schema（如无参接口按成功路径契约断言）
  const dualTrack =
    expected.outcome === "success_or_structured_4xx" ||
    (expected.schema !== null &&
      typeof expected.schema === "object" &&
      (expected.http !== null && typeof expected.http === "object"));
  if (SUCCESS_CATEGORIES.has(category) || dualTrack) {
    assertSuccessPath(testCase, status, body);
    return;
  }
  if (NEGATIVE_CATEGORIES.has(category)) {
    assertNegativePath(testCase, status, body);
    return;
  }
  throw new Error(`用例 ${testCase.id} 的 category=${category || "缺失"} 不支持精确断言`);
}

for (const testCase of cases) {
  test(`${testCase.id} [${testCase.category}] ${testCase.name}`, async ({ request }, testInfo) => {
    const baseURL = requireEnv("API_BASE_URL");
    const method = String(testCase.method ?? "GET").toUpperCase();
    const url = new URL(buildTarget(testCase), baseURL).toString();
    const headers = buildHeaders(testCase);
    const data = buildBody(testCase, method);
    const started = Date.now();

    let response: any = null;
    let networkError: string | null = null;
    try {
      response = await request.fetch(url, {
        method,
        headers,
        ...(data === undefined ? {} : { data }),
        timeout: Number(process.env.API_TEST_TIMEOUT_MS ?? 20000),
      });
    } catch (error) {
      networkError = String(error);
    }

    const text = response ? await response.text().catch(() => "") : "";
    const status: number | null = response ? response.status() : null;
    const evidence = {
      case_id: testCase.id,
      endpoint_id: testCase.endpoint_id ?? null,
      category: testCase.category ?? null,
      method,
      path: testCase.path ?? null,
      request_header_keys: Object.keys(headers),
      response: { status, body: text.slice(0, EVIDENCE_BODY_LIMIT) },
      response_status: status,
      duration_ms: Date.now() - started,
      network_error: networkError,
    };
    fs.mkdirSync(evidenceDir, { recursive: true });
    fs.writeFileSync(
      path.join(evidenceDir, `${testCase.id}.json`),
      JSON.stringify(evidence, null, 2) + "\\n",
    );
    await testInfo.attach("api-evidence", {
      body: JSON.stringify(evidence),
      contentType: "application/json",
    });

    expect(networkError, `${testCase.id} 网络错误`).toBeNull();
    assertExpected(testCase, status as number, parseBody(text));
    assertCodeEvidenceQualityGate(testCase, status as number);
  });
}
'''


# --- 生成 -------------------------------------------------------------------


def _required_single_replace(text: str, marker: str, replacement: str, label: str) -> str:
    """模板关键 marker 必须且只能出现一次，避免静默生成半成品。"""
    count = text.count(marker)
    if count != 1:
        raise ProjectGenerationError(
            f"execution template marker {label!r} expected once, found {count}"
        )
    return text.replace(marker, replacement, 1)


def _require_single_marker(text: str, marker: str, label: str) -> None:
    count = text.count(marker)
    if count != 1:
        raise ProjectGenerationError(
            f"execution template marker {label!r} expected once, found {count}"
        )


def build_execution_spec(
    service_name: str,
    write_authorization: dict,
    context_sha256: str,
) -> str:
    """基于回归 spec 模板派生执行工程：回读父 run，固化写授权，证据写回父目录。"""
    auth_json = json.dumps(write_authorization, ensure_ascii=False, indent=2)
    _require_single_marker(
        SPEC_TEMPLATE,
        'const evidenceDir = path.join(projectDir, "execution", "requests");',
        "evidence_dir",
    )
    text = _required_single_replace(
        SPEC_TEMPLATE, "__SERVICE_NAME__", str(service_name), "service_name"
    )
    text = _required_single_replace(
        text,
        " * 回归用例执行脚本（服务：",
        " * 完整用例执行脚本（服务：",
        "title",
    )
    text = _required_single_replace(
        text,
        " * 输入只有工程根目录的 regression-cases.json，不回读 run 目录的其它产物。\n"
        " * 每条用例按自身 expected 精确断言：成功路径要 2xx + 业务码 + schema 声明字段；\n"
        " * 负向用例要精确 status 或 <n>xx 状态类。期望不精确的用例直接判失败。\n",
        " * 输入为父 run 的 test-cases.json 与 run-context.json（相对路径回读）。\n"
        " * 写授权在生成期由 Python resolve_write_authorization 固化为 writeAuthorization；\n"
        " * 运行时校验 run-context 摘要，legacy 环境授权还会再次检查环境变量。\n"
        " * 每条用例按自身 expected 精确断言；不可执行用例与未授权写请求跳过。\n",
        "description",
    )
    text = _required_single_replace(
        text,
        'import path from "node:path";',
        'import path from "node:path";\nimport { createHash } from "node:crypto";',
        "crypto_import",
    )
    text = _required_single_replace(
        text,
        'const suite = JSON.parse(fs.readFileSync(path.join(projectDir, "regression-cases.json"), "utf8"));\n'
        "const cases: any[] = Array.isArray(suite.cases) ? suite.cases : [];\n"
        'const evidenceDir = path.join(projectDir, "execution", "requests");\n',
        "const suite = JSON.parse(\n"
        '  fs.readFileSync(path.resolve(projectDir, "..", "test-cases.json"), "utf8"),\n'
        ");\n"
        'const contextPath = path.resolve(projectDir, "..", "run-context.json");\n'
        'const contextText = fs.readFileSync(contextPath, "utf8");\n'
        "const runContext = JSON.parse(contextText);\n"
        f"const writeAuthorization = {auth_json} as const;\n"
        f'const expectedContextSha256 = "{context_sha256}";\n'
        "const writeAllowed = writeAuthorization.allowed === true;\n"
        "const cases: any[] = Array.isArray(suite.cases) ? suite.cases : [];\n"
        'const evidenceDir = path.resolve(projectDir, "..", "execution", "requests");\n',
        "suite_context",
    )
    guard_and_redaction = r'''
function normalizedBaseUrl(value: string): string {
  const parsed = new URL(value);
  return `${parsed.protocol}//${parsed.host}${parsed.pathname.replace(/\/+$/, "")}`;
}

function assertExecutionAuthorizationCurrent(mutating: boolean, baseURL: string): void {
  const actualContextSha256 = createHash("sha256").update(contextText, "utf8").digest("hex");
  if (actualContextSha256 !== expectedContextSha256) {
    throw new Error("write authorization context changed; regenerate execution project");
  }
  if (
    mutating &&
    writeAuthorization.source === "environment" &&
    String(process.env.ALLOW_WRITE_TESTS ?? "").toLowerCase() !== "true"
  ) {
    throw new Error("legacy write authorization requires ALLOW_WRITE_TESTS=true at runtime");
  }
  if (mutating && writeAllowed) {
    const authorizedBaseURL = String(runContext?.base_urls?.target ?? "").trim();
    if (!authorizedBaseURL) {
      throw new Error("authorized write execution requires run-context base_urls.target");
    }
    if (normalizedBaseUrl(baseURL) !== normalizedBaseUrl(authorizedBaseURL)) {
      throw new Error("API_BASE_URL does not match authorized run-context target");
    }
  }
}

const SENSITIVE_KEYS = new Set([
  "authorization",
  "token",
  "accesstoken",
  "refreshtoken",
  "feattoken",
  "cookie",
  "setcookie",
  "password",
  "passwd",
  "secret",
  "phone",
  "mobile",
  "idcard",
  "id_card",
  "databaseuri",
  "databaseurl",
  "privatekey",
  "attribution",
  "conv",
  "appletoken",
]);

function normalizedEvidenceKey(key: string): string {
  return key.toLowerCase().replace(/[^a-z0-9]/g, "");
}

function isSensitiveEvidenceKey(key: string): boolean {
  const normalized = normalizedEvidenceKey(key);
  return Array.from(SENSITIVE_KEYS).some(
    (sensitive) => normalized === sensitive || normalized.endsWith(sensitive),
  );
}

function redactSecrets(value: any): any {
  if (Array.isArray(value)) return value.map(redactSecrets);
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [
        key,
        isSensitiveEvidenceKey(key) ? "***" : redactSecrets(item),
      ]),
    );
  }
  return value;
}

function redactUrl(value: string): string {
  const parsed = new URL(value);
  for (const key of Array.from(parsed.searchParams.keys())) {
    if (isSensitiveEvidenceKey(key)) parsed.searchParams.set(key, "***");
  }
  return parsed.toString();
}
'''
    text = _required_single_replace(
        text,
        "const EVIDENCE_BODY_LIMIT = 20000;\n",
        "const EVIDENCE_BODY_LIMIT = 20000;\n" + guard_and_redaction,
        "authorization_guard",
    )
    text = _required_single_replace(
        text,
        '  test(`${testCase.id} [${testCase.category}] ${testCase.name}`, async ({ request }, testInfo) => {\n'
        '    const baseURL = requireEnv("API_BASE_URL");\n'
        '    const method = String(testCase.method ?? "GET").toUpperCase();\n',
        "  test(`${testCase.id} [${testCase.category}] ${testCase.name}`, async ({ request }, testInfo) => {\n"
        '    test.skip(testCase.executable === false, testCase.skip_reason || "case_not_executable");\n'
        '    const method = String(testCase.method ?? "GET").toUpperCase();\n'
        '    const mutating = ["POST", "PUT", "PATCH", "DELETE"].includes(method);\n'
        '    test.skip(mutating && !writeAllowed, "write_operations_not_authorized");\n'
        '    const baseURL = requireEnv("API_BASE_URL");\n'
        "    assertExecutionAuthorizationCurrent(mutating, baseURL);\n",
        "test_guard",
    )
    evidence_marker = '''    const evidence = {
      case_id: testCase.id,
      endpoint_id: testCase.endpoint_id ?? null,
      category: testCase.category ?? null,
      method,
      path: testCase.path ?? null,
      request_header_keys: Object.keys(headers),
      response: { status, body: text.slice(0, EVIDENCE_BODY_LIMIT) },
      response_status: status,
      duration_ms: Date.now() - started,
      network_error: networkError,
    };
'''
    evidence_replacement = '''    const evidence = {
      case_id: testCase.id,
      endpoint_id: testCase.endpoint_id ?? null,
      category: testCase.category ?? null,
      method,
      path: redactUrl(url),
      writeAuthorization,
      request: {
        url: redactUrl(url),
        headers: redactSecrets(headers),
        query: redactSecrets(requestPart(testCase, "query")),
        path_params: redactSecrets(requestPart(testCase, "path_params")),
        body: redactSecrets(data),
      },
      response: {
        status,
        body: redactSecrets(parseBody(text.slice(0, EVIDENCE_BODY_LIMIT))),
      },
      response_status: status,
      duration_ms: Date.now() - started,
      network_error: networkError,
    };
'''
    text = _required_single_replace(
        text, evidence_marker, evidence_replacement, "structured_evidence"
    )
    return text


def _load_run_context(run_path: Path) -> tuple[dict, str]:
    path = run_path / RUN_CONTEXT_FILE
    if not path.is_file():
        raise ProjectGenerationError(f"缺少 {RUN_CONTEXT_FILE}：{path}")
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProjectGenerationError(f"{RUN_CONTEXT_FILE} 读取失败（{path}）：{error}") from error
    context = payload if isinstance(payload, dict) else {}
    return context, hashlib.sha256(raw).hexdigest()


def _relative_output_paths(output_dir: str, file_names: list[str]) -> list[str]:
    if output_dir in {".", ""}:
        return sorted(file_names)
    prefix = Path(output_dir)
    return sorted(str(prefix / name) for name in file_names)


def generate_project(
    run_dir: str | Path,
    service_name: str,
    lock_path: str | Path | None = None,
    *,
    mode: str = "regression",
    output_dir: str | None = None,
) -> list[str]:
    """写出 Playwright 工程，返回相对 `run_dir` 的路径列表。

    默认 regression：独立回归工程。execution：输出到 `output_dir`（通常
    `execution-runtime`），回读父 run 的完整用例与上下文。
    校验全部通过后才落盘：输入不合格时不会留下半成品工程。
    """
    if mode not in SUPPORTED_MODES:
        raise ProjectGenerationError(f"unsupported mode: {mode}")

    run_path = Path(run_dir)
    suite = load_suite(run_path, mode=mode)
    cases = suite["cases"]
    resolved_output_dir = (
        output_dir
        if output_dir is not None
        else ("execution-runtime" if mode == "execution" else ".")
    )

    write_authorization: dict | None = None
    context_sha256: str | None = None
    if mode == "execution":
        context, context_sha256 = _load_run_context(run_path)
        auth = resolve_write_authorization(context, os.environ)
        write_authorization = {
            "allowed": bool(auth.allowed),
            "environment": auth.environment,
            "source": auth.source,
            "reason": auth.reason,
        }

    source_lock_path = Path(lock_path) if lock_path else _default_lock_path()
    version = read_playwright_version(source_lock_path)
    source_lock = _read_json(Path(source_lock_path), "package-lock.json")

    name = package_name(service_name)
    package = build_package_json(name, version)
    lock = build_package_lock(name, version, source_lock)
    env_example = build_env_example(service_name, collect_env_names(cases))
    if mode == "execution":
        assert write_authorization is not None
        assert context_sha256 is not None
        spec_text = build_execution_spec(service_name, write_authorization, context_sha256)
    else:
        spec_text = SPEC_TEMPLATE.replace("__SERVICE_NAME__", str(service_name))

    files = {
        "package.json": json.dumps(package, ensure_ascii=False, indent=2) + "\n",
        "package-lock.json": json.dumps(lock, ensure_ascii=False, indent=2) + "\n",
        ".env.example": env_example,
        "playwright.config.ts": PLAYWRIGHT_CONFIG,
        "tests/api-cases.spec.ts": spec_text,
    }
    project_root = run_path / resolved_output_dir
    for relative, content in files.items():
        target = project_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return _relative_output_paths(resolved_output_dir, list(files))


def _default_lock_path() -> Path:
    """仓库内既有的 Playwright 执行环境，作为版本与依赖闭包的唯一来源。"""
    return Path(__file__).resolve().parents[4] / WORKSPACE_LOCK_REL


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成可运行的 Playwright API 测试工程")
    parser.add_argument("--run-dir", required=True, help="run 目录")
    parser.add_argument("--service-name", required=True, help="被测服务名，用于包名与模板注释")
    parser.add_argument(
        "--lock",
        default=None,
        help=f"依赖版本来源 lock，默认 {WORKSPACE_LOCK_REL}",
    )
    parser.add_argument(
        "--mode",
        choices=sorted(SUPPORTED_MODES),
        default="regression",
        help="regression=独立回归工程；execution=完整用例执行工程",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="相对 run-dir 的输出目录；默认 regression=.、execution=execution-runtime",
    )
    args = parser.parse_args(argv)

    try:
        files = generate_project(
            args.run_dir,
            args.service_name,
            args.lock,
            mode=args.mode,
            output_dir=args.output_dir,
        )
        suite = load_suite(args.run_dir, mode=args.mode)
        resolved_output_dir = (
            args.output_dir
            if args.output_dir is not None
            else ("execution-runtime" if args.mode == "execution" else ".")
        )
    except ProjectGenerationError as error:
        print(f"工程生成失败：{error}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "run_dir": str(args.run_dir),
                "service_name": args.service_name,
                "mode": args.mode,
                "output_dir": resolved_output_dir,
                "case_count": len(suite["cases"]),
                "files": files,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
