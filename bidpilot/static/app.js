const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const state = { spec: null, run: null, system: null, activeTab: "search" };

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
}

function safeUrl(value = "") {
  try { const url = new URL(value); return ["http:", "https:"].includes(url.protocol) ? url.href : "#"; }
  catch { return "#"; }
}

function formatTime(value) {
  if (!value) return "尚未执行";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
}

function toast(message, duration = 3200) {
  const node = $("#toast"); node.textContent = message; node.classList.add("show");
  clearTimeout(window.__toastTimer); window.__toastTimer = setTimeout(() => node.classList.remove("show"), duration);
}

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `请求失败 (${response.status})`);
  return data;
}

function currentQuery() { return $("#query-input").value.trim(); }
function currentChannel() { return $("#delivery-channel").value || "local"; }

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
    const run = await api("/api/v1/runs", { method: "POST", body: JSON.stringify({ query, delivery_channel: currentChannel() }) });
    state.run = run; clearInterval(window.__pipeTimer); $$(".pipe-step").forEach((step) => { step.classList.remove("active"); step.classList.add("done"); });
    $("#run-state").innerHTML = "✓ 执行完成"; renderResults(run); await Promise.all([loadReports(), loadSources()]);
  } catch (error) { clearInterval(window.__pipeTimer); toast(error.message, 5000); $("#run-state").textContent = "执行失败"; }
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
      const links = item.source_urls.map((url, i) => `<a href="${escapeHtml(safeUrl(url))}" target="_blank" rel="noreferrer">来源 ${i + 1} ↗</a>`).join("");
      return `<article class="result-card"><div class="result-top"><div><h3>${escapeHtml(item.title)}</h3><div class="record-meta"><span><b>${escapeHtml(item.event_type)}</b></span><span>${escapeHtml(item.published_at.slice(0,10))}</span><span>${escapeHtml(item.region || "地域未标注")}</span><span>${escapeHtml(item.buyer || "采购人未提取")}</span><span>合并 ${item.duplicate_count} 条</span></div></div><div class="score">${Math.round(item.opportunity_score)}</div></div><p class="summary">${escapeHtml(item.summary)}</p><div class="record-links">${links}</div></article>`;
    }).join("");
  }
  $("#results-panel").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function loadSources() {
  const rows = await api("/api/v1/sources/status");
  $("#source-list").innerHTML = rows.map((row) => {
    const warning = !row.configured || ["partial", "failed", "auth_required"].includes(row.last_status);
    const detail = row.last_message || (row.configured ? "等待首次运行" : "待授权或配置");
    return `<div class="rail-source" title="${escapeHtml(detail)}"><span>${row.official ? "◆ " : ""}${escapeHtml(row.name)}</span><i class="${warning ? "warn" : ""}"></i></div>`;
  }).join("");
  renderSourceCenter(rows);
  return rows;
}

function sourceState(row) {
  if (!row.configured) return ["待授权", "muted"];
  if (row.last_status === "failed") return ["最近失败", "danger"];
  if (["partial", "auth_required"].includes(row.last_status)) return ["部分可用", "warning"];
  if (row.last_status === "ok") return ["运行正常", "success"];
  return ["等待首轮", "muted"];
}

function renderSourceCenter(rows) {
  const summary = $("#source-summary"), root = $("#source-center-list");
  if (!summary || !root) return;
  const configured = rows.filter((row) => row.configured).length;
  const official = rows.filter((row) => row.official).length;
  const fetched = rows.reduce((sum, row) => sum + (row.last_fetched_count || 0), 0);
  summary.innerHTML = [["已接入来源", rows.length], ["官方平台", official], ["当前可运行", configured], ["最近抓取候选", fetched]].map(([label, value]) => `<div class="metric"><small>${label}</small><b>${value}</b></div>`).join("");
  root.innerHTML = rows.map((row) => {
    const [label, tone] = sourceState(row);
    const access = row.member_enhanced ? `${row.mode} · 已授权增强` : row.mode;
    const checked = row.last_checked_at ? formatTime(row.last_checked_at) : "尚未运行";
    const message = row.last_message || (row.configured ? "等待首轮真实查询验证" : "需要用户在本机完成授权或配置");
    return `<article class="source-card"><div class="source-card-head"><div><h3>${escapeHtml(row.name)}</h3><div class="source-tags"><span>${row.official ? "官方来源" : "行业来源"}</span><span>${escapeHtml(access)}</span></div></div><span class="state-badge ${tone}">${label}</span></div><div class="source-stats"><span><small>最近检查</small><b>${checked}</b></span><span><small>抓取 / 保留</small><b>${row.last_fetched_count || 0} / ${row.last_kept_count || 0}</b></span></div><p>${escapeHtml(message)}</p>${!row.configured ? `<div class="source-action-note">需授权源不会被静默伪装成成功；完成本机登录后，下次任务自动启用。</div>` : ""}</article>`;
  }).join("");
}

function renderDeliveryOptions() {
  const select = $("#delivery-channel"), previous = select.value || "local";
  const channels = state.system?.delivery_channels || [];
  select.innerHTML = channels.map((channel) => `<option value="${escapeHtml(channel.id)}" ${channel.configured ? "" : "disabled"}>${escapeHtml(channel.name)}${channel.configured ? "" : "（待配置）"}</option>`).join("");
  select.value = channels.some((item) => item.id === previous && item.configured) ? previous : "local";
  renderChannelHint();
}

function renderChannelHint() {
  const channel = state.system?.delivery_channels?.find((item) => item.id === currentChannel());
  $("#channel-hint").textContent = channel?.message || "请选择交付方式。";
}

function renderSchedulerHealth() {
  const root = $("#scheduler-health"); if (!state.system || !root) return;
  const online = state.system.worker_online;
  root.classList.toggle("offline", !online);
  root.innerHTML = `<div><i></i><b>${online ? "长期任务服务在线" : "长期任务服务离线"}</b><p>${online ? `已启用 ${state.system.enabled_subscription_count} 个 · 正在执行 ${state.system.running_subscription_count || 0} 个 · 到期等待 ${state.system.due_count} 个 · ${escapeHtml(state.system.timezone)}` : "任务已安全保存但不会自动执行。请启动：python -m bidpilot worker"}</p></div><button class="secondary-button" id="refresh-subscriptions">刷新状态</button>`;
  $("#refresh-subscriptions").addEventListener("click", () => loadSubscriptions().catch((error) => toast(error.message)));
}

async function loadSystemStatus() {
  state.system = await api("/api/v1/system/status");
  renderDeliveryOptions(); renderSchedulerHealth(); return state.system;
}

function channelOptions(selected) {
  return (state.system?.delivery_channels || []).map((channel) => `<option value="${escapeHtml(channel.id)}" ${channel.id === selected ? "selected" : ""} ${!channel.configured && channel.id !== selected ? "disabled" : ""}>${escapeHtml(channel.name)}${channel.configured ? "" : "（待配置）"}</option>`).join("");
}

function subscriptionState(row) {
  if (row.in_progress) return ["执行中", "warning"];
  if (!row.enabled) return row.spec.schedule.kind === "once" && row.last_run_at ? ["已完成", "muted"] : ["已暂停", "muted"];
  if (row.last_status === "failed") return ["等待重试", "danger"];
  if (row.next_run_at && new Date(row.next_run_at) <= new Date()) return ["待领取", "warning"];
  return ["已启用", "success"];
}

async function loadSubscriptions() {
  const [, rows] = await Promise.all([loadSystemStatus(), api("/api/v1/subscriptions")]);
  const root = $("#subscription-list");
  if (!rows.length) { root.innerHTML = `<div class="loading-card">尚无订阅。在情报检索中输入“每天 / 每周 / 今天 9:00 发送”即可创建。</div>`; return; }
  root.innerHTML = rows.map((row) => {
    const [label, tone] = subscriptionState(row);
    return `<article class="subscription-card" data-id="${escapeHtml(row.id)}">
      <div class="subscription-main"><div class="subscription-title"><div><h3>${escapeHtml(row.name)}</h3><p>${escapeHtml(row.spec.raw_query)}</p></div><span class="state-badge ${tone}">${label}</span></div>
      <div class="subscription-meta"><span><small>计划</small><b>${escapeHtml(row.spec.schedule.expression)}</b></span><span><small>下次执行</small><b>${formatTime(row.next_run_at)}</b></span><span><small>最近执行</small><b>${formatTime(row.last_run_at)}</b></span><span><small>最近新增</small><b>${row.last_new_count || 0} 条</b></span></div>
      <p class="last-message ${row.last_status === "failed" ? "error" : ""}">${escapeHtml(row.last_message || "首轮运行尚未完成；worker 将按到期时间领取。")}</p></div>
      <div class="subscription-controls"><label><span>交付</span><select class="subscription-channel">${channelOptions(row.delivery_channel)}</select></label><label><span>无新增</span><select class="subscription-policy"><option value="always" ${row.delivery_policy === "always" ? "selected" : ""}>发送回执</option><option value="on_change" ${row.delivery_policy === "on_change" ? "selected" : ""}>保持安静</option></select></label>
      <div class="row-actions"><button class="secondary-button run-now" ${row.in_progress ? "disabled" : ""}>${row.in_progress ? "执行中…" : "立即执行"}</button><button class="secondary-button toggle-enabled" ${row.in_progress ? "disabled" : ""}>${row.in_progress ? "本轮完成后可暂停" : row.enabled ? "暂停后续" : "恢复"}</button><button class="secondary-button edit-subscription" ${row.in_progress ? "disabled" : ""}>编辑规则</button><button class="secondary-button view-log">日志</button><button class="danger-button delete-subscription" ${row.in_progress ? "disabled" : ""}>删除</button></div></div>
      <div class="subscription-editor hidden"><div class="editor-grid"><label><span>订阅名称</span><input class="subscription-name" value="${escapeHtml(row.name)}" maxlength="100"></label><label class="editor-query"><span>自然语言规则</span><textarea class="subscription-query" rows="3" maxlength="500">${escapeHtml(row.spec.raw_query)}</textarea></label></div><p>修改后会重新计算下次执行时间；既有投递账本保留，避免同一公告重复提醒。</p><div class="editor-actions"><button class="primary-button compact save-subscription">保存规则</button><button class="secondary-button cancel-edit">取消</button></div></div>
      <div class="subscription-log hidden"></div></article>`;
  }).join("");
  bindSubscriptionActions();
}

function bindSubscriptionActions() {
  $$(".subscription-card").forEach((card) => {
    const id = card.dataset.id;
    card.querySelector(".run-now").addEventListener("click", async (event) => {
      const button = event.currentTarget; button.disabled = true; button.textContent = "执行中…";
      try { const run = await api(`/api/v1/subscriptions/${id}/run`, { method: "POST" }); toast(`运行完成：${run.new_count} 条新增；${run.delivery_message}`); await Promise.all([loadSubscriptions(), loadReports()]); }
      catch (error) { toast(error.message, 5000); await loadSubscriptions(); }
      finally { if (button.isConnected) { button.disabled = false; button.textContent = "立即执行"; } }
    });
    card.querySelector(".toggle-enabled").addEventListener("click", async () => {
      const rowPaused = card.querySelector(".state-badge").textContent.includes("暂停");
      const action = rowPaused ? "resume" : "pause";
      await api(`/api/v1/subscriptions/${id}/${action}`, { method: "POST", body: action === "resume" ? JSON.stringify({ run_immediately: false }) : undefined });
      toast(action === "resume" ? "订阅已恢复" : "订阅已暂停"); await loadSubscriptions();
    });
    card.querySelector(".subscription-policy").addEventListener("change", async (event) => {
      try { await api(`/api/v1/subscriptions/${id}`, { method: "PATCH", body: JSON.stringify({ delivery_policy: event.target.value }) }); toast("通知策略已更新"); }
      catch (error) { toast(error.message); await loadSubscriptions(); }
    });
    card.querySelector(".subscription-channel").addEventListener("change", async (event) => {
      try { await api(`/api/v1/subscriptions/${id}`, { method: "PATCH", body: JSON.stringify({ delivery_channel: event.target.value }) }); toast("交付通道已更新"); }
      catch (error) { toast(error.message); await loadSubscriptions(); }
    });
    card.querySelector(".edit-subscription").addEventListener("click", () => card.querySelector(".subscription-editor").classList.remove("hidden"));
    card.querySelector(".cancel-edit").addEventListener("click", () => card.querySelector(".subscription-editor").classList.add("hidden"));
    card.querySelector(".save-subscription").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      const name = card.querySelector(".subscription-name").value.trim();
      const query = card.querySelector(".subscription-query").value.trim();
      if (!name || query.length < 2) return toast("请填写订阅名称和完整规则");
      button.disabled = true; button.textContent = "保存中…";
      try {
        await api(`/api/v1/subscriptions/${id}`, { method: "PATCH", body: JSON.stringify({ name, query }) });
        toast("订阅规则已更新，下次执行时间已重新计算"); await loadSubscriptions();
      } catch (error) { toast(error.message, 5000); button.disabled = false; button.textContent = "保存规则"; }
    });
    card.querySelector(".view-log").addEventListener("click", () => toggleSubscriptionLog(card, id).catch((error) => toast(error.message)));
    card.querySelector(".delete-subscription").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      if (button.dataset.confirm !== "true") {
        button.dataset.confirm = "true"; button.textContent = "再次点击确认";
        toast("再次点击将停止任务并清除该订阅的增量账本", 8000);
        clearTimeout(button.__confirmTimer);
        button.__confirmTimer = setTimeout(() => { if (button.isConnected) { button.dataset.confirm = "false"; button.textContent = "删除"; } }, 10000);
        return;
      }
      button.disabled = true; button.textContent = "删除中…";
      try { await api(`/api/v1/subscriptions/${id}`, { method: "DELETE" }); toast("订阅已删除"); await loadSubscriptions(); }
      catch (error) { toast(error.message, 5000); button.disabled = false; button.dataset.confirm = "false"; button.textContent = "删除"; }
    });
  });
}

async function toggleSubscriptionLog(card, id) {
  const root = card.querySelector(".subscription-log");
  if (!root.classList.contains("hidden")) { root.classList.add("hidden"); return; }
  root.classList.remove("hidden"); root.innerHTML = "正在读取运行与投递日志…";
  const [runs, deliveries] = await Promise.all([api(`/api/v1/subscriptions/${id}/runs?limit=6`), api(`/api/v1/subscriptions/${id}/deliveries`)]);
  if (!runs.length) { root.innerHTML = "尚无运行记录。"; return; }
  root.innerHTML = runs.map((run) => {
    const attempt = deliveries.find((item) => item.run_id === run.id);
    const labels = { completed: "完成", partial: "部分完成", failed: "失败", running: "执行中", queued: "排队中" };
    const reason = run.trigger_reason === "schedule" ? "自动" : "手动";
    const report = run.report_path ? `<a href="/api/v1/reports/${encodeURIComponent(run.report_path.replaceAll("\\", "/").split("/").pop())}">报告 ↗</a>` : "";
    return `<div class="log-row"><span class="log-status ${escapeHtml(run.status)}">${labels[run.status] || escapeHtml(run.status)}</span><b>${formatTime(run.started_at)} · ${reason}</b><span>发现 ${run.result_count} / 新增 ${run.new_count}</span><em>${escapeHtml(attempt?.message || run.error || "已完成")} ${report}</em></div>`;
  }).join("");
}

async function loadReports() {
  const rows = await api("/api/v1/reports"); const root = $("#report-list");
  if (!rows.length) { root.innerHTML = `<div class="loading-card">尚无报告。完成一次情报任务后，Word 报告会出现在这里。</div>`; return; }
  root.innerHTML = rows.map((row) => { const name = row.path.replaceAll("\\", "/").split("/").pop(); return `<article class="data-row"><div><h3>${escapeHtml(name)}</h3><p>${row.item_count} 条 · ${formatTime(row.created_at)}</p></div><div class="row-actions"><span class="tag">DOCX</span><a class="secondary-button" href="/api/v1/reports/${encodeURIComponent(name)}">下载</a></div></article>`; }).join("");
}

async function createSubscriptionFromQuery() {
  const button = $("#create-subscription-button"); button.disabled = true;
  try {
    const query = currentQuery(); const spec = state.spec || await parseIntent();
    if (spec.schedule.kind === "immediate") return toast("问题中需要包含每天、每周或未来发送时间");
    const name = `${spec.region || "全国"} · ${spec.topic} · ${spec.schedule.expression}`;
    const subscription = await api("/api/v1/subscriptions", { method: "POST", body: JSON.stringify({ name, query, delivery_channel: currentChannel(), delivery_policy: $("#delivery-policy").value, run_immediately: true }) });
    await activateTab("subscriptions");
    const workerOnline = state.system?.worker_online;
    toast(workerOnline ? "订阅已保存，首轮扫描已进入持久队列" : "订阅已保存，但 worker 离线；启动后会自动补跑", 5000);
    pollSubscription(subscription.id, subscription.last_run_at);
  } finally { button.disabled = false; }
}

function pollSubscription(id, previousLastRunAt = null) {
  const started = Date.now(); clearInterval(window.__subscriptionPoll);
  window.__subscriptionPoll = setInterval(async () => {
    if (Date.now() - started > 300000) {
      clearInterval(window.__subscriptionPoll);
      return toast("任务仍保存在后台；可在订阅中心继续查看状态和日志", 5000);
    }
    try {
      const row = await api(`/api/v1/subscriptions/${id}`); await loadSubscriptions();
      if (row.last_run_at && row.last_run_at !== previousLastRunAt) { clearInterval(window.__subscriptionPoll); toast(row.last_status === "failed" ? `本轮执行失败，系统已安排重试：${row.last_message}` : `本轮执行完成：新增 ${row.last_new_count} 条`, 5000); }
    } catch { clearInterval(window.__subscriptionPoll); }
  }, 3000);
}

async function activateTab(tab) {
  state.activeTab = tab;
  $$(".nav-link").forEach((node) => node.classList.toggle("active", node.dataset.tab === tab));
  $$(".tab-panel").forEach((node) => node.classList.remove("active")); $(`#tab-${tab}`).classList.add("active");
  if (tab === "subscriptions") await loadSubscriptions(); if (tab === "sources") await loadSources(); if (tab === "reports") await loadReports();
}

$$(".nav-link").forEach((button) => button.addEventListener("click", () => activateTab(button.dataset.tab).catch((error) => toast(error.message))));
$$("[data-query]").forEach((button) => button.addEventListener("click", () => { $("#query-input").value = button.dataset.query; state.spec = null; }));
$("#parse-button").addEventListener("click", () => parseIntent().catch((error) => toast(error.message)));
$("#run-button").addEventListener("click", runQuery);
$("#create-subscription-button").addEventListener("click", () => createSubscriptionFromQuery().catch((error) => toast(error.message, 5000)));
$("#delivery-channel").addEventListener("change", renderChannelHint);
$("#query-input").addEventListener("keydown", (event) => { if ((event.metaKey || event.ctrlKey) && event.key === "Enter") runQuery(); });
document.addEventListener("keydown", (event) => { if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); $("#query-input").focus(); } });
window.createBidPilotSubscription = createSubscriptionFromQuery;
Promise.all([loadSystemStatus(), loadSources(), loadReports(), loadSubscriptions()]).catch((error) => toast(error.message));
setInterval(() => {
  const editing = $(".subscription-editor:not(.hidden)");
  if (state.activeTab === "subscriptions" && !editing) loadSubscriptions().catch(() => {});
}, 15000);
