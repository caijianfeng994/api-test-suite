# 响应 schema 以代码出参校准

成功路径的 `expected.schema` 必须以 **CodeWiki / proto / catalog 代码出参** 为准。YApi `res_body` 只作补充；与代码冲突时以代码为准，禁止用 YApi 或「默认 object」盖过代码。

## 适用

- `functional` / `list` / `side_effect` 的 HTTP 200 schema 断言
- catalog 已有 `responses[].schema.fields`，或接口清单「出参」列写了根类型（如 `[]ClassCourse`、`GetXxxRes`）
- YApi 与代码字段名、类型、根类型不一致（如 `VideoId` vs `videoId`，`serviceRights` 被写成 object，根数组被写成 object）

## 优先级

1. **代码出参**（catalog `responses[].schema.fields` + `response_root_type` / 出参列）
2. YApi `res_body`（仅当代码未声明该字段/类型时补齐）
3. 两者都无：`source=undeclared`，**禁止**默认 `type=object` 去否定实测数组；无证据时只做 HTTP/业务包校验，或 `type` 留空由生成器按「无契约」处理

## 强制规则

| 场景 | 正确做法 |
|---|---|
| 出参为 `[]Xxx` / `repeated Xxx` 且无外层 message | `schema.type=array`，元素字段进 `items` |
| 出参为 message / object | `schema.type=object`，顶层字段进 `properties` / `required_fields` |
| 字段类型为 `repeated` / `[]` | 该字段 `type=array`；**禁止**因 YApi 带了 `properties` 就断言成 object |
| 字段名大小写 | 用 proto/JSON 标签原文（`videoId`），禁止沿用 YApi 的 `VideoId` |
| YApi 与代码冲突 | `source=code_calibrated`，写入 `code_overrides`（字段或类型差异）；断言用代码侧 |
| 仅有代码、无 YApi | `source=code_wiki` |
| 仅有 YApi、代码无出参表 | 仍可用 `yapi_res_body`，但 discovery 须尽量补全 CodeWiki 出参 |

## 产物

生成器写入 `expected.schema`：

```json
{
  "type": "array",
  "required_fields": [],
  "source": "code_wiki",
  "items": {
    "type": "object",
    "properties": {
      "name": { "type": "string" },
      "publishers": { "type": "array" }
    }
  }
}
```

冲突校准示例：

```json
{
  "type": "object",
  "source": "code_calibrated",
  "required_fields": ["cardList"],
  "properties": {
    "cardList": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["videoId"],
        "properties": { "videoId": { "type": "string" } }
      }
    }
  },
  "code_overrides": [
    { "path": "cardList[].VideoId", "yapi": "VideoId", "code": "videoId", "reason": "field_name_case" }
  ]
}
```

## 执行与门禁

- Playwright `assertSchema`：`type=array` 时只按数组校验；不得因同节点还有 `properties` 就改判 object。
- `validate_run.py`：catalog 已有代码出参时，成功路径用例不得再写 `source=undeclared` 或未校准的 `yapi_res_body`（与代码根类型/字段名冲突时失败）。
- Healer：schema 失败先对照 CodeWiki 出参；代码与实测一致、仅 YApi/默认 object 不一致时，按本规则改断言并记 `code_calibrated`，不得先当产品缺陷。

## 与线上日志

访问日志通常只有 status / size，没有响应 JSON。格式一致性以 **代码出参 + 测试环境实测** 为准；日志只用来确认接口仍在被调用，不能代替 schema 校准。
