# 统一 run-context 契约

本套件所有独立内核阶段共享并持续更新该文件。

## 固定结构

```json
{
  "schema_version": "1.0",
  "suite": "api-test-suite",
  "test_kind": "functional",
  "run_mode": "full",
  "run_id": "20260812-110300",
  "run_dir": "exports/api-test-suite/functional/yapi-2619-141510/20260812-110300",
  "slug": "yapi-2619-141510",
  "environment": "test",
  "write_operations": {
    "authorized": true,
    "authorized_by": "user",
    "scope": "test_environment",
    "authorized_at": "2026-09-21T17:57:00+08:00"
  },
  "input_sources": [],
  "yapi": {
    "connected": null,
    "filter": "yapi-proto-filter.json"
  },
  "base_urls": {
    "target": "",
    "source": ""
  },
  "contract_readiness": null,
  "stages": {
    "discovery": "pending",
    "contract": "pending",
    "ops": "pending",
    "planner": "pending",
    "generator": "pending",
    "executor": "pending",
    "comparator": "not_applicable",
    "reporter": "pending",
    "gitlab_upload": "pending"
  },
  "db": {
    "mapped": false,
    "source": "",
    "source_file": "",
    "gitlab_project": "",
    "nacos_group": ""
  },
  "artifacts": {},
  "status": "started",
  "blockers": [],
  "warnings": []
}
```

### 写接口授权（`environment` + `write_operations`）

测试环境写接口的权威解析在 `scripts/write_authorization.py` 的
`resolve_write_authorization(context, env=None)`；生成器、执行工程与 `validate_run.py`
必须复用该模块，禁止在文档或 TypeScript 中复制第二套冲突算法。

完整授权示例（与上表字段一致）：

```json
{
  "environment": "test",
  "write_operations": {
    "authorized": true,
    "authorized_by": "user",
    "scope": "test_environment",
    "authorized_at": "2026-09-21T17:57:00+08:00"
  }
}
```

字段语义：

| 字段 | 含义 |
|-|-|
| `environment` | 运行目标环境；仅 `test` 可使用 `scope=test_environment` |
| `write_operations.authorized` | 必须为布尔 `true` 才算显式授权 |
| `write_operations.authorized_by` | 授权来源审计（如 `user`） |
| `write_operations.scope` | 授权范围；非测试环境必须精确使用 `<environment>` 或 `<environment>_environment`，任意其它值拒绝 |
| `write_operations.authorized_at` | ISO8601 授权时间 |

解析规则（以脚本为准）：

1. 非 `test` 环境：`authorized=true` 且 `scope=test_environment` → 拒绝
   （`test_environment_authorization_cannot_be_inherited`）；须用该环境自己的显式 scope
   （`<environment>` 或 `<environment>_environment`）重新授权。任意非空 scope 不算授权。
2. `test` 环境且 `authorized=true` → 允许，`source=run_context`。
3. `ALLOW_WRITE_TESTS=true` 仅为 **legacy 兼容**（仅 `environment=test` 且 context 未显式授权时），
   `source=environment`；**新运行必须写入本文件的 `write_operations`**，context 显式授权优先于环境变量。
4. 运行期摘要与本文件不一致，或 legacy 环境授权被撤销时 **fail closed**：先抛错并停止发送请求，
   不得继续发写请求；必须更新本文件后重新生成 execution 工程才能恢复。

当前 `good` 验收口径：授权型跳过（「写接口未获得本次显式副作用授权」/
「破坏性写操作未授权」等）必须为 0；真实参数缺失仍允许不可执行/跳过，且必须带明确
`required_binding_missing:*` blocker，**禁止**为凑「0 skipped」伪造参数。

## 枚举

- 阶段状态：`pending`、`running`、`completed`、`blocked`、`not_applicable`
- 运行状态：`started`、`running`、`completed`、`blocked`
- `test_kind`：`functional`、`refactor`、`migration`
- `run_mode`：`full`、`plan`、`generate`、`execute`、`contract`、`baseline`、`replay`
- `contract_readiness`：`READY`、`PARTIAL`、`BLOCKED` 或 `null`（未跑契约阶段）
- `environment`：至少区分 `test` 与非测试值；非测试不得继承 `scope=test_environment`

## 更新规则

1. 阶段开始前写 `running`。
2. 产物存在且通过校验后才能写 `completed`，并在 `artifacts` 中记录相对路径。
3. 门禁失败写 `blocked`，同时追加结构化 `blockers`。
4. 后续阶段必须检查其前置阶段状态，不得只凭文件存在继续。
5. `refactor replay` 的 baseline 输入写入 `input_sources`，不得覆盖原 baseline。
6. `run_mode=contract` 时：`ops`、`executor`、`comparator`、`reporter`（执行报告）设为 `not_applicable`；`contract` 与 `planner`、`generator`、`gitlab_upload` 必须完成。
6b. 已有 `test-cases.md` 时 `gitlab_upload` 不得为 `pending`；远程失败写 `blocked`，总状态不得 `completed`。
6c. `run_mode=plan` 且无用例文件时 `gitlab_upload=not_applicable`。
7. `run_mode=execute|replay` 时：`contract` 可为 `not_applicable`（复用既有产物）。
8. 契约阶段完成后同步 `contract_readiness` 字段。
9. ops 解析到服务库时写 `db.mapped=true`、`db.source`（`gitlab` 或 `nacos`）、`db.source_file`、`db.gitlab_project`、`db.nacos_group`（不含密码 / accessToken），并在 `artifacts` 记录 `db-config.json`（该文件禁止上传远程用例仓）。
10. `run_mode=full|baseline|replay` 完成前必须生成 `regression-fixtures.json`、`regression-promotion.json`、`regression-cases.json`、`regression-cases.md`、`backlog-cases.md`，并通过 `validate_regression_artifacts`；`contract` 不要求这些执行后产物。
11. 回归门禁失败（晋升用例非 `passed`、无证据、含占位/敏感明文、fixture 来源不可解析，或待上传 Markdown/脚本/配置含敏感明文）时，总状态不得写 `completed`。敏感值仅允许环境变量引用。
12. 测试环境执行写接口前必须写入可审计的 `environment` + `write_operations`；`base_urls.target` 必须是已确认的目标，运行时 `API_BASE_URL` 必须与其一致。授权或目标变化后须重新生成 execution 工程，不得沿用旧固化摘要。

## blockers 结构

```json
{
  "stage": "contract",
  "code": "MISSING_ERROR_CONTRACT",
  "message": "缺少错误码与鉴权失败行为定义",
  "required_inputs": ["error_code_table", "auth_failure_behavior"],
  "endpoint_ids": ["F1"]
}
```
