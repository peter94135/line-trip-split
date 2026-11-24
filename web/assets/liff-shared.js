import { api, esc } from "./common.js?v=21";

export const state = { token: "", config: null, listing: null, trip: null, tab: "ai", settleView: "settle", manualKind: "expense",
  stats: null, statsError: "", statsCategory: "", settlementDisplay: null, displayRequest: null,
  ai: { text: "", busy: "", error: "", result: null } };
export const icon = (name) => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
export const endpoint = (suffix = "") => `/api/trips/${state.trip.id}${suffix}`;
export const call = (path, options = {}) => api(path, { ...options, token: state.token });
export const isOpen = () => state.trip.status === "open";
export const nameOf = (id) => state.trip.members.find((member) => member.id === id)?.name || "成員";

export const statusBadge = (trip) => `<span class="badge ${trip.status === "closed" ? "closed" : ""}">${trip.status === "open" ? "進行中" : "已結束"}</span>`;
export const dates = (trip) => {
  const days = [trip.start_date, trip.end_date].filter(Boolean);
  const crossYear = days.length === 2 && days[0].slice(0, 4) !== days[1].slice(0, 4);
  return days.map(day => (crossYear ? day : day.slice(5)).replaceAll("-", "/")).join("–") || "日期未設定";
};
export const heading = (eyebrow, title, description = "") => `<div class="intro"><p class="eyebrow">${eyebrow}</p><h1>${esc(title)}</h1>${description ? `<p class="muted">${esc(description)}</p>` : ""}</div>`;
export const button = (label, action, style = "secondary", symbol = "", id = "") => `<button type="button" class="${style}" data-action="${action}" data-id="${id}">${symbol ? icon(symbol) : ""}${label}</button>`;
export const empty = (text) => `<div class="card empty">${esc(text)}</div>`;
export const categoryLabel = (category) => `<span class="category-label" data-category="${esc(category)}">
  <span class="category-swatch" aria-hidden="true"></span><span>${esc(category)}</span></span>`;

export function currencyField(label, name, value, readonly = false) {
  return `<label>${label}<input name="${name}" value="${esc(value)}" ${readonly ? "readonly" : 'list="currencies"'} required maxlength="3" pattern="[A-Za-z]{3}" autocapitalize="characters"></label>`;
}

export function currencyOptions() {
  return `<datalist id="currencies">${Object.entries(state.config.currencies).map(([code, label]) => `<option value="${esc(code)}">${esc(label)}</option>`).join("")}</datalist>`;
}

export function memberOptions(selected) {
  return state.trip.members.map((member) => `<option value="${member.id}" ${member.id === selected ? "selected" : ""}>${esc(member.name)}${member.active ? "" : "（已停用）"}</option>`).join("");
}
