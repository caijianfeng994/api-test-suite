# 缺绑定后的线上日志补数

夹具三步和 `reference/code-request-values.md` 都收口不了的字段，**先查该 path 近 30 天线上访问日志**，用真实请求里的非空值回填。禁止在这一步直接写 `required_binding_missing`。

代码字面量、无校验透传、`code_unused` 仍然优先。日志只补这三种都不成立、夹具也没有真值的字段。

## 查询

1. 用 CodeWiki `get_wiki_runtime_evidence` 取该服务线上 topicId。topicId 只是入口，必须再查日志。
2. 用火山 TLS `search_logs_v2_tool` 按 **path** 检索，例如 `path:"/traffic-investment/click/wifi"`。同一 path 的多个缺字段共用这一次检索，再按字段拆证据。
3. 时间窗固定为 **近 30 天**，写入 `start` / `end`。禁止用 7 天或默认 15 分钟代替。
4. 查询失败、topic 缺失、超时、日志里没有请求 query/body，都不得当成「该字段线上没有值」。

从命中记录里读请求 query 或 body，只取仍缺的 schema 字段。响应字段、Header `Authorization`、Cookie 不得抄进请求。

## 判定

| 近 30 天结果 | `decision` | 处理 |
|---|---|---|
| 请求已记录，该字段有非空值，且不是敏感字段 | `bound` | 回填 `endpoint_bindings`，`sources` 写 `log:<path>#<位置>.<字段>`，重新生成成功路径并执行。不得再留 `required_binding_missing` |
| 请求已记录，该字段始终缺失或为空 | `absent` | 成功路径按线上请求省略该字段后执行，不得编造值。仍可保留 `required_binding_missing` |
| path 调用数 `== 0` | `no_traffic` | 没有样本可抄。按 `reference/zero-traffic-ignore.md` 忽略整条接口，不留在跳过或失败里 |
| 字段是手机号、证件号、token、cookie、password | `sensitive_withheld` | 不抄明文。允许保持缺绑定，或改走已有换票 / 环境变量。证据里不得写原值 |
| 查询失败，或日志没有请求 query/body | `unverified` | **不得**记 `required_binding_missing`，也不得把运行标成 `completed` |

`absent` 必须同时满足：`hit_count > 0`、`request_logged=true`、`field_present_count=0`。只看到 status / size、看不到请求内容时，用 `unverified`。

回填值必须是日志里出现过的原值，禁止改写成 `test`、`placeholder` 或猜测枚举。同一 path 有多条样本时，取该字段出现次数最多的非空值，并在证据里写 `sample_count`。

## 产物

`<run_dir>/execution/binding-log-supplement.json`：

```json
{
  "window_days": 30,
  "start": "2026-09-22T15:40:00+08:00",
  "end": "2026-10-22T15:40:00+08:00",
  "topic_id": "topic-id",
  "fields": [
    {
      "endpoint_id": "F4",
      "method": "GET",
      "path": "/traffic-investment/click/wifi",
      "field": "query.cid",
      "query": "path:\"/traffic-investment/click/wifi\"",
      "hit_count": 18,
      "request_logged": true,
      "field_present_count": 18,
      "sample_count": 18,
      "decision": "bound",
      "source": "log:/traffic-investment/click/wifi#query.cid"
    }
  ]
}
```

`decision` 只允许 `bound`、`absent`、`no_traffic`、`sensitive_withheld`、`unverified`。

`bound` 的值写在 `fixtures.json`，不写进本文件，避免把线上样本再复制一份。`sensitive_withheld` 禁止带 `value`。

`validate_run.py` 在仍有 `required_binding_missing` 却缺少本文件、时间窗不是 30 天、`decision=bound` 仍缺绑定，或 `unverified` 被当成缺绑定收口时失败。
