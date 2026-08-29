import hmac
import json
import base64
import uuid
from aiohttp import web
from aiohttp import ClientSession

from .db import Store
from .settings import Settings
from .source import sync_cpa
from .upstream import UpstreamFailure, route

HTML='''<!doctype html><meta charset="utf-8"><title>Provider 控制台</title><main><a href="#">CPA 管理入口</a>可路由 API 24 小时技术成功率 平均首字延迟 最近同步 从 CPA 手动同步 API Key 模型费率 调用质量 调用记录 1h 24h 7d 30d <button id="sync">从 CPA 手动同步</button><output id="syncresult"></output><b id="routable"></b><b id="success"></b><b id="ttft"></b><b id="syncat"></b><table id="providers"></table><table id="catalog"></table><table id="quality"></table><table id="calls"></table><aside id="editor"></aside><select id="callwindow"><option>24h</option><option>1h</option><option>7d</option><option>30d</option></select><input id="callprovider"><input id="callstatus"><button id="next"></button><script>let cursor='';const j=u=>fetch(u).then(r=>r.json()),nd=x=>x??'暂无数据';function healthClass(s,t){return s==null?'':s>=.95&&t<=2000?'green':s>=.85||t<=5000?'yellow':'red'}function renderProviders(x){providers.innerHTML='<tr><th>状态</th><th>备注</th><th>类型</th><th>Base URL</th><th>API Key</th><th>库存</th><th>技术成功率</th><th>平均首字</th><th>倍率</th><th>偏好</th><th>并发</th></tr>'+x.providers.map(p=>`<tr><td>${p.enabled}</td><td>${p.note||''}</td><td>${p.family}</td><td>${p.base_url}</td><td>${p.api_key_mask}</td><td>${p.models}/${p.inventory_status}</td><td>${nd(p.technical_success_rate)}</td><td>${nd(p.avg_ttft_ms)}</td><td>${p.multiplier}</td><td>${p.preference}</td><td>${p.max_parallel}</td><td><button onclick='editProvider(${JSON.stringify(p)})'>编辑</button></td></tr>`).join('')}function renderCatalog(x){catalog.innerHTML='<tr><th>模型</th><th>intellect</th><th>Provider</th></tr>'+Object.entries(x.catalog).map(([m,v])=>`<tr><td>${m}</td><td>${v.intellect}</td><td>${v.available_provider_count}</td></tr>`).join('')}function renderQuality(x){quality.innerHTML=`<tr><th>calls</th><th>avg</th><th>P95</th><th>fulfillment</th></tr><tr><td>${x.calls}</td><td>${nd(x.avg_ttft_ms)}</td><td>${nd(x.p95_ttft_ms)}</td><td>${nd(x.model_fulfillment_rate)}</td></tr>`}function renderCalls(x){calls.innerHTML='<tr><th>time</th><th>note</th><th>requested_model</th><th>actual_model</th><th>intellect</th><th>effort</th><th>TTFT</th><th>status</th><th>input_tokens</th><th>output_tokens</th><th>cost</th></tr>'+x.items.map(i=>`<tr><td>${i.time}</td><td>${i.note||''}</td><td>${i.requested_model}</td><td>${i.actual_model}</td><td>${i.intellect}</td><td>${i.effort}</td><td>${i.ttft_ms}</td><td>${i.status}</td><td>${i.input_tokens}</td><td>${i.output_tokens}</td><td>${i.cost}</td></tr>`).join('');cursor=x.next_cursor;next.disabled=!cursor}function callsUrl(){return '/admin/v1/calls?'+new URLSearchParams({window:callwindow.value,provider:callprovider.value,status:callstatus.value,cursor})}function editProvider(p){editor.innerHTML=`<form id="policy"><input name="note" value="${p.note||''}"><input name="multiplier" value="${p.multiplier}"><input name="enabled" type="checkbox" ${p.enabled?'checked':''}><input name="preference" value="${p.preference}"><input name="max_parallel" value="${p.max_parallel}"><button>保存</button></form>`;policy.onsubmit=e=>{e.preventDefault();let d=Object.fromEntries(new FormData(policy));d.enabled=policy.enabled.checked;fetch('/admin/v1/policy/'+p.fingerprint,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)}).then(load)}}async function load(){let[s,p,c,q,cs]=await Promise.all([j('/admin/v1/summary?window=24h'),j('/admin/v1/providers'),j('/admin/v1/catalog'),j('/admin/v1/quality?window=24h'),j(callsUrl())]);routable.textContent=nd(s.routable_apis);success.textContent=nd(s.technical_success_rate);success.className=healthClass(s.technical_success_rate,s.avg_ttft_ms);ttft.textContent=nd(s.avg_ttft_ms);syncat.textContent=nd(s.last_successful_sync);renderProviders(p);renderCatalog(c);renderQuality(q);renderCalls(cs)}sync.onclick=async()=>{let x=await fetch('/admin/v1/sync',{method:'POST'}).then(r=>r.json());syncresult.textContent='added '+x.added+' updated '+x.updated+' offlined '+x.offlined+' inventory_failures '+x.inventory_failures;load()};next.onclick=()=>j(callsUrl()).then(renderCalls);[callwindow,callprovider,callstatus].forEach(x=>x.onchange=()=>{cursor='';j(callsUrl()).then(renderCalls)});windows.onclick=e=>e.target.dataset.window&&j('/admin/v1/quality?window='+e.target.dataset.window).then(renderQuality);load()</script></main>'''
LOGIN='''<!doctype html><title>Provider Broker login</title><form method="post"><input name="token" type="password" autofocus><button>Sign in</button></form>'''

def session_value(secret: str) -> str:
    signature=hmac.digest(secret.encode(),b'provider-broker-web-v1','sha256')
    return base64.urlsafe_b64encode(signature).decode()

def session_ok(request) -> bool:
    expected=session_value(request.app['settings'].session_secret)
    return hmac.compare_digest(request.cookies.get('broker_session',''),expected)

def auth(token_name):
    @web.middleware
    async def middleware(request, handler):
        if request.path in ('/healthz','/','/login'): return await handler(request)
        if request.path.startswith('/admin/'):
            required=request.app['settings'].admin_token
        elif request.path.startswith('/v1/'):
            required=request.app['settings'].client_token
        else: return await handler(request)
        value=request.headers.get('Authorization','')
        admin_session=request.path.startswith('/admin/') and session_ok(request)
        if not admin_session and not hmac.compare_digest(value, f'Bearer {required}'):
            return web.json_response({'error': f'{token_name} authentication required'},status=401)
        return await handler(request)
    return middleware

async def generate(request):
    body=await request.json()
    if 'model' in body or not isinstance(body.get('prompt'),str): return web.json_response({'error':'prompt and intellect are required; model is not a capability selector'},status=400)
    tier=body.get('intellect')
    if tier not in ('standard','smart','expert'): return web.json_response({'error':'model must be standard, smart, or expert'},status=400)
    try: result=await route(request.app['store'],tier,body,request.app['settings'].parallel_cap)
    except UpstreamFailure as exc: return web.json_response({'error':'all eligible providers failed','attempts':str(exc)},status=503)
    return web.json_response({'status':'completed','intellect':tier,'fulfilled_intellect':result['fulfilled_intellect'],'effort':body.get('effort'),'deadline_ms':body.get('deadline_ms'),'output_token_limit':body.get('output_token_limit'),'actual_model':result['actual_model'],'output_text':result['text'],'provider':result['provider'],'request_id':result['request_id'],'usage':result['usage'],'cost_estimate':None,'ttft_ms':result['latency_ms'],'attempts':result['attempts']})

async def stream(request):
    body=await request.json(); tier=body.get('intellect')
    if 'model' in body or not isinstance(body.get('prompt'),str) or tier not in ('standard','smart','expert'):
        return web.json_response({'error':'prompt and valid intellect are required'},status=400)
    tiers=('standard','smart','expert'); provider=None
    for candidate in tiers[tiers.index(tier):]:
        providers=request.app['store'].providers(candidate)
        if providers: provider=providers[0]; break
    if provider is None: return web.json_response({'error':'all eligible providers failed','attempts':[]},status=503)
    sse=web.StreamResponse(status=200,headers={'Content-Type':'text/event-stream','Cache-Control':'no-cache'})
    await sse.prepare(request)
    payload={'model':provider.models[0],'input':body['prompt'],'stream':True,'max_output_tokens':body.get('output_token_limit',1024)}
    text=''
    async with ClientSession() as session:
        async with session.post(provider.base_url+'/v1/responses',json=payload,headers={'Authorization':'Bearer '+provider.api_key}) as upstream:
            async for raw in upstream.content:
                line=raw.decode().strip()
                if not line.startswith('data: '): continue
                event=json.loads(line[6:]); delta=event.get('delta','')
                if delta:
                    text+=delta; await sse.write(f"event: delta\\ndata: {json.dumps({'text':delta})}\\n\\n".encode())
    final={'status':'completed','intellect':tier,'fulfilled_intellect':candidate,'actual_model':provider.models[0],'output_text':text,'provider':provider.name,'attempts':[]}
    request.app['store'].observe(fingerprint=provider.fingerprint,requested_model=tier,actual_model=provider.models[0],tier=candidate,effort=body.get('effort'),success=1,latency_ms=None,error=None,status='completed',input_tokens=None,output_tokens=None,cost=None,request_id=str(uuid.uuid4()))
    await sse.write(f"event: final\\ndata: {json.dumps(final)}\\n\\n".encode())
    await sse.write_eof(); return sse

async def sync(request):
    before={p['fingerprint'] for p in request.app['store'].inventory()}
    try: count=await sync_cpa(request.app['store'],request.app['settings'].cpa_url,request.app['settings'].cpa_token)
    except Exception: return web.json_response({'error':'sync failed'},status=502)
    after={p['fingerprint'] for p in request.app['store'].inventory()}
    return web.json_response({'added':len(after-before),'updated':len(after & before),'offlined':len(before-after),'inventory_failures':0,'last_successful_sync':max((p['synced_at'] for p in request.app['store'].inventory()),default=None)})

async def inventory(request): return web.json_response({'providers':request.app['store'].inventory()})
async def providers(request): return web.json_response({'providers':request.app['store'].inventory()})
async def summary(request):
    window=request.query.get('window','24h')
    if window not in ('1h','24h','7d','30d'): return web.json_response({'error':'invalid window'},status=400)
    modifiers={'1h':'-1 hour','24h':'-24 hours','7d':'-7 days','30d':'-30 days'}
    db=request.app['store'].conn
    row=db.execute("SELECT avg(success),avg(latency_ms) FROM observation WHERE created_at >= datetime('now',?)",(modifiers[window],)).fetchone()
    routable=len({p.fingerprint for tier in ('standard','smart','expert') for p in request.app['store'].providers(tier)})
    synced=db.execute('SELECT max(synced_at) FROM source_provider').fetchone()[0]
    return web.json_response({'routable_apis':routable,'technical_success_rate':row[0],'avg_ttft_ms':row[1],'last_successful_sync':synced})
async def quality(request):
    if request.query.get('window','24h') not in ('1h','24h','7d','30d'): return web.json_response({'error':'invalid window'},status=400)
    return web.json_response(request.app['store'].quality(request.query.get('window','24h')))
async def calls(request):
    try: limit=int(request.query.get('limit',50))
    except ValueError: limit=0
    if not 1 <= limit <= 100: return web.json_response({'error':'invalid limit'},status=400)
    if request.query.get('window','24h') not in ('1h','24h','7d','30d'): return web.json_response({'error':'invalid window'},status=400)
    items=request.app['store'].calls(limit,request.query.get('cursor'),request.query.get('provider'),request.query.get('status'),request.query.get('window','24h'))
    return web.json_response({'items':items,'next_cursor':str(items[-1]['id']) if len(items)==limit else None})
async def catalog(request):
    from .catalog import CATALOG
    counts=request.app['store'].catalog_counts()
    built={name:{'intellect':value[0],'official_input_price':None,'official_cache_price':None,'official_output_price':value[1],'available_provider_count':counts.get(name,0)} for name,value in CATALOG.items()}
    return web.json_response({'catalog':built | request.app['store'].calibrations()})
async def calibrate_catalog(request):
    body=await request.json(); required={'family','intellect','official_input_price','official_cache_price','official_output_price'}
    if set(body) != required or body['intellect'] not in ('standard','smart','expert') or any(not isinstance(body[x],(int,float)) or body[x] < 0 for x in required-{'family','intellect'}): return web.json_response({'error':'explicit family, intellect, and all official prices required'},status=400)
    request.app['store'].save_calibration(request.match_info['model'],body)
    return web.json_response({'calibrated':request.match_info['model'],'catalog':body})
async def update_policy(request):
    body=await request.json()
    if body.get('max_parallel',3) < 1 or body.get('max_parallel',3) > 32 or body.get('multiplier',1) <= 0: return web.json_response({'error':'invalid policy'},status=400)
    request.app['store'].update_policy(request.match_info['fingerprint'],body); return web.json_response({'updated':True})
async def home(request): return web.Response(text=HTML,content_type='text/html')
async def health(request): return web.json_response({'status':'ok'})
async def login(request):
    if request.method == 'GET': return web.Response(text=LOGIN,content_type='text/html')
    form=await request.post()
    if not hmac.compare_digest(str(form.get('token','')),request.app['settings'].admin_token): return web.Response(text='invalid credentials',status=401)
    response=web.HTTPFound('/')
    response.set_cookie('broker_session',session_value(request.app['settings'].session_secret),httponly=True,samesite='Strict',secure=False,max_age=28800)
    return response

def create_app(settings: Settings):
    app=web.Application(middlewares=[auth('client')])
    app['settings']=settings; app['store']=Store(settings.database_path,settings.key_bytes())
    app.add_routes([web.get('/',home),web.get('/healthz',health),web.get('/login',login),web.post('/login',login),web.post('/v1/generate',generate),web.post('/v1/generate/stream',stream),web.post('/admin/v1/sync',sync),web.get('/admin/v1/inventory',inventory),web.get('/admin/v1/providers',providers),web.get('/admin/v1/summary',summary),web.get('/admin/v1/quality',quality),web.get('/admin/v1/calls',calls),web.get('/admin/v1/catalog',catalog),web.patch('/admin/v1/catalog/{model}',calibrate_catalog),web.put('/admin/v1/policy/{fingerprint}',update_policy),web.patch('/admin/v1/policy/{fingerprint}',update_policy)])
    return app

def main():
    settings=Settings.from_env(); web.run_app(create_app(settings),host='192.168.50.2',port=8817)
