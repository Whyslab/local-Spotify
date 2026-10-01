/* Вариант 2 — «Пульт».
 *
 * Ось: плотность и постоянная структура. Инструмент, в котором живут:
 * на компьютере три панели (медиатека слева, содержимое в центре,
 * «сейчас играет» справа) и полоса плеера внизу, которая никуда не
 * уходит. Списки — таблица с колонками. Шапка коллекции окрашена цветом
 * её обложки и стекает в панель. Опора — Spotify, но палитра своя:
 * графит и шафран. */
(function () {
    "use strict";
    const L = window.Lab;
    const { h, icon, clear } = L;

    let ctx = null;
    const els = {};
    let viewCleanup = null;
    let wide = false;
    let rightOpen = true;
    let libFilter = "";       // "" | playlists | artists | albums
    let libQuery = "";
    let homeFilter = "all";
    let librarySort = "new";
    let searchQuery = "";

    const TRACKS = ["трек", "трека", "треков"];

    // ------------------------------------------------------------------
    // Цвет обложки → оттенок и насыщенность (светлоту задаёт CSS)
    // ------------------------------------------------------------------

    function toHs(rgb) {
        if (!rgb) return [240, 4];
        let [r, g, b] = rgb.map((x) => x / 255);
        const max = Math.max(r, g, b), min = Math.min(r, g, b);
        let hue = 0, s = 0;
        const l = (max + min) / 2;
        if (max !== min) {
            const d = max - min;
            s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
            hue = max === r ? (g - b) / d + (g < b ? 6 : 0) : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
            hue *= 60;
        }
        s = Math.round(s * 100);
        return [Math.round(hue), s < 10 ? s : Math.max(32, Math.min(72, s))];
    }

    function paint(el, rgb, prefix) {
        if (!el) return;
        const [hue, s] = toHs(rgb);
        el.style.setProperty("--" + prefix + "h", String(hue));
        el.style.setProperty("--" + prefix + "s", s + "%");
    }

    /* Шапка страницы красится цветом своей обложки, а не играющей. */
    function paintFrom(el, path) {
        L.coverColor(path).then((rgb) => paint(el, rgb, "c"));
    }

    // ------------------------------------------------------------------
    // Мелкие части
    // ------------------------------------------------------------------

    function iconBtn(cls, iconName, label, onclick) {
        return h("button", { class: "p-icon " + (cls || ""), type: "button", "aria-label": label, title: label, onclick }, icon(iconName));
    }

    function playBtn(cls, label, onclick) {
        return h("button", { class: "p-play " + (cls || ""), type: "button", "aria-label": label, title: label, onclick }, icon("play"));
    }

    function bars() {
        return h("span", { class: "p-bars", "aria-hidden": "true" }, h("i"), h("i"), h("i"));
    }

    function isCurrent(track) {
        const c = L.current();
        return Boolean(c && track && c.path === track.path);
    }

    function chips(items, active, onpick) {
        return h("div", { class: "p-chips", role: "tablist" }, items.map(([key, label]) =>
            h("button", {
                class: "p-chip" + (active === key ? " is-on" : ""), type: "button", role: "tab",
                "aria-selected": active === key ? "true" : "false", onclick: () => onpick(key),
            }, label)));
    }

    function section(title, action, ...body) {
        return h("section", { class: "p-section" },
            h("div", { class: "p-section-head" },
                h("h2", null, title),
                action ? h("button", { class: "p-more-link", type: "button", onclick: action.run }, action.label) : null),
            ...body);
    }

    function shelf(items) {
        return h("div", { class: "p-shelf" }, items);
    }

    function card({ coverEl, title, sub, open, play, round }) {
        return h("div", { class: "p-card" + (round ? " is-round" : "") },
            h("button", { class: "p-card-open", type: "button", onclick: open },
                h("span", { class: "p-card-art" }, coverEl),
                h("span", { class: "p-card-title" }, title),
                sub ? h("span", { class: "p-card-sub" }, sub) : null),
            play ? playBtn("p-card-play", "Слушать «" + title + "»", play) : null);
    }

    /* Верхняя строка страницы: назад/вперёд на компьютере, назад на
     * телефоне — только на вложенных экранах. */
    function topRow(sub, ...trailing) {
        // На телефоне у верхних экранов строки нет: кнопки уходят в заголовок.
        if (!sub && !wide) return null;
        return h("div", { class: "p-top" + (sub ? " is-sub" : "") },
            h("div", { class: "p-top-nav" },
                sub || wide ? iconBtn("p-round", "back", "Назад", () => history.back()) : null,
                wide ? iconBtn("p-round is-fwd", "back", "Вперёд", () => history.forward()) : null),
            h("div", { class: "p-top-end" }, trailing));
    }

    function pageTitle(text, ...trailing) {
        return h("header", { class: "p-title" }, h("h1", null, text), h("div", { class: "p-title-end" }, trailing));
    }

    function empty(text, actionLabel, run) {
        return h("div", { class: "p-empty" }, h("p", null, text),
            actionLabel ? h("button", { class: "p-pill is-accent", type: "button", onclick: run }, actionLabel) : null);
    }

    // ------------------------------------------------------------------
    // Таблица треков
    // ------------------------------------------------------------------

    function trackTable(list, opts) {
        opts = opts || {};
        const cls = "p-table" + (opts.noAlbum ? " is-noalbum" : "") + (opts.noCover ? " is-nocover" : "");
        return h("div", { class: cls, role: "table" },
            h("div", { class: "p-thead", role: "row" },
                h("span", { class: "p-th-num" }, "#"),
                h("span", null, "Название"),
                opts.noAlbum ? null : h("span", { class: "p-th-album" }, "Альбом"),
                h("span", { class: "p-th-added" }, "Добавлен"),
                h("span", { class: "p-th-time", "aria-label": "Длительность" }, icon("clock")),
                h("span")),
            list.map((t, i) => trackRow(t, list, i, opts)));
    }

    function trackRow(t, list, i, opts) {
        opts = opts || {};
        return h("div", { class: "p-tr" + (isCurrent(t) ? " is-playing" : "") + (t.missing ? " is-missing" : ""), role: "row", dataset: { path: t.path } },
            h("button", { class: "p-tr-main", type: "button", onclick: () => L.play(list, i, { source: opts.source || "" }) },
                h("span", { class: "p-tr-num" }, h("span", { class: "p-n" }, String(i + 1)), icon("play", "p-tr-hover"), bars()),
                h("span", { class: "p-tr-title" },
                    opts.noCover ? null : L.cover(t.path, 40),
                    h("span", { class: "p-tr-text" },
                        h("span", { class: "p-tr-name" }, t.title),
                        h("span", { class: "p-tr-artist" }, t.artist || ""))),
                opts.noAlbum ? null : h("span", { class: "p-tr-album" }, t.album || ""),
                h("span", { class: "p-tr-added" }, L.agoText(t.added)),
                h("span", { class: "p-tr-time" }, L.fmtTime(t.duration))),
            h("button", { class: "p-icon p-tr-more", type: "button", "aria-label": "Действия с треком", title: "Действия с треком", onclick: (e) => openTrackMenu(t, e) }, icon("more")));
    }

    function markPlaying() {
        const c = L.current();
        for (const row of els.app.querySelectorAll(".p-tr")) {
            row.classList.toggle("is-playing", Boolean(c && row.dataset.path === c.path));
        }
    }

    // ------------------------------------------------------------------
    // Экраны
    // ------------------------------------------------------------------

    function viewHome() {
        const d = L.data;
        const home = d.home || { moods: [], discover: { tracks: [] }, albums: [] };

        // Быстрый доступ: подборки, настроения, свежие альбомы — до восьми.
        const quick = [];
        for (const p of d.playlists) quick.push({ coverEl: L.playlistCover(p, 56), name: p.name, open: () => L.go("playlist", p.name), play: () => L.playlistTracks(p.name).then((pl) => L.play(pl.tracks, 0, { source: p.name })).catch((e) => L.toast(e.message)) });
        for (const m of home.moods) quick.push({ coverEl: L.cover(m.tracks[0] && m.tracks[0].path, 56), name: m.name, open: () => L.go("mood", m.key), play: () => L.play(m.tracks, 0, { source: m.name }) });
        for (const a of d.albums.filter((x) => x.tracks.length > 1)) {
            if (quick.length >= 8) break;
            quick.push({ coverEl: L.cover(a.cover, 56), name: a.album, open: () => L.go("album", a.artist, a.album), play: () => L.play(a.tracks, 0, { source: a.album }) });
        }
        const quickGrid = h("div", { class: "p-quick-grid" }, quick.slice(0, 8).map((q) =>
            h("div", { class: "p-quick" },
                h("button", { class: "p-quick-open", type: "button", onclick: q.open }, q.coverEl, h("span", null, q.name)),
                playBtn("p-quick-play", "Слушать «" + q.name + "»", q.play))));

        const moods = home.moods.map((m) => card({
            coverEl: L.cover(m.tracks[0] && m.tracks[0].path, 180), title: m.name, sub: m.hint,
            open: () => L.go("mood", m.key), play: () => L.play(m.tracks, 0, { source: m.name }),
        }));
        const disc = (home.discover && home.discover.tracks) || [];
        const based = (home.discover && home.discover.based_on) || [];
        const discCards = disc.slice(0, 14).map((t, i) => card({
            coverEl: L.cover(t.path, 180), title: t.title, sub: t.artist,
            open: () => L.play(disc, i, { source: "Похоже на любимое" }), play: () => L.play(disc, i, { source: "Похоже на любимое" }),
        }));
        const recent = d.tracks.slice(0, 16).map((t, i, list) => card({
            coverEl: L.cover(t.path, 180), title: t.title, sub: t.artist + ", " + L.agoText(t.added),
            open: () => L.play(list, i, { source: "Недавно добавлено" }), play: () => L.play(list, i, { source: "Недавно добавлено" }),
        }));
        const albums = home.albums.map((a) => {
            const al = d.albumByKey.get(L.albumKey(a.artist, a.album));
            return card({
                coverEl: L.cover(a.cover, 180), title: a.album, sub: a.artist,
                open: () => L.go("album", a.artist, a.album), play: al ? () => L.play(al.tracks, 0, { source: a.album }) : null,
            });
        });
        const artists = [...d.artists].sort((x, y) => y.tracks.length - x.tracks.length).slice(0, 12).map((a) => card({
            coverEl: L.cover(a.cover, 180, { class: "cover is-round" }), title: a.name, sub: L.plural(a.tracks.length, TRACKS),
            open: () => L.go("artist", a.name), play: () => L.play(a.tracks, 0, { source: a.name }), round: true,
        }));

        const f = homeFilter;
        const page = h("div", { class: "p-page p-home" },
            h("div", { class: "p-home-glow" }),
            topRow(false, wide ? null : iconBtn("", "service", "Служба", () => L.go("service"))),
            wide ? null : pageTitle("Главная", iconBtn("", "service", "Служба", () => L.go("service"))),
            chips([["all", "Всё"], ["moods", "Настроения"], ["new", "Новое"], ["albums", "Альбомы"], ["artists", "Артисты"]], f,
                (key) => { homeFilter = key; renderRoute(); }),
            f === "all" ? quickGrid : null,
            f === "all" || f === "moods" ? section("Под настроение", null, shelf(moods)) : null,
            (f === "all" || f === "new") && disc.length ? section("Похоже на любимое", null,
                based.length ? h("p", { class: "p-note" }, "По тем, кого вы слушаете: " + based.slice(0, 4).join(", ")) : null,
                shelf(discCards)) : null,
            f === "all" || f === "new" ? section("Недавно добавлено", { label: "Показать все", run: () => L.go("library", { tab: "tracks" }) }, shelf(recent)) : null,
            (f === "all" || f === "albums") && albums.length ? section("Альбомы", { label: "Показать все", run: () => L.go("library", { tab: "albums" }) }, shelf(albums)) : null,
            f === "all" || f === "artists" ? section("Больше всего в фонотеке", { label: "Показать все", run: () => L.go("library", { tab: "artists" }) }, shelf(artists)) : null);
        return page;
    }

    function viewLibrary(r) {
        const tab = r.query.tab || "tracks";
        const tabs = chips([["playlists", "Подборки"], ["tracks", "Треки"], ["artists", "Артисты"], ["albums", "Альбомы"]], tab,
            (key) => (key === "playlists" ? L.go("playlists") : L.go("library", { tab: key })));
        let body;
        if (tab === "artists") {
            body = h("div", { class: "p-grid" }, L.data.artists.map((a) => card({
                coverEl: L.cover(a.cover, 180, { class: "cover is-round" }), title: a.name, sub: L.plural(a.tracks.length, TRACKS),
                open: () => L.go("artist", a.name), play: () => L.play(a.tracks, 0, { source: a.name }), round: true,
            })));
        } else if (tab === "albums") {
            const albums = L.data.albums.filter((a) => a.tracks.length > 1);
            body = h("div", { class: "p-grid" }, albums.map((a) => card({
                coverEl: L.cover(a.cover, 180), title: a.album, sub: a.artist,
                open: () => L.go("album", a.artist, a.album), play: () => L.play(a.tracks, 0, { source: a.album }),
            })));
        } else {
            const list = librarySort === "name"
                ? [...L.data.tracks].sort((x, y) => x.title.localeCompare(y.title, "ru", { sensitivity: "base" }))
                : L.data.tracks;
            const total = list.reduce((s, t) => s + (t.duration || 0), 0);
            body = h("div", null,
                h("div", { class: "p-actions" },
                    playBtn("p-play-big", "Слушать фонотеку", () => L.play(list, 0, { shuffle: false, source: "Фонотека" })),
                    iconBtn("p-act" + (L.player.shuffle ? " is-on" : ""), "shuffle", "Перемешать фонотеку", () => L.play(list, -1, { shuffle: true, source: "Фонотека" })),
                    h("span", { class: "p-meta" }, L.plural(list.length, TRACKS) + ", " + L.fmtTotal(total)),
                    h("button", {
                        class: "p-sort", type: "button",
                        onclick: () => { librarySort = librarySort === "new" ? "name" : "new"; renderRoute(); },
                    }, h("span", null, librarySort === "new" ? "Сначала новые" : "По названию"), icon("sort"))),
                trackTable(list, { source: "Фонотека" }));
        }
        return h("div", { class: "p-page" },
            topRow(false, iconBtn("", "add", "Добавить музыку", () => L.go("add"))),
            pageTitle("Медиатека", wide ? null : iconBtn("", "add", "Добавить музыку", () => L.go("add"))),
            tabs, body);
    }

    /* Шапка коллекции — альбом, подборка, настроение: цвет обложки
     * сверху, стекает в панель через полосу действий. */
    function collectionPage({ coverEl, coverPath, title, sub, onSub, meta, tracks, source, opts, menu }) {
        const total = tracks.reduce((s, t) => s + (t.duration || 0), 0);
        const page = h("div", { class: "p-page p-collection" },
            topRow(true),
            h("header", { class: "p-col-head" },
                h("div", { class: "p-col-art" }, coverEl),
                h("div", { class: "p-col-text" },
                    h("h1", { class: "p-col-title" + (title.length > 22 ? " is-long" : "") }, title),
                    h("p", { class: "p-col-meta" },
                        sub ? h("button", { class: "p-col-sub", type: "button", onclick: onSub || null, disabled: !onSub }, sub) : null,
                        h("span", null, meta || (L.plural(tracks.length, TRACKS) + ", " + L.fmtTotal(total)))))),
            h("div", { class: "p-col-body" },
                h("div", { class: "p-actions" },
                    playBtn("p-play-big", "Слушать «" + title + "»", () => L.play(tracks, 0, { shuffle: false, source })),
                    iconBtn("p-act", "shuffle", "Перемешать «" + title + "»", () => L.play(tracks, -1, { shuffle: true, source })),
                    menu ? iconBtn("p-act", "more", "Ещё", (e) => openMenu({ title, actions: menu }, e)) : null),
                tracks.length ? trackTable(tracks, Object.assign({ source }, opts || {})) : empty("Здесь пока нет треков.")));
        if (coverPath) paintFrom(page, coverPath);
        return page;
    }

    function viewAlbum(r) {
        const [artist, album] = r.params;
        const al = L.data.albumByKey.get(L.albumKey(artist, album));
        if (!al) return h("div", { class: "p-page" }, topRow(true), empty("Такого альбома в фонотеке нет."));
        return collectionPage({
            coverEl: L.cover(al.cover, 300), coverPath: al.cover, title: al.album, sub: al.artist,
            onSub: () => L.go("artist", al.artist), tracks: al.tracks, source: al.album,
            opts: { noCover: true, noAlbum: true },
            menu: [
                { label: "Перейти к артисту", icon: "artist", run: () => L.go("artist", al.artist) },
                { label: "Добавить в подборку", icon: "playlists", run: () => L.act("Добавить альбом в подборку") },
                { label: "Удалить альбом", icon: "trash", danger: true, run: () => L.act("Удалить альбом") },
            ],
        });
    }

    function viewMood(r) {
        const m = L.data.home && L.data.home.moods.find((x) => x.key === r.params[0]);
        if (!m) return h("div", { class: "p-page" }, topRow(true), empty("Подборка по настроению обновилась — откройте главную.", "На главную", () => L.go("home")));
        const tracks = m.tracks.map((t) => L.data.byPath.get(t.path) || t);
        const first = m.tracks[0] && m.tracks[0].path;
        return collectionPage({
            coverEl: L.cover(first, 300), coverPath: first, title: m.name, sub: m.hint, tracks, source: m.name,
            menu: [{ label: "Сохранить как подборку", icon: "playlists", run: () => L.act("Сохранить как подборку") }],
        });
    }

    let artistAll = false;
    function viewArtist(r) {
        const a = L.data.artistByName.get(r.params[0]);
        if (!a) return h("div", { class: "p-page" }, topRow(true), empty("Такого артиста в фонотеке нет."));
        const albums = L.data.albums.filter((al) => al.artist === a.name);
        const shown = artistAll ? a.tracks : a.tracks.slice(0, 8);
        const page = h("div", { class: "p-page p-artist" },
            h("header", { class: "p-hero" },
                L.cover(a.cover, 600),
                h("div", { class: "p-hero-shade" }),
                topRow(true),
                h("div", { class: "p-hero-text" },
                    h("h1", { class: a.name.length > 16 ? "is-long" : "" }, a.name),
                    h("p", null, L.plural(a.tracks.length, TRACKS) + (albums.length ? ", " + L.plural(albums.length, ["альбом", "альбома", "альбомов"]) : "")))),
            h("div", { class: "p-col-body" },
                h("div", { class: "p-actions" },
                    playBtn("p-play-big", "Слушать " + a.name, () => L.play(a.tracks, 0, { source: a.name })),
                    iconBtn("p-act", "shuffle", "Перемешать " + a.name, () => L.play(a.tracks, -1, { shuffle: true, source: a.name })),
                    iconBtn("p-act", "more", "Ещё", (e) => openMenu({ title: a.name, actions: [
                        { label: "Добавить всё в очередь", icon: "list", run: () => a.tracks.forEach((t) => L.enqueue(t)) },
                        { label: "Удалить артиста из фонотеки", icon: "trash", danger: true, run: () => L.act("Удалить артиста") },
                    ] }, e))),
                section("Треки", null, trackTable(shown, { source: a.name }),
                    a.tracks.length > 8 ? h("button", { class: "p-more-link is-block", type: "button", onclick: () => { artistAll = !artistAll; renderRoute(); } },
                        artistAll ? "Свернуть" : "Показать все " + a.tracks.length) : null),
                albums.length ? section("Альбомы и синглы", null, shelf(albums.map((al) => card({
                    coverEl: L.cover(al.cover, 180), title: al.album, sub: L.plural(al.tracks.length, TRACKS),
                    open: () => L.go("album", al.artist, al.album), play: () => L.play(al.tracks, 0, { source: al.album }),
                })))) : null));
        paintFrom(page, a.cover);
        return page;
    }

    function viewPlaylists() {
        const pls = L.data.playlists;
        return h("div", { class: "p-page" },
            topRow(false, iconBtn("", "add", "Новая подборка", () => L.act("Новая подборка"))),
            pageTitle("Подборки", wide ? null : iconBtn("", "add", "Новая подборка", () => L.act("Новая подборка"))),
            wide ? null : chips([["playlists", "Подборки"], ["tracks", "Треки"], ["artists", "Артисты"], ["albums", "Альбомы"]], "playlists",
                (key) => (key === "playlists" ? null : L.go("library", { tab: key }))),
            pls.length ? h("div", { class: "p-grid" }, pls.map((p) => card({
                coverEl: L.playlistCover(p, 180), title: p.name, sub: L.plural(p.tracks, TRACKS),
                open: () => L.go("playlist", p.name), play: () => L.playlistTracks(p.name).then((pl) => L.play(pl.tracks, 0, { source: p.name })).catch((e) => L.toast(e.message)),
            })))
                : empty("Подборок пока нет. Соберите первую из треков фонотеки.", "Новая подборка", () => L.act("Новая подборка")));
    }

    function viewPlaylist(r) {
        const name = r.params[0];
        const meta = L.data.playlists.find((p) => p.name === name) || { name };
        const holder = h("div", { class: "p-page" }, topRow(true), h("div", { class: "p-skel is-head" }), h("div", { class: "p-skel" }), h("div", { class: "p-skel" }));
        L.playlistTracks(name).then((pl) => {
            if (!holder.isConnected) return;
            const firstPath = pl.tracks[0] && pl.tracks[0].path;
            holder.replaceWith(collectionPage({
                coverEl: L.playlistCover(meta, 300), coverPath: firstPath, title: name,
                meta: L.plural(pl.tracks.length, TRACKS) + ", " + L.fmtTotal(pl.tracks.reduce((s, t) => s + (t.duration || 0), 0)) +
                    (meta.updated_at ? ", изменена " + L.agoText(meta.updated_at) : ""),
                tracks: pl.tracks, source: name,
                menu: [
                    { label: "Переименовать", icon: "lyrics", run: () => L.act("Переименовать подборку") },
                    { label: "Сменить обложку", icon: "grid", run: () => L.act("Сменить обложку") },
                    { label: "Изменить порядок", icon: "sort", run: () => L.act("Изменить порядок") },
                    { label: "Удалить подборку", icon: "trash", danger: true, run: () => L.act("Удалить подборку") },
                ],
            }));
        }).catch((e) => clear(holder, topRow(true), empty(e.message)));
        return holder;
    }

    function browseTiles() {
        const d = L.data;
        const tiles = [];
        for (const m of (d.home ? d.home.moods : [])) {
            tiles.push({ name: m.name, path: m.tracks[0] && m.tracks[0].path, open: () => L.go("mood", m.key) });
        }
        for (const p of d.playlists) tiles.push({ name: p.name, playlist: p, open: () => L.go("playlist", p.name) });
        for (const a of [...d.artists].sort((x, y) => y.tracks.length - x.tracks.length).slice(0, 10)) {
            tiles.push({ name: a.name, path: a.cover, open: () => L.go("artist", a.name) });
        }
        return h("div", { class: "p-browse" }, tiles.map((t) => {
            const tile = h("button", { class: "p-tile", type: "button", onclick: t.open },
                h("span", { class: "p-tile-name" }, t.name),
                t.playlist ? L.playlistCover(t.playlist, 96) : L.cover(t.path, 96));
            tile.style.setProperty("--th", String(L.hashHue(t.name)));
            return tile;
        }));
    }

    function viewSearch(r) {
        searchQuery = r.query.q !== undefined ? r.query.q : searchQuery;
        const results = h("div", { class: "p-results" });
        const input = h("input", {
            class: "p-search-input", type: "search", placeholder: "Что хотите послушать?", value: searchQuery,
            "aria-label": "Поиск по фонотеке", autocomplete: "off", enterkeyhint: "search",
        });
        let timer = 0;
        input.addEventListener("input", () => {
            clearTimeout(timer);
            timer = setTimeout(() => {
                searchQuery = input.value;
                L.replaceRoute("search", { q: searchQuery });
                fill();
            }, 120);
        });
        // Набрали и сразу ушли на результат — запоздавший таймер не должен
        // переписать адрес уже открытого экрана.
        viewCleanup = () => clearTimeout(timer);

        function fill() {
            const q = searchQuery.trim();
            if (!q) {
                clear(results, section("Обзор", null, browseTiles()));
                return;
            }
            const found = L.searchLocal(q, 60);
            const yt = h("div", { class: "p-yt" },
                h("button", { class: "p-pill", type: "button", onclick: () => askYouTube(q, yt) },
                    icon("search"), h("span", null, "Найти «" + q + "» на YouTube")));
            if (!found.tracks.length && !found.artists.length && !found.albums.length) {
                clear(results, empty("В фонотеке ничего не нашлось по «" + q + "»."), yt);
                return;
            }
            // Лучшее совпадение: артист, если имя совпало, иначе первый трек.
            const bestArtist = found.artists[0];
            const bestTrack = found.tracks[0];
            const best = bestArtist
                ? h("button", { class: "p-best", type: "button", onclick: () => L.go("artist", bestArtist.name) },
                    L.cover(bestArtist.cover, 120, { class: "cover is-round" }),
                    h("span", { class: "p-best-name" }, bestArtist.name),
                    h("span", { class: "p-best-kind" }, "Артист, " + L.plural(bestArtist.tracks.length, TRACKS)))
                : h("button", { class: "p-best", type: "button", onclick: () => L.play(found.tracks, 0, { source: "Поиск" }) },
                    L.cover(bestTrack.path, 120),
                    h("span", { class: "p-best-name" }, bestTrack.title),
                    h("span", { class: "p-best-kind" }, "Трек, " + (bestTrack.artist || "")));
            clear(results,
                h("div", { class: "p-search-top" },
                    section("Лучшее совпадение", null, best),
                    found.tracks.length ? section("Треки", null, h("div", { class: "p-table is-compact is-noalbum" },
                        found.tracks.slice(0, 4).map((t, i) => trackRow(t, found.tracks, i, { source: "Поиск", noAlbum: true })))) : null),
                found.tracks.length > 4 ? section("Все треки", null, trackTable(found.tracks.slice(4), { source: "Поиск" })) : null,
                found.artists.length ? section("Артисты", null, shelf(found.artists.map((a) => card({
                    coverEl: L.cover(a.cover, 180, { class: "cover is-round" }), title: a.name, sub: "Артист",
                    open: () => L.go("artist", a.name), play: () => L.play(a.tracks, 0, { source: a.name }), round: true,
                })))) : null,
                found.albums.length ? section("Альбомы", null, shelf(found.albums.map((a) => card({
                    coverEl: L.cover(a.cover, 180), title: a.album, sub: a.artist,
                    open: () => L.go("album", a.artist, a.album), play: () => L.play(a.tracks, 0, { source: a.album }),
                })))) : null,
                yt);
        }

        fill();
        setTimeout(() => { if (!searchQuery && wide) input.focus({ preventScroll: true }); }, 50);
        return h("div", { class: "p-page" },
            topRow(false),
            wide ? null : pageTitle("Поиск"),
            h("label", { class: "p-search" }, icon("search"), input),
            results);
    }

    function askYouTube(q, box) {
        clear(box, h("div", { class: "p-skel" }));
        L.searchYouTube(q).then((body) => {
            const list = body.results || [];
            clear(box, section("На YouTube", null, list.length ? h("div", { class: "p-yt-list" }, list.map((v) =>
                h("div", { class: "p-yt-row" },
                    h("span", { class: "p-yt-icon" }, icon("track")),
                    h("span", { class: "p-tr-text" },
                        h("span", { class: "p-tr-name" }, v.title || ""),
                        h("span", { class: "p-tr-artist" }, [v.channel || v.uploader || "", v.duration ? L.fmtTime(v.duration) : ""].filter(Boolean).join(", "))),
                    h("button", { class: "p-pill is-small", type: "button", onclick: () => L.act("Скачать в фонотеку") }, icon("download"), h("span", null, "Скачать")))))
                : empty("YouTube ничего не нашёл.")));
        }).catch((e) => clear(box, empty("Поиск на YouTube не ответил: " + e.message)));
    }

    function statusText(t) {
        return { done: "Готово", failed: "Ошибка", error: "Ошибка", processing: "Скачивается", downloading: "Скачивается", tagging: "Подписываем теги", queued: "В очереди", pending: "В очереди", skipped: "Уже есть" }[t.status] || t.status;
    }

    function viewAdd() {
        const area = h("textarea", {
            class: "p-textarea", rows: "4", placeholder: "Ссылки на YouTube, Spotify или Deezer — по одной в строке",
            "aria-label": "Ссылки для скачивания",
        });
        const tasksBox = h("div", { class: "p-tasks" }, h("div", { class: "p-skel" }), h("div", { class: "p-skel" }));
        function loadTasks() {
            L.tasks().then((tasks) => {
                if (!tasksBox.isConnected) return;
                const counts = {};
                for (const t of tasks) counts[t.status] = (counts[t.status] || 0) + 1;
                clear(tasksBox,
                    h("div", { class: "p-thead is-tasks" }, h("span"), h("span", null, "Трек"), h("span", { class: "p-th-added" }, "Состояние"), h("span", { class: "p-th-time" }, "Когда")),
                    tasks.slice(0, 40).map((t) =>
                        h("div", { class: "p-task is-" + t.status },
                            h("span", { class: "p-task-icon" }, icon(t.status === "done" ? "check" : /fail|error/.test(t.status) ? "warn" : "download")),
                            h("span", { class: "p-tr-text" },
                                h("span", { class: "p-tr-name" }, t.title || t.url),
                                h("span", { class: "p-tr-artist" }, [t.artist, t.error].filter(Boolean).join(" — "))),
                            h("span", { class: "p-task-status" }, statusText(t)),
                            h("span", { class: "p-task-time" }, (t.updated_at || "").slice(5, 16)))));
            }).catch((e) => clear(tasksBox, empty(e.message)));
        }
        loadTasks();
        const timer = setInterval(loadTasks, 5000);
        viewCleanup = () => clearInterval(timer);
        return h("div", { class: "p-page" },
            topRow(false),
            pageTitle("Добавить музыку"),
            h("div", { class: "p-add" },
                area,
                h("div", { class: "p-add-actions" },
                    h("button", { class: "p-pill is-accent", type: "button", onclick: () => L.act("Скачать") }, icon("download"), h("span", null, "Скачать")),
                    h("button", { class: "p-pill", type: "button", onclick: () => L.act("Загрузить файлы") }, icon("add"), h("span", null, "Из файлов")),
                    h("button", { class: "p-pill", type: "button", onclick: () => L.act("Повторить неудачные") }, icon("repeat"), h("span", null, "Повторить неудачные")))),
            section("Загрузки", null, tasksBox));
    }

    function viewService() {
        const stats = h("div", { class: "p-stats" }, [1, 2, 3, 4].map(() => h("div", { class: "p-skel is-stat" })));
        const checks = h("div", { class: "p-checks" });
        L.health().then((s) => {
            const tiles = [
                ["Треков", s.tracks], ["Альбомов", s.albums], ["Подборок", s.playlists], ["Прослушиваний", s.plays_logged],
                ["В очереди", s.queue_size], ["Ждут Navidrome", s.navidrome_pending], ["Потоков загрузки", s.workers],
            ];
            clear(stats, tiles.map(([k, v]) => h("div", { class: "p-stat" }, h("span", { class: "p-stat-n" }, String(v ?? "—")), h("span", { class: "p-stat-k" }, k))));
            const ok = (v) => v === "ok" || v === "healthy" || v === "configured";
            const rows = [
                ["Служба", s.status, s.status === "healthy" ? "Работает" : s.status],
                ["База", s.database, s.database], ["Фонотека", s.library, s.library_path || s.library],
                ["ffmpeg", s.ffmpeg, s.ffmpeg], ["Deno", s.js_runtime, s.js_runtime],
                ["Navidrome", s.navidrome, s.navidrome === "configured" ? "Подключён" : s.navidrome],
                ["ListenBrainz", s.listenbrainz, s.listenbrainz],
            ];
            clear(checks, rows.map(([k, state, text]) => h("div", { class: "p-check" + (ok(state) ? " is-ok" : " is-bad") },
                h("span", { class: "p-dot" }), h("span", null, k), h("span", { class: "p-check-v" }, String(text ?? "—")))));
        }).catch((e) => clear(stats, empty(e.message)));
        return h("div", { class: "p-page" },
            topRow(false),
            pageTitle("Служба",
                h("button", { class: "p-pill", type: "button", onclick: () => L.act("Проверить фонотеку") }, icon("service"), h("span", null, "Проверить")),
                h("button", { class: "p-pill", type: "button", onclick: () => L.act("Синхронизировать с Navidrome") }, icon("repeat"), h("span", null, "Синхронизировать"))),
            stats, section("Проверки", null, checks));
    }

    const VIEWS = {
        home: viewHome, library: viewLibrary, artist: viewArtist, album: viewAlbum, mood: viewMood,
        playlists: viewPlaylists, playlist: viewPlaylist, search: viewSearch, add: viewAdd, service: viewService,
    };

    let lastRouteKey = "";
    function renderRoute() {
        if (viewCleanup) { viewCleanup(); viewCleanup = null; }
        const r = L.route.current;
        if (lastRouteKey !== r.raw) artistAll = false;
        updateNav(r);
        if (!L.data.ready) {
            clear(els.view, L.data.error
                ? h("div", { class: "p-page" }, empty(L.data.error.message, L.data.error.auth ? "Открыть плеер" : "Повторить",
                    () => { if (L.data.error.auth) location.href = "/"; else L.load(); }))
                : h("div", { class: "p-page" }, h("div", { class: "p-skel is-head" }), h("div", { class: "p-skel" }), h("div", { class: "p-skel" })));
            return;
        }
        const view = VIEWS[r.name] || VIEWS.home;
        clear(els.view, view(r));
        if (lastRouteKey !== r.raw) {
            window.scrollTo(0, 0);
            if (els.main) els.main.scrollTop = 0;
        }
        lastRouteKey = r.raw;
        fillLib();
    }

    // ------------------------------------------------------------------
    // Навигация: левая панель (компьютер) и вкладки (телефон)
    // ------------------------------------------------------------------

    const TABS = [
        ["home", "Главная", "home", ["home", "mood", "service"]],
        ["search", "Поиск", "search", ["search"]],
        ["library", "Медиатека", "library", ["library", "artist", "album", "playlists", "playlist"]],
        ["add", "Добавить", "add", ["add"]],
    ];

    function updateNav(r) {
        for (const b of els.app.querySelectorAll("[data-nav]")) {
            const on = b.dataset.nav.split(",").includes(r.name);
            b.classList.toggle("is-on", on);
            if (on) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
        }
    }

    function buildLeft() {
        const nav = (route, label, iconName, match) =>
            h("button", { class: "p-nav", type: "button", dataset: { nav: match.join(",") }, onclick: () => L.go(route) },
                icon(iconName), h("span", null, label));
        const filterInput = h("input", { class: "p-lib-filter", type: "search", placeholder: "Искать в медиатеке", "aria-label": "Искать в медиатеке", value: libQuery });
        filterInput.addEventListener("input", () => { libQuery = filterInput.value; fillLib(); });
        els.libChips = h("div", { class: "p-lib-chips" });
        els.libList = h("div", { class: "p-lib-list" });
        return h("div", { class: "p-left" },
            h("nav", { class: "p-panel p-navpanel", "aria-label": "Разделы" },
                nav("home", "Главная", "home", ["home", "mood"]),
                nav("search", "Поиск", "search", ["search"]),
                nav("add", "Добавить музыку", "download", ["add"]),
                nav("service", "Служба", "service", ["service"])),
            h("section", { class: "p-panel p-lib", "aria-label": "Медиатека" },
                h("div", { class: "p-lib-head" },
                    h("button", { class: "p-lib-title", type: "button", onclick: () => L.go("library") }, icon("library"), h("span", null, "Медиатека")),
                    iconBtn("p-small", "add", "Новая подборка", () => L.act("Новая подборка"))),
                els.libChips,
                h("label", { class: "p-lib-search" }, icon("search"), filterInput),
                els.libList));
    }

    function fillLib() {
        if (!els.libList || !wide) return;
        clear(els.libChips, [["playlists", "Подборки"], ["artists", "Артисты"], ["albums", "Альбомы"]].map(([key, label]) =>
            h("button", {
                class: "p-chip is-small" + (libFilter === key ? " is-on" : ""), type: "button",
                "aria-pressed": libFilter === key ? "true" : "false",
                onclick: () => { libFilter = libFilter === key ? "" : key; fillLib(); },
            }, label)));
        if (!L.data.ready) return;
        const q = libQuery.trim().toLowerCase();
        const hit = (s) => !q || (s || "").toLowerCase().includes(q);
        const r = L.route.current;
        const rows = [];
        const row = (coverEl, name, kind, onclick, on, round) => rows.push(
            h("button", { class: "p-lib-row" + (on ? " is-on" : "") + (round ? " is-round" : ""), type: "button", onclick },
                coverEl, h("span", { class: "p-lib-text" }, h("span", { class: "p-lib-name" }, name), h("span", { class: "p-lib-kind" }, kind))));
        if (!libFilter || libFilter === "playlists") {
            row(h("span", { class: "p-lib-liked" }, icon("track")), "Все треки", "Фонотека, " + L.plural(L.data.tracks.length, TRACKS),
                () => L.go("library", { tab: "tracks" }), r.name === "library" && (r.query.tab || "tracks") === "tracks");
            for (const p of L.data.playlists) {
                if (!hit(p.name)) continue;
                row(L.playlistCover(p, 48), p.name, "Подборка, " + L.plural(p.tracks, TRACKS), () => L.go("playlist", p.name),
                    r.name === "playlist" && r.params[0] === p.name);
            }
        }
        if (!libFilter || libFilter === "albums") {
            for (const a of L.data.albums) {
                if (a.tracks.length < 2 || !(hit(a.album) || hit(a.artist))) continue;
                row(L.cover(a.cover, 48), a.album, "Альбом, " + a.artist, () => L.go("album", a.artist, a.album),
                    r.name === "album" && r.params[0] === a.artist && r.params[1] === a.album);
            }
        }
        if (!libFilter || libFilter === "artists") {
            for (const a of L.data.artists) {
                if (!hit(a.name)) continue;
                row(L.cover(a.cover, 48, { class: "cover is-round" }), a.name, "Артист", () => L.go("artist", a.name),
                    r.name === "artist" && r.params[0] === a.name, true);
            }
        }
        clear(els.libList, rows.length ? rows : h("p", { class: "p-lib-empty" }, "Ничего не нашлось."));
    }

    // ------------------------------------------------------------------
    // Правая панель «Сейчас играет» (компьютер)
    // ------------------------------------------------------------------

    function renderRight() {
        if (!els.right) return;
        // Панель скрыта (телефон или закрыта) — не рисуем и не спрашиваем текст.
        if (!wide || !rightOpen) return;
        const t = L.current();
        if (!t) {
            clear(els.rightBody, h("div", { class: "p-right-empty" },
                h("div", { class: "p-right-empty-art" }, icon("track")),
                h("p", null, "Включите трек — здесь будут обложка, текст и очередь.")));
            els.rightTitle.textContent = "Сейчас играет";
            return;
        }
        els.rightTitle.textContent = L.player.source || "Сейчас играет";
        const next = L.upcoming()[0];
        const artist = L.data.artistByName.get(L.mainArtist(t));
        const lyr = h("div", { class: "p-lyr-card" },
            h("div", { class: "p-lyr-card-head" }, h("h3", null, "Текст"),
                h("button", { class: "p-pill is-small is-ghost", type: "button", onclick: () => L.openNow("lyrics") }, "Показать")),
            h("div", { class: "p-lyr-preview" }, h("p", null, "Ищем текст…")));
        L.lyrics(t.path).then((body) => {
            if (L.current() !== t || !lyr.isConnected) return;
            const lines = body.synced && body.synced.length ? body.synced.map((s) => s.line).filter(Boolean)
                : body.plain ? body.plain.split("\n").filter(Boolean) : [];
            clear(lyr.lastChild, lines.length ? lines.slice(0, 6).map((l) => h("p", null, l))
                : h("p", { class: "is-dim" }, body.reason || "Текста для этого трека нет."));
        });
        clear(els.rightBody,
            h("button", { class: "p-right-art", type: "button", onclick: () => L.openNow("cover"), "aria-label": "Открыть плеер" }, L.cover(t.path, 300)),
            h("div", { class: "p-right-head" },
                h("div", { class: "p-right-text" },
                    h("div", { class: "p-right-title" }, t.title),
                    h("button", { class: "p-link", type: "button", onclick: () => L.go("artist", L.mainArtist(t)) }, t.artist || "")),
                iconBtn("", "more", "Действия с треком", (e) => openTrackMenu(t, e))),
            lyr,
            h("div", { class: "p-card-box" },
                h("div", { class: "p-lyr-card-head" }, h("h3", null, "Далее"),
                    h("button", { class: "p-pill is-small is-ghost", type: "button", onclick: () => L.openNow("queue") }, "Очередь")),
                next ? h("button", { class: "p-next", type: "button", onclick: () => L.playAt(next.index) },
                    L.cover(next.track.path, 48),
                    h("span", { class: "p-tr-text" }, h("span", { class: "p-tr-name" }, next.track.title), h("span", { class: "p-tr-artist" }, next.track.artist || "")))
                    : h("p", { class: "p-dim" }, "Очередь закончится на этом треке.")),
            artist ? h("div", { class: "p-card-box p-about" },
                h("h3", null, "В фонотеке"),
                h("button", { class: "p-about-name", type: "button", onclick: () => L.go("artist", artist.name) }, artist.name),
                h("p", { class: "p-dim" }, L.plural(artist.tracks.length, TRACKS) + (artist.albums.size ? ", " + L.plural(artist.albums.size, ["релиз", "релиза", "релизов"]) : "")))
                : null);
        L.coverColor(t.path).then((rgb) => { if (L.current() === t) paint(els.app, rgb, "n"); });
    }

    function setRight(open) {
        rightOpen = open;
        els.app.classList.toggle("is-right-closed", !open);
        if (els.barPanel) els.barPanel.classList.toggle("is-on", open);
        if (open) renderRight();
    }

    // ------------------------------------------------------------------
    // Полоса плеера (компьютер), мини-плеер и вкладки (телефон)
    // ------------------------------------------------------------------

    function seekRange(cls, label) {
        const range = h("input", { class: "p-range " + (cls || ""), type: "range", min: "0", max: "1000", value: "0", step: "1", "aria-label": label || "Позиция в треке" });
        range.addEventListener("input", () => {
            range.dataset.drag = "1";
            range.style.setProperty("--p", String(range.value / 1000));
        });
        range.addEventListener("change", () => { delete range.dataset.drag; L.seek(range.value / 1000 * L.duration()); });
        return range;
    }

    function buildBar() {
        const art = h("button", { class: "p-bar-art", type: "button", onclick: () => L.openNow("cover"), "aria-label": "Открыть плеер" });
        const title = h("button", { class: "p-bar-title", type: "button", onclick: () => L.openNow("cover") }, "Ничего не играет");
        const sub = h("button", { class: "p-bar-sub", type: "button", onclick: () => { const t = L.current(); if (t) L.go("artist", L.mainArtist(t)); } }, "Выберите трек в медиатеке");
        const range = seekRange("p-bar-range");
        const t0 = h("span", { class: "p-time" }, "0:00");
        const t1 = h("span", { class: "p-time" }, "0:00");
        const play = h("button", { class: "p-bar-play", type: "button", "aria-label": "Играть", onclick: () => L.toggle() }, icon("play"));
        const shuf = iconBtn("p-toggle", "shuffle", "Перемешивать", () => L.setShuffle());
        const rep = iconBtn("p-toggle", "repeat", "Повтор", () => L.cycleRepeat());
        const lyr = iconBtn("p-toggle", "lyrics", "Текст", () => toggleNowTab("lyrics"));
        const que = iconBtn("p-toggle", "queue", "Очередь", () => toggleNowTab("queue"));
        const panel = iconBtn("p-toggle is-on", "album", "Панель «Сейчас играет»", () => setRight(!rightOpen));
        let vol = null;
        if (L.volumeAdjustable) {
            // Громкость человека живёт в ядре: поправка трека (gainFactor)
            // умножается на неё при каждой смене трека и при смене варианта.
            const vrange = h("input", { class: "p-range p-vol", type: "range", min: "0", max: "100", value: String(Math.round(L.player.userVolume * 100)), "aria-label": "Громкость" });
            vrange.style.setProperty("--p", String(L.player.userVolume));
            vrange.addEventListener("input", () => {
                L.player.userVolume = vrange.value / 100;
                vrange.style.setProperty("--p", String(L.player.userVolume));
                L.player.audio.volume = Math.min(1, L.player.gainFactor * L.player.userVolume);
            });
            vol = h("div", { class: "p-vol-wrap" }, icon("volume"), vrange);
        }
        Object.assign(els, { barArt: art, barTitle: title, barSub: sub, barRange: range, barT0: t0, barT1: t1, barPlay: play, barShuf: shuf, barRep: rep, barLyr: lyr, barQue: que, barPanel: panel });
        return h("footer", { class: "p-bar" },
            h("div", { class: "p-bar-left" }, art, h("div", { class: "p-bar-text" }, title, sub)),
            h("div", { class: "p-bar-center" },
                h("div", { class: "p-bar-buttons" }, shuf, iconBtn("", "prev", "Предыдущий", () => L.prev()), play, iconBtn("", "next", "Следующий", () => L.next()), rep),
                h("div", { class: "p-bar-progress" }, t0, range, t1)),
            h("div", { class: "p-bar-right" }, lyr, que, panel, vol));
    }

    function toggleNowTab(tab) {
        if (L.ui.now && L.ui.nowTab === tab) L.closeNow();
        else L.openNow(tab);
    }

    function buildDock() {
        const art = h("span", { class: "p-mini-art" });
        const title = h("span", { class: "p-mini-title" });
        const sub = h("span", { class: "p-mini-sub" });
        const play = h("button", { class: "p-icon p-mini-play", type: "button", "aria-label": "Играть", onclick: () => L.toggle() }, icon("play"));
        const mini = h("div", { class: "p-mini", hidden: true },
            h("button", { class: "p-mini-open", type: "button", onclick: () => L.openNow("cover"), "aria-label": "Открыть плеер" },
                art, h("span", { class: "p-mini-text" }, title, sub)),
            iconBtn("p-mini-btn", "queue", "Очередь", () => L.openNow("queue")),
            play,
            h("div", { class: "p-mini-progress" }, h("i")));
        const tabs = h("nav", { class: "p-tabs", "aria-label": "Разделы" }, TABS.map(([route, label, iconName, match]) =>
            h("button", { class: "p-tab", type: "button", dataset: { nav: match.join(",") }, onclick: () => L.go(route) },
                icon(iconName), h("span", null, label))));
        Object.assign(els, { miniArt: art, miniTitle: title, miniSub: sub, miniPlay: play, mini });
        return h("div", { class: "p-dock" }, mini, tabs);
    }

    // ------------------------------------------------------------------
    // «Сейчас играет»: телефон — во весь экран, компьютер — поверх центра
    // ------------------------------------------------------------------

    function buildNow() {
        const sheet = h("div", { class: "p-now", role: "dialog", "aria-modal": "true", "aria-label": "Сейчас играет", hidden: true },
            h("div", { class: "p-now-inner" }));
        els.now = sheet;
        els.nowInner = sheet.firstChild;
        return sheet;
    }

    function nowHeader(t, label, onBack) {
        return h("div", { class: "p-now-head" },
            iconBtn("p-now-down", onBack ? "back" : "down", onBack ? "К обложке" : "Свернуть", onBack || (() => L.closeNow())),
            h("div", { class: "p-now-src" }, h("span", null, label)),
            iconBtn("", "more", "Действия с треком", (e) => openTrackMenu(t, e)));
    }

    function nowControls(big) {
        const range = seekRange("p-now-range");
        const t0 = h("span", { class: "p-time" }, "0:00");
        const t1 = h("span", { class: "p-time" }, "0:00");
        const play = h("button", { class: "p-now-play", type: "button", "aria-label": "Играть или пауза", onclick: () => L.toggle() }, icon(L.player.audio.paused ? "play" : "pause"));
        Object.assign(els, { nowRange: range, nowT0: t0, nowT1: t1, nowPlay: play });
        return h("div", { class: "p-now-controls" + (big ? "" : " is-small") },
            h("div", { class: "p-now-progress" }, range, h("div", { class: "p-now-times" }, t0, t1)),
            h("div", { class: "p-now-buttons" },
                iconBtn("p-toggle" + (L.player.shuffle ? " is-on" : ""), "shuffle", "Перемешивать", () => L.setShuffle()),
                iconBtn("p-now-skip", "prev", "Предыдущий", () => L.prev()),
                play,
                iconBtn("p-now-skip", "next", "Следующий", () => L.next()),
                iconBtn("p-toggle" + (L.player.repeat !== "off" ? " is-on" : "") + (L.player.repeat === "one" ? " is-one" : ""), "repeat",
                    L.player.repeat === "one" ? "Повтор трека" : "Повтор", () => L.cycleRepeat())));
    }

    function renderNow() {
        const t = L.current();
        const open = L.ui.now && Boolean(t);
        const sheet = els.now;
        if (els.barLyr) els.barLyr.classList.toggle("is-on", open && L.ui.nowTab === "lyrics");
        if (els.barQue) els.barQue.classList.toggle("is-on", open && L.ui.nowTab === "queue");
        if (!open) {
            els.lyricLines = null;
            if (!sheet.hidden) {
                sheet.classList.remove("is-open");
                document.documentElement.classList.remove("p-locked");
                setTimeout(() => { if (!L.ui.now) sheet.hidden = true; }, 280);
            }
            return;
        }
        const tab = L.ui.nowTab;
        sheet.dataset.tab = tab;
        els.lyricLines = null;
        lastLine = -1;
        let content;
        if (tab === "lyrics") {
            content = h("div", { class: "p-now-lyrics" },
                nowHeader(t, t.title + " — " + (t.artist || ""), wide ? null : () => L.setNowTab("cover")),
                lyricsFull(t),
                nowControls(false));
        } else if (tab === "queue") {
            content = h("div", { class: "p-now-queue" },
                nowHeader(t, "Очередь", wide ? null : () => L.setNowTab("cover")),
                queueList(t));
        } else {
            const lyrCard = h("button", { class: "p-now-lyrcard", type: "button", onclick: () => L.setNowTab("lyrics") },
                h("span", { class: "p-now-lyrcard-head" }, "Текст"),
                h("span", { class: "p-now-lyrcard-lines" }, h("span", null, "Ищем текст…")));
            L.lyrics(t.path).then((body) => {
                if (L.current() !== t || !lyrCard.isConnected) return;
                const lines = body.synced && body.synced.length ? body.synced.map((s) => s.line).filter(Boolean)
                    : body.plain ? body.plain.split("\n").filter(Boolean) : [];
                clear(lyrCard.lastChild, lines.length ? lines.slice(0, 5).map((l) => h("span", null, l))
                    : h("span", null, body.reason || "Текста для этого трека нет."));
            });
            content = h("div", { class: "p-now-cover" },
                nowHeader(t, L.player.source ? "Из: " + L.player.source : "Сейчас играет"),
                h("div", { class: "p-now-art" }, L.cover(t.path, 600)),
                h("div", { class: "p-now-meta" },
                    h("div", { class: "p-now-text" },
                        h("div", { class: "p-now-title" }, t.title),
                        h("button", { class: "p-now-artist", type: "button", onclick: () => { L.closeNow(); L.go("artist", L.mainArtist(t)); } }, t.artist || "")),
                    iconBtn("p-act", "heart", "В избранное", () => L.act("В избранное"))),
                nowControls(true),
                h("div", { class: "p-now-foot" },
                    iconBtn("p-toggle", "lyrics", "Текст", () => L.setNowTab("lyrics")),
                    iconBtn("p-toggle", "queue", "Очередь", () => L.setNowTab("queue"))),
                lyrCard);
        }
        clear(els.nowInner, content);
        L.coverColor(t.path).then((rgb) => { if (L.current() === t) paint(els.app, rgb, "n"); });
        onTime();
        if (sheet.hidden) {
            sheet.hidden = false;
            if (!wide) document.documentElement.classList.add("p-locked");
            requestAnimationFrame(() => requestAnimationFrame(() => sheet.classList.add("is-open")));
        }
    }

    function lyricsFull(t) {
        const box = h("div", { class: "p-lyrics" }, h("p", { class: "p-lyrics-wait" }, "Ищем текст…"));
        L.lyrics(t.path).then((body) => {
            if (L.current() !== t || !box.isConnected) return;
            if (body.synced && body.synced.length) {
                const lines = body.synced.map((s) => h("button", {
                    class: "p-line", type: "button", dataset: { at: String(s.at) }, onclick: () => L.seek(s.at),
                }, s.line || "♪"));
                els.lyricLines = lines;
                clear(box, lines);
                onTime();
            } else if (body.plain) {
                clear(box, body.plain.split("\n").map((line) => h("p", { class: "p-line is-plain" }, line || " ")));
            } else {
                clear(box, h("p", { class: "p-lyrics-wait" }, body.reason || "Текста для этого трека нет."),
                    h("button", { class: "p-pill", type: "button", onclick: () => L.act("Выбрать текст") }, icon("search"), h("span", null, "Найти другой текст")));
            }
        });
        return box;
    }

    function queueList(t) {
        const next = L.upcoming();
        return h("div", { class: "p-queue" },
            h("h3", null, "Сейчас играет"),
            h("div", { class: "p-q-row is-now" }, L.cover(t.path, 48),
                h("span", { class: "p-tr-text" }, h("span", { class: "p-tr-name" }, t.title), h("span", { class: "p-tr-artist" }, t.artist || "")),
                bars()),
            h("h3", null, L.player.source ? "Далее из: " + L.player.source : "Далее"),
            next.length ? next.slice(0, 100).map(({ track, index }) =>
                h("button", { class: "p-q-row", type: "button", onclick: () => L.playAt(index) },
                    L.cover(track.path, 48),
                    h("span", { class: "p-tr-text" }, h("span", { class: "p-tr-name" }, track.title), h("span", { class: "p-tr-artist" }, track.artist || "")),
                    h("span", { class: "p-time" }, L.fmtTime(track.duration))))
                : h("p", { class: "p-dim" }, "Очередь закончится на этом треке."));
    }

    let lastLine = -1;
    function setRange(range, t0, t1, pos, dur) {
        if (!range || !range.isConnected || range.dataset.drag) return;
        const frac = dur ? pos / dur : 0;
        range.value = String(Math.round(frac * 1000));
        range.style.setProperty("--p", String(frac));
        t0.textContent = L.fmtTime(pos);
        t1.textContent = L.fmtTime(dur);
    }

    function onTime() {
        const pos = L.player.audio.currentTime || 0;
        const dur = L.duration();
        if (els.mini) els.mini.style.setProperty("--p", String(dur ? pos / dur : 0));
        setRange(els.barRange, els.barT0, els.barT1, pos, dur);
        setRange(els.nowRange, els.nowT0, els.nowT1, pos, dur);
        const lines = els.lyricLines;
        if (lines && lines[0] && lines[0].isConnected) {
            let idx = -1;
            for (let i = 0; i < lines.length; i++) {
                if (Number(lines[i].dataset.at) <= pos + 0.25) idx = i; else break;
            }
            if (idx !== lastLine) {
                lastLine = idx;
                lines.forEach((el, i) => {
                    el.classList.toggle("is-now", i === idx);
                    el.classList.toggle("is-past", i < idx);
                });
                const el = lines[idx];
                if (el) el.scrollIntoView({ block: "center", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
            }
        }
    }

    function onTrack() {
        const t = L.current();
        lastLine = -1;
        els.mini.hidden = !t;
        els.app.classList.toggle("has-track", Boolean(t));
        if (t) {
            clear(els.miniArt, L.cover(t.path, 40));
            els.miniTitle.textContent = t.title;
            els.miniSub.textContent = t.artist || "";
            clear(els.barArt, L.cover(t.path, 56));
            els.barTitle.textContent = t.title;
            els.barSub.textContent = t.artist || "";
            L.coverColor(t.path).then((rgb) => { if (L.current() === t) paint(els.app, rgb, "n"); });
        }
        markPlaying();
        renderRight();
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
        const setToggles = (root) => {
            if (!root) return;
            for (const b of root.querySelectorAll(".p-toggle")) {
                const name = b.querySelector(".ic") && b.querySelector(".ic").classList[1];
                if (name === "ic-shuffle") b.classList.toggle("is-on", L.player.shuffle);
                if (name === "ic-repeat") {
                    b.classList.toggle("is-on", L.player.repeat !== "off");
                    b.classList.toggle("is-one", L.player.repeat === "one");
                }
            }
        };
        setToggles(els.bar);
        setToggles(els.now);
    }

    // ------------------------------------------------------------------
    // Меню: у курсора на компьютере, лист снизу на телефоне
    // ------------------------------------------------------------------

    function openTrackMenu(track, event) {
        openMenu({ track, actions: L.trackActions(track) }, event);
    }

    function openMenu({ track, title, actions }, event) {
        closeMenu();
        const list = h("div", { class: "p-menu-list", role: "menu" }, actions.map((a) =>
            h("button", { class: "p-menu-item" + (a.danger ? " is-danger" : ""), type: "button", role: "menuitem", onclick: () => { closeMenu(); a.run(); } },
                icon(a.icon || "more"), h("span", null, a.label))));
        const head = track
            ? h("div", { class: "p-menu-head" }, L.cover(track.path, 48),
                h("span", { class: "p-tr-text" }, h("span", { class: "p-tr-name" }, track.title), h("span", { class: "p-tr-artist" }, track.artist || "")))
            : h("div", { class: "p-menu-head is-plain" }, h("span", { class: "p-tr-name" }, title || ""));
        const menu = h("div", { class: "p-menu" + (wide ? " is-pop" : " is-sheet") }, wide ? null : h("i", { class: "p-menu-grab" }), wide ? null : head, list);
        const layer = h("div", { class: "p-menu-layer" }, h("div", { class: "p-scrim", onclick: closeMenu }), menu);
        els.menu = layer;
        els.app.appendChild(layer);
        if (wide && event) {
            const rect = menu.getBoundingClientRect();
            menu.style.left = Math.max(8, Math.min(event.clientX, innerWidth - rect.width - 8)) + "px";
            menu.style.top = Math.max(8, Math.min(event.clientY, innerHeight - rect.height - 8)) + "px";
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
        setTimeout(() => layer.remove(), 180);
    }

    function onKey(e) {
        if (e.key !== "Escape") return;
        if (els.menu) closeMenu();
        else if (L.ui.now) L.closeNow();
    }

    // ------------------------------------------------------------------

    function mount(c) {
        ctx = c;
        const mq = matchMedia("(min-width: 900px)");
        wide = mq.matches;
        const onMq = () => { wide = mq.matches; fillLib(); renderRoute(); renderRight(); if (L.ui.now) renderNow(); };
        mq.addEventListener("change", onMq);
        ctx.cleanup(() => mq.removeEventListener("change", onMq));
        document.addEventListener("keydown", onKey);
        ctx.cleanup(() => document.removeEventListener("keydown", onKey));
        ctx.cleanup(() => document.documentElement.classList.remove("p-locked"));

        els.view = h("div", { class: "p-view" });
        els.main = h("main", { class: "p-panel p-main" }, els.view);
        els.rightTitle = h("h2", null, "Сейчас играет");
        els.rightBody = h("div", { class: "p-right-body" });
        els.right = h("aside", { class: "p-panel p-right", "aria-label": "Сейчас играет" },
            h("div", { class: "p-right-top" }, els.rightTitle, iconBtn("p-small", "close", "Скрыть панель", () => setRight(false))),
            els.rightBody);
        els.app = h("div", { class: "p-app" });
        els.bar = buildBar();
        clear(els.app, h("div", { class: "p-shell" }, buildLeft(), els.main, els.right), els.bar, buildDock(), buildNow());
        ctx.root.replaceChildren(els.app);
        setRight(rightOpen);

        ctx.on("data", () => { renderRoute(); renderRight(); });
        ctx.on("route", renderRoute);
        ctx.on("track", onTrack);
        ctx.on("state", onState);
        ctx.on("time", onTime);
        ctx.on("now", renderNow);
        ctx.on("queue", () => { renderRight(); if (L.ui.now && L.ui.nowTab === "queue") renderNow(); });
        renderRoute();
        onTrack();
        onState();
        renderNow();
    }

    function unmount() {
        if (viewCleanup) { viewCleanup(); viewCleanup = null; }
        for (const key of Object.keys(els)) delete els[key];
    }

    L.register({ id: "v2", name: "Пульт", themeColor: "#0e0e10", mount, unmount });
})();
