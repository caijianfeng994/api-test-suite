# 编辑接口：先 GET 详情再编辑

适用于 `PUT` / `PATCH` / 名称含「编辑」「更新」的成功路径（如 `EditSkuGood`、`PUT /api/v3/skuGood`）。创建 `POST`、纯状态切换（上下架）不强制套本规则，除非契约写明 body 是完整资源快照。

## 为何必须 GET

许多服务把编辑 body 当成 **完整资源快照**，缺省字段当零值，**不会**用库里旧行补全。

教训（`good` `PUT /api/v3/skuGood`）：

- 库表 `sku_good.amount_range` 已有 `{"min":1888,"max":1888}`，编辑仍 **只读请求** `amountRange`
- 请求不传 → `GetAmountRange()` 为 `nil` → 转换函数解引用 panic → HTTP 500 `UNKNOWN / unknown request error`（`cause=<nil>`）
- 只回填 YApi 示例里的 8 个键（`id/name/amount/...`）不够；DB 有字段 ≠ 接口会取到

## 强制执行顺序

1. 用 fixture 的资源 ID 调 **同资源详情 GET**（如 `GET /api/v3/skuGood?id=`），200 才继续。
2. 以 **YApi / proto 请求 schema**（`req_body` / `EditXxxReq`）为白名单，用 GET 同名字段填值。
3. 再发编辑请求（PUT/PATCH）。写后仍走 `verify_read`。
4. 证据写 `execution/requests/<case_id>.pre-get.json` 与主请求证据；`sources` 标 `get:<detail-path>`。

用例上须有 `pre_read`（或等价字段）：

```json
{
  "pre_read": {
    "required": true,
    "method": "GET",
    "path": "/api/v3/skuGood",
    "id_from": "fixture",
    "purpose": "edit_snapshot"
  }
}
```

生成脚本时：同一 test 内先 GET 再 PUT，禁止跨 case 依赖详情。

## 字段映射

| 做 | 不做 |
|---|---|
| 按 **请求 schema 全量键** 从 GET 取值（含 YApi 示例漏掉、但 proto 有的 `amountRange`、条目列表、支付场景） | 只用生成器占位 body 的稀疏键 |
| 枚举保持 GET/库里的合法值（如 `iosPayScene=不支持`，不是 `null`） | 把展示文案乱映射成英文枚举 |
| 丢掉响应独有字段：`status`、`createdAt`、`skuIdList`、`paymentPlatform`、名称类派生字段等 | 把整个 GET JSON 原样当 PUT body |
| 鉴权按契约：如 `shadow-uid` 取请求头，不要指望 body 的 `updatedBy` | 假设「库里有 updatedBy 就会用」 |

「禁止改参数结构」对编辑接口的含义是：**不得发明请求 schema 里没有的字段**；**必须**把 schema 里有、YApi 示例却没写的键从 GET 补齐。禁止继续「只改已有 case 键、缺 `amountRange` 也硬 PUT」。

## 失败解读

| 现象 | 优先判断 |
|---|---|
| 500 UNKNOWN + `cause=<nil>` + 延迟极短 | 缺嵌套 message（如 `amountRange`）导致 panic，先查是否跳过 GET |
| 400 明确业务文案 | GET 快照已送达，按文案补枚举/条目/鉴权 |
| 400 `DetailUser` / 飞书用户无效 | Header `shadow-uid` 无效；换契约要求的操作者，不是改 body 结构 |

healer：编辑接口 5xx 且 body 明显小于详情/proto 请求字段时，分类 `FIXTURE`/`TEST_CODE`（请求构造），按本页补 `pre_read` 后重跑；不得把 500 直接记成产品缺陷。
