/* Скачанное на этом устройстве: слушать без сети.
 *
 * Трек скачивается целиком и кладётся в хранилище браузера (Cache API);
 * отдаёт его оттуда sw.js — и без сети, и с ней. Страница здесь только
 * качает, удаляет и показывает, что скачано.
 *
 * Работает там, где страница открыта по HTTPS (через Tailscale) или с этого
 * же компьютера: service worker браузеры пускают только туда. По адресу
 * вида http://192.168.x.x:8787 кнопок скачивания нет.
 *
 * На айфоне держать плеер на экране «Домой»: у открытого просто в Safari
 * сайта iOS стирает хранилище, если им не пользовались неделю.
 */

const OFFLINE_AUDIO = "offline-audio-v1";
const OFFLINE_META = "offline-meta-v1";
// Обложки и тексты скачанных треков: отдельно от общей памяти sw.js, которую
// та ограничивает по размеру, — иначе они вытеснялись бы вместе с прочим.
const OFFLINE_EXTRAS = "offline-covers-v1";
const COVER_SIZES = [96, 300, 600];  // те, что просят список, плеер и экран блокировки
const RECHECK_MS = 24 * 3600 * 1000;
const CHECKED_KEY = "offlineChecked";
const PENDING_PLAYS_KEY = "pendingPlays";
const PENDING_PLAYS_MAX = 500;

const offline = {
    supported: "caches" in window && "serviceWorker" in navigator && window.isSecureContext,
    paths: new Set(),     // что скачано
    running: null,        // {name, done, total, stop} — идущее скачивание подборки
    epoch: 0,             // растёт при «удалить всё»: начатые до него скачивания не сохраняются
};

function offlineAudioKey(path) { return "/offline/audio?path=" + encodeURIComponent(path); }
function offlineMetaKey(path) { return "/offline/meta?path=" + encodeURIComponent(path); }
function coverKey(path, size) { return "/api/cover?path=" + encodeURIComponent(path) + "&size=" + size; }
function lyricsKey(path) { return "/api/lyrics?path=" + encodeURIComponent(path); }

function isDownloaded(path) { return offline.paths.has(path); }

async function loadOfflineIndex() {
    if (!offline.supported) return;
    try {
        const keys = await (await caches.open(OFFLINE_META)).keys();
        offline.paths = new Set(keys.map(r => new URL(r.url).searchParams.get("path")).filter(Boolean));
    } catch (e) {
        offline.paths = new Set();
    }
    refreshOfflineMarks();
}

/* Строки со скачанным треком помечаются — тем же проходом, что и играющая. */
function refreshOfflineMarks() {
    for (const row of document.querySelectorAll("[data-track-path]")) {
        row.classList.toggle("is-offline", offline.paths.has(row.dataset.trackPath));
    }
    renderPlaylistOffline();
    renderOfflineCard();
}

/* Один трек. Сначала звук, потом запись о нём: запись — признак того, что
 * трек скачан целиком. Удаление — в обратном порядке. */
async function downloadTrack(track, force = false) {
    if (!offline.supported || !track || isOutside(track)) return;
    if (isDownloaded(track.path) && !force) return;
    const epoch = offline.epoch;
    const stream = await streamUrlFor(track.path, true);
    // fresh=1 и здесь: при перекачке sw.js иначе отдал бы прежнюю копию.
    const response = await fetch(stream.url + "&fresh=1", { cache: "no-store" });
    if (!response.ok) throw new Error(`не скачался (${response.status})`);
    const blob = await response.blob();
    const expected = Number(response.headers.get("Content-Length"));
    if (expected && blob.size !== expected) throw new Error("скачался не целиком");
    // Пока качалось, скачанное могли удалить целиком — тогда не возвращать.
    if (epoch !== offline.epoch) return;
    const type = response.headers.get("Content-Type") || "audio/mp4";
    const audio = await caches.open(OFFLINE_AUDIO);
    await audio.put(offlineAudioKey(track.path), new Response(blob, {
        headers: { "Content-Type": type, "Content-Length": String(blob.size) },
    }));
    const meta = {
        path: track.path,
        title: track.title || "",
        artist: track.artist || "",
        album: track.album || "",
        duration: track.duration || null,
        gain: stream.gain,
        size: blob.size,
        at: Date.now(),
    };
    try {
        if (epoch !== offline.epoch) throw new Error("removed meanwhile");
        await (await caches.open(OFFLINE_META)).put(offlineMetaKey(track.path), new Response(JSON.stringify(meta), {
            headers: { "Content-Type": "application/json" },
        }));
    } catch (e) {
        await audio.delete(offlineAudioKey(track.path));  // без записи звук — сирота
        throw e;
    }
    if (epoch !== offline.epoch) {
        // «Удалить всё» пришлось ровно между проверкой и записью.
        await (await caches.open(OFFLINE_META)).delete(offlineMetaKey(track.path));
        await audio.delete(offlineAudioKey(track.path));
        return;
    }
    offline.paths.add(track.path);
    markChecked(track.path);
    await saveExtras(track.path);
}

/* Обложки и текст — чтобы без сети была картинка, а не буква, и слова. */
async function saveExtras(path) {
    const extras = await caches.open(OFFLINE_EXTRAS);
    const urls = COVER_SIZES.map(size => coverKey(path, size)).concat([lyricsKey(path)]);
    for (const url of urls) {
        try {
            const r = await fetch(url, { headers: headers(), cache: "no-store" });
            if (r.ok) await extras.put(url, r);
        } catch (e) { /* без неё */ }
    }
}

async function removeDownloaded(path) {
    if (!offline.supported) return;
    await (await caches.open(OFFLINE_META)).delete(offlineMetaKey(path));
    await (await caches.open(OFFLINE_AUDIO)).delete(offlineAudioKey(path));
    const extras = await caches.open(OFFLINE_EXTRAS);
    for (const url of COVER_SIZES.map(size => coverKey(path, size)).concat([lyricsKey(path)])) {
        await extras.delete(url);
    }
    offline.paths.delete(path);
    forgetOfflineLinks(path);
}

/* Ссылки на удалённую копию (sig=offline) сервер не пустит: приготовленная
 * для следующего трека выбрасывается, а играющий трек переходит на ссылку
 * сервера с того же места. */
function forgetOfflineLinks(path) {
    if (nextStream && (path === null || nextStream.path === path)) nextStream = null;
    const current = player.queue[player.index];
    const src = player.audio.getAttribute("src") || "";
    if (current && (path === null || current.path === path) && src.includes("sig=offline")) {
        reloadCurrentSource();
    }
}

/* Скачанное сверяется с фонотекой раз в сутки на трек: трек удалили —
 * копия уходит; заменили файл (другая версия, перевод в Opus) — копия
 * перекачивается. Размер файла спрашивается одним байтом.
 *
 * Удаление — осторожно: 404 бывает и когда вся фонотека недоступна (диск не
 * подключён, папку переименовывают), и одна такая сверка стёрла бы с телефона
 * всё. Поэтому: сервер сам должен сказать, что фонотека в порядке; трек должен
 * пропадать дольше суток (две сверки в разные дни); и не больше
 * MAX_REMOVALS за проход. */
const MISSING_KEY = "offlineMissing";
const MISSING_GRACE_MS = 20 * 3600 * 1000;
const MAX_REMOVALS = 10;

function readMap(key) {
    try { return JSON.parse(localStorage.getItem(key) || "{}"); } catch (e) { return {}; }
}

function writeMap(key, map) {
    try { localStorage.setItem(key, JSON.stringify(map)); } catch (e) { /* без памяти — проверим ещё раз */ }
}

function markChecked(path) {
    const checked = readMap(CHECKED_KEY);
    checked[path] = Date.now();
    writeMap(CHECKED_KEY, checked);
}

/* Записи о треках, которых на устройстве больше нет, не копятся. */
function pruneMarks() {
    for (const key of [CHECKED_KEY, MISSING_KEY]) {
        const map = readMap(key);
        for (const path of Object.keys(map)) if (!offline.paths.has(path)) delete map[path];
        writeMap(key, map);
    }
}

async function libraryIsHealthy() {
    try {
        const r = await fetch("/health", { headers: headers(), cache: "no-store" });
        if (!r.ok) return false;
        const health = await r.json();
        return health.library === "ok" && Number(health.tracks) > 0;
    } catch (e) {
        return false;
    }
}

async function hasExtras(path) {
    const extras = await caches.open(OFFLINE_EXTRAS);
    return Boolean(await extras.match(coverKey(path, 600)) || await extras.match(lyricsKey(path)));
}

let reconciling = false;

async function reconcileDownloads() {
    if (!offline.supported || reconciling || !token()) return;
    reconciling = true;
    const checked = readMap(CHECKED_KEY);
    const missing = readMap(MISSING_KEY);
    let removed = 0;
    try {
        if (!await libraryIsHealthy()) return;  // не сейчас: вывод «трека нет» был бы ложным
        const metas = await caches.open(OFFLINE_META);
        for (const path of [...offline.paths]) {
            if (offline.running) break;  // не мешать скачиванию подборки
            if (Date.now() - (checked[path] || 0) < RECHECK_MS) continue;
            let stream;
            try {
                stream = await streamUrlFor(path, true);
            } catch (e) {
                if (e.status !== 404) break;  // сервер недоступен — в другой раз
                missing[path] = missing[path] || Date.now();
                writeMap(MISSING_KEY, missing);
                if (Date.now() - missing[path] >= MISSING_GRACE_MS && removed < MAX_REMOVALS) {
                    await removeDownloaded(path);
                    removed += 1;
                }
                continue;
            }
            delete missing[path];
            writeMap(MISSING_KEY, missing);
            const probe = await fetch(stream.url + "&fresh=1", { headers: { Range: "bytes=0-0" }, cache: "no-store" })
                .catch(() => null);
            if (!probe || !probe.ok) break;
            const total = Number(((probe.headers.get("Content-Range") || "").split("/")[1]) || 0);
            let meta = {};
            try { meta = await (await metas.match(offlineMetaKey(path))).json(); } catch (e) { /* пусто */ }
            const current = player.queue[player.index];
            const playing = current && current.path === path && !player.audio.paused;
            if (total && meta.size && total !== meta.size) {
                // Играющий сейчас — не трогать: его звук читается из этой копии.
                if (playing) continue;
                try {
                    await downloadTrack({ ...meta, path }, true);
                } catch (e) { continue; }
            } else if (!await hasExtras(path)) {
                await saveExtras(path);  // скачанное до обложек и текстов
            }
            markChecked(path);
        }
    } finally {
        reconciling = false;
        pruneMarks();
        refreshOfflineMarks();
    }
}

async function askToKeepStorage() {
    try {
        if (navigator.storage && navigator.storage.persist) await navigator.storage.persist();
    } catch (e) { /* не дали — работает и так, но iOS может стереть */ }
}

function isQuotaError(e) {
    return e && (e.name === "QuotaExceededError" || /quota/i.test(String(e.message)));
}

/* Подборка целиком, по одному треку: так прогресс честный, а прерванное
 * скачивание продолжается с места — скачанные пропускаются. */
async function downloadPlaylist() {
    const pl = player.playlist;
    if (!offline.supported || !pl || offline.running) return;
    await askToKeepStorage();
    const tracks = uniqueTracks(pl.entries).filter(t => !isOutside(t));
    const job = { name: pl.name, done: 0, total: tracks.length, failed: 0, stop: false };
    offline.running = job;
    renderPlaylistOffline();
    try {
        for (const track of tracks) {
            if (job.stop) break;
            if (!isDownloaded(track.path)) {
                try {
                    await downloadTrack(track);
                } catch (e) {
                    if (isQuotaError(e)) {
                        setOfflineNote("На устройстве кончилось место — скачано не всё.");
                        break;
                    }
                    job.failed += 1;
                }
            }
            job.done += 1;
            refreshOfflineMarks();
        }
    } finally {
        offline.running = null;
        refreshOfflineMarks();
    }
    if (job.failed) setOfflineNote(`Не скачалось: ${job.failed}. Нажми ещё раз — докачает.`);
    else if (!job.stop) setOfflineNote("");
}

function stopDownloading() {
    if (offline.running) offline.running.stop = true;
}

async function removePlaylistDownloads() {
    const pl = player.playlist;
    if (!pl) return;
    if (offline.running && offline.running.name === pl.name) stopDownloading();
    for (const track of uniqueTracks(pl.entries)) await removeDownloaded(track.path);
    refreshOfflineMarks();
}

async function removeAllDownloads() {
    if (!offline.supported) return;
    offline.epoch += 1;  // начатые скачивания не сохранят своё
    stopDownloading();
    offline.paths = new Set();
    await caches.delete(OFFLINE_META);
    await caches.delete(OFFLINE_AUDIO);
    await caches.delete(OFFLINE_EXTRAS);
    forgetOfflineLinks(null);
    refreshOfflineMarks();
}

async function toggleTrackDownload(track) {
    try {
        if (isDownloaded(track.path)) await removeDownloaded(track.path);
        else { await askToKeepStorage(); await downloadTrack(track); }
    } catch (e) {
        setOfflineNote(isQuotaError(e) ? "На устройстве кончилось место." : `«${track.title}»: ${e.message}`);
    }
    refreshOfflineMarks();
}

function uniqueTracks(entries) {
    const seen = new Set();
    const out = [];
    for (const entry of entries) {
        if (!entry || !entry.path || seen.has(entry.path)) continue;
        seen.add(entry.path);
        out.push(entry);
    }
    return out;
}

function setOfflineNote(text) {
    const note = document.getElementById("playlistNote");
    if (note) note.textContent = text;
}

/* Кнопка в шапке подборки: сколько скачано и что можно сделать. */
function renderPlaylistOffline() {
    const button = document.getElementById("playlistOffline");
    if (!button) return;
    const pl = player.playlist;
    button.hidden = !offline.supported || !pl;
    if (button.hidden) return;
    const tracks = uniqueTracks(pl.entries).filter(t => !isOutside(t));
    const have = tracks.filter(t => isDownloaded(t.path)).length;
    const job = offline.running;
    if (job && job.name === pl.name) {
        button.textContent = `Скачиваю ${job.done} из ${job.total} · стоп`;
        button.onclick = stopDownloading;
    } else if (tracks.length && have === tracks.length) {
        button.textContent = "На телефоне ✓ · убрать";
        button.onclick = removePlaylistDownloads;
    } else {
        button.textContent = have ? `На телефон (${have} из ${tracks.length})` : "На телефон";
        button.onclick = downloadPlaylist;
        button.disabled = Boolean(job);
    }
    if (!job || job.name === pl.name) button.disabled = false;
}

/* Карточка в «Сервисе»: сколько скачано и сколько это места. */
async function renderOfflineCard() {
    const card = document.getElementById("offlineCard");
    if (!card) return;
    card.hidden = !offline.supported;
    if (!offline.supported) return;
    const facts = document.getElementById("offlineFacts");
    let used = "";
    try {
        const estimate = navigator.storage && navigator.storage.estimate ? await navigator.storage.estimate() : null;
        if (estimate && estimate.usage) used = ` · занято ${(estimate.usage / 1e9).toFixed(2)} ГБ`;
    } catch (e) { /* без цифр */ }
    let kept = "";
    try {
        if (navigator.storage && navigator.storage.persisted) {
            kept = (await navigator.storage.persisted()) ? " · хранится надёжно" : " · браузер может стереть при нехватке места";
        }
    } catch (e) { /* без этого */ }
    facts.textContent = `Скачано треков: ${offline.paths.size}${used}${kept}`;
    document.getElementById("offlineRemoveAll").disabled = offline.paths.size === 0;
}

/* ---------------- Прослушивания без сети ----------------
 *
 * Журнал прослушиваний без сети не отправить, а терять его жалко: по нему
 * умное перемешивание решает, что давно не звучало. Неотправленное копится
 * здесь и уходит, когда сеть вернётся. */
function queuePlay(body, response) {
    try {
        const pending = JSON.parse(localStorage.getItem(PENDING_PLAYS_KEY) || "[]");
        pending.push(body);
        localStorage.setItem(PENDING_PLAYS_KEY, JSON.stringify(pending.slice(-PENDING_PLAYS_MAX)));
    } catch (e) { return; /* приватное окно — без журнала */ }
    retryPendingPlaysLater(response);
}

/* Повтор по таймеру, а не только по «online»: сервер мог не принять при живой
 * сети (предел 60 прослушиваний в минуту — 429, компьютер спит — 502), и
 * события «online» тогда не будет. Один таймер на всё; Retry-After сервера,
 * если он есть, — иначе минута, и вдвое дольше после каждой новой неудачи
 * (до 16 минут). Без сети таймер не нужен — придёт «online»; отказ в доступе
 * (сменили токен) таймером не лечится — отправка ждёт входа (saveToken). */
let PLAY_RETRY_MS = 60 * 1000;
let playRetryTimer = null;
let playRetryFailures = 0;

function retryPendingPlaysLater(response) {
    clearTimeout(playRetryTimer);
    playRetryTimer = null;
    if (response && (response.status === 401 || response.status === 403)) return;
    if (navigator.onLine === false) return;
    const after = response ? Number(response.headers.get("Retry-After")) : NaN;
    const backoff = PLAY_RETRY_MS * 2 ** Math.min(playRetryFailures, 4);
    playRetryFailures += 1;
    const delay = after > 0 ? Math.min(after * 1000, 10 * PLAY_RETRY_MS) : backoff;
    playRetryTimer = setTimeout(() => { playRetryTimer = null; flushPendingPlays(); }, delay);
}

function readPendingPlays() {
    return JSON.parse(localStorage.getItem(PENDING_PLAYS_KEY) || "[]");
}

let flushingPlays = false;

async function flushPendingPlays() {
    if (flushingPlays || !token()) return;
    let pending;
    try { pending = readPendingPlays(); } catch (e) { return; }
    if (!pending.length) return;
    flushingPlays = true;
    try {
        while (pending.length) {
            const item = pending[0];
            const r = await fetch("/api/plays", {
                method: "POST",
                headers: { ...headers(), "Content-Type": "application/json" },
                body: JSON.stringify(item),
            });
            /* Сервер не принял — повтор позже; 422 — битая запись, выбросить. */
            if (!r.ok && r.status !== 422) { retryPendingPlaysLater(r); break; }
            playRetryFailures = 0;
            /* Перечитать: пока шёл запрос, queuePlay мог дописать новое. */
            pending = readPendingPlays();
            if (pending.length && JSON.stringify(pending[0]) === JSON.stringify(item)) pending.shift();
            localStorage.setItem(PENDING_PLAYS_KEY, JSON.stringify(pending));
        }
    } catch (e) {
        retryPendingPlaysLater();  /* сети всё ещё нет */
    } finally {
        flushingPlays = false;
    }
}

window.addEventListener("online", () => { flushPendingPlays(); reconcileDownloads(); });

(function initOffline() {
    if (offline.supported) {
        navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => { offline.supported = false; });
        loadOfflineIndex().then(() => setTimeout(reconcileDownloads, 15000));
    }
    flushPendingPlays();
})();
