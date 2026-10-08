/* Экраны «Обложки» (с 01.10.2026): главная, артисты, альбомы, артист,
 * альбом, настроение, все подборки, поиск — и навигация между ними.
 *
 * Вид взят из варианта «Обложка» (лаборатория web/design/, удалена 08.10.2026),
 * а работа — прежняя: треки играет player.js (playQueue), строки трека —
 * libraryRow из app.js со своим «⋯», обложки — loadTrackCover и
 * loadPlaylistCover. Здесь только раскладка и переходы.
 *
 * Всё из тегов и названий — через textContent: ключ доступа лежит в
 * localStorage этой же страницы.
 */

/* ---------------- Мелочи ---------------- */

const SVG_NS = "http://www.w3.org/2000/svg";
const LAB_ICONS = {
    play: [["path", { d: "M7 4.5v15l12.5-7.5z", fill: "currentColor", stroke: "none" }]],
    shuffle: [["path", { d: "M16 3h5v5M4 20 21 3M21 16v5h-5M15 15l6 6M4 4l5 5" }]],
    back: [["path", { d: "m14.5 5-7 7 7 7" }]],
    chevron: [["path", { d: "m9.5 5 7 7-7 7" }]],
    add: [["path", { d: "M12 5v14M5 12h14" }]],
    search: [["circle", { cx: "11", cy: "11", r: "6.5" }], ["path", { d: "m16 16 4.5 4.5" }]],
};

function labIcon(name) {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "icon");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    for (const [tag, attrs] of LAB_ICONS[name] || []) {
        const node = document.createElementNS(SVG_NS, tag);
        for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
        svg.appendChild(node);
    }
    return svg;
}

/* el("div", "class", "текст", дети…) — текст всегда текстом. */
function el(tag, cls, text, ...kids) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null && text !== "") node.textContent = text;
    for (const kid of kids) if (kid) node.appendChild(kid);
    return node;
}

function button(cls, onclick, ...kids) {
    const b = el("button", cls, "", ...kids);
    b.type = "button";
    b.onclick = onclick;
    return b;
}

/* replaceChildren без пустых мест: null в списке детей становится текстом
 * «null» прямо на странице. */
function put(box, ...parts) {
    box.replaceChildren(...parts.filter(Boolean));
    return box;
}

function hueOf(text) {
    let x = 0;
    for (let i = 0; i < text.length; i++) x = (x * 31 + text.charCodeAt(i)) >>> 0;
    return x % 360;
}

/* Квадрат обложки. Пока картинки нет — цветная заглушка по имени артиста,
 * чтобы сетка не мигала серым. */
function labCover(path, size, seed, round) {
    const box = el("div", "o-cover" + (round ? " is-round" : ""));
    box.style.setProperty("--ph-hue", String(hueOf(seed || path || "?")));
    if (path) loadTrackCover(box, path, size || THUMB_LARGE);
    return box;
}

function labPlaylistCover(name) {
    const box = el("div", "o-cover o-cover-letter");
    box.style.setProperty("--ph-hue", String(hueOf(name)));
    loadPlaylistCover(box, name);
    return box;
}

/* Фото артиста (своё или из Deezer, /api/artist-photo), а нет его — обложка
 * трека, как было. Просим, только когда картинка подъезжает к экрану: в
 * списке артистов их три сотни, и первый раз служба спрашивает Deezer. */
function labArtistCover(name, fallbackPath, size, round) {
    const box = el("div", "o-cover" + (round ? " is-round" : ""));
    box.style.setProperty("--ph-hue", String(hueOf(name || fallbackPath || "?")));
    const job = (signal) => artistPhotoQueue(box, () => artistPhotoUrl(name, size, signal), signal).then((url) => {
        if (signal && signal.aborted) return false;  // ушла с экрана — попросит, вернувшись
        if (!box.isConnected) return true;
        if (url) {
            const img = document.createElement("img");
            img.alt = "";
            img.src = url;
            box.replaceChildren(img);
        } else if (fallbackPath) {
            return fetchTrackCover(box, fallbackPath, size || THUMB_LARGE, signal);
        }
        return true;
    });
    whenCoverVisible(box, job);
    return box;
}

/* Не больше двух фото за раз. Браузер держит к службе шесть соединений, а
 * первый показ фото — это вопрос к Deezer (не чаще раза в 0,15 с): быстро
 * пролистанный список из трёх сотен артистов занял бы все шесть, и звук
 * ждал бы в очереди за картинками. Ушедшую со страницы картинку не просим,
 * а ушедшую с экрана (signal) — отменяем, и в очереди, и в пути: иначе она
 * держала место до ответа Deezer, а видимые строки ждали за ней. */
const artistPhotoWaiting = [];
let artistPhotoBusy = 0;

function artistPhotoQueue(box, ask, signal) {
    return new Promise((resolve) => {
        const entry = { box, ask, resolve, signal };
        artistPhotoWaiting.push(entry);
        if (signal) signal.addEventListener("abort", () => {
            const at = artistPhotoWaiting.indexOf(entry);
            if (at >= 0) { artistPhotoWaiting.splice(at, 1); resolve(null); }
        }, { once: true });
        nextArtistPhoto();
    });
}

function nextArtistPhoto() {
    while (artistPhotoBusy < 2 && artistPhotoWaiting.length) {
        const { box, ask, resolve, signal } = artistPhotoWaiting.shift();
        if (!box.isConnected || (signal && signal.aborted)) { resolve(null); continue; }
        artistPhotoBusy += 1;
        ask().catch(() => null).then((url) => {
            artistPhotoBusy -= 1;
            resolve(url);
            nextArtistPhoto();
        });
    }
}

function artistPhotoUrl(name, size, signal) {
    return coverUrl(`artist:${name}:${size || 0}`,
        "/api/artist-photo?name=" + encodeURIComponent(name) + "&size=" + (size || 0), signal);
}

/* Шапку поменяли — все размеры её фото в памяти страницы устарели. */
function forgetArtistPhoto(name) {
    for (const key of [...coverUrls.keys()]) {
        if (key.startsWith(`artist:${name}:`)) forgetCover(key);
    }
}

function tracksWord(n) { return plural(n, "трек", "трека", "треков"); }

function mainArtist(t) {
    return ((t.albumartist || t.artist || "").split(" • ")[0] || "").trim() || "Без артиста";
}

/* ---------------- Вся фонотека ----------------
 *
 * Главной, артистам и поиску нужна фонотека целиком (1134 строки — 300 КБ),
 * а не первые двести строк списка треков. Берём раз в минуту. */
let labTracks = null;
let labTracksAt = 0;
let labTracksPromise = null;

function labIndex() {
    if (labTracks && Date.now() - labTracksAt < 60000) return Promise.resolve(labTracks);
    if (labTracksPromise) return labTracksPromise;
    labTracksPromise = fetch("/api/library?limit=100000&sort=new", { headers: headers() })
        .then((r) => (r.ok ? r.json() : labTracks || []))
        .then((rows) => {
            labTracks = rows;
            labTracksAt = Date.now();
            return rows;
        })
        .catch(() => labTracks || [])
        .finally(() => { labTracksPromise = null; });
    return labTracksPromise;
}

function groupArtists(rows) {
    const map = new Map();
    for (const t of rows) {
        const name = mainArtist(t);
        let a = map.get(name);
        if (!a) map.set(name, (a = { name, tracks: [], cover: t.path }));
        a.tracks.push(t);
    }
    return [...map.values()].sort((x, y) => x.name.localeCompare(y.name, "ru", { sensitivity: "base" }));
}

function libraryTotalNote() {
    if (labTracks) return tracksWord(labTracks.length);
    /* Фонотеку целиком ещё не брали (сразу после перезагрузки) — возьмём и
     * допишем число, когда придёт. */
    labIndex().then((rows) => {
        const note = document.getElementById("libraryModeNote");
        if (note && libraryMode === "tracks" && !searchText()) note.textContent = tracksWord(rows.length);
    });
    return "";
}

/* ---------------- Переходы и «назад» ----------------
 *
 * Страницы артиста, альбома, настроения и подборки — шаг в истории браузера:
 * «назад» на телефоне (жест, кнопка) ведёт туда, откуда пришли, а не уводит
 * со страницы. Разделы верхнего уровня (вкладки) историю не копят. */
const LAB_PAGES = ["viewArtist", "viewAlbum", "viewMood", "viewPlaylist"];
let pendingPush = null;
let restoringScroll = null;

/* Прокручивается окно (телефон) или середина (компьютер). */
function scroller() {
    return window.matchMedia("(min-width: 1100px)").matches
        ? document.getElementById("views")
        : (document.scrollingElement || document.documentElement);
}

function samePage(a, b) {
    return !!a && !!b && a.view === b.view && a.name === b.name && a.key === b.key
        && a.artist === b.artist && a.album === b.album;
}

/* Раздел верхнего уровня (вкладка, пункт панели) — тоже запись истории, но
 * не новая: текущая переписывается тем, где мы теперь. Иначе «назад» после
 * смены вкладки возвращал в раздел, из которого ушли давным-давно. */
function recordTop() {
    const st = history.state;
    if (st && st.playerOpen) return;
    if (LAB_PAGES.includes(activeView)) return;
    try { history.replaceState({ lab: true, view: activeView, mode: libraryMode }, ""); } catch (e) { /* без истории */ }
}

function pushPage(state) {
    /* Большой плеер только что попросил history.back(): запись сейчас
     * затёрлась бы его возвратом. Запишем, когда возврат случится. */
    if (window.sheetBackPending) { pendingPush = state; return; }
    const st = history.state;
    /* Та же страница (двойное нажатие, перезагрузка) — новой записи нет. */
    if (st && st.lab && samePage(st, state)) return;
    try {
        /* Где были и докуда прокрутили — в текущую запись, чтобы «назад»
         * вернул туда же. */
        if (!(st && st.playerOpen)) {
            const here = st && st.lab ? st : { lab: true, view: activeView, mode: libraryMode };
            history.replaceState(Object.assign({}, here, { scroll: scroller().scrollTop }), "");
        }
        history.pushState(Object.assign({ lab: true }, state), "");
    } catch (e) { /* без истории */ }
    /* Новая страница — с начала, а не с места, докуда листали прежнюю. */
    restoringScroll = 0;
}

/* Прокрутку ставим, когда страница уже нарисована. */
function applyScroll() {
    if (restoringScroll === null) return;
    const y = restoringScroll;
    restoringScroll = null;
    requestAnimationFrame(() => { scroller().scrollTop = y; });
}

function goBack(fallback) {
    if (history.state && history.state.lab) history.back();
    else switchView(fallback || "viewHome");
}

window.addEventListener("popstate", () => {
    if (window.sheetBackPending) {
        window.sheetBackPending = false;
        if (pendingPush) {
            const state = pendingPush;
            pendingPush = null;
            try { history.pushState(Object.assign({ lab: true }, state), ""); } catch (e) { /* без истории */ }
        } else {
            /* Пока плеер сворачивался, успели сменить раздел («показать,
             * откуда играет») — запись должна знать, где мы теперь. */
            recordTop();
        }
        return;
    }
    labTicket += 1;
    const st = history.state;
    if (st && st.playerOpen) return;
    if (st && st.lab) {
        restoringScroll = typeof st.scroll === "number" ? st.scroll : 0;
        showPage(st);
        return;
    }
    if (LAB_PAGES.includes(activeView)) switchView(lastBrowseView || "viewHome");
});

function showPage(st) {
    if (!LAB_PAGES.includes(st.view)) {
        if (st.view === "viewLibrary" && st.mode && st.mode !== libraryMode) setLibraryMode(st.mode);
        if (activeView !== st.view) switchView(st.view);
        else applyScroll();
        return;
    }
    if (st.view === "viewArtist") renderArtistPage(st.name);
    else if (st.view === "viewAlbum") renderAlbumPage(st.artist, st.album);
    else if (st.view === "viewMood") renderMoodPage(st.key);
    else if (st.view === "viewPlaylist") {
        if (activeView === "viewPlaylist" && player.playlist && player.playlist.name === st.name) applyScroll();
        else openPlaylistPlain(st.name).then(applyScroll);
    }
}

/* Подборка — тоже страница: оборачиваем player.js-овский openPlaylist. */
const openPlaylistPlain = openPlaylist;
openPlaylist = function (name, ...rest) {
    const same = activeView === "viewPlaylist" && player.playlist && player.playlist.name === name;
    if (!same) pushPage({ view: "viewPlaylist", name });
    return openPlaylistPlain(name, ...rest).then((ok) => { applyScroll(); return ok; });
};

/* Переименовали открытую подборку — запись истории под новым именем, иначе
 * «назад» к ней искал бы старое. */
const renamePlaylistPlain = renamePlaylist;
renamePlaylist = async function (...args) {
    const before = player.playlist && player.playlist.name;
    const result = await renamePlaylistPlain(...args);
    const after = player.playlist && player.playlist.name;
    const st = history.state;
    if (before && after && before !== after && st && st.view === "viewPlaylist" && st.name === before) {
        try { history.replaceState(Object.assign({}, st, { name: after }), ""); } catch (e) { /* без истории */ }
    }
    return result;
};

function openLibrary(mode) {
    if (libraryMode !== mode) setLibraryMode(mode);
    if (activeView !== "viewLibrary") switchView("viewLibrary");
    else { syncNav(); recordTop(); }
}

/* «+» в заголовке: в «Подборках» — новая подборка, иначе — добавить музыку. */
function topAddClick(anchor) {
    if (activeView === "viewPlaylists") askNewPlaylist(anchor);
    else switchView("viewAdd");
}

/* Подсветка разделов: в data-nav — список экранов, «viewLibrary:artists» —
 * фонотека в режиме артистов. */
function syncNav() {
    /* Кнопки «Слушать / Перемешать» фонотеки — только у треков, как в варианте. */
    const lib = document.getElementById("viewLibrary");
    if (lib) lib.dataset.mode = libraryMode;
    const add = document.getElementById("topAdd");
    if (add) {
        const label = activeView === "viewPlaylists" ? "Новая подборка" : "Добавить музыку";
        add.setAttribute("aria-label", label);
        add.title = label;
    }
    for (const node of document.querySelectorAll("[data-nav]")) {
        const on = node.dataset.nav.split(" ").some((token) => {
            const [view, mode] = token.split(":");
            return view === activeView && (!mode || mode === libraryMode);
        });
        node.classList.toggle("is-active", on);
        if (on) node.setAttribute("aria-current", "page"); else node.removeAttribute("aria-current");
    }
    /* Подборка в левой панели подсвечена, только пока открыта её страница:
     * раньше подсветка оставалась на последней открытой и горела рядом с
     * «Главной» или «Артистами», как будто под мышью. */
    const open = railOpenPlaylist();
    for (const item of document.querySelectorAll(".rail-item")) {
        const on = item.getAttribute("aria-label") === open;
        item.classList.toggle("is-active", on);
        if (on) item.setAttribute("aria-current", "page"); else item.removeAttribute("aria-current");
    }
}

/* Поле фильтра одно (#librarySearch): в фонотеке оно под заголовком, в
 * подборке — под её шапкой, где заголовка раздела нет. */
function placeFilter(id) {
    const field = document.querySelector(".search");
    if (!field) return;
    if (id === "viewPlaylist") {
        const head = document.querySelector("#viewPlaylist .playlist-head");
        if (head && field.previousElementSibling !== head) head.after(field);
    } else if (!field.closest(".topbar")) {
        document.querySelector(".topbar").appendChild(field);
    }
}

function onViewShown(id) {
    placeFilter(id);
    /* Каждый заход на главную — новые находки. */
    if (id === "viewHome") showFinds(true);
    if (id === "viewPlaylists") renderPlaylistsGrid(true);
    if (id === "viewSearch") renderSearch(true);
    if (!LAB_PAGES.includes(id)) {
        /* Ушли в раздел — недогруженная страница артиста не вернёт сюда. */
        labTicket += 1;
        recordTop();
        /* Раздел открывается с начала; при возврате «назад» — с того места,
         * где был (restoringScroll ставит popstate). */
        if (restoringScroll === null) scroller().scrollTop = 0;
        else applyScroll();
    } else {
        applyScroll();
    }
}

/* ---------------- Общие куски ---------------- */

function labSection(title, action, ...body) {
    const head = el("div", "o-section-head", "", el("h2", "", title));
    if (action) head.appendChild(button("o-link", action.run, document.createTextNode(action.label)));
    return el("section", "o-section", "", head, ...body);
}

function labShelf(items, cls) {
    const shelf = el("div", "o-shelf " + (cls || ""));
    for (const item of items) shelf.appendChild(item);
    return shelf;
}

function labCard(coverEl, title, sub, onclick, cls) {
    const card = button("o-card " + (cls || ""), onclick, coverEl, el("span", "o-card-title", title));
    if (sub) card.appendChild(el("span", "o-card-sub", sub));
    return card;
}

function capsule(label, iconName, onclick, id) {
    const b = button("o-capsule", onclick, labIcon(iconName), el("span", "", label));
    if (id) b.id = id;
    return b;
}

/* Строки трека — те же, что в фонотеке (с «⋯»), номер — для альбома. */
function labRows(tracks, numbered, ownArtist) {
    const list = el("div", "rows o-rows" + (numbered ? " is-numbered" : ""));
    tracks.forEach((t, i) => {
        const row = libraryRow(t, tracks);
        /* На странице артиста его имя под каждым треком — лишнее; фиты
         * («A • B») остаются. */
        if (ownArtist && t.artist === ownArtist) row.querySelector(".track-artist")?.remove();
        if (numbered) row.insertBefore(el("div", "album-track-number", String(i + 1)), row.firstChild);
        list.appendChild(row);
    });
    return list;
}

function backBar(label, fallback) {
    return el("div", "o-backbar", "", button("o-back", () => goBack(fallback), labIcon("back"), el("span", "", label || "Назад")));
}

/* «Перемешать» везде с выбором: обычное — тот же список вперемешку,
 * умное — похожее рядом, часть новых. */
function openShuffleChoice(anchor, plain, smart) {
    const wasMine = openMenu && openMenu.button === anchor;
    closeTrackMenu();
    if (wasMine) return;
    const menu = el("div", "row-menu more-menu shuffle-menu");
    menu.setAttribute("role", "menu");
    const item = (label, hint, run) => {
        const b = button("row-menu-item", () => { closeTrackMenu(); run(); }, el("span", "", label), el("small", "", hint));
        b.setAttribute("role", "menuitem");
        menu.appendChild(b);
    };
    item("Обычное", "тот же список вперемешку", plain);
    item("Умное", "похожее рядом, примерно треть новых", smart);
    document.body.appendChild(menu);
    const box = anchor.getBoundingClientRect();
    menu.style.left = Math.round(Math.max(8, Math.min(box.left, window.innerWidth - menu.offsetWidth - 8))) + "px";
    if (window.innerHeight - box.bottom > menu.offsetHeight + 16) {
        menu.style.top = Math.round(box.bottom + 6) + "px";
    } else {
        menu.style.top = "auto";
        menu.style.bottom = Math.round(window.innerHeight - box.top + 6) + "px";
    }
    anchor.setAttribute("aria-expanded", "true");
    openMenu = { menu, button: anchor };
    document.addEventListener("keydown", menuKeydown, true);
    document.addEventListener("pointerdown", menuPointerDown, true);
    menu.querySelector(".row-menu-item").focus();
}

/* Обычное перемешивание набора: та же очередь, режим «вперемешку» включён —
 * видно и по кнопке в плеере, и выключается ею же. */
function playShuffled(tracks, source) {
    const list = (tracks || []).filter((t) => t && t.path);
    if (!list.length) return;
    playQueue(list, Math.floor(Math.random() * list.length), "manual", source || null);
    setShuffle("plain");
}

/* ---------------- Главная ---------------- */

let homeDataForMoods = null;

function renderLabHome(data) {
    const box = document.getElementById("homeBody");
    if (!box) return;
    homeDataForMoods = data;
    const moods = (data.moods || []).filter((m) => (m.tracks || []).length);
    const disc = (data.discover && data.discover.tracks) || [];
    const based = (data.discover && data.discover.based_on) || [];

    const parts = [];

    if (moods.length) {
        parts.push(labSection("Под настроение", null, labShelf(moods.map((m) => {
            const first = m.tracks[0];
            const open = button("o-mood-open", () => openMoodPage(m.key),
                labCover(first && first.path, THUMB_LARGE, m.name),
                el("span", "o-mood-shade"),
                el("span", "o-mood-text", "", el("span", "o-mood-name", m.name), el("span", "o-mood-hint", m.hint)));
            open.setAttribute("aria-label", m.name);
            const play = button("o-mood-play", () => playQueue(m.tracks, 0, "manual"), labIcon("play"));
            play.setAttribute("aria-label", "Слушать «" + m.name + "»");
            return el("div", "o-mood", "", open, play);
        }), "is-moods")));
    }

    if (disc.length) {
        const picks = el("div", "o-picks");
        disc.slice(0, 16).forEach((t, i) => {
            const row = libraryRow(t, disc);
            row.querySelector(".track-info").onclick = () => playQueue(disc, i, "manual");
            picks.appendChild(row);
        });
        parts.push(labSection("Похоже на любимое", null,
            based.length ? el("p", "o-section-note", "По тем, кого вы слушаете: " + based.slice(0, 3).join(", ")) : null,
            picks));
    }

    /* Недавние и артисты — из всей фонотеки: место под них держим сразу,
     * чтобы полки не прыгали, когда фонотека приедет. */
    const recentSlot = el("div");
    const albumsSlot = el("div");
    const artistsSlot = el("div");
    const findsSlot = el("div");
    parts.push(recentSlot, albumsSlot, artistsSlot, findsSlot);
    put(box, ...parts);

    labIndex().then((rows) => {
        if (!recentSlot.isConnected) return;
        const recent = rows.slice(0, 18);
        if (recent.length) {
            recentSlot.replaceChildren(labSection("Недавно добавлено", { label: "Все", run: () => openLibrary("tracks") },
                labShelf(recent.map((t, i) => labCard(labCover(t.path, THUMB_LARGE, mainArtist(t)), t.title, t.artist,
                    () => playQueue(recent, i, "manual"))))));
        }
        const albums = (data.albums || []);
        if (albums.length) {
            const groups = groupIntoAlbums(rows);
            albumsSlot.replaceChildren(labSection("Альбомы", { label: "Все", run: () => openLibrary("albums") },
                labShelf(albums.map((a) => labCard(labCover(a.cover, THUMB_LARGE, a.artist), a.album, a.artist, () => {
                    const group = groups.find((g) => g.artist === a.artist && g.album === a.album);
                    if (group) openAlbumPage(group);
                })))));
        }
        const top = groupArtists(rows).sort((x, y) => y.tracks.length - x.tracks.length).slice(0, 14);
        if (top.length) {
            artistsSlot.replaceChildren(labSection("Чаще всего в фонотеке", { label: "Все", run: () => openLibrary("artists") },
                labShelf(top.map((a) => labCard(labArtistCover(a.name, a.cover, THUMB_SMALL * 2, true), a.name, tracksWord(a.tracks.length),
                    () => openArtistPage(a.name), "is-artist")), "is-artists")));
        }
    });

    findsBox = findsSlot;
    showFinds(false);

    if (!parts.length) homeNote(box, "Пока нечего показать — фонотека ещё не измерена.");
}

/* «Новое для вас» — то, чего в фонотеке нет, но что слушают похожие
 * артисты. Нажатие — поиск на YouTube с готовым запросом: какую загрузку
 * брать, решает человек.
 *
 * Набор меняется на каждый заход на главную (onViewShown), но не от
 * перерисовки главной и не от фонового опроса: под рукой полка не прыгает. */
let findsBox = null;
let findsShown = null;
/* Быстро ушли и вернулись — ответы приходят по очереди, и полка сменилась бы
 * несколько раз подряд. Рисуем только ответ на последний заход. */
let findsTicket = 0;

function showFinds(fresh) {
    const slot = findsBox;
    if (!slot) return;
    const ticket = ++findsTicket;
    const got = !fresh && findsShown ? Promise.resolve(findsShown) : externalFinds();
    got.then((finds) => {
        if (ticket !== findsTicket || slot !== findsBox || !slot.isConnected) return;
        findsShown = finds;
        if (!finds.length) { slot.replaceChildren(); return; }
        slot.replaceChildren(labSection("Новое для вас", null,
            el("p", "o-section-note", "Этого нет в фонотеке — нажмите, чтобы найти и скачать"),
            labShelf(finds.slice(0, 12).map((f) =>
                labCard(findCoverBox(f), f.title || "", f.artist || "", () => searchForFind(f))))));
    });
}

/* Обложка находки: буква на цвете, пока картинка не пришла (или её нет). */
function findCoverBox(f) {
    const cover = el("div", "o-cover o-cover-letter is-find", (f.artist || f.title || "?").slice(0, 1).toUpperCase());
    cover.style.setProperty("--ph-hue", String(hueOf(f.artist || f.title || "")));
    if (f.cover) {
        findCover(f.cover).then((url) => {
            if (!url) return;
            const img = document.createElement("img");
            img.alt = "";
            img.src = url;
            cover.replaceChildren(img);
        });
    }
    return cover;
}

/* ---------------- Фонотека: артисты и альбомы ---------------- */

function renderArtistList(box, rows) {
    const artists = groupArtists(rows);
    const list = el("div", "o-list");
    for (const a of artists) {
        list.appendChild(button("o-artist-row", () => openArtistPage(a.name),
            labArtistCover(a.name, a.cover, THUMB_SMALL, true),
            el("span", "o-row-text", "", el("span", "o-row-title", a.name), el("span", "o-row-sub", tracksWord(a.tracks.length))),
            labIcon("chevron")));
    }
    box.appendChild(list);
    return artists.length;
}

function renderAlbumGrid(box, groups) {
    const albums = groups.filter((g) => g.tracks.length > 1);
    const grid = el("div", "o-grid");
    for (const g of albums) {
        grid.appendChild(labCard(labCover(g.tracks[0].path, THUMB_LARGE, g.artist), g.album, g.artist, () => openAlbumPage(g)));
    }
    box.appendChild(grid);
    return albums.length;
}

/* «Слушать» в фонотеке — вся фонотека по порядку списка. */
function playWholeLibrary() {
    labIndex().then((rows) => {
        const list = librarySort === "name"
            ? [...rows].sort((a, b) => (a.artist || "").localeCompare(b.artist || "", "ru") || (a.title || "").localeCompare(b.title || "", "ru"))
            : rows;
        if (list.length) playQueue(list, 0, "manual", { kind: "library" });
    });
}

function openLibraryShuffle(anchor) {
    openShuffleChoice(anchor, () => playPlainShuffle(), () => playSmartShuffle());
}

/* ---------------- Коллекция: альбом, настроение ---------------- */

function collectionHead({ coverEl, title, subtitle, onSubtitle, meta, tracks, plain, smart, noteId }) {
    const sub = subtitle ? button("o-col-sub", onSubtitle || null, document.createTextNode(subtitle)) : null;
    if (sub && !onSubtitle) sub.disabled = true;
    const shuffle = capsule("Перемешать", "shuffle", () => openShuffleChoice(shuffle, plain, smart));
    shuffle.setAttribute("aria-haspopup", "menu");
    const note = el("p", "note");
    if (noteId) note.id = noteId;
    return el("header", "o-col-head", "",
        el("div", "o-col-art", "", coverEl),
        el("h1", "o-col-title", title),
        sub,
        el("p", "o-col-meta", meta),
        el("div", "o-actions is-center", "", capsule("Слушать", "play", () => playQueue(tracks, 0, "manual")), shuffle),
        note);
}

function totalMeta(tracks) {
    const seconds = tracks.reduce((s, t) => s + (t.duration || 0), 0);
    return tracksWord(tracks.length) + (seconds ? ", " + humanLength(seconds) : "");
}

function openAlbumPage(group) {
    pushPage({ view: "viewAlbum", artist: group.artist, album: group.album });
    showAlbum(group);
}

async function renderAlbumPage(artist, album) {
    const ticket = ++labTicket;
    const rows = await labIndex();
    if (ticket !== labTicket) return;
    const group = groupIntoAlbums(rows).find((g) => g.artist === artist && g.album === album);
    if (group) showAlbum(group);
    else { switchView("viewAlbum"); document.getElementById("albumBody").replaceChildren(backBar("Назад", "viewLibrary"), el("p", "empty", "Этого альбома в фонотеке уже нет.")); }
}

/* Страница, открытая последней: долгий ответ для прежней не возвращает на неё. */
let labTicket = 0;
document.addEventListener("click", (e) => {
    if (e.target.closest(".tab, .side-item, .rail-item")) labTicket += 1;
}, true);

function showAlbum(group) {
    const body = document.getElementById("albumBody");
    put(body,
        backBar("Назад", "viewLibrary"),
        collectionHead({
            coverEl: labCover(group.tracks[0].path, 600, group.artist),
            title: group.album,
            subtitle: group.artist,
            onSubtitle: () => openArtistPage(mainArtist({ albumartist: group.artist })),
            meta: totalMeta(group.tracks),
            tracks: group.tracks,
            plain: () => playShuffled(group.tracks),
            smart: () => shuffleTracks(group.tracks, "albumNote"),
            noteId: "albumNote",
        }),
        labRows(group.tracks, true));
    switchView("viewAlbum");
    setViewTitle(group.album);
    markPlayingRow();
}

function openMoodPage(key) {
    pushPage({ view: "viewMood", key });
    renderMoodPage(key);
}

async function renderMoodPage(key) {
    const data = homeDataForMoods || homeCache;
    const mood = data && (data.moods || []).find((m) => m.key === key);
    const body = document.getElementById("moodBody");
    if (!mood) {
        body.replaceChildren(backBar("Главная", "viewHome"), el("p", "empty", "Подборка по настроению обновилась — откройте главную."));
        switchView("viewMood");
        return;
    }
    const ticket = ++labTicket;
    const rows = await labIndex();
    if (ticket !== labTicket) return;
    const byPath = new Map(rows.map((t) => [t.path, t]));
    const tracks = mood.tracks.map((t) => byPath.get(t.path) || t);
    put(body,
        backBar("Главная", "viewHome"),
        collectionHead({
            coverEl: labCover(tracks[0] && tracks[0].path, 600, mood.name),
            title: mood.name,
            subtitle: mood.hint,
            meta: totalMeta(tracks),
            tracks,
            plain: () => playShuffled(tracks),
            smart: () => shuffleTracks(tracks, "moodNote"),
            noteId: "moodNote",
        }),
        labRows(tracks, false));
    switchView("viewMood");
    setViewTitle(mood.name);
    markPlayingRow();
}

/* ---------------- Артист ---------------- */

/* Шапка — фото в полном размере (1000 точек): в шапку шириной в окно
 * миниатюра 600 растягивалась и мылилась. */
function artistHero(name, fallbackPath) {
    const cover = labArtistCover(name, fallbackPath, 0);
    cover.classList.add("is-hero");
    return cover;
}

/* «⋯» артиста: скачать недостающее из его дискографии; своя шапка вместо
 * фото из Deezer — и обратно. */
async function openArtistMenu(anchor, name) {
    const wasMine = openMenu && openMenu.button === anchor;
    closeTrackMenu();
    if (wasMine) return;
    let own = false;
    try {
        const r = await fetch("/api/artist-photo/info?name=" + encodeURIComponent(name), { headers: headers() });
        if (r.ok) own = !!(await r.json()).own;
    } catch (e) { /* не знаем — покажем только «Своя шапка…» */ }
    if (!anchor.isConnected) return;

    const menu = el("div", "row-menu more-menu");
    menu.setAttribute("role", "menu");
    const item = (label, run) => {
        const b = button("row-menu-item", () => { closeTrackMenu(); run(); }, document.createTextNode(label));
        b.setAttribute("role", "menuitem");
        menu.appendChild(b);
        return b;
    };
    item("Скачать недостающее…", () => downloadArtistFromLibrary(name));
    item(own ? "Другая шапка…" : "Своя шапка…", () => pickArtistPhoto(name));
    if (own) item("Вернуть фото из Deezer", () => dropArtistPhoto(name));
    document.body.appendChild(menu);

    const box = anchor.getBoundingClientRect();
    menu.style.left = Math.round(Math.max(8, Math.min(box.left, window.innerWidth - menu.offsetWidth - 8))) + "px";
    if (window.innerHeight - box.bottom > menu.offsetHeight + 12) {
        menu.style.top = Math.round(box.bottom + 6) + "px";
    } else {
        menu.style.top = "auto";
        menu.style.bottom = Math.round(window.innerHeight - box.top + 6) + "px";
    }
    anchor.setAttribute("aria-expanded", "true");
    openMenu = { menu, button: anchor };
    document.addEventListener("keydown", menuKeydown, true);
    document.addEventListener("pointerdown", menuPointerDown, true);
    menu.querySelector(".row-menu-item").focus();
}

function artistNote(text) {
    const note = document.getElementById("artistNote");
    if (note) note.textContent = text;
}

function pickArtistPhoto(name) {
    const picker = document.createElement("input");
    picker.type = "file";
    picker.accept = "image/jpeg,image/png,image/webp";
    picker.onchange = async () => {
        const file = picker.files && picker.files[0];
        if (!file) return;
        artistNote("Загружаю шапку…");
        const form = new FormData();
        form.append("image", file);
        try {
            const r = await fetch("/api/artist-photo?name=" + encodeURIComponent(name), {
                method: "POST", headers: headers(), body: form,
            });
            const data = await r.json().catch(() => ({}));
            if (!r.ok) { artistNote(data.detail || "Не удалось загрузить шапку"); return; }
        } catch (e) {
            artistNote("Не удалось загрузить шапку: " + e.message);
            return;
        }
        artistNote("");
        artistPhotoChanged(name);
    };
    picker.click();
}

async function dropArtistPhoto(name) {
    try {
        const r = await fetch("/api/artist-photo?name=" + encodeURIComponent(name), { method: "DELETE", headers: headers() });
        if (!r.ok) { artistNote("Не удалось убрать шапку"); return; }
    } catch (e) {
        artistNote("Не удалось убрать шапку: " + e.message);
        return;
    }
    artistPhotoChanged(name);
}

/* Новая шапка — на странице артиста сразу; кружки артиста на главной и в
 * списке подхватят её при следующей отрисовке. */
function artistPhotoChanged(name) {
    forgetArtistPhoto(name);
    if (activeView !== "viewArtist") return;
    const hero = document.querySelector("#artistBody .o-hero > .o-cover");
    const artist = groupArtists(labTracks || []).find((a) => a.name === name);
    if (hero) hero.replaceWith(artistHero(name, artist && artist.cover));
}

function openArtistPage(name) {
    pushPage({ view: "viewArtist", name });
    renderArtistPage(name);
}

async function renderArtistPage(name) {
    const ticket = ++labTicket;
    const rows = await labIndex();
    if (ticket !== labTicket) return;
    const artist = groupArtists(rows).find((a) => a.name === name);
    const body = document.getElementById("artistBody");
    if (!artist) {
        body.replaceChildren(backBar("Назад", "viewLibrary"), el("p", "empty", "Такого артиста в фонотеке нет."));
        switchView("viewArtist");
        return;
    }
    const albums = groupIntoAlbums(rows).filter((g) => mainArtist({ albumartist: g.artist }) === name && g.tracks.length > 1);
    const back = button("o-back o-back-float", () => goBack("viewLibrary"), labIcon("back"));
    back.setAttribute("aria-label", "Назад");
    const playAll = button("o-play-round", () => playQueue(artist.tracks, 0, "manual"), labIcon("play"));
    playAll.setAttribute("aria-label", "Слушать " + name);
    const shuffle = capsule("Перемешать", "shuffle", () => openShuffleChoice(shuffle,
        () => playShuffled(artist.tracks), () => shuffleTracks(artist.tracks, "artistNote")));
    shuffle.setAttribute("aria-haspopup", "menu");
    const note = el("p", "note");
    note.id = "artistNote";
    const more = button("o-more-round", () => openArtistMenu(more, name), document.createTextNode("⋯"));
    more.setAttribute("aria-haspopup", "menu");
    more.setAttribute("aria-expanded", "false");
    more.setAttribute("aria-label", "Ещё об артисте");
    more.title = "Скачать недостающее, своя шапка";

    put(body,
        el("header", "o-hero", "",
            artistHero(name, artist.cover),
            el("div", "o-hero-shade"),
            back,
            el("div", "o-hero-text", "", el("h1", "", name), playAll)),
        el("div", "o-actions", "", shuffle, el("span", "o-muted", tracksWord(artist.tracks.length)), more),
        note,
        labSection("Треки", null, labRows(artist.tracks, false, name)),
        albums.length ? labSection("Альбомы", null, labShelf(albums.map((g) =>
            labCard(labCover(g.tracks[0].path, THUMB_LARGE, name), g.album, tracksWord(g.tracks.length), () => openAlbumPage(g))))) : null);
    switchView("viewArtist");
    setViewTitle(name);
    markPlayingRow();
}

/* ---------------- Все подборки ---------------- */

let playlistsGridSignature = "";

function renderPlaylistsGrid(force) {
    const body = document.getElementById("playlistsBody");
    if (!body) return;
    const list = typeof knownPlaylists !== "undefined" ? knownPlaylists : [];
    /* Опрос раз в 30 с с теми же подборками не пересобирает сетку: иначе
     * фокус клавиатуры с карточки улетал на страницу. */
    const signature = JSON.stringify(list.map((p) => [p.name, p.tracks]));
    if (!force && signature === playlistsGridSignature && body.childElementCount) return;
    playlistsGridSignature = signature;
    if (!list.length) {
        body.replaceChildren(el("div", "o-empty", "",
            el("p", "", "Подборок пока нет. Соберите первую из треков фонотеки."),
            capsule("Новая подборка", "add", function () { askNewPlaylist(this); })));
        return;
    }
    const grid = el("div", "o-grid");
    for (const p of list) {
        grid.appendChild(labCard(labPlaylistCover(p.name), p.name, tracksWord(p.tracks), () => openPlaylist(p.name)));
    }
    body.replaceChildren(grid);
}

/* Подборки пришли или изменились — сетка следует за ними. */
(function followPlaylists() {
    const original = renderRail;
    renderRail = function (data) {
        original(data);
        if (activeView === "viewPlaylists") renderPlaylistsGrid();
    };
})();

/* ---------------- Поиск ---------------- */

let searchQueryText = "";
let searchTimer = 0;

function renderSearch(focus) {
    const input = document.getElementById("searchEverywhere");
    if (!input) return;
    if (input.value !== searchQueryText) input.value = searchQueryText;
    if (focus && !searchQueryText) setTimeout(() => input.focus({ preventScroll: true }), 30);
    fillSearch();
}

async function fillSearch() {
    const body = document.getElementById("searchBody");
    const q = searchQueryText.trim().toLowerCase();
    if (!q) {
        const moods = (homeDataForMoods || homeCache || {}).moods || [];
        put(body, moods.length ? labSection("Под настроение", null, el("div", "o-browse-grid", "",
            ...moods.filter((m) => m.tracks.length).map((m) =>
                button("o-browse", () => openMoodPage(m.key), labCover(m.tracks[0].path, THUMB_LARGE, m.name), el("span", "", m.name))))) : null);
        return;
    }
    const rows = await labIndex();
    if (searchQueryText.trim().toLowerCase() !== q) return;  // набрали дальше
    const hit = (s) => (s || "").toLowerCase().includes(q);
    const tracks = rows.filter((t) => hit(t.title) || hit(t.artist) || hit(t.album)).slice(0, 60);
    const artists = groupArtists(rows).filter((a) => hit(a.name)).slice(0, 12);
    const albums = groupIntoAlbums(rows).filter((g) => g.tracks.length > 1 && (hit(g.album) || hit(g.artist))).slice(0, 12);
    const youtube = button("o-yt-ask", () => {
        switchView("viewAdd");
        const field = document.getElementById("searchQuery");
        if (field) field.value = searchQueryText.trim();
        runSearch();
    }, labIcon("search"), el("span", "", "Найти «" + searchQueryText.trim() + "» на YouTube и скачать"));
    const parts = [];
    if (artists.length) {
        parts.push(labSection("Артисты", null, labShelf(artists.map((a) =>
            labCard(labCover(a.cover, THUMB_SMALL * 2, a.name, true), a.name, null, () => openArtistPage(a.name), "is-artist")), "is-artists")));
    }
    if (albums.length) {
        parts.push(labSection("Альбомы", null, labShelf(albums.map((g) =>
            labCard(labCover(g.tracks[0].path, THUMB_LARGE, g.artist), g.album, g.artist, () => openAlbumPage(g))))));
    }
    if (tracks.length) parts.push(labSection("Треки", null, labRows(tracks, false)));
    if (!parts.length) parts.push(el("div", "o-empty", "", el("p", "", "В фонотеке ничего не нашлось по «" + searchQueryText.trim() + "».")));
    parts.push(el("div", "o-yt", "", youtube));
    put(body, ...parts);
    markPlayingRow();
}

(function wireSearch() {
    const input = document.getElementById("searchEverywhere");
    const side = document.getElementById("sideSearch");
    if (input) {
        input.addEventListener("input", () => {
            clearTimeout(searchTimer);
            searchTimer = setTimeout(() => {
                searchQueryText = input.value;
                if (side && side.value !== input.value) side.value = input.value;
                fillSearch();
            }, SEARCH_DEBOUNCE_MS);
        });
    }
    if (side) {
        /* Поле в левой панели — тот же поиск: набор сразу открывает его. */
        side.addEventListener("input", () => {
            searchQueryText = side.value;
            if (activeView !== "viewSearch") switchView("viewSearch");
            else renderSearch(false);
        });
        side.addEventListener("keydown", (e) => {
            if (e.key === "Enter") { searchQueryText = side.value; switchView("viewSearch"); }
        });
    }
})();

/* Перемешать подборку — тоже с выбором (умное или простое). */
function openPlaylistShuffle(anchor) {
    const name = player.playlist && player.playlist.name;
    if (!name) return;
    openShuffleChoice(anchor,
        () => loadShuffle("plain", name, "playlistNote"),
        () => loadShuffle("smart", name, "playlistNote"));
}

syncNav();
