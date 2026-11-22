import { state, icon, endpoint, call, isOpen, nameOf, statusBadge, dates, heading, button } from "./liff-shared.js?v=19";
import { aiView, settleView, expenseList, expenseRows, balanceBreakdown } from "./liff-ledger.js?v=19";
import { tripView, rateRow, invite } from "./liff-settings.js?v=19";
import { recordInfo, expenseFields, wireExpenseForm, readExpense, transferFields } from "./liff-fields.js?v=19";
import { $, api, clearUndo, esc, money, notice, storage } from "./common.js?v=19";

const TOKEN_KEY = "trip-split:token";
const LINE_USER_KEY = "trip-split:line-user";
const views = ["manual", "expenses", "ai", "settle", "trip"];
// LINE 訊息與舊書籤裡的分頁名稱。
const aliases = { overview: ["settle", "settle"], stats: ["settle", "stats"], members: ["trip"], settings: ["trip"] };
const app = $("#app");
const dialog = $("#dialog");
const tabs = $("#tabs");
function updateUrl(update, hash) {
  const url = new URL(location.href);
  update(url.searchParams);
  if (hash !== undefined) url.hash = hash;
  history.replaceState(null, "", url.pathname + url.search + url.hash);
}

function setPage(markup) {
  app.innerHTML = markup;
  app.setAttribute("aria-busy", "false");
  tabs.hidden = !state.trip;
  document.body.classList.toggle("in-trip", Boolean(state.trip));
  $("#refresh").hidden = !state.listing;
  $("#trip-title").textContent = state.trip ? state.trip.name : "旅伴分帳";
  $("#trip-status").innerHTML = state.trip ? statusBadge(state.trip) : "";
  $("#home").setAttribute("aria-label", state.trip ? "切換帳本" : "我的帳本");
  for (const tab of tabs.querySelectorAll("[data-tab]")) {
    if (tab.dataset.tab === state.tab) tab.setAttribute("aria-current", "page");
    else tab.removeAttribute("aria-current");
    tab.disabled = Boolean(state.trip) && !isOpen() && ["manual", "ai"].includes(tab.dataset.tab);
  }
}

function showError(error) {
  if (error.status === 401) {
    storage.remove(TOKEN_KEY);
    state.token = "";
    state.listing = state.trip = null;
    setPage(heading("LINE 登入", "請重新登入", error.message) + button("重新開啟", "retry", "primary"));
    return;
  }
  if (state.listing) notice(error.message);
  else setPage(heading("旅程帳本", "無法開啟帳本", error.message) + button("重試", "retry", "primary"));
}

function listView() {
  const groups = [["進行中的帳本", "open"], ["已結束的帳本", "closed"]];
  const cards = groups.map(([label, key]) => {
    const trips = state.listing.trips.filter((trip) => trip.status === key);
    if (!trips.length) return "";
    return `<section class="section"><div class="section-head"><h2>${label}</h2></div><div class="stack">${trips.map((trip) => `
      <button class="card row row-button" type="button" data-action="open" data-id="${trip.id}">
        <span><strong>${esc(trip.name)}</strong><span class="line-item trip-dates">${esc(dates(trip))} · ${esc(trip.settle_currency)}</span></span>
        <span class="actions">${statusBadge(trip)}${icon("next")}</span>
      </button>`).join("")}</div></section>`;
  }).join("");
  return heading("旅行一起記", "我的帳本", "查看旅程花費，和旅伴把帳分清楚。") + (cards || `<div class="card empty"><h2>還沒有帳本</h2><p>在 LINE 群組傳「開帳本 旅程名稱」，或請旅伴分享邀請連結。</p></div>`);
}

function manualView() {
  const kinds = [["expense", "支出"], ["refund", "退款"], ["transfer", "還款"]];
  const kind = state.manualKind;
  const fields = kind === "transfer" ? transferFields() : expenseFields(null, kind);
  return `<div class="segmented" role="tablist" aria-label="紀錄類型">${kinds.map(([key, label]) =>
    `<button type="button" role="tab" data-action="manual-kind" data-id="${key}" aria-selected="${kind === key}">${label}</button>`).join("")}</div>
    <form id="manual-form" class="card form section">${fields}<p class="error" role="alert" hidden></p>
      <div class="actions"><button type="submit" class="primary">${kind === "transfer" ? "記錄還款" : "儲存"}</button></div></form>`;
}

function render() {
  if (!state.listing) return;
  if (!state.trip) { setPage(listView()); return; }
  const view = { manual: manualView, expenses: expenseList, ai: aiView, settle: settleView, trip: tripView }[state.tab];
  setPage((state.trip.group_warning && ["manual", "ai", "trip"].includes(state.tab) ? `<p class="warn">${esc(state.trip.group_warning)}</p>` : "") + view());
  if (state.tab === "manual") wireManualForm();
}

async function loadList() {
  state.listing = await call("/api/trips");
  render();
}

let navigation = 0;
async function openTrip(id, tab = null, preloaded = null) {
  const request = ++navigation;
  const trip = preloaded || await call(`/api/trips/${id}`);
  if (request !== navigation) return;
  const sameTrip = new URLSearchParams(location.search).get("trip") === String(id);
  if (!sameTrip) state.ai = { text: "", busy: "", error: "", result: null };
  state.trip = trip;
  state.stats = null;
  state.statsError = "";
  state.statsCategory = "";
  state.settlementDisplay = null;
  state.displayRequest = null;
  updateUrl((params) => { params.set("trip", id); params.delete("join"); }, sameTrip ? undefined : "");
  await switchTab(tab || (trip.status === "open" ? "ai" : "settle"));
}

async function loadStats() {
  const trip = state.trip;
  state.statsError = "";
  try {
    const data = await call(`/api/trips/${trip.id}/stats`);
    if (state.trip === trip) state.stats = data;
  } catch (error) {
    if (state.trip !== trip) return;
    if (error.status === 401) { showError(error); return; }
    state.statsError = error.message;
  }
  if (state.trip === trip && state.tab === "settle" && state.settleView === "stats") render();
}

async function switchTab(tab) {
  if (aliases[tab]) [tab, state.settleView = state.settleView] = aliases[tab];
  if (!views.includes(tab)) tab = "ai";
  if (!isOpen() && ["manual", "ai"].includes(tab)) tab = "settle";
  state.tab = tab;
  updateUrl((params) => params.set("tab", tab === "settle" && state.settleView === "stats" ? "stats" : tab));
  render();
  if (tab === "settle" && state.settleView === "stats" && !state.stats) await loadStats();
}

async function changeSettlementCurrency(select) {
  const trip = state.trip;
  const request = Symbol();
  state.displayRequest = request;
  if (select.value === trip.settle_currency) {
    state.settlementDisplay = null;
    render();
    $("#settlement-currency").focus();
    return;
  }
  select.disabled = true;
  try {
    const data = await call(endpoint(`/settlement-display?${new URLSearchParams({ currency: select.value })}`));
    if (state.displayRequest !== request) return;
    if (data.settlement_version !== trip.settlement_version) throw new Error("結算已更新，請重新整理帳本");
    state.settlementDisplay = data;
  } catch (error) {
    if (state.displayRequest === request) showError(error);
  } finally {
    if (state.displayRequest === request && state.trip === trip && state.tab === "settle") {
      render();
      $("#settlement-currency").focus();
    }
  }
}

function openForm(title, fields, save, { submitLabel = "儲存", extra = "", danger = false,
  refresh = () => openTrip(state.trip.id, state.tab) } = {}) {
  dialog.innerHTML = `<div class="dialog-head"><h2 id="dialog-title">${esc(title)}</h2><button type="button" class="plain" data-action="dismiss" aria-label="關閉">${icon("close")}</button></div>
    <form class="form">${fields}<p class="error" role="alert" hidden></p><div class="actions"><button type="submit" class="${danger ? "danger" : "primary"}">${submitLabel}</button>${extra}</div></form>`;
  const form = $("form", dialog);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = $("button[type=submit]", form);
    const error = $(".error", form);
    submit.disabled = true;
    error.hidden = true;
    try {
      const message = await save(new FormData(form), form);
      dialog.close();
      notice(message);
      await refresh().catch(showError);
    } catch (err) {
      if (err.status === 401) { dialog.close(); showError(err); }
      else { error.textContent = err.message; error.hidden = false; }
    } finally { submit.disabled = false; }
  });
  dialog.showModal();
  return form;
}

function confirmReopen() {
  const tripId = state.trip.id;
  openForm("重新開啟帳本", "<p>重新開啟後，可以繼續記帳與邀請旅伴。</p>", async () => {
    await call(`/api/trips/${tripId}/reopen`, { method: "POST" });
    return "帳本已重新開啟";
  }, { submitLabel: "確認重新開啟", extra: button("取消", "dismiss") });
}

function confirmDeletion(memberId = null) {
  const wholeTrip = memberId === null;
  const title = wholeTrip ? "刪除帳本" : "刪除成員";
  const name = wholeTrip ? state.trip.name : nameOf(memberId);
  const detail = wholeTrip ? "所有成員之後都無法在 LINE 或網頁查閱這本帳。" : "此成員會從帳本名單移除。";
  const path = endpoint(wholeTrip ? "" : `/members/${memberId}`);
  openForm(title, `<p>確定刪除「${esc(name)}」？${detail}</p>`, async () => {
    await call(path, { method: "DELETE" });
    return wholeTrip ? "帳本已刪除" : "成員已刪除";
  }, { submitLabel: "確認刪除", extra: button("取消", "dismiss"), danger: true, refresh: wholeTrip ? async () => {
    state.trip = null;
    updateUrl((params) => { params.delete("trip"); params.delete("tab"); }, "");
    await loadList();
  } : undefined });
}

function paymentNoteForm() {
  const member = state.trip.members.find(({ id }) => id === state.listing.member_id);
  openForm("收款方式", `<label>收款平台與末碼<input name="note" maxlength="100" value="${esc(member.payment_note)}"
    placeholder="例如 LINE Pay 末碼 1234"></label>
    <p class="small muted">只填平台名稱與最多四位末碼。帳本成員及 LINE 群組成員看得到結算卡片上的收款方式。</p>`,
  async (data) => {
    await call(endpoint(`/members/${member.id}/payment-note`), { method: "PUT", body: { payment_note: data.get("note") } });
    return "收款方式已儲存";
  });
}

function markPayment(index) {
  const trip = state.trip;
  const { from_id, to_id, amount } = trip.settlement.payments[index];
  const body = { from_id, to_id, amount, currency: trip.settle_currency, settlement_version: trip.settlement_version };
  const receiving = state.listing.member_id === to_id;
  const sentence = receiving ? `已收到 ${esc(nameOf(from_id))} 付的` : `${esc(nameOf(from_id))} 已付給 ${esc(nameOf(to_id))}`;
  openForm(receiving ? "確認已收到" : "標記已付", `<p>確認${sentence} <strong class="num">${esc(money(amount, body.currency))}</strong>？</p>`,
    async () => {
      await call(endpoint("/transfers"), { method: "POST", body });
      return "已記還款";
    }, { submitLabel: receiving ? "確認已收到" : "確認已付", extra: button("取消", "dismiss") });
}

function expenseForm(entry = null, { draftIndex = null } = {}) {
  const draft = draftIndex !== null;
  const form = openForm(draft ? "修改草稿" : entry ? "編輯紀錄" : "新增紀錄", expenseFields(entry), async (_, formNode) => {
    const body = readExpense(formNode);
    if (draft) {
      state.ai.result.expenses[draftIndex] = body;
      return "草稿已更新";
    }
    await call(endpoint(`/expenses${entry ? `/${entry.id}` : ""}`), { method: entry ? "PUT" : "POST", body });
    const label = body.kind === "refund" ? "退款" : "支出";
    return entry ? `${label}已更新` : `${label}已新增`;
  }, { extra: entry && !draft ? button(entry.kind === "refund" ? "刪除退款" : "刪除支出", "delete-expense", "danger", "", entry.id) : "",
    refresh: draft ? async () => render() : undefined });
  wireExpenseForm(form, entry);
}

async function saveTransfer(formNode) {
  const body = Object.fromEntries(new FormData(formNode));
  body.from_id = Number(body.from_id); body.to_id = Number(body.to_id); body.currency = body.currency.toUpperCase();
  if (body.from_id === body.to_id) throw new Error("請選擇兩位不同的成員。");
  await call(endpoint("/transfers"), { method: "POST", body });
  return "還款已記錄";
}

function wireManualForm() {
  const form = $("#manual-form");
  if (state.manualKind !== "transfer") wireExpenseForm(form);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = $("button[type=submit]", form);
    const error = $(".error", form);
    submit.disabled = true;
    error.hidden = true;
    try {
      let message;
      if (state.manualKind === "transfer") message = await saveTransfer(form);
      else {
        const body = readExpense(form);
        if (!form.dataset.categoryChosen) delete body.category;
        await call(endpoint("/expenses"), { method: "POST", body });
        message = body.kind === "refund" ? "退款已新增" : "支出已新增";
      }
      notice(message);
      clearUndo();
      const next = state.manualKind === "transfer" ? "settle" : "manual";
      state.manualKind = "expense";
      await openTrip(state.trip.id, next);
      window.scrollTo(0, 0);
    } catch (err) {
      if (err.status === 401) showError(err);
      else { error.textContent = err.message; error.hidden = false; }
    } finally { submit.disabled = false; }
  });
}

function transferDialog(row) {
  const trip = state.trip;
  dialog.innerHTML = `<div class="dialog-head"><h2 id="dialog-title">還款紀錄</h2><button type="button" class="plain" data-action="dismiss" aria-label="關閉">${icon("close")}</button></div>
    <div class="stack"><p><strong>${esc(nameOf(row.from_id))} → ${esc(nameOf(row.to_id))}</strong></p>
    <p class="num">${esc(money(row.amount, row.currency))}${row.amount_settle != null && row.currency !== trip.settle_currency ? `（約 ${esc(money(row.amount_settle, trip.settle_currency))}）` : ""}</p>
    ${recordInfo(row)}
    <div class="actions">${button("刪除還款", "delete-transfer", "danger", "", row.id)}${button("關閉", "dismiss", "plain")}</div></div>`;
  dialog.showModal();
}

async function aiSubmit(text = null, photo = null) {
  const ai = state.ai;
  const trip = state.trip;
  ai.error = "";
  ai.busy = photo ? "AI 正在讀收據…" : "AI 解析中…";
  render();
  try {
    let result;
    if (photo) {
      const blob = await shrinkPhoto(photo);
      const response = await fetch(endpoint("/ai/photo"), { method: "POST", body: blob,
        headers: { Authorization: `Bearer ${state.token}`, "Content-Type": blob.type || "image/jpeg" } });
      if (!response.ok) {
        const detail = (await response.json().catch(() => ({}))).detail;
        throw Object.assign(new Error(detail || "收據讀取失敗，請稍後重試。"), { status: response.status });
      }
      result = await response.json();
    } else {
      result = await call(endpoint("/ai/parse"), { method: "POST", body: { text } });
    }
    if (state.trip !== trip) return;
    ai.result = result;
    ai.text = text || "";
  } catch (error) {
    if (error.status === 401) { showError(error); return; }
    ai.error = error.message;
  } finally {
    ai.busy = "";
    if (state.trip === trip && state.tab === "ai") render();
    if (refreshPending) refreshOnReturn();
  }
}

// 手機照片常有 5 MB 以上；縮到長邊 1600px 再上傳，讀收據仍清楚。
async function shrinkPhoto(file) {
  try {
    const bitmap = await createImageBitmap(file);
    const scale = Math.min(1, 1600 / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bitmap.width * scale);
    canvas.height = Math.round(bitmap.height * scale);
    canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.85));
    return blob || file;
  } catch { return file; }
}

async function confirmDrafts() {
  const ai = state.ai;
  const body = { source: ai.result.source, text: ai.result.text, expenses: ai.result.expenses };
  await call(endpoint("/expenses/bulk"), { method: "POST", body });
  const count = ai.result.expenses.length;
  state.ai = { text: "", busy: "", error: "", result: null };
  notice(`已入帳 ${count} 筆`);
  clearUndo();
  await openTrip(state.trip.id, "ai");
}

function memberForm(member = null) {
  const canToggle = member && state.trip.can_manage && !member.organizer;
  openForm(member ? "編輯成員" : "新增成員", `<label>暱稱<input name="nickname" maxlength="80" ${member ? "" : "required"} value="${esc(member?.nickname)}"></label>
    <label>分攤權重<input name="weight" type="number" min="0.0001" max="100" step="0.0001" required value="${esc(member?.weight || 1)}"></label>
    ${canToggle ? `<label class="check"><input type="checkbox" name="active" ${member.active ? "checked" : ""}>參與新支出的分攤</label>` : ""}
    <p class="muted small">權重影響之後的支出，舊帳仍保留原本分攤。</p>`, async (data) => {
    const body = { nickname: data.get("nickname").trim() || null, weight: data.get("weight") };
    if (member) body.active = canToggle ? data.has("active") : member.active;
    await call(endpoint(`/members${member ? `/${member.id}` : ""}`), { method: member ? "PUT" : "POST", body });
    return member ? "成員已更新" : "成員已新增";
  });
}

async function saveSettings(form) {
  const body = Object.fromEntries(new FormData(form));
  const notify = form.querySelector("[name=notify_web_changes]");
  if (notify) body.notify_web_changes = notify.checked;
  body.default_payer_id = Number(body.default_payer_id || 0);
  body.start_date ||= null;
  body.end_date ||= null;
  body.default_currency = body.default_currency.toUpperCase();
  body.settle_currency = body.settle_currency.toUpperCase();
  const rates = {};
  for (const row of form.querySelectorAll(".rate-row")) {
    const code = $("[data-code]", row).value.toUpperCase();
    if (code in rates) throw new Error(`${code} 的匯率重複了。`);
    rates[code] = $("[data-rate]", row).value;
  }
  body.fixed_rates = rates;
  await call(endpoint("/settings"), { method: "PUT", body });
  notice("設定已儲存");
  await openTrip(state.trip.id, state.tab).catch(showError);
}

async function exportCsv() {
  const response = await call(endpoint("/export.csv"), { raw: true });
  const url = URL.createObjectURL(await response.blob());
  dialog.innerHTML = `<div class="dialog-head"><h2 id="dialog-title">匯出帳本</h2>${button("關閉", "dismiss", "plain")}</div>
    <p class="muted small">CSV 包含支出及還款紀錄，可用 Excel 開啟。</p>
    <a class="primary" href="${esc(url)}" download="${esc(state.trip.name)}-帳本.csv">${icon("download")}下載 CSV</a>`;
  dialog.addEventListener("close", () => setTimeout(() => URL.revokeObjectURL(url), 1000), { once: true });
  dialog.showModal();
}

function lineLogin() {
  const query = new URLSearchParams(location.search);
  const redirect = new URL("/liff/", location.origin);
  for (const key of ["trip", "join", "tab"]) if (query.has(key)) redirect.searchParams.set(key, query.get(key));
  redirect.hash = location.hash;
  window.liff.login({ redirectUri: redirect.href });
}

function inlineConfig() {
  try { return JSON.parse($("#liff-config")?.textContent || "null"); } catch { return null; }
}

function tokenFresh(token) {
  try {
    const part = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(atob(part)).exp * 1000 > Date.now() + 60_000;
  } catch { return false; }
}

// 回傳 false 表示已改顯示登入畫面。
async function signIn({ reuse = true } = {}) {
  const lineUser = window.liff.getDecodedIDToken()?.sub || "";
  const saved = storage.get(TOKEN_KEY);
  // 同一個 LINE 帳號、憑證還沒過期，就不必每次打開都請伺服器向 LINE 驗證。
  if (reuse && lineUser && saved && storage.get(LINE_USER_KEY) === lineUser && tokenFresh(saved)) {
    state.token = saved;
    return true;
  }
  const idToken = window.liff.getIDToken();
  if (!idToken) throw new Error("無法取得 LINE 登入身分，請重新開啟帳本。");
  try {
    state.token = (await api("/api/auth/line", { method: "POST", body: { id_token: idToken } })).token;
  } catch (error) {
    // 外部瀏覽器會沿用已過期的 ID token，要先登出才拿得到新的。LINE 內開啟時每次都會換新。
    if (error.status !== 401 || window.liff.isInClient()) throw error;
    window.liff.logout();
    storage.remove(TOKEN_KEY);
    setPage(heading("LINE 登入", "請重新登入", "LINE 登入已過期。") + button("使用 LINE 登入", "login", "primary"));
    return false;
  }
  storage.set(TOKEN_KEY, state.token);
  storage.set(LINE_USER_KEY, lineUser);
  return true;
}

async function start(tripId, tab) {
  // 帳本列表和帳本內容同時讀，不必一個等一個。帳本打不開時仍顯示列表。
  const tripRequest = tripId ? call(`/api/trips/${tripId}`).catch((error) => error) : null;
  state.listing = await call("/api/trips");
  const trip = await tripRequest;
  if (trip instanceof Error) {
    if (trip.status === 401) throw trip;
    render();
    notice(trip.message);
  } else if (trip) await openTrip(tripId, tab, trip);
  else render();
}

async function boot() {
  try {
    state.config = inlineConfig() || await api("/api/liff/config");
    if (!state.config.liff_id) {
      setPage(heading("LINE 登入", "帳本登入準備中", "請稍後從 LINE 重新開啟帳本。"));
      return;
    }
    if (!window.liff) throw new Error("LINE 登入元件暫時無法載入，請重新開啟帳本。");
    await window.liff.init({ liffId: state.config.liff_id });
    if (!window.liff.isLoggedIn()) {
      storage.remove(TOKEN_KEY);
      setPage(heading("LINE 登入", "旅程帳本", "用 LINE 登入後即可查看帳本。") + button("使用 LINE 登入", "login", "primary"));
      return;
    }
    if (!await signIn()) return;
    const params = new URLSearchParams(location.search);
    let tripId = params.get("trip");
    // 從 LINE 打開時一律由 AI 記帳開始；重新整理會帶著網址上的分頁。
    let tab = params.get("tab");
    if (params.has("join")) {
      tab = null;
      try {
        tripId = String((await call("/api/trips/join", { method: "POST", body: { code: params.get("join") } })).trip_id);
        notice("已加入帳本");
      } finally { updateUrl((query) => query.delete("join")); }
    }
    if (!(tripId && /^\d+$/.test(tripId))) tripId = null;
    try {
      await start(tripId, tab);
    } catch (error) {
      // 沿用的憑證被拒時（例如伺服器換了簽章金鑰），重新向 LINE 驗證一次。
      if (error.status !== 401) throw error;
      if (!await signIn({ reuse: false })) return;
      await start(tripId, tab);
    }
    if ("serviceWorker" in navigator) navigator.serviceWorker.register("/liff/sw.js").catch(() => {});
  } catch (error) { showError(error); }
}

let refreshPending = false;
let refreshing = false;
async function refreshOnReturn() {
  if (!state.listing || document.hidden || refreshing) return;
  if (dialog.open || state.ai.busy || $("form[data-dirty]")) {
    refreshPending = true;
    return;
  }
  refreshPending = false;
  refreshing = true;
  const trip = state.trip, version = navigation;
  try {
    const data = await call(trip ? endpoint() : "/api/trips");
    if (state.trip !== trip || version !== navigation) return;
    if (dialog.open || state.ai.busy || $("form[data-dirty]")) { refreshPending = true; return; }
    if (trip) await openTrip(trip.id, state.tab, data);
    else { state.listing = data; render(); }
  } catch (err) { if (state.trip === trip) showError(err); }
  finally { refreshing = false; }
}

document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshOnReturn(); });
window.addEventListener("pageshow", event => { if (event.persisted) refreshOnReturn(); });
dialog.addEventListener("close", () => { if (refreshPending) refreshOnReturn(); });
app.addEventListener("input", ({target}) => {
  const form = target.closest("#manual-form, #settings-form");
  if (form) form.dataset.dirty = "1";
});

document.addEventListener("click", async (event) => {
  const target = event.target.closest("button[data-action], button[data-tab]");
  if (!target || target.disabled) return;
  const { action, tab, id } = target.dataset;
  if (tab) {
    clearUndo();
    await switchTab(tab);
    if (refreshPending) await refreshOnReturn();
    window.scrollTo(0, 0);
    return;
  }
  target.disabled = true;
  try {
    if (action === "retry") location.reload();
    else if (action === "login") lineLogin();
    else if (action === "dismiss") dialog.close();
    else if (action === "list" && state.listing) {
      navigation++;
      state.trip = null;
      updateUrl((params) => { params.delete("trip"); params.delete("tab"); }, "");
      await loadList();
    } else if (action === "refresh") await (state.trip ? openTrip(state.trip.id, state.tab) : loadList());
    else if (action === "open") await openTrip(id);
    else if (action === "retry-stats") { state.statsError = ""; render(); await loadStats(); }
    else if (action === "filter-category") {
      updateUrl(() => {}, new URLSearchParams({ category: id }).toString());
      await switchTab("expenses");
      window.scrollTo(0, 0);
    } else if (action === "clear-filters") { updateUrl(() => {}, ""); render(); }
    else if (action === "add-expense") { state.manualKind = "expense"; await switchTab("manual"); }
    else if (action === "edit-expense") expenseForm(state.trip.expenses.find((entry) => entry.id === Number(id)));
    else if (action === "edit-transfer") transferDialog(state.trip.transfers.find((row) => row.id === Number(id)));
    else if (action === "transfer") { state.manualKind = "transfer"; await switchTab("manual"); window.scrollTo(0, 0); }
    else if (action === "manual-kind") { state.manualKind = id; render(); }
    else if (action === "settle-view") {
      state.settleView = id;
      updateUrl((params) => params.set("tab", id === "stats" ? "stats" : "settle"));
      render();
      if (id === "stats" && !state.stats) await loadStats();
    } else if (action === "goto-expenses") { await switchTab("expenses"); window.scrollTo(0, 0); }
    else if (action === "ai-photo") $("#ai-photo").click();
    else if (action === "draft-edit") expenseForm(state.ai.result.expenses[Number(id)], { draftIndex: Number(id) });
    else if (action === "draft-remove") {
      state.ai.result.expenses.splice(Number(id), 1);
      if (!state.ai.result.expenses.length) state.ai.result = null;
      render();
    } else if (action === "draft-cancel") { state.ai.result = null; render(); }
    else if (action === "draft-confirm") await confirmDrafts();
    else if (action === "mark-payment") markPayment(Number(id));
    else if (action === "edit-member") memberForm(state.trip.members.find((member) => member.id === Number(id)));
    else if (action === "edit-payment-note") paymentNoteForm();
    else if (action === "add-rate") {
      $("#rates").insertAdjacentHTML("beforeend", rateRow());
      $("#rates").lastElementChild.querySelector("input").focus();
      $("#settings-form").dataset.dirty = "1";
    } else if (action === "remove-rate") {
      target.closest(".rate-row").remove();
      $("#settings-form").dataset.dirty = "1";
    } else if (action === "add-member") memberForm();
    else if (action === "reopen-trip") confirmReopen();
    else if (action === "delete-member") confirmDeletion(Number(id));
    else if (action === "delete-trip") confirmDeletion();
    else if (action === "invite") await invite();
    else if (action === "export") await exportCsv();
    else if (action === "delete-expense" || action === "delete-transfer") {
      if (!window.confirm(action === "delete-expense" ? "刪除這筆紀錄？" : "刪除這筆還款？")) return;
      await call(endpoint(`/${action === "delete-expense" ? "expenses" : "transfers"}/${id}`), { method: "DELETE" });
      if (dialog.open) dialog.close();
      notice("紀錄已刪除");
      await openTrip(state.trip.id, state.tab);
    } else if (action === "close-trip" && window.confirm("結束這本帳？結束後可查閱明細與結算。")) {
      await call(endpoint("/close"), { method: "POST" });
      notice("帳本已結束");
      await openTrip(state.trip.id);
    }
  } catch (error) { showError(error); }
  finally { target.disabled = false; }
});

app.addEventListener("toggle", ({ target }) => {
  if (!target.matches("details[data-member-id]") || !target.open) return;
  const content = $(".balance-breakdown", target);
  if (!content.hasChildNodes()) content.innerHTML = balanceBreakdown(Number(target.dataset.memberId));
}, true);

app.addEventListener("change", ({ target }) => {
  if (target.id === "ai-photo" && target.files?.[0]) { aiSubmit(null, target.files[0]); return; }
  if (target.closest("#expense-filters") && target.name !== "q") updateExpenseFilter(target);
  if (target.id === "settlement-currency") changeSettlementCurrency(target);
  if (target.id !== "stats-category") return;
  state.statsCategory = target.value;
  render();
  $("#stats-category").focus();
});

function updateExpenseFilter(target) {
  const filters = new URLSearchParams(location.hash.slice(1));
  const value = target.type === "checkbox" ? (target.checked ? "1" : "") : target.value;
  if (value) filters.set(target.name, value);
  else filters.delete(target.name);
  updateUrl(() => {}, filters.toString());
  $("#expense-results").innerHTML = expenseRows();
}

app.addEventListener("input", ({ target }) => {
  if (target.id === "ai-text") state.ai.text = target.value;
  if (target.matches("#expense-filters [name=q]")) updateExpenseFilter(target);
});

document.addEventListener("submit", async (event) => {
  if (event.target.id === "ai-form") {
    event.preventDefault();
    const text = $("#ai-text").value.trim();
    if (text && !state.ai.busy) await aiSubmit(text);
    else if (!text) { state.ai.error = "請輸入要記的支出，例如「晚餐 3000」。"; render(); }
    return;
  }
  if (event.target.id !== "settings-form") return;
  event.preventDefault();
  const form = event.target;
  const submit = $("[type=submit]", form);
  const error = $(".error", form);
  submit.disabled = true;
  error.hidden = true;
  try { await saveSettings(form); }
  catch (err) {
    if (err.status === 401) showError(err);
    else { error.textContent = err.message; error.hidden = false; }
  } finally { submit.disabled = false; }
});

boot();
