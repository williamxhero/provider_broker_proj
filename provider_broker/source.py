import datetime
from aiohttp import ClientSession


def expand_config(payload: object) -> list[dict]:
    """Normalize CPA config variants into one immutable direct-upstream per key/model."""
    roots = payload.get("providers", payload.get("data", payload)) if isinstance(payload, dict) else payload
    if isinstance(roots, dict): roots = roots.values()
    result=[]
    for provider in roots or []:
        if not isinstance(provider, dict): continue
        base = provider.get("base_url") or provider.get("baseUrl") or provider.get("url")
        kind = (provider.get("type") or provider.get("provider_type") or "openai").lower()
        keys = provider.get("keys") or provider.get("api_keys") or [provider]
        for key in keys:
            secret = key.get("api_key") or key.get("key") or key.get("token")
            models = key.get("models") or provider.get("models") or []
            for model in models:
                name = model.get("id") if isinstance(model,dict) else model
                if base and secret and name:
                    result.append({"name":provider.get("name") or str(name),"base_url":base,"api_key":secret,"model":str(name),"provider_type":kind,"source":{"provider":provider.get("name"),"model":str(name)}})
    return result


async def sync_cpa(store, url: str, token: str) -> int:
    headers={"X-Management-Key":token} if token else {}
    async with ClientSession() as session:
        async with session.get(url.rstrip("/")+"/v0/management/config",headers=headers,timeout=20) as response:
            response.raise_for_status(); payload=await response.json()
    entries=expand_config(payload)
    store.replace_source_snapshot(entries, datetime.datetime.now(datetime.UTC).isoformat())
    return len(entries)
