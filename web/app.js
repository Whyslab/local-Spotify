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
const SEARCH_DEBOUNCE_MS = 300;

let activeView = "viewHome";
let librarySearchTimer = null;
/* Номер последнего перехода. Медленный ответ (подборка грузилась, а человек
 * тем временем ушёл в фонотеку) не должен возвращать его обратно. */
let navigation = 0;

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

/* ---------------- «Ещё» ----------------
 *
 * Добавление и служебный раздел ушли из постоянного ряда: к ним обращаются
 * изредка, а место в ряду они занимали наравне с фонотекой. Теперь они за
 * одной кнопкой — дотянуться можно, но не задев локтем.
 */
const MORE_VIEWS = [
    ["viewAdd", "Добавить"],
    ["viewService", "Сервис"],
];

function closeMoreMenu() {
    const menu = document.getElementById("moreMenu");
    if (menu) menu.remove();
    const tab = document.getElementById("moreTab");
    if (tab) tab.setAttribute("aria-expanded", "false");
    document.removeEventListener("keydown", moreKeydown, true);
    document.removeEventListener("pointerdown", morePointerDown, true);
}

function moreKeydown(event) {
    if (event.key === "Escape") { closeMoreMenu(); document.getElementById("moreTab")?.focus(); }
}

function morePointerDown(event) {
    const menu = document.getElementById("moreMenu");
    const tab = document.getElementById("moreTab");
    if (menu && !menu.contains(event.target) && !tab.contains(event.target)) closeMoreMenu();
}

function toggleMoreMenu(event) {
    if (event) event.stopPropagation();
    const tab = document.getElementById("moreTab");
    if (document.getElementById("moreMenu")) { closeMoreMenu(); return; }

    const menu = document.createElement("div");
    menu.id = "moreMenu";
    menu.className = "row-menu more-menu";
    menu.setAttribute("role", "menu");
    for (const [view, label] of MORE_VIEWS) {
        const item = document.createElement("button");
        item.className = "row-menu-item";
        item.setAttribute("role", "menuitem");
        item.textContent = label;
        item.onclick = () => { closeMoreMenu(); switchView(view); };
        menu.appendChild(item);
    }

    tab.insertAdjacentElement("afterend", menu);

    /* Ставим по месту кнопки: на телефоне ряд вкладок прижат к низу экрана,
     * на ноутбуке стоит наверху рельсы, и «всегда вверх» там уезжает за край. */
    const box = tab.getBoundingClientRect();
    menu.style.left = Math.round(box.left) + "px";
    if (window.innerHeight - box.bottom > 180) {
        menu.style.top = Math.round(box.bottom + 6) + "px";
    } else {
        /* top сбрасывается: у .row-menu он задан для меню строки, и вместе с
         * bottom сжимал меню в полоску у нижнего края. */
        menu.style.top = "auto";
        menu.style.bottom = Math.round(window.innerHeight - box.top + 6) + "px";
    }

    tab.setAttribute("aria-expanded", "true");
    document.addEventListener("keydown", moreKeydown, true);
    document.addEventListener("pointerdown", morePointerDown, true);
    menu.querySelector(".row-menu-item").focus();
}

/* Заголовок экрана. Раньше на всех было «Фонотека». */
const VIEW_TITLES = {
    viewHome: "Главная",
    viewLibrary: "Фонотека",
    viewPlaylists: "Подборки",
    viewPlaylist: "Подборка",
    viewAdd: "Добавить",
    viewService: "Сервис",
    viewLyrics: "Текст песни",
};
const VIEW_KEY = "lastView";
const PLAYLIST_KEY = "lastPlaylist";

function setViewTitle(text) {
    const title = document.getElementById("viewTitle");
    if (title) title.textContent = text;
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
    /* Внутри подборки подсвечен раздел «Подборки», откуда в неё пришли. */
    const section = id === "viewPlaylist" ? "viewPlaylists" : id;
    for (const tab of document.querySelectorAll(".tab")) {
        tab.classList.toggle("is-active", tab.dataset.view === section);
    }
    /* Раздел спрятан за «Ещё» — пусть кнопка показывает, что мы внутри неё. */
    const more = document.getElementById("moreTab");
    if (more) more.classList.toggle("is-active", MORE_VIEWS.some(([view]) => view === id));
    /* Кнопка текста в плеере горит, пока открыт текст, — как бы из него ни
     * ушли: вкладкой, из рельсы или той же кнопкой. */
    const lyricsButton = document.getElementById("playerLyricsButton");
    if (lyricsButton) {
        lyricsButton.classList.toggle("is-on", id === "viewLyrics");
        lyricsButton.setAttribute("aria-pressed", String(id === "viewLyrics"));
    }
    refresh(true);
}

/* Где ищет поле: фонотека, список подборок или одна подборка. Поле одно на
 * все разделы, и слово, набранное в фонотеке, молча фильтровало следующую
 * открытую подборку — до нуля строк, будто она пустая. Поэтому при переходе
 * в другое место поиска поле очищается; в разделах без поиска (главная,
 * текст, сервис) оно просто ждёт, и возврат туда же набранное сохраняет. */
let searchScope = null;

function scopeOf(view) {
    if (view === "viewLibrary" || view === "viewPlaylists") return view;
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
}

/* Поле одно, а ищет оно в разном — пусть само говорит, где именно. */
function updateSearchPlaceholder(view) {
    const field = document.getElementById("librarySearch");
    if (!field) return;
    field.placeholder = view === "viewPlaylists" ? "Поиск по подборкам"
        : view === "viewPlaylist" ? "Поиск в этой подборке"
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
    librarySearchTimer = setTimeout(runSearchHere, SEARCH_DEBOUNCE_MS);
}

function runSearchHere() {
    if (activeView === "viewPlaylists") { filterPlaylists(); return; }
    if (activeView === "viewPlaylist") { filterOpenPlaylist(); return; }
    /* Из главной и остальных разделов искать всё равно логично по фонотеке —
     * там лежит всё, что можно найти. */
    if (activeView !== "viewLibrary" && searchText()) switchView("viewLibrary");
    /* Открытый альбом держится против фонового опроса, а заодно держался и
     * против поиска: набирал — и ничего не происходило. Поиск его закрывает. */
    if (searchText()) openAlbumGroup = null;
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

function filterPlaylists() {
    const needle = searchText();
    let shown = 0;
    for (const row of document.querySelectorAll("#playlists .playlist-row")) {
        const ok = rowMatches(row, needle);
        row.hidden = !ok;
        if (ok) shown += 1;
    }
    const empty = document.getElementById("playlistsEmpty");
    if (empty) {
        empty.hidden = shown > 0;
        if (!shown && needle) showFilterEmpty(empty, needle);
        else if (!shown) empty.textContent = "Пока ни одной.";
    }
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
 * Треки, которых в фонотеке нет вовсе. Спрашиваются один раз за жизнь страницы:
 * ими пользуются и очередь, и главная, а Deezer от повторных вопросов новых
 * артистов не придумает. Первый запрос ходит в сеть (секунды), дальше сервер
 * отвечает из своего кэша мгновенно.
 *
 * Ничего не скачивается: находка — повод открыть поиск и выбрать версию руками.
 */
let externalFindsCache = null;
let externalFindsPromise = null;

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
        /* Не белая: три белые кнопки на главной были ярче самих обложек. */
        play.className = "ghost";
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
    loadTrackCover(art, track.path, THUMB_LARGE);

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
    art.setAttribute("aria-label",
        `Искать «${[find.artist, find.title].filter(Boolean).join(" — ")}» на YouTube`);
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
            row.onclick = () => {
                pendingAlbum = { artist: a.artist, album: a.album };
                setLibraryMode("albums");
                switchView("viewLibrary");
            };
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
    libraryLimit = LIBRARY_PAGE;
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
    loadTrackCover(art, group.tracks[0].path, THUMB_LARGE);

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
/* Какой именно альбом открыть, когда фонотека догрузится. С главной альбом
 * раньше открывал просто раздел «Альбомы» — и человек оказывался в сетке из
 * сотни изданий, без того, на что нажал. */
let pendingAlbum = null;

function openAlbum(group) {
    openAlbumGroup = group;
    /* Альбом рисуется поверх сетки — «Назад» обязан её перерисовать, даже если
     * данные те же. */
    librarySignature = "";
    const box = document.getElementById("library");
    const note = document.getElementById("libraryModeNote");
    box.replaceChildren();
    if (note) note.textContent = group.artist;

    const head = document.createElement("div");
    head.className = "album-open";

    const art = document.createElement("div");
    art.className = "album-open-art";
    art.textContent = group.album.slice(0, 1).toUpperCase();
    loadTrackCover(art, group.tracks[0].path, THUMB_LARGE);

    const meta = document.createElement("div");
    meta.className = "album-open-meta";
    const name = document.createElement("h2");
    name.textContent = group.album;
    const who = document.createElement("p");
    who.className = "muted";
    /* Сколько это играть — вопрос, который задают альбому первым после «чей он».
     * Складываем то, что уже посчитано при чтении тегов. */
    const seconds = group.tracks.reduce((sum, t) => sum + (t.duration || 0), 0);
    who.textContent = group.artist
        + " · " + plural(group.tracks.length, "трек", "трека", "треков")
        + (seconds ? " · " + humanLength(seconds) : "");

    const row = document.createElement("div");
    row.className = "row wrap";
    const play = document.createElement("button");
    play.className = "primary";
    play.textContent = "Слушать";
    play.onclick = () => playQueue(group.tracks, 0, "manual");
    /* Умно: альбом — костяк очереди, между его треками — похожее, в том числе
     * то, чего в фонотеке нет. */
    const mix = document.createElement("button");
    mix.className = "ghost";
    mix.textContent = "Перемешать";
    mix.title = "Альбом вперемешку, плюс похожие треки — и из фонотеки, и новые";
    mix.onclick = () => shuffleTracks(group.tracks, "albumNote");
    const back = document.createElement("button");
    back.className = "ghost";
    back.textContent = "Назад";
    back.onclick = () => { openAlbumGroup = null; library(); };
    row.append(play, mix, back);
    const albumNote = document.createElement("p");
    albumNote.id = "albumNote";
    albumNote.className = "note";

    meta.append(name, who, row, albumNote);
    head.append(art, meta);
    box.appendChild(head);

    const list = document.createElement("div");
    list.className = "rows";
    group.tracks.forEach((t, i) => {
        const card = libraryRow(t, group.tracks);
        /* Номер по порядку, а не из тега: тег track в этой фонотеке пустой у
         * большинства файлов, а «третий сверху» человеку и нужен. */
        const number = document.createElement("div");
        number.className = "album-track-number";
        number.textContent = String(i + 1);
        card.insertBefore(number, card.firstChild);
        list.appendChild(card);
    });
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

function coverUrl(key, url) {
    const have = coverUrls.get(key);
    if (typeof have === "number") {
        if (Date.now() < have) return Promise.resolve(null);
        coverUrls.delete(key);
    } else if (have !== undefined) {
        // Недавно нужная — в конец очереди на выселение.
        coverUrls.delete(key);
        coverUrls.set(key, have);
        return Promise.resolve(have);
    }
    const pending = fetch(url, { headers: headers() })
        .then(r => {
            if (r.ok) return r.blob();
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
        .catch(() => {
            if (coverUrls.get(key) === pending) coverUrls.delete(key);
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

function loadTrackCover(host, path, size = THUMB_SMALL) {
    coverUrl(`track:${size}:${path}`, "/api/cover?path=" + encodeURIComponent(path) + "&size=" + size)
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
        /* Пока мы ходили за фонотекой, мог открыться альбом: нажатие с главной
         * запускает загрузку дважды — из setLibraryMode и из switchView, — и
         * вторая перерисовка смахивала бы открытый альбом обратно в сетку. */
        if (openAlbumGroup) return;
        /* Опрос принёс то же самое — список не трогаем: перерисовка сбивала
         * прокрутку и открытые под строкой вопросы. */
        const signature = [mode, limit, q, data.length, ...data.map(t => t.path)].join("\n");
        if (signature === librarySignature && box.childElementCount && !pendingAlbum) return;
        librarySignature = signature;
        box.replaceChildren();
        empty.textContent = "Ничего не нашлось.";

        if (mode === "tracks") {
            if (note) note.textContent = "";
            /* Число в поле — только при поиске: «200+» в пустом поле читалось
             * как «нашлось двести». */
            count.textContent = q && data.length ? String(data.length) + (data.length === limit ? "+" : "") : "";
            empty.hidden = data.length > 0;
            if (!data.length && q) showFilterEmpty(empty, q);
            /* Keep the rendered list around: playing one row queues the rest, so
             * "next" carries on down the screen instead of stopping at one track. */
            for (const t of data) box.appendChild(libraryRow(t, data));
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

            if (pendingAlbum) {
                const wanted = albums.find(g =>
                    g.album === pendingAlbum.album && g.artist === pendingAlbum.artist);
                pendingAlbum = null;
                if (wanted) { openAlbum(wanted); return; }
            }
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
