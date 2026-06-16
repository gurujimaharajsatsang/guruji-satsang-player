/* Guruji Ka Satsang Player — service worker
   ------------------------------------------------------------------
   Two jobs:
     1. App shell offline: keep index.html + tracks.json so the app
        opens with no internet. Network-first, so it always updates
        when online and never gets "stuck" on an old version.
     2. Offline audio: serve any track the user has DOWNLOADED from the
        cache (cache-first, with HTTP Range support so seeking works,
        including on iPhone Safari). Tracks are saved into AUDIO_CACHE
        by the page when the user taps Download.

   To force every device to refresh the app shell, bump SHELL_VER.
   AUDIO_CACHE is intentionally NOT versioned, so downloaded satsangs
   survive app updates.
*/
const SHELL_VER   = 'v1';
const SHELL_CACHE = 'satsang-shell-' + SHELL_VER;
const AUDIO_CACHE = 'satsang-audio';          // do not version — keeps downloads
const SHELL_ASSETS = ['./', './index.html', './tracks.json', './manifest.webmanifest'];

self.addEventListener('install', (e) => {
  self.skipWaiting();
  e.waitUntil(
    caches.open(SHELL_CACHE).then((c) => c.addAll(SHELL_ASSETS).catch(() => {}))
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(
      keys.filter((k) => k.startsWith('satsang-shell-') && k !== SHELL_CACHE)
          .map((k) => caches.delete(k))
    );
    await self.clients.claim();
  })());
});

// Let the page trigger an immediate activation after an update.
self.addEventListener('message', (e) => {
  if (e.data === 'skipWaiting') self.skipWaiting();
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  // Cross-origin (the audio + images on Cloudflare R2): cache-first.
  if (url.origin !== self.location.origin) {
    e.respondWith(serveFromAudioCache(req));
    return;
  }

  // Same-origin app shell: network-first, fall back to cache when offline.
  e.respondWith((async () => {
    try {
      const fresh = await fetch(req);
      if (fresh && fresh.ok &&
          (req.destination === 'document' || url.pathname.endsWith('tracks.json'))) {
        const c = await caches.open(SHELL_CACHE);
        c.put(req, fresh.clone());
      }
      return fresh;
    } catch (err) {
      const c = await caches.open(SHELL_CACHE);
      return (await c.match(req)) || (await c.match('./index.html')) || Response.error();
    }
  })());
});

// Serve a downloaded track from cache. If the track was not downloaded,
// fall through to the network (normal streaming).
async function serveFromAudioCache(req) {
  const cache = await caches.open(AUDIO_CACHE);
  // Tracks are stored under their plain URL (no Range header), so match the URL.
  let hit = await cache.match(req.url, { ignoreVary: true });
  if (!hit) {
    try { return await fetch(req); }
    catch (e) { return Response.error(); }
  }

  // Honour Range requests by slicing the cached full file -> 206 Partial.
  const range = req.headers.get('range');
  if (range && hit.status === 200) {
    const buf = await hit.arrayBuffer();
    const total = buf.byteLength;
    const m = /bytes=(\d+)-(\d*)/.exec(range);
    const start = m ? parseInt(m[1], 10) : 0;
    const end = (m && m[2]) ? parseInt(m[2], 10) : total - 1;
    if (start >= total) {
      return new Response(null, { status: 416,
        headers: { 'Content-Range': `bytes */${total}` } });
    }
    const chunk = buf.slice(start, end + 1);
    return new Response(chunk, {
      status: 206,
      headers: {
        'Content-Range': `bytes ${start}-${end}/${total}`,
        'Accept-Ranges': 'bytes',
        'Content-Length': String(chunk.byteLength),
        'Content-Type': hit.headers.get('Content-Type') || 'audio/mpeg'
      }
    });
  }
  return hit;
}
