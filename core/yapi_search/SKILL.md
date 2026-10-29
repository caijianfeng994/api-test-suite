---
name: yapi-search
description: Use when api-test-suite needs to ping YApi, read one or paired YApi interface details, list a YApi category or entire project, or search candidate interfaces before proto path filtering.
---

# YApi MCP 查询

## 使用场景

在接口测试中，discovery **必须先**确认 YApi 连通，再读取单个链接、分类、关键词或新旧链接对。YApi 拉到的接口还要经过 GitLab proto HTTP 路径过滤（见 `core/discovery`），本 Skill 只负责 YApi 侧。

优先使用当前会话已注册的 YApi MCP 工具；**未注册时必须**调用本套件脚本，不直接臆造接口字段。

先探测连通：

```bash
python .claude/skills/api-test-suite/scripts/fetch_yapi_interface.py --ping --output "<run_dir>/yapi-ping.json"
```

再拉接口：

```bash
python .claude/skills/api-test-suite/scripts/fetch_yapi_interface.py --url "<YApi 接口链接>" --output "<run_dir>/yapi-raw.json"
```

脚本连接 `yapi-common-mcp`。本机系统 DNS 无法解析 `yapi-common-mcp.7to12` 时，会向 `YAPI_CLUSTER_DNS` 查 A 记录并改用 IP。该变量必填，Skill 不内置集群 DNS。集群地址由 `scripts/internal_http.py` 强制绕过系统代理，`502 Bad Gateway` 通常说明请求被代理截走。不要改去调用 `locust-skills-ui` 或 `backend/tests/services/test_yapi_mcp.py` 作为主路径。

## 工具能力

当前 `yapi-common-mcp` 已验证暴露以下工具：

| 工具 | 用途 | 注意 |
|-|-|-|
| `get_interface_detail` | 根据 `interfaceID` 或具体接口 URL 获取接口详情 | URL 必须是 `/project/<projectId>/interface/api/<数字接口ID>` |
| `list_interfaces_by_cat` | 根据 `catID` 或 YApi 分类页链接获取分类元信息和该分类下全部接口详情 | 单分类最多 100 个接口 |
| `list_interfaces_by_project` | 根据 `projectId` 或 YApi **项目页**链接拉取该项目全部接口详情 | **无条数上限**；大项目响应可能较大 |
| `search_interface` | 根据 `rawQuery` 和 `tokens` 搜索候选接口 | 最多返回 5 条；部分环境会触发 elicitation 选择接口 |

**没有** `list_project` / `list_project_cats`（不能只列项目清单或分类菜单）。但 **已有** `list_interfaces_by_project`：用户给项目页、`projectId`、或「测某服务全部接口」且能解析到 YApi 项目时，**必须优先**用它一次拉全量。`search_interface` 最多约 5 条且可能卡住 elicitation，不得当作项目全量。

`cat_` 分类链接，例如 `/project/2619/interface/api/cat_20609`，不是具体接口 URL，也不是项目页。分类链接必须使用 `list_interfaces_by_cat`；项目页 `/project/<id>/interface/api`（无后续数字接口 ID、无 `cat_`）必须使用 `list_interfaces_by_project`。二者都不要传给 `get_interface_detail`。

## 项目全量接口（优先 list_interfaces_by_project）

用户要某个 YApi **项目下全部接口**（含「测 go-revenue 所有接口」、项目页 `https://yapi.yc345.tv/project/2619/interface/api`）时：

1. **优先**调用 `list_interfaces_by_project`（会话已注册则直接调；未注册则执行脚本）：

```bash
python .claude/skills/api-test-suite/scripts/fetch_yapi_interface.py \
  --project-url "https://yapi.yc345.tv/project/2619/interface/api" \
  --output "<run_dir>/yapi-raw.json"
```

或 `--project-id 2619`。MCP 参数：

```json
{
  "projectId": 2619
}
```

```json
{
  "projectURL": "https://yapi.yc345.tv/project/2619/interface/api"
}
```

`projectId` 与 `projectURL` 至少传一个；同时传时以 `projectId` 为准。仅传 `projectURL` 时从路径 `/project/<id>/` 解析项目 ID。

成功返回大致为 `{ "projectId", "yapiProjectUrl", "count", "interfaces": [ ...接口详情 ] }`。写入 `<run_dir>/yapi-raw.json`，记录 `interface_count`。随后仍须走 `core/discovery` 的 proto 过滤，**不得**把项目全量直接当 catalog。

2. **仅当** `list_interfaces_by_project` 未注册、返回 HTTP 404 / 鉴权失败、或 `ok=false` 时，才走分类旁路：
   - 用户直接给 `cat_` 链接 / `catID`（可多个）；或
   - 登录态调 YApi REST（如 `getCatMenu?project_id=<id>`、`/api/interface/list_menu`）；global token 对 `yapi.yc345.tv` 常返回「请登录」，不得假装已拿到菜单；或
   - 对可能的 `catId` 做探测扫描（`list_interfaces_by_cat` 有结果且 `project_id` 匹配则留下）。扫描须有界、记录范围；扫不到则询问用户给分类或项目链接，**禁止**编造接口。
   - 对每个分类调用 `list_interfaces_by_cat`（单分类最多 100；打满须在产物标明截断），合并去重（主键优先 `_id` / `interface_id`）。

不得用 `search_interface` 冒充全量。YApi 仍拉空时交给 `core/discovery` 走 proto 门禁（`source_label=proto有`），并在 warnings 写明原因。

## 标准流程

1. 先探测连通：会话已注册则冒烟调用；未注册则 `--ping`。连通失败则记录错误并交给 discovery 阻断，**不要**继续用 GitLab proto 全量编造 YApi 候选。

2. 从当前运行时工具目录确认以下工具已注册：

   - `get_interface_detail`
   - `list_interfaces_by_cat`
   - `list_interfaces_by_project`
   - `search_interface`

   已注册则至少执行一次只读调用作为冒烟验证。未注册时执行
   `python .claude/skills/api-test-suite/scripts/fetch_yapi_interface.py ...`，以脚本输出为准。
   不要把 `backend/tests/services/test_yapi_mcp.py` 当作本套件 discovery 入口。

3. 如果输入是具体接口链接，优先调用 `get_interface_detail`：

```json
{
  "interfaceURL": "https://yapi.yc345.tv/project/2619/interface/api/141510"
}
```

4. 如果只有接口 ID，调用：

```json
{
  "interfaceID": 141510
}
```

5. 如果只有关键词，调用 `search_interface`：

```json
{
  "rawQuery": "用户登录",
  "tokens": ["用户", "登录"]
}
```

6. 如果用户给的是 `cat_` 分类链接，调用 `list_interfaces_by_cat`：

```json
{
  "catURL": "https://yapi.yc345.tv/project/2619/interface/api/cat_20609"
}
```

7. 如果只有分类 ID，调用：

```json
{
  "catID": 20609
}
```

8. 如果用户给的是 **项目页**（`/project/<projectId>/interface/api`，无接口数字 ID、无 `cat_`），或只要 `projectId` / 「某服务全部接口」且已映射到该项目，调用 `list_interfaces_by_project`：

```json
{
  "projectURL": "https://yapi.yc345.tv/project/2619/interface/api"
}
```

```json
{
  "projectId": 2619
}
```

## YApi 项目接口包

当用户提供 YApi 项目页、`projectId`，或要求按服务拉取该 YApi 项目全部接口时：

1. 使用 `list_interfaces_by_project` 拉取项目下全部接口详情。
2. 将每个接口整理成清单条目（字段同分类包）。
3. 本工具无 100 条上限；仍须记录 `count`，并在后续 proto 过滤后写入 catalog。
4. 失败时不得假装已拉全量，按上文旁路或阻断 discovery。

输出结构：

```yaml
yapi_project:
  source_link: ""
  project_id: ""
  interfaces_count: 0
  interfaces:
    - interface_id: ""
      name: ""
      method: ""
      path: ""
      fetch_status: success | failed
      fetch_error: ""
```

## YApi 分类接口包

当用户提供 YApi 分类链接或 `catID` 时：

1. 使用 `list_interfaces_by_cat` 拉取分类元信息和该分类下全部接口详情。
2. 将每个接口整理成迁移接口清单条目。
3. 单分类最多 100 个接口；如果超过工具限制，报告里标记“分类接口未完整覆盖”。
4. 对每个接口保留 `interface_id`、`name`、`method`、`path`、请求参数、响应字段和 `fetch_status`。

输出结构：

```yaml
yapi_category:
  source_link: ""
  cat_id: ""
  category_name: ""
  interfaces_count: 0
  interfaces:
    - interface_id: ""
      name: ""
      method: ""
      path: ""
      request:
        path_params: []
        query_params: []
        headers: []
        body_fields: []
      response:
        success_fields: []
        error_fields: []
      fetch_status: success | failed
      fetch_error: ""
```

## 新旧 YApi 链接对

当用户同时提供旧 YApi 和新 YApi 链接时：

1. 分别读取旧接口和新接口详情。
2. 将旧接口标记为 `old_interface`，新接口标记为 `new_interface`。
3. 对比 `method`、`path`、请求字段、响应字段、错误字段。
4. 只把有文档依据或用户确认的差异标为“预期差异”；其他差异进入迁移测试风险点。

输出结构：

```yaml
yapi_pair:
  old_interface:
    source_link: ""
    interface_id: ""
    name: ""
    method: ""
    path: ""
  new_interface:
    source_link: ""
    interface_id: ""
    name: ""
    method: ""
    path: ""
  diff:
    method_changed: false
    path_changed: false
    request_field_changes: []
    response_field_changes: []
    error_contract_changes: []
  fetch_status: success | partial | failed
  fetch_error: ""
```

## 详情提取输出

读取到接口详情后，整理为迁移测试可消费的结构：

```yaml
yapi_interface:
  source_link: ""
  interface_id: ""
  name: ""
  method: ""
  path: ""
  request:
    path_params: []
    query_params: []
    headers: []
    body_fields: []
  response:
    success_fields: []
    error_fields: []
  migration_notes:
    - "用于补齐源服务/目标服务接口清单和字段契约"
  fetch_status: success | failed
  fetch_error: ""
```

## 异常处理

- MCP 连接失败：输出连接错误；**api-test-suite discovery 必须阻断**，不得用 proto 清单代替 YApi 候选。
- `get_interface_detail` 提示无法提取接口 ID：检查是否误传了 `cat_` 分类链接或项目页（缺数字接口 ID）。
- `list_interfaces_by_project` 失败：记录原始错误；再走分类旁路或询问项目链接。禁止改用 `search_interface` 冒充全量。
- `search_interface` 提示 elicitation 不支持、或卡住 elicitation：记录失败原因；**不要**用搜索结果当项目全量。请改用 `list_interfaces_by_project`，或提供接口 ID、分类链接。
- YApi 鉴权失败：记录 `fetch_status=failed` 和原始错误，不猜测接口字段。
- YApi 接口的 path 在对应服务 proto 中不存在：由 `filter_yapi_by_proto.py` 标记 `not_in_proto`，不编写、不维护该接口 case。
- proto 有而 YApi 无：仍维护 case，catalog / 用例标明 `source_label=proto有`。

## 迁移测试使用方式

将单个 YApi、新旧 YApi 对或 YApi 分类结果合并到 `service-migration-api-test` 的迁移接口清单中：

```markdown
| 编号 | 方法 | 旧路径 | 新路径 | 能力 | 测试类型 | 副作用 |
|-|-|-|-|-|-|-|
| M1 | POST | <旧 YApi path> | <新 YApi path> | 来自新旧 YApi 接口名称 | 功能 + 契约 + 字段差异 | 按接口详情补充 |
```
