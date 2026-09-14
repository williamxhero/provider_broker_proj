"""Canonical Provider pricing vocabulary shared by CPA sync and storage."""

from collections.abc import Iterable
from urllib.parse import urlsplit


PROVIDER_TYPES = frozenset({
    "openai",
    "anthropic",
    "deepseek",
    "qwen",
    "doubao",
    "deepinfra",
})
SUPPORTED_PROVIDER_TYPES = PROVIDER_TYPES
PROVIDER_ALLOWLIST = PROVIDER_TYPES
DEFAULT_OUTPUT_PRICE_CNY = 20.0

_ALIASES = {
    "openai": "openai",
    "codex": "openai",
    "anthropic": "anthropic",
    "claude": "anthropic",
    "deepseek": "deepseek",
    "qwen": "qwen",
    "bailian": "qwen",
    "aliyun": "qwen",
    "dashscope": "qwen",
    "doubao": "doubao",
    "doubao_seed": "doubao",
    "seed": "doubao",
    "ark": "doubao",
    "volcengine": "doubao",
    "volc_engine": "doubao",
    "deepinfra": "deepinfra",
}
_GENERIC_PROTOCOL_LABELS = {"openai_chat", "openai_compatible", "chat", "chat_completions", "completions", "direct", "relay"}


def _label(value: object) -> str:
    return str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")


def canonical_provider_type(
    declared: object,
    *,
    base_url: str = "",
    models: Iterable[object] = (),
) -> str | None:
    """Return one of the six supported pricing identities, or ``None``.

    Vendor-specific hosts and model IDs are stronger evidence than a generic
    OpenAI-compatible protocol label.  Transport adapters are deliberately not
    Provider identities.
    """
    declared_label = _label(declared)
    if declared_label and declared_label not in _ALIASES and declared_label not in _GENERIC_PROTOCOL_LABELS:
        return None
    try:
        host = (urlsplit(str(base_url)).hostname or "").casefold()
    except ValueError:
        host = ""
    if host == "api.deepinfra.com" or host.endswith(".deepinfra.com"):
        return "deepinfra"
    if host == "api.deepseek.com" or host.endswith(".deepseek.com"):
        return "deepseek"
    if host.endswith(".aliyuncs.com") or host.endswith(".dashscope.aliyuncs.com"):
        return "qwen"
    if host.endswith(".volces.com"):
        return "doubao"
    if host == "api.anthropic.com" or host.endswith(".anthropic.com"):
        return "anthropic"
    if host == "api.openai.com" or host.endswith(".openai.com"):
        return "openai"

    # An explicit vendor identity wins over a model-name guess for generic
    # gateways.  A gateway can legally expose another vendor's model IDs;
    # only generic transport labels should fall through to model inference.
    declared_provider = _ALIASES.get(declared_label)
    if declared_provider is not None and declared_label not in _GENERIC_PROTOCOL_LABELS:
        return declared_provider

    names = [str(model).strip().casefold().replace("_", "-") for model in models]
    for name in names:
        if name.startswith("claude-"):
            return "anthropic"
        if name.startswith("deepseek-"):
            return "deepseek"
        if name.startswith("qwen"):
            return "qwen"
        if name.startswith("doubao-"):
            return "doubao"
        if name.startswith(("gpt-", "codex-", "o1", "o3", "o4")):
            return "openai"

    return declared_provider


def require_provider_type(value: object) -> str:
    provider_type = canonical_provider_type(value)
    if provider_type is None:
        raise ValueError("provider type must be one of: Anthropic, DeepInfra, DeepSeek, Doubao, OpenAI, Qwen")
    return provider_type
