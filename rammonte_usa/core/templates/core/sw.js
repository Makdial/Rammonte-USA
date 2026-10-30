{% load static %}/*
 * Fazakkir service worker.
 *
 * Keeps the app shell available offline and feeds the in-app audio player from
 * a local cache for any MP3 the user chose to "Save offline".
 */
const SHELL_CACHE = "fazakkir-shell-v1";
const MEDIA_CACHE = "fazakkir-media-v1";
const OFFLINE_URL = "{% url 'videos:offline' %}";

function asset(path) {
    return new URL(path, self.location.origin).href;
}

const SHELL_ASSETS = [
    "{% static 'css/variables.css' %}",
    "{% static 'css/base.css' %}",
    "{% static 'css/navbar.css' %}",
    "{% static 'css/messages.css' %}",
    "{% static 'css/buttons.css' %}",
    "{% static 'css/forms.css' %}",
    "{% static 'css/videos.css' %}",
    "{% static 'css/offline.css' %}",
    "{% static 'js/theme.js' %}",
    "{% static 'js/navbar.js' %}",
    "{% static 'js/offline.js' %}",
    "{% static 'icons/icon.svg' %}",
].map(asset);

self.addEventListener("install", (event) => {
    event.waitUntil(
        caches
            .open(SHELL_CACHE)
            .then((cache) => cache.addAll(SHELL_ASSETS.concat([OFFLINE_URL])))
            .then(() => self.skipWaiting())
    );
});

self.addEventListener("activate", (event) => {
    event.waitUntil(
        caches
            .keys()
            .then((keys) =>
                Promise.all(
                    keys
                        .filter((key) => key !== SHELL_CACHE && key !== MEDIA_CACHE)
                        .map((key) => caches.delete(key))
                )
            )
            .then(() => self.clients.claim())
    );
});

self.addEventListener("fetch", (event) => {
    const request = event.request;
    if (request.method !== "GET") return;

    const url = new URL(request.url);

    // Audio streamed through Django: serve saved files from the media cache.
    if (url.origin === self.location.origin && /\/audio-stream\/?$/.test(url.pathname)) {
        event.respondWith(serveMedia(request));
        return;
    }

    // Page navigations: go to the network, fall back to the offline library.
    if (request.mode === "navigate") {
        event.respondWith(
            fetch(request).catch(() =>
                caches.match(OFFLINE_URL).then((cached) => cached || caches.match(request))
            )
        );
        return;
    }

    // Static assets: always fresh when online, cached when offline.
    if (url.origin === self.location.origin && url.pathname.indexOf("/static/") === 0) {
        event.respondWith(networkFirst(request));
    }
});

async function serveMedia(request) {
    const cache = await caches.open(MEDIA_CACHE);
    const cached = await cache.match(request.url, { ignoreSearch: true });
    if (!cached) return fetch(request);
    return responseWithRange(request, cached);
}

/* The <audio> element asks for byte ranges; slice the cached body to match. */
async function responseWithRange(request, response) {
    const range = request.headers.get("range");
    if (!range) return response;

    const match = /^bytes=(\d+)-(\d*)$/.exec(range.trim());
    if (!match) return response;

    const buffer = await response.clone().arrayBuffer();
    const total = buffer.byteLength;
    const start = parseInt(match[1], 10);
    let end = match[2] ? parseInt(match[2], 10) : total - 1;

    if (isNaN(start) || start >= total) {
        return new Response(null, {
            status: 416,
            headers: { "Content-Range": "bytes */" + total },
        });
    }
    if (isNaN(end) || end >= total) end = total - 1;

    const slice = buffer.slice(start, end + 1);
    return new Response(slice, {
        status: 206,
        headers: {
            "Content-Type": response.headers.get("Content-Type") || "audio/mpeg",
            "Content-Range": "bytes " + start + "-" + end + "/" + total,
            "Content-Length": String(slice.byteLength),
            "Accept-Ranges": "bytes",
        },
    });
}

async function networkFirst(request) {
    const cache = await caches.open(SHELL_CACHE);
    try {
        const response = await fetch(request);
        if (response && response.ok && response.type === "basic") {
            cache.put(request, response.clone());
        }
        return response;
    } catch (error) {
        const cached = await cache.match(request, { ignoreSearch: true });
        if (cached) return cached;
        throw error;
    }
}
