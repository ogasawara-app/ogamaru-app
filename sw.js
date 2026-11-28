// Network first; cached data is explicitly identified as unverified.
const CACHE_NAME = 'ogamaru-app-v3';
const SHELL = ['./', './index.html', './manifest.json', './icon.jpg', './icon-192.png', './icon-512.png'];
const EXTERNAL = ['https://cdn.tailwindcss.com/3.4.17', 'https://unpkg.com/lucide@0.460.0/dist/umd/lucide.min.js'];
self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);
    await Promise.allSettled(SHELL.map(url => cache.add(url)));
    await self.skipWaiting();
  })());
});
self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) if (key.startsWith('ogamaru-app-') && key !== CACHE_NAME) await caches.delete(key);
    await self.clients.claim();
  })());
});
self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin && !EXTERNAL.includes(url.href)) return;
  const isData = /\/(schedule_data\.json|schedule_history\.csv)$/.test(url.pathname);
  const key = isData ? new URL(url.pathname, url.origin).href : req;
  event.respondWith((async () => {
    const cache = await caches.open(CACHE_NAME);
    try {
      const response = await fetch(req);
      if (response.ok || response.type === 'opaque') {
        try { await cache.put(key, response.clone()); } catch (_) {}
        return response;
      }
      if (!isData) return response;
    } catch (_) {}
    const cached = await cache.match(key) || (req.mode === 'navigate' ? await cache.match('./index.html') : null);
    if (!cached) return new Response('Offline', {status: 503});
    if (!isData) return cached;
    const headers = new Headers(cached.headers);
    headers.set('X-Ogamaru-Offline', '1');
    return new Response(cached.body, {status: 200, headers});
  })());
});
