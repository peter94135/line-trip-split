import { $, esc } from "./common.js?v=20";
import { state, call, endpoint, currencyField, currencyOptions, memberOptions } from "./liff-shared.js?v=20";

export function recordInfo(entry) {
  if (!entry?.created_at) return "";
  const sources = { line_text: "LINE", gemini: "LINE AI", line_photo: "LINE 照片", web: "網頁",
    web_ai: "網頁 AI", web_photo: "網頁照片" };
  const by = state.trip.members.find(m => m.id === entry.created_by)?.name || entry.creator_name || "記帳人未記錄";
  const at = new Date(entry.created_at).toLocaleString("zh-TW", { timeZone: state.trip.timezone });
  return `<p class="small muted">${esc(by)} · ${esc(at)} · ${esc(sources[entry.source] || "來源未記錄")}</p>`;
}

export function expenseFields(entry = null, kind = null) {
  const trip = state.trip;
  const shareById = new Map((entry?.shares || []).map((share) => [share.member_id, share]));
  const choices = trip.members.filter((member) => member.active || shareById.has(member.id));
  const currentKind = kind || entry?.kind || "expense";
  const kindField = kind ? `<input type="hidden" name="kind" value="${esc(kind)}">`
    : `<label>類型<select name="kind"><option value="expense">支出</option>
    <option value="refund" ${currentKind === "refund" ? "selected" : ""}>退款</option></select></label>`;
  const weighted = entry && choices.some(m => Number(shareById.get(m.id)?.weight ?? 1) !== 1);
  const mode = entry?.split_mode === "exact" ? "exact" : weighted ? "weighted" : "even";
  return `${recordInfo(entry)}${kindField}
    <div class="fields"><label>金額<input name="amount" type="number" min="0.01" step="0.01" inputmode="decimal" required value="${esc(entry?.amount)}"></label>
    ${currencyField("幣別", "currency", entry?.currency || trip.default_currency)}</div>${currencyOptions()}
    <label>品項<input name="item" required maxlength="100" value="${esc(entry?.item)}" placeholder="${currentKind === "refund" ? "押金退回" : "晚餐"}"></label>
    <div class="fields"><label>日期<input name="spent_on" type="date" required value="${esc(entry?.spent_on || trip.today)}"></label>
    <label><span data-payer-label>${currentKind === "refund" ? "收款人" : "付款人"}</span><select name="payer_id" required>${memberOptions(entry?.payer_id || trip.default_payer_id || state.listing.member_id)}</select></label></div>
    <label>類別<select name="category">${state.config.categories.map(category => `<option ${category === (entry?.category || "待分類") ? "selected" : ""}>${esc(category)}</option>`).join("")}</select></label>
    <div class="share-box"><h3>分攤成員</h3>${choices.map(m => `<label class="check"><input type="checkbox" data-member="${m.id}" ${!entry || shareById.has(m.id) ? "checked" : ""}>
      ${esc(m.name)}${m.active ? "" : "（已停用）"}</label>`).join("")}</div>
    <details class="stack" ${mode !== "even" || entry?.merchant ? "open" : ""}><summary>進階</summary>
      <label>分攤方式<select name="split_mode">${[["even","平分"], ["weighted","按權重"], ["exact","指定金額"]].map(([value,label]) =>
        `<option value="${value}" ${value === mode ? "selected" : ""}>${label}</option>`).join("")}</select></label>
      ${choices.map(m => `<label data-share="${m.id}"><span>${esc(m.name)}</span>
        <input type="number" inputmode="decimal" data-weighted="${esc(shareById.get(m.id)?.weight ?? m.weight)}" data-exact="${esc(shareById.get(m.id)?.amount)}"></label>`).join("")}
      <label>商家（選填）<input name="merchant" maxlength="100" value="${esc(entry?.merchant)}"></label>
    </details>`;
}

export function wireExpenseForm(form, entry = null) {
  const fields = form.elements;
  let mode, timer, revision = 0;
  function syncShares() {
    const next = fields.split_mode.value;
    for (const row of form.querySelectorAll("[data-share]")) {
      const input = $("input", row);
      const selected = $(`[data-member="${row.dataset.share}"]`, form).checked;
      if (mode !== next) {
        if (mode && mode !== "even") input.dataset[mode] = input.value;
        input.value = input.dataset[next] || "";
      }
      input.disabled = row.hidden = next === "even" || !selected;
      input.required = !input.disabled;
      input.min = next === "exact" ? "0.01" : "0.0001";
      input.step = input.min;
    }
    mode = next;
  }
  form.addEventListener("change", ({ target }) => {
    if (target.name === "kind") $("[data-payer-label]", form).textContent = target.value === "refund" ? "收款人" : "付款人";
    if (target.name === "category") form.dataset.categoryChosen = "1";
    syncShares();
  });
  if (!entry) form.addEventListener("input", ({target}) => {
    if (!["item", "merchant"].includes(target.name) || form.dataset.categoryChosen) return;
    clearTimeout(timer);
    const current = ++revision;
    const query = new URLSearchParams({ item: fields.namedItem("item").value, merchant: fields.merchant.value });
    const path = endpoint(`/category?${query}`);
    timer = setTimeout(async () => {
      try {
        const { category } = await call(path);
        if (form.isConnected && current === revision && !form.dataset.categoryChosen) fields.category.value = category;
      } catch { /* 儲存時後端會再次查類別。 */ }
    }, 200);
  });
  syncShares();
}

export function readExpense(form) {
  const body = Object.fromEntries(new FormData(form));
  const mode = body.split_mode;
  body.split_mode = mode === "exact" ? "exact" : "equal";
  body.currency = body.currency.toUpperCase();
  body.payer_id = Number(body.payer_id);
  body.merchant = body.merchant?.trim() || null;
  body.shares = [...form.querySelectorAll("[data-member]:checked")].map(box => {
    const id = Number(box.dataset.member);
    const value = mode === "even" ? 1 : $(`[data-share="${id}"] input`, form).value;
    return { member_id: id, [mode === "exact" ? "amount" : "weight"]: value };
  });
  if (!body.shares.length) throw new Error("請至少選一位分攤成員。");
  return body;
}

export function transferFields() {
  const trip = state.trip;
  const current = state.listing.member_id;
  const payment = trip.settlement.payments.find((row) => row.from_id === current) || trip.settlement.payments[0];
  const from = payment?.from_id || current;
  const to = payment?.to_id || trip.members.find((member) => member.id !== from)?.id;
  return `<p class="small muted">還款或把公費交給保管的人，都記在這裡。結算會自動扣掉。</p>
    <div class="fields">
    <label>金額<input name="amount" type="number" min="0.01" step="0.01" inputmode="decimal" required value="${esc(payment?.amount)}"></label>
    ${currencyField("幣別", "currency", trip.settle_currency)}</div>${currencyOptions()}
    <div class="fields"><label>付款人<select name="from_id">${memberOptions(from)}</select></label>
    <label>收款人<select name="to_id">${memberOptions(to)}</select></label></div>`;
}
