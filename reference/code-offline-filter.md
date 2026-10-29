# 代码已标下线：不维护用例

## 目的

YApi∩proto（含仅 proto 有）仍可能对应**业务已在实现层标下线**的接口。此类接口：

- **不得**写入可执行 `test-cases` / 计划可跑编号
- **不得**作为 fixture 绑定目标、回归基线、GitLab 用例维护对象
- 在过滤产物中标记 `reason=code_offline`，与 `not_in_proto` 同级「不维护」

## 判定标准（须同时满足语义）

须有**可追溯的实现层下线证据**（非仅 YApi 标题），例如：

| 证据类型 | 示例 |
|----------|------|
| 中文下线注释 | `// 2025.4.1日下线`、`// 已下线`、`// 下线`、`/* 业务下线 */`（紧贴 Handler/Service） |
| Deprecated + 业务下线说明 | 注释同时含 deprecated / 下线，且实现为空壳 |
| 下线空壳实现 | 注释标明下线后，直接 `return` 空列表 / `State: false` / 固定空 Reply，且无真实业务分支 |
| 仓库 openspec / 变更说明明确「下线」且与路由拆除一致 | 如 `openspec/specs/*-offline*/spec.md` 写明 MUST NOT 暴露某 HTTP 路径，且 `internal/server/http.go`（或等价注册点）已取消对应 `Register*HTTPServer` |

**不足以下线依据（不得单独据此过滤）：**

- 仅 YApi 标题含「下线」而代码仍有完整实现与路由注册
- mux 运行时 404 / 环境未挂载（属执行态，归 `fixture_or_route`，不是 `code_offline`）
- 配置中心开关临时关闭（属运行态，不是「代码已标下线」）

## 扫描步骤（强制，不可跳过）

在 `filter_yapi_by_proto.py` 产出 kept 之后、定稿 `interface-catalog.json` / 跑 APP 调用证据之前，对**每一条即将保留的接口**做下线扫描。

### A. 主路径（path → Handler）

1. 用 `get_api_reference` / Wiki 路由表 / `analyze_api_impact`（最小范围）拿到 `HTTP path → Handler/Service 符号`
2. 用 `get_symbol_context` / `search_code_graph` / 读源码确认符号体
3. 命中「判定标准」→ `code_offline`

### B. 兜底（主路径失败时**必须**执行，禁止直接 `unknown` 放行）

出现任一下列情况，**不得**停在「证据缺口 → 保留」：

- Wiki / CodeWiki 对该 path 标注 `证据缺口`、无 Handler、映射为空
- Kratos / 生成码导致 path 字符串搜不到 `Register*HTTPServer` 字面量
- 同仓库其它 path 已能解析到某 `internal/service/*.go`，本 path 却映射失败

对失败条目按顺序做兜底（命中即停，写入 `via`）：

1. **path 片段检索**  
   取 path 末 2～3 段（如 `batch/user/strategy_id`、`user/strategy_id`）+ 方法名（GET/POST）在仓内 `search_code_graph` / `rg`：`strategy_id`、`BatchUser`、`UserUseStrategy` 等；命中 Service/Handler 函数后按「判定标准」检查。

2. **YApi / Wiki 接口名 → 符号**  
   用 catalog 的 `title` / YApi 名 / Wiki 方法名（如 `ClientBatchUserCanUseStrategy`）搜符号；注意 **HTTP 名与 Go 方法名常不一致**（例：Wiki `ClientBatchUserCanUseStrategy` ↔ Go `ClientBatchGetUserUseStrategyIdV4`），需同时搜近义前缀 `ClientBatch*Strategy*`、`ClientGet*Strategy*`。

3. **同文件兄弟扫描（高优先级）**  
   若本 run 已在同一 `internal/service/foo.go`（或同 Handler 文件）上对**其它** path 判定为 `code_offline`（尤其 `handler_comment` / 同一句 `// …下线`），则**必须**打开该文件，扫描**全部**导出方法：凡带同类下线注释或空壳 return 的方法，再反查 Wiki/proto/path，把对应 HTTP 接口一并标 `code_offline`。  
   > 反例教训：`strategyv4.go` 中已扫到部分 V4 下线方法，却漏掉同文件的 `ClientBatchGetUserUseStrategyIdV4` / `ClientGetUserUseStrategyIdV4`（对应 `/client/v4/batch/user/strategy_id`、`/client/v4/user/strategy_id`）。

4. **openspec / http 注册**  
   扫 `openspec/specs/*offline*` 与 `internal/server/http.go`（或等价）取消注册的路由前缀。

### C. 仍无法定位时

兜底 1～4 均无符号、无 openspec 命中 → `status=unknown`，记入 `warnings`，**可以**暂保留进 catalog，但：

- 必须在 `scan.by_path` 写明已做哪些兜底（`fallback_attempted: true` + 步骤列表）
- **禁止**把「主路径证据缺口」写成「已确认未下线」
- 执行阶段若该 path 出现 mux 404，优先回查是否漏扫同文件下线注释，而不是直接当环境问题结案

```text
主路径成功 → 判定 offline / kept
主路径失败 → 必须走兜底 B
兜底命中下线 → code_offline
兜底后仍无证据 → unknown（保留 + warnings），不得跳过兜底
```

## 产物格式

### `code-offline-filter.json`（推荐独立文件；也可合并进 `yapi-proto-filter.json`）

```json
{
  "filtered": [
    {
      "method": "GET",
      "path": "/revenue-strategy/client/v4/user/strategy_id",
      "reason": "code_offline",
      "evidence": {
        "via": "fallback_file_sibling",
        "file": "internal/service/strategyv4.go",
        "symbol": "ClientGetUserUseStrategyIdV4",
        "snippet": "// 2025.4.1日下线",
        "kind": "handler_comment"
      }
    }
  ],
  "excluded_case_ids": [],
  "scan": {
    "complete": true,
    "by_path": {
      "GET /revenue-strategy/client/v4/user/strategy_id": {
        "status": "offline",
        "via": "fallback_file_sibling"
      },
      "GET /revenue-strategy/client/summer/diff-price/special": {
        "status": "kept",
        "via": "handler",
        "symbol": "CheckSummerDiffPriceSpecial"
      },
      "POST /example/unknown": {
        "status": "unknown",
        "via": "fallback_exhausted",
        "fallback_attempted": true,
        "fallback_steps": ["path_fragment", "title_symbol", "file_sibling", "openspec"],
        "warning": "无 Handler 映射且兜底未命中"
      }
    }
  },
  "warnings": []
}
```

要求：

- `scan.complete` 必须为 `true` 后才可定稿 catalog
- **每条** catalog 将保留的接口（以及已 `code_offline` 的）都要在 `scan.by_path` 有条目；key 建议 `METHOD path`（path 与 catalog 一致，可带或不带服务前缀，但同一 run 内统一）
- `via` 取值：`handler` | `fallback_path_search` | `fallback_title_symbol` | `fallback_file_sibling` | `openspec` | `fallback_exhausted`

`interface-catalog.md` 增加一节：**已过滤（代码已标下线，不维护）**。  
计划可列「已过滤不维护」表，但不得分配 `F*-C*` 可执行编号。GitLab 上传：**不得**为 `code_offline` 接口新增/合并用例。

## 与 `not_in_proto` 的关系

| reason | 含义 |
|--------|------|
| `not_in_proto` | YApi 有、当前服务 proto/HTTP 映射无该 path |
| `code_offline` | proto/YApi 可能仍有，但实现已标下线 |

二者都不维护 case；过滤顺序建议：先 proto 交集，再对 kept 做本页下线扫描（含兜底）。

## 正例

- `GET /revenue-strategy/client/bigvip/strategy/list` → Handler `UserBigVipStrategyListByFrom` 含 `// 2025.4.1日下线` → `code_offline`（`via=handler`）
- `GET|POST .../blocksgood/config|info` → openspec + `http.go` 无注册 → `code_offline`
- `.../xugou/v1/**` 等 → openspec + 取消注册 → `code_offline`（保留 shadow/goodreduction 续购 v3）
- `GET /revenue-strategy/client/v4/batch/user/strategy_id` → 主路径证据缺口 → 同文件 `strategyv4.go` 兄弟扫描命中 `ClientBatchGetUserUseStrategyIdV4` + `// 2025.4.1日下线` → `code_offline`（`via=fallback_file_sibling`）

## 反例（禁止）

- Wiki 写「证据缺口」→ 直接 `unknown` 保留、**不做** path/同文件兜底
- 只扫 openspec，不扫仍注册但注释下线的空壳 Handler
- 同文件已有多条 `handler_comment` 下线，却不扫该文件其余方法
- 把 mux 404 单独当成 `code_offline`（无实现层证据时）
