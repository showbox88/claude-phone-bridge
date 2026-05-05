// Service Worker: handle web push + notification clicks.
// No offline caching — the bridge is useless without the server anyway.

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
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
