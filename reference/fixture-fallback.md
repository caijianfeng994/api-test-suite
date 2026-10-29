# 夹具补齐：造数工具 → 服务库 → 后台取数

接口测试缺真实请求参数时，必须按下面三步执行，**禁止跳步**，禁止编造 UUID / 占位 ID，禁止因工具不支持就直接 `executable=false` 收场。

`run_mode=contract` 默认不发起造数、查库和后台操作，但**不能**用来跳过缺绑定门禁。用例出现 `required_binding_missing` 时，任何模式都必须在 `fixtures.json` 写下对应 `pending_requirements`，并补齐可复核的三步 `attempts`。证据不全时 `validate_run` 报 `fixture leak`，`status=completed` 不得通过。

## 顺序

```text
1. 造数工具（core/ops：已授权创建 API、本套件脚本、YApi/用户给定夹具）
2. 服务对应数据库只读查询（reference/db-query.md）
3. 本套件 web_cli：Shadow / onion-backstage 后台取数
```

任一步拿到**请求 schema 已声明字段**的可追溯真值后即可停止；把值写入 `fixtures.json` 再生成或重跑。

## 禁止部分绑定收工（防漏补）

常见事故：只从库绑了 `userId` / `strategyId` / `workerId`，同接口其它声明字段（如 `workerName`、`category`、`addContent`）未补，generator 大批 `executable=false`。

强制规则：

1. **以接口请求 schema 为清单**：YApi `properties` / proto 字段 / catalog 参数，默认全部要绑定（主 Skill「请求参数默认必填」）。
2. **`endpoint_bindings` 差集必须清空或进 pending**：已绑定键 ⊂ schema 时，差集每个字段继续三步；不得因「已经有几个值了」结束 ops。
3. **同行字段一次性取全**：对同一表同一行（例：`push_purchase` 已取 `worker_id`），同一次 SQL 选出并回填 `worker_name`、`worker_avatar`、`kind`（→`category`）、`fenqi`（→`installmentPayType`）、`show_in_card`、`add_content` 等映射列。
4. **pending 必须可审计**：`pending_requirements[]` 每项至少含 `endpoint_id`、`field`（如 `body.category`）、`attempts`。三步都要留下可复核动作，不能只写步骤名。
5. **`validate_run.py` 门禁**：用例出现 `required_binding_missing:*` 时，对应字段的 attempts 必须同时满足下面三条，否则报 `fixture leak` 且不得 `status=completed`：
   - `tool`：必须实际调用创建 API、造数脚本、`ops-ass` 或后台创建动作，并记录造数目标、`status=failed|blocked` 与结果。仅查询、catalog 扫描、`checked_endpoints` 或 `no_dedicated_fixture_factory` 都不算造数。
   - `db`：记录针对该字段的只读 `sql`（必须是 `SELECT`），并在 SQL 或结果中点名字段。
   - `web_cli`：记录 `opened_url` 或 `captured_api`，并有 `<run_dir>/execution/web-cli/` 下的非空证据文件。文件内容要能看到打开的页面或抓到的后台接口。

回填前必须遵守 `reference/fixture-discriminator-fields.md`：按 `*Scope` / 布尔开关决定配套字段；条件 URL 字段未启用时必须空，禁止把 `all` 等枚举抄进 `*Layer`。

## 第 1 步：造数工具

优先使用：

- 用户或计划已给出的合法夹具
- 已授权测试环境的前置创建 / 查询 API（`run-context.write_operations.authorized=true`）
- 本套件已有脚本能覆盖的场景（Excel 上传、固定组合商品等）

### 创建补数给下游写接口（通用）

库里已有行但**状态不对**（已作废、已绑定、已核销、已领取、已处理订单券等）时，优先用同资源族的**创建 / 领取 / 下发** API 造**未消费**数据，再回填下游用例；不要反复打同一条脏数据指望修绿。

| 规则 | 说明 |
|---|---|
| 匹配下游约束 | 创建参数必须满足下游接口前提（判别字段、资源类型、未绑定 SKU、可领取范围等），见 `fixture-discriminator-fields.md`「下游接口约束」 |
| `source` 可追溯 | 创建/领取返回的 id 写入 `endpoint_bindings`，`sources` 用 `api:<CreateXxx>.id` / `api:<ClaimXxx>.id` 等 |
| 独立种子 | 同一 `endpoint_id` 下，每条会改数据的成功路径（尤其 `functional` 与 `side_effect`，以及会消费资源的 C01/C05/C06）**各用独立种子**；禁止共用一条「跑一次就脏」的 id |
| 探活勿吞种子 | 调试/手工 `curl` 若会核销、绑定、领取，不得占用已写入用例的种子；先复制一份再探 |

示例（模式，非唯一服务）：`create(无 sku) → void`；`create → claim → usedCoupon`；`create(符合类型) → bind-sku / issue`；库中 `source_type=buy` 未处理券 → `redeem` / `restore`。

造数工具**不支持**当前场景的判定（满足任一即进入第 2 步）：

- 没有对应创建 API 或脚本
- 创建 API 因业务约束无法造出目标状态（已审核、定时上下架、飞书审批中、历史商品等）
- 工具报错且无法用同一工具换参重试
- 文档/代码明确该数据只能后台配置或只读库表产生

第 1 步失败必须写入 `fixtures.json` 的 `pending_requirements` 或 `execution/healing-log.json`，说明「工具不支持 / 调用失败」及已尝试路径。**禁止**在此步直接记 `required_binding_missing` 结束。

“没有专用造数脚本”不等于不支持造数。必须继续扫描同资源族的 `POST` 创建接口；存在创建接口时，按契约构造请求并实际调用。没有服务创建接口时，继续检查本套件 `ops-ass` 和 Shadow 对应资源的创建页面。

## 删除 / 更新的资源主键（先创建、再获取，最后才查库）

`PUT` / `PATCH` / `DELETE` 成功路径（含会带上真实主键的 `auth`）需要资源 `id` 时，**不要**先把库抽样列填进 `id`。同 path 已有创建或获取接口时，顺序固定为：

```text
1. 同 path 的 POST 创建接口，按契约造一条独立数据
2. 同 path 的 GET 获取/列表接口，从响应取出主键
   创建响应没有 id（例如只返回 {"ok":true}）时，这一步必须做
3. 没有创建/获取接口，或两步都实际调用后仍没有可用主键，才只读查库
```

查库回填必须写成 `db:<service>.<table>.<column>`，SQL 点名该资源表的主键列。禁止用库名（如 `db:course-mall.course_shop`）或不相关表的整数列充当 UUID 主键。

`sources` / `fixture_trace.source` 优先写 `api:<CreateRpc>.id` 或 `api:<GetRpc>.result[].id`。只有创建和获取都失败时，才允许 `db:`，并在 `fixtures.json` 的 `resource_seed_attempts.<endpoint_id>` 记录：

```json
[
  {"step": "create", "target": "POST /course-mall/course-shop/shadow/treasure-tags", "status": "failed", "result": "name最长不能超过6个字符"},
  {"step": "get", "target": "GET /course-mall/course-shop/shadow/treasure-tags", "status": "blocked", "result": "列表响应没有可用主键"}
]
```

`validate_run.py` 对同 path 存在 POST 或 GET、主键来源却是 `db:` 且缺少上述失败证据的用例报 `resource seed`。负向用例（空 body、故意不存在的 id）不走本条。

## 第 2 步：服务数据库

按 `reference/db-query.md`：GitLab 服务仓解析测试库连接，无明文则 Nacos `sensitive` / `DataId=dict`，再用 `db_readonly.py` / `db_agent.py` 只读查询。

- 查到记录：`sources` 写 `db:<service>.<table>.<column>`，回填请求同名字段。
- 库可连但无匹配行、表对不上字段、或 `service_db_unmapped`：**进入第 3 步**，不得伪造行。
- 禁止 INSERT / UPDATE / DELETE / DDL，禁止浏览器操作 Nacos。

## 第 3 步：web_cli 后台取数

**REQUIRED SUB-SKILL:** 本目录 `web_cli/SKILL.md`（不要用仓库根 `.claude/skills/web_cli/`）。

1. 读 `web_cli/shadow-path/SKILL.md`，按服务与缺数字段选菜单（组合商品、SKU、订单、用户、审核等）。菜单表没有该服务名字，只说明索引未覆盖，**不得结束本步**。
2. 读 `web_cli/playwright-cli/SKILL.md`，用 `playwright-cli` 打开测试环境 Shadow / onion-backstage；已有可用 session 则复用。
3. 页面查询；字段不够则用 Shadow 登录态抓对应后台接口。业务配置页不在菜单索引时，从已打开后台的 Network 找该服务的 shadow/admin 列表或详情接口继续取数。
4. 只回填**请求 schema 已有**字段；响应独有字段不得塞进请求 body。
5. `sources` 写 `web_cli:shadow:<一级/二级/三级菜单或路径>`。每个仍缺的字段在 attempt 中写 `opened_url` 或 `captured_api`，证据文件放 `<run_dir>/execution/web-cli/`。

以下写法**不算**完成第 3 步，`validate_run.py` 会判 `fixture leak`：

- `action=shadow_path_lookup`，或结果只写「菜单索引没有该服务 / 未索引 / 无法打开后台」
- 没有 `execution/web-cli/` 证据文件
- 用全局一条 `steps.web_cli` 覆盖全部缺口字段

枚举、分页和代码默认值（如 `grade`、`scene`、`pad`、`page`、`limit`、`state`）从契约枚举或实现默认值绑定，不进入「没有造数」流程。

后台页面和后台接口都取不到现有数据时，必须回到造数步骤，使用同资源创建 API、造数脚本、`ops-ass` 或后台创建页面生成独立测试数据。创建成功后回填新 ID/完整请求字段并继续执行。创建实际失败或被环境能力阻断后，仍须按 `reference/code-request-values.md` 读处理函数：代码字面量、无校验透传文案或 `code_unused` 可以收口。三种情况都不成立时，按 `reference/binding-log-supplement.md` 查该 path 近 30 天线上访问日志补请求值。日志给出非空且非敏感的值就回填并继续执行。确认字段未出现、path 无调用，或敏感字段不能抄明文时，才允许该字段 `executable=false` + `required_binding_missing:<位置>.<字段>`，并在报告写明造数目标及失败结果、SQL、打开的 URL，以及 `execution/binding-log-supplement.json`。

## C 端网关 Authorization（与「后台取数」并列）

缺的是**登录态 JWT**、不是业务资源 ID 时：不要走菜单点选，按 `reference/c-end-auth.md` 用 `web_cli/tokenUtil.getAppToken` 或 `scripts/fetch_c_end_auth.py` 换票。`uid` 与换票 `userId` 必须一致。集群 `.7to12` 直连且仅需 `uid` 的接口不适用本条。

## 产物

| 文件 | 说明 |
|------|------|
| `<run_dir>/fixtures.json` | 最终夹具；每条值带 `sources` |
| `<run_dir>/db-config.json` / `db-tables.json` | 第 2 步（脱敏） |
| `<run_dir>/execution/web-cli/` | 第 3 步页面/接口摘要（不上 GitLab） |
| `<run_dir>/execution/healing-log.json` | healer 触发补数时记录步骤 |
| `<run_dir>/execution/binding-log-supplement.json` | 代码仍补不上时，近 30 天线上日志补数证据 |

## 禁止

- 造数工具失败后跳过查库直接打开后台，或查库失败后不打开后台就标缺参
- 把「没有专用造数脚本」或 catalog 扫描写成造数完成；同资源已有创建接口、`ops-ass` 或后台创建能力时必须实际调用
- 把 `shadow-path` 文档检索写成第 3 步完成；必须打开后台或抓到后台接口
- 把 H5 / 完整 UI 回归当成接口造数的必经步骤
- 把后台 Cookie、token、明文手机号写入 `test-cases.json` 或上传 GitLab
- 用 Shadow 登录账号冒充 C 端 `userId`
