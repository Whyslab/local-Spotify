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
const API_CACHE = "api-v1";  // списки, которые sw.js помнит для работы без сети
// Обложки и тексты скачанных треков: отдельно от общей памяти sw.js, которую
// та ограничивает по размеру, — иначе они вытеснялись бы вместе с прочим.
const OFFLINE_EXTRAS = "offline-covers-v1";
const COVER_SIZES = [96, 300, 600];  // те, что просят список, плеер и экран блокировки
const RECHECK_MS = 6 * 3600 * 1000;
const RECONCILED_KEY = "offlineReconciled";      // когда сверка прошла целиком
const LIBRARY_COUNT_KEY = "offlineLibraryCount"; // сколько треков было в фонотеке тогда
const OLD_CHECKED_KEY = "offlineChecked";        // отметки по трекам прежней сверки: стереть
const PENDING_PLAYS_KEY = "pendingPlays";
const PENDING_PLAYS_MAX = 500;

const offline = {
    supported: "caches" in window && "serviceWorker" in navigator && window.isSecureContext,
    paths: new Set(),     // что скачано
    running: null,        // {name, done, total, stop} — идущее скачивание подборки или фонотеки
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

/* Строки со скачанным треком помечаются — тем же проходом, что и играющая.
 * Проход по всем строкам дорог: при скачивании многих треков — раз на
 * MARKS_EVERY треков, а между ними обновляется только счётчик. */
const MARKS_EVERY = 10;

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
        // Отпечаток файла из строки фонотеки: по нему сверка видит замену и
        // перетегирование того же размера. У трека из подборки его нет —
        // тогда первая сверка сравнит размер и запишет отпечаток.
        stamp: track.stamp || "",
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

/* Скачанные треки строками очереди — для перемешивания без сети. `only` —
 * множество путей, из которых брать (очередь, подборка); без него — всё. */
async function downloadedTracks(only = null) {
    if (!offline.supported) return [];
    const metas = await caches.open(OFFLINE_META);
    const paths = [...offline.paths].filter(path => !only || only.has(path));
    // Разом, а не по одному: при скачанной фонотеке это тысячи ожиданий на айфоне.
    const found = await Promise.all(paths.map(async path => {
        const r = await metas.match(offlineMetaKey(path));
        const meta = r ? await r.json().catch(() => null) : null;
        return meta && { path, title: meta.title, artist: meta.artist, album: meta.album, duration: meta.duration };
    }));
    return found.filter(Boolean);
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

/* Скачанное сверяется с фонотекой одним запросом её списка (не чаще раза в
 * RECHECK_MS): трек удалили — копия уходит; заменили файл (другая версия,
 * перевод в Opus) или переписали теги — копия перекачивается. Замену видно по
 * отпечатку файла `stamp` в строке списка.
 *
 * Удаление — осторожно: трек пропадает из списка и когда фонотека недоступна
 * целиком или частично (диск не подключён, папку переименовывают), и одна
 * такая сверка стёрла бы с телефона всё. Поэтому: сервер сам должен сказать,
 * что фонотека в порядке; в ответе не меньше 90 % прошлого числа треков; трек
 * должен пропадать дольше MISSING_GRACE_MS (две сверки в разное время); и не
 * больше MAX_REMOVALS за проход. */
const MISSING_KEY = "offlineMissing";
const MISSING_GRACE_MS = 20 * 3600 * 1000;
const MAX_REMOVALS = 10;
const FULL_ANSWER = 0.9;

function readMap(key) {
    try { return JSON.parse(localStorage.getItem(key) || "{}"); } catch (e) { return {}; }
}

function writeMap(key, map) {
    try { localStorage.setItem(key, JSON.stringify(map)); } catch (e) { /* без памяти — проверим ещё раз */ }
}

function readNumber(key) {
    try { return Number(localStorage.getItem(key)) || 0; } catch (e) { return 0; }
}

async function libraryTrackCount() {
    try {
        const r = await fetch("/health", { headers: headers(), cache: "no-store" });
        if (!r.ok) return 0;
        const health = await r.json();
        return health.library === "ok" ? Number(health.tracks) || 0 : 0;
    } catch (e) {
        return 0;
    }
}

/* Вся фонотека одним ответом, мимо копии sw.js. null — не вышло. */
async function fetchLibraryRows() {
    try {
        const r = await fetch("/api/library?limit=100000&sort=name&fresh=1", { headers: headers(), cache: "no-store" });
        return r.ok ? await r.json() : null;
    } catch (e) {
        return null;
    }
}

async function hasExtras(path) {
    const extras = await caches.open(OFFLINE_EXTRAS);
    return Boolean(await extras.match(coverKey(path, 600)) || await extras.match(lyricsKey(path)));
}

let reconciling = false;

async function reconcileDownloads(force = false) {
    if (!offline.supported || reconciling || !token()) return;
    if (!force && Date.now() - readNumber(RECONCILED_KEY) < RECHECK_MS) return;
    reconciling = true;
    const missing = readMap(MISSING_KEY);
    let removed = 0;
    let finished = false;
    try {
        const healthy = await libraryTrackCount();
        if (!healthy) return;  // не сейчас: вывод «трека нет» был бы ложным
        const rows = await fetchLibraryRows();
        if (!rows) return;
        const byPath = new Map(rows.map(row => [row.path, row]));
        // Неполный ответ — не повод удалять: фонотека может быть видна частично.
        const full = rows.length >= FULL_ANSWER * Math.max(healthy, readNumber(LIBRARY_COUNT_KEY));
        const metas = await caches.open(OFFLINE_META);
        const current = player.queue[player.index];
        for (const path of [...offline.paths]) {
            if (offline.running) return;  // не мешать скачиванию
            const row = byPath.get(path);
            if (!row) {
                if (!full) continue;
                missing[path] = missing[path] || Date.now();
                if (Date.now() - missing[path] >= MISSING_GRACE_MS && removed < MAX_REMOVALS) {
                    await removeDownloaded(path);
                    delete missing[path];
                    removed += 1;
                }
                continue;
            }
            delete missing[path];
            let meta = {};
            try { meta = await (await metas.match(offlineMetaKey(path))).json(); } catch (e) { /* пусто */ }
            // Копия до отпечатков: сравнить размер; совпал — запомнить отпечаток.
            const changed = meta.stamp ? meta.stamp !== row.stamp : Boolean(meta.size && row.size !== meta.size);
            if (changed) {
                // Играющий сейчас — не трогать: его звук читается из этой копии.
                if (current && current.path === path && !player.audio.paused) continue;
                try {
                    await downloadTrack({ ...meta, ...row }, true);
                } catch (e) { /* в другой раз */ }
            } else if (!meta.stamp && row.stamp) {
                await metas.put(offlineMetaKey(path), new Response(JSON.stringify({ ...meta, stamp: row.stamp }), {
                    headers: { "Content-Type": "application/json" },
                }));
            } else if (!await hasExtras(path)) {
                await saveExtras(path);  // скачанное до обложек и текстов
            }
        }
        finished = true;
        try {
            localStorage.setItem(RECONCILED_KEY, String(Date.now()));
            if (full) localStorage.setItem(LIBRARY_COUNT_KEY, String(rows.length));
        } catch (e) { /* без памяти — сверим ещё раз */ }
    } finally {
        reconciling = false;
        // Отметки о треках, которых на устройстве больше нет, не копятся;
        // всё — одной записью за проход.
        for (const path of Object.keys(missing)) if (!offline.paths.has(path)) delete missing[path];
        writeMap(MISSING_KEY, missing);
        if (finished) {
            try { localStorage.removeItem(OLD_CHECKED_KEY); } catch (e) { /* не важно */ }
        }
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
            if (job.done % MARKS_EVERY === 0) refreshOfflineMarks();
            else renderPlaylistOffline();
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

/* ---------------- Вся фонотека на телефон ----------------
 *
 * По одному треку, как подборка: прерванное продолжается с места. Перед
 * стартом — две проверки, без которых на айфоне скачанное пропадёт или не
 * влезет: хранилище «надёжное» (persisted) и места хватает с запасом. */
const ROOM_MARGIN = 1.1;
const YIELD_MS = 500;
const YIELD_MAX_MS = 30000;

function setLibraryNote(text) {
    const note = document.getElementById("offlineLibraryNote");
    if (note) note.textContent = text;
}

async function storageKept() {
    try {
        if (!navigator.storage || !navigator.storage.persisted) return true;  // не узнать — не мешать
        return await navigator.storage.persisted();
    } catch (e) {
        return true;
    }
}

/* Сколько ещё можно положить, байт; null — браузер не говорит. */
async function freeRoom() {
    try {
        if (!navigator.storage || !navigator.storage.estimate) return null;
        const { quota, usage } = await navigator.storage.estimate();
        return quota ? quota - (usage || 0) : null;
    } catch (e) {
        return null;
    }
}

function gigabytes(bytes) { return (bytes / 1e9).toFixed(2) + " ГБ"; }

/* «Осталось ~N мин» по скорости уже скачанного; пусто, пока мерить не по чему. */
function libraryTimeLeft(job) {
    const elapsed = Date.now() - job.started;
    if (!job.bytes || elapsed <= 0) return "";
    const minutes = Math.max(1, Math.round((job.need - job.bytes) / (job.bytes / elapsed) / 60000));
    return `осталось ~${minutes} мин; держи приложение открытым — iOS в фоне не качает`;
}

/* Скачивание не отнимает сеть у играющего трека: пока тот ждёт данных, ждёт и оно. */
async function yieldToPlayer(job) {
    const until = Date.now() + YIELD_MAX_MS;
    while (!job.stop && Date.now() < until) {
        const a = player.audio;
        const src = a.getAttribute("src") || "";
        if (a.paused || a.readyState >= 3 || src.includes("sig=offline")) return;
        await new Promise(resolve => setTimeout(resolve, YIELD_MS));
    }
}

async function keepScreenOn(job) {
    try {
        if (navigator.wakeLock && !job.stop) job.lock = await navigator.wakeLock.request("screen");
    } catch (e) { /* нет — качаем и так */ }
}

/* Экран, погасший и включённый снова, отпускает блокировку — взять заново. */
document.addEventListener("visibilitychange", () => {
    const job = offline.running;
    if (job && job.library && document.visibilityState === "visible") keepScreenOn(job);
});

async function downloadLibrary(fitOnly = false) {
    if (!offline.supported || offline.running) return;
    const partial = document.getElementById("offlineLibraryPartial");
    if (partial) partial.hidden = true;
    await askToKeepStorage();
    if (!await storageKept()) {
        setLibraryNote("Браузер может стереть скачанное, поэтому всю фонотеку не качаю. На айфоне: «Поделиться» → "
            + "«На экран «Домой»», открыть плеер оттуда и нажать ещё раз.");
        return;
    }
    const rows = await fetchLibraryRows();
    if (!rows) { setLibraryNote("Сервер не ответил — попробуй позже."); return; }
    let tracks = rows.filter(row => !isDownloaded(row.path));
    let left = 0;  // треков, которым не хватило места
    let need = tracks.reduce((sum, row) => sum + (row.size || 0), 0);
    const room = await freeRoom();
    if (room !== null && room < need * ROOM_MARGIN) {
        // Сначала спросить: молча скачать половину и упереться — хуже.
        let budget = room / ROOM_MARGIN;
        const fit = [];
        for (const row of tracks) {
            if ((row.size || 0) > budget) continue;
            budget -= row.size || 0;
            fit.push(row);
        }
        if (!fitOnly) {
            setLibraryNote(`Всё не влезет: нужно ${gigabytes(need * ROOM_MARGIN)}, свободно ${gigabytes(room)}. `
                + `Влезет треков: ${fit.length} из ${tracks.length}.`);
            if (partial) partial.hidden = fit.length === 0;
            return;
        }
        left = tracks.length - fit.length;
        tracks = fit;
        need = fit.reduce((sum, row) => sum + (row.size || 0), 0);
    }
    if (!tracks.length) { setLibraryNote("Вся фонотека уже на телефоне."); return; }

    const job = { name: "", library: true, done: 0, total: tracks.length, failed: 0, stop: false,
        bytes: 0, need, started: Date.now(), lock: null };
    offline.running = job;
    setLibraryNote("");
    await keepScreenOn(job);
    renderOfflineCard();
    let full = false;
    try {
        for (const row of tracks) {
            if (job.stop) break;
            await yieldToPlayer(job);
            if (job.stop) break;
            try {
                await downloadTrack(row);
                job.bytes += row.size || 0;
            } catch (e) {
                if (isQuotaError(e)) { full = true; break; }
                job.failed += 1;
            }
            job.done += 1;
            if (job.done % MARKS_EVERY === 0) refreshOfflineMarks();
            else renderLibraryProgress(job);
        }
    } finally {
        offline.running = null;
        try { if (job.lock) await job.lock.release(); } catch (e) { /* уже отпущена */ }
        refreshOfflineMarks();
    }
    if (full) setLibraryNote("На телефоне кончилось место — скачано не всё, ничего не удалено.");
    else if (job.failed) setLibraryNote(`Не скачалось: ${job.failed}. Нажми ещё раз — докачает.`);
    else if (job.stop) setLibraryNote("");
    else if (left) setLibraryNote(`Готово: скачано ${job.done}, ещё ${left} не влезло.`);
    else setLibraryNote("Готово: вся фонотека на телефоне.");
}

function renderLibraryProgress(job) {
    const button = document.getElementById("offlineLibrary");
    if (!button) return;
    button.textContent = `Скачиваю ${job.done} из ${job.total} · стоп`;
    button.onclick = stopDownloading;
    button.disabled = false;
    setLibraryNote(libraryTimeLeft(job));
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
    if (job && job.name === pl.name && !job.library) {
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
    if (!job || (job.name === pl.name && !job.library)) button.disabled = false;
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
    facts.textContent = `Скачано треков: ${offline.paths.size}${used}`;
    // Надёжно ли хранится — всегда на виду: на айфоне от этого зависит, доживёт ли скачанное до завтра.
    let kept = null;
    try {
        if (navigator.storage && navigator.storage.persisted) kept = await navigator.storage.persisted();
    } catch (e) { /* не узнать */ }
    document.getElementById("offlineKept").textContent = kept === true
        ? "Хранится надёжно: браузер не сотрёт скачанное сам."
        : kept === false
            ? "Браузер может стереть скачанное, когда ему не хватит места или плеер долго не открывали. "
                + "На айфоне открой плеер с экрана «Домой» («Поделиться» → «На экран «Домой»») — "
                + "там хранилище надёжное. Что ещё проверить — docs/iphone-checklist.md."
            : "Браузер не говорит, надёжно ли хранится скачанное.";
    document.getElementById("offlineRemoveAll").disabled = offline.paths.size === 0;
    const job = offline.running;
    const button = document.getElementById("offlineLibrary");
    if (job && job.library) {
        renderLibraryProgress(job);
    } else {
        button.textContent = "Вся фонотека на телефон";
        button.onclick = () => downloadLibrary();
        button.disabled = Boolean(job);  // идёт подборка
    }
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
    /* Ответ сервера — уже доказательство связи, что бы ни говорил onLine. */
    if (!response && navigator.onLine === false) return;
    const after = response ? Number(response.headers.get("Retry-After")) : NaN;
    const backoff = PLAY_RETRY_MS * 2 ** Math.min(playRetryFailures, 4);
    playRetryFailures += 1;
    const delay = after > 0 ? Math.min(after * 1000, 10 * PLAY_RETRY_MS) : backoff;
    playRetryTimer = setTimeout(() => { playRetryTimer = null; flushPendingPlays(); }, delay);
}

/* Живое прослушивание дошло: сервер снова отвечает, накопленное уходит
 * сразу, а не по таймеру, который мог отодвинуться до 16 минут. */
function playAccepted() {
    playRetryFailures = 0;
    try { if (readPendingPlays().length) flushPendingPlays(); } catch (e) { /* без хранилища */ }
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
