---
name: api-test-suite-reporter
description: Use when api-test-suite must produce a traceable Markdown report from plans, execution evidence, blockers, and optional compatibility comparison.
---

# 报告内核

## 输入

只读取当前 `run_dir` 中的上下文与真实产物，不根据计划数量推算实测结果。

`run_mode=contract`：本阶段通常为 `not_applicable`；契约结论以 `contract-analysis.md`、`traceability-matrix.md`、`test-cases.md` 为准，不必再写执行报告。若用户明确要求汇总，可写简短设计报告且标题不得含「执行」。

## 输出

写入 `<run_dir>/api-test-report.md`，固定章节：

1. 测试范围与环境
2. 输入来源、Base URL（脱敏）和运行模式
3. 契约就绪度（若有 `contract_readiness` / `contract-analysis.md`）
4. fixtures 与门禁状态（含是否连上 GitLab 解析的服务库、`verify_db` 只读结果摘要；密码脱敏）
5. **用例覆盖摘要**（按 `category` 计数：functional / validation / auth / error / side_effect / list；接口数 vs 用例总数）
6. 用例执行摘要（须区分：真缺陷 / 模板假失败 / 弱通过 / **结构化 400 通过**。HTTP 400 + JSON `code/message/reason` 记通过，不得写成产品缺陷。列表可选 ID 返回 200 空列表而用例期望 4xx 记为假失败）
7. 逐用例实测结果（含请求入参）
8. 失败、跳过、未执行与 blocker
9. refactor/migration 兼容性结论
10. 风险与改进建议
11. 产物索引（必须指向完整 `test-cases.md` / `test-cases.json`，供用户拿走最全用例）

### 冒烟专项（仅当存在 `category=smoke`）

若 `test-cases.json` 中存在任何 `category=smoke`（或名称含「【冒烟探测】」），报告**必须**增加独立章节：

> ## 冒烟探测说明（非正式完整覆盖）
> - 下列用例仅为连通性/可达性探测，**不能**代表功能、边界、鉴权与错误契约已测全。
> - 列出全部 smoke 用例 ID 与结果。
> - 完整功能用例见 `test-cases.md` 中非 smoke 类别。

默认完整跑批不应出现该章节；出现则视为覆盖降级，须在「风险与改进建议」中写明。

执行结束后，报告结论旁注明：**完整接口用例交付物** = `test-cases.json` + `test-cases.md`（不是执行摘要 alone）。

## 统计规则

- `passed`、`failed`、`skipped`、`ignored` 来自 `execution/results.json`
- `ignored` 统计两类，都不得并入失败数：`execution/unimplemented-traffic.json` 中 `decision=ignore` 的 501，单独成章「未实现且近 30 天无调用，已忽略」；`execution/path-traffic.json` 与 `traffic-ignore.json` 中近 30 天 path 调用数为 0 的接口，单独成章「近 30 天无请求，已忽略」。`decision=fail` 的 501，以及有流量的 HTTP 500，仍写在失败清单，并附近 30 天调用数
- `not_executed` 来自不可执行用例与 blockers
- `regression_ready` 数与 backlog 数只能来自 `regression-promotion.json`，不得用 `passed` 数代替
- 兼容率仅以 comparator 中可比较用例为分母
- 无执行证据时只能输出计划报告，不得称为执行报告
- 契约设计报告不得声称接口已实测
- **禁止**用「冒烟通过率」作为唯一结论标题；有完整类别时按类别分别汇总
- 存在 `code-validation-evidence.json` 时，单列“缺参代码状态校准”：逐条写 case ID、目标缺失字段、
  CodeWiki/GitNexus 符号、推断状态与实测状态。代码结论、精确预期与实测 500 一致时执行结果写
  `passed`，并写入已晋升回归集。

## 回归晋升与 backlog

存在 `regression-promotion.json` 时，报告必须增加独立章节，且三组数分别列出、不得合并：

- `passed`：执行通过数，来自 `execution/results.json`
- `regression_ready`：已晋升的回归用例数，来自 `regression-promotion.json` 的 `summary.promoted`
- backlog：未晋升数与 blocker 分布，来自 `summary.backlog` 与 `blocker_summary`

写法要求：

- 通常 `regression_ready`（`summary.promoted`）应等于 `passed`；差异须报为问题。代码核对一致的 5xx 计入 `passed` 并进入回归集。
- backlog 含 `failed` / `skipped` / `not_run`；
  晋升率分母是用例总数。出现 `regression_ready` 大于 `passed` 属数据异常，必须报为问题而不是抹平。
- 产物索引须同时指向 `regression-cases.json` / `regression-cases.md`（已验证回归集，含请求取值）与 `backlog-cases.md`（未通过用例）。
- 存在代码校准时，产物索引还必须指向脱敏后的 `code-validation-evidence.json`。
- `regression-promotion.json` 的 `input_warnings` 非空时必须在本章节列出（重复结果 ID、无法映射的计数、损坏证据等），这类输入问题会让统计偏保守，不能当作无事发生。
- 未运行晋升脚本时本章节写「未生成」，不得凭执行摘要声称回归集已建立。

## 严重级别

- P0：数据破坏、安全、核心流程不可用
- P1：状态码/业务语义不兼容、关键功能失败
- P2：非关键字段、边界或错误信息差异
- P3：性能波动、格式与建议项

报告生成后更新 reporter 阶段。随后 **必须** 执行 `core/gitlab-upload`；主入口校验本地产物 **且** `gitlab-upload.json.ok=true` 后才设置总状态 `completed`。
