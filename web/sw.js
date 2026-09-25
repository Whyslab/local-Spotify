/* Офлайн: то, что страница не может сделать сама, когда сети нет.
 *
 * Service worker стоит между страницей и сервером и отвечает за три вещи.
 *
 * 1. Сама страница (/, /static/*) — сначала сеть, без сети — последняя
 *    сохранённая копия. Сначала сеть, чтобы новая версия приходила сразу.
 * 2. Скачанные треки (/api/stream, /api/stream-url) — всегда с устройства:
 *    и без сети, и с ней (зачем качать второй раз). <audio> просит файл
 *    кусками (Range), и айфон без честного ответа 206 на кусок не играет
 *    вовсе — поэтому куски режутся здесь.
 * 3. Списки (фонотека, подборки, главная, обложки, тексты) — сначала сеть,
 *    без неё — то, что пришло в последний раз.
 *
 * Скачивает и удаляет треки страница (offline.js), здесь их только отдают.
 * Имена хранилищ общие с offline.js.
 */

const SHELL = "shell-v1";
const API = "api-v1";
const AUDIO = "offline-audio-v1";
const META = "offline-meta-v1";
const NETWORK_TIMEOUT_MS = 6000;

// Списки, которые стоит помнить на случай без сети. Только GET.
const REMEMBERED = [
    /^\/api\/library$/,
    /^\/api\/playlists$/,
    /^\/api\/playlists\/[^/]+\/tracks$/,
    /^\/api\/home$/,
    /^\/api\/cover$/,
    /^\/api\/lyrics$/,
];

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

function audioKey(path) { return "/offline/audio?path=" + encodeURIComponent(path); }
function metaKey(path) { return "/offline/meta?path=" + encodeURIComponent(path); }

self.addEventListener("fetch", (event) => {
    const request = event.request;
    if (request.method !== "GET") return;
    const url = new URL(request.url);
    if (url.origin !== self.location.origin) return;

    if (url.pathname === "/api/stream") {
        event.respondWith(serveStream(request, url));
    } else if (url.pathname === "/api/stream-url") {
        event.respondWith(serveStreamUrl(request, url));
    } else if (url.pathname === "/" || url.pathname.startsWith("/static/")) {
        event.respondWith(networkFirst(request, SHELL, true));
    } else if (REMEMBERED.some((re) => re.test(url.pathname))) {
        event.respondWith(networkFirst(request, API, false));
    }
});

async function serveStream(request, url) {
    const path = url.searchParams.get("path") || "";
    const cached = path ? await (await caches.open(AUDIO)).match(audioKey(path)) : null;
    if (!cached) return fetch(request);
    return rangeResponse(cached, request.headers.get("range"));
}

/* Ссылка на скачанный трек выдаётся здесь же: подпись серверу не нужна,
 * файл всё равно отдаст serveStream с устройства. Без сети сервер бы и не
 * ответил. */
async function serveStreamUrl(request, url) {
    const path = url.searchParams.get("path") || "";
    const meta = path ? await (await caches.open(META)).match(metaKey(path)) : null;
    if (!meta) return fetch(request);
    let info = {};
    try { info = await meta.json(); } catch (e) { /* пустая запись — без поправки */ }
    const far = Math.floor(Date.now() / 1000) + 30 * 24 * 3600;
    const body = {
        url: "/api/stream?path=" + encodeURIComponent(path) + "&exp=" + far + "&sig=offline",
        expires_at: far,
        gain: typeof info.gain === "number" ? info.gain : null,
    };
    return new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
}

async function rangeResponse(cached, range) {
    const blob = await cached.blob();
    const size = blob.size;
    const type = cached.headers.get("Content-Type") || "audio/mp4";
    const whole = () => new Response(blob, {
        status: 200,
        headers: { "Content-Type": type, "Content-Length": String(size), "Accept-Ranges": "bytes" },
    });
    const match = range ? /^bytes=(\d*)-(\d*)$/.exec(range.trim()) : null;
    if (!match || (match[1] === "" && match[2] === "")) return whole();
    let start;
    let end;
    if (match[1] === "") {
        const length = Number(match[2]);
        if (length === 0) return new Response(null, { status: 416, headers: { "Content-Range": `bytes */${size}` } });
        start = Math.max(0, size - length);
        end = size - 1;
    } else {
        start = Number(match[1]);
        end = match[2] === "" ? size - 1 : Math.min(Number(match[2]), size - 1);
    }
    if (start >= size || end < start) {
        return new Response(null, { status: 416, headers: { "Content-Range": `bytes */${size}` } });
    }
    return new Response(blob.slice(start, end + 1), {
        status: 206,
        headers: {
            "Content-Type": type,
            "Content-Length": String(end - start + 1),
            "Content-Range": `bytes ${start}-${end}/${size}`,
            "Accept-Ranges": "bytes",
        },
    });
}

function withTimeout(promise, ms) {
    return new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error("timeout")), ms);
        promise.then(
            (value) => { clearTimeout(timer); resolve(value); },
            (error) => { clearTimeout(timer); reject(error); },
        );
    });
}

/* Сеть, а при удаче — копия в хранилище. Без сети (или сеть молчит дольше
 * NETWORK_TIMEOUT_MS) — последняя копия. Ответ с ошибкой (401 после смены
 * токена, 500) не сохраняется и не подменяется старой копией: страница
 * должна увидеть, что что-то не так. */
async function networkFirst(request, cacheName, dropOldVersions) {
    const cache = await caches.open(cacheName);
    try {
        const response = await withTimeout(fetch(request), NETWORK_TIMEOUT_MS);
        if (response.ok && response.status === 200) {
            const copy = response.clone();
            if (dropOldVersions) await dropOtherVersions(cache, request.url);
            await cache.put(request, copy);
        }
        return response;
    } catch (error) {
        const cached = await cache.match(request);
        if (cached) return cached;
        throw error;
    }
}

/* /static/app.js?v=… после каждой новой версии — другой адрес. Старые
 * копии той же страницы убираются, чтобы хранилище не росло с каждым
 * обновлением. */
async function dropOtherVersions(cache, href) {
    const target = new URL(href);
    for (const key of await cache.keys()) {
        const other = new URL(key.url);
        if (other.pathname === target.pathname && other.search !== target.search) {
            await cache.delete(key);
        }
    }
}
