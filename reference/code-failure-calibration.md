# 失败响应的代码核对

接口执行判失败后，必须用 CodeWiki/GitNexus 沿当前请求追到实际返回点，核对 **HTTP 状态码** 和 **响应格式**。两者都与代码实现一致时，这条用例执行结果记 `passed`，不再算失败。

## 触发

执行结果为 `failed`，且保存的响应是 HTTP 5xx，或断言失败原因是状态码 / 错误体与预期不符。

501 / `not implemented` 先走 `reference/unimplemented-traffic.md`，不重复用本规则把未实现接口改成通过。

## 核对

1. 先查 `get_wiki_freshness`，再用 `search_code_graph` / `get_symbol_context` 追到该请求的返回点。
2. 从代码写出它实际产生的 HTTP 状态，以及响应信封：`code`、`reason`、`message`。
   - 显式 `errors.New(400, reason, message)` → 400，正文用这两段文案。
   - 普通 `error` 被 Kratos 收成 500、`reason` 为空、`message` 为该错误原文 → 状态 500，`message` 必须对上原文。
   - panic 被 recovery 收成 `reason=UNKNOWN`、`message=unknown request error` → 状态 500，格式必须是这对字段。
3. 与 `execution/requests/<case_id>.json` 的 `response_status` 和响应 body 对比。
4. 状态码一致且信封字段、文案都一致：`status_match=true` 且 `format_match=true`，执行结果改为 `passed`。
5. 任一不一致：保持 `failed`。代码写的是 400、实测是 500 时，不得把预期改成 500。

没有源码符号和行号的摘要不能算核对完成。`router/router.go GinHandler` 这类路由摘要无效。`validate_run.py` 在 `contract` 模式下也不跳过本门禁。

## 闭合

核对完成后必须执行：

`python .claude/skills/api-test-suite/scripts/apply_code_return_calibration.py --run-dir "<run_dir>"`

脚本只接受 `file.go:行号 函数名` 形式的符号。状态码和响应格式都一致时，它把 `validation` / `error` 用例的 `expected.status`、`response_format`、`code_evidence` 写成代码返回值，并把执行结果改为 `passed`。不一致则保持 `failed`。

`validation` / `error` 在契约未声明 HTTP 状态时，生成器只能写 `assertion_policy=code_return_required` 与 `calibration.source=pending_code_return`。禁止写 `status_class=2xx_or_4xx` 或 `not_status` 包含 5xx。运行已完成，或该用例已有请求证据时，`validate_run` 会拒绝仍未闭合的断言。

## 证据

写入运行目录 `code-validation-evidence.json` 的 `cases[]`：

```json
{
  "case_id": "F23-C02",
  "actual_status": 500,
  "code_status": 500,
  "status_match": true,
  "format_match": true,
  "response_format": {
    "code": 500,
    "reason": "",
    "message": "strconv.Atoi: parsing \"\": invalid syntax"
  },
  "classification": "PRODUCT_DEFECT",
  "symbols": ["internal/data/course_shop.go:61-115 GetTreasureTagListByStage"],
  "reason": "空 stage 进入 strconv.Atoi，普通 error 被收成 500"
}
```

`status_match` 与 `format_match` 都为 true 时，`execution/results.json` 里该用例的 `status` 必须是 `passed`。`validate_run.py` 发现仍是 `failed` 会报 `code failure calibration`。

## 通过的范围

状态码和响应格式都与代码一致时，执行记 `passed`，并进入 `regression-cases.json`。HTTP 5xx 仍可标 `PRODUCT_DEFECT`，该标记只说明当前实现如此返回，不阻止晋升。
