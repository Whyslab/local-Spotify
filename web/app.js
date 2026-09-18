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
    if (activeView === "viewHome") home();
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

/* Что действительно ждёт работы. Готовое сюда не входит: /api/tasks отдаёт
 * последние пятьдесят задач любого состояния, и раньше все пятьдесят попадали
 * в «Очередь» — со счётчиком «50» и готовыми треками в списке. Выглядело это
 * как «трек висит в очереди», хотя он уже лежал в фонотеке.
 *
 * Ошибка остаётся здесь намеренно: это незаконченная работа, о ней надо знать. */
const QUEUE_STATUSES = new Set(["queued", "downloading", "tagging", "error"]);

/* Сколько последних готовых показывать. Достаточно, чтобы убедиться «трек
 * доехал», и мало, чтобы список не превращался в журнал. */
const RECENT_DONE_LIMIT = 5;

async function tasks() {
    const box = document.getElementById("tasks");
    const empty = document.getElementById("tasksEmpty");
    const count = document.getElementById("queueCount");
    const recentBox = document.getElementById("recentTasks");
    const recentHead = document.getElementById("recentHead");

    try {
        const r = await fetch("/api/tasks", { headers: headers() });
        if (!r.ok) return;
        const data = await r.json();

        const queue = data.filter(t => QUEUE_STATUSES.has(t.status));
        const recent = data.filter(t => t.status === "done").slice(0, RECENT_DONE_LIMIT);

        count.textContent = queue.length ? `${queue.length}` : "";
        empty.hidden = queue.length > 0;
        box.replaceChildren();

        for (const t of queue) {
            box.appendChild(trackRow({
                title: t.title || t.url || "—",
                artist: t.artist || "",
                status: t.status,
                error: t.error,
            }));
        }

        if (recentBox && recentHead) {
            recentHead.hidden = recent.length === 0;
            recentBox.replaceChildren();
            for (const t of recent) {
                recentBox.appendChild(trackRow({
                    title: t.title || t.url || "—",
                    artist: t.artist || "",
                    status: t.status,
                    error: t.error,
                }));
            }
        }
    } catch (e) {
        // Polling loop - a transient network hiccup shouldn't throw to console.
    }
}

function trackRow({ title, artist, status, error }) {
    const card = document.createElement("div");
    card.className = "track";

    /* У задачи обложки нет и быть не может: файла ещё нет на диске. Пустой
     * серый квадрат в каждой из пятидесяти строк выглядел как ненагрузившаяся
     * картинка, поэтому его тут просто нет. */
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

    card.append(info, badge);
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
        }

        /* The living numbers, which move while you watch: what is downloading
         * and what is waiting to reach the phone. Kept apart from the counts
         * above, which change about once a day. */
        renderRailQueue(data.active_tasks, data.navidrome_pending);

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

/* Что качается — в нижней части рельсы.
 *
 * Здесь раньше жил второй, крошечный плеер: название, полоска, время. Он
 * повторял то, что и так есть в плеере внизу экрана, а главное — рос снизу и
 * отжимал список подборок, из-за чего при играющем треке до нижней подборки
 * было не дотянуться. Теперь тут только то, чего больше нигде не видно:
 * на каком этапе каждая загрузка.
 *
 * Данные приходят вместе с /health, который и так опрашивается: отдельный
 * запрос каждые три секунды ради этой строчки был бы лишним шумом и в сети,
 * и в журнале службы.
 */
const TASK_STAGE = {
    queued: "ждёт очереди",
    downloading: "качается",
    tagging: "проставляю теги",
    error: "ошибка",
    /* Готовые держатся пару минут — ровно чтобы увидеть, что трек доехал.
       Дальше они живут в «Недавно добавлены» на странице добавления. */
    done: "готово",
};

function renderRailQueue(tasks, pending) {
    const box = document.getElementById("railWork");
    if (!box) return;
    box.replaceChildren();

    const rows = Array.isArray(tasks) ? tasks : [];
    for (const task of rows) {
        const row = document.createElement("div");
        row.className = "rail-job" + (task.status === "error" ? " is-error" : "");

        const what = document.createElement("b");
        what.textContent = [task.artist, task.title].filter(Boolean).join(" — ") || task.url || "—";

        const stage = document.createElement("small");
        stage.textContent = TASK_STAGE[task.status] || task.status;

        row.append(what, stage);
        box.appendChild(row);
    }

    /* Строка про Navidrome — не про загрузки, но про то же ожидание: сколько
       готовых треков ещё не доехало до телефона. */
    if (pending) {
        const wait = document.createElement("div");
        wait.className = "rail-jobs";
        wait.textContent = pending + " ждёт Navidrome";
        box.appendChild(wait);
    }

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

/* ---------------- Главная ----------------
 *
 * Всё на этой странице считается из того, что уже лежит на диске. Единственный
 * выход наружу — список похожих артистов, и он кэшируется навсегда: соседство
 * артистов меняется годами, а не днями.
 *
 * Подборки по настроению берут границы от самой фонотеки, а не из воздуха:
 * «спокойное» при абсолютном пороге дало бы здесь восемнадцать треков из
 * тысячи с лишним. Самая спокойная четверть есть у любого собрания музыки.
 */
/* ---------------- Находки извне ----------------
 *
 * Треки, которых в фонотеке нет вовсе. Спрашиваются один раз за жизнь страницы:
 * ими пользуются и очередь, и главная, а Deezer от повторных вопросов новых
 * артистов не придумает. Первый запрос ходит в сеть (секунды), дальше сервер
 * отвечает из своего кэша мгновенно.
 *
 * Ничего не скачивается: находка — повод открыть поиск и выбрать версию руками.
 */
let externalFindsCache = null;
let externalFindsPromise = null;

function externalFindsReady() {
    return externalFindsCache || [];
}

function externalFinds() {
    if (externalFindsCache) return Promise.resolve(externalFindsCache);
    if (externalFindsPromise) return externalFindsPromise;

    externalFindsPromise = fetch("/api/discover-external?limit=12", { headers: headers() })
        .then(r => (r.ok ? r.json() : { tracks: [] }))
        .then(data => {
            externalFindsCache = Array.isArray(data.tracks) ? data.tracks : [];
            return externalFindsCache;
        })
        .catch(() => {
            /* Находки — дополнение, а не часть страницы: без них так без них. */
            externalFindsCache = [];
            return externalFindsCache;
        });
    return externalFindsPromise;
}

/* «+» ничего не качает: открывает поиск с готовым запросом. Две загрузки одной
 * песни различаются длиной и каналом, и выбор остаётся за человеком — то же
 * правило, по которому /api/search сам ничего не выбирает. */
function searchForFind(find) {
    const query = find.artist ? `${find.artist} — ${find.title}` : find.title;
    switchView("viewAdd");
    const field = document.getElementById("searchQuery");
    if (field) field.value = query;
    runSearch();
}

let homeCache = null;
/* Из каких данных собран экран. Главная перерисовывалась каждые три секунды
 * фоновым опросом — при одних и тех же данных, — и каждая перерисовка сбрасывала
 * листание полок вбок: пролистал ряд обложек, через пару секунд он снова в
 * начале. Данные те же — экран не трогаем. */
let homeRendered = null;

/* Строка-заглушка тоже строится узлами. В этом файле присваивание innerHTML
 * однажды уже стоило утечки токена из хранилища через подставленное название
 * трека, и тест на это правило стоит именно потому, что тогда оно уплыло
 * незаметно. Исключений нет даже для строки без данных. */
function homeNote(box, text) {
    const note = document.createElement("p");
    note.className = "empty";
    note.textContent = text;
    box.replaceChildren(note);
}

async function home() {
    const box = document.getElementById("homeBody");
    if (!box) return;
    if (homeCache) {
        if (homeRendered !== homeCache) {
            renderHome(homeCache);
            homeRendered = homeCache;
        }
        return;
    }
    try {
        const r = await fetch("/api/home", { headers: headers() });
        if (!r.ok) { homeNote(box, "Не собралось."); return; }
        homeCache = await r.json();
        renderHome(homeCache);
        homeRendered = homeCache;
    } catch (e) {
        homeNote(box, "Не собралось.");
    }
}

function homeShelf(title, hint, tracks, playAll) {
    const card = document.createElement("div");
    card.className = "card home-shelf";

    const head = document.createElement("div");
    head.className = "card-head";
    const h = document.createElement("h2");
    h.textContent = title;
    head.appendChild(h);
    if (hint) {
        const note = document.createElement("span");
        note.className = "muted";
        note.textContent = hint;
        head.appendChild(note);
    }
    if (playAll) {
        const play = document.createElement("button");
        play.className = "primary";
        play.textContent = "Слушать";
        play.onclick = playAll;
        head.appendChild(play);
    }

    /* Полка — это ряд обложек, который листается вбок, а не список строк.
     *
     * Строками это занимало всю ширину экрана под четыреста пикселей текста
     * и рядом с сеткой альбомов выглядело как список дел. Двенадцать плиток,
     * а не сорок: полка на главной — приглашение, а не фонотека. Нажал
     * «Слушать» — играет вся полка целиком. */
    const shelf = document.createElement("div");
    shelf.className = "shelf";
    tracks.slice(0, 12).forEach((t, i) => shelf.appendChild(shelfTile(t, tracks, i)));

    card.append(head, shelf);
    return card;
}

function shelfTile(track, tracks, index) {
    const tile = document.createElement("div");
    tile.className = "shelf-tile";

    const art = document.createElement("button");
    art.className = "shelf-art";
    art.setAttribute("aria-label", "Играть «" + (track.title || track.path) + "»");
    art.onclick = () => playQueue(tracks, index, "manual");
    loadTrackCover(art, track.path);

    const play = document.createElement("button");
    play.className = "album-play";
    play.setAttribute("aria-label", "Играть");
    play.onclick = event => { event.stopPropagation(); playQueue(tracks, index, "manual"); };
    play.appendChild(playIcon());

    const box = document.createElement("div");
    box.className = "album-artbox";
    box.append(art, play);

    const name = document.createElement("div");
    name.className = "shelf-name";
    name.textContent = track.title || track.path;
    const who = document.createElement("div");
    who.className = "album-artist";
    who.textContent = track.artist || "";

    tile.append(box, name, who);
    return tile;
}

/* Плитка находки. Обложки у неё нет и быть не может — файла-то нет, — поэтому
 * вместо неё буква артиста: пустой серый квадрат читался бы как не загрузившаяся
 * картинка. Нажатие ведёт в поиск, а не в плеер: играть пока нечего. */
function findTile(find) {
    const tile = document.createElement("div");
    tile.className = "shelf-tile is-find";

    const art = document.createElement("button");
    art.className = "shelf-art";
    art.textContent = (find.artist || find.title || "?").slice(0, 1).toUpperCase();
    art.setAttribute("aria-label", `Искать «${find.artist} — ${find.title}» на YouTube`);
    art.onclick = () => searchForFind(find);

    const box = document.createElement("div");
    box.className = "album-artbox";
    box.appendChild(art);

    const name = document.createElement("div");
    name.className = "shelf-name";
    name.textContent = find.title || "";

    const who = document.createElement("div");
    who.className = "album-artist";
    who.textContent = find.artist || "";

    const mark = document.createElement("div");
    mark.className = "find-mark";
    mark.textContent = "нет в фонотеке";

    tile.append(box, name, who, mark);
    return tile;
}

/* Находки приезжают позже своей полки: сеть не должна задерживать главную.
 * Поэтому они не перерисовывают её, а вставляются в уже собранный ряд — иначе
 * пролистанная полка отскочила бы в начало, что мы только что чинили. */
function mixFindsIntoShelf(shelf, finds) {
    if (!shelf || !finds.length) return;
    /* Через две свои — одна чужая: полка остаётся про твою музыку, а находки
     * попадаются по дороге, а не выстраиваются отдельной стеной. */
    let at = 2;
    for (const find of finds.slice(0, 4)) {
        const before = shelf.children[at];
        if (before) shelf.insertBefore(findTile(find), before);
        else shelf.appendChild(findTile(find));
        at += 3;
    }
}

function renderHome(data) {
    const box = document.getElementById("homeBody");
    box.replaceChildren();

    const discover = data.discover || {};
    if ((discover.tracks || []).length) {
        const on = (discover.based_on || []).slice(0, 3).join(", ");
        const card = homeShelf(
            "Может понравиться",
            on ? `похоже на ${on}` : "",
            discover.tracks,
            () => playQueue(discover.tracks, 0, "manual"),
        );
        box.appendChild(card);
        /* Полка про вкус, а не про то, что уже лежит на диске: к своим трекам
         * подмешивается то, чего в фонотеке нет вовсе. */
        externalFinds().then(finds => mixFindsIntoShelf(card.querySelector(".shelf"), finds));
    }

    for (const mood of data.moods || []) {
        if (!(mood.tracks || []).length) continue;
        box.appendChild(homeShelf(
            mood.name, mood.hint, mood.tracks,
            () => playQueue(mood.tracks, 0, "manual"),
        ));
    }

    if ((data.albums || []).length) {
        const card = document.createElement("div");
        card.className = "card";
        const head = document.createElement("div");
        head.className = "card-head";
        const h = document.createElement("h2");
        h.textContent = "Альбомы";
        const note = document.createElement("span");
        note.className = "muted";
        note.textContent = "самые большие";
        head.append(h, note);

        const grid = document.createElement("div");
        grid.className = "rows";
        for (const a of data.albums) {
            const row = document.createElement("button");
            row.className = "track playlist-row";
            row.onclick = () => { setLibraryMode("albums"); switchView("viewLibrary"); };
            const cover = document.createElement("div");
            cover.className = "cover";
            cover.textContent = a.album.slice(0, 1).toUpperCase();
            loadTrackCover(cover, a.cover);
            const info = document.createElement("div");
            info.className = "track-info";
            const name = document.createElement("div");
            name.className = "track-title";
            name.textContent = a.album;
            const who = document.createElement("div");
            who.className = "track-artist";
            who.textContent = a.artist;
            const count = document.createElement("div");
            count.className = "track-album";
            count.textContent = plural(a.count, "трек", "трека", "треков");
            info.append(name, who, count);
            row.append(cover, info);
            grid.appendChild(row);
        }
        card.append(head, grid);
        box.appendChild(card);
    }

    if (!box.childElementCount) {
        homeNote(box, "Пока нечего показать — фонотека ещё не измерена.");
    }
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

/* Порядок списка треков. Запоминается: это привычка, а не разовый выбор. */
const LIBRARY_SORT_KEY = "librarySort";
let librarySort = "new";

function applyLibrarySortButton() {
    const b = document.getElementById("librarySort");
    if (!b) return;
    const fresh = librarySort === "new";
    b.classList.toggle("is-on", fresh);
    b.textContent = fresh ? "Свежие сверху" : "По алфавиту";
    b.title = fresh ? "Сначала недавно добавленные" : "По артисту, как на диске";
}

function toggleLibrarySort() {
    librarySort = librarySort === "new" ? "name" : "new";
    try { localStorage.setItem(LIBRARY_SORT_KEY, librarySort); } catch (e) { /* приватное окно */ }
    applyLibrarySortButton();
    library();
}

function initLibrarySort() {
    try {
        const stored = localStorage.getItem(LIBRARY_SORT_KEY);
        if (stored === "new" || stored === "name") librarySort = stored;
    } catch (e) { /* приватное окно — свежие сверху */ }
    applyLibrarySortButton();
}

initLibrarySort();

function setLibraryMode(mode) {
    libraryMode = mode;
    openAlbumGroup = null;
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

/* Карточка альбома — плитка, а не строка.
 *
 * Строкой это выглядело плохо не случайно: обложка 34 пикселя, название,
 * артист и две кнопки в ряду шириной 1100 — девять десятых карточки пустые,
 * а обложку, ради которой альбом и узнают, не разглядеть. Плитки кладутся
 * сеткой, обложка в них квадратная и во всю ширину.
 *
 * Треки внутри карточки больше не разворачиваются: раскрытая плитка ломает
 * сетку, да и читать список в колонке шириной 170 пикселей нечем. По нажатию
 * альбом открывается целиком, со своим заголовком и кнопкой «Назад».
 */
function albumTile(group) {
    const tile = document.createElement("div");
    tile.className = "album-tile";

    const art = document.createElement("button");
    art.className = "album-art";
    art.setAttribute("aria-label", "Открыть «" + group.album + "»");
    art.onclick = () => openAlbum(group);
    const letter = document.createElement("span");
    letter.className = "album-letter";
    letter.textContent = group.album.slice(0, 1).toUpperCase();
    art.appendChild(letter);
    loadTrackCover(art, group.tracks[0].path);

    const play = document.createElement("button");
    play.className = "album-play";
    play.setAttribute("aria-label", "Играть альбом");
    play.title = "Играть альбом";
    play.onclick = event => { event.stopPropagation(); playQueue(group.tracks, 0, "manual"); };
    play.appendChild(playIcon());

    const name = document.createElement("button");
    name.className = "album-name";
    name.textContent = group.album;
    name.onclick = () => openAlbum(group);

    const who = document.createElement("div");
    who.className = "album-artist";
    who.textContent = group.artist + " · " + plural(group.tracks.length, "трек", "трека", "треков");

    const box = document.createElement("div");
    box.className = "album-artbox";
    box.append(art, play);
    tile.append(box, name, who);
    return tile;
}

/* Один альбом целиком. Показывается на месте сетки — так же, как подборка
 * показывается на месте списка подборок. */
let openAlbumGroup = null;

function openAlbum(group) {
    openAlbumGroup = group;
    const box = document.getElementById("library");
    const note = document.getElementById("libraryModeNote");
    box.replaceChildren();
    if (note) note.textContent = group.artist;

    const head = document.createElement("div");
    head.className = "album-open";

    const art = document.createElement("div");
    art.className = "album-open-art";
    art.textContent = group.album.slice(0, 1).toUpperCase();
    loadTrackCover(art, group.tracks[0].path);

    const meta = document.createElement("div");
    meta.className = "album-open-meta";
    const name = document.createElement("h2");
    name.textContent = group.album;
    const who = document.createElement("p");
    who.className = "muted";
    who.textContent = group.artist + " · " + plural(group.tracks.length, "трек", "трека", "треков");

    const row = document.createElement("div");
    row.className = "row wrap";
    const play = document.createElement("button");
    play.className = "primary";
    play.textContent = "Слушать";
    play.onclick = () => playQueue(group.tracks, 0, "manual");
    const back = document.createElement("button");
    back.className = "ghost";
    back.textContent = "Назад";
    back.onclick = () => { openAlbumGroup = null; library(); };
    row.append(play, back);

    meta.append(name, who, row);
    head.append(art, meta);
    box.appendChild(head);

    const list = document.createElement("div");
    list.className = "rows";
    for (const t of group.tracks) list.appendChild(libraryRow(t, group.tracks));
    box.appendChild(list);
    markPlayingRow();
}

function playIcon() {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "icon");
    svg.setAttribute("viewBox", "0 0 24 24");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "M8 5v14l11-7z");
    svg.appendChild(path);
    return svg;
}

/* ---------------- Обложки ----------------
 *
 * Обложка никогда не <img src>: /api/cover требует токен, а атрибут src
 * заголовков не несёт. Поэтому fetch, blob и ссылка на объект.
 *
 * И поэтому же — кэш. Список перерисовывается фоновым опросом раз в несколько
 * секунд; без кэша это 121 запрос за обложками каждый раз, картинки успевают
 * мигнуть на букву-заглушку, и выглядит это поломкой. Ссылка живёт до
 * перезагрузки страницы и не освобождается: в том и смысл.
 */
const coverUrls = new Map();

function coverUrl(key, url) {
    if (coverUrls.has(key)) return Promise.resolve(coverUrls.get(key));
    const pending = fetch(url, { headers: headers() })
        .then(r => (r.ok ? r.blob() : null))
        .then(blob => {
            const made = blob ? URL.createObjectURL(blob) : null;
            coverUrls.set(key, made);
            return made;
        })
        .catch(() => null);
    // Запрос кладём в кэш сразу, а не по возвращении: сетка рисует сто
    // плиток подряд, и иначе одна обложка запрашивалась бы дважды.
    coverUrls.set(key, pending);
    return pending;
}

function forgetCover(key) {
    const old = coverUrls.get(key);
    if (typeof old === "string") URL.revokeObjectURL(old);
    coverUrls.delete(key);
}

function loadTrackCover(host, path) {
    coverUrl("track:" + path, "/api/cover?path=" + encodeURIComponent(path))
        .then(url => {
            if (!url) return;
            const img = document.createElement("img");
            img.alt = "";
            img.src = url;
            host.replaceChildren(img);
        })
        .catch(() => { /* остаётся буква */ });
}

/* Фоновый опрос не должен стирать то, что человек только что открыл.
 *
 * Список перерисовывается целиком раз в несколько секунд. Если в этот момент
 * на экране развёрнут выбор подборки или подтверждение удаления, он просто
 * исчезал — со стороны это выглядит как «меню закрывается от любого вздоха,
 * от скролла, от движения мышки», хотя дело было не в мышке, а в таймере.
 */
function hasOpenChoice(id) {
    const box = document.getElementById(id);
    return !!box && !!box.querySelector(".confirm");
}

async function library() {
    /* Открыт один альбом — фоновый опрос не должен смахивать его обратно в
     * сетку под руками. Выход из него — только кнопкой «Назад». */
    if (openAlbumGroup || hasOpenChoice("library")) return;
    const box = document.getElementById("library");
    const empty = document.getElementById("libraryEmpty");
    const count = document.getElementById("libraryCount");
    const note = document.getElementById("libraryModeNote");
    const q = document.getElementById("librarySearch").value.trim();

    /* Списком треков хватает первых двухсот: дальше всё равно ищут поиском.
     * Альбомы так собрать нельзя — издание может начинаться на любой букве,
     * поэтому для группировки берём фонотеку целиком.
     *
     * Режим запоминается до запроса и сверяется после. Иначе так: фоновый
     * опрос уходит за двумя сотнями треков, пока открыт список; человек
     * жмёт «Альбомы»; ответ возвращается — и двести треков раскладываются
     * в альбомы вместо всей фонотеки. Получалось 25 изданий вместо 121, и
     * зависело это от того, попал ли клик между опросами. */
    const mode = libraryMode;
    const limit = mode === "tracks" ? 200 : 5000;

    try {
        const r = await fetch(
            "/api/library?limit=" + limit + "&sort=" + librarySort + "&q=" + encodeURIComponent(q),
            { headers: headers() });
        if (!r.ok) return;
        const data = await r.json();
        if (mode !== libraryMode) return;
        box.replaceChildren();

        if (mode === "tracks") {
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
        if (mode === "albums") {
            const albums = groups.filter(g => g.tracks.length > 1);
            count.textContent = albums.length || "";
            empty.hidden = albums.length > 0;
            if (note) note.textContent = plural(albums.length, "издание", "издания", "изданий");
            const grid = document.createElement("div");
            grid.className = "album-grid";
            for (const g of albums) grid.appendChild(albumTile(g));
            box.appendChild(grid);
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
    /* Обложка была только у альбомов и подборок, а в списках стоял пустой
     * серый квадрат — шесть подряд на главной выглядели как незагрузившаяся
     * страница. Запрос дешёвый: обложки кэшируются на весь сеанс. */
    loadTrackCover(cover, t.path);

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

    /* У сингла издание называется так же, как трек, и третья строка просто
     * повторяла первую. Печатаем альбом только когда он говорит новое. */
    if (t.album && t.album !== t.title) {
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

/* ---------------- Ширина панелей ----------------
 *
 * Широкая раскладка — три колонки, и обе боковые нужны не всегда: списку бывает
 * тесно, а на главной правая панель и вовсе повторяет то, что уже в плеере.
 *
 * Левая тянется за край, как в файловых менеджерах, и сворачивается до значков;
 * правая убирается кнопкой в плеере. Оба состояния запоминаются в браузере:
 * ширина панели — не то, что хочется настраивать заново после каждой вкладки.
 *
 * На телефоне ничего этого нет: там .rail раскладывается в display: contents,
 * колонок нет вовсе, а переменная ширины просто не на что влиять.
 */

const RAIL_KEY = "railWidth";
const ASIDE_KEY = "asideHidden";
const RAIL_MIN = 72;      // ровно под значок с полями
const RAIL_MAX = 360;
const RAIL_SLIM_AT = 150; // уже этого подписи не помещаются
const RAIL_DEFAULT = 232;

function applyRailWidth(width) {
    const app = document.querySelector(".app");
    if (!app) return;
    const clamped = Math.min(RAIL_MAX, Math.max(RAIL_MIN, Math.round(width)));
    app.style.setProperty("--rail-w", clamped + "px");
    app.classList.toggle("rail-slim", clamped < RAIL_SLIM_AT);
    return clamped;
}

function saveRailWidth(width) {
    try { localStorage.setItem(RAIL_KEY, String(width)); } catch (e) { /* приватное окно */ }
}

function toggleAside() {
    const app = document.querySelector(".app");
    const button = document.getElementById("playerAsideButton");
    if (!app) return;
    const hidden = app.classList.toggle("aside-off");
    try { localStorage.setItem(ASIDE_KEY, hidden ? "1" : "0"); } catch (e) { /* приватное окно */ }
    if (button) {
        button.classList.toggle("is-on", !hidden);
        button.setAttribute("aria-pressed", String(!hidden));
        button.title = hidden ? "Показать панель трека" : "Скрыть панель трека";
    }
}

function initPanels() {
    const app = document.querySelector(".app");
    const grip = document.getElementById("railGrip");
    if (!app) return;

    let width = RAIL_DEFAULT;
    try {
        const stored = parseInt(localStorage.getItem(RAIL_KEY), 10);
        if (Number.isFinite(stored)) width = stored;
    } catch (e) { /* приватное окно — ширина по умолчанию */ }
    applyRailWidth(width);

    try {
        if (localStorage.getItem(ASIDE_KEY) === "1") toggleAside();
    } catch (e) { /* приватное окно — панель на месте */ }

    if (!grip) return;

    grip.addEventListener("pointerdown", (event) => {
        event.preventDefault();
        grip.setPointerCapture(event.pointerId);
        grip.classList.add("is-dragging");

        const move = (e) => {
            /* Ширина — это расстояние от левого края окна до курсора: панель
               начинается там же, так что пересчитывать нечего. */
            applyRailWidth(e.clientX);
        };
        const up = (e) => {
            grip.classList.remove("is-dragging");
            grip.releasePointerCapture(event.pointerId);
            grip.removeEventListener("pointermove", move);
            grip.removeEventListener("pointerup", up);
            saveRailWidth(applyRailWidth(e.clientX));
        };
        grip.addEventListener("pointermove", move);
        grip.addEventListener("pointerup", up);
    });

    /* Двойной щелчок по краю — свернуть или вернуть. Тянуть до упора мышью
       ради «спрятать подписи» каждый раз утомительно. */
    grip.addEventListener("dblclick", () => {
        const now = parseInt(getComputedStyle(app).getPropertyValue("--rail-w"), 10) || RAIL_DEFAULT;
        saveRailWidth(applyRailWidth(now < RAIL_SLIM_AT ? RAIL_DEFAULT : RAIL_MIN));
    });
}

initPanels();
