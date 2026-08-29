const byId = (id) => document.getElementById(id);
const state = { cursor: "", provider: null, catalogModel: null, callsRequest: 0, filterTimer: null, qualityWindow: "24h" };
const preferencesKey = "provider-broker.console.preferences.v1";
const preferences = (() => { try { return JSON.parse(window.localStorage.getItem(preferencesKey) || "{}"); } catch (_) { return {}; } })();
const empty = (value) => value === null || value === undefined || value === "" ? "n/a" : String(value);
const cell = (value) => {
  const td = document.createElement("td");
  td.textContent = empty(value);
  return td;
};
const formatPercent = (value) => value === null || value === undefined ? "n/a" : `${(value * 100).toFixed(1)}%`;
const formatMs = (value) => {
  if (value === null || value === undefined) return "n/a";
  return value >= 1000 ? `${(value / 1000).toFixed(1)} s` : `${Math.round(value)} ms`;
};
const formatPrice = (value) => value === null || value === undefined ? "n/a" : String(value);
const formatMultiplier = (value) => value === null || value === undefined || !Number.isFinite(Number(value)) ? "n/a" : Number(value).toFixed(3);
const formatCost = (value) => value === null || value === undefined || !Number.isFinite(Number(value))
  ? "n/a"
  : new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 6 }).format(Number(value));

function savePreferences() {
  window.localStorage.setItem(preferencesKey, JSON.stringify(preferences));
}

function restoreControls() {
  ["callwindow", "calllimit", "callprovider", "callstatus"].forEach((id) => {
    if (preferences[id] !== undefined) byId(id).value = preferences[id];
  });
  state.qualityWindow = preferences.qualityWindow || state.qualityWindow;
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

function tableHead(table, labels) {
  const thead = document.createElement("thead");
  const row = document.createElement("tr");
  labels.forEach((label) => {
    const th = document.createElement("th");
    th.scope = "col";
    th.textContent = label;
    row.append(th);
  });
  thead.append(row);
  table.replaceChildren(thead, document.createElement("tbody"));
  return table.tBodies[0];
}

function healthClass(success, ttft) {
  if (success === null || success === undefined || ttft === null || ttft === undefined) return "";
  if (success >= 0.95 && ttft <= 2000) return "healthy";
  if (success >= 0.85 || ttft <= 5000) return "warning";
  return "critical";
}

function renderSummary(summary) {
  byId("routable").textContent = empty(summary.routable_apis);
  byId("success").textContent = formatPercent(summary.technical_success_rate);
  byId("ttft").textContent = formatMs(summary.avg_ttft_ms);
  byId("syncat").textContent = formatShanghaiTime(summary.last_successful_sync);
  byId("success").className = healthClass(summary.technical_success_rate, summary.avg_ttft_ms);
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

function openEditor(provider) {
  state.provider = provider;
  const form = byId("policy");
  form.elements.note.value = provider.note || "";
  form.elements.multiplier.value = provider.multiplier;
  form.elements.enabled.checked = provider.enabled === true;
  form.elements.max_parallel.value = provider.max_parallel;
  byId("editor-source").textContent = `${provider.name} · ${provider.family} · ${provider.base_url} · ${provider.api_key_mask}`;
  byId("editor").hidden = false;
  form.elements.note.focus();
}

function closeEditor() {
  byId("editor").hidden = true;
  state.provider = null;
}

function openCatalogEditor(model, item) {
  state.catalogModel = model || null;
  const form = byId("catalog-form");
  form.reset();
  form.elements.model.value = model || "";
  form.elements.model.readOnly = Boolean(model);
  form.elements.family.value = item?.family || "";
  form.elements.intellect.value = item?.intellect || "standard";
  form.elements.official_input_price.value = item?.official_input_price ?? "";
  form.elements.official_cache_price.value = item?.official_cache_price ?? "";
  form.elements.official_output_price.value = item?.official_output_price ?? "";
  updateBlendedPrice();
  byId("catalog-delete").hidden = !model;
  byId("catalog-editor").hidden = false;
  form.elements.model.focus();
}

function updateBlendedPrice() {
  const form = byId("catalog-form");
  const input = Number(form.elements.official_input_price.value);
  const cached = Number(form.elements.official_cache_price.value);
  const output = Number(form.elements.official_output_price.value);
  form.elements.blended_price.value = [input, cached, output].every(Number.isFinite)
    ? (input * 0.04 + cached * 0.16 + output * 0.80).toFixed(6)
    : "";
}

function closeCatalogEditor() {
  byId("catalog-editor").hidden = true;
  state.catalogModel = null;
}

function renderProviders(payload) {
  const table = byId("providers");
  const body = tableHead(table, ["状态", "备注名", "Provider 类型", "Base URL", "API Key", "费率倍率", "单 Key 并发上限", "模型库存", "24h 费用", "技术成功率", "平均首字延迟", "操作"]);
  payload.providers.forEach((provider) => {
    const row = document.createElement("tr");
    const status = document.createElement("span");
    status.className = `status ${provider.enabled ? "on" : "off"}`;
    status.textContent = provider.enabled ? "启用" : "停用";
    const statusCell = document.createElement("td");
    statusCell.append(status);
    const models = document.createElement("td");
    const tags = document.createElement("div");
    tags.className = "model-tags";
    (provider.models || []).forEach((model) => {
      const tag = document.createElement("span");
      tag.className = "model-tag";
      tag.textContent = model;
      tags.append(tag);
    });
    if (!tags.childElementCount) tags.textContent = "n/a";
    models.append(tags);
    row.append(statusCell, cell(provider.note), cell(provider.family), cell(provider.base_url), cell(provider.api_key_mask), cell(formatMultiplier(provider.multiplier)), cell(provider.max_parallel), models, cell(formatCost(provider.cost_24h)), cell(formatPercent(provider.technical_success_rate)), cell(formatMs(provider.avg_ttft_ms)));
    const action = document.createElement("td");
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "text-button";
    edit.textContent = "编辑";
    edit.addEventListener("click", () => openEditor(provider));
    action.append(edit);
    row.append(action);
    body.append(row);
  });
}

function renderCatalog(payload) {
  const table = byId("catalog");
  const body = tableHead(table, ["模型 ID", "模型家族", "stage", "输入 / 1M", "缓存输入 / 1M", "输出 / 1M", "整合价 / 1M", "可用 Key", "操作"]);
  Object.entries(payload.catalog).forEach(([model, item]) => {
    const row = document.createElement("tr");
    [model, item.family, item.intellect, formatPrice(item.official_input_price), formatPrice(item.official_cache_price), formatPrice(item.official_output_price), formatPrice(item.blended_price), item.available_provider_count].forEach((value) => row.append(cell(value)));
    const action = document.createElement("td");
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "text-button";
    edit.textContent = "编辑";
    edit.addEventListener("click", () => openCatalogEditor(model, item));
    action.append(edit);
    row.append(action);
    body.append(row);
  });
}

function metric(label, value) {
  const item = document.createElement("div");
  const name = document.createElement("span");
  const number = document.createElement("strong");
  name.textContent = label;
  number.textContent = empty(value);
  item.append(name, number);
  return item;
}

function renderQuality(payload) {
  const failures = payload.failures || {};
  byId("quality").replaceChildren(
    metric("技术成功率", formatPercent(payload.technical_success_rate)),
    metric("平均 TTFT", formatMs(payload.avg_ttft_ms)),
    metric("P95 TTFT", formatMs(payload.p95_ttft_ms)),
    metric("调用数", payload.calls),
    metric("总费用", formatCost(payload.total_cost)),
    metric("模型履约率", formatPercent(payload.model_fulfillment_rate)),
    metric("cancelled", failures.cancelled),
    metric("timed_out", failures.timed_out),
    metric("transport_failed", failures.transport_failed),
    metric("protocol_failed", failures.protocol_failed),
    metric("stream_incomplete", failures.stream_incomplete),
  );
}

function callsUrl(cursor = state.cursor) {
  return `/admin/v1/calls?${new URLSearchParams({
    window: byId("callwindow").value,
    limit: byId("calllimit").value,
    provider: byId("callprovider").value,
    status: byId("callstatus").value,
    cursor,
  })}`;
}

function renderCalls(payload) {
  const table = byId("calls");
  const body = tableHead(table, ["调用时间", "API Key 备注", "Provider", "请求模型", "实际模型", "intellect", "effort", "TTFT", "技术状态", "输入 Token", "输出 Token", "成本", "request ID"]);
  payload.items.forEach((item) => {
    const row = document.createElement("tr");
    [item.time, item.note, item.provider, item.requested_model, item.actual_model, item.intellect, item.effort, formatMs(item.ttft_ms), item.status, item.input_tokens, item.output_tokens, formatCost(item.cost), item.request_id].forEach((value) => row.append(cell(value)));
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

async function load() {
  const [summary, providers, catalog, quality, routing] = await Promise.all([
    requestJson("/admin/v1/summary?window=24h"),
    requestJson("/admin/v1/providers"),
    requestJson("/admin/v1/catalog"),
    requestJson(`/admin/v1/quality?window=${encodeURIComponent(state.qualityWindow)}`),
    requestJson("/admin/v1/routing"),
  ]);
  renderSummary(summary);
  renderProviders(providers);
  renderCatalog(catalog);
  renderQuality(quality);
  byId("race-parallel-cap").value = routing.race_parallel_cap;
  await loadCalls("");
}

byId("policy").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const policy = {
    note: form.elements.note.value,
    multiplier: Number(form.elements.multiplier.value),
    enabled: form.elements.enabled.checked,
    max_parallel: Number(form.elements.max_parallel.value),
  };
  await requestJson(`/admin/v1/policy/${encodeURIComponent(state.provider.fingerprint)}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(policy),
  });
  closeEditor();
  await load();
});

byId("save-routing").addEventListener("click", async () => {
  const race_parallel_cap = Number(byId("race-parallel-cap").value);
  const result = await requestJson("/admin/v1/routing", { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ race_parallel_cap }) });
  byId("race-parallel-cap").value = result.race_parallel_cap;
  byId("syncresult").textContent = `同价竞速 Key 数已设为 ${result.race_parallel_cap}`;
});

byId("catalog-create").addEventListener("click", () => openCatalogEditor());
byId("catalog-apply").addEventListener("click", async () => {
  const result = await requestJson("/admin/v1/catalog/apply", { method: "POST" });
  await load();
  byId("syncresult").textContent = `已应用目录：${result.providers} 个 Key，保留 ${result.retained_models} 个模型，移除 ${result.removed_models} 个模型`;
});
["official_input_price", "official_cache_price", "official_output_price"].forEach((name) => byId("catalog-form").elements[name].addEventListener("input", updateBlendedPrice));
byId("catalog-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const body = {
    family: form.elements.family.value,
    intellect: form.elements.intellect.value,
    official_input_price: Number(form.elements.official_input_price.value),
    official_cache_price: Number(form.elements.official_cache_price.value),
    official_output_price: Number(form.elements.official_output_price.value),
  };
  if (state.catalogModel) {
    await requestJson(`/admin/v1/catalog/${encodeURIComponent(state.catalogModel)}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  } else {
    await requestJson("/admin/v1/catalog", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...body, model: form.elements.model.value }) });
  }
  closeCatalogEditor();
  await load();
});
byId("catalog-delete").addEventListener("click", async () => {
  await requestJson(`/admin/v1/catalog/${encodeURIComponent(state.catalogModel)}`, { method: "DELETE" });
  closeCatalogEditor();
  await load();
});
byId("close-catalog-editor").addEventListener("click", closeCatalogEditor);
byId("cancel-catalog-editor").addEventListener("click", closeCatalogEditor);

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
  renderQuality(await requestJson(`/admin/v1/quality?window=${encodeURIComponent(state.qualityWindow)}`));
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
