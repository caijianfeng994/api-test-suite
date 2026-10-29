# 未实现接口的 30 天流量门禁

YApi 与代码都声明了接口，执行时返回 HTTP 501，或响应表明方法未实现（`not implemented` / `Unimplemented`）时，不得直接记产品失败，也不得直接忽略。必须先查该服务线上访问日志。

## 触发

同时满足：

- 接口在 YApi 与 proto/代码中都存在（catalog 保留，或执行请求已经打到该 path）
- 实测 HTTP 501，或响应/错误含 `not implemented`、`Unimplemented`

普通 HTTP 500、业务 4xx、缺数据、schema 不一致不走本规则。近 30 天 path 调用数为 0 的接口，包括已经绑上参数后仍返回 500 的接口，走 `reference/zero-traffic-ignore.md`。

## 日志

1. 用 CodeWiki `get_wiki_runtime_evidence` 取该服务 topicId。topicId 只是入口，必须再查日志。
2. 用火山 TLS `search_logs_v2_tool` 按 **path** 统计，例如 `path:"/api/courseCommendTag/constructLike"`。
3. 时间窗固定为 **近 30 天**，写入 `start` / `end`。禁止用 7 天或默认 15 分钟代替。
4. 查询失败、topic 缺失、超时时不得当成 0 次调用。

## 判定

| 近 30 天 path 调用数 | 处理 |
|---|---|
| `> 0` | 保留失败。接口仍被调用，未实现是缺陷 |
| `== 0` | **忽略整条接口**。该接口全部用例改为 `ignored`，不计入失败、不进入回归集、不晋升 |
| 查询失败 | 保留失败，并写明日志未核验 |

忽略只适用于本规则里的 501 / 未实现。不得用它跳过有流量的 501。有流量的普通 500 保持失败；无流量的普通 500 按 `reference/zero-traffic-ignore.md` 忽略整条接口。

## 产物

`<run_dir>/execution/unimplemented-traffic.json`：

```json
{
  "window_days": 30,
  "start": "2026-09-21T11:17:00+08:00",
  "end": "2026-10-21T11:17:00+08:00",
  "topic_id": "topic-id",
  "interfaces": [
    {
      "endpoint_id": "F4",
      "method": "PUT",
      "path": "/api/courseCommendTag/constructLike",
      "query": "path:\"/api/courseCommendTag/constructLike\"",
      "hit_count": 0,
      "decision": "ignore"
    }
  ]
}
```

`decision` 只允许 `ignore`（`hit_count=0`）、`fail`（`hit_count>0`）、`unverified`（查询失败且用例仍为 `failed`）。

`validate_run.py` 对缺少该证据、时间窗不是 30 天、或 0 调用仍标 `failed` 的结果报错。报告把 `ignore` 单独列入「未实现且近 30 天无调用，已忽略」，不写入失败清单。
