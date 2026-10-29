---
name: api-test-suite-comparator
description: Use when api-test-suite must compare baseline/replay or source/target API evidence while honoring explicit compatibility rules.
---

# 结果比对内核

## 输入

- 成对的请求/响应证据
- `test-cases.json`
- 显式差异白名单
- 场景：`refactor replay` 或 `migration`

## 归一化

比较前只应用明确规则：

- 忽略动态字段：时间戳、trace ID、请求 ID、明确声明的随机 ID
- 对 JSON object 键顺序不敏感
- 数组默认顺序敏感；只有计划声明集合语义时才排序
- 数值/字符串类型默认不等价
- 状态码、业务码、关键字段默认必须一致

不得自动忽略未知差异。

## 结论类型

- `compatible`：行为一致或差异在白名单
- `incompatible`：状态码、业务语义、结构、关键数据或副作用不一致
- `not_comparable`：一侧未执行、环境阻塞或证据缺失

## 输出

写入：

```text
<run_dir>/compare/comparison.json
<run_dir>/compare/comparison.md
```

每个用例包含：

- case ID
- 两侧实际请求
- 两侧状态码、耗时和归一化响应
- 差异路径
- 命中的白名单规则
- 兼容性结论与风险级别
- 若存在 `execution/db/`：两侧只读查库结果（表名、匹配键、行是否存在）；库不可比时 `not_comparable`，不得忽略已采集的落库差异

只有所有可执行用例都有结论，comparator 阶段才能完成。
