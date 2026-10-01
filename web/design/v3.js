/* Вариант 3 — «Лента».
 *
 * Ось: открытие нового. Главная — лента полок, которые листаются вбок, и
 * строка настроений-фишек над ней: выбрал настроение — лента стала им.
 * Сверху мягкое свечение цвета первой обложки. Красный — только про то,
 * что играет и где вы: прогресс, метка раздела, столбики у трека.
 * Главные кнопки — белые «пилюли». Опора — YouTube Music. */
(function () {
    "use strict";
    const L = window.Lab;
    const { h, icon, clear } = L;

    let ctx = null;
    const els = {};
    let viewCleanup = null;
    let wide = false;
    let homeMood = "all";          // фишка над лентой: all | ключ настроения
    let relatedOpen = false;       // своя вкладка «Похожие» в «сейчас играет»
    let librarySort = "new";
    let searchQuery = "";

    // ------------------------------------------------------------------
    // Мелкие части
    // ------------------------------------------------------------------

    function btn(cls, iconName, label, onclick, extra) {
        return h("button", Object.assign({ class: cls, type: "button", "aria-label": label, title: label, onclick }, extra || {}),
            iconName ? icon(iconName) : null);
    }

    function pill(label, iconName, onclick, cls) {
        return h("button", { class: "y-pill " + (cls || ""), type: "button", onclick },
            iconName ? icon(iconName) : null, h("span", null, label));
    }

    function bars() {
        return h("span", { class: "y-bars", "aria-hidden": "true" }, h("i"), h("i"), h("i"));
    }

    function tracksWord(n) { return L.plural(n, ["трек", "трека", "треков"]); }

    function isCurrent(track) {
        const c = L.current();
        return Boolean(c && track && c.path === track.path);
    }

    /* Свечение сверху — цвет обложки, которая сейчас «главная» на экране. */
    function setGlow(path) {
        if (!els.app) return;
        if (!path) { els.app.style.setProperty("--glow", "rgba(80, 80, 90, 0.35)"); return; }
        L.coverColor(path).then((rgb) => {
            if (!els.app) return;
            if (!rgb) { els.app.style.setProperty("--glow", "rgba(80, 80, 90, 0.35)"); return; }
            const [r, g, b] = rgb;
            els.app.style.setProperty("--glow", `rgba(${r}, ${g}, ${b}, 0.62)`);
        });
    }

    function trackRow(track, list, i, opts) {
        opts = opts || {};
        return h("div", { class: "y-row" + (isCurrent(track) ? " is-playing" : "") + (track.missing ? " is-missing" : ""), dataset: { path: track.path } },
            h("button", {
                class: "y-row-main", type: "button",
                onclick: () => L.play(list, i, { source: opts.source || "" }),
            },
                h("span", { class: "y-row-art" }, L.cover(track.path, 48), bars(), h("span", { class: "y-row-hover" }, icon("play"))),
                h("span", { class: "y-row-text" },
                    h("span", { class: "y-row-title" }, track.title),
                    h("span", { class: "y-row-sub" },
                        h("span", null, track.artist || ""),
                        opts.album && track.album && track.album !== track.title ? h("span", { class: "y-row-album" }, track.album) : null)),
                h("span", { class: "y-row-time" }, L.fmtTime(track.duration))),
            btn("y-icon y-row-more", "more", "Действия с треком", (e) => openMenu(track, e)));
    }

    function markPlaying() {
        const c = L.current();
        for (const row of els.app.querySelectorAll(".y-row, .y-card")) {
            if (!row.dataset.path) continue;
            row.classList.toggle("is-playing", Boolean(c && row.dataset.path === c.path));
        }
    }

    /* Полка со стрелками ‹ › в заголовке (стрелки — только на компьютере). */
    function section(title, opts, body) {
        opts = opts || {};
        const head = h("div", { class: "y-section-head" },
            h("div", { class: "y-section-titles" },
                h("h2", null, title),
                opts.note ? h("p", { class: "y-section-note" }, opts.note) : null));
        const tools = h("div", { class: "y-section-tools" });
        if (opts.more) tools.appendChild(h("button", { class: "y-more-link", type: "button", onclick: opts.more.run }, opts.more.label));
        if (opts.scroller) {
            const scroll = (dir) => opts.scroller.scrollBy({ left: dir * opts.scroller.clientWidth * 0.85, behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
            tools.appendChild(btn("y-icon y-arrow", "back", "Назад по полке", () => scroll(-1)));
            tools.appendChild(btn("y-icon y-arrow is-next", "back", "Дальше по полке", () => scroll(1)));
        }
        head.appendChild(tools);
        return h("section", { class: "y-section" + (opts.cls ? " " + opts.cls : "") }, head, body);
    }

    function shelf(items, cls) {
        return h("div", { class: "y-shelf " + (cls || "") }, items);
    }

    function card(path, title, sub, onclick, opts) {
        opts = opts || {};
        const coverEl = opts.coverEl || L.cover(path, opts.px || 180, opts.round ? { class: "cover is-round" } : null);
        return h("div", { class: "y-card" + (opts.round ? " is-round" : "") + (opts.cls ? " " + opts.cls : ""), dataset: opts.trackPath ? { path: opts.trackPath } : {} },
            h("button", { class: "y-card-open", type: "button", onclick, "aria-label": title },
                h("span", { class: "y-card-art" }, coverEl, opts.onPlay ? null : null),
                h("span", { class: "y-card-title" }, title),
                sub ? h("span", { class: "y-card-sub" }, sub) : null),
            opts.onPlay ? btn("y-card-play", "play", "Слушать «" + title + "»", opts.onPlay) : null);
    }

    function empty(text, actionLabel, run) {
        return h("div", { class: "y-empty" }, h("p", null, text),
            actionLabel ? pill(actionLabel, null, run, "is-white") : null);
    }

    function skeleton(n, cls) {
        return h("div", { class: "y-skeletons" }, Array.from({ length: n || 3 }, () => h("div", { class: "y-skeleton " + (cls || "") })));
    }

    function pageTitle(text, trailing) {
        return h("header", { class: "y-page-title" }, h("h1", null, text), trailing || null);
    }

    // ------------------------------------------------------------------
    // Фишки настроений
    // ------------------------------------------------------------------

    function moodChips() {
        const moods = (L.data.home && L.data.home.moods) || [];
        const chips = [["all", "Всё"], ...moods.map((m) => [m.key, m.name])];
        return h("div", { class: "y-chips", role: "tablist", "aria-label": "Настроение ленты" }, chips.map(([key, label]) =>
            h("button", {
                class: "y-chip" + (homeMood === key ? " is-on" : ""), type: "button", role: "tab",
                "aria-selected": homeMood === key ? "true" : "false",
                onclick: () => { homeMood = key; renderRoute(true); },
            }, label)));
    }

    // ------------------------------------------------------------------
    // Экраны
    // ------------------------------------------------------------------

    /* Быстрый выбор: четыре строки, листается столбцами. */
    function quickPicks(tracks, source) {
        const grid = h("div", { class: "y-quick" }, tracks.map((t, i) => trackRow(t, tracks, i, { source })));
        return { grid, el: section("Быстрый выбор", { scroller: grid, more: { label: "Слушать всё", run: () => L.play(tracks, 0, { source }) } }, grid) };
    }

    function viewHome() {
        const d = L.data;
        const home = d.home || { moods: [], discover: { tracks: [] }, albums: [] };

        if (homeMood !== "all") {
            const m = home.moods.find((x) => x.key === homeMood);
            if (m) {
                const tracks = m.tracks.map((t) => d.byPath.get(t.path) || t);
                setGlow(tracks[0] && tracks[0].path);
                const picks = quickPicks(tracks.slice(0, 20), m.name);
                const rest = tracks.slice(4, 24);
                const moreShelf = shelf(rest.map((t, i) => card(t.path, t.title, t.artist, () => L.play(rest, i, { source: m.name }), { trackPath: t.path })), "is-big");
                return h("div", { class: "y-page y-home" },
                    moodChips(),
                    h("header", { class: "y-mood-head" },
                        h("h1", null, m.name),
                        h("p", null, m.hint + ", " + tracksWord(tracks.length)),
                        h("div", { class: "y-pills" },
                            pill("Слушать", "play", () => L.play(tracks, 0, { shuffle: false, source: m.name }), "is-white"),
                            pill("Перемешать", "shuffle", () => L.play(tracks, -1, { shuffle: true, source: m.name })))),
                    picks.el,
                    section("Ещё под это настроение", { scroller: moreShelf }, moreShelf));
            }
            homeMood = "all";
        }

        const first = d.tracks[0];
        setGlow(first && first.path);

        const disc = (home.discover && home.discover.tracks) || [];
        const based = (home.discover && home.discover.based_on) || [];
        const quickSource = disc.length >= 8 ? disc : d.tracks.slice(0, 24);
        const picks = quickPicks(quickSource.slice(0, 20), disc.length >= 8 ? "Быстрый выбор" : "Недавно добавлено");

        const recent = d.tracks.slice(0, 16);
        const again = shelf(recent.map((t, i) => card(t.path, t.title, t.artist,
            () => L.play(recent, i, { source: "Недавно добавлено" }), { trackPath: t.path })), "is-big is-two-rows");

        const moodCards = shelf(home.moods.map((m) => card(m.tracks[0] && m.tracks[0].path, m.name, m.hint,
            () => { homeMood = m.key; renderRoute(true); }, { onPlay: () => L.play(m.tracks, 0, { source: m.name }) })), "is-big");

        const albums = shelf(home.albums.map((a) => card(a.cover, a.album, a.artist + ", " + tracksWord(a.count),
            () => L.go("album", a.artist, a.album))));

        const topArtists = [...d.artists].sort((x, y) => y.tracks.length - x.tracks.length).slice(0, 16);
        const artists = shelf(topArtists.map((a) => card(a.cover, a.name, tracksWord(a.tracks.length),
            () => L.go("artist", a.name), { round: true })), "is-round");

        const discShelf = disc.length >= 8 ? shelf(disc.slice(8, 24).map((t, i, list) => card(t.path, t.title, t.artist,
            () => L.play(list, i, { source: "Похоже на любимое" }), { trackPath: t.path }))) : null;

        return h("div", { class: "y-page y-home" },
            moodChips(),
            picks.el,
            section("Недавно добавлено", { scroller: again, more: { label: "Все", run: () => L.go("library", { tab: "tracks" }) } }, again),
            section("Под настроение", { scroller: moodCards }, moodCards),
            discShelf ? section("Похоже на любимое", { note: based.length ? "Для тех, кто слушает " + based.slice(0, 2).join(" и ") : null, scroller: discShelf }, discShelf) : null,
            home.albums.length ? section("Альбомы", { scroller: albums, more: { label: "Все", run: () => L.go("library", { tab: "albums" }) } }, albums) : null,
            section("Артисты", { scroller: artists, more: { label: "Все", run: () => L.go("library", { tab: "artists" }) } }, artists));
    }

    function libraryChips(active) {
        return h("div", { class: "y-chips is-static", role: "tablist" },
            [["tracks", "Треки"], ["artists", "Артисты"], ["albums", "Альбомы"], ["playlists", "Подборки"]].map(([key, label]) =>
                h("button", {
                    class: "y-chip" + (active === key ? " is-on" : ""), type: "button", role: "tab",
                    "aria-selected": active === key ? "true" : "false",
                    onclick: () => (key === "playlists" ? L.go("playlists") : L.go("library", { tab: key })),
                }, label)));
    }

    function viewLibrary(r) {
        const tab = r.query.tab || "tracks";
        setGlow(null);
        let body;
        if (tab === "artists") {
            body = h("div", { class: "y-grid is-round" }, L.data.artists.map((a) =>
                card(a.cover, a.name, tracksWord(a.tracks.length), () => L.go("artist", a.name), { round: true, px: 160 })));
        } else if (tab === "albums") {
            const albums = L.data.albums.filter((a) => a.tracks.length > 1 || L.data.albums.length < 40);
            body = h("div", { class: "y-grid" }, albums.map((a) =>
                card(a.cover, a.album, a.artist, () => L.go("album", a.artist, a.album), { px: 220 })));
        } else {
            const list = librarySort === "name"
                ? [...L.data.tracks].sort((x, y) => x.title.localeCompare(y.title, "ru", { sensitivity: "base" }))
                : L.data.tracks;
            body = h("div", null,
                h("div", { class: "y-toolbar" },
                    h("div", { class: "y-pills" },
                        pill("Перемешать", "shuffle", () => L.play(list, -1, { shuffle: true, source: "Фонотека" }), "is-white"),
                        pill("Слушать", "play", () => L.play(list, 0, { shuffle: false, source: "Фонотека" }))),
                    h("button", {
                        class: "y-sort", type: "button",
                        onclick: () => { librarySort = librarySort === "new" ? "name" : "new"; renderRoute(true); },
                    }, icon("sort"), h("span", null, librarySort === "new" ? "Сначала новые" : "По названию"))),
                h("p", { class: "y-count" }, tracksWord(list.length)),
                h("div", { class: "y-list" }, list.map((t, i) => trackRow(t, list, i, { source: "Фонотека", album: true }))));
        }
        return h("div", { class: "y-page" }, pageTitle("Фонотека"), libraryChips(tab), body);
    }

    function viewPlaylists() {
        setGlow(null);
        const pls = L.data.playlists;
        return h("div", { class: "y-page" },
            pageTitle("Фонотека"), libraryChips("playlists"),
            pls.length ? h("div", { class: "y-grid" },
                h("div", { class: "y-card is-new" },
                    h("button", { class: "y-card-open", type: "button", onclick: () => L.act("Новая подборка") },
                        h("span", { class: "y-card-art y-new-art" }, icon("add")),
                        h("span", { class: "y-card-title" }, "Новая подборка"))),
                pls.map((p) => card(null, p.name, tracksWord(p.tracks), () => L.go("playlist", p.name), { coverEl: L.playlistCover(p, 220) })))
                : empty("Подборок пока нет. Соберите первую из треков фонотеки.", "Новая подборка", () => L.act("Новая подборка")));
    }

    /* Страница коллекции: альбом, подборка, настроение. На компьютере —
     * обложка с кнопками слева, список справа. */
    function collectionPage({ coverEl, coverPath, title, subtitle, onSubtitle, meta, tracks, source, extraMenu }) {
        setGlow(coverPath || (tracks[0] && tracks[0].path));
        const total = tracks.reduce((s, t) => s + (t.duration || 0), 0);
        const moreBtn = btn("y-icon y-col-more", "more", "Ещё", (e) => {
            if (extraMenu) openActions(extraMenu, e, title); else L.act("Действия с коллекцией");
        });
        return h("div", { class: "y-page y-collection" },
            h("header", { class: "y-col-head" },
                h("button", { class: "y-back", type: "button", onclick: () => history.back(), "aria-label": "Назад" }, icon("back")),
                h("div", { class: "y-col-art" }, coverEl),
                h("div", { class: "y-col-info" },
                    h("h1", null, title),
                    subtitle ? (onSubtitle
                        ? h("button", { class: "y-col-sub is-link", type: "button", onclick: onSubtitle }, subtitle)
                        : h("p", { class: "y-col-sub" }, subtitle)) : null,
                    h("p", { class: "y-col-meta" }, meta || (tracksWord(tracks.length) + ", " + L.fmtTotal(total))),
                    h("div", { class: "y-pills" },
                        pill("Слушать", "play", () => L.play(tracks, 0, { shuffle: false, source }), "is-white"),
                        pill("Перемешать", "shuffle", () => L.play(tracks, -1, { shuffle: true, source })),
                        moreBtn))),
            h("div", { class: "y-list y-col-list" }, tracks.map((t, i) => trackRow(t, tracks, i, { source }))));
    }

    function viewAlbum(r) {
        const [artist, album] = r.params;
        const al = L.data.albumByKey.get(L.albumKey(artist, album));
        if (!al) return h("div", { class: "y-page" }, empty("Такого альбома в фонотеке нет.", "На главную", () => L.go("home")));
        return collectionPage({
            coverEl: L.cover(al.cover, 300), coverPath: al.cover, title: al.album, subtitle: al.artist,
            onSubtitle: () => L.go("artist", al.artist), tracks: al.tracks, source: al.album,
        });
    }

    function viewMood(r) {
        const m = L.data.home && L.data.home.moods.find((x) => x.key === r.params[0]);
        if (!m) return h("div", { class: "y-page" }, empty("Подборка по настроению обновилась — откройте главную.", "На главную", () => L.go("home")));
        const tracks = m.tracks.map((t) => L.data.byPath.get(t.path) || t);
        return collectionPage({
            coverEl: L.cover(m.tracks[0] && m.tracks[0].path, 300), title: m.name, subtitle: m.hint, tracks, source: m.name,
        });
    }

    function viewPlaylist(r) {
        const name = r.params[0];
        const meta = L.data.playlists.find((p) => p.name === name) || { name };
        const holder = h("div", { class: "y-page" }, skeleton(4));
        L.playlistTracks(name).then((pl) => {
            if (!holder.isConnected) return;
            const total = pl.tracks.reduce((s, t) => s + (t.duration || 0), 0);
            holder.replaceWith(collectionPage({
                coverEl: L.playlistCover(meta, 300), coverPath: pl.tracks[0] && pl.tracks[0].path, title: name, subtitle: "Подборка",
                meta: tracksWord(pl.tracks.length) + ", " + L.fmtTotal(total) + (meta.updated_at ? ", изменена " + L.agoText(meta.updated_at) : ""),
                tracks: pl.tracks, source: name,
                extraMenu: [
                    { label: "Изменить подборку", icon: "list", run: () => L.act("Изменить подборку") },
                    { label: "Сменить обложку", icon: "grid", run: () => L.act("Сменить обложку") },
                    { label: "Удалить подборку", icon: "trash", danger: true, run: () => L.act("Удалить подборку") },
                ],
            }));
        }).catch((e) => clear(holder, empty(e.message)));
        return holder;
    }

    function viewArtist(r) {
        const a = L.data.artistByName.get(r.params[0]);
        if (!a) return h("div", { class: "y-page" }, empty("Такого артиста в фонотеке нет.", "На главную", () => L.go("home")));
        setGlow(a.cover);
        const albums = L.data.albums.filter((al) => al.artist === a.name);
        let showAll = false;
        const listBox = h("div", { class: "y-list" });
        const toggleBtn = h("button", { class: "y-more-link", type: "button" });
        const fill = () => {
            const shown = showAll ? a.tracks : a.tracks.slice(0, 5);
            clear(listBox, shown.map((t, i) => trackRow(t, a.tracks, i, { source: a.name, album: true })));
            toggleBtn.textContent = showAll ? "Свернуть" : "Все " + tracksWord(a.tracks.length);
            toggleBtn.hidden = a.tracks.length <= 5;
        };
        toggleBtn.addEventListener("click", () => { showAll = !showAll; fill(); });
        fill();
        const albumShelf = albums.length ? shelf(albums.map((al) => card(al.cover, al.album, tracksWord(al.tracks.length),
            () => L.go("album", al.artist, al.album)))) : null;
        return h("div", { class: "y-page y-artist" },
            h("header", { class: "y-banner" },
                h("div", { class: "y-banner-art" }, L.cover(a.cover, 600)),
                h("div", { class: "y-banner-shade" }),
                h("button", { class: "y-back is-float", type: "button", onclick: () => history.back(), "aria-label": "Назад" }, icon("back")),
                h("div", { class: "y-banner-text" },
                    h("h1", null, a.name),
                    h("p", null, tracksWord(a.tracks.length) + (a.albums.size ? ", " + L.plural(a.albums.size, ["альбом", "альбома", "альбомов"]) : "")),
                    h("div", { class: "y-pills" },
                        pill("Перемешать", "shuffle", () => L.play(a.tracks, -1, { shuffle: true, source: a.name }), "is-white"),
                        pill("Слушать", "play", () => L.play(a.tracks, 0, { shuffle: false, source: a.name }))))),
            h("section", { class: "y-section" },
                h("div", { class: "y-section-head" }, h("div", { class: "y-section-titles" }, h("h2", null, "Треки")), h("div", { class: "y-section-tools" }, toggleBtn)),
                listBox),
            albumShelf ? section("Альбомы", { scroller: albumShelf }, albumShelf) : null);
    }

    /* Поиск без запроса — это «Обзор»: настроения, артисты, свежие альбомы. */
    function viewSearch(r) {
        searchQuery = r.query.q !== undefined ? r.query.q : searchQuery;
        setGlow(null);
        const results = h("div", { class: "y-results" });

        function fill() {
            const q = searchQuery.trim();
            if (!q) {
                const moods = (L.data.home ? L.data.home.moods : []);
                const topArtists = [...L.data.artists].sort((x, y) => y.tracks.length - x.tracks.length).slice(0, 24);
                const newAlbums = L.data.albums.filter((a) => a.tracks.length > 1).slice(0, 18);
                const artistShelf = shelf(topArtists.map((a) => card(a.cover, a.name, tracksWord(a.tracks.length), () => L.go("artist", a.name), { round: true })), "is-round");
                const albumShelf = shelf(newAlbums.map((a) => card(a.cover, a.album, a.artist, () => L.go("album", a.artist, a.album))));
                clear(results,
                    section("Настроения", null, h("div", { class: "y-tiles" }, moods.map((m) =>
                        h("button", {
                            class: "y-tile", type: "button", onclick: () => L.go("mood", m.key),
                            style: { "--tile-hue": String(L.hashHue(m.key)) },
                        }, h("span", null, m.name))))),
                    section("Артисты", { scroller: artistShelf, more: { label: "Все", run: () => L.go("library", { tab: "artists" }) } }, artistShelf),
                    section("Новые альбомы", { scroller: albumShelf, more: { label: "Все", run: () => L.go("library", { tab: "albums" }) } }, albumShelf));
                return;
            }
            const found = L.searchLocal(q, 60);
            const yt = h("div", { class: "y-yt" },
                h("button", { class: "y-yt-ask", type: "button", onclick: () => askYouTube(q, yt) },
                    icon("search"), h("span", null, "Искать «" + q + "» на YouTube")));
            if (!found.tracks.length && !found.artists.length && !found.albums.length) {
                clear(results, empty("В фонотеке ничего не нашлось по «" + q + "»."), yt);
                return;
            }
            const top = found.artists[0]
                ? h("button", { class: "y-best", type: "button", onclick: () => L.go("artist", found.artists[0].name) },
                    L.cover(found.artists[0].cover, 120, { class: "cover is-round" }),
                    h("span", { class: "y-best-text" }, h("span", { class: "y-best-title" }, found.artists[0].name),
                        h("span", { class: "y-row-sub" }, "Артист, " + tracksWord(found.artists[0].tracks.length))))
                : null;
            clear(results,
                top ? section("Лучшее совпадение", null, top) : null,
                found.tracks.length ? section("Треки", null, h("div", { class: "y-list" }, found.tracks.slice(0, 20).map((t, i) => trackRow(t, found.tracks, i, { source: "Поиск", album: true })))) : null,
                found.albums.length ? section("Альбомы", null, shelf(found.albums.map((a) => card(a.cover, a.album, a.artist, () => L.go("album", a.artist, a.album))))) : null,
                found.artists.length > 1 ? section("Артисты", null, shelf(found.artists.map((a) => card(a.cover, a.name, null, () => L.go("artist", a.name), { round: true })), "is-round")) : null,
                yt);
        }

        fill();
        els.searchFill = fill;
        if (els.searchInput && els.searchInput.value !== searchQuery) els.searchInput.value = searchQuery;
        const field = wide ? null : searchField(true);
        if (field && !searchQuery) setTimeout(() => { const i = field.querySelector("input"); if (i && L.route.current.name === "search") i.focus({ preventScroll: true }); }, 60);
        return h("div", { class: "y-page" }, field, searchQuery.trim() ? null : pageTitle("Обзор"), results);
    }

    function searchField(inPage) {
        const input = h("input", {
            class: "y-search-input", type: "search", placeholder: "Поиск по фонотеке", value: searchQuery,
            "aria-label": "Поиск по фонотеке", autocomplete: "off", enterkeyhint: "search",
        });
        let timer = 0;
        input.addEventListener("input", () => {
            clearTimeout(timer);
            timer = setTimeout(() => {
                // Пока ждали, человек ушёл из поля (нажал результат) — не уводим его обратно.
                if (document.activeElement !== input) return;
                searchQuery = input.value;
                if (L.route.current.name === "search") {
                    L.replaceRoute("search", { q: searchQuery });
                    if (els.searchFill) els.searchFill();
                } else {
                    L.go("search", { q: searchQuery });
                }
            }, 140);
        });
        input.addEventListener("keydown", (e) => { if (e.key === "Enter" && L.route.current.name !== "search") L.go("search", { q: input.value }); });
        if (!inPage) els.searchInput = input;
        return h("label", { class: "y-search" + (inPage ? " is-page" : "") }, icon("search"), input);
    }

    function askYouTube(q, box) {
        clear(box, skeleton(2));
        L.searchYouTube(q).then((body) => {
            const list = body.results || [];
            clear(box, section("На YouTube", null, list.length ? h("div", { class: "y-list" }, list.map((v) =>
                h("div", { class: "y-row" },
                    h("div", { class: "y-row-main is-static" },
                        h("span", { class: "y-row-art is-video" }, icon("track")),
                        h("span", { class: "y-row-text" },
                            h("span", { class: "y-row-title" }, v.title || ""),
                            h("span", { class: "y-row-sub" }, h("span", null, v.channel || v.uploader || ""), v.duration ? h("span", null, L.fmtTime(v.duration)) : null))),
                    pill("Скачать", "download", () => L.act("Скачать в фонотеку"), "is-small"))))
                : empty("YouTube ничего не нашёл.")));
        }).catch((e) => clear(box, empty("Поиск на YouTube не ответил: " + e.message)));
    }

    const STATUS = {
        done: ["Готово", "is-done"], failed: ["Не получилось", "is-failed"], error: ["Не получилось", "is-failed"],
        processing: ["Скачивается", "is-work"], downloading: ["Скачивается", "is-work"], tagging: ["Подписываем теги", "is-work"], queued: ["В очереди", "is-wait"],
        pending: ["В очереди", "is-wait"], skipped: ["Уже есть", "is-wait"],
    };

    function viewAdd() {
        setGlow(null);
        const input = h("input", {
            class: "y-link-input", type: "url", placeholder: "Ссылка на трек или плейлист",
            "aria-label": "Ссылка для скачивания", autocomplete: "off", inputmode: "url",
        });
        const feed = h("div", { class: "y-feed" }, skeleton(4, "is-task"));
        function loadTasks() {
            L.tasks().then((tasks) => {
                if (!feed.isConnected) return;
                clear(feed, tasks.length ? tasks.slice(0, 30).map((t) => {
                    const [label, cls] = STATUS[t.status] || [t.status, "is-wait"];
                    const path = t.result_path && L.data.byPath.get(t.result_path) ? t.result_path : null;
                    return h("article", { class: "y-task" },
                        path ? L.cover(path, 64) : h("div", { class: "y-task-art" }, icon(cls === "is-failed" ? "warn" : "download")),
                        h("div", { class: "y-task-text" },
                            h("span", { class: "y-row-title" }, t.title || t.url),
                            h("span", { class: "y-row-sub" }, h("span", null, t.artist || ""), h("span", null, (t.updated_at || "").slice(5, 16))),
                            t.error ? h("span", { class: "y-task-error" }, t.error) : null),
                        h("span", { class: "y-status " + cls }, cls === "is-work" ? h("i", { class: "y-spin", "aria-hidden": "true" }) : null, label),
                        path ? btn("y-icon", "play", "Слушать", () => L.play([L.data.byPath.get(path)], 0, { source: "Загрузки" })) : null);
                }) : empty("Загрузок пока нет. Вставьте ссылку выше."));
            }).catch((e) => clear(feed, empty(e.message)));
        }
        loadTasks();
        const timer = setInterval(loadTasks, 5000);
        viewCleanup = () => clearInterval(timer);
        return h("div", { class: "y-page y-add" },
            pageTitle("Загрузки"),
            h("div", { class: "y-add-box" },
                h("div", { class: "y-link" }, icon("link"), input),
                h("div", { class: "y-pills" },
                    pill("Скачать", "download", () => L.act("Скачать"), "is-white"),
                    pill("Загрузить файлы", "add", () => L.act("Загрузить файлы")))),
            section("Лента загрузок", { more: { label: "Состояние службы", run: () => L.go("service") } }, feed));
    }

    function viewService() {
        setGlow(null);
        const box = h("div", { class: "y-stats" }, skeleton(3));
        L.health().then((s) => {
            const tiles = [
                ["Служба", s.status === "healthy" ? "Работает" : s.status, s.status === "healthy"],
                ["Треков", String(s.tracks)], ["Альбомов", String(s.albums)], ["Подборок", String(s.playlists)],
                ["В очереди загрузки", String(s.queue_size)], ["Navidrome", s.navidrome === "configured" ? "Подключён" : s.navidrome],
                ["Ждут синхронизации", String(s.navidrome_pending)], ["Прослушиваний", String(s.plays_logged)],
                ["ffmpeg", s.ffmpeg], ["База", s.database],
            ];
            clear(box, tiles.map(([k, v, ok]) => h("div", { class: "y-stat" + (ok ? " is-ok" : "") }, h("span", { class: "y-stat-k" }, k), h("span", { class: "y-stat-v" }, v))));
        }).catch((e) => clear(box, empty(e.message)));
        return h("div", { class: "y-page" }, pageTitle("Служба"), box,
            h("div", { class: "y-pills y-pad" },
                pill("Проверить фонотеку", "service", () => L.act("Проверить фонотеку"), "is-white"),
                pill("Синхронизировать", "repeat", () => L.act("Синхронизировать с Navidrome"))));
    }

    const VIEWS = {
        home: viewHome, library: viewLibrary, artist: viewArtist, album: viewAlbum, mood: viewMood,
        playlists: viewPlaylists, playlist: viewPlaylist, search: viewSearch, add: viewAdd, service: viewService,
    };

    function renderRoute(keepScroll) {
        if (viewCleanup) { viewCleanup(); viewCleanup = null; }
        const r = L.route.current;
        updateNav(r);
        if (els.searchInput && r.name !== "search" && document.activeElement !== els.searchInput) els.searchInput.value = "";
        if (!L.data.ready) {
            clear(els.view, L.data.error
                ? h("div", { class: "y-page" }, empty(L.data.error.message, L.data.error.auth ? "Открыть плеер" : "Повторить",
                    () => { if (L.data.error.auth) location.href = "/"; else L.load(); }))
                : h("div", { class: "y-page" }, h("div", { class: "y-skeleton is-chips" }), skeleton(5)));
            return;
        }
        const view = VIEWS[r.name] || VIEWS.home;
        const y = window.scrollY;
        els.view.replaceChildren(view(r));
        window.scrollTo(0, keepScroll === true ? y : 0);
    }

    // ------------------------------------------------------------------
    // Навигация
    // ------------------------------------------------------------------

    const NAV = [
        ["home", "Главная", "home", ["home", "mood"]],
        ["search", "Обзор", "radio", ["search"]],
        ["library", "Фонотека", "library", ["library", "artist", "album", "playlists", "playlist"]],
        ["add", "Загрузки", "download", ["add", "service"]],
    ];
    const RAIL = [
        ["home", "Главная", "home", ["home", "mood"]],
        ["search", "Обзор", "radio", ["search"]],
        ["library", "Фонотека", "library", ["library", "artist", "album"]],
        ["playlists", "Подборки", "playlists", ["playlists", "playlist"]],
        ["add", "Загрузки", "download", ["add"]],
        ["service", "Служба", "service", ["service"]],
    ];

    function updateNav(r) {
        for (const b of els.app.querySelectorAll("[data-nav]")) {
            const on = b.dataset.nav.split(",").includes(r.name);
            b.classList.toggle("is-on", on);
            if (on) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
        }
    }

    function navButton(cls, [route, label, iconName, match]) {
        return h("button", { class: cls, type: "button", dataset: { nav: match.join(",") }, onclick: () => { if (route === "home") homeMood = "all"; L.go(route); } },
            icon(iconName), h("span", null, label));
    }

    function wordmark() {
        return h("button", { class: "y-mark", type: "button", onclick: () => { homeMood = "all"; L.go("home"); }, "aria-label": "Фонотека — на главную" },
            h("span", { class: "y-mark-dot", "aria-hidden": "true" }, icon("play")), h("span", null, "Фонотека"));
    }

    function buildTop() {
        return h("header", { class: "y-top" },
            wordmark(),
            h("div", { class: "y-top-search" }, searchField(false)),
            h("div", { class: "y-top-tools" },
                btn("y-icon y-top-find", "search", "Поиск", () => L.go("search", { q: "" })),
                btn("y-icon", "add", "Добавить музыку", () => L.go("add"))));
    }

    function buildRail() {
        return h("nav", { class: "y-rail", "aria-label": "Разделы" }, RAIL.map((item) => navButton("y-rail-item", item)));
    }

    // ------------------------------------------------------------------
    // Мини-плеер, полоса плеера
    // ------------------------------------------------------------------

    function buildDock() {
        const art = h("div", { class: "y-mini-art" });
        const title = h("span", { class: "y-mini-title" });
        const sub = h("span", { class: "y-mini-sub" });
        const play = btn("y-icon y-mini-play", "play", "Играть", () => L.toggle());
        const mini = h("div", { class: "y-mini", hidden: true },
            h("div", { class: "y-mini-line" }, h("i")),
            h("button", { class: "y-mini-open", type: "button", onclick: () => L.openNow(), "aria-label": "Открыть плеер" },
                art, h("span", { class: "y-mini-text" }, title, sub)),
            play, btn("y-icon", "next", "Следующий", () => L.next()));
        const nav = h("nav", { class: "y-nav", "aria-label": "Разделы" }, NAV.map((item) => navButton("y-nav-item", item)));
        Object.assign(els, { miniArt: art, miniTitle: title, miniSub: sub, miniPlay: play, mini });
        return h("div", { class: "y-dock" }, mini, nav);
    }

    function rangeInput(cls, onDragText) {
        const range = h("input", { class: "y-range " + cls, type: "range", min: "0", max: "1000", value: "0", step: "1", "aria-label": "Позиция в треке" });
        range.addEventListener("input", () => {
            range.dataset.drag = "1";
            range.style.setProperty("--p", String(range.value / 1000));
            if (onDragText) onDragText(L.fmtTime(range.value / 1000 * L.duration()));
        });
        range.addEventListener("change", () => { delete range.dataset.drag; L.seek(range.value / 1000 * L.duration()); });
        return range;
    }

    function buildBar() {
        const time = h("span", { class: "y-bar-time" }, "0:00 / 0:00");
        const range = rangeInput("y-bar-range", (t) => { time.textContent = t + " / " + L.fmtTime(L.duration()); });
        const play = btn("y-icon y-bar-play", "play", "Играть", () => L.toggle());
        const art = h("div", { class: "y-bar-art" });
        const title = h("span", { class: "y-bar-title" }, "Ничего не играет");
        const sub = h("span", { class: "y-bar-sub" }, "Выберите трек в ленте");
        const expand = btn("y-icon y-bar-expand", "down", "Развернуть плеер", () => (L.ui.now ? L.closeNow() : L.openNow()));
        const shuf = btn("y-icon y-toggle", "shuffle", "Перемешивать", () => L.setShuffle());
        const rep = btn("y-icon y-toggle", "repeat", "Повтор", () => L.cycleRepeat());
        const more = btn("y-icon", "more", "Действия с треком", (e) => { const t = L.current(); if (t) openMenu(t, e); });
        Object.assign(els, { barTime: time, barRange: range, barPlay: play, barArt: art, barTitle: title, barSub: sub, barExpand: expand, barShuf: shuf, barRep: rep, barMore: more });
        return h("footer", { class: "y-bar" },
            range,
            h("div", { class: "y-bar-left" },
                btn("y-icon", "prev", "Предыдущий", () => L.prev()), play, btn("y-icon", "next", "Следующий", () => L.next()), time),
            h("div", { class: "y-bar-mid" },
                h("button", { class: "y-bar-open", type: "button", onclick: () => L.openNow(), "aria-label": "Открыть плеер" }, art,
                    h("span", { class: "y-bar-text" }, title, sub)),
                more),
            h("div", { class: "y-bar-right" },
                btn("y-icon", "lyrics", "Текст", () => { relatedOpen = false; L.openNow("lyrics"); }),
                btn("y-icon", "queue", "Далее", () => { relatedOpen = false; L.openNow("queue"); }),
                shuf, rep, expand));
    }

    // ------------------------------------------------------------------
    // Сейчас играет
    // ------------------------------------------------------------------

    function nowTab() {
        if (L.ui.nowTab === "lyrics" || L.ui.nowTab === "queue") return L.ui.nowTab;
        return relatedOpen ? "related" : "cover";
    }

    function pickTab(tab) {
        if (tab === "related") {
            relatedOpen = true;
            if (L.ui.nowTab !== "cover") L.setNowTab("cover"); else renderNow();
        } else {
            relatedOpen = false;
            if (L.ui.nowTab !== tab) L.setNowTab(tab); else renderNow();
        }
    }

    function relatedTracks(t) {
        const out = [];
        const seen = new Set([t.path]);
        const push = (x) => { if (x && !seen.has(x.path)) { seen.add(x.path); out.push(x); } };
        const artist = L.data.artistByName.get(L.mainArtist(t));
        if (artist) artist.tracks.forEach(push);
        const moods = (L.data.home && L.data.home.moods) || [];
        for (const m of moods) {
            if (m.tracks.some((x) => x.path === t.path)) m.tracks.forEach((x) => push(L.data.byPath.get(x.path) || x));
        }
        if (out.length < 10) {
            const disc = (L.data.home && L.data.home.discover && L.data.home.discover.tracks) || [];
            disc.forEach((x) => push(L.data.byPath.get(x.path) || x));
        }
        return out.slice(0, 40);
    }

    function buildNow() {
        const sheet = h("div", { class: "y-now", role: "dialog", "aria-modal": "true", "aria-label": "Сейчас играет", hidden: true },
            h("div", { class: "y-now-bg" }), h("div", { class: "y-now-inner" }));
        els.now = sheet;
        els.nowBg = sheet.firstChild;
        els.nowInner = sheet.lastChild;
        return sheet;
    }

    function renderNow() {
        const t = L.current();
        const open = L.ui.now && Boolean(t);
        const sheet = els.now;
        if (els.barExpand) {
            els.barExpand.classList.toggle("is-open", open);
            els.barExpand.setAttribute("aria-label", open ? "Свернуть плеер" : "Развернуть плеер");
        }
        if (!open) {
            relatedOpen = false;
            if (!sheet.hidden) {
                sheet.classList.remove("is-open");
                document.documentElement.classList.remove("y-locked");
                setTimeout(() => { if (!L.ui.now) sheet.hidden = true; }, 320);
            }
            return;
        }
        const tab = nowTab();
        const t0 = h("span", null, "0:00");
        const t1 = h("span", null, "0:00");
        const range = rangeInput("y-now-range", (txt) => { t0.textContent = txt; });
        const play = btn("y-now-play", L.player.audio.paused ? "play" : "pause", "Играть или пауза", () => L.toggle());
        Object.assign(els, { nowRange: range, nowT0: t0, nowT1: t1, nowPlay: play });

        const stage = h("div", { class: "y-now-stage" },
            h("div", { class: "y-now-art" }, L.cover(t.path, 600)),
            h("div", { class: "y-now-meta" },
                h("div", { class: "y-now-text" },
                    h("h2", { class: "y-now-title" }, t.title),
                    h("button", { class: "y-now-artist", type: "button", onclick: () => { L.closeNow(); L.go("artist", L.mainArtist(t)); } }, t.artist || "")),
                btn("y-icon", "more", "Действия", (e) => openMenu(t, e))),
            h("div", { class: "y-now-progress" }, range, h("div", { class: "y-now-times" }, t0, t1)),
            h("div", { class: "y-now-controls" },
                btn("y-icon y-toggle" + (L.player.shuffle ? " is-on" : ""), "shuffle", "Перемешивать", () => L.setShuffle()),
                btn("y-icon y-now-skip", "prev", "Предыдущий", () => L.prev()),
                play,
                btn("y-icon y-now-skip", "next", "Следующий", () => L.next()),
                btn("y-icon y-toggle" + (L.player.repeat !== "off" ? " is-on" : "") + (L.player.repeat === "one" ? " is-one" : ""), "repeat",
                    L.player.repeat === "one" ? "Повтор трека" : "Повтор", () => L.cycleRepeat())));

        // На телефоне «обложка» = панель закрыта; на компьютере справа всегда есть список.
        const panelTab = tab === "cover" ? (wide ? "queue" : null) : tab;
        const tabs = h("div", { class: "y-now-tabs", role: "tablist" },
            [["queue", "Далее"], ["lyrics", "Текст"], ["related", "Похожие"]].map(([key, label]) =>
                h("button", {
                    class: "y-now-tab" + (panelTab === key ? " is-on" : ""), type: "button", role: "tab",
                    "aria-selected": panelTab === key ? "true" : "false",
                    onclick: () => pickTab(!wide && panelTab === key ? "cover" : key),
                }, label)));

        let content = null;
        if (panelTab === "lyrics") content = lyricsPanel(t);
        else if (panelTab === "queue") content = queuePanel();
        else if (panelTab === "related") content = relatedPanel(t);

        const panel = h("div", { class: "y-now-panel" + (panelTab ? " is-open" : "") },
            wide ? null : h("button", { class: "y-now-grab", type: "button", onclick: () => pickTab("cover"), "aria-label": "Свернуть панель" }, h("i")),
            tabs,
            content ? h("div", { class: "y-now-content" }, content) : null);

        clear(els.nowInner,
            h("div", { class: "y-now-top" },
                btn("y-icon", "down", "Свернуть", () => L.closeNow()),
                h("span", { class: "y-now-source" }, L.player.source ? "Из: " + L.player.source : "Сейчас играет"),
                btn("y-icon", "more", "Действия", (e) => openMenu(t, e))),
            h("div", { class: "y-now-body" }, stage, panel));

        if (els.nowBg.dataset.path !== t.path) {
            els.nowBg.dataset.path = t.path;
            clear(els.nowBg, L.cover(t.path, 96));
        }
        onTime();

        if (sheet.hidden) {
            sheet.hidden = false;
            document.documentElement.classList.add("y-locked");
            requestAnimationFrame(() => requestAnimationFrame(() => sheet.classList.add("is-open")));
        }
    }

    function lyricsPanel(t) {
        const box = h("div", { class: "y-lyrics" }, h("p", { class: "y-lyrics-wait" }, "Ищем текст…"));
        els.lyricLines = null;
        lastLine = -1;
        L.lyrics(t.path).then((body) => {
            if (L.current() !== t || !box.isConnected) return;
            if (body.synced && body.synced.length) {
                const lines = body.synced.map((s) => h("button", {
                    class: "y-line", type: "button", dataset: { at: String(s.at) }, onclick: () => L.seek(s.at),
                }, s.line || "♪"));
                els.lyricLines = lines;
                clear(box, lines, h("p", { class: "y-lyrics-src" }, "Текст: " + (body.source || "LRCLIB")));
                onTime();
            } else if (body.plain) {
                clear(box, body.plain.split("\n").map((line) => h("p", { class: "y-line is-plain" }, line || " ")),
                    h("p", { class: "y-lyrics-src" }, "Без таймингов"));
            } else {
                clear(box, h("p", { class: "y-lyrics-wait" }, body.reason || "Текста для этого трека нет."),
                    pill("Найти другой текст", "search", () => L.act("Выбрать текст")));
            }
        });
        return box;
    }

    /* Играющая строка в очереди: нажатие — пауза или продолжение, а не новая
     * очередь из одного трека. Перехват на фазе захвата — раньше, чем сработает
     * кнопка строки; «⋯» работает как обычно. */
    function currentRow(cur) {
        const box = h("div", { class: "y-list is-current" }, trackRow(cur, [cur], 0, {}));
        box.addEventListener("click", (e) => {
            const main = e.target.closest("button");
            if (!main || main.getAttribute("aria-label") === "Действия с треком" || /more/.test(main.className)) return;
            e.preventDefault();
            e.stopPropagation();
            L.toggle();
        }, true);
        return box;
    }

    function queuePanel() {
        const next = L.upcoming();
        const cur = L.current();
        return h("div", { class: "y-queue" },
            h("div", { class: "y-queue-head" },
                h("span", null, L.player.source ? "Играет из: " + L.player.source : "Очередь"),
                h("span", { class: "y-muted" }, next.length ? tracksWord(next.length) + " дальше" : "")),
            // Играющая строка — пауза и продолжение, а не новая очередь из одного трека.
            cur ? currentRow(cur) : null,
            next.length ? h("div", { class: "y-list" }, next.slice(0, 80).map(({ track, index }) =>
                h("div", { class: "y-row" },
                    h("button", { class: "y-row-main", type: "button", onclick: () => L.playAt(index) },
                        h("span", { class: "y-row-art" }, L.cover(track.path, 48)),
                        h("span", { class: "y-row-text" }, h("span", { class: "y-row-title" }, track.title), h("span", { class: "y-row-sub" }, h("span", null, track.artist || ""))),
                        h("span", { class: "y-row-time" }, L.fmtTime(track.duration))))))
                : h("p", { class: "y-muted y-pad" }, "Очередь закончится на этом треке."));
    }

    function relatedPanel(t) {
        const list = relatedTracks(t);
        if (!list.length) return h("p", { class: "y-muted y-pad" }, "Похожих треков в фонотеке не нашлось.");
        return h("div", { class: "y-queue" },
            h("div", { class: "y-queue-head" }, h("span", null, "Тот же артист и то же настроение"),
                pill("Слушать", "play", () => L.play(list, 0, { source: "Похоже на «" + t.title + "»" }), "is-small is-white")),
            h("div", { class: "y-list" }, list.map((x, i) => trackRow(x, list, i, { source: "Похоже на «" + t.title + "»" }))));
    }

    let lastLine = -1;
    function onTime() {
        const pos = L.player.audio.currentTime || 0;
        const dur = L.duration();
        const frac = dur ? pos / dur : 0;
        if (els.mini) els.mini.style.setProperty("--p", String(frac));
        if (els.barRange && !els.barRange.dataset.drag) {
            els.barRange.value = String(Math.round(frac * 1000));
            els.barRange.style.setProperty("--p", String(frac));
            els.barTime.textContent = L.fmtTime(pos) + " / " + L.fmtTime(dur);
        }
        if (els.nowRange && els.nowRange.isConnected && !els.nowRange.dataset.drag) {
            els.nowRange.value = String(Math.round(frac * 1000));
            els.nowRange.style.setProperty("--p", String(frac));
            els.nowT0.textContent = L.fmtTime(pos);
            els.nowT1.textContent = L.fmtTime(dur);
        }
        if (els.lyricLines && els.lyricLines[0] && els.lyricLines[0].isConnected) {
            let idx = -1;
            for (let i = 0; i < els.lyricLines.length; i++) {
                if (Number(els.lyricLines[i].dataset.at) <= pos + 0.25) idx = i; else break;
            }
            if (idx !== lastLine) {
                lastLine = idx;
                els.lyricLines.forEach((el, i) => {
                    el.classList.toggle("is-now", i === idx);
                    el.classList.toggle("is-past", i < idx);
                });
                const el = els.lyricLines[idx];
                if (el) el.scrollIntoView({ block: "center", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
            }
        }
    }

    function onTrack() {
        const t = L.current();
        lastLine = -1;
        els.mini.hidden = !t;
        els.app.classList.toggle("has-track", Boolean(t));
        if (!t) return;
        clear(els.miniArt, L.cover(t.path, 48));
        els.miniTitle.textContent = t.title;
        els.miniSub.textContent = t.artist || "";
        clear(els.barArt, L.cover(t.path, 48));
        els.barTitle.textContent = t.title;
        els.barSub.textContent = [t.artist, t.album && t.album !== t.title ? t.album : ""].filter(Boolean).join(" — ");
        markPlaying();
        if (L.ui.now) renderNow();
    }

    function onState() {
        const paused = L.player.audio.paused;
        for (const b of [els.miniPlay, els.barPlay, els.nowPlay]) {
            if (!b) continue;
            clear(b, icon(paused ? "play" : "pause"));
            b.setAttribute("aria-label", paused ? "Играть" : "Пауза");
        }
        els.app.classList.toggle("is-paused", paused);
        els.app.classList.toggle("is-loading", L.player.loading);
        const toggles = [[els.barShuf, L.player.shuffle], [els.barRep, L.player.repeat !== "off"]];
        if (els.now && !els.now.hidden) {
            const tog = els.now.querySelectorAll(".y-now-controls .y-toggle");
            toggles.push([tog[0], L.player.shuffle], [tog[1], L.player.repeat !== "off"]);
            if (tog[1]) tog[1].classList.toggle("is-one", L.player.repeat === "one");
        }
        for (const [b, on] of toggles) if (b) b.classList.toggle("is-on", on);
        if (els.barRep) els.barRep.classList.toggle("is-one", L.player.repeat === "one");
    }

    // ------------------------------------------------------------------
    // Меню: у курсора на компьютере, лист снизу на телефоне
    // ------------------------------------------------------------------

    function openMenu(track, event) {
        const head = h("div", { class: "y-menu-head" }, L.cover(track.path, 48),
            h("span", { class: "y-row-text" }, h("span", { class: "y-row-title" }, track.title), h("span", { class: "y-row-sub" }, h("span", null, track.artist || ""))));
        openActions(L.trackActions(track), event, null, head);
    }

    function openActions(actions, event, title, head) {
        closeMenu();
        const list = h("div", { class: "y-menu-list", role: "menu" }, actions.map((a) =>
            h("button", { class: "y-menu-item" + (a.danger ? " is-danger" : ""), type: "button", role: "menuitem", onclick: () => { closeMenu(); a.run(); } },
                icon(a.icon), h("span", null, a.label))));
        const menu = h("div", { class: "y-menu" + (wide ? " is-pop" : " is-sheet") },
            wide ? null : h("i", { class: "y-menu-grab", "aria-hidden": "true" }),
            wide ? null : (head || (title ? h("div", { class: "y-menu-head is-title" }, h("span", { class: "y-row-title" }, title)) : null)),
            list);
        const scrim = h("div", { class: "y-scrim", onclick: closeMenu });
        els.menu = h("div", { class: "y-menu-layer" }, scrim, menu);
        els.app.appendChild(els.menu);
        if (wide && event) {
            const rect = menu.getBoundingClientRect();
            const x = Math.min(event.clientX, innerWidth - rect.width - 12);
            const y = event.clientY + rect.height > innerHeight - 12 ? event.clientY - rect.height : event.clientY;
            menu.style.left = Math.max(12, x) + "px";
            menu.style.top = Math.max(12, y) + "px";
        }
        requestAnimationFrame(() => els.menu && els.menu.classList.add("is-open"));
        const first = list.querySelector("button");
        if (first) first.focus({ preventScroll: true });
    }

    function closeMenu() {
        if (!els.menu) return;
        const layer = els.menu;
        els.menu = null;
        layer.classList.remove("is-open");
        setTimeout(() => layer.remove(), 200);
    }

    function onKey(e) {
        if (e.key === "Escape") {
            if (els.menu) closeMenu();
            else if (L.ui.now) L.closeNow();
        } else if (e.key === "/" && !/^(INPUT|TEXTAREA)$/.test(e.target.tagName) && els.searchInput && wide) {
            e.preventDefault();
            els.searchInput.focus();
        }
    }

    // ------------------------------------------------------------------

    function mount(c) {
        ctx = c;
        const mq = matchMedia("(min-width: 900px)");
        wide = mq.matches;
        const onMq = () => { wide = mq.matches; renderRoute(true); if (L.ui.now) renderNow(); };
        mq.addEventListener("change", onMq);
        ctx.cleanup(() => mq.removeEventListener("change", onMq));
        document.addEventListener("keydown", onKey);
        ctx.cleanup(() => document.removeEventListener("keydown", onKey));
        ctx.cleanup(() => document.documentElement.classList.remove("y-locked"));

        els.view = h("div", { class: "y-view" });
        els.app = h("div", { class: "y-app" });
        els.app.style.setProperty("--glow", "rgba(80, 80, 90, 0.35)");
        clear(els.app,
            h("div", { class: "y-glow", "aria-hidden": "true" }),
            buildTop(), buildRail(),
            h("main", { class: "y-main" }, els.view),
            buildDock(), buildBar(), buildNow());
        clear(ctx.root, els.app);

        ctx.on("data", () => renderRoute());
        ctx.on("route", () => renderRoute());
        ctx.on("track", onTrack);
        ctx.on("state", onState);
        ctx.on("time", onTime);
        ctx.on("now", renderNow);
        ctx.on("queue", () => { if (L.ui.now && nowTab() === "queue") renderNow(); });
        renderRoute();
        onTrack();
        onState();
        renderNow();
    }

    function unmount() {
        if (viewCleanup) { viewCleanup(); viewCleanup = null; }
        for (const key of Object.keys(els)) delete els[key];
    }

    L.register({ id: "v3", name: "Лента", themeColor: "#0b0b0c", mount, unmount });
})();
