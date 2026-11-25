/* Офлайн: то, что страница не может сделать сама, когда сети нет.
 *
 * Service worker стоит между страницей и сервером и отвечает за три вещи.
 *
 * 1. Сама страница (/, /static/*) — сначала сеть, без сети — последняя
 *    сохранённая копия. Сначала сеть, чтобы новая версия приходила сразу.
 *    Файл с отпечатком (/static/app.js?v=…) не меняется никогда — его
 *    сначала из хранилища, сеть только когда копии нет.
 * 2. Скачанные треки (/api/stream, /api/stream-url) — всегда с устройства:
 *    и без сети, и с ней (зачем качать второй раз). <audio> просит файл
 *    кусками (Range), и айфон без честного ответа 206 на кусок не играет
 *    вовсе — поэтому куски режутся здесь.
 * 3. Списки (фонотека, подборки, главная, тексты) — сначала сеть, без неё —
 *    то, что пришло в последний раз. Обложки — сеть, без неё — копия
 *    скачанного трека.
 *
 * Скачивает и удаляет треки страница (offline.js), здесь их только отдают.
 * Имена хранилищ общие с offline.js.
 */

const SHELL = "shell-v1";
const API = "api-v1";
const AUDIO = "offline-audio-v1";
const META = "offline-meta-v1";
const COVERS = "offline-covers-v1";   // обложки скачанных треков — их не вытесняет API_MAX
const KNOWN = [SHELL, API, AUDIO, META, COVERS];
const NETWORK_TIMEOUT_MS = 6000;
/* Списки первого экрана (главная, подборки) — при копии на телефоне
 * ждём сеть меньше: проснувшийся туннель Tailscale молчит секундами, и
 * первый экран ждал все 6 с (журнал 24.11: dom@3432 → ready@9573). Чтение
 * перед правкой подборки ждёт полные 6 с (см. fetch ниже). */
const LIST_TIMEOUT_MS = 2500;
/* Фонотеки здесь нет: её копию страница рисует сама (app.js) и ждёт от сети
 * именно свежий ответ, чтобы заменить им копию. */
const FIRST_SCREEN = [/^\/api\/playlists$/, /^\/api\/playlists\/[^/]+\/tracks$/, /^\/api\/home$/];
/* Сама страница ждёт сеть недолго: запуск с иконки — это чёрный экран, пока
 * она не пришла, а туннель Tailscale после сна просыпается секундами (видео
 * 23.10: 2–4 с). Не успела — сохранённая копия, а свежая всё равно ложится в
 * хранилище и откроется в следующий раз. Файлы страницы — с отпечатком,
 * копия и её скрипты всегда одной версии. */
const PAGE_TIMEOUT_MS = 1000;
const API_MAX = 1500;                 // записей в API: старые вытесняются
const TRIM_EVERY = 50;                // обрезка API — раз на столько записей
let apiPuts = 0;
const SERVER_DOWN = new Set([502, 503, 504]);

// Списки, которые стоит помнить на случай без сети. Только GET.
const REMEMBERED = [
    /^\/api\/library$/,
    /^\/api\/playlists$/,
    /^\/api\/playlists\/[^/]+\/tracks$/,
    /^\/api\/home$/,
    /^\/api\/cover$/,
    /^\/api\/lyrics$/,
];

/* Страница сохраняется сразу при установке. Иначе первое открытие проходило
 * мимо: страница и её файлы грузились до того, как worker начинал отвечать, и
 * ярлык, открытый потом без сети, показывал «нет соединения». */
self.addEventListener("install", (event) => event.waitUntil(precacheShell().then(() => self.skipWaiting())));
self.addEventListener("activate", (event) => event.waitUntil((async () => {
    // Хранилища от прежних версий (v0, будущие v2) — не копить.
    for (const name of await caches.keys()) {
        if (!KNOWN.includes(name)) await caches.delete(name);
    }
    await self.clients.claim();
})()));

async function precacheShell() {
    try {
        const cache = await caches.open(SHELL);
        const page = await fetch("/", { cache: "no-store" });
        if (!page.ok) return;
        const html = await page.clone().text();
        await cache.put("/", page);
        const assets = new Set(["/static/manifest.webmanifest", "/static/icon-180.png", "/static/icon.svg"]);
        /* Шрифт подключает fonts.css, а не страница, — в разметке его ссылок
         * нет. Без него первый запуск без сети шёл бы запасным шрифтом. */
        for (const subset of ["latin", "latin-ext", "cyrillic", "cyrillic-ext"]) {
            assets.add(`/static/fonts/Onest-400_800-${subset}.woff2`);
        }
        for (const match of html.matchAll(/(?:src|href)="(\/static\/[^"]+)"/g)) assets.add(match[1]);
        for (const asset of assets) {
            try {
                const response = await fetch(asset, { cache: "no-store" });
                if (response.ok) {
                    await dropOtherVersions(cache, new URL(asset, self.location.origin).href);
                    await cache.put(asset, response);
                }
            } catch (e) { /* без этого файла — догрузится при следующем открытии */ }
        }
    } catch (e) {
        /* нет сети при установке — сохранится при следующем открытии */
    }
}

function audioKey(path) { return "/offline/audio?path=" + encodeURIComponent(path); }
function metaKey(path) { return "/offline/meta?path=" + encodeURIComponent(path); }

self.addEventListener("fetch", (event) => {
    const request = event.request;
    if (request.method !== "GET") return;
    const url = new URL(request.url);
    if (url.origin !== self.location.origin) return;
    // fresh=1 — спросить сам сервер, мимо скачанного (сверка копий в offline.js).
    if (url.searchParams.get("fresh") === "1") return;

    if (url.pathname === "/api/stream") {
        event.respondWith(serveStream(request, url));
    } else if (url.pathname === "/api/stream-url") {
        event.respondWith(serveStreamUrl(request, url));
    } else if (url.pathname.startsWith("/static/") && url.searchParams.has("v")) {
        event.respondWith(cacheFirst(event, request));
    } else if (url.pathname === "/") {
        event.respondWith(networkFirst(event, request, SHELL, true, PAGE_TIMEOUT_MS));
    } else if (url.pathname.startsWith("/static/")) {
        event.respondWith(networkFirst(event, request, SHELL, true));
    } else if (REMEMBERED.some((re) => re.test(url.pathname))) {
        // Поиск по фонотеке не помнится: каждое нажатие клавиши — своя запись.
        const search = url.pathname === "/api/library" && (url.searchParams.get("q") || "") !== "";
        /* X-Fresh — чтение перед правкой подборки (player.js): ему нужна
         * свежая ревизия, а не быстрая копия, иначе запись получит 409.
         * Свой заголовок, а не request.cache: его WebKit точно передаёт. */
        const quick = request.headers.get("X-Fresh") !== "1" && FIRST_SCREEN.some((re) => re.test(url.pathname));
        const wait = quick ? LIST_TIMEOUT_MS : NETWORK_TIMEOUT_MS;
        /* Обложка не запоминается в API: каждая смена трека писала её туда и
         * раз в 50 записей обходила 1500 ключей — очередь хранилища, в которой
         * стояли списки. Без сети обложка скачанного трека есть в COVERS. */
        const store = url.pathname === "/api/cover" ? null : API;
        event.respondWith(search ? fetch(request) : networkFirst(event, request, store, false, wait));
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

/* Сеть, а при удаче — копия в хранилище. Без сети, при ответе «сервер
 * недоступен» (502–504: так отвечает Tailscale, когда компьютер спит) или
 * если сеть молчит дольше таймаута — последняя копия. Ждать дольше
 * таймаута стоит, когда копии нет: тексты песен сервер ищет до 16 с, и
 * обрыв на 6-й превращал медленный ответ в ошибку. Ответ с ошибкой (401
 * после смены токена, 500) не сохраняется и не подменяется старой копией:
 * страница должна увидеть, что что-то не так.
 *
 * Хранилище — только когда сеть не ответила. Раньше копия искалась до
 * ответа сети, на каждом запросе: на айфоне со скачанными подборками
 * (3,5 ГБ в Cache API) первое обращение к хранилищу после запуска стоит
 * секунды — сервер ответил за 44 мс, а страница начала читаться на 2,5-й
 * секунде (журнал 24.11.2026). Пока сеть отвечает, хранилище не ждём. */
async function networkFirst(event, request, cacheName, dropOldVersions, timeoutMs = NETWORK_TIMEOUT_MS) {
    const started = Date.now();
    const network = fetch(request);
    /* Копия пишется после ответа, а не до него: страница ждала запись в
     * хранилище (и обход старых версий) на каждом списке. waitUntil держит
     * worker живым, пока запись не кончится. Копия снимается раньше, чем
     * страница начнёт читать ответ: этот обработчик стоит в очереди первым. */
    const saved = network.then((response) => {
        if (cacheName && response.ok && response.status === 200) {
            return remember(cacheName, request, response.clone(), dropOldVersions);
        }
        return undefined;
    }).catch(() => { /* без копии — сохранится в следующий раз */ });
    /* Из cacheFirst сюда приходят после await: спецификация это позволяет,
     * пока ответ странице не отдан, но если браузер строже — запись всё равно
     * идёт, а ответ странице не должен из-за этого сорваться. */
    try { event.waitUntil(saved); } catch (e) { /* см. выше */ }
    let early = null;
    try {
        early = await withTimeout(network, timeoutMs);
        if (!SERVER_DOWN.has(early.status)) {
            note(request, started, "net");
            return early;
        }
    } catch (error) { /* сеть молчит или её нет — к копии */ }
    /* Сеть не успела — что раньше: копия или всё-таки сеть. Поиск копии на
     * айфоне бывает секундами, и пришедший за это время свежий ответ лучше
     * старой копии. */
    const late = early ? Promise.resolve(undefined)
        : network.then((r) => (SERVER_DOWN.has(r.status) ? undefined : r), () => undefined);
    const found = await firstFound([
        findCopy(request, cacheName).then((c) => c && { copy: c }),
        late.then((r) => r && { fresh: r }),
    ]).catch(() => null);
    if (found && found.fresh) {
        note(request, started, "late");
        return found.fresh;
    }
    if (found) {
        note(request, started, early ? "down" : "copy");
        return cacheName === API ? markedCopy(found.copy) : found.copy;
    }
    // Копии нет — ждём сеть сколько потребуется (или отдаём её 502–504).
    note(request, started, "wait");
    return early || network;
}

/* Список из копии — с пометкой: страница (app.js) не считает такую главную
 * свежей и перечитывает её, когда сеть проснётся. */
async function markedCopy(copy) {
    const headers = new Headers(copy.headers);
    headers.set("X-Saved-Copy", "1");
    /* blob, а не поток copy.body: поток из Cache API в конструкторе — то, в чём
     * WebKit мог бы отказать, и тогда без сети вместо копии была бы ошибка. */
    return new Response(await copy.blob(), { status: copy.status, statusText: copy.statusText, headers });
}

/* Копия — в своём хранилище; обложка скачанного трека лежит ещё и в COVERS
 * (её не вытесняет API_MAX). Остальные хранилища — скачанный звук — не
 * трогаем: их обход на айфоне и стоил секунд. */
const WITH_DOWNLOADS = new Set(["/api/cover", "/api/lyrics"]);

async function findCopy(request, cacheName) {
    try {
        const own = cacheName ? await (await caches.open(cacheName)).match(request) : undefined;
        if (own) return own;
        // Обложки и тексты скачанных треков offline.js кладёт в COVERS: без
        // сети они должны найтись, даже если онлайн этот текст не открывали.
        if (WITH_DOWNLOADS.has(new URL(request.url).pathname)) {
            return await (await caches.open(COVERS)).match(request);
        }
    } catch (e) { /* хранилище недоступно — как будто копии нет */ }
    return undefined;
}

/* Как ответили страница и её первые запросы: страница спросит это при
 * запуске и допишет в строку «startup» журнала (app.js) — так видно, ждали ли
 * сеть или хранилище. Только первые NOTES_MAX за жизнь worker'а. */
const NOTES_MAX = 12;
const notes = [];
function note(request, started, how) {
    if (notes.length >= NOTES_MAX) return;
    const path = new URL(request.url).pathname;
    const name = path === "/" ? "page" : path.replace(/^\/(api|static)\//, "").split("/")[0].slice(0, 9);
    notes.push(`${name}:${how}${Date.now() - started}`);
}

self.addEventListener("message", (event) => {
    if (event.data !== "sw-notes" || !event.ports || !event.ports[0]) return;
    event.ports[0].postMessage(notes.join(","));
});

async function remember(cacheName, request, copy, dropOldVersions) {
    const cache = await caches.open(cacheName);
    if (dropOldVersions) await dropOtherVersions(cache, request.url);
    await cache.put(request, copy);
    /* Обход всех ключей — дорогой: не на каждую запись. Первая запись после
     * запуска worker'а обрезает всегда — айфон усыпляет его быстро, и счётчик
     * мог бы не дойти до TRIM_EVERY ни разу. */
    if (cacheName === API && apiPuts++ % TRIM_EVERY === 0) await trimCache(cache, API_MAX);
}

/* Файл с отпечатком: копия есть — она и есть ответ, сеть не нужна. Но копии
 * даётся только COPY_HEAD_START_MS форы: на айфоне первое обращение к
 * хранилищу после запуска бывает секундами (см. networkFirst), и тогда
 * файлы страницы идут по сети — что придёт раньше, то и ответ.
 *
 * Сеть в этой гонке считается, только если сервер подтвердил отпечаток
 * (immutable, StaticCaching в app.py): после выкатки старый ?v= приходит с
 * новым содержимым и no-cache — взяв его, страница собралась бы из файлов
 * разных версий, а запись под старым ключом стёрла бы новую копию.
 *
 * 50 мс, а не 300: на айфоне хранилище при запуске отвечало дольше 300 мс,
 * а сеть — за 20, и каждый файл ждал фору впустую (журнал 25.11:
 * app.js:slow306). Цена — ~250 КБ файлов по сети на холодный запуск. */
const COPY_HEAD_START_MS = 50;
async function cacheFirst(event, request) {
    const started = Date.now();
    const copy = findCopy(request, SHELL);
    const quick = await Promise.race([copy, sleep(COPY_HEAD_START_MS).then(() => undefined)]);
    if (quick) return quick;
    const raw = fetch(request);
    const confirmed = raw.then((response) => {
        if (!isConfirmedVersion(response)) return undefined;
        /* В хранилище — только если копии там правда нет: на айфоне сеть
         * выигрывает почти всегда, и каждый запуск переписывал бы все файлы
         * страницы (с обходом ключей) в и без того медленное хранилище. */
        const kept = response.clone();
        const saved = copy.then((found) => (found ? undefined : remember(SHELL, request, kept, true)));
        try { event.waitUntil(saved); } catch (e) { /* без копии */ }
        return response;
    }, () => undefined);
    const winner = await firstFound([copy, confirmed]).catch(() => undefined);
    if (winner) {
        note(request, started, "slow");
        return winner;
    }
    /* Ни копии, ни подтверждённой версии — сеть как есть, но не в хранилище. */
    note(request, started, "wait");
    return raw;
}

function isConfirmedVersion(response) {
    return response.status === 200 && /immutable/.test(response.headers.get("Cache-Control") || "");
}

function sleep(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }

/* Первый из обещанных ответов, который есть (не пустой и не ошибка). */
function firstFound(promises) {
    return new Promise((resolve, reject) => {
        let left = promises.length;
        const miss = () => { if (--left === 0) reject(new Error("nothing")); };
        for (const promise of promises) {
            promise.then((value) => (value ? resolve(value) : miss()), miss);
        }
    });
}

/* Старые записи — первыми: Cache API хранит их в порядке добавления. */
async function trimCache(cache, max) {
    try {
        const all = await cache.keys();
        /* Обложки в API больше не пишутся (см. fetch) — старые убираются здесь
         * же, обход ключей всё равно сделан. */
        const covers = [], keys = [];
        for (const key of all) (new URL(key.url).pathname === "/api/cover" ? covers : keys).push(key);
        await deleteAll(cache, covers.concat(keys.slice(0, Math.max(0, keys.length - max))));
    } catch (e) { /* не страшно */ }
}

/* Пачками, а не по одному: старых обложек в API на айфоне — тысячи, а
 * каждое обращение к Cache API там небыстрое. */
async function deleteAll(cache, keys) {
    for (let i = 0; i < keys.length; i += 50) {
        await Promise.all(keys.slice(i, i + 50).map(key => cache.delete(key)));
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
