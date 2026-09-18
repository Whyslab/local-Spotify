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
    queueSource: null,   // {kind: "playlist", name} | {kind: "library"} | null

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
    /* На широком экране очередь занимает правую колонку — ту, где обложка.
     * Всплывающим окном она перекрывала список, ради которого её и открывают,
     * а на узком экране колонок нет вовсе, и там окно остаётся окном. */
    const app = document.querySelector(".app");
    if (app) app.classList.toggle("queue-open", !panel.hidden);
    const button = document.getElementById("playerQueueButton");
    if (button) {
        button.classList.toggle("is-on", !panel.hidden);
        button.setAttribute("aria-pressed", String(!panel.hidden));
    }
    if (!panel.hidden) {
        renderQueuePanel();
        /* Находки приезжают отдельно и позже: очередь не должна ждать чужой
         * сервис, чтобы открыться. */
        externalFinds().then(() => renderQueuePanel());
    }
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

    /* Очередь начинается с того, что играет, а не с начала маршрута. Сыгранное
     * показывать незачем: панель открывают с вопросом «что дальше», а
     * отыгранные строки отодвигали ответ вниз — при длинной подборке до него
     * приходилось прокручивать. */
    const from = at >= 0 ? at : 0;

    route.slice(from).forEach((queueIndex, offset) => {
        const position = from + offset;
        const track = player.queue[queueIndex];
        if (!track) return;
        /* Строка — не <button>: внутри неё живёт своя кнопка «откуда играет», а
         * кнопка в кнопке — недопустимая разметка. Роль и tabindex сохраняют
         * доступ с клавиатуры; Enter включает трек, а пробел остаётся за
         * воспроизведением, как и везде на странице. */
        const row = document.createElement("div");
        row.className = "track queue-row";
        row.setAttribute("role", "button");
        row.tabIndex = 0;
        row.onkeydown = (event) => {
            if (event.key !== "Enter") return;
            event.preventDefault();
            row.click();
        };
        if (queueIndex === player.index) row.classList.add("is-playing");
        row.onclick = () => { player.orderAt = position; playAt(queueIndex); renderQueuePanel(); };

        /* «Откуда это» — в очереди все треки на одно лицо, а списком их видно
         * в родном окружении: в своей подборке или в фонотеке рядом с соседями. */
        const reveal = document.createElement("button");
        reveal.className = "icon-button small queue-reveal";
        reveal.textContent = "↗";
        reveal.title = "Показать, откуда играет";
        reveal.setAttribute("aria-label", "Показать, откуда играет");
        reveal.onclick = (event) => { event.stopPropagation(); revealTrack(track); };

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
        row.appendChild(reveal);
        /* Видно, что трек пришёл со стороны, а не из подборки. Иначе
         * непонятно, откуда он взялся, и это выглядит ошибкой. */
        if (track.outside) {
            const mark = document.createElement("span");
            mark.className = "queue-outside";
            mark.textContent = "находка";
            mark.title = "Этого трека нет в подборке — подобран по темпу";
            row.appendChild(mark);
        }
        box.appendChild(row);
    });

    /* Находок нет — и раздела нет: пустой заголовок выглядел бы поломкой. */
    const finds = externalFindsReady();
    if (!finds.length) return;
    const head = document.createElement("div");
    head.className = "queue-finds-head";
    head.textContent = "Можно добавить — этого нет в фонотеке";
    box.appendChild(head);
    for (const find of finds) box.appendChild(externalFindRow(find));
}

/* «+» ничего не качает. Он открывает поиск с готовым запросом: две загрузки
 * одной песни различаются длиной и каналом, и выбор остаётся за человеком —
 * то же правило, по которому /api/search сам ничего не выбирает. */
function externalFindRow(find) {
    const artist = find.artist || "";
    const title = find.title || "";
    const query = artist ? `${artist} — ${title}` : title;

    const row = document.createElement("div");
    row.className = "track queue-row queue-find";

    const info = document.createElement("div");
    info.className = "track-info";
    const line = document.createElement("div");
    line.className = "track-title";
    line.textContent = title;
    info.appendChild(line);
    if (artist) {
        const who = document.createElement("div");
        who.className = "track-artist";
        who.textContent = artist;
        info.appendChild(who);
    }
    row.appendChild(info);

    const add = document.createElement("button");
    add.className = "icon-button small";
    add.textContent = "+";
    add.title = "Искать на YouTube";
    add.setAttribute("aria-label", `Искать «${query}» на YouTube`);
    add.onclick = () => {
        switchView("viewAdd");
        const field = document.getElementById("searchQuery");
        if (field) field.value = query;
        runSearch();
        /* Панель закрывается: она стоит поверх выдачи, за которой человек
         * и нажал «+». */
        toggleQueuePanel();
    };
    row.appendChild(add);
    return row;
}

/* `source` — откуда эта очередь: подборка с именем или фонотека. Нужен для
 * кнопки «показать, откуда играет»: из самой очереди этого не видно, треки в
 * ней одинаковые независимо от происхождения. */
/* Показать трек там, откуда он играет.
 *
 * Очередь помнит своё происхождение, но точного места в списке не знает: трек
 * мог попасть в неё и из подборки, и подмешиванием. Поэтому подборка
 * открывается целиком, а нужная строка подсвечивается и подъезжает к глазам;
 * для фонотеки то же делает поиск по названию — двести строк за раз она всё
 * равно не покажет.
 */
async function revealTrack(track) {
    if (!track) return;
    const source = player.queueSource;

    if (source && source.kind === "playlist") {
        await openPlaylist(source.name);
        flashTrackRow(track.path);
        return;
    }

    /* Фонотека: подставляем название в поиск — иначе трек может лежать за
     * двухсотой строкой и на экране его не будет вовсе. */
    switchView("viewLibrary");
    const field = document.getElementById("librarySearch");
    if (field) field.value = track.title || track.artist || "";
    await library();
    flashTrackRow(track.path);
}

function flashTrackRow(path) {
    const row = document.querySelector(`[data-track-path="${CSS.escape(path)}"]`);
    if (!row) return;
    row.scrollIntoView({ block: "center", behavior: "smooth" });
    row.classList.add("is-found");
    setTimeout(() => row.classList.remove("is-found"), 2000);
}

function playQueue(tracks, startAt = 0, mode = "manual", source = null) {
    player.queue = tracks;
    player.queueMode = mode;
    player.queueSource = source;
    buildOrder(startAt);
    playAt(startAt);
}

/* ---------------- Shuffling ---------------- */

async function loadShuffle(mode, playlist = "", noteId = "shuffleNote") {
    const note = document.getElementById(noteId);
    if (note) note.textContent = "Собираю очередь…";
    const say = text => { if (note) note.textContent = text; };
    try {
        const url = `/api/shuffle?size=50&mode=${mode}`
            + (playlist ? `&playlist=${encodeURIComponent(playlist)}` : "");
        const r = await fetch(url, { headers: headers() });
        const data = await r.json();
        if (!r.ok) { say(data.detail || ("Ошибка " + r.status)); return; }
        if (!data.queue.length) { say("Нечего играть."); return; }

        playQueue(data.queue, 0, mode);

        if (mode !== "smart") { say(""); return; }

        const report = data.report || {};
        if (playlist) {
            /* Про подборку интересно другое: сколько в очереди своего и
             * сколько пришло со стороны. Разброс темпа тут — мелкий шрифт. */
            say(`Своих ${data.queue.length - data.outside}, подобрано ещё ${data.outside}`
                + ` — разброс темпа до ${report.max_tempo_jump ?? "—"} BPM`);
        } else if (data.analysed < data.total) {
            /* Said plainly, because it is the difference between "it works"
             * and "it has nothing to work with yet": tempo cannot order a
             * library that has not been measured. */
            say(`Измерено ${data.analysed} из ${data.total} — остальные ставятся без учёта темпа`);
        } else {
            say(`Разброс темпа до ${report.max_tempo_jump ?? "—"} BPM, артистов ${report.distinct_artists}`);
        }
    } catch (e) {
        say(e.message);
    }
}

function playSmartShuffle() { loadShuffle("smart"); }
function playPlainShuffle() { loadShuffle("plain"); }

/* Перемешать подборку, не запирая очередь внутри неё. */
function shufflePlaylist() {
    if (!player.playlist || !player.playlist.entries.length) return;
    loadShuffle("smart", player.playlist.name, "playlistNote");
}

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
const lyrics = { path: null, lines: [], index: -1, box: null, follow: true };

function loadLyrics(track) {
    const box = document.getElementById("lyricsBody");
    if (!box) return;
    const title = document.getElementById("lyricsTitle");
    const artist = document.getElementById("lyricsArtist");
    if (title) title.textContent = track.title || track.path;
    if (artist) artist.textContent = track.artist || "";
    lyrics.follow = true;
    updateSyncButton();
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
    /* Пролистал сам — не выдёргиваем обратно: человек читает не ту строку, что
     * звучит, и это его право. Вернуть слежение можно кнопкой. */
    if (!lyrics.follow) return;
    if (i >= 0 && rows[i]) {
        const row = rows[i];
        const wanted = row.offsetTop - lyrics.box.clientHeight / 2 + row.clientHeight / 2;
        lyrics.scrolling = true;
        lyrics.box.scrollTo({ top: Math.max(0, wanted), behavior: "smooth" });
        clearTimeout(lyrics.settle);
        lyrics.settle = setTimeout(() => { lyrics.scrolling = false; }, 700);
    }
}

/* ---------------- Текст: кнопка и слежение ---------------- */

function updateSyncButton() {
    const button = document.getElementById("lyricsSync");
    if (button) button.hidden = lyrics.follow || !lyrics.lines.length;
}

function resyncLyrics() {
    lyrics.follow = true;
    updateSyncButton();
    highlightLyric(true);
}

/* Откуда пришли в текст. Закрывая его, возвращаемся туда же: раньше выход
 * всегда вёл в фонотеку — читал текст, закрыл, и ты не там, где был. */
let viewBeforeLyrics = null;

function toggleLyricsView() {
    const button = document.getElementById("playerLyricsButton");
    const open = activeView !== "viewLyrics";
    if (open) viewBeforeLyrics = activeView;
    switchView(open ? "viewLyrics" : (viewBeforeLyrics || "viewHome"));
    if (button) {
        button.classList.toggle("is-on", open);
        button.setAttribute("aria-pressed", String(open));
    }
    if (open) {
        const track = player.queue[player.index];
        if (track) loadLyrics(track);
        else renderNoLyrics(document.getElementById("lyricsBody"), "Ничего не играет");
    }
}

/* Ручная прокрутка выключает слежение. Отличить её от своей помогает флажок:
 * плавная прокрутка к строке тоже приходит сюда событием. */
function watchLyricsScroll() {
    const box = document.getElementById("lyricsBody");
    if (!box) return;
    box.addEventListener("scroll", () => {
        if (lyrics.scrolling) return;
        if (!lyrics.follow) return;
        lyrics.follow = false;
        updateSyncButton();
    }, { passive: true });
}

watchLyricsScroll();

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
    if (hasOpenChoice("playlists")) return;
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
        /* Список перерисовывается фоновым опросом — без этого набранный поиск
         * сбрасывался бы каждые три секунды прямо под руками. */
        filterPlaylists();
    } catch (e) { /* the next poll retries */ }
}

/* Те же подборки строками в рельсе — с обложкой.
 *
 * Раньше здесь были только название и счётчик: считалось, что в 232 пикселя
 * обложка не влезет. Влезает: 34 пикселя слева, текст рядом. Подборку узнаёшь
 * по картинке быстрее, чем читаешь название. */
function renderRail(data) {
    const rail = document.getElementById("railPlaylists");
    if (!rail) return;
    const open = player.playlist ? player.playlist.name : null;
    rail.replaceChildren();
    for (const p of data) {
        const item = document.createElement("button");
        item.className = "rail-item" + (p.name === open ? " is-active" : "");
        item.onclick = () => openPlaylist(p.name);

        const art = document.createElement("span");
        art.className = "rail-art";
        art.textContent = p.name.slice(0, 1).toUpperCase();
        loadPlaylistCover(art, p.name);

        const text = document.createElement("span");
        text.className = "rail-text";
        const name = document.createElement("b");
        name.textContent = p.name;
        const count = document.createElement("small");
        count.textContent = p.tracks === 1 ? "1 трек" : `${p.tracks} треков`;
        text.append(name, count);

        item.append(art, text);
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
    /* Через общий кэш обложек: рельс перерисовывается каждым опросом, и без
     * него обложка подборки мигала бы на букву несколько раз в минуту. */
    coverUrl("playlist:" + name, "/api/playlists/" + encodeURIComponent(name) + "/cover")
        .then(url => {
            if (!url) { letter(); return; }
            const img = document.createElement("img");
            img.alt = "";
            img.onerror = letter;
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
    filterOpenPlaylist();
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

    // Обложка — как во всех остальных списках.
    const cover = document.createElement("div");
    cover.className = "cover";
    loadTrackCover(cover, entry.path);

    const info = document.createElement("div");
    info.className = "track-info";
    info.onclick = () => playQueue(player.playlist.entries, position, "manual",
        { kind: "playlist", name: player.playlist.name });
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

    /* Пять кнопок подряд у каждой строки — это пять кнопок, умноженные на
     * тысячу треков: ряд значков, в котором нечего читать. Всё то же самое
     * теперь живёт за одной кнопкой, и там у действий есть названия словами.
     * Перетаскивание никуда не делось — оно быстрее любого меню, когда
     * двигать надо на строку-другую. */
    const more = smallButton("⋯", "Что сделать с треком", (event) => {
        event.stopPropagation();
        openTrackMenu(more, position);
    });
    more.setAttribute("aria-haspopup", "menu");
    more.setAttribute("aria-expanded", "false");

    row.append(handle, cover, info, more);
    return row;
}

/* ---------------- Меню строки ----------------
 *
 * Открытое меню одно на весь список: два раскрытых меню — это вопрос «какое
 * из них про этот трек».
 */
let openMenu = null;

function closeTrackMenu() {
    if (!openMenu) return;
    const { menu, button } = openMenu;
    menu.remove();
    button.setAttribute("aria-expanded", "false");
    openMenu = null;
    document.removeEventListener("keydown", menuKeydown, true);
    document.removeEventListener("pointerdown", menuPointerDown, true);
}

function menuKeydown(event) {
    if (event.key !== "Escape") return;
    const button = openMenu && openMenu.button;
    closeTrackMenu();
    /* Возвращаем указатель туда, откуда меню открыли: иначе после Esc фокус
     * оказывается в начале страницы. */
    if (button) button.focus();
}

function menuPointerDown(event) {
    if (openMenu && !openMenu.menu.contains(event.target) && event.target !== openMenu.button) {
        closeTrackMenu();
    }
}

function openTrackMenu(button, position) {
    const wasMine = openMenu && openMenu.button === button;
    closeTrackMenu();
    if (wasMine) return;  // повторное нажатие закрывает

    const total = player.playlist.entries.length;
    const menu = document.createElement("div");
    menu.className = "row-menu";
    menu.setAttribute("role", "menu");

    const item = (label, title, onClick, disabled) => {
        const b = document.createElement("button");
        b.className = "row-menu-item";
        b.textContent = label;
        b.setAttribute("role", "menuitem");
        if (title) b.title = title;
        b.disabled = Boolean(disabled);
        b.onclick = () => { closeTrackMenu(); onClick(); };
        return b;
    };

    menu.append(
        item("В начало подборки", "", () => moveTrack(position, 0), position === 0),
        item("Выше на один", "", () => moveTrack(position, position - 1), position === 0),
        item("Ниже на один", "", () => moveTrack(position, position + 1), position === total - 1),
        item("В конец подборки", "", () => moveTrack(position, total - 1), position === total - 1),
        /* Прыжок на номер: в подборке на тысячу треков «выше на один» бесполезно,
         * а тащить мышью через весь список — тем более. */
        item("Переместить на место…", "", () => askForPosition(button, position, total)),
    );

    const separator = document.createElement("div");
    separator.className = "row-menu-line";
    menu.appendChild(separator);
    menu.appendChild(item("Заменить другой версией…", "", () => askForReplacement(button, position)));
    menu.appendChild(item("Убрать из подборки", "", () => removeAt(position)));
    menu.lastChild.classList.add("is-danger");

    button.insertAdjacentElement("afterend", menu);
    button.setAttribute("aria-expanded", "true");
    openMenu = { menu, button };
    document.addEventListener("keydown", menuKeydown, true);
    document.addEventListener("pointerdown", menuPointerDown, true);
    const first = menu.querySelector(".row-menu-item:not([disabled])");
    if (first) first.focus();
}

/* Спрашиваем номер прямо в списке, а не браузерным окном: тем же правилом
 * живёт подтверждение удаления в app.js — родное окно на телефоне слишком
 * легко смахнуть мимоходом, и оно ничего не объясняет. */
/* Замена трека: скачалась не та версия — концертник вместо студийной, дорожка
 * из клипа вместо трека. Даём два пути: ссылка на YouTube или файл с диска.
 * Трек не исчезает и не уезжает в конец — новая версия встаёт на его место,
 * а старая уходит в корзину. Всё после загрузки, на сервере: страницу можно
 * закрыть. */
function askForReplacement(button, position) {
    const entry = player.playlist.entries[position];
    closeTrackMenu();
    if (!entry) return;

    const form = document.createElement("form");
    form.className = "row-menu row-menu-form";

    const label = document.createElement("label");
    label.className = "row-menu-label";
    label.textContent = "Правильная версия: ссылка на YouTube";

    const field = document.createElement("input");
    field.type = "url";
    field.placeholder = "https://youtu.be/…";
    field.className = "row-menu-input";

    const go = document.createElement("button");
    go.type = "submit";
    go.className = "row-menu-item";
    go.textContent = "Заменить по ссылке";

    const or = document.createElement("label");
    or.className = "row-menu-item";
    or.textContent = "…или выбрать файл";
    const picker = document.createElement("input");
    picker.type = "file";
    picker.accept = ".mp3,.m4a,.flac,.opus,.ogg";
    picker.hidden = true;
    picker.onchange = () => {
        if (picker.files && picker.files[0]) sendReplacement(entry.path, null, picker.files[0]);
        closeTrackMenu();
    };
    or.appendChild(picker);

    form.onsubmit = (event) => {
        event.preventDefault();
        const url = field.value.trim();
        closeTrackMenu();
        if (url) sendReplacement(entry.path, url, null);
    };

    form.append(label, field, go, or);
    button.insertAdjacentElement("afterend", form);
    button.setAttribute("aria-expanded", "true");
    openMenu = { menu: form, button };
    document.addEventListener("keydown", menuKeydown, true);
    document.addEventListener("pointerdown", menuPointerDown, true);
    field.focus();
}

async function sendReplacement(path, url, file) {
    setPlaylistNote("Ставлю в очередь замену…");
    try {
        let r;
        if (file) {
            const body = new FormData();
            body.append("file", file);
            r = await fetch("/api/replace-file?path=" + encodeURIComponent(path), {
                method: "POST",
                headers: headers(),
                body,
            });
        } else {
            r = await fetch("/api/replace", {
                method: "POST",
                headers: { ...headers(), "Content-Type": "application/json" },
                body: JSON.stringify({ path, url }),
            });
        }
        const data = await r.json();
        if (!r.ok) { setPlaylistNote(data.detail || "Не удалось заменить"); return; }
        setPlaylistNote("Версия качается. Когда доедет — встанет на это же место.");
    } catch (e) {
        setPlaylistNote("Не удалось заменить: " + e.message);
    }
}

function askForPosition(button, from, total) {
    closeTrackMenu();

    const form = document.createElement("form");
    form.className = "row-menu row-menu-form";

    const label = document.createElement("label");
    label.className = "row-menu-label";
    label.textContent = `На какое место? 1 — ${total}`;

    const field = document.createElement("input");
    field.type = "number";
    field.min = "1";
    field.max = String(total);
    field.value = String(from + 1);
    field.className = "row-menu-input";

    const go = document.createElement("button");
    go.type = "submit";
    go.className = "row-menu-item";
    go.textContent = "Переместить";

    form.onsubmit = (event) => {
        event.preventDefault();
        const wanted = parseInt(field.value, 10);
        closeTrackMenu();
        if (!Number.isFinite(wanted)) return;
        /* Человек считает с единицы, список — с нуля. */
        moveTrack(from, Math.min(total - 1, Math.max(0, wanted - 1)));
    };

    form.append(label, field, go);
    button.insertAdjacentElement("afterend", form);
    button.setAttribute("aria-expanded", "true");
    openMenu = { menu: form, button };
    document.addEventListener("keydown", menuKeydown, true);
    document.addEventListener("pointerdown", menuPointerDown, true);
    field.focus();
    field.select();
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
        playQueue(player.playlist.entries, 0, "manual",
            { kind: "playlist", name: player.playlist.name });
    }
}

async function createPlaylist() {
    const field = document.getElementById("newPlaylist");
    const note = document.getElementById("playlistsNote");
    const name = field.value.trim();
    /* Молчание на пустое поле читается как поломка кнопки: нажал — ничего не
     * произошло, и почему, страница не говорит. */
    if (!name) {
        note.textContent = "Впиши название: подборка станет файлом с этим именем.";
        field.focus();
        return;
    }
    const r = await fetch("/api/playlists", {
        method: "POST",
        headers: { ...headers(), "Content-Type": "application/json" },
        body: JSON.stringify({ name, paths: [] }),
    });
    const data = await r.json();
    if (!r.ok) { note.textContent = data.detail || "Ошибка"; return; }
    field.value = "";
    note.textContent = "";
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
    // Старую из кэша выкинуть, иначе новая не появится до перезагрузки.
    forgetCover("playlist:" + player.playlist.name);
    setPlaylistNote("");
    renderPlaylist();
    playlists();
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
        /* В начало, а не в конец: только что добавленное — это то, что
         * хочется услышать сейчас, а не через тысячу треков. */
        paths.unshift(track.path);

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
    playQueue(rows, rows.findIndex(r => r.path === track.path), "manual", { kind: "library" });
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

/* ---------------- Громкость ----------------
 *
 * Своя громкость нужна там, где страница — не единственное, что звучит: на
 * ноутбуке системный ползунок тянет за собой всё сразу.
 *
 * На iOS её не будет, и это не недоделка. Apple отдаёт громкость физическим
 * кнопкам: `audio.volume` там только для чтения и всегда возвращает 1,
 * слушается один `muted`. Обойти это можно было бы через Web Audio, но там
 * звук замолкает, стоит странице уйти в фон, — плеер потерял бы фоновое
 * воспроизведение ради ползунка. Поэтому блок просто не показывается, и
 * проверяется это опытом, а не разбором названия браузера: подставляем
 * значение и смотрим, осталось ли оно.
 */

const VOLUME_KEY = "playerVolume";

function volumeIsAdjustable() {
    const before = player.audio.volume;
    try {
        player.audio.volume = 0.42;
        return Math.abs(player.audio.volume - 0.42) < 0.01;
    } finally {
        player.audio.volume = before;
    }
}

/* Заполнение ползунка: WebKit не красит пройденную часть сам, поэтому доля
 * уходит в CSS переменной, а рисует её style.css. */
function paintVolume(level) {
    const slider = document.getElementById("playerVolumeRange");
    if (slider) slider.style.setProperty("--vol", String(level));
}

function updateVolumeIcon() {
    const icon = document.getElementById("playerVolumeIcon");
    const button = document.getElementById("playerMute");
    if (!icon || !button) return;

    const silent = player.audio.muted || player.audio.volume === 0;
    icon.setAttribute("d", silent
        ? "M4 9v6h4l5 4V5L8 9zM17 9l4 6M21 9l-4 6"
        : "M4 9v6h4l5 4V5L8 9zM16 9a4 4 0 0 1 0 6");
    button.setAttribute("aria-label", silent ? "Включить звук" : "Выключить звук");
    button.setAttribute("title", silent ? "Включить звук" : "Выключить звук");
}

function setVolumeFromSlider(value) {
    const level = Math.min(100, Math.max(0, Number(value) || 0)) / 100;
    player.audio.muted = false;
    player.audio.volume = level;
    paintVolume(level);
    try { localStorage.setItem(VOLUME_KEY, String(level)); } catch (e) { /* приватное окно */ }
    updateVolumeIcon();
}

function toggleMute() {
    player.audio.muted = !player.audio.muted;
    /* Нажал «без звука» на нуле — это просьба вернуть звук, а не поставить
     * беззвучное воспроизведение: поднимаем ползунок до половины. */
    if (!player.audio.muted && player.audio.volume === 0) setVolumeFromSlider(50);
    const slider = document.getElementById("playerVolumeRange");
    if (slider) slider.value = String(Math.round(player.audio.volume * 100));
    updateVolumeIcon();
}

function initVolume() {
    const box = document.getElementById("playerVolume");
    const slider = document.getElementById("playerVolumeRange");
    if (!box || !slider || !volumeIsAdjustable()) return;

    let saved = 1;
    try {
        const stored = parseFloat(localStorage.getItem(VOLUME_KEY));
        if (Number.isFinite(stored) && stored >= 0 && stored <= 1) saved = stored;
    } catch (e) { /* приватное окно — играем на полной */ }

    player.audio.volume = saved;
    slider.value = String(Math.round(saved * 100));
    paintVolume(saved);
    box.hidden = false;
    updateVolumeIcon();
}

initVolume();

/* Чем в последний раз двигали фокус. Обновляется на перехвате, до чужих
 * обработчиков, чтобы к моменту нажатия значение было уже верным. */
let focusCameFromPointer = false;
document.addEventListener("pointerdown", () => { focusCameFromPointer = true; }, true);
document.addEventListener("keydown", (event) => {
    if (event.key === "Tab") focusCameFromPointer = false;
}, true);

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
        // В поле ввода пробел — это пробел. Ползунок громкости — исключение:
        // это тоже <input>, но печатать в нём нечего, а отнимать у него пробел
        // значит ломать «пробел всегда играет/ставит на паузу».
        const isSlider = tag === "INPUT" && el.type === "range";
        if (!isSlider && (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT")) return;
        if (el.isContentEditable) return;
        /* Кнопка под фокусом — самый частый случай в жизни: нажал «Добавить»
         * или выбрал подборку, фокус остался там, и пробел снова жал ту же
         * кнопку вместо паузы.
         *
         * Отличаем по тому, как на кнопку попали. Ходить по странице табом —
         * значит управлять клавиатурой, и там пробел обязан нажимать кнопку.
         * Пришли мышью — пробел про воспроизведение.
         *
         * :focus-visible для этого не годится, хотя и выглядит созданным ровно
         * для такого случая: браузер включает его в момент самого нажатия, и
         * внутри обработчика он истинный всегда — проверено. */
        const isButton = tag === "BUTTON" || el.getAttribute("role") === "button";
        if (isButton && !focusCameFromPointer) return;
    }

    event.preventDefault();  // иначе страница ещё и прокрутится
    togglePlay();
});
