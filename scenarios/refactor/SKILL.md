---
name: api-test-suite-refactor
description: Use when api-test-suite independently captures a pre-refactor API baseline or replays the exact baseline after refactoring for compatibility comparison.
---

# 场景插件：refactor

## 输入

必填 `service_name` 与 `run_mode=baseline|replay`。

- baseline：可选 `base_url`
- replay：必填 `baseline_run_dir`，可选 `target_base_url`

服务名必须满足 DNS label。Base URL 优先用户输入；未提供时仅允许从 CodeWiki 证据识别 `.7to12` / `.backsys`。仍不唯一则停止，禁止默认猜测。

## baseline

1. discovery：按 `core/discovery` 先连 YApi 再核 `service_name` 对应 GitLab proto HTTP 路径，并按 `reference/code-offline-filter.md` 排除已标下线实现。catalog 含 YApi∩proto **以及** 仅 proto 有且未下线（标明 `proto有`），不含 YApi 有而 proto 无，也不含 `code_offline`。
2. contract（建议）：基于目录与可选契约材料做就绪度；无契约材料时轻量分析即可。
3. ops → planner → generator → executor → healer（必要时）→ reporter。写操作落库校验按 `reference/db-query.md` 只读查库（GitLab 无明文则回退 Nacos）。
4. 固化 `<run_dir>/baseline-manifest.json`：

```json
{
  "schema_version": "1.0",
  "service_name": "discount",
  "base_url": "http://discount.backsys/",
  "catalog_sha256": "...",
  "fixtures_sha256": "...",
  "plan_sha256": "...",
  "cases_sha256": "...",
  "scripts_sha256": "...",
  "execution_results": "execution/results.json"
}
```

manifest 中每个文件必须真实存在并计算 SHA-256。

## replay

1. 校验 baseline manifest 和哈希；任一不一致停止。
2. 将 baseline 的 catalog、fixtures、plan、cases（`test-cases.json` + `test-cases.md`）、scripts 复制到新 `run_dir`，保留原件。
3. 只替换运行时 target Base URL，不重新生成用例和预期。
4. executor 执行 replay。
5. comparator 对 baseline 与 replay 的同 case ID 证据比较。
6. reporter 生成差异报告。

## 内核依赖

按阶段读取本套件 `core/discovery`、`core/contract`、`core/ops`、`core/planner`、`core/generator`、`core/executor`、`core/healer`、`core/comparator`、`core/reporter`、`core/gitlab-upload`。

baseline 不执行 comparator；replay 必须执行 comparator。`replay` 时 `contract=not_applicable`（复用 baseline 契约/用例，不重评）。
