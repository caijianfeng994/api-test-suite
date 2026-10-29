---
name: api-test-suite-ops
description: Use when api-test-suite must prepare fixtures, bind required request fields, query the service database, or fall back to bundled web_cli admin data collection.
---

# 测试数据内核

## 目标

为执行类运行准备可追溯夹具。`run_mode=contract` 可以不主动造数，但已生成用例若存在 `required_binding_missing`，本阶段不再是 `not_applicable`：必须补齐 `pending_requirements[].attempts`，否则校验失败。

固定产物：`fixtures.json`。需要连库时另产 `db-config.json`、`db-tables.json`。后台取数证据在 `<run_dir>/execution/web-cli/`。

## 补数顺序（强制）

完整规则见 `reference/fixture-fallback.md`。

1. **造数工具**：用户夹具、已授权创建/查询 API，以及 `web_cli/ops-ass/SKILL.md` 所指的业务造数工具。本套件不附带具体造数场景。状态不对时用创建给下游供数，且成功路径独立种子（`reference/fixture-fallback.md`）。工具不覆盖当前场景时不得结束。
2. **删除/更新主键**：同 path 有 `POST` 创建或 `GET` 获取/列表时，先创建再获取主键，两步都不满足才查库。来源写 `api:`；查库必须是 `db:<service>.<table>.<column>`，并在 `resource_seed_attempts` 留下创建/获取的 `failed|blocked`。禁止用库里其它列当主键。
3. **服务数据库**：`reference/db-query.md`。只读；无匹配行则进入下一步。资源主键不适用「先于创建/获取查库」。
4. **后台取数**：读取并执行本套件 `web_cli/SKILL.md`（Shadow / onion-backstage）。禁止改用仓库根目录 `web_cli`。菜单文档没有服务名时仍要打开后台或抓后台接口，禁止把 `shadow_path_lookup` 当成取数完成。

数据库和后台都拿不到现有数据时，必须使用同资源创建 API、造数脚本、`ops-ass` 或后台创建页面实际造数；创建成功就回填并继续执行。实际造数仍失败或被环境阻断后，按 `reference/code-request-values.md` 读处理函数补代码字面量、透传文案，或把未读取字段标 `code_unused`。三种情况都不成立时，按 `reference/binding-log-supplement.md` 查近 30 天线上访问日志补请求值。日志给出非空且非敏感的值就回填；确认字段未出现、path 无调用，或敏感字段不能抄明文，才写 `required_binding_missing` 且 `executable=false`。每条 pending attempt 必须能被 `validate_run.py` 复核：造数目标和 `failed|blocked` 结果、字段级 `SELECT`、`execution/web-cli/` 证据文件加 `opened_url` 或 `captured_api`。只写步骤名、catalog 扫描或查询接口会 `fixture leak`。

## C 端网关鉴权夹具

当 `run-context.base_urls.target`（或用例 `base_url`）指向公网网关（`7to12-*.yangcong345.com/backend` 等），且成功路径需要登录态时：

1. 先用业务造数工具解析真实 C 端 `userId`（见 `web_cli/ops-ass/SKILL.md`，或已有 fixture），**禁止**用 Shadow 登录邮箱账号冒充。
2. 按 `reference/c-end-auth.md` 换票：
   - 优先 `web_cli/tokenUtil.getAppToken(user_id, env="test")`
   - 或 `scripts/fetch_c_end_auth.py --user-id ... --env test`
3. 回填 `shared.uid` / 请求头 `uid`，以及执行期 `Authorization`（推荐环境变量 `API_TEST_AUTHORIZATION`，`sources` 写 `api:tokenUtil.getAppToken` 或 `env:API_TEST_AUTHORIZATION`）。
4. 完整 JWT **不得**写入将上传 GitLab 的 `test-cases.json`；可写入本机 `execution/c-end-auth.json` 的脱敏摘要。

## 禁止部分绑定收工（P0，防漏补）

`endpoint_bindings.<endpoint_id>` **已有部分字段不等于 ops 完成**。

对每个将生成 functional/auth/side_effect 成功路径的接口：

1. 从 YApi/`req_body_other`/proto/catalog 列出**请求 schema 全部声明字段**（Path/Query/Body；默认必填策略见主 Skill）。
2. 与 `endpoint_bindings` 已绑定键做差集。
3. 差集非空时 **必须继续** 夹具三步，直到：
   - 全部回填；或
   - 每个缺口字段写入 `pending_requirements[]`，且带 `attempts`（至少含 `tool` / `db` / `web_cli` 中已执行项与结果）
4. **禁止**只绑了 `strategyId`/`userId` 等易查字段就进入 generator，导致大量 `required_binding_missing` + skipped。
5. 同一资源行（如 `push_purchase`）已查出 `worker_id` 时，**必须同查同回填**同行相关列（`worker_name`/`worker_avatar`/`kind`/`fenqi`/`show_in_card`/`add_content` 等），不得漏列。

`fixtures.json.steps` 须能看出对缺口字段的查库/后台动作；仅有 `fetch_db_config` 不算完成补数。

## 回填

- 只填请求 schema 已有字段；禁止塞响应独有字段。
- `sources` 必须标明 `api:` / `db:` / `web_cli:shadow:` / `code:` / `log:` 之一。`code:` 只用于 `reference/code-request-values.md` 的字面量、透传文案和 `code_unused`。`log:` 只用于 `reference/binding-log-supplement.md` 从近 30 天线上请求样本回填的值。
- **判别/条件字段**：按 `reference/fixture-discriminator-fields.md` 绑定。`userScope=user_ids` 时 `userLayer` 必须空；`chasePrevOrder` 非 true 时 `prevOrderLayer` 必须空；禁止 `userLayer: "all"` 一类占位。创建补数须满足下游接口约束（同文档）。
- 编辑成功路径另遵 `reference/edit-get-then-put.md`。
- `pending_requirements` 非空时不得声称成功路径已可执行。

## 禁止

- 编造连接串、UUID、占位 ID
- 对数据库执行写语句
- 因造数工具不支持就跳过查库和后台
- 用 `no_dedicated_fixture_factory`、无 SQL 的「已查相关表」、`shadow_path_lookup` 凑满三步名称后标缺参
- 数据库和 Shadow 都没有现有数据时，不调用创建 API、造数脚本、`ops-ass` 或后台创建页面就直接标缺参
- 把 Cookie / token 写入将上传 GitLab 的用例文档
- 把 `all` / `unlimited` / 枚举名填进「有值须为 http(s)」的分层/URL 字段
