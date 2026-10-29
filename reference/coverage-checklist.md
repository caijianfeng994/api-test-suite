# API 契约覆盖检查表

契约分析与计划阶段对照本表，缺项记入 `contract-analysis.md` 的覆盖缺口，并尽量落到条款/`UNCOVERED`。

## 交付门禁（防冒烟误导）

- [ ] 未使用「每接口 1 条 smoke」作为默认策略
- [ ] `test-cases.json` 中默认无 `category=smoke`（除非用户显式要求连通性探测）
- [ ] 若存在 smoke：报告有「冒烟探测说明（非正式完整覆盖）」章节，且用例名带「【冒烟探测】」
- [ ] 每接口至少：functional + validation；auth 按三态：`called_by_app`/`unknown`/缺字段必须有 auth，仅完整 `not_called_by_app` 可省略；全部请求字段默认必填；error/side_effect/list 按 `reference/regression-case-semantics.md`（无资源 ID 禁止「不存在→4xx」）
- [ ] `endpoint_bindings` 未「部分绑定收工」：请求 schema 声明字段相对绑定键的差集已清空；数据库和后台均无现有数据时已实际调用创建 API、造数脚本、`web_cli/SKILL.md` 或后台创建页面。仍 pending 的字段带造数失败结果、字段级 `SELECT`、`execution/web-cli/` 证据。catalog/菜单文档检索和「无造数脚本」不算完成
- [ ] 同一 DB 行已取主键/外键时，同行业务列（如 `push_purchase` 的 worker_name/avatar/kind/fenqi/add_content）已同查同回填
- [ ] 无 `required_binding_missing` 却缺少 fixtures 三步证据的「漏补」用例（`validate_run` 会失败）
- [ ] 夹具三步无真值的字段已按 `reference/code-request-values.md` 核对处理函数：字面量或透传文案来源为 `code:`，未读取字段为 `code_unused`
- [ ] 代码仍补不上的字段已按 `reference/binding-log-supplement.md` 查近 30 天线上访问日志：有非空请求值则 `log:` 回填；字段未出现、path 无调用或敏感字段不抄明文，才 `required_binding_missing`，并写入 `execution/binding-log-supplement.json`
- [ ] 每条可执行 functional 满足闭环 6 条：`fixture_refs`/`fixture_trace`（或 `none_ok`）、`expected.http+business+schema`、`verify_read`、`state_check` 或 `state_policy=not_applicable`、`test_marker`+`cleanup`、`independent=true` 且 `depends_on=[]`
- [ ] 会改数据的写接口另有 side_effect，且与 functional **独立种子**；GET/列表查询不得有 side_effect
- [ ] 列表另有 list，且请求**实际携带** limit/pageSize
- [ ] 未把 YApi `required=0` 的过滤字段当成必填
- [ ] 校验失败期望与契约一致（允许 200+业务码，禁止无依据 4xx）
- [ ] functional：结构化 4xx 且 JSON 含 `code`/`errcode`/`message`/`errmsg`/`reason` 等记通过，不因非 200 判失败；5xx 默认失败，状态码和响应格式都与代码一致时执行通过（`code-failure-calibration.md`）
- [ ] `full` / `baseline` / `replay` 已生成并通过五个回归产物门禁；`contract` 不要求回归产物
- [ ] 晋升用例均为 `passed`、有独立请求证据、无占位/敏感明文，且 `fixture_trace.source` 可解析
- [ ] `regression-fixtures.json`、回归 JSON/Markdown、待上传 Markdown/脚本/配置已通过敏感扫描；敏感值只使用环境变量引用
- [ ] 用户交付物指向完整 `test-cases.md` / `test-cases.json`
- [ ] 已执行 `core/gitlab-upload`，`gitlab-upload.json.ok=true`，并给出远程 tree 链接

## 身份与契约来源

- [ ] 方法、路径、接口用途明确
- [ ] 已确认 YApi 连通，且候选来自 YApi
- [ ] 已对照 GitLab 服务 `api/*/*.proto`（CodeWiki HTTP 映射）确认 path 存在；仅交集进入用例
- [ ] YApi 有而 proto 无的接口已过滤，不维护 case
- [ ] 代码实现已标下线的接口已过滤（`reason=code_offline`），不维护 case（见 `reference/code-offline-filter.md`）
- [ ] HTTP 501 或 `not implemented` 已按 `reference/unimplemented-traffic.md` 查近 30 天访问日志：有调用保留失败，0 调用整条接口 `ignored`，并写入 `execution/unimplemented-traffic.json`
- [ ] catalog 每个保留 path 已按 `reference/zero-traffic-ignore.md` 统计近 30 天调用数：0 调用整条忽略，有调用的 HTTP 500 保留失败，证据写入 `execution/path-traffic.json`
- [ ] 成功路径 `expected.schema` 已按 `reference/code-response-schema.md` 用代码出参校准：根 `[]Xxx` 为 array；字段名与 `repeated` 类型以代码为准；YApi 冲突写 `code_calibrated`，禁止未校准的默认 object
- [ ] 下线扫描已写 `code-offline-filter.json`：`scan.complete=true`，每条 kept/filtered path 均有 `scan.by_path`；主路径证据缺口已走 path/同文件兄弟兜底，禁止「缺口即放行」
- [ ] proto 有而 YApi 无、且未标下线的接口已进入 catalog 与用例，并标明 **proto有**
- [ ] 契约来源可追溯（YApi / OpenAPI / Proto / Issue / CodeWiki 接口文档 / 用户确认）
- [ ] 版本或兼容策略已知（若适用）

## 请求

- [ ] 路径参数：必填、类型、格式、枚举
- [ ] Query：全部默认必填、类型、边界；列表接口显式 `limit`
- [ ] Header：鉴权、租户、幂等键、内容类型
- [ ] Body：必填字段、类型、嵌套、长度/数值边界、示例
- [ ] 判别/条件字段：已按 `reference/fixture-discriminator-fields.md` 清空未启用分支（如 `userScope=user_ids` 时 `userLayer=""`，无 `*Layer=all`）
- [ ] 创建补数：下游写接口种子满足下游约束；`functional`/`side_effect` 等会消费资源的成功路径各用独立种子（`fixture-fallback.md`）
- [ ] 删除/更新主键先同 path 创建再获取；查库仅在两步都不满足时使用，且为 `db:<service>.<table>.<column>`
- [ ] 省略必填 path 的 validation：path 已去掉 `{param}`，`path_params={}`，无空串 placeholder（`omit_required_path`）
- [ ] 编辑/更新：计划与用例含 `pre_read`（先 GET 详情再按请求 schema 编辑，见 `reference/edit-get-then-put.md`）

## 响应与错误

- [ ] 成功状态码与业务码
- [ ] 成功响应 schema / 关键字段
- [ ] 错误码与 HTTP 状态一致性
- [ ] 校验失败、未授权、无权限、不存在、冲突等分支
- [ ] 空列表 / 空对象语义（列表可选过滤未知 ID → 200 空列表，不是 4xx）

## 安全与隔离

- [ ] 鉴权缺失/无效
- [ ] 权限不足
- [ ] 租户/用户数据隔离
- [ ] 敏感字段脱敏预期

## 状态、幂等与副作用

- [ ] 资源生命周期 / 业务状态流转
- [ ] 写操作幂等（重放、幂等键）
- [ ] 可观察副作用与清理策略
- [ ] 不可逆操作（支付、删除、消息）授权边界

## 可靠性与降级

- [ ] 超时、重试、限流行为（若契约声明）
- [ ] 下游失败 / 降级响应
- [ ] 部分成功语义（若适用）

## 兼容与迁移

- [ ] 字段增删改兼容规则
- [ ] 默认值与未知字段处理
- [ ] 迁移双跑允许差异白名单（仅 migration）

## 可追溯性

- [ ] 每条计划用例可映射到至少一个条款，或明确标记探索性并说明原因
- [ ] 阻塞条款有对应提问，无静默假设
