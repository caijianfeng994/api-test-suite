# 判别字段与条件字段绑参（P0）

回填 `fixtures.json` / 生成用例 body 时，**禁止**把无关枚举占位值塞进条件字段。典型事故：`userScope=user_ids` 时仍写 `userLayer: "all"`，服务端按「有值须为 http(s) 链接」拒绝。

## 原则

1. **先读判别字段（discriminator）**，再决定哪些配套字段该有值、哪些必须空。
2. **条件字段「有值才校验」≠「默认必填」**：schema 里写了「有值时须为 URL」时，未启用该分支必须 **从请求 body 省略该键**（优先），不要写 `""` / `all` / `none` / `test` / 业务枚举字面量。空字符串可能被 `validate_run` 判为 placeholder。
3. **禁止跨字段抄值**：不得把 `userScope` / `goodsScope` / `saleSceneType` 的 `all` / `unlimited` 复制到 `*Layer` / `*Url` 类字段。
4. **URL 类字段**只接受：真实 `http://` / `https://` 链接（含文档要求的 query 键），或空；不得用中文、枚举名、ID。

## 优惠券模板（discount / CouponTemplate）

接口：`POST /discount/shadow/coupon-template`（及同资源写接口若带相同字段）。

### 用户范围（互斥，由 `userScope` 决定）

| `userScope` | 必须填 | 必须空 / 省略 |
|---|---|---|
| `all` | （无用户列表） | `userIds`、`userLayer` |
| `user_ids` | `userIds`（真实用户 ID 列表） | **省略 `userLayer`** |
| `user_layer` | `userLayer`（Shadow 分层管理复制的 **https 链接**，query 含非空 `type`） | 省略 `userIds`（或按契约清空） |

YApi/proto 已写：`userScope` = `all` / `user_ids` / `user_layer`；`userLayer` =「有值时须为 http 链接, query 含非空 type」。  
**没有**「`userIds` / `userLayer` / `prevOrderLayer` 三选一」——后两者不是同一组。

### 追前序订单（与用户范围无关）

| `chasePrevOrder` | `prevOrderLayer` |
|---|---|
| `false` / 未开启 | **省略该键** |
| `true` | 填 https 分层链接，query 含非空 `layerKey` |

禁止在 `chasePrevOrder=false` 时写 `prevOrderLayer: "all"`。

### 绑参自检（生成/回填后强制）

- [ ] `userScope=user_ids` ⇒ `userLayer` 为空字符串或不在 body
- [ ] `userScope=user_layer` ⇒ `userLayer` 以 `http://` 或 `https://` 开头，且含 `type=`
- [ ] `userScope=all` ⇒ 不传假用户 ID、不传假分层 URL
- [ ] `chasePrevOrder` 非 true ⇒ `prevOrderLayer` 为空
- [ ] 任一 `*Layer` 字段值若等于 `all` / `unlimited` / `none` / `test` ⇒ **视为非法，必须清空或改为真实 URL**

失败示例（禁止再现）：

```json
{
  "userScope": "user_ids",
  "userIds": ["<userId>"],
  "userLayer": "all",
  "prevOrderLayer": "all"
}
```

合法示例：

```json
{
  "userScope": "user_ids",
  "userIds": ["<userId>"],
  "chasePrevOrder": false
}
```

（body 中不出现 `userLayer` / `prevOrderLayer` 键。）

### 作废前置（create → void）

为 `PUT .../void` 用创建接口补 `id` 时：

- 创建 body **不要带 `skuId`**（或清空）：服务端「已绑定 SKU，不允许作废」
- `innerName` 每次唯一，避免唯一索引冲突
- `displayName` ≤ 10 字；`displayStartAt` / `collectStartAt` 不得早于当天
- 创建成功后把返回 `id` 写入 void 的 `endpoint_bindings`，`source=api:CreateCouponTemplate.id`

### 下游接口约束（create 补数时）

用创建接口给**其它写接口**供数时，创建参数必须对齐**下游**前提，而不是只对齐「创建本身能 200」：

| 下游意图 | 创建侧注意（discount 示例，可类推） |
|---|---|
| 领取 / claim | 领取人必须在范围内：优先 `userScope=all`；若 `user_ids` 则 claim 的 `userId` ∈ `userIds` |
| 绑 SKU / 购后发券 | 资源类型须满足下游（如 `issueType=purchase`）；绑 SKU 前模板须未绑定 |
| 核销券 | 券须未使用；`functional` 与 `side_effect` 各 claim 一张 |
| 作废模板 | 见上「作废前置」：无 `skuId` |

通用：读下游错误文案 / 契约说明里的「仅支持…」「须先…」，把约束写回创建 body 的判别字段，再写入 `fixtures.notes`。

## 推广规则（其它服务）

遇到下列命名模式时，按同一套「判别 → 条件字段」处理，并在 `fixtures.json` 的 `notes` 写明启用分支：

| 模式 | 做法 |
|---|---|
| `*Scope` + 配套 `*Ids` / `*Layer` / `*Groups` | 只填当前 scope 对应配套字段，其余清空 |
| `*Enabled` / `chase*` / `has*` 布尔 + 配套配置字段 | 布尔为 false 时配套字段清空 |
| 字段说明含「有值时须为 http」 | 非空必须是 URL；否则空 |

## 与「默认必填」的关系

「请求 schema 声明字段默认按必填绑真实值」**不适用于**当前判别分支下应关闭的条件字段。  
对这些字段：正确绑定是 **空值**（可追溯 `source=user_provided:omit_for_<discriminator>` 或等价说明），而不是编造占位。

缺真实分层 URL 且当前分支需要 URL 时：走 `reference/fixture-fallback.md` 第 3 步从 Shadow 后台复制链接；仍没有则该成功路径 `executable=false`，禁止用 `"all"` 凑数。
