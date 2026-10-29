---
name: api-test-suite-gitlab-upload
description: Uploads api-test-suite cases and runnable Playwright scripts under GitLab api/<service-name>/. When cases already exist, merges/supplements into one complete case document instead of replacing. Use as the mandatory final stage after generator/reporter.
---

# GitLab 落库（api-test-suite）

对齐 `test-multica/core/gitlab-upload`：**本地 `exports/` 不是最终交付物。**
接口用例与可执行脚本必须写入远程 GitLab 的 **`api/<服务名>/`**，禁止仅以工作区路径结束。

## 固定目标

| 项 | 值 |
|-|-|
| 远程仓库 | `git@gitlab.yc345.tv:backsys-testgroup/mstrategysddtestcase.git` |
| Web 地址 | `https://gitlab.yc345.tv/backsys-testgroup/mstrategysddtestcase` |
| 项目路径 | `backsys-testgroup/mstrategysddtestcase` |
| 默认分支 | **`main`** |
| 仓库内根目录 | **`api/`**（见 [main/api](https://gitlab.yc345.tv/backsys-testgroup/mstrategysddtestcase/-/tree/main/api)） |

用户显式指定同项目 URL 时可覆盖 checkout 地址，**不得**换到其他 GitLab 项目。

## 服务目录规则（强制）

1. 服务名 = GitLab / 业务服务名，优先取：
   - CLI `--service-name`
   - `run-context.service_name`
   - `run-context.slug`
   - CodeWiki / 输入来源中的服务引用
   例：`good` → 目录 `api/good/`
2. **`api/<服务名>/` 已存在**：不得再套一层日期/run_id；在原目录内**合并更新**。
3. **不存在**：在 `api/` 下新建 `<服务名>/`，再写入产物。
4. 禁止再使用旧路径 `api-test-suite/<kind>/<slug>/<run_id>/`。

## 用例合并策略（强制）

目标：**每个服务只维护一份完整、可执行的接口用例文档。**

当远程已有 `test-cases.json` / `test-cases.md` 时：

1. 读取远程已有用例作为基底
2. 读取本次 run 新生成用例
3. 按主键合并：
   - 优先 `case.id`
   - 无 id 时用 `method + path + category + name`
4. 相同主键 → **用本次内容更新**（保持最新请求/断言）
5. 新主键 → **追加**到文档
6. 远程有、本次没有的用例 → **保留**（不删除）
7. 合并后重算 `case_count` / `category_counts`，并用 `scripts/render_test_cases_table.py` **重渲染** `test-cases.md`
8. 合并后的 JSON/MD 同步回本地 `run_dir`，保证本地与远程权威文档一致

可执行脚本：

- `playwright.config.ts`：有则覆盖为最新
- `tests/*.spec.ts`：**同名覆盖，远程独有文件保留**（保证历史脚本仍可跑）

禁止：整文件无脑覆盖导致旧接口用例丢失。

**代码已标下线**：本次 catalog / `filtered.reason=code_offline` 的接口不得作为 incoming 新增或更新完整库/回归集；远程若仍有历史文档按「远程有、本次没有」保留，但不得再为其补充 fixture、晋升或修脚本（见 `reference/code-offline-filter.md`）。

## 回归集保护合并（强制）

- `test-cases.json` 是完整库，继续按上述主键增量合并；回归集条数应接近本次 `passed` 数。普通
  `passed` 晋升，包含代码核对后状态码和响应格式一致的 5xx。
- `regression-cases.json` 是已验证成功集（含请求取值）。同 ID 只有 incoming 同时满足
  `regression_ready=true`、`execution_status=passed`、`regression_blockers=[]` 时才允许替换远程版本。
- incoming 为 failed / skipped / not_run、未标 ready、仍有 blocker，或本次缺少该 ID 时，均不得降级或删除远程成功版本。
- `mergeStats.protected_remote` 统计被 incoming 非成功版本保护下来的远程条目；
  `mergeStats.promoted` 统计本次成功进入回归集的条目。
- 缺 `regression_ready=true` / `execution_status=passed` 的远程老格式条目不得视作成功回归；
  从回归集隔离到完整库/backlog，标记 `legacy_unverified_remote`，统计 `legacy_quarantined`，
  提示重新执行晋升，且不得阻断其它新验证用例上传。
- 合并 `regression-fixtures.json` 时必须保留远程回归用例引用的数据。远程与 incoming fixture
  做并集合并；受保护远程 case 引用的同键 fixture 由远程版本胜出，其余同键 incoming 更新，远程独有键保留。
- 所有未被 incoming 成功版本实际替换的远程成功 case（包括 incoming 省略该 ID）都属于受保护条目。
- 合并必须先写临时 staging；验证失败时 run_dir 与缓存仓库目标目录字节不变。Files API 仅能读取
  已通过 staging 校验的完整 `files` 清单和 staging 结果，不得回退到合并前 snapshot。
- `case_count=0` 的空回归集也必须稳定上传 JSON/MD、fixture 与可收集 0 条的独立工程。

## 触发时机

- `generate` / `full` / `contract` / `baseline` / `replay` 在本地用例齐备后 **必须** 执行
- 用户要求「上传 GitLab」「推远程」「落库」
- `plan` 尚未生成 `test-cases.md` → `not_applicable`

禁止：只输出 `exports/api-test-suite/...` 就声明完成。

## 仓库内路径

```text
api/<service_name>/
├── README.md                 # 索引（含最近一次合并统计）
├── test-cases.md             # 完整用例表（合并后的权威文档）
├── test-cases.json           # 完整用例机读（合并后的权威文档）
├── test-plan.md              # 本次有则更新
├── api-test-report.md        # 本次有则更新
├── code-validation-evidence.json # 本次有 CodeWiki/GitNexus 状态校准时更新
├── interface-catalog.md      # 本次有则更新
├── contract-analysis.md      # 本次有则更新
├── traceability-matrix.md    # 本次有则更新
├── regression-cases.json
├── regression-cases.md
├── regression-fixtures.json
├── regression-promotion.json
├── backlog-cases.md
├── package.json
├── package-lock.json
├── .env.example
├── playwright.config.ts      # 可执行配置
└── tests/
    └── api-cases.spec.ts
```

**禁止上传**：`.env`、Token、密钥、`yapi-raw.json`、`fixtures.json`、`db-config.json`、`db-tables.json`、`execution/` 证据目录（含 `execution/web-cli/`）、`node_modules/`、二进制与超大附件。

## 推荐命令

```bash
python .claude/skills/api-test-suite/scripts/upload_gitlab.py \
  --run-dir "<run_dir>" \
  --service-name "<gitlab服务名>"
```

省略 `--service-name` 时从 `run-context` 推导。
默认推送到 **`main`**。若 `main` 受保护无法直推，脚本自动降级到分支 `testcase/api-<service>`，路径仍为 `api/<service>/`。

认证：优先 SSH；失败则用 `GITLAB_PRIVATE_TOKEN` 走 HTTPS / Files API。

## 执行流程

1. **在任何 clone/fetch 前**调用 `validate_run.py` 的完整预检；仅忽略尚未产生
   `gitlab-upload.json` 的交付回执门禁。任一其它失败时立即停止，禁止 clone、commit、push 或 Files API。
2. clone/fetch `mstrategysddtestcase`，checkout `main`（或用户指定分支）。
3. 解析服务名；若 `api/<service>/` 已存在则进入合并模式，否则创建。
4. 增量合并完整库；保护合并回归成功集；合并 fixture；更新文档和独立 Playwright 工程。
5. 对合并后的 staging 目录再次执行 `case_count`、回归资格和全上传白名单敏感检查；任一失败不得 commit/push。
6. `git add api/<service>/` → commit → push（禁止 force push）。
7. 写 `<run_dir>/gitlab-upload.json`（含 `updatedInPlace`、`mergeStats`、`treeUrl`）。

上传白名单包含完整/回归/backlog 文档、脱敏后的 `code-validation-evidence.json`，以及
`package.json`、`package-lock.json`、`.env.example`、`playwright.config.ts`、
`tests/api-cases.spec.ts`。远程 README 必须说明：

```bash
npm ci && npx playwright test
```

## 输出契约

```yaml
gitlab_result:
  projectPath: "backsys-testgroup/mstrategysddtestcase"
  branch: "main"
  serviceName: "good"
  directoryPath: "api/good"
  updatedInPlace: true
  mergeStats:
    mode: "merge"          # merge | create
    remote_before: 748
    incoming: 20
    added: 5
    updated: 15
    unchanged_remote: 733
    protected_remote: 2
    promoted: 8
    total: 753
  files:
    - "test-cases.md"
    - "test-cases.json"
    - "playwright.config.ts"
    - "tests/api-cases.spec.ts"
  commitId: "<sha>"
  treeUrl: "https://gitlab.yc345.tv/backsys-testgroup/mstrategysddtestcase/-/tree/main/api/good"
  mock: false
  message: ""
```

交付回复必须含：完整用例数、已晋升数、backlog 数、`protected_remote`、`promoted`、服务目录、分支、commit、浏览链接。

## 失败处理

- 鉴权失败：说明检查 SSH / Token / 网络，不静默结束
- 分支冲突：先 fetch，禁止 force push
- 仅本地成功、远程失败：`stages.gitlab_upload=blocked`，总状态不得 `completed`
