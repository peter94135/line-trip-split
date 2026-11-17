import { $, api, esc, notice } from "./common.js?v=17";

const app = $("#app");
const logout = $("#logout");
const loginMarkup = app.innerHTML;
let token = "";

const SOURCES = { line_text: "LINE 文字", gemini: "LINE AI", line_photo: "LINE 收據", web: "網頁", web_ai: "網頁 AI", web_photo: "網頁收據" };
const CHANNELS = { line: "LINE", web: "網頁", admin: "管理頁" };
const INBOX = { pending: "待處理", processing: "處理中", done: "完成", failed: "待核對", reviewed: "已核對" };
const DELIVERY = { pending: "待送", sent: "已送", failed: "送出失敗" };

const money = (value, currency) => `${currency} ${new Intl.NumberFormat("zh-TW", { maximumFractionDigits: 2 }).format(value)}`;
const when = (value) => value ? new Date(value).toLocaleString("zh-TW", { hour12: false }) : "—";
const day = (value) => value ? new Date(value).toLocaleDateString("zh-TW") : "—";
const section = (title, content, extra = "") => `<section class="section"><div class="section-head"><h2>${title}</h2>${extra}</div><div class="card">${content}</div></section>`;
const empty = (message) => `<p class="muted small">${message}</p>`;
const row = (left, right = "") => `<div class="row list-row"><span>${left}</span><span class="amount-right">${right}</span></div>`;
const badge = (trip) => trip.deleted_at ? '<span class="badge closed">已刪除</span>'
  : `<span class="badge ${trip.status === "closed" ? "closed" : ""}">${trip.status === "open" ? "進行中" : "已結束"}</span>`;
const metric = (label, value) => `<div><span>${label}</span><strong class="num">${value}</strong></div>`;

function showLogin() {
  token = "";
  logout.hidden = true;
  app.innerHTML = loginMarkup;
}

function bytes(value) {
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(0)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function tripCard(trip) {
  return `<button type="button" class="card row row-button" data-trip="${trip.id}">
    <span><strong>${esc(trip.name)}</strong>
      <span class="line-item">#${trip.id} · ${esc(trip.creator)} 建立 · ${trip.members} 人 · ${trip.expenses} 筆支出${trip.deleted_expenses ? `（另 ${trip.deleted_expenses} 筆已刪除）` : ""}</span>
      <span class="line-item">最後活動 ${esc(when(trip.last_activity))}${trip.line_group ? " · 已綁定 LINE 群組" : " · 未綁定群組"}</span></span>
    <span class="amount-right">${badge(trip)}<strong class="num">${esc(money(trip.total, trip.settle_currency))}</strong></span></button>`;
}

async function dashboard() {
  const data = await api("/api/admin/overview", { token });
  logout.hidden = false;
  const live = data.trips.filter((trip) => !trip.deleted_at);
  const removed = data.trips.filter((trip) => trip.deleted_at);
  const { line, gemini, system } = data;
  const recent = Object.values(line.recent).reduce((sum, count) => sum + count, 0);
  const quota = line.quota ? `${line.quota.used ?? "?"} / ${line.quota.type === "none" ? "不限" : line.quota.limit ?? "?"}` : "—";

  const failed = line.failed.map((item) => row(`<strong>事件 #${item.id} · ${esc(item.type)}</strong><span class="line-item">${esc(when(item.received_at))}</span>`,
    `<button type="button" class="secondary" data-review="${item.id}">標為已核對</button>`)).join("");
  const lineRows = [
    ...Object.entries(line.recent).map(([status, count]) => row(`近 24 小時 · ${esc(INBOX[status] || status)}`, `<strong>${esc(count)} 則</strong>`)),
    ...Object.entries(line.delivery).map(([status, count]) => row(`回覆補送 · ${esc(DELIVERY[status] || status)}`, `<strong>${esc(count)} 筆</strong>`)),
    row("本月推播額度", `<strong>${esc(quota)}</strong>`),
  ].join("");
  const models = gemini.models.map((item) => row(`<strong>${esc(item.model)}</strong>`,
    item.cooling_until ? `<span class="badge closed">冷卻到 ${esc(new Date(item.cooling_until * 1000).toLocaleTimeString("zh-TW", { hour12: false }))}</span>` : '<span class="badge">可用</span>')).join("");
  const usage = gemini.usage.map((item) => row(`<strong>${esc(item.model)}</strong><span class="line-item">輸入 ${esc(item.input_tokens)} · 輸出 ${esc(item.output_tokens)} tokens</span>`,
    `<strong>${esc(item.calls)} 次</strong><span class="line-item">${item.cost_usd == null ? "無價格估算" : `約 US$${esc(item.cost_usd.toFixed(4))}`}</span>`)).join("");
  const daily = gemini.daily.map((item) => row(esc(item.day), `<strong>${esc(item.calls)} 次</strong>`)).join("");

  app.innerHTML = `<div class="intro"><p class="eyebrow">服務管理</p><h1>旅伴分帳管理頁</h1></div>
    <section class="card stats-metrics" aria-label="摘要">
      ${metric("進行中帳本", `${live.filter((trip) => trip.status === "open").length} / ${live.length}`)}
      ${metric("近 24 小時 LINE 事件", recent)}
      ${metric("待核對事件", line.failed.length)}
      ${metric("本月推播", quota)}
    </section>
    <section class="section"><div class="section-head"><h2>帳本</h2><span class="small muted">點開看明細與操作紀錄</span></div>
      <div class="stack">${live.map(tripCard).join("") || empty("目前沒有帳本。")}</div></section>
    ${removed.length ? `<section class="section"><div class="section-head"><h2>已刪除的帳本</h2></div><div class="stack">${removed.map(tripCard).join("")}</div></section>` : ""}
    ${section("LINE 待核對事件", failed || empty("目前沒有待核對事件。"))}
    ${section("LINE 狀態", lineRows)}
    ${section("Gemini 模型", (gemini.configured ? models : empty("尚未設定 GEMINI_API_KEY。")) + (daily ? `<h3 class="section">最近 7 天</h3>${daily}` : ""))}
    ${section("Gemini 用量 · 最近 30 天", usage || empty("目前沒有 Gemini 呼叫紀錄。"))}
    ${section("系統", [
      row("部署版本", `<strong>${esc(system.version)}</strong>`),
      row("啟動時間", esc(when(system.started_at))),
      row("Python", esc(system.python)),
      row("資料庫大小", esc(bytes(system.db_bytes))),
      row("匯率更新日", `${esc(system.fx_date || "尚未取得")}<span class="line-item">${esc(system.fx_source)}</span>`),
      row("操作紀錄", `${esc(system.audit_count)} 筆`),
    ].join(""))}`;
  window.scrollTo(0, 0);
}

async function tripDetail(id) {
  const data = await api(`/api/admin/trips/${id}`, { token });
  const { trip, settlement, stats } = data;
  const currency = trip.settle_currency;
  const payments = settlement.payments.map((item) => row(`${esc(item.sender)} → ${esc(item.recipient)}`, `<strong class="num">${esc(money(item.amount, currency))}</strong>`)).join("");
  const members = data.members.map((member) => row(
    `<strong>${esc(member.name)}</strong><span class="line-item">LINE 名稱 ${esc(member.line_name || "—")}${member.nickname ? ` · 暱稱 ${esc(member.nickname)}` : ""} · 權重 ${esc(member.weight)}${member.active ? "" : " · 已停用"}</span>
      <span class="line-item">加入 ${esc(when(member.joined_at))}</span>`,
    `<strong class="num">結餘 ${esc(money(member.balance, currency))}</strong><span class="line-item">付 ${esc(money(member.paid, currency))} · 分攤 ${esc(money(member.owed, currency))}</span>`)).join("");
  const categories = stats.by_category.map((item) => row(esc(item.category), `<strong class="num">${esc(money(item.amount, currency))}</strong><span class="line-item">${esc(item.percent)}%</span>`)).join("");
  const expenses = data.expenses.map((item) => `<div class="row list-row ${item.deleted_at ? "deleted-row" : ""}"><span>
      <strong>${item.kind === "refund" ? "退款 · " : ""}${esc(item.item)}</strong>
      <span class="line-item">${esc(item.spent_on)} · ${esc(item.category)} · ${esc(item.payer)}${item.kind === "refund" ? "收" : "付"} · ${item.shares} 人分攤${item.merchant ? ` · ${esc(item.merchant)}` : ""}</span>
      <span class="line-item">${esc(SOURCES[item.source] || item.source)} · ${esc(item.created_by)} 於 ${esc(when(item.created_at))} 記錄</span>
      ${item.deleted_at ? `<span class="line-item deleted-note">已由 ${esc(item.deleted_by || "—")} 於 ${esc(when(item.deleted_at))} 刪除</span>` : ""}</span>
    <span class="amount-right"><strong class="num">${esc(money(item.amount, item.currency))}</strong>
      ${item.amount_settle != null && item.currency !== currency ? `<span class="line-item">${esc(money(item.amount_settle, currency))}</span>` : ""}</span></div>`).join("");
  const transfers = data.transfers.map((item) => `<div class="row list-row ${item.deleted_at ? "deleted-row" : ""}"><span>
      <strong>${esc(item.sender)} → ${esc(item.recipient)}</strong><span class="line-item">${esc(item.created_by)} 於 ${esc(when(item.created_at))} 記錄${item.deleted_at ? ` · ${esc(when(item.deleted_at))} 刪除` : ""}</span></span>
    <strong class="num">${esc(money(item.amount, item.currency))}</strong></div>`).join("");
  const audit = data.audit.map((item) => row(`<strong>${esc(item.actor)} · ${esc(item.action)}</strong><span class="line-item">${esc(item.detail)}</span>`,
    `<span class="small">${esc(when(item.at))}</span><span class="line-item">${esc(CHANNELS[item.channel] || item.channel)}</span>`)).join("");
  const dates = [trip.start_date, trip.end_date].filter(Boolean).join(" ～ ") || "日期未設定";

  app.innerHTML = `<button type="button" class="plain back" data-back>返回總覽</button>
    <div class="intro"><div class="row"><div><p class="eyebrow">帳本 #${trip.id}</p><h1>${esc(trip.name)}</h1></div>${badge(trip)}</div>
      <p class="muted small">${esc(dates)} · ${esc(trip.creator)} 建立於 ${esc(day(trip.created_at))} · ${trip.line_group ? "已綁定 LINE 群組" : "未綁定群組"}</p>
      <p class="muted small">記帳 ${esc(trip.default_currency)} · 結算 ${esc(currency)} · 時區 ${esc(trip.timezone)}${Object.keys(trip.fixed_rates).length ? ` · 固定匯率 ${esc(Object.entries(trip.fixed_rates).map(([code, rate]) => `${code}=${rate}`).join("、"))}` : ""}</p>
      ${trip.deleted_at ? `<p class="warn">已於 ${esc(when(trip.deleted_at))} 刪除。<button type="button" class="secondary" data-restore="${trip.id}">還原帳本</button></p>` : ""}</div>
    <section class="card stats-metrics" aria-label="摘要">
      ${metric("總花費", esc(money(settlement.total, currency)))}${metric("成員", data.members.length)}
      ${metric("支出", data.expenses.filter((item) => !item.deleted_at).length)}${metric("待換算", settlement.missing_expense_ids.length + settlement.missing_transfer_ids.length)}
    </section>
    ${section("結算", payments || empty("目前沒有待轉帳金額。"))}
    ${section("成員", members || empty("沒有成員。"))}
    ${section("類別", categories || empty("目前沒有可統計的支出。"))}
    ${section(`明細 · ${data.expenses.length} 筆（含已刪除）`, expenses || empty("沒有支出。"))}
    ${section("還款", transfers || empty("沒有還款。"))}
    ${section("操作紀錄", audit || empty("還沒有操作紀錄。2026-11-11 之前的操作沒有留下紀錄。"))}`;
  window.scrollTo(0, 0);
}

logout.addEventListener("click", showLogin);

app.addEventListener("submit", async (event) => {
  if (event.target.id !== "login") return;
  event.preventDefault();
  const form = event.target;
  const submit = $("button[type=submit]", form);
  const error = $("#login-error");
  submit.disabled = true;
  error.hidden = true;
  try {
    const password = new FormData(form).get("password");
    token = (await api("/api/admin/login", { method: "POST", body: { password } })).token;
    form.reset();
    await dashboard();
  } catch (err) { error.textContent = err.message; error.hidden = false; }
  finally { submit.disabled = false; }
});

app.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-review], button[data-trip], button[data-back], button[data-restore]");
  if (!button) return;
  button.disabled = true;
  try {
    if (button.dataset.review) {
      await api(`/api/admin/inbox/${button.dataset.review}/review`, { method: "POST", token });
      await dashboard();
      notice("已標為核對完成");
    } else if (button.dataset.trip) await tripDetail(button.dataset.trip);
    else if (button.dataset.restore) {
      if (!window.confirm("還原這本帳本？成員會重新在 LINE 和網頁看到它。")) return;
      await api(`/api/admin/trips/${button.dataset.restore}/restore`, { method: "POST", token });
      notice("帳本已還原");
      await tripDetail(button.dataset.restore);
    } else await dashboard();
  } catch (err) {
    if (err.status === 401) showLogin();
    notice(err.message);
  } finally { button.disabled = false; }
});
