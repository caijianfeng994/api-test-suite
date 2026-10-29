# 近 30 天无请求接口直接忽略

catalog 里每个保留接口都要按 **path** 统计近 30 天线上访问日志。调用数为 0 就整条忽略，不因为已经绑上参数、代码仍是 `active`，或执行打出 HTTP 500 就留在失败清单里。

缺绑定补数只覆盖「还有字段没绑上」的接口。参数已经从库或夹具绑上的接口，不会进入那份清单，但仍可能没有任何线上请求。`workImg`、`getAppletCode` 就是这种情况：绑定完整，实测 HTTP 500 空指针，近 30 天 `count=0`，不得继续记失败。

## 何时统计

代码下线过滤之后、把接口留在 catalog 并执行之前，对**每一个保留 path** 统计一次。禁止只统计缺绑定字段的接口，禁止只统计 HTTP 501。

执行之后如果仍有 HTTP 5xx（含 panic、空指针、业务码没收住的 500），汇总失败前再核对这个 path 的 30 天调用数。有调用才保留失败；0 次调用改为忽略。

## 日志

1. 用 CodeWiki `get_wiki_runtime_evidence` 取该服务 topicId。topicId 只是入口，必须再查日志。
2. 用火山 TLS `search_logs_v2_tool` 按 path 统计，例如 `path:"/traffic-investment/tiktok/workImg" | select count(*) as count`。
3. 时间窗固定为近 30 天，写入 `start` / `end`。禁止用 7 天或默认 15 分钟代替。
4. 查询失败、topic 缺失、超时，都不得写成 `hit_count=0`。

## 判定

| 近 30 天 path 调用数 | 处理 |
|---|---|
| `== 0` | **忽略整条接口**。从维护清单、用例、失败、跳过和回归集移除；`traffic-ignore.json` 记 `decision=ignore`。执行结果里若还留着该 path，状态只能是 `ignored` |
| `> 0` | 保留。HTTP 500 / panic 继续记失败，不得因为别的接口没流量就一起忽略 |
| 查询失败 | 保留原状态，`decision=unverified`。不得忽略 |

`app-usage-evidence.json` 的 `matched_requests=null` 不算已经核验。字段级 `binding-log-supplement.json` 的 `absent` 只说明这个字段没出现，不能代替 path 调用数。

## 产物

`<run_dir>/execution/path-traffic.json`：

```json
{
  "window_days": 30,
  "start": "2026-09-22T15:51:00+08:00",
  "end": "2026-10-22T15:51:00+08:00",
  "topic_id": "topic-id",
  "interfaces": [
    {
      "endpoint_id": "F34",
      "method": "GET",
      "path": "/traffic-investment/tiktok/workImg",
      "query": "path:\"/traffic-investment/tiktok/workImg\" | select count(*) as count",
      "hit_count": 0,
      "decision": "ignore"
    }
  ]
}
```

`decision` 只允许 `ignore`（`hit_count=0`）、`keep`（`hit_count>0`）、`unverified`（查询失败且接口仍保留）。

被忽略的接口同时写入 `<run_dir>/traffic-ignore.json`，`reason` 写明近 30 天 path 调用数为 0。上传时按 `endpoint_id` 从远程用例和回归集移除。

`validate_run.py` 对 HTTP 5xx 失败缺少该证据、时间窗不是 30 天、或 0 调用仍标 `failed` 的结果报错。HTTP 501 / `not implemented` 仍走 `reference/unimplemented-traffic.md`。有流量的 500 不得忽略。
