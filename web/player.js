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

    /* Порядок обхода очереди — список её позиций, а не сама очередь.
     * Так перемешивание не перетасовывает список, который человек видит:
     * очередь остаётся той, что он собрал, меняется только маршрут по ней.
     * Выключил перемешивание — маршрут снова прямой, и ничего не потеряно. */
    order: [],
    orderAt: -1,
    shuffle: false,
    repeat: "off",       // "off" | "all" | "one"
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
    renderQueuePanel();
}

/* ---------------- Порядок обхода, перемешивание и повтор ---------------- */

function buildOrder(startIndex) {
    const n = player.queue.length;
    const straight = Array.from({ length: n }, (_, i) => i);
    if (!player.shuffle) {
        player.order = straight;
        player.orderAt = startIndex >= 0 ? startIndex : -1;
        return;
    }
    /* Играющий трек остаётся первым: включать перемешивание не значит
     * перебивать то, что сейчас звучит. Остальные тасуются по Фишеру—Йетсу. */
    const rest = straight.filter(i => i !== startIndex);
    for (let i = rest.length - 1; i > 0; i -= 1) {
        const j = Math.floor(Math.random() * (i + 1));
        [rest[i], rest[j]] = [rest[j], rest[i]];
    }
    player.order = startIndex >= 0 ? [startIndex, ...rest] : rest;
    player.orderAt = startIndex >= 0 ? 0 : -1;
}

function stepInOrder(delta) {
    if (!player.order.length) return -1;
    let at = player.orderAt;
    /* Позиция могла разъехаться, если очередь меняли снаружи. */
    if (at < 0 || player.order[at] !== player.index) {
        at = player.order.indexOf(player.index);
    }
    const next = at + delta;
    if (next >= 0 && next < player.order.length) {
        player.orderAt = next;
        return player.order[next];
    }
    if (player.repeat === "all" && player.order.length) {
        player.orderAt = delta > 0 ? 0 : player.order.length - 1;
        return player.order[player.orderAt];
    }
    return -1;
}

function toggleShuffle() {
    player.shuffle = !player.shuffle;
    buildOrder(player.index);
    renderPlayerModes();
    renderQueuePanel();
}

function cycleRepeat() {
    player.repeat = player.repeat === "off" ? "all" : player.repeat === "all" ? "one" : "off";
    renderPlayerModes();
}

function renderPlayerModes() {
    const shuffle = document.getElementById("playerShuffle");
    if (shuffle) {
        shuffle.classList.toggle("is-on", player.shuffle);
        shuffle.setAttribute("aria-pressed", String(player.shuffle));
        shuffle.title = player.shuffle ? "Перемешивание включено" : "Перемешать";
    }
    const repeat = document.getElementById("playerRepeat");
    const one = document.getElementById("playerRepeatOne");
    if (repeat) {
        repeat.classList.toggle("is-on", player.repeat !== "off");
        repeat.title = player.repeat === "one" ? "Повтор одного трека"
            : player.repeat === "all" ? "Повтор всей очереди" : "Повтор выключен";
    }
    if (one) one.hidden = player.repeat !== "one";
}

/* ---------------- Панель очереди ---------------- */

function toggleQueuePanel() {
    const panel = document.getElementById("playQueue");
    if (!panel) return;
    panel.hidden = !panel.hidden;
    const button = document.getElementById("playerQueueButton");
    if (button) {
        button.classList.toggle("is-on", !panel.hidden);
        button.setAttribute("aria-pressed", String(!panel.hidden));
    }
    if (!panel.hidden) renderQueuePanel();
}

function renderQueuePanel() {
    const panel = document.getElementById("playQueue");
    if (!panel || panel.hidden) return;
    const box = document.getElementById("playQueueRows");
    const count = document.getElementById("playQueueCount");
    box.replaceChildren();

    /* Показываем в том порядке, в каком оно будет играть, а не в том,
     * в каком лежит: при перемешивании это разные вещи, и человек
     * открывает очередь именно чтобы увидеть, что будет дальше. */
    const route = player.order.length ? player.order : player.queue.map((_, i) => i);
    const at = route.indexOf(player.index);
    if (count) {
        const left = at >= 0 ? route.length - at - 1 : route.length;
        count.textContent = left === 1 ? "дальше 1 трек" : `дальше ${left}`;
    }

    route.forEach((queueIndex, position) => {
        const track = player.queue[queueIndex];
        if (!track) return;
        const row = document.createElement("button");
        row.className = "track queue-row";
        if (queueIndex === player.index) row.classList.add("is-playing");
        if (at >= 0 && position < at) row.classList.add("is-played");
        row.onclick = () => { player.orderAt = position; playAt(queueIndex); renderQueuePanel(); };

        const info = document.createElement("div");
        info.className = "track-info";
        const title = document.createElement("div");
        title.className = "track-title";
        title.textContent = track.title || track.path;
        info.appendChild(title);
        if (track.artist) {
            const artist = document.createElement("div");
            artist.className = "track-artist";
            artist.textContent = track.artist;
            info.appendChild(artist);
        }
        row.appendChild(info);
        box.appendChild(row);
    });
}

function playQueue(tracks, startAt = 0, mode = "manual") {
    player.queue = tracks;
    player.queueMode = mode;
    buildOrder(startAt);
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

function nextTrack() {
    const next = stepInOrder(1);
    if (next >= 0) playAt(next);
}
function prevTrack() {
    /* Restart the track first, like every other player: pressing back three
     * seconds in means "from the top", not "the previous song". */
    if (player.audio.currentTime > 3) { player.audio.currentTime = 0; return; }
    const back = stepInOrder(-1);
    if (back >= 0) playAt(back);
}

player.audio.addEventListener("ended", () => {
    reportPlay(true);
    if (player.repeat === "one") {
        /* Тот же трек с начала: позиция в маршруте не двигается. */
        player.audio.currentTime = 0;
        player.audio.play().catch(() => renderPlayer());
        return;
    }
    const next = stepInOrder(1);
    if (next >= 0) playAt(next);
    else renderPlayer();
});
player.audio.addEventListener("timeupdate", renderProgress);
player.audio.addEventListener("timeupdate", () => highlightLyric(false));
player.audio.addEventListener("seeked", () => highlightLyric(true));
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
    loadLyrics(track);

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

/* Текст песни.
 *
 * Запрашивается один раз на трек — сервер сам держит кэш на диске, так что
 * повторное включение того же трека стоит двенадцать миллисекунд.
 *
 * Подсветка строки идёт от времени воспроизведения. Индекс текущей строки
 * помнится между вызовами: перебирать полсотни строк тридцать раз в секунду
 * незачем, когда почти всегда нужна следующая по счёту.
 */
const lyrics = { path: null, lines: [], index: -1, box: null };

function loadLyrics(track) {
    const box = document.getElementById("nowLyrics");
    if (!box) return;
    lyrics.box = box;
    lyrics.path = track.path;
    lyrics.lines = [];
    lyrics.index = -1;
    box.replaceChildren();
    box.classList.remove("has-lyrics");

    fetch("/api/lyrics?path=" + encodeURIComponent(track.path), { headers: headers() })
        .then(r => (r.ok ? r.json() : null))
        .then(data => {
            /* Медленный ответ для трека, который уже не играет, не должен
             * затирать тот, что играет сейчас. */
            if (!data || lyrics.path !== track.path) return;
            if (!data.found) { renderNoLyrics(box, data.reason); return; }

            if (data.synced && data.synced.length) {
                lyrics.lines = data.synced;
                box.classList.add("has-lyrics");
                for (const item of data.synced) {
                    const line = document.createElement("p");
                    line.className = "lyric";
                    line.textContent = item.line || "♪";
                    box.appendChild(line);
                }
                highlightLyric(true);
            } else if (data.plain) {
                box.classList.add("has-lyrics");
                for (const raw of data.plain.split("\n")) {
                    const line = document.createElement("p");
                    line.className = "lyric plain";
                    line.textContent = raw;
                    box.appendChild(line);
                }
            } else {
                renderNoLyrics(box, "");
            }
        })
        .catch(() => renderNoLyrics(box, "Не удалось получить текст"));
}

function renderNoLyrics(box, reason) {
    const note = document.createElement("p");
    note.className = "lyric-note";
    note.textContent = reason || "Текста нет";
    box.replaceChildren(note);
}

function highlightLyric(force) {
    if (!lyrics.lines.length || !lyrics.box) return;
    const at = player.audio.currentTime || 0;

    let i = lyrics.index;
    /* Перемотали назад — начинаем счёт заново. */
    if (i >= 0 && lyrics.lines[i] && lyrics.lines[i].at > at) i = -1;
    while (i + 1 < lyrics.lines.length && lyrics.lines[i + 1].at <= at) i += 1;
    if (i === lyrics.index && !force) return;
    lyrics.index = i;

    const rows = lyrics.box.querySelectorAll(".lyric");
    rows.forEach((row, n) => row.classList.toggle("now", n === i));
    if (i >= 0 && rows[i]) {
        const row = rows[i];
        const wanted = row.offsetTop - lyrics.box.clientHeight / 2 + row.clientHeight / 2;
        lyrics.box.scrollTo({ top: Math.max(0, wanted), behavior: "smooth" });
    }
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

/* Обложка подборки — тоже не <img src>.
 *
 * Правило то же, что абзацем выше про обложку играющего трека: /api/…/cover
 * требует токен, а атрибут src заголовков не несёт. Здесь это правило забыли
 * применить, и обе обложки подборок молча получали 401: сначала срабатывал
 * onerror, потом вместо картинки показывалась первая буква названия. Снаружи
 * это выглядело как «обложка не ставится», хотя на сервере она лежала целая.
 *
 * Ссылка на объект освобождается сразу после отрисовки: картинка к этому
 * моменту уже декодирована, держать ссылку дальше незачем.
 */
function loadPlaylistCover(host, name) {
    const letter = () => { host.replaceChildren(); host.textContent = name.slice(0, 1).toUpperCase(); };
    fetch("/api/playlists/" + encodeURIComponent(name) + "/cover", { headers: headers() })
        .then(r => (r.ok ? r.blob() : null))
        .then(blob => {
            if (!blob) { letter(); return; }
            const url = URL.createObjectURL(blob);
            const img = document.createElement("img");
            img.alt = "";
            img.onload = () => URL.revokeObjectURL(url);
            img.onerror = () => { URL.revokeObjectURL(url); letter(); };
            img.src = url;
            host.replaceChildren(img);
        })
        .catch(letter);
}

function playlistRow(p) {
    const row = document.createElement("button");
    row.className = "track playlist-row";
    row.onclick = () => openPlaylist(p.name);

    const cover = document.createElement("div");
    cover.className = "cover";
    cover.textContent = p.name.slice(0, 1).toUpperCase();
    loadPlaylistCover(cover, p.name);

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
    art.textContent = pl.name.slice(0, 1).toUpperCase();
    loadPlaylistCover(art, pl.name);

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

    /* Соседние перестановки и перетаскивание годятся, пока список короткий.
     * В подборке на тысячу треков подняться наверх соседними шагами нельзя,
     * а тащить мышью через тысячу строк — тем более. Поэтому рядом стоят
     * два прыжка сразу на край. */
    const top = smallButton("⤒", "В начало подборки", () => moveTrack(position, 0));
    const up = smallButton("↑", "Выше", () => moveTrack(position, position - 1));
    const down = smallButton("↓", "Ниже", () => moveTrack(position, position + 1));
    const bottom = smallButton("⤓", "В конец подборки",
        () => moveTrack(position, player.playlist.entries.length - 1));
    const drop = smallButton("×", "Убрать из подборки", () => removeAt(position));
    drop.classList.add("danger");

    /* Трек и так на своём краю — прыгать некуда. */
    if (position === 0) top.disabled = true;
    if (position === player.playlist.entries.length - 1) bottom.disabled = true;

    row.append(handle, info, top, up, down, bottom, drop);
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
/* Добавить трек из фонотеки в подборку.
 *
 * Строка фонотеки заменяется на выбор подборки — тем же приёмом, что и
 * подтверждение удаления, чтобы не заводить модальных окон. Список подборок
 * читается в момент открытия: он короткий, а держать его свежим между
 * устройствами всё равно пришлось бы перечитыванием.
 *
 * Запись идёт тем же путём, что и перестановка треков: читаем текущий список
 * вместе с номером версии, дописываем путь в конец и отправляем целиком. Если
 * подборку успели изменить с телефона, сервер ответит 409 и мы честно скажем
 * об этом, а не затрём чужую правку.
 */
async function askAddToPlaylist(card, track) {
    const box = document.createElement("div");
    box.className = "confirm";

    const head = document.createElement("h3");
    head.textContent = "В какую подборку?";

    const note = document.createElement("p");
    note.textContent = "Читаю список…";

    const list = document.createElement("div");
    list.className = "rows";

    const row = document.createElement("div");
    row.className = "row";
    const cancel = document.createElement("button");
    cancel.className = "ghost grow";
    cancel.textContent = "Отмена";
    cancel.onclick = () => box.replaceWith(card);
    row.appendChild(cancel);

    box.append(head, note, list, row);
    card.replaceWith(box);

    let all;
    try {
        const r = await fetch("/api/playlists", { headers: headers() });
        if (!r.ok) throw new Error("Ошибка " + r.status);
        all = await r.json();
    } catch (e) {
        note.textContent = e.message;
        return;
    }

    if (!all.length) {
        note.textContent = "Пока ни одной подборки. Создай её в разделе с подборками.";
        return;
    }

    note.textContent = track.title;
    for (const p of all) {
        const b = document.createElement("button");
        b.className = "ghost";
        b.textContent = p.name + " · " + p.tracks;
        b.onclick = () => addToPlaylist(box, card, note, cancel, p.name, track);
        list.appendChild(b);
    }
}

async function addToPlaylist(box, card, note, cancel, name, track) {
    for (const b of box.querySelectorAll("button")) b.disabled = true;
    note.textContent = "Добавляю…";
    try {
        const url = "/api/playlists/" + encodeURIComponent(name) + "/tracks";
        const got = await fetch(url, { headers: headers() });
        if (!got.ok) throw new Error("Ошибка " + got.status);
        const pl = await got.json();

        const paths = pl.entries.map(e => e.path);
        if (paths.includes(track.path)) {
            note.textContent = "Уже в «" + name + "»";
            cancel.disabled = false;
            cancel.textContent = "Закрыть";
            return;
        }
        paths.push(track.path);

        const put = await fetch(url, {
            method: "PUT",
            headers: { ...headers(), "Content-Type": "application/json" },
            body: JSON.stringify({ paths, revision: pl.revision }),
        });
        if (put.status === 409) {
            note.textContent = "Подборку изменили с другого устройства. Открой ещё раз.";
            cancel.disabled = false;
            cancel.textContent = "Закрыть";
            return;
        }
        if (!put.ok) {
            const data = await put.json().catch(() => ({}));
            throw new Error(data.detail || ("Ошибка " + put.status));
        }

        note.textContent = "Добавлено в «" + name + "»";
        /* Если эта же подборка открыта рядом — показать её новой. */
        if (player.playlist && player.playlist.name === name) await openPlaylist(name);
        playlists();
        setTimeout(() => box.replaceWith(card), 1200);
    } catch (e) {
        note.textContent = e.message;
        for (const b of box.querySelectorAll("button")) b.disabled = false;
        cancel.textContent = "Закрыть";
    }
}

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

/* Кнопки должны показывать своё состояние сразу, а не после первого нажатия. */
renderPlayerModes();

// Пробел — играть/пауза, но только когда не печатаешь.
//
// Раньше это жило в оболочке GTK (desktop/local-spotify.py) и было сломано:
// обработчик на окне срабатывает РАНЬШЕ, чем WebKit отдаёт событие странице,
// поэтому пробел уходил в паузу даже посреди набора в поиске. Здесь, в самой
// странице, видно document.activeElement — и решение принимается верно.
// Заодно пробел появился и в браузерной версии, где его не было вовсе.
document.addEventListener("keydown", (event) => {
    if (event.code !== "Space" && event.key !== " ") return;
    if (event.ctrlKey || event.altKey || event.metaKey) return;

    const el = document.activeElement;
    if (el) {
        const tag = el.tagName;
        // В поле ввода пробел — это пробел.
        if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
        if (el.isContentEditable) return;
        // На кнопке пробел — это нажатие кнопки, не трогаем.
        if (tag === "BUTTON" || el.getAttribute("role") === "button") return;
    }

    event.preventDefault();  // иначе страница ещё и прокрутится
    togglePlay();
});
