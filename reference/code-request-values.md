# 请求值代码补齐

夹具三步（创建 / 只读库 / Shadow）拿不到非空真值时，**先读处理函数**，再决定能不能生成成功路径。禁止在这一步直接写 `required_binding_missing`。

首次生成如果只查绑定、库和后台，就会漏掉代码里已经写明的取值，或把处理函数根本不读的字段当成缺数。

代码查询顺序与 `reference/code-failure-calibration.md` 相同：先 `get_wiki_freshness`，再按接口方法最小范围查处理函数。证据写入 `fixtures.json` 的 `sources`，前缀 `code:<file>#<symbol>`。

## 1. 代码字面量

处理函数用该字段和字符串、数字或布尔字面量比较，或在为空时赋了一个字面量时，成功路径绑定这个字面量。

示例：`GetClientTreasureTagList` 只在 `req.FromPageName == "iosReviewPage"` 时保留「苹果商品」。成功路径绑定 `fromPageName=iosReviewPage`，来源 `code:internal/service/course_shop.go#GetClientTreasureTagList`。

空值分支如果只是「不过滤」，不要拿空字符串充当成功值。

## 2. 无校验的透传文案

字段是普通字符串，处理函数原样写入模型，没有枚举、长度或格式校验，并且注释说明了含义时，按注释写一条短的非占位文案。

示例：`DiscountDesc` 注释是「价格文案」，创建和更新都是 `req.DiscountDesc` 直接落库。库里只有占位串时，绑定 `限时优惠`，来源 `code:internal/service/PaidCourse.go#PaidCourseCreateShadow`。

禁止使用 `test`、`placeholder`、`regression-negative-unknown`。资源 ID、枚举、URL、时间不走本条，仍必须来自真实数据或代码字面量。

## 3. 处理函数未读取

请求 schema 声明了字段，但对应处理函数没有校验、没有分支、也没有把它拷进持久化对象时，该字段标 `code_unused`：

```json
"usage": {"package": "code_unused"},
"sources": {"package": "code:internal/service/videoblock.go#CreateVideoBlock"}
```

`sources` 必须能指出文件和符号。生成器省略该字段，不记 `required_binding_missing`，也不要补空串、空数组或猜测值。

示例：`CreateVideoBlock` / `UpdateVideoBlock` 只读取 `courseType`、`courseName`、`subId`、`state`、`creator`、`operator`、`bgImg`、`userLayering`、`hotAreaList`、`weight`。`package` 和 `courseTree` 不进入 `VideoBlock`，标 `code_unused`。

## 仍缺绑定

三种情况都不成立时，还不能写 `required_binding_missing`。按 `reference/binding-log-supplement.md` 查该 path 近 30 天线上访问日志，用请求里的非空值回填，来源 `log:<path>#<位置>.<字段>`。

只有日志确认该字段未出现、该 path 近 30 天没有调用，或字段本身是敏感信息不能抄明文时，才允许 `executable=false` 和 `required_binding_missing:<位置>.<字段>`，并保留夹具三步证据与 `execution/binding-log-supplement.json`。查询失败不得当成没有值。`validate_run` 对 `code_unused` 缺少 `code:` 来源报 `code unused`。
