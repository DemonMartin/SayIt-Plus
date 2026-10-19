"""公开站点的静态页：中英两个首页、爬虫要读的几个文件，都得由后端直接给出。

线上有两条路径会服务 server/web：gateway（static-path.mjs，有自己的测试）和这里的 StaticFiles。
两边要一致，否则换个部署方式 /zh/ 就 404 了。
"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from backend.app import main


class PublicSiteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app, client=("192.0.2.10", 50000))

    def test_english_home(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('<html lang="en">', r.text)
        self.assertIn('hreflang="zh-CN" href="https://sayitapp.site/zh/"', r.text)

    def test_chinese_home_served_from_directory(self):
        r = self.client.get("/zh/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('<html lang="zh-CN">', r.text)
        self.assertIn('<link rel="canonical" href="https://sayitapp.site/zh/">', r.text)

    def test_chinese_home_without_slash_redirects(self):
        r = self.client.get("/zh", follow_redirects=False)
        self.assertIn(r.status_code, (301, 307, 308))
        self.assertTrue(r.headers["location"].endswith("/zh/"))

    def test_crawler_files(self):
        robots = self.client.get("/robots.txt")
        self.assertEqual(robots.status_code, 200)
        self.assertTrue(robots.headers["content-type"].startswith("text/plain"))
        self.assertIn("Sitemap: https://sayitapp.site/sitemap.xml", robots.text)
        self.assertIn("Disallow: /api/", robots.text)

        sitemap = self.client.get("/sitemap.xml")
        self.assertEqual(sitemap.status_code, 200)
        self.assertIn("<loc>https://sayitapp.site/zh/</loc>", sitemap.text)

        llms = self.client.get("/llms.txt")
        self.assertEqual(llms.status_code, 200)
        self.assertTrue(llms.text.startswith("# SayIt"))

    def test_pages_revalidate_but_assets_do_not(self):
        # Without this a browser keeps showing the previous build after a deploy.
        for path in ("/", "/zh/", "/index.html"):
            r = self.client.get(path)
            self.assertEqual(r.headers.get("cache-control"), "no-cache", path)
        css = self.client.get("/style.css")
        self.assertEqual(css.status_code, 200)
        self.assertNotIn("no-cache", css.headers.get("cache-control", ""))

    def test_web_demo_limit_matches_the_app(self):
        # The desktop app stops a take at MAX_RECORDING_SEC = 300 (client/src/services/recorder/types.ts)
        # and the site says "5 minutes"; the demo default must not drift from either.
        from backend.app.config import WebDemoConfig

        self.assertEqual(WebDemoConfig().max_duration_sec, 300)

    def test_api_routes_still_win_over_static(self):
        r = self.client.get("/api/public/config")
        self.assertEqual(r.status_code, 200)
        self.assertIn("web_demo", r.json())


if __name__ == "__main__":
    unittest.main()
