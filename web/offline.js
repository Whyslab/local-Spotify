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
const PENDING_PLAYS_KEY = "pendingPlays";
const PENDING_PLAYS_MAX = 500;

const offline = {
    supported: "caches" in window && "serviceWorker" in navigator && window.isSecureContext,
    paths: new Set(),     // что скачано
    running: null,        // {name, done, total, stop} — идущее скачивание подборки
};

function offlineAudioKey(path) { return "/offline/audio?path=" + encodeURIComponent(path); }
function offlineMetaKey(path) { return "/offline/meta?path=" + encodeURIComponent(path); }

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
async function downloadTrack(track) {
    if (!offline.supported || !track || isOutside(track) || isDownloaded(track.path)) return;
    const stream = await streamUrlFor(track.path);
    const response = await fetch(stream.url, { cache: "no-store" });
    if (!response.ok) throw new Error(`не скачался (${response.status})`);
    const blob = await response.blob();
    const expected = Number(response.headers.get("Content-Length"));
    if (expected && blob.size !== expected) throw new Error("скачался не целиком");
    const type = response.headers.get("Content-Type") || "audio/mp4";
    await (await caches.open(OFFLINE_AUDIO)).put(offlineAudioKey(track.path), new Response(blob, {
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
    await (await caches.open(OFFLINE_META)).put(offlineMetaKey(track.path), new Response(JSON.stringify(meta), {
        headers: { "Content-Type": "application/json" },
    }));
    offline.paths.add(track.path);
    /* Обложки тех размеров, что просят список, плеер и экран блокировки, —
     * чтобы без сети была картинка, а не буква. Сохранит их sw.js. */
    for (const size of [96, 300, 512]) {
        fetch("/api/cover?path=" + encodeURIComponent(track.path) + "&size=" + size, { headers: headers() })
            .catch(() => { /* без обложки */ });
    }
}

async function removeDownloaded(path) {
    if (!offline.supported) return;
    await (await caches.open(OFFLINE_META)).delete(offlineMetaKey(path));
    await (await caches.open(OFFLINE_AUDIO)).delete(offlineAudioKey(path));
    offline.paths.delete(path);
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
    for (const track of uniqueTracks(pl.entries)) await removeDownloaded(track.path);
    refreshOfflineMarks();
}

async function removeAllDownloads() {
    if (!offline.supported) return;
    offline.paths = new Set();
    await caches.delete(OFFLINE_META);
    await caches.delete(OFFLINE_AUDIO);
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
function queuePlay(body) {
    try {
        const pending = JSON.parse(localStorage.getItem(PENDING_PLAYS_KEY) || "[]");
        pending.push(body);
        localStorage.setItem(PENDING_PLAYS_KEY, JSON.stringify(pending.slice(-PENDING_PLAYS_MAX)));
    } catch (e) { /* приватное окно — без журнала */ }
}

let flushingPlays = false;

async function flushPendingPlays() {
    if (flushingPlays || !token()) return;
    let pending;
    try { pending = JSON.parse(localStorage.getItem(PENDING_PLAYS_KEY) || "[]"); } catch (e) { return; }
    if (!pending.length) return;
    flushingPlays = true;
    try {
        while (pending.length) {
            const r = await fetch("/api/plays", {
                method: "POST",
                headers: { ...headers(), "Content-Type": "application/json" },
                body: JSON.stringify(pending[0]),
            });
            if (!r.ok && r.status !== 422) break;  // сервер недоступен — в другой раз; 422 — битая запись, выбросить
            pending.shift();
            localStorage.setItem(PENDING_PLAYS_KEY, JSON.stringify(pending));
        }
    } catch (e) {
        /* сети всё ещё нет */
    } finally {
        flushingPlays = false;
    }
}

window.addEventListener("online", () => { flushPendingPlays(); });

(function initOffline() {
    if (offline.supported) {
        navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => { offline.supported = false; });
        loadOfflineIndex();
    }
    flushPendingPlays();
})();
