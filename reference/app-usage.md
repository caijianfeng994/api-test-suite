# APP 调用证据查询方法

在 catalog 定稿前，对每个保留接口查询是否被 APP 调用，写入 `<run_dir>/app-usage-evidence.json`，并回填 `interface.app_usage`。细则与 `scripts/app_usage.py` 校验一致。

**原则：**「未搜到」≠「未被 APP 调用」。仅双侧证据同时成立才可判 `not_called_by_app` 并省略 auth；其余一律 `unknown` 或 `called_by_app`，仍生成 auth。

## 查询顺序（强制）

```text
后端 METHOD+path
  → CodeWiki route / 网关改写（转换前/后 path、Host、上游）
  → 全部已知 APP 仓 + 后台/Web 仓代码搜索
  → 匹配环境 TLS（按转换后 path）
  → 三态判定 → 写 evidence + catalog 摘要
```

### 1. CodeWiki 路由与网关

1. 用后端 `METHOD + path` 调 CodeWiki `get_wiki_route_reference`（可辅以 `get_api_reference` / `search_wiki`）。
2. 记录：转换前 path、转换后 path、Host、上游服务。
3. 客户端实际请求的是转换前 path；TLS 与代码搜索须覆盖两侧 path。

### 2. 客户端代码仓

搜索**全部**下列已知仓中的原始 path、网关前 path、API 常量/调用函数。每条证据记录：仓、文件或 Wiki 页、匹配 path、调用方类型。

| 类型 | 已知仓（GitLab / Wiki 名，按实际可访问清单扩展） |
|-|-|
| APP | `android/onionpocket`、`android/ycmath345-android`、`ios/apps/ycplanet-ios`、家长端/学生端 APP 仓、Harmony APP 仓 |
| 后台/Web | `frontend/shadow`、`shadow`、`shadow-admin`、运营后台/Web 仓、定时任务与服务间调用仓 |

- APP 仓命中可追溯直接调用 → 倾向 `called_by_app`。
- 仅后台/Web/定时任务/服务间命中，且**本次可访问 APP 仓清单已全部逐仓搜过无命中**（`coverage_complete=true`）→ 代码侧支持「非 APP」，但仍须 TLS 双侧确认。
- **只搜了一个 APP 仓无匹配，或未按本次可访问清单逐仓记录 → 代码覆盖不足 → `unknown`。**
- 上表仓名为形状示例，**禁止当固定清单照抄**。每次运行必须列出本次可访问 APP 仓，并在证据中逐仓记录 miss/hit。

### 3. TLS 运行时

1. `get_wiki_runtime_evidence` 取与目标环境匹配的 topic，再用 `user-volcengine-tls` / `search_logs_v2_tool`。
2. 按**转换后 path** 查询；默认时间窗最近 7 天，必须写入 `time_range.start` / `end`。
3. 统计并落盘：`hostname`、`app-from`、`client-type`、`client-version`、`uid`、总请求数 `matched_requests`、明确 APP 标识请求数 `app_identified_requests`。

**零日志 / 查询失败 / 字段不可用 → 不得判 `not_called_by_app`，只能 `unknown`。**

### 4. 三态判定

| status | 条件 |
|-|-|
| `called_by_app` | APP 仓有可追溯调用链，**或** TLS 有明确 Android/iOS/APP 包/版本标识流量 |
| `not_called_by_app` | **同时**：代码侧仅后台/Web/定时/服务间；`coverage_complete=true` 且 `covered_app_repos` 覆盖本次可访问清单（至少逐仓记录 2 个不同客户端仓 miss，实际须覆盖清单全部）；`matched_requests` 为真正 int 且 `>0`；`app_identified_requests` 为真正 int 且 `==0`；双侧无冲突 |
| `unknown` | 其他一切情况（含：缺 `app_usage`、零日志、单仓无匹配、证据冲突、查询失败） |

## catalog 摘要

```json
{
  "app_usage": {
    "status": "not_called_by_app",
    "evidence_ref": "app-usage-evidence.json#F105",
    "reason": "代码调用链仅指向运营后台，且统计时间窗内无 APP 标识流量"
  }
}
```

旧 catalog 无 `app_usage` → 按 `unknown`，必须生成 auth。

## 完整证据示例（允许省略 auth）

仓名仅为形状示例，**禁止照抄为固定清单**。必须按**本次可访问 APP 仓清单**逐仓写 `app_repo_miss`/`hit`，并设 `covered_app_repos` 与 `coverage_complete=true`。下面只演示至少 2 个不同客户端仓 miss；真实运行须覆盖清单全部仓。

```json
{
  "schema_version": "1.0",
  "generated_at": "2026-09-22T12:00:00+08:00",
  "interfaces": [
    {
      "endpoint_id": "F105",
      "method": "GET",
      "path": "/api/categories",
      "status": "not_called_by_app",
      "covered_app_repos": [
        "android/onionpocket",
        "ios/apps/ycplanet-ios"
      ],
      "coverage_complete": true,
      "code_evidence": [
        {
          "kind": "admin_client_call",
          "repo": "frontend/shadow",
          "path": "/api/payment/categories",
          "caller_type": "admin_web"
        },
        {
          "kind": "gateway_rewrite",
          "source_path": "/api/payment/categories",
          "target_path": "/api/categories",
          "host": "shadow.yangcong345.com",
          "upstream": "payment"
        },
        {
          "kind": "app_repo_miss",
          "repo": "android/onionpocket",
          "path": "/api/categories",
          "caller_type": "app_android"
        },
        {
          "kind": "app_repo_miss",
          "repo": "ios/apps/ycplanet-ios",
          "path": "/api/categories",
          "caller_type": "app_ios"
        }
      ],
      "runtime_evidence": {
        "source": "volcengine_tls",
        "time_range": {
          "start": "2026-09-15T00:00:00+08:00",
          "end": "2026-09-22T00:00:00+08:00"
        },
        "matched_requests": 9333,
        "app_identified_requests": 0,
        "hostnames": ["shadow.yangcong345.com"],
        "app_from_values": ["shadow-gateway-apisix;good"],
        "client_types": [""],
        "client_versions": [""],
        "uids": []
      },
      "reason": "代码调用链仅指向运营后台，且统计时间窗内无 APP 标识流量"
    }
  ]
}
```

`not_called_by_app` 时 validator（`scripts/app_usage.py`）要求，与脚本一致：

- `coverage_complete is True`（严格布尔 `true`）
- `covered_app_repos` 为至少 2 个非空、去重后的字符串仓名
- `code_evidence` 非空，且对每个 covered repo 都有对应 `{kind: "app_repo_miss", repo: ...}`；缺任一拒绝
- 若存在任一 `{kind: "app_repo_hit", ...}`，拒绝 `not_called_by_app`
- 对象型 `runtime_evidence`；真正 int 的 `matched_requests>0` 与 `app_identified_requests==0`；非空 `time_range.start/end`
- `runtime_evidence` 必须存在列表字段（允许空列表）：`hostnames`、`app_from_values`、`client_types`、`client_versions`、`uids`
- 所有错误带 endpoint ID，并聚合返回

## 明确反例（只能 `unknown`，仍生成 auth）

| 场景 | 错误判定 | 正确 |
|-|-|-|
| TLS 最近 7 天 0 条 | `not_called_by_app` | `unknown`（无真实流量） |
| 仅 `android/onionpocket` 无匹配 + 后台有调用 | `not_called_by_app` | `unknown`（APP 仓覆盖不足） |
| 单仓无匹配 + 零日志 | `not_called_by_app` | `unknown` |
| 空 `client-type` 但无其他 APP 标识 | 单独证明非 APP | 不够；结合代码与 `app_identified_requests` |
| 缺 `app_usage` 字段 | 省略 auth | 视为 `unknown`，生成 auth |

## auth 生成

| status | auth |
|-|-|
| `not_called_by_app`（且证据通过校验） | 省略；计划写 `UNCOVERED [auth] 已确认非 APP 调用接口；证据见 app-usage-evidence.json#<id>` |
| `called_by_app` / `unknown` / 缺字段 | **必须**生成 `category=auth` |

functional / validation / error / side_effect / list 规则不变。
