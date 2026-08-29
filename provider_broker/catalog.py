"""Default model-directory seed. Runtime entries are managed in SQLite; unknown models are never inferred."""

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
}

ALIASES = {"gpt-5.6": "gpt-5.6-sol", "claude-opus-4.8": "claude-opus-4-8"}


def canonicalize(model: str) -> str:
    normalized = model.strip().lower().replace("_", "-")
    return ALIASES.get(normalized, normalized)


def classify(model: str):
    item = CATALOG.get(canonicalize(model))
    if item is None:
        return None
    return item["intellect"], item["official_output_price"]


def blended_price(pricing: dict) -> float:
    """Blended USD/1M estimate: 20% input (80% cached) and 80% output."""
    return round(
        pricing["official_input_price"] * 0.04
        + pricing["official_cache_price"] * 0.16
        + pricing["official_output_price"] * 0.80,
        10,
    )
