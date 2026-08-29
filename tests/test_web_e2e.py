import asyncio
import base64
import threading
from pathlib import Path

from aiohttp import web
from playwright.sync_api import expect, sync_playwright

from provider_broker.app import create_app
from provider_broker.settings import Settings


class LiveBroker:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.ready = threading.Event()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self.thread.start()
        assert self.ready.wait(10)
        return self

    def _run(self):
        asyncio.set_event_loop(self.loop)
        settings = Settings(self.database_path, "client-secret", "admin-secret", "session-secret", base64.b64encode(b"x" * 32).decode())
        self.app = create_app(settings)
        self.runner = web.AppRunner(self.app)
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


def test_console_edits_policy_syncs_and_pages_calls(tmp_path):
    """An operator can use management, catalog CRUD, and filters from visible DOM."""
    provider = {
        "fingerprint": "provider-a", "name": "Alpha <img src=x onerror=window.__injected=1>",
        "note": "initial note <script>window.__injected=2</script>", "enabled": True,
        "family": "openai", "base_url": "https://alpha.invalid/<svg onload=window.__injected=3>", "api_key_mask": "abc***xyz",
        "models": ["luna"], "inventory_status": "available", "technical_success_rate": 0.98,
        "avg_ttft_ms": 1800, "cost_24h": 0.02, "multiplier": 1.0, "max_parallel": 3,
    }
    same_site_provider = provider | {
        "fingerprint": "provider-b", "note": "second note", "api_key_mask": "def***uvw",
        "technical_success_rate": 0.9, "avg_ttft_ms": 900, "cost_24h": 0.01, "multiplier": 1.5,
    }
    calls = [
        {"id": 2, "time": "2026-08-29T10:00:00Z", "note": "initial note", "provider": "Alpha", "requested_model": "luna", "actual_model": "luna", "intellect": "standard", "effort": "high", "ttft_ms": 120, "status": "completed", "input_tokens": 10, "output_tokens": 4, "cost": 0.02, "request_id": "r-2"},
        {"id": 1, "time": "2026-08-29T09:00:00Z", "note": "initial note", "provider": "Alpha", "requested_model": "luna", "actual_model": "luna", "intellect": "standard", "effort": "medium", "ttft_ms": 140, "status": "transport_failed", "input_tokens": 2, "output_tokens": 0, "cost": None, "request_id": "r-1"},
    ]
    catalog = {"luna": {"family": "openai", "intellect": "standard", "official_input_price": 1, "official_cache_price": 0.1, "official_output_price": 2, "blended_price": 1.656, "available_provider_count": 1}}
    seen = []

    def payload(path):
        if path.startswith("/admin/v1/summary"):
            return {"routable_apis": 1, "technical_success_rate": 0.98, "avg_ttft_ms": 1800, "last_successful_sync": "2026-08-29T10:00:00Z"}
        if path == "/admin/v1/providers":
            return {"providers": [provider, same_site_provider]}
        if path == "/admin/v1/catalog":
            return {"catalog": catalog}
        if path == "/admin/v1/routing":
            return {"race_parallel_cap": 3}
        if path.startswith("/admin/v1/quality"):
            return {"calls": 7 if "window=7d" in path else 2, "total_cost": 0.02, "avg_ttft_ms": 130, "p95_ttft_ms": 140, "model_fulfillment_rate": 1, "failures": {"cancelled": 0, "timed_out": 0, "transport_failed": 1, "protocol_failed": 0, "stream_incomplete": 0}}
        if path.startswith("/admin/v1/calls"):
            if "cursor=2" in path:
                return {"items": [calls[1]], "next_cursor": None}
            return {"items": [calls[0]], "next_cursor": "2"}
        if path == "/admin/v1/sync":
            return {"added": 1, "updated": 0, "offlined": 0, "inventory_failures": 0, "last_successful_sync": "2026-08-29T10:00:00Z"}
        raise AssertionError(path)

    with LiveBroker(tmp_path / "broker.sqlite3") as broker, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()

        def route_handler(route):
            request = route.request
            path = request.url.removeprefix(broker.url)
            seen.append((request.method, path, request.post_data))
            if path == "/admin/v1/catalog" and request.method == "POST":
                body = request.post_data_json
                catalog[body["model"]] = {key: value for key, value in body.items() if key != "model"} | {"blended_price": 2.456, "available_provider_count": 0}
                route.fulfill(status=201, content_type="application/json", body='{"model":"gpt-console-route"}')
            elif path == "/admin/v1/routing" and request.method == "PATCH":
                route.fulfill(status=200, content_type="application/json", body='{"race_parallel_cap":2}')
            elif path == "/admin/v1/catalog/apply" and request.method == "POST":
                route.fulfill(status=200, content_type="application/json", body='{"providers":1,"retained_models":1,"removed_models":2}')
            elif path.startswith("/admin/v1/catalog/") and request.method == "PUT":
                model = path.rsplit("/", 1)[-1]
                catalog[model] = request.post_data_json | {"blended_price": 2.456, "available_provider_count": catalog[model]["available_provider_count"]}
                route.fulfill(status=200, content_type="application/json", body='{"updated":true}')
            elif path.startswith("/admin/v1/catalog/") and request.method == "DELETE":
                catalog.pop(path.rsplit("/", 1)[-1])
                route.fulfill(status=204, body="")
            elif request.method == "PATCH":
                provider.update(request.post_data_json)
                route.fulfill(status=200, content_type="application/json", body='{"updated":true}')
            else:
                route.fulfill(status=200, content_type="application/json", body=__import__("json").dumps(payload(path)))

        page.route("**/admin/v1/**", route_handler)
        page.goto(broker.url)
        assert page.evaluate("window.__injected") is None
        assert page.locator("#providers img, #providers svg, #providers script").count() == 0
        assert page.get_by_text("https://alpha.invalid/<svg onload=window.__injected=3>", exact=True).count() == 1
        assert "provider-secret" not in page.content()
        page.get_by_text("1.8 s", exact=True).first.wait_for()
        page.get_by_text("2026/08/29 18:00", exact=True).wait_for()
        assert page.locator("#race-parallel-cap").count() == 1
        assert "价格决定先后" not in page.content()
        assert "stage 决定路由分区" not in page.content()
        assert page.locator("#providers .model-tag").all_inner_texts() == ["luna", "luna"]
        assert "available" not in page.locator("#providers").inner_text()
        page.get_by_text("$0.02", exact=True).first.wait_for()
        base_urls = page.locator("#providers tbody td[rowspan]")
        assert base_urls.count() == 1
        assert base_urls.first.get_attribute("rowspan") == "2"
        assert base_urls.first.inner_text() == "https://alpha.invalid/<svg onload=window.__injected=3>"
        model_view = page.locator("#model-view tbody tr")
        assert model_view.count() == 2
        assert model_view.locator("td").all_inner_texts() == ["standard", "luna", "低价组", "initial note <script>window.__injected=2</script>", "$1.656", "高价组", "second note", "$2.484"]
        assert model_view.locator("td").nth(0).get_attribute("rowspan") == "2"
        assert model_view.locator("td").nth(1).get_attribute("rowspan") == "2"
        page.locator("#model-view").get_by_role("button", name="价格组").click()
        page.locator("#race-parallel-cap").fill("2")
        page.locator("#save-routing").click()
        page.get_by_text("同价竞速 Key 数已设为 2").wait_for()
        page.locator("#catalog-apply").click()
        page.get_by_text("已应用目录：1 个 Key，保留 1 个模型，移除 2 个模型").wait_for()
        page.locator("#catalog-create").click()
        page.locator("#catalog-form [name=model]").fill("gpt-console-route")
        page.locator("#catalog-form [name=family]").fill("Console test")
        page.locator("#catalog-form [name=intellect]").select_option("standard")
        page.locator("#catalog-form [name=official_input_price]").fill("1")
        page.locator("#catalog-form [name=official_cache_price]").fill("0.1")
        page.locator("#catalog-form [name=official_output_price]").fill("3")
        assert page.locator("#catalog-form [name=blended_price]").input_value() == "2.456000"
        page.locator("#catalog-form button[type=submit]").click()
        page.locator("#catalog").get_by_text("gpt-console-route", exact=True).wait_for()
        catalog_row = page.locator("#catalog tbody tr").filter(has_text="gpt-console-route")
        catalog_row.locator("button").click()
        page.locator("#catalog-form [name=intellect]").select_option("smart")
        page.locator("#catalog-form button[type=submit]").click()
        page.locator("#catalog tbody tr").filter(has_text="gpt-console-route").get_by_text("smart", exact=True).wait_for()
        page.locator("#catalog tbody tr").filter(has_text="gpt-console-route").locator("button").click()
        page.locator("#catalog-delete").click()
        expect(page.locator("#catalog").get_by_text("gpt-console-route", exact=True)).to_have_count(0)
        page.locator("#providers").get_by_role("button", name="编辑").first.click()
        assert "Alpha <img src=x onerror=window.__injected=1>" in page.locator("#editor-source").inner_text()
        assert page.locator("#policy [name=multiplier]").get_attribute("step") == "0.001"
        page.get_by_label("备注").fill("saved note")
        page.get_by_label("启用").uncheck()
        page.locator("#policy").get_by_role("button", name="保存").click()
        page.get_by_role("button", name="从 CPA 手动同步").click()
        page.get_by_text("added 1 updated 0 offlined 0 inventory_failures 0").wait_for()
        page.get_by_role("button", name="7d").click()
        page.get_by_text("7", exact=True).last.wait_for()
        page.locator("#callprovider").fill("Alpha")
        page.locator("#callstatus").fill("completed")
        page.locator("#callwindow").select_option("1h")
        page.locator("#calllimit").select_option("2")
        page.get_by_text("r-2").wait_for()
        page.get_by_role("button", name="下一页").click()
        page.get_by_text("r-1").wait_for()
        page.reload()
        expect(page.get_by_role("button", name="7d")).to_have_class(__import__("re").compile("active"))
        expect(page.locator("#model-view").get_by_role("button", name="价格组")).to_have_class(__import__("re").compile("active"))
        assert page.locator("#callwindow").input_value() == "1h"
        assert page.locator("#calllimit").input_value() == "2"
        assert page.locator("#callprovider").input_value() == "Alpha"
        assert page.locator("#callstatus").input_value() == "completed"
        assert page.locator("#providers").inner_text().find("saved note") >= 0
        assert any(method == "PATCH" and '"enabled":false' in (body or "") for method, _, body in seen)
        assert any("window=1h" in path and "provider=Alpha" in path and "status=completed" in path for _, path, _ in seen)
        assert any("limit=2" in path for _, path, _ in seen)
        assert any(method == "POST" and path == "/admin/v1/catalog" for method, path, _ in seen)
        assert any(method == "POST" and path == "/admin/v1/catalog/apply" for method, path, _ in seen)
        assert any(method == "PATCH" and path == "/admin/v1/routing" and '"race_parallel_cap":2' in (body or "") for method, path, body in seen)
        assert any(method == "PUT" and path.endswith("/gpt-console-route") for method, path, _ in seen)
        assert any(method == "DELETE" and path.endswith("/gpt-console-route") for method, path, _ in seen)
        browser.close()
