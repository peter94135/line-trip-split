const CACHE = "trip-split-assets-v21";
const ASSETS = ["/assets/style.css?v=21", "/assets/common.js?v=21", "/assets/liff.js?v=21",
  "/assets/liff-fields.js?v=21", "/assets/liff-ledger.js?v=21", "/assets/liff-settings.js?v=21", "/assets/liff-shared.js?v=21"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((key) => key.startsWith("trip-split-assets-") && key !== CACHE)
    .map((key) => caches.delete(key)))).then(() => self.clients.claim()));
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin || !ASSETS.includes(url.pathname + url.search)) return;
  event.respondWith(caches.match(event.request).then((hit) => hit || fetch(event.request)));
});
