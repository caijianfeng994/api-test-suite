# C 端网关鉴权：Shadow → 用户 Authorization

与 Header `uid`（业务身份）不同：走 `7to12-*.yangcong345.com/backend` 等**公网网关**的接口通常还要校验登录态 JWT。本套件实现来自内嵌 `web_cli/tokenUtil.py`（自仓库根 `.claude/skills/web_cli/tokenUtil.py` 同步）。

## 两种身份头

| 头 | 用途 | 典型入口 |
|---|---|---|
| `uid` | 服务内读业务用户 ID | 集群直连 `http://<service>.7to12/`（如 `revenue-strategy` client） |
| `Authorization` | 网关登录态（Bearer JWT） | `https://7to12-test.yangcong345.com/backend/...`（如 `course-mall` 发现页） |

只带 `uid`、不带 `Authorization` 打网关 → 常见 **401**。只带 Shadow 账号、不用目标 `userId` 换票 → **禁止**（不得用运营账号冒充 C 端用户）。

## 换票流程（强制）

```text
1. POST Shadow /api/auth/inner/test/login
   body: { email, password } → shadowToken
2. GET  Shadow /api/user/usercore/shadow/token/{userId}?role=student
   Header: shadowauthorization=<shadowToken>
   → JSON.authorization（C 端用户 token，常含 Bearer 前缀）
3. 业务请求：
   Authorization: <上一步 authorization>
   uid: <同一 userId>
   client-version: 按用例/夹具（如 7.78.0）
   Referer: 与网关同源页面（如 https://7to12-test.yangcong345.com/）
   Accept: application/json
```

网关偶发对缺 `Referer` 的请求返回 403「网络异常,请检查网络」；成功路径夹具应带上与环境匹配的 `Referer`。

环境与 Shadow host：

| env | Shadow host | 业务网关示例 |
|---|---|---|
| `test` | `shadow-test.yangcong345.com` | `https://7to12-test.yangcong345.com/backend` |
| `stage` | `shadow-stage.yangcong345.com` | `https://7to12-stage.yangcong345.com/backend` |
| `online` | `shadow.yangcong345.com` | `https://7to12.yangcong345.com/backend` |

凭证只从环境变量读取：`SHADOW_EMAIL` / `SHADOW_PASSWORD`。也可直接设 `SHADOW_AUTHORIZATION`（已是 shadowToken）跳过登录。Skill 不内置 Shadow 账号或密码。

## 本套件调用方式

### Python（与 web_cli 相同）

```python
from pathlib import Path
import importlib.util

module_path = Path(".claude/skills/api-test-suite/web_cli/tokenUtil.py").resolve()
spec = importlib.util.spec_from_file_location("tokenUtil", module_path)
token_util = importlib.util.module_from_spec(spec)
spec.loader.exec_module(token_util)

# C 端用户 JWT（默认 env=online；测试环境务必 env="test"）
authorization = token_util.getAppToken(user_id, env="test", role="student")
```

依赖：`requests`（与根目录 `web_cli/tokenUtil.py` 一致）。

### CLI（写入 fixtures / 本地调试）

```bash
python3 .claude/skills/api-test-suite/scripts/fetch_c_end_auth.py \
  --user-id "<userId>" \
  --env test \
  --output "<run_dir>/execution/c-end-auth.json"
```

输出 JSON 含 `userId`、`env`、以及**脱敏后的**头形状说明；完整 `Authorization` 只写入本机环境变量或执行期内存，**禁止**写入将上传 GitLab 的 `test-cases.json` / `regression-cases.md`。

推荐执行期注入：

```bash
export API_TEST_AUTHORIZATION="$(python3 .claude/skills/api-test-suite/scripts/fetch_c_end_auth.py --user-id "$UID" --env test --print-token)"
# 用例 headers 使用 ${API_TEST_AUTHORIZATION} 占位；敏感键见 regression_promotion
```

## ops / generator 规则

1. Base URL 为 `7to12-*.yangcong345.com/backend`（或等价网关）且接口非纯内网服务直连时：成功路径必须绑定 `Authorization` + `uid`（`uid` 与换票 `userId` 一致）。
2. `sources`：`api:tokenUtil.getAppToken` 或 `env:API_TEST_AUTHORIZATION`；`uid` 仍可 `shared.uid` / `db:...`。
3. `category=auth` 负向：去掉 `Authorization`（和/或 `uid`，按契约）；不得用错误用户的 JWT 冒充「无鉴权」。
4. 禁止把完整 JWT、Shadow 密码、Cookie 写入 GitLab 用例文档；报告只写 `userId` / env / 是否已换票。
5. 集群 `.7to12` 直连且实测仅需 `uid` 时，**不要**强行加网关 JWT（避免假失败）；以契约 + 实测门禁为准。

## 相关文件

- `web_cli/tokenUtil.py`：`getAppToken` / `getShowToken` / `buildH5UrlWithAppToken`
- `web_cli/playwright-cli/SKILL.md`：H5 打开前拼 token
- `reference/fixture-fallback.md`：缺参三步；网关鉴权属于可执行夹具，不是后台菜单取数
- 根目录 `.claude/skills/web_cli/tokenUtil.py`：同源实现；套件内以本目录副本为准
