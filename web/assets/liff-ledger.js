import { esc, money } from "./common.js?v=19";
import { state, button, categoryLabel, empty, icon, isOpen, nameOf } from "./liff-shared.js?v=19";

function settlementPanel() {
  const trip = state.trip;
  const { settlement: result, members } = trip;
  const missing = result.missing_expense_ids.length + result.missing_transfer_ids.length;
  const people = members.map((member) => `<details class="list-row balance-person" data-member-id="${member.id}">
    <summary><span><strong>${esc(member.name)}</strong>${member.active ? "" : '<span class="line-item">已停用</span>'}</span>
      <span class="amount-right small"><span class="num"><strong>${esc(balanceLabel(result.balances[member.id], trip.settle_currency))}</strong></span>
      <span class="line-item">已付 ${esc(money(result.paid[member.id] ?? 0, trip.settle_currency))} · 分攤 ${esc(money(result.owed[member.id] ?? 0, trip.settle_currency))}</span></span>
    </summary><div class="balance-breakdown"></div></details>`).join("");
  const viewer = state.listing.member_id;
  const display = state.settlementDisplay;
  const viewerIsOwner = members.some(({ id, organizer }) => id === viewer && organizer);
  const mine = [], others = [];
  result.payments.forEach(({ from_id, to_id, amount }, index) => {
    const note = members.find(({ id }) => id === to_id)?.payment_note;
    const personal = viewer === from_id || viewer === to_id;
    const label = viewer === from_id ? `你要付${nameOf(to_id)}` : viewer === to_id ? `你要向${nameOf(from_id)}收款` : `${nameOf(from_id)} → ${nameOf(to_id)}`;
    (personal ? mine : others).push(`<div class="row list-row"><div><strong class="payment">${esc(label)}</strong>
      ${note ? `<span class="line-item">收款方式 ${esc(note)}</span>` : ""}</div>
      <div class="amount-right"><strong class="num">${display ? "約 " : ""}${esc(money(display?.amounts[index] ?? amount, display?.currency ?? trip.settle_currency))}</strong>
      ${viewer === from_id || viewerIsOwner ? `<div>${button(viewer === to_id ? "確認已收到" : "標記已付", "mark-payment", "plain", "", index)}</div>` : ""}</div></div>`);
  });
  const expenses = trip.expenses.filter(entry => entry.kind !== "refund").length;
  const refunds = trip.expenses.length - expenses;
  return `<section class="card section"><h2>我的結算</h2>${mine.join("") || `<p>${missing ? "目前沒有待轉帳金額，仍有紀錄待換算。" : "你已結清。"}</p>`}</section>
    <section class="hero section"><p class="eyebrow">總花費</p><strong class="hero-amount">${esc(money(result.total, trip.settle_currency))}</strong>
    <div class="hero-foot"><span>${expenses} 筆支出${refunds ? ` · ${refunds} 筆退款` : ""}</span><span>${members.length} 位成員</span></div></section>
    <div class="card section"><div class="section-head"><h2>其他轉帳</h2>${isOpen() && members.length > 1 ? button("還款／交公費", "transfer", "secondary") : ""}</div>
    ${trip.display_currencies.length > 1 ? `<div class="form settlement-currency"><label>顯示幣別<select id="settlement-currency">
      ${trip.display_currencies.map((code) => `<option value="${esc(code)}" ${(display?.currency ?? trip.settle_currency) === code ? "selected" : ""}>${esc(code)}</option>`).join("")}
    </select></label></div>` : ""}
    ${others.join("") || '<p class="muted small">沒有其他待轉帳。</p>'}
    ${display ? `<p class="small muted">實際結算為 ${esc(trip.settle_currency)}。匯率來源 ${display.source_url
      ? `<a href="${esc(display.source_url)}" target="_blank" rel="noopener">${esc(display.source)}</a>` : esc(display.source)}</p>` : ""}</div>
    ${missing ? `<p class="warn">有 ${missing} 筆紀錄待換算，暫未計入結算。</p>` : ""}
    <section class="section"><div class="section-head"><h2>每人結餘</h2><span class="small muted">點開看明細</span></div><div class="card">${people}</div></section>`;
}

export function settleView() {
  const choices = [["settle", "結算"], ["stats", "統計"]];
  return `<div class="segmented" role="tablist" aria-label="結算或統計">${choices.map(([key, label]) =>
    `<button type="button" role="tab" data-action="settle-view" data-id="${key}" aria-selected="${state.settleView === key}">${label}</button>`).join("")}</div>
    ${state.settleView === "stats" ? statsView() : settlementPanel()}`;
}

function myBalance() {
  const trip = state.trip;
  const balance = Number(trip.settlement.balances[state.listing.member_id] ?? 0);
  return balanceLabel(balance, trip.settle_currency);
}

function balanceLabel(amount, currency) {
  const value = Number(amount || 0);
  return value ? `${value > 0 ? "應收" : "應付"} ${money(Math.abs(value), currency)}` : "已結清";
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

export function aiView() {
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
      <span class="amount-right"><strong class="${refund ? "refund-amount" : ""}">${esc(money(refund ? -entry.amount : entry.amount, entry.currency))}</strong>
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

export function expenseRows() {
  const filters = new URLSearchParams(location.hash.slice(1));
  const keyword = (filters.get("q") || "").trim().normalize("NFKC").toLowerCase();
  const category = filters.get("category");
  const payer = filters.get("payer");
  const kind = filters.get("kind");
  const mine = filters.get("mine") === "1";
  const me = state.listing.member_id;
  const { expenses, transfers } = state.trip;
  const rows = [];
  const start = state.trip.start_date || [...expenses.map(e => e.spent_on), ...transfers.map(transferDay)].sort()[0];
  for (const entry of expenses) {
    if (kind && entry.kind !== kind) continue;
    if (category && entry.category !== category) continue;
    if (payer && String(entry.payer_id) !== payer) continue;
    if (mine && entry.payer_id !== me && !entry.shares.some(({ member_id }) => member_id === me)) continue;
    if (keyword && ![entry.item, entry.merchant].some((value) => value?.normalize("NFKC").toLowerCase().includes(keyword))) continue;
    const portions = state.trip.settlement.shares[entry.id];
    const net = portions ? Object.values(portions).reduce((n, share) => n + Number(share), 0) : null;
    rows.push([entry.spent_on, entry.id, expenseRow(entry), net]);
  }
  for (const row of transfers) {
    if ((kind && kind !== "transfer") || category || keyword) continue;
    if (payer && String(row.from_id) !== payer) continue;
    if (mine && row.from_id !== me && row.to_id !== me) continue;
    rows.push([transferDay(row), 0, transferRow(row), 0]);
  }
  if (!rows.length) return empty(expenses.length || transfers.length ? "沒有符合篩選條件的紀錄。" : "這本帳目前沒有紀錄。");
  rows.sort((a, b) => b[0].localeCompare(a[0]) || b[1] - a[1]);
  const groups = new Map();
  for (const [day, , markup, amount] of rows) {
    if (!groups.has(day)) groups.set(day, { items: [], total: 0, missing: 0 });
    const group = groups.get(day);
    group.items.push(markup);
    if (amount === null) group.missing++;
    else group.total += amount;
  }
  return [...groups].map(([day, group]) => {
    const weekday = new Date(day).toLocaleDateString("zh-TW", { weekday: "long", timeZone: "UTC" });
    const n = Math.round((Date.parse(day) - Date.parse(start)) / 86400000) + 1;
    const position = n > 0 ? `第 ${n} 天` : `出發前 ${1 - n} 天`;
    return `<section class="section"><h3 class="date-heading">${esc(day)} ${weekday} · ${position}
      <span class="line-item">${filters.size ? "篩選小計" : "當日小計"} ${money(group.total, state.trip.settle_currency)}${group.missing ? ` · ${group.missing} 筆待換算` : ""}</span></h3>
      <div class="stack">${group.items.join("")}</div></section>`;
  }).join("");
}

export function expenseList() {
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

export function balanceBreakdown(memberId) {
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
    已還 ${format(sent)} − 已收 ${format(received)} = <strong class="num">${esc(balanceLabel(balances[memberId], trip.settle_currency))}</strong></p>`;
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
      <div><span>平均每天</span><strong class="num">${esc(money(data.average_per_day, data.currency, true))}</strong></div>
      <div><span>平均每人</span><strong class="num">${esc(money(data.average_per_member, data.currency, true))}</strong></div>
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
