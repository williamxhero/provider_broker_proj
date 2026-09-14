# SPEC #71 release-train evidence

- Candidate baseline: `e97ca3f8445719ca64caec8fb5169de3df51521b`
- Implementation branch: `codex/spec-71`
- Scope: pricing cutover and integrated release acceptance after SPEC #69/#70
- Checkpoint size: `10`

## Contract gates

- Model directory is metadata-only (`Provider + Model` prices are separate).
- The only runtime price multiplier is `key_model_mapping.multiplier`.
- Provider-level, policy/global, relay-binding, model-rate compatibility, and legacy source projections are excluded from authoritative reads and costing.
- Missing and explicitly unpriced prices remain unknown and never become zero-cost.
- Relay pricing is an explicit relay Provider+Model row.
- Historical observation cost/currency data is preserved; accounting remains bucketed by currency.

## Local evidence plan

- L0: `python -m compileall -q provider_broker scripts`, `node --check provider_broker/web/app.js`, `git diff --check`
- L1-L2: `python -m pytest -q --disable-warnings`
- L3 public contract: admin pricing/model/mapping APIs, forbidden legacy endpoints, Key/Stage allowlists, four accounting windows, currency buckets, hostname normalization, and Analytics/Routes browser layout
- L4 release shape: production smoke test and package/entrypoint syntax checks
- L5 external runtime: not run from this local worktree; requires the deployed yosef-server service and live CPA/MarketHub dependencies

## Acceptance notes

Tests were updated where their assertions encoded the removed catalog, relay-binding, or Provider/policy multiplier contract. The fixed seed data remains source evidence for explicit Provider+Model rows, not a compatibility projection.

## Final yosef-server release evidence

- Deployed release: `0.2.21` at `/data/provider-broker/releases/0.2.21`; `provider-broker.service`, `provider-broker-browser.service`, and `provider-broker-browser-web.service` are active.
- Health gate: `/healthz` returned HTTP 200 with `{"status":"ok"}`; browser endpoint reported Chrome `149.0.7827.200`.
- Structured runtime smoke: smart 200 (`gpt-5.6-terra`), expert 200 (`gpt-5.6-sol`), memory/research 5/5 200, research-plan 3/3 200.
- Stage canary: `gpt-5.6-terra` passed 3/3; unavailable targets and the partially failing `gpt-5.6-sol` cell remained fail-closed and did not invalidate the passing enabled target.
- Deployment verifier repair: planner canary now discovers both supported companion layouts and emits `needs_repair` when the installed runtime or data contract is unavailable; focused regression 4 passed and the full repository suite passed with 171 tests.
- Final local gates: compileall and `git diff --check` passed. This repository has no JavaScript entrypoint, so a Node syntax check was not applicable.
