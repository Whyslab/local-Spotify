/* Вариант 4 — «Плакат».
 *
 * Ось: шрифт как изображение и навигация-указатель. Единственный вариант,
 * который не держится за обложки: они маленькие и в два цвета (чернила и
 * розовый), а страницу несут названия, набранные узким гротеском во всю
 * ширину, как афиша концерта. Нижних вкладок нет: разделы — это указатель
 * из крупных слов с настоящими числами. Внизу — бумажная «строка сейчас»
 * с тем, что играет. */
(function () {
    "use strict";
    const L = window.Lab;
    const { h, icon, clear } = L;

    let ctx = null;
    const els = {};
    let viewCleanup = null;
    let wide = false;
    const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

    // ------------------------------------------------------------------
    // Мелкие части
    // ------------------------------------------------------------------

    function btn(cls, iconName, label, onclick, extra) {
        return h("button", Object.assign({ class: cls, type: "button", "aria-label": label, title: label, onclick }, extra || {}),
            iconName ? icon(iconName) : null);
    }

    function paperBtn(label, iconName, onclick, cls) {
        return h("button", { class: "k-btn " + (cls || "is-paper"), type: "button", onclick },
            iconName ? icon(iconName) : null, h("span", null, label));
    }

    /* Обложка в два цвета: серое изображение, умноженное на розовый,
     * тени подняты до чернил (lighten). Блендинг внутри — isolation. */
    function duo(path, px, cls) {
        return h("div", { class: "k-duo " + (cls || "") }, L.cover(path, px));
    }
    function duoPlaylist(pl, px, cls) {
        return h("div", { class: "k-duo " + (cls || "") }, L.playlistCover(pl, px));
    }

    function isCurrent(track) {
        const c = L.current();
        return Boolean(c && track && c.path === track.path);
    }

    const tracksWord = (n) => L.plural(n, ["трек", "трека", "треков"]);

    /* Строка сет-листа: номер — порядок, в котором трек заиграет. */
    function setRow(track, list, i, opts) {
        opts = opts || {};
        return h("div", { class: "k-row" + (isCurrent(track) ? " is-playing" : "") + (track.missing ? " is-missing" : ""), dataset: { path: track.path } },
            h("button", {
                class: "k-row-main", type: "button",
                onclick: () => L.play(list, i, { source: opts.source || "" }),
            },
                h("span", { class: "k-row-num" }, String(i + 1).padStart(2, "0")),
                h("span", { class: "k-row-text" },
                    h("span", { class: "k-row-title" }, track.title),
                    opts.hideArtist ? null : h("span", { class: "k-row-artist" }, track.artist || "")),
                h("span", { class: "k-row-time" }, L.fmtTime(track.duration))),
            btn("k-icon k-row-more", "more", "Действия с треком", (e) => openMenu(track, e)));
    }

    function setlist(tracks, opts) {
        return h("div", { class: "k-setlist" }, tracks.map((t, i) => setRow(t, tracks, i, opts)));
    }

    function markPlaying() {
        if (!els.app) return;
        const c = L.current();
        for (const row of els.app.querySelectorAll(".k-row")) {
            row.classList.toggle("is-playing", Boolean(c && row.dataset.path === c.path));
        }
    }

    function heading(text, cls) {
        return h("h2", { class: "k-h2 " + (cls || "") }, text);
    }

    function pageTitle(text, meta) {
        return h("header", { class: "k-page-head" },
            h("h1", { class: "k-h1" }, text),
            meta ? h("p", { class: "k-meta" }, meta) : null);
    }

    function empty(text, actionLabel, run) {
        return h("div", { class: "k-empty" }, h("p", null, text),
            actionLabel ? paperBtn(actionLabel, null, run) : null);
    }

    function backLink() {
        return h("button", { class: "k-back", type: "button", onclick: () => history.back() }, icon("back"), h("span", null, "Назад"));
    }

    // ------------------------------------------------------------------
    // Указатель разделов
    // ------------------------------------------------------------------

    function albumsShown() {
        return L.data.albums.filter((a) => a.tracks.length > 1 || L.data.albums.length < 40);
    }

    function indexItems() {
        const d = L.data;
        const ready = d.ready;
        return [
            { key: "home", label: "Главная", go: () => L.go("home"), match: ["home", "mood"] },
            { key: "artists", label: "Артисты", count: ready ? d.artists.length : null, go: () => L.go("library", { tab: "artists" }), match: ["library-artists", "artist"] },
            { key: "albums", label: "Альбомы", count: ready ? albumsShown().length : null, go: () => L.go("library", { tab: "albums" }), match: ["library-albums", "album"] },
            { key: "tracks", label: "Треки", count: ready ? d.tracks.length : null, go: () => L.go("library", { tab: "tracks" }), match: ["library-tracks"] },
            { key: "playlists", label: "Подборки", count: ready ? d.playlists.length : null, go: () => L.go("playlists"), match: ["playlists", "playlist"] },
            { key: "search", label: "Поиск", go: () => L.go("search"), match: ["search"] },
            { key: "add", label: "Добавить", go: () => L.go("add"), match: ["add"] },
            { key: "service", label: "Служба", go: () => L.go("service"), match: ["service"] },
        ];
    }

    function routeKeys(r) {
        const keys = [r.name];
        if (r.name === "library") keys.push("library-" + (r.query.tab || "tracks"));
        return keys;
    }

    function sectionName(r) {
        const keys = routeKeys(r);
        const item = indexItems().find((it) => it.match.some((m) => keys.includes(m)));
        return item ? item.label : "Главная";
    }

    function buildIndex() {
        const list = h("nav", { class: "k-index-list", "aria-label": "Разделы" });
        const pls = h("div", { class: "k-index-pls" });
        els.indexList = list;
        els.indexPls = pls;
        const box = h("aside", { class: "k-index", id: "k-index" },
            h("div", { class: "k-index-top" },
                h("span", { class: "k-wordmark" }, "Фонотека"),
                h("button", { class: "k-index-close", type: "button", onclick: () => setIndex(false) }, icon("close"), h("span", null, "Закрыть"))),
            list, pls);
        els.index = box;
        fillIndex();
        return box;
    }

    function fillIndex() {
        if (!els.indexList) return;
        const keys = routeKeys(L.route.current);
        clear(els.indexList, indexItems().map((it) => {
            const on = it.match.some((m) => keys.includes(m));
            return h("button", {
                class: "k-index-item" + (on ? " is-on" : ""), type: "button",
                "aria-current": on ? "page" : null,
                onclick: () => { setIndex(false); it.go(); },
            }, h("span", { class: "k-index-word" }, it.label),
                it.count !== null && it.count !== undefined ? h("span", { class: "k-index-count" }, String(it.count)) : null);
        }));
        clear(els.indexPls, L.data.playlists.length ? h("p", { class: "k-index-sub" }, "Подборки") : null,
            L.data.playlists.map((p) => h("button", { class: "k-index-pl", type: "button", onclick: () => { setIndex(false); L.go("playlist", p.name); } },
                h("span", null, p.name), h("span", { class: "k-index-pl-n" }, String(p.tracks)))));
        if (els.topName) els.topName.textContent = sectionName(L.route.current);
    }

    function setIndex(open) {
        if (!els.index) return;
        els.index.classList.toggle("is-open", open);
        els.app.classList.toggle("is-index", open);
        if (els.menuBtn) els.menuBtn.setAttribute("aria-expanded", open ? "true" : "false");
        if (open && !wide) {
            const first = els.indexList.querySelector("button");
            if (first) first.focus({ preventScroll: true });
        }
    }

    function buildTop() {
        const name = h("span", { class: "k-top-name" }, "Главная");
        els.topName = name;
        const menu = h("button", {
            class: "k-top-menu", type: "button", "aria-controls": "k-index", "aria-expanded": "false",
            onclick: () => setIndex(!els.index.classList.contains("is-open")),
        }, icon("menu"), h("span", null, "Меню"));
        els.menuBtn = menu;
        return h("header", { class: "k-top" }, menu, name,
            btn("k-icon", "search", "Поиск", () => L.go("search")));
    }

    // ------------------------------------------------------------------
    // Экраны
    // ------------------------------------------------------------------

    function moodPoster(m) {
        const tracks = m.tracks.map((t) => L.data.byPath.get(t.path) || t);
        return h("section", { class: "k-poster" },
            h("button", { class: "k-poster-name", type: "button", onclick: () => L.go("mood", m.key) }, m.name),
            h("p", { class: "k-poster-hint" }, m.hint),
            setlist(tracks.slice(0, 6), { source: m.name }),
            h("div", { class: "k-actions" },
                paperBtn("Играть всё", "play", () => L.play(tracks, 0, { shuffle: false, source: m.name })),
                h("button", { class: "k-text-btn", type: "button", onclick: () => L.go("mood", m.key) }, "Все " + tracksWord(tracks.length))));
    }

    function viewHome() {
        const d = L.data;
        const home = d.home || { moods: [], discover: { tracks: [], based_on: [] }, albums: [] };
        const disc = (home.discover && home.discover.tracks) || [];
        const based = (home.discover && home.discover.based_on) || [];
        const fresh = d.tracks.slice(0, 10);
        return h("div", { class: "k-page" },
            home.moods.map(moodPoster),
            h("section", { class: "k-poster" },
                h("h2", { class: "k-poster-name is-static" }, "Новое"),
                h("p", { class: "k-poster-hint" }, "Последнее, что приехало в фонотеку"),
                setlist(fresh, { source: "Новое" }),
                h("div", { class: "k-actions" },
                    h("button", { class: "k-text-btn", type: "button", onclick: () => L.go("library", { tab: "tracks" }) }, "Все треки"))),
            disc.length ? h("section", { class: "k-poster" },
                h("h2", { class: "k-poster-name is-static" }, "Похоже на любимое"),
                based.length ? h("p", { class: "k-names" }, based.map((name, i) => [
                    L.data.artistByName.get(name)
                        ? h("button", { class: "k-name", type: "button", onclick: () => L.go("artist", name) }, name)
                        : h("span", { class: "k-name" }, name),
                    i < based.length - 1 ? h("span", { class: "k-name-sep", "aria-hidden": "true" }, "/") : null,
                ])) : null,
                setlist(disc.slice(0, 8), { source: "Похоже на любимое" })) : null,
            home.albums.length ? h("section", { class: "k-poster" },
                h("h2", { class: "k-poster-name is-static" }, "Альбомы"),
                h("div", { class: "k-album-list" }, home.albums.map((a) => albumRow({ cover: a.cover, album: a.album, artist: a.artist, count: a.count })))) : null);
    }

    function albumRow(a) {
        return h("button", { class: "k-album", type: "button", onclick: () => L.go("album", a.artist, a.album) },
            duo(a.cover, 64, "is-thumb"),
            h("span", { class: "k-album-text" },
                h("span", { class: "k-album-name" }, a.album),
                h("span", { class: "k-album-sub" }, a.artist + ", " + tracksWord(a.count))));
    }

    let librarySort = "new";
    function viewLibrary(r) {
        const tab = r.query.tab || "tracks";
        if (tab === "artists") return viewArtists();
        if (tab === "albums") {
            const albums = albumsShown();
            return h("div", { class: "k-page" },
                pageTitle("Альбомы", L.plural(albums.length, ["альбом", "альбома", "альбомов"])),
                h("div", { class: "k-album-list" }, albums.map((a) => albumRow({ cover: a.cover, album: a.album, artist: a.artist, count: a.tracks.length }))));
        }
        const list = librarySort === "name"
            ? [...L.data.tracks].sort((x, y) => x.title.localeCompare(y.title, "ru", { sensitivity: "base" }))
            : L.data.tracks;
        const total = list.reduce((s, t) => s + (t.duration || 0), 0);
        return h("div", { class: "k-page" },
            pageTitle("Треки", tracksWord(list.length) + ", " + L.fmtTotal(total)),
            h("div", { class: "k-actions" },
                paperBtn("Играть", "play", () => L.play(list, 0, { shuffle: false, source: "Фонотека" })),
                paperBtn("Вперемешку", "shuffle", () => L.play(list, -1, { shuffle: true, source: "Фонотека" }), "is-line"),
                h("button", {
                    class: "k-text-btn", type: "button",
                    onclick: () => { librarySort = librarySort === "new" ? "name" : "new"; renderRoute(); },
                }, librarySort === "new" ? "Сначала новые" : "По названию")),
            setlist(list, { source: "Фонотека" }));
    }

    function letterOf(name) {
        const ch = (name.trim()[0] || "#").toUpperCase();
        if (ch === "Ё") return "Е";
        return /[A-ZА-Я]/.test(ch) ? ch : "#";
    }

    function viewArtists() {
        const groups = new Map();
        for (const a of L.data.artists) {
            const letter = letterOf(a.name);
            if (!groups.has(letter)) groups.set(letter, []);
            groups.get(letter).push(a);
        }
        const order = [...groups.keys()].filter((k) => k !== "#");
        if (groups.has("#")) order.push("#");
        const anchors = new Map();
        const sections = order.map((letter) => {
            const head = h("h2", { class: "k-letter", id: "k-letter-" + letter }, letter);
            anchors.set(letter, head);
            return h("section", { class: "k-letter-group" }, head,
                groups.get(letter).map((a) => {
                    const row = h("button", { class: "k-artist", type: "button", onclick: () => L.go("artist", a.name) },
                        h("span", { class: "k-artist-name" }, a.name),
                        h("span", { class: "k-artist-n" }, String(a.tracks.length)));
                    if (hoverable()) {
                        row.addEventListener("mouseenter", (e) => showFollow(a.cover, e));
                        row.addEventListener("mousemove", moveFollow);
                        row.addEventListener("mouseleave", hideFollow);
                    }
                    return row;
                }));
        });

        const jump = (letter) => {
            const el = anchors.get(letter);
            if (el) el.parentElement.scrollIntoView({ block: "start" });
        };
        const rail = h("nav", { class: "k-rail", "aria-label": "Буквы" }, order.map((letter) =>
            h("button", { class: "k-rail-l", type: "button", dataset: { letter }, onclick: () => jump(letter) }, letter)));
        // Палец ведут по полоске букв — список прыгает следом, как в телефонной книге.
        let dragging = false;
        rail.addEventListener("pointerdown", (e) => {
            if (e.pointerType === "mouse") return;
            dragging = true;
            rail.setPointerCapture(e.pointerId);
        });
        rail.addEventListener("pointermove", (e) => {
            if (!dragging) return;
            const el = document.elementFromPoint(rail.getBoundingClientRect().left + 6, e.clientY);
            const letter = el && el.dataset && el.dataset.letter;
            if (letter && letter !== rail.dataset.at) { rail.dataset.at = letter; jump(letter); }
        });
        const stop = () => { dragging = false; delete rail.dataset.at; };
        rail.addEventListener("pointerup", stop);
        rail.addEventListener("pointercancel", stop);

        viewCleanup = hideFollow;
        return h("div", { class: "k-page k-artists" },
            pageTitle("Артисты", L.plural(L.data.artists.length, ["артист", "артиста", "артистов"])),
            sections, rail);
    }

    const hoverable = () => matchMedia("(hover: hover) and (pointer: fine)").matches;
    function showFollow(path, e) {
        if (!els.follow) return;
        if (els.follow.dataset.path !== path) {
            els.follow.dataset.path = path;
            clear(els.follow, duo(path, 160));
        }
        els.follow.classList.add("is-on");
        moveFollow(e);
    }
    function moveFollow(e) {
        if (!els.follow) return;
        els.follow.style.transform = `translate(${e.clientX + 24}px, ${e.clientY - 80}px)`;
    }
    function hideFollow() { if (els.follow) els.follow.classList.remove("is-on"); }

    function collection({ coverEl, title, subtitle, onSubtitle, meta, tracks, source, extra, after }) {
        const total = tracks.reduce((s, t) => s + (t.duration || 0), 0);
        return h("div", { class: "k-page k-collection" },
            backLink(),
            h("header", { class: "k-col-head" },
                h("div", { class: "k-col-art" }, coverEl),
                h("h1", { class: "k-h1 is-col" }, title),
                subtitle ? (onSubtitle
                    ? h("button", { class: "k-col-sub", type: "button", onclick: onSubtitle }, subtitle)
                    : h("p", { class: "k-col-sub is-static" }, subtitle)) : null,
                h("p", { class: "k-meta" }, meta || (tracksWord(tracks.length) + ", " + L.fmtTotal(total)))),
            h("div", { class: "k-actions" },
                paperBtn("Играть", "play", () => L.play(tracks, 0, { shuffle: false, source })),
                paperBtn("Вперемешку", "shuffle", () => L.play(tracks, -1, { shuffle: true, source }), "is-line")),
            extra || null,
            setlist(tracks, { source }),
            after || null);
    }

    function viewAlbum(r) {
        const [artist, album] = r.params;
        const al = L.data.albumByKey.get(L.albumKey(artist, album));
        if (!al) return h("div", { class: "k-page" }, backLink(), empty("Такого альбома в фонотеке нет."));
        return collection({
            coverEl: duo(al.cover, 300), title: al.album, subtitle: al.artist,
            onSubtitle: () => L.go("artist", al.artist), tracks: al.tracks, source: al.album,
        });
    }

    function viewMood(r) {
        const m = L.data.home && L.data.home.moods.find((x) => x.key === r.params[0]);
        if (!m) return h("div", { class: "k-page" }, backLink(), empty("Подборка по настроению обновилась — откройте главную."));
        const tracks = m.tracks.map((t) => L.data.byPath.get(t.path) || t);
        return collection({ coverEl: duo(m.tracks[0] && m.tracks[0].path, 300), title: m.name, subtitle: m.hint, tracks, source: m.name });
    }

    function viewArtist(r) {
        const a = L.data.artistByName.get(r.params[0]);
        if (!a) return h("div", { class: "k-page" }, backLink(), empty("Такого артиста в фонотеке нет."));
        const albums = L.data.albums.filter((al) => al.artist === a.name && al.tracks.length > 1);
        return collection({
            coverEl: duo(a.cover, 300), title: a.name,
            meta: tracksWord(a.tracks.length) + (albums.length ? ", " + L.plural(albums.length, ["альбом", "альбома", "альбомов"]) : ""),
            tracks: a.tracks, source: a.name,
            after: albums.length ? [heading("Альбомы"), h("div", { class: "k-album-list" }, albums.map((al) =>
                albumRow({ cover: al.cover, album: al.album, artist: al.artist, count: al.tracks.length })))] : null,
        });
    }

    function viewPlaylists() {
        const pls = L.data.playlists;
        return h("div", { class: "k-page" },
            pageTitle("Подборки", L.plural(pls.length, ["подборка", "подборки", "подборок"])),
            h("div", { class: "k-actions" }, paperBtn("Новая подборка", "add", () => L.act("Новая подборка"), "is-line")),
            pls.length ? h("div", { class: "k-pl-list" }, pls.map((p) =>
                h("button", { class: "k-pl", type: "button", onclick: () => L.go("playlist", p.name) },
                    duoPlaylist(p, 96, "is-thumb"),
                    h("span", { class: "k-pl-name" }, p.name),
                    h("span", { class: "k-pl-n" }, tracksWord(p.tracks)))))
                : empty("Подборок пока нет. Соберите первую из треков фонотеки."));
    }

    function viewPlaylist(r) {
        const name = r.params[0];
        const holder = h("div", { class: "k-page" }, backLink(), h("div", { class: "k-skeleton is-title" }), h("div", { class: "k-skeleton" }));
        const meta = L.data.playlists.find((p) => p.name === name) || { name };
        L.playlistTracks(name).then((pl) => {
            if (!holder.isConnected) return;
            const total = pl.tracks.reduce((s, t) => s + (t.duration || 0), 0);
            holder.replaceWith(collection({
                coverEl: duoPlaylist(meta, 300), title: name,
                meta: tracksWord(pl.tracks.length) + ", " + L.fmtTotal(total) + (meta.updated_at ? ", изменена " + L.agoText(meta.updated_at) : ""),
                tracks: pl.tracks, source: name,
                extra: h("div", { class: "k-actions is-quiet" },
                    h("button", { class: "k-text-btn", type: "button", onclick: () => L.act("Изменить подборку") }, "Изменить"),
                    h("button", { class: "k-text-btn", type: "button", onclick: () => L.act("Сменить обложку") }, "Обложка"),
                    h("button", { class: "k-text-btn is-danger", type: "button", onclick: () => L.act("Удалить подборку") }, "Удалить")),
            }));
        }).catch((e) => clear(holder, backLink(), empty(e.message)));
        return holder;
    }

    let searchQuery = "";
    function viewSearch(r) {
        searchQuery = r.query.q !== undefined ? r.query.q : searchQuery;
        const results = h("div", { class: "k-results" });
        const input = h("input", {
            class: "k-search-input", type: "search", placeholder: "Кого ищем?", value: searchQuery,
            "aria-label": "Поиск по фонотеке", autocomplete: "off", enterkeyhint: "search",
        });
        let timer = 0;
        input.addEventListener("input", () => {
            clearTimeout(timer);
            timer = setTimeout(() => {
                searchQuery = input.value;
                history.replaceState(history.state, "", L.href("search", { q: searchQuery }));
                fill();
            }, 120);
        });
        viewCleanup = () => clearTimeout(timer);

        function fill() {
            const q = searchQuery.trim();
            if (!q) {
                const top = [...L.data.artists].sort((x, y) => y.tracks.length - x.tracks.length).slice(0, 18);
                clear(results,
                    heading("Чаще всего в фонотеке"),
                    h("p", { class: "k-names is-big" }, top.map((a, i) => [
                        h("button", { class: "k-name", type: "button", onclick: () => L.go("artist", a.name) }, a.name),
                        i < top.length - 1 ? h("span", { class: "k-name-sep", "aria-hidden": "true" }, "/") : null,
                    ])));
                return;
            }
            const found = L.searchLocal(q, 60);
            const yt = h("div", { class: "k-yt" },
                h("button", { class: "k-yt-ask", type: "button", onclick: () => askYouTube(q, yt) },
                    icon("search"), h("span", null, "Найти «" + q + "» на YouTube")));
            if (!found.tracks.length && !found.artists.length && !found.albums.length) {
                clear(results, empty("В фонотеке ничего не нашлось по «" + q + "»."), yt);
                return;
            }
            clear(results,
                found.artists.length ? [heading("Артисты"), h("p", { class: "k-names is-big" }, found.artists.map((a, i) => [
                    h("button", { class: "k-name", type: "button", onclick: () => L.go("artist", a.name) }, a.name),
                    i < found.artists.length - 1 ? h("span", { class: "k-name-sep", "aria-hidden": "true" }, "/") : null,
                ]))] : null,
                found.albums.length ? [heading("Альбомы"), h("div", { class: "k-album-list" }, found.albums.map((a) =>
                    albumRow({ cover: a.cover, album: a.album, artist: a.artist, count: a.tracks.length })))] : null,
                found.tracks.length ? [heading("Треки"), setlist(found.tracks, { source: "Поиск" })] : null,
                yt);
        }

        fill();
        setTimeout(() => { if (!searchQuery && input.isConnected) input.focus({ preventScroll: true }); }, 50);
        return h("div", { class: "k-page" },
            h("label", { class: "k-search" }, h("span", { class: "k-visually-hidden" }, "Поиск"), input),
            results);
    }

    function askYouTube(q, box) {
        clear(box, h("div", { class: "k-skeleton" }));
        L.searchYouTube(q).then((body) => {
            const list = body.results || [];
            clear(box, heading("На YouTube"), list.length ? h("div", { class: "k-setlist" }, list.map((v, i) =>
                h("div", { class: "k-row" },
                    h("div", { class: "k-row-main is-static" },
                        h("span", { class: "k-row-num" }, String(i + 1).padStart(2, "0")),
                        h("span", { class: "k-row-text" },
                            h("span", { class: "k-row-title" }, v.title || ""),
                            h("span", { class: "k-row-artist" }, v.channel || v.uploader || "")),
                        h("span", { class: "k-row-time" }, v.duration ? L.fmtTime(v.duration) : "")),
                    h("button", { class: "k-chip", type: "button", onclick: () => L.act("Скачать в фонотеку") }, "Скачать"))))
                : empty("YouTube ничего не нашёл."));
        }).catch((e) => clear(box, empty("Поиск на YouTube не ответил: " + e.message)));
    }

    function statusText(t) {
        return { done: "готово", failed: "не получилось", error: "не получилось", processing: "скачивается", downloading: "скачивается", tagging: "подписываем теги", queued: "в очереди", pending: "в очереди", skipped: "уже есть" }[t.status] || t.status;
    }

    function viewAdd() {
        const area = h("textarea", {
            class: "k-textarea", rows: "4", placeholder: "Ссылки на YouTube, Spotify или Deezer — по одной в строке",
            "aria-label": "Ссылки для скачивания",
        });
        const tasksBox = h("div", { class: "k-setlist" }, h("div", { class: "k-skeleton" }));
        function loadTasks() {
            L.tasks().then((tasks) => {
                if (!tasksBox.isConnected) return;
                clear(tasksBox, tasks.length ? tasks.slice(0, 30).map((t) =>
                    h("div", { class: "k-task is-" + t.status },
                        h("span", { class: "k-task-state" }, statusText(t)),
                        h("span", { class: "k-row-text" },
                            h("span", { class: "k-row-title" }, t.title || t.url),
                            h("span", { class: "k-row-artist" }, [t.artist, t.error].filter(Boolean).join(" — "))),
                        h("span", { class: "k-row-time" }, (t.updated_at || "").slice(5, 16))))
                    : empty("Загрузок ещё не было."));
            }).catch((e) => clear(tasksBox, empty(e.message)));
        }
        loadTasks();
        const timer = setInterval(loadTasks, 5000);
        viewCleanup = () => clearInterval(timer);
        return h("div", { class: "k-page" },
            pageTitle("Добавить", "Ссылка или файл — дальше служба скачает, подпишет и положит в фонотеку"),
            h("div", { class: "k-add" }, area,
                h("div", { class: "k-actions" },
                    paperBtn("Скачать", "download", () => L.act("Скачать")),
                    paperBtn("Из файлов", "add", () => L.act("Загрузить файлы"), "is-line"))),
            heading("Загрузки"),
            tasksBox);
    }

    function viewService() {
        const stats = h("div", { class: "k-stats" }, h("div", { class: "k-skeleton" }));
        const rows = h("div", { class: "k-kv-list" });
        L.health().then((s) => {
            clear(stats, [
                [s.tracks, ["трек", "трека", "треков"]],
                [s.albums, ["альбом", "альбома", "альбомов"]],
                [s.playlists, ["подборка", "подборки", "подборок"]],
                [s.plays_logged, ["прослушивание", "прослушивания", "прослушиваний"]],
            ].map(([n, forms]) => {
                const word = L.plural(Number(n) || 0, forms).replace(/^\d+\s/, "");
                return h("div", { class: "k-stat" }, h("span", { class: "k-stat-n" }, String(n)), h("span", { class: "k-stat-l" }, word));
            }));
            clear(rows, [
                ["Служба", s.status === "healthy" ? "работает" : s.status],
                ["Navidrome", s.navidrome === "configured" ? "подключён" : s.navidrome],
                ["В очереди загрузки", String(s.queue_size)],
                ["Ждут синхронизации", String(s.navidrome_pending)],
                ["ffmpeg", s.ffmpeg], ["База", s.database],
            ].map(([k, v]) => h("div", { class: "k-kv" }, h("span", null, k), h("span", null, v))));
        }).catch((e) => clear(stats, empty(e.message)));
        return h("div", { class: "k-page" },
            pageTitle("Служба"), stats, rows,
            h("div", { class: "k-actions" },
                paperBtn("Проверить фонотеку", "service", () => L.act("Проверить фонотеку"), "is-line"),
                paperBtn("Синхронизировать", "repeat", () => L.act("Синхронизировать с Navidrome"), "is-line")));
    }

    const VIEWS = {
        home: viewHome, library: viewLibrary, artist: viewArtist, album: viewAlbum, mood: viewMood,
        playlists: viewPlaylists, playlist: viewPlaylist, search: viewSearch, add: viewAdd, service: viewService,
    };

    function renderRoute() {
        if (viewCleanup) { viewCleanup(); viewCleanup = null; }
        const r = L.route.current;
        fillIndex();
        setIndex(false);
        if (!L.data.ready) {
            clear(els.view, L.data.error
                ? h("div", { class: "k-page" }, empty(L.data.error.message, L.data.error.auth ? "Открыть плеер" : "Повторить",
                    () => { if (L.data.error.auth) location.href = "/"; else L.load(); }))
                : h("div", { class: "k-page" }, h("div", { class: "k-skeleton is-title" }), h("div", { class: "k-skeleton" }), h("div", { class: "k-skeleton" })));
            return;
        }
        const view = VIEWS[r.name] || VIEWS.home;
        els.view.replaceChildren(view(r));
        window.scrollTo(0, 0);
    }

    // ------------------------------------------------------------------
    // Строка «сейчас» внизу
    // ------------------------------------------------------------------

    function buildLine() {
        const play = btn("k-line-play", "play", "Играть", () => L.toggle());
        const text = h("span", { class: "k-line-text" });
        const time = h("span", { class: "k-line-time" }, "0:00");
        const line = h("div", { class: "k-line", hidden: true },
            h("div", { class: "k-line-progress" }, h("i")),
            play,
            h("button", { class: "k-line-open", type: "button", onclick: () => L.openNow(), "aria-label": "Открыть плеер" },
                h("span", { class: "k-line-clip" }, text), time),
            btn("k-line-next", "next", "Следующий", () => L.next()));
        Object.assign(els, { line, linePlay: play, lineText: text, lineTime: time });
        return line;
    }

    /* Бегущая строка — только если название не влезает и движение не выключено. */
    function fitMarquee() {
        const text = els.lineText;
        if (!text) return;
        text.classList.remove("is-running");
        text.style.removeProperty("--shift");
        requestAnimationFrame(() => {
            const clip = text.parentElement;
            const over = text.scrollWidth - clip.clientWidth;
            if (over > 4 && !reduced()) {
                text.style.setProperty("--shift", -over - 16 + "px");
                text.style.setProperty("--dur", Math.max(6, over / 18) + "s");
                text.classList.add("is-running");
            }
        });
    }

    // ------------------------------------------------------------------
    // Сейчас играет — афиша
    // ------------------------------------------------------------------

    function buildNow() {
        const now = h("div", { class: "k-now", role: "dialog", "aria-modal": "true", "aria-label": "Сейчас играет", hidden: true });
        els.now = now;
        return now;
    }

    function fitTitle(el, max, min, lines) {
        if (!el || !el.isConnected) return;
        let size = max;
        el.style.fontSize = size + "px";
        const lh = parseFloat(getComputedStyle(el).lineHeight) / size || 0.9;
        while (size > min && el.scrollHeight > size * lh * lines + 6) {
            size -= 3;
            el.style.fontSize = size + "px";
        }
    }

    function renderNow() {
        const t = L.current();
        const open = L.ui.now && Boolean(t);
        const now = els.now;
        if (!open) {
            if (!now.hidden) {
                now.classList.remove("is-open");
                document.documentElement.classList.remove("k-locked");
                setTimeout(() => { if (!L.ui.now) now.hidden = true; }, 260);
            }
            els.karaoke = null;
            return;
        }
        const tab = L.ui.nowTab;
        const range = h("input", { class: "k-range", type: "range", min: "0", max: "1000", value: "0", step: "1", "aria-label": "Позиция в треке" });
        range.addEventListener("input", () => { range.dataset.drag = "1"; els.nowT0.textContent = L.fmtTime(range.value / 1000 * L.duration()); });
        range.addEventListener("change", () => { delete range.dataset.drag; L.seek(range.value / 1000 * L.duration()); });
        const t0 = h("span", { class: "k-time" }, "0:00");
        const t1 = h("span", { class: "k-time is-total" }, L.fmtTime(L.duration()));
        const play = btn("k-now-play", L.player.audio.paused ? "play" : "pause", "Играть или пауза", () => L.toggle());
        const shuf = h("button", { class: "k-toggle" + (L.player.shuffle ? " is-on" : ""), type: "button", "aria-pressed": L.player.shuffle ? "true" : "false", onclick: () => L.setShuffle() }, "Вперемешку");
        const rep = h("button", { class: "k-toggle" + (L.player.repeat !== "off" ? " is-on" : ""), type: "button", onclick: () => L.cycleRepeat() }, repeatLabel());
        Object.assign(els, { nowRange: range, nowT0: t0, nowT1: t1, nowPlay: play, nowShuf: shuf, nowRep: rep });

        const title = h("h1", { class: "k-now-title" }, t.title);
        const head = h("div", { class: "k-now-head" },
            h("div", { class: "k-now-art" }, duo(t.path, 300)),
            title,
            h("button", { class: "k-now-artist", type: "button", onclick: () => { L.closeNow(); L.go("artist", L.mainArtist(t)); } }, t.artist || ""));

        let panel;
        if (tab === "lyrics") panel = lyricsPanel(t);
        else if (tab === "queue") panel = queuePanel();
        else panel = h("div", { class: "k-now-big" }, duo(t.path, 600));

        const tabs = h("div", { class: "k-now-tabs", role: "tablist" },
            [["cover", "Трек"], ["lyrics", "Текст"], ["queue", "Очередь"]].map(([key, label]) =>
                h("button", {
                    class: "k-now-tab" + (tab === key ? " is-on" : ""), type: "button", role: "tab",
                    "aria-selected": tab === key ? "true" : "false", onclick: () => L.setNowTab(key),
                }, label)));

        clear(now,
            h("div", { class: "k-now-bar" },
                h("button", { class: "k-now-close", type: "button", onclick: () => L.closeNow() }, icon("down"), h("span", null, "Свернуть")),
                h("span", { class: "k-now-source" }, L.player.source ? "Из: " + L.player.source : ""),
                btn("k-icon", "more", "Действия", (e) => openMenu(t, e))),
            h("div", { class: "k-now-body is-" + tab },
                head,
                h("div", { class: "k-now-panel" }, panel),
                h("div", { class: "k-now-ctl" },
                    range,
                    h("div", { class: "k-times" }, t0, t1),
                    h("div", { class: "k-now-buttons" },
                        btn("k-now-skip", "prev", "Предыдущий", () => L.prev()),
                        play,
                        btn("k-now-skip", "next", "Следующий", () => L.next())),
                    h("div", { class: "k-now-modes" }, shuf, rep),
                    tabs)));

        if (now.hidden) {
            now.hidden = false;
            document.documentElement.classList.add("k-locked");
            requestAnimationFrame(() => requestAnimationFrame(() => now.classList.add("is-open")));
        }
        const fit = () => {
            const compact = tab !== "cover" && !wide;
            const max = compact ? 44 : wide ? Math.min(160, Math.round(innerWidth * 0.11)) : Math.min(150, Math.round(innerWidth * 0.36));
            fitTitle(title, max, compact ? 24 : 34, compact ? 2 : 3);
        };
        fit();
        if (document.fonts && document.fonts.ready) document.fonts.ready.then(fit);
        onTime();
    }

    function repeatLabel() {
        return { off: "Повтор: нет", all: "Повтор: всё", one: "Повтор: трек" }[L.player.repeat];
    }

    function lyricsPanel(t) {
        const box = h("div", { class: "k-lyrics" }, h("p", { class: "k-lyrics-wait" }, "Ищем текст…"));
        els.karaoke = null;
        L.lyrics(t.path).then((body) => {
            if (L.current() !== t || !box.isConnected) return;
            if (body.synced && body.synced.length) {
                const prev = h("button", { class: "k-k-prev", type: "button" });
                const cur = h("button", { class: "k-k-cur", type: "button" });
                const next = h("button", { class: "k-k-next", type: "button" });
                for (const b of [prev, cur, next]) b.addEventListener("click", () => { if (b.dataset.at) L.seek(Number(b.dataset.at)); });
                els.karaoke = { lines: body.synced, prev, cur, next, idx: -2 };
                clear(box, h("div", { class: "k-karaoke" }, prev, cur, next));
                onTime();
            } else if (body.plain) {
                clear(box, h("div", { class: "k-plain" }, body.plain.split("\n").map((line) => h("p", null, line || " "))));
            } else {
                clear(box, h("p", { class: "k-lyrics-wait" }, body.reason || "Текста для этого трека нет."),
                    paperBtn("Найти другой текст", "search", () => L.act("Выбрать текст"), "is-line"));
            }
        });
        return box;
    }

    function setLine(b, item, fallback) {
        b.textContent = item ? (item.line || "♪") : (fallback || "");
        if (item) b.dataset.at = String(item.at); else delete b.dataset.at;
        b.disabled = !item;
    }

    function updateKaraoke(pos) {
        const k = els.karaoke;
        if (!k || !k.cur.isConnected) return;
        let idx = -1;
        for (let i = 0; i < k.lines.length; i++) {
            if (k.lines[i].at <= pos + 0.25) idx = i; else break;
        }
        if (idx === k.idx) return;
        k.idx = idx;
        setLine(k.prev, k.lines[idx - 1]);
        setLine(k.cur, k.lines[idx], "♪");
        setLine(k.next, k.lines[idx + 1]);
        k.cur.classList.toggle("is-wait", idx < 0);
        if (!reduced()) {
            k.cur.classList.remove("is-in");
            void k.cur.offsetWidth;
            k.cur.classList.add("is-in");
        }
    }

    function queuePanel() {
        const next = L.upcoming();
        if (!next.length) return h("p", { class: "k-lyrics-wait" }, "Очередь закончится на этом треке.");
        return h("div", { class: "k-setlist is-queue" }, next.slice(0, 80).map(({ track, index }, i) =>
            h("div", { class: "k-row" },
                h("button", { class: "k-row-main", type: "button", onclick: () => L.playAt(index) },
                    h("span", { class: "k-row-num" }, String(i + 1).padStart(2, "0")),
                    h("span", { class: "k-row-text" }, h("span", { class: "k-row-title" }, track.title), h("span", { class: "k-row-artist" }, track.artist || "")),
                    h("span", { class: "k-row-time" }, L.fmtTime(track.duration))))));
    }

    function onTime() {
        const pos = L.player.audio.currentTime || 0;
        const dur = L.duration();
        const frac = dur ? pos / dur : 0;
        if (els.line) els.line.style.setProperty("--p", String(frac));
        if (els.lineTime) els.lineTime.textContent = L.fmtTime(pos);
        if (els.nowRange && els.nowRange.isConnected && !els.nowRange.dataset.drag) {
            els.nowRange.value = String(Math.round(frac * 1000));
            els.nowRange.style.setProperty("--p", String(frac));
            els.nowT0.textContent = L.fmtTime(pos);
            els.nowT1.textContent = L.fmtTime(dur);
        }
        updateKaraoke(pos);
    }

    function onTrack() {
        const t = L.current();
        els.line.hidden = !t;
        els.app.classList.toggle("has-track", Boolean(t));
        if (!t) return;
        clear(els.lineText, h("b", null, t.title), h("span", null, "  " + (t.artist || "")));
        fitMarquee();
        markPlaying();
        if (L.ui.now) renderNow();
    }

    function onState() {
        const paused = L.player.audio.paused;
        for (const b of [els.linePlay, els.nowPlay]) {
            if (!b) continue;
            clear(b, icon(paused ? "play" : "pause"));
            b.setAttribute("aria-label", paused ? "Играть" : "Пауза");
        }
        els.app.classList.toggle("is-paused", paused);
        els.app.classList.toggle("is-loading", L.player.loading);
        if (els.nowShuf && els.nowShuf.isConnected) {
            els.nowShuf.classList.toggle("is-on", L.player.shuffle);
            els.nowShuf.setAttribute("aria-pressed", L.player.shuffle ? "true" : "false");
            els.nowRep.classList.toggle("is-on", L.player.repeat !== "off");
            els.nowRep.textContent = repeatLabel();
        }
    }

    // ------------------------------------------------------------------
    // Меню действий
    // ------------------------------------------------------------------

    function openMenu(track, event) {
        closeMenu();
        const list = h("div", { class: "k-menu-list", role: "menu" }, L.trackActions(track).map((a) =>
            h("button", { class: "k-menu-item" + (a.danger ? " is-danger" : ""), type: "button", role: "menuitem", onclick: () => { closeMenu(); a.run(); } },
                icon(a.icon), h("span", null, a.label))));
        const menu = h("div", { class: "k-menu" + (wide ? " is-pop" : " is-sheet") },
            h("div", { class: "k-menu-head" }, h("span", { class: "k-menu-title" }, track.title), h("span", { class: "k-menu-sub" }, track.artist || "")),
            list,
            wide ? null : h("button", { class: "k-menu-cancel", type: "button", onclick: closeMenu }, "Отменить"));
        const layer = h("div", { class: "k-menu-layer" }, h("div", { class: "k-scrim", onclick: closeMenu }), menu);
        els.menu = layer;
        els.app.appendChild(layer);
        if (wide && event) {
            const rect = menu.getBoundingClientRect();
            menu.style.left = Math.max(12, Math.min(event.clientX, innerWidth - rect.width - 12)) + "px";
            menu.style.top = Math.max(12, Math.min(event.clientY, innerHeight - rect.height - 12)) + "px";
        }
        requestAnimationFrame(() => layer.classList.add("is-open"));
        const first = list.querySelector("button");
        if (first) first.focus({ preventScroll: true });
    }

    function closeMenu() {
        if (!els.menu) return;
        const layer = els.menu;
        els.menu = null;
        layer.classList.remove("is-open");
        setTimeout(() => layer.remove(), 180);
    }

    function onKey(e) {
        if (e.key !== "Escape") return;
        if (els.menu) closeMenu();
        else if (els.index && els.index.classList.contains("is-open")) setIndex(false);
        else if (L.ui.now) L.closeNow();
    }

    // ------------------------------------------------------------------

    function mount(c) {
        ctx = c;
        const mq = matchMedia("(min-width: 900px)");
        wide = mq.matches;
        const onMq = () => { wide = mq.matches; renderRoute(); if (L.ui.now) renderNow(); fitMarquee(); };
        mq.addEventListener("change", onMq);
        ctx.cleanup(() => mq.removeEventListener("change", onMq));
        document.addEventListener("keydown", onKey);
        ctx.cleanup(() => document.removeEventListener("keydown", onKey));
        const onResize = () => fitMarquee();
        window.addEventListener("resize", onResize);
        ctx.cleanup(() => window.removeEventListener("resize", onResize));
        ctx.cleanup(() => document.documentElement.classList.remove("k-locked"));

        els.view = h("div", { class: "k-view" });
        els.follow = h("div", { class: "k-follow", "aria-hidden": "true" });
        els.app = h("div", { class: "k-app" });
        clear(els.app, buildTop(), buildIndex(), h("main", { class: "k-main" }, els.view), buildLine(), buildNow(), els.follow);
        clear(ctx.root, els.app);

        ctx.on("data", renderRoute);
        ctx.on("route", renderRoute);
        ctx.on("track", onTrack);
        ctx.on("state", onState);
        ctx.on("time", onTime);
        ctx.on("now", renderNow);
        ctx.on("queue", () => { if (L.ui.now && L.ui.nowTab === "queue") renderNow(); });
        renderRoute();
        onTrack();
        onState();
        renderNow();
    }

    function unmount() {
        if (viewCleanup) { viewCleanup(); viewCleanup = null; }
        for (const key of Object.keys(els)) delete els[key];
    }

    L.register({ id: "v4", name: "Плакат", themeColor: "#0c0f2e", mount, unmount });
})();
