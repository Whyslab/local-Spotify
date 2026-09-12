/* local-Spotify control panel.
 *
 * One rule runs through this file: values coming back from the API are put on
 * the page with textContent and DOM calls, never by assigning innerHTML. Track
 * titles and artist names come from YouTube and from third-party metadata, so
 * they are untrusted strings that happen to be displayed. A test enforces this.
 */

const POLL_MS = 3000;
const SEARCH_DEBOUNCE_MS = 300;

let activeView = "viewAdd";
let librarySearchTimer = null;

/* ---------------- Token ---------------- */

function token() {
    return localStorage.getItem("token") || "";
}

function headers() {
    return { "Authorization": "Bearer " + token() };
}

function saveToken() {
    const value = document.getElementById("tokenInput").value.trim();
    if (!value) return;
    localStorage.setItem("token", value);
    document.getElementById("tokenInput").value = "";
    applyLoginState();
    refresh();
}

function logout() {
    localStorage.removeItem("token");
    applyLoginState();
    switchView("viewAdd");
}

/* The token prompt is a one-time step, so it only takes up the screen while
 * there is no token. With one stored, the panel starts on its actual work. */
function applyLoginState() {
    const signedIn = Boolean(token());
    document.getElementById("loginBox").hidden = signedIn;
    document.getElementById("views").hidden = !signedIn;
    document.querySelector(".tabbar").hidden = !signedIn;
    document.querySelector(".rail").hidden = !signedIn;
    // Nothing to search until there is a key; the wide header shows the field
    // unconditionally otherwise.
    document.querySelector(".topbar .search").hidden = !signedIn;
}

/* ---------------- Views ---------------- */

function switchView(id) {
    activeView = id;
    /* The rail layout keys off this: on a phone the search band belongs to the
     * library and appears with it, on a desktop it is always the top band. */
    document.querySelector(".app").dataset.view = id;
    for (const section of document.querySelectorAll(".view")) {
        section.hidden = section.id !== id;
    }
    for (const tab of document.querySelectorAll(".tab")) {
        tab.classList.toggle("is-active", tab.dataset.view === id);
    }
    refresh();
}

function refresh() {
    if (!token()) return;
    health();
    /* The playlists are in the rail now, which is on screen whatever section
     * you are in -- so they cannot be fetched only while their own tab is
     * open. On a phone the rail is not rendered and this is one small request
     * that costs a list nobody sees; it is the same request the tab made. */
    playlists();
    if (activeView === "viewAdd") tasks();
    if (activeView === "viewLibrary") library();
}

/* ---------------- Adding ---------------- */

function clearInput() {
    document.getElementById("links").value = "";
    document.getElementById("addResult").textContent = "";
}

/* A playlist link is not a track link: it names many, and Spotify names them
 * without giving anything downloadable at all. Both go to their own endpoint. */
function isPlaylistLink(link) {
    return /open\.spotify\.com\/playlist\//.test(link) || /[?&]list=/.test(link);
}

async function addTracks() {
    const field = document.getElementById("links");
    const links = field.value.split("\n").map(x => x.trim()).filter(Boolean);
    const result = document.getElementById("addResult");

    if (!links.length) {
        result.textContent = "Вставь хотя бы одну ссылку.";
        return;
    }

    const playlists = links.filter(isPlaylistLink);
    if (playlists.length) {
        await importPlaylists(playlists, result);
        const rest = links.filter(l => !isPlaylistLink(l));
        if (!rest.length) { field.value = ""; tasks(); return; }
        field.value = rest.join("\n");
        return;
    }

    result.textContent = "Отправляю…";
    try {
        const r = await fetch("/api/add", {
            method: "POST",
            headers: { ...headers(), "Content-Type": "application/json" },
            body: JSON.stringify({ links }),
        });
        const data = await r.json();

        if (!r.ok) {
            result.textContent = data.detail || ("Ошибка " + r.status);
            return;
        }

        const n = (data.added || []).length;
        result.textContent = n
            ? `В очереди: ${n}`
            : "Ничего не добавлено — возможно, эти треки уже есть.";
        field.value = "";
        tasks();
    } catch (e) {
        result.textContent = e.message;
    }
}

/* ---------------- Queue ---------------- */

const STATUS_LABEL = {
    queued: "в очереди",
    downloading: "качаю",
    tagging: "теги",
    done: "готово",
    error: "ошибка",
};

async function tasks() {
    const box = document.getElementById("tasks");
    const empty = document.getElementById("tasksEmpty");
    const count = document.getElementById("queueCount");

    try {
        const r = await fetch("/api/tasks", { headers: headers() });
        if (!r.ok) return;
        const data = await r.json();

        count.textContent = data.length ? `${data.length}` : "";
        empty.hidden = data.length > 0;
        box.replaceChildren();

        for (const t of data) {
            box.appendChild(trackRow({
                title: t.title || t.url || "—",
                artist: t.artist || "",
                status: t.status,
                error: t.error,
            }));
        }
    } catch (e) {
        // Polling loop - a transient network hiccup shouldn't throw to console.
    }
}

function trackRow({ title, artist, status, error }) {
    const card = document.createElement("div");
    card.className = "track";

    const cover = document.createElement("div");
    cover.className = "cover";

    const info = document.createElement("div");
    info.className = "track-info";

    const titleEl = document.createElement("div");
    titleEl.className = "track-title";
    titleEl.textContent = title;
    info.appendChild(titleEl);

    if (artist) {
        const artistEl = document.createElement("div");
        artistEl.className = "track-artist";
        artistEl.textContent = artist;
        info.appendChild(artistEl);
    }

    if (status === "error" && error) {
        const errEl = document.createElement("div");
        errEl.className = "track-album";
        errEl.textContent = error;
        info.appendChild(errEl);
    }

    const badge = document.createElement("div");
    badge.className = "track-status";
    if (status) badge.classList.add(status);
    badge.textContent = STATUS_LABEL[status] || status || "";

    card.append(cover, info, badge);
    return card;
}

/* ---------------- Service ---------------- */

async function health() {
    const box = document.getElementById("health");
    const pill = document.getElementById("statusPill");
    const text = document.getElementById("statusText");
    const stats = document.getElementById("libraryStats");

    try {
        const r = await fetch("/health");
        const data = await r.json();
        const ok = data.status === "healthy";

        pill.className = "pill " + (ok ? "pill-ok" : "pill-error");
        text.textContent = ok ? "онлайн" : "проблема";

        if (typeof data.tracks === "number") {
            const albums = typeof data.albums === "number" ? ` · ${plural(data.albums, "альбом", "альбома", "альбомов")}` : "";
            stats.textContent = plural(data.tracks, "трек", "трека", "треков") + albums;
            const counts = document.getElementById("railCounts");
            if (counts) counts.textContent = stats.textContent;
        }

        /* The living numbers, which move while you watch: what is downloading
         * and what is waiting to reach the phone. Kept apart from the counts
         * above, which change about once a day. */
        const work = document.getElementById("railWork");
        if (work) {
            const parts = [];
            if (data.queue_size) parts.push(plural(data.queue_size, "задача", "задачи", "задач") + " в очереди");
            if (data.navidrome_pending) parts.push(data.navidrome_pending + " ждёт Navidrome");
            work.textContent = parts.length ? parts.join(" · ") : "очередь пуста";
        }

        box.replaceChildren();
        const rows = [
            ["Сервис", ok ? "работает" : data.status, !ok],
            ["База", data.database, data.database !== "ok"],
            ["Фонотека", data.library, data.library !== "ok"],
            ["В очереди", String(data.queue_size ?? "—"), false],
            ["Воркеров", String(data.workers ?? "—"), false],
            ["Путь", data.library_path || "—", false],
        ];
        for (const [label, value, bad] of rows) {
            box.appendChild(fact(label, value, bad));
        }
    } catch (e) {
        pill.className = "pill pill-error";
        text.textContent = "нет связи";
    }
}

function fact(label, value, bad) {
    const row = document.createElement("div");
    row.className = "fact";

    const l = document.createElement("span");
    l.className = "fact-label";
    l.textContent = label;

    const v = document.createElement("span");
    v.className = "fact-value" + (bad ? " bad" : "");
    v.textContent = value;

    row.append(l, v);
    return row;
}

function plural(n, one, few, many) {
    const mod10 = n % 10, mod100 = n % 100;
    let word = many;
    if (mod10 === 1 && mod100 !== 11) word = one;
    else if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) word = few;
    return `${n} ${word}`;
}

/* ---------------- Library ---------------- */

function scheduleLibrarySearch() {
    clearTimeout(librarySearchTimer);
    librarySearchTimer = setTimeout(library, SEARCH_DEBOUNCE_MS);
}

/* ---------------- Альбомы и синглы ----------------
 *
 * Группировать приходится по тегам, а не по папкам: в этой фонотеке все
 * 1111 файлов лежат в «Артист/Singles/», то есть по дереву каталогов всё
 * выглядит синглами. Теги же знают 873 издания, из которых сотня — настоящие
 * альбомы от двух до семнадцати треков, а остальные действительно одиночные.
 *
 * Альбом опознаётся по паре «артист альбома + название альбома». Артист
 * берётся из albumartist, а не из artist: у трека с фитом artist — это
 * «A • B», и один альбом рассыпался бы на несколько.
 */
let libraryMode = "tracks";

function setLibraryMode(mode) {
    libraryMode = mode;
    for (const [name, id] of [["tracks", "modeTracks"], ["albums", "modeAlbums"], ["singles", "modeSingles"]]) {
        const b = document.getElementById(id);
        if (b) b.classList.toggle("is-on", name === mode);
    }
    library();
}

function groupIntoAlbums(rows) {
    const albums = new Map();
    for (const t of rows) {
        const who = (t.albumartist || t.artist || "").trim() || "Без артиста";
        const name = (t.album || "").trim() || "Без альбома";
        const key = who + "\u0000" + name;
        if (!albums.has(key)) albums.set(key, { artist: who, album: name, tracks: [] });
        albums.get(key).tracks.push(t);
    }
    for (const group of albums.values()) {
        /* По номеру трека, а не по названию: альбом — это порядок. */
        group.tracks.sort((a, b) => (a.track || 0) - (b.track || 0)
            || (a.title || "").localeCompare(b.title || "", "ru"));
    }
    return [...albums.values()].sort((a, b) =>
        a.artist.localeCompare(b.artist, "ru") || a.album.localeCompare(b.album, "ru"));
}

function albumRow(group) {
    const card = document.createElement("div");
    card.className = "track album-card";

    const head = document.createElement("div");
    head.className = "album-head";

    const cover = document.createElement("div");
    cover.className = "cover";
    cover.textContent = group.album.slice(0, 1).toUpperCase();
    loadTrackCover(cover, group.tracks[0].path);

    const info = document.createElement("div");
    info.className = "track-info";
    const name = document.createElement("div");
    name.className = "track-title";
    name.textContent = group.album;
    const who = document.createElement("div");
    who.className = "track-artist";
    who.textContent = group.artist;
    const count = document.createElement("div");
    count.className = "track-album";
    count.textContent = plural(group.tracks.length, "трек", "трека", "треков");
    info.append(name, who, count);

    const play = document.createElement("button");
    play.className = "icon-button";
    play.setAttribute("aria-label", "Играть альбом");
    play.title = "Играть альбом";
    play.onclick = () => playQueue(group.tracks, 0, "manual");
    const playSvg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    playSvg.setAttribute("class", "icon");
    playSvg.setAttribute("viewBox", "0 0 24 24");
    const playPath = document.createElementNS("http://www.w3.org/2000/svg", "path");
    playPath.setAttribute("d", "M8 5v14l11-7z");
    playSvg.appendChild(playPath);
    play.appendChild(playSvg);

    const list = document.createElement("div");
    list.className = "rows album-tracks";
    list.hidden = true;

    const expand = document.createElement("button");
    expand.className = "icon-button";
    expand.setAttribute("aria-label", "Показать треки");
    expand.title = "Показать треки";
    expand.textContent = "▾";
    expand.onclick = () => {
        list.hidden = !list.hidden;
        expand.textContent = list.hidden ? "▾" : "▴";
        if (!list.hidden && !list.childElementCount) {
            for (const t of group.tracks) list.appendChild(libraryRow(t, group.tracks));
        }
    };

    head.append(cover, info, play, expand);
    card.append(head, list);
    return card;
}

/* Обложка трека — тоже не <img src>: /api/cover требует токен. */
function loadTrackCover(host, path) {
    fetch("/api/cover?path=" + encodeURIComponent(path), { headers: headers() })
        .then(r => (r.ok ? r.blob() : null))
        .then(blob => {
            if (!blob) return;
            const url = URL.createObjectURL(blob);
            const img = document.createElement("img");
            img.alt = "";
            img.onload = () => URL.revokeObjectURL(url);
            img.onerror = () => URL.revokeObjectURL(url);
            img.src = url;
            host.replaceChildren(img);
        })
        .catch(() => { /* остаётся буква */ });
}

async function library() {
    const box = document.getElementById("library");
    const empty = document.getElementById("libraryEmpty");
    const count = document.getElementById("libraryCount");
    const note = document.getElementById("libraryModeNote");
    const q = document.getElementById("librarySearch").value.trim();

    /* Списком треков хватает первых двухсот: дальше всё равно ищут поиском.
     * Альбомы так собрать нельзя — издание может начинаться на любой букве,
     * поэтому для группировки берём фонотеку целиком. */
    const limit = libraryMode === "tracks" ? 200 : 5000;

    try {
        const r = await fetch(
            "/api/library?limit=" + limit + "&q=" + encodeURIComponent(q),
            { headers: headers() });
        if (!r.ok) return;
        const data = await r.json();
        box.replaceChildren();

        if (libraryMode === "tracks") {
            if (note) note.textContent = "";
            count.textContent = data.length ? data.length + (data.length === 200 ? "+" : "") : "";
            empty.hidden = data.length > 0;
            /* Keep the rendered list around: playing one row queues the rest, so
             * "next" carries on down the screen instead of stopping at one track. */
            for (const t of data) box.appendChild(libraryRow(t, data));
            markPlayingRow();
            return;
        }

        const groups = groupIntoAlbums(data);
        if (libraryMode === "albums") {
            const albums = groups.filter(g => g.tracks.length > 1);
            count.textContent = albums.length || "";
            empty.hidden = albums.length > 0;
            if (note) note.textContent = plural(albums.length, "издание", "издания", "изданий");
            for (const g of albums) box.appendChild(albumRow(g));
        } else {
            /* Сингл — издание из одного трека. Показываем его обычной строкой:
             * разворачивать там нечего. */
            const singles = groups.filter(g => g.tracks.length === 1).map(g => g.tracks[0]);
            count.textContent = singles.length || "";
            empty.hidden = singles.length > 0;
            if (note) note.textContent = plural(singles.length, "сингл", "сингла", "синглов");
            for (const t of singles) box.appendChild(libraryRow(t, singles));
        }
        markPlayingRow();
    } catch (e) {
        // Same reasoning as tasks(): the next keystroke or poll retries.
    }
}

function libraryRow(t, rows) {
    const card = document.createElement("div");
    card.className = "track";
    card.dataset.trackPath = t.path;

    const cover = document.createElement("div");
    cover.className = "cover";
    if (t.track) cover.textContent = String(t.track);

    const info = document.createElement("div");
    info.className = "track-info";

    const titleEl = document.createElement("div");
    titleEl.className = "track-title";
    titleEl.textContent = t.title;
    info.appendChild(titleEl);

    if (t.artist) {
        const artistEl = document.createElement("div");
        artistEl.className = "track-artist";
        artistEl.textContent = t.artist;
        info.appendChild(artistEl);
    }

    if (t.album) {
        const albumEl = document.createElement("div");
        albumEl.className = "track-album";
        albumEl.textContent = t.album;
        info.appendChild(albumEl);
    }

    info.onclick = () => playFromLibrary(t, rows);

    const play = document.createElement("button");
    play.className = "icon-button";
    play.setAttribute("aria-label", "Играть");
    play.onclick = () => playFromLibrary(t, rows);
    const playSvg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    playSvg.setAttribute("class", "icon");
    playSvg.setAttribute("viewBox", "0 0 24 24");
    const playPath = document.createElementNS("http://www.w3.org/2000/svg", "path");
    playPath.setAttribute("d", "M8 5v14l11-7z");
    playSvg.appendChild(playPath);
    play.appendChild(playSvg);

    const toPlaylist = document.createElement("button");
    toPlaylist.className = "icon-button";
    toPlaylist.setAttribute("aria-label", "В подборку");
    toPlaylist.title = "В подборку";
    toPlaylist.onclick = () => askAddToPlaylist(card, t);
    const addSvg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    addSvg.setAttribute("class", "icon");
    addSvg.setAttribute("viewBox", "0 0 24 24");
    const addPath = document.createElementNS("http://www.w3.org/2000/svg", "path");
    addPath.setAttribute("d", "M12 5v14M5 12h14");
    addSvg.appendChild(addPath);
    toPlaylist.appendChild(addSvg);

    const del = document.createElement("button");
    del.className = "icon-button danger";
    del.setAttribute("aria-label", "Удалить");
    del.onclick = () => askRemove(card, t);

    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "icon");
    svg.setAttribute("viewBox", "0 0 24 24");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6");
    svg.appendChild(path);
    del.appendChild(svg);

    card.append(cover, info, play, toPlaylist, del);
    return card;
}

/* Deleting is the one destructive thing this panel does, so it confirms in
 * place rather than through a browser dialog - a native confirm() on iOS is
 * easy to dismiss by accident and says nothing about where the file goes. */
function askRemove(card, t) {
    const box = document.createElement("div");
    box.className = "confirm";

    const head = document.createElement("h3");
    head.textContent = "Удалить «" + t.title + "»?";

    const note = document.createElement("p");
    note.textContent = "Файл переедет в корзину на ноутбуке, а не сотрётся. Вернуть можно.";

    const row = document.createElement("div");
    row.className = "row";

    const cancel = document.createElement("button");
    cancel.className = "ghost grow";
    cancel.textContent = "Отмена";
    cancel.onclick = () => box.replaceWith(card);

    const confirm = document.createElement("button");
    confirm.className = "danger grow";
    confirm.textContent = "Удалить";
    confirm.onclick = async () => {
        confirm.disabled = true;
        cancel.disabled = true;
        confirm.textContent = "Удаляю…";
        const failure = await removeTrack(t);
        if (failure) {
            note.textContent = failure;
            confirm.disabled = false;
            cancel.disabled = false;
            confirm.textContent = "Повторить";
            return;
        }
        box.remove();
    };

    row.append(cancel, confirm);
    box.append(head, note, row);
    card.replaceWith(box);
}

/** Returns null on success, or a message to show in the confirm box. */
async function removeTrack(t) {
    try {
        const r = await fetch("/api/library", {
            method: "DELETE",
            headers: { ...headers(), "Content-Type": "application/json" },
            body: JSON.stringify({ path: t.path }),
        });
        if (r.ok) return null;
        const err = await r.json().catch(() => ({}));
        return err.detail || ("Не удалось удалить: " + r.status);
    } catch (e) {
        return "Не удалось удалить: " + e.message;
    }
}

/* ---------------- Boot ---------------- */

/* Both scripts are at the end of the body, so this fires once player.js has
 * run. It has to: refresh() reaches into playlists(), which lives there, and
 * calling it a moment too early throws before the polling timer is ever set --
 * leaving a page that renders once and then never updates again. */
document.addEventListener("DOMContentLoaded", () => {
    document.querySelector(".app").dataset.view = activeView;
    applyLoginState();
    refresh();
    setInterval(refresh, POLL_MS);
});


/* ---------------- Playlist links ---------------- */

async function importPlaylists(links, result) {
    for (const url of links) {
        result.textContent = "Читаю плейлист…";
        try {
            const r = await fetch("/api/import-playlist", {
                method: "POST",
                headers: { ...headers(), "Content-Type": "application/json" },
                body: JSON.stringify({ url }),
            });
            const data = await r.json();
            if (!r.ok) { result.textContent = data.detail || ("Ошибка " + r.status); continue; }

            const parts = [`Прочитано ${data.read}, в очередь ${data.queued}`];
            if (data.unmatched && data.unmatched.length) {
                /* Not silently dropped: a track that could not be matched is
                 * named, because the alternative is discovering the gap months
                 * later with no way to tell what is missing. */
                parts.push(`не нашлось ${data.unmatched.length}: ` +
                    data.unmatched.slice(0, 3).map(t => `${t.artist} — ${t.title}`).join("; ") +
                    (data.unmatched.length > 3 ? " и другие" : ""));
            }
            if (data.note) parts.push(data.note);
            result.textContent = parts.join(". ");
        } catch (e) {
            result.textContent = e.message;
        }
    }
    tasks();
}

/* ---------------- Search ---------------- */

async function runSearch() {
    const query = document.getElementById("searchQuery").value.trim();
    const note = document.getElementById("searchNote");
    const box = document.getElementById("searchResults");
    box.replaceChildren();
    if (!query) return;

    note.textContent = "Ищу…";
    try {
        const r = await fetch("/api/search?q=" + encodeURIComponent(query), { headers: headers() });
        const data = await r.json();
        if (!r.ok) { note.textContent = data.detail || ("Ошибка " + r.status); return; }
        note.textContent = data.results.length ? "" : "Ничего не нашлось.";
        for (const item of data.results) box.appendChild(searchRow(item));
    } catch (e) {
        note.textContent = e.message;
    }
}

/* The choice is deliberately the user's: two uploads of one song differ in
 * length and in channel, and picking automatically is what filled the library
 * with live versions the last time. */
function searchRow(item) {
    const row = document.createElement("div");
    row.className = "track";

    const info = document.createElement("div");
    info.className = "track-info";
    const title = document.createElement("div");
    title.className = "track-title";
    title.textContent = item.title;
    const meta = document.createElement("div");
    meta.className = "result-meta";
    meta.textContent = [item.channel, item.duration ? formatTime(item.duration) : null]
        .filter(Boolean).join(" · ");
    info.append(title, meta);

    const add = document.createElement("button");
    add.className = "ghost";
    add.textContent = "Добавить";
    add.onclick = async () => {
        add.disabled = true;
        add.textContent = "…";
        const r = await fetch("/api/add", {
            method: "POST",
            headers: { ...headers(), "Content-Type": "application/json" },
            body: JSON.stringify({ links: [item.url] }),
        });
        add.textContent = r.ok ? "В очереди" : "Ошибка";
        tasks();
    };

    row.append(info, add);
    return row;
}

/* ---------------- Files from disk ---------------- */

async function importFiles(fileList) {
    const note = document.getElementById("importNote");
    const files = Array.from(fileList || []);
    if (!files.length) return;

    note.textContent = `Отправляю ${files.length}…`;
    const body = new FormData();
    for (const file of files) body.append("files", file);

    try {
        const r = await fetch("/api/import", { method: "POST", headers: headers(), body });
        const data = await r.json();
        if (!r.ok) { note.textContent = data.detail || ("Ошибка " + r.status); return; }

        const parts = [`Принято: ${data.accepted.length}`];
        if (data.skipped.length) {
            parts.push("пропущено: " + data.skipped
                .map(s => `${s.file} (${s.reason})`).slice(0, 3).join("; "));
        }
        note.textContent = parts.join(", ");
        tasks();
    } catch (e) {
        note.textContent = e.message;
    }
}

document.addEventListener("DOMContentLoaded", () => {
    const zone = document.getElementById("dropZone");
    if (!zone) return;
    for (const event of ["dragenter", "dragover"]) {
        zone.addEventListener(event, e => {
            e.preventDefault();
            zone.classList.add("is-over");
        });
    }
    for (const event of ["dragleave", "drop"]) {
        zone.addEventListener(event, () => zone.classList.remove("is-over"));
    }
    zone.addEventListener("drop", e => {
        e.preventDefault();
        importFiles(e.dataTransfer.files);
    });
});
