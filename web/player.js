/* Playback, the queue, and editing playlists.
 *
 * Same rule as app.js: values from the API reach the page through textContent
 * and DOM calls, never innerHTML. Track titles come from YouTube and from
 * third-party metadata, so they are untrusted strings that happen to be shown.
 *
 * Reordering rewrites the whole playlist file in one request rather than
 * sending a move instruction. That is what makes the write atomic on the
 * server, and it is why every edit carries the revision it was made against:
 * an edit built on a stale view is refused with 409 instead of silently
 * overwriting one made from the other device.
 */

const SKIP_THRESHOLD_SECONDS = 30;

const player = {
    audio: new Audio(),
    queue: [],
    index: -1,
    playlist: null,      // {name, revision, entries}
    reported: false,     // one journal entry per track, not one per pause
    queueMode: "manual", // "smart", "plain" or "manual" -- see reportPlay
};

/* ---------------- Journal ---------------- */

/* Navidrome keeps a play count and a last-played date, not a log, so the
 * question "what was playing at this hour" has no source but this one. */
function reportPlay(finished) {
    const current = player.queue[player.index];
    if (!current || player.reported) return;
    player.reported = true;

    const played = player.audio.currentTime || 0;
    fetch("/api/plays", {
        method: "POST",
        headers: { ...headers(), "Content-Type": "application/json" },
        body: JSON.stringify({
            path: current.path,
            played_seconds: Number(played.toFixed(2)),
            duration: player.audio.duration || current.duration || null,
            skipped: !finished && played < SKIP_THRESHOLD_SECONDS,
            source: "player",
            /* Which kind of queue this came out of. The comparison of skip
             * rates cannot be reconstructed later, so the label has to travel
             * with the play. */
            mode: player.queueMode,
        }),
    }).catch(() => { /* the journal is not worth interrupting playback for */ });
}

/* ---------------- Playback ---------------- */

async function streamUrlFor(path) {
    const r = await fetch("/api/stream-url?path=" + encodeURIComponent(path), { headers: headers() });
    if (!r.ok) throw new Error("Не удалось получить ссылку на трек");
    return (await r.json()).url;
}

async function playAt(position) {
    if (position < 0 || position >= player.queue.length) return;
    reportPlay(false);

    player.index = position;
    player.reported = false;
    const track = player.queue[position];

    try {
        player.audio.src = await streamUrlFor(track.path);
        await player.audio.play();
    } catch (e) {
        setPlayerNote(e.message);
        return;
    }
    renderPlayer();
    markPlayingRow();
}

function playQueue(tracks, startAt = 0, mode = "manual") {
    player.queue = tracks;
    player.queueMode = mode;
    playAt(startAt);
}

/* ---------------- Shuffling ---------------- */

async function loadShuffle(mode) {
    const note = document.getElementById("shuffleNote");
    note.textContent = "Собираю очередь…";
    try {
        const r = await fetch(`/api/shuffle?size=50&mode=${mode}`, { headers: headers() });
        const data = await r.json();
        if (!r.ok) { note.textContent = data.detail || ("Ошибка " + r.status); return; }
        if (!data.queue.length) { note.textContent = "Нечего играть."; return; }

        playQueue(data.queue, 0, mode);

        if (mode === "smart") {
            const report = data.report || {};
            /* Said plainly, because it is the difference between "it works"
             * and "it has nothing to work with yet": tempo cannot order a
             * library that has not been measured. */
            note.textContent = data.analysed < data.total
                ? `Измерено ${data.analysed} из ${data.total} — остальные ставятся без учёта темпа`
                : `Разброс темпа до ${report.max_tempo_jump ?? "—"} BPM, артистов ${report.distinct_artists}`;
        } else {
            note.textContent = "";
        }
    } catch (e) {
        note.textContent = e.message;
    }
}

function playSmartShuffle() { loadShuffle("smart"); }
function playPlainShuffle() { loadShuffle("plain"); }

function togglePlay() {
    if (!player.queue.length) return;
    if (player.audio.paused) player.audio.play(); else player.audio.pause();
    renderPlayer();
}

function nextTrack() { playAt(player.index + 1); }
function prevTrack() {
    /* Restart the track first, like every other player: pressing back three
     * seconds in means "from the top", not "the previous song". */
    if (player.audio.currentTime > 3) { player.audio.currentTime = 0; return; }
    playAt(player.index - 1);
}

player.audio.addEventListener("ended", () => {
    reportPlay(true);
    if (player.index + 1 < player.queue.length) playAt(player.index + 1);
    else renderPlayer();
});
player.audio.addEventListener("timeupdate", renderProgress);
player.audio.addEventListener("play", renderPlayer);
player.audio.addEventListener("pause", renderPlayer);

/* A signed link outlives its track and then some, but a long pause can still
 * outlast it. Fetch a fresh one and carry on from the same spot rather than
 * dropping the user back to silence. */
player.audio.addEventListener("error", async () => {
    const track = player.queue[player.index];
    if (!track) return;
    const at = player.audio.currentTime;
    try {
        player.audio.src = await streamUrlFor(track.path);
        player.audio.currentTime = at;
        await player.audio.play();
    } catch (e) {
        setPlayerNote("Трек недоступен");
    }
});

/* ---------------- Player bar ---------------- */

function setPlayerNote(text) {
    document.getElementById("playerNote").textContent = text || "";
}

function formatTime(seconds) {
    if (!isFinite(seconds) || seconds < 0) return "0:00";
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return `${m}:${String(s).padStart(2, "0")}`;
}

function renderPlayer() {
    const bar = document.getElementById("player");
    const track = player.queue[player.index];
    bar.hidden = !track;
    document.getElementById("nowPanel").hidden = !track;
    if (!track) return;

    renderNowPanel(track);

    document.getElementById("playerTitle").textContent = track.title || track.path;
    document.getElementById("playerArtist").textContent = track.artist || "";
    document.getElementById("playerToggle").setAttribute(
        "aria-label", player.audio.paused ? "Играть" : "Пауза");
    document.getElementById("playerToggleIcon").setAttribute(
        "d", player.audio.paused ? "M8 5v14l11-7z" : "M7 5h4v14H7zM13 5h4v14h-4z");
    setPlayerNote("");
    renderProgress();
}

/* The right-hand panel. Everything here also exists somewhere else -- the bar
 * has the title, the list has the artist -- except the three measured numbers,
 * which have had nowhere to live until now. It is only ever on screen at the
 * width that has room for it, so the duplication costs nothing.
 *
 * The details arrive from their own request rather than from the queue row,
 * because tempo and key are not in the queue: a playlist is a list of files,
 * and nothing has measured them at the point the list is built. */
let nowRequested = null;
let nowCoverUrl = null;

function renderNowPanel(track) {
    const panel = document.getElementById("nowPanel");
    if (!panel || panel.hidden) return;

    document.getElementById("nowTitle").textContent = track.title || track.path;
    document.getElementById("nowArtist").textContent = track.artist || "";

    if (nowRequested === track.path) return;
    nowRequested = track.path;
    document.getElementById("nowAlbum").textContent = "";
    document.getElementById("nowFacts").replaceChildren();
    loadNowCover(track);

    fetch("/api/track?path=" + encodeURIComponent(track.path), { headers: headers() })
        .then(r => r.ok ? r.json() : null)
        .then(data => {
            // A slow answer for a track that is no longer playing must not
            // overwrite the one that is.
            if (!data || nowRequested !== track.path) return;
            document.getElementById("nowAlbum").textContent = data.album || "";

            const f = data.features || {};
            const key = f.music_key ? f.music_key + (f.mode ? " " + f.mode : "") : null;
            renderFacts([
                [f.tempo ? Math.round(f.tempo) : null, "BPM"],
                [key, "тональность"],
                [typeof f.energy === "number" ? f.energy.toFixed(2) : null, "энергия"],
            ]);
        })
        .catch(() => { /* the panel simply stays without numbers */ });
}

/* The picture cannot be an <img src>: /api/cover wants a bearer token and a
 * src attribute carries no headers. The same wall the audio element ran into,
 * with a cheaper way round it -- one fetch, one object URL, released as soon
 * as the next track claims the panel. Signing this URL as well would widen the
 * unauthenticated surface for a thumbnail. */
function loadNowCover(track) {
    const cover = document.getElementById("nowCover");
    const wanted = track.path;

    const release = () => {
        if (nowCoverUrl) URL.revokeObjectURL(nowCoverUrl);
        nowCoverUrl = null;
    };

    fetch("/api/cover?path=" + encodeURIComponent(wanted), { headers: headers() })
        .then(r => r.ok ? r.blob() : null)
        .then(blob => {
            if (nowRequested !== wanted) return;
            release();
            if (!blob) { cover.style.backgroundImage = "none"; return; }
            nowCoverUrl = URL.createObjectURL(blob);
            cover.style.backgroundImage = `url("${nowCoverUrl}")`;
        })
        .catch(() => { if (nowRequested === wanted) cover.style.backgroundImage = "none"; });
}

function renderFacts(facts) {
    const box = document.getElementById("nowFacts");
    box.replaceChildren();
    for (const [value, label] of facts) {
        const cell = document.createElement("div");
        cell.className = "now-fact" + (value === null ? " is-unknown" : "");
        const big = document.createElement("b");
        // Not measured yet. A dash says that; a zero would say the track has
        // no tempo, which is a different and wrong claim.
        big.textContent = value === null ? "—" : String(value);
        const small = document.createElement("small");
        small.textContent = label;
        cell.append(big, small);
        box.appendChild(cell);
    }
}

function renderProgress() {
    const bar = document.getElementById("playerFill");
    const done = player.audio.currentTime || 0;
    const total = player.audio.duration || 0;
    bar.style.width = total ? `${(done / total) * 100}%` : "0%";
    document.getElementById("playerElapsed").textContent = formatTime(done);
    document.getElementById("playerTotal").textContent = formatTime(total);
}

function seekFromClick(event) {
    const total = player.audio.duration;
    if (!total) return;
    const box = event.currentTarget.getBoundingClientRect();
    player.audio.currentTime = ((event.clientX - box.left) / box.width) * total;
}

function markPlayingRow() {
    const current = player.queue[player.index];
    for (const row of document.querySelectorAll("[data-track-path]")) {
        row.classList.toggle("is-playing", !!current && row.dataset.trackPath === current.path);
    }
}

/* ---------------- Playlists ---------------- */

async function playlists() {
    const box = document.getElementById("playlists");
    const empty = document.getElementById("playlistsEmpty");
    try {
        const r = await fetch("/api/playlists", { headers: headers() });
        if (!r.ok) return;
        const data = await r.json();

        empty.hidden = data.length > 0;
        box.replaceChildren();
        for (const p of data) box.appendChild(playlistRow(p));
        renderRail(data);
    } catch (e) { /* the next poll retries */ }
}

/* The same playlists as one-line entries in the rail. Names and counts only:
 * a rail 232px wide has no room for covers, and the point of it is to get to a
 * playlist in one click from wherever you are. */
function renderRail(data) {
    const rail = document.getElementById("railPlaylists");
    if (!rail) return;
    const open = player.playlist ? player.playlist.name : null;
    rail.replaceChildren();
    for (const p of data) {
        const item = document.createElement("button");
        item.className = "rail-item" + (p.name === open ? " is-active" : "");
        item.onclick = () => openPlaylist(p.name);
        const name = document.createElement("b");
        name.textContent = p.name;
        const count = document.createElement("small");
        count.textContent = p.tracks === 1 ? "1 трек" : `${p.tracks} треков`;
        item.append(name, count);
        rail.appendChild(item);
    }
}

function playlistRow(p) {
    const row = document.createElement("button");
    row.className = "track playlist-row";
    row.onclick = () => openPlaylist(p.name);

    const cover = document.createElement("div");
    cover.className = "cover";
    const img = document.createElement("img");
    img.alt = "";
    img.loading = "lazy";
    img.src = "/api/playlists/" + encodeURIComponent(p.name) + "/cover";
    img.onerror = () => { img.remove(); cover.textContent = p.name.slice(0, 1).toUpperCase(); };
    cover.appendChild(img);

    const info = document.createElement("div");
    info.className = "track-info";
    const name = document.createElement("div");
    name.className = "track-title";
    name.textContent = p.name;
    const count = document.createElement("div");
    count.className = "track-artist";
    count.textContent = p.tracks === 1 ? "1 трек" : `${p.tracks} треков`;
    info.append(name, count);

    row.append(cover, info);
    return row;
}

async function openPlaylist(name) {
    const r = await fetch("/api/playlists/" + encodeURIComponent(name) + "/tracks", { headers: headers() });
    if (!r.ok) { setPlaylistNote("Не удалось открыть подборку"); return; }
    player.playlist = await r.json();
    switchView("viewPlaylist");
    renderPlaylist();
}

function renderPlaylist() {
    const pl = player.playlist;
    if (!pl) return;

    document.getElementById("playlistName").textContent = pl.name;
    document.getElementById("playlistCount").textContent =
        pl.entries.length === 1 ? "1 трек" : `${pl.entries.length} треков`;

    const art = document.getElementById("playlistCover");
    art.replaceChildren();
    const img = document.createElement("img");
    img.alt = "";
    img.src = "/api/playlists/" + encodeURIComponent(pl.name) + "/cover?t=" + Date.now();
    img.onerror = () => { img.remove(); art.textContent = pl.name.slice(0, 1).toUpperCase(); };
    art.appendChild(img);

    const box = document.getElementById("playlistTracks");
    box.replaceChildren();
    pl.entries.forEach((entry, position) => box.appendChild(playlistTrackRow(entry, position)));
    markPlayingRow();
}

/* The row carries its index, not its path: nineteen tracks in Monday.m3u
 * appear twice, so a path does not identify a line. */
function playlistTrackRow(entry, position) {
    const row = document.createElement("div");
    row.className = "track playlist-track";
    row.draggable = true;
    row.dataset.index = String(position);
    row.dataset.trackPath = entry.path;

    row.addEventListener("dragstart", e => {
        e.dataTransfer.setData("text/plain", String(position));
        row.classList.add("is-dragging");
    });
    row.addEventListener("dragend", () => row.classList.remove("is-dragging"));
    row.addEventListener("dragover", e => { e.preventDefault(); row.classList.add("is-over"); });
    row.addEventListener("dragleave", () => row.classList.remove("is-over"));
    row.addEventListener("drop", e => {
        e.preventDefault();
        row.classList.remove("is-over");
        moveTrack(Number(e.dataTransfer.getData("text/plain")), position);
    });

    const handle = document.createElement("div");
    handle.className = "handle";
    handle.textContent = "⠿";
    handle.title = "Перетащить";

    const info = document.createElement("div");
    info.className = "track-info";
    info.onclick = () => playQueue(player.playlist.entries, position, "manual");
    const title = document.createElement("div");
    title.className = "track-title";
    title.textContent = entry.title;
    info.appendChild(title);
    if (entry.duration > 0) {
        const meta = document.createElement("div");
        meta.className = "track-artist";
        meta.textContent = formatTime(entry.duration);
        info.appendChild(meta);
    }

    const up = smallButton("↑", "Выше", () => moveTrack(position, position - 1));
    const down = smallButton("↓", "Ниже", () => moveTrack(position, position + 1));
    const drop = smallButton("×", "Убрать из подборки", () => removeAt(position));
    drop.classList.add("danger");

    row.append(handle, info, up, down, drop);
    return row;
}

function smallButton(label, title, onClick) {
    const b = document.createElement("button");
    b.className = "icon-button small";
    b.textContent = label;
    b.title = title;
    b.setAttribute("aria-label", title);
    b.onclick = onClick;
    return b;
}

function moveTrack(from, to) {
    const pl = player.playlist;
    if (!pl || from === to || to < 0 || to >= pl.entries.length) return;
    const paths = pl.entries.map(e => e.path);
    const [moved] = paths.splice(from, 1);
    paths.splice(to, 0, moved);
    savePlaylist(paths);
}

function removeAt(position) {
    const paths = player.playlist.entries.map(e => e.path);
    paths.splice(position, 1);
    savePlaylist(paths);
}

async function savePlaylist(paths) {
    const pl = player.playlist;
    setPlaylistNote("Сохраняю…");
    try {
        const r = await fetch("/api/playlists/" + encodeURIComponent(pl.name) + "/tracks", {
            method: "PUT",
            headers: { ...headers(), "Content-Type": "application/json" },
            body: JSON.stringify({ paths, revision: pl.revision }),
        });
        const data = await r.json();
        if (r.status === 409) {
            /* Someone edited from the other device while this view was open.
             * Reload rather than overwrite: their edit is as real as this one. */
            setPlaylistNote("Подборку изменили с другого устройства — перечитываю");
            await openPlaylist(pl.name);
            return;
        }
        if (!r.ok) { setPlaylistNote(data.detail || ("Ошибка " + r.status)); return; }
        player.playlist = data;
        renderPlaylist();
        setPlaylistNote("");
    } catch (e) {
        setPlaylistNote(e.message);
    }
}

function setPlaylistNote(text) {
    document.getElementById("playlistNote").textContent = text || "";
}

function playPlaylist() {
    if (player.playlist && player.playlist.entries.length) {
        playQueue(player.playlist.entries, 0, "manual");
    }
}

async function createPlaylist() {
    const field = document.getElementById("newPlaylist");
    const name = field.value.trim();
    if (!name) return;
    const r = await fetch("/api/playlists", {
        method: "POST",
        headers: { ...headers(), "Content-Type": "application/json" },
        body: JSON.stringify({ name, paths: [] }),
    });
    const data = await r.json();
    if (!r.ok) { document.getElementById("playlistsNote").textContent = data.detail || "Ошибка"; return; }
    field.value = "";
    document.getElementById("playlistsNote").textContent = "";
    playlists();
}

async function renamePlaylist() {
    const pl = player.playlist;
    const next = document.getElementById("playlistRename").value.trim();
    if (!pl || !next || next === pl.name) return;
    const r = await fetch("/api/playlists/" + encodeURIComponent(pl.name), {
        method: "PATCH",
        headers: { ...headers(), "Content-Type": "application/json" },
        body: JSON.stringify({ name: next }),
    });
    if (!r.ok) { setPlaylistNote("Не удалось переименовать"); return; }
    player.playlist = await r.json();
    document.getElementById("playlistRename").value = "";
    renderPlaylist();
}

async function deletePlaylist() {
    const pl = player.playlist;
    if (!pl) return;
    const r = await fetch("/api/playlists/" + encodeURIComponent(pl.name), {
        method: "DELETE", headers: headers(),
    });
    if (!r.ok) { setPlaylistNote("Не удалось удалить"); return; }
    player.playlist = null;
    switchView("viewPlaylists");
    playlists();
}

/* The phone is where covers get chosen: the photo is already in the gallery
 * there. It shows immediately from the local copy rather than waiting for
 * Navidrome to be told about it. */
async function uploadCover(input) {
    const file = input.files && input.files[0];
    if (!file || !player.playlist) return;
    setPlaylistNote("Загружаю обложку…");
    const body = new FormData();
    body.append("image", file);
    const r = await fetch("/api/playlists/" + encodeURIComponent(player.playlist.name) + "/cover", {
        method: "POST", headers: headers(), body,
    });
    const data = await r.json();
    input.value = "";
    if (!r.ok) { setPlaylistNote(data.detail || "Не удалось загрузить обложку"); return; }
    setPlaylistNote("");
    renderPlaylist();
}

/* Playing a track straight from the library screen queues what is on screen,
 * so "next" continues down the list instead of stopping. */
function playFromLibrary(track, rows) {
    playQueue(rows, rows.findIndex(r => r.path === track.path), "manual");
}

/* ---------------- Desktop shell bridge ---------------- */

/* The GTK shell registers a "mpris" message handler and mirrors whatever
 * arrives here onto the session bus, which is what makes the laptop's media
 * keys work. In a plain browser tab window.webkit is absent and every call
 * below is a no-op, so the page behaves identically either way. */
function notifyShell() {
    const handler = window.webkit && window.webkit.messageHandlers
        && window.webkit.messageHandlers.mpris;
    if (!handler) return;
    const track = player.queue[player.index];
    handler.postMessage(JSON.stringify({
        status: !track ? "stopped" : (player.audio.paused ? "paused" : "playing"),
        path: track ? track.path : "",
        title: track ? (track.title || track.path) : "",
        artist: track ? (track.artist || "") : "",
        position: player.audio.currentTime || 0,
        duration: player.audio.duration || (track ? track.duration : 0) || 0,
    }));
}

for (const event of ["play", "pause", "ended", "loadedmetadata"]) {
    player.audio.addEventListener(event, notifyShell);
}
