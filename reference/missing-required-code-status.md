# 缺少必填参数的代码级状态校准

适用于 `validation` / `error` 中的缺参用例：契约未明确错误 HTTP 状态，或实际返回与通用
`2xx_or_4xx` 预期冲突时，通过 CodeWiki/GitNexus 追踪当前代码，精确判断应观察 `200`、`400`
还是 `500`。

## 触发条件

- `negative_construction.strategy` 为 `omit_required_body`、`omit_required_query`、
  `empty_body_probe` 或同类缺参构造；
- 实测返回 5xx，或通用 `2xx_or_4xx` 无法表达当前代码行为；
- 用户明确要求从代码判断缺参状态。

普通接口发现阶段仍只用 CodeWiki `get_api_reference` 获取 proto/HTTP 清单。仅满足上述条件时，
才允许进入 GitNexus 源码图谱追踪。

## 取证顺序

1. 调用 `get_wiki_freshness(scope=<service>)`，记录 Wiki 的生成时间、`lastVerified` 与
   `confidence`；当前态仍须用源码图谱和运行证据核验。
2. 用 `get_api_reference(repo=<service>, keyword=<method path>)` 确认 RPC、请求类型与实现入口。
3. 仓库名未知时调用一次 `list_gitnexus_repos`，后续必须使用返回的规范 repo 名。
4. 用 `search_code_graph` 定位路由/transport、codec/validator、service、biz/usecase 与统一错误映射；
   再用 `get_symbol_context` 读取关键符号完整上下文。
5. 至少追踪到以下任一终点：
   - 显式 `errors.New(400, ...)`、validator/codec 拒绝 → `400`；
   - 缺失字段使用 protobuf 零值、默认值或空过滤条件且继续成功 → `200`；
   - nil 解引用、越界/type assertion panic，最终由 recovery 转为 5xx → 当前实现观察 `500`，
     分类 `PRODUCT_DEFECT`；
   - 无法闭合调用链 → 保持 `pending_code_return`，条款标 `MISSING`，不得写 `2xx_or_4xx` 或禁止 5xx。

代码结论属于 `INFERRED`，不能伪装成已确认业务契约。

## 单变量隔离

缺参用例一次只移除一个目标字段。其余字段必须：

- 类型合法；
- 满足 validator 的长度、格式、枚举等约束；
- 使用 fixture、数据库样本、契约 example/default/enum 或可追溯的确定性变换；
- 不得使用 `regression-negative-unknown` 填充 bool、enum、时间、日期、状态或受长度限制字段；资源 ID 负向用例只替换 ID，其余字段省略或使用类型合法的真实值；
- `fixture_trace` 与实际请求保持一致。

若响应先命中其他字段错误（例如 bool 解析失败、字符串长度违规），该次结果不能证明目标缺参状态。
修正非目标字段后必须重跑，直到响应由目标字段路径决定。

## 用例记录

精确结论写入 `expected`：

```json
{
  "status": [500],
  "status_class": "5xx",
  "code_evidence": {
    "artifact": "code-validation-evidence.json",
    "classification": "PRODUCT_DEFECT",
    "symbols": [
      "internal/service/example.go:20-45 Create",
      "internal/biz/example.go:10-30 Check"
    ]
  }
}
```

`code-validation-evidence.json` 至少包含：

- `repo`、`source=CodeWiki/GitNexus`、Wiki 新鲜度；
- case ID、method/path、目标缺失字段；
- 精确状态、分类、原因；
- 关键源码符号与行范围；
- 单变量隔离说明；
- 对应运行证据路径。

该文件禁止写真实 Token、Cookie、手机号、数据库连接或其他敏感值；请求校准只记录字段名与来源。

## 200 / 400 / 500 处理

- `200`：精确断言 200，并按响应契约补 schema/业务码；可正常晋升。
- `400`：精确断言 400；结构化业务拒绝按负向通过处理，可正常晋升。
- `500`：按 `reference/code-failure-calibration.md` 同时核对状态码和响应格式（`code` / `reason` / `message`）。
  两者都与代码一致时执行器记为 `passed`，并进入 `regression-cases.json`。`code_evidence.classification` 可标
  `PRODUCT_DEFECT`，该标记不阻止晋升。

## 运行冲突处理

- 代码推断 200、实测 400：先检查 codec/validator 与非目标字段；完成单变量隔离后再判断。
- 代码显式 400、实测 500：保留 500 产品缺陷，检查部署版本/Wiki 新鲜度，不得把预期改成任意 4xx/5xx。
- 代码推断 500、实测 400：以实测当前环境为执行结论，同时记录版本/索引冲突为 `CONFLICTING`。
- 任何修正都须保留旧请求/响应证据，并在 `execution/healing-log.json` 增加一轮记录。

## 完成门禁

1. `code-validation-evidence.json` 非空且无敏感值。
2. 目标用例定向重跑，保存请求、响应、HTTP 状态与耗时。
3. 状态码和响应格式都与代码一致的 5xx 已进入回归集，`regression_ready=true`。
4. reporter 单列代码校准结果，写明执行通过且已晋升。
5. GitLab 上传包含脱敏后的 `code-validation-evidence.json`。
