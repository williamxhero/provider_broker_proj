import asyncio
import random
import time
from aiohttp import ClientSession


class UpstreamFailure(Exception): pass

async def invoke(provider, body: dict) -> dict:
    model = provider.models[0]
    effort = body.get("effort")
    headers={"Authorization":f"Bearer {provider.api_key}","Content-Type":"application/json"}
    if provider.provider_type in ("anthropic","claude"):
        headers={"Authorization":f"Bearer {provider.api_key}","Content-Type":"application/json"}
        payload={"model":model,"max_tokens":body.get("output_token_limit",1024),"messages":[{"role":"user","content":body['prompt']}]}
        endpoint="/v1/chat/completions"
    else:
        payload={"model":model,"input":body['prompt'],"max_output_tokens":body.get('output_token_limit',1024),"stream":False}
        if effort: payload["reasoning"]={"effort":effort}
        endpoint="/v1/responses"
    started=time.perf_counter()
    try:
        async with ClientSession() as session:
            async with session.post(provider.base_url+endpoint,json=payload,headers=headers,timeout=max(1,body.get("deadline_ms",60000)/1000)) as response:
                data=await response.json(content_type=None)
                if response.status >= 400: raise UpstreamFailure(str(data))
    except Exception as exc:
        raise UpstreamFailure(str(exc)) from exc
    text = data.get("output_text") or data.get("content", [{}])[0].get("text") or data.get("content", [{}])[0].get("text", "")
    return {"text":text,"actual_model":data.get("model",model),"latency_ms":round((time.perf_counter()-started)*1000,2),"raw":data}


async def route(store, tier: str, body: dict, parallel_cap: int = 3) -> dict:
    attempts=[]
    tiers=('standard','smart','expert')
    groups={}
    for candidate_tier in tiers[tiers.index(tier):]:
        for p in store.providers(candidate_tier): groups.setdefault((candidate_tier,p.price_group),[]).append(p)
    errors=[]
    for (candidate_tier,_), providers in sorted(groups.items(), key=lambda x:(tiers.index(x[0][0]),x[0][1])):
        selected=random.sample(providers,k=min(len(providers),max(1,parallel_cap)))
        tasks={asyncio.create_task(invoke(p,body)):p for p in selected}
        while tasks:
            done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                p=tasks.pop(task)
                try:
                    output=task.result()
                    if output['actual_model'] not in p.models:
                        attempts.append({'provider':p.name,'status':'model_mismatch','actual_model':output['actual_model']}); continue
                    for pending in tasks: pending.cancel()
                    await asyncio.gather(*tasks,return_exceptions=True)
                    store.observe(fingerprint=p.fingerprint,requested_model=tier,actual_model=output['actual_model'],tier=candidate_tier,effort=body.get('effort'),success=1,latency_ms=output['latency_ms'],error=None)
                    return output | {'provider':p.name,'attempts':attempts,'fulfilled_intellect':candidate_tier}
                except Exception as exc:
                    attempts.append({'provider':p.name,'status':'failed'})
                    errors.append(type(exc).__name__)
    raise UpstreamFailure("; ".join(errors) or "no eligible provider")
