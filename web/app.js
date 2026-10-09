/* local-Spotify control panel.
 *
 * One rule runs through this file: values coming back from the API are put on
 * the page with textContent and DOM calls, never by assigning innerHTML. Track
 * titles and artist names come from YouTube and from third-party metadata, so
 * they are untrusted strings that happen to be displayed. A test enforces this.
 */

/* Опрос сервера. Часто — только загрузки на «Добавить», пока их видно: там
 * смотрят, как идёт скачивание. Остальное меняется редко, и опрос раз в три
 * секунды писал в журнал службы ~29 тысяч строк в сутки, а в режиме
 * «Альбомы» каждый раз заново качал всю фонотеку. Во вкладке, которую не
 * видно, не опрашивается ничего. */
const POLL_MS = 3000;
const POLL_SLOW_MS = 30000;
/* Пауза после буквы перед поиском. Было 300 (а в «Поиске» 120): весь бюджет
 * ответа в 150 мс уходил на ожидание, ещё до работы. Теперь 30 — в «Поиске»,
 * где выдача считается на месте. Фонотека спрашивает сервер сразу: пауза
 * почти ничего не экономила (запрос и так уходил почти на каждую букву,
 * рисуется только ответ на последний — libraryTicket), а «телефону» с сетью
 * 60 мс стоила цели: 144–152 мс при 150 (замер 08.10.2026). Открытая подборка
 * фильтруется тоже сразу — там это работа на месте и недорогая. */
const SEARCH_DEBOUNCE_MS = 30;
/* Сравнение по-русски — один раз собранное: localeCompare(…, "ru") собирает
 * правила заново на каждое сравнение, а сортировка фонотеки — это тысячи их. */
const RU_ORDER = new Intl.Collator("ru");
const RU_ORDER_BASE = new Intl.Collator("ru", { sensitivity: "base" });

let activeView = "viewHome";
let librarySearchTimer = null;
/* Номер последнего перехода. Медленный ответ (подборка грузилась, а человек
 * тем временем ушёл в фонотеку) не должен возвращать его обратно. */
let navigation = 0;

/* Первый экран нарисован тем, что пришло с сервера, — точка «готово», до которой
 * меряется скорость открытия (scripts/measure_ui.py, measure_window.py). Один раз
 * за жизнь страницы: дальше — переходы, а не открытие. Метка ставится в кадре
 * после отрисовки, а не в момент вставки узлов. */
let appReady = false;
function markAppReady() {
    if (appReady) return;
    appReady = true;
    requestAnimationFrame(() => performance.mark("app-ready"));
}

/* ---------------- Token ---------------- */

function token() {
    return localStorage.getItem("token") || "";
}

/* Ссылка из тега файла — чужая строка: загруженный файл приносит свои теги.
 * В href попадает только http(s), не javascript: и не data:. */
function isWebLink(text) {
    if (!text) return false;
    try {
        const url = new URL(text);
        return url.protocol === "https:" || url.protocol === "http:";
    } catch (e) {
        return false;
    }
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
    flushPendingPlays();  /* прослушивания, отвергнутые со старым токеном */
    flushPlaylistEdits();  /* правки подборок — тоже */
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

/* Заголовок экрана. Раньше на всех было «Фонотека». */
const VIEW_TITLES = {
    viewHome: "Главная",
    viewLibrary: "Фонотека",
    viewPlaylist: "Подборка",
    viewPlaylists: "Подборки",
    viewArtist: "Артист",
    viewAlbum: "Альбом",
    viewMood: "Настроение",
    viewSearch: "Поиск",
    viewAdd: "Добавить",
    viewService: "Служба",
    viewLyrics: "Текст песни",
};
const VIEW_KEY = "lastView";
const PLAYLIST_KEY = "lastPlaylist";

function setViewTitle(text) {
    const title = document.getElementById("viewTitle");
    if (title) title.textContent = text;
}

/* Куда вести «Назад» из подборки, если истории нет (открыли по ссылке,
 * перезагрузили): туда, где был до неё. */
let lastBrowseView = (() => {
    try { return localStorage.getItem("lastBrowseView") || "viewHome"; } catch (e) { return "viewHome"; }
})();

function leavePlaylist() {
    if (typeof goBack === "function") goBack(lastBrowseView);
    else switchView(lastBrowseView);
}

function switchView(id) {
    navigation += 1;
    activeView = id;
    updateSearchPlaceholder(id);
    keepSearchFor(id);
    /* В подборке — её имя: и при входе, и при возврате из текста песни. */
    setViewTitle(id === "viewPlaylist" && player.playlist ? player.playlist.name
        : (VIEW_TITLES[id] || "Фонотека"));
    /* Перезагрузка страницы возвращает туда же, а не на главную. */
    try { localStorage.setItem(VIEW_KEY, id); } catch (e) { /* приватное окно */ }
    /* The rail layout keys off this: on a phone the search band belongs to the
     * library and appears with it, on a desktop it is always the top band. */
    document.querySelector(".app").dataset.view = id;
    for (const section of document.querySelectorAll(".view")) {
        section.hidden = section.id !== id;
    }
    if (id === "viewService") libraryHealth();
    /* Вкладки и пункты левой панели подсвечивает views.js (syncNav): у
     * пункта «Артисты» это и фонотека в режиме артистов, и страница артиста. */
    if (typeof syncNav === "function") syncNav();
    /* Куда вернёт «Назад» из подборки: туда, откуда в неё пришли. */
    /* Страницы артиста, альбома, настроения — шаги вглубь, а не «где был». */
    if (!["viewPlaylist", "viewLyrics", "viewArtist", "viewAlbum", "viewMood"].includes(id)) {
        lastBrowseView = id;
        try { localStorage.setItem("lastBrowseView", id); } catch (e) { /* приватное окно */ }
    }
    /* Кнопка текста в плеере горит, пока открыт текст, — как бы из него ни
     * ушли: вкладкой, из рельсы или той же кнопкой. */
    const lyricsButton = document.getElementById("playerLyricsButton");
    if (lyricsButton) {
        lyricsButton.classList.toggle("is-on", id === "viewLyrics");
        lyricsButton.setAttribute("aria-pressed", String(id === "viewLyrics"));
    }
    if (typeof onViewShown === "function") onViewShown(id);
    refresh(true);
}

/* Где ищет поле: фонотека, список подборок или одна подборка. Поле одно на
 * все разделы, и слово, набранное в фонотеке, молча фильтровало следующую
 * открытую подборку — до нуля строк, будто она пустая. Поэтому при переходе
 * в другое место поиска поле очищается; в разделах без поиска (главная,
 * текст, сервис) оно просто ждёт, и возврат туда же набранное сохраняет. */
let searchScope = null;

function scopeOf(view) {
    if (view === "viewLibrary") return view;
    if (view === "viewPlaylist") return "playlist:" + (player.playlist ? player.playlist.name : "");
    return null;
}

function keepSearchFor(view) {
    const scope = scopeOf(view);
    if (!scope || scope === searchScope) return;
    searchScope = scope;
    const field = document.getElementById("librarySearch");
    if (field && field.value) {
        field.value = "";
        libraryLimit = LIBRARY_PAGE;
    }
    /* Число найденного — от прежнего места поиска, в новом оно врало бы. */
    const found = document.getElementById("libraryCount");
    if (found) found.textContent = "";
}

/* Поле одно, а ищет оно в разном — пусть само говорит, где именно. */
function updateSearchPlaceholder(view) {
    const field = document.getElementById("librarySearch");
    if (!field) return;
    field.placeholder = view === "viewPlaylist" ? "Поиск в этой подборке"
        : "Артист, трек или альбом";
}

let lastSlowRefresh = 0;
/* Есть ли загрузки в работе — по последнему ответу /health. */
let downloadsActive = false;

/* `now` — не ждать очереди: сменили экран или вернулись во вкладку. */
function refresh(now = false) {
    if (!token()) return;
    if (document.hidden && !now) return;
    if (activeView === "viewAdd") {
        tasks();
        artistImportStatus();
        /* Загрузки идут — их число в рельсе и состояние сервиса видны сразу. */
        if (downloadsActive) health();
    }
    if (!now && Date.now() - lastSlowRefresh < POLL_SLOW_MS) return;
    lastSlowRefresh = Date.now();
    health();
    /* The playlists are in the rail now, which is on screen whatever section
     * you are in -- so they cannot be fetched only while their own tab is
     * open. On a phone the rail is not rendered and this is one small request
     * that costs a list nobody sees; it is the same request the tab made. */
    playlists();
    if (activeView === "viewLibrary") library();
    if (activeView === "viewHome") home();
}

document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refresh(true);
});

/* ---------------- Adding ---------------- */

function clearInput() {
    document.getElementById("links").value = "";
    document.getElementById("addResult").textContent = "";
}

/* A playlist link is not a track link: it names many, and Spotify names them
 * without giving anything downloadable at all. Both go to their own endpoint. */
function isPlaylistLink(link) {
    return /open\.spotify\.com\/playlist\//.test(link) || /[?&]list=/.test(link)
        || /deezer\.com\/(?:[a-z]{2}(?:-[a-z]{2})?\/)?album\/\d+/.test(link);
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
        health();  // новая загрузка — в рельсе сразу, не через полминуты
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
        const retry = document.getElementById("retryFailed");
        if (retry) retry.hidden = !queue.some(t => t.status === "error");

        /* Список пар перечитывается, только когда меняется число
         * предупреждений, а не на каждом опросе. */
        const warned = data.filter(t => t.warning).map(t => t.id).join(",");
        if (warned !== lastWarned) {
            lastWarned = warned;
            duplicates();
        }
        empty.hidden = queue.length > 0;
        box.replaceChildren();

        for (const t of queue) {
            box.appendChild(trackRow({
                title: t.title || t.url || "—",
                artist: t.artist || "",
                status: t.status,
                error: t.error,
                warning: t.warning,
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
                    warning: t.warning,
                }));
            }
        }
    } catch (e) {
        // Polling loop - a transient network hiccup shouldn't throw to console.
    }
}

/* ---------------- Похожие треки ---------------- */

let lastWarned = null;

function formatBytes(size) {
    return size >= 1048576 ? (size / 1048576).toFixed(1) + " МБ" : Math.round(size / 1024) + " КБ";
}

function formatSeconds(total) {
    if (typeof total !== "number") return "—";
    const s = Math.round(total);
    return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}

/* Одна сторона пары: всё, по чему выбирают, — длина, качество, откуда. */
function duplicateSide(label, t) {
    const side = document.createElement("div");
    side.className = "dup-side";
    const head = document.createElement("div");
    head.className = "track-artist";
    head.textContent = label;
    const title = document.createElement("div");
    title.className = "track-title";
    title.textContent = t.title;
    const artist = document.createElement("div");
    artist.className = "track-artist";
    artist.textContent = t.artist;
    const facts = document.createElement("div");
    facts.className = "track-album";
    facts.textContent = [
        formatSeconds(t.duration),
        t.bitrate ? t.bitrate + " кбит/с" : null,
        t.codec,
        formatBytes(t.size),
    ].filter(Boolean).join(" · ");
    const path = document.createElement("div");
    path.className = "track-album";
    path.textContent = t.path;
    side.append(head, title, artist, facts, path);
    if (isWebLink(t.source)) {
        const link = document.createElement("a");
        link.href = t.source;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.className = "track-album";
        link.textContent = "Источник";
        side.appendChild(link);
    }
    return side;
}

async function resolveDuplicate(task, keep, box) {
    for (const b of box.querySelectorAll("button")) b.disabled = true;
    try {
        const r = await fetch("/api/duplicates/resolve", {
            method: "POST",
            headers: { ...headers(), "Content-Type": "application/json" },
            body: JSON.stringify({ task, keep }),
        });
        if (!r.ok) throw new Error();
        box.remove();
        lastWarned = null;
        await tasks();
    } catch (e) {
        for (const b of box.querySelectorAll("button")) b.disabled = false;
    }
}

async function duplicates() {
    const box = document.getElementById("duplicates");
    const head = document.getElementById("duplicatesHead");
    const count = document.getElementById("duplicatesCount");
    if (!box || !head) return;
    try {
        const r = await fetch("/api/duplicates", { headers: headers() });
        if (!r.ok) return;
        const found = await r.json();
        head.hidden = found.length === 0;
        count.textContent = found.length ? String(found.length) : "";
        box.replaceChildren();
        for (const pair of found) {
            const card = document.createElement("div");
            card.className = "confirm duplicate";
            const sides = document.createElement("div");
            sides.className = "dup-sides";
            sides.append(duplicateSide("Новый", pair.new), duplicateSide("Был в фонотеке", pair.existing));
            const row = document.createElement("div");
            row.className = "row";
            const choices = [
                ["Оставить новый", "new", "ghost grow"],
                ["Оставить прежний", "existing", "ghost grow"],
                ["Оставить оба", "both", "ghost grow"],
            ];
            for (const [label, keep, cls] of choices) {
                const b = document.createElement("button");
                b.className = cls;
                b.textContent = label;
                b.onclick = () => resolveDuplicate(pair.task, keep, card);
                row.appendChild(b);
            }
            const note = document.createElement("p");
            note.textContent = "Лишний уедет в корзину; в подборках его место займёт оставленный.";
            card.append(sides, note, row);
            box.appendChild(card);
        }
    } catch (e) {
        // Как и tasks(): следующий опрос повторит.
    }
}

async function retryFailed() {
    const button = document.getElementById("retryFailed");
    button.disabled = true;
    try {
        const r = await fetch("/api/tasks/retry-failed", { method: "POST", headers: headers() });
        if (r.ok) await tasks();
    } finally {
        button.disabled = false;
    }
}

function trackRow({ title, artist, status, error, warning }) {
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

    /* Трек сохранён, но похож на уже имеющийся: оставить оба или удалить
     * лишний — решает человек, поэтому это предупреждение, а не ошибка. */
    if (warning) {
        const warnEl = document.createElement("div");
        warnEl.className = "track-album track-warning";
        warnEl.textContent = "⚠ " + warning;
        info.appendChild(warnEl);
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
        /* Подробности сервер отдаёт только с ключом; без него — одно состояние. */
        const r = await fetch("/health", { headers: headers() });
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
        downloadsActive = (data.active_tasks || []).length > 0 || (data.queue_size || 0) > 0;

        box.replaceChildren();
        const rows = [
            ["Сервис", ok ? "работает" : data.status, !ok],
            ["База", data.database ?? "—", data.database !== undefined && data.database !== "ok"],
            ["Фонотека", data.library ?? "—", data.library !== undefined && data.library !== "ok"],
            ["ffmpeg", data.ffmpeg ?? "—", data.ffmpeg === "missing"],
            ["Deno", data.js_runtime ?? "—", data.js_runtime === "missing"],
            ["ListenBrainz", data.listenbrainz === "off" ? "выключен" : (data.listenbrainz ?? "—"),
                data.listenbrainz === "token rejected"],
            ["В очереди", String(data.queue_size ?? "—"), false],
            ["Воркеров", String(data.workers ?? "—"), false],
            ["Путь", data.library_path || "—", false],
        ];
        for (const [label, value, bad] of rows) {
            box.appendChild(fact(label, value, bad));
        }
        libraryHealth();
    } catch (e) {
        pill.className = "pill pill-error";
        text.textContent = "нет связи";
    }
}

/* ---------------- Состояние фонотеки ---------------- */

const LIBRARY_PROBLEMS = [
    ["no_cover", "Без обложки", "covers", "Найти обложки"],
    ["no_loudness", "Без выравнивания громкости", "loudness", "Измерить громкость"],
    ["fallback_single", "Альбом не найден (записан как сингл)", null, null],
    ["no_album", "Без альбома", null, null],
    ["unreadable", "Файл не читается", null, null],
];

let libraryHealthRunning = false;

async function libraryHealth() {
    const box = document.getElementById("libraryHealth");
    const jobNote = document.getElementById("libraryHealthJob");
    const view = document.getElementById("viewService");
    if (!box || !view || view.hidden) return;
    try {
        const r = await fetch("/api/library/health", { headers: headers() });
        if (!r.ok) return;
        const data = await r.json();
        const job = data.job || {};
        libraryHealthRunning = Boolean(job.running);
        box.replaceChildren(fact("Треков", String(data.tracks), false));

        for (const [key, label, fix, fixLabel] of LIBRARY_PROBLEMS) {
            const problem = data.problems[key];
            if (!problem) continue;
            const row = fact(label, String(problem.count), problem.count > 0);
            if (problem.count > 0) {
                /* Примеры — под строкой, свёрнутыми: полный список не нужен,
                 * а понять, о каких файлах речь, нужно. */
                const details = document.createElement("details");
                details.className = "health-examples";
                const summary = document.createElement("summary");
                summary.textContent = "примеры";
                details.appendChild(summary);
                for (const path of problem.examples) {
                    const line = document.createElement("div");
                    line.className = "track-album";
                    line.textContent = path;
                    details.appendChild(line);
                }
                row.appendChild(details);
                if (fix) {
                    const button = document.createElement("button");
                    button.className = "ghost small";
                    button.textContent = fixLabel;
                    button.disabled = libraryHealthRunning;
                    button.onclick = () => startLibraryFix(fix);
                    row.appendChild(button);
                }
            }
            box.appendChild(row);
        }

        if (job.running) {
            jobNote.textContent = (job.what === "covers" ? "Ищу обложки" : "Измеряю громкость") + "… Это может занять долго.";
        } else if (job.result) {
            const res = job.result;
            jobNote.textContent = res.error ? "Не удалось: " + res.error
                : job.what === "covers" ? `Обложек добавлено: ${res.added}, не найдено: ${res.not_found}`
                : `Измерено: ${res.measured}, не удалось: ${res.failed}`;
        } else {
            jobNote.textContent = "";
        }
    } catch (e) {
        // Следующий опрос повторит.
    }
}

async function startLibraryFix(what) {
    const r = await fetch("/api/library/health/fix", {
        method: "POST",
        headers: { ...headers(), "Content-Type": "application/json" },
        body: JSON.stringify({ what }),
    });
    if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        document.getElementById("libraryHealthJob").textContent = err.detail || "Не удалось запустить";
        return;
    }
    await libraryHealth();
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

/* «1 ч 12 мин» вместо 4327 секунд: у альбома спрашивают, сколько он идёт,
 * а не сколько в нём секунд. */
function humanLength(seconds) {
    const total = Math.round(seconds / 60);
    const hours = Math.floor(total / 60);
    const minutes = total % 60;
    if (!hours) return `${minutes} мин`;
    return minutes ? `${hours} ч ${minutes} мин` : `${hours} ч`;
}

function plural(n, one, few, many) {
    const mod10 = n % 10, mod100 = n % 100;
    let word = many;
    if (mod10 === 1 && mod100 !== 11) word = one;
    else if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) word = few;
    return `${n} ${word}`;
}

/* ---------------- Library ---------------- */

/* Поиск сверху ищет там, где стоишь. Раньше он умел только фонотеку: в
 * подборках и внутри подборки поле было, а толку от него не было никакого.
 *
 * Фонотеку спрашиваем у сервера — двести строк из тысячи ста, — а подборки и
 * треки открытой подборки фильтруем на месте: они уже на странице, и ходить
 * за ними второй раз незачем. */
function searchText() {
    const field = document.getElementById("librarySearch");
    return field ? field.value.trim().toLowerCase() : "";
}

const LIBRARY_PAGE = 200;
let libraryLimit = LIBRARY_PAGE;
let librarySignature = "";
/* Номер последнего запроса фонотеки. Ответы приходят не по порядку: опрос
 * без фильтра, ушедший раньше, мог вернуться после поиска и заменить его
 * выдачу. Рисует только последний. */
let libraryTicket = 0;

function scheduleLibrarySearch() {
    libraryLimit = LIBRARY_PAGE;
    /* Набранное относится к тому месту, где будет искать, — с главной это
     * фонотека. Иначе переход туда по первой букве тут же стёр бы поле. */
    searchScope = scopeOf(activeView) || "viewLibrary";
    clearTimeout(librarySearchTimer);
    librarySearchTimer = setTimeout(runSearchHere, 0);  // вставка — одно событие на всё
}

function runSearchHere() {
    if (activeView === "viewPlaylist") { filterOpenPlaylist(); return; }
    /* Из главной и остальных разделов искать всё равно логично по фонотеке —
     * там лежит всё, что можно найти. */
    if (activeView !== "viewLibrary" && searchText()) switchView("viewLibrary");
    library();
}

/* Фильтр спрятал всё — сказать словами и дать сбросить одним нажатием. */
function showFilterEmpty(box, needle) {
    const text = document.createElement("span");
    text.textContent = `Ничего не нашлось по «${needle}». `;
    const reset = document.createElement("button");
    reset.className = "ghost small-inline";
    reset.textContent = "Сбросить поиск";
    reset.onclick = () => {
        const field = document.getElementById("librarySearch");
        if (field) field.value = "";
        runSearchHere();
    };
    box.replaceChildren(text, reset);
    box.hidden = false;
}

/* Совпадение по тексту строки целиком: в ней и название, и артист, и альбом. */
function rowMatches(row, needle) {
    return !needle || (row.textContent || "").toLowerCase().includes(needle);
}

/* Строки прячем, а не пересобираем список: у каждой строки записан её номер в
 * подборке, и на нём держатся перемещения. Пересобери мы отфильтрованный
 * список — «выше на один» двигал бы трек не туда. */
function filterOpenPlaylist() {
    const needle = searchText();
    let shown = 0, total = 0;
    for (const row of document.querySelectorAll("#playlistTracks .playlist-track")) {
        row.hidden = !rowMatches(row, needle);
        total += 1;
        if (!row.hidden) shown += 1;
    }
    const empty = document.getElementById("playlistFilterEmpty");
    if (!empty) return;
    if (total && !shown) showFilterEmpty(empty, needle);
    else empty.hidden = true;
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
 * Треки, которых в фонотеке нет вовсе. С 02.10.2026 — новый набор на каждый
 * заход на главную: одни и те же двенадцать переставали быть находками.
 * Следующий набор готовится заранее, сразу с обложками, — заход на главную,
 * и он уже на месте. Обложки лежат у службы на диске (/api/web-cover) и в
 * памяти страницы (coverUrl), сами треки не качаются.
 *
 * Ничего не скачивается: находка — повод открыть поиск и выбрать версию руками.
 */
let findsNext = null;

/* Картинка находки — служба за ней ходит в Deezer, до секунды. Двенадцать
 * находок и заготовка следующих просили двадцать четыре разом и держали все
 * соединения к службе: обложки и ссылка на звук ждали за ними. Теперь — в
 * общей очереди обложек, после видимых строк. */
function findCover(url) {
    const key = "find:" + url;
    const address = "/api/web-cover?url=" + encodeURIComponent(url) + "&size=" + THUMB_LARGE;
    /* Мимо очереди — только готовая или уже идущая; истёкшее «нет» — снова в очередь. */
    const have = coverUrls.get(key);
    if (typeof have === "string" || have instanceof Promise) return coverUrl(key, address);
    return whenCoverIdle((signal) => coverUrl(key, address, signal));
}

function fetchFinds() {
    return fetch("/api/discover-external?limit=12", { headers: headers() })
        .then(r => (r.ok ? r.json() : { tracks: [] }))
        .then(data => {
            const finds = Array.isArray(data.tracks) ? data.tracks : [];
            for (const f of finds) if (f.cover) findCover(f.cover);
            return finds;
        })
        /* Находки — дополнение, а не часть страницы: без них так без них. */
        .catch(() => []);
}

/* Набор для показа, и сразу за ним — заготовка следующего. */
function externalFinds() {
    const ready = findsNext || fetchFinds();
    findsNext = ready.then(() => fetchFinds());
    return ready;
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
        markAppReady();
        return;
    }
    try {
        const r = await fetch("/api/home", { headers: headers() });
        if (!r.ok) { homeNote(box, "Не собралось."); return; }
        homeCache = await r.json();
        renderHome(homeCache);
        homeRendered = homeCache;
        markAppReady();
    } catch (e) {
        homeNote(box, "Не собралось.");
    }
}

/* Главная «Обложки» (настроения, «Похоже на любимое», недавние, альбомы,
 * артисты) — в views.js. */
function renderHome(data) {
    renderLabHome(data);
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
    libraryLimit = LIBRARY_PAGE;
    for (const [name, id] of [["tracks", "modeTracks"], ["artists", "modeArtists"], ["albums", "modeAlbums"]]) {
        const b = document.getElementById(id);
        if (!b) continue;
        b.classList.toggle("is-on", name === mode);
        b.setAttribute("aria-selected", String(name === mode));
    }
    if (typeof syncNav === "function") syncNav();
    /* Режим — часть того, куда вернёт «назад». */
    if (typeof recordTop === "function") recordTop();
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
            || RU_ORDER.compare(a.title || "", b.title || ""));
    }
    return [...albums.values()].sort((a, b) =>
        RU_ORDER.compare(a.artist, b.artist) || RU_ORDER.compare(a.album, b.album));
}

/* ---------------- Обложки ----------------
 *
 * Обложка никогда не <img src>: /api/cover требует токен, а атрибут src
 * заголовков не несёт. Поэтому fetch, blob и ссылка на объект.
 *
 * И поэтому же — кэш. Список перерисовывается фоновым опросом раз в несколько
 * секунд; без кэша это 121 запрос за обложками каждый раз, картинки успевают
 * мигнуть на букву-заглушку, и выглядит это поломкой.
 *
 * Но не бесконечный: фонотека в тысячу строк держала бы тысячу картинок в
 * памяти. Держим последние COVER_CACHE_MAX, самую давнюю выселяем и
 * освобождаем — уже нарисованная картинка от этого не пропадает.
 *
 * «Обложки нет» (404) тоже помнится, но недолго: её могли поставить минуту
 * спустя, а навсегда запомненный промах показывал букву до перезагрузки.
 * Сбой сети не помнится вовсе — следующая перерисовка спросит снова.
 *
 * Значение в кэше — ссылка (строка), запрос в пути (Promise) или время, до
 * которого считаем, что обложки нет (число).
 */
const coverUrls = new Map();
const COVER_CACHE_MAX = 300;
const COVER_MISS_MS = 10 * 60 * 1000;

/* Размер миниатюры для сервера. Строке в списке хватает 96 точек; плитке в
 * 140–200 точек на экране с двойной плотностью — 300. Без него фонотека в
 * двести строк качала 81 МБ полноразмерных обложек. */
const THUMB_SMALL = 96;
const THUMB_LARGE = 300;

function coverUrl(key, url, signal) {
    const have = coverUrls.get(key);
    if (typeof have === "number") {
        if (Date.now() < have) return Promise.resolve(null);
        coverUrls.delete(key);
    } else if (have !== undefined) {
        // Недавно нужная — в конец очереди на выселение.
        coverUrls.delete(key);
        coverUrls.set(key, have);
        /* Ждать пачку, где эта обложка уже есть, — не повод держать место:
         * такие строки занимали все, и остальные ждали. После пачки обложка
         * трека ничего не спрашивает. Чужое фото артиста — повод: при 404
         * задача пойдёт за обложкой трека, и место её ограничивает. */
        if (have.batched) coverSlotFree(signal);
        return Promise.resolve(have);
    }
    const pending = fetch(url, { headers: headers(), signal })
        .then(r => {
            /* Только картинка: после «нет» задача может спросить другую
             * (фото артиста → обложка трека), и та шла бы без места. */
            if (r.ok) {
                coverSlotFree(signal);
                return r.blob();
            }
            if (r.status === 404) return Date.now() + COVER_MISS_MS;
            return null;
        })
        .then(got => {
            const made = got instanceof Blob ? URL.createObjectURL(got) : null;
            /* Пока шёл запрос, обложку могли сбросить (новая загружена) —
             * тогда устаревший ответ в кэш не кладём. */
            if (coverUrls.get(key) === pending) {
                // Удалить и вставить заново — чтобы пришедшая встала в конец
                // очереди на выселение, а не на место, где стоял запрос.
                coverUrls.delete(key);
                if (made) coverUrls.set(key, made);
                else if (typeof got === "number") coverUrls.set(key, got);
            }
            trimCovers();
            return made;
        })
        .catch((error) => {
            if (coverUrls.get(key) === pending) coverUrls.delete(key);
            /* Отменённый — не «обложки нет»: строка ушла с экрана и попросит
             * снова, когда вернётся. */
            if (error && error.name === "AbortError") throw error;
            return null;
        });
    // Запрос кладём в кэш сразу, а не по возвращении: сетка рисует сто
    // плиток подряд, и иначе одна обложка запрашивалась бы дважды.
    coverUrls.set(key, pending);
    return pending;
}

function trimCovers() {
    for (const [key, value] of coverUrls) {
        if (coverUrls.size <= COVER_CACHE_MAX) return;
        if (value instanceof Promise) continue;  // ещё в пути — не трогаем
        /* Освобождаем не сразу: картинка с этой ссылкой могла только что
         * получить src и ещё не прочитаться — отзыв сейчас оставил бы дыру. */
        if (typeof value === "string") setTimeout(() => URL.revokeObjectURL(value), 10000);
        coverUrls.delete(key);
    }
}

function forgetCover(key) {
    const old = coverUrls.get(key);
    if (typeof old === "string") URL.revokeObjectURL(old);
    coverUrls.delete(key);
}

/* Обложку строки просим, только когда строка на экране.
 * Раньше подборка Monday на 1124 трека запрашивала 1124 обложки разом:
 * открытие шло 4–9 с на ноутбуке и ~17 с на телефоне, а ссылка на трек
 * по «играть» ждала в очереди за картинками (замер 30.09.2026).
 *
 * И только когда прокрутка стоит (COVER_QUIET_MS): пролистанный список
 * просил обложку каждой строки, мимо которой проехал. Первыми — строки,
 * которые видны сейчас, за ними запас чуть ниже и выше экрана; среди равных —
 * последние в очереди. Не больше COVER_PARALLEL сразу —
 * браузер держит к службе шесть соединений, и двум надо остаться звуку.
 * Строка, ушедшая с экрана, свой запрос отменяет.
 *
 * Место в очереди освобождают заголовки ответа, а не готовая картинка: тело
 * миниатюры уже пришло, а чтение его и вставка ждали основной поток ещё
 * 20–35 мс на каждый запрос (замер 08.10.2026).
 *
 * whenCoverVisible(host, job): job(signal) — обещание; false из него значит
 * «не сделано» (отменили), и строка попросит снова, вернувшись на экран.
 * whenCoverIdle(job) — картинка без строки (заготовка): в те же места, но
 * после всех видимых и не больше двух сразу — каждая идёт до секунды, и
 * строкам, пришедшим позже, должно остаться где пройти. Обещание с ответом job.
 *
 * Обложки треков (loadTrackCover) идут не по одной, а пачкой на одно место:
 * всё, что ждёт, видимые первыми, — один запрос /api/covers. По одной на
 * телефоне десять строк экрана шли тремя кругами по сети, и цель 0.3 с не
 * выполнялась (384 мс, замер 08.10.2026). Пачка не вышла (нет сети, ошибка) —
 * обложки идут по одной, старым путём, через копию service worker'а;
 * миниатюру, которую ещё делать ffmpeg, строка тоже просит одна. Обложки,
 * пришедшие пачкой, service worker на случай без сети не запоминает.
 *
 * Шесть соединений — предел HTTP/1.1. Телефон получает страницу через
 * Tailscale по HTTP/2, все запросы одним соединением: там мест восемь
 * (обложки по одной: 306 мс против 374, замер 08.10.2026). Браузер не
 * назвал протокол — считаем, что HTTP/1.1. */
const PAGE_PROTOCOL = performance.getEntriesByType?.("navigation")[0]?.nextHopProtocol || "";
const COVER_PARALLEL = /^h[23]$/.test(PAGE_PROTOCOL) ? 8 : 4;
const COVER_QUIET_MS = 50;
const COVER_IDLE_PARALLEL = 2;
const COVERS_MAX = 64;  // обложек в одной пачке — как на сервере (тест сверяет)
const coverWaiting = [];
const coverIdle = [];
let coverIdleBusy = 0;
let coverBusy = 0;
let coverTimer = 0;
let lastScrollAt = 0;
const coverSlots = new WeakMap();  // signal запроса → освободить его место

function coverSlotFree(signal) {
    const free = signal && coverSlots.get(signal);
    if (free) free();
}

document.addEventListener("scroll", () => {
    lastScrollAt = performance.now();
}, { capture: true, passive: true });

const coverObserver = "IntersectionObserver" in window
    ? new IntersectionObserver((entries) => {
        for (const entry of entries) {
            const host = entry.target;
            if (entry.isIntersecting) {
                /* Отменённый запрос (строка уходила с экрана) места не держит:
                 * вернувшись, строка встаёт в очередь сразу, не дожидаясь его
                 * конца — пачка, в которой он был, может ещё идти. */
                const busy = host._coverAbort && !host._coverAbort.signal.aborted;
                if (host._coverJob && !host._coverQueued && !busy) {
                    host._coverQueued = true;
                    coverWaiting.push(host);
                }
                continue;
            }
            if (host._coverQueued) {
                host._coverQueued = false;
                const at = coverWaiting.indexOf(host);
                if (at >= 0) coverWaiting.splice(at, 1);
            }
            if (host._coverAbort) host._coverAbort.abort();
        }
        /* Не сразу: второй наблюдатель отмечает видимые строки в той же
         * задаче, но позже, — без отметок первыми шли строки из запаса. */
        clearTimeout(coverTimer);
        coverTimer = setTimeout(pumpCovers, 0);
    }, { rootMargin: "200px 0px" })
    : null;

/* Видна ли строка сейчас, без запаса, — его отмечает второй наблюдатель.
 * Спрашивать положение у самой строки нельзя: после каждой вставленной
 * картинки это пересчитывало раскладку страницы (на телефоне — десятки мс). */
const coverOnScreen = coverObserver
    ? new IntersectionObserver((entries) => {
        for (const entry of entries) entry.target._coverOnScreen = entry.isIntersecting;
    })
    : null;

/* batch — {path, size}: обложка трека, её можно спросить в пачке. */
function whenCoverVisible(host, job, batch = null) {
    if (!coverObserver) { job(); return; }
    host._coverJob = job;
    host._coverBatch = batch;
    host._coverSingle = false;
    /* Тот же элемент с новой задачей (шапка подборки — одна на все): старая,
     * если идёт, отменяется, и по её концу элемент встанет в очередь заново;
     * стоит в очереди — возьмётся уже новая. */
    if (host._coverAbort) host._coverAbort.abort();
    else if (!host._coverQueued) coverObserver.unobserve(host);
    coverObserver.observe(host);
    coverOnScreen.observe(host);
}

function whenCoverIdle(job) {
    return new Promise((resolve) => {
        coverIdle.push({ job, resolve });
        pumpCovers();
    });
}

/* Занять место; вернуть «освободить» — его зовут заголовки ответа или конец. */
function takeCoverSlot(signal) {
    coverBusy += 1;
    let held = true;
    const free = () => {
        if (!held) return;
        held = false;
        coverBusy -= 1;
        pumpCovers();
    };
    coverSlots.set(signal, free);
    return free;
}

function pumpCovers() {
    clearTimeout(coverTimer);
    const quiet = performance.now() - lastScrollAt;
    if (quiet < COVER_QUIET_MS) {
        coverTimer = setTimeout(pumpCovers, COVER_QUIET_MS - quiet);
        return;
    }
    while (coverBusy < COVER_PARALLEL && coverWaiting.length) {
        const host = coverWaiting.splice(nextCoverIndex(), 1)[0];
        host._coverQueued = false;
        const job = host._coverJob;
        if (!job || !host.isConnected) continue;
        if (batchable(host)) sendCoverBatch([host, ...takeBatchable(COVERS_MAX - 1)]);
        else runCoverJob(host, job, true);
    }
    while (coverBusy < COVER_PARALLEL && coverIdleBusy < COVER_IDLE_PARALLEL && coverIdle.length) {
        const { job, resolve } = coverIdle.shift();
        const signal = new AbortController().signal;
        coverIdleBusy += 1;
        const slot = takeCoverSlot(signal);
        let held = true;
        const free = () => {
            if (held) { held = false; coverIdleBusy -= 1; }
            slot();
        };
        coverSlots.set(signal, free);
        const done = Promise.resolve().then(() => job(signal));
        done.then(free, free);
        resolve(done);
    }
}

/* holdSlot — держать место, пока задача идёт; без него — картинка уже в
 * coverUrls (пришла пачкой), сети задача не трогает. */
function runCoverJob(host, job, holdSlot) {
    const controller = new AbortController();
    host._coverAbort = controller;
    const free = holdSlot ? takeCoverSlot(controller.signal) : () => {};
    Promise.resolve()
        .then(() => job(controller.signal))
        .catch(() => !controller.signal.aborted)
        .then((done) => {
            // Строка могла вернуться и начать новую задачу, пока шла отменённая.
            if (host._coverAbort === controller) host._coverAbort = null;
            if (done !== false && !controller.signal.aborted && host._coverJob === job) {
                delete host._coverJob;
                coverObserver.unobserve(host);
                coverOnScreen.unobserve(host);
            } else if (host.isConnected) {
                /* Снова наблюдать — наблюдатель сам скажет, на экране ли она. */
                coverObserver.unobserve(host);
                coverObserver.observe(host);
            }
            free();
        });
}

function batchKey(host) {
    return `track:${host._coverBatch.size}:${host._coverBatch.path}`;
}

/* В пачку — обложка трека на экране (не из запаса: быстрый бросок на
 * медленном телефоне иначе слал десятки обложек, которые уже не нужны),
 * которой ещё нет в coverUrls (есть — задача возьмёт её оттуда сама) и
 * которую не велено спрашивать одну. */
function batchable(host) {
    if (!host._coverBatch || host._coverSingle || !host._coverJob || !host._coverOnScreen) return false;
    const have = coverUrls.get(batchKey(host));
    return have === undefined || (typeof have === "number" && Date.now() >= have);
}

/* До n строк экрана из очереди для пачки. */
function takeBatchable(n) {
    const taken = [];
    for (let i = coverWaiting.length - 1; i >= 0 && taken.length < n; i--) {
        const host = coverWaiting[i];
        if (!batchable(host) || !host.isConnected) continue;
        coverWaiting.splice(i, 1);
        host._coverQueued = false;
        taken.push(host);
    }
    return taken;
}

/* Одна пачка — одно место, до заголовков ответа, как одиночный запрос. Пока
 * она в пути, в coverUrls у каждой обложки — обещание: другой элемент с тем
 * же треком подождёт его, а не пойдёт за ней сам; та же обложка дважды в
 * пачке спрашивается один раз.
 *
 * Строки ушли с экрана все — пачка отменяется и место освобождает. Висит
 * дольше COVERS_TIMEOUT_MS (сеть пропала, компьютер уснул за Tailscale) —
 * тоже отменяется, и обложки идут по одной, через копию service worker'а:
 * сама пачка — POST, его service worker не трогает. */
const COVERS_TIMEOUT_MS = 6000;

function sendCoverBatch(hosts) {
    const batch = new AbortController();
    const free = takeCoverSlot(batch.signal);
    const timer = setTimeout(() => batch.abort(), COVERS_TIMEOUT_MS);
    const first = new Map();  // ключ → первая строка с этой обложкой
    const members = [];
    for (const host of hosts) {
        const controller = new AbortController();
        host._coverAbort = controller;
        const member = { host, job: host._coverJob, controller, key: batchKey(host), ...host._coverBatch };
        if (!first.has(member.key)) {
            first.set(member.key, member);
            member.pending = new Promise((resolve, reject) => {
                member.settle = resolve;
                member.fail = reject;
            });
            member.pending.catch(() => {});  // ждут его не всегда
            member.pending.batched = true;  // см. coverUrl
            coverUrls.set(member.key, member.pending);
        }
        controller.signal.addEventListener("abort", () => {
            if (members.every((m) => m.controller.signal.aborted)) batch.abort();
        });
        members.push(member);
    }
    const asked = [...first.values()];
    fetch("/api/covers", {
        method: "POST",
        headers: { ...headers(), "Content-Type": "application/json" },
        body: JSON.stringify({ items: asked.map(({ path, size }) => ({ path, size })) }),
        signal: batch.signal,
    })
        .then((r) => {
            free();
            if (!r.ok) throw new Error(`covers ${r.status}`);
            return r.arrayBuffer();
        })
        .then(unpackCovers)
        .catch(() => {
            const gone = members.every((m) => m.controller.signal.aborted);
            return asked.map(() => (gone ? "gone" : "later"));
        })
        .then((answers) => {
            clearTimeout(timer);
            free();
            const answer = new Map(asked.map((m, i) => [m.key, answers[i] || "later"]));
            for (const m of asked) settleBatched(m, answer.get(m.key));
            for (const m of members) finishBatched(m, answer.get(m.key));
            trimCovers();
        });
}

/* Тип картинки из заголовка — только один из этих: Blob с text/html или SVG,
 * открытый когда-нибудь ссылкой, выполнился бы как страница этого сайта. */
const COVER_TYPES = new Set(["image/jpeg", "image/png", "image/webp", "image/gif"]);

/* Ответ /api/covers: 4 байта длины заголовка, заголовок JSON, картинки подряд.
 * Каждая картинка — своя копия байтов, не срез общего Blob: срез держал бы в
 * памяти всю пачку, пока жива хоть одна её обложка. Длины сверяются с тем,
 * что пришло: обрезанный ответ давал обрезанные картинки, и строка до конца
 * сеанса показывала битую обложку. Не сходится — ошибка, пачка «позже». */
function unpackCovers(bytes) {
    const size = new DataView(bytes).getUint32(0);
    const { items } = JSON.parse(new TextDecoder().decode(bytes.slice(4, 4 + size)));
    let at = 4 + size;
    const answers = items.map((item) => {
        if (item.later) return "later";
        if (!item.length) return "missing";
        if (at + item.length > bytes.byteLength) throw new Error("covers: answer cut off");
        const type = COVER_TYPES.has(item.type) ? item.type : "image/jpeg";
        const picture = new Blob([bytes.slice(at, at + item.length)], { type });
        at += item.length;
        return picture;
    });
    if (at !== bytes.byteLength) throw new Error("covers: answer does not add up");
    return answers;
}

/* Ответ пачки — в coverUrls и тем, кто ждал обещание. «Позже», сбой или
 * отмена — для ждавших «не сделано» (AbortError): они спросят снова, а не
 * останутся с буквой. */
function settleBatched(member, answer) {
    const { key, pending } = member;
    let url = null;
    /* Пока шла пачка, обложку могли сбросить (новая загружена) — тогда
     * устаревший ответ в кэш не кладём, как и у одиночного запроса. */
    if (coverUrls.get(key) === pending) {
        coverUrls.delete(key);
        if (answer instanceof Blob) {
            url = URL.createObjectURL(answer);
            coverUrls.set(key, url);
        } else if (answer === "missing") {
            coverUrls.set(key, Date.now() + COVER_MISS_MS);
        }
    }
    if (answer instanceof Blob || answer === "missing") member.settle(url);
    else member.fail(new DOMException("covers: ask again", "AbortError"));
}

function finishBatched(member, answer) {
    const { host, job, controller } = member;
    if (host._coverAbort === controller) host._coverAbort = null;
    const later = answer === "later";
    if (later) host._coverSingle = true;
    if (later || answer === "gone" || controller.signal.aborted || host._coverJob !== job || !host.isConnected) {
        /* Не сделано: наблюдатель вернёт строку в очередь, если она на экране, —
         * «позже» уже по одной. */
        if (host.isConnected) {
            coverObserver.unobserve(host);
            coverObserver.observe(host);
        }
        return;
    }
    runCoverJob(host, job, false);
}

/* Индекс той, что видна на экране, — с конца очереди; видимых нет — последняя. */
function nextCoverIndex() {
    for (let i = coverWaiting.length - 1; i >= 0; i--) {
        if (coverWaiting[i]._coverOnScreen) return i;
    }
    return coverWaiting.length - 1;
}

/* В пачку — только размеры, что заготовлены заранее (thumbs.PREPARED): 600
 * почти всегда «позже», и пачка стоила бы ему лишнего круга. */
function loadTrackCover(host, path, size = THUMB_SMALL) {
    const batch = size <= THUMB_LARGE ? { path, size } : null;
    whenCoverVisible(host, (signal) => fetchTrackCover(host, path, size, signal), batch);
}

function fetchTrackCover(host, path, size, signal) {
    return coverUrl(`track:${size}:${path}`, "/api/cover?path=" + encodeURIComponent(path) + "&size=" + size, signal)
        .then(url => {
            if (!url) return true;
            const img = document.createElement("img");
            img.alt = "";
            img.decoding = "async";
            img.src = url;
            host.replaceChildren(img);
            return true;
        })
        .catch((error) => !(error && error.name === "AbortError"));  /* сбой — остаётся буква */
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
    /* .row-menu — открытое «⋯»: перерисовка снесла бы его из-под руки. */
    return !!box && !!box.querySelector(".confirm, .row-menu");
}

async function library() {
    /* Открыт один альбом — фоновый опрос не должен смахивать его обратно в
     * сетку под руками. Выход из него — только кнопкой «Назад». */
    if (hasOpenChoice("library")) return;
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
    const limit = mode === "tracks" ? libraryLimit : 5000;
    const ticket = ++libraryTicket;

    try {
        const r = await fetch(
            "/api/library?limit=" + limit + "&sort=" + librarySort + "&q=" + encodeURIComponent(q),
            { headers: headers() });
        if (!r.ok) return;
        const data = await r.json();
        if (ticket !== libraryTicket || mode !== libraryMode) return;
        /* Пока ответ шёл, под строкой могли открыть вопрос («удалить?», «в
         * какую подборку?») — проверка в начале этого уже не видела. */
        if (hasOpenChoice("library")) return;
        /* Опрос принёс то же самое — список не трогаем: перерисовка сбивала
         * прокрутку и открытые под строкой вопросы. */
        const signature = [mode, limit, q, data.length, ...data.map(t => t.path)].join("\n");
        if (signature === librarySignature && box.childElementCount) return;
        librarySignature = signature;
        box.replaceChildren();
        empty.textContent = "Ничего не нашлось.";

        if (mode === "tracks") {
            if (note) note.textContent = q ? "" : libraryTotalNote();
            /* Число в поле — только при поиске: «200+» в пустом поле читалось
             * как «нашлось двести». */
            count.textContent = q && data.length ? String(data.length) + (data.length === limit ? "+" : "") : "";
            empty.hidden = data.length > 0;
            if (!data.length && q) showFilterEmpty(empty, q);
            /* Keep the rendered list around: playing one row queues the rest, so
             * "next" carries on down the screen instead of stopping at one track. */
            appendInChunks(box, data.map((t) => libraryRow(t, data)));
            /* Раньше список молча обрывался на двухсотом треке из тысячи с
             * лишним, и до остальных можно было добраться только поиском. */
            if (data.length === limit) {
                const more = document.createElement("button");
                more.className = "ghost block library-more";
                more.textContent = "Показать ещё " + LIBRARY_PAGE;
                more.onclick = async () => {
                    libraryLimit += LIBRARY_PAGE;
                    more.disabled = true;
                    more.textContent = "Загружаю…";
                    await library();
                    /* Не перерисовалось (вопрос под строкой, сбой сети) — кнопка
                     * не должна застрять в «Загружаю…». */
                    if (more.isConnected) {
                        more.disabled = false;
                        more.textContent = "Показать ещё " + LIBRARY_PAGE;
                    }
                };
                box.appendChild(more);
            }
            markPlayingRow();
            markAppReady();
            return;
        }

        /* Артисты — строками с круглой обложкой, альбомы — сеткой карточек
         * (views.js). Весь список нужен целиком: издание или артист могут
         * начинаться на любую букву. */
        if (mode === "artists") {
            const artists = renderArtistList(box, data);
            count.textContent = q && artists ? String(artists) : "";
            empty.hidden = artists > 0;
            if (note) note.textContent = plural(artists, "артист", "артиста", "артистов");
        } else {
            const albums = renderAlbumGrid(box, groupIntoAlbums(data));
            count.textContent = q && albums ? String(albums) : "";
            empty.hidden = albums > 0;
            if (note) note.textContent = plural(albums, "альбом", "альбома", "альбомов");
        }
        markPlayingRow();
        markAppReady();
    } catch (e) {
        // Same reasoning as tasks(): the next keystroke or poll retries.
    }
}

/* Строки фонотеки — пачками по пятьдесят. Пропускать раскладку и отрисовку
 * того, что за экраном (content-visibility), браузер умеет и построчно, но на
 * 1417 строках каждый кадр прокрутки на «телефоне» раскладывал въезжающие
 * строки по одной и следил за всеми: 250 задач дольше 50 мс на прокрутку всей
 * фонотеки. Пачками — 29 (замер 08.10.2026). Вид тот же: style.css, .row-chunk. */
const ROW_CHUNK = 50;

function appendInChunks(box, rows) {
    for (let i = 0; i < rows.length; i += ROW_CHUNK) {
        const chunk = document.createElement("div");
        chunk.className = "row-chunk";
        chunk.append(...rows.slice(i, i + ROW_CHUNK));
        box.appendChild(chunk);
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

    /* Длительность — справа, как в списках Apple Music; на телефоне её
     * прячет CSS: там место — названию. */
    const time = document.createElement("span");
    time.className = "track-time";
    time.textContent = t.duration ? formatSeconds(t.duration) : "";

    /* Четыре значка подряд (играть, в подборку, теги, удалить) у каждой
     * строки — это ряд кнопок, умноженный на тысячу треков. Всё за одной «⋯»,
     * и у действий там названия словами — как у строк подборки. Играть можно
     * и нажатием на саму строку. */
    const more = smallButton("⋯", `Что сделать с треком «${t.title}»`, (event) => {
        event.stopPropagation();
        openLibraryMenu(more, card, t, rows);
    });
    more.setAttribute("aria-haspopup", "menu");
    more.setAttribute("aria-expanded", "false");

    card.append(cover, info, time, more);
    return card;
}

/* Меню строки фонотеки. Открытое меню одно на всю страницу — то же, что у
 * строк подборки (openMenu, closeTrackMenu в player.js). */
function openLibraryMenu(button, card, t, rows) {
    const wasMine = openMenu && openMenu.button === button;
    closeTrackMenu();
    if (wasMine) return;  // повторное нажатие закрывает

    const menu = document.createElement("div");
    menu.className = "row-menu";
    menu.setAttribute("role", "menu");
    const item = (label, onClick) => {
        const b = document.createElement("button");
        b.className = "row-menu-item";
        b.setAttribute("role", "menuitem");
        b.textContent = label;
        b.onclick = () => { closeTrackMenu(); onClick(); };
        menu.appendChild(b);
        return b;
    };
    item("Играть", () => playFromLibrary(t, rows));
    item("В подборку…", () => askAddToPlaylist(card, t));
    item("Изменить теги…", () => askEdit(card, t, rows));
    const line = document.createElement("div");
    line.className = "row-menu-line";
    menu.appendChild(line);
    item("Удалить…", () => askRemove(card, t)).classList.add("is-danger");

    button.insertAdjacentElement("afterend", menu);
    keepMenuOnScreen(menu);
    button.setAttribute("aria-expanded", "true");
    openMenu = { menu, button };
    document.addEventListener("keydown", menuKeydown, true);
    document.addEventListener("pointerdown", menuPointerDown, true);
    menu.querySelector(".row-menu-item").focus();
}

/* Правка тегов — на месте строки, как и удаление. Исполнители через «;»:
 * запятая встречается в самих именах («Tyler, The Creator»). Файл не
 * переезжает: Navidrome группирует по тегам, а не по папкам. */
function askEdit(card, t, rows) {
    const form = document.createElement("form");
    form.className = "confirm edit-tags";

    const head = document.createElement("h3");
    head.textContent = "Теги трека";

    const field = (label, value) => {
        const wrap = document.createElement("label");
        wrap.className = "field";
        const caption = document.createElement("span");
        caption.textContent = label;
        const input = document.createElement("input");
        input.value = value || "";
        input.spellcheck = false;
        wrap.append(caption, input);
        return { wrap, input };
    };
    const title = field("Название", t.title);
    const artists = field("Исполнители (через ;)", (t.artist || "").split(" \u2022 ").join("; "));
    const album = field("Альбом", t.album);

    const refetch = document.createElement("label");
    refetch.className = "check";
    const refetchBox = document.createElement("input");
    refetchBox.type = "checkbox";
    refetch.append(refetchBox, document.createTextNode(" Найти обложку заново"));

    const note = document.createElement("p");
    note.className = "muted";
    if (isWebLink(t.source)) {
        const link = document.createElement("a");
        link.href = t.source;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = "Открыть источник";
        note.appendChild(link);
    }

    const row = document.createElement("div");
    row.className = "row";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "ghost grow";
    cancel.textContent = "Отмена";
    cancel.onclick = () => form.replaceWith(card);
    const save = document.createElement("button");
    save.type = "submit";
    save.className = "primary grow";
    save.textContent = "Сохранить";
    row.append(cancel, save);

    form.onsubmit = async (event) => {
        event.preventDefault();
        save.disabled = cancel.disabled = true;
        save.textContent = "Сохраняю…";
        const body = {
            path: t.path,
            title: title.input.value,
            artists: artists.input.value.split(";").map(s => s.trim()).filter(Boolean),
            album: album.input.value,
            refetch_cover: refetchBox.checked,
        };
        try {
            const r = await fetch("/api/track", {
                method: "PATCH",
                headers: { ...headers(), "Content-Type": "application/json" },
                body: JSON.stringify(body),
            });
            const data = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(data.detail || ("Не удалось сохранить: " + r.status));
            const updated = { ...t, ...data };
            const index = rows.indexOf(t);
            if (index >= 0) rows[index] = updated;
            form.replaceWith(libraryRow(updated, rows));
            if (data.cover === "not found" && typeof setPlayerNote === "function") {
                setPlayerNote("Новая обложка не нашлась — оставлена прежняя");
            }
        } catch (e) {
            note.textContent = e.message;
            save.disabled = cancel.disabled = false;
            save.textContent = "Сохранить";
        }
    };

    form.append(head, title.wrap, artists.wrap, album.wrap, refetch, note, row);
    card.replaceWith(form);
    title.input.focus();
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
    /* restoreView сам переключает экран, а переключение само обновляет. */
    if (!restoreView()) refresh(true);
    setInterval(refresh, POLL_MS);
});

/* Экран, открытый до перезагрузки. Подборка — по имени: её могли удалить или
 * переименовать, тогда просто остаёмся на главной. Текст песни без музыки
 * пуст — туда не возвращаем. */
function restoreView() {
    let view = null;
    let playlist = null;
    try {
        view = localStorage.getItem(VIEW_KEY);
        playlist = localStorage.getItem(PLAYLIST_KEY);
    } catch (e) { return false; }
    /* Страница артиста, альбома, настроения после перезагрузки не знает,
     * чья она, — открываем фонотеку (в том же режиме) или главную. */
    if (view === "viewArtist" || view === "viewAlbum") view = "viewLibrary";
    if (view === "viewMood") view = "viewHome";
    if (!view || !VIEW_TITLES[view] || view === "viewLyrics" || view === activeView) return false;
    if (view === "viewPlaylist") {
        if (!playlist || !token()) return false;
        openPlaylist(playlist);
        return false;  // пока подборка грузится, остальное обновим как обычно
    }
    switchView(view);
    return true;
}


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

/* ---------------- Albums ---------------- */

/* Альбом по названию: список найденного в Deezer, скачивается выбранный.
 * Сразу качать первый найденный опасно — у альбома бывают переиздания,
 * концертные версии и одноимённые синглы. */
async function runAlbumSearch() {
    const query = document.getElementById("searchQuery").value.trim();
    const note = document.getElementById("searchNote");
    const box = document.getElementById("searchResults");
    const ticket = ++searchTicket;
    box.replaceChildren();
    if (!query) { note.textContent = "Введи исполнителя и альбом"; return; }
    note.textContent = "Ищу альбомы…";
    try {
        const r = await fetch("/api/albums/search?q=" + encodeURIComponent(query), { headers: headers() });
        const albums = await r.json();
        if (ticket !== searchTicket) return;
        if (!r.ok) { note.textContent = albums.detail || ("Ошибка " + r.status); return; }
        note.textContent = albums.length ? "" : "Альбомов не нашлось";
        for (const album of albums) box.appendChild(albumRow(album, note));
    } catch (e) {
        if (ticket === searchTicket) note.textContent = e.message;
    }
}

function albumRow(album, note) {
    const card = document.createElement("div");
    card.className = "track";
    const cover = document.createElement("div");
    cover.className = "cover";
    if (album.cover) {
        const img = document.createElement("img");
        img.src = album.cover;  // Deezer CDN, публичная картинка
        img.alt = "";
        img.loading = "lazy";
        cover.appendChild(img);
    }
    const info = document.createElement("div");
    info.className = "track-info";
    const title = document.createElement("div");
    title.className = "track-title";
    title.textContent = album.title;
    const artist = document.createElement("div");
    artist.className = "track-artist";
    artist.textContent = album.artist;
    const facts = document.createElement("div");
    facts.className = "track-album";
    facts.textContent = [album.type, album.tracks ? album.tracks + " тр." : null].filter(Boolean).join(" · ");
    info.append(title, artist, facts);
    const take = document.createElement("button");
    take.className = "ghost";
    take.textContent = "Скачать альбом";
    take.onclick = async () => {
        take.disabled = true;
        note.textContent = `Ищу треки «${album.title}» на YouTube… Это займёт с минуту.`;
        try {
            const r = await fetch("/api/import-album", {
                method: "POST",
                headers: { ...headers(), "Content-Type": "application/json" },
                body: JSON.stringify({ id: album.id }),
            });
            const data = await r.json();
            if (!r.ok) throw new Error(data.detail || ("Ошибка " + r.status));
            const parts = [`«${album.title}»: треков ${data.read}, в очередь ${data.queued}`];
            if (data.unmatched && data.unmatched.length) {
                parts.push(`не нашлось на YouTube: ` +
                    data.unmatched.slice(0, 3).map(t => t.title).join("; ") +
                    (data.unmatched.length > 3 ? " и другие" : ""));
            }
            note.textContent = parts.join(". ");
            tasks();
        } catch (e) {
            note.textContent = e.message;
            take.disabled = false;
        }
    };
    card.append(cover, info, take);
    return card;
}

/* ---------------- Артист целиком ----------------
 *
 * Найти артиста в Deezer (с фото — не спутать с тёзкой), открыть его
 * альбомы, EP и синглы списком с галочками и скачать отмеченное. Версии
 * (ремиксы, sped up, концертные) служба в список не кладёт, что уже есть в
 * фонотеке — помечено и не отмечается. Поиск каждого трека на YouTube идёт
 * на сервере в фоне, по одному: страницу можно закрыть. */
let artistTicket = 0;

function artistBox() { return document.getElementById("artistResults"); }
function artistSay(text) { document.getElementById("artistNote").textContent = text; }

async function runArtistSearch() {
    const query = document.getElementById("artistQuery").value.trim();
    const ticket = ++artistTicket;
    artistBox().replaceChildren();
    if (!query) { artistSay("Введи имя артиста"); return; }
    artistSay("Ищу артиста…");
    try {
        const r = await fetch("/api/artists/search?q=" + encodeURIComponent(query), { headers: headers() });
        const found = await r.json();
        if (ticket !== artistTicket) return;
        if (!r.ok) { artistSay(found.detail || ("Ошибка " + r.status)); return; }
        artistSay(found.length ? "Какой из них?" : "Такого артиста Deezer не знает");
        for (const artist of found) artistBox().appendChild(artistChoiceRow(artist));
    } catch (e) {
        if (ticket === artistTicket) artistSay(e.message);
    }
}

function artistChoiceRow(artist) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "disco-choice";
    const pic = document.createElement("span");
    pic.className = "disco-pic";
    if (artist.picture) {
        const img = document.createElement("img");
        img.src = artist.picture;  // Deezer CDN, публичная картинка
        img.alt = "";
        img.loading = "lazy";
        pic.appendChild(img);
    }
    const text = document.createElement("span");
    text.className = "disco-choice-text";
    const name = document.createElement("b");
    name.textContent = artist.name;
    const facts = document.createElement("small");
    facts.textContent = [plural(artist.fans || 0, "слушатель", "слушателя", "слушателей"),
        artist.albums ? plural(artist.albums, "релиз", "релиза", "релизов") : null].filter(Boolean).join(" · ");
    text.append(name, facts);
    row.append(pic, text);
    row.onclick = () => openDiscography(artist.id, artist.name);
    return row;
}

/* С чужой страницы (артист фонотеки → «Скачать недостающее»): сразу его
 * дискография, если Deezer знает его под тем же именем; иначе — выбор. */
async function downloadArtistFromLibrary(name) {
    switchView("viewAdd");
    document.getElementById("artistQuery").value = name;
    document.getElementById("artistImport").scrollIntoView({ block: "start" });
    const ticket = ++artistTicket;
    artistBox().replaceChildren();
    artistSay("Ищу артиста…");
    try {
        const r = await fetch("/api/artists/find?name=" + encodeURIComponent(name), { headers: headers() });
        const data = await r.json();
        if (ticket !== artistTicket) return;
        if (r.ok && data.artist) { openDiscography(data.artist.id, data.artist.name); return; }
    } catch (e) { /* дальше — обычный поиск */ }
    if (ticket === artistTicket) runArtistSearch();
}

async function openDiscography(id, name) {
    const ticket = ++artistTicket;
    artistBox().replaceChildren();
    artistSay(`Собираю дискографию «${name}»… У плодовитых это до полуминуты.`);
    let data;
    try {
        const r = await fetch("/api/artists/" + encodeURIComponent(id) + "/discography", { headers: headers() });
        data = await r.json();
        if (ticket !== artistTicket) return;
        if (!r.ok) { artistSay(data.detail || ("Ошибка " + r.status)); return; }
    } catch (e) {
        if (ticket === artistTicket) artistSay(e.message);
        return;
    }
    renderDiscography(data);
}

function renderDiscography(data) {
    const box = artistBox();
    const all = [];
    const list = document.createElement("div");
    list.className = "disco";
    const footer = document.createElement("div");
    footer.className = "disco-actions";
    const take = document.createElement("button");
    take.className = "primary grow";
    const update = () => {
        const n = all.filter(x => x.box.checked).length;
        take.textContent = n ? `Скачать ${plural(n, "трек", "трека", "треков")}` : "Отметь, что скачать";
        take.disabled = !n;
        for (const sync of releaseSyncs) sync();
    };
    const releaseSyncs = [];

    for (const release of data.releases) {
        const section = document.createElement("section");
        section.className = "disco-release";
        const head = document.createElement("label");
        head.className = "disco-release-head";
        const whole = document.createElement("input");
        whole.type = "checkbox";
        const cover = document.createElement("span");
        cover.className = "disco-pic is-square";
        if (release.cover) {
            const img = document.createElement("img");
            img.src = release.cover;  // Deezer CDN
            img.alt = "";
            img.loading = "lazy";
            cover.appendChild(img);
        }
        const text = document.createElement("span");
        text.className = "disco-choice-text";
        const title = document.createElement("b");
        title.textContent = release.title;
        const facts = document.createElement("small");
        facts.textContent = [{ album: "альбом", ep: "EP", single: "сингл" }[release.kind] || release.kind, release.year]
            .filter(Boolean).join(" · ");
        text.append(title, facts);
        head.append(whole, cover, text);
        section.appendChild(head);

        const mine = [];
        for (const track of release.tracks) {
            const line = document.createElement("label");
            line.className = "disco-track" + (track.have ? " is-have" : "");
            const tick = document.createElement("input");
            tick.type = "checkbox";
            tick.disabled = track.have;
            const name = document.createElement("span");
            name.className = "disco-track-title";
            name.textContent = track.title;
            line.append(tick, name);
            /* Трек с его релиза, но другого артиста — подписать, кто это. */
            if (track.artist && track.artist !== data.artist.name) {
                const who = document.createElement("small");
                who.className = "disco-track-artist";
                who.textContent = track.artist;
                line.appendChild(who);
            }
            if (track.have) {
                const mark = document.createElement("small");
                mark.textContent = "есть";
                line.appendChild(mark);
            } else {
                const item = { box: tick, track: { ...track, album: release.id } };
                all.push(item);
                mine.push(item);
                tick.onchange = update;
            }
            section.appendChild(line);
        }
        whole.disabled = !mine.length;
        whole.onchange = () => { for (const x of mine) x.box.checked = whole.checked; update(); };
        releaseSyncs.push(() => {
            const on = mine.filter(x => x.box.checked).length;
            whole.checked = mine.length > 0 && on === mine.length;
            whole.indeterminate = on > 0 && on < mine.length;
        });
        list.appendChild(section);
    }

    const tracks = data.releases.reduce((n, r) => n + r.tracks.length, 0);
    const have = tracks - all.length;
    const parts = [`${data.artist.name}: ${plural(tracks, "трек", "трека", "треков")}`];
    if (have) parts.push(`${have} уже есть`);
    if (data.hidden) parts.push(`скрыто версий и повторов: ${data.hidden}`);
    if (data.hidden_releases) parts.push(`релизов-версий: ${data.hidden_releases}`);
    artistSay(parts.join(", ") + (all.length ? "" : ". Скачивать нечего."));

    const pickAll = document.createElement("button");
    pickAll.className = "ghost";
    pickAll.textContent = "Отметить все";
    pickAll.onclick = () => { for (const x of all) x.box.checked = true; update(); };
    const pickNone = document.createElement("button");
    pickNone.className = "ghost";
    pickNone.textContent = "Снять все";
    pickNone.onclick = () => { for (const x of all) x.box.checked = false; update(); };
    const picks = document.createElement("div");
    picks.className = "row";
    picks.append(pickAll, pickNone);

    take.onclick = async () => {
        const chosen = all.filter(x => x.box.checked).map(x => x.track);
        take.disabled = true;
        const ticket = artistTicket;
        try {
            const r = await fetch("/api/artists/import", {
                method: "POST",
                headers: { ...headers(), "Content-Type": "application/json" },
                body: JSON.stringify({ tracks: chosen }),
            });
            const body = await r.json();
            if (!r.ok) throw new Error(typeof body.detail === "string" ? body.detail : ("Ошибка " + r.status));
            showArtistImport(body);
            /* Пока шёл ответ, могли начать новый поиск — его не стираем. */
            if (ticket !== artistTicket) return;
            artistBox().replaceChildren();
            artistSay(`${plural(chosen.length, "трек", "трека", "треков")} ждут поиска на YouTube — по одному, в фоне. Страницу можно закрыть.`);
        } catch (e) {
            artistSay(e.message);
            take.disabled = false;
        }
    };
    footer.appendChild(take);
    if (all.length) box.append(picks, list, footer); else box.append(list);
    update();
}

/* Ход фоновой загрузки — пока на экране «Добавить» и есть что показать. */
async function artistImportStatus() {
    try {
        const r = await fetch("/api/artists/import", { headers: headers() });
        if (r.ok) showArtistImport(await r.json());
    } catch (e) { /* покажем в следующий раз */ }
}

let artistImportShown = "";

function showArtistImport(st) {
    const box = document.getElementById("artistImportStatus");
    if (!box) return;
    /* Опрос раз в несколько секунд: без перемен не перерисовываем — иначе
     * нажатие на кнопку здесь могло пропасть в момент перерисовки. */
    const signature = JSON.stringify(st);
    if (signature === artistImportShown) return;
    artistImportShown = signature;
    box.hidden = !st.total;
    if (!st.total) { box.replaceChildren(); return; }
    const done = st.total - st.wait;
    const line = document.createElement("p");
    line.className = "disco-status-line";
    line.textContent = (st.wait ? `Ищу на YouTube: ${done} из ${st.total}` : `Готово: ${st.total}`)
        + ` · поставлено в загрузку ${st.queued}` + (st.had ? ` · уже были ${st.had}` : "")
        + (st.missed ? ` · не нашлось ${st.missed}` : "");
    const parts = [line];
    if (st.failing) {
        const failing = document.createElement("p");
        failing.className = "note";
        failing.textContent = `Поиск не удался у ${st.failing} — повторю через несколько минут`
            + (st.last_error ? ` (${st.last_error})` : "") + ".";
        parts.push(failing);
    }
    if (st.missed_tracks && st.missed_tracks.length) {
        const missed = document.createElement("p");
        missed.className = "note";
        missed.textContent = "Не нашлось той же записи: "
            + st.missed_tracks.slice(0, 8).map(t => t.title).join("; ")
            + (st.missed_tracks.length > 8 ? ` и ещё ${st.missed_tracks.length - 8}` : "")
            + ". Их можно поискать по названию выше.";
        parts.push(missed);
    }
    const action = (label, query) => {
        const b = document.createElement("button");
        b.className = "ghost small";
        b.textContent = label;
        b.onclick = async () => {
            b.disabled = true;
            try {
                const r = await fetch("/api/artists/import" + query, { method: "DELETE", headers: headers() });
                if (r.ok) showArtistImport(await r.json());
            } catch (e) {
                b.disabled = false;
            }
        };
        return b;
    };
    const row = document.createElement("div");
    row.className = "row";
    if (st.wait) row.appendChild(action("Отменить оставшиеся", "?waiting=true"));
    if (done) row.appendChild(action("Убрать итог", ""));
    parts.push(row);
    box.replaceChildren(...parts);
}

/* ---------------- Search ---------------- */

/* Как у фонотеки: второй поиск, начатый раньше ответа на первый, не должен
 * быть перезаписан этим первым ответом. */
let searchTicket = 0;

async function runSearch() {
    const query = document.getElementById("searchQuery").value.trim();
    const note = document.getElementById("searchNote");
    const box = document.getElementById("searchResults");
    const ticket = ++searchTicket;
    box.replaceChildren();
    if (!query) { note.textContent = ""; return; }

    note.textContent = "Ищу…";
    try {
        const r = await fetch("/api/search?q=" + encodeURIComponent(query), { headers: headers() });
        const data = await r.json();
        if (ticket !== searchTicket) return;
        if (!r.ok) { note.textContent = data.detail || ("Ошибка " + r.status); return; }
        note.textContent = data.results.length ? "" : "Ничего не нашлось.";
        for (const item of data.results) box.appendChild(searchRow(item));
    } catch (e) {
        if (ticket === searchTicket) note.textContent = e.message;
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
        try {
            const r = await fetch("/api/add", {
                method: "POST",
                headers: { ...headers(), "Content-Type": "application/json" },
                body: JSON.stringify({ links: [item.url] }),
            });
            add.textContent = r.ok ? "В очереди" : "Ошибка";
            /* Не вышло — пусть можно нажать ещё раз, а не застрять. */
            add.disabled = r.ok;
        } catch (e) {
            add.textContent = "Ошибка";
            add.disabled = false;
        }
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

    /* Файл, брошенный мимо этого поля, браузер открывает вместо страницы, а
     * в окне на ноутбуке уйти со страницы — значит потерять плеер. Мимо поля
     * файлы просто не принимаются; текст в поля ввода бросать можно. */
    for (const type of ["dragover", "drop"]) {
        document.addEventListener(type, e => {
            const files = e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files");
            if (!files || e.defaultPrevented) return;
            if (e.target.closest && e.target.closest("#dropZone")) return;
            e.preventDefault();
            e.dataTransfer.dropEffect = "none";
        });
    }
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
