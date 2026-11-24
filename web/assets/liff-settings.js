import { $, esc, notice } from "./common.js?v=21";
import { state, isOpen, dates, button, nameOf, currencyField, currencyOptions } from "./liff-shared.js?v=21";

const dialog = $("#dialog");

function memberList() {
  const trip = state.trip;
  const rows = trip.members.map((member) => `<div class="row list-row"><span><strong>${esc(member.name)}</strong>${member.id === state.listing.member_id ? " · 你" : ""}
    <span class="line-item">權重 ${esc(member.weight)}${member.organizer ? " · 建立者" : ""}${member.active ? "" : " · 已停用"}</span></span>
    <div class="actions">
      ${isOpen() && (trip.can_manage || member.id === state.listing.member_id) ? button("編輯", "edit-member", "plain", "", member.id) : ""}
      ${member.id === state.listing.member_id ? button("收款方式", "edit-payment-note", "plain", "", member.id) : ""}
      ${trip.can_manage && member.can_delete ? button("刪除", "delete-member", "danger", "", member.id) : ""}
    </div></div>`).join("");
  return `<div class="section-head"><h2>成員</h2>${isOpen() && trip.can_manage ? button("新增成員", "add-member", "secondary", "plus") : ""}</div>
    <div class="card">${rows}</div>${trip.invite_url ? `<section class="section"><div class="card stack"><h2>邀請旅伴</h2>
    <p class="small muted">分享連結，旅伴用 LINE 登入即可加入帳本。</p>${button(canShare() ? "用 LINE 分享邀請" : "複製邀請連結", "invite")}</div></section>` : ""}`;
}

export function rateRow(code = "", rate = "") {
  return `<div class="rate-row"><label>原幣<input list="currencies" required maxlength="3" pattern="[A-Za-z]{3}" value="${esc(code)}" data-code></label>
    <label>匯率<input type="number" required step="any" min="0.000000000001" inputmode="decimal" value="${esc(rate)}" data-rate></label>
    ${button("移除", "remove-rate", "plain")}</div>`;
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
    <label>時區<select name="timezone">${[...new Set([trip.timezone, "UTC", ...(Intl.supportedValuesOf?.("timeZone") || ["Asia/Taipei", "Asia/Tokyo", "Asia/Seoul", "Europe/London", "America/New_York"])])].map(zone =>
      `<option ${zone === trip.timezone ? "selected" : ""}>${esc(zone)}</option>`).join("")}</select></label>
    <div class="fields">${currencyField("記帳幣別", "default_currency", trip.default_currency)}${currencyField("結算幣別", "settle_currency", trip.settle_currency, trip.has_records)}</div>
    ${currencyOptions()}
    ${trip.has_records ? '<p class="muted small">帳本已有紀錄，結算幣別已鎖定。</p>' : ""}
    <fieldset class="stack"><legend>固定匯率</legend><p class="small muted">每 1 單位原幣可換多少結算幣。已換算的紀錄保留原匯率。</p>
      <div id="rates" class="stack">${Object.entries(trip.fixed_rates).map(([code, rate]) => rateRow(code, rate)).join("")}</div>
      ${button("新增匯率", "add-rate", "secondary", "plus")}</fieldset>
    <label>預設付款人<select name="default_payer_id"><option value="0">記帳的人</option>${trip.members.filter((member) => member.active)
      .map((member) => `<option value="${member.id}" ${member.id === trip.default_payer_id ? "selected" : ""}>${esc(member.name)}</option>`).join("")}</select></label>
    <p class="muted small">在 LINE 或網頁記帳沒說誰付時記在這個人身上，適合由一人先付或保管公費。說「我付」或請客時仍記在自己身上。</p>
    ${trip.line_group ? `<label class="check"><input type="checkbox" name="notify_web_changes" ${trip.notify_web_changes ? "checked" : ""}>網頁記帳或修改時通知 LINE 群組</label>
    <p class="muted small">每則通知會依群組人數用掉推播額度（每月 200 則）。在群組直接傳訊息記帳不受影響。</p>` : ""}
    <p class="error" role="alert" hidden></p><div class="actions"><button type="submit" class="primary">儲存設定</button></div>
  </form><section class="card section stack"><h2>結束帳本</h2><p class="small muted">結束後停止新增支出，旅伴仍可確認還款。</p>${button("結束帳本", "close-trip", "danger")}</section>`;
}

export function tripView() {
  return `<section>${memberList()}</section><section class="section">${settingsView()}</section>`;
}

function canShare() {
  return window.liff?.isApiAvailable?.("shareTargetPicker");
}

export async function invite() {
  const link = new URL(state.trip.invite_url, location.href).href;
  if (canShare()) {
    try {
      const result = await window.liff.shareTargetPicker([{ type: "text", text: `一起加入「${state.trip.name}」的旅程帳本：\n${link}` }]);
      if (result?.status === "success") notice("已分享帳本邀請");
      return;
    } catch {
      notice("未能開啟 LINE 分享，可改用邀請連結");
    }
  }
  try { await navigator.clipboard.writeText(link); notice("已複製邀請連結"); }
  catch {
    dialog.innerHTML = `<div class="dialog-head"><h2 id="dialog-title">邀請連結</h2>${button("關閉", "dismiss", "plain")}</div>
      <div class="form"><label>選取後複製<input value="${esc(link)}" readonly></label></div>`;
    dialog.showModal();
    $("input", dialog).select();
  }
}
