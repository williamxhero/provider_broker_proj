"""Official, explicit model catalog. Unknown models are never inferred."""

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
}

ALIASES = {"gpt-5.6": "gpt-5.6-sol"}


def canonicalize(model: str) -> str:
    normalized = model.strip().lower().replace("_", "-")
    return ALIASES.get(normalized, normalized)


def classify(model: str):
    item = CATALOG.get(canonicalize(model))
    if item is None:
        return None
    return item["intellect"], item["official_output_price"]
