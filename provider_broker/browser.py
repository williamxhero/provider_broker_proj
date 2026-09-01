"""Small, local-only bridge to the persistent interactive balance browser."""

import json

from aiohttp import ClientSession, ClientTimeout

from .balances import BalanceFailure, extract_balance


class BrowserFailure(Exception):
    """The operator browser is unavailable or has not finished a site login."""


class BalanceBrowser:
    """Drive Chrome through localhost CDP without exporting cookies or tokens."""

    def __init__(self, endpoint: str = "http://127.0.0.1:9223"):
        self.endpoint = endpoint.rstrip("/")

    async def _json(self, path: str):
        try:
            async with ClientSession(timeout=ClientTimeout(total=5)) as session:
                async with session.get(f"{self.endpoint}{path}") as response:
                    if response.status >= 400:
                        raise BrowserFailure("interactive browser is unavailable")
                    return await response.json(content_type=None)
        except BrowserFailure:
            raise
        except Exception as exc:
            raise BrowserFailure("interactive browser is unavailable") from exc

    async def _command(self, websocket_url: str, method: str, params: dict) -> dict:
        try:
            async with ClientSession(timeout=ClientTimeout(total=20)) as session:
                async with session.ws_connect(websocket_url) as socket:
                    await socket.send_json({"id": 1, "method": method, "params": params})
                    while True:
                        message = await socket.receive_json()
                        if message.get("id") == 1:
                            if "error" in message:
                                raise BrowserFailure("interactive browser command failed")
                            return message.get("result", {})
        except BrowserFailure:
            raise
        except Exception as exc:
            raise BrowserFailure("interactive browser command failed") from exc

    async def open_login(self, site: dict) -> None:
        version = await self._json("/json/version")
        websocket_url = version.get("webSocketDebuggerUrl") if isinstance(version, dict) else None
        if not isinstance(websocket_url, str):
            raise BrowserFailure("interactive browser is unavailable")
        await self._command(websocket_url, "Target.createTarget", {"url": f"{site['base_url']}/login"})

    async def _site_tab(self, site: dict) -> dict:
        tabs = await self._json("/json/list")
        if not isinstance(tabs, list):
            raise BrowserFailure("interactive browser is unavailable")
        for tab in reversed(tabs):
            if isinstance(tab, dict) and str(tab.get("url", "")).startswith(site["base_url"]) and isinstance(tab.get("webSocketDebuggerUrl"), str):
                return tab
        raise BrowserFailure("open the site in the interactive browser and finish logging in first")

    async def _evaluate(self, tab: dict, expression: str) -> dict:
        result = await self._command(tab["webSocketDebuggerUrl"], "Runtime.evaluate", {
            "expression": expression, "awaitPromise": True, "returnByValue": True,
        })
        remote = result.get("result", {})
        value = remote.get("value") if isinstance(remote, dict) else None
        if not isinstance(value, str):
            raise BrowserFailure("site did not return a login result")
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise BrowserFailure("site did not return a valid login result") from exc

    async def fetch_balance(self, site: dict) -> float:
        tab = await self._site_tab(site)
        if site["adapter"] == "newapi":
            expression = """(async () => { const r = await fetch('/api/user/self', {credentials: 'include'}); return JSON.stringify({status: r.status, body: await r.text()}); })()"""
        elif site["adapter"] == "wawapi":
            expression = """(async () => {
              let r = await fetch('/api/v1/auth/me', {credentials: 'include'});
              if (r.status === 401) {
                const values = Object.values(localStorage).flatMap(value => { try { const item = JSON.parse(value); return [value, item.access_token, item.token, item.data && item.data.access_token]; } catch (_) { return [value]; } });
                const token = values.find(value => typeof value === 'string' && value.split('.').length === 3);
                if (token) r = await fetch('/api/v1/auth/me', {headers: {Authorization: `Bearer ${token}`}});
              }
              return JSON.stringify({status: r.status, body: await r.text()});
            })()"""
        else:
            raise BrowserFailure("unsupported provider site")
        payload = await self._evaluate(tab, expression)
        if not isinstance(payload, dict) or payload.get("status") != 200 or not isinstance(payload.get("body"), str):
            raise BrowserFailure("site login is not complete")
        try:
            return extract_balance(json.loads(payload["body"]))
        except (json.JSONDecodeError, BalanceFailure) as exc:
            raise BrowserFailure("site did not return an available balance") from exc
