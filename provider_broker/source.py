import datetime
from aiohttp import ClientSession

from .catalog import canonicalize


def _request_headers(value: object) -> dict[str, str]:
    """Keep CPA's transport defaults without permitting credential/header overrides."""
    blocked = {"authorization", "content-type", "content-length", "host"}
    if not isinstance(value, dict):
        return {}
    return {
        str(name): str(header_value)
        for name, header_value in value.items()
        if str(name).lower() not in blocked and isinstance(header_value, (str, int, float))
    }


def _site_name(*values: object) -> str | None:
    for value in values:
        if not isinstance(value, dict):
            continue
        for field in ('site_name', 'site-name', 'siteName', 'name', 'id', 'label', 'endpoint'):
            candidate = value.get(field)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def _api_url(base_url: str, suffix: str) -> str:
    base = base_url.rstrip("/")
    return base + suffix if base.endswith("/v1") else base + "/v1" + suffix


def expand_config(payload: object) -> list[dict]:
    """Normalize CPA config variants into one immutable direct-upstream per key/model."""
    if isinstance(payload, dict) and any(k in payload for k in ('codex-api-key','claude-api-key','openai-compatibility')):
        result=[]
        for section, family in (('codex-api-key','codex'),('claude-api-key','anthropic'),('openai-compatibility','openai')):
            defaults = _request_headers(payload.get('codex-header-defaults')) if family == 'codex' else {}
            for key in payload.get(section,[]) or []:
                base=key.get('base_url') or key.get('base-url') or key.get('url')
                secret=key.get('api_key') or key.get('api-key') or key.get('key')
                if base and secret:
                    site_name = _site_name(key)
                    configured=key.get('models') or []
                    aliases={str(model.get('alias')):str(model.get('name')) for model in configured if isinstance(model,dict) and model.get('alias') and model.get('name')}
                    result.append({'name':site_name or section,'site_name':site_name,'base_url':base,'api_key':secret,'models':['unavailable'],'aliases':aliases,'provider_type':family,'request_headers':defaults,'source':{'section':section,'site_name':site_name}})
        return result
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
            names = [model.get("id") if isinstance(model,dict) else model for model in models]
            names = [str(name) for name in names if name]
            if base and secret and names:
                site_name = _site_name(key, provider)
                result.append({"name":site_name or names[0],"site_name":site_name,"base_url":base,"api_key":secret,"models":names,"provider_type":kind,"request_headers":_request_headers(provider.get("headers")),"source":{"site_name":site_name}})
    return result


async def sync_cpa(store, url: str, token: str) -> dict:
    headers={"X-Management-Key":token} if token else {}
    inventory_failures=0
    async with ClientSession() as session:
        async with session.get(url.rstrip("/")+"/v0/management/config",headers=headers,timeout=20) as response:
            response.raise_for_status(); payload=await response.json()
    if not isinstance(payload, dict): raise ValueError('invalid source configuration')
    entries=expand_config(payload)
    if not entries: raise ValueError('invalid source configuration')
    async with ClientSession() as session:
        for entry in entries:
            headers=entry.get('request_headers', {}) | {'Authorization':'Bearer '+entry['api_key']}
            try:
                async with session.get(_api_url(entry['base_url'], '/models'),headers=headers,timeout=10) as response:
                    raw=await response.json(content_type=None)
                    discovered=[str(x.get('id')) for x in raw.get('data',[]) if isinstance(x,dict) and x.get('id')] if response.status == 200 and isinstance(raw,dict) else []
                    aliases=entry.get('aliases',{})
                    models=list(dict.fromkeys(canonicalize(aliases.get(model,model)) for model in discovered))
                    entry['models']=models or ['unavailable']; entry['inventory_status']='available' if models else 'unavailable'
            except Exception:
                entry['models']=['unavailable']; entry['inventory_status']='unavailable'
                inventory_failures+=1
    store.replace_source_snapshot(entries, datetime.datetime.now(datetime.UTC).isoformat())
    return {'count':len(entries),'inventory_failures':inventory_failures}
