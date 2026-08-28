import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from provider_broker.app import create_app
from provider_broker.settings import Settings


@pytest.fixture
async def client(tmp_path):
    app = create_app(
        Settings(
            database_path=tmp_path / "broker.sqlite3",
            client_token="client-secret",
            admin_token="admin-secret",
            session_secret="session-secret",
            encryption_key="MDEyMzQ1Njc4OWFiY2RlZg==",
        )
    )
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    yield client
    await client.close()


async def test_generate_requires_client_bearer(client):
    response = await client.post("/v1/generate", json={"model": "standard"})
    assert response.status == 401
    assert await response.json() == {"error": "client authentication required"}


@pytest.fixture
async def cpa(client):
    async def config(request):
        assert request.headers['X-Management-Key'] == 'cpa-secret'
        return web.json_response(request.app['config'])
    app=web.Application(); app['config']={'providers':[{'name':'Test OpenAI','base_url':str('http://placeholder'),'type':'openai','keys':[{'key':'provider-secret','models':['gpt-test']}]}]}; app.router.add_get('/v0/management/config',config)
    async def response(request): return web.json_response({'model':'gpt-5.6-luna','output_text':'hello broker'})
    upstream=web.Application(); upstream.router.add_post('/v1/responses',response)
    async def models(request): return web.json_response({'data':[{'id':'gpt-5.6-luna'}]})
    upstream.router.add_get('/models',models)
    upstream_server=TestServer(upstream); await upstream_server.start_server(); app['upstream']=str(upstream_server.make_url('')).rstrip('/'); app['config']['providers'][0]['base_url']=app['upstream']
    server=TestServer(app); await server.start_server()
    client.app['settings'] = client.app['settings'].__class__(**(client.app['settings'].__dict__ | {'cpa_url':str(server.make_url('')).rstrip('/'),'cpa_token':'cpa-secret'}))
    yield server
    await server.close(); await upstream_server.close()


async def test_manual_sync_then_generate_and_stream(client, cpa):
    headers={'Authorization':'Bearer admin-secret'}
    response=await client.post('/admin/v1/sync',headers=headers)
    assert response.status == 200
    assert await response.json() == {'synced':1}
    inventory=await client.get('/admin/v1/inventory',headers=headers)
    provider=(await inventory.json())['providers'][0]
    assert provider['models'] == ['gpt-5.6-luna']
    assert provider['inventory_status'] == 'available'
    assert 'provider-secret' not in str(provider)
    await client.put('/admin/v1/policy/'+provider['fingerprint'],headers=headers,json={'calibrated':True,'tiers':['standard','smart','expert']})
    updated=await client.get('/admin/v1/inventory',headers=headers)
    assert (await updated.json())['providers'][0]['calibrated'] is True
    result=await client.post('/v1/generate',headers={'Authorization':'Bearer client-secret'},json={'prompt':'hi','intellect':'standard','effort':'medium'})
    assert (await result.json())['actual_model'] == 'gpt-5.6-luna'
    streamed=await client.post('/v1/generate/stream',headers={'Authorization':'Bearer client-secret'},json={'prompt':'hi','intellect':'standard'})
    assert streamed.headers['Content-Type'].startswith('text/event-stream')
    assert 'gpt-5.6-luna' in await streamed.text()


async def test_web_login_uses_session_for_admin_sync(client):
    response=await client.post('/login',data={'token':'admin-secret'},allow_redirects=False)
    assert response.status == 302
    response=await client.get('/',headers={'Cookie':response.headers['Set-Cookie']})
    assert response.status == 200


async def test_sync_normalizes_cpa_sections_and_generate_uses_canonical_request(client, cpa):
    cpa.app['config'] = {
            'codex-api-key': [{'name':'Luna key','base_url':cpa.app['upstream'],'api_key':'luna-key'}],
            'claude-api-key': [{'name':'Sonnet key','base_url':cpa.app['upstream'],'api_key':'claude-key'}],
            'openai-compatibility': [{'name':'compat','base_url':cpa.app['upstream'],'api_key':'compat-key'}],
        }
    synced=await client.post('/admin/v1/sync',headers={'Authorization':'Bearer admin-secret'})
    assert synced.status == 200
    inventory=(await (await client.get('/admin/v1/inventory',headers={'Authorization':'Bearer admin-secret'})).json())['providers']
    assert {p['family'] for p in inventory} == {'codex','anthropic','openai'}
    assert all(secret not in str(inventory) for secret in ('luna-key','claude-key','compat-key'))
    response=await client.post('/v1/generate',headers={'Authorization':'Bearer client-secret'},json={'prompt':'hello','intellect':'standard','effort':'high','deadline_ms':1000,'output_token_limit':20})
    assert response.status == 503  # providers require policy calibration before routing
    legacy=await client.post('/v1/generate',headers={'Authorization':'Bearer client-secret'},json={'prompt':'hello','model':'standard'})
    assert legacy.status == 400


async def test_catalog_is_explicit_and_unknown_models_are_not_priced(client):
    response=await client.get('/admin/v1/catalog',headers={'Authorization':'Bearer admin-secret'})
    catalog=(await response.json())['catalog']
    assert catalog['luna'][0] == 'standard'
    assert catalog['terra'][0] == 'smart'
    assert catalog['sol'][0] == 'expert'
