import os
import unittest
import sys
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tokenUtil

FIXTURE_EMAIL = "shadow-user@example.com"
FIXTURE_PASSWORD = "shadow-test-password"
FIXTURE_USER_ID = "fixture-user-id"
FIXTURE_H5_USER_ID = "fixture-h5-user"


class ShadowCredentialMixin(unittest.TestCase):
    def setUp(self):
        self._env = patch.dict(
            os.environ,
            {
                "SHADOW_EMAIL": FIXTURE_EMAIL,
                "SHADOW_PASSWORD": FIXTURE_PASSWORD,
            },
            clear=False,
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()


class GetShowTokenTests(ShadowCredentialMixin):
    def test_get_show_token_exchanges_shadow_token_for_cookie_token(self):
        login_response = Mock()
        login_response.json.return_value = {"shadowToken": "shadow-token"}
        login_response.raise_for_status.return_value = None

        me_response = Mock()
        me_response.headers = {"ShadowAuthorization": "cookie-token"}
        me_response.raise_for_status.return_value = None

        with patch("tokenUtil.requests.post", return_value=login_response) as post, patch(
            "tokenUtil.requests.get", return_value=me_response
        ) as get:
            token = tokenUtil.getShowToken()

        self.assertEqual(token, "cookie-token")
        post.assert_called_once_with(
            url="https://shadow-test.yangcong345.com/api/auth/inner/test/login",
            json={"email": "shadow-user@example.com", "password": "shadow-test-password"},
            timeout=10,
        )
        get.assert_called_once_with(
            url="https://shadow-test.yangcong345.com/api/auth/me",
            headers={"ShadowAuthorization": "shadow-token", "Authorization": "shadow-token"},
            timeout=10,
        )

    def test_get_show_token_cookies_returns_browser_cookie_payloads(self):
        with patch("tokenUtil.getShowToken", return_value="cookie-token"):
            cookies = tokenUtil.getShowTokenCookies()

        self.assertEqual(
            cookies,
            [
                {
                    "name": "onionsToken",
                    "value": "cookie-token",
                    "domain": "shadow-test.yangcong345.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": False,
                    "sameSite": "Lax",
                },
                {
                    "name": "shadowToken",
                    "value": "cookie-token",
                    "domain": "shadow-test.yangcong345.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": False,
                    "sameSite": "Lax",
                },
            ],
        )

    def test_get_show_token_storage_state_contains_cookies_and_local_storage(self):
        with patch("tokenUtil.getShowToken", return_value="cookie-token"):
            storage_state = tokenUtil.getShowTokenStorageState()

        self.assertEqual(
            [cookie["name"] for cookie in storage_state["cookies"]],
            ["onionsToken", "shadowToken"],
        )
        self.assertEqual(storage_state["origins"][0]["origin"], "https://shadow-test.yangcong345.com")
        self.assertEqual(
            storage_state["origins"][0]["localStorage"],
            [
                {"name": "ShadowAuthorization", "value": "cookie-token"},
                {"name": "authorization", "value": "cookie-token"},
                {"name": "token", "value": "cookie-token"},
            ],
        )

    def test_get_show_login_recipe_matches_aiweb_login_api_contract(self):
        recipe = tokenUtil.getShowLoginRecipe()

        self.assertEqual(recipe["login_url"], "https://shadow-test.yangcong345.com/api/auth/inner/test/login")
        self.assertEqual(recipe["token_path"], "shadowToken")
        self.assertEqual(
            recipe["me"],
            {
                "url": "https://shadow-test.yangcong345.com/api/auth/me",
                "send_headers": ["ShadowAuthorization", "Authorization"],
                "resp_header": "ShadowAuthorization",
            },
        )
        self.assertEqual(
            recipe["inject"],
            {
                "cookies": [
                    {"name": "onionsToken", "domain": "shadow-test.yangcong345.com", "path": "/"},
                    {"name": "shadowToken", "domain": "shadow-test.yangcong345.com", "path": "/"},
                ],
                "local_storage": [
                    {"key": "ShadowAuthorization"},
                    {"key": "authorization"},
                    {"key": "token"},
                ],
            },
        )

    def test_get_show_site_config_returns_aiweb_site_payload(self):
        site_config = tokenUtil.getShowSiteConfig()

        self.assertEqual(site_config["url"], "https://shadow-test.yangcong345.com/shadow/#/")
        self.assertEqual(site_config["authType"], "login_api")
        self.assertEqual(site_config["authPayload"], {"recipe": tokenUtil.getShowLoginRecipe()})


class GetAppTokenTests(ShadowCredentialMixin):
    def test_get_app_token_uses_shadow_usercore_token_flow(self):
        login_response = Mock()
        login_response.json.return_value = {"shadowToken": "shadow-token"}
        login_response.raise_for_status.return_value = None

        token_response = Mock()
        token_response.json.return_value = {"authorization": "Bearer app-token"}
        token_response.raise_for_status.return_value = None

        with patch("tokenUtil.requests.post", return_value=login_response) as post, patch(
            "tokenUtil.requests.get", return_value=token_response
        ) as get:
            token = tokenUtil.getAppToken("fixture-user-id")

        self.assertEqual(token, "Bearer app-token")
        post.assert_called_once_with(
            url="https://shadow.yangcong345.com/api/auth/inner/test/login",
            json={
                "email": "shadow-user@example.com",
                "password": "shadow-test-password",
            },
            timeout=10,
        )
        get.assert_called_once_with(
            url=(
                "https://shadow.yangcong345.com/api/user/usercore/shadow/token/"
                "fixture-user-id?role=student"
            ),
            headers={"shadowauthorization": "shadow-token"},
            timeout=10,
        )


class BuildH5UrlWithAppTokenTests(unittest.TestCase):
    def test_build_h5_url_replaces_token_and_keeps_user(self):
        raw = (
            "https://7to12-test.yangcong345.com/revenue-resident-page/welfare-index"
            "?activityId=91b71172-899c-49e2-ac11-44664a3f87c2"
            "&userId=fixture-h5-user"
            "&token=Bearer%20old"
            "&user=fixture-h5-user"
        )
        with patch("tokenUtil.getAppToken", return_value="Bearer app-token") as get_app:
            opened = tokenUtil.buildH5UrlWithAppToken(raw)

        get_app.assert_called_once_with(
            "fixture-h5-user",
            env="test",
            role="student",
        )
        self.assertIn("token=Bearer%20app-token", opened)
        self.assertIn("userId=fixture-h5-user", opened)
        self.assertNotIn("token=Bearer%20old", opened)

    def test_build_h5_url_uses_explicit_user_and_env(self):
        raw = "https://7to12.yangcong345.com/revenue-resident-page/welfare-index"
        with patch("tokenUtil.getAppToken", return_value="Bearer app-token") as get_app:
            opened = tokenUtil.buildH5UrlWithAppToken(
                raw,
                user_id="fixture-h5-user",
                env="test",
            )

        get_app.assert_called_once_with(
            "fixture-h5-user",
            env="test",
            role="student",
        )
        self.assertIn("sojumpparm=fixture-h5-user", opened)


if __name__ == "__main__":
    unittest.main()
