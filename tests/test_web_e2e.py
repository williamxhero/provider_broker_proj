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
        settings = Settings(self.database_path, "admin-secret", "session-secret", base64.b64encode(b"x" * 32).decode())
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


def test_console_uses_strict_key_and_stage_contract(tmp_path):
    """The visible console exposes Key-safe fields and Stage-level test controls."""
    with LiveBroker(tmp_path / "strict-console.sqlite3") as broker, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()

        def payload(path):
            if path.startswith("/admin/v1/summary"):
                return {"routable_apis": 1, "last_successful_sync": None}
            if path.startswith("/admin/v1/providers"):
                return {"providers": [{
                    "fingerprint": "fp-a", "normalized_hostname": "alpha.invalid", "status": "enabled",
                    "note": "safe note", "api_key_mask": "abc***xyz", "max_parallel": 3,
                    "total_tokens": 12, "fee_buckets": {"UNKNOWN": {"total_fee": 0.02}},
                }]}
            if path.startswith("/admin/v1/stages"):
                return {"items": [{
                    "stage": "standard", "model": "gpt-5.6-luna", "family": "openai",
                    "provider_types": ["openai"], "callable_key_count": 1, "latest_test": None,
                    "technical_success_rate": 1, "avg_first_token_latency_ms": 120,
                    "total_tokens": 12, "fee_buckets": {"UNKNOWN": {"total_fee": 0.02}},
                }], "window": "24h"}
            if path == "/admin/v1/catalog":
                return {"catalog": {}}
            if path.startswith("/admin/v1/routing"):
                return {"race_parallel_cap": 3, "hedge_delay_ms": 0}
            if path.startswith("/admin/v1/models") or path.startswith("/admin/v1/pricing"):
                return {"items": []}
            if path.startswith("/admin/v1/quality"):
                return {"calls": 0, "failures": {}}
            if path.startswith("/admin/v1/calls") or path.startswith("/admin/v1/routes"):
                return {"items": [], "next_cursor": None}
            if path.startswith("/admin/v1/data-health"):
                return {"in_progress": 0, "reconciled_unknown": 0, "legacy_records": 0}
            if path.startswith("/admin/v1/analytics"):
                return {"groups": []}
            raise AssertionError(path)

        page.route("**/admin/v1/**", lambda route: route.fulfill(
            status=200, content_type="application/json", body=__import__("json").dumps(
                payload(route.request.url.removeprefix(broker.url))
            )
        ))
        page.goto(broker.url)
        page.locator("#providers tbody tr").wait_for()

        provider_headers = [text.replace("↑", "").replace("↓", "").strip() for text in page.locator("#providers thead th").all_inner_texts()]
        assert provider_headers == ["域名", "状态", "备注", "API Key", "单 Key 并发上限", "24h Token", "24h 费用", "操作"]
        assert page.locator("#providers").get_by_role("button", name="一键测试").count() == 0
        assert page.locator("#providers").get_by_text("safe note", exact=True).count() == 1
        assert page.locator("#model-view tbody tr").get_by_role("button", name="测试").count() == 1
        assert page.locator("#model-directory input[name=official_input_price]").count() == 0
        assert page.locator("main > section:has(#analytics-title)").count() == 1
        assert page.locator("main > section:has(#route-audit-title)").count() == 1
        browser.close()

def test_pricing_console_separates_models_prices_and_escapes_source_text(tmp_path):
    with LiveBroker(tmp_path / "pricing-console.sqlite3") as broker, sync_playwright() as playwright:
        seeded = threading.Event()
        def seed():
            store = broker.app["store"]
            store.create_canonical_model("console-model", stage="smart", family="Console family")
            provider_id = store.create_pricing_provider(
                "console-direct", provider_type="direct",
                name="Console <img src=x onerror=window.__injected=1>", multiplier=1.5,
            )
            store.insert_provider_model_price(
                provider_id=provider_id, model_id="console-model", source_kind="direct",
                input_price=1, cache_price=.2, output_price=4, currency="USD",
                source_name="Console price list", source_url="https://prices.invalid/console",
                source_evidence="Evidence <script>window.__injected=2</script>",
                verified_at="2026-09-12T00:00:00Z",
            )
            relay_id = store.create_pricing_provider("console-relay", provider_type="relay", name="Console Relay", multiplier=1.0)
            benchmark_id = store.create_pricing_provider("console-benchmark", provider_type="direct", name="Console Benchmark", multiplier=1.0)
            store.insert_provider_model_price(
                provider_id=benchmark_id, model_id="console-model", source_kind="direct",
                input_price=1, cache_price=.2, output_price=4, currency="USD",
                source_url="https://prices.invalid/benchmark", source_evidence="Benchmark table",
            )
            store.bind_relay_price(
                relay_id, "console-model", benchmark_id, "console-model",
                source_name="Relay terms", source_url="https://prices.invalid/binding", source_evidence="Initial binding",
            )
            seeded.set()
        broker.loop.call_soon_threadsafe(seed)
        assert seeded.wait(5)

        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.goto(broker.url)
        page.locator("#model-directory").get_by_text("console-model", exact=True).wait_for()
        page.locator("#pricing").get_by_text("console-model", exact=True).first.wait_for()
        assert page.evaluate("window.__injected") is None
        assert page.locator("#pricing img, #pricing svg, #pricing script").count() == 0
        assert "Console price list" in page.locator("#pricing").inner_text()
        assert "Evidence <script>window.__injected=2</script>" in page.locator("#pricing").inner_text()
        assert page.locator("#model-directory-section input[name=official_input_price]").count() == 0
        page.locator("#pricing-bindings").get_by_text("Initial binding", exact=False).wait_for()
        page.locator("#pricing-bindings").get_by_role("button", name="编辑").click()
        page.locator("#binding-form input[name=source_evidence]").fill("Updated binding")
        page.locator("#binding-form").get_by_role("button", name="保存").click()
        page.locator("#pricing-bindings").get_by_text("Updated binding", exact=False).wait_for()
        page.locator("#pricing-bindings").get_by_role("button", name="编辑").click()
        page.locator("#binding-deactivate").click()
        expect(page.locator("#pricing-bindings").get_by_text("Updated binding", exact=False)).to_have_count(0)
        browser.close()
