---
name: api-test-suite-migration
description: Use when api-test-suite independently dual-runs old and new service APIs, resolving routes via CodeWiki service names and/or YApi, and evaluates migration compatibility, side effects, and declared differences.
---

# 场景插件：migration

## 输入

必填：

- source Base URL（`run_mode=contract` 可暂缺）
- target Base URL（`run_mode=contract` 可暂缺）
- 迁移接口映射

映射可由以下方式生成，**不得按接口名称相似度自动配对**：

- 用户显式清单
- 新旧 YApi / 单 YApi + 路径规则 / 分类
- **`source_service_name` / `target_service_name`（或可解析的服务名）经 CodeWiki 拉两侧路由后再按用户规则或 path 对应**

传入任一侧服务名时，discovery **必须**对该侧先拉 YApi、再拉 CodeWiki/proto HTTP 路径，再按 `reference/code-offline-filter.md` 排除已标下线实现。catalog 保留该侧 YApi∩proto **以及** 仅 proto 有且未下线（`source_label=proto有`）。两侧都有服务名时分别匹配 Wiki 仓、分别拉 `get_api_reference`、分别过滤。YApi 有而对应侧 proto 无 path，或实现已标下线的接口不进入映射、不写 case。

可选：差异白名单、保留接口、写入副作用、鉴权和测试数据。

`run_mode=plan|generate|execute|full|contract`，默认 `full`。

## 接口映射

discovery 输出每项须包含：

```json
{
  "id": "M1",
  "method": "POST",
  "source_path": "/old/resource",
  "target_path": "/new/resource",
  "capability": "创建资源",
  "side_effects": [],
  "allowed_differences": []
}
```

路径或能力无法一一对应时标记 blocker，不得按名称相似度自动配对。

## 独立编排

1. discovery 归一新旧接口与映射（两侧各自 YApi/proto 对齐，含 proto有；冲突不静默选边）。
2. contract（建议）：标注两侧契约条款、允许差异与冲突；`CONFLICTING` 不得静默选边。
3. ops 准备两侧可复用且语义相同的 fixtures；需要落库比对时按 `reference/db-query.md` 分别解析两侧库配置（GitLab 明文优先，否则 Nacos）并只读查询。造数工具与库都缺数据时按 `reference/fixture-fallback.md` 用本套件 `web_cli/` 取后台数据。
4. planner 定义成对请求、兼容规则和副作用验证，并引用条款 ID。
5. generator 生成每个 case 的 source/target 成对调用，并回填追踪矩阵。
6. executor 运行；一侧失败不应阻止另一侧证据收集。
7. healer 只修测试基础设施问题。
8. comparator 输出逐字段兼容结论。
9. reporter 输出迁移验收报告。
10. gitlab-upload 将用例与可执行脚本推到 `mstrategysddtestcase` 的 `api/<服务名>/`（已存在则合并补充到已有完整用例文档）。

以上阶段分别读取本套件对应 `core/*/SKILL.md`。`run_mode=contract` 时停在 generator 文档产物，两侧执行与 comparator 为 `not_applicable`。

## 写操作

- 可使用不同但等价的隔离数据，必须记录映射。
- 支付、删除、消息等不可逆副作用需明确授权；否则标记未执行。
- 同时验证响应与可观察副作用，无法观察时在报告中列为覆盖缺口。

## 完成条件

每个可执行迁移用例都必须有 source、target 两侧证据和 comparator 结论；一侧环境阻塞时为 `not_comparable`，不得计为兼容。
