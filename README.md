# Provider Broker

Private aiohttp + SQLite provider router for `192.168.50.1 -> 192.168.50.2:8817`.

CPA is a read-only source. `POST /admin/v1/sync` is the only refresh mechanism; it reads CPA's management configuration, expands each key/model to a direct upstream record, encrypts keys with AES-GCM in SQLite, and atomically replaces the source snapshot. Returned inventory masks source credentials by omission. Broker-owned policy and observations are retained by an HMAC source fingerprint; changing a key creates a new provider.

Client bearer endpoints:

- `POST /v1/generate` for `standard`, `smart`, or `expert`, with independent `effort`.
- `POST /v1/generate/stream` for SSE.

The management console opens directly at `/`, and `/admin/v1/*` is intentionally unauthenticated. It is protected by the dedicated host firewall, which only admits the direct-link client (`192.168.50.1`) and the server itself. Generation endpoints remain protected by the separate client Bearer token.

## Model directory and routing

The **模型费率** section is a Broker-owned, persistent model directory. It is seeded with the shipped defaults on first start, then can be created, edited, and deleted from the console (or `POST`, `PUT`/`PATCH`, and `DELETE /admin/v1/catalog`). Each entry contains the model ID, family, rates per 1M tokens, and its `stage`:

- `standard`: first choice for a standard request; if unavailable, the router may use `smart`, then `expert`.
- `smart`: first choice for a smart request; if unavailable, the router may use `expert`.
- `expert`: used only for expert requests.

Thus `stage` is the routing partition, not a quality guarantee. A catalog change takes effect immediately; deleting a model removes it from routing even if CPA still reports it.

The catalog also shows a read-only **整合价 / 1M** for quick comparison: `4% × input + 16% × cached input + 80% × output`. The three underlying rates remain editable and are used for the exact per-request cost estimate. For each API Key, its multiplier is applied to those official rates. When CPA supplies a site name (`site_name`, `name`, `id`, `label`, or `endpoint`), a manual sync copies it to the Broker note.

Routing races the selected keys in the cheapest model group and cancels losers after the first success, then falls upward only when a group fails. Within a capped race, keys enter in descending `preference` order (higher integer first). `max_parallel` is the per-API-Key maximum number of simultaneous upstream requests; a saturated key is skipped so another eligible key can be used. The global `BROKER_PARALLEL_CAP` remains the maximum number of keys raced for one request. OpenAI/Codex providers use Responses; Anthropic/Claude uses Messages. Native tools are intentionally not forwarded.

## Local verification

`python -m pytest -q`

## Deploy

From Windows: `powershell -ExecutionPolicy Bypass -File scripts/deploy.ps1`.

The deploy script builds and uploads one wheel, installs it under `/data/provider-broker/releases/<version>`, atomically switches `/data/provider-broker/current`, and retains `/data/provider-broker/previous` for rollback. It preserves the 0600 secret env file, binds to `192.168.50.2:8817`, and applies UFW rules when UFW is active. Run the independent smoke on the server with `set -a; . /data/provider-broker/secrets/broker.env; set +a; /data/provider-broker/current/venv/bin/python /data/provider-broker/current/smoke.py`.
