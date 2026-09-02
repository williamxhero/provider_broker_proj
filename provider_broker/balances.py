"""Authenticated balance collection for the fixed upstream provider sites."""

import asyncio
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from aiohttp import ClientSession, ClientTimeout, CookieJar
from yarl import URL


class BalanceFailure(Exception):
    """A provider account could not be read without exposing its response."""


@dataclass(frozen=True)
class SiteSpec:
    id: str
    name: str
    adapter: str
    base_url: str
    currency: str
    default_threshold: float


SITES = (
    SiteSpec("liangrekui", "凉热葵", "newapi", "https://api.liangrekui.com", "CNY", 20.0),
    SiteSpec("cola", "可乐AI", "newapi", "https://code28.ccwu.cc", "USD", 5.0),
    SiteSpec("wawapi", "WawAPI", "wawapi", "https://wawapii.com", "USD", 5.0),
    SiteSpec("topapi", "Top-API", "newapi", "https://api-top.com", "USD", 5.0),
)


def _unwrap(payload):
    if not isinstance(payload, dict):
        raise BalanceFailure("unexpected response")
    if payload.get("success") is False:
        raise BalanceFailure(str(payload.get("message") or "provider rejected request"))
    return payload.get("data", payload)


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return float(result) if result.is_finite() else None


def extract_balance(payload: object) -> float:
    """Read only an explicit available-balance field; never infer it from usage."""
    data = _unwrap(payload)
    candidates = ("balance", "quota", "remaining_balance", "available_balance", "credit")
    queue = [data]
    while queue:
        item = queue.pop(0)
        if not isinstance(item, dict):
            continue
        for name in candidates:
            value = _number(item.get(name))
            if value is not None:
                return value
        queue.extend(value for value in item.values() if isinstance(value, dict))
    raise BalanceFailure("provider did not return an available balance")


def _cookies(jar: CookieJar, base_url: str) -> dict[str, str]:
    return {name: morsel.value for name, morsel in jar.filter_cookies(URL(base_url)).items()}


async def _json(response):
    if response.status >= 400:
        raise BalanceFailure(f"provider returned HTTP {response.status}")
    try:
        return await response.json(content_type=None)
    except Exception as exc:
        raise BalanceFailure("provider returned invalid JSON") from exc


async def _newapi_login(session: ClientSession, base_url: str, account: str, password: str) -> dict:
    async with session.post(f"{base_url}/api/user/login?turnstile=", json={"username": account, "password": password}) as response:
        _unwrap(await _json(response))
    async with session.get(f"{base_url}/api/user/self") as response:
        return await _json(response)


async def _wawapi_login(session: ClientSession, base_url: str, account: str, password: str) -> tuple[dict, dict]:
    async with session.post(f"{base_url}/api/v1/auth/login", json={"email": account, "password": password}) as response:
        login = _unwrap(await _json(response))
    token = login.get("access_token") if isinstance(login, dict) else None
    if not isinstance(token, str) or not token:
        raise BalanceFailure("provider did not return an access token")
    auth = {"Authorization": f"Bearer {token}"}
    async with session.get(f"{base_url}/api/v1/auth/me", headers=auth) as response:
        return await _json(response), {"access_token": token, "refresh_token": login.get("refresh_token")}


async def login(site: dict, account: str, password: str) -> tuple[float, dict]:
    timeout = ClientTimeout(total=25)
    jar = CookieJar()
    async with ClientSession(timeout=timeout, cookie_jar=jar) as session:
        if site["adapter"] == "newapi":
            payload = await _newapi_login(session, site["base_url"], account, password)
            credential = {"account": account, "password": password, "cookies": _cookies(jar, site["base_url"])}
        elif site["adapter"] == "wawapi":
            payload, tokens = await _wawapi_login(session, site["base_url"], account, password)
            credential = {"account": account, "password": password} | tokens
        else:
            raise BalanceFailure("unsupported provider adapter")
    return extract_balance(payload), credential


async def login_with_cookie(site: dict, cookie: str, user_agent: str) -> tuple[float, dict]:
    """Verify a user-supplied browser session without ever returning it."""
    if site["adapter"] != "newapi":
        raise BalanceFailure("manual cookie import is not supported for this site")
    headers = {"Cookie": cookie, "User-Agent": user_agent}
    async with ClientSession(timeout=ClientTimeout(total=25)) as session:
        async with session.get(f"{site['base_url']}/api/user/self", headers=headers) as response:
            payload = await _json(response)
    return extract_balance(payload), {"cookie_header": cookie, "user_agent": user_agent}


async def refresh(site: dict, credential: dict) -> tuple[float, dict]:
    """Use the saved session first, then deliberately re-authenticate once."""
    timeout = ClientTimeout(total=25)
    cookie_header, user_agent = credential.get("cookie_header"), credential.get("user_agent")
    if isinstance(cookie_header, str) and isinstance(user_agent, str):
        return await login_with_cookie(site, cookie_header, user_agent)
    account, password = credential.get("account"), credential.get("password")
    if not isinstance(account, str) or not isinstance(password, str):
        raise BalanceFailure("login needs to be completed again")
    try:
        async with ClientSession(timeout=timeout, cookie_jar=CookieJar()) as session:
            if site["adapter"] == "newapi":
                jar = session.cookie_jar
                for name, value in (credential.get("cookies") or {}).items():
                    jar.update_cookies({name: value}, response_url=URL(site["base_url"]))
                async with session.get(f"{site['base_url']}/api/user/self") as response:
                    payload = await _json(response)
                return extract_balance(payload), credential | {"cookies": _cookies(jar, site["base_url"])}
            if site["adapter"] == "wawapi" and credential.get("access_token"):
                async with session.get(f"{site['base_url']}/api/v1/auth/me", headers={"Authorization": f"Bearer {credential['access_token']}"}) as response:
                    return extract_balance(await _json(response)), credential
    except BalanceFailure:
        pass
    return await login(site, account, password)


async def sync_one(store, site_id: str, browser=None) -> dict:
    site = store.balance_site_secret(site_id)
    if site is None:
        raise BalanceFailure("unknown provider site")
    if not site["credential"]:
        raise BalanceFailure("not logged in")
    try:
        credential = site["credential"]
        if credential.get("browser_session"):
            if browser is None:
                raise BalanceFailure("interactive browser is unavailable")
            try:
                balance = await browser.fetch_balance(site)
            except Exception as exc:
                raise BalanceFailure(str(exc)) from exc
        else:
            balance, credential = await refresh(site, credential)
    except BalanceFailure as exc:
        store.record_balance_error(site_id, str(exc))
        return {"site": site_id, "ok": False, "error": str(exc)}
    event = store.record_balance(site_id, balance, credential)
    if event["entered_low"]:
        await notify_low_balance(store, event)
    return {"site": site_id, "ok": True, "balance": balance, "low": event["low"]}


async def notify_low_balance(store, event: dict) -> None:
    webhook = store.balance_webhook()
    if not webhook:
        return
    payload = {
        "event": "provider_balance_low",
        "site": event["name"],
        "balance": event["balance"],
        "threshold": event["threshold"],
        "currency": event["currency"],
        "message": f"{event['name']} 余额不足：{event['currency']} {event['balance']:.2f}（阈值 {event['threshold']:.2f}）",
    }
    try:
        async with ClientSession(timeout=ClientTimeout(total=15)) as session:
            async with session.post(webhook, json=payload) as response:
                if response.status >= 400:
                    raise BalanceFailure(f"webhook returned HTTP {response.status}")
    except Exception as exc:
        store.record_balance_notification_error(event["site"], str(exc))
    else:
        store.record_balance_notification_sent(event["site"])


async def scheduler(app) -> None:
    """Keep balance polling isolated from health probes and never stop the app."""
    while True:
        await asyncio.sleep(app["settings"].balance_scheduler_seconds)
        sites = app["store"].balance_sites()
        await asyncio.gather(*(sync_one(app["store"], site["id"], app.get("balance_browser")) for site in sites if site["enabled"] and site["configured"]), return_exceptions=True)
