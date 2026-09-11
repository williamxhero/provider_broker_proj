## Problem Statement

Broker 已经开始同时记录 attempt 级调用和 route 级请求终态，能够区分“某次上游尝试成功”和“用户的一次请求最终成功”，并记录上游 TTFT、Broker 对外首次 delta、完整完成耗时、获胜 Provider/model/site、故障分类及安全的请求形态诊断。这解决了竞速取消显著压低旧“技术成功率”的主要口径问题。

但现有证据仍不足以支持一段时间后的精准优化。新 route 数据仅从新版本上线后开始积累，管理网页仍主要展示旧 attempt 指标；Broker 对外首次 delta 只适用于普通文本流式调用，当前没有显示适用样本数和缺失原因，也没有真实客户端收到首字的时间。一次请求启动了多少候选、备用是否真正救回、额外花费多少流量、容量等待发生在哪一级、连接是否复用、哪些候选被排除，以及当时使用的发布版本和路由配置，都不能通过稳定的聚合接口完整分析。

现有管理 API 最长查询 30 天，原始记录没有明确的保留、汇总和数据质量策略。若直接根据混合版本、小样本或被竞速取消截断的观测调整 Provider 排名和 hedge 延迟，容易把请求形态差异、时间段拥堵、站点相关故障或选择偏差误认为 Provider 固有质量。

目标是补齐一套隐私安全、可审计、可长期比较的 Broker 可观测性：能够按请求、attempt、Provider、Key、站点、模型、请求形态、交付模式和策略版本回答成功率及首字速度问题，并明确每个比例的分子、分母、适用范围、样本量和置信程度，为后续受控优化提供可信基线。

## Solution

在现有 route 生命周期、attempt observation、Provider health 和 capability 数据之上，补充交付模式、候选决策、分阶段延迟、调用放大、版本配置及可选客户端遥测。为 route、attempt、候选和客户端观测建立稳定关联，同时保留现有公开字段的兼容性。

管理 API 和网页提供请求级总览、按维度分组、时间趋势和单次 route 明细。所有成功率、延迟和成本指标同时返回样本数、适用数、排除数及数据覆盖率；普通文本流式首字、结构化有效完成和非流式完成分别呈现，不能将不适用值填成零。

小电脑保存有限周期的原始明细，并生成小时、日级长期汇总。每次 route 固化发布版本、策略版本、配置指纹和实验分组，使调整前后能够在相同请求形态下比较。统计层提供小样本标记和置信区间，但本规格不自动改变路由策略；后续优化必须基于预先定义的观察窗、样本门槛和受控灰度。

## User Stories

1. As a Broker operator, I want to see request-level success rather than only attempt success, so that race cancellations do not make the service appear less reliable than it is.
2. As a Broker operator, I want every success ratio to show its numerator and denominator, so that I can verify the metric definition.
3. As a Broker operator, I want client cancellation, validation rejection, in-progress routes, and unknown terminal states shown separately, so that excluded requests do not silently disappear.
4. As a Broker operator, I want a telemetry coverage ratio, so that I know whether a time window is safe to compare.
5. As a Broker operator, I want metrics split by data-schema version, so that old incomplete records are not mistaken for current complete observations.
6. As a Broker operator, I want to see when route-level collection began, so that a 24-hour window containing only a few new routes is not presented as a full-day baseline.
7. As a plain-stream client, I want the Broker’s first forwarded delta latency measured, so that I can evaluate perceived responsiveness.
8. As a desktop client operator, I want optional actual first-delta receive latency measured at the client, so that network and client buffering are distinguishable from Broker processing.
9. As a structured-output client, I want valid-result completion latency measured instead of a provisional text metric, so that speed does not reward invalid output.
10. As a non-stream client, I want full response latency measured separately, so that my requests are not mixed with streaming first-delta measurements.
11. As a Broker operator, I want first-delta applicability and coverage shown, so that a null or sparse P95 has an explicit explanation.
12. As a Broker operator, I want accept-to-first-attempt delay measured, so that Broker scheduling overhead is visible.
13. As a Broker operator, I want capacity wait time split by Key, site, and global limits, so that the correct concurrency boundary can be tuned.
14. As a Broker operator, I want response-header time separated from first non-empty text time, so that network/queue delay is distinguishable from model generation delay.
15. As a Broker operator, I want connection reuse recorded, so that connection-pool improvements can be quantified.
16. As a Broker operator, I want stream progress gaps and post-first-delta interruptions measured, so that a fast first token cannot hide unstable delivery.
17. As a Broker operator, I want each route’s eligible candidate count, so that failures caused by thin inventory are visible.
18. As a Broker operator, I want candidate exclusion reasons counted, so that disabled policy, missing capability, open health, route block, site disablement, and capacity pressure can be distinguished.
19. As a Broker operator, I want each attempt’s role recorded, so that primary, hedge, retry, repair, exploration, and recovery traffic can be analyzed separately.
20. As a Broker operator, I want attempts started per request and their distribution, so that reliability gains can be weighed against amplification.
21. As a Broker operator, I want hedge-start rate and hedge rescue rate, so that hedge delay can be moved earlier or later based on actual value.
22. As a Broker operator, I want race-loser cancellations kept neutral for reliability while still counted as resource usage, so that scoring and cost analysis use different meanings.
23. As a Broker operator, I want distinct sites attempted per request, so that cross-site diversification can be verified.
24. As a Broker operator, I want same-site and cross-site rescue outcomes compared, so that correlated failure domains can be detected.
25. As a Broker operator, I want total attempted tokens and cost compared with winner-only tokens and cost, so that duplicate-call overhead is visible.
26. As a Broker operator, I want unknown usage and unknown cost preserved as unknown, so that partial streams are not falsely counted as free.
27. As a Broker operator, I want stable failure classes grouped by Provider, Key, model, contract, and site, so that remediation targets the correct scope.
28. As a Broker operator, I want credential failures separated from Provider overload and transport failures, so that bad keys do not poison site or model health.
29. As a Broker operator, I want structured-contract failures separated by Schema family, so that unsupported request shapes can be routed away without disabling plain text.
30. As a Broker operator, I want success and latency split by original intellect and effort, so that Smart and Expert behavior is not averaged together.
31. As a Broker operator, I want request length, output-budget, deadline, Schema, and delivery-mode cohorts, so that unlike workloads are not compared directly.
32. As a privacy-conscious user, I want request cohorts derived without storing raw prompts, output bodies, credentials, or headers, so that optimization does not expand sensitive-data exposure.
33. As a Broker operator, I want request-shape buckets to be versioned and bounded, so that dashboard cardinality and SQLite growth remain controlled.
34. As a Broker operator, I want every route tied to a Broker release version, so that regressions can be located after deployment.
35. As a Broker operator, I want every route tied to a routing-policy version and configuration fingerprint, so that parameter changes can be compared reproducibly.
36. As a Broker operator, I want configuration changes recorded with time and safe before/after values, so that metric shifts can be aligned with operational actions.
37. As a Broker operator, I want controlled experiment groups recorded on each route, so that canary and baseline traffic can be compared without relying only on calendar time.
38. As a Broker operator, I want grouped metrics to include confidence intervals and insufficient-sample warnings, so that small cohorts do not drive aggressive tuning.
39. As a Broker operator, I want selectable one-hour, one-day, seven-day, thirty-day, and custom bounded windows, so that current congestion and long-term priors can both be examined.
40. As a Broker operator, I want hourly and daily trend series, so that isolated incidents and persistent changes are distinguishable.
41. As a Broker operator, I want route-level drill-down from an aggregate anomaly, so that I can inspect the exact attempt sequence without exposing request content.
42. As a Broker operator, I want filters and groupings to compose predictably, so that Provider, site, model, shape, delivery mode, policy version, and outcome can be cross-checked.
43. As a Broker operator, I want aggregate groups to reconcile with the unfiltered total, so that dashboards do not report contradictory numbers.
44. As a Broker operator, I want old data with unavailable dimensions labeled unknown, so that migrations do not invent historical facts.
45. As a Broker operator, I want incomplete routes reconciled after restart, so that crashes do not permanently inflate in-progress counts.
46. As a Broker operator, I want raw telemetry retained long enough for incident analysis, so that recent regressions remain auditable.
47. As a small-computer administrator, I want old raw data compacted into bounded rollups, so that long-term evidence does not exhaust disk or degrade live routing.
48. As a Broker operator, I want retention and rollup jobs to run outside the request critical path, so that statistics maintenance does not slow client traffic.
49. As a Broker operator, I want a data-freshness indicator for each dashboard section, so that stale rollups are not mistaken for current state.
50. As a Broker operator, I want alerts for request success, first-delta P95, valid-completion P95, interruption rate, and amplification, so that regressions are noticed before manual review.
51. As a Broker operator, I want alert evaluation to enforce minimum samples and persistence windows, so that sparse traffic does not cause noisy alarms.
52. As a Broker operator, I want an export of privacy-safe aggregates, so that offline analysis does not require copying the production credential database.
53. As a Broker operator, I want comparison reports to state the exact metric definition, sample window, cohort, version, and uncertainty, so that optimization claims are reproducible.
54. As a Broker operator, I want later routing changes gated on a fixed baseline and predeclared success/latency thresholds, so that parameters are not tuned after seeing only favorable slices.

## Implementation Decisions

1. **Preserve and clarify metric layers.** Keep the existing attempt-level `technical_success_rate` for compatibility, label it explicitly as raw attempt completion rate, and add a cancellation-neutral attempt rate. Treat route-level request success as the primary reliability indicator. Every proportion returns completed numerator, known-outcome denominator, excluded counts and coverage; `null` means unavailable or not applicable and must never be converted to zero.
2. **Version telemetry semantics.** Every new route records a telemetry schema version. Aggregations group incompatible versions or mark dimensions unavailable; they do not backfill unknown historical facts from attempts. The API reports the earliest complete observation time for each metric so a requested window cannot imply more coverage than exists.
3. **Extend the immutable route fact.** A route records requested intellect, effort, deadline bucket, delivery mode, request-shape version and buckets, release version, routing-policy version, routing-configuration fingerprint, optional experiment arm, candidate counts, attempts started, distinct sites attempted, winner, terminal state, terminal reason and all applicable route-level timing milestones. Terminal writes remain idempotent by route ID.
4. **Define delivery modes.** Use separate stable values for non-stream responses, immediately forwarded plain-text streams, and streams delayed for complete validation. First-forwarded-delta metrics apply only when the contract permits a delta. Validated streams and non-stream responses use valid-result/full-response latency as their primary speed metric. Aggregates always include the applicable-request denominator.
5. **Record candidate decisions.** Store one bounded candidate-decision fact per route and eligible Provider/model option, including site, eligibility, exclusion reason, initial rank, role, safe score components and whether it was launched. Exclusion reasons use a stable enum covering policy disablement, inventory/model mismatch, capability rejection, health/open state, route block, site disablement, Key/site/global capacity and deadline/budget constraints. Do not store credentials, URLs containing secrets, prompts or response content.
6. **Measure route scheduling phases.** Record Broker accept-to-routing-start, capacity wait, first-attempt start, first response header, first non-empty upstream text, first Broker-forwarded delta, contract validation completion and route completion with a monotonic clock. Preserve wall-clock timestamps only for correlation and windows. Each phase reports applicability and missing reason.
7. **Measure transport reuse safely.** Instrument the shared upstream connector to record whether an existing connection was reused and, where reliably observable, bounded DNS, connection and TLS setup timings. Missing tracing support stays unknown. Connection metrics must not include peer credentials, complete URLs, headers or bodies.
8. **Summarize attempt amplification.** Derive attempts started/completed/cancelled, hedge/retry/repair/recovery counts, distinct Keys/sites, total upstream elapsed time, known total tokens and known total cost per route. Report winner-only cost separately from all-attempt cost and include cost-coverage counts. Race cancellation remains neutral for health and reliability but contributes to amplification and known resource use.
9. **Define rescue metrics.** A hedge rescue requires a non-primary attempt to complete the route after the primary has not produced a deliverable result. Report hedge-start rate, hedge-rescue numerator/denominator, time saved where a comparable primary terminal event is observed, and extra-attempt/cost distributions. Cancellation-censored primary attempts cannot be treated as proven failures or used to claim exact time saved.
10. **Retain layered fault attribution.** Continue stable failure classes for neutral cancellation, contract/capability, credential, Provider overload, transport and Provider failure. Add aggregate scopes for Key, model/contract and site without allowing a narrow failure to poison broader health. Terminal route reasons and candidate exclusion reasons are separate dimensions from attempt failure classes.
11. **Use bounded request-shape cohorts.** Derive versioned buckets for input characters/bytes or estimated tokens, output-token budget, client deadline, Schema family/fingerprint, delivery mode, intellect and effort. Exact prompt and Schema hashes may remain available for bounded forensic drill-down but are not default grouping dimensions. Raw prompt, output, tool payloads, credentials and headers remain prohibited.
12. **Add optional client receive telemetry.** A trusted client may submit an idempotent observation containing route correlation, metric type, elapsed time, client family/version and telemetry schema version through a dedicated authenticated endpoint. The route correlation identifier must be available before the first deliverable delta. Only timing and safe client metadata are accepted; late, duplicate, impossible, unknown-route and untrusted observations are rejected or explicitly classified. Server-forwarded and client-received first delta remain separate metrics with separate coverage.
13. **Freeze version and configuration context.** Stamp every route with the installed Broker release, routing-policy algorithm version and a canonical hash of effective routing settings. Record safe configuration-change events for hedge delay, parallel caps, site policy and relevant timeout/budget controls, including actor/source and timestamps. Secrets and unrelated settings are excluded.
14. **Support controlled comparison.** Allow a route to carry a stable experiment identifier and arm assigned before candidate ranking. Assignment must be deterministic for the declared unit, persist with the route and never change output contract or intellect implicitly. The telemetry layer compares arms but does not automatically promote a winner or mutate production policy.
15. **Expose one coherent analytics surface.** Extend the existing quality summary without removing current fields, and add request-level detail and grouped time-series endpoints. Supported filters include bounded window, intellect, effort, requested/actual model, Provider/Key, site, request-shape bucket, delivery mode, outcome, failure class, policy/release version and experiment arm. Supported groupings are allowlisted, pagination is required for route detail, and arbitrary SQL-like expressions are forbidden.
16. **Return auditable aggregates.** Each aggregate includes metric name/version, numerator and denominator where applicable, sample count, applicable count, excluded/unknown count, coverage, mean only where useful, P50/P95 for latency and amplification, confidence information, window boundaries, bucket granularity and data freshness. Group totals reconcile with the ungrouped result subject to explicitly reported unknown buckets.
17. **Make the management page request-centric.** Add sections for request outcomes, speed by delivery mode, attempt amplification/rescue, failures/exclusions, Provider/site cohorts and version/experiment comparison. The default headline is request success, not raw attempt success. Every card displays sample size and coverage, exposes the metric definition, and distinguishes unavailable, insufficient and zero values. Existing Provider inventory and call-detail workflows remain available.
18. **Provide privacy-safe drill-down.** Route detail shows the ordered attempt timeline, roles, Provider display name, model, site, safe failure class, timing milestones, outcome and known cost/usage without prompt or output text. Navigation from an aggregate preserves filters. Access follows the existing private-network/admin boundary, and exported data uses the same allowlist.
19. **Use confidence and sample gates.** Success proportions use a documented binomial confidence interval; latency percentiles report sample count and are marked insufficient below a configurable default threshold. Initial guidance is at least 200 known routes in a cohort for reliability comparison and at least 200 applicable latency samples for P95 comparison. These defaults are configurable but any change is recorded with the analysis.
20. **Acknowledge selection and censoring.** Statistics identify launched role and candidate eligibility, keep race-loser latency as right-censored rather than failed, and avoid presenting observed winners as an unbiased estimate of all candidates. Comparative reports label observational results; causal claims require a recorded controlled arm or another predeclared design.
21. **Bound storage on the small computer.** Keep raw route, candidate, attempt and client telemetry for 90 days by default, configurable within an operationally safe range. Produce hourly rollups retained for at least one year and daily rollups retained long term. Retention runs in bounded batches outside request handling, never deletes data newer than its successful rollup watermark, and records its own status and last success.
22. **Prepare SQLite for sustained queries.** Add indexes for route/attempt correlation, event time and the most common bounded dimensions. Aggregations use rollups for long windows and raw facts for recent drill-down. Maintenance avoids long write locks, respects WAL operation and exposes query/rollup duration so statistical work cannot silently degrade routing latency.
23. **Reconcile incomplete data.** Startup or a bounded background job marks routes left open by a prior process as unknown with a recovery reason after a grace period. Duplicate terminal events are idempotent. Rollups are rebuildable from retained raw facts, use versioned watermarks and can be verified against source counts.
24. **Expose freshness and health.** Analytics responses include latest raw event, latest completed rollup, retention watermark, incomplete-route count and collection errors. The management page warns when data is stale, coverage is incomplete or a selected window crosses a telemetry-version boundary.
25. **Add thresholded alerts.** Support alerts for request success, Broker-forwarded/client-received first-delta P95, valid-completion P95, post-delta interruption, zero-eligible routes, site-correlated failures and attempt/cost amplification. Alerts require a minimum applicable sample count and persistence window and include the triggering cohort/version. Notification transport reuses existing safe operations mechanisms rather than introducing credentials into telemetry.
26. **Provide aggregate export.** Export filtered hourly/daily aggregates and metric definitions in a machine-readable, UTF-8 format without raw request text, output or secrets. Raw production database copying is not required for routine analysis. Export records its window, filters, telemetry versions and generated time.
27. **Keep routing unchanged in this phase.** Collection, aggregation and display must not automatically alter Provider scores, health, candidate order, hedge delay, retries or concurrency. After sufficient data accumulates, routing changes require a separate spec or ticket with a frozen baseline, controlled rollout, non-inferiority requirement for request success and predeclared latency/cost thresholds.

## Testing Decisions

- Use the existing public generation/streaming APIs and management APIs as the primary behavior seam, with the existing aiohttp test client, controlled upstream servers, temporary SQLite stores and controllable clocks. Avoid tests coupled to private table layout or scoring constants.
- Verify one request with one completed winner and one race cancellation produces one successful route, a neutral cancelled attempt, correct attempts-per-route/amplification counts and reconcilable numerator/denominator values.
- Verify completed, failed, timed-out, client-cancelled, validation-rejected, in-progress and restart-reconciled unknown routes appear in the correct request-success numerator, denominator, exclusion and coverage fields.
- Exercise non-stream, plain-text stream and validated structured stream through the same external API. Assert plain streaming records first Broker-forwarded delta before upstream completion; structured delivery reports valid-result completion and marks forwarded-delta latency not applicable rather than zero.
- Use a real streaming test client to capture client receive time and submit optional client telemetry. Verify idempotency, authentication, route correlation, impossible timing rejection, duplicate handling, unknown routes and separate server/client coverage.
- Drive controlled capacity contention at Key, site and global boundaries. Verify waiting and exclusion reasons are attributed to the correct boundary, released capacity wakes work, and measured waits do not exceed the request deadline contract.
- Use controllable connector behavior to cover reused and newly established connections. Assert unsupported transport phase timings remain unknown and no URL credentials, headers or bodies appear in diagnostics or exports.
- Construct candidate sets containing disabled policy, unsupported contract, open health, route block, disabled site, full capacity and insufficient deadline cases. Verify candidate counts and exclusion-reason groups reconcile with route detail.
- Cover primary, hedge, retry, structured repair, exploration and recovery roles. Verify hedge rescue requires a non-primary winner and that a cancelled primary is not treated as a proven failure or exact saved-time baseline.
- Verify all-attempt tokens/cost, winner-only tokens/cost, unknown cost and cost coverage. Partial or cancelled streams without final usage must remain unknown instead of zero.
- Simulate credential, overload, transport, Provider, contract and model-fulfillment failures across Keys sharing and not sharing sites. Verify aggregate attribution is narrow and existing health/capability isolation remains unchanged.
- Generate request-shape boundary cases for prompt-size, output-budget and deadline buckets plus plain and multiple structured Schema families. Verify bucket versions are stable, bounded and contain no raw content.
- Deploy two synthetic release/policy/config versions and two experiment arms under the same controlled workload. Verify filters, groupings and comparison results retain version context and do not mix arms silently.
- For every supported grouping, verify numerator/denominator/sample/unknown totals reconcile with the ungrouped query. Invalid dimensions, unbounded windows, unsupported combinations and pagination errors must return bounded validation errors.
- Extend existing browser end-to-end tests to verify request success is the default headline, metric cards show sample and coverage, filters persist, unknown differs from zero, and aggregate-to-route drill-down preserves context.
- Migrate a database containing legacy observations and current route records. Verify historical missing dimensions remain unknown, existing API fields remain compatible, collection start times are reported and no fabricated backfill affects metrics.
- Simulate process termination with open routes, repeated terminal writes and interrupted rollup batches. Verify reconciliation is idempotent, rollup watermarks prevent gaps/double counts and source-to-rollup checks pass.
- Run retention against data spanning raw, hourly and daily boundaries. Verify newer-than-watermark facts are never removed, long-term totals remain available and maintenance batches do not block a concurrent generation request beyond a defined small budget.
- Populate production-shaped volumes and verify common 1h/24h/7d/30d dashboard queries remain bounded in time and memory on the small computer. Record query and maintenance durations rather than relying only on developer-machine performance.
- Scan persisted facts, API responses, exports and logs with sentinel prompts, outputs, API keys, authorization headers and signed URLs. The test passes only when prohibited values never leave their original request boundary.
- Alert tests use controllable windows and sample counts to verify minimum-sample and persistence gates, recovery notifications and cohort/version context without sending real external notifications.
- Statistical tests use fixed datasets to verify confidence interval calculations, percentile definitions, insufficient-sample states and right-censored cancellation treatment. Tests assert documented behavior, not a particular future ranking decision.
- A production observation period is not considered an implementation test pass by itself. Before later optimization, freeze window, cohorts, sample thresholds, request-success non-inferiority, latency improvement and amplification/cost limits; insufficient evidence must be reported as insufficient rather than optimized around.

## Out of Scope

- This specification does not automatically change Provider ordering, health thresholds, hedge delay, retry counts, attempt budgets, Key/site/global concurrency or price preference.
- It does not claim that the current 38 route samples prove a durable success-rate improvement.
- It does not redefine a technically valid Broker response as factually correct or high-quality business output.
- It does not relax intellect/model fulfillment, structured Schema validation, research-plan validation or streaming integrity to improve apparent latency.
- It does not store raw prompts, model output, tool payloads, credentials, authorization headers, cookies, complete sensitive URLs or arbitrary client metadata.
- It does not build a machine-learning or reinforcement-learning routing platform. Initial cohort analysis uses bounded, explainable aggregates.
- It does not infer exact counterfactual latency or Provider quality from race-loser cancellations.
- It does not purchase new Providers, alter external accounts, recharge balances or enable disabled Keys/sites.
- It does not expose management analytics outside the existing private/admin security boundary.
- It does not replace service/database backups; retention and rollups protect analytical continuity but are not a disaster-recovery system.

## Further Notes

- This specification is the observability follow-up to the completed Broker success/first-token work. The current implementation already has route terminal facts, attempt diagnostics, safe request fingerprints, fault classes, Provider/model capability state, site fault domains, request-success aggregates and server-side first-delta/completion aggregates; these are extended rather than replaced.
- A read-only production snapshot on 2026-09-08 showed 5,726 attempt observations in the 24-hour window, raw attempt technical success of about 53.65%, 2,367 neutral race cancellations, and only 38 newly instrumented known-outcome routes. Those routes showed 36 completions out of 38 known outcomes, but the sample is too small and starts partway through the 24-hour window.
- The same snapshot had no applicable Broker-forwarded first-delta sample in the new route data. This is consistent with traffic that had not yet exercised immediately forwarded plain-text streaming; upstream attempt TTFT remains available but is not a substitute for client-visible first delta.
- The current 24-hour route completion average and P95 are affected by mixed request shapes, especially long validated requests. Future dashboards must split delivery mode, intellect, effort and shape before these figures are used for tuning.
- Raw records currently have no automatic pruning in application code, while management windows stop at 30 days. The retention and rollup design must protect the small computer’s disk and SQLite performance without losing long-term baselines.
- Recommended initial evidence gate is at least seven days of complete telemetry and at least 200 known routes per major reliability cohort; P95 comparisons require at least 200 applicable observations and preferably more. These are initial defaults, not proof thresholds that may be changed after inspecting favorable results.
- Later optimization examples include lowering weight for persistently unreliable Provider/model/shape cohorts, reducing site concurrency for correlated overload, moving hedge timing based on measured rescue value, and distinguishing connection/TTFB delay from model TTFT. Each such behavior change remains a separate controlled implementation decision.

## Upstream contract summary

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
