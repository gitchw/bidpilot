const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const state = {
  spec: null,
  run: null,
  system: null,
  config: null,
  configEditToken: null,
  activeTab: "search",
  opportunityView: "opportunities",
  opportunityProjectKeys: new Set(),
  buyerRadarLoadSequence: 0,
  sources: [],
  profile: null,
  profileLoaded: false,
  profileDirty: false,
  profileLoadSequence: 0,
  feedbackMap: new Map(),
  qaAnswer: null,
  qaInFlight: false,
  qaRequestSequence: 0,
  qaAbortController: null,
  runInFlight: false,
  runRequestSequence: 0,
  intentRequestSequence: 0,
  assessmentRefreshSequence: 0,
};
const OPPORTUNITY_STAGES = {
  new: "待评估", following: "跟进中", bidding: "投标准备",
  won: "已中标", lost: "未中标", archived: "已归档"
};
const FILTER_REASON_LABELS = {
  outside_time: "超出时间范围", region_mismatch: "地域不匹配",
  event_type_mismatch: "公告类型不匹配", excluded_keyword: "命中排除词",
  buyer_mismatch: "采购单位不匹配", keyword_mismatch: "主题/同义词未命中", low_relevance: "相关度不足",
};
const SOURCE_STATUS_LABELS = {
  ok: "正常完成", partial: "覆盖不完整",
  auth_required: "需要登录", failed: "抓取失败", skipped: "本轮未调用",
};
const FIT_RECOMMENDATION_LABELS = { bid: "建议参与", watch: "持续观察", skip: "暂不投入" };
const FEEDBACK_LABELS = { relevant: "相关", irrelevant: "无关", watch: "观察", contacted: "已联系" };
const ASSESSMENT_STATUS_LABELS = {
  applied: "AI 语义复核已通过", cached: "复用已验证 AI 结果", disabled: "AI 已关闭 · 本地判断",
  not_configured: "模型未配置 · 本地判断", profile_missing: "画像未填写 · 通用判断",
  invalid_response: "AI 输出已拒绝 · 本地判断", unavailable: "模型不可用 · 本地判断", empty: "本轮无可信结果",
};

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
}

function safeUrl(value = "") {
  try { const url = new URL(value); return ["http:", "https:"].includes(url.protocol) ? url.href : ""; }
  catch { return ""; }
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

function feedbackKey(canonicalId, versionHash) { return `${canonicalId}::${versionHash}`; }

function splitProfileList(value = "") {
  return [...new Set(String(value).split(/[\n,，;；]+/).map((item) => item.trim()).filter(Boolean))];
}

function toast(message, duration = 3200) {
  const node = $("#toast"); node.textContent = message; node.classList.add("show");
  clearTimeout(window.__toastTimer); window.__toastTimer = setTimeout(() => node.classList.remove("show"), duration);
}

async function api(path, options = {}) {
  const { timeoutMs = 180000, ...requestOptions } = options;
  const controller = new AbortController();
  const externalSignal = requestOptions.signal;
  const abortFromExternal = () => controller.abort();
  if (externalSignal?.aborted) controller.abort();
  else externalSignal?.addEventListener("abort", abortFromExternal, { once: true });
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  let response;
  try {
    response = await fetch(path, { ...requestOptions, signal: controller.signal, headers: { "Content-Type": "application/json", ...(requestOptions.headers || {}) } });
  } catch (error) {
    const transient = error?.name === "AbortError" || error instanceof TypeError;
    if (!transient) throw error;
    const wrapped = new Error(error?.name === "AbortError" ? `页面已停止等待（超过 ${Math.ceil(timeoutMs / 1000)} 秒），后台结果尚未确认，请先刷新状态` : "本地服务连接刚刚中断，结果尚未确认，请刷新后再决定是否重试");
    wrapped.transient = true;
    throw wrapped;
  } finally {
    window.clearTimeout(timeout);
    externalSignal?.removeEventListener("abort", abortFromExternal);
  }
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
  const sequence = ++state.intentRequestSequence;
  const button = $("#parse-button"); button.disabled = true; button.textContent = "正在解析…";
  try {
    const spec = await api("/api/v1/intent/parse", { method: "POST", body: JSON.stringify({ query }) });
    if (sequence !== state.intentRequestSequence || currentQuery() !== query) return null;
    state.spec = spec; renderIntent(spec); return spec;
  } finally {
    if (sequence === state.intentRequestSequence && !state.runInFlight) { button.disabled = false; button.textContent = "解析意图"; }
  }
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

function setRunControlsBusy(busy) {
  state.runInFlight = busy;
  ["#query-input", "#delivery-channel", "#delivery-policy", "#parse-button"].forEach((selector) => { const control = $(selector); if (control) control.disabled = busy; });
  $$("[data-query]").forEach((control) => { control.disabled = busy; });
  const button = $("#run-button"); button.disabled = busy;
  button.querySelector("span").textContent = busy ? "多源采集中…" : "启动情报任务";
}

async function runQuery() {
  const query = currentQuery(); if (query.length < 2) return toast("请先输入查询问题");
  if (state.runInFlight) return toast("当前情报任务正在执行，请等待本轮完成");
  const sequence = ++state.runRequestSequence;
  let runCompleted = false;
  setRunControlsBusy(true);
  state.qaAbortController?.abort(); state.qaAbortController = null; state.qaInFlight = false; state.qaRequestSequence += 1;
  try {
    if (!state.spec || state.spec.raw_query !== query) {
      const parsed = await parseIntent();
      if (!parsed || currentQuery() !== query) throw new Error("查询内容在解析期间发生变化，请确认后重新启动");
    }
    $("#run-panel").classList.remove("hidden"); $("#results-panel").classList.add("hidden"); animatePipeline();
    $("#run-panel").scrollIntoView({ behavior: "smooth", block: "center" });
    const run = await api("/api/v1/runs", { method: "POST", body: JSON.stringify({ query, delivery_channel: currentChannel() }) });
    if (sequence !== state.runRequestSequence) return;
    state.run = run; state.qaAnswer = null; runCompleted = true;
    clearInterval(window.__pipeTimer); $$(".pipe-step").forEach((step) => { step.classList.remove("active"); step.classList.add("done"); });
    $("#run-state").innerHTML = "✓ 执行完成"; renderResults(run);
    const auxiliary = await Promise.allSettled([loadFeedbackMemory(false), loadReports(), loadSources()]);
    if (auxiliary[0].status === "fulfilled") renderResults(run, false);
    const failedPanels = ["反馈记忆", "报告列表", "来源状态"].filter((_, index) => auxiliary[index].status === "rejected");
    if (failedPanels.length) toast(`本轮检索已完成，但${failedPanels.join("、")}暂未刷新`, 8000);
  } catch (error) {
    clearInterval(window.__pipeTimer);
    if (!runCompleted) {
      toast(error.transient ? `${error.message}；请先查看“报告历史”，避免重复执行` : error.message, 8000);
      $("#run-state").textContent = error.transient ? "结果待确认" : "执行失败";
    }
  }
  finally { if (sequence === state.runRequestSequence) setRunControlsBusy(false); }
}

function renderResults(run, scroll = true) {
  $("#results-panel").classList.remove("hidden");
  const refreshAssessments = $("#refresh-assessments"); if (refreshAssessments) { refreshAssessments.disabled = !run?.run_id; refreshAssessments.textContent = "刷新适配判断"; }
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
  renderIntelligenceBrief(run);
  renderRetrievalTrace(run);
  renderRunQA(run);
  const list = $("#result-list"), empty = $("#empty-state");
  if (!run.records.length) {
    list.innerHTML = ""; empty.classList.remove("hidden"); renderEmptyDiagnosis(explanation, empty);
  }
  else {
    empty.classList.add("hidden");
    const assessments = new Map((run.opportunity_assessments?.assessments || []).map((item) => [feedbackKey(item.canonical_id, item.version_hash), item]));
    list.innerHTML = run.records.map((item) => {
      const links = item.source_urls.map((url, i) => { const sourceUrl = safeUrl(url); return sourceUrl ? `<a href="${escapeHtml(sourceUrl)}" target="_blank" rel="noreferrer">来源 ${i + 1} ↗</a>` : ""; }).join("");
      const tracked = state.opportunityProjectKeys.has(item.project_key);
      const key = feedbackKey(item.canonical_id, item.version_hash);
      return `<article class="result-card" data-canonical="${escapeHtml(item.canonical_id)}" data-version="${escapeHtml(item.version_hash)}"><div class="result-top"><div><h3>${escapeHtml(item.title)}</h3><div class="record-meta"><span><b>${escapeHtml(item.event_type)}</b></span><span>${escapeHtml(item.published_at.slice(0,10))}</span><span>${escapeHtml(item.region || "地域未标注")}</span><span>${escapeHtml(item.buyer || "采购人未提取")}</span><span>合并 ${item.duplicate_count} 条</span></div></div><div class="score" title="原始机会分">${Math.round(item.opportunity_score)}</div></div><p class="summary">${escapeHtml(item.summary)}</p>${renderFitAssessment(assessments.get(key), run.opportunity_assessments)}${renderFeedbackControls(item)}<div class="record-links">${links}<button class="add-opportunity" data-canonical="${escapeHtml(item.canonical_id)}" data-version="${escapeHtml(item.version_hash)}" ${tracked ? "disabled" : ""}>${tracked ? "✓ 已加入" : "＋ 加入机会"}</button></div></article>`;
    }).join("");
    bindResultOpportunityActions();
    bindResultFeedbackActions();
  }
  if (scroll) $("#results-panel").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderFitAssessment(assessment, assessmentSet) {
  if (!assessment) return `<div class="fit-panel missing"><div><b>企业适配判断尚未生成</b><span>刷新本轮判断后会显示本地分数、风险和建议。</span></div></div>`;
  const recommendation = FIT_RECOMMENDATION_LABELS[assessment.recommendation] || assessment.recommendation;
  const adjustment = Number(assessment.personalization_adjustment || 0);
  const adjustmentText = `${adjustment > 0 ? "+" : ""}${adjustment}`;
  const matched = (assessment.matched_profile_terms || []).map((term) => `<span>${escapeHtml(term)}</span>`).join("");
  const gaps = (assessment.gaps || []).map((gap) => `<li>${escapeHtml(gap)}</li>`).join("");
  const quotes = (assessment.evidence_quotes || []).map((quote) => `<blockquote>${escapeHtml(quote)}</blockquote>`).join("");
  const sourceUrl = safeUrl(assessment.source_url);
  const evidenceSource = sourceUrl ? `<a href="${escapeHtml(sourceUrl)}" target="_blank" rel="noreferrer">打开官方原文核验 ↗</a>` : `<span class="missing-source">原文链接未保留</span>`;
  const modeLabel = ASSESSMENT_STATUS_LABELS[assessmentSet?.status] || assessmentSet?.status || "本地判断";
  return `<section class="fit-panel ${escapeHtml(assessment.recommendation)}"><div class="fit-panel-head"><div><span class="fit-recommendation">${escapeHtml(recommendation)}</span><small>${escapeHtml(modeLabel)} · ${escapeHtml(assessment.evidence_id)}</small></div><div class="fit-score"><b>${Math.round(assessment.fit_score)}</b><span>适配分</span></div></div><div class="fit-score-ledger"><span><small>本地基础分</small><b>${assessment.base_fit_score}</b></span><span class="${adjustment > 0 ? "positive" : adjustment < 0 ? "negative" : ""}"><small>反馈影响</small><b>${adjustmentText}</b></span><span><small>最终建议</small><b>${escapeHtml(recommendation)}</b></span></div><p class="fit-reason">${escapeHtml(assessment.reason)}</p>${matched ? `<div class="fit-terms"><small>画像关联</small>${matched}</div>` : `<div class="fit-terms empty"><small>画像关联</small><em>尚未确认明确画像词</em></div>`}${gaps ? `<ul class="fit-gaps">${gaps}</ul>` : ""}<div class="fit-action"><div><small>主要风险</small><p>${escapeHtml(assessment.risk)}</p></div><div><small>下一步</small><p>${escapeHtml(assessment.next_action)}</p></div></div><p class="fit-personalization">${escapeHtml(assessment.personalization_reason)}</p>${quotes ? `<details class="fit-evidence"><summary>查看 AI / 本地判断实际引用的逐字证据</summary>${quotes}${evidenceSource}</details>` : ""}</section>`;
}

function renderFeedbackControls(item) {
  const current = state.feedbackMap.get(feedbackKey(item.canonical_id, item.version_hash));
  const buttons = Object.entries(FEEDBACK_LABELS).map(([verdict, label]) => `<button class="feedback-verdict ${current?.verdict === verdict ? "active" : ""}" data-verdict="${verdict}" aria-pressed="${current?.verdict === verdict}">${label}</button>`).join("");
  return `<div class="feedback-panel"><div class="feedback-panel-head"><div><b>这条判断对吗？</b><span>你的直接反馈优先，并对相似机会产生最多 ±12 分的透明调整。</span></div><div class="feedback-panel-status">${current ? `<span class="feedback-current">当前：${escapeHtml(FEEDBACK_LABELS[current.verdict] || current.verdict)}</span>` : ""}<span class="feedback-operation" aria-live="polite"></span></div></div><div class="feedback-row">${buttons}<input class="feedback-reason" maxlength="500" value="${escapeHtml(current?.reason || "")}" placeholder="可选原因，例如：符合信创服务器交付能力"><button class="secondary-button compact update-feedback-reason" ${current ? "" : "disabled"}>保存原因</button><button class="feedback-delete ${current ? "" : "hidden"}">撤销反馈</button></div></div>`;
}

function setFeedbackControlsBusy(card, busy, message = "") {
  const current = state.feedbackMap.get(feedbackKey(card.dataset.canonical, card.dataset.version));
  card.querySelector(".feedback-panel")?.setAttribute("aria-busy", String(busy));
  card.querySelectorAll(".feedback-verdict").forEach((control) => { control.disabled = busy; });
  const reason = card.querySelector(".feedback-reason"); if (reason) reason.disabled = busy;
  const update = card.querySelector(".update-feedback-reason"); if (update) update.disabled = busy || !current;
  const remove = card.querySelector(".feedback-delete"); if (remove) remove.disabled = busy || !current;
  const operation = card.querySelector(".feedback-operation"); if (operation) operation.textContent = message;
}

function bindResultFeedbackActions() {
  $$(".result-card").forEach((card) => {
    card.querySelectorAll(".feedback-verdict").forEach((button) => button.addEventListener("click", () => saveResultFeedback(card, button.dataset.verdict, button)));
    card.querySelector(".update-feedback-reason")?.addEventListener("click", (event) => {
      const current = state.feedbackMap.get(feedbackKey(card.dataset.canonical, card.dataset.version));
      if (current) saveResultFeedback(card, current.verdict, event.currentTarget);
    });
    card.querySelector(".feedback-delete")?.addEventListener("click", (event) => deleteResultFeedback(card, event.currentTarget));
  });
}

async function saveResultFeedback(card, verdict, button) {
  const canonical = card.dataset.canonical, version = card.dataset.version;
  const reason = card.querySelector(".feedback-reason").value.trim();
  setFeedbackControlsBusy(card, true, "正在保存并重新计算…");
  let saved;
  try {
    saved = await configApi(`/api/v1/feedback/${encodeURIComponent(canonical)}/${encodeURIComponent(version)}`, { method: "PUT", body: JSON.stringify({ verdict, reason }), timeoutMs: 15000 });
  } catch (error) { toast(`反馈没有保存：${error.message}`, 8000); }
  if (!saved) { if (card.isConnected) setFeedbackControlsBusy(card, false); return; }
  state.feedbackMap.set(feedbackKey(canonical, version), saved); renderResults(state.run, false);
  toast(`反馈已保存为“${FEEDBACK_LABELS[verdict]}”，正在重新计算适配分`);
  try { await refreshCurrentAssessments(); toast(`适配分已使用“${FEEDBACK_LABELS[verdict]}”反馈重新计算`); }
  catch (error) { toast(`反馈已保存，但适配分暂未刷新：${error.message}`, 8000); }
  try { await loadFeedbackMemory(state.activeTab === "decision"); }
  catch (error) { toast(`反馈已保存，但反馈列表暂未刷新：${error.message}`, 8000); }
}

async function deleteResultFeedback(card, button) {
  const canonical = card.dataset.canonical, version = card.dataset.version;
  setFeedbackControlsBusy(card, true, "正在撤销并重新计算…");
  let deleted = false;
  try {
    await configApi(`/api/v1/feedback/${encodeURIComponent(canonical)}/${encodeURIComponent(version)}`, { method: "DELETE", timeoutMs: 15000 });
    deleted = true;
  } catch (error) {
    if (error.transient) {
      try { await loadFeedbackMemory(false); deleted = !state.feedbackMap.has(feedbackKey(canonical, version)); }
      catch { deleted = false; }
    }
    if (!deleted) toast(`反馈撤销结果尚未确认：${error.message}`, 8000);
  }
  if (!deleted) { if (card.isConnected) setFeedbackControlsBusy(card, false); return; }
  state.feedbackMap.delete(feedbackKey(canonical, version)); renderResults(state.run, false); renderDecisionSummary();
  toast("反馈已撤销，正在移除个性化影响");
  try { await refreshCurrentAssessments(); toast("反馈已撤销，个性化影响已移除"); }
  catch (error) { toast(`反馈已撤销，但适配分暂未刷新：${error.message}`, 8000); }
  try { await loadFeedbackMemory(state.activeTab === "decision"); }
  catch (error) { toast(`反馈已撤销，但反馈列表暂未刷新：${error.message}`, 8000); }
}

async function refreshCurrentAssessments() {
  if (!state.run?.run_id) return;
  const runId = state.run.run_id;
  const sequence = ++state.assessmentRefreshSequence;
  $$("#result-list .result-card").forEach((card) => setFeedbackControlsBusy(card, true, "正在刷新本轮适配判断…"));
  const refreshButton = $("#refresh-assessments"); if (refreshButton) { refreshButton.disabled = true; refreshButton.textContent = "刷新中…"; }
  try {
    const assessments = await api(`/api/v1/runs/${encodeURIComponent(runId)}/assessments`, { method: "POST" });
    if (sequence !== state.assessmentRefreshSequence || state.run?.run_id !== runId) return;
    state.run.opportunity_assessments = assessments;
    renderResults(state.run, false);
  } finally {
    if (sequence === state.assessmentRefreshSequence && state.run?.run_id === runId) {
      $$("#result-list .result-card").forEach((card) => setFeedbackControlsBusy(card, false));
      if (refreshButton) { refreshButton.disabled = false; refreshButton.textContent = "刷新适配判断"; }
    }
  }
}

function renderRunQA(run) {
  const root = $("#run-qa");
  if (!root || !run?.run_id) { root?.classList.add("hidden"); return; }
  root.classList.remove("hidden");
  if (state.qaAnswer?.run_id === run.run_id) renderRunQAAnswer(state.qaAnswer);
  else { $("#run-qa-answer").classList.add("hidden"); $("#run-qa-state").textContent = "等待提问"; $("#run-qa-state").className = "state-badge muted"; }
}

function renderRunQAAnswer(answer) {
  const root = $("#run-qa-answer"), badge = $("#run-qa-state");
  const labels = {
    applied: "AI 逐字选证", cached: "复用已验证答案", not_configured: "模型未配置 · 本地抽取",
    unavailable: "模型不可用 · 本地抽取", invalid_response: "AI 输出被拒绝 · 本地抽取",
    insufficient_evidence: "证据不足", refused: "安全拒绝", empty: "本轮无证据", evidence_incomplete: "证据快照损坏",
  };
  const ok = answer.answerable;
  badge.textContent = labels[answer.status] || answer.status; badge.className = `state-badge ${ok ? "success" : ["refused", "evidence_incomplete"].includes(answer.status) ? "danger" : "warning"}`;
  const citations = (answer.claims || []).flatMap((claim) => claim.citations || []).map((citation) => {
    const sourceUrl = safeUrl(citation.source_url);
    const source = sourceUrl ? `<a href="${escapeHtml(sourceUrl)}" target="_blank" rel="noreferrer">${escapeHtml(citation.title)} ↗</a>` : `<span class="missing-source">原文链接未保留 · ${escapeHtml(citation.title)}</span>`;
    return `<article><div><b>${escapeHtml(citation.evidence_id)}</b><span>${escapeHtml(citation.event_type)} · ${escapeHtml(citation.published_at.slice(0,10))}</span></div><p>${escapeHtml(citation.quote)}</p>${source}</article>`;
  }).join("");
  const limitations = (answer.limitations || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  root.innerHTML = `<p class="run-qa-answer-text">${escapeHtml(answer.answer).replaceAll("\n", "<br>")}</p><div class="run-qa-meta"><span>本轮 ${answer.total_evidence_count} 条</span><span>本次上下文 ${answer.context_evidence_count} 条</span><span>${answer.context_truncated ? "已做有界选取" : "使用完整本轮证据"}</span><span>${answer.latency_ms || 0} ms</span></div>${citations ? `<div class="run-qa-citations">${citations}</div>` : ""}${limitations ? `<details><summary>回答边界与限制</summary><ul>${limitations}</ul></details>` : ""}<small>${escapeHtml(answer.summary)}</small>`;
  root.classList.remove("hidden");
}

function renderRunQAError(message) {
  const root = $("#run-qa-answer"), badge = $("#run-qa-state");
  badge.textContent = "回答未完成"; badge.className = "state-badge danger";
  root.innerHTML = `<p class="run-qa-answer-text">${escapeHtml(message)}</p><small>本次没有展示或缓存未经验证的模型内容。请检查模型连接后重试，或换成指定 E 编号的问题。</small>`;
  root.classList.remove("hidden");
}

async function askRunEvidence() {
  if (!state.run?.run_id) return toast("请先完成一次情报检索");
  if (state.qaInFlight) return toast("正在核对上一条问题，请等待回答完成");
  const question = $("#run-question").value.trim(); if (question.length < 2) return toast("请先输入至少 2 个字的问题");
  const runId = state.run.run_id;
  const sequence = ++state.qaRequestSequence;
  const controller = new AbortController(); state.qaAbortController = controller; state.qaInFlight = true;
  const button = $("#ask-run"), input = $("#run-question"); button.disabled = true; input.disabled = true; button.textContent = "正在核对本轮证据…";
  $$("[data-run-question]").forEach((control) => { control.disabled = true; });
  $("#run-qa-state").textContent = "正在逐字核验"; $("#run-qa-state").className = "state-badge muted";
  try {
    const answer = await api(`/api/v1/runs/${encodeURIComponent(runId)}/ask`, { method: "POST", body: JSON.stringify({ question }), signal: controller.signal });
    if (sequence !== state.qaRequestSequence || state.run?.run_id !== runId || answer.run_id !== runId) return;
    state.qaAnswer = answer; renderRunQAAnswer(answer);
  } catch (error) {
    if (sequence === state.qaRequestSequence && state.run?.run_id === runId) renderRunQAError(error.message);
  }
  finally {
    if (sequence === state.qaRequestSequence && state.run?.run_id === runId) {
      state.qaInFlight = false; state.qaAbortController = null; button.disabled = false; input.disabled = false; button.textContent = "根据本轮证据回答";
      $$("[data-run-question]").forEach((control) => { control.disabled = false; });
    }
  }
}

function renderIntelligenceBrief(run) {
  const root = $("#intelligence-brief"), brief = run.intelligence_brief;
  if (!root) return;
  if (!brief) { root.innerHTML = ""; root.classList.add("hidden"); return; }
  root.classList.remove("hidden");
  const statusLabels = {
    applied: "证据约束 AI", cached: "AI 缓存复用", disabled: "确定性建议 · AI 已关闭",
    not_configured: "确定性建议 · 模型未配置", invalid_response: "确定性建议 · AI 输出已拒绝",
    unavailable: "确定性建议 · 模型不可用", empty: "零结果诊断",
  };
  const riskLabels = { high: "高风险", medium: "中风险", low: "提示" };
  const catalog = new Map((brief.evidence_catalog || []).map((item) => [item.evidence_id, item]));
  const references = (ids) => (ids || []).map((id) => {
    const evidence = catalog.get(id);
    if (!evidence) return `<span>${escapeHtml(id)}</span>`;
    const sourceUrl = safeUrl(evidence.source_url);
    return sourceUrl ? `<a href="${escapeHtml(sourceUrl)}" target="_blank" rel="noopener" title="${escapeHtml(evidence.title)}">${escapeHtml(id)}</a>` : `<span title="原文链接未保留">${escapeHtml(id)}</span>`;
  }).join(" ");
  const needs = (brief.buyer_needs || []).map((item) => `<li><span>${references(item.evidence_ids)}</span><p>${escapeHtml(item.text)}</p></li>`).join("");
  const priorities = (brief.priorities || []).map((item) => {
    const sourceUrl = safeUrl(item.source_url);
    const source = sourceUrl ? `<a href="${escapeHtml(sourceUrl)}" target="_blank" rel="noopener">核验证据原文 ↗</a>` : `<span class="missing-source">原文链接未保留</span>`;
    return `<article class="brief-priority"><div><span>${escapeHtml(item.evidence_id)}</span><small>${escapeHtml(item.event_type)} · ${Math.round(item.opportunity_score)} 分</small></div><h4>${escapeHtml(item.title)}</h4><p>${escapeHtml(item.buyer || "采购人待原文确认")}</p><dl><div><dt>为什么优先</dt><dd>${escapeHtml(item.reason)}</dd></div><div><dt>下一步</dt><dd>${escapeHtml(item.recommended_action)}</dd></div></dl>${source}</article>`;
  }).join("");
  const risks = (brief.risks || []).map((item) => `<li class="${escapeHtml(item.level)}"><b>${escapeHtml(riskLabels[item.level] || item.level)}</b><p>${escapeHtml(item.text)}</p><span>${references(item.evidence_ids)}</span></li>`).join("");
  const actions = (brief.actions || []).map((item) => `<li><b>${escapeHtml(item.priority)}</b><p>${escapeHtml(item.text)}</p><span>${references(item.evidence_ids)}</span></li>`).join("");
  const fallback = brief.mode !== "llm_grounded";
  root.innerHTML = `<div class="brief-head"><div><p class="eyebrow">EVIDENCE-GROUNDED COPILOT</p><h3>AI 情报副驾驶</h3></div><span class="state-badge ${fallback ? "warning" : "success"}">${escapeHtml(statusLabels[brief.status] || brief.status)}</span></div><p class="brief-overview">${escapeHtml(brief.overview)}</p><small class="brief-summary">${escapeHtml(brief.summary)}</small>${priorities ? `<div class="brief-priorities">${priorities}</div>` : ""}<div class="brief-grid">${needs ? `<section><h4>采购需求判断</h4><ul class="brief-claims">${needs}</ul></section>` : ""}${risks ? `<section><h4>风险提示</h4><ul class="brief-risks">${risks}</ul></section>` : ""}${actions ? `<section class="wide"><h4>建议行动</h4><ul class="brief-actions">${actions}</ul></section>` : ""}</div>`;
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
  const sourceUrl = safeUrl(record.source_urls[0] || "");
  const source = sourceUrl ? `<a href="${escapeHtml(sourceUrl)}" target="_blank" rel="noreferrer">原文 ↗</a>` : `<span class="missing-source">原文链接未保留</span>`;
  const tags = item.tags.map((tag) => `<span>${escapeHtml(tag)}</span>`).join("");
  const archived = item.stage === "archived";
  return `<article class="opportunity-card ${item.is_read ? "" : "unread"}" data-id="${escapeHtml(item.id)}" data-project="${escapeHtml(item.project_key)}">
    <div class="opportunity-card-head"><div><div class="opportunity-kicker"><span>${escapeHtml(record.event_type)}</span><span>${escapeHtml(record.region || "地域未标注")}</span></div><span class="workspace-stage ${escapeHtml(item.stage)}">工作阶段：${escapeHtml(OPPORTUNITY_STAGES[item.stage] || item.stage)}</span><h3>${escapeHtml(record.title)}</h3></div><div class="score">${Math.round(record.opportunity_score)}</div></div>
    <p class="opportunity-buyer">${escapeHtml(record.buyer || "采购人未提取")} · ${escapeHtml(record.published_at.slice(0, 10))}</p>
    <div class="opportunity-tags">${tags || "<span>未设置标签</span>"}</div>
    <div class="opportunity-next"><span><small>负责人</small><b>${escapeHtml(item.owner || "待分配")}</b></span><span><small>下一步</small><b>${item.next_action_at ? formatTime(item.next_action_at) : "待安排"}</b></span></div>
    <div class="opportunity-links">${source}<button class="mark-read">${item.is_read ? "标为未读" : "标为已读"}</button><button class="view-timeline" aria-expanded="false">查看原公告时间线</button><button class="archive-opportunity">${archived ? "恢复到待评估" : "归档保留"}</button><button class="delete-opportunity" data-confirm="false">删除卡片</button></div>
    <details class="opportunity-editor"><summary>跟进设置</summary><div class="opportunity-form">
      <label><span>阶段</span><select class="opportunity-stage" aria-label="机会阶段">${opportunityStageOptions(item.stage)}</select></label>
      <label><span>负责人</span><input class="opportunity-owner" maxlength="100" value="${escapeHtml(item.owner)}" placeholder="姓名或团队"></label>
      <label><span>下一步时间</span><input class="opportunity-next-at" type="datetime-local" value="${formatDateTimeInput(item.next_action_at)}"></label>
      <label><span>标签</span><input class="opportunity-tags-input" maxlength="240" value="${escapeHtml(item.tags.join("，"))}" placeholder="重点，GPU，教育"></label>
      <label class="opportunity-notes-label"><span>跟进备注</span><textarea class="opportunity-notes" rows="3" maxlength="4000" placeholder="记录判断、风险与下一步">${escapeHtml(item.notes)}</textarea></label>
    </div><button class="primary-button compact save-opportunity">保存跟进</button></details>
    <div class="opportunity-delete-note hidden" aria-live="polite"></div><div class="opportunity-timeline hidden"></div>
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
    const projectKey = card.dataset.project;
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
    card.querySelector(".archive-opportunity").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      const archived = card.querySelector(".opportunity-stage").value === "archived";
      button.disabled = true;
      button.textContent = archived ? "恢复中…" : "归档中…";
      try {
        await api(`/api/v1/opportunities/${id}`, {
          method: "PATCH",
          body: JSON.stringify({ stage: archived ? "new" : "archived" }),
        });
        toast(archived ? "卡片已恢复到“待评估”，可以继续跟进" : "卡片已归档保留，跟进信息和原公告时间线都没有删除", 5000);
        await loadOpportunities();
      } catch (error) {
        button.disabled = false;
        button.textContent = archived ? "恢复到待评估" : "归档保留";
        toast(error.message, 5000);
      }
    });
    card.querySelector(".delete-opportunity").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      const note = card.querySelector(".opportunity-delete-note");
      if (button.dataset.confirm !== "true") {
        button.dataset.confirm = "true";
        button.classList.add("armed");
        button.textContent = "再次点击，确认删除";
        note.classList.remove("hidden");
        note.innerHTML = "<b>只删除这张工作台卡片。</b><span>原始标讯、报告、反馈和来源证据仍会保留，以后可以从检索结果重新加入。</span><button type=\"button\" class=\"cancel-opportunity-delete\">取消</button>";
        note.querySelector(".cancel-opportunity-delete").addEventListener("click", () => {
          button.dataset.confirm = "false";
          button.classList.remove("armed");
          button.textContent = "删除卡片";
          note.classList.add("hidden");
          note.replaceChildren();
        }, { once: true });
        return;
      }
      button.disabled = true;
      button.textContent = "正在删除…";
      note.querySelector(".cancel-opportunity-delete")?.setAttribute("disabled", "");
      try {
        await api(`/api/v1/opportunities/${id}`, { method: "DELETE", timeoutMs: 30000 });
        state.opportunityProjectKeys.delete(projectKey);
        toast("工作台卡片已删除；原始标讯仍然保留，可随时重新加入", 5500);
        await loadOpportunities();
      } catch (error) {
        if (error.transient) {
          try {
            await api(`/api/v1/opportunities/${id}`, { timeoutMs: 10000 });
          } catch (checkError) {
            if (checkError.status === 404) {
              state.opportunityProjectKeys.delete(projectKey);
              toast("删除请求已生效；原始标讯仍然保留，可随时重新加入", 5500);
              await loadOpportunities();
              return;
            }
          }
        }
        button.disabled = false;
        button.dataset.confirm = "false";
        button.classList.remove("armed");
        button.textContent = "删除卡片";
        note.classList.remove("hidden");
        note.textContent = `${error.message}。卡片状态尚未确认，请刷新后再操作。`;
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
        root.innerHTML = events.map((event) => { const sourceUrl = safeUrl(event.source_urls[0] || ""); const source = sourceUrl ? `<a href="${escapeHtml(sourceUrl)}" target="_blank" rel="noreferrer">查看证据 ↗</a>` : `<span class="missing-source">原文链接未保留</span>`; return `<div class="timeline-event"><i></i><div><b>${escapeHtml(event.event_type)} · ${escapeHtml(event.published_at.slice(0, 10))}</b><p>${escapeHtml(event.title)}</p>${source}</div></div>`; }).join("");
      } catch (error) { root.textContent = error.message; }
    });
  });
}

function buyerRadarCard(item) {
  const stages = Object.entries(item.stage_counts || {}).map(([stage, count]) => `<span><b>${escapeHtml(stage)}</b>${count} 条</span>`).join("");
  const topics = (item.top_topics || []).map((topic) => `<span><b>${escapeHtml(topic.name)}</b>${topic.notice_count} 条</span>`).join("");
  const activities = (item.recent_activities || []).map((activity) => {
    const sourceUrl = safeUrl(activity.source_url || "");
    const source = sourceUrl ? `<a href="${escapeHtml(sourceUrl)}" target="_blank" rel="noreferrer">核验官方原文 ↗</a>` : `<span class="missing-source">原文链接未保留</span>`;
    return `<article class="buyer-activity"><div><span>${escapeHtml(activity.event_type)}</span><time>${escapeHtml(activity.published_at.slice(0, 10))}</time></div><h4>${escapeHtml(activity.title)}</h4><p>${escapeHtml(activity.summary || "该公告没有可用摘要，请以原文为准。")}</p><footer><span>${escapeHtml(activity.region || "地域未标注")} · ${escapeHtml(activity.source_name || "来源未标注")}</span>${source}</footer></article>`;
  }).join("");
  const topic = item.top_topics?.[0]?.name || "采购项目";
  const monitorName = `${item.buyer_name}采购监控`.slice(0, 100);
  const monitorQuery = `每天9点汇总最近1个月${topic}采购公告`.slice(0, 500);
  const channelChoices = channelOptions("local") || '<option value="local">本地报告</option>';
  return `<article class="buyer-radar-card" data-buyer-id="${escapeHtml(item.buyer_id)}">
    <div class="buyer-radar-card-head"><div><p class="eyebrow">VERIFIED LOCAL BUYER</p><h3>${escapeHtml(item.buyer_name)}</h3><span>${escapeHtml((item.sources || []).join(" · ") || "来源未标注")}</span></div><div class="buyer-activity-count"><b>${item.notice_count}</b><span>本地公告</span></div></div>
    <div class="buyer-radar-metrics"><span><small>估算项目</small><b>${item.project_count}</b></span><span><small>内容版本</small><b>${item.version_count}</b></span><span><small>最早活动</small><b>${escapeHtml(item.first_activity_at.slice(0, 10))}</b></span><span><small>最近活动</small><b>${escapeHtml(item.latest_activity_at.slice(0, 10))}</b></span></div>
    <div class="buyer-radar-section"><small>公告生命周期（不是销售跟进阶段）</small><div class="buyer-stage-chips">${stages || "<span>暂无可统计阶段</span>"}</div></div>
    <div class="buyer-radar-section"><small>本地公告高频主题（不是采购预测）</small><div class="buyer-topic-chips">${topics || "<span>暂无可统计主题</span>"}</div></div>
    <details class="buyer-activities"><summary>查看近期原文证据（${item.recent_activities.length} 条）</summary><div>${activities || '<p class="buyer-empty-copy">暂无可展示活动。</p>'}</div></details>
    <details class="buyer-monitor-editor"><summary>＋ 为这个采购单位创建自动监控</summary><div>
      <label><span>任务名称</span><input class="buyer-monitor-name" maxlength="100" value="${escapeHtml(monitorName)}"></label>
      <label class="wide"><span>自然语言规则</span><textarea class="buyer-monitor-query" rows="3" maxlength="500">${escapeHtml(monitorQuery)}</textarea><small>必须写清每天、每周、每月或未来某个时间；采购单位已由系统锁定，不必重复填写。</small></label>
      <label><span>投递方式</span><select class="buyer-monitor-channel">${channelChoices}</select></label>
      <label><span>无新增时</span><select class="buyer-monitor-policy"><option value="on_change">保持安静</option><option value="always">也发送回执</option></select></label>
      <label class="buyer-monitor-immediate"><input class="buyer-monitor-run" type="checkbox" checked><span><b>创建后立即检查一次</b><small>长期任务仍会持久保存；worker 离线时会等待恢复。</small></span></label>
      <div class="buyer-monitor-action"><button class="primary-button compact create-buyer-monitor">创建精准监控</button><span class="buyer-monitor-state" aria-live="polite"></span></div>
    </div></details>
  </article>`;
}

function bindBuyerRadarActions() {
  $$(".buyer-radar-card").forEach((card) => {
    card.querySelector(".create-buyer-monitor").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      const status = card.querySelector(".buyer-monitor-state");
      const name = card.querySelector(".buyer-monitor-name").value.trim();
      const query = card.querySelector(".buyer-monitor-query").value.trim();
      if (!name) return toast("请先填写任务名称");
      if (query.length < 2) return toast("请写清要监控的主题和执行时间");
      button.disabled = true;
      button.textContent = "正在创建…";
      status.textContent = "正在锁定采购单位并解析执行计划…";
      try {
        const runImmediately = card.querySelector(".buyer-monitor-run").checked;
        const subscription = await api(`/api/v1/buyers/${card.dataset.buyerId}/subscriptions`, {
          method: "POST",
          body: JSON.stringify({
            name,
            query,
            delivery_channel: card.querySelector(".buyer-monitor-channel").value || "local",
            delivery_policy: card.querySelector(".buyer-monitor-policy").value,
            run_immediately: runImmediately,
          }),
        });
        status.textContent = `已锁定：${subscription.spec.buyer_keywords.join("、")}；正在打开订阅中心。`;
        toast(`精准监控已创建：${subscription.name}`, 5000);
        await activateTab("subscriptions");
        if (runImmediately) pollSubscription(subscription.id, subscription.last_run_at);
      } catch (error) {
        button.disabled = false;
        button.textContent = "创建精准监控";
        status.textContent = error.message;
        toast(error.message, 6000);
      }
    });
  });
}

async function loadBuyerRadar() {
  const sequence = ++state.buyerRadarLoadSequence;
  const search = $("#buyer-radar-search").value.trim();
  const limit = $("#buyer-radar-limit").value || "50";
  const summary = $("#buyer-radar-summary"), coverage = $("#buyer-radar-coverage"), grid = $("#buyer-radar-grid");
  summary.innerHTML = '<div class="loading-card">正在统计本地买方证据…</div>';
  grid.setAttribute("aria-busy", "true");
  try {
    await loadSystemStatus().catch(() => null);
    const params = new URLSearchParams({ limit, activity_limit: "5" });
    if (search) params.set("search", search);
    const result = await api(`/api/v1/buyers?${params}`);
    if (sequence !== state.buyerRadarLoadSequence) return null;
    summary.innerHTML = [
      [search ? "匹配采购单位" : "本地采购单位", search ? result.matched_buyer_count : result.total_buyer_count],
      ["已识别公告", result.identified_buyer_notice_count],
      ["识别覆盖率", `${result.buyer_coverage_rate}%`],
      ["未识别公告", result.unknown_buyer_notice_count],
    ].map(([label, value]) => `<div class="metric"><small>${label}</small><b>${value}</b></div>`).join("");
    coverage.innerHTML = `<b>先看数据边界：</b><span>当前只统计本机已保存的 ${result.total_local_notice_count} 条去重公告；返回 ${result.returned_buyer_count} 家。项目数是估算，不代表采购方官方项目总量。${result.invalid_version_count ? `另有 ${result.invalid_version_count} 个损坏历史版本未参与统计。` : "历史快照均可读取。"}</span>`;
    if (!result.buyers.length) {
      grid.innerHTML = `<section class="buyer-radar-zero"><span>◎</span><h2>${search ? "没有匹配的本地采购单位" : "本地还没有可识别的采购单位"}</h2><p>${search ? "换一个采购单位简称、产品主题或来源名称，也可以清空搜索查看全部。" : "先去“情报检索”执行真实查询；系统只会聚合实际保存的公告，不填充演示假数据。"}</p><button class="secondary-button" data-buyer-empty-action="${search ? "clear" : "search"}">${search ? "清空搜索" : "去情报检索"}</button></section>`;
      grid.querySelector("[data-buyer-empty-action]").addEventListener("click", async (event) => {
        if (event.currentTarget.dataset.buyerEmptyAction === "clear") { $("#buyer-radar-search").value = ""; await loadBuyerRadar(); }
        else { await activateTab("search"); $("#query-input").focus(); }
      });
    } else {
      grid.innerHTML = result.buyers.map(buyerRadarCard).join("");
      bindBuyerRadarActions();
    }
    return result;
  } finally {
    if (sequence === state.buyerRadarLoadSequence) grid.removeAttribute("aria-busy");
  }
}

async function setOpportunityWorkspaceView(view) {
  const buyers = view === "buyers";
  state.opportunityView = buyers ? "buyers" : "opportunities";
  $("#opportunity-workspace-view").classList.toggle("hidden", buyers);
  $("#buyer-radar-view").classList.toggle("hidden", !buyers);
  $$("[data-workspace-view]").forEach((button) => {
    const active = button.dataset.workspaceView === state.opportunityView;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  if (buyers) await loadBuyerRadar();
  else await loadOpportunities();
}

async function loadSources() {
  const [statusRows, health] = await Promise.all([
    api("/api/v1/sources/status"),
    api("/api/v1/sources/health?window=20"),
  ]);
  const healthById = new Map((health.sources || []).map((row) => [row.id, row]));
  const rows = statusRows.map((row) => ({ ...row, health: healthById.get(row.id) || null }));
  state.sources = rows;
  state.sourceHealth = health;
  const proofSourceCount = $("#proof-source-count"); if (proofSourceCount) proofSourceCount.textContent = rows.length;
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
  if (row.health?.health_level === "unhealthy") return ["持续异常", "danger"];
  if (row.health?.health_level === "auth_required") return ["等待授权", "warning"];
  if (row.health?.health_level === "degraded") return ["运行降级", "warning"];
  if (row.health?.health_level === "healthy") return ["运行健康", "success"];
  if (row.health?.health_level === "no_data") return ["等待首轮", "muted"];
  if (row.last_status === "failed") return ["最近失败", "danger"];
  if (["partial", "auth_required"].includes(row.last_status)) return ["部分可用", "warning"];
  if (row.last_status === "skipped") return ["按地域跳过", "muted"];
  if (row.last_status === "ok") return ["运行正常", "success"];
  return ["等待首轮", "muted"];
}

function renderSourceHealth(row) {
  const health = row.health;
  if (!health || !health.sample_count) return `<div class="source-health empty"><b>健康历史</b><p>尚无真实运行样本；完成一次检索后自动生成趋势。</p></div>`;
  const trendLabels = { improving: "改善", stable: "稳定", worsening: "变差", insufficient_data: "样本不足" };
  const history = health.history || [];
  const bars = history.map((item) => `<i class="${escapeHtml(item.status)}" title="${escapeHtml(formatTime(item.started_at))} · ${escapeHtml(SOURCE_STATUS_LABELS[item.status] || item.status)} · 扫描 ${item.scanned_count} / 保留 ${item.kept_count}"></i>`).join("");
  const rows = history.slice(-6).reverse().map((item) => `<div class="source-history-row"><time>${escapeHtml(formatTime(item.started_at))}</time><b class="${escapeHtml(item.status)}">${escapeHtml(SOURCE_STATUS_LABELS[item.status] || item.status)}</b><span>${item.scanned_count} / ${item.fetched_count} / ${item.kept_count}</span><em>${item.latency_ms ? `${item.latency_ms} ms` : "—"}</em></div>`).join("");
  return `<div class="source-health"><div class="source-health-head"><div><b>最近 ${health.sample_count} 次健康趋势</b><p>${escapeHtml(health.explanation)}</p></div><small>延迟 ${escapeHtml(trendLabels[health.latency_trend] || health.latency_trend)} · 产出 ${escapeHtml(trendLabels[health.yield_trend] || health.yield_trend)}</small></div><div class="source-health-bars" aria-label="最近运行状态序列">${bars}</div><div class="source-health-metrics"><span><small>完全健康</small><b>${health.healthy_rate ?? "—"}${health.healthy_rate == null ? "" : "%"}</b></span><span><small>完成率</small><b>${health.completion_rate ?? "—"}${health.completion_rate == null ? "" : "%"}</b></span><span><small>平均 / P95</small><b>${health.average_latency_ms || 0} / ${health.p95_latency_ms || 0} ms</b></span><span><small>候选保留率</small><b>${health.yield_rate || 0}%</b></span></div><details class="source-history"><summary>查看最近运行明细</summary><div class="source-history-head"><span>时间</span><span>状态</span><span>扫描/候选/保留</span><span>耗时</span></div>${rows}</details></div>`;
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
    const loginUrl = safeUrl(auth.login_url || "");
    return loginUrl ? `<a class="secondary-button compact" href="${escapeHtml(loginUrl)}" target="_blank" rel="noreferrer">打开原站工作台 ↗</a>` : "";
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
  const healthy = rows.filter((row) => row.health?.health_level === "healthy").length;
  const samples = rows.reduce((sum, row) => sum + (row.health?.sample_count || 0), 0);
  summary.innerHTML = [["已接入来源", rows.length], ["官方平台", official], ["当前可运行", configured], ["健康 / 样本", `${healthy} / ${samples}`]].map(([label, value]) => `<div class="metric"><small>${label}</small><b>${value}</b></div>`).join("");
  root.innerHTML = rows.map((row) => {
    const [label, tone] = sourceState(row);
    const access = row.member_enhanced ? `${row.mode} · 已授权增强` : row.mode;
    const checked = row.last_checked_at ? formatTime(row.last_checked_at) : "尚未运行";
    const message = row.last_message || (row.configured ? "等待首轮真实查询验证" : "需要用户在本机完成授权或配置");
    const auth = row.authorization || { state: "not_supported", authorization_scope: "该公开来源无需授权。", message: "" };
    const rejection = Object.entries(row.last_rejection_reasons || {}).sort((a, b) => b[1] - a[1]).map(([reason, count]) => `<span>${escapeHtml(FILTER_REASON_LABELS[reason] || reason)} ${count}</span>`).join("");
    return `<article class="source-card" data-source-id="${escapeHtml(row.id)}"><div class="source-card-head"><div><h3>${escapeHtml(row.name)}</h3><div class="source-tags"><span>${row.official ? "官方来源" : "行业来源"}</span><span>${escapeHtml(access)}</span><span>${escapeHtml(row.query_mode || "列表检索")}</span></div></div><span class="state-badge ${tone}">${label}</span></div><div class="source-capabilities">${sourceCapabilityTags(row)}</div><div class="source-stats"><span><small>最近检查</small><b>${checked}</b></span><span><small>扫描 / 候选 / 保留</small><b>${row.last_scanned_count || row.last_fetched_count || 0} / ${row.last_fetched_count || 0} / ${row.last_kept_count || 0}</b></span><span><small>最近耗时</small><b>${row.last_latency_ms ? `${row.last_latency_ms} ms` : "暂无"}</b></span></div>${rejection ? `<div class="source-rejections">${rejection}</div>` : ""}<p>${escapeHtml(message)}</p>${renderSourceHealth(row)}<details class="source-boundary"><summary>覆盖范围与限制</summary><p>${escapeHtml(row.coverage_note || "来源暂未提供覆盖说明。")}</p></details><div class="source-auth-panel ${escapeHtml(auth.state)}"><div><b>${escapeHtml(authStateLabel(auth))}</b><p>${escapeHtml(auth.authorization_scope || "")}</p><small>${escapeHtml(auth.message || "")}${auth.last_test_status === "passed" ? " · 最近测试通过" : auth.last_test_status === "failed" ? " · 最近测试失败" : ""}</small></div><div class="source-auth-actions">${sourceAuthActions(row)}</div></div></article>`;
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
  let response;
  try {
    response = await api("/api/v1/config/edit-token", { method: "POST", timeoutMs: 12000 });
  } catch (error) {
    if (!error.transient) throw error;
    response = await api("/api/v1/config/edit-token", { method: "POST", timeoutMs: 12000 });
  }
  state.configEditToken = response.edit_token;
  return state.configEditToken;
}

async function configApi(path, options = {}, retryToken = true, retryNetwork = true) {
  const token = await ensureConfigEditToken();
  try {
    return await api(path, { ...options, headers: { ...(options.headers || {}), "X-BidPilot-Config-Token": token } });
  } catch (error) {
    if (retryToken && error.status === 403) { await ensureConfigEditToken(true); return configApi(path, options, false, retryNetwork); }
    const method = String(options.method || "GET").toUpperCase();
    if (retryNetwork && error.transient && method === "PUT") return configApi(path, options, retryToken, false);
    throw error;
  }
}

async function loadFeedbackMemory(render = true) {
  const rows = await api("/api/v1/feedback?limit=5000");
  state.feedbackMap = new Map(rows.map((item) => [feedbackKey(item.canonical_id, item.version_hash), item]));
  if (render) { renderFeedbackMemory(rows); renderDecisionSummary(); }
  return rows;
}

function setProfileControlsLoading(loading) {
  $("#profile-fields").disabled = loading;
  $("#save-profile").disabled = loading || !state.profileLoaded;
}

async function loadCompanyProfile(force = false) {
  if (state.profileLoaded && !force) { renderCompanyProfile(state.profile); setProfileControlsLoading(false); return state.profile; }
  const sequence = ++state.profileLoadSequence;
  setProfileControlsLoading(true); $("#reload-profile").classList.add("hidden");
  const badge = $("#profile-state"); badge.textContent = "正在读取画像"; badge.className = "state-badge muted";
  $("#profile-save-result").textContent = "读取完成前不会允许空表单覆盖已有画像";
  try {
    const profile = await api("/api/v1/company-profile");
    if (sequence !== state.profileLoadSequence) return null;
    state.profile = profile; state.profileLoaded = true; state.profileDirty = false;
    renderCompanyProfile(profile); setProfileControlsLoading(false); return profile;
  } catch (error) {
    if (sequence !== state.profileLoadSequence) return null;
    state.profileLoaded = false; setProfileControlsLoading(true);
    badge.textContent = "画像读取失败"; badge.className = "state-badge danger";
    $("#profile-save-result").textContent = `${error.message}。请点击“重新读取画像”，系统不会提交当前空表单。`;
    $("#reload-profile").classList.remove("hidden");
    throw error;
  }
}

async function loadDecisionCenter() {
  const [profileResult, feedbackResult] = await Promise.allSettled([loadCompanyProfile(false), loadFeedbackMemory(false)]);
  if (feedbackResult.status === "fulfilled") renderFeedbackMemory(feedbackResult.value);
  else $("#feedback-memory").innerHTML = `<div class="feedback-zero"><span>!</span><b>反馈记忆暂时读取失败</b><p>${escapeHtml(feedbackResult.reason?.message || "请检查本地服务后重试")}</p></div>`;
  renderDecisionSummary();
  if (profileResult.status === "rejected" || feedbackResult.status === "rejected") toast("决策中心有部分数据暂未读取，请按页面提示重试", 8000);
}

function profileConfigured(profile = state.profile) {
  return Boolean(profile && (profile.offerings?.length || profile.strengths?.length || profile.target_regions?.length || profile.excluded_terms?.length || profile.preferred_buyers?.length));
}

function renderDecisionSummary() {
  const root = $("#decision-summary"); if (!root) return;
  const feedback = [...state.feedbackMap.values()];
  const positive = feedback.filter((item) => ["relevant", "contacted"].includes(item.verdict)).length;
  const negative = feedback.filter((item) => item.verdict === "irrelevant").length;
  root.innerHTML = [["画像状态", profileConfigured() ? "已生效" : "待填写"], ["产品 / 服务", state.profile?.offerings?.length || 0], ["反馈记忆", feedback.length], ["正向 / 无关", `${positive} / ${negative}`]].map(([label, value]) => `<div class="metric"><small>${label}</small><b>${escapeHtml(value)}</b></div>`).join("");
  const clear = $("#clear-feedback"); if (clear) clear.disabled = !feedback.length;
}

function renderCompanyProfile(profile) {
  $("#profile-company-name").value = profile.company_name || "";
  $("#profile-offerings").value = (profile.offerings || []).join("\n");
  $("#profile-strengths").value = (profile.strengths || []).join("\n");
  $("#profile-regions").value = (profile.target_regions || []).join("\n");
  $("#profile-excluded").value = (profile.excluded_terms || []).join("\n");
  $("#profile-buyers").value = (profile.preferred_buyers || []).join("\n");
  $("#profile-focus").value = profile.decision_focus || "balanced";
  const badge = $("#profile-state"); badge.textContent = profileConfigured(profile) ? "画像已生效" : "画像待填写"; badge.className = `state-badge ${profileConfigured(profile) ? "success" : "warning"}`;
  $("#profile-save-result").textContent = profile.updated_at ? `上次保存：${formatTime(profile.updated_at)} · 版本 ${profile.version}` : "首次保存后立即生效";
  $("#reload-profile").classList.add("hidden");
}

function collectCompanyProfile() {
  return {
    company_name: $("#profile-company-name").value.trim(),
    offerings: splitProfileList($("#profile-offerings").value),
    strengths: splitProfileList($("#profile-strengths").value),
    target_regions: splitProfileList($("#profile-regions").value),
    excluded_terms: splitProfileList($("#profile-excluded").value),
    preferred_buyers: splitProfileList($("#profile-buyers").value),
    decision_focus: $("#profile-focus").value,
  };
}

async function saveCompanyProfile() {
  if (!state.profileLoaded) return toast("企业画像尚未读取完成，当前不会提交空表单", 8000);
  const button = $("#save-profile"); button.disabled = true; button.textContent = "正在保存画像…";
  try {
    const profile = await configApi("/api/v1/company-profile", { method: "PUT", body: JSON.stringify(collectCompanyProfile()), timeoutMs: 30000 });
    state.profile = profile; state.profileDirty = false; renderCompanyProfile(profile); renderDecisionSummary();
    toast("企业画像已保存，正在刷新本轮适配判断");
    if (state.run?.run_id) {
      try { await refreshCurrentAssessments(); }
      catch (error) { toast(`画像已保存，但本轮适配分暂未刷新：${error.message}`, 8000); }
    }
  } catch (error) { toast(`画像没有保存：${error.message}`, 8000); }
  finally { button.disabled = !state.profileLoaded; button.textContent = "保存并立即用于后续判断"; }
}

function renderFeedbackMemory(rows = [...state.feedbackMap.values()]) {
  const root = $("#feedback-memory"); if (!root) return;
  if (!rows.length) { root.innerHTML = `<div class="feedback-zero"><span>◎</span><b>还没有反馈记忆</b><p>完成一次检索后，在结果卡选择“相关 / 无关 / 观察 / 已联系”。系统不会放入演示数据。</p><button class="secondary-button" data-go-search>去完成一次真实检索</button></div>`; root.querySelector("[data-go-search]")?.addEventListener("click", () => activateTab("search")); return; }
  root.innerHTML = rows.map((item) => {
    const options = Object.entries(FEEDBACK_LABELS).map(([value, label]) => `<option value="${value}" ${item.verdict === value ? "selected" : ""}>${label}</option>`).join("");
    return `<article data-canonical="${escapeHtml(item.canonical_id)}" data-version="${escapeHtml(item.version_hash)}"><div><span class="feedback-badge ${escapeHtml(item.verdict)}">${escapeHtml(FEEDBACK_LABELS[item.verdict] || item.verdict)}</span><small>${escapeHtml(formatTime(item.updated_at))}</small></div><h3>${escapeHtml(item.record.title)}</h3><p>${escapeHtml(item.reason || "未填写原因")}</p><div class="feedback-memory-meta"><span>${escapeHtml(item.record.buyer || "采购人未提取")} · ${escapeHtml(item.record.event_type)}</span><button class="feedback-memory-delete">撤销</button></div><details class="feedback-memory-editor"><summary>修改判断或原因</summary><div><label><span>判断</span><select class="feedback-memory-verdict">${options}</select></label><label><span>原因（可选）</span><input class="feedback-memory-reason" maxlength="500" value="${escapeHtml(item.reason || "")}" placeholder="补充为什么相关或无关"></label><button class="secondary-button compact feedback-memory-save">保存修改</button><span class="feedback-memory-state" aria-live="polite"></span></div></details></article>`;
  }).join("");
  $$("#feedback-memory .feedback-memory-save").forEach((button) => button.addEventListener("click", () => saveFeedbackMemoryCard(button.closest("article"), button)));
  $$("#feedback-memory .feedback-memory-delete").forEach((button) => button.addEventListener("click", () => deleteFeedbackMemoryCard(button.closest("article"), button)));
}

async function saveFeedbackMemoryCard(card, button) {
  const canonical = card.dataset.canonical, version = card.dataset.version;
  const verdict = card.querySelector(".feedback-memory-verdict").value;
  const reason = card.querySelector(".feedback-memory-reason").value.trim();
  const stateNode = card.querySelector(".feedback-memory-state"); button.disabled = true; stateNode.textContent = "正在保存…";
  let saved;
  try {
    saved = await configApi(`/api/v1/feedback/${encodeURIComponent(canonical)}/${encodeURIComponent(version)}`, { method: "PUT", body: JSON.stringify({ verdict, reason }), timeoutMs: 15000 });
  } catch (error) {
    stateNode.textContent = `没有保存：${error.message}`; button.disabled = false; return;
  }
  state.feedbackMap.set(feedbackKey(canonical, version), saved); renderFeedbackMemory(); renderDecisionSummary();
  toast(`反馈已修改为“${FEEDBACK_LABELS[verdict]}”`);
  if (state.run?.run_id) {
    try { await refreshCurrentAssessments(); }
    catch (error) { toast(`反馈已保存，但适配分暂未刷新：${error.message}`, 8000); }
  }
}

async function deleteFeedbackMemoryCard(card, button) {
  const canonical = card.dataset.canonical, version = card.dataset.version;
  const key = feedbackKey(canonical, version); button.disabled = true; button.textContent = "撤销中…";
  let deleted = false;
  try {
    await configApi(`/api/v1/feedback/${encodeURIComponent(canonical)}/${encodeURIComponent(version)}`, { method: "DELETE", timeoutMs: 15000 }); deleted = true;
  } catch (error) {
    if (error.transient) {
      try { await loadFeedbackMemory(false); deleted = !state.feedbackMap.has(key); }
      catch { deleted = false; }
    }
    if (!deleted) { button.disabled = false; button.textContent = "撤销"; toast(`撤销结果尚未确认：${error.message}`, 8000); return; }
  }
  state.feedbackMap.delete(key); renderFeedbackMemory(); renderDecisionSummary(); toast("反馈已撤销");
  if (state.run?.run_id) {
    try { await refreshCurrentAssessments(); }
    catch (error) { toast(`反馈已撤销，但适配分暂未刷新：${error.message}`, 8000); }
  }
}

async function clearAllFeedback() {
  if (!state.feedbackMap.size) return toast("当前没有可清空的反馈");
  const knownCount = state.feedbackMap.size;
  if (!window.confirm(`确定清空全部反馈吗？当前已读取 ${knownCount} 条；企业画像、原公告和报告不会删除。`)) return;
  const button = $("#clear-feedback"); button.disabled = true;
  let result = null;
  try {
    result = await configApi("/api/v1/feedback", { method: "DELETE", timeoutMs: 15000 });
  } catch (error) {
    if (error.transient) {
      try { const remaining = await loadFeedbackMemory(false); if (!remaining.length) result = { deleted_count: knownCount, confirmed_after_reconnect: true }; }
      catch { result = null; }
    }
    if (!result) toast(`清空结果尚未确认：${error.message}`, 8000);
  }
  if (result) {
    state.feedbackMap.clear(); renderFeedbackMemory(); renderDecisionSummary();
    toast(`已清空 ${result.deleted_count} 条反馈，正在移除个性化影响`);
    if (state.run?.run_id) {
      try { await refreshCurrentAssessments(); toast("全部反馈已清空，个性化调整归零"); }
      catch (error) { toast(`反馈已清空，但适配分暂未刷新：${error.message}`, 8000); }
    }
  }
  button.disabled = !state.feedbackMap.size;
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
    intelligence_brief_mode: config.ai.intelligence_brief_mode, intelligence_brief_max_records: config.ai.intelligence_brief_max_records,
    decision_assessment_mode: config.ai.decision_assessment_mode, decision_assessment_max_records: config.ai.decision_assessment_max_records,
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
    if (!spec) return toast("查询内容发生变化，请重新解析后再创建订阅", 6000);
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
  if (tab === "decision") await loadDecisionCenter(); if (tab === "opportunities") await setOpportunityWorkspaceView(state.opportunityView); if (tab === "subscriptions") await loadSubscriptions(); if (tab === "sources") await loadSources(); if (tab === "config") await loadConfig(); if (tab === "reports") await loadReports();
}

$$(".nav-link").forEach((button) => button.addEventListener("click", () => activateTab(button.dataset.tab).catch((error) => toast(error.message))));
$$("[data-workspace-view]").forEach((button) => button.addEventListener("click", () => setOpportunityWorkspaceView(button.dataset.workspaceView).catch((error) => toast(error.message, 5000))));
$$("[data-query]").forEach((button) => button.addEventListener("click", () => { $("#query-input").value = button.dataset.query; state.spec = null; state.intentRequestSequence += 1; }));
$("#parse-button").addEventListener("click", () => parseIntent().catch((error) => toast(error.message)));
$("#run-button").addEventListener("click", runQuery);
$("#create-subscription-button").addEventListener("click", () => createSubscriptionFromQuery().catch((error) => toast(error.message, 5000)));
$("#delivery-channel").addEventListener("change", renderChannelHint);
$("#refresh-opportunities").addEventListener("click", () => loadOpportunities().catch((error) => toast(error.message)));
$("#refresh-buyer-radar").addEventListener("click", () => loadBuyerRadar().catch((error) => toast(error.message, 5000)));
$("#buyer-radar-limit").addEventListener("change", () => loadBuyerRadar().catch((error) => toast(error.message, 5000)));
$("#buyer-radar-search").addEventListener("input", () => {
  clearTimeout(window.__buyerRadarSearchTimer);
  window.__buyerRadarSearchTimer = setTimeout(() => loadBuyerRadar().catch((error) => toast(error.message, 5000)), 280);
});
$("#opportunity-stage-filter").addEventListener("change", () => loadOpportunities().catch((error) => toast(error.message)));
$("#opportunity-search").addEventListener("input", () => {
  clearTimeout(window.__opportunitySearchTimer);
  window.__opportunitySearchTimer = setTimeout(() => loadOpportunities().catch((error) => toast(error.message)), 250);
});
$("#save-config").addEventListener("click", () => saveConfig().catch((error) => toast(error.message, 5000)));
$("#save-profile").addEventListener("click", () => saveCompanyProfile().catch((error) => toast(error.message, 5000)));
$("#reload-profile").addEventListener("click", () => loadCompanyProfile(true).catch(() => {}));
$("#clear-feedback").addEventListener("click", () => clearAllFeedback().catch((error) => toast(error.message, 5000)));
$("#refresh-assessments").addEventListener("click", async () => {
  try { await refreshCurrentAssessments(); toast("本轮企业适配判断已刷新"); }
  catch (error) { toast(`适配判断暂未刷新：${error.message}`, 8000); }
});
$("#ask-run").addEventListener("click", askRunEvidence);
$("#run-question").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); askRunEvidence(); } });
$$('[data-run-question]').forEach((button) => button.addEventListener("click", () => { $("#run-question").value = button.dataset.runQuestion; $("#run-question").focus(); }));
$("#test-model").addEventListener("click", testModelConnection);
$$('.channel-test').forEach((button) => button.addEventListener("click", () => testDeliveryChannel(button)));
$("#profile-fields").addEventListener("input", () => { if (state.profileLoaded) { state.profileDirty = true; $("#profile-save-result").textContent = "有未保存的修改"; } });
$("#profile-fields").addEventListener("change", () => { if (state.profileLoaded) { state.profileDirty = true; $("#profile-save-result").textContent = "有未保存的修改"; } });
$("#query-input").addEventListener("input", () => {
  state.spec = null; state.intentRequestSequence += 1;
  if (!state.runInFlight) { $("#parse-button").disabled = false; $("#parse-button").textContent = "解析意图"; }
});
$("#query-input").addEventListener("keydown", (event) => { if ((event.metaKey || event.ctrlKey) && event.key === "Enter") runQuery(); });
document.addEventListener("keydown", (event) => { if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); $("#query-input").focus(); } });
window.createBidPilotSubscription = createSubscriptionFromQuery;
Promise.all([loadSystemStatus(), loadSources(), loadReports(), loadSubscriptions(), loadOpportunities()]).catch((error) => toast(error.message));
setInterval(() => {
  const editing = $(".subscription-editor:not(.hidden)");
  if (state.activeTab === "subscriptions" && !editing) loadSubscriptions().catch(() => {});
}, 15000);
