---
name: api-test-suite
description: Use when independently running functional API regression, refactor baseline/replay, migration dual-run comparison, or contract-driven API test-case design. Discovery connects YApi first, then keeps only paths present in the GitLab service proto files and not marked offline in handler code. Missing fixtures follow create-API then readonly service DB then bundled web_cli Shadow/admin data collection. DB connection comes from the GitLab service repo, falling back to the test Nacos Open API sensitive dict.
---

# 统一接口测试套件

## 目标

这是可单独运行的接口测试主入口。所有发现、契约分析、造数、计划、生成、执行、修复、比对和报告能力均来自本目录 `core/`；后台取数使用本目录内嵌的 `web_cli/`。禁止再委托仓库内其它接口测试 Skill，也禁止接口造数时改用仓库根目录 `.claude/skills/web_cli/`。

支持：

- `functional`：单接口或同服务接口集合常规测试；先连 YApi 拉候选，再与 GitLab 服务各 proto 的 HTTP 路径求交，并排除实现已标下线的接口后生成用例
- `refactor`：同服务重构前 baseline 与重构后 replay；用例范围是 YApi∩proto **加上** 仅 proto 有（标明 `proto有`），排除 YApi 有而 proto 无，以及代码已标下线
- `migration`：旧服务与新服务同时在线双跑比对；两侧分别走同一套保留规则（含下线过滤）
- 契约设计：`run_mode=contract`，从契约/目录生成可追溯用例文档，**默认不执行、不造数**
- 夹具补齐：造数工具不支持的场景 → 服务库只读查询（`reference/db-query.md`）→ 库仍缺失则用内嵌 `web_cli/` 打开 Shadow / onion-backstage 取后台数据（`reference/fixture-fallback.md`）
- 查库连接：先从 GitLab 服务仓解析测试库；仓内只有 `${PG_*}` 占位或拉不到明文时，用测试站 Nacos Open API 拉 `sensitive` / `DataId=dict` 补齐，再只读查表

原 `api-contract-to-test-cases` 能力已并入本套件的 `core/contract` 与 `run_mode=contract`，勿再单独调用外部契约 Skill。

智能体指令约定片段见 `reference/agent-instructions-snippet.md`。

## 路由

| test_kind | 触发语义 | 场景插件 |
|-|-|-|
| `functional` | 单接口、同服务、YApi、`service_name`+proto 路径校验、endpoint_id、常规回归、契约写用例 | `scenarios/functional` |
| `refactor` | 重构、baseline、replay、基线重放（`service_name` + YApi/proto 对齐，含 proto有） | `scenarios/refactor` |
| `migration` | 迁移、新旧服务、双跑、兼容比对（两侧 YApi/proto 对齐或用户映射） | `scenarios/migration` |

用户显式值优先；语义仍不清时只询问 `test_kind`。

契约相关说法（OpenAPI、Proto、契约 Issue、条款矩阵、只出用例不执行）优先映射为 `run_mode=contract`（通常搭配 `test_kind=functional`）。

## run_mode

| run_mode | 含义 |
|-|-|
| `full` | 完整执行链路（各场景默认，除 refactor） |
| `plan` | 到计划为止 |
| `generate` | 生成用例与脚本 |
| `execute` | 执行已有脚本 |
| `contract` | 契约分析 + 计划 + 用例文档；跳过造数与执行 |
| `baseline` / `replay` | 仅 refactor |

## 运行目录

一次运行只创建一个目录：

```text
run_id = YYYYMMDD-HHMMSS
run_dir = exports/api-test-suite/<test_kind>/<slug>/<run_id>/
```

初始化：

```text
<run_dir>/
├── run-context.json
├── yapi-ping.json
├── yapi-raw.json
├── codewiki-raw.json
├── yapi-proto-filter.json
├── interface-catalog.json
├── interface-catalog.md
├── app-usage-evidence.json
├── contract-clauses.json
├── contract-analysis.md
├── traceability-matrix.md
├── fixtures.json
├── db-config.json
├── db-tables.json
├── test-plan.md
├── test-cases.json
├── test-cases.md
├── code-validation-evidence.json
├── tests/
├── execution/
│   └── web-cli/              # 后台取数证据（不上 GitLab）
├── compare/
└── api-test-report.md
```

不存在内容的文件应在相应阶段创建，不得用空文件伪造完成。`run_mode=contract` 可不创建 `tests/`、`execution/`、`compare/` 与执行报告。只要用例含 `required_binding_missing`，就必须有 `fixtures.json` 的 `pending_requirements` 和三步证据；禁止把 `run_mode` 改成 `contract` 来跳过 `fixture leak`，证据不全时不得 `status=completed`。上下文契约见 `reference/run-context.md`。

## 独立内核

各阶段必须读取本目录对应 Skill：

| 阶段 | REQUIRED SUB-SKILL | 固定产物 |
|-|-|-|
| 接口发现 | `core/discovery` | `interface-catalog.json`、`interface-catalog.md` |
| 契约分析 | `core/contract` | `contract-clauses.json`、`contract-analysis.md`、`traceability-matrix.md` |
| 测试数据 | `core/ops` | `fixtures.json`（`contract` 模式 N/A）；造数不够则查库（`db-config.json`，`reference/db-query.md`）；库仍缺则 `web_cli/` 后台取数（`reference/fixture-fallback.md`） |
| 测试计划 | `core/planner` | `test-plan.md` |
| 用例与脚本 | `core/generator` | `test-cases.json`、`test-cases.md`；执行模式另需 `tests/*.spec.ts` |
| 自动执行 | `core/executor` | `execution/results.json`、stdout/stderr |
| 失败修复 | `core/healer` | 修复日志与复跑证据；缺参代码校准时另产 `code-validation-evidence.json` |
| 结果比对 | `core/comparator` | `compare/comparison.json`（refactor/migration） |
| 报告 | `core/reporter` | `api-test-report.md`（`contract` 模式可用分析文档代替执行报告） |
| GitLab 落库 | `core/gitlab-upload` | `gitlab-upload.json`；远程 `mstrategysddtestcase`（与 test-multica 同仓） |

覆盖检查见 `reference/coverage-checklist.md`；APP 调用查询与鉴权三态见 `reference/app-usage.md`；回归用例语义见 `reference/regression-case-semantics.md`；缺参用例的 CodeWiki/GitNexus 状态校准见 `reference/missing-required-code-status.md`；执行失败后用代码核对状态码和响应格式见 `reference/code-failure-calibration.md`；代码已标下线不维护见 `reference/code-offline-filter.md`；YApi 与代码都有但执行 501/未实现时按近 30 天日志决定失败或忽略见 `reference/unimplemented-traffic.md`；保留接口近 30 天 path 无请求时整条忽略见 `reference/zero-traffic-ignore.md`；成功路径响应 schema 以代码出参校准见 `reference/code-response-schema.md`；夹具三步仍无值时按处理函数补请求值见 `reference/code-request-values.md`；代码仍补不上时按近 30 天线上日志补请求值见 `reference/binding-log-supplement.md`；造数/查库/后台取数顺序见 `reference/fixture-fallback.md`；判别字段与条件 URL 绑参见 `reference/fixture-discriminator-fields.md`；查库细节见 `reference/db-query.md`；编辑接口先 GET 再 PUT 见 `reference/edit-get-then-put.md`。

YApi 输入：discovery **必须先**探测 YApi 连通（`--ping`），再拉接口；会话有 YApi MCP 工具可直接调用，否则 **必须**执行 `scripts/fetch_yapi_interface.py`。项目页 / `projectId` / 某服务全部接口优先 `list_interfaces_by_project`（`--project-url` / `--project-id`），失败才按分类旁路。
有服务名时再走 CodeWiki `get_api_reference`，把它当作 GitLab `api/*/*.proto` 的 HTTP 路径清单（会话未注册则执行 `scripts/fetch_codewiki_interfaces.py`）。然后 **必须**执行 `scripts/filter_yapi_by_proto.py`：YApi 且 proto 都有的 path 写入 catalog（`YApi+proto`）；仅 proto 有的也写入并维护 case（`source_label=proto有`）；YApi 有而 proto 无的自动过滤、不维护。写入保留条目后，**必须**按 `reference/code-offline-filter.md` 扫描 Handler 实现（含证据缺口时的 path/同文件兄弟兜底，并产出 `code-offline-filter.json` 的 `scan.by_path`）：代码已标下线（如 `// 2025.4.1日下线` + 空壳）→ `filtered.reason=code_offline`，**不维护**。写入保留条目的 JSON 形状与原 GitNexus 产物相同（`source.type` 仍为 `gitnexus`，并保留 `sources`/`yapi`/`presence`/`source_label`）。普通接口发现禁止再调用 `list_gitnexus_repos` / `get_route_graph` / `get_symbol_context`；**例外**仅为 `reference/code-offline-filter.md`（下线扫描）、`reference/missing-required-code-status.md`（缺参校准）、`reference/code-failure-calibration.md`（失败响应核对）与 `reference/code-request-values.md`（请求值代码补齐），均须先查新鲜度和 API 入口，再按最小范围使用 `search_code_graph` / `get_symbol_context`。本目录 `scripts/` 不会被自动调度，只有 Skill 写明的命令才会被智能体执行。

## 标准启动流程

1. 确定 `test_kind`、`run_mode`、`slug`，创建 `run_dir` 和 `run-context.json`。
2. 读取对应场景插件：
   - `scenarios/functional/SKILL.md`
   - `scenarios/refactor/SKILL.md`
   - `scenarios/migration/SKILL.md`
3. 场景插件按需编排独立内核，不得跳过门禁。
4. 每阶段结束更新 `run-context.json.stages` 和 `artifacts`。
5. 任一阶段阻塞时写明原因并停止后续依赖阶段；契约 `BLOCKED` 时仍可继续未受影响接口的计划/用例，但不得伪称契约完备。
6. 已生成 `test-cases.md` 后 **必须** 读取并执行 `core/gitlab-upload/SKILL.md`（推荐 `scripts/upload_gitlab.py`）。禁止仅以本地 `exports/` 交付。

## 全局门禁

- 接口定义必须来自 YApi、平台端点工具、CodeWiki/proto 接口文档、OpenAPI/Proto/契约文档或用户显式定义。
- `functional` / `refactor` / `migration` 编写用例前：先确认 YApi 连通并拉候选；再核对该 GitLab 服务各 proto HTTP path。**YApi∩proto 与仅 proto 有的接口都写 case**；仅 proto 有的必须标 `source_label=proto有`。YApi 有而 proto 无 → 过滤不维护。**实现层已标下线**（`reference/code-offline-filter.md`，含证据缺口兜底）→ `code_offline` 过滤、不维护，即使 proto/YApi 仍声明。YApi 不通或仓库不唯一 → discovery 停止。禁止把未过 proto 门禁或已标下线的接口写入 `test-cases.json`。
- Base URL 只能来自工具结果、用户输入、环境变量、`core/discovery` 的 k8s 服务名规则，或 CodeWiki 证据中的 `.7to12` / `.backsys`；`contract` 模式允许暂缺 Base URL。
- `fixtures.pending_requirements` 非空时不得生成成功路径脚本。
- **写接口授权（P0）**：测试环境写请求的授权写入 `run-context.json` 的 `environment` + `write_operations`（完整示例与字段语义见 `reference/run-context.md`）。权威解析为 `scripts/write_authorization.py` 的 `resolve_write_authorization`；generator / execution 工程 / `validate_run.py` 必须复用，禁止另写冲突算法。仅 `environment=test` 可使用 `scope=test_environment`；非测试环境不可继承该授权，且 scope 必须精确匹配自身环境。`ALLOW_WRITE_TESTS=true` 仅为 legacy 兼容，context 显式授权优先；新运行必须写 context。已授权时 **禁止** 按 `POST`/`PUT`/`PATCH`/`DELETE` Method 统一 skip，也 **禁止** 产生「写接口未获得本次显式副作用授权」；仍按 `executable`、`required_binding_missing`、敏感环境变量与契约 blocker 决定是否执行。写请求的运行时 `API_BASE_URL` 必须与生成时 `run-context.base_urls.target` 一致；目标缺失、不一致或 context 摘要变化时须 fail closed：先抛错并停止发请求，更新 `run-context.json` 后重新生成 execution 工程才能恢复。当前 `good` 验收：授权型跳过为 0；真实参数缺失仍允许不可执行/跳过且必须带明确 blocker，禁止为「0 skipped」伪造参数。
- **请求参数默认必填（P0）**：只要接口请求 schema 声明了 Path、Query 或 Body 字段，用例生成和数据准备时一律按必填处理，不读取、不采信 YApi/proto/代码中的 `required=false`、可选标记或缺省行为。functional 与 auth 必须为每个声明字段绑定非空、可追溯的有效值。缺值时先走夹具三步；三步仍没有非空真值时，必须按 `reference/code-request-values.md` 读处理函数：代码字面量或无校验透传文案写入 `code:` 绑定，处理函数未读取的字段标 `code_unused` 并省略。三种情况都不成立时，按 `reference/binding-log-supplement.md` 查该 path 近 30 天线上访问日志，用请求里的非空值回填。日志确认字段未出现、path 无调用，或敏感字段不能抄明文，才 `executable=false` 并记录 `required_binding_missing:<位置>.<字段>`。查询失败不得当成没有值。该规则是测试执行门禁，不得反向篡改原始契约材料。已获写授权 **不等于** 允许缺失 fixture、伪造参数或使用 placeholder。
- **失败响应代码核对（P0）**：执行判失败后，必须按 `reference/code-failure-calibration.md` 用代码核对 HTTP 状态码和响应格式（`code` / `reason` / `message` / `msg`）。符号必须是 `file.go:行号 函数名`，路由摘要无效。两者都与代码实现一致时执行 `scripts/apply_code_return_calibration.py`，把负向用例改成该精确断言并记 `passed`。状态或文案对不上才保留 `failed`。`validate_run` 在 `contract` 模式下也不跳过：猜测性 `2xx_or_4xx`、未闭合的 `pending_code_return`、伪符号、已核对一致却仍标 `failed` 都会失败。
- **缺参状态代码校准（P0）**：validation/error 的缺参状态未被契约明确、实测出现 5xx，或用户要求从代码确认 200/400/500 时，必须执行 `reference/missing-required-code-status.md`。一次只移除一个字段，其余字段保持类型合法且可追溯；代码证据标 `INFERRED`。显式校验拒绝断言 400，默认/空过滤继续执行断言 200，nil/panic 导致的 500 记录为 `PRODUCT_DEFECT`；状态码和响应格式都与代码一致时执行结果记为 `passed`，并进入回归集。
- **删除/更新主键（P0）**：`PUT`/`PATCH`/`DELETE` 的资源 `id` 先用同 path 创建接口造数，再用同 path 获取/列表接口取出主键；创建响应没有 `id` 时必须再 GET。两步都不满足才允许查库，且来源必须是 `db:<service>.<table>.<column>`。禁止把库抽样的其它列填进主键。证据见 `reference/fixture-fallback.md`；`validate_run` 缺少创建/获取失败证据却用 `db:` 会报 `resource seed`。
- **夹具三步（P0）**：造数工具不支持当前场景时，必须查该服务测试库；库无匹配数据时，必须读取并执行本目录 `web_cli/SKILL.md`，用 Shadow / onion-backstage 取后台数据回填请求字段（`reference/fixture-fallback.md`）。数据库和后台都没有现有数据时，必须回到造数步骤，实际调用同资源创建 API、造数脚本、`web_cli/SKILL.md` 或后台创建页面；成功路径使用**独立种子**。禁止因工具能力不足直接 `required_binding_missing`。实际造数仍失败或被环境阻断才允许不可执行。`no_dedicated_fixture_factory`、catalog 扫描、查询接口、无字段 SQL 的查库摘要、`shadow_path_lookup` 菜单文档检索都不算造数完成；`validate_run` 会因缺少可执行证据报 `fixture leak`。**禁止部分绑定收工**：`endpoint_bindings` 已有部分字段时须继续补齐该接口请求 schema 全部声明字段（或 pending+attempts）；同表已取 `worker_id` 须同回填 `worker_name` 等同行列。
- **C 端网关鉴权（P0）**：公网网关成功路径除 Header `uid` 外须绑定用户 `Authorization`。换票按 `web_cli/SKILL.md` 与 `reference/c-end-auth.md`，使用业务自己的造数工具。用例使用 `${API_TEST_AUTHORIZATION}`。禁止把完整 JWT 写入将上传 GitLab 的用例文档。
- **未实现且无流量（P0）**：YApi 与代码都有的接口，执行返回 HTTP 501 或 `not implemented` / `Unimplemented` 时，必须查该服务线上访问日志，时间窗固定近 30 天（`reference/unimplemented-traffic.md`）。`hit_count>0` 保留失败；`hit_count=0` 整条接口记 `ignored`，不计入失败。查询失败不得当成无调用。证据写入 `execution/unimplemented-traffic.json`，`validate_run` 缺少证据或时间窗不符会失败。
- **全量 path 无请求（P0）**：代码下线过滤后，catalog 里每个保留 path 都要统计近 30 天线上调用数（`reference/zero-traffic-ignore.md`）。禁止只统计缺绑定字段的接口，也禁止只统计 501。`hit_count=0` 整条接口忽略，写入 `execution/path-traffic.json` 和 `traffic-ignore.json`，不进入用例、失败、跳过或回归集。参数已绑定、代码为 `active`、实测 HTTP 500 或 panic，只要 path 调用数为 0，同样忽略。`hit_count>0` 的 500 保留失败。查询失败不得当成 0。`validate_run` 发现 5xx 失败没有 path 证据、时间窗不符，或 0 调用仍标 `failed` 时失败。
- **缺绑定线上日志补数（P0）**：夹具三步和代码字面量 / 透传 / `code_unused` 都补不上的字段，必须再查该 path 近 30 天线上访问日志（`reference/binding-log-supplement.md`）。请求里有非空且非敏感的值就回填，来源 `log:<path>#<位置>.<字段>`，并重新生成成功路径。字段未出现、path 无调用，或敏感字段不能抄明文，才允许 `required_binding_missing`。查询失败或日志没有请求内容时不得收口。证据写入 `execution/binding-log-supplement.json`，`validate_run` 缺少证据、时间窗不符或把 `unverified` 当成缺绑定会失败。
- **响应 schema 代码校准（P0）**：成功路径 `expected.schema` 必须以 CodeWiki/proto 出参为准（`reference/code-response-schema.md`）。根类型 `[]Xxx` 断言 array；字段名用代码原文（`videoId` 不是 `VideoId`）；`repeated` 字段不得因 YApi 带 `properties` 就断言成 object。YApi 与代码冲突时 `source=code_calibrated`。禁止无代码证据时默认 `type=object` 否定实测数组。`validate_run` 在 catalog 已有代码出参却仍用未校准 YApi/`undeclared` 时失败。
- **判别/条件字段（P0）**：存在 `*Scope` / `chase*` 等开关时，只为当前分支填配套字段；未启用分支的 `*Layer` / URL 字段必须空，禁止把 `all` 等枚举抄进分层链接（`reference/fixture-discriminator-fields.md`）。创建给下游供数时对齐下游约束。
- 需要真实业务 ID、库表数据或写后落库校验时：必须用 `scripts/fetch_gitlab_db_config.py` 解析连接（支持 `PG_HOST` 等独立字段及 `PG_SOURCE` / `POSTGRES_SOURCE` / `DATABASE_URL` URI；GitLab 明文优先，无明文则回退 `scripts/fetch_nacos_db_config.py` / 同一命令内的 Nacos Open API），再用 `scripts/db_readonly.py`（或 `db_agent.py`）只读查询。全部声明字段默认必填，其中业务 ID 必须绑定真实数据；auth 用例只改变鉴权条件，全部业务参数仍用有效 fixture。查库仍无值时进入 `web_cli` 后台取数，不得发送空 UUID/占位 ID。禁止编造连接串，禁止 INSERT/UPDATE/DELETE/DDL，禁止 Playwright 操作 Nacos 控制台。
- 列表/查询接口正常与边界请求必须显式携带 `limit`（或契约中的 `pageSize`）。
- **默认生成接近回归测试的完整用例**（functional 闭环 6 条 + validation；auth 按 `app_usage` 三态：仅完整 `not_called_by_app` 可省略，见 `reference/app-usage.md`；error/side_effect/list **按契约语义**），交付 `test-cases.json`+`test-cases.md`；**禁止**默认 smoke，也**禁止**无必填资源 ID 仍套「不存在 → 4xx」。细则：`reference/regression-case-semantics.md`（含 functional 闭环与双轨断言、`omit_required_path`、结构化 `errcode`/`errmsg`）。**编辑/更新成功路径必须先 GET 详情再编辑**（`reference/edit-get-then-put.md`）。**结构化 4xx + JSON 业务包算通过**。5xx 默认失败；按 `reference/code-failure-calibration.md` 确认状态码和响应格式都与代码一致后，执行记 `passed`。
- 仅当用户显式要求连通性探测时才可生成 `category=smoke`，名称必须以「【冒烟探测】」开头，并在 `api-test-report.md` 独立章节标明非正式完整覆盖。
- 推荐用 `scripts/generate_full_cases.py` 展开完整用例，再用 `render_test_cases_table.py` 出表。
- 测试框架固定为 Playwright APIRequest，不启动浏览器（`contract` 模式可不生成脚本）；脚本勿默认命名 `smoke.spec.ts`。
- 所有实测结论必须可追溯到请求、响应、状态码、耗时与 stdout/stderr。
- 契约条款状态必须为 `CONFIRMED` / `INFERRED` / `MISSING` / `CONFLICTING`；不得把推断当已确认，不得在冲突时静默选边。
- 不得删除失败用例、伪造 fixtures 或无证据放宽断言。例外只有两处：`reference/unimplemented-traffic.md` 确认近 30 天 0 次调用的 501/未实现接口；`reference/zero-traffic-ignore.md` 确认近 30 天 path 调用数为 0 的接口。两者都必须有日志证据，查询失败不得忽略。
- 执行结束后报告必须指向完整用例产物，不得只给执行摘要。
- 用例生成完成后必须上传 GitLab（`core/gitlab-upload`，仓库 `backsys-testgroup/mstrategysddtestcase`，路径 **`api/<服务名>/`**；已存在则**合并补充**到已有完整用例文档与可执行脚本，不丢旧 case）。远程失败不得声明完成。
- GitLab 同时维护完整库与已验证回归集：完整库继续增量合并；回归集同 ID 只接受
  `regression_ready=true` 且实测 `passed` 的 incoming（执行通过即晋升并保留请求取值，含代码核对一致的 5xx）。
  失败、跳过、缺失的 incoming 不得覆盖/删除远程成功版本；`regression-fixtures.json` 必须保留远程回归引用的数据。
- 上传前必须先通过 `validate_run.py` 预检；合并后 staging 必须再次通过一致性与敏感检查，之后才允许 commit/push。

## 完成条件

- `functional full`：发现、契约（若适用）、fixtures、计划、用例、脚本、执行、报告、GitLab 落库均非空。
- `functional contract`：发现、契约三件套、计划、用例（JSON+MD+追踪矩阵回填）、GitLab 落库均非空；无执行产物。
- `refactor baseline`：上述执行产物加 `baseline-manifest.json` 与 GitLab 落库。
- `refactor replay`：复用 baseline 用例并产生执行、comparison、报告与 GitLab 落库。
- `migration full`：新旧成对执行、comparison、报告、GitLab 落库均非空。
- 所有执行类交付必须报告：完整用例数、已晋升数、backlog 数、`protected_remote` 与 `promoted`；
  通常 **执行 `passed` 数应等于已晋升数**。代码核对一致的 5xx 计入通过并进入回归集，不单独产生差额。
- GitLab 服务目录必须包含可独立安装和收集的 Playwright 工程：
  `package.json`、`package-lock.json`、`.env.example`、`playwright.config.ts`、
  `tests/api-cases.spec.ts`，并可执行 `npm ci && npx playwright test`。

声明完成前必须执行：

```bash
python .claude/skills/api-test-suite/scripts/validate_run.py "<run_dir>"
```

只有命令退出码为 0，且 `run-context.json.status=completed`，才能声明完成。
