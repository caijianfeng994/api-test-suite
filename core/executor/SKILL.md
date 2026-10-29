---
name: api-test-suite-executor
description: Use when api-test-suite has collected Playwright APIRequest tests and must execute them with durable request, response, and process evidence.
---

# 自动执行内核

## 前置

- generator 阶段为 `completed`
- 完整执行优先使用 `generate_playwright_project.py --mode execution` 生成的工程（默认
  `<run_dir>/execution-runtime/`，内含 `tests/api-cases.spec.ts` 与 `playwright.config.ts`）
- 必需环境变量与服务网络可用
- `run-context.json` 的写授权已按 `reference/run-context.md` 写入；解析权威为
  `scripts/write_authorization.py`

## 写授权执行契约

当 `resolve_write_authorization` 判定 `allowed=true`（典型：`environment=test` 且
`write_operations.authorized=true`，或 test + legacy `ALLOW_WRITE_TESTS`）时：

- **禁止**根据 `POST` / `PUT` / `PATCH` / `DELETE` Method 统一 `test.skip`
- **禁止**产生「写接口未获得本次显式副作用授权」或同类授权型跳过文案
- 仍按 `executable`、`required_binding_missing:*`、敏感环境变量缺失、契约 blocker 决定是否执行
- 写请求的运行时 `API_BASE_URL` 必须与生成时 `run-context.base_urls.target` 一致；缺失或不一致时在发请求前 fail closed
- 授权上下文被改写或 legacy 环境授权被撤销时 **fail closed**：先抛错并停止发送任何请求；
  必须更新 `run-context.json` 后重新生成 execution 工程，才能恢复执行

未授权时，写方法可跳过并记 `write_operations_not_authorized`；破坏性未授权在生成期已标
`executable=false`。

`validate_run.py` 在授权运行中额外校验：

- 写用例仍因缺授权被跳过 → 报错
  `authorized write case still skipped for missing authorization: <id>`
- 显式 `write_authorization.destructive=true` 且无 `required_binding_missing:*` 时不得继续
  `executable=false` → 报错
  `authorized destructive case remains non-executable without binding blocker: <id>`
- `destructive=false` 不得误判为破坏性；旧产物缺 `destructive` 字段才可按 `category=side_effect` 回退

当前 `good` 验收：授权型跳过为 0；真实参数缺失仍允许不可执行/跳过，且必须带明确 blocker。

## 未实现流量复核

执行结果出现 HTTP 501，或响应含 `not implemented` / `Unimplemented` 时，在汇总失败前按 `reference/unimplemented-traffic.md` 查询该 path 近 30 天线上访问日志。0 次调用把该接口全部结果改为 `ignored`；有调用保持 `failed`。证据写入 `execution/unimplemented-traffic.json`。

其余 HTTP 5xx 在汇总失败前按 `reference/zero-traffic-ignore.md` 核对同一 path 的近 30 天调用数。0 次调用整条忽略；有调用保持 `failed`。证据写入 `execution/path-traffic.json`。catalog 定稿后就应对每个保留 path 做这次统计，不能等失败后再只补缺绑定接口。

## 执行

优先在 execution 工程内安装并运行（证据仍写回父 run）：

```bash
python3 .claude/skills/api-test-suite/scripts/generate_playwright_project.py \
  --run-dir "<run_dir>" \
  --service-name "<service>" \
  --mode execution \
  --output-dir execution-runtime
npm ci --prefix "<run_dir>/execution-runtime"
npm test --prefix "<run_dir>/execution-runtime"
```

也可从仓库根目录对已有配置执行：

```bash
npx playwright test --config "<run_dir>/playwright.config.ts" --reporter=json
```

不得启动浏览器，不得执行 UI 测试。

断言须按 `test-cases.json` 的 `expected`：支持双轨（`expected.http` + `expected.business`）与 `expected.schema`。`empty_collection=true` 时接受 HTTP 200 + 空列表；`status_class=2xx_or_4xx` 且 note 标明未强制鉴权时，200 不算失败。`validation` / `error` 的 `assertion_policy=code_return_required` 在没有精确 `status` 时直接失败，禁止把「非 5xx」当成断言；闭合后只接受代码返回的状态码和 `response_format`。

**禁止执行器猜测业务成功值**：仅当 `expected.business.success_when` 显式含 `code_in` / `ok_is` 时才校验取值；不得默认 `ok===true` 或 `code∈{0,200}`。

**HTTP 400 结构化响应 = 通过**（见 `reference/regression-case-semantics.md`）：状态 400，body 为 JSON 对象且含 `code`/`reason`/`message`/`msg` 任一字段 → `passed`，不得因 `expected.http.status` 只有 200 判失败。5xx 与 `unknown request error` 默认失败；按 `reference/code-failure-calibration.md` 确认状态码和响应格式都与代码一致后记 `passed`。400 通过时跳过 `verify_read` / `verify_db` / `cleanup` 写后步骤。

缺参代码状态校准按 `reference/missing-required-code-status.md` 执行。若
`expected.code_evidence.classification=PRODUCT_DEFECT` 且响应为 5xx，执行器必须校验精确状态和响应格式：
与代码一致则记 `passed` 并允许晋升，不一致才失败。

functional 闭环执行顺序（若字段存在）：

1. 可选 `state_check.observe`（变更前）
1b. **编辑类**（`PUT`/`PATCH`/「编辑」且存在 `pre_read` 或契约为整资源更新）：先 GET 详情，按请求 schema 组 body，再发主请求（`reference/edit-get-then-put.md`）。禁止用稀疏占位 body 直接 PUT。
2. 主请求
3. 断言 HTTP + 业务码 + schema
4. `verify_read`（写后查询或 `mode=self`）
5. 可选 `verify_db`：写操作且存在 `<run_dir>/db-config.json` 时，用 `scripts/db_readonly.py` **只读**核验落库（见 `reference/db-query.md`）；证据写入 `execution/db/<case_id>.json`。DB/GET 回填只填 **请求 schema** 字段，不得把响应独有字段合并进 body
6. `state_check` 变更后观察
7. `cleanup`：生成器给出的 `cleanup.enabled=true` 只是清理候选，执行前必须核验该 DELETE /
   清理方案确实存在且安全，核验通过才发清理请求；确认无安全清理接口时把产物改成
   `cleanup.enabled=false` 且 `cleanup.reason` 写具体原因（如人工处理、无可用清理接口），
   **不得**伪称已清理，也**不得**发送臆造的清理请求

禁止把「可选过滤返回空列表」判成产品缺陷。禁止跳过 `verify_read` 仅凭创建响应宣称写成功。无 `db-config.json` 时 `verify_db` 为 `not_applicable`，不得手写连接串。

## 证据

保存：

```text
<run_dir>/execution/
├── results.json
├── stdout.log
├── stderr.log
└── requests/
```

`results.json` 至少包含 total、passed、failed、skipped、duration、每个用例状态和错误。

请求证据（execution 工程写回父目录 `execution/requests/<case-id>.json`）至少包含：

- case ID
- endpoint ID
- target/source 标识
- method、完整 URL（敏感 query 脱敏为 `***`）
- 请求 headers / query / path_params / body（敏感字段值必须为 `***`，不得泄露）
- 响应状态、Body、耗时
- 结构化 authorization 元数据（与生成期 `writeAuthorization` 一致）

## 执行后必须晋升

执行与证据落盘完成后 **必须** 运行晋升脚本，生成回归集与 backlog：

```bash
python3 .claude/skills/api-test-suite/scripts/regression_promotion.py --run-dir "<run_dir>"
```

脚本按 case ID 关联 `test-cases.json`、`execution/results.json`、`execution/requests/<case-id>.json`，回写每条用例的 `execution_status` / `regression_ready` / `regression_blockers` / `promotion_notes`，并产出：

- `regression-promotion.json`：机器可读的逐用例判定、执行分布与 blocker 统计
- `regression-cases.json` / `regression-cases.md`：仅 `regression_ready=true` 的已验证回归集
- `backlog-cases.md`：未通过用例（failed / skipped / not_run）的请求摘要与 blocker

`regression-promotion.json`、`regression-cases.md` 与 `backlog-cases.md` 的请求摘要**只写请求形状，不含取值**；
完整可执行请求值只保留在受敏感字段环境变量替换保护的 `regression-cases.json`。

通常 **执行 `passed` 即进入回归集**：`execution_status=passed` 时设 `regression_ready=true`、`regression_blockers=[]`，并**保留完整请求取值**（query / body / path_params）。缺证据、fixture 追溯不全、占位/敏感明文、期望精度等问题只写入 `promotion_notes`，**不挡晋升**；生成期的 `required_binding_missing:*`、`case_not_executable` 等对已通过用例同样不挡。敏感键写入回归集前改为 `${API_TEST_*}` 引用。

代码核对后状态码和响应格式都一致的 5xx，与其它 `passed` 一样 `regression_ready=true`。failed / skipped / not_run 进入 backlog。

输入损坏时脚本以退出码 2 失败：`test-cases.json` / `results.json` / 证据 JSON 解析失败，用例 ID 缺失或重复，结果结构完全不可识别。**必须修复输入后重跑，不得跳过晋升**；只有 `results.json` 确实不存在才允许保守按 `not_run` 处理。同一 case 出现多条结果按最差状态合并（failed > skipped > not_run > passed），可疑输入写入 `regression-promotion.json` 的 `input_warnings`。

不得手工编辑晋升产物，也不得为未通过用例伪造 `passed` 以抬高晋升数。

## 失败处理

- 测试进程失败仍必须保存证据。
- 框架/脚本错误进入 healer。
- 真实 API 缺陷不得通过修改预期修复；保留失败并进入报告/比较。
- **HTTP 5xx、写后读失败、数据库只读验证失败**不得被 healing 或晋升隐藏。
- 网络或权限阻塞写入 run-context blocker。

只有结果文件可解析、用例总数与收集结果一致，且 `regression-promotion.json` 已生成，executor 阶段才能完成。
