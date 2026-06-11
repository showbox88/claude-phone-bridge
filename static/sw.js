// Service Worker: precache static assets + handle web push + notification clicks.
//
// Phase 4 Task 19: precache the full ES-module + CSS manifest so the PWA
// works through brief network blips. Cache-first for /static/* assets,
// network-first for everything else (so API + WS aren't affected).

const CACHE_VERSION = "v50";
const CACHE_NAME = `phone-bridge-${CACHE_VERSION}`;

const ASSETS = [
  '/',
  '/manifest.json',
  '/static/icon.svg',
  '/static/icons.js?v=49',
  '/static/marked.min.js',
  '/static/vendor/purify.min.js?v=49',
  '/static/app.js?v=49',

  // JS modules
  '/static/js/boot.js',
  '/static/js/state.js',
  '/static/js/dom.js',
  '/static/js/api.js',
  '/static/js/util/escape.js',
  '/static/js/util/format.js',
  '/static/js/util/timers.js',
  '/static/js/util/yaml.js',
  '/static/js/util/dialog.js',
  '/static/js/ws/socket.js',
  '/static/js/ws/handlers.js',
  '/static/js/render/markdown.js',
  '/static/js/render/scroll.js',
  '/static/js/render/typing.js',
  '/static/js/render/message.js',
  '/static/js/render/tool.js',
  '/static/js/render/perm.js',
  '/static/js/render/checkin-card.js',
  '/static/js/session/header.js',
  '/static/js/session/list.js',
  '/static/js/session/drawer.js',
  '/static/js/composer/input.js',
  '/static/js/composer/attachments.js',
  '/static/js/composer/send.js',
  '/static/js/features/sources.js',
  '/static/js/features/checkin.js',
  '/static/js/features/cwd-browser.js',
  '/static/js/features/usage.js',
  '/static/js/features/weekly-report.js',
  '/static/js/features/sync-settings.js',
  '/static/js/features/bell.js',

  // CSS
  '/static/css/tokens.css?v=49',
  '/static/css/base.css?v=49',
  '/static/css/utilities.css?v=49',
  '/static/css/layout.css?v=49',
  '/static/css/appbar.css?v=49',
  '/static/css/drawer.css?v=49',
  '/static/css/messages.css?v=49',
  '/static/css/tools-perms.css?v=49',
  '/static/css/composer.css?v=49',
  '/static/css/picker.css?v=49',
  '/static/css/dialogs/checkin.css?v=49',
  '/static/css/dialogs/usage.css?v=49',
  '/static/css/dialogs/sync.css?v=49',
  '/static/css/dialogs/weekly.css?v=49',
  '/static/css/dialogs/cwd.css?v=49',
  '/static/css/dialogs/bell.css?v=49',
];

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    try {
      const cache = await caches.open(CACHE_NAME);
      // addAll is atomic — a single 404 fails the whole batch. Use
      // allSettled so a stray miss doesn't tank the install.
      await Promise.allSettled(ASSETS.map((u) => cache.add(u)));
    } catch (_) { /* offline first install — fetch handler still works */ }
    self.skipWaiting();
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    // Drop old versioned caches.
    const names = await caches.keys();
    await Promise.all(
      names
        .filter((n) => n.startsWith('phone-bridge-') && n !== CACHE_NAME)
        .map((n) => caches.delete(n))
    );
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Only intercept /static/* and the bare '/' shell.
  const isStatic = url.pathname.startsWith('/static/');
  const isShell = url.pathname === '/' || url.pathname === '/manifest.json';
  if (!isStatic && !isShell) return;

  event.respondWith((async () => {
    const cache = await caches.open(CACHE_NAME);
    const cached = await cache.match(req, { ignoreSearch: false });
    if (cached) return cached;
    try {
      const res = await fetch(req);
      if (res && res.ok && res.type === 'basic') {
        cache.put(req, res.clone());
      }
      return res;
    } catch (e) {
      // Last-ditch: try ignoring the ?v= query so a stale cached version
      // is still better than a hard fail.
      const loose = await cache.match(req, { ignoreSearch: true });
      if (loose) return loose;
      throw e;
    }
  })());
});

self.addEventListener('push', (event) => {
  let data = { title: 'Claude', body: '', tag: undefined };
  if (event.data) {
    try { data = Object.assign(data, event.data.json()); }
    catch (_) { data.body = event.data.text(); }
  }
  event.waitUntil(self.registration.showNotification(data.title, {
    body: data.body || '',
    tag: data.tag,
    icon: '/icon.svg',
    badge: '/icon.svg',
    requireInteraction: true,
    vibrate: [120, 60, 120],
    data: { tag: data.tag },
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const w of wins) {
      if ('focus' in w) return w.focus();
    }
    if (self.clients.openWindow) return self.clients.openWindow('/');
  })());
});
