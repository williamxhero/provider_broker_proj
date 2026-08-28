import asyncio
import time
from aiohttp import ClientSession


class UpstreamFailure(Exception): pass

async def invoke(provider, body: dict) -> dict:
    model = provider.models[0]
    effort = body.get("effort")
    headers={"Authorization":f"Bearer {provider.api_key}","Content-Type":"application/json"}
    if provider.provider_type in ("anthropic","claude"):
        headers={"x-api-key":provider.api_key,"anthropic-version":"2023-06-01","Content-Type":"application/json"}
        payload={"model":model,"max_tokens":body.get("max_tokens",1024),"messages":body.get("messages",[])}
        endpoint="/v1/messages"
    else:
        payload={"model":model,"input":body.get("input") or body.get("messages") or "", "stream":False}
        if effort: payload["reasoning"]={"effort":effort}
        endpoint="/v1/responses"
    started=time.perf_counter()
    try:
        async with ClientSession() as session:
            async with session.post(provider.base_url+endpoint,json=payload,headers=headers,timeout=body.get("timeout",60)) as response:
                data=await response.json(content_type=None)
                if response.status >= 400: raise UpstreamFailure(str(data))
    except Exception as exc:
        raise UpstreamFailure(str(exc)) from exc
    text = data.get("output_text") or data.get("content", [{}])[0].get("text") or data.get("content", [{}])[0].get("text", "")
    return {"text":text,"actual_model":data.get("model",model),"latency_ms":round((time.perf_counter()-started)*1000,2),"raw":data}


async def route(store, tier: str, body: dict) -> dict:
    groups={}
    for p in store.providers(tier): groups.setdefault(p.price_group,[]).append(p)
    errors=[]
    for _, providers in groups.items():
        tasks=[asyncio.create_task(invoke(p,body)) for p in providers]
        done,pending=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
        for task in pending: task.cancel()
        await asyncio.gather(*pending,return_exceptions=True)
        winner=next(iter(done))
        try:
            output=winner.result(); p=providers[tasks.index(winner)]
            store.observe(fingerprint=p.fingerprint,requested_model=str(body.get("model",tier)),actual_model=output["actual_model"],tier=tier,effort=body.get("effort"),success=1,latency_ms=output["latency_ms"],error=None)
            return output | {"provider":p.name}
        except Exception as exc: errors.append(str(exc))
    raise UpstreamFailure("; ".join(errors) or "no eligible provider")
