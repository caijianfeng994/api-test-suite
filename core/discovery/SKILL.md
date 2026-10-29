---
name: api-test-suite-discovery
description: Use when api-test-suite must independently resolve API definitions from YApi first, then keep only paths present in the GitLab service proto files, for functional, refactor, and migration runs.
---

# 接口发现内核

## 输出

必须同时写入：

- `<run_dir>/interface-catalog.json`：机器可读（含 YApi∩proto，以及 **仅 proto 有** 的接口）
- `<run_dir>/interface-catalog.md`：人工审阅（含保留清单与过滤清单）
- `<run_dir>/app-usage-evidence.json`：每个保留接口的 APP 调用双侧证据（见 `reference/app-usage.md`）
- `<run_dir>/yapi-ping.json`：YApi 连通探测结果
- `<run_dir>/yapi-raw.json`：YApi 原始拉取（有接口时）
- `<run_dir>/codewiki-raw.json`：GitLab 服务 proto 派生的 HTTP 路径（CodeWiki `get_api_reference`，来源 `api/*/*.proto` 的 `google.api.http`）
- `<run_dir>/yapi-proto-filter.json`：过滤脚本输出（kept / filtered）

每个保留接口至少包含：

```json
{
  "id": "F1",
  "name": "接口名称",
  "method": "GET",
  "path": "/api/items",
  "base_url": "https://example.test",
  "headers": [],
  "path_parameters": [],
  "query_parameters": [],
  "request_body": null,
  "responses": [],
  "auth": {},
  "side_effects": [],
  "proto_match": true,
  "yapi_documented": true,
  "presence": "yapi_and_proto",
  "source_label": "YApi+proto",
  "app_usage": {
    "status": "unknown",
    "evidence_ref": "app-usage-evidence.json#F1",
    "reason": "待 discovery 完成 APP 调用查询后回填"
  },
  "yapi": { "id": "141510", "path": "/good/api/items", "name": "接口名称" },
  "source": {
    "type": "gitnexus",
    "reference": "good",
    "service_name": "good",
    "repo": "good",
    "code_evidence": []
  },
  "sources": [
    { "type": "yapi", "reference": "141510", "path": "/good/api/items" }
  ]
}
```

`source.type` 允许：`yapi`、`endpoint`、`gitnexus`、`user_input`、`openapi`、`protobuf`、`contract_doc`、`contract_issue`。多来源并存时用 `sources` 数组。CodeWiki 拉到的 proto HTTP 映射 **仍写入** `source.type=gitnexus`（字段 `reference` / `service_name` / `repo` / `code_evidence` 与原先一致）。

**catalog.interfaces 禁止包含 `proto_match=false`、`filtered.reason=not_in_proto` 或 `filtered.reason=code_offline` 的条目。** 仅 YApi 有、proto 无，或实现已标下线的接口只出现在 `filtered`，后续不得为其编写或维护用例。`presence=proto_only`（`source_label=proto有`）在**未**命中 `code_offline` 时必须进入 catalog 并维护 case。

## 默认门禁：先 YApi，再 proto 路径

`functional` / `refactor` / `migration` 生成或执行用例时，discovery **必须**按下列顺序，不得颠倒。不得把未标明来源的 CodeWiki 清单直接当 YApi 文档：

1. **探测 YApi 连通**
2. **从 YApi 拉取候选接口**（必须有 method + path）
3. **从对应 GitLab 服务的各 proto service 拉取 HTTP 路径**
4. **YApi 与 proto 路径对齐**：两边都有的保留；仅 proto 有的也保留并标明 `proto有`
5. YApi 有、proto 无 → 自动过滤，不写 case、不维护
6. proto 有、YApi 无 → **写入 catalog，维护 case**，`presence=proto_only`，`source_label=proto有`
7. **代码已标下线**（`reference/code-offline-filter.md`）→ 从保留清单移出，`filtered.reason=code_offline`，**不写 case、不维护**（即使 proto/YApi 仍声明该 path）

`run_mode=contract` 且用户**只**提供 OpenAPI / 契约 Issue、明确无 YApi 时，允许跳过 YApi 门禁，但须在 `warnings` 写明；一旦存在 YApi 链接、分类、项目或可搜索服务名，仍走本门禁。

禁止调用 GitNexus 图谱工具：`list_gitnexus_repos`、`get_route_graph`、`get_symbol_context`、`analyze_api_impact`、`search_code_graph`（**例外**：`reference/code-offline-filter.md` 的下线扫描，以及 `reference/missing-required-code-status.md` 的缺参校准，均须最小范围）。禁止用本地随便扫仓代替 CodeWiki proto 清单；proto 路径以 CodeWiki 接口文档（由 `api/<service>/<service>.proto` 的 `google.api.http` 生成）为准。

## 输入适配器

### 1. YApi 连通（必须先做）

会话已注册 YApi MCP 时，先做一次只读冒烟（`get_interface_detail`、`list_interfaces_by_project` 或 `search_interface`）。**无论会话是否挂载 MCP，都必须落盘探测结果**，未注册时执行：

```bash
python .claude/skills/api-test-suite/scripts/fetch_yapi_interface.py \
  --ping \
  --output "<run_dir>/yapi-ping.json"
```

脚本默认连 `http://yapi-common-mcp.7to12/mcp`。系统 DNS 解析不了 `.7to12` 时，自动向 `YAPI_CLUSTER_DNS`（默认 `10.8.8.89`）查 A 记录并以 IP 回退。`YAPI_GLOBAL_TOKEN` 必填，Skill 不内置 token。可用 `YAPI_COMMON_MCP_URL` / `YAPI_BASE_URL` 覆盖地址。

`.7to12` / `.yc345.tv` 等集群地址由 `scripts/internal_http.py` 强制直连，不走系统代理（走代理会拿到 502）。需要额外直连后缀时用 `API_TEST_SUITE_DIRECT_SUFFIXES` 逗号分隔追加。新增 stdlib HTTP 调用必须用 `open_url`，不得直接 `urlopen`。

`ok=false`：`discovery=blocked`，**停止**，不得用 proto 全量冒充 YApi 候选、不得编造接口。

### 2. 拉取 YApi 接口

连通成功后拉取候选。会话有工具则直接调用；否则必须走脚本。`ok=false` 时 discovery 记 blocker，不得编造字段。

```bash
python .claude/skills/api-test-suite/scripts/fetch_yapi_interface.py \
  --url "https://yapi.yc345.tv/project/<projectId>/interface/api/<interfaceId>" \
  --output "<run_dir>/yapi-raw.json"
```

项目页 / 服务全量：

```bash
python .claude/skills/api-test-suite/scripts/fetch_yapi_interface.py \
  --project-url "https://yapi.yc345.tv/project/<projectId>/interface/api" \
  --output "<run_dir>/yapi-raw.json"
```

分类页用 `--cat-url` / `--cat-id`；项目页用 `--project-url` / `--project-id`；关键词用 `--search`。

- 具体接口 URL / ID → `get_interface_detail`（或 `--url` / `--interface-id`）
- `cat_` 分类 URL / `catID` → `list_interfaces_by_cat`（或 `--cat-url` / `--cat-id`）
- **项目页 / projectId / 「某服务全部接口」且已映射到 YApi 项目** → **优先** `list_interfaces_by_project`（或 `--project-url` / `--project-id`）。详见 `core/yapi_search`「项目全量接口」。
- 关键词且尚未确定项目 → `search_interface`（或 `--search`），候选不唯一时让用户确认 YApi 项目或分类；**禁止**用搜索冒充全量
- `list_interfaces_by_project` 失败时才旁路：用户 `cat_` / `catID`、登录态 `getCatMenu`、或有界 `catId` 扫描后再 `list_interfaces_by_cat` 合并去重
- 全量服务：优先项目全量工具；YApi 拉空时仍须拉 proto，将仅 proto 有的接口标 `proto有` 后维护 case

分类返回 100 条时记录截断风险。不得把分类 URL 传给具体接口工具。

YApi 无任何 method+path：仍须拉取 proto HTTP 路径；catalog 中条目全部为 `presence=proto_only`、`source_label=proto有`，并维护 case。须在 warnings 写明「YApi 未返回接口」。

### 3. GitLab proto HTTP 路径（CodeWiki）

在已解析 `service_name`（用户传入、Wiki 仓名、或 path 第一段）后拉取该服务各 proto service 的 HTTP 映射。会话已注册 `list_codewiki_repos` / `get_api_reference` 时直接调用；否则：

```bash
python .claude/skills/api-test-suite/scripts/fetch_codewiki_interfaces.py \
  --service-name "<service_name>" \
  --path "<可选 METHOD 对应的 path>" \
  --output "<run_dir>/codewiki-raw.json"
```

脚本默认连接 `https://codewiki-mcp-inner.yc345.tv/api/mcp`。`CODEWIKI_MCP_TOKEN` 必填，Skill 不内置 token。可用 `CODEWIKI_MCP_URL` 覆盖地址。`ok=false` 或仓库不唯一时 discovery 记 blocker。

前端路由 / 网关页可用 `get_wiki_route_reference` 作补充，**不得**替代 `get_api_reference`，也不得把未出现在 proto HTTP 映射中的路径写入 catalog。

#### 服务 → Wiki 仓匹配

1. 调用 `list_codewiki_repos`（`type=repo`）。后续 `get_api_reference` 的 `repo` 必须使用返回的 Wiki 名（如 `good`，不是 `backend/good`）。
2. 用传入服务名匹配：优先完整 `name` 精确匹配；否则匹配最后一段（`backend/good` ↔ `good`）。
3. 零匹配：列出相近候选并停止，只询问服务名/Wiki 仓，不猜测。
4. 多于一个精确匹配：列出冲突仓并停止。
5. 唯一匹配后才允许拉接口文档。

migration 对 source、target **分别**匹配、分别做 YApi/proto 对齐（含 proto有）。

#### proto 路径证据

唯一匹配后必须：

1. `get_api_reference`（`repo` 必填；用户给了具体 path 时再传 `keyword`）。
2. 从文档中的 HTTP 映射建立 `HTTP Method + Path + Handler` 清单，并记录对应 proto 文件（如 `api/sku/sku.proto`、`api/sku_good/sku_good.proto`）。
3. 无法从文档确认的字段写「待确认」，不得按命名猜测。

无服务名且无法从 YApi path 解析 DNS label 时：proto 门禁无法执行，`discovery=blocked`，询问 GitLab 服务名，禁止只凭 YApi 写执行用例。

### 4. 路径过滤（必须跑脚本）

```bash
python .claude/skills/api-test-suite/scripts/filter_yapi_by_proto.py \
  --yapi "<run_dir>/yapi-raw.json" \
  --proto "<run_dir>/codewiki-raw.json" \
  --service-name "<service_name>" \
  --output "<run_dir>/yapi-proto-filter.json"
```

匹配规则：

- 比较 `METHOD`（大小写无关）+ 规范化 path（去 query、去尾斜杠）
- 允许 YApi 带网关前缀、proto 不带（或相反），例如 `/good/api/v2/good` ↔ `/api/v2/good`（前缀等于 `service_name` 最后一段）
- **禁止**按接口中文名、RPC 名相似度配对

过滤结果：

| 情况 | 动作 |
|-|-|
| YApi 有且 proto 有同一路径 | 写入 `catalog.interfaces`，`presence=yapi_and_proto`，`source_label=YApi+proto`；冲突写入 `warnings` 与后续契约 `CONFLICTING`，禁止静默选边 |
| YApi 有、proto 无 | `filtered.reason=not_in_proto`，**不写 case、不维护** |
| proto 有、YApi 无 | 写入 `catalog.interfaces`，**维护 case**，`presence=proto_only`，`source_label=proto有`，`yapi_documented=false` |
| 实现已标下线（见下） | `filtered.reason=code_offline`，**不写 case、不维护**（即使 YApi/proto 仍有） |

将 `yapi-proto-filter.json` 的 `interfaces` 拷入 `interface-catalog.json`（可加 `base_url` 等）。`interface-catalog.md` 必须分四节：**保留（YApi+proto）**、**保留（proto有）**、**已过滤（YApi 有、proto 无，不维护）**、**已过滤（代码已标下线，不维护）**。

### 4.1 代码已标下线过滤（必须）

对步骤 4 产出的每条 kept 接口，**必须**按 `reference/code-offline-filter.md` 扫描 Handler/Service 实现，并写入 `<run_dir>/code-offline-filter.json`（含 `scan.complete=true` 与覆盖每条 path 的 `scan.by_path`）：

- 主路径：path → Handler/Service → 命中下线注释或下线空壳 → 移出 `interfaces`，写入 `filtered`（`reason=code_offline` + `evidence`）
- **主路径失败（证据缺口 / 无 Handler / Kratos 映射空）→ 必须走兜底**，禁止直接 `unknown` 放行：
  1. path 末段 / 近义符号检索
  2. YApi/Wiki 接口名 → Go 方法名（允许命名不一致）
  3. **同文件兄弟扫描**：本 run 已在某 `internal/service/*.go` 上命中 `handler_comment` 下线时，必须扫该文件全部导出方法并反查 path
  4. openspec + `http.go` 取消注册
- 兜底耗尽仍无证据 → `status=unknown`，`fallback_attempted=true`，记 `warnings` 后仍可保留；**不得**把「证据缺口」写成「已确认未下线」
- 本步允许最小范围调用 `list_gitnexus_repos` / `search_code_graph` / `get_symbol_context` / `analyze_api_impact`（仅用于下线判定）

未完成本步（含缺 `scan.by_path` 覆盖）不得将 discovery 标为 completed（单接口且用户已用源码证明未下线时可在 warnings 注明后跳过全量扫描）。

### 5. APP 调用证据（catalog 定稿前必须完成）

对每个即将写入 `catalog.interfaces` 的保留接口（已排除 `code_offline`），**必须**按 `reference/app-usage.md` 执行：

1. CodeWiki route / 网关：记录转换前/后 path、Host、上游
2. 搜索全部已知 APP 仓与后台/Web 仓
3. 匹配环境 TLS：统计 `hostname`、`app-from`、`client-type`、`client-version`、`uid`、`matched_requests`、`app_identified_requests`
4. 判定 `called_by_app` | `not_called_by_app` | `unknown`
5. 写入 `<run_dir>/app-usage-evidence.json`，并回填每条接口的 `app_usage` 摘要（`status` / `evidence_ref` / `reason`）

零日志、单仓无匹配、查询失败 → `unknown`。不得在缺证据时写 `not_called_by_app`。未完成本步不得将 discovery 标为 completed。

### endpoint_id

若存在 `get_endpoint_details`，调用并归一化后，仍须能对应到 YApi 或用户补的 YApi，并过 proto 路径门禁。工具不可用且无 YApi 时停止。

### 显式定义

用户直接提供 method、path、Base URL、参数与响应时 `source.type=user_input`。只要能解析服务名，仍须做 proto 路径确认：proto 中无该 path 则过滤、不写 case。用户明确声明「仅契约设计、跳过 YApi/proto 门禁」时除外，并写入 warnings。

### OpenAPI / Proto / 契约文档

`run_mode=contract` 且无 YApi 时，可从 OpenAPI、protobuf/schema、契约 Issue 归一化；`source.type` 记为 `openapi` / `protobuf` / `contract_doc` / `contract_issue`。一旦同时有 YApi，可维护用例如上：YApi∩proto + 仅 proto 有。

## Base URL

按以下优先级：

1. 用户显式输入
2. endpoint/YApi 工具明确返回
3. `API_BASE_URL`
4. functional / 已解析服务名的 k8s 服务名规则
5. CodeWiki 证据中明确出现的 `.7to12` / `.backsys` 主机（仍不唯一则停止）

### k8s 服务名规则

用户指定目标为 k8s 测试环境，且服务名已知并满足 DNS label 时，允许按已确认规则生成：

```text
path = /good-order/good/list
service_name = good-order
base_url = http://good-order.7to12/
final_url = http://good-order.7to12/good-order/good/list
```

- Base URL 只取 path 第一段作为服务名，不得把后续业务路由拼进主机名。
- 最终 URL 必须保留完整原始 path，不得因已用于服务名解析而删除第一段。
- 第一段不满足 DNS label、path 为空、环境不是 k8s 测试环境或用户指定了其他 Base URL 时，不应用该规则。
- 命名空间仍不唯一时停止并询问，禁止继续猜测。

执行类模式（`full` / `execute` / `baseline` / `replay` / migration 执行）都没有 Base URL 时写 blocker，禁止猜测域名。

`run_mode=contract`（以及纯设计的 `plan` 且用户声明不执行）允许 `base_url` 为空，但须在 catalog 与 `warnings` 中注明「待补充环境」。

## 完成门禁

- `yapi-ping.json` 存在且连通成功（合同允许跳过 YApi 的 contract 除外）
- 已拉取 YApi 候选，或明确记录「YApi 无接口」且 `interfaces` 为空
- 已拉取对应服务 proto HTTP 路径（有服务名时）；仓库不唯一则 `discovery=blocked`
- 已运行 `filter_yapi_by_proto.py`；catalog 含 YApi∩proto 与仅 proto 有；不含 `not_in_proto` / `code_offline`
- 已按 `reference/code-offline-filter.md` 完成下线扫描（含证据缺口兜底与 `code-offline-filter.json`/`scan.by_path`）；命中项仅在 `filtered`
- 已写入 `app-usage-evidence.json`；每个保留接口有 `app_usage.status`（三态之一）与 `evidence_ref`
- method、path、来源非空；所有保留项必须有 proto `code_evidence`；仅 `presence=yapi_and_proto` 必须有 YApi 引用，`presence=proto_only` 不要求 YApi
- 执行模式所需 Base URL 已确定；`contract` 模式可暂缺
- 目录 JSON 可解析且 ID 唯一
- 更新 `run-context.stages.discovery=completed` 和 artifacts
