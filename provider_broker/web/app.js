const byId = (id) => document.getElementById(id);
const sortDefaults = {
  providers: { key: "note", direction: "asc" },
  catalog: { key: "model", direction: "asc" },
  modelView: { key: "intellect", direction: "asc" },
  calls: { key: "time", direction: "desc" },
};
const state = { cursor: "", provider: null, catalogModel: null, callsRequest: 0, filterTimer: null, qualityWindow: "24h", providers: [], catalog: {}, sorts: {} };
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
  state.providers = payload.providers;
  const table = byId("providers");
  const columns = [{ key: "base_url", label: "域名" }, { key: "enabled", label: "状态" }, { key: "note", label: "备注名" }, { key: "api_key_mask", label: "API Key" }, { key: "family", label: "Provider 类型" }, { key: "multiplier", label: "费率倍率" }, { key: "max_parallel", label: "单 Key 并发上限" }, { key: "models", label: "模型库存" }, { key: "cost_24h", label: "24h 费用" }, { key: "technical_success_rate", label: "技术成功率" }, { key: "avg_ttft_ms", label: "平均首字延迟" }, { label: "操作" }];
  const body = tableHead(table, columns, "providers", () => renderProviders({ providers: state.providers }));
  const groups = [...payload.providers.reduce((byUrl, provider) => {
    const domain = providerDomain(provider.base_url);
    const group = byUrl.get(domain) || { domain, providers: [] };
    group.providers.push(provider);
    byUrl.set(domain, group);
    return byUrl;
  }, new Map()).values()];
  const valueFor = (provider, key) => key === "models" ? provider.models.join(" ") : provider[key];
  sortItems(groups, "providers", (group, key) => key === "base_url" ? group.domain : valueFor(sortItems(group.providers, "providers", valueFor)[0], key)).forEach((group) => {
    const providers = sortItems(group.providers, "providers", valueFor);
    providers.forEach((provider, index) => {
      const row = document.createElement("tr");
      if (index === 0) {
        const domain = cell(group.domain);
        domain.rowSpan = providers.length;
        row.append(domain);
      }
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
      row.append(statusCell, cell(provider.note), cell(provider.api_key_mask), cell(provider.family), cell(formatMultiplier(provider.multiplier)), cell(provider.max_parallel), models, cell(formatCost(provider.cost_24h)), cell(formatPercent(provider.technical_success_rate)), cell(formatMs(provider.avg_ttft_ms)));
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
  });
}

function providerDomain(baseUrl) {
  try { return new URL(baseUrl).origin; } catch (_) { return baseUrl; }
}

function renderCatalog(payload) {
  state.catalog = payload.catalog;
  const table = byId("catalog");
  const columns = [{ key: "model", label: "模型 ID" }, { key: "family", label: "模型家族" }, { key: "intellect", label: "stage" }, { key: "official_input_price", label: "输入 / 1M" }, { key: "official_cache_price", label: "缓存输入 / 1M" }, { key: "official_output_price", label: "输出 / 1M" }, { key: "blended_price", label: "整合价 / 1M" }, { key: "available_provider_count", label: "可用 Key" }, { label: "操作" }];
  const body = tableHead(table, columns, "catalog", () => renderCatalog({ catalog: state.catalog }));
  const entries = sortItems(Object.entries(payload.catalog).map(([model, item]) => ({ model, ...item })), "catalog", (item, key) => key === "intellect" ? stageOrder(item.intellect) : item[key]);
  entries.forEach((item) => {
    const row = document.createElement("tr");
    [item.model, item.family, item.intellect, formatPrice(item.official_input_price), formatPrice(item.official_cache_price), formatPrice(item.official_output_price), formatPrice(item.blended_price), item.available_provider_count].forEach((value) => row.append(cell(value)));
    const action = document.createElement("td");
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "text-button";
    edit.textContent = "编辑";
    edit.addEventListener("click", () => openCatalogEditor(item.model, item));
    action.append(edit);
    row.append(action);
    body.append(row);
  });
}

function stageOrder(value) {
  return ({ standard: 0, smart: 1, expert: 2 })[value] ?? 99;
}

function renderModelView() {
  const table = byId("model-view");
  const columns = [{ key: "intellect", label: "模型分组" }, { key: "model", label: "模型名称" }, { key: "price_band", label: "价格组" }, { key: "note", label: "备注名" }, { key: "price", label: "模型价格 / 1M" }];
  const body = tableHead(table, columns, "modelView", renderModelView);
  const models = Object.entries(state.catalog).map(([model, item]) => ({
    model,
    intellect: item.intellect,
    providers: state.providers.filter((provider) => provider.models.includes(model)).map((provider) => ({
      note: provider.note || "n/a",
      price: Number(item.blended_price) * Number(provider.multiplier),
    })),
  }));
  const sorted = sortItems(models, "modelView", (item, key) => {
    if (key === "intellect") return stageOrder(item.intellect);
    if (key === "price_band" || key === "price") return item.providers.length ? Math.min(...item.providers.map((provider) => provider.price)) : null;
    if (key === "note") return item.providers.map((provider) => provider.note).join(" ");
    return item.model;
  });
  const groups = [...sorted.reduce((byStage, item) => {
    const group = byStage.get(item.intellect) || { intellect: item.intellect, models: [] };
    group.models.push(item);
    byStage.set(item.intellect, group);
    return byStage;
  }, new Map()).values()];
  groups.forEach((group) => {
    const groupRowCount = group.models.reduce((count, item) => count + priceBands(item.providers).reduce((total, band) => total + band.providers.length, 0), 0);
    let groupRowIndex = 0;
    group.models.forEach((item) => {
      const bands = priceBands(item.providers);
      const modelRowCount = bands.reduce((count, band) => count + band.providers.length, 0);
      let modelRowIndex = 0;
      bands.forEach((band) => {
        band.providers.forEach((provider, index) => {
          const row = document.createElement("tr");
          if (groupRowIndex === 0) {
            const intellect = cell(group.intellect);
            intellect.rowSpan = groupRowCount;
            row.append(intellect);
          }
          if (modelRowIndex === 0) {
            const model = cell(item.model);
            model.rowSpan = modelRowCount;
            row.append(model);
          }
          if (index === 0) {
            const priceBand = cell(band.label);
            priceBand.rowSpan = band.providers.length;
            row.append(priceBand);
          }
          row.append(cell(provider.note), cell(formatCost(provider.price)));
          body.append(row);
          groupRowIndex += 1;
          modelRowIndex += 1;
        });
      });
    });
  });
}

function priceBands(providers) {
  if (!providers.length) return [{ label: "n/a", providers: [{ note: "n/a", price: null }] }];
  const prices = [...new Set(providers.map((provider) => provider.price))].sort((left, right) => left - right);
  if (prices.length === 1) return [{ label: "低价组", providers }];
  const split = prices.slice(0, -1).reduce((best, price, index) => prices[index + 1] - price > prices[best + 1] - prices[best] ? index : best, 0);
  const lower = new Set(prices.slice(0, split + 1));
  return [
    { label: "低价组", providers: providers.filter((provider) => lower.has(provider.price)) },
    { label: "高价组", providers: providers.filter((provider) => !lower.has(provider.price)) },
  ];
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
  const sort = sortFor("calls");
  return `/admin/v1/calls?${new URLSearchParams({
    window: byId("callwindow").value,
    limit: byId("calllimit").value,
    provider: byId("callprovider").value,
    status: byId("callstatus").value,
    cursor,
    sort: `${sort.key}:${sort.direction}`,
  })}`;
}

function renderCalls(payload) {
  const table = byId("calls");
  const columns = [{ key: "time", label: "调用时间" }, { key: "note", label: "API Key 备注" }, { key: "provider", label: "Provider" }, { key: "requested_model", label: "请求模型" }, { key: "actual_model", label: "实际模型" }, { key: "intellect", label: "intellect" }, { key: "effort", label: "effort" }, { key: "ttft", label: "TTFT" }, { key: "status", label: "技术状态" }, { key: "input_tokens", label: "输入 Token" }, { key: "output_tokens", label: "输出 Token" }, { key: "cost", label: "成本" }, { key: "request_id", label: "request ID" }];
  const body = tableHead(table, columns, "calls", () => loadCalls(""));
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
  renderModelView();
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
