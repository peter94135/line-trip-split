import { $, api, clearUndo, esc, notice, storage } from "./common.js?v=17";

const TOKEN_KEY = "trip-split:token";
const LINE_USER_KEY = "trip-split:line-user";
const views = ["manual", "expenses", "ai", "settle", "trip"];
// LINE 訊息與舊書籤裡的分頁名稱。
const aliases = { overview: ["settle", "settle"], stats: ["settle", "stats"], members: ["trip"], settings: ["trip"] };
const app = $("#app");
const dialog = $("#dialog");
const tabs = $("#tabs");
const state = { token: "", config: null, listing: null, trip: null, tab: "ai", settleView: "settle", manualKind: "expense",
  stats: null, statsError: "", statsCategory: "", settlementDisplay: null, displayRequest: null,
  ai: { text: "", busy: "", error: "", result: null } };
const icon = (name) => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
const endpoint = (suffix = "") => `/api/trips/${state.trip.id}${suffix}`;
const call = (path, options = {}) => api(path, { ...options, token: state.token });
const isOpen = () => state.trip.status === "open";
const nameOf = (id) => state.trip.members.find((member) => member.id === id)?.name || "成員";
const money = (value, currency) => `${currency} ${new Intl.NumberFormat("zh-TW", { maximumFractionDigits: 2 }).format(value)}`;
const statusBadge = (trip) => `<span class="badge ${trip.status === "closed" ? "closed" : ""}">${trip.status === "open" ? "進行中" : "已結束"}</span>`;
const dates = (trip) => [trip.start_date, trip.end_date].filter(Boolean).join(" ～ ") || "日期未設定";
const heading = (eyebrow, title, description = "") => `<div class="intro"><p class="eyebrow">${eyebrow}</p><h1>${esc(title)}</h1>${description ? `<p class="muted">${esc(description)}</p>` : ""}</div>`;
const button = (label, action, style = "secondary", symbol = "", id = "") => `<button type="button" class="${style}" data-action="${action}" data-id="${id}">${symbol ? icon(symbol) : ""}${label}</button>`;
const empty = (text) => `<div class="card empty">${esc(text)}</div>`;
const categoryLabel = (category) => `<span class="category-label" data-category="${esc(category)}">
  <span class="category-swatch" aria-hidden="true"></span><span>${esc(category)}</span></span>`;

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
        <span><strong>${esc(trip.name)}</strong><span class="line-item">${esc(dates(trip))} · ${esc(trip.settle_currency)}</span></span>
        <span class="actions">${statusBadge(trip)}${icon("next")}</span>
      </button>`).join("")}</div></section>`;
  }).join("");
  return heading("旅行一起記", "我的帳本", "查看旅程花費，和旅伴把帳分清楚。") + (cards || `<div class="card empty"><h2>還沒有帳本</h2><p>在 LINE 群組傳「開帳本 旅程名稱」，或請旅伴分享邀請連結。</p></div>`);
}

function settlementPanel() {
  const trip = state.trip;
  const { settlement: result, members } = trip;
  const missing = result.missing_expense_ids.length + result.missing_transfer_ids.length;
  const people = members.map((member) => `<details class="list-row balance-person" data-member-id="${member.id}">
    <summary><span><strong>${esc(member.name)}</strong>${member.active ? "" : '<span class="line-item">已停用</span>'}</span>
      <span class="amount-right small"><span class="num">結餘 <strong>${esc(money(result.balances[member.id] ?? 0, trip.settle_currency))}</strong></span>
      <span class="line-item">已付 ${esc(money(result.paid[member.id] ?? 0, trip.settle_currency))} · 分攤 ${esc(money(result.owed[member.id] ?? 0, trip.settle_currency))}</span></span>
    </summary><div class="balance-breakdown"></div></details>`).join("");
  const viewer = state.listing.member_id;
  const display = state.settlementDisplay;
  const viewerIsOwner = members.some(({ id, organizer }) => id === viewer && organizer);
  const payments = result.payments.map(({ from_id, to_id, amount }, index) => {
    const note = members.find(({ id }) => id === to_id)?.payment_note;
    return `<div class="row list-row"><div><span class="payment">${esc(nameOf(from_id))}${icon("arrow")}${esc(nameOf(to_id))}</span>
      ${note ? `<span class="line-item">收款方式 ${esc(note)}</span>` : ""}</div>
      <div class="amount-right"><strong class="num">${display ? "約 " : ""}${esc(money(display?.amounts[index] ?? amount, display?.currency ?? trip.settle_currency))}</strong>
      ${viewer === from_id || viewerIsOwner ? `<div>${button("標記已付", "mark-payment", "plain", "", index)}</div>` : ""}</div></div>`;
  }).join("");
  return `<section class="hero"><p class="eyebrow">總花費</p><strong class="hero-amount">${esc(money(result.total, trip.settle_currency))}</strong>
    <div class="hero-foot"><span>${trip.expenses.length} 筆支出</span><span>${members.length} 位成員</span></div></section>
    <div class="card section"><div class="section-head"><h2>誰該給誰</h2>${isOpen() && members.length > 1 ? button("還款／交公費", "transfer", "secondary") : ""}</div>
    ${trip.display_currencies.length > 1 ? `<div class="form settlement-currency"><label>顯示幣別<select id="settlement-currency">
      ${trip.display_currencies.map((code) => `<option value="${esc(code)}" ${(display?.currency ?? trip.settle_currency) === code ? "selected" : ""}>${esc(code)}</option>`).join("")}
    </select></label></div>` : ""}
    ${payments || '<p class="muted small">目前沒有待轉帳金額。</p>'}
    ${display ? `<p class="small muted">實際結算為 ${esc(trip.settle_currency)}。匯率來源 ${display.source_url
      ? `<a href="${esc(display.source_url)}" target="_blank" rel="noopener">${esc(display.source)}</a>` : esc(display.source)}</p>` : ""}</div>
    ${missing ? `<p class="warn">有 ${missing} 筆紀錄待換算，暫未計入結算。</p>` : ""}
    <section class="section"><div class="section-head"><h2>每人結餘</h2><span class="small muted">點開看明細</span></div><div class="card">${people}</div></section>`;
}

function settleView() {
  const choices = [["settle", "結算"], ["stats", "統計"]];
  return `<div class="segmented" role="tablist" aria-label="結算或統計">${choices.map(([key, label]) =>
    `<button type="button" role="tab" data-action="settle-view" data-id="${key}" aria-selected="${state.settleView === key}">${label}</button>`).join("")}</div>
    ${state.settleView === "stats" ? statsView() : settlementPanel()}`;
}

function myBalance() {
  const trip = state.trip;
  const balance = Number(trip.settlement.balances[state.listing.member_id] ?? 0);
  if (!balance) return "已結清";
  return `${balance > 0 ? "應收" : "應付"} ${money(Math.abs(balance), trip.settle_currency)}`;
}

function draftCard(entry, index) {
  const shares = entry.shares.map(({ member_id, amount, weight }) =>
    `${nameOf(member_id)}${amount != null ? ` ${money(amount, entry.currency)}` : weight != null && Number(weight) !== 1 ? ` ×${Number(weight)}` : ""}`);
  const refund = entry.kind === "refund";
  return `<div class="card draft"><div class="row"><span class="small muted">${esc(entry.spent_on)} · ${categoryLabel(entry.category)}</span>
      ${refund ? '<span class="badge">退款</span>' : ""}</div>
    <div class="row draft-main"><strong>${esc(entry.item)}</strong><strong class="num">${esc(money(entry.amount, entry.currency))}</strong></div>
    <p class="small muted">${esc(nameOf(entry.payer_id))}${refund ? "收" : "付"} · 分給 ${entry.shares.length} 人：${esc(shares.join("、"))}</p>
    <div class="actions">${button("修改", "draft-edit", "plain", "", index)}${button("移除", "draft-remove", "plain", "", index)}</div></div>`;
}

function aiView() {
  const { ai } = state;
  const trip = state.trip;
  const photo = state.config.ai;
  const drafts = ai.result?.expenses || [];
  const recent = [...trip.expenses].sort((a, b) => b.id - a.id).slice(0, 5);
  return `<form id="ai-form" class="card ai-compose">
      <label for="ai-text" class="ai-label">一句話記帳${photo ? "或拍收據" : ""}</label>
      <textarea id="ai-text" name="text" rows="3" maxlength="500" enterkeyhint="send" placeholder="晚餐 3000 我和小明&#10;計程車 800、早餐 350${photo ? "&#10;押金退回 5000 小明收" : ""}">${esc(ai.text)}</textarea>
      <div class="ai-bar">${photo ? `<button type="button" class="secondary" data-action="ai-photo">${icon("camera")}拍收據</button>
        <input type="file" id="ai-photo" accept="image/*" hidden>` : ""}<span class="spacer"></span>
        <button type="submit" class="primary" ${ai.busy ? "disabled" : ""}>${icon("spark")}解析</button></div>
    </form>
    ${ai.busy ? `<p class="busy" role="status">${esc(ai.busy)}</p>` : ""}
    ${ai.error ? `<p class="warn" role="alert">${esc(ai.error)}</p>` : ""}
    ${drafts.length ? `<section class="section stack" aria-label="待確認">
      <div class="section-head"><h2>待確認 ${drafts.length} 筆</h2><span class="small muted">確認後才會入帳</span></div>
      ${drafts.map(draftCard).join("")}
      <div class="actions">${button(`全部入帳`, "draft-confirm", "primary")}${button("取消", "draft-cancel", "plain")}</div></section>` : ""}
    <section class="section summary-grid" aria-label="帳本摘要">
      <button type="button" class="card" data-tab="settle"><span class="small muted">總花費</span><strong class="num">${esc(money(trip.settlement.total, trip.settle_currency))}</strong></button>
      <button type="button" class="card" data-tab="settle"><span class="small muted">我目前</span><strong class="num">${esc(myBalance())}</strong></button>
    </section>
    <section class="section"><div class="section-head"><h2>最近記的</h2>${trip.expenses.length > 5 ? button("全部明細", "goto-expenses", "plain") : ""}</div>
      <div class="stack">${recent.length ? recent.map(expenseRow).join("") : empty("還沒有紀錄。輸入一句話，或拍收據開始記帳。")}</div></section>`;
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

function expenseRow(entry) {
  const me = state.listing.member_id;
  const { settlement, settle_currency: currency } = state.trip;
  const refund = entry.kind === "refund";
  const share = settlement.shares[entry.id]?.[me];
  const myShare = share != null ? `我分擔 ${money(share, currency)}`
    : entry.shares.some(({ member_id }) => member_id === me) ? "我的分攤待換算" : "未分攤";
  return `
    <button type="button" class="card row row-button" data-action="edit-expense" data-id="${entry.id}">
      <span><strong>${esc(entry.item)}</strong><span class="line-item">${refund ? "退款 · " : ""}${categoryLabel(entry.category)} · ${esc(nameOf(entry.payer_id))}${refund ? "收" : "付"}${entry.merchant ? ` · ${esc(entry.merchant)}` : ""}</span></span>
      <span class="amount-right"><strong class="${refund ? "refund-amount" : ""}">${refund ? "−" : ""}${esc(money(entry.amount, entry.currency))}</strong>
        <span class="line-item num">${esc(myShare)}</span>${entry.amount_settle == null && myShare !== "我的分攤待換算" ? '<span class="line-item">待換算</span>' : ""}</span>
    </button>`;
}

function transferRow(row) {
  return `<button type="button" class="card row row-button transfer-row" data-action="edit-transfer" data-id="${row.id}">
      <span><strong>${esc(nameOf(row.from_id))} → ${esc(nameOf(row.to_id))}</strong><span class="line-item">還款</span></span>
      <span class="amount-right"><strong class="num">${esc(money(row.amount, row.currency))}</strong>
      ${row.amount_settle == null ? '<span class="line-item">待換算</span>' : ""}</span></button>`;
}

function transferDay(row) {
  return new Date(row.created_at).toLocaleDateString("sv-SE", { timeZone: state.trip.timezone });
}

function expenseRows() {
  const filters = new URLSearchParams(location.hash.slice(1));
  const keyword = (filters.get("q") || "").trim().normalize("NFKC").toLowerCase();
  const category = filters.get("category");
  const payer = filters.get("payer");
  const kind = filters.get("kind");
  const mine = filters.get("mine") === "1";
  const me = state.listing.member_id;
  const { expenses, transfers } = state.trip;
  const rows = [];
  for (const entry of expenses) {
    if (kind && entry.kind !== kind) continue;
    if (category && entry.category !== category) continue;
    if (payer && String(entry.payer_id) !== payer) continue;
    if (mine && entry.payer_id !== me && !entry.shares.some(({ member_id }) => member_id === me)) continue;
    if (keyword && ![entry.item, entry.merchant].some((value) => value?.normalize("NFKC").toLowerCase().includes(keyword))) continue;
    rows.push([entry.spent_on, entry.id, expenseRow(entry)]);
  }
  for (const row of transfers) {
    if ((kind && kind !== "transfer") || category || keyword) continue;
    if (payer && String(row.from_id) !== payer) continue;
    if (mine && row.from_id !== me && row.to_id !== me) continue;
    rows.push([transferDay(row), 0, transferRow(row)]);
  }
  if (!rows.length) return empty(expenses.length || transfers.length ? "沒有符合篩選條件的紀錄。" : "這本帳目前沒有紀錄。");
  rows.sort((a, b) => b[0].localeCompare(a[0]) || b[1] - a[1]);
  const groups = new Map();
  for (const [day, , markup] of rows) {
    if (!groups.has(day)) groups.set(day, []);
    groups.get(day).push(markup);
  }
  return [...groups].map(([day, items]) => `<section class="section"><h3 class="date-heading">${esc(day)}</h3><div class="stack">${items.join("")}</div></section>`).join("");
}

function expenseList() {
  const filters = new URLSearchParams(location.hash.slice(1));
  const option = (key, value, label = value) =>
    `<option value="${esc(value)}" ${filters.get(key) === String(value) ? "selected" : ""}>${esc(label)}</option>`;
  const active = ["q", "category", "payer", "kind", "mine"].some((key) => filters.get(key));
  return `<div class="section-head"><h2>明細</h2>${button("匯出 CSV", "export", "plain", "download")}</div>
    <details class="card form filters" id="expense-filters" ${active ? "open" : ""}><summary>搜尋與篩選${active ? ' <span class="badge">已篩選</span>' : ""}</summary>
      <label>關鍵字<input type="search" name="q" maxlength="100" value="${esc(filters.get("q"))}" placeholder="品項或商家"></label>
      <div class="fields"><label>類型<select name="kind"><option value="">全部</option>${option("kind", "expense", "支出")}${option("kind", "refund", "退款")}${option("kind", "transfer", "還款")}</select></label>
      <label>類別<select name="category"><option value="">全部類別</option>${state.config.categories.map((category) => option("category", category)).join("")}</select></label></div>
      <label>付款人<select name="payer"><option value="">所有成員</option>${state.trip.members.map(({ id, name }) => option("payer", id, name)).join("")}</select></label>
      <div class="row"><label class="check"><input type="checkbox" name="mine" ${filters.get("mine") === "1" ? "checked" : ""}>與我有關</label>${button("清除篩選", "clear-filters", "plain")}</div>
    </details>
    <div id="expense-results">${expenseRows()}</div>`;
}

function balanceBreakdown(memberId) {
  const trip = state.trip;
  const { paid, owed, balances, shares, transfers } = trip.settlement;
  const format = (amount) => esc(money(amount, trip.settle_currency));
  const paidItems = [], owedItems = [], sentItems = [], receivedItems = [];
  let sent = 0, received = 0;
  const item = (date, label, amount) => `<li class="row"><span>${esc(date)} · ${esc(label)}</span>
    <span class="num">${format(amount)}</span></li>`;
  for (const entry of trip.expenses) {
    const portions = shares[entry.id];
    if (!portions) continue;
    const label = entry.kind === "refund" ? `退款 · ${entry.item}` : entry.item;
    if (entry.payer_id === memberId) {
      paidItems.push(item(entry.spent_on, label, Object.values(portions).reduce((sum, value) => sum + Number(value), 0)));
    }
    if (memberId in portions) owedItems.push(item(entry.spent_on, label, portions[memberId]));
  }
  for (const row of trip.transfers) {
    const amount = transfers[row.id];
    if (amount == null) continue;
    const day = new Date(row.created_at).toLocaleDateString("zh-TW", { timeZone: trip.timezone });
    if (row.from_id === memberId) {
      sent += Number(amount);
      sentItems.push(item(day, nameOf(row.to_id), amount));
    }
    if (row.to_id === memberId) {
      received += Number(amount);
      receivedItems.push(item(day, nameOf(row.from_id), amount));
    }
  }
  const section = (title, items) => `<section><h3>${title}</h3>${items.length
    ? `<ul class="balance-items">${items.join("")}</ul>` : '<p class="small muted">無紀錄</p>'}</section>`;
  return section("付了", paidItems) + section("分擔", owedItems) + section("已還", sentItems) + section("已收", receivedItems) +
    `<p class="small balance-formula">付了 ${format(paid[memberId] ?? 0)} − 分擔 ${format(owed[memberId] ?? 0)} +
    已還 ${format(sent)} − 已收 ${format(received)} = <strong class="num">結餘 ${format(balances[memberId] ?? 0)}</strong></p>`;
}

function statsView() {
  const data = state.stats;
  if (state.statsError) return `<div class="card stack"><p role="alert">${esc(state.statsError)}</p>${button("重試", "retry-stats")}</div>`;
  if (!data) return '<p class="muted" role="status">正在載入統計…</p>';
  const { by_category: categories, by_day: days, by_member: members, by_member_category: memberCategories } = data;
  const format = (amount) => esc(money(amount, data.currency));
  const maxAmount = (rows) => rows.reduce((max, { amount }) => Math.max(max, Math.abs(amount)), 1);
  const maxDay = maxAmount(days);
  const bar = (amount, max) => Math.abs(amount) / max * 100;
  // 混合正負金額的淨額比例可能小於 0% 或大於 100%，這時只顯示表列比例。
  const total = Number(data.total);
  const canStack = total !== 0 && categories.every(({ amount }) => Number(amount) * total >= 0);
  const segments = canStack ? categories.filter(({ amount }) => Number(amount) !== 0).map(({ category, amount }) =>
    `<span data-category="${esc(category)}" style="flex-grow:${Math.abs(Number(amount))}"></span>`).join("") : "";
  const categoryRows = categories.map(({ category, amount, percent }) =>
    `<button class="stats-category" type="button" data-action="filter-category" data-id="${esc(category)}">
      ${categoryLabel(category)}<strong class="num">${format(amount)}</strong><span class="small num muted">${total ? `${esc(percent)}%` : "—"}</span>
    </button>`).join("");
  const dayRows = days.map(({ day, amount }) => `<li class="stats-day">
    <span class="small num">${format(amount)}</span><span class="stats-day-track" aria-hidden="true"><span style="height:${bar(amount, maxDay)}%"></span></span>
    <time class="small muted num" datetime="${esc(day)}" title="${esc(day)}">${esc(day.slice(5))}</time></li>`).join("");
  const selected = state.statsCategory;
  const visibleMembers = selected ? memberCategories[selected] ?? {} : members;
  const memberRows = state.trip.members.map(({ id, name }) => `<div class="stats-member-row">
    <span>${esc(name)}</span><strong class="num">${format(visibleMembers[id]?.paid ?? 0)}</strong>
    <strong class="num">${format(visibleMembers[id]?.owed ?? 0)}</strong></div>`).join("");
  const me = state.listing.member_id;
  const myRows = categories.filter(({ category }) => Number(memberCategories[category][me]?.owed ?? 0) !== 0)
    .map(({ category }) => `<div class="row list-row">${categoryLabel(category)}
      <strong class="num">${format(memberCategories[category][me].owed)}</strong></div>`).join("");
  return `${data.missing_count ? `<p class="warn">${data.missing_count} 筆缺匯率未計入</p>` : ""}
    <section class="card stats-metrics" aria-label="花費總計">
      <div><span>總花費</span><strong class="num">${format(data.total)}</strong></div>
      <div><span>天數</span><strong class="num">${data.days} 天</strong></div>
      <div><span>平均每天</span><strong class="num">${format(data.average_per_day)}</strong></div>
      <div><span>平均每人</span><strong class="num">${format(data.average_per_member)}</strong></div>
    </section>
    <section class="card section stats-categories"><h2>類別占比</h2>
      ${segments ? `<div class="stats-composition" aria-hidden="true">${segments}</div>` : ""}
      ${categoryRows ? `<div class="stats-category-head small muted" aria-hidden="true"><span>類別</span><span>金額</span><span>比例</span></div>${categoryRows}`
        : '<p class="small muted">目前沒有可統計的支出。</p>'}</section>
    <section class="card stack section"><h2>依日期</h2>${dayRows
      ? `<ul class="stats-days" aria-label="每日花費" tabindex="0">${dayRows}</ul>` : '<p class="small muted">目前沒有可統計的支出。</p>'}</section>
    <section class="card stack section"><h2>依成員</h2>
      <div class="form"><label>類別<select id="stats-category"><option value="">全部類別</option>${categories.map(({ category }) =>
        `<option value="${esc(category)}" ${selected === category ? "selected" : ""}>${esc(category)}</option>`).join("")}</select></label></div>
      <div class="stats-member-row small muted"><span>成員</span><span>付了</span><span>分擔</span></div>${memberRows}
    </section>
    <section class="card stack section"><div class="row"><h2>我的花費</h2><strong class="num">${format(members[me]?.owed ?? 0)}</strong></div>
      ${myRows || '<p class="small muted">目前沒有分攤。</p>'}</section>`;
}

function memberList() {
  const trip = state.trip;
  const rows = trip.members.map((member) => `<div class="row list-row"><span><strong>${esc(member.name)}</strong>${member.id === state.listing.member_id ? " · 你" : ""}
    <span class="line-item">權重 ${esc(member.weight)}${member.organizer ? " · 建立者" : ""}${member.active ? "" : " · 已停用"}</span></span>
    <div class="actions">
      ${isOpen() && (trip.can_manage || member.id === state.listing.member_id) ? button("編輯", "edit-member", "plain", "", member.id) : ""}
      ${member.id === state.listing.member_id ? button("收款方式", "edit-payment-note", "plain", "", member.id) : ""}
      ${trip.can_manage && !member.organizer ? button("刪除", "delete-member", "danger", "", member.id) : ""}
    </div></div>`).join("");
  return `<div class="section-head"><h2>成員</h2>${isOpen() && trip.can_manage ? button("新增成員", "add-member", "secondary", "plus") : ""}</div>
    <div class="card">${rows}</div>${trip.invite_url ? `<section class="section"><div class="card stack"><h2>邀請旅伴</h2>
    <p class="small muted">分享連結，旅伴用 LINE 登入即可加入帳本。</p>${button("複製邀請連結", "invite")}</div></section>` : ""}`;
}

function settingsView() {
  const trip = state.trip;
  const payerName = trip.default_payer_id ? nameOf(trip.default_payer_id) : "記帳的人";
  const rates = Object.entries(trip.fixed_rates).map(([code, rate]) => `${code} = ${rate}`).join("\n");
  if (!trip.can_manage || !isOpen()) {
    return `<section class="card stack"><h2>帳本設定</h2><p>日期 ${esc(dates(trip))}</p><p>時區 ${esc(trip.timezone)}</p>
      <p>記帳幣別 ${esc(trip.default_currency)} · 結算幣別 ${esc(trip.settle_currency)}</p><p class="small muted">固定匯率 ${rates ? esc(rates.replaceAll("\n", "、")) : "未設定"}</p>
      <p class="small muted">預設付款人 ${esc(payerName)}</p>
      ${trip.line_group ? `<p class="small muted">網頁改帳通知 LINE 群組：${trip.notify_web_changes ? "開啟" : "關閉"}</p>` : ""}
      ${trip.can_manage && !isOpen() ? `<div class="actions">${button("重新開啟帳本", "reopen-trip", "primary")}${button("刪除帳本", "delete-trip", "danger")}</div>` : ""}</section>`;
  }
  return `<form id="settings-form" class="card form">
    <h2>帳本設定</h2><label>帳本名稱<input name="name" required maxlength="80" value="${esc(trip.name)}"></label>
    <div class="fields"><label>開始日期<input type="date" name="start_date" value="${esc(trip.start_date)}"></label><label>結束日期<input type="date" name="end_date" value="${esc(trip.end_date)}"></label></div>
    <label>時區<input name="timezone" required maxlength="64" value="${esc(trip.timezone)}" placeholder="Asia/Taipei"></label>
    <div class="fields">${currencyField("記帳幣別", "default_currency", trip.default_currency)}${currencyField("結算幣別", "settle_currency", trip.settle_currency, trip.has_records)}</div>
    ${currencyOptions()}
    ${trip.has_records ? '<p class="muted small">帳本已有紀錄，結算幣別已鎖定。</p>' : ""}
    <label>固定匯率<textarea name="fixed_rates" rows="4" spellcheck="false" placeholder="JPY = 0.2135">${esc(rates)}</textarea></label>
    <p class="muted small">每行填一種原幣與匯率，表示 1 單位原幣可換多少結算幣。已換算的紀錄會保留原匯率。</p>
    <label>預設付款人<select name="default_payer_id"><option value="0">記帳的人</option>${trip.members.filter((member) => member.active)
      .map((member) => `<option value="${member.id}" ${member.id === trip.default_payer_id ? "selected" : ""}>${esc(member.name)}</option>`).join("")}</select></label>
    <p class="muted small">在 LINE 或網頁記帳沒說誰付時記在這個人身上，適合由一人先付或保管公費。說「我付」或請客時仍記在自己身上。</p>
    ${trip.line_group ? `<label class="check"><input type="checkbox" name="notify_web_changes" ${trip.notify_web_changes ? "checked" : ""}>網頁記帳或修改時通知 LINE 群組</label>
    <p class="muted small">每則通知會依群組人數用掉推播額度（每月 200 則）。在群組直接傳訊息記帳不受影響。</p>` : ""}
    <p class="error" role="alert" hidden></p><div class="actions"><button type="submit" class="primary">儲存設定</button>${button("結束帳本", "close-trip", "danger")}</div>
  </form>`;
}

function tripView() {
  return `<section>${memberList()}</section><section class="section">${settingsView()}</section>`;
}

function render() {
  if (!state.listing) return;
  if (!state.trip) { setPage(listView()); return; }
  const view = { manual: manualView, expenses: expenseList, ai: aiView, settle: settleView, trip: tripView }[state.tab];
  setPage(view());
  if (state.tab === "manual") wireManualForm();
}

async function loadList() {
  state.listing = await call("/api/trips");
  render();
}

async function openTrip(id, tab = null, preloaded = null) {
  const trip = preloaded || await call(`/api/trips/${id}`);
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

function currencyField(label, name, value, readonly = false) {
  return `<label>${label}<input name="${name}" value="${esc(value)}" ${readonly ? "readonly" : 'list="currencies"'} required maxlength="3" pattern="[A-Za-z]{3}" autocapitalize="characters"></label>`;
}

function currencyOptions() {
  return `<datalist id="currencies">${Object.entries(state.config.currencies).map(([code, label]) => `<option value="${esc(code)}">${esc(label)}</option>`).join("")}</datalist>`;
}

function memberOptions(selected) {
  return state.trip.members.map((member) => `<option value="${member.id}" ${member.id === selected ? "selected" : ""}>${esc(member.name)}${member.active ? "" : "（已停用）"}</option>`).join("");
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
  openForm("標記已付", `<p>確認 ${esc(nameOf(from_id))} 已付給 ${esc(nameOf(to_id))} <strong class="num">${esc(money(amount, body.currency))}</strong>？</p>`,
    async () => {
      await call(endpoint("/transfers"), { method: "POST", body });
      return "已記還款";
    }, { submitLabel: "確認已付", extra: button("取消", "dismiss") });
}

function expenseFields(entry = null, kind = null) {
  const trip = state.trip;
  const shareById = new Map((entry?.shares || []).map((share) => [share.member_id, share]));
  const choices = trip.members.filter((member) => member.active || shareById.has(member.id));
  const currentKind = kind || entry?.kind || "expense";
  const kindField = kind ? `<input type="hidden" name="kind" value="${esc(kind)}">`
    : `<label>類型<select name="kind"><option value="expense">支出</option>
    <option value="refund" ${currentKind === "refund" ? "selected" : ""}>退款</option></select></label>`;
  return `${kindField}
    <label>品項<input name="item" required maxlength="100" value="${esc(entry?.item)}" placeholder="${currentKind === "refund" ? "押金退回" : "晚餐"}"></label>
    <div class="fields"><label>金額<input name="amount" type="number" min="0.01" step="0.01" inputmode="decimal" required value="${esc(entry?.amount)}"></label>
    ${currencyField("幣別", "currency", entry?.currency || trip.default_currency)}</div>${currencyOptions()}
    <div class="fields"><label>日期<input name="spent_on" type="date" required value="${esc(entry?.spent_on || trip.today)}"></label>
    <label><span data-payer-label>${currentKind === "refund" ? "收款人" : "付款人"}</span><select name="payer_id" required>${memberOptions(entry?.payer_id || state.trip.default_payer_id || state.listing.member_id)}</select></label></div>
    <div class="fields"><label>類別<select name="category">${state.config.categories.map((category) => `<option ${category === (entry?.category || "待分類") ? "selected" : ""}>${esc(category)}</option>`).join("")}</select></label>
    <label>分攤方式<select name="split_mode"><option value="equal">按權重</option><option value="exact" ${entry?.split_mode === "exact" ? "selected" : ""}>指定金額</option></select></label></div>
    <div class="share-box"><h3>分攤成員</h3>${choices.map((member) => { const share = shareById.get(member.id); return `
      <div class="share-row" data-member="${member.id}"><label class="check"><input type="checkbox" ${entry ? (share ? "checked" : "") : "checked"}><span>${esc(member.name)}${member.active ? "" : "（已停用）"}</span></label>
      <input type="number" inputmode="decimal" data-weight="${esc(share?.weight ?? member.weight)}" data-amount="${esc(share?.amount ?? "")}" aria-label="${esc(member.name)}的分攤值"></div>`; }).join("")}</div>
    <label>商家（選填）<input name="merchant" maxlength="100" value="${esc(entry?.merchant)}"></label>`;
}

function wireExpenseForm(form, entry = null) {
  let mode = entry?.split_mode || "equal";
  const modeSelect = $("[name=split_mode]", form);
  function syncShares(reset) {
    const nextMode = modeSelect.value;
    for (const row of form.querySelectorAll("[data-member]")) {
      const input = $("[type=number]", row);
      const selected = $("[type=checkbox]", row).checked;
      if (!reset && mode !== nextMode) input.dataset[mode === "exact" ? "amount" : "weight"] = input.value;
      if (reset || mode !== nextMode) input.value = input.dataset[nextMode === "exact" ? "amount" : "weight"];
      input.disabled = !selected;
      input.required = selected;
      input.min = nextMode === "exact" ? "0.01" : "0.0001";
      input.step = input.min;
      input.placeholder = nextMode === "exact" ? "金額" : "權重";
    }
    mode = nextMode;
  }
  form.addEventListener("change", ({ target }) => {
    if (target.name === "kind") $("[data-payer-label]", form).textContent = target.value === "refund" ? "收款人" : "付款人";
    syncShares(false);
  });
  syncShares(true);
}

function readExpense(formNode) {
  const body = Object.fromEntries(new FormData(formNode));
  body.currency = body.currency.toUpperCase();
  body.payer_id = Number(body.payer_id);
  body.merchant = body.merchant?.trim() || null;
  body.shares = [...formNode.querySelectorAll("[data-member]")].filter((row) => $("[type=checkbox]", row).checked)
    .map((row) => ({ member_id: Number(row.dataset.member), [body.split_mode === "exact" ? "amount" : "weight"]: $("[type=number]", row).value }));
  if (!body.shares.length) throw new Error("請至少選一位分攤成員。");
  return body;
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

function transferFields() {
  const trip = state.trip;
  const current = state.listing.member_id;
  const payment = trip.settlement.payments.find((row) => row.from_id === current) || trip.settlement.payments[0];
  const from = payment?.from_id || current;
  const to = payment?.to_id || trip.members.find((member) => member.id !== from)?.id;
  return `<p class="small muted">還款或把公費交給保管的人，都記在這裡。結算會自動扣掉。</p>
    <div class="fields"><label>付款人<select name="from_id">${memberOptions(from)}</select></label>
    <label>收款人<select name="to_id">${memberOptions(to)}</select></label></div><div class="fields">
    <label>金額<input name="amount" type="number" min="0.01" step="0.01" inputmode="decimal" required value="${esc(payment?.amount)}"></label>
    ${currencyField("幣別", "currency", trip.settle_currency)}</div>${currencyOptions()}`;
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
    <p class="small muted">記錄於 ${esc(new Date(row.created_at).toLocaleString("zh-TW", { timeZone: trip.timezone }))}</p>
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
  for (const line of body.fixed_rates.split("\n").map((row) => row.trim()).filter(Boolean)) {
    const pair = line.match(/^([A-Za-z]{3})\s*=\s*(\d+(?:\.\d+)?)$/);
    if (!pair || pair[1].toUpperCase() in rates) throw new Error("每行填一種幣別與匯率，例如 JPY = 0.2135。");
    rates[pair[1].toUpperCase()] = pair[2];
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

async function invite() {
  const link = new URL(state.trip.invite_url, location.href).href;
  try { await navigator.clipboard.writeText(link); notice("已複製邀請連結"); }
  catch {
    dialog.innerHTML = `<div class="dialog-head"><h2 id="dialog-title">邀請連結</h2>${button("關閉", "dismiss", "plain")}</div>
      <div class="form"><label>選取後複製<input value="${esc(link)}" readonly></label></div>`;
    dialog.showModal();
    $("input", dialog).select();
  }
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

document.addEventListener("click", async (event) => {
  const target = event.target.closest("button[data-action], button[data-tab]");
  if (!target || target.disabled) return;
  const { action, tab, id } = target.dataset;
  if (tab) {
    clearUndo();
    await switchTab(tab);
    window.scrollTo(0, 0);
    return;
  }
  target.disabled = true;
  try {
    if (action === "retry") location.reload();
    else if (action === "login") lineLogin();
    else if (action === "dismiss") dialog.close();
    else if (action === "list" && state.listing) {
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
    else if (action === "add-member") memberForm();
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
