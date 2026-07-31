const HISTORY_KEY = "ai-balances-context-v2-history";
const MAX_HISTORY = 30;
const state = { sessionId: null, revision: 0, busy: false, histories: loadHistory() };
const byId = (id) => document.getElementById(id);

function loadHistory() {
  try { return JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]"); }
  catch { return []; }
}
function saveHistory() {
  localStorage.setItem(HISTORY_KEY, JSON.stringify(state.histories.slice(0, MAX_HISTORY)));
}
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
}
function requestId() {
  return globalThis.crypto?.randomUUID?.() || `ui-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}
async function api(path, options = {}) {
  const response = await fetch(path, {headers:{"Content-Type":"application/json"}, ...options});
  let body = {};
  try { body = await response.json(); } catch { body = {}; }
  if (!response.ok) {
    const error = new Error(body.detail?.message || body.detail?.code || `HTTP ${response.status}`);
    error.status = response.status; error.code = body.detail?.code; error.body = body;
    throw error;
  }
  return body;
}

function activeHistory() { return state.histories.find((item) => item.sessionId === state.sessionId); }
function touchHistory(patch = {}) {
  let item = activeHistory();
  if (!item) {
    item = {sessionId: state.sessionId, title: "Новый чат", createdAt: new Date().toISOString(), messages: []};
    state.histories.unshift(item);
  }
  Object.assign(item, patch, {updatedAt: new Date().toISOString(), revision: state.revision});
  state.histories = [item, ...state.histories.filter((entry) => entry !== item)].slice(0, MAX_HISTORY);
  saveHistory(); renderHistory();
  return item;
}
function renderHistory() {
  const target = byId("historyList");
  if (!state.histories.length) {
    target.innerHTML = '<div class="history-empty">История появится после первого запроса</div>'; return;
  }
  target.innerHTML = state.histories.map((item) => `
    <div class="history-item ${item.sessionId === state.sessionId ? "active" : ""}">
      <button class="history-main" type="button" data-session="${escapeHtml(item.sessionId)}">
        <strong>${escapeHtml(item.title || "Новый чат")}</strong>
        <span>${escapeHtml(operationLabel(item.operation || "chat"))} · ${formatTime(item.updatedAt)}</span>
      </button>
      <button class="history-delete" type="button" data-delete="${escapeHtml(item.sessionId)}" title="Удалить">×</button>
    </div>`).join("");
}
function formatTime(value) {
  if (!value) return "сейчас";
  return new Intl.DateTimeFormat("ru-RU", {hour:"2-digit", minute:"2-digit"}).format(new Date(value));
}
function operationLabel(value) {
  return ({show:"просмотр", aggregate:"агрегация", compare:"сравнение", compare_periods:"сравнение периодов", group:"группировка", clarification:"уточнение", chat:"чат"})[value] || value;
}
function periodLabel(period) {
  if (!period) return "—";
  const start = new Date(`${period.date_from}T00:00:00`);
  const exclusive = new Date(`${period.date_to}T00:00:00`);
  exclusive.setDate(exclusive.getDate() - 1);
  const fmt = (date) => new Intl.DateTimeFormat("ru-RU", {day:"2-digit", month:"2-digit", year:"numeric"}).format(date);
  return `${fmt(start)} – ${fmt(exclusive)}`;
}
function numberLabel(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? new Intl.NumberFormat("ru-RU", {maximumFractionDigits: 2}).format(numeric) : String(value ?? "—");
}

function setStatus(text, kind = "ready") {
  byId("status").textContent = text;
  byId("statusPill").dataset.kind = kind;
}
function setStages(active = -1, failed = false) {
  document.querySelectorAll(".stage").forEach((node, index) => {
    node.classList.toggle("done", active > index && !failed);
    node.classList.toggle("active", active === index);
    node.classList.toggle("failed", failed && active === index);
  });
}
function renderContext(payload) {
  const session = payload.session || {};
  state.sessionId = session.session_id || state.sessionId;
  state.revision = session.revision ?? state.revision;
  byId("revision").textContent = `revision ${state.revision}`;
  const intent = payload.context?.active?.intent;
  if (!intent) {
    byId("scope").className = "scope empty";
    byId("scope").textContent = "Появится после первого успешного запроса";
    byId("contextBadge").textContent = "Контекст не задан";
    touchHistory(); return;
  }
  const periods = (intent.periods || []).map(periodLabel).join(", ") || "Период не задан";
  const allEntities = (intent.operands || []).flatMap((operand) => (operand.entities || []).map((item) => item.entity?.display_name).filter(Boolean));
  const uniqueEntities = [...new Set(allEntities)];
  const visible = uniqueEntities.slice(0, 4);
  const extra = uniqueEntities.length > visible.length ? ` · ещё ${uniqueEntities.length - visible.length}` : "";
  byId("contextBadge").textContent = `${operationLabel(intent.operation)} · ${periods}`;
  byId("scope").className = "scope";
  byId("scope").innerHTML = `
    <span class="context-chip"><small>Операция</small>${escapeHtml(operationLabel(intent.operation))}</span>
    <span class="context-chip"><small>Период</small>${escapeHtml(periods)}</span>
    <span class="context-chip wide"><small>Сущности</small>${escapeHtml(visible.join(" · ") || "не заданы")}${escapeHtml(extra)}</span>`;
  touchHistory({operation:intent.operation});
}

function appendMessage(kind, payload) {
  const item = {id:requestId(), kind, at:new Date().toISOString(), ...payload};
  const history = touchHistory();
  history.messages = [...(history.messages || []), item].slice(-100);
  if (kind === "user" && history.title === "Новый чат") history.title = payload.text.slice(0, 70);
  saveHistory(); renderHistory(); renderMessage(item, true);
  return item;
}
function renderStoredMessages() {
  byId("messages").innerHTML = "";
  const messages = activeHistory()?.messages || [];
  if (!messages.length) renderWelcome();
  else messages.forEach((item) => renderMessage(item, false));
}
function renderWelcome() {
  byId("messages").innerHTML = `<article class="welcome">
    <span class="assistant-mark">AB</span><div><h2>Начните анализ</h2>
    <p>Задайте вопрос по балансам. Следующие реплики могут наследовать период, географию и другие сущности текущего диалога.</p>
    <div class="suggestions">
      <button type="button" data-suggest="Покажи поставки газа в Казань за май 2025">Поставки в Казань за май</button>
      <button type="button" data-suggest="Сравни поставки в Казань весной и летом 2025">Сравнить периоды</button>
    </div></div></article>`;
}
function renderMessage(item, scroll) {
  const node = document.createElement("article");
  node.className = `message ${item.kind}`; node.dataset.messageId = item.id;
  if (item.kind === "user") node.innerHTML = `<div class="message-meta">Пользователь · ${formatTime(item.at)}</div><div class="user-bubble">${escapeHtml(item.text)}</div>`;
  else if (item.kind === "error") node.innerHTML = `<div class="message-meta">AI Балансы · ${formatTime(item.at)}</div><div class="result-card error-card"><h2>${escapeHtml(item.title)}</h2><p>${escapeHtml(item.text)}</p>${item.requestId ? `<small>Код запроса: ${escapeHtml(item.requestId)}</small>` : ""}</div>`;
  else node.innerHTML = `<div class="message-meta">AI Балансы · ${formatTime(item.at)}</div>${resultHtml(item)}`;
  byId("messages").append(node);
  if (scroll) node.scrollIntoView({behavior:"smooth", block:"end"});
}
function resultHtml(item) {
  const result = item.result || {};
  const status = item.status || result.status || "ok";
  const summary = result.summary || {};
  const title = summary.title || result.title || (status === "no_data" ? "Данные не найдены" : status === "needs_clarification" ? "Нужно уточнение" : "Результат готов");
  const text = summary.text || summary.answer || summary.description || "";
  const bullets = Array.isArray(summary.bullets) ? `<ul>${summary.bullets.map((value) => `<li>${escapeHtml(value)}</li>`).join("")}</ul>` : "";
  const comparison = comparisonHtml(result.comparison);
  const table = tableHtml(result);
  const warnings = (result.warnings || []).length ? `<div class="warnings">${result.warnings.map((value) => `<p>${escapeHtml(typeof value === "string" ? value : value.message || JSON.stringify(value))}</p>`).join("")}</div>` : "";
  const clarification = clarificationHtml(item.context?.pending_clarification);
  return `<div class="result-card">
    <div class="result-heading"><div><span class="result-status ${escapeHtml(status)}">${escapeHtml(operationLabel(result.operation || status))}</span><h2>${escapeHtml(title)}</h2></div></div>
    ${text ? `<p class="summary-text">${escapeHtml(text)}</p>` : ""}${bullets}${comparison}${table}${warnings}${clarification}
    <div class="result-actions"><button type="button" data-copy="${escapeHtml(item.id)}">Копировать</button>${hasRows(result) ? `<button type="button" data-csv="${escapeHtml(item.id)}">CSV</button>` : ""}</div>
  </div>`;
}
function comparisonHtml(comparison) {
  if (!comparison) return "";
  return `<section class="comparison-grid">
    <div><small>Базовое значение</small><strong>${numberLabel(comparison.baseline_value)}</strong><span>${escapeHtml(comparison.unit || "")}</span></div>
    <div><small>Сравниваемое</small><strong>${numberLabel(comparison.target_value)}</strong><span>${escapeHtml(comparison.unit || "")}</span></div>
    <div><small>Изменение</small><strong>${numberLabel(comparison.delta)}</strong><span>${comparison.percent_change == null ? "" : `${numberLabel(comparison.percent_change)} %`}</span></div>
  </section>`;
}
function rowsFor(result) {
  if (Array.isArray(result.rows) && result.rows.length) return result.rows;
  if (Array.isArray(result.facts) && result.facts.length) return result.facts.map((fact) => ({Показатель:fact.label, Значение:fact.value, Единица:fact.unit}));
  return [];
}
function hasRows(result) { return rowsFor(result).length > 0; }
function tableHtml(result) {
  const rows = rowsFor(result); if (!rows.length) return "";
  const columns = [...new Set(rows.flatMap((row) => Object.keys(row)))].filter((key) => key !== "provenance");
  return `<section class="table-section"><div class="section-title">Основная таблица <span>${rows.length} ${rows.length === 1 ? "строка" : "строк"}</span></div>
    <div class="table-scroll"><table><thead><tr>${columns.map((key) => `<th>${escapeHtml(key)}</th>`).join("")}</tr></thead><tbody>
    ${rows.map((row) => `<tr>${columns.map((key) => `<td>${escapeHtml(formatCell(row[key], key))}</td>`).join("")}</tr>`).join("")}</tbody></table></div></section>`;
}
function formatCell(value, key) {
  if (value == null) return "—";
  if (typeof value === "object") return Array.isArray(value) ? value.join(", ") : JSON.stringify(value);
  if (/value|fact|delta|percent|объ.м|знач/i.test(key) && Number.isFinite(Number(value))) return numberLabel(value);
  return value;
}
function clarificationHtml(pending) {
  if (!pending?.questions?.length) return "";
  return `<section class="clarification"><h3>Уточните параметры</h3>${pending.questions.map((question, index) => `
    <div class="clarification-question"><p>${escapeHtml(question.question || "Выберите вариант")}</p>
    <div>${(question.options || []).map((option) => {
      const value = typeof option === "object" ? option.value ?? option.label : option;
      const label = typeof option === "object" ? option.label ?? option.value : option;
      return `<button type="button" data-clarify="${escapeHtml(value)}" data-question="${index}">${escapeHtml(label)}</button>`;
    }).join("")}</div></div>`).join("")}</section>`;
}

async function createSession() {
  if (state.busy) return;
  setStatus("создание сессии", "busy");
  const body = await api("/api/v2/chat/sessions", {method:"POST"});
  renderContext(body); touchHistory({title:"Новый чат", messages:[]}); renderStoredMessages();
  setStages(-1); setStatus("готов", "ready");
}
async function restoreSession(sessionId) {
  if (state.busy || sessionId === state.sessionId) return;
  try {
    setStatus("восстановление", "busy");
    const body = await api(`/api/v2/chat/sessions/${encodeURIComponent(sessionId)}`);
    renderContext(body); renderHistory(); renderStoredMessages(); setStatus("готов", "ready");
  } catch (error) {
    if (error.status === 404) { state.histories = state.histories.filter((item) => item.sessionId !== sessionId); saveHistory(); renderHistory(); }
    setStatus("ошибка", "error");
  }
}
async function send(message, clarification = null) {
  if (state.busy || !message.trim()) return;
  state.busy = true; byId("submitButton").disabled = true;
  if (!clarification) appendMessage("user", {text:message});
  setStatus("выполнение", "busy"); setStages(0);
  let stage = 0;
  const timer = setInterval(() => { stage = Math.min(stage + 1, 3); setStages(stage); }, 900);
  const traceId = requestId();
  try {
    const body = await api("/api/v2/chat", {method:"POST", body:JSON.stringify({session_id:state.sessionId, expected_revision:state.revision, message, execute_db:byId("executeDb").checked, clarification, request_id:traceId})});
    clearInterval(timer); renderContext(body); setStages(4);
    appendMessage("assistant", {status:body.status, result:body.result, context:body.context, requestId:body.request_id});
    setTimeout(() => setStages(5), 250); setStatus(body.status === "needs_clarification" ? "нужно уточнение" : body.status, body.status === "ok" ? "ready" : "warning");
  } catch (error) {
    clearInterval(timer); setStages(Math.max(stage, 1), true);
    if (error.status === 409) {
      await restoreCurrent();
      appendMessage("error", {title:"Контекст изменился", text:"Другой запрос успел изменить эту сессию. Контекст обновлён — повторите запрос.", requestId:traceId});
    } else appendMessage("error", {title:"Контекстный запрос не выполнен", text:humanError(error), requestId:traceId});
    setStatus("ошибка", "error");
  } finally {
    state.busy = false; byId("submitButton").disabled = false; byId("message").focus();
  }
}
async function restoreCurrent() {
  const body = await api(`/api/v2/chat/sessions/${encodeURIComponent(state.sessionId)}`); renderContext(body);
}
function humanError(error) {
  const messages = {
    interpretation_contract_invalid:"Модель вернула ответ, который не прошёл строгую проверку контракта. Запрос не исполнялся. Переформулируйте его или повторите попытку.",
    interpretation_binding_failed:"Не удалось однозначно связать формулировку с каноническими сущностями metadata. Уточните баланс, статью или географию.",
    native_planning_failed:"Такое сочетание сравнения, периодов или группировки пока не поддерживается безопасным планировщиком.",
    context_store_unavailable:"Хранилище контекста временно недоступно. Сессия не изменена.",
    session_not_found:"Сессия больше не существует. Создайте новый чат.",
  };
  if (error.status === 503) return "Backend или одно из его обязательных хранилищ временно недоступно.";
  return messages[error.code] || error.message || "Неизвестная ошибка backend.";
}

function resultText(item) {
  const result = item.result || {}; const summary = result.summary || {};
  const lines = [summary.title || result.title || "Результат", summary.text || summary.answer || ""];
  rowsFor(result).forEach((row) => lines.push(Object.entries(row).filter(([key]) => key !== "provenance").map(([key,value]) => `${key}: ${formatCell(value,key)}`).join("; ")));
  return lines.filter(Boolean).join("\n");
}
async function copyResult(id) {
  const item = activeHistory()?.messages?.find((entry) => entry.id === id); if (!item) return;
  await navigator.clipboard.writeText(resultText(item)); setStatus("скопировано", "ready");
}
function exportCsv(id) {
  const item = activeHistory()?.messages?.find((entry) => entry.id === id); const rows = rowsFor(item?.result || {}); if (!rows.length) return;
  const columns = [...new Set(rows.flatMap((row) => Object.keys(row)))].filter((key) => key !== "provenance");
  const quote = (value) => `"${String(value ?? "").replaceAll('"','""')}"`;
  const csv = "\uFEFF" + [columns.map(quote).join(";"), ...rows.map((row) => columns.map((key) => quote(formatCell(row[key],key))).join(";"))].join("\r\n");
  download(new Blob([csv], {type:"text/csv;charset=utf-8"}), `ai-balances-${Date.now()}.csv`);
}
function exportHistory() { download(new Blob([JSON.stringify(state.histories, null, 2)], {type:"application/json"}), `ai-balances-history-${new Date().toISOString().slice(0,10)}.json`); }
function download(blob, filename) { const link=document.createElement("a"); link.href=URL.createObjectURL(blob); link.download=filename; link.click(); setTimeout(()=>URL.revokeObjectURL(link.href),500); }

byId("chatForm").addEventListener("submit", (event) => { event.preventDefault(); const message=byId("message").value.trim(); if (message) { byId("message").value=""; send(message); } });
byId("message").addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); byId("chatForm").requestSubmit(); } });
byId("newChat").addEventListener("click", () => createSession().catch(showStartupError));
byId("exportHistory").addEventListener("click", exportHistory);
byId("clearHistory").addEventListener("click", () => { state.histories=[]; saveHistory(); renderHistory(); renderStoredMessages(); });
byId("historyList").addEventListener("click", async (event) => {
  const remove=event.target.closest("[data-delete]");
  if (remove) { const id=remove.dataset.delete; try { await api(`/api/v2/chat/sessions/${encodeURIComponent(id)}`, {method:"DELETE"}); } catch {} state.histories=state.histories.filter((item)=>item.sessionId!==id); saveHistory(); renderHistory(); if(id===state.sessionId) createSession().catch(showStartupError); return; }
  const open=event.target.closest("[data-session]"); if(open) restoreSession(open.dataset.session);
});
byId("messages").addEventListener("click", (event) => {
  const suggest=event.target.closest("[data-suggest]"); if(suggest) { byId("message").value=suggest.dataset.suggest; byId("chatForm").requestSubmit(); return; }
  const copy=event.target.closest("[data-copy]"); if(copy) copyResult(copy.dataset.copy); const csv=event.target.closest("[data-csv]"); if(csv) exportCsv(csv.dataset.csv);
  const clarify=event.target.closest("[data-clarify]"); if(clarify) send(clarify.dataset.clarify, {answer:clarify.dataset.clarify});
});
function showStartupError(error) { setStatus("backend недоступен", "error"); byId("messages").innerHTML=""; appendMessage("error", {title:"Не удалось создать сессию", text:humanError(error)}); }

renderHistory();
(async () => {
  const candidate=state.histories[0]?.sessionId;
  if(candidate) { try { await restoreSession(candidate); return; } catch {} }
  await createSession();
})().catch(showStartupError);
