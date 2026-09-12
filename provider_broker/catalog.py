"""Default model-directory seed. Runtime entries are managed in SQLite; unknown models are never inferred."""
from urllib.parse import urlsplit

CATALOG = {
    "gpt-5.6-luna": {
        "family": "OpenAI GPT-5.6", "intellect": "standard",
        "official_input_price": 0.2, "official_cache_price": 0.02, "official_output_price": 1.2,
    },
    "gpt-5.6-terra": {
        "family": "OpenAI GPT-5.6", "intellect": "smart",
        "official_input_price": 2.0, "official_cache_price": 0.2, "official_output_price": 12.0,
    },
    "gpt-5.6-sol": {
        "family": "OpenAI GPT-5.6", "intellect": "expert",
        "official_input_price": 4.0, "official_cache_price": 0.4, "official_output_price": 20.0,
    },
    "gpt-5.5": {
        "family": "OpenAI GPT-5.5", "intellect": "expert",
        "official_input_price": 5.0, "official_cache_price": 0.5, "official_output_price": 30.0,
    },
    "claude-opus-5": {
        "family": "Anthropic Claude", "intellect": "expert",
        "official_input_price": 5.0, "official_cache_price": 0.5, "official_output_price": 25.0,
    },
    "claude-opus-4-8": {
        "family": "Anthropic Claude", "intellect": "expert",
        "official_input_price": 5.0, "official_cache_price": 0.5, "official_output_price": 25.0,
    },
    "claude-sonnet-5": {
        "family": "Anthropic Claude", "intellect": "smart",
        "official_input_price": 2.0, "official_cache_price": 0.2, "official_output_price": 10.0,
    },
    # Approved vendor identities.  Provider pricing was not included in the
    # approval attachment, so zero is an explicit unverified placeholder until
    # live price verification supplies billable rates; it is never copied from
    # another model.
    "deepseek-v4-flash": {
        "family": "DeepSeek", "intellect": "smart",
        "currency": "USD",
        "official_input_price": 0.14, "official_cache_price": 0.0028, "official_output_price": 0.28,
    },
    "deepseek-v4-pro": {
        "family": "DeepSeek", "intellect": "expert",
        "currency": "USD",
        "official_input_price": 0.435, "official_cache_price": 0.003625, "official_output_price": 0.87,
    },
    "doubao-seed-2.0-lite": {
        "family": "Doubao Seed", "intellect": "standard",
        "official_input_price": 0.0, "official_cache_price": 0.0, "official_output_price": 0.0,
    },
    "glm-5.3-flash": {
        "family": "GLM", "intellect": "smart",
        "official_input_price": 0.0, "official_cache_price": 0.0, "official_output_price": 0.0,
    },
    "qwen3.8-flash-next": {
        "family": "Qwen", "intellect": "smart",
        "official_input_price": 0.0, "official_cache_price": 0.0, "official_output_price": 0.0,
    },
    "doubao-seed-2.1-turbo": {
        "family": "Doubao Seed", "intellect": "smart",
        "currency": "CNY",
        "official_input_price": 3.0, "official_cache_price": 0.6, "official_output_price": 15.0,
    },
    "doubao-seed-2.1-pro": {
        "family": "Doubao Seed", "intellect": "smart",
        "currency": "CNY",
        "official_input_price": 6.0, "official_cache_price": 1.2, "official_output_price": 30.0,
    },
    "glm-5.3": {
        "family": "GLM", "intellect": "expert",
        "official_input_price": 0.0, "official_cache_price": 0.0, "official_output_price": 0.0,
    },
}

for _item in CATALOG.values():
    _item.setdefault("currency", "USD")

ALIASES = {
    "gpt-5.6": "gpt-5.6-sol", "claude-opus-4.8": "claude-opus-4-8",
    "deepseek-chat": "deepseek-v4-flash",
    "deepseek-reasoner": "deepseek-v4-flash",
    "deepseek-v4-flash-0731": "deepseek-v4-flash",
    "deepseek-v4-1-flash": "deepseek-v4-flash",
    "doubao-seed-2-0-lite": "doubao-seed-2.0-lite",
    "doubao-seed-2-1-turbo": "doubao-seed-2.1-turbo",
    "doubao-seed-2-1-pro": "doubao-seed-2.1-pro",
    "glm-5-3-flash": "glm-5.3-flash",
    "glm-5-3": "glm-5.3",
    "qwen3-8-flash-next": "qwen3.8-flash-next",
}

CATALOG_SEED_VERSION = 4
CATALOG_V2_MODELS = frozenset({
    "deepseek-v4-flash", "deepseek-v4-pro", "doubao-seed-2.0-lite", "glm-5.3-flash",
    "qwen3.8-flash-next", "doubao-seed-2.1-turbo",
    "doubao-seed-2.1-pro", "glm-5.3",
})

PUBLIC_MODEL_IDS = {
    "deepseek.com": {
        "deepseek-v4-flash": "deepseek-v4-flash",
        "deepseek-v4-pro": "deepseek-v4-pro",
    },
    "volces.com": {
        "doubao-seed-2.0-lite": "doubao-seed-2-0-lite-260215",
        "doubao-seed-2.1-turbo": "doubao-seed-2.1-turbo",
        "doubao-seed-2.1-pro": "doubao-seed-2.1-pro",
    },
}

PROVIDER_PRICING = {
    "llm-uqnm5hkklj592o02.cn-beijing.maas.aliyuncs.com": {
        "qwen3.8-flash-next": {"currency": "CNY", "official_input_price": 0.8, "official_cache_price": 0.1, "official_output_price": 2.7},
    },
    "api.deepinfra.com": {
        "deepseek-v4-flash": {"currency": "USD", "official_input_price": 0.09, "official_cache_price": 0.018, "official_output_price": 0.18},
        "deepseek-v4-pro": {"currency": "USD", "official_input_price": 1.30, "official_cache_price": 0.10, "official_output_price": 2.60},
    },
    "api.deepseek.com": {
        "deepseek-v4-flash": {"currency": "USD", "official_input_price": 0.14, "official_cache_price": 0.0028, "official_output_price": 0.28},
        "deepseek-v4-pro": {"currency": "USD", "official_input_price": 0.435, "official_cache_price": 0.003625, "official_output_price": 0.87},
    },
    "ark.cn-beijing.volces.com": {
        "doubao-seed-2.1-turbo": {"currency": "CNY", "official_input_price": 3.0, "official_cache_price": 0.6, "official_output_price": 15.0},
        "doubao-seed-2.1-pro": {"currency": "CNY", "official_input_price": 6.0, "official_cache_price": 1.2, "official_output_price": 30.0},
    },
}


def provider_pricing(base_url: str, model: str, fallback: dict) -> dict:
    host = urlsplit(base_url).hostname.lower() if urlsplit(base_url).hostname else ""
    for endpoint, prices in PROVIDER_PRICING.items():
        if host == endpoint and canonicalize(model) in prices:
            return prices[canonicalize(model)] | {"price_source": "official-provider"}
    return fallback


def canonicalize(model: str) -> str:
    normalized = model.strip().lower().replace("_", "-")
    return ALIASES.get(normalized, normalized)


def classify(model: str):
    item = CATALOG.get(canonicalize(model))
    if item is None:
        return None
    return item["intellect"], item["official_output_price"]


def blended_price(pricing: dict) -> float:
    """Blended per-currency/1M estimate: 4% input, 16% cached, 80% output."""
    return round(
        pricing["official_input_price"] * 0.04
        + pricing["official_cache_price"] * 0.16
        + pricing["official_output_price"] * 0.80,
        10,
    )
