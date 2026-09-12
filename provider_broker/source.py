import datetime
import asyncio
import json
from urllib.parse import urlsplit
from aiohttp import ClientSession

from .catalog import canonicalize


_CHAT_API_ROOTS = (
    "/v1",
    "/v1/openai",
    "/api/v3",
    "/openai",
    "/compatible-mode/v1",
    "/api/paas/v4",
)
_BLOCKED_HEADERS = {
    "authorization", "content-type", "content-length", "host",
    "cookie", "proxy-authorization", "set-cookie", "x-api-key",
    "x-management-key",
}

_CPA_CONFIG_SECTIONS = {
    "codex": "codex-api-key",
    "anthropic": "claude-api-key",
    "claude": "claude-api-key",
}


def _management_headers(token: str) -> dict[str, str]:
    """Authenticate every CPA management request without putting the key in logs."""
    if not isinstance(token, str) or not token.strip() or len(token) > 4096 or "\r" in token or "\n" in token:
        raise ValueError("CPA management key is required")
    # Current CPA releases use Authorization; the legacy header is retained for
    # older protected installations.  Both are sent only on the private hop.
    return {
        "Authorization": f"Bearer {token}",
        "X-Management-Key": token,
    }


def _safe_base_url(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip() or "\r" in value or "\n" in value:
        return None
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        return None
    if parts.query or parts.fragment:
        return None
    return value.strip().rstrip("/")


def _safe_secret(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 4096 or "\r" in value or "\n" in value:
        return None
    return value


def _registration_entries(value: object) -> list[dict]:
    """Validate operator-supplied entries before they cross the CPA boundary."""
    raw = value.get("providers") if isinstance(value, dict) else value
    if not isinstance(raw, list) or not raw or len(raw) > 32:
        raise ValueError("providers must be a non-empty list")
    result = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("provider entry must be an object")
        base_url = _safe_base_url(item.get("base_url") or item.get("base-url") or item.get("url"))
        api_key = _safe_secret(item.get("api_key") or item.get("api-key") or item.get("key"))
        models = item.get("models")
        if not base_url or not api_key or not isinstance(models, list) or not models or len(models) > 128:
            raise ValueError("provider entries require a safe base_url, api_key, and models")
        model_names = []
        for model in models:
            if not isinstance(model, str) or not model.strip() or len(model) > 160 or "\r" in model or "\n" in model:
                raise ValueError("provider model names must be bounded strings")
            model_names.append(model.strip())
        provider_type = _provider_label(item.get("provider_type") or item.get("type") or item.get("provider") or "openai")
        name = item.get("name") or item.get("site_name") or provider_type
        if not isinstance(name, str) or not name.strip() or len(name) > 160 or "\r" in name or "\n" in name:
            raise ValueError("provider name must be a bounded string")
        if api_key in name:
            name = provider_type
        result.append({
            "name": name.strip(), "site_name": str(item.get("site_name") or name).strip()[:160],
            "base_url": base_url, "api_key": api_key, "models": list(dict.fromkeys(model_names)),
            "provider_type": provider_type,
        })
    return result


def _entry_identity(item: dict) -> tuple[str, str] | None:
    base_url = _safe_base_url(item.get("base_url") or item.get("base-url") or item.get("url"))
    api_key = _safe_secret(item.get("api_key") or item.get("api-key") or item.get("key"))
    return (base_url, api_key) if base_url and api_key else None


def _cpa_entry(entry: dict) -> dict:
    return {
        "name": entry["name"], "base_url": entry["base_url"],
        "api_key": entry["api_key"], "models": entry["models"],
    }


def _merge_registration(config: dict, entries: list[dict]) -> tuple[dict, int, int]:
    """Merge by endpoint/key, preserving CPA's existing config shape."""
    merged = json.loads(json.dumps(config))
    if "openai-compatibility" in merged:
        values = _as_dicts(merged.get("openai-compatibility", []))
        added = updated = 0
        for entry in entries:
            candidate = {
                "name": entry["name"], "base-url": entry["base_url"],
                "api-key-entries": [{"api-key": entry["api_key"]}],
                "models": [{"name": model, "alias": model} for model in entry["models"]],
            }
            identity = _entry_identity({"base_url": candidate["base-url"], "key": entry["api_key"]})
            found = None
            for index, item in enumerate(values):
                for key in _as_dicts(item.get("api-key-entries")):
                    if _entry_identity({
                        "base_url": item.get("base-url") or item.get("base_url") or item.get("url"),
                        "key": key.get("api-key") or key.get("api_key") or key.get("key"),
                    }) == identity:
                        found = index
                        break
                if found is not None:
                    break
            if found is None:
                values.append(candidate)
                added += 1
            else:
                values[found] = values[found] | candidate
                updated += 1
        merged["openai-compatibility"] = values
        return merged, added, updated
    legacy = any(section in merged for section in _CPA_CONFIG_SECTIONS.values())
    added = updated = 0
    if legacy:
        for entry in entries:
            section = _CPA_CONFIG_SECTIONS.get(entry["provider_type"], "openai-compatibility")
            values = _as_dicts(merged.get(section, []))
            candidate = _cpa_entry(entry)
            identity = _entry_identity(candidate)
            found = next((index for index, item in enumerate(values) if _entry_identity(item) == identity), None)
            if found is None:
                values.append(candidate); added += 1
            else:
                values[found] = values[found] | candidate; updated += 1
            merged[section] = values
    else:
        values = _as_dicts(merged.get("providers", []))
        for entry in entries:
            candidate = {
                "name": entry["name"], "type": entry["provider_type"], "base_url": entry["base_url"],
                "keys": [{"key": entry["api_key"], "models": entry["models"]}],
            }
            identity = _entry_identity({"base_url": candidate["base_url"], "key": entry["api_key"]})
            found = None
            for index, item in enumerate(values):
                for key in _as_dicts(item.get("keys")) or [item]:
                    if _entry_identity({"base_url": item.get("base_url") or item.get("baseUrl") or item.get("url"), "key": key.get("key") or key.get("api_key") or key.get("token")}) == identity:
                        found = index; break
                if found is not None: break
            if found is None:
                values.append(candidate); added += 1
            else:
                values[found] = values[found] | {"name": candidate["name"], "type": candidate["type"], "base_url": candidate["base_url"], "keys": candidate["keys"]}; updated += 1
        merged["providers"] = values
    return merged, added, updated


async def register_cpa(url: str, token: str, providers: object) -> dict:
    """Register entries through CPA's authenticated management config endpoint.

    Credentials exist only in request memory and the authenticated CPA request;
    the return value intentionally contains counts only.
    """
    entries = _registration_entries(providers)
    headers = _management_headers(token)
    endpoint = _safe_base_url(url)
    if endpoint is None:
        raise ValueError("CPA management URL must be an HTTP(S) URL without credentials")
    async with ClientSession() as session:
        management_endpoint = endpoint + "/v0/management/openai-compatibility"
        try:
            async with session.get(management_endpoint, headers=headers, timeout=20) as response:
                if response.status in (404, 405):
                    raise LookupError("specialized management endpoint unavailable")
                response.raise_for_status()
                payload = await response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("openai-compatibility"), list):
                raise ValueError("invalid CPA compatibility configuration")
            merged, added, updated = _merge_registration(payload, entries)
            body = json.dumps(merged["openai-compatibility"], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            put_headers = headers | {"Content-Type": "application/json", "Accept": "application/json"}
            async with session.put(management_endpoint, data=body, headers=put_headers, timeout=20) as response:
                response.raise_for_status()
        except LookupError:
            async with session.get(endpoint + "/v0/management/config", headers=headers, timeout=20) as response:
                response.raise_for_status()
                config = await response.json()
            if not isinstance(config, dict):
                raise ValueError("invalid CPA configuration")
            merged, added, updated = _merge_registration(config, entries)
            put_headers = headers | {"Content-Type": "application/yaml", "Accept": "application/json"}
            # JSON is a YAML 1.2 subset and avoids introducing a second serializer;
            # CPA's config.yaml management handler accepts it as YAML.
            body = json.dumps(merged, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            async with session.put(endpoint + "/v0/management/config.yaml", data=body, headers=put_headers, timeout=20) as response:
                response.raise_for_status()
    return {"added": added, "updated": updated, "registered": len(entries)}


def _as_dicts(value: object) -> list[dict]:
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, dict)]
    return []


def _host_matches(host: str, *domains: str) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def _url_parts(value: str):
    try:
        return urlsplit(value)
    except (TypeError, ValueError):
        return None


def _provider_label(value: object) -> str:
    return str(value or "openai").strip().casefold().replace("-", "_").replace(" ", "_")


def _request_headers(value: object) -> dict[str, str]:
    """Keep CPA's transport defaults without permitting credential/header overrides."""
    if not isinstance(value, dict):
        return {}
    return {
        str(name): str(header_value)
        for name, header_value in value.items()
        if isinstance(name, str)
        and name.strip()
        and "\r" not in name and "\n" not in name
        and name.casefold() not in _BLOCKED_HEADERS
        and isinstance(header_value, (str, int, float))
        and "\r" not in str(header_value) and "\n" not in str(header_value)
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
    base = str(base_url).strip().rstrip("/")
    # CPA entries may contain a complete API root (/v1, /api/v3, /openai).
    # Only add /v1 when the operator supplied a bare host.
    parts = _url_parts(base)
    path = (parts.path if parts else "").rstrip("/").lower()
    return base + suffix if path.endswith(_CHAT_API_ROOTS) else base + "/v1" + suffix


def _provider_type(base_url: str, declared: object, protocol: object = None) -> str:
    """Normalize vendor entries to the wire protocol used by the broker."""
    value = _provider_label(declared)
    style = _provider_label(protocol) if protocol else ""
    if style in {"chat", "chat_completions", "openai_chat", "openai_compatible", "completions"}:
        return "openai_chat"
    if value in {
        "deepinfra", "deepseek", "bailian", "qwen", "aliyun", "dashscope",
        "ark", "volcengine", "volc_engine", "doubao", "seed", "doubao_seed",
        "glm", "zhipu", "zhipuai", "zai", "bigmodel", "openai_compatible",
    }:
        return "openai_chat"
    parts = _url_parts(base_url)
    host = (parts.hostname if parts else None) or ""
    host = host.lower()
    if _host_matches(host, "deepinfra.com", "deepseek.com", "dashscope.aliyuncs.com",
                     "aliyuncs.com", "volces.com", "bigmodel.cn", "z.ai"):
        return "openai_chat"
    return value


def _normalize_base_url(base_url: str, declared: object) -> str:
    """Fill only documented vendor defaults; arbitrary gateways stay untouched."""
    base = str(base_url).strip().rstrip("/")
    parts = _url_parts(base)
    path = (parts.path if parts else "").rstrip("/").lower()
    if path:
        return base
    host = ((parts.hostname if parts else None) or "").lower()
    value = _provider_label(declared) if declared else ""
    if _host_matches(host, "deepinfra.com") or value == "deepinfra":
        return base + "/v1/openai"
    if _host_matches(host, "volces.com") or value in {"ark", "volcengine", "volc_engine", "doubao", "seed", "doubao_seed"}:
        return base + "/api/v3"
    if _host_matches(host, "dashscope.aliyuncs.com", "aliyuncs.com") or value in {"bailian", "qwen", "aliyun", "dashscope"}:
        return base + "/compatible-mode/v1"
    if _host_matches(host, "bigmodel.cn", "z.ai") or value in {"glm", "zhipu", "zhipuai", "zai", "bigmodel"}:
        return base + "/api/paas/v4"
    if _host_matches(host, "deepseek.com") or value == "deepseek":
        return base + "/v1"
    return base


def expand_config(payload: object) -> list[dict]:
    """Normalize CPA config variants into one immutable direct-upstream per key/model."""
    if isinstance(payload, dict) and any(k in payload for k in ('codex-api-key','claude-api-key','openai-compatibility')):
        result=[]
        for section, family in (('codex-api-key','codex'),('claude-api-key','anthropic'),('openai-compatibility','openai')):
            defaults = _request_headers(payload.get('codex-header-defaults')) if family == 'codex' else {}
            for key in _as_dicts(payload.get(section, [])):
                base=key.get('base_url') or key.get('base-url') or key.get('url')
                if not _safe_base_url(str(base)):
                    continue
                base = _normalize_base_url(str(base), family)
                site_name = _site_name(key)
                configured=_as_dicts(key.get('models', []))
                aliases={
                    str(model.get('name')).strip().casefold(): str(model.get('alias')).strip()
                    for model in configured
                    if isinstance(model.get('alias'), str) and isinstance(model.get('name'), str)
                    and model.get('alias').strip() and model.get('name').strip()
                } if family == 'openai' else {
                    str(model.get('alias')).strip().casefold(): str(model.get('name')).strip()
                    for model in configured
                    if isinstance(model.get('alias'), str) and isinstance(model.get('name'), str)
                    and model.get('alias').strip() and model.get('name').strip()
                }
                credentials = _as_dicts(key.get('api-key-entries')) or [key]
                for credential in credentials:
                    secret=_safe_secret(credential.get('api_key') or credential.get('api-key') or credential.get('key'))
                    if not secret:
                        continue
                    visible_name = site_name
                    if visible_name and secret in visible_name:
                        visible_name = None
                    request_headers = defaults | _request_headers(key.get('headers')) | _request_headers(credential.get('headers'))
                    result.append({'name':visible_name or section,'site_name':visible_name,'base_url':base,'api_key':secret,'models':['unavailable'],'aliases':aliases,'provider_type':_provider_type(base, family),'request_headers':request_headers,'source':{'section':section,'site_name':visible_name}})
        return result
    roots = payload.get("providers", payload.get("data", payload)) if isinstance(payload, dict) else payload
    if isinstance(roots, dict): roots = roots.values()
    result=[]
    for provider in roots or []:
        if not isinstance(provider, dict): continue
        base = provider.get("base_url") or provider.get("baseUrl") or provider.get("url")
        kind = _provider_label(provider.get("type") or provider.get("provider_type") or provider.get("provider") or provider.get("vendor") or "openai")
        keys = _as_dicts(provider.get("keys") or provider.get("api_keys")) or [provider]
        normalized_base = _normalize_base_url(str(base), kind) if _safe_base_url(str(base)) else None
        for key in keys:
            secret = _safe_secret(key.get("api_key") or key.get("key") or key.get("token"))
            models = key.get("models") or provider.get("models") or []
            if not isinstance(models, (list, tuple)):
                models = []
            names = [model.get("id") if isinstance(model,dict) else model for model in models]
            names = [str(name) for name in names if name]
            if normalized_base and secret and names:
                site_name = _site_name(key, provider)
                if site_name and secret in site_name:
                    site_name = None
                request_headers = _request_headers(provider.get("headers"))
                request_headers.update(_request_headers(key.get("headers")))
                result.append({"name":site_name or names[0],"site_name":site_name,"base_url":normalized_base,"api_key":secret,"models":names,"provider_type":_provider_type(normalized_base, kind, provider.get("protocol") or key.get("protocol")),"request_headers":request_headers,"source":{"site_name":site_name}})
    return result


async def sync_cpa(store, url: str, token: str) -> dict:
    headers = _management_headers(token)
    endpoint = _safe_base_url(url)
    if endpoint is None:
        raise ValueError("CPA management URL must be an HTTP(S) URL without credentials")
    inventory_failures=0
    async with ClientSession() as session:
        async with session.get(endpoint+"/v0/management/config",headers=headers,timeout=20) as response:
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
                    models=list(dict.fromkeys(canonicalize(aliases.get(model.casefold(), model)) for model in discovered))
                    entry['models']=models or ['unavailable']; entry['inventory_status']='available' if models else 'unavailable'
                    if aliases and entry.get('provider_type') == 'openai_chat':
                        entry['model_aliases'] = {
                            canonicalize(alias): actual
                            for actual, alias in aliases.items()
                            if canonicalize(alias) in models
                        }
            except Exception:
                entry['models']=['unavailable']; entry['inventory_status']='unavailable'
                inventory_failures+=1
    store.replace_source_snapshot(entries, datetime.datetime.now(datetime.UTC).isoformat())
    return {'count':len(entries),'inventory_failures':inventory_failures}


async def scheduler(app) -> None:
    """Refresh CPA inventory independently; a failed refresh leaves its snapshot intact."""
    settings = app["settings"]
    try:
        while True:
            await asyncio.sleep(max(30, settings.source_scheduler_seconds))
            try:
                await sync_cpa(app["store"], settings.cpa_url, settings.cpa_token)
                app["store"].ensure_health_targets(app["clock"]())
            except Exception:
                # The operator can inspect the last successful snapshot; routing
                # must never turn a management outage into an inventory wipe.
                pass
    except asyncio.CancelledError:
        raise
