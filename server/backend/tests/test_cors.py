"""CORS：任何响应都不能带 Access-Control-Allow-Credentials: true。

注意判据不是"不回显来源"：Starlette 0.52.1 在请求带 cookie 时，不管开没开凭据都会把 * 换成
来访来源回显。浏览器肯不肯把带凭据请求的响应交给别的网站，看的是 Allow-Credentials 这个头。
"""
from __future__ import annotations

import unittest

import starlette
from fastapi.testclient import TestClient

from backend.app import main

EVIL = "https://evil.example"
TAURI = "http://tauri.localhost"


class CorsTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app, client=("192.0.2.10", 50000))

    def _assert_no_credentials(self, response):
        self.assertNotEqual(
            response.headers.get("access-control-allow-credentials", "").lower(), "true",
            f"{response.request.method} {response.request.url} 带出了 Allow-Credentials: true",
        )

    def test_pinned_starlette_version(self):
        # 行为是按这个版本核对的；版本变了要重新核对再改这里
        self.assertEqual(starlette.__version__, "0.52.1")

    def test_plain_request(self):
        self._assert_no_credentials(self.client.get("/api/notice", headers={"Origin": EVIL}))

    def test_request_with_cookie(self):
        r = self.client.get("/api/notice", headers={"Origin": EVIL, "Cookie": "AWSALB=x"})
        self._assert_no_credentials(r)

    def test_request_with_authorization(self):
        r = self.client.get("/api/notice", headers={"Origin": EVIL, "Authorization": "Basic YTpi"})
        self._assert_no_credentials(r)

    def test_preflight(self):
        r = self.client.options(
            "/api/feedback",
            headers={
                "Origin": EVIL,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,authorization",
            },
        )
        self._assert_no_credentials(r)

    def test_desktop_client_origin_can_read(self):
        r = self.client.get("/api/notice", headers={"Origin": TAURI})
        self.assertEqual(r.status_code, 200)
        self.assertIn(r.headers.get("access-control-allow-origin"), ("*", TAURI))


if __name__ == "__main__":
    unittest.main()
