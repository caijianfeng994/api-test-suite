import os
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

import requests


SHADOW_TEST_HOST = "shadow-test.yangcong345.com"
SHADOW_TEST_ORIGIN = f"https://{SHADOW_TEST_HOST}"
SHADOW_TEST_FRONTEND_URL = f"{SHADOW_TEST_ORIGIN}/shadow/#/"
SHADOW_TEST_LOGIN_URL = f"https://{SHADOW_TEST_HOST}/api/auth/inner/test/login"
SHADOW_TEST_ME_URL = f"https://{SHADOW_TEST_HOST}/api/auth/me"
SHADOW_TEST_LOCAL_STORAGE_KEYS = ["ShadowAuthorization", "authorization", "token"]
SHADOW_ENVIRONMENTS = {
    "test": {
        "host": "shadow-test.yangcong345.com",
        "login_url": "https://shadow-test.yangcong345.com/api/auth/inner/test/login",
        "me_url": "https://shadow-test.yangcong345.com/api/auth/me",
    },
    "stage": {
        "host": "shadow-stage.yangcong345.com",
        "login_url": "https://shadow-stage.yangcong345.com/api/auth/inner/test/login",
        "me_url": "https://shadow-stage.yangcong345.com/api/auth/me",
    },
    "online": {
        "host": "shadow.yangcong345.com",
        "login_url": "https://shadow.yangcong345.com/api/auth/inner/test/login",
        "me_url": "https://shadow.yangcong345.com/api/auth/me",
    },
}


def _require_env(name):
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"缺少环境变量 {name}。Skill 不内置账号或密码。")
    return value


def _resolve_shadow_env(env="test"):
    if env not in SHADOW_ENVIRONMENTS:
        raise ValueError(f"不支持的 Shadow 环境: {env}")
    config = dict(SHADOW_ENVIRONMENTS[env])
    config["email"] = _require_env("SHADOW_EMAIL")
    config["password"] = _require_env("SHADOW_PASSWORD")
    return config


def _build_shadow_cookie_payload(token, host=SHADOW_TEST_HOST):
    return [
        {
            "name": "onionsToken",
            "value": token,
            "domain": host,
            "path": "/",
            "secure": True,
            "httpOnly": False,
            "sameSite": "Lax",
        },
        {
            "name": "shadowToken",
            "value": token,
            "domain": host,
            "path": "/",
            "secure": True,
            "httpOnly": False,
            "sameSite": "Lax",
        },
    ]


def _build_shadow_local_storage_payload(token):
    return [{"name": key, "value": token} for key in SHADOW_TEST_LOCAL_STORAGE_KEYS]


def getShowLoginRecipe(env="test"):
    """
    获取可直接配置到 ai-web login_api 的 Shadow 测试环境免登配方。
    """
    config = _resolve_shadow_env(env)
    return {
        "login_url": config["login_url"],
        "method": "POST",
        "payload": {"email": config["email"], "password": config["password"]},
        "token_path": "shadowToken",
        "token_prefix": "",
        "me": {
            "url": config["me_url"],
            "send_headers": ["ShadowAuthorization", "Authorization"],
            "resp_header": "ShadowAuthorization",
        },
        "inject": {
            "cookies": [
                {"name": "onionsToken", "domain": config["host"], "path": "/"},
                {"name": "shadowToken", "domain": config["host"], "path": "/"},
            ],
            "local_storage": [{"key": key} for key in SHADOW_TEST_LOCAL_STORAGE_KEYS],
        },
    }


def getShowSiteConfig(env="test"):
    """
    获取 ai-web 站点管理接口可用的 Shadow 测试环境站点配置 payload。
    """
    config = _resolve_shadow_env(env)
    return {
        "name": f"Shadow {env}",
        "keywords": "shadow 洋葱中台 运营平台",
        "url": f"https://{config['host']}/shadow/#/",
        "authType": "login_api",
        "enabled": True,
        "authPayload": {"recipe": getShowLoginRecipe(env)},
    }


def _teacher_login(url, username_env, password_env):
    """入校登录。账号密码只从环境变量读取，不打印 token。"""
    body = {
        "username": _require_env(username_env),
        "password": _require_env(password_env),
        "type": "onion",
    }
    response = requests.post(url=url, json=body)
    response.raise_for_status()
    token = response.headers.get("Authorization")
    if not token:
        raise RuntimeError("入校登录响应缺少 Authorization")
    return token


def teacherToken():
    """入校测试环境登录，读取 ONION_TEACHER_TEST_USERNAME / ONION_TEACHER_TEST_PASSWORD。"""
    return _teacher_login(
        "https://api-xy-test.yangcong345.com/login",
        "ONION_TEACHER_TEST_USERNAME",
        "ONION_TEACHER_TEST_PASSWORD",
    )


def teacherTokenStage():
    """入校预发环境登录，读取 ONION_TEACHER_STAGE_USERNAME / ONION_TEACHER_STAGE_PASSWORD。"""
    return _teacher_login(
        "https://api-xy-stage.yangcong345.com/login",
        "ONION_TEACHER_STAGE_USERNAME",
        "ONION_TEACHER_STAGE_PASSWORD",
    )


def getShowToken(env="test"):
    """
    获取 Shadow 指定环境最终 cookie token。

    流程：
    1. 调用内部测试登录接口获取 shadowToken。
    2. 使用 shadowToken 作为 ShadowAuthorization 和 Authorization 请求 /api/auth/me。
    3. 从响应头 ShadowAuthorization 读取最终值，用于写入 onionsToken / shadowToken cookie。
    :return:
    """
    config = _resolve_shadow_env(env)
    login_response = requests.post(
        url=config["login_url"],
        json={"email": config["email"], "password": config["password"]},
        timeout=10,
    )
    login_response.raise_for_status()
    shadow_token = login_response.json().get("shadowToken")
    if not shadow_token:
        raise RuntimeError("内部测试登录响应缺少 shadowToken")

    me_response = requests.get(
        url=config["me_url"],
        headers={
            "ShadowAuthorization": shadow_token,
            "Authorization": shadow_token,
        },
        timeout=10,
    )
    me_response.raise_for_status()
    cookie_token = me_response.headers.get("ShadowAuthorization")
    if not cookie_token:
        raise RuntimeError("/api/auth/me 响应头缺少 ShadowAuthorization")
    return cookie_token


def getAppToken(user_id, env="online", role="student"):
    """
    apptoken：复刻 ApiTestWeb/qingke/buchajia.py get_token 的底层逻辑。

    不调用测试平台 /token，也不调用 api-*.yangcong345.com/login。
    流程：
    1. POST Shadow /api/auth/inner/test/login 获取 shadowToken
    2. GET /api/user/usercore/shadow/token/{userId}?role=student
       Header: shadowauthorization=<shadowToken>
    3. 返回响应 JSON 的 authorization（C 端用户 token）

    buchajia 写死线上 Shadow，故默认 env=online。
    """
    if not user_id:
        raise ValueError("user_id 不能为空")
    config = _resolve_shadow_env(env)
    login_response = requests.post(
        url=config["login_url"],
        json={"email": config["email"], "password": config["password"]},
        timeout=10,
    )
    login_response.raise_for_status()
    shadow_token = login_response.json().get("shadowToken")
    if not shadow_token:
        raise RuntimeError("内部测试登录响应缺少 shadowToken")

    token_url = (
        f"https://{config['host']}/api/user/usercore/shadow/token/{user_id}"
        f"?role={role}"
    )
    token_response = requests.get(
        url=token_url,
        headers={"shadowauthorization": shadow_token},
        timeout=10,
    )
    token_response.raise_for_status()
    app_token = token_response.json().get("authorization")
    if not app_token:
        raise RuntimeError(
            f"shadow token 响应缺少 authorization: {token_response.text}"
        )
    return app_token


H5_HOST_ENV = {
    "7to12-test.yangcong345.com": "test",
    "7to12-stage.yangcong345.com": "stage",
    "7to12.yangcong345.com": "online",
}


def inferH5Env(url, env=None):
    """
    从 C 端 H5 host 推断 Shadow/usercore 环境。
    显式传入 env 时优先使用传入值。
    """
    if env:
        if env not in SHADOW_ENVIRONMENTS:
            raise ValueError(f"不支持的 Shadow 环境: {env}")
        return env
    host = (urlparse(url).hostname or "").lower()
    if host in H5_HOST_ENV:
        return H5_HOST_ENV[host]
    if "7to12-test" in host:
        return "test"
    if "7to12-stage" in host:
        return "stage"
    if host.endswith("yangcong345.com"):
        return "online"
    return "test"


def buildH5UrlWithAppToken(url, user_id=None, env=None, role="student"):
    """
    为 C 端 H5 打开链接拼接 getAppToken() 结果。

    契约对齐真实 App WebView URL：用查询参数 token=Bearer <jwt>（空格编码为 %20）。
    user_id 缺省时从 URL 的 userId / user / sojumpparm 读取。
    env 缺省时按 7to12-test / 7to12-stage / 7to12 推断。
    """
    if not url:
        raise ValueError("H5 url 不能为空")
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    resolved_user = user_id or query.get("userId") or query.get("user") or query.get("sojumpparm")
    if not resolved_user:
        raise ValueError("H5 URL 缺少 userId，且未传入 user_id")
    resolved_env = inferH5Env(url, env)
    app_token = getAppToken(resolved_user, env=resolved_env, role=role)
    query["token"] = app_token
    query["userId"] = resolved_user
    query["user"] = resolved_user
    if not query.get("sojumpparm"):
        query["sojumpparm"] = resolved_user
    new_query = urlencode(query, quote_via=quote)
    return urlunparse(parsed._replace(query=new_query))


def getShowTokenCookies(env="test"):
    """
    获取可直接写入 Playwright browser context 的 Shadow 测试环境 cookie。

    注意：Shadow 前端免登还需要 localStorage，请优先使用 getShowTokenStorageState()。
    """
    config = _resolve_shadow_env(env)
    return _build_shadow_cookie_payload(getShowToken(env), config["host"])


def getShowTokenStorageState(env="test"):
    """
    获取完整 Playwright storageState，保持与 ai-web login_api 注入结果一致。
    """
    config = _resolve_shadow_env(env)
    token = getShowToken(env)
    return {
        "cookies": _build_shadow_cookie_payload(token, config["host"]),
        "origins": [
            {
                "origin": f"https://{config['host']}",
                "localStorage": _build_shadow_local_storage_payload(token),
            }
        ],
    }

def getShowTokenStage():
    """
    获取shadowToken
    :return:
    """
    return getShowToken("stage")


def getShowTokenOnline():
    """
    获取线上shadowToken
    :return:
    """
    return getShowToken("online")
