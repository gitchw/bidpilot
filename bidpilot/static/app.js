const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const state = {
  spec: null,
  run: null,
  system: null,
  config: null,
  configEditToken: null,
  activeTab: "search",
  opportunityProjectKeys: new Set(),
  sources: [],
};
const OPPORTUNITY_STAGES = {
  new: "待评估", following: "跟进中", bidding: "投标准备",
  won: "已中标", lost: "未中标", archived: "已归档"
};
const FILTER_REASON_LABELS = {
  outside_time: "超出时间范围", region_mismatch: "地域不匹配",
  event_type_mismatch: "公告类型不匹配", excluded_keyword: "命中排除词",
  keyword_mismatch: "主题/同义词未命中", low_relevance: "相关度不足",
};
const SOURCE_STATUS_LABELS = {
  ok: "正常完成", partial: "覆盖不完整",
  auth_required: "需要登录", failed: "抓取失败", skipped: "本轮未调用",
};

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

function formatDateTimeInput(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 16);
}

function toast(message, duration = 3200) {
  const node = $("#toast"); node.textContent = message; node.classList.add("show");
  clearTimeout(window.__toastTimer); window.__toastTimer = setTimeout(() => node.classList.remove("show"), duration);
}

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}) } });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const validation = Array.isArray(data.detail) ? data.detail.map((item) => `${(item.loc || []).slice(1).join(".") || "请求"}：${item.msg}`).join("；") : null;
    const error = new Error(typeof data.detail === "string" ? data.detail : validation || `请求失败 (${response.status})`);
    error.status = response.status; throw error;
  }
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
  const resolution = spec.resolution || { mode: "rules", llm_status: "not_needed", trigger_reasons: [], decisions: [], summary: "规则解析结果可直接使用。" };
  const resolutionLabels = {
    not_needed: ["规则直接通过", "rules"], disabled: ["只使用规则", "rules"],
    not_configured: ["模型未配置，安全回退", "fallback"], applied: ["LLM 辅助修正", "hybrid"],
    confirmed: ["LLM 复核通过", "hybrid"], rejected: ["提议被拒绝，保留规则", "fallback"],
    invalid_response: ["JSON 无效，安全回退", "fallback"], unavailable: ["模型不可用，安全回退", "fallback"],
  };
  const [resolutionLabel, resolutionTone] = resolutionLabels[resolution.llm_status] || ["规则解析", "rules"];
  const reasons = (resolution.trigger_reasons || []).map((reason) => `<li>${escapeHtml(reason)}</li>`).join("");
  const decisions = (resolution.decisions || []).filter((item) => item.field !== "all" || item.reason).map((item) => {
    const marks = { accepted: "✓", unchanged: "＝", locked: "▣", rejected: "×" };
    const names = { topic: "主题", region: "地域", time_range: "时间", schedule: "计划", delivery_channel: "通道", exclude_keywords: "排除词", event_types: "公告类型", all: "全部提议" };
    return `<li class="${escapeHtml(item.outcome)}"><b>${marks[item.outcome] || "·"} ${escapeHtml(names[item.field] || item.field)}</b><span>${escapeHtml(item.reason)}</span></li>`;
  }).join("");
  $("#intent-resolution").innerHTML = `<div class="resolution-head"><span class="resolution-badge ${resolutionTone}">${escapeHtml(resolutionLabel)}</span><p>${escapeHtml(resolution.summary)}${resolution.latency_ms ? ` · ${resolution.latency_ms} ms` : ""}</p></div>${reasons ? `<details><summary>为什么触发智能复核</summary><ul>${reasons}</ul></details>` : ""}${decisions ? `<details ${resolution.llm_status === "applied" || resolution.llm_status === "rejected" ? "open" : ""}><summary>字段级校验记录</summary><ul class="decision-list">${decisions}</ul></details>` : ""}`;
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
  const limitedSources = run.diagnostics.filter((item) => ["partial", "auth_required", "failed"].includes(item.status)).length;
  $("#result-summary").textContent = `${run.new_count} 条可信结果 · ${limitedSources ? `${limitedSources} 个来源覆盖受限` : "已配置来源均正常"}`;
  const download = $("#download-report");
  if (run.report_path) { const name = run.report_path.replaceAll("\\", "/").split("/").pop(); download.href = `/api/v1/reports/${encodeURIComponent(name)}`; download.classList.remove("hidden"); } else download.classList.add("hidden");
  const high = run.records.filter((item) => item.opportunity_score >= 80).length;
  const projects = new Set(run.records.map((item) => item.lifecycle_id)).size;
  const merged = run.records.reduce((sum, item) => sum + Math.max(0, item.duplicate_count - 1), 0);
  const explanation = run.search_explanation;
  const metrics = !run.records.length && explanation ? [["扫描公告", explanation.total_scanned], ["进入候选", explanation.total_candidates], ["筛选排除", Object.values(explanation.rejection_reasons || {}).reduce((sum, value) => sum + value, 0)], ["可信结果", 0]] : [["可信结果", run.records.length], ["高优机会", high], ["项目生命线", projects], ["重复合并", merged]];
  $("#metric-strip").innerHTML = metrics.map(([label, value]) => `<div class="metric"><small>${label}</small><b>${value}</b></div>`).join("");
  $("#source-coverage").innerHTML = run.diagnostics.map((item) => {
    const rejection = Object.entries(item.rejection_reasons || {}).map(([reason, count]) => `${FILTER_REASON_LABELS[reason] || reason} ${count}`).join("；");
    const statusLabel = SOURCE_STATUS_LABELS[item.status] || item.status;
    return `<span class="source-chip ${escapeHtml(item.status)}" title="${escapeHtml(rejection || item.message || "无额外诊断")}">${escapeHtml(item.source)} · 扫描 ${item.scanned_count ?? item.fetched_count} / 候选 ${item.fetched_count} / 保留 ${item.kept_count} · ${escapeHtml(statusLabel)}</span>`;
  }).join("");
  renderRetrievalTrace(run);
  const list = $("#result-list"), empty = $("#empty-state");
  if (!run.records.length) {
    list.innerHTML = ""; empty.classList.remove("hidden"); renderEmptyDiagnosis(explanation, empty);
  }
  else {
    empty.classList.add("hidden");
    list.innerHTML = run.records.map((item) => {
      const links = item.source_urls.map((url, i) => `<a href="${escapeHtml(safeUrl(url))}" target="_blank" rel="noreferrer">来源 ${i + 1} ↗</a>`).join("");
      const tracked = state.opportunityProjectKeys.has(item.project_key);
      return `<article class="result-card"><div class="result-top"><div><h3>${escapeHtml(item.title)}</h3><div class="record-meta"><span><b>${escapeHtml(item.event_type)}</b></span><span>${escapeHtml(item.published_at.slice(0,10))}</span><span>${escapeHtml(item.region || "地域未标注")}</span><span>${escapeHtml(item.buyer || "采购人未提取")}</span><span>合并 ${item.duplicate_count} 条</span></div></div><div class="score">${Math.round(item.opportunity_score)}</div></div><p class="summary">${escapeHtml(item.summary)}</p><div class="record-links">${links}<button class="add-opportunity" data-canonical="${escapeHtml(item.canonical_id)}" data-version="${escapeHtml(item.version_hash)}" ${tracked ? "disabled" : ""}>${tracked ? "✓ 已加入" : "＋ 加入机会"}</button></div></article>`;
    }).join("");
    bindResultOpportunityActions();
  }
  $("#results-panel").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderRetrievalTrace(run) {
  const root = $("#retrieval-trace"), trace = run.retrieval;
  if (!root) return;
  if (!trace?.plan) { root.innerHTML = ""; root.classList.add("hidden"); return; }
  root.classList.remove("hidden");
  const plan = trace.plan;
  const statusLabels = {
    disabled: "AI 已关闭", not_configured: "模型未配置", applied: "AI 计划已采用",
    rejected: "AI 提议被拒绝", invalid_response: "JSON 无效，已回退",
    unavailable: "模型不可用，已回退",
  };
  const reviewStatusLabels = {
    disabled: "AI 边界复核已关闭", not_needed: "无需语义复核",
    not_configured: "模型未配置，边界候选已保守拒绝", applied: "AI 边界复核已完成",
    invalid_response: "复核 JSON 无效，边界候选已保守拒绝", unavailable: "模型不可用，边界候选已保守拒绝",
  };
  const termOriginLabels = { query: "原始问题", rules: "规则同义词", lexicon: "行业词典", llm: "AI 建议" };
  const terms = (plan.terms || []).map((term) => `<span class="retrieval-term ${escapeHtml(term.kind)}" title="${escapeHtml(term.reason)}"><b>${escapeHtml(term.text)}</b><small>${escapeHtml(termOriginLabels[term.origin] || term.origin)}</small></span>`).join("");
  const rounds = new Map((trace.rounds || []).map((round) => [round.round, round]));
  const roundCard = (roundNumber) => {
    const round = rounds.get(roundNumber);
    const step = roundNumber === 1 ? "02" : "04";
    if (!round) return `<div class="retrieval-stage muted"><span>${step}</span><div><b>${roundNumber === 1 ? "第 1 轮召回" : "第 2 轮补搜"}</b><p>${roundNumber === 1 ? "没有轮次数据" : "未触发补搜"}</p></div></div>`;
    const queryNames = (round.queries || []).map((query) => query.text).join("、") || "核心主题";
    return `<div class="retrieval-stage"><span>${step}</span><div><b>${roundNumber === 1 ? "第 1 轮召回" : "第 2 轮补搜"}</b><p>${escapeHtml(round.trigger)}</p><small>${escapeHtml(queryNames)} · ${round.source_calls} 次来源调用 · 扫描 ${round.scanned_count} · 候选 ${round.candidate_count} · 去重 ${round.unique_candidate_count}</small></div></div>`;
  };
  const gaps = (trace.gap_analysis || []).map((gap) => `<li>${escapeHtml(gap)}</li>`).join("");
  const rejectionReasons = Object.entries(run.search_explanation?.rejection_reasons || {}).sort((a, b) => b[1] - a[1]).map(([reason, count]) => `<span>${escapeHtml(FILTER_REASON_LABELS[reason] || reason)} <b>${count}</b></span>`).join("");
  const semanticDecisions = (trace.semantic_decisions || []).slice(0, 8).map((item) => `<li class="${escapeHtml(item.outcome)}"><b>${item.outcome === "accepted" ? "✓" : item.outcome === "rejected" ? "×" : "·"} ${escapeHtml(item.title)}</b><span>${Math.round(item.confidence * 100)}% · ${escapeHtml(item.reason)}</span></li>`).join("");
  const merged = run.records.reduce((sum, item) => sum + Math.max(0, item.duplicate_count - 1), 0);
  root.innerHTML = `<div class="retrieval-trace-head"><div><p class="eyebrow">AUDITABLE AI RETRIEVAL</p><h3>这批结果是怎么找出来的</h3></div><span class="state-badge ${plan.mode === "hybrid" ? "success" : "muted"}">${escapeHtml(statusLabels[plan.llm_status] || plan.mode)}</span></div><p class="retrieval-summary">${escapeHtml(trace.summary || plan.summary)}</p><div class="retrieval-terms"><b>受控检索词</b><div>${terms || "只使用核心主题"}</div></div><div class="retrieval-flow"><div class="retrieval-stage"><span>01</span><div><b>AI 查询计划</b><p>${escapeHtml(plan.summary)}</p><small>最多 ${plan.max_rounds} 轮 · 每来源 ${plan.query_budget_per_source} 个查询预算${plan.source_priorities?.length ? ` · 优先 ${escapeHtml(plan.source_priorities.join("、"))}` : ""}</small></div></div>${roundCard(1)}<div class="retrieval-stage ${gaps ? "warning" : "muted"}"><span>03</span><div><b>缺口判断</b>${gaps ? `<ul>${gaps}</ul>` : "<p>首轮覆盖没有触发额外缺口说明</p>"}</div></div>${roundCard(2)}<div class="retrieval-stage"><span>05</span><div><b>硬过滤</b><p>日期、地域、公告类型和排除词不允许模型越权。</p><div class="hard-filter-tags">${rejectionReasons || "本轮无硬过滤淘汰"}</div></div></div><div class="retrieval-stage"><span>06</span><div><b>AI 边界复核</b><p>${escapeHtml(reviewStatusLabels[trace.semantic_review_status] || trace.semantic_review_status)}</p>${semanticDecisions ? `<ul class="semantic-decision-list">${semanticDecisions}</ul>` : "<small>没有需要模型裁决的边界候选。</small>"}</div></div><div class="retrieval-stage"><span>07</span><div><b>去重保留</b><p>${trace.unique_raw_candidates} 个唯一原始候选 → ${run.records.length} 条可信记录</p><small>合并 ${merged} 条跨站转载/重复版本；更正和中标生命周期仍单独保留。</small></div></div></div>`;
}

function renderEmptyDiagnosis(explanation, root) {
  if (!explanation) return;
  const reasonRows = Object.entries(explanation.rejection_reasons || {}).sort((a, b) => b[1] - a[1]).map(([reason, count]) => `<div><span>${escapeHtml(FILTER_REASON_LABELS[reason] || reason)}</span><b>${count} 条</b></div>`).join("");
  const coverage = (explanation.coverage_notes || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  const suggestions = (explanation.suggestions || []).map((item, index) => `<button class="diagnosis-suggestion" data-suggestion-index="${index}"><b>${escapeHtml(item.title)}</b><span>${escapeHtml(item.query)}</span><small>${escapeHtml(item.explanation)}</small></button>`).join("");
  const title = explanation.outcome === "all_filtered" ? "找到了候选，但都不符合你的条件" : "公开入口暂时没有返回候选";
  root.innerHTML = `<span>◎</span><h3>${title}</h3><p>${escapeHtml(explanation.summary)}</p><p class="funnel-explanation"><b>怎么看数字：</b>扫描 = 从来源页面读到；候选 = 送入统一核验；保留 = 最终可信结果。</p>${reasonRows ? `<div class="rejection-breakdown"><h4>为什么一条也没留下</h4>${reasonRows}</div>` : ""}${coverage ? `<details class="coverage-notes" open><summary>为什么不能简单理解成“全网没有”</summary><ul>${coverage}</ul></details>` : ""}${suggestions ? `<div class="diagnosis-suggestions"><h4>可以怎么改（点击只填入，不会自动查询）</h4>${suggestions}</div>` : ""}`;
  $$(".diagnosis-suggestion").forEach((button) => button.addEventListener("click", async () => {
    const suggestion = explanation.suggestions[Number(button.dataset.suggestionIndex)];
    $("#query-input").value = suggestion.query; state.spec = null;
    await activateTab("search"); $("#query-input").focus();
    window.scrollTo({ top: 0, behavior: "smooth" }); toast("建议已填入，请先解析意图，确认后再执行");
  }));
}

function bindResultOpportunityActions() {
  $$(".add-opportunity").forEach((button) => button.addEventListener("click", async () => {
    button.disabled = true; button.textContent = "加入中…";
    try {
      const opportunity = await api("/api/v1/opportunities", { method: "POST", body: JSON.stringify({ canonical_id: button.dataset.canonical, version_hash: button.dataset.version }) });
      state.opportunityProjectKeys.add(opportunity.project_key);
      button.textContent = "✓ 已加入";
      toast(`已加入机会：${opportunity.record.title}`, 5000);
      await loadOpportunities();
    } catch (error) { button.disabled = false; button.textContent = "＋ 加入机会"; toast(error.message, 5000); }
  }));
}

function opportunityStageOptions(selected) {
  return Object.entries(OPPORTUNITY_STAGES).map(([value, label]) => `<option value="${value}" ${value === selected ? "selected" : ""}>${label}</option>`).join("");
}

function opportunityCard(item) {
  const record = item.record;
  const source = record.source_urls[0] ? `<a href="${escapeHtml(safeUrl(record.source_urls[0]))}" target="_blank" rel="noreferrer">原文 ↗</a>` : "";
  const tags = item.tags.map((tag) => `<span>${escapeHtml(tag)}</span>`).join("");
  return `<article class="opportunity-card ${item.is_read ? "" : "unread"}" data-id="${escapeHtml(item.id)}">
    <div class="opportunity-card-head"><div><div class="opportunity-kicker"><span>${escapeHtml(record.event_type)}</span><span>${escapeHtml(record.region || "地域未标注")}</span></div><h3>${escapeHtml(record.title)}</h3></div><div class="score">${Math.round(record.opportunity_score)}</div></div>
    <p class="opportunity-buyer">${escapeHtml(record.buyer || "采购人未提取")} · ${escapeHtml(record.published_at.slice(0, 10))}</p>
    <div class="opportunity-tags">${tags || "<span>未设置标签</span>"}</div>
    <div class="opportunity-next"><span><small>负责人</small><b>${escapeHtml(item.owner || "待分配")}</b></span><span><small>下一步</small><b>${item.next_action_at ? formatTime(item.next_action_at) : "待安排"}</b></span></div>
    <div class="opportunity-links">${source}<button class="mark-read">${item.is_read ? "标为未读" : "标为已读"}</button><button class="view-timeline" aria-expanded="false">生命周期</button></div>
    <details class="opportunity-editor"><summary>跟进设置</summary><div class="opportunity-form">
      <label><span>阶段</span><select class="opportunity-stage" aria-label="机会阶段">${opportunityStageOptions(item.stage)}</select></label>
      <label><span>负责人</span><input class="opportunity-owner" maxlength="100" value="${escapeHtml(item.owner)}" placeholder="姓名或团队"></label>
      <label><span>下一步时间</span><input class="opportunity-next-at" type="datetime-local" value="${formatDateTimeInput(item.next_action_at)}"></label>
      <label><span>标签</span><input class="opportunity-tags-input" maxlength="240" value="${escapeHtml(item.tags.join("，"))}" placeholder="重点，GPU，教育"></label>
      <label class="opportunity-notes-label"><span>跟进备注</span><textarea class="opportunity-notes" rows="3" maxlength="4000" placeholder="记录判断、风险与下一步">${escapeHtml(item.notes)}</textarea></label>
    </div><button class="primary-button compact save-opportunity">保存跟进</button></details>
    <div class="opportunity-timeline hidden"></div>
  </article>`;
}

async function loadOpportunities() {
  const filter = $("#opportunity-stage-filter")?.value || "";
  const search = $("#opportunity-search")?.value.trim() || "";
  const params = new URLSearchParams();
  if (filter) params.set("stage", filter); if (search) params.set("search", search);
  const rows = await api(`/api/v1/opportunities${params.size ? `?${params}` : ""}`);
  rows.forEach((item) => state.opportunityProjectKeys.add(item.project_key));
  const summary = $("#opportunity-summary"), board = $("#opportunity-board");
  if (!summary || !board) return rows;
  const now = Date.now(), soon = now + 3 * 86400000;
  const due = rows.filter((item) => item.next_action_at && new Date(item.next_action_at).getTime() <= soon && !["won", "lost", "archived"].includes(item.stage)).length;
  summary.innerHTML = [[filter || search ? "筛选结果" : "机会总数", rows.length], ["未读", rows.filter((item) => !item.is_read).length], ["三日内待办", due], ["已中标", rows.filter((item) => item.stage === "won").length]].map(([label, value]) => `<div class="metric"><small>${label}</small><b>${value}</b></div>`).join("");
  if (!rows.length) {
    const filtered = Boolean(filter || search);
    board.innerHTML = `<section class="opportunity-zero"><span>${filtered ? "⌕" : "◎"}</span><h2>${filtered ? "没有符合当前条件的机会" : "还没有机会项目"}</h2><p>${filtered ? "清空搜索和阶段筛选，回到完整机会看板。" : "先检索真实标讯，再从结果卡点击“加入机会”；这里不会填充演示假数据。"}</p><button class="secondary-button" data-empty-action="${filtered ? "clear" : "search"}">${filtered ? "清空筛选" : "去检索真实标讯"}</button></section>`;
    const emptyAction = board.querySelector("[data-empty-action]");
    emptyAction.addEventListener("click", async () => {
      try {
        if (emptyAction.dataset.emptyAction === "clear") {
          $("#opportunity-search").value = "";
          $("#opportunity-stage-filter").value = "";
          await loadOpportunities();
        } else {
          await activateTab("search");
          $("#query-input").focus();
        }
      } catch (error) { toast(error.message, 5000); }
    });
    return rows;
  }
  const stages = filter ? [filter] : Object.keys(OPPORTUNITY_STAGES);
  board.innerHTML = stages.map((stage) => {
    const items = rows.filter((item) => item.stage === stage);
    return `<section class="opportunity-column" data-stage="${stage}"><div class="opportunity-column-head"><h2>${OPPORTUNITY_STAGES[stage]}</h2><span>${items.length}</span></div><div class="opportunity-column-body">${items.length ? items.map(opportunityCard).join("") : `<div class="opportunity-empty">暂无${OPPORTUNITY_STAGES[stage]}机会</div>`}</div></section>`;
  }).join("");
  bindOpportunityActions();
  return rows;
}

function bindOpportunityActions() {
  $$(".opportunity-card").forEach((card) => {
    const id = card.dataset.id;
    card.querySelector(".save-opportunity").addEventListener("click", async (event) => {
      const button = event.currentTarget; button.disabled = true; button.textContent = "保存中…";
      const tags = card.querySelector(".opportunity-tags-input").value.split(/[,，]/).map((item) => item.trim()).filter(Boolean);
      const nextAction = card.querySelector(".opportunity-next-at").value || null;
      try {
        await api(`/api/v1/opportunities/${id}`, { method: "PATCH", body: JSON.stringify({ stage: card.querySelector(".opportunity-stage").value, owner: card.querySelector(".opportunity-owner").value.trim(), next_action_at: nextAction, notes: card.querySelector(".opportunity-notes").value.trim(), tags }) });
        toast("机会跟进信息已保存"); await loadOpportunities();
      } catch (error) { button.disabled = false; button.textContent = "保存跟进"; toast(error.message, 5000); }
    });
    card.querySelector(".mark-read").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      const unread = card.classList.contains("unread");
      button.disabled = true;
      try {
        await api(`/api/v1/opportunities/${id}`, { method: "PATCH", body: JSON.stringify({ is_read: unread }) });
        await loadOpportunities();
      } catch (error) {
        button.disabled = false;
        toast(error.message, 5000);
      }
    });
    card.querySelector(".view-timeline").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      const root = card.querySelector(".opportunity-timeline");
      if (!root.classList.contains("hidden")) { root.classList.add("hidden"); button.setAttribute("aria-expanded", "false"); return; }
      button.setAttribute("aria-expanded", "true");
      root.classList.remove("hidden"); root.textContent = "正在读取项目生命周期…";
      try {
        const events = await api(`/api/v1/opportunities/${id}/timeline`);
        root.innerHTML = events.map((event) => `<div class="timeline-event"><i></i><div><b>${escapeHtml(event.event_type)} · ${escapeHtml(event.published_at.slice(0, 10))}</b><p>${escapeHtml(event.title)}</p><a href="${escapeHtml(safeUrl(event.source_urls[0] || ""))}" target="_blank" rel="noreferrer">查看证据 ↗</a></div></div>`).join("");
      } catch (error) { root.textContent = error.message; }
    });
  });
}

async function loadSources() {
  const rows = await api("/api/v1/sources/status");
  state.sources = rows;
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
  if (row.last_status === "skipped") return ["按地域跳过", "muted"];
  if (row.last_status === "ok") return ["运行正常", "success"];
  return ["等待首轮", "muted"];
}

function authStateLabel(auth) {
  const labels = {
    not_supported: "无需/不可复用授权", not_authorized: "尚未授权",
    authorizing: "等待你完成登录", authorized: "授权已保存",
    expired: "授权已过期", failed: "授权失败",
  };
  return labels[auth?.state] || "授权状态未知";
}

function sourceCapabilityTags(row) {
  const tags = [];
  if (row.supports_query_variants) tags.push("关键词补搜");
  tags.push(row.supports_region_filter ? "地域过滤" : "本地硬校验地域");
  tags.push(row.supports_date_filter ? "日期过滤" : "本地硬校验日期");
  if (row.supports_pagination) tags.push("分页");
  tags.push(row.supports_detail ? "正文" : "列表摘要");
  return tags.map((tag) => `<span>${escapeHtml(tag)}</span>`).join("");
}

function sourceAuthActions(row) {
  const auth = row.authorization || {};
  if (!auth.managed) {
    return auth.login_url ? `<a class="secondary-button compact" href="${escapeHtml(safeUrl(auth.login_url))}" target="_blank" rel="noreferrer">打开原站工作台 ↗</a>` : "";
  }
  if (auth.state === "authorizing") {
    return `<button class="primary-button compact source-auth-complete" data-source-id="${escapeHtml(row.id)}" data-session-id="${escapeHtml(auth.active_session_id || "")}">我已登录，完成授权</button><button class="secondary-button compact source-auth-clear" data-source-id="${escapeHtml(row.id)}">取消并清理</button>`;
  }
  if (auth.state === "authorized") {
    return `<button class="secondary-button compact source-auth-test" data-source-id="${escapeHtml(row.id)}">测试授权</button><button class="secondary-button compact source-auth-start" data-source-id="${escapeHtml(row.id)}">重新授权</button><button class="danger-button source-auth-clear" data-source-id="${escapeHtml(row.id)}">清除</button>`;
  }
  return `<button class="primary-button compact source-auth-start" data-source-id="${escapeHtml(row.id)}">打开浏览器授权</button>`;
}

function renderSourceCenter(rows) {
  const summary = $("#source-summary"), root = $("#source-center-list");
  if (!summary || !root) return;
  const configured = rows.filter((row) => row.configured).length;
  const official = rows.filter((row) => row.official).length;
  const scanned = rows.reduce((sum, row) => sum + (row.last_scanned_count || row.last_fetched_count || 0), 0);
  summary.innerHTML = [["已接入来源", rows.length], ["官方平台", official], ["当前可运行", configured], ["最近扫描公告", scanned]].map(([label, value]) => `<div class="metric"><small>${label}</small><b>${value}</b></div>`).join("");
  root.innerHTML = rows.map((row) => {
    const [label, tone] = sourceState(row);
    const access = row.member_enhanced ? `${row.mode} · 已授权增强` : row.mode;
    const checked = row.last_checked_at ? formatTime(row.last_checked_at) : "尚未运行";
    const message = row.last_message || (row.configured ? "等待首轮真实查询验证" : "需要用户在本机完成授权或配置");
    const auth = row.authorization || { state: "not_supported", authorization_scope: "该公开来源无需授权。", message: "" };
    const rejection = Object.entries(row.last_rejection_reasons || {}).sort((a, b) => b[1] - a[1]).map(([reason, count]) => `<span>${escapeHtml(FILTER_REASON_LABELS[reason] || reason)} ${count}</span>`).join("");
    return `<article class="source-card" data-source-id="${escapeHtml(row.id)}"><div class="source-card-head"><div><h3>${escapeHtml(row.name)}</h3><div class="source-tags"><span>${row.official ? "官方来源" : "行业来源"}</span><span>${escapeHtml(access)}</span><span>${escapeHtml(row.query_mode || "列表检索")}</span></div></div><span class="state-badge ${tone}">${label}</span></div><div class="source-capabilities">${sourceCapabilityTags(row)}</div><div class="source-stats"><span><small>最近检查</small><b>${checked}</b></span><span><small>扫描 / 候选 / 保留</small><b>${row.last_scanned_count || row.last_fetched_count || 0} / ${row.last_fetched_count || 0} / ${row.last_kept_count || 0}</b></span><span><small>最近耗时</small><b>${row.last_latency_ms ? `${row.last_latency_ms} ms` : "暂无"}</b></span></div>${rejection ? `<div class="source-rejections">${rejection}</div>` : ""}<p>${escapeHtml(message)}</p><details class="source-boundary"><summary>覆盖范围与限制</summary><p>${escapeHtml(row.coverage_note || "来源暂未提供覆盖说明。")}</p></details><div class="source-auth-panel ${escapeHtml(auth.state)}"><div><b>${escapeHtml(authStateLabel(auth))}</b><p>${escapeHtml(auth.authorization_scope || "")}</p><small>${escapeHtml(auth.message || "")}${auth.last_test_status === "passed" ? " · 最近测试通过" : auth.last_test_status === "failed" ? " · 最近测试失败" : ""}</small></div><div class="source-auth-actions">${sourceAuthActions(row)}</div></div></article>`;
  }).join("");
  bindSourceAuthActions();
}

async function startSourceAuth(sourceId, button) {
  button.disabled = true; button.textContent = "正在打开浏览器…";
  try {
    await configApi(`/api/v1/sources/${encodeURIComponent(sourceId)}/auth/start`, { method: "POST" });
    toast("授权浏览器已打开。请在里面亲自完成登录，再回到这里点击“我已登录，完成授权”。", 7000);
    await loadSources();
  } catch (error) { toast(error.message, 7000); }
  finally { button.disabled = false; }
}

async function completeSourceAuth(sessionId, button) {
  if (!sessionId) return toast("授权会话已失效，请重新开始", 5000);
  button.disabled = true; button.textContent = "正在加密保存…";
  try {
    await configApi(`/api/v1/sources/auth/sessions/${encodeURIComponent(sessionId)}/complete`, { method: "POST" });
    toast("授权已加密保存。现在可以点击“测试授权”验证真实检索。", 6000);
    await loadSources();
  } catch (error) { toast(error.message, 7000); await loadSources(); }
  finally { button.disabled = false; }
}

async function testSourceAuth(sourceId, button) {
  button.disabled = true; button.textContent = "真实测试中…";
  try {
    const result = await configApi(`/api/v1/sources/${encodeURIComponent(sourceId)}/auth/test`, { method: "POST" });
    toast(result.message, 7000); await loadSources();
  } catch (error) { toast(error.message, 7000); await loadSources(); }
  finally { button.disabled = false; }
}

async function clearSourceAuth(sourceId, button) {
  if (!window.confirm("确定清除这个来源保存在本机的加密授权会话吗？不会删除原网站账号。")) return;
  button.disabled = true;
  try {
    await configApi(`/api/v1/sources/${encodeURIComponent(sourceId)}/auth`, { method: "DELETE" });
    toast("本机授权会话已清除"); await loadSources();
  } catch (error) { toast(error.message, 6000); }
  finally { button.disabled = false; }
}

function bindSourceAuthActions() {
  $$(".source-auth-start").forEach((button) => button.addEventListener("click", () => startSourceAuth(button.dataset.sourceId, button)));
  $$(".source-auth-complete").forEach((button) => button.addEventListener("click", () => completeSourceAuth(button.dataset.sessionId, button)));
  $$(".source-auth-test").forEach((button) => button.addEventListener("click", () => testSourceAuth(button.dataset.sourceId, button)));
  $$(".source-auth-clear").forEach((button) => button.addEventListener("click", () => clearSourceAuth(button.dataset.sourceId, button)));
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

async function ensureConfigEditToken(force = false) {
  if (state.configEditToken && !force) return state.configEditToken;
  const response = await api("/api/v1/config/edit-token", { method: "POST" });
  state.configEditToken = response.edit_token;
  return state.configEditToken;
}

async function configApi(path, options = {}, retry = true) {
  const token = await ensureConfigEditToken();
  try {
    return await api(path, { ...options, headers: { ...(options.headers || {}), "X-BidPilot-Config-Token": token } });
  } catch (error) {
    if (retry && error.status === 403) { await ensureConfigEditToken(true); return configApi(path, options, false); }
    throw error;
  }
}

function setConfigStatus(id, ready, readyLabel = "已就绪") {
  const node = $(`#config-status-${id}`); if (!node) return;
  node.textContent = ready ? readyLabel : "未配置";
  node.className = `state-badge ${ready ? "success" : "muted"}`;
}

function renderSecretState(field, configured) {
  const label = $(`[data-secret-state="${field}"]`);
  const input = $(`[data-config="${field}"]`);
  if (label) { label.textContent = configured ? "已配置" : "未配置"; label.classList.toggle("configured", configured); }
  if (input) { input.value = ""; input.placeholder = configured ? "已配置，留空保持原值" : "尚未配置"; }
}

function renderConfig(config) {
  state.config = config;
  const values = {
    llm_base_url: config.ai.llm_base_url, llm_model: config.ai.llm_model, llm_timeout: config.ai.llm_timeout, intent_llm_mode: config.ai.intent_llm_mode, intent_llm_confidence_threshold: config.ai.intent_llm_confidence_threshold,
    retrieval_llm_mode: config.ai.retrieval_llm_mode, retrieval_max_rounds: config.ai.retrieval_max_rounds, retrieval_query_budget_per_source: config.ai.retrieval_query_budget_per_source, retrieval_semantic_review: config.ai.retrieval_semantic_review, retrieval_semantic_threshold: config.ai.retrieval_semantic_threshold,
    feishu_app_id: config.feishu.app_id, feishu_receive_id: config.feishu.receive_id, feishu_receive_id_type: config.feishu.receive_id_type, public_base_url: config.feishu.public_base_url,
    smtp_host: config.email.host, smtp_port: config.email.port, smtp_security: config.email.security, smtp_username: config.email.username, smtp_from: config.email.sender, smtp_to: config.email.recipients, smtp_timeout: config.email.timeout,
    delivery_webhook_timeout: config.generic_webhook.timeout,
  };
  Object.entries(values).forEach(([field, value]) => { const input = $(`[data-config="${field}"]`); if (input?.type === "checkbox") input.checked = Boolean(value); else if (input) input.value = value ?? ""; });
  const secrets = {
    llm_api_key: config.ai.llm_api_key.configured,
    feishu_webhook_url: config.feishu.webhook_url.configured,
    feishu_webhook_secret: config.feishu.webhook_secret.configured,
    feishu_app_secret: config.feishu.app_secret.configured,
    smtp_password: config.email.password.configured,
    dingtalk_webhook_url: config.dingtalk.webhook_url.configured,
    dingtalk_webhook_secret: config.dingtalk.secret?.configured || false,
    wecom_webhook_url: config.wecom.webhook_url.configured,
    generic_webhook_url: config.generic_webhook.webhook_url.configured,
    generic_webhook_bearer_token: config.generic_webhook.bearer_token.configured,
  };
  Object.entries(secrets).forEach(([field, configured]) => renderSecretState(field, configured));
  $$('[data-clear]').forEach((input) => { input.checked = false; });
  setConfigStatus("ai", config.ai.ready, "模型已就绪");
  setConfigStatus("feishu", config.feishu.webhook_ready || config.feishu.app_ready, config.feishu.app_ready ? "应用已就绪" : "机器人已就绪");
  setConfigStatus("email", config.email.ready, "邮件已就绪");
  setConfigStatus("dingtalk", config.dingtalk.ready, "机器人已就绪");
  setConfigStatus("wecom", config.wecom.ready, "机器人已就绪");
  setConfigStatus("generic", config.generic_webhook.ready, "接口已就绪");
  $("#config-security-notice").textContent = config.security_notice;
  const saveState = $("#config-save-state"); saveState.textContent = "配置已同步"; saveState.className = "state-badge success";
}

async function loadConfig() {
  const config = await api("/api/v1/config");
  renderConfig(config); await ensureConfigEditToken(); return config;
}

function collectConfigPayload() {
  const payload = {};
  $$('[data-config]').forEach((input) => {
    const field = input.dataset.config;
    if (input.type === "password") { if (input.value.trim()) payload[field] = input.value.trim(); return; }
    if (input.type === "checkbox") { payload[field] = input.checked; return; }
    if (input.type === "number") { if (input.value !== "") payload[field] = Number(input.value); return; }
    payload[field] = input.value.trim();
  });
  payload.clear_secrets = $$('[data-clear]:checked').map((input) => input.dataset.clear);
  return payload;
}

async function saveConfig(showMessage = true) {
  const button = $("#save-config"), saveState = $("#config-save-state");
  button.disabled = true; saveState.textContent = "正在保存"; saveState.className = "state-badge warning";
  try {
    const config = await configApi("/api/v1/config", { method: "PUT", body: JSON.stringify(collectConfigPayload()) });
    renderConfig(config); await loadSystemStatus();
    if (showMessage) toast("配置已保存并立即生效");
    return config;
  } catch (error) {
    saveState.textContent = "保存失败"; saveState.className = "state-badge danger"; throw error;
  } finally { button.disabled = false; }
}

function showConfigTestResult(group, result, failed = false) {
  const node = $(`#test-result-${group}`); if (!node) return;
  node.textContent = failed ? result : `✓ ${result.message} · ${result.latency_ms} ms${result.preview ? ` · ${result.preview}` : ""}`;
  node.className = failed ? "test-result failed" : "test-result success";
}

async function testModelConnection() {
  const button = $("#test-model"); button.disabled = true; button.textContent = "连接中…";
  try {
    await saveConfig(false);
    const result = await configApi("/api/v1/config/model/test", { method: "POST" });
    showConfigTestResult("ai", result); toast("模型连接测试成功");
  } catch (error) { showConfigTestResult("ai", error.message, true); toast(error.message, 5000); }
  finally { button.disabled = false; button.textContent = "测试模型连接"; }
}

function channelTestGroup(channel) {
  if (channel.startsWith("feishu")) return "feishu";
  if (channel === "email") return "email";
  if (channel === "dingtalk_webhook") return "dingtalk";
  if (channel === "wecom_webhook") return "wecom";
  return "generic";
}

async function testDeliveryChannel(button) {
  const channel = button.dataset.testChannel, group = channelTestGroup(channel);
  if (!window.confirm("此操作会通过所选通道真实发送一条“配置中心连通性测试”消息。确定继续吗？")) return;
  button.disabled = true; const original = button.textContent; button.textContent = "发送中…";
  try {
    await saveConfig(false);
    const result = await configApi(`/api/v1/config/channels/${encodeURIComponent(channel)}/test`, { method: "POST" });
    showConfigTestResult(group, result); toast("测试消息已发送");
  } catch (error) { showConfigTestResult(group, error.message, true); toast(error.message, 5000); }
  finally { button.disabled = false; button.textContent = original; }
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
  if (tab === "opportunities") await loadOpportunities(); if (tab === "subscriptions") await loadSubscriptions(); if (tab === "sources") await loadSources(); if (tab === "config") await loadConfig(); if (tab === "reports") await loadReports();
}

$$(".nav-link").forEach((button) => button.addEventListener("click", () => activateTab(button.dataset.tab).catch((error) => toast(error.message))));
$$("[data-query]").forEach((button) => button.addEventListener("click", () => { $("#query-input").value = button.dataset.query; state.spec = null; }));
$("#parse-button").addEventListener("click", () => parseIntent().catch((error) => toast(error.message)));
$("#run-button").addEventListener("click", runQuery);
$("#create-subscription-button").addEventListener("click", () => createSubscriptionFromQuery().catch((error) => toast(error.message, 5000)));
$("#delivery-channel").addEventListener("change", renderChannelHint);
$("#refresh-opportunities").addEventListener("click", () => loadOpportunities().catch((error) => toast(error.message)));
$("#opportunity-stage-filter").addEventListener("change", () => loadOpportunities().catch((error) => toast(error.message)));
$("#opportunity-search").addEventListener("input", () => {
  clearTimeout(window.__opportunitySearchTimer);
  window.__opportunitySearchTimer = setTimeout(() => loadOpportunities().catch((error) => toast(error.message)), 250);
});
$("#save-config").addEventListener("click", () => saveConfig().catch((error) => toast(error.message, 5000)));
$("#test-model").addEventListener("click", testModelConnection);
$$('.channel-test').forEach((button) => button.addEventListener("click", () => testDeliveryChannel(button)));
$("#query-input").addEventListener("keydown", (event) => { if ((event.metaKey || event.ctrlKey) && event.key === "Enter") runQuery(); });
document.addEventListener("keydown", (event) => { if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); $("#query-input").focus(); } });
window.createBidPilotSubscription = createSubscriptionFromQuery;
Promise.all([loadSystemStatus(), loadSources(), loadReports(), loadSubscriptions(), loadOpportunities()]).catch((error) => toast(error.message));
setInterval(() => {
  const editing = $(".subscription-editor:not(.hidden)");
  if (state.activeTab === "subscriptions" && !editing) loadSubscriptions().catch(() => {});
}, 15000);
