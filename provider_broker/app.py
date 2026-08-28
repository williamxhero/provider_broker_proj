import hmac
import json
import base64
from aiohttp import web

from .db import Store
from .settings import Settings
from .source import sync_cpa
from .upstream import UpstreamFailure, route

HTML='''<!doctype html><title>Provider Broker</title><h1>Provider Broker</h1><p>CPA is read-only. <a href="http://yosef-server:8317/" target="_blank" rel="noreferrer">Open CPA Manager</a></p><button id="sync">Sync CPA</button><button id="inventory">Routing / catalog / inventory / quality</button><pre id="out"></pre><script>const o=document.querySelector('#out');document.querySelector('#sync').onclick=async()=>{let r=await fetch('/admin/v1/sync',{method:'POST'});o.textContent=await r.text()};document.querySelector('#inventory').onclick=async()=>{let r=await fetch('/admin/v1/inventory');o.textContent=JSON.stringify(await r.json(),null,2)}</script>'''
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
    return web.json_response({'status':'fulfilled','intellect':tier,'fulfilled_intellect':result['fulfilled_intellect'],'effort':body.get('effort'),'deadline_ms':body.get('deadline_ms'),'output_token_limit':body.get('output_token_limit'),'actual_model':result['actual_model'],'output_text':result['text'],'provider':result['provider'],'attempts':result['attempts']})

async def stream(request):
    response=await generate(request)
    if response.status != 200: return response
    data=json.loads(response.text)
    sse=web.StreamResponse(status=200,headers={'Content-Type':'text/event-stream','Cache-Control':'no-cache'})
    await sse.prepare(request)
    await sse.write(f"event: result\\ndata: {json.dumps(data)}\\n\\n".encode())
    await sse.write(b"event: done\\ndata: {}\\n\\n")
    await sse.write_eof(); return sse

async def sync(request):
    try: count=await sync_cpa(request.app['store'],request.app['settings'].cpa_url,request.app['settings'].cpa_token)
    except Exception as exc: return web.json_response({'error':'CPA sync failed','detail':str(exc)},status=502)
    return web.json_response({'synced':count})

async def inventory(request): return web.json_response({'providers':request.app['store'].inventory()})
async def catalog(request):
    from .catalog import CATALOG
    return web.json_response({'catalog':CATALOG})
async def update_policy(request):
    request.app['store'].update_policy(request.match_info['fingerprint'],await request.json()); return web.json_response({'updated':True})
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
    app.add_routes([web.get('/',home),web.get('/healthz',health),web.get('/login',login),web.post('/login',login),web.post('/v1/generate',generate),web.post('/v1/generate/stream',stream),web.post('/admin/v1/sync',sync),web.get('/admin/v1/inventory',inventory),web.get('/admin/v1/catalog',catalog),web.put('/admin/v1/policy/{fingerprint}',update_policy)])
    return app

def main():
    settings=Settings.from_env(); web.run_app(create_app(settings),host='192.168.50.2',port=8817)
