export const $ = (selector, root = document) => root.querySelector(selector);
export const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[char]);

export const storage = {
  get(key) { try { return localStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch { /* WebView may disable storage. */ } },
  remove(key) { try { localStorage.removeItem(key); } catch { /* WebView may disable storage. */ } },
};

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

export async function api(path, { method = "GET", body, token, raw = false } = {}) {
  const headers = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new ApiError(error.detail || "連線失敗，請稍後重試。", response.status);
  }
  if (raw) return response;
  return response.status === 204 ? null : response.json();
}

let toastTimer;
export function notice(text) {
  const node = $("#notice");
  node.textContent = text;
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, 4000);
}

let undoFrame;
export function clearUndo() {
  try {
    if (!undoFrame) {
      undoFrame = document.createElement("iframe");
      undoFrame.hidden = true;
      undoFrame.tabIndex = -1;
      undoFrame.setAttribute("aria-hidden", "true");
      document.body.append(undoFrame);
    }
    undoFrame.srcdoc = `<!-- ${Date.now()} -->`;
  } catch { /* Keep normal editing available if WebView blocks the frame. */ }
}

document.addEventListener("submit", () => setTimeout(clearUndo, 0), true);
document.addEventListener("focusout", ({ target }) => {
  if (target.matches?.("input:not([type=checkbox]), textarea")) setTimeout(clearUndo, 0);
}, true);
