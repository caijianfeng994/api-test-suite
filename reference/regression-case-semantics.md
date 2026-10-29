# 回归用例语义（禁止机械套模板）

目标：生成的接口 case 接近**可维护的回归测试**，而不是「每接口固定六类招式」。

## 请求参数默认必填（P0，最高优先级）

- 只要请求 schema 声明了 Path、Query 或 Body 字段，数据准备、functional、auth 和执行门禁一律按必填处理。
- 不读取、不采信 YApi/proto/代码中的 `required=false`、可选标记或缺省行为；schema 的 `required` 数组不参与取舍。
- 每个声明字段都必须有非空、非占位、可追溯的有效值。夹具三步没有真值时，先按 `reference/code-request-values.md` 用代码字面量、透传文案或 `code_unused` 收口；三种情况都不成立时，再按 `reference/binding-log-supplement.md` 用近 30 天线上日志里的请求值回填。日志也补不上，才
  `executable=false` + `required_binding_missing:<body|query|path>.<字段>`。
- 这是测试套件的保守执行策略；原始 YApi/proto/代码仍按事实保存，不得伪造为“契约已声明必填”。

教训来源：`good` 全量 run 中 error 失败大量是假失败——对无必填资源 ID 的列表/配置接口套「资源不存在 → HTTP 4xx」，而文档标明参数全可选或不存在，实测为 `200` + 全量/空列表。

## 必读字段

生成前必须读取 catalog / YApi raw / proto：

- YApi/proto 的全部请求字段名、类型、格式、示例、默认值与枚举；
- `required` 信息只用于契约差异报告，不用于决定是否准备或发送字段；
- 接口用途（列表、配置枚举、详情、写操作）

## error（错误契约）

**禁止**对每个接口机械生成「资源不存在或非法标识 → `status_class=4xx`」。

| 契约事实 | 应生成的回归用例 | 期望 |
|-|-|-|
| 存在资源标识（path/query/body `id` 类；忽略 required 标记） | `error`：不存在/非法 ID | 契约有 4xx 则 4xx；仅声明业务码则 **HTTP 200 + 业务失败码/空对象**，不得只套 4xx |
| 列表过滤 ID（无论原契约是否标可选） | `error` 或 `list`：未知过滤值 | 按契约或实测语义断言；若语义为过滤不命中，则 **`200` + 空列表**，禁止机械期望 4xx |
| **无任何资源标识**（如 `GET /api/categories`、`GET /api/good/kind-count`、无参配置/枚举） | **不生成**「资源不存在」error | 计划写 `UNCOVERED`：无必填资源标识，禁止套 4xx |
| 契约声明冲突/状态不允许 | `error`：冲突/非法状态 | 按文档状态码或业务码 |

无推导依据时宁可少一条 error，也不许造假失败。

## validation（校验）

- 接口没有任何请求字段时，禁止「缺必填/空 Body」用例。
- 存在请求字段时，全部字段按默认必填策略设计缺参校验；有类型约束时设计类型错误。
- 许多服务校验失败仍 **HTTP 200 + 业务 code**：期望必须双轨（HTTP + 业务码）。未声明 4xx 时不得强求 4xx。
- Schema 是否写 `required` 不影响本套件：所有 properties 均按必填准备和校验。

## auth（鉴权）

- 契约/proto **声明必须鉴权**：无 token 期望 `401`/`403`（或文档业务码）。
- **未声明强制鉴权**：`auth_probe`（category 仍可为 `auth`），期望「非 5xx」，**不得**因 200 判失败；名称注明「契约未声明强制鉴权」。
- 禁止用「非 5xx 即过」冒充鉴权门禁。

## side_effect

- 仅**会改数据的写操作**（创建/更新/删除/上下架等）。
- **禁止**：GET；名称/path 为获取列表/查询的 POST（如 `.../list`）。
- 破坏性写：无隔离 fixture 则 `executable=false`，不得省略条目。
- **独立种子（P0）**：与同接口 `functional`（及其它会消费同一资源的成功路径）**不得共用**会脏的 id；按 `reference/fixture-fallback.md`「创建补数给下游写接口」各造一份。复用已作废/已核销/已绑定数据导致的业务 400，优先补新种子，不要先改断言。

## list

- path/名称含 list/page/search，或存在 `limit`/`pageSize`/`pageNo`。
- 正常/边界请求**必须真正带上** `limit` 或 `pageSize`（名写 limit=0 而 query 为空视为生成失败）。
- 分页值无来源时不写死 `1` / `2`，按 `required_binding_missing:query.<字段>` 置 `executable=false`。
- 边界用例只在契约 `minimum` / `maximum` 或 fixture `<字段>Min` / `<字段>Max` 提供来源时生成，
  名称写明实际取值（如 `分页上界边界-pageSize=200`）。无来源时只出 1 条正常分页用例，
  并在 `pagination_coverage` 标 `mode=contract_probe` + `uncovered: ["min","max"]`：
  **不得**用「边界值重复性/稳定性」之类名字暗示测过 0 / 99999。
- 过滤 ID 的未知值仍按接口实际错误/空列表语义断言；“默认必填”不等于“未知值必须返回 4xx”。

## functional

- 成功路径入参必须是契约允许的有效值（fixtures / 示例 / 枚举），禁止用无业务含义的占位字段冒充创建成功。解析规则与未解析时的处置见下文「1. 真实、可追溯的 fixture」。
- 无数据时允许空列表 200，不要改成 4xx。
- **编辑/更新**（`PUT`/`PATCH`、名称含编辑）：成功路径必须 `pre_read` 先 GET 详情，再用详情按 **请求 schema** 组 body 后编辑。禁止只发 YApi 示例稀疏字段并假设服务会读库补全（见 `reference/edit-get-then-put.md`）。创建 POST 不强制本条。
- 请求 schema 中的全部字段均按必填处理；ID/UUID 等业务定位字段由 ops 按
  `core/ops/SKILL.md` 获取 GitLab/Nacos 数据库配置、只读查询真实记录并绑定；查不到则不可执行。
- auth 用例只改变鉴权 Header，其他业务参数必须与成功路径一样真实有效，避免空 UUID/假 ID 先触发数据库 5xx，
  使鉴权结论失真。

## functional 闭环门禁（P0，强制）

每条 `category=functional` 且 `executable=true` 的用例必须满足下列 6 条；不满足则 **生成失败 / validate_run 失败**，不得交付为回归用例。双轨断言（HTTP + 业务 code/msg）是第 2 条的一部分，**不能代替**其余 5 条。

### 1. 真实、可追溯的 fixture

- 必须有非空 `fixture_refs`（或显式 `fixture_refs: []` 且 `fixture_policy=none_ok`，仅限无入参只读接口）。
- 每个引用必须能在 `fixtures.json` 解析到值；禁止 `"test"` / `"placeholder"` / 空字符串冒充业务数据。
- 写入字段级 `fixture_trace[]`：`{ref, source, required, value}`，且 `value` 必须与请求实际值一致；`source` 形如 `endpoint_bindings.skuGoodId`、`shared.skuGoodId`、`yapi.example` 或 `generated:test_marker`。
- `fixture_refs` / `fixture_trace` 只能引用请求实际使用的字段；禁止用 `fixtures.json#shared` 粗粒度兜底。无参只读用例明确写 `fixture_refs=[]`、`fixture_trace=[]`、`fixture_policy=none_ok`。

#### 取值解析顺序（成功路径强制）

`generate_full_cases.py` 的 `resolve_value(name, schema, binding, shared)` 按下列顺序取值，全部落空即判「未解析」：

| 顺序 | 来源 | `source` |
|-|-|-|
| 1 | `fixtures.json` → `endpoint_bindings.<eid>.values.<字段>` | `endpoint_bindings.<字段>` |
| 2 | `fixtures.json` → `fixtures.shared.<字段>` | `shared.<字段>` |
| 3 | 契约 `example` / `default` / `enum[0]` | `yapi.example` / `yapi.default` / `yapi.enum` |

- 字段名**精确匹配**，禁止模糊套用（不得把 `shared.goodId` 塞给 `appleGoodId`）。
- 候选值本身命中占位判定（`test` / `placeholder` / 零 UUID / 未替换的 `{xxx}`）时视为未解析。
- ID / UUID / 资源标识字段的契约 `default=0`、`default=false` 或空值不得作为成功数据；非资源枚举、布尔字段的合法 default 可保留，并记录 `yapi.default`。
- query 中 `limit` / `pageSize` / `page` / `pageNo` / `size` 等分页字段同样禁止硬编码 1/2：只允许 endpoint binding、`shared.limit/page`（或同名 shared）及 YApi example/default/enum；缺少来源即 unresolved。body 中普通业务字段 `size` 不按分页处理。
- fixture 使用纯值结构保持兼容；若值包装或分离元数据明确声明 `usage=negative_only`，成功路径必须拒绝，不能只靠键名推断用途。

#### 未解析时的行为（P0）

- 任一声明字段未解析：省略该键（**不写假值**），用例 `executable=false`，`skip_reason` 列出缺失字段，
  `regression_blockers` 记 `required_binding_missing:<body|query|path>.<字段>`。
- 缺真实绑定的成功路径既**不可执行**也**不可晋升**；要恢复可执行，只能由 ops 补真实数据，不能靠改生成器兜底。
- auth 用例复用 functional 的业务 query/body/path 与字段级 trace，只移除或替换鉴权信息；functional 缺真实绑定时 auth 同样不可执行。

#### 生成期晋升状态

每条用例生成时一律：

```json
{"regression_ready": false, "regression_blockers": ["execution_not_passed"]}
```

不可执行的用例另加 `case_not_executable`。执行后由 `scripts/regression_promotion.py` 判定：
普通 **`execution_status=passed` → `regression_ready=true`**，并保留完整请求取值（敏感键改为 `${API_TEST_*}`）；
缺证据 / fixture 追溯 / 期望精度等问题只进 `promotion_notes`，不挡晋升。
代码核对后状态码和响应格式都一致的 5xx 同样 `regression_ready=true`。
生成阶段不得声称已晋升；未通过（failed / skipped / not_run）进入 backlog。

### 2. 成功调用 + 双轨断言 + 响应 schema

`expected` 必须同时包含：

```json
{
  "http": {"status": [200, 400], "not_status": [500, 502, 503, 504], "accept_structured_4xx": true},
  "business": {
    "envelope": "auto",
    "success_when": {"/* 仅当 YApi res_body 对 code/ok 写了 const 或 enum 时填写；禁止默认 ok=true / code∈{0,200} */": true},
    "code_path": ["code", "errcode", "errCode"],
    "msg_path": ["message", "msg", "reason", "errmsg"]
  },
  "schema": {"type": "object", "required_fields": ["/* 仅来自 YApi res_body 已声明字段；EmptyResp 则为 [] */"]},
  "outcome": "success_or_structured_4xx"
}
```

- **HTTP 400（或 `accept_structured_4xx=true` 时的任意 4xx）且「正常返回数据」算通过**（强制）：响应是 JSON 对象，且含业务信封键至少一项：`code` / `errcode` / `errCode` / `reason` / `message` / `msg` / `errmsg`（如 `{"code":400,"reason":"..."}` 或 `{"errcode":-1,"errmsg":"..."}`）。这是服务校验拒绝，不是崩溃。报告记 `passed`（可标 `outcome=structured_4xx`），**不得**当产品缺陷。执行器 `isStructuredBusinessReject` 必须识别上述键（见 `scripts/generate_playwright_project.py`）。
- 不算通过：未有代码校准证据的 HTTP 5xx、`UNKNOWN` / `unknown request error`、空 body、非 JSON、HTML 网关页。失败后按 `reference/code-failure-calibration.md` 核对：HTTP 状态码和响应格式都与代码实现一致时，执行记 `passed` 并进入回归集。
- HTTP 200 时：仅当契约用 `const`/`enum` 声明了成功取值，才校验 `code_in` / `ok_is`；**禁止**默认要求 `ok===true` 或 `code∈{0,200}`。HTTP 400 时不要求成功业务码，也不跑写后 `verify_read` / `verify_db`。
- 禁止只有 `status_class` 而无 `http`+`business`（`envelope=http_only` 且文档确认无业务包时可例外，须在 `calibration.note` 写明）。`list` / `side_effect` 必须与 functional 一样带契约 `schema`，禁止再写无 schema 的 `status_class=2xx` 壳子。
- `schema.required_fields`：200 成功体**只**用契约已声明字段；YApi 为 EmptyResp /「暂无数据」/ 无 properties 时必须 `required_fields=[]`，只校验 `type`（如空 object / array），**禁止**启发式补 `code`/`data`。无响应契约时 `source=undeclared`，同样不得猜字段。**有 CodeWiki/proto 出参时优先代码**（`reference/code-response-schema.md`）：根 `[]Xxx` → `type=array`；字段名与 `repeated` 以代码为准；与 YApi 冲突写 `code_calibrated`，禁止默认 `object` 否定代码数组。400 结构化拒绝用信封键即可，不得因缺成功体字段判失败。`business.code_path` 建议同时声明 `code` / `errcode` / `errCode`。
- **负向 validation/error**：契约未声明缺参/非法入参的 HTTP 码时，禁止写 `status_class=2xx_or_4xx` 或 `not_status=5xx`。生成阶段只标记 `assertion_policy=code_return_required`、`calibration.source=pending_code_return`。执行后按 `reference/code-failure-calibration.md` 把 `expected.status` 和 `response_format` 写成代码返回点的精确结果。仅当契约明确声明鉴权强制（`auth.required`）时，无鉴权头才可期望 `4xx`。
- **省略必填 path 参数**：执行器在 path 仍含 `{id}` 且未提供替换值时会抛「路径参数未提供」；`path_params.id=""` 又会被 `validate_run` 判为 placeholder。表示「缺 path」时：把用例 `path` 改成**去掉占位段**的形态（如 `/api/couponBatch/`、`/api/coupon/`），`path_params` 用 `{}`，`negative_construction.strategy=omit_required_path`。禁止在回归集里保留空字符串 path 参数。
- 若按 `reference/missing-required-code-status.md` 完成 CodeWiki/GitNexus 调用链闭环、单变量隔离与定向重跑，
  可把缺参状态收敛为精确 200/400/500。精确 500 在状态码和响应格式都与代码一致时执行通过并进入回归集。

### 3. 写后读回（verify_read）

- 创建/更新类（mutating write）**必须**有 `verify_read`：`{method, path, id_from, assert_fields}`。
- **更新/编辑**另必须有 `pre_read`：先 GET 详情再编辑（`reference/edit-get-then-put.md`）。写后 `verify_read` 不能代替写前 GET。
- 执行顺序：编辑则 GET 详情 → 写接口成功 → 用返回 ID（或 fixture ID）再查询 → 断言读回字段与写入一致。
- 禁止「只断言创建响应 200」就算 functional 通过。
- 纯只读 GET/列表：`verify_read.mode=self`（本次响应即校验对象）可接受。
- 写操作且 ops 已解析到服务库时，另加 `verify_db`：只读 `SELECT` 确认落库字段；无库配置则 `verify_db_policy=not_applicable`。禁止用写 SQL 造数。

### 4. 状态变更前后校验（state_check）

- 上下架/启用禁用/审核状态等接口必须有 `state_check`：`{field, before, after, observe}`。
- 执行：变更前查询 → 变更 → 变更后查询；前后状态必须不同且 after 符合期望。
- 非状态机接口：`state_check: null` 且 `state_policy=not_applicable`。

### 5. 唯一测试标识 + 可清理

- 每条可写数据的 functional 必须带 `test_marker`（形如 `api-regression-{run_id}-{case_id}`），写入 name/note/title 等**契约已声明**的可读字段。标识前缀刻意避开 `test` 字样，避免与占位门禁混淆。
- 契约没有这类可读字段时，标识只保留在 `case.test_marker`，**不得**往请求 body 里 `setdefault` 契约外的键（如凭空加 `note`）：那既可能被服务拒绝，也会让「请求里不得出现非契约取值」失效。
- 必须有 `cleanup`：`{enabled, method, path, id_from}`；破坏性删除无隔离环境时 `enabled=false` 且写明原因，但标记字段仍要有，便于人工清理。
- 只读用例：`cleanup.enabled=false`，`cleanup.reason=read_only`。

### 6. 用例间不依赖执行顺序

- `independent: true`（强制）。
- `depends_on` 必须为 `[]`；禁止依赖其他 case 的创建结果。
- 共享数据只读自 `fixtures.json`；本用例写入的数据由本用例 cleanup 负责。

### functional 产物字段清单（机器可读）

| 字段 | 必填条件 |
|-|-|
| `fixture_refs` / `fixture_trace` | 始终 |
| `expected.http` + `expected.business` + `expected.schema` | `executable=true` |
| `verify_read` | mutating write；只读可为 `mode=self` |
| `pre_read` | 编辑/更新类 functional；创建/只读不强制 |
| `state_check` 或 `state_policy=not_applicable` | 始终 |
| `test_marker` | 始终（只读也生成，便于日志关联） |
| `cleanup` | 始终 |
| `independent: true`，`depends_on: []` | 始终 |
| `verify_db` | 写操作且存在 `db-config.json`；否则 `verify_db_policy=not_applicable` |
| `regression_ready: false` + `regression_blockers` | 始终（所有类别，不限 functional） |

## 负向构造必须自报家门

`validation` / `auth` / `error` 允许按契约构造非法值，但每条必须写：

- `fixture_policy: "negative_case"`（`fixture_trace` 因此可为空，见 `regression_promotion.FIXTURE_OPTIONAL_POLICIES`）
- `negative_construction: {"strategy": "...", "basis": "..."}`

| `strategy` | 适用 | `basis` 要写清 |
|-|-|-|
| `omit_required_body` | 契约声明 body required | 哪些字段被刻意省略 |
| `omit_required_query` | 契约声明 query required | 移除了哪些必填 query |
| `omit_required_path` | 契约声明 path 参数 | 用例 `path` 已去掉 `{param}` 段；`path_params={}`；禁止空串占位 |
| `invalid_field_type` | schema 有类型声明 | 按声明类型反向取值 |
| `empty_body_probe` | 契约未声明 required | 仅探测，不得据此强求 4xx |
| `pagination_contract_probe` | 有 limit/pageSize | 用的哪个有来源分页值；断言只收敛到非 5xx |
| `omit_optional_params` | 契约无必填参数 | 合法省略后应 2xx，**不得**期望 4xx |
| `unknown_required_resource_id` | 有必填资源 ID | 替换成哪个确定不存在的 ID |
| `unknown_optional_filter_id` | 可选过滤 ID required=0 | 期望空列表而非 4xx |
| `drop_auth_header` | 鉴权探测 | 契约是否声明强制鉴权 |

负向取值用**确定性非法值**（不存在 ID `00000000-0000-0000-0000-ffffffffdead`、`regression-negative-unknown`、越界数字），
**不是**占位符。`test` / `placeholder` / 全零 UUID 在任何类别的用例里都不允许——
它们无法表达「刻意非法」，只会让晋升内核判成未替换占位。

## 执行解读

失败须区分：

1. **真缺陷**：5xx / UNKNOWN、契约要求拒绝却放行、功能入参正确却无结构化响应地失败
2. **通过（含 4xx 业务包）**：HTTP 200 业务成功，或结构化 4xx + JSON 业务包（`code`/`errcode`/`message`/`errmsg`/`reason` 等）
3. **假失败**：模板期望 4xx，服务按可选参数返回 200/空列表
4. **弱通过**：auth 过松、断言只有非 5xx

报告不得把假失败或「结构化 400」计成产品缺陷。
