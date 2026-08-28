# Provider Broker

Private aiohttp + SQLite provider router for `192.168.50.1 -> 192.168.50.2:8817`.

CPA is a read-only source. `POST /admin/v1/sync` is the only refresh mechanism; it reads CPA's management configuration, expands each key/model to a direct upstream record, encrypts keys with AES-GCM in SQLite, and atomically replaces the source snapshot. Returned inventory masks source credentials by omission. Broker-owned policy and observations are retained by an HMAC source fingerprint; changing a key creates a new provider.

Client bearer endpoints:

- `POST /v1/generate` for `standard`, `smart`, or `expert`, with independent `effort`.
- `POST /v1/generate/stream` for SSE.

Admin bearer endpoints are `POST /admin/v1/sync`, `GET /admin/v1/inventory`, and `PUT /admin/v1/policy/{fingerprint}`. The embedded Web login creates an HttpOnly, Strict session cookie and has no CPA mutation paths.

Routing races all eligible providers in the cheapest price group, cancels losers after the first success, then falls upward only when a group fails. OpenAI/Codex providers use Responses; Anthropic/Claude uses Messages. Native tools are intentionally not forwarded.

## Local verification

`python -m pytest -q`

## Deploy

From Windows: `powershell -ExecutionPolicy Bypass -File scripts/deploy.ps1`.

The installer creates `/data/provider-broker`, preserves an existing secret env file, imports CPA's server-local management token only during first install, binds to `192.168.50.2:8817`, and adds UFW rules when UFW is available. Run the independent smoke on the server with `set -a; . /data/provider-broker/secrets/broker.env; set +a; /data/provider-broker/venv/bin/python /data/provider-broker/app/scripts/smoke.py`.
