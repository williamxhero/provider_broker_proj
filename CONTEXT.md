# Provider Broker

Provider Broker selects a configured upstream for a requested capability and records the cost and delivery evidence needed to operate that routing safely.

## Language

**Provider**:
One of the supported vendor pricing identities: OpenAI, Anthropic, DeepSeek, Qwen, Doubao, or DeepInfra. A Provider is not an API endpoint, relay, or wire protocol.
_Avoid_: Pricing source, direct Provider, relay Provider, protocol

**Provider+Model Price**:
The CNY price per one million tokens for one Provider and canonical Model. The same output price applies to input, cached input, and output token accounting.
_Avoid_: Input price, cache price, currency-specific price, blended price

**API Key Mapping**:
The relationship from one configured API Key and Model to a Provider+Model Price, with the only permitted pricing multiplier.
_Avoid_: Provider multiplier, policy multiplier

**Canonical Model**:
The stable Model identity used to match CPA inventory and runtime observations. Stage and family describe routing metadata and are not pricing attributes.
_Avoid_: Model directory price, global model price
