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
        return web.json_response({'providers':[{'name':'Test OpenAI','base_url':str(request.app['upstream']),'type':'openai','keys':[{'key':'provider-secret','models':['gpt-test']}]}]})
    app=web.Application(); app.router.add_get('/v0/management/config',config)
    async def response(request): return web.json_response({'model':'gpt-fulfilled','output_text':'hello broker'})
    upstream=web.Application(); upstream.router.add_post('/v1/responses',response)
    upstream_server=TestServer(upstream); await upstream_server.start_server(); app['upstream']=str(upstream_server.make_url('')).rstrip('/')
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
    assert provider['models'] == ['gpt-test']
    assert 'provider-secret' not in str(provider)
    result=await client.post('/v1/generate',headers={'Authorization':'Bearer client-secret'},json={'model':'smart','input':'hi','effort':'medium'})
    assert await result.json() == {'model':'smart','actual_model':'gpt-fulfilled','output_text':'hello broker','provider':'Test OpenAI'}
    streamed=await client.post('/v1/generate/stream',headers={'Authorization':'Bearer client-secret'},json={'model':'expert','input':'hi'})
    assert streamed.headers['Content-Type'].startswith('text/event-stream')
    assert 'gpt-fulfilled' in await streamed.text()


async def test_web_login_uses_session_for_admin_sync(client):
    response=await client.post('/login',data={'token':'admin-secret'},allow_redirects=False)
    assert response.status == 302
    response=await client.get('/',headers={'Cookie':response.headers['Set-Cookie']})
    assert response.status == 200
