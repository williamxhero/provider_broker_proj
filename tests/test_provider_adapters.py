import json
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestServer

from provider_broker.source import _api_url, _public_model_ids, expand_config
from provider_broker.upstream import api_url, invoke_stream, provider_headers


def test_vendor_roots_keep_their_path_and_use_chat_completions():
    assert _api_url("https://api.deepinfra.com/v1/openai", "/models").endswith("/v1/openai/models")
    assert api_url("https://ark.cn-beijing.volces.com/api/v3", "/chat/completions").endswith("/api/v3/chat/completions")
    entries = expand_config({"providers": [
        {"type": "deepseek", "base_url": "https://api.deepseek.com/v1", "keys": [{"key": "secret", "models": ["deepseek-chat"]}]},
        {"type": "openai", "protocol": "chat", "base_url": "https://example.test/api/v3", "keys": [{"key": "secret", "models": ["model"]}]},
    ]})
    assert [item["provider_type"] for item in entries] == ["openai_chat", "openai_chat"]


def test_existing_openai_defaults_to_responses_protocol():
    entry = expand_config({"providers": [{"base_url": "https://example.test", "keys": [{"key": "secret", "models": ["model"]}]}]})[0]
    assert entry["provider_type"] == "openai"
    assert api_url(entry["base_url"], "/responses") == "https://example.test/v1/responses"


def test_known_vendor_bare_hosts_get_documented_api_roots():
    entries = expand_config({"providers": [
        {"type": "deepinfra", "base_url": "https://api.deepinfra.com", "keys": [{"key": "x", "models": ["m"]}]},
        {"type": "ark", "base_url": "https://ark.cn-beijing.volces.com", "keys": [{"key": "x", "models": ["m"]}]},
        {"type": "qwen", "base_url": "https://dashscope.aliyuncs.com", "keys": [{"key": "x", "models": ["m"]}]},
        {"type": "glm", "base_url": "https://open.bigmodel.cn", "keys": [{"key": "x", "models": ["m"]}]},
    ]})
    assert entries[0]["base_url"] == "https://api.deepinfra.com/v1/openai"
    assert entries[1]["base_url"] == "https://ark.cn-beijing.volces.com/api/v3"
    assert entries[2]["base_url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert entries[3]["base_url"] == "https://open.bigmodel.cn/api/paas/v4"


def test_vendor_aliases_and_hosts_select_chat_without_rewriting_arbitrary_gateways():
    entries = expand_config({"providers": [
        {"provider": "DeepSeek", "base_url": "https://api.deepseek.com", "keys": [{"key": "x", "models": ["m"]}]},
        {"vendor": "doubao-seed", "base_url": "https://ark.cn-beijing.volces.com", "keys": [{"key": "x", "models": ["m"]}]},
        {"type": "zhipuai", "base_url": "https://open.bigmodel.cn", "keys": [{"key": "x", "models": ["m"]}]},
        {"type": "openai", "base_url": "https://gateway.example/custom", "keys": [{"key": "x", "models": ["m"]}]},
    ]})

    assert [item["provider_type"] for item in entries] == ["openai_chat", "openai_chat", "openai_chat", "openai"]
    assert entries[0]["base_url"] == "https://api.deepseek.com/v1"
    assert entries[1]["base_url"] == "https://ark.cn-beijing.volces.com/api/v3"
    assert entries[2]["base_url"] == "https://open.bigmodel.cn/api/paas/v4"
    assert entries[3]["base_url"] == "https://gateway.example/custom"


def test_malformed_provider_shapes_are_ignored_without_leaking_headers():
    entries = expand_config({"providers": [
        None,
        "not-a-provider",
        {"type": 42, "base_url": "https://example.test", "keys": [None, "not-a-key", {"key": "x", "models": "not-a-list"}]},
        {"type": "openai", "base_url": "https://example.test", "keys": [{
            "key": "secret", "models": ["m"],
            "headers": {"Authorization": "override", "X-Api-Key": "override", "X-Ok": "yes\nno"},
        }], "headers": {"Host": "override", "X-Good": "ok"}},
    ]})

    assert len(entries) == 1
    assert entries[0]["request_headers"] == {"X-Good": "ok"}
    assert "secret" not in json.dumps(entries[0]["request_headers"])


async def test_openai_chat_adapter_preserves_stream_contract_and_model_identity():
    captured = {}

    async def chat(request):
        captured.update(await request.json())
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(b'data: {"model":"vendor/gpt-5.6-luna"}\n\n')
        await response.write(b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n')
        await response.write(b'data: {"choices":[{"delta":{"content":" broker"},"finish_reason":"stop"}]}\n\n')
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post("/api/v3/chat/completions", chat)
    server = TestServer(app)
    await server.start_server()
    provider = SimpleNamespace(
        base_url=str(server.make_url("/api/v3")), api_key="provider-secret",
        provider_type="openai_chat", models=["gpt-5.6-luna"], request_headers={
            "Authorization": "bad", "Host": "bad", "X-Trace": "safe",
        }, multiplier=1.0, pricing=None, wire_model="vendor/gpt-5.6-luna",
        model_aliases={"vendor/gpt-5.6-luna": "gpt-5.6-luna"},
    )
    try:
        output = await invoke_stream(provider, {
            "prompt": "prompt-secret", "deadline_ms": 1000, "output_token_limit": 100,
        })
    finally:
        await server.close()

    assert output["text"] == "hello broker"
    assert output["actual_model"] == "gpt-5.6-luna"
    assert captured["model"] == "vendor/gpt-5.6-luna"
    assert captured["messages"] == [{"role": "user", "content": "prompt-secret"}]
    assert captured["max_tokens"] == 100 and captured["stream"] is True
    assert "Authorization" not in output["diagnostic"]
    assert "prompt-secret" not in json.dumps(output["diagnostic"])


async def test_openai_chat_adapter_sends_structured_contract_and_validates_sse():
    captured = {}

    async def chat(request):
        captured.update(await request.json())
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(b'data: {"type":"response.output_text.delta","delta":"{\\"answer\\":\\"ok\\"}"}\n\n')
        await response.write(b'data: {"type":"response.completed","response":{"id":"structured","model":"gpt-5.6-luna","usage":{}}}\n\n')
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    server = TestServer(app)
    await server.start_server()
    provider = SimpleNamespace(
        base_url=str(server.make_url("")), api_key="provider-secret",
        provider_type="openai_chat", models=["gpt-5.6-luna"], request_headers={},
        multiplier=1.0, pricing=None,
    )
    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["answer"], "properties": {"answer": {"type": "string"}},
    }
    try:
        output = await invoke_stream(provider, {
            "prompt": "structured prompt-secret", "output_schema": schema,
            "deadline_ms": 1000, "output_token_limit": 100,
        })
    finally:
        await server.close()

    assert output["text"] == '{"answer":"ok"}'
    assert captured["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "broker_output", "strict": True, "schema": schema},
    }
    assert captured["messages"][0]["content"].startswith("structured prompt-secret")


def test_upstream_header_normalization_reinstates_broker_owned_headers():
    provider = SimpleNamespace(request_headers={
        "Authorization": "bad", "Content-Type": "bad", "Host": "bad",
        "X-Api-Key": "bad", "X-Trace": "safe",
    }, api_key="real-secret")

    assert provider_headers(provider) == {
        "Authorization": "Bearer real-secret", "Content-Type": "application/json", "X-Trace": "safe",
    }


def test_vendor_protocol_aliases_and_key_headers_are_normalized_safely():
    entries = expand_config({"providers": [{
        "type": "doubao", "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "headers": {"X-Gateway": "provider", "Authorization": "bad"},
        "keys": [{"key": "secret", "models": ["doubao-seed-2.1-pro"], "headers": {
            "X-Key": "key", "Content-Type": "bad", "X-Count": 3,
        }}],
    }]})
    assert entries[0]["provider_type"] == "openai_chat"
    assert entries[0]["request_headers"] == {"X-Gateway": "provider", "X-Key": "key", "X-Count": "3"}


def test_documented_vendor_roots_are_not_double_versioned():
    roots = {
        "https://api.deepinfra.com/v1/openai": "/chat/completions",
        "https://api.deepseek.com/v1": "/models",
        "https://dashscope.aliyuncs.com/compatible-mode/v1": "/chat/completions",
        "https://ark.cn-beijing.volces.com/api/v3": "/chat/completions",
        "https://open.bigmodel.cn/api/paas/v4": "/chat/completions",
    }
    for base, suffix in roots.items():
        assert _api_url(base, suffix) == base + suffix
        assert api_url(base, suffix) == base + suffix


def test_cpa_openai_compatibility_key_entries_and_model_aliases_are_supported():
    entries = expand_config({"openai-compatibility": [{
        "name": "DeepInfra",
        "base-url": "https://api.deepinfra.com/v1/openai",
        "api-key-entries": [{"api-key": "secret"}],
        "models": [{"name": "deepseek-ai/DeepSeek-V4-Flash-0731", "alias": "deepseek-v4-flash-0731"}],
    }]})

    assert len(entries) == 1
    assert entries[0]["api_key"] == "secret"
    assert entries[0]["aliases"] == {"deepseek-ai/deepseek-v4-flash-0731": "deepseek-v4-flash-0731"}
    assert entries[0]["provider_type"] == "openai_chat"


def test_public_provider_inventory_uses_builtin_model_ids_without_endpoint_ids():
    assert _public_model_ids("https://api.deepseek.com/v1", ["deepseek-v4-flash", "deepseek-v4-pro"]) == {
        "deepseek-v4-flash": "deepseek-v4-flash",
        "deepseek-v4-pro": "deepseek-v4-pro",
    }
    assert _public_model_ids("https://ark.cn-beijing.volces.com/api/v3", ["doubao-seed-2.0-lite"]) == {
        "doubao-seed-2.0-lite": "doubao-seed-2-0-lite-260215",
    }
