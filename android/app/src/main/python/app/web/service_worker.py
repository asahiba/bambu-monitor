"""Service Worker（PWA 用）。

只做最小必要的事：缓存应用外壳（首页 + 图标），让「添加到主屏幕」后的启动更快、
断网时也能打开一个提示页；视频流与接口请求一律直接走网络，不做缓存。
"""

SERVICE_WORKER_JS = r"""/* 拓竹打印机监控台 · Service Worker */
const CACHE = "bambu-monitor-v1";
const SHELL = ["/", "/manifest.webmanifest", "/icon-192.png", "/icon-512.png"];

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE)
      .then(cache => cache.addAll(SHELL).catch(() => undefined))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", event => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== location.origin){
    return;
  }
  // 视频与控制接口不做缓存
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/stream/")){
    return;
  }
  if (url.pathname === "/" || SHELL.includes(url.pathname)){
    event.respondWith(
      fetch(event.request)
        .then(response => {
          const copy = response.clone();
          caches.open(CACHE).then(cache => cache.put(event.request, copy)).catch(() => undefined);
          return response;
        })
        .catch(() => caches.match(event.request).then(hit => hit || caches.match("/")))
    );
  }
});
"""
