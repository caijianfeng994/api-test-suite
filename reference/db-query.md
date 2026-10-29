# 查库造数与写后断言

需要真实业务 ID 或写操作落库校验时，**先从 GitLab 服务仓读数据库连接**；仓内没有明文 postgres（仅 `${PG_*}` 占位、文件缺失或项目解析失败）时，**再用测试站 Nacos Open API 拉 `sensitive` / `DataId=dict`**。禁止猜测 host/密码，禁止 INSERT / UPDATE / DELETE / DDL，禁止 Playwright / 浏览器操作 Nacos。

查询实现复制自 `traffic-investment/scripts/verify_db.py` 的 `_run_sql`（`SQLDatabase.from_uri` + 只读事务）和 `db_agent.py`。**不复制** `ensure_traffic_partition.py`（含 `CREATE TABLE`）。

## 何时执行

- ops 成功路径缺 fixture（订单号、商品 ID、skuId 等）
- functional / side_effect 写操作需要落库断言
- refactor / migration 要比对两侧可观察的库表副作用

`run_mode=contract` 默认不连库。造数工具不支持、或库中没有匹配记录时，必须继续 `reference/fixture-fallback.md` 第 3 步（本套件 `web_cli/`），禁止在此停住并伪造 ID。

服务在 GitLab **且** Nacos 都找不到可连接配置时：已有 API/用户 fixture 不阻塞；`verify_db` 记 `not_applicable` + `service_db_unmapped`，并进入 `reference/fixture-fallback.md` 第 3 步后台取数。

## 标准命令

仓库根目录执行。`$SCRIPT_DIR=.claude/skills/api-test-suite/scripts`。

```bash
SCRIPT_DIR=".claude/skills/api-test-suite/scripts"

# 1. GitLab 明文优先；无明文则同一命令回退 Nacos Open API
python3 "$SCRIPT_DIR/fetch_gitlab_db_config.py" \
  --service-name "<service_name>" \
  --output "<run_dir>/db-config.json" \
  --redact

# 1b. 仅 Nacos（跳过 GitLab，或已确认仓内只有占位）
python3 "$SCRIPT_DIR/fetch_nacos_db_config.py" \
  --service-name "<service_name>" \
  --output "<run_dir>/db-config.json" \
  --redact

# 2. 列表 / 表结构 / 只读查询（方法同 traffic-investment verify_db._run_sql）
python3 "$SCRIPT_DIR/db_readonly.py" --config "<run_dir>/db-config.json" tables
python3 "$SCRIPT_DIR/db_readonly.py" --config "<run_dir>/db-config.json" schema --table good_list
python3 "$SCRIPT_DIR/db_readonly.py" --config "<run_dir>/db-config.json" query \
  --sql "SELECT id, name, status FROM good_list WHERE status IS NOT NULL LIMIT 5"

# 3. 可选：自然语言只读问答（复制自 traffic-investment/db_agent.py）
.venv/bin/python "$SCRIPT_DIR/db_agent.py" \
  --config "<run_dir>/db-config.json" \
  --question "列出 public 下与商品相关的表及行数"
```

GitLab 项目路径不是 `backend/<service>` 时加 `--gitlab-path`。GitLab 认证使用 `.env` 的 `GITLAB_PRIVATE_TOKEN`。

Nacos 回退认证使用 `.env` 的 `NACOS_SD_ACCOUNT`（缺省 `nacos`）与 **`NACOS_SD_PASSWD`（必填，禁止写进 Skill）**。可用 `NACOS_PASSWORD` / `--nacos-password` 覆盖。只连 `https://nacos.yc345.tv`，禁止 `nacos.ops.svc.cluster.local`。仅调试 GitLab、不要回退时加 `--no-nacos`。

## 配置来源优先级

1. GitLab `configs/local.yaml` → `development.yaml` → `test.yaml` → `default.yaml`。只采用 **host / db / password 均为明文** 的块。
2. 上述文件只有 `${PG_HOST}` 等占位、或缺文件 / 项目无法解析 → Nacos Open API：
   - `POST /nacos/v1/auth/login`
   - `GET /nacos/v1/console/namespaces`，取展示名 `sensitive` 的 namespaceId 作 tenant
   - `GET /nacos/v1/cs/configs`，`dataId=dict`，Group 首选 `configs/default.yaml` 的 `name`，再试 `-`↔`_` 与去连字符
   - 用 yaml 里的 `${PG_*}` 名称（或扁平 `PG_HOST` / `PG_PORT` / `PG_USER` / `PG_PASSWORD` / `PG_DB`）从 dict 取值
3. `${PG_HOST}` 已在当前环境导出时，仍可在 GitLab 解析阶段展开，不必打 Nacos。

配置解析必须覆盖两种形态：

- 独立字段：`PG_HOST`、`PG_PORT`、`PG_USER`、`PG_PASSWORD`、`PG_DB`；
- PostgreSQL URI：`PG_SOURCE`、`POSTGRES_SOURCE`、`DATABASE_URL`，支持 `postgres://` 与
  `postgresql://`，从 URI 提取 host、port、user、password、db、sslmode。

配置只暴露 `${PG_SOURCE}` 等变量名时，必须继续从 Nacos dict 取 URI 实值并解析，不能因为没有
`PG_HOST` 独立字段就记为 `service_db_unmapped`。连接前先输出脱敏后的 source、host、port、db；
不得在报告、日志或 fixture 中保留 URI、密码明文。

`db-config.json.source` 为 `gitlab` 或 `nacos`。accessToken 只留在脚本内存，不得写入 `run_dir`。

## 回填规则（强制）

允许用 DB / GET 详情给**请求 schema 已有参数**填真实值；**禁止把响应独有字段塞进请求**。

详情、编辑、删除、状态变更以及含业务 ID/UUID 的成功路径，必须查询能代表有效业务状态的真实记录；
不能只验证字段非空。查询前先从源码/CodeWiki/proto 确认表和字段映射，查询后将
`db:<service>.<table>.<column>`、`verified_at` 和脱敏证据写入 fixture。若无记录，优先调用已授权的
前置创建 API；仍没有则按 `reference/fixture-fallback.md` 用 `web_cli/` 从后台取数。数据库禁止写入。

| 允许 | 禁止 |
|------|------|
| 按 YApi / proto **请求**字段名赋值（同名才填） | 塞入响应独有字段（`status`、`paymentPlatform`、`skuIdList`、`createdAt` 等） |
| 创建/部分更新：可只填当前 case 已有 key | 编辑接口只用稀疏示例 body，指望服务读库补全 |
| `sources` 标明 `db:...` 或 `get:<path>` | 改名、把响应 schema 当请求 schema |

**编辑类接口（PUT/PATCH/「编辑」）** 另遵守 `reference/edit-get-then-put.md`：先 GET 详情，再按 **请求 schema 全量键** 组 body 后编辑。白名单是 proto/`req_body`，不是生成器占位里那几个 key。库里有列（如 `amount_range`）不等于编辑接口会读取。

写操作造数顺序：

1. 创建/校验：以 case 现有 `request` 键为白名单，DB/GET 同名赋值  
2. 编辑成功路径：先 GET 详情 → 按请求 schema 从 GET 填全量键 → 再 PUT/PATCH  
3. 白名单外的详情字段一律丢弃  

## 安全

- 会话级 `default_transaction_read_only=on`，SQL 再拦一遍写关键字。
- `db-config.json`、`fixtures.json` **禁止**上传到 `mstrategysddtestcase`。
- 报告与 `steps` 中的密码、连接串脱敏。
- 禁止手写未过门禁的 SQL 直连；必须走上述脚本。

## 产物

| 文件 | 阶段 |
|------|------|
| `<run_dir>/db-config.json` | ops 解析到库时 |
| `<run_dir>/db-tables.json` | 列出表之后 |
| `<run_dir>/execution/db/<case_id>.json` | 写后只读断言 |
| `<run_dir>/execution/web-cli/` | 库仍缺失、后台取数时 |
