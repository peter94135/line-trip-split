import { $, api, esc, money, notice } from "./common.js?v=21";

const app = $("#app");
const logout = $("#logout");
const loginMarkup = app.innerHTML;
let token = "";

const SOURCES = { line_text: "LINE 文字", gemini: "LINE AI", line_photo: "LINE 收據", web: "網頁", web_ai: "網頁 AI", web_photo: "網頁收據" };
const CHANNELS = { line: "LINE", web: "網頁", admin: "管理頁" };
const INBOX = { pending: "待處理", processing: "處理中", done: "完成", failed: "待核對", reviewed: "已核對" };
const DELIVERY = { pending: "待送", sent: "已送", failed: "送出失敗" };

const when = (value) => value ? new Date(value).toLocaleString("zh-TW", { hour12: false }) : "—";
const day = (value) => value ? new Date(value).toLocaleDateString("zh-TW") : "—";
const section = (title, content, extra = "", id = "") => `<section class="section"${id ? ` id="${id}"` : ""}><div class="section-head"><h2>${title}</h2>${extra}</div><div class="card">${content}</div></section>`;
const empty = (message) => `<p class="muted small">${message}</p>`;
const row = (left, right = "") => `<div class="row list-row"><span>${left}</span><span class="amount-right">${right}</span></div>`;
const badge = (trip) => trip.deleted_at ? '<span class="badge closed">已刪除</span>'
  : `<span class="badge ${trip.status === "closed" ? "closed" : ""}">${trip.status === "open" ? "進行中" : "已結束"}</span>`;
const metric = (label, value) => `<div><span>${label}</span><strong class="num">${value}</strong></div>`;

const pushSupported = () => "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
const standalone = () => window.matchMedia("(display-mode: standalone)").matches || navigator.standalone;

function base64Bytes(value) {
  const text = atob((value + "=".repeat((4 - value.length % 4) % 4)).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(text, (char) => char.charCodeAt(0));
}

async function pushRegistration() {
  return navigator.serviceWorker.register("/admin-sw.js", { scope: "/admin" });
}

async function currentSubscription() {
  if (!pushSupported()) return null;
  const registration = await navigator.serviceWorker.getRegistration("/admin");
  return registration ? registration.pushManager.getSubscription() : null;
}

async function pushCard(devices) {
  let state, actions = "";
  if (!pushSupported()) {
    state = /iPhone|iPad/.test(navigator.userAgent) && !standalone()
      ? "iPhone 或 iPad 請先在 Safari 按分享鍵，選「加入主畫面」，再從主畫面打開管理頁開啟通知。"
      : "這個瀏覽器不支援通知。請改用 Chrome、Edge、Firefox 或 Safari。";
  } else if (Notification.permission === "denied") {
    state = "通知已被封鎖。請在瀏覽器或系統設定允許這個網站的通知，再重新整理。";
  } else if (await currentSubscription()) {
    state = "這台裝置已開啟通知。";
    actions = `<button type="button" class="secondary" data-push="test">送測試通知</button>
      <button type="button" class="plain" data-push="off">關閉這台的通知</button>`;
  } else {
    state = "LINE 事件失敗、回覆送不出去、推播額度快用完、Gemini 無法使用、匯率抓不到或有人一直猜密碼時，會通知這台裝置。";
    actions = '<button type="button" class="primary" data-push="on">開啟通知</button>';
  }
  return `<p class="small">${esc(state)}</p><p class="small muted">已開啟通知的裝置：${esc(devices)} 台。服務完全停止時無法通知，請另外用外部監控檢查 /healthz。</p>
    ${actions ? `<div class="actions">${actions}</div>` : ""}`;
}

async function enablePush() {
  if (await Notification.requestPermission() !== "granted") throw new Error("沒有取得通知權限");
  const { public_key: key } = await api("/api/admin/push", { token });
  const registration = await pushRegistration();
  await navigator.serviceWorker.ready;
  const subscription = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: base64Bytes(key) });
  await api("/api/admin/push/subscribe", { method: "POST", token, body: subscription.toJSON() });
}

async function disablePush() {
  const subscription = await currentSubscription();
  if (!subscription) return;
  await api("/api/admin/push/unsubscribe", { method: "POST", token, body: { endpoint: subscription.endpoint } });
  await subscription.unsubscribe();
}

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
      <span class="line-item">#${trip.id} · ${esc(trip.creator)} 建立 · ${trip.members} 人 · ${trip.expenses} 筆支出${trip.refunds ? ` · ${trip.refunds} 筆退款` : ""}${trip.deleted_expenses ? `（另 ${trip.deleted_expenses} 筆已刪除）` : ""}</span>
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
  const modelName = (item) => `<strong>${esc(item.label || item.model)}</strong>${item.label && item.label !== item.model ? `<span class="line-item">${esc(item.model)}</span>` : ""}`;
  const models = gemini.models.map((item) => row(modelName(item),
    item.missing && item.cooling_until ? '<span class="badge closed">找不到模型</span>'
      : item.cooling_until ? `<span class="badge closed">冷卻到 ${esc(new Date(item.cooling_until * 1000).toLocaleTimeString("zh-TW", { hour12: false }))}</span>` : '<span class="badge">可用</span>')).join("");
  const usage = gemini.usage.map((item) => row(`${modelName(item)}<span class="line-item">輸入 ${esc(item.input_tokens)} · 輸出 ${esc(item.output_tokens)} tokens</span>`,
    `<strong>${esc(item.calls)} 次</strong><span class="line-item">${item.cost_usd == null ? "無價格估算" : `約 US$${esc(item.cost_usd.toFixed(4))}`}</span>`)).join("");
  const alerts = data.alerts.map((item) => row(`<strong>${esc(item.title)}</strong><span class="line-item">${esc(item.body)}</span>`,
    `<span class="small">${esc(when(item.since * 1000))} 起</span>`)).join("");
  const daily = gemini.daily.map((item) => row(esc(item.day), `<strong>${esc(item.calls)} 次</strong>`)).join("");

  app.innerHTML = `<div class="intro"><p class="eyebrow">服務管理</p><h1>旅伴分帳管理頁</h1></div>
    <section class="card stats-metrics" aria-label="摘要">
      ${metric("進行中帳本", `${live.filter((trip) => trip.status === "open").length} / ${live.length}`)}
      ${metric("近 24 小時 LINE 事件", recent)}
      ${metric("待核對事件", line.failed.length)}
      ${metric("本月推播", quota)}
    </section>
    ${alerts ? section("目前的異常", alerts, "", "alerts") : ""}
    ${section("通知", await pushCard(data.push_devices), "", "push")}
    <section class="section"><div class="section-head"><h2>帳本</h2><span class="small muted">點開看明細與操作紀錄</span></div>
      <div class="stack">${live.map(tripCard).join("") || empty("目前沒有帳本。")}</div></section>
    ${removed.length ? `<section class="section"><div class="section-head"><h2>已刪除的帳本</h2></div><div class="stack">${removed.map(tripCard).join("")}</div></section>` : ""}
    ${section("LINE 待核對事件", failed || empty("目前沒有待核對事件。"), "", "line-failed")}
    ${section("LINE 狀態", lineRows, "", "line-status")}
    ${section("Gemini 模型", (gemini.configured ? models : empty("尚未設定 GEMINI_API_KEY。")) + (daily ? `<h3 class="section">最近 7 天</h3>${daily}` : ""), "", "gemini")}
    ${section("Gemini 用量 · 最近 30 天", usage || empty("目前沒有 Gemini 呼叫紀錄。"))}
    ${section("系統", [
      row("部署版本", `<strong>${esc(system.version)}</strong>`),
      row("啟動時間", esc(when(system.started_at))),
      row("Python", esc(system.python)),
      row("資料庫大小", esc(bytes(system.db_bytes))),
      row("匯率更新日", `${esc(system.fx_date || "尚未取得")}<span class="line-item">${esc(system.fx_source)}</span>`),
      row("操作紀錄", `${esc(system.audit_count)} 筆`),
    ].join(""), "", "system")}`;
  const target = location.hash && document.getElementById(location.hash.slice(1));
  if (target) target.scrollIntoView(); else window.scrollTo(0, 0);
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
  const button = event.target.closest("button[data-review], button[data-trip], button[data-back], button[data-restore], button[data-push]");
  if (!button) return;
  button.disabled = true;
  try {
    if (button.dataset.push === "test") {
      await api("/api/admin/push/test", { method: "POST", token });
      notice("已送出測試通知");
    } else if (button.dataset.push) {
      await (button.dataset.push === "on" ? enablePush() : disablePush());
      await dashboard();
      notice(button.dataset.push === "on" ? "已開啟通知" : "已關閉這台的通知");
    } else if (button.dataset.review) {
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
