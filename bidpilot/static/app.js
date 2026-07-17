const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const state = { spec: null, run: null };

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
}

function toast(message) {
  const node = $("#toast"); node.textContent = message; node.classList.add("show");
  clearTimeout(window.__toastTimer); window.__toastTimer = setTimeout(() => node.classList.remove("show"), 2600);
}

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `请求失败 (${response.status})`);
  return data;
}

function currentQuery() { return $("#query-input").value.trim(); }

async function parseIntent() {
  const query = currentQuery(); if (query.length < 2) return toast("请先输入查询问题");
  const spec = await api("/api/v1/intent/parse", { method: "POST", body: JSON.stringify({ query }) });
  state.spec = spec; renderIntent(spec); return spec;
}

function renderIntent(spec) {
  $("#intent-panel").classList.remove("hidden");
  const confidence = Math.min(...Object.values(spec.slot_confidence || { value: .8 }));
  $("#intent-confidence").textContent = confidence >= .9 ? "高置信" : confidence >= .7 ? "可执行" : "建议确认";
  const cells = [
    ["主题 / TOPIC", spec.topic], ["地域 / REGION", spec.region || "全国"],
    ["时间 / WINDOW", `${spec.start_date} → ${spec.end_date}`], ["计划 / SCHEDULE", spec.schedule.expression]
  ];
  $("#intent-grid").innerHTML = cells.map(([label, value]) => `<div class="intent-cell"><small>${label}</small><b>${escapeHtml(value)}</b></div>`).join("");
  $("#intent-warnings").innerHTML = (spec.warnings || []).map((warning) => `⚠ ${escapeHtml(warning)}`).join("<br>");
  $("#create-subscription-button").classList.toggle("hidden", spec.schedule.kind === "immediate");
  $("#intent-panel").scrollIntoView({ behavior: "smooth", block: "center" });
}

function animatePipeline() {
  const steps = $$(".pipe-step"); steps.forEach((step) => step.classList.remove("active", "done"));
  let index = 0; steps[0].classList.add("active");
  window.__pipeTimer = setInterval(() => {
    steps[index].classList.remove("active"); steps[index].classList.add("done"); index += 1;
    if (index >= steps.length) return clearInterval(window.__pipeTimer);
    steps[index].classList.add("active");
  }, 1200);
}

async function runQuery() {
  const query = currentQuery(); if (query.length < 2) return toast("请先输入查询问题");
  const button = $("#run-button"); button.disabled = true; button.querySelector("span").textContent = "多源采集中…";
  try {
    if (!state.spec || state.spec.raw_query !== query) await parseIntent();
    $("#run-panel").classList.remove("hidden"); $("#results-panel").classList.add("hidden"); animatePipeline();
    $("#run-panel").scrollIntoView({ behavior: "smooth", block: "center" });
    const run = await api("/api/v1/runs", { method: "POST", body: JSON.stringify({ query, delivery_channel: "local" }) });
    state.run = run; clearInterval(window.__pipeTimer); $$(".pipe-step").forEach((step) => { step.classList.remove("active"); step.classList.add("done"); });
    $("#run-state").innerHTML = "✓ 执行完成"; renderResults(run); await Promise.all([loadReports(), loadSources()]);
  } catch (error) { toast(error.message); $("#run-state").textContent = "执行失败"; }
  finally { button.disabled = false; button.querySelector("span").textContent = "启动情报任务"; }
}

function renderResults(run) {
  $("#results-panel").classList.remove("hidden");
  $("#result-summary").textContent = `${run.new_count} 条可信结果 · ${run.status === "partial" ? "部分来源受限" : "来源完整"}`;
  const download = $("#download-report");
  if (run.report_path) { const name = run.report_path.replaceAll("\\", "/").split("/").pop(); download.href = `/api/v1/reports/${encodeURIComponent(name)}`; download.classList.remove("hidden"); } else download.classList.add("hidden");
  const high = run.records.filter((item) => item.opportunity_score >= 80).length;
  const projects = new Set(run.records.map((item) => item.lifecycle_id)).size;
  const merged = run.records.reduce((sum, item) => sum + Math.max(0, item.duplicate_count - 1), 0);
  $("#metric-strip").innerHTML = [["可信结果", run.records.length], ["高优机会", high], ["项目生命线", projects], ["重复合并", merged]].map(([label, value]) => `<div class="metric"><small>${label}</small><b>${value}</b></div>`).join("");
  $("#source-coverage").innerHTML = run.diagnostics.map((item) => `<span class="source-chip ${escapeHtml(item.status)}">${escapeHtml(item.source)} · ${item.kept_count}/${item.fetched_count} · ${escapeHtml(item.status)}</span>`).join("");
  const list = $("#result-list"), empty = $("#empty-state");
  if (!run.records.length) { list.innerHTML = ""; empty.classList.remove("hidden"); }
  else {
    empty.classList.add("hidden");
    list.innerHTML = run.records.map((item) => {
      const links = item.source_urls.map((url, i) => `<a href="${escapeHtml(url)}" target="_blank" rel="noreferrer">来源 ${i + 1} ↗</a>`).join("");
      return `<article class="result-card"><div class="result-top"><div><h3>${escapeHtml(item.title)}</h3><div class="record-meta"><span><b>${escapeHtml(item.event_type)}</b></span><span>${escapeHtml(item.published_at.slice(0,10))}</span><span>${escapeHtml(item.region || "地域未标注")}</span><span>${escapeHtml(item.buyer || "采购人未提取")}</span><span>合并 ${item.duplicate_count} 条</span></div></div><div class="score">${Math.round(item.opportunity_score)}</div></div><p class="summary">${escapeHtml(item.summary)}</p><div class="record-links">${links}</div></article>`;
    }).join("");
  }
  $("#results-panel").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function loadSources() {
  const rows = await api("/api/v1/sources/status");
  $("#source-list").innerHTML = rows.map((row) => `<div class="rail-source"><span>${escapeHtml(row.name)}</span><i class="${row.configured ? "" : "warn"}" title="${row.configured ? "已就绪" : "待授权"}"></i></div>`).join("");
}

async function loadSubscriptions() {
  const rows = await api("/api/v1/subscriptions"); const root = $("#subscription-list");
  if (!rows.length) { root.innerHTML = `<div class="loading-card">尚无订阅。在情报检索中输入“每天 / 每周 / 今天 9:00 发送”即可创建。</div>`; return; }
  root.innerHTML = rows.map((row) => `<article class="data-row"><div><h3>${escapeHtml(row.name)}</h3><p>${escapeHtml(row.raw_query)} · ${escapeHtml(row.spec.schedule.expression)}</p></div><div class="row-actions"><span class="tag">${row.enabled ? "运行中" : "已停用"}</span><button class="secondary-button compact-run" data-id="${row.id}">立即试跑</button></div></article>`).join("");
  $$(".compact-run").forEach((button) => button.addEventListener("click", async () => { button.disabled = true; try { const run = await api(`/api/v1/subscriptions/${button.dataset.id}/run`, { method:"POST" }); toast(`试跑完成：${run.new_count} 条新增`); } catch (error) { toast(error.message); } finally { button.disabled = false; } }));
}

async function loadReports() {
  const rows = await api("/api/v1/reports"); const root = $("#report-list");
  if (!rows.length) { root.innerHTML = `<div class="loading-card">尚无报告。完成一次情报任务后，Word 报告会出现在这里。</div>`; return; }
  root.innerHTML = rows.map((row) => { const name = row.path.replaceAll("\\", "/").split("/").pop(); return `<article class="data-row"><div><h3>${escapeHtml(name)}</h3><p>${row.item_count} 条 · ${escapeHtml(row.created_at.slice(0,19).replace("T"," "))}</p></div><div class="row-actions"><span class="tag">DOCX</span><a class="secondary-button" href="/api/v1/reports/${encodeURIComponent(name)}">下载</a></div></article>`; }).join("");
}

async function createSubscriptionFromQuery() {
  const button = $('#create-subscription-button'); button.disabled = true;
  try {
  const query = currentQuery(); const spec = state.spec || await parseIntent();
  if (spec.schedule.kind === "immediate") return toast("问题中需要包含每天、每周或未来发送时间");
  const name = `${spec.region || "全国"} · ${spec.topic} · ${spec.schedule.expression}`;
  await api("/api/v1/subscriptions", { method:"POST", body:JSON.stringify({ name, query, delivery_channel:"local" }) });
  toast("增量订阅已创建"); await loadSubscriptions();
  } finally { button.disabled = false; }
}

$$('.nav-link').forEach((button) => button.addEventListener('click', async () => {
  $$('.nav-link').forEach((node) => node.classList.remove('active')); button.classList.add('active');
  $$('.tab-panel').forEach((node) => node.classList.remove('active')); $(`#tab-${button.dataset.tab}`).classList.add('active');
  if (button.dataset.tab === 'subscriptions') await loadSubscriptions(); if (button.dataset.tab === 'reports') await loadReports();
}));
$$('[data-query]').forEach((button) => button.addEventListener('click', () => { $('#query-input').value = button.dataset.query; state.spec = null; }));
$('#parse-button').addEventListener('click', () => parseIntent().catch((error) => toast(error.message)));
$('#run-button').addEventListener('click', runQuery);
$('#create-subscription-button').addEventListener('click', () => createSubscriptionFromQuery().catch((error) => toast(error.message)));
$('#query-input').addEventListener('keydown', (event) => { if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') runQuery(); });
document.addEventListener('keydown', (event) => { if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); $('#query-input').focus(); } });
window.createBidPilotSubscription = createSubscriptionFromQuery;
Promise.all([loadSources(), loadReports(), loadSubscriptions()]).catch((error) => toast(error.message));
