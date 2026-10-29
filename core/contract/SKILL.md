---
name: api-test-suite-contract
description: Use when api-test-suite must assess API contract readiness, clause status, coverage gaps, and case-to-clause traceability before planning or as a design-only run.
---

# 契约分析内核

把接口目录与可选契约材料转成可评审的就绪度结论、条款清单与追踪矩阵。默认**不执行接口、不造数、不改应用代码**。

## 何时运行

| run_mode | 要求 |
|-|-|
| `contract` | **必跑**；本阶段是主交付之一 |
| `plan` / `generate` / `full` / `baseline` | **建议跑**：有 OpenAPI、Proto、契约 Issue、错误码表或用户要求条款可追溯时必跑；仅有 YApi/catalog 时也应用目录字段做轻量就绪度分析 |
| `execute` / `replay` | `not_applicable`（沿用既有契约产物，不重评） |

## 输入

必须：

- `run-context.json`
- `interface-catalog.json`

尽量收集（缺什么记 `MISSING`，禁止静默补全）：

- 需求 / 父 Issue / PRD
- 契约 Issue 或契约文档
- OpenAPI / protobuf / schema
- 鉴权授权规则
- 请求/响应字段约束与示例
- 业务状态与枚举
- 错误码与状态行为
- 超时 / 重试 / 幂等 / 降级 / 兼容
- 上下游字段映射
- CodeWiki / GitLab proto 接口文档（条款标 `INFERRED`，来源 `gitnexus:<repo>`；不得当作已确认契约。与 YApi 冲突标 `CONFLICTING`）
- `yapi-proto-filter.json`：`not_in_proto` 不进入条款覆盖义务；`presence=proto_only` 必须覆盖并在分析文档标明 **proto有**

覆盖检查项见 `reference/coverage-checklist.md`。

## 固定输出

1. `<run_dir>/contract-clauses.json`：机器可读条款
2. `<run_dir>/contract-analysis.md`：契约完整性与接口清单分析
3. `<run_dir>/traceability-matrix.md`：条款 ↔ 用例追踪（planner/generator 之后可回填；本阶段先写条款侧骨架）

### contract-clauses.json

```json
{
  "schema_version": "1.0",
  "generated_at": "2026-08-12T14:35:00+08:00",
  "readiness": "PARTIAL",
  "endpoints": [
    {
      "endpoint_id": "F1",
      "method": "GET",
      "path": "/api/items",
      "clauses": [
        {
          "id": "F1-CL-001",
          "area": "request.query",
          "summary": "limit 必填且为 1..100",
          "status": "CONFIRMED",
          "source": "yapi:2619",
          "blocking": false
        }
      ]
    }
  ],
  "blockers": [],
  "open_questions": []
}
```

条款 `status` 仅允许：

- `CONFIRMED`：契约/工具/用户已确认
- `INFERRED`：代码或示例推断
- `MISSING`：覆盖必需但材料缺失
- `CONFLICTING`：多来源冲突

`readiness`：

- `READY`：无阻塞 `MISSING`/`CONFLICTING`
- `PARTIAL`：有缺口但不阻塞全部接口
- `BLOCKED`：存在阻塞缺口，成功路径不可静默设计

### contract-analysis.md 固定章节

1. `# 接口测试分析`
2. `## 契约完整性`：`READY` / `PARTIAL` / `BLOCKED` 与摘要
3. `## 接口清单`：身份、用途、请求/响应、安全、错误、副作用、降级、兼容
4. `## 条款状态表`：条款 ID、接口、区域、摘要、状态、来源、是否阻塞
5. `## 阻塞与待确认`：集中提问，禁止替用户选定解释
6. `## 覆盖缺口`：对照 `reference/coverage-checklist.md`

### traceability-matrix.md

本阶段至少写出条款列与「待规划」占位：

| 条款 ID | 接口 | 条款摘要 | 状态 | 用例 ID | 备注 |
|-|-|-|-|-|-|

`planner`/`generator` 完成后必须回填真实用例 ID；无对应用例时写 `UNCOVERED`。

## 流程

1. 按接口整理契约清单（身份、用途、请求/响应、安全、错误、副作用、降级、兼容）。
2. 逐条款标注 `CONFIRMED` / `INFERRED` / `MISSING` / `CONFLICTING`。
3. 阻塞时：只允许后续为**未受影响**接口继续设计；受影响成功路径必须 `executable=false` 并引用 blocker；禁止静默选解释。
4. 对照覆盖清单标出缺口。
5. 更新 `run-context.stages.contract` 与 `artifacts`；阻塞写入 `blockers`（`stage=contract`）。

## 与后续阶段关系

- `contract` 模式：下一步 `planner` → `generator`（仅文档用例）；`ops`/`executor`/`healer`/`comparator` 均为 `not_applicable`。
- 执行类模式：`contract` 完成后进入 `ops`；`planner` 必须读取本阶段产物做覆盖与可追溯约束。
- `BLOCKED` 不等于整次运行失败：可继续产出未阻塞接口的计划/用例，但总状态不得伪称契约完备。

## 门禁

- 不得把 `INFERRED` 写成已确认预期而不标注。
- 不得在材料冲突时任选一侧。
- 不得伪造条款来源。
- 三个固定产物均非空后才可 `stages.contract=completed`。
