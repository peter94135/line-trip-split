// 管理頁通知用的 service worker，只處理推播，不快取任何檔案。
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { data = { body: event.data?.text() }; }
  event.waitUntil(self.registration.showNotification(data.title || "旅伴分帳", {
    body: data.body || "",
    tag: data.tag || "admin",
    renotify: true,
    icon: "/assets/apple-touch-icon.png?v=21",
    data: { url: data.url || "/admin" },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = new URL(event.notification.data?.url || "/admin", self.location.origin);
  if (target.origin !== self.location.origin) return;
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const open = windows.find((client) => new URL(client.url).pathname === "/admin");
    if (open) { await open.focus(); return open.navigate(target.href); }
    return self.clients.openWindow(target.href);
  })());
});
