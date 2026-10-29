---
name: api-test-suite-generator
description: Use when api-test-suite must convert an approved plan and fixtures into structured cases, optional Playwright scripts, and contract traceability updates.
---

# 用例与脚本生成内核

## 输入门禁

必须存在并非空：

- `interface-catalog.json`
- `test-plan.md`

只允许给 catalog `interfaces` 中的条目生成用例。若存在 `yapi-proto-filter.json`，禁止为 `filtered` 里 `not_in_proto` 或 `code_offline` 的路径生成 `test-cases.json` 条目或 Playwright 脚本。`presence=proto_only` 且未下线的接口必须生成用例，且每条 case 写入 `"source_label": "proto有"`。

### 完整用例（默认）— 禁止冒烟冒充覆盖

- 默认产出用户可直接使用的**回归向接口用例**：按 planner 覆盖展开，且必须遵守 `reference/regression-case-semantics.md`，**不要**生成 `category=smoke`。
- **禁止**机械套「每接口一条资源不存在 → HTTP 4xx」。YApi `required=0` 或无参配置/列表过滤，未知 ID 应变为 **200 + 空列表**，或根本不生成该 error。
- **禁止**「每接口仅 1 条冒烟」写入 `test-cases.json` / `test-cases.md`。
- 允许的 `category`：`functional` | `validation` | `auth` | `error` | `side_effect` | `list`（列表接口）| 场景扩展类别。
- 仅当用户显式要求连通性探测时才可写 `smoke`；此类用例名称必须以「【冒烟探测】」开头，并在报告中单独章节标明（见 reporter）。
- 推荐命令（与 plan 一致时可一次写出 plan+cases）：

```bash
python .claude/skills/api-test-suite/scripts/generate_full_cases.py \
  --catalog "<run_dir>/interface-catalog.json" \
  --yapi-raw "<run_dir>/yapi-raw.json" \
  --fixtures "<run_dir>/fixtures.json" \
  --run-dir "<run_dir>" \
  --write-plan --write-cases
python .claude/skills/api-test-suite/scripts/render_test_cases_table.py \
  "<run_dir>/test-cases.json" --output "<run_dir>/test-cases.md"
```

`fixtures.json`：

- 执行类生成（将产出可执行脚本）时必须存在
- `ops` 未完成不得生成声称可执行的成功路径。缺绑定时必须先走 `reference/fixture-fallback.md`（造数工具 → 查库 → `web_cli/`），三步失败才允许 `executable=false`。
- `run_mode=contract` 时可不存在

若 `stages.contract=completed`，必须读取 `contract-clauses.json` 并在生成后回填 `traceability-matrix.md`。

## 判别/条件字段（P0）

生成 body 前读取 `reference/fixture-discriminator-fields.md`。  
`userScope=user_ids` 时不得输出非空 `userLayer`；`chasePrevOrder` 非 true 时不得输出非空 `prevOrderLayer`；禁止 `userLayer`/`prevOrderLayer` 等于 `all`。  
用创建 API 给下游写接口供数时，创建参数须满足下游约束（同文档「下游接口约束」）；`side_effect` 与 `functional` 各用独立种子（`reference/fixture-fallback.md`）。`PUT`/`PATCH`/`DELETE` 的资源主键先写同 path 创建接口造出的值，再从同 path 获取/列表响应取主键，`source` 用 `api:`；创建和获取都不满足才允许 `db:<service>.<table>.<column>`。

省略必填 path 的 validation：按 `reference/regression-case-semantics.md` 使用 `omit_required_path`（path 去掉 `{param}`，`path_params={}`），禁止空串。

## 真实 fixture 门禁（P0，强制）

成功路径（`functional` / `list` / `side_effect`）的入参**只能**来自真实可追溯的取值，按以下顺序解析
（`generate_full_cases.py` 的 `resolve_value`）：

1. `fixtures.json` 的 `endpoint_bindings.<eid>.values.<字段名>` → `source=endpoint_bindings.<字段>`
2. `fixtures.json` 的 `fixtures.shared.<字段名>` → `source=shared.<字段>`
3. 契约声明的 `example` / `default` / `enum[0]` → `source=yapi.example|yapi.default|yapi.enum`

规则：

- **必填字段解析不到时不写任何假值**：省略该键，用例标 `executable=false`、`skip_reason` 写明缺哪些字段，
  并在 `regression_blockers` 记 `required_binding_missing:<body|query|path>.<字段>`。
- **禁止**用 `test`、`placeholder`、`api-test-placeholder`、全零 UUID、`placeholder-id` 兜底；候选值本身命中占位判定
  （复用 `regression_promotion.contains_placeholder`）时视为未解析。
- **禁止**按字段名模糊套用（如把 `shared.goodId` 塞给 `appleGoodId`）：只做字段名精确匹配。
- **分页参数没有兜底默认值**：`limit` / `pageSize` / `page` / `pageNo` 只接受 `endpoint_bindings.<字段>`、
  `shared.limit` / `shared.page`（含同名字段）、契约 `example` / `default` / `enum` 这些有来源的取值；
  一个来源都没有时按 `required_binding_missing:query.<字段>` 处理，用例 `executable=false`，
  **不得**写死 `2` / `1`。`ensure_limit` 只是兼容旧调用点，不注入任何无来源取值。
- body 里业务含义的 `size` 等字段不按分页处理，仍走普通字段的真实绑定要求。
- 分页边界（下界/上界）只在契约 `minimum` / `maximum` 或 fixture（`<字段>Min` / `<字段>Max`）给出时才生成对应用例，
  并把该值和来源写进字段级 `fixture_trace`；没有边界来源时只出 1 条正常分页用例，
  并在 `pagination_coverage` 记 `mode=contract_probe` + `uncovered`，**不得**声称测了 0 / 99999。
- Path/Query/Body 的所有声明字段默认必填；不参考 YApi/proto/代码的 required/optional 标记。解析顺序在夹具和契约示例之后，还包括 `reference/code-request-values.md` 写入的 `code:` 值，以及 `reference/binding-log-supplement.md` 写入的 `log:` 值。`usage=code_unused` 的字段从成功请求省略，不记 `required_binding_missing`。其余字段解析不到即标记不可执行并写 `required_binding_missing`，不得塞假值。写缺绑定之前必须已有近 30 天线上日志证据。
- `test_marker` 只写进契约已声明的可读字段（`name`/`title`/`note` 等）；**不得**往 body 里 `setdefault` 契约外的键。
- **Header `uid`（业务身份）**：go-revenue 等从 `RequestHeader().Get("uid")` 取登录用户（如 redeemCode）。
  除刻意去身份的强制鉴权负向（`drop_auth_header`）外，所有类别用例都必须从
  `shared.uid` / `shared.userId` / `shared.user_id` 写入请求头 `uid`，**禁止**编造。
  这与 Authorization / token 鉴权头无关——空 body/空 query 的接口仍可能依赖 Header `uid`；
  负向若只省略 query/body 却不带 uid，服务会先报「用户ID不能为空」盖住契约缺参行为。
- **Header `Authorization`（网关登录态）**：`base_url` 为 `7to12-*.yangcong345.com/backend` 等公网网关时，
  成功路径须带用户 JWT。取值来自 `reference/c-end-auth.md`（`tokenUtil.getAppToken` /
  `fetch_c_end_auth.py`），用例中优先 `${API_TEST_AUTHORIZATION}` 占位，**禁止**把完整 JWT
  写进将上传 GitLab 的 case 正文。同请求还应带 `uid`、`client-version` 与环境匹配的 `Referer`
  （缺 Referer 时网关可能 403）。集群 `.7to12` 直连且仅需 `uid` 时不要强加该头。

每条用例生成时一律 `regression_ready=false`，`regression_blockers` 至少含 `execution_not_passed`；
是否可晋升由 `regression_promotion` 在执行通过 + 证据齐全后判定，生成阶段不得声称已晋升。

## 写接口授权与副作用用例

`generate_full_cases.py` 读取 `<run_dir>/run-context.json`，经
`scripts/write_authorization.py` 的 `resolve_write_authorization(context, os.environ)`
得到 `allowed`，再传入 `expand_interface(..., write_authorized=...)`。字段语义见
`reference/run-context.md`；**禁止**在本 Skill 或生成脚本中复制第二套授权算法。

强制规则：

- **已授权 ≠ 可缺 fixture**：所有声明字段默认必填；缺真实绑定写
  `required_binding_missing:<location>.<field>`，并令 case `executable=false`。不得因已授权而填
  placeholder / 空 UUID / `{}` 假 body。
- **destructive `side_effect`**：必须用 `build_body_from_schema(..., mode="valid")` 组装完整请求
  body，**不得**因 destructive 写空对象 `{}`。未授权时
  `executable=false`、`skip_reason` 为「破坏性写操作：测试环境未记录显式写授权」；已授权且绑定齐全时
  可执行。
- mutating 的 `side_effect` 必须写入元数据：
  `write_authorization: {"authorized": <bool>, "destructive": <bool>}`，其中 `destructive` 为显式布尔。
- **清理只用 `cleanup.enabled` + `cleanup.reason`**：mutating 用例默认生成
  `cleanup.enabled=true` 的**清理候选**（`method=DELETE` + 推断的详情 path + `id_from`），这只是候选，
  不代表清理接口已核实存在。执行前由 ops / executor 核验目标 DELETE / 清理方案确实存在且安全；
  确认无安全清理接口时，必须把产物改成 `cleanup.enabled=false` 且 `cleanup.reason` 写具体原因
  （如人工处理、无可用清理接口），**不得**伪称已清理，也**不得**发送臆造的清理请求；
  只读用例固定 `cleanup.enabled=false` + `cleanup.reason=read_only`。禁止用 SQL 写库做清理。
- 授权上下文变化后须重新生成用例与 execution 工程，不得沿用旧 `writeAllowed` 固化结果。

### 负向用例必须自报家门

`validation` / `auth` / `error` 允许按契约构造非法值，但必须写：

- `fixture_policy: "negative_case"`
- `negative_construction: {"strategy": "...", "basis": "..."}`，`basis` 说明构造依据（哪条契约声明、改了什么）

可用的负向策略：`omit_required_body`、`omit_required_query`、`omit_required_path`、`omit_optional_params`、`invalid_field_type`、
`empty_body_probe`、`pagination_contract_probe`、`unknown_required_resource_id`、`unknown_optional_filter_id`、
`drop_auth_header`；没给具体策略时兜底为 `contract_negative`。`omit_required_path`：path 去掉 `{param}` 段且 `path_params={}`，禁止空串（见 `regression-case-semantics.md`）。

其中 `pagination_contract_probe` 是分页场景的唯一策略：只用有来源的分页值探测契约行为，
断言收敛到「非 5xx」。**没有** `invalid_pagination_value` 这种策略——构造非法分页值需要契约声明的边界，
契约没声明就不许自造。
负向取值用确定性的非法值（不存在 ID `00000000-0000-0000-0000-ffffffffdead`、`regression-negative-unknown`、越界数字），
**不是**占位符——占位符在任何用例里都不允许。

### 缺参状态代码校准

契约未声明缺参 HTTP 状态时，禁止写 `2xx_or_4xx` 或禁止 5xx。负向用例只标记
`pending_code_return`，等代码返回点闭合后再写精确状态。实测出现 5xx、用户明确要求代码判断，
或正在修复旧模糊断言时，必须执行 `reference/missing-required-code-status.md`：

- 通过 CodeWiki/GitNexus 追踪 transport/codec/validator → service → biz/usecase → error/recovery；
- 一次仅移除一个目标字段；其余字段必须类型合法、满足 validator 且有来源，禁止让其他非法字段抢先返回；
- 将精确状态写入 `expected.status`，并写 `expected.code_evidence`；
- 生成脱敏的 `<run_dir>/code-validation-evidence.json`；
- 代码推断只标 `INFERRED`；运行结果冲突时保留双方证据，不得静默任选；
- 当前实现因 nil/panic 返回 500 时，`classification=PRODUCT_DEFECT`。代码结论、精确状态、响应格式与实测
  一致时执行结果记为通过，并进入回归集。

### 本门禁下禁止产出

- **缺真实绑定却仍标 `executable=true` 的成功路径**（必须 `executable=false` + `required_binding_missing:*`）
- **用 `test` / `placeholder` / 零 UUID 冒充业务数据**的任何请求字段
- 未标 `fixture_policy=negative_case` 的负向构造
- 往请求 body 里塞契约外的 `note` 等字段来挂 `test_marker`

## 固定输出

1. `<run_dir>/test-cases.json`：机器可读，执行与比对使用
2. `<run_dir>/test-cases.md`：表格可读视图，给人审阅
3. `<run_dir>/regression-fixtures.json`：可上传的 fixture 清单（敏感值只留 `${ENV}` 引用，占位值进 `rejected`），见 ops
4. 执行类模式另需：`<run_dir>/tests/*.spec.ts`、`<run_dir>/playwright.config.ts`
5. 契约启用时：更新 `<run_dir>/traceability-matrix.md`
6. 发生缺参代码校准时：`<run_dir>/code-validation-evidence.json`

`run_mode=contract`：**禁止**生成 Playwright 脚本；所有用例默认 `executable=false`，除非计划明确且用户已提供可落地的请求样例（仍不生成脚本）。

测试用例结构：

```json
{
  "id": "F1-C01",
  "endpoint_id": "F1",
  "name": "有效参数查询",
  "category": "functional",
  "method": "GET",
  "path": "/api/items",
  "request": {},
  "expected": {},
  "fixture_refs": [],
  "clause_refs": ["F1-CL-001"],
  "contract_source": "yapi:2619",
  "source_label": "YApi+proto",
  "executable": true,
  "skip_reason": null,
  "regression_ready": false,
  "regression_blockers": ["execution_not_passed"]
}
```

`test-cases.json` 建议顶层为：

```json
{
  "schema_version": "1.0",
  "generated_at": "2026-08-12T11:17:00+08:00",
  "case_count": 1,
  "category_counts": {
    "functional": 0,
    "validation": 0,
    "auth": 0,
    "error": 0,
    "side_effect": 0,
    "list": 0,
    "smoke": 0
  },
  "cases": [ /* 用例对象数组 */ ]
}
```

门禁：生成后每个 `endpoint_id` 至少含 `functional`+`validation`；`auth` 按 `app_usage.status`：仅完整 `not_called_by_app` 可省略（须有 `app-usage-evidence.json` 双侧证据，计划含 `UNCOVERED [auth]`），`called_by_app`/`unknown`/缺字段必须含 auth（`scripts/app_usage.should_generate_auth`）。所有请求字段默认必填。`error` **仅在契约可推导时**必有（资源 ID 的不存在分支，或过滤不命中的空列表分支）。无资源标识的配置/枚举接口允许没有「不存在」类 error，须在计划写 `UNCOVERED`。会改数据的写接口另含 `side_effect`（列表查询 POST/GET 不要 side_effect）。列表另含 `list`，且 query 必须带 `limit`/`pageSize`。`category_counts.smoke` 默认应为 0。

### APP 调用与 auth（强制）

读取 catalog 的 `app_usage`（细则 `reference/app-usage.md`）：

- `not_called_by_app` → **不**生成 `category=auth`；计划写 `UNCOVERED [auth] 已确认非 APP 调用接口；证据见 app-usage-evidence.json#<endpoint_id>`
- `called_by_app` / `unknown` / 缺字段 → **必须**生成 auth
- 推荐用 `generate_full_cases.py`（已接 `should_generate_auth`）；禁止凭「零日志」或「单仓无匹配」自行省略 auth

### functional 闭环（P0，强制）

每条可执行 `functional` 必须符合 `reference/regression-case-semantics.md`「functional 闭环门禁」：

1. `fixture_refs` + `fixture_trace`（或 `fixture_policy=none_ok`）
2. `expected.http` + `expected.business` + `expected.schema`（双轨 + schema）。**响应 schema 优先代码出参**（`reference/code-response-schema.md`）：catalog `responses[].schema.fields` / `response_root_type` 覆盖 YApi；根 `[]Xxx` → `type=array`；字段名与 `repeated` 类型以代码为准；冲突写 `source=code_calibrated`。禁止无 YApi 时默认 `object` 否定代码数组。
3. `verify_read`（写后读；只读可为 `mode=self`）；编辑/更新须另有 `pre_read`（先 GET 再编辑，见 `reference/edit-get-then-put.md`）
4. `state_check` 或 `state_policy=not_applicable`
4b. 写操作且存在 `db-config.json` 时写入 `verify_db`（表名必须来自只读 `tables`/`schema` 产物）
5. `test_marker` + `cleanup`
6. `independent=true` 且 `depends_on=[]`

`scripts/validate_run.py` 会校验上述字段；缺失视为生成失败。

禁止产出：

- 无资源 ID 却期望 4xx 的「资源不存在」
- 无任何请求字段却生成「空 Body/缺参」
- 列表边界用例名称含 limit 但请求未带该参数
- 根据 Schema 的 `required` 数组省略其他 properties；本套件要求所有声明字段默认必填
- functional 只有 HTTP status、无业务码/schema、无 verify_read 的「假成功路径」

## 表格视图（强制）

生成 `test-cases.json` 后，必须同步写出 `<run_dir>/test-cases.md`，不得只保留 JSON。

推荐命令：

```bash
python .claude/skills/api-test-suite/scripts/render_test_cases_table.py \
  "<run_dir>/test-cases.json" \
  --output "<run_dir>/test-cases.md"
```

Markdown 最少包含：

1. 标题与摘要：用例总数、可执行数、不可执行数、`test_kind`、`run_mode`
2. 总览表（一行一个用例）：

| 编号 | 接口 | Method | Path | 类型 | 用例名称 | 预期状态码 | 可执行 | 跳过原因 | fixture | 条款 |
|-|-|-|-|-|-|-|-|-|-|-|

3. 按接口分组的明细表，额外列：

| 编号 | 用例名称 | 请求参数摘要 | 预期断言摘要 | 比较规则 | 契约来源 | 备注 |
|-|-|-|-|-|-|-|

规则：

- 表格列顺序固定，便于 diff 与人工扫描
- `request` / `expected` 单元格做单行摘要，过长截断并保留关键字段
- 敏感值脱敏（token、手机号、身份证等）
- `refactor replay` 复制 baseline 的 `test-cases.json` 时，必须同步复制或重新渲染 `test-cases.md`
- JSON 与 Markdown 用例编号、数量必须一致；不一致视为生成失败
- `presence=proto_only` 时，明细表「契约来源」或「备注」必须写 **proto有**，不得写成 YApi

## 代码规范

- 固定 Playwright + TypeScript，只使用 `APIRequestContext`。
- 脚本文件命名用 `api-cases.spec.ts`（或按类别拆分），**不要**默认命名为 `smoke.spec.ts`（易误导为仅冒烟）。
- Base URL 从 `run-context.base_urls` 或环境变量读取，不硬编码猜测域名。
- 敏感 Header 从环境变量读取。
- 请求、状态码、响应体和耗时写入测试附件或结构化日志。
- 一个 test 对应一个用例 ID，名称必须包含 ID 与 category。
- 列表正常/边界请求必须显式传 `limit` 或契约中的 `pageSize`。
- 断言按 `expected`：优先精确 status **和** 业务码；允许 `status_class`。functional 默认 `http.status=[200,400]` 且 `accept_structured_4xx=true`：结构化 4xx + JSON 业务包（含 `code`/`errcode`/`errmsg` 等）算通过，只把 5xx 当失败。列表可选过滤未知 ID 期望 200+空集合。写接口副作用不得用「非 5xx 即过」代替业务断言（除非计划标明探测且 `executable=false`）。未声明强制鉴权的 auth 探测不得因 200 失败。
- `expected.code_evidence.classification=PRODUCT_DEFECT` 且响应为 5xx 时：校验精确状态和响应格式；命中则执行通过并晋升，
  未命中才失败。
- Playwright `assertExpected`：若 `status===400` 且 body 为对象并含 `code`/`message`/`msg`/`reason`，直接 return 通过。
- 不依赖用例执行顺序；写操作准备与清理放入 fixture/helper。
- 编辑类脚本：同一 test 内 GET 详情 → 按请求 schema 映射 body → PUT/PATCH；不得把 YApi 示例稀疏 body 直接当成功路径。

## 完整用例执行工程（`--mode execution`）

执行完整 `test-cases.json`（含已授权写接口）时，用生成器产出独立 execution 工程，默认目录
`<run_dir>/execution-runtime/`：

```bash
python .claude/skills/api-test-suite/scripts/generate_playwright_project.py \
  --run-dir "<run_dir>" \
  --service-name "<service>" \
  --mode execution \
  --output-dir execution-runtime
```

约束（与实现一致）：

- 读取父目录 `test-cases.json` / `run-context.json`；证据写回父目录 `execution/requests/`。
- 生成期调用 `resolve_write_authorization`，将 `allowed/environment/source/reason` 固化为
  `writeAuthorization`；运行前校验 context SHA-256，不一致则 **fail closed**：抛
  `write authorization context changed; regenerate execution project` 并停止发送请求，
  必须更新 `run-context.json` 后重新生成 execution 工程才能恢复。
- `source=environment` 的 legacy 授权在发写请求前仍要求 `ALLOW_WRITE_TESTS=true`；撤销时同样先抛错
  停止请求，恢复同样走「更新 context → 重新生成工程」。
- `environment=test` 且 resolver 判定授权时：**禁止**按写 Method 统一 `test.skip`，**禁止**产出
  「写接口未获得本次显式副作用授权」；未授权写请求才 `test.skip(..., "write_operations_not_authorized")`。
- 仍按 `executable === false`、`required_binding_missing`、敏感环境变量与契约 blocker 决定是否执行。
- 证据含脱敏后的完整 URL、headers、query、path_params、body、响应 status/body、耗时；敏感字段值为
  `***`，不得明文泄露。

## 可独立运行的回归工程（晋升后）

`regression-cases.json` 产出后（即已有晋升用例），必须把晋升集打包成**可脱离本仓库运行**的 Playwright 工程
（默认 `--mode regression`，输出在 run 根目录）：

```bash
python .claude/skills/api-test-suite/scripts/generate_playwright_project.py \
  --run-dir "<run_dir>" \
  --service-name "<service>"
```

输出固定 5 个文件：`package.json`、`package-lock.json`、`.env.example`、`playwright.config.ts`、`tests/api-cases.spec.ts`。

硬约束：

- **无本机绝对路径**：依赖按包名解析（`from "@playwright/test"`），`testDir` 等路径相对工程目录；禁止再出现
  `import ... from "/Users/.../node_modules/@playwright/test"` 或把 run 目录写死成常量。
- **版本不手写**：`@playwright/test` 的精确版本与依赖闭包来自 `backend/workspace/api/package-lock.json`
  （当前锁定 `1.61.1`），生成 `package-lock.json` 供 `npm ci` 复现；也可用 `npm install --package-lock-only` 复核。
- **输入只有晋升集**：spec 读 `path.resolve(__dirname, "..")` 下的 `regression-cases.json`，
  **不得**回读 `test-cases.json` / `fixtures.json` / `run-context.json`；backlog 用例不进工程。
- **取值靠环境变量**：请求里的 `${VAR}` 在运行期从 `process.env` 展开，缺变量直接抛错，不发出带字面量的请求；
  `Base URL` 取 `API_BASE_URL`，可选 `API_HOST_HEADER`（影子环境 Host）。全部变量名写进 `.env.example`，**只列键名不写取值**。
- **断言按每条用例的 `expected`**：`functional`/`list`/`side_effect` 要 2xx（或结构化 400）+ 契约 `schema`；
  `success_when.code_in` / `ok_is` **仅当契约 const/enum 声明成功值时才写入与校验**，禁止默认 `ok=true` / `code∈{0,200}`；
  EmptyResp/无字段则只验 `type`，禁止猜 `code`。`validation`/`error` 在契约未声明错误 HTTP 码时
  不得写 `2xx_or_4xx` 或禁止 5xx，必须等代码返回点写入精确 status 与 `response_format`；
  `auth` 仅在契约 `auth.required` 时用 4xx。
  经 `reference/missing-required-code-status.md` 完成源码闭环与单变量重跑后，允许收敛为精确 200/400/500；
  其中 `PRODUCT_DEFECT` 500 在状态码和响应格式都命中后执行通过并晋升。
  期望不精确或 category 不支持时该用例直接判失败，**禁止**退化成「非 5xx 即通过」却又不写 schema。

输入门禁（任一不满足时 CLI 退出码 2、不落任何文件）：缺 `regression-cases.json`、JSON 损坏、`cases` 非列表、
用例缺 `id` 或 `id` 重复、顶层 `case_count` 缺失/类型错误/与实际条数不一致、任一用例未明确
`regression_ready=true`。独立工程只接收已晋升成功集，禁止把 backlog 混入。

`case_count=0` 是合法的“全部 backlog、暂无晋升”状态；仍须生成独立工程 5 文件，
且 `npm run test:list` 稳定收集 `0` 条，不得因空回归集跳过 GitLab 交付。

运行期断言必须执行最小 JSON Schema 校验：支持 `object`、`array`、`string`、`number`、`integer`、
`boolean`，递归校验 `properties` / `items`，并同时支持 JSON Schema `required` 与既有
`required_fields` 点号路径；类型不符或必填字段缺失必须失败，不得只检查 HTTP/业务码后放行。

生成后只做静态校验，**不要执行接口**：

```bash
cd "<run_dir>"
npm ci            # 私有镜像拉不到 playwright 时加 --registry=https://registry.npmjs.org
npm run test:list # 收集数必须等于 regression-cases.json 的 case_count
```

收集数与 `case_count` 不一致视为生成失败。执行发生在 executor，且由使用者自行导出 `.env.example` 中的变量。

## 场景生成

- `functional`：单 target 请求；请求字段以 catalog 为准（YApi∩proto 合并字段，或仅 proto 字段）；`proto_only` 用例必须带 `source_label=proto有`；不得给未过 proto 门禁或 `code_offline` 的接口生成用例。
- `functional` + `contract`：只写 JSON/MD 用例与追踪矩阵，不写脚本。
- `refactor baseline`：单 target 请求并产出可重放 request snapshot。
- `refactor replay`：复制 baseline 的 `test-cases.json`、`test-cases.md` 和测试语义，只替换目标 Base URL；禁止重新生成用例。
- `migration`：每个用例在同一个 test 中按相同输入调用 source 和 target，分别保存结果；表格中 Path 列写 `source_path -> target_path`。请求字段取两侧 catalog 合并后的可执行交集，CodeWiki 独有约束写入断言或 `INFERRED` 条款，不得丢弃。

## 追踪矩阵回填

生成用例后更新 `traceability-matrix.md`：

- 每个条款对应一个或多个用例 ID
- 无覆盖写 `UNCOVERED` 与原因
- 用例的 `clause_refs` 必须与矩阵一致

## 校验

生成后必须：

1. 确认 `test-cases.json` 与 `test-cases.md` 同时存在且非空
2. 确认 Markdown 总览表行数等于 `cases` 数组长度
3. 执行类模式：执行 Playwright `--list` 或 TypeScript/Playwright 收集检查
4. `contract` 模式：确认无声称已执行的脚本产物依赖，且矩阵已回填

任一失败必须修复后再进入 executor（执行类）或声明契约设计完成（`contract`）。
