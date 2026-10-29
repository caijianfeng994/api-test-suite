---
name: api-test-suite-healer
description: Use when api-test-suite execution has framework, fixture, request-construction, or assertion failures that require evidence-based repair and rerun.
---

# 失败修复内核

## 分类

先根据实际证据分类：

- `TEST_CODE`：语法、导入、Playwright API 使用错误
- `CONFIG`：Base URL、环境变量、路径、超时配置
- `FIXTURE`：数据不存在、已消费、权限不符
- `CONTRACT_CHANGE`：文档与实际响应确有变化
- `PRODUCT_DEFECT`：服务返回错误或兼容性问题
- `ENVIRONMENT`：网络、DNS、服务不可用

## 可修复范围

自动修复：`TEST_CODE`、明确的 `CONFIG`、可重新准备的 `FIXTURE`。

`FIXTURE` 缺真实业务数据时，按 `reference/fixture-fallback.md` 补齐后再重跑：造数工具 → 服务库只读 → 本套件 `web_cli/` 后台取数 → 处理函数代码补齐 → 近 30 天线上日志补请求值（`reference/binding-log-supplement.md`）。禁止因工具不支持就把失败改成 skip。

删除/更新因主键类型或「记录不存在」失败，且同 path 有创建或获取接口时：先 POST 造独立数据，再 GET 取出主键后重跑。不得先用库抽样列替换 `id`。创建和获取都失败后才允许查 `db:<service>.<table>.<column>`。

业务码提示「已作废 / 已使用 / 已绑定 / 已处理 / 请勿重复领取」等**状态类**失败：按同文档「创建补数给下游写接口」换**独立新种子**（`functional` 与 `side_effect` 分开），并遵守 `reference/fixture-discriminator-fields.md` 下游约束；禁止把成功断言改宽来吞脏数据。

负向缺 path 因「路径参数未提供」或 validate 报 `path_params` placeholder：按 `reference/regression-case-semantics.md` 用去掉 `{param}` 的 path + 空 `path_params`，勿填空字符串。

结构化 4xx 带 `errcode`/`errmsg` 却被判失败：确认执行工程已按 `generate_playwright_project.py` 识别该信封；缺则重生 execution 工程，勿改业务预期凑过。

不可“修绿”：`PRODUCT_DEFECT`、无证据的 `CONTRACT_CHANGE`。这些保留失败进入报告。

若失败 path 在 `yapi-proto-filter.json` / catalog `filtered` 中 `reason=code_offline`（实现已标下线，见 `reference/code-offline-filter.md`）：**禁止**补 fixture、改断言或继续维护该接口；从后续计划/用例中移除并记入「已过滤不维护」，不得为修绿而保留。

mux / 路由 **404** 且 discovery 对该 path 曾为 `unknown` 或主路径「证据缺口」：先按 `code-offline-filter.md` 兜底（尤其同文件兄弟下线注释）回查；确认 `code_offline` 后同上移除，勿先当环境/fixture 修绿。

HTTP **501** 或响应含 `not implemented` / `Unimplemented`，且接口在 YApi 与代码中都存在：先按 `reference/unimplemented-traffic.md` 查近 30 天线上访问日志。有调用则保留 `failed`；确认 0 次调用则整条接口改为 `ignored`，写入 `execution/unimplemented-traffic.json`。这不是修绿，禁止在未查日志时忽略。

HTTP 5xx（含 panic、空指针）在保留失败前，按 `reference/zero-traffic-ignore.md` 核对该 path 近 30 天调用数。调用数为 0 则整条接口忽略，即使参数已经绑定、代码仍是 `active`。调用数大于 0 的普通 500 保留失败。查询失败不得当成无调用。

schema 不一致（根类型 array/object、字段名大小写、`repeated` 被断言成 object）：先按 `reference/code-response-schema.md` 对照 CodeWiki/proto 出参。代码与实测一致、仅 YApi 或默认 object 不一致时，校准 `expected.schema` 为 `code_calibrated`/`code_wiki` 后重跑，不得先当产品缺陷。

## 循环

1. 读取失败用例请求、响应、stdout/stderr。
2. 写入 `<run_dir>/execution/healing-log.json`，记录分类、证据和拟修复。
3. 一次只修复一类根因。
4. 重新执行受影响用例。
5. 保存修复前后结果；仍失败则继续分类，最多 3 轮。

编辑接口 5xx（尤其 `UNKNOWN` / `unknown request error`）且请求 body 明显小于详情或 proto 请求字段时：按 `reference/edit-get-then-put.md` 补 GET→全量请求 schema 后重跑，分类 `FIXTURE` 或 `TEST_CODE`。库表有列但请求未传，**不是**「服务没读到库」的产品缺陷依据。

接口返回失败后，按 `reference/code-failure-calibration.md` 用代码核对状态码和响应格式。两者都与代码实现一致时，执行结果记 `passed` 并进入回归集。对不上才保留 `failed`。禁止没有代码符号和格式证据时把预期改成 500。

缺参用例另按 `reference/missing-required-code-status.md` 做单变量隔离。修复非目标字段后必须重跑；nil/panic 或普通 error 收成的 500，在状态码和 `message`/`reason` 都与代码一致时同样记 `passed`。

禁止：

- 删除或 skip 失败用例（例外：`unimplemented-traffic.md` 确认近 30 天 0 调用的 501/未实现接口，以及 `zero-traffic-ignore.md` 确认近 30 天 path 调用数为 0 的接口；状态必须是 `ignored` 或已从维护清单移除，且有日志证据）
- 把具体断言改成宽泛 truthy
- 将预期状态码改成“任意 2xx/4xx”
- 覆盖原始执行证据

修复后由 executor 重新生成结果，healer 自身不能宣称通过。
