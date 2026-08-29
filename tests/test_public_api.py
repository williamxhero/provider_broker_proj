import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from provider_broker.app import create_app
from provider_broker.db import Store
from provider_broker.settings import Settings
from provider_broker.upstream import price_bands


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


async def test_console_and_management_api_are_available_without_login(client):
    response = await client.get('/')
    assert response.status == 200
    assert 'Provider' in await response.text()
    assert (await client.get('/admin/v1/summary')).status == 200
    assert (await client.get('/login')).status == 404
    assert (await client.post('/v1/generate', json={'prompt': 'no token', 'intellect': 'standard'})).status == 401


@pytest.fixture
async def cpa(client):
    async def config(request):
        assert request.headers['X-Management-Key'] == 'cpa-secret'
        return web.json_response(request.app['config'])
    app=web.Application(); app['config']={'providers':[{'name':'Test OpenAI','base_url':str('http://placeholder'),'type':'openai','keys':[{'key':'provider-secret','models':['gpt-test']}]}]}; app.router.add_get('/v0/management/config',config)
    async def response(request):
        request.app['last_response_headers'] = dict(request.headers)
        payload=await request.json()
        assert 'tools' not in payload
        if payload.get('input') == 'hold':
            request.app['hold_started'].set()
            await request.app['hold_release'].wait()
        if payload.get('stream'):
            stream=web.StreamResponse(headers={'Content-Type':'text/event-stream'}); await stream.prepare(request)
            await stream.write(b'data: {"type":"response.output_text.delta","delta":"upstream-one"}\n\n')
            await stream.write(b'data: {"type":"response.output_text.delta","delta":"upstream-two"}\n\n')
            await stream.write(b'data: {"type":"response.completed","response":{"id":"req-stream","model":"gpt-5.6-luna","usage":{"input_tokens":3,"output_tokens":2}}}\n\n')
            await stream.write_eof(); return stream
        if payload.get('input') == 'empty':
            return web.json_response({'id':'req-empty','model':'gpt-5.6-luna','output':[],'usage':{}})
        if payload.get('input') == 'fail-secret':
            return web.json_response({'error':'provider-secret must never escape'},status=500)
        if payload.get('input') == 'mismatch':
            return web.json_response({'id':'req-mismatch','model':'gpt-5.6-terra','output_text':'complete but wrong model','usage':{'input_tokens':2,'output_tokens':3}})
        return web.json_response({'id':'req-test','model':payload.get('model','gpt-5.6-luna'),'output':[{'type':'message','content':[{'type':'output_text','text':'hello broker'}]}],'usage':{'output_tokens':2}})
    upstream=web.Application(); app['upstream_app']=upstream; upstream.router.add_post('/v1/responses',response)
    async def chat(request):
        payload=await request.json(); assert 'tools' not in payload
        return web.json_response({'id':'req-chat','model':'gpt-5.6-luna','choices':[{'message':{'content':'hello chat'}}],'usage':{'output_tokens':2}})
    upstream.router.add_post('/v1/chat/completions',chat)
    async def models(request): return web.json_response({'data':[{'id': model} for model in request.app.get('models', ['gpt-5.6-luna'])]})
    upstream.router.add_get('/models',models)
    upstream.router.add_get('/v1/models',models)
    upstream_server=TestServer(upstream); await upstream_server.start_server(); app['upstream']=str(upstream_server.make_url('')).rstrip('/'); app['config']['providers'][0]['base_url']=app['upstream']
    server=TestServer(app); await server.start_server()
    client.app['settings'] = client.app['settings'].__class__(**(client.app['settings'].__dict__ | {'cpa_url':str(server.make_url('')).rstrip('/'),'cpa_token':'cpa-secret'}))
    yield server
    await server.close(); await upstream_server.close()


async def test_manual_sync_then_generate_and_stream(client, cpa):
    headers={'Authorization':'Bearer admin-secret'}
    response=await client.post('/admin/v1/sync',headers=headers)
    assert response.status == 200
    assert (await response.json())['added'] == 1
    inventory=await client.get('/admin/v1/inventory',headers=headers)
    provider=(await inventory.json())['providers'][0]
    assert provider['models'] == ['gpt-5.6-luna']
    assert provider['inventory_status'] == 'available'
    assert 'provider-secret' not in str(provider)
    await client.put('/admin/v1/policy/'+provider['fingerprint'],headers=headers,json={'calibrated':True,'tiers':['standard','smart','expert']})
    updated=await client.get('/admin/v1/inventory',headers=headers)
    assert (await updated.json())['providers'][0]['calibrated'] is True
    result=await client.post('/v1/generate',headers={'Authorization':'Bearer client-secret'},json={'prompt':'hi','intellect':'standard','effort':'medium'})
    result_body=await result.json()
    assert result_body['actual_model'] == 'gpt-5.6-luna'
    assert result_body['request_id'] == 'req-test'
    assert result_body['usage'] == {'output_tokens':2}
    assert result_body['ttft_ms'] >= 0
    audit=(await (await client.get('/admin/v1/calls?limit=1',headers=headers)).json())['items'][0]
    assert audit['status']=='completed' and audit['intellect']=='standard' and audit['effort']=='medium'
    assert audit['output_tokens']==2 and audit['request_id']=='req-test' and audit['cost'] is None
    assert 'hi' not in str(audit) and 'provider-secret' not in str(audit)
    streamed=await client.post('/v1/generate/stream',headers={'Authorization':'Bearer client-secret'},json={'prompt':'hi','intellect':'standard'})
    assert streamed.headers['Content-Type'].startswith('text/event-stream')
    assert 'gpt-5.6-luna' in await streamed.text()


async def test_cost_rollups_are_exposed_for_keys_and_quality_window(client, cpa):
    await client.post('/admin/v1/sync', headers={'Authorization':'Bearer admin-secret'})
    provider = (await (await client.get('/admin/v1/providers')).json())['providers'][0]
    client.app['store'].observe(
        fingerprint=provider['fingerprint'], requested_model='gpt-5.6-luna', actual_model='gpt-5.6-luna',
        tier='standard', effort='medium', success=1, latency_ms=100, error=None, status='completed',
        input_tokens=100, output_tokens=50, cost=.125, request_id='cost-rollup',
    )

    inventory = (await (await client.get('/admin/v1/providers')).json())['providers']
    quality = await (await client.get('/admin/v1/quality?window=1h')).json()

    assert inventory[0]['cost_24h'] == .125
    assert quality['total_cost'] == .125
    assert (await client.get('/admin/v1/calls?sort=cost:asc')).status == 200
    assert (await client.get('/admin/v1/calls?sort=unknown:asc')).status == 400


async def test_stream_emits_delta_before_final(client, cpa):
    response=await client.post('/admin/v1/sync',headers={'Authorization':'Bearer admin-secret'})
    provider=(await (await client.get('/admin/v1/inventory',headers={'Authorization':'Bearer admin-secret'})).json())['providers'][0]
    await client.put('/admin/v1/policy/'+provider['fingerprint'],headers={'Authorization':'Bearer admin-secret'},json={'calibrated':True})
    streamed=await client.post('/v1/generate/stream',headers={'Authorization':'Bearer client-secret'},json={'prompt':'stream','intellect':'standard'})
    body=await streamed.text()
    assert 'event: delta' in body
    assert 'event: final' in body
    assert body.index('event: delta') < body.index('event: final')
    assert 'upstream-one' in body
    audit=(await (await client.get('/admin/v1/calls?limit=1',headers={'Authorization':'Bearer admin-secret'})).json())['items'][0]
    assert audit['status']=='completed' and audit['request_id']=='req-stream' and audit['actual_model']=='gpt-5.6-luna'
    assert audit['input_tokens']==3 and audit['output_tokens']==2


async def test_generate_parses_chat_completions_without_native_tools(client, cpa):
    cpa.app['config']={'claude-api-key':[{'name':'Claude compatible','base_url':cpa.app['upstream'],'api_key':'claude-secret'}]}
    headers={'Authorization':'Bearer admin-secret'}
    assert (await client.post('/admin/v1/sync',headers=headers)).status == 200
    result=await client.post('/v1/generate',headers={'Authorization':'Bearer client-secret'},json={'prompt':'chat','intellect':'standard','effort':'low'})
    body=await result.json()
    assert result.status == 200 and body['output_text'] == 'hello chat' and body['request_id'] == 'req-chat'


async def test_generate_classifies_and_sanitizes_upstream_failures(client, cpa):
    headers={'Authorization':'Bearer admin-secret'}
    await client.post('/admin/v1/sync',headers=headers)
    failed=await client.post('/v1/generate',headers={'Authorization':'Bearer client-secret'},json={'prompt':'fail-secret','intellect':'standard'})
    body=await failed.json()
    assert failed.status == 503
    assert body == {'error':'all eligible providers failed','attempts':[{'provider':'Test OpenAI','status':'unavailable'}]}
    assert 'provider-secret' not in str(body)
    audit=(await (await client.get('/admin/v1/calls?limit=1',headers=headers)).json())['items'][0]
    assert audit['status'] == 'unavailable'
    empty=await client.post('/v1/generate',headers={'Authorization':'Bearer client-secret'},json={'prompt':'empty','intellect':'standard'})
    assert empty.status == 503
    empty_audit=(await (await client.get('/admin/v1/calls?limit=1',headers=headers)).json())['items'][0]
    assert empty_audit['status'] == 'protocol_failed'


async def test_model_mismatch_is_completed_but_blocked_until_manual_sync(client, cpa):
    headers={'Authorization':'Bearer admin-secret'}
    await client.post('/admin/v1/sync',headers=headers)
    request={'prompt':'mismatch','intellect':'standard'}
    first=await client.post('/v1/generate',headers={'Authorization':'Bearer client-secret'},json=request)
    assert first.status == 503
    audit=(await (await client.get('/admin/v1/calls?limit=10',headers=headers)).json())['items']
    assert len(audit)==1 and audit[0]['status']=='completed'
    assert audit[0]['requested_model']=='gpt-5.6-luna' and audit[0]['actual_model']=='gpt-5.6-terra'
    second=await client.post('/v1/generate',headers={'Authorization':'Bearer client-secret'},json=request)
    assert second.status == 503 and (await second.json())['attempts'] == []
    await client.post('/admin/v1/sync',headers=headers)
    third=await client.post('/v1/generate',headers={'Authorization':'Bearer client-secret'},json=request)
    assert third.status == 503
    assert len((await (await client.get('/admin/v1/calls?limit=10',headers=headers)).json())['items']) == 2


async def test_web_console_is_direct_and_management_api_needs_no_session(client):
    response=await client.get('/')
    assert response.status == 200
    page=await response.text()
    assert 'href="/static/styles.css"' in page
    assert 'src="/static/app.js"' in page
    for label in ('可路由 API','24 小时技术成功率','平均首字延迟','最近同步','从 CPA 手动同步','API Key','模型视角','模型费率','调用质量','调用记录','1h','24h','7d','30d'):
        assert label in page
    assert 'client-secret' not in page and 'admin-secret' not in page
    css=await client.get('/static/styles.css')
    js=await client.get('/static/app.js')
    assert css.status == 200 and css.content_type == 'text/css'
    assert js.status == 200 and js.content_type in ('application/javascript','text/javascript')
    script=await js.text()
    for token in ('renderQuality','renderCalls','renderModelView','sortItems','/admin/v1/sync','/admin/v1/routing','callsUrl','formatShanghaiTime','n/a'):
        assert token in script
    assert (await client.get('/admin/v1/summary')).status == 200
    assert (await client.get('/admin/v1/providers')).status == 200
    assert (await client.get('/admin/v1/routing')).status == 200
    assert (await client.get('/login')).status == 404


async def test_sync_normalizes_cpa_sections_and_generate_uses_canonical_request(client, cpa):
    cpa.app['config'] = {
            'codex-api-key': [{'name':'Luna key','base_url':cpa.app['upstream'],'api_key':'luna-key','models':[{'name':'gpt-5.6-terra','alias':'gpt-5.6-luna'}]}],
            'claude-api-key': [{'name':'Sonnet key','base_url':cpa.app['upstream'],'api_key':'claude-key'}],
            'openai-compatibility': [{'name':'compat','base_url':cpa.app['upstream'],'api_key':'compat-key'}],
        }
    synced=await client.post('/admin/v1/sync',headers={'Authorization':'Bearer admin-secret'})
    assert synced.status == 200
    inventory=(await (await client.get('/admin/v1/inventory',headers={'Authorization':'Bearer admin-secret'})).json())['providers']
    assert {p['family'] for p in inventory} == {'codex','anthropic','openai'}
    codex = next(p for p in inventory if p['family']=='codex')
    assert codex['models'] == ['gpt-5.6-terra']
    assert codex['note'] == 'Luna key'
    assert all(secret not in str(inventory) for secret in ('luna-key','claude-key','compat-key'))
    response=await client.post('/v1/generate',headers={'Authorization':'Bearer client-secret'},json={'prompt':'hello','intellect':'standard','effort':'high','deadline_ms':1000,'output_token_limit':20})
    assert response.status == 200  # fixed official catalog auto-calibrates known inventory
    legacy=await client.post('/v1/generate',headers={'Authorization':'Bearer client-secret'},json={'prompt':'hello','model':'standard'})
    assert legacy.status == 400


async def test_codex_sync_uses_cpa_headers_and_v1_base_url_for_responses(client, cpa):
    cpa.app['config'] = {
        'codex-header-defaults': {'user-agent': 'CPA Codex', 'beta-features': 'responses=1'},
        'codex-api-key': [{
            'name': 'Codex direct', 'base-url': cpa.app['upstream'] + '/v1', 'api-key': 'codex-secret',
            'models': [{'name': 'gpt-5.6-terra', 'alias': 'gpt-5.6-luna'}],
        }],
    }
    headers = {'Authorization': 'Bearer admin-secret'}
    assert (await client.post('/admin/v1/sync', headers=headers)).status == 200

    response = await client.post(
        '/v1/generate', headers={'Authorization': 'Bearer client-secret'},
        json={'prompt': 'headers', 'intellect': 'smart', 'effort': 'medium'},
    )

    assert response.status == 200
    assert (await response.json())['actual_model'] == 'gpt-5.6-terra'
    observed = {name.lower(): value for name, value in cpa.app['upstream_app']['last_response_headers'].items()}
    assert observed['user-agent'] == 'CPA Codex'
    assert observed['beta-features'] == 'responses=1'


async def test_catalog_is_explicit_and_unknown_models_are_not_priced(client):
    response=await client.get('/admin/v1/catalog',headers={'Authorization':'Bearer admin-secret'})
    catalog=(await response.json())['catalog']
    assert catalog['gpt-5.6-luna'] == {'family':'OpenAI GPT-5.6','intellect':'standard','official_input_price':.2,'official_cache_price':.02,'official_output_price':1.2,'blended_price':.9712,'available_provider_count':0}
    assert catalog['gpt-5.6-terra']['intellect'] == 'smart'
    assert catalog['gpt-5.6-sol']['intellect'] == 'expert'
    assert catalog['claude-opus-5']['official_output_price'] == 25
    assert catalog['claude-opus-4-8']['intellect'] == 'expert'
    assert catalog['claude-sonnet-5']['intellect'] == 'smart'
    assert 'luna' not in catalog and 'private-model' not in catalog


async def test_catalog_can_create_a_model_with_an_explicit_stage(client):
    body = {
        'model': 'gpt-custom-route', 'family': 'Internal GPT', 'intellect': 'smart',
        'official_input_price': 1.0, 'official_cache_price': 0.1, 'official_output_price': 3.0,
    }
    created = await client.post('/admin/v1/catalog', json=body)

    assert created.status == 201
    catalog = (await (await client.get('/admin/v1/catalog')).json())['catalog']
    assert catalog['gpt-custom-route'] == {key: value for key, value in body.items() if key != 'model'} | {'blended_price': 2.456, 'available_provider_count': 0}


async def test_catalog_stage_update_and_delete_control_routing(client, cpa):
    await client.post('/admin/v1/sync')
    catalog = (await (await client.get('/admin/v1/catalog')).json())['catalog']
    changed = catalog['gpt-5.6-luna'] | {'intellect': 'smart'}
    changed.pop('available_provider_count')
    changed.pop('blended_price')

    assert (await client.put('/admin/v1/catalog/gpt-5.6-luna', json=changed)).status == 200
    generated = await client.post('/v1/generate', headers={'Authorization': 'Bearer client-secret'}, json={'prompt': 'stage', 'intellect': 'standard'})
    assert generated.status == 200
    assert (await generated.json())['fulfilled_intellect'] == 'smart'

    assert (await client.delete('/admin/v1/catalog/gpt-5.6-luna')).status == 204
    assert 'gpt-5.6-luna' not in (await (await client.get('/admin/v1/catalog')).json())['catalog']
    unavailable = await client.post('/v1/generate', headers={'Authorization': 'Bearer client-secret'}, json={'prompt': 'deleted', 'intellect': 'standard'})
    assert unavailable.status == 503


async def test_catalog_rejects_incomplete_and_unknown_updates(client):
    bad=await client.patch('/admin/v1/catalog/private-model',json={'family':'x','intellect':'expert'})
    assert bad.status == 400
    body={'family':'private','intellect':'expert','official_input_price':1.0,'official_cache_price':.1,'official_output_price':2.0}
    assert (await client.patch('/admin/v1/catalog/private-model',json=body)).status == 404


async def test_catalog_deletions_survive_a_store_restart(client):
    assert (await client.delete('/admin/v1/catalog/gpt-5.5')).status == 204

    reopened = Store(client.app['settings'].database_path, client.app['settings'].key_bytes())

    try:
        assert 'gpt-5.5' not in reopened.catalog()
    finally:
        reopened.conn.close()


async def test_catalog_provider_count_reflects_synced_inventory(client, cpa):
    headers={'Authorization':'Bearer admin-secret'}
    before=(await (await client.get('/admin/v1/catalog',headers=headers)).json())['catalog']
    assert before['gpt-5.6-luna']['available_provider_count'] == 0
    await client.post('/admin/v1/sync',headers=headers)
    after=(await (await client.get('/admin/v1/catalog',headers=headers)).json())['catalog']
    assert after['gpt-5.6-luna']['available_provider_count'] == 1


async def test_catalog_apply_prunes_existing_inventory_and_future_sync(client, cpa):
    headers = {'Authorization': 'Bearer admin-secret'}
    cpa.app['upstream_app']['models'] = ['gpt-5.6-luna', 'private-model']
    await client.post('/admin/v1/sync', headers=headers)
    store = client.app['store']
    provider = (await (await client.get('/admin/v1/inventory', headers=headers)).json())['providers'][0]
    assert (await client.patch(f"/admin/v1/policy/{provider['fingerprint']}", headers=headers, json={'note': 'preserved', 'multiplier': .45})).status == 200
    with store.conn:
        store.conn.execute("UPDATE source_provider SET models_json=?", (json.dumps(['gpt-5.6-luna', 'private-model']),))

    applied = await client.post('/admin/v1/catalog/apply', headers=headers)

    assert await applied.json() == {'providers': 1, 'removed_models': 1, 'retained_models': 1}
    assert (await (await client.get('/admin/v1/inventory', headers=headers)).json())['providers'][0]['models'] == ['gpt-5.6-luna']
    cpa.app['config'] = {'codex-api-key': [{'base_url': cpa.app['upstream'], 'api_key': 'provider-secret'}]}
    assert (await client.post('/admin/v1/sync', headers=headers)).status == 200
    refreshed = (await (await client.get('/admin/v1/inventory', headers=headers)).json())['providers'][0]
    assert refreshed['models'] == ['gpt-5.6-luna']
    assert (refreshed['note'], refreshed['multiplier']) == ('preserved', .45)


async def test_summary_and_provider_stats_use_mixed_observations(client, cpa):
    headers={'Authorization':'Bearer admin-secret'}; await client.post('/admin/v1/sync',headers=headers)
    provider=(await (await client.get('/admin/v1/providers',headers=headers)).json())['providers'][0]
    await client.patch('/admin/v1/policy/'+provider['fingerprint'],headers=headers,json={'calibrated':True})
    db=client.app['store'].conn
    with db:
        for success,ttft,status in [(1,100,'completed'),(0,300,'transport_failed')]:
            db.execute("INSERT INTO observation(fingerprint,requested_model,actual_model,tier,effort,success,latency_ms,error,status) VALUES(?,?,?,?,?,?,?,?,?)",(provider['fingerprint'],'luna','luna','standard','low',success,ttft,status,status))
    summary=await client.get('/admin/v1/summary?window=24h',headers=headers); data=await summary.json()
    assert data['routable_apis']==1 and data['technical_success_rate']==.5 and data['avg_ttft_ms']==200
    row=(await (await client.get('/admin/v1/providers',headers=headers)).json())['providers'][0]
    assert row['technical_success_rate']==.5 and row['avg_ttft_ms']==200


async def test_summary_counts_only_actual_routable_provider_keys(client, cpa):
    headers={'Authorization':'Bearer admin-secret'}
    await client.post('/admin/v1/sync',headers=headers)
    provider=(await (await client.get('/admin/v1/providers',headers=headers)).json())['providers'][0]
    # Synced available canonical Luna is calibrated from the fixed official catalog.
    assert (await (await client.get('/admin/v1/summary?window=24h',headers=headers)).json())['routable_apis'] == 1
    await client.patch('/admin/v1/policy/'+provider['fingerprint'],headers=headers,json={'enabled':False})
    assert (await (await client.get('/admin/v1/summary?window=24h',headers=headers)).json())['routable_apis'] == 0


async def test_sync_replaces_source_and_malformed_rolls_back(client, cpa):
    headers={'Authorization':'Bearer admin-secret'}
    await client.post('/admin/v1/sync',headers=headers)
    a=(await (await client.get('/admin/v1/providers',headers=headers)).json())['providers'][0]
    await client.patch('/admin/v1/policy/'+a['fingerprint'],headers=headers,json={'calibrated':True})
    await client.post('/v1/generate',headers={'Authorization':'Bearer client-secret'},json={'prompt':'audit','intellect':'standard'})
    cpa.app['config']={'providers':[{'name':'B','base_url':cpa.app['upstream'],'type':'openai','keys':[{'key':'other-secret','models':['gpt-5.6-luna']}]}]}
    replacement=await client.post('/admin/v1/sync',headers=headers)
    assert (await replacement.json())['offlined'] == 1
    assert [p['name'] for p in (await (await client.get('/admin/v1/providers',headers=headers)).json())['providers']] == ['B']
    assert (await (await client.get('/admin/v1/calls?limit=10',headers=headers)).json())['items']
    cpa.app['config']=['malformed']
    failed=await client.post('/admin/v1/sync',headers=headers)
    assert failed.status == 502 and await failed.json() == {'error':'sync failed'}
    assert [p['name'] for p in (await (await client.get('/admin/v1/providers',headers=headers)).json())['providers']] == ['B']


async def test_sync_reports_inventory_failures(client, cpa):
    cpa.app['config']={'providers':[{'name':'Offline','base_url':'http://127.0.0.1:1','type':'openai','keys':[{'key':'offline-secret','models':['gpt-5.6-luna']}]}]}
    headers={'Authorization':'Bearer admin-secret'}
    response=await client.post('/admin/v1/sync',headers=headers)
    body=await response.json()
    assert response.status == 200 and body['inventory_failures'] == 1
    provider=(await (await client.get('/admin/v1/providers',headers=headers)).json())['providers'][0]
    assert provider['inventory_status'] == 'unavailable'
    assert (await (await client.get('/admin/v1/summary?window=24h',headers=headers)).json())['routable_apis'] == 0


async def test_global_race_cap_randomly_selects_same_price_keys(client, cpa):
    cpa.app['config'] = {'providers': [{'name': 'Shared upstream', 'base_url': cpa.app['upstream'], 'type': 'openai', 'keys': [
        {'key': 'low-key', 'models': ['gpt-5.6-luna']},
        {'key': 'high-key', 'models': ['gpt-5.6-luna']},
    ]}]}
    headers = {'Authorization': 'Bearer admin-secret'}
    assert (await client.post('/admin/v1/sync', headers=headers)).status == 200
    assert await (await client.patch('/admin/v1/routing', headers=headers, json={'race_parallel_cap': 1})).json() == {'race_parallel_cap': 1}
    assert await (await client.get('/admin/v1/routing', headers=headers)).json() == {'race_parallel_cap': 1}
    with patch('provider_broker.upstream.random.sample', side_effect=lambda providers, k: [next(provider for provider in providers if provider.api_key == 'high-key')]) as sample:
        result = await client.post('/v1/generate', headers={'Authorization': 'Bearer client-secret'}, json={'prompt': 'random', 'intellect': 'standard'})

    assert result.status == 200
    assert sample.call_args.kwargs['k'] == 1
    assert cpa.app['upstream_app']['last_response_headers']['Authorization'] == 'Bearer high-key'


def test_price_bands_split_all_key_prices_at_the_median():
    providers = [SimpleNamespace(price_group=price, id=index) for index, price in enumerate((100, 110, 115, 400))]

    bands = price_bands(providers)

    assert [[provider.price_group for provider in band] for band in bands] == [[100, 110], [115, 400]]


async def test_max_parallel_skips_a_busy_key_until_its_request_finishes(client, cpa):
    headers = {'Authorization': 'Bearer admin-secret'}
    assert (await client.post('/admin/v1/sync', headers=headers)).status == 200
    row = (await (await client.get('/admin/v1/providers', headers=headers)).json())['providers'][0]
    assert (await client.patch(f"/admin/v1/policy/{row['fingerprint']}", headers=headers, json={'max_parallel': 1})).status == 200
    upstream = cpa.app['upstream_app']
    upstream['hold_started'] = asyncio.Event()
    upstream['hold_release'] = asyncio.Event()

    first = asyncio.create_task(client.post('/v1/generate', headers={'Authorization': 'Bearer client-secret'}, json={'prompt': 'hold', 'intellect': 'standard'}))
    await asyncio.wait_for(upstream['hold_started'].wait(), timeout=2)
    skipped = await client.post('/v1/generate', headers={'Authorization': 'Bearer client-secret'}, json={'prompt': 'next', 'intellect': 'standard'})
    upstream['hold_release'].set()
    completed = await first

    assert skipped.status == 503
    assert (await skipped.json()) == {'error': 'all eligible providers failed', 'attempts': []}
    assert completed.status == 200


async def test_management_summary_providers_and_validated_policy(client, cpa):
    headers={'Authorization':'Bearer admin-secret'}
    empty=await client.get('/admin/v1/summary?window=24h',headers=headers)
    assert (await empty.json())['technical_success_rate'] is None
    await client.post('/admin/v1/sync',headers=headers)
    providers=await client.get('/admin/v1/providers',headers=headers)
    row=(await providers.json())['providers'][0]
    assert row['api_key_mask'].endswith('ret') and 'provider-secret' not in str(row)
    changed=await client.patch('/admin/v1/policy/'+row['fingerprint'],headers=headers,json={'note':'fast lane','multiplier':1.2,'enabled':True,'max_parallel':3})
    assert changed.status == 200
    echoed=(await (await client.get('/admin/v1/providers',headers=headers)).json())['providers'][0]
    assert (echoed['note'],echoed['max_parallel']) == ('fast lane',3)
    invalid=await client.patch('/admin/v1/policy/'+row['fingerprint'],headers=headers,json={'max_parallel':0})
    assert invalid.status == 400
    assert (await client.patch('/admin/v1/policy/'+row['fingerprint'],headers=headers,json={'preference':2})).status == 400


async def test_policy_requires_real_boolean_and_rejects_source_fields(client, cpa):
    headers={'Authorization':'Bearer admin-secret'}
    await client.post('/admin/v1/sync',headers=headers)
    provider=(await (await client.get('/admin/v1/providers',headers=headers)).json())['providers'][0]
    endpoint='/admin/v1/policy/'+provider['fingerprint']
    wrong_type=await client.patch(endpoint,headers=headers,json={'enabled':'false'})
    assert wrong_type.status == 400
    source_mutation=await client.patch(endpoint,headers=headers,json={'base_url':'https://changed.invalid'})
    assert source_mutation.status == 400
    assert (await (await client.get('/admin/v1/providers',headers=headers)).json())['providers'][0]['enabled'] is True
    assert (await client.patch(endpoint,headers=headers,json={'enabled':False})).status == 200
    assert (await (await client.get('/admin/v1/providers',headers=headers)).json())['providers'][0]['enabled'] is False


async def test_quality_and_calls_are_derived_from_completed_generate(client, cpa):
    headers={'Authorization':'Bearer admin-secret'}
    await client.post('/admin/v1/sync',headers=headers)
    provider=(await (await client.get('/admin/v1/providers',headers=headers)).json())['providers'][0]
    await client.patch('/admin/v1/policy/'+provider['fingerprint'],headers=headers,json={'calibrated':True})
    await client.post('/v1/generate',headers={'Authorization':'Bearer client-secret'},json={'prompt':'private','intellect':'standard','effort':'low'})
    quality=await client.get('/admin/v1/quality?window=24h',headers=headers)
    assert (await quality.json())['calls'] == 1
    calls=await client.get('/admin/v1/calls?limit=1',headers=headers)
    row=(await calls.json())['items'][0]
    assert row['status'] == 'completed' and 'prompt' not in row and 'provider-secret' not in str(row)
    by_name=await client.get('/admin/v1/calls?provider=Test%20OpenAI&limit=10',headers=headers)
    assert len((await by_name.json())['items']) == 1
    assert (await client.get('/admin/v1/calls?cursor=not-an-id',headers=headers)).status == 400


async def test_quality_window_failures_and_cursor_pagination(client):
    store=client.app['store']
    with store.conn:
        for status,ttft,created in [('completed',100,"datetime('now')"),('cancelled',200,"datetime('now')"),('timed_out',300,"datetime('now')"),('transport_failed',400,"datetime('now')"),('protocol_failed',500,"datetime('now')"),('stream_incomplete',600,"datetime('now')"),('completed',999,"datetime('now','-2 hours')")]:
            store.conn.execute(f"INSERT INTO observation(fingerprint,requested_model,actual_model,tier,effort,success,latency_ms,error,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,{created})",('p-new' if status!='timed_out' else 'p-other','luna','luna','standard','low',status=='completed',ttft,status,status))
    headers={'Authorization':'Bearer admin-secret'}
    quality=await client.get('/admin/v1/quality?window=1h',headers=headers)
    data=await quality.json()
    assert data['calls'] == 6 and data['p95_ttft_ms'] == 500
    assert data['failures'] == {'cancelled':1,'timed_out':1,'transport_failed':1,'protocol_failed':1,'stream_incomplete':1}
    first=await client.get('/admin/v1/calls?provider=p-new&limit=2',headers=headers); page1=await first.json()
    second=await client.get('/admin/v1/calls?provider=p-new&limit=2&cursor='+page1['next_cursor'],headers=headers); page2=await second.json()
    assert {x['id'] for x in page1['items']}.isdisjoint({x['id'] for x in page2['items']})
    filtered=await client.get('/admin/v1/calls?status=cancelled',headers=headers)
    assert all(x['status']=='cancelled' for x in (await filtered.json())['items'])
    recent=await client.get('/admin/v1/calls?window=1h&limit=100',headers=headers)
    assert all(x['ttft_ms'] != 999 for x in (await recent.json())['items'])
