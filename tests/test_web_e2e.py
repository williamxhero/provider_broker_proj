import asyncio
import base64
import threading
from pathlib import Path

from aiohttp import web
from playwright.sync_api import expect, sync_playwright

from provider_broker.app import create_app
from provider_broker.settings import Settings
from web_test_support import safe_loopback_socket


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
        self.socket = safe_loopback_socket()
        self.site = web.SockSite(self.runner, self.socket)
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
                return {"providers": [
                    {
                        "fingerprint": "fp-a", "normalized_hostname": "alpha.invalid", "status": "enabled",
                        "note": "safe note", "api_key_mask": "abc***xyz", "max_parallel": 3,
                        "total_tokens": 12, "fee_buckets": {"UNKNOWN": {"total_fee": 0.02}},
                    },
                    {
                        "fingerprint": "fp-b", "normalized_hostname": "alpha.invalid", "status": "disabled",
                        "note": "backup key", "api_key_mask": "def***uvw", "max_parallel": 2,
                        "total_tokens": 8, "fee_buckets": {"UNKNOWN": {"total_fee": 0.01}},
                    },
                ]}
            if path.startswith("/admin/v1/stages"):
                return {"items": [{
                    "stage": "standard", "fingerprint": "fp-a", "note": "safe note",
                    "model": "gpt-5.6-luna", "family": "OpenAI GPT-5.6", "provider_type": "openai",
                    "normalized_hostname": "alpha.invalid", "api_key_mask": "abc***xyz",
                    "status": "enabled", "max_parallel": 3, "callable": True, "latest_test": None,
                    "technical_success_rate": 1, "avg_first_token_latency_ms": 120,
                    "total_tokens": 12, "fee_buckets": {"UNKNOWN": {"total_fee": 0.02}},
                    }], "window": "24h"}
            if path.startswith("/admin/v1/models"):
                return {"items": [{"id": "gpt-5.6-luna", "stage": "standard", "family": "OpenAI GPT-5.6", "active": True}]}
            if path.startswith("/admin/v1/routing"):
                return {"race_parallel_cap": 3, "hedge_delay_ms": 0}
            if path.startswith("/admin/v1/pricing"):
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
        page.locator("#providers tbody tr").first.wait_for()

        provider_headers = [text.replace("↑", "").replace("↓", "").strip() for text in page.locator("#providers thead th").all_inner_texts()]
        assert provider_headers == ["域名", "状态", "备注", "API Key", "单 Key 并发上限", "24h Token", "24h 费用", "操作"]
        assert page.locator("#providers").get_by_role("button", name="一键测试").count() == 0
        assert page.locator("#providers").get_by_text("safe note", exact=True).count() == 1
        assert page.locator("#providers tbody tr").count() == 2
        assert page.locator("#providers tbody td[rowspan='2']").count() == 1
        assert page.locator("#providers").get_by_text("abc***xyz", exact=True).count() == 1
        assert page.locator("#providers").get_by_text("def***uvw", exact=True).count() == 1
        model_headers = [text.replace("↑", "").replace("↓", "").strip() for text in page.locator("#model-view thead th").all_inner_texts()]
        assert model_headers == ["Stage", "备注", "模型", "Provider", "API Key", "状态", "单 Key 并发上限", "可调用", "最近测试", "技术成功率", "平均首字延迟", "24h Token", "24h 费用", "操作"]
        assert page.locator("#model-view tbody tr").get_by_role("button", name="测试 Stage").count() == 1
        assert page.locator("#model-view").get_by_text("gpt-5.6-luna", exact=True).count() == 1
        assert page.locator("#model-view").get_by_text("safe note", exact=True).count() == 1
        assert page.locator("#probe-stage, #probe-race, #probe-all, #probe-results").count() == 0
        assert page.locator("#model-directory").count() == 0
        assert page.locator("#models-section").count() == 1
        assert page.locator("#model-list .model-list-tag").filter(has_text="gpt-5.6-luna").count() == 1
        assert page.locator("main > section:has(#analytics-title)").count() == 1
        assert page.locator("main > section:has(#route-audit-title)").count() == 1
        browser.close()

def test_pricing_console_shows_only_provider_model_and_cny_price(tmp_path):
    with LiveBroker(tmp_path / "pricing-console.sqlite3") as broker, sync_playwright() as playwright:
        seeded = threading.Event()
        def seed():
            store = broker.app["store"]
            console_model = "gpt-5.6-luna"
            provider_id = store.create_pricing_provider(
                "console-openai", provider_type="openai",
                name="Console <img src=x onerror=window.__injected=1>",
            )
            store.insert_provider_model_price(
                provider_id=provider_id, model_id=console_model, output_price_cny=4,
                source_name="Console price list", source_url="https://prices.invalid/console",
                source_evidence="Evidence <script>window.__injected=2</script>",
                verified_at="2026-09-12T00:00:00Z",
            )
            seeded.set()
        broker.loop.call_soon_threadsafe(seed)
        assert seeded.wait(5)

        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.goto(broker.url)
        page.locator("#pricing").get_by_text("gpt-5.6-luna", exact=True).first.wait_for()
        assert page.evaluate("window.__injected") is None
        assert page.locator("#pricing img, #pricing svg, #pricing script").count() == 0
        assert page.locator("#pricing").get_by_text("4", exact=True).count() == 1
        headers = [text.replace("↑", "").replace("↓", "").strip() for text in page.locator("#pricing thead th").all_inner_texts()]
        assert headers == ["Provider", "Model", "输出价格 / 1M CNY", "操作"]
        assert "Console price list" not in page.locator("#pricing").inner_text()
        assert "Evidence <script>window.__injected=2</script>" not in page.locator("#pricing").inner_text()
        assert page.locator("#model-directory-section").count() == 0
        assert page.locator("#pricing-bindings").count() == 0
        browser.close()
