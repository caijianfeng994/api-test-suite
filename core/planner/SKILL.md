---
name: api-test-suite-planner
description: Use when api-test-suite has normalized interfaces, optional contract analysis, and fixtures and must create an API test plan with clause traceability.
---

# 测试计划内核

## 输入

- `run-context.json`
- `interface-catalog.json`（仅含可维护接口；`filtered` 中 `not_in_proto` / `code_offline` 不得进入计划）
- `yapi-proto-filter.json`（若存在则必读：确认计划不含已过滤 path）
- `fixtures.json`（`run_mode=contract` 且 `ops=not_applicable` 时可缺失；按无 fixture 处理）
- `contract-clauses.json` / `contract-analysis.md`（若 `stages.contract=completed` 则**必读**）
- 场景插件提供的比较规则或差异白名单
- 覆盖检查：`reference/coverage-checklist.md`
- APP 调用与鉴权三态（必读）：`reference/app-usage.md` — 仅完整 `not_called_by_app` 可省略 auth
- 回归语义（必读）：`reference/regression-case-semantics.md` — 按 YApi/proto **必填**出 error/validation，禁止机械套「资源不存在 → 4xx」

## 输出

写入 `<run_dir>/test-plan.md`。交付物是**完整接口用例计划**，不是连通性冒烟清单。

### 禁止默认冒烟

- **禁止**以「每接口 1 条 smoke/冒烟」作为计划策略。
- **禁止**计划标题或策略写「冒烟覆盖即完成」。
- 仅当用户**显式**要求「只做连通性探测」时，才可规划 `category=smoke`，且必须在计划首页用醒目段落标明：**本计划含冒烟探测用例，非正式完整功能覆盖**。
- 默认 `full` / `generate` / `contract`：计划中**不得**出现以冒烟为主的用例条目。

### 每个接口至少覆盖（强制，须符合回归语义）

先读 YApi `required` 与 proto 入参，再选题；细则见 `reference/regression-case-semantics.md`。

1. **正常功能**（`category=functional`）— 有效参数成功路径；入参来自 fixture/示例/枚举。请求 schema 声明的全部 Path/Query/Body 字段默认必填，不参考 required/optional 标记。**必须**满足闭环 6 条（见 `reference/regression-case-semantics.md`）：可追溯 fixture、HTTP+业务码+schema 双轨断言、写后读回、状态变更前后校验（若适用）、`test_marker`+cleanup、`independent` 无顺序依赖。禁止只断言创建响应 200。**编辑/更新接口**计划须写明：先 GET 详情再 PUT/PATCH（`reference/edit-get-then-put.md`），不得把「只改 name/amount」当成功路径。
2. **校验**（`category=validation`）— 至少 1 条。接口存在请求字段时，按默认必填策略设计缺参；有类型约束时测类型错误。只有完全无请求字段时才禁止「空 Body/缺参」。未声明 HTTP 4xx 时，校验失败可期望 200+业务码
3. **认证与权限**（`category=auth`）— 按 `app_usage.status` 三态：
   - `called_by_app` / `unknown` / 缺 `app_usage` → **必须**至少 1 条 auth
   - `not_called_by_app`（且 `app-usage-evidence.json` 双侧证据完整）→ **省略** auth，计划写 `UNCOVERED [auth] 已确认非 APP 调用接口；证据见 app-usage-evidence.json#<endpoint_id>`
   - 契约强制鉴权：无 token 期望 401/403。**未声明强制鉴权**：只做探测（200 不算失败），名称写明依据，禁止当鉴权门禁
   - 零日志、单仓无匹配不得当作 `not_called_by_app`（见 `reference/app-usage.md`）
4. **错误契约**（`category=error`）— **仅当能从契约推导**时生成：
   - 资源 ID（忽略 required 标记）→ 不存在/非法标识（期望按文档：4xx 或 200+业务失败）
   - 列表过滤 ID → 未知 ID 按契约或实测语义断言；过滤不命中时为 **200 + 空列表**
   - 无资源标识的配置/枚举/统计接口（如无参 `GET /api/categories`）→ **禁止**套「资源不存在 → 4xx」；计划标 `UNCOVERED` 并写原因
5. **写操作副作用与清理**（`category=side_effect`）— 仅对**会改数据**的写操作；GET 与「获取列表/查询」类 POST **禁止** side_effect。无安全 fixture 时 `executable=false` + 原因。若 ops 已写出 `db-config.json`，计划须为写操作指定只读 `verify_db`（表名来自 `db-tables.json` / `db_readonly.py schema`，禁止臆造表名）
6. **列表接口**（`category=list`）— 分页/过滤 + **请求里真正带上** `limit`/`pageSize`（含 0 与过大边界）；名实必须一致

存在契约产物时，额外保证：

7. 每个 `CONFIRMED`/`INFERRED` 条款至少映射到计划用例，或显式 `UNCOVERED` + 原因
8. `MISSING`/`CONFLICTING` 且 `blocking=true` 的条款所影响的成功路径标记不可执行，并引用 blocker
9. 计划中写明契约来源与条款 ID

每个用例条目必须包含：

- 稳定编号（如 `F1-C01`）
- 接口 ID、method、path
- `category`（不得默认填 `smoke`）
- 前置 fixture 引用（无则写 `none` 并标不可执行条件）
- 实际请求参数
- 预期状态码与业务断言
- 契约条款 ID 列表（`clause_refs`，可空但需说明）
- `source_label`：`YApi+proto` 或 `proto有`
- 是否可执行；不可执行原因
- refactor/migration 的比较规则与忽略字段

计划摘要必须统计：接口数、用例总数、按 `category` 计数、可执行/不可执行数；**不得**用「冒烟通过率」冒充完整覆盖。

推荐用脚本辅助展开（仍须人工核对契约缺口）：

```bash
python .claude/skills/api-test-suite/scripts/generate_full_cases.py \
  --catalog "<run_dir>/interface-catalog.json" \
  --yapi-raw "<run_dir>/yapi-raw.json" \
  --fixtures "<run_dir>/fixtures.json" \
  --run-dir "<run_dir>" \
  --write-plan
```

## 场景扩展

- `functional`：单目标行为断言；catalog 含 GitNexus 形态字段（由 CodeWiki 写入）时必须据此设计边界/错误/必填用例，并在 `clause_refs` 引用对应 `INFERRED` 条款。优先读 YApi `req_body_other` / `req_query` 补全字段。
- `functional` + `run_mode=contract`：只做可评审计划，不假设环境与鉴权已就绪；缺环境的用例 `executable=false`。
- `refactor baseline`：记录当前契约与可重放请求（完整类别，非冒烟）。
- `refactor replay`：禁止重新设计用例，使用 baseline 计划。
- `migration`：同一逻辑用例绑定 source/target 两个 URL，并定义兼容性规则；两侧 catalog 字段（含 CodeWiki 写入的 GitNexus 形态字段）都要进入成对请求设计。

## 门禁

- 不得使用无来源测试数据。
- 不得为 `yapi-proto-filter.json` / catalog `filtered` 中 `reason=not_in_proto` 或 `reason=code_offline` 的接口编写用例；计划中可列「已过滤不维护」表，但不得分配可执行用例编号。
- `presence=proto_only` 且未命中 `code_offline` 的接口**必须**编写并维护用例，计划与用例中标明来源为 **proto有**（`source_label`）。
- `fixtures.pending_requirements` 非空的成功路径标记不可执行。
- 不得把 `INFERRED` 条款写成已确认产品预期而不标注。
- 不得覆盖契约阶段已标记的阻塞而不记录。
- 若计划中每个接口仅 1 条用例、或 `category` 全为 `smoke`，视为 planner **失败**，必须重写后再进入 generator。
- 若对无资源 ID 的接口规划「资源不存在 → 4xx」，视为 planner **失败**（假失败模板）。
- 测试计划非空且所有用例编号唯一后，更新 planner 阶段和 artifacts；并预留/更新 `traceability-matrix.md` 中的用例列。
