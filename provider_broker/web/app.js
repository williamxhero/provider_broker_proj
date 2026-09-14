const byId = (id) => document.getElementById(id);
const sortDefaults = {
  providers: { key: "note", direction: "asc" },
  modelView: { key: "stage", direction: "asc" },
  models: { key: "id", direction: "asc" },
  pricing: { key: "provider", direction: "asc" },
  calls: { key: "time", direction: "desc" },
};
const state = { cursor: "", provider: null, model: null, pricing: null, callsRequest: 0, filterTimer: null, pricingTimer: null, qualityWindow: "24h", providers: [], stages: [], summary: {}, sorts: {}, models: [], pricingItems: [] };
const preferencesKey = "provider-broker.console.preferences.v1";
const preferences = (() => { try { return JSON.parse(window.localStorage.getItem(preferencesKey) || "{}"); } catch (_) { return {}; } })();
const empty = (value) => value === null || value === undefined || value === "" || value === "UNKNOWN" ? "n/a" : String(value);
const cell = (value) => {
  const td = document.createElement("td");
  td.textContent = empty(value);
  return td;
};
const formatPercent = (value) => value === null || value === undefined ? "n/a" : `${(value * 100).toFixed(1)}%`;
const formatFailureRate = (count, calls) => Number.isFinite(Number(count)) && Number(calls) > 0
  ? formatPercent(Number(count) / Number(calls))
  : "n/a";
const statusLabels = {
  completed: "成功", cancelled: "已取消", timed_out: "超时", unavailable: "不可用",
  transport_failed: "传输失败", protocol_failed: "协议失败", stream_incomplete: "流式响应不完整",
  first_token_timeout: "首字超时", model_mismatch: "模型不匹配", capacity_reached: "容量不足",
};
const displayStatus = (status) => statusLabels[status] || empty(status);
const statusCode = (value) => Object.entries(statusLabels).find(([, label]) => label === value)?.[0] || value;
const formatMs = (value) => {
  if (value === null || value === undefined) return "n/a";
  return value >= 1000 ? `${(value / 1000).toFixed(1)} s` : `${Math.round(value)} ms`;
};
const formatPrice = (value) => value === null || value === undefined || value === "UNKNOWN" ? "n/a" : String(value);
const formatMultiplier = (value) => value === null || value === undefined || !Number.isFinite(Number(value)) ? "n/a" : Number(value).toFixed(3);
const formatCost = (value) => value === null || value === undefined || !Number.isFinite(Number(value))
  ? "n/a"
  : new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 6 }).format(Number(value));
const formatTokens = (value) => value === null || value === undefined || !Number.isFinite(Number(value))
  ? "n/a"
  : new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(Number(value));
const formatFeeBuckets = (buckets) => {
  if (!buckets || typeof buckets !== "object" || !Object.keys(buckets).length) return "n/a";
  return Object.entries(buckets).map(([currency, item]) => `${empty(currency)} ${item?.total_fee === null || item?.total_fee === undefined ? "n/a" : Number(item.total_fee).toFixed(6)}`).join(" · ");
};

function savePreferences() {
  window.localStorage.setItem(preferencesKey, JSON.stringify(preferences));
}

function restoreControls() {
  ["callwindow", "calllimit", "callprovider", "callstatus"].forEach((id) => {
    if (preferences[id] !== undefined) byId(id).value = preferences[id];
  });
  state.qualityWindow = preferences.qualityWindow || state.qualityWindow;
  state.sorts = { ...sortDefaults, ...(preferences.sorts || {}) };
  setQualityWindow(state.qualityWindow);
}

function persistControl(id) {
  preferences[id] = byId(id).value;
  savePreferences();
}

async function requestJson(url, options) {
  const response = await fetch(url, options);
  const raw = await response.text();
  const body = raw ? JSON.parse(raw) : {};
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return body;
}

function sortFor(list) {
  return state.sorts[list] || sortDefaults[list];
}

function toggleSort(list, key, render) {
  const current = sortFor(list);
  state.sorts[list] = { key, direction: current.key === key && current.direction === "asc" ? "desc" : "asc" };
  preferences.sorts = state.sorts;
  savePreferences();
  render();
}

function compareValues(left, right) {
  const leftMissing = left === null || left === undefined || left === "";
  const rightMissing = right === null || right === undefined || right === "";
  if (leftMissing || rightMissing) return leftMissing === rightMissing ? 0 : leftMissing ? 1 : -1;
  if (typeof left === "number" && typeof right === "number") return left - right;
  return String(left).localeCompare(String(right), undefined, { numeric: true, sensitivity: "base" });
}

function sortItems(items, list, valueFor) {
  const sort = sortFor(list);
  return [...items].sort((left, right) => compareValues(valueFor(left, sort.key), valueFor(right, sort.key)) * (sort.direction === "asc" ? 1 : -1));
}

function tableHead(table, columns, list, render) {
  const thead = document.createElement("thead");
  const row = document.createElement("tr");
  columns.forEach(({ key, label }) => {
    const th = document.createElement("th");
    th.scope = "col";
    if (!key) {
      th.textContent = label;
    } else {
      const button = document.createElement("button");
      const current = sortFor(list);
      button.type = "button";
      button.className = `sort-button${current.key === key ? " active" : ""}`;
      button.textContent = label;
      button.title = `按${label}排序`;
      if (current.key === key) {
        const indicator = document.createElement("span");
        indicator.className = "sort-indicator";
        indicator.textContent = current.direction === "asc" ? "↑" : "↓";
        button.append(indicator);
      }
      button.addEventListener("click", () => toggleSort(list, key, render));
      th.append(button);
    }
    row.append(th);
  });
  thead.append(row);
  table.replaceChildren(thead, document.createElement("tbody"));
  return table.tBodies[0];
}

function renderSummary(summary) {
  state.summary = summary;
  byId("syncat").textContent = formatShanghaiTime(summary.last_successful_sync);
}

function formatShanghaiTime(value) {
  if (!value) return "n/a";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "n/a";
  const values = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(date).filter((part) => part.type !== "literal").map((part) => [part.type, part.value]));
  return `${values.year}/${values.month}/${values.day} ${values.hour}:${values.minute}`;
}

async function openEditor(provider) {
  state.provider = provider;
  const form = byId("policy");
  form.elements.note.value = provider.note || "";
  form.elements.enabled.checked = provider.status === "enabled";
  form.elements.max_parallel.value = provider.max_parallel;
  byId("editor-source").textContent = `${provider.normalized_hostname} · ${provider.api_key_mask}`;
  byId("key-mappings").replaceChildren(document.createTextNode("正在加载 mappings…"));
  byId("editor").hidden = false;
  form.elements.note.focus();
  try {
    const detail = await requestJson(`/admin/v1/keys/${encodeURIComponent(provider.fingerprint)}?window=${encodeURIComponent(state.qualityWindow)}`);
    renderMappingEditor(detail.edit?.mappings || []);
  } catch (_) {
    byId("key-mappings").textContent = "mappings 加载失败";
  }
}

function renderMappingEditor(items) {
  const container = byId("key-mappings");
  container.replaceChildren();
  if (!items.length) {
    container.append(document.createTextNode("当前没有已配置 mapping"));
    return;
  }
  items.forEach((item) => {
    const row = document.createElement("div");
    row.className = "mapping-row";
    row.dataset.mappingId = item.id;
    const model = document.createElement("span");
    model.textContent = item.model_id;
    model.title = item.model_id;
    const targetProvider = document.createElement("input");
    targetProvider.type = "number"; targetProvider.name = "target_provider_id"; targetProvider.value = item.target_provider_id; targetProvider.setAttribute("aria-label", `${item.model_id} target provider`);
    const targetModel = document.createElement("input");
    targetModel.type = "text"; targetModel.name = "target_model_id"; targetModel.value = item.target_model_id; targetModel.setAttribute("aria-label", `${item.model_id} target model`);
    const multiplier = document.createElement("input");
    multiplier.type = "number"; multiplier.name = "multiplier"; multiplier.min = "0.001"; multiplier.step = "0.001"; multiplier.value = item.multiplier; multiplier.setAttribute("aria-label", `${item.model_id} multiplier`);
    const enabled = document.createElement("label");
    enabled.className = "toggle";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox"; checkbox.name = "enabled"; checkbox.checked = item.enabled; checkbox.setAttribute("aria-label", `${item.model_id} enabled`);
    enabled.append(checkbox, document.createTextNode("启用"));
    row.append(model, targetProvider, targetModel, multiplier, enabled);
    container.append(row);
  });
}

function closeEditor() {
  byId("editor").hidden = true;
  state.provider = null;
}

function openPricingEditor(item) {
  state.pricing = item || null;
  const form = byId("pricing-form");
  form.reset();
  form.elements.provider_id.value = item?.provider?.id || "";
  form.elements.model_id.value = item?.model?.id || "";
  form.elements.model_id.readOnly = Boolean(item);
  form.elements.output_price_cny.value = item?.output_price_cny ?? "";
  byId("pricing-editor").hidden = false;
  form.elements.provider_id.focus();
}

function closePricingEditor() {
  byId("pricing-editor").hidden = true;
  state.pricing = null;
}

function renderPricing(items = state.pricingItems) {
  state.pricingItems = items;
  const table = byId("pricing");
  const columns = [{ key: "provider", label: "Provider" }, { key: "model", label: "Model" }, { key: "price", label: "输出价格 / 1M CNY" }, { label: "操作" }];
  const body = tableHead(table, columns, "pricing", () => renderPricing());
  sortItems(items, "pricing", (item, key) => ({ provider: item.provider.name, model: item.model.id, price: item.output_price_cny }[key])).forEach((item) => {
    const row = document.createElement("tr");
    if (!item.active) row.className = "inactive-row";
    [item.provider.name, item.model.id, formatPrice(item.output_price_cny)].forEach((value) => row.append(cell(value)));
    const action = document.createElement("td");
    const edit = document.createElement("button");
    edit.type = "button"; edit.className = "text-button"; edit.textContent = "编辑";
    edit.addEventListener("click", () => openPricingEditor(item));
    action.append(edit); row.append(action); body.append(row);
  });
}

async function loadPricingViews() {
  const query = new URLSearchParams({
    provider: byId("pricing-filter-provider").value,
    model: byId("pricing-filter-model").value,
  });
  [...query.keys()].forEach((key) => { if (!query.get(key)) query.delete(key); });
  const prices = await requestJson(`/admin/v1/pricing?${query}`);
  renderPricing(prices.items);
}

function renderProviders(payload) {
  state.providers = payload.providers;
  const table = byId("providers");
  const columns = [{ key: "normalized_hostname", label: "域名" }, { key: "status", label: "状态" }, { key: "note", label: "备注" }, { key: "api_key_mask", label: "API Key" }, { key: "max_parallel", label: "单 Key 并发上限" }, { key: "total_tokens", label: `${state.qualityWindow} Token` }, { key: "fee_buckets", label: `${state.qualityWindow} 费用` }];
  columns.push({ label: "操作" });
  const body = tableHead(table, columns, "providers", () => renderProviders({ providers: state.providers }));
  sortItems(payload.providers, "providers", (provider, key) => provider[key]).forEach((provider) => {
    const row = document.createElement("tr");
    if (provider.status !== "enabled") row.className = "inactive-row";
    const status = document.createElement("span");
    status.className = `status ${provider.status === "enabled" ? "on" : "off"}`;
    status.textContent = provider.status === "enabled" ? "启用" : provider.status === "disabled" ? "停用" : "不可用";
    const statusCell = document.createElement("td"); statusCell.append(status);
    row.append(cell(provider.normalized_hostname), statusCell, cell(provider.note), cell(provider.api_key_mask), cell(provider.max_parallel), cell(formatTokens(provider.total_tokens)), cell(formatFeeBuckets(provider.fee_buckets)));
    const action = document.createElement("td");
    const edit = document.createElement("button"); edit.type = "button"; edit.className = "text-button"; edit.textContent = "编辑";
    edit.addEventListener("click", () => openEditor(provider));
    action.append(edit); row.append(action); body.append(row);
  });
}

function providerDomain(baseUrl) {
  try { return new URL(baseUrl).origin; } catch (_) { return baseUrl; }
}

function stageOrder(value) {
  return ({ standard: 0, smart: 1, expert: 2 })[value] ?? 99;
}

function formatPriceBand(band) {
  if (!band || typeof band !== "object") return "n/a";
  const prices = Array.isArray(band.output_prices_cny) ? band.output_prices_cny :
    band.output_price_cny === null || band.output_price_cny === undefined ? [] : [band.output_price_cny];
  return prices.length ? prices.map(formatPrice).join(" · ") : "n/a";
}

function renderModelView(payload = { items: state.stages }) {
  state.stages = payload.items || [];
  const table = byId("model-view");
  const columns = [{ key: "stage", label: "Stage" }, { key: "low_price", label: "低价输出 / 1M CNY" }, { key: "high_price", label: "高价输出 / 1M CNY" }, { key: "provider_types", label: "Provider types" }, { key: "callable_key_count", label: "可调用 Key" }, { key: "latest_test", label: "最近测试" }, { key: "technical_success_rate", label: "技术成功率" }, { key: "avg_first_token_latency_ms", label: "平均首字延迟" }, { key: "total_tokens", label: `${state.qualityWindow} Token` }, { key: "fee_buckets", label: `${state.qualityWindow} 费用` }, { label: "操作" }];
  const body = tableHead(table, columns, "modelView", () => renderModelView({ items: state.stages }));
  sortItems(state.stages, "modelView", (item, key) => key === "provider_types" ? (item.provider_types || []).join(" ") : key === "latest_test" ? item.latest_test?.at : key === "low_price" ? item.price_bands?.low?.output_price_cny : key === "high_price" ? item.price_bands?.high?.output_price_cny : item[key]).forEach((item) => {
    const row = document.createElement("tr");
    const latest = item.latest_test ? `${displayStatus(item.latest_test.status)} · ${formatShanghaiTime(item.latest_test.at)}` : "n/a";
    const providers = (item.provider_types || []).map(empty).join(", ") || "n/a";
    [item.stage, formatPriceBand(item.price_bands?.low), formatPriceBand(item.price_bands?.high), providers, item.callable_key_count, latest, formatPercent(item.technical_success_rate), formatMs(item.avg_first_token_latency_ms), formatTokens(item.total_tokens), formatFeeBuckets(item.fee_buckets)].forEach((value) => row.append(cell(value)));
    const action = document.createElement("td");
    const test = document.createElement("button"); test.type = "button"; test.className = "text-button"; test.textContent = "测试";
    test.addEventListener("click", () => testStage(item, test));
    action.append(test); row.append(action); body.append(row);
  });
}

async function testStage(item, button) {
  const output = byId("stage-test-result");
  button.disabled = true;
  output.textContent = `正在测试 ${item.stage} 的启用 Key/Model pairs…`;
  try {
    const result = await requestJson("/admin/v1/stages/test", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stage: item.stage }),
    });
    await loadStageView();
    output.textContent = `测试完成：${result.succeeded_count || 0}/${result.tested_count || 0} 个 capability pair 成功`;
  } catch (error) {
    output.textContent = `Stage 测试失败：${error.message}`;
  } finally {
    button.disabled = false;
  }
}

function priceBands(providers) {
  if (!providers.length) return [];
  const ordered = [...providers].sort((left, right) => left.priceGroup - right.priceGroup || compareValues(left.fingerprint, right.fingerprint));
  const midpoint = Math.floor(ordered.length / 2);
  const median = ordered.length % 2 ? ordered[midpoint].priceGroup : (ordered[midpoint - 1].priceGroup + ordered[midpoint].priceGroup) / 2;
  const lower = ordered.filter((provider) => provider.priceGroup <= median);
  const higher = ordered.filter((provider) => provider.priceGroup > median);
  return [{ label: "低价组", providers: lower, threshold: median }, ...(higher.length ? [{ label: "高价组", providers: higher, threshold: median }] : [])];
}

function metric(label, value) {
  const item = document.createElement("div");
  const name = document.createElement("span");
  const number = document.createElement("strong");
  name.textContent = label;
  number.textContent = empty(value);
  number.title = number.textContent;
  item.append(name, number);
  return item;
}

function fitMetricValues() {
  byId("quality").querySelectorAll("strong").forEach((number) => {
    number.style.fontSize = "";
    let size = Number.parseFloat(getComputedStyle(number).fontSize);
    while (number.scrollWidth > number.clientWidth && size > 8) {
      size -= 0.5;
      number.style.fontSize = `${size}px`;
    }
  });
}

function renderQuality(payload) {
  const failures = payload.failures || {};
  const requestMetrics = Object.hasOwn(payload, "request_success_denominator") ? [
    metric("\u8bf7\u6c42\u6210\u529f\u7387", formatPercent(payload.request_success_rate)),
    metric("\u5df2\u77e5\u8bf7\u6c42\u6837\u672c", `${payload.request_success_numerator}/${payload.request_success_denominator}`),
    metric("\u9065\u6d4b\u8986\u76d6\u7387", formatPercent(payload.request_coverage)),
  ] : [];
  const amplification = payload.amplification ? [
    metric("attempt P95", payload.amplification.attempts_p95),
    metric("hedge rescue", `${payload.amplification.hedge_rescue_numerator}/${payload.amplification.hedge_rescue_denominator}`),
    metric("cost coverage", formatPercent(payload.amplification.cost_coverage)),
  ] : [];
  byId("quality").replaceChildren(
    ...requestMetrics,
    ...amplification,
    metric("可路由 API", state.summary.routable_apis),
    metric("技术成功率", formatPercent(payload.technical_success_rate)),
    metric("平均 TTFT", formatMs(payload.avg_ttft_ms)),
    metric("P95 TTFT", formatMs(payload.p95_ttft_ms)),
    metric("调用数", payload.calls),
    metric("总费用", formatCost(payload.total_cost)),
    metric("模型履约率", formatPercent(payload.model_fulfillment_rate)),
    metric(displayStatus("cancelled"), formatFailureRate(failures.cancelled, payload.calls)),
    metric(displayStatus("timed_out"), formatFailureRate(failures.timed_out, payload.calls)),
    metric(displayStatus("transport_failed"), formatFailureRate(failures.transport_failed, payload.calls)),
    metric(displayStatus("protocol_failed"), formatFailureRate(failures.protocol_failed, payload.calls)),
    metric(displayStatus("stream_incomplete"), formatFailureRate(failures.stream_incomplete, payload.calls)),
  );
  requestAnimationFrame(fitMetricValues);
}

function callsUrl(cursor = state.cursor) {
  const sort = sortFor("calls");
  return `/admin/v1/calls?${new URLSearchParams({
    window: byId("callwindow").value,
    limit: byId("calllimit").value,
    provider: byId("callprovider").value,
    status: statusCode(byId("callstatus").value),
    cursor,
    sort: `${sort.key}:${sort.direction}`,
  })}`;
}

function renderCalls(payload) {
  const table = byId("calls");
  const columns = [{ key: "time", label: "调用时间" }, { key: "note", label: "API Key 备注" }, { key: "requested_model", label: "请求模型" }, { key: "actual_model", label: "实际模型" }, { key: "intellect", label: "intellect" }, { key: "effort", label: "effort" }, { key: "ttft", label: "TTFT" }, { key: "status", label: "技术状态" }, { key: "input_tokens", label: "输入 Token" }, { key: "output_tokens", label: "输出 Token" }, { key: "cost", label: "成本" }];
  const body = tableHead(table, columns, "calls", () => loadCalls(""));
  payload.items.forEach((item) => {
    const row = document.createElement("tr");
    [item.time, item.note, item.requested_model, item.actual_model, item.intellect, item.effort, formatMs(item.ttft_ms), displayStatus(item.status), item.input_tokens, item.output_tokens, formatCost(item.cost)].forEach((value) => row.append(cell(value)));
    body.append(row);
  });
  state.cursor = payload.next_cursor || "";
  byId("next").dataset.cursor = state.cursor;
  byId("next").disabled = !state.cursor;
}

async function loadCalls(cursor = state.cursor) {
  const requestNumber = ++state.callsRequest;
  byId("calls").replaceChildren();
  byId("next").dataset.cursor = "";
  byId("next").disabled = true;
  const payload = await requestJson(callsUrl(cursor));
  if (requestNumber === state.callsRequest) renderCalls(payload);
}

function renderRoutes(payload) {
  const table = byId("routes");
  const columns = [{ label: "time" }, { label: "outcome" }, { label: "mode" }, { label: "model" }, { label: "site" }, { label: "detail" }];
  const body = tableHead(table, columns, "routes", () => renderRoutes(payload));
  payload.items.forEach((item) => {
    const row = document.createElement("tr");
    [formatShanghaiTime(item.started_at), displayStatus(item.outcome), item.delivery_mode, item.selected_model, item.selected_site_id].forEach((value) => row.append(cell(value)));
    const detail = document.createElement("button");
    detail.type = "button"; detail.className = "text-button"; detail.textContent = "view";
    detail.addEventListener("click", async () => {
      const audit = await requestJson(`/admin/v1/routes/${encodeURIComponent(item.route_id)}`);
      byId("route-detail").textContent = JSON.stringify(audit, null, 2);
    });
    const action = document.createElement("td"); action.append(detail); row.append(action); body.append(row);
  });
}

async function loadRoutes() {
  renderRoutes(await requestJson(`/admin/v1/routes?window=${encodeURIComponent(state.qualityWindow)}&limit=25`));
}

async function loadDataHealth() {
  const health = await requestJson("/admin/v1/data-health");
  byId("telemetry-health").textContent = health.in_progress || health.reconciled_unknown || health.legacy_records
    ? `telemetry warning: in-progress=${health.in_progress}, reconciled-unknown=${health.reconciled_unknown}, legacy=${health.legacy_records}`
    : "telemetry coverage is complete for collected routes";
}

function renderAnalytics(payload) {
  const table = byId("analytics");
  const body = tableHead(table, [{ label: "cohort" }, { label: "request success" }, { label: "sample" }, { label: "95% CI" }, { label: "status" }], "analytics", () => renderAnalytics(payload));
  payload.groups.forEach((group) => {
    const interval = group.confidence_interval_95 ? group.confidence_interval_95.map(formatPercent).join(" - ") : "n/a";
    const row = document.createElement("tr");
    [group.group, formatPercent(group.success_rate), group.success_denominator, interval, group.insufficient ? "insufficient" : "ready"].forEach((value) => row.append(cell(value)));
    body.append(row);
  });
}

async function loadAnalytics() {
  renderAnalytics(await requestJson(`/admin/v1/analytics?window=${encodeURIComponent(state.qualityWindow)}&group_by=site`));
}

async function loadStageView() {
  renderModelView(await requestJson(`/admin/v1/stages?window=${encodeURIComponent(state.qualityWindow)}`));
}

async function load() {
  const [summary, providers, routing, stages] = await Promise.all([
    requestJson("/admin/v1/summary?window=24h"),
    requestJson(`/admin/v1/providers?window=${encodeURIComponent(state.qualityWindow)}`),
    requestJson("/admin/v1/routing"),
    requestJson(`/admin/v1/stages?window=${encodeURIComponent(state.qualityWindow)}`),
  ]);
  renderSummary(summary);
  renderProviders(providers);
  renderModelView(stages);
  byId("race-parallel-cap").value = routing.race_parallel_cap;
  byId("hedge-delay-ms").value = routing.hedge_delay_ms;
  // Secondary panels must not delay the primary dashboard. Fetch them in
  // parallel after the core data has been rendered.
  void Promise.allSettled([
    loadPricingViews(),
    requestJson(`/admin/v1/quality?window=${encodeURIComponent(state.qualityWindow)}`).then(renderQuality),
    loadCalls(""),
    loadRoutes(),
    loadDataHealth(),
    loadAnalytics(),
  ]);
}

byId("policy").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const policy = {
    note: form.elements.note.value,
    enabled: form.elements.enabled.checked,
    max_parallel: Number(form.elements.max_parallel.value),
  };
  const mappings = [...byId("key-mappings").querySelectorAll(".mapping-row")].map((row) => ({
    id: Number(row.dataset.mappingId),
    target_provider_id: Number(row.querySelector('[name="target_provider_id"]').value),
    target_model_id: row.querySelector('[name="target_model_id"]').value,
    enabled: row.querySelector('[name="enabled"]').checked,
  }));
  await requestJson(`/admin/v1/keys/${encodeURIComponent(state.provider.fingerprint)}?window=${encodeURIComponent(state.qualityWindow)}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...policy, mappings }),
  });
  closeEditor();
  await load();
});

byId("save-routing").addEventListener("click", async () => {
  const race_parallel_cap = Number(byId("race-parallel-cap").value);
  const hedge_delay_ms = Number(byId("hedge-delay-ms").value);
  const result = await requestJson("/admin/v1/routing", { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ race_parallel_cap, hedge_delay_ms }) });
  byId("race-parallel-cap").value = result.race_parallel_cap;
  byId("hedge-delay-ms").value = result.hedge_delay_ms;
  byId("syncresult").textContent = `同价竞速 Key 数已设为 ${result.race_parallel_cap}，对冲延迟 ${result.hedge_delay_ms} ms`;
});
byId("pricing-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const outputPrice = Number(form.elements.output_price_cny.value);
  const body = { output_price_cny: outputPrice };
  const providerId = form.elements.provider_id.value;
  const modelId = encodeURIComponent(form.elements.model_id.value);
  const endpoint = state.pricing ? `/admin/v1/pricing/${providerId}/${modelId}` : "/admin/v1/pricing";
  await requestJson(endpoint, { method: state.pricing ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(state.pricing ? body : { ...body, provider_id: Number(providerId), model_id: form.elements.model_id.value }) });
  closePricingEditor(); await loadPricingViews();
});
["close-pricing-editor", "cancel-pricing-editor"].forEach((id) => byId(id).addEventListener("click", closePricingEditor));

[
  "pricing-filter-provider", "pricing-filter-model",
].forEach((id) => byId(id).addEventListener("input", () => {
  clearTimeout(state.pricingTimer); state.pricingTimer = setTimeout(() => loadPricingViews().catch(() => {}), 250);
}));
byId("sync").addEventListener("click", async () => {
  const output = byId("syncresult");
  output.textContent = "正在同步…";
  try {
    const result = await requestJson("/admin/v1/sync", { method: "POST" });
    await load();
    output.textContent = `added ${result.added} updated ${result.updated} offlined ${result.offlined} inventory_failures ${result.inventory_failures}`;
  } catch (_) {
    output.textContent = "同步失败；已保留上一次成功快照";
  }
});

byId("windows").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-window]");
  if (!button) return;
  setQualityWindow(button.dataset.window);
  const [quality, providers, stages] = await Promise.all([
    requestJson(`/admin/v1/quality?window=${encodeURIComponent(state.qualityWindow)}`),
    requestJson(`/admin/v1/providers?window=${encodeURIComponent(state.qualityWindow)}`),
    requestJson(`/admin/v1/stages?window=${encodeURIComponent(state.qualityWindow)}`),
  ]);
  renderQuality(quality);
  renderProviders(providers);
  renderModelView(stages);
});

function setQualityWindow(windowName) {
  state.qualityWindow = windowName;
  preferences.qualityWindow = windowName;
  savePreferences();
  byId("windows").querySelectorAll("button").forEach((item) => item.classList.toggle("active", item.dataset.window === windowName));
}

function scheduleCallsReset() {
  window.clearTimeout(state.filterTimer);
  state.callsRequest += 1;
  byId("calls").replaceChildren();
  byId("next").disabled = true;
  state.filterTimer = window.setTimeout(() => loadCalls(""), 30);
}
["callwindow", "calllimit", "callprovider", "callstatus"].forEach((id) => byId(id).addEventListener("input", () => {
  persistControl(id);
  scheduleCallsReset();
}));
byId("next").addEventListener("click", () => loadCalls(byId("next").dataset.cursor));
byId("close-editor").addEventListener("click", closeEditor);
byId("cancel-editor").addEventListener("click", closeEditor);
restoreControls();
load().catch(() => { byId("syncresult").textContent = "管理数据加载失败"; });
window.addEventListener("resize", () => requestAnimationFrame(fitMetricValues));
