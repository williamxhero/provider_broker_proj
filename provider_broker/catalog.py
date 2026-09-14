"""Default model-directory seed and fixed official pricing evidence."""
from urllib.parse import urlsplit

CATALOG = {
    "gpt-5.6-luna": {
        "family": "OpenAI GPT-5.6", "intellect": "standard",
        "official_output_price_cny": 1.2,
    },
    "gpt-5.6-terra": {
        "family": "OpenAI GPT-5.6", "intellect": "smart",
        "official_output_price_cny": 12.0,
    },
    "gpt-5.6-sol": {
        "family": "OpenAI GPT-5.6", "intellect": "expert",
        "official_output_price_cny": 20.0,
    },
    "gpt-5.5": {
        "family": "OpenAI GPT-5.5", "intellect": "expert",
        "official_output_price_cny": 30.0,
    },
    "claude-opus-5": {
        "family": "Anthropic Claude", "intellect": "expert",
        "official_output_price_cny": 25.0,
    },
    "claude-opus-4-8": {
        "family": "Anthropic Claude", "intellect": "expert",
        "official_output_price_cny": 25.0,
    },
    "claude-sonnet-5": {
        "family": "Anthropic Claude", "intellect": "smart",
        "official_output_price_cny": 10.0,
    },
    # Approved vendor identities.  Provider pricing was not included in the
    # approval attachment, so zero is an explicit unverified placeholder until
    # live price verification supplies billable rates; it is never copied from
    # another model.
    "deepseek-v4-flash": {
        "family": "DeepSeek", "intellect": "smart",
        "official_output_price_cny": 0.28,
    },
    "deepseek-v4-pro": {
        "family": "DeepSeek", "intellect": "expert",
        "official_output_price_cny": 0.87,
    },
    "doubao-seed-2.0-lite": {
        "family": "Doubao Seed", "intellect": "standard",
        "official_output_price_cny": 0.0,
    },
    "glm-5.3-flash": {
        "family": "GLM", "intellect": "smart",
        "official_output_price_cny": 0.0,
    },
    "qwen3.8-flash-next": {
        "family": "Qwen", "intellect": "smart",
        "official_output_price_cny": 0.0,
    },
    "doubao-seed-2.1-turbo": {
        "family": "Doubao Seed", "intellect": "smart",
        "official_output_price_cny": 15.0,
    },
    "doubao-seed-2.1-pro": {
        "family": "Doubao Seed", "intellect": "smart",
        "official_output_price_cny": 30.0,
    },
    "glm-5.3": {
        "family": "GLM", "intellect": "expert",
        "official_output_price_cny": 0.0,
    },
}

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

# These are deliberately fixed inputs to the pricing migration.  They are not
# a live price feed: changing one is a new, reviewable seed revision.  The
# DeepSeek rows use the confirmed peak snapshot so a migration cannot silently
# choose a lower promotional tier.
OFFICIAL_PRICE_SNAPSHOT = {
    "source_name": "official-website-snapshot-2026-09-14",
    "source_url": "https://platform.openai.com/docs/pricing",
    "source_evidence": "Fixed official website snapshot; DeepSeek uses the confirmed peak choice: deepseek-v4-pro peak official tier.",
    "verified_at": "2026-09-14T00:00:00+00:00",
    "deepseek_peak_choice": "deepseek-v4-pro peak official tier",
}

OFFICIAL_PROVIDER_SNAPSHOTS = {
    "official-openai": {
        "name": "OpenAI official",
        "source_url": "https://platform.openai.com/docs/pricing",
        "models": {model for model, item in CATALOG.items() if item["family"].startswith("OpenAI")},
    },
    "official-anthropic": {
        "name": "Anthropic official",
        "source_url": "https://docs.anthropic.com/en/docs/about-claude/pricing",
        "models": {model for model, item in CATALOG.items() if item["family"].startswith("Anthropic")},
    },
}

def normalize_hostname(value: str) -> str:
    """Return the lower-case, path/port-free hostname capped at three labels."""
    if not isinstance(value, str):
        return ""
    parsed = urlsplit(value if "://" in value else f"//{value}")
    hostname = (parsed.hostname or "").strip().rstrip(".").lower()
    if not hostname:
        return ""
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    return ".".join(hostname.split(".")[-3:])


def canonicalize(model: str) -> str:
    normalized = model.strip().lower().replace("_", "-")
    return ALIASES.get(normalized, normalized)


def classify(model: str):
    item = CATALOG.get(canonicalize(model))
    if item is None:
        return None
    return item["intellect"], item["official_output_price_cny"]
