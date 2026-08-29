# Provider Broker

Private aiohttp + SQLite provider router for `192.168.50.1 -> 192.168.50.2:8817`.

CPA is a read-only source. `POST /admin/v1/sync` is the only refresh mechanism; it reads CPA's management configuration, expands each key/model to a direct upstream record, encrypts keys with AES-GCM in SQLite, and atomically replaces the source snapshot. Returned inventory masks source credentials by omission. Broker-owned policy and observations are retained by an HMAC source fingerprint; changing a key creates a new provider.

Client bearer endpoints:

- `POST /v1/generate` for `standard`, `smart`, or `expert`, with independent `effort`.
- `POST /v1/generate/stream` for SSE.

The management console opens directly at `/`, and `/admin/v1/*` is intentionally unauthenticated. It is protected by the dedicated host firewall, which only admits the direct-link client (`192.168.50.1`) and the server itself. Generation endpoints remain protected by the separate client Bearer token.

Routing races all eligible providers in the cheapest price group, cancels losers after the first success, then falls upward only when a group fails. OpenAI/Codex providers use Responses; Anthropic/Claude uses Messages. Native tools are intentionally not forwarded.

## Local verification

`python -m pytest -q`

## Deploy

From Windows: `powershell -ExecutionPolicy Bypass -File scripts/deploy.ps1`.

The deploy script builds and uploads one wheel, installs it under `/data/provider-broker/releases/<version>`, atomically switches `/data/provider-broker/current`, and retains `/data/provider-broker/previous` for rollback. It preserves the 0600 secret env file, binds to `192.168.50.2:8817`, and applies UFW rules when UFW is active. Run the independent smoke on the server with `set -a; . /data/provider-broker/secrets/broker.env; set +a; /data/provider-broker/current/venv/bin/python /data/provider-broker/current/smoke.py`.
