# Broker observability and optimization evidence

Source: GitHub parent specification [#20](https://github.com/williamxhero/provider_broker_proj/issues/20), captured for implementation and release evidence.

## Contract

The Broker records privacy-safe route, candidate, attempt and optional client timing facts. It distinguishes request outcomes from raw attempt completion, keeps race-loser cancellations neutral for reliability, and exposes bounded request-shape, Provider/site/model, delivery, release, policy/configuration and experiment cohorts. Collection must not alter routing order, health semantics, hedge delay, retries, concurrency, intellect or response contracts.

Raw prompts, output bodies, tool payloads, credentials, authorization headers, cookies and sensitive URLs are prohibited from telemetry, drill-down and exports. Unknown and inapplicable facts remain unknown rather than becoming zero or reconstructed historical data.

## Evidence requirements

- Request success reports a completed numerator, known terminal denominator, exclusions, coverage and telemetry version/start boundary.
- Plain stream forwarded delta, client received delta, validated completion and non-stream completion are separate metrics with applicability/sample counts.
- Candidate exclusions, stable attempt roles, cancellation censoring, amplification, site diversity and cost/usage coverage are auditable per route.
- Aggregates are allowlisted, return sample gates and binomial confidence intervals, and permit safe route drill-down.
- Configuration/release/policy context is frozen per route; telemetry cannot automatically promote a policy change.
- Raw facts are retained for a bounded period only after idempotent rollup watermarks; long-running analytical work remains outside the request path.
- Export and alert evaluation are aggregate-only and sample-gated. Alert canaries must not send real external notifications.

## Initial production observation gate

Before any later routing optimization, freeze a seven-day window, named cohorts and version context. Require at least 200 known routes for reliability and 200 applicable observations for P95 comparisons. State request-success non-inferiority and latency/amplification/cost limits before inspecting the window; insufficient evidence remains insufficient.
