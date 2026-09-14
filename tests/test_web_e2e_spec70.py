import asyncio
import base64
import threading

from aiohttp import web
from playwright.sync_api import sync_playwright

from provider_broker.app import create_app
from provider_broker.settings import Settings


class LiveBroker:
    def __init__(self, database_path):
        self.ready = threading.Event()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.database_path = database_path

    def __enter__(self):
        self.thread.start()
        assert self.ready.wait(10)
        return self

    def _run(self):
        asyncio.set_event_loop(self.loop)
        app = create_app(Settings(self.database_path, "admin-secret", "session-secret", base64.b64encode(b"x" * 32).decode()))
        self.runner = web.AppRunner(app)
        self.loop.run_until_complete(self.runner.setup())
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        self.loop.run_until_complete(self.site.start())
        self.url = f"http://127.0.0.1:{self.site._server.sockets[0].getsockname()[1]}"
        self.ready.set()
        self.loop.run_forever()

    def __exit__(self, *_):
        asyncio.run_coroutine_threadsafe(self.runner.cleanup(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)


def test_analytics_and_routes_share_the_main_container_at_desktop_and_narrow_widths(tmp_path):
    with LiveBroker(tmp_path / "layout.sqlite3") as broker, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()

        def response(path):
            if path.startswith("/admin/v1/summary"):
                return {"routable_apis": 0, "last_successful_sync": None}
            if path.startswith("/admin/v1/providers"):
                return {"providers": []}
            if path.startswith("/admin/v1/stages"):
                return {"items": [], "window": "24h"}
            if path.startswith("/admin/v1/routing"):
                return {"race_parallel_cap": 3, "hedge_delay_ms": 0}
            if path.startswith("/admin/v1/models"):
                return {"items": []}
            if path.startswith("/admin/v1/pricing"):
                return {"items": []}
            if path.startswith("/admin/v1/quality"):
                return {"calls": 0, "failures": {}}
            if path.startswith("/admin/v1/calls"):
                return {"items": [], "next_cursor": None}
            if path.startswith("/admin/v1/routes"):
                return {"items": [], "next_cursor": None}
            if path.startswith("/admin/v1/data-health"):
                return {"in_progress": 0, "reconciled_unknown": 0, "legacy_records": 0}
            if path.startswith("/admin/v1/analytics"):
                return {"groups": []}
            raise AssertionError(path)

        page.route("**/admin/v1/**", lambda route: route.fulfill(
            status=200, content_type="application/json", body=__import__("json").dumps(response(route.request.url.removeprefix(broker.url)))
        ))
        page.goto(broker.url)
        page.locator("#analytics").wait_for()
        for width in (1440, 480):
            page.set_viewport_size({"width": width, "height": 900})
            geometry = page.locator("main > section:has(#quality-title), main > section:has(#analytics-title), main > section:has(#route-audit-title)").evaluate_all(
                "items => items.map(item => ({left: item.getBoundingClientRect().left, right: item.getBoundingClientRect().right}))"
            )
            assert len({round(item["left"], 3) for item in geometry}) == 1
            assert all(item["right"] > item["left"] for item in geometry)
        browser.close()
