---
name: api-test-suite-functional
description: Use when api-test-suite independently tests a single endpoint or same-service API collection through CodeWiki/YApi discovery, contract analysis, planning, generation, execution, healing, and reporting.
---

# 场景插件：functional

## 输入

以下至少一项：`service_name`、YApi 链接/分类、`endpoint_id`、OpenAPI/Proto/契约文档或完整接口定义。执行前还需明确 Base URL 与鉴权。

discovery **必须**按 `core/discovery`：先探测并拉取 YApi（项目全量优先 `list_interfaces_by_project`），再按服务名拉取 GitLab 各 proto 的 HTTP 路径，再按 `reference/code-offline-filter.md` 排除实现已标下线的接口。YApi∩proto 与仅 proto 有、且未下线的接口都进入契约与用例；仅 proto 有的标明 `proto有`。仅有 YApi 且无法解析服务名时停止并询问 GitLab 服务名，不得只凭 YApi 写执行用例。YApi 有而 proto 无，或代码已标下线的 path 不得生成 case。

`run_mode`：

- `plan`：discovery → contract → ops → planner
- `generate`：读取已有目录，契约/planner/ops 完成后执行 generator
- `execute`：读取已有脚本，executor → healer（必要时）→ reporter；`contract=not_applicable`
- `contract`：discovery → contract → planner → generator（仅用例文档）；跳过 ops/执行
- `full`：完整顺序，默认值

触发「按契约写用例 / 条款矩阵 / 只设计不执行」时使用 `contract`。

## 独立编排

### full / plan / generate

按阶段读取：

1. **REQUIRED:** `core/discovery/SKILL.md`（YApi 连通 → 拉 YApi → proto 路径校验 → 代码下线过滤 → APP 调用证据 → 保留 YApi∩proto 与仅 proto 有且未下线，过滤 `not_in_proto` / `code_offline`）
2. **REQUIRED:** `core/contract/SKILL.md`（仅有目录时也做轻量就绪度；有契约材料时完整分析）
3. **REQUIRED:** `core/ops/SKILL.md`（`plan`/`full`/`generate`；缺 fixture 或写操作需落库时按 `reference/db-query.md` 查 GitLab 库配置，无明文则回退 Nacos Open API，再只读查表；编辑接口按 `reference/edit-get-then-put.md` 准备详情 GET）
4. **REQUIRED:** `core/planner/SKILL.md`
5. **REQUIRED:** `core/generator/SKILL.md`（`generate`/`full`）
6. **REQUIRED:** `core/executor/SKILL.md`（仅 `full`）
7. 仅框架、配置或 fixture 可修复失败时读取 `core/healer/SKILL.md`
8. **REQUIRED:** `core/reporter/SKILL.md`（`full`；`plan` 可出计划型报告）
9. **REQUIRED:** `core/gitlab-upload/SKILL.md`（`generate`/`full`；已有 `test-cases.md` 必须推 GitLab）

discovery 完成后必须确认：`<run_dir>/app-usage-evidence.json` 存在，且 catalog 每接口含 `app_usage` 摘要（见 `reference/app-usage.md`）。缺产物不得进入 planner。

### contract

1. **REQUIRED:** `core/discovery/SKILL.md`（允许暂缺 Base URL；有 YApi 或服务名则仍须 YApi/proto 对齐，含 proto有；须产出 `app-usage-evidence.json`）
2. **REQUIRED:** `core/contract/SKILL.md`
3. 写入空或占位 `fixtures.json` 仅当 planner 需要引用结构；默认 `stages.ops=not_applicable`，不调用真实造数
4. **REQUIRED:** `core/planner/SKILL.md`（读取契约产物；成功路径无数据时标不可执行）
5. **REQUIRED:** `core/generator/SKILL.md`（只产出 `test-cases.json` / `test-cases.md`，**不**生成 Playwright 脚本）
6. 回填 `traceability-matrix.md`
7. `executor` / `healer` / `comparator` / 执行型 `reporter` = `not_applicable`
8. **REQUIRED:** `core/gitlab-upload/SKILL.md`

`generate`/`execute` 使用用户提供的 `run_dir`，并验证前置阶段，不创建第二个时间戳目录。

## 结果

不运行 comparator。`run-context.stages.comparator=not_applicable`。

`full` 完成条件：

- catalog、契约产物（若本 run 启用）、`app-usage-evidence.json`、fixtures、plan、cases（含 `test-cases.md` 表格）、scripts、execution results、report、`gitlab-upload.json` 全部非空
- 用例为完整覆盖（全部请求字段默认必填的 functional 闭环 6 条 + validation；auth 按 `app_usage` 三态，仅完整 `not_called_by_app` 可省略；可推导的 error，改数据写操作的 side_effect，列表的 list），**默认无 smoke**；禁止无资源 ID 的 4xx「资源不存在」模板；functional 须含双轨断言、`verify_read`、`test_marker`/`cleanup`、`independent`（见 `reference/regression-case-semantics.md`）
- 追踪矩阵已回填用例 ID
- 所有不可执行用例均有明确原因
- 总状态按真实结果完成；存在产品缺陷允许测试运行完成，但报告必须保留失败
- 报告与 `test-cases.md` 作为用户可拿走的最全用例交付物；权威副本在 GitLab `mstrategysddtestcase`
- 交付报告明确列出完整用例数、已晋升数、backlog 数、`protected_remote` 和 `promoted`；
  完整库中的失败/跳过用例不等于可回归用例
- GitLab 回归集不被 incoming 失败/跳过/缺失版本降级；fixture 合并后仍可解析远程回归引用
- GitLab 目录含 `package.json`、`package-lock.json`、`.env.example`、`playwright.config.ts`、
  `tests/api-cases.spec.ts`，并可 `npm ci && npx playwright test`
- 上传前 `validate_run.py` 预检通过，合并后 staging 二次一致性/敏感检查通过，才允许 commit/push

`contract` 完成条件：

- catalog、`app-usage-evidence.json`、`contract-clauses.json`、`contract-analysis.md`、`traceability-matrix.md`、`test-plan.md`、`test-cases.json`、`test-cases.md` 均非空
- 矩阵中每个条款有用例 ID 或 `UNCOVERED`
- 无 `tests/*.spec.ts` 与执行结果要求
- `gitlab-upload.json` 非空且 `ok=true`
